"""
The packages a course needs besides pyics, declared in its manifest as ``requires`` (PEP
508 requirements such as ``"numpy>=1.26"``), and the pyics version it needs, declared as
``requires_pyics`` (a version specifier such as ``">=0.1"``).

A course's tests run in the same interpreter as pyics, so the packages have to be
installed in pyics's own environment. This module checks whether they are, builds the
command that installs them there, decides which modules a course's task files may make
json_tricks import (see ``ModulePolicy``), and finds the packages a course uses, so that
its author can declare them.
"""

import ast
import importlib.metadata
import importlib.util
import re
import shlex
import subprocess
import sys
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from pyics.notebook import cells, loader
from pyics.util import json_handling as json

# always available to a course: pyics itself, and the standard library
_ALWAYS_ALLOWED = frozenset({"pyics", *sys.stdlib_module_names})


class MissingDependencyError(ImportError):
    """A task needs a package that isn't installed."""


class ModuleNotAllowedError(Exception):
    """A task file names a module for json_tricks to import that its course doesn't
    declare (see ``ModulePolicy``)."""


def pyics_version() -> str:
    """The installed version of pyics."""
    try:
        return importlib.metadata.version("pyics")
    except importlib.metadata.PackageNotFoundError:
        return "0"


def installed_version(distribution: str) -> str | None:
    """The installed version of ``distribution``, or None if it isn't installed."""
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


@dataclass(frozen=True)
class Problem:
    """A requirement of a course that isn't met."""

    # as declared, e.g. "numpy>=1.26", or "pyics>=0.2" for ``requires_pyics``
    requirement: str
    # why it isn't met, e.g. "not installed"
    reason: str
    # whether installing ``requirement`` meets it (pyics itself is upgraded instead)
    installable: bool = True

    def __str__(self) -> str:
        return f"{self.requirement} ({self.reason})"


def _applies(req: Requirement) -> bool:
    """Whether ``req`` applies to this environment, i.e. its marker (e.g.
    ``sys_platform == 'win32'``) is met, if it has one."""
    return req.marker is None or req.marker.evaluate()


def check(
    requires: Iterable[str] = (), requires_pyics: str | None = None
) -> list[Problem]:
    """The requirements of ``requires`` and ``requires_pyics`` that aren't met by the
    installed packages."""
    problems: list[Problem] = []
    if requires_pyics:
        try:
            spec = SpecifierSet(requires_pyics)
        except InvalidSpecifier:
            problems.append(
                Problem(
                    f"pyics{requires_pyics}", "not a valid version", installable=False
                )
            )
        else:
            if not spec.contains(pyics_version(), prereleases=True):
                problem = Problem(
                    f"pyics{requires_pyics}",
                    f"you have {pyics_version()}",
                    installable=False,
                )
                problems.append(problem)

    for raw in requires:
        try:
            req = Requirement(raw)
        except InvalidRequirement:
            problems.append(Problem(raw, "not a valid requirement", installable=False))
            continue
        if not _applies(req):
            continue
        version = installed_version(req.name)
        if version is None:
            problems.append(Problem(raw, "not installed"))
        elif not req.specifier.contains(version, prereleases=True):
            problems.append(Problem(raw, f"{version} is installed"))
    return problems


def manifest_requirements(manifest: Mapping[str, Any]) -> tuple[list[str], str | None]:
    """
    The ``requires`` and ``requires_pyics`` of a course ``manifest``.

    Raises
    ------
    ValueError
        If they aren't a list of requirements and a version specifier.
    """
    requires = manifest.get("requires") or []
    if not isinstance(requires, list) or not all(isinstance(r, str) for r in requires):
        raise ValueError("its requires must be a list of requirements, e.g. ['numpy']")
    requires_pyics = manifest.get("requires_pyics") or None
    if requires_pyics is not None and not isinstance(requires_pyics, str):
        raise ValueError("its requires_pyics must be a version specifier, e.g. '>=0.1'")
    return requires, requires_pyics


def check_manifest(manifest: Mapping[str, Any]) -> list[Problem]:
    """The requirements of the course ``manifest`` that aren't met (see ``check``)."""
    try:
        requires, requires_pyics = manifest_requirements(manifest)
    except ValueError as err:
        return [Problem("requirements", str(err), installable=False)]
    return check(requires, requires_pyics)


def format_command(command: Sequence[str]) -> str:
    """``command`` as it would be typed into the platform's shell."""
    if sys.platform == "win32":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def install_command(requirements: Sequence[str]) -> list[str]:
    """
    The command that installs ``requirements`` into pyics's own environment, where the
    tests of courses are run: pip, run by this interpreter, or uv pointed at it if pip
    isn't installed (e.g. in environments created by uv).
    """
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip", "install", *requirements]
    return ["uv", "pip", "install", "--python", sys.executable, *requirements]


def _tool_install_note(requirements: Sequence[str]) -> str | None:
    """How to keep ``requirements`` installed along pyics if it is installed as a
    tool by uv or pipx, which recreate its environment when it is upgraded."""
    parts = [part.lower() for part in Path(sys.prefix).parts]
    if "pipx" in parts and "venvs" in parts:
        return f"pyics is installed with pipx, so you can also run: pipx inject pyics {' '.join(requirements)}"
    if "tools" in parts and any("uv" in part for part in parts):
        withs = " ".join(f"--with {shlex.quote(r)}" for r in requirements)
        return (
            "pyics is installed as a uv tool: to keep these packages when it is "
            f"upgraded, reinstall it with: uv tool install pyics {withs}"
        )
    return None


def install_hint(problems: Iterable[Problem]) -> str:
    """How to meet the requirements of ``problems``, as one or more sentences."""
    problems = list(problems)
    installable = [p.requirement for p in problems if p.installable]
    hints: list[str] = []
    if installable:
        command = format_command(install_command(installable))
        hints.append(f"Install {_them(installable)} with: {command}")
        if note := _tool_install_note(installable):
            hints.append(note)
    if any(p.requirement.startswith("pyics") and not p.installable for p in problems):
        hints.append(
            "Upgrade pyics with: " + format_command(install_command(["-U", "pyics"]))
        )
    return " ".join(hints)


def _them(items: Collection[str]) -> str:
    return "it" if len(items) == 1 else "them"


def describe(problems: Iterable[Problem]) -> str:
    """``problems`` as a list in a sentence, e.g. "numpy (not installed) and pandas (1.0
    is installed)"."""
    texts = [str(p) for p in problems]
    if len(texts) <= 1:
        return "".join(texts)
    return ", ".join(texts[:-1]) + " and " + texts[-1]


def install(requirements: Sequence[str]) -> int:
    """Install ``requirements`` into pyics's environment (see ``install_command``),
    showing the installer's output. Returns its exit code."""
    try:
        return subprocess.run(install_command(requirements)).returncode
    except FileNotFoundError:  # no uv either
        return 127


# --- modules ---


def _distributions_by_module() -> dict[str, list[str]]:
    """The installed distributions that provide each top-level module."""
    return importlib.metadata.packages_distributions()


def import_names(requires: Iterable[str]) -> set[str]:
    """
    The top-level modules the requirements of ``requires`` provide, e.g. ``yaml`` for
    ``PyYAML``. Modules of distributions that aren't installed can't be looked up, so
    their names are guessed from the distributions' names (e.g. ``scikit_learn``).
    """
    wanted: set[str] = set()
    for raw in requires:
        try:
            name = Requirement(raw).name
        except InvalidRequirement:
            continue
        wanted.add(canonicalize_name(name))
    names = {name.replace("-", "_") for name in wanted}
    if not wanted:  # the lookup below takes a while
        return names
    for module, distributions in _distributions_by_module().items():
        if any(canonicalize_name(d) in wanted for d in distributions):
            names.add(module)
    return names


def distribution_of(module: str) -> str:
    """The name of the installed distribution that provides the top-level ``module``,
    or ``module`` itself if none does."""
    return _distributions_by_module().get(module, [module])[0]


def _top_level(module: str) -> str:
    return module.split(".")[0]


class ModulePolicy:
    """
    The modules that the task files of a course may name for json_tricks to import, to
    decode the objects stored in them (e.g. a test's expected value): those of pyics,
    of the standard library, and of the packages the course declares in its
    ``requires``. Undeclared modules are refused before anything is decoded, so that a
    task file can't make pyics import any module that happens to be installed.

    This is a safeguard against undeclared or misspelled modules, not a sandbox: the
    tests of a course run its code anyway.
    """

    def __init__(self, requires: Iterable[str] = ()) -> None:
        self.requires = list(requires)
        self._allowed = _ALWAYS_ALLOWED | import_names(self.requires)

    def allows(self, module: str | None) -> bool:
        """Whether a task file may name ``module`` (None for a class defined in a
        script, which can't be imported)."""
        return module is not None and _top_level(module) in self._allowed


def _requirement_for(module: str, requires: Sequence[str]) -> str:
    """The declared requirement that provides the top-level ``module``, or the name of
    the distribution that would."""
    for raw in requires:
        if module in import_names([raw]):
            return raw
    return distribution_of(module)


def check_task_data(
    data: Any, task_file: object, policy: ModulePolicy | None = None
) -> None:
    """
    Check that the task data ``data``, as read by ``json_handling.loads_plain`` from
    ``task_file``, can be decoded: that it names only modules ``policy`` allows, if
    given, and that what decoding it needs is installed.

    Raises
    ------
    ModuleNotAllowedError
        If it names a module the policy doesn't allow.
    MissingDependencyError
        If it needs a package that isn't installed.
    """
    tags = json.scan_tags(data)
    if policy is not None:
        for module in sorted(tags.modules, key=str):
            if policy.allows(module):
                continue
            if module is None:
                raise ModuleNotAllowedError(
                    f"The task file {task_file} holds an object of a class that was "
                    "defined in a script, which can't be loaded."
                )
            raise ModuleNotAllowedError(
                f"The task file {task_file} uses the module {module!r}, which its "
                f"course doesn't declare: add the package that provides it (e.g. "
                f"{distribution_of(_top_level(module))!r}) to the requires of the "
                "course manifest."
            )

    requires = policy.requires if policy is not None else []
    needed = {_top_level(m) for m in tags.modules if m is not None} | tags.packages
    for module in sorted(needed):
        if importlib.util.find_spec(module) is not None:
            continue
        requirement = _requirement_for(module, requires)
        raise MissingDependencyError(
            f"This course needs {requirement}, which isn't installed. "
            + install_hint([Problem(requirement, "not installed")]),
            name=module,
        )


# --- finding the packages a course uses ---


def imported_modules(code: str) -> set[str]:
    """The top-level modules ``code`` imports absolutely. Code that can't be parsed
    (e.g. an unfinished exercise) imports none."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(_top_level(alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(_top_level(node.module))
    return modules


def _python_code(code: str, file: object) -> str:
    """``code``, the contents stored for ``file``, as Python: for a notebook, the code
    of its cells, with magics turned into Python (see ``loader.python_source``)."""
    if not isinstance(file, str | PathLike) or not cells.is_notebook(file):
        return code
    try:
        return loader.python_source(cells.parse(code)).source
    except cells.NotebookError:
        return ""


# a notebook line that installs packages, e.g. ``%pip install numpy`` or ``!pip install
# -q numpy``
_PIP_INSTALL = re.compile(r"[%!]\s*(?:pip3?|python3? -m pip)\s+install\b")


def pip_install_lines(code: str) -> list[tuple[int, str]]:
    """
    The lines of the code cells of the notebook ``code`` (the contents of an ``.ipynb``
    file) that install packages, e.g. ``%pip install numpy``, with the number of their
    cell (see ``Cell.number``). They hint at the packages a course needs, which tests
    don't install (see ``loader``). Empty if ``code`` isn't a notebook.
    """
    try:
        code_cells = cells.code_cells(cells.parse(code))
    except cells.NotebookError:
        return []
    return [
        (cell.number, line.strip())
        for cell in code_cells
        for line in cell.source.splitlines()
        if _PIP_INSTALL.match(line.strip())
    ]


def used_modules(
    tasks: Mapping[str, Any], local: Collection[str] = ()
) -> dict[str, set[str]]:
    """
    The top-level modules besides pyics and the standard library that the task data
    of ``tasks`` (by task file name, as read by ``json_handling.loads_plain``) use: to
    decode the objects stored in them, and imported by their exercise and solution
    code (of notebooks, by their code cells). Modules of the course itself (``local``)
    are left out.

    Returns
    -------
    dict[str, set[str]]
        The modules, mapped to the names of the task files that use them.
    """
    used: dict[str, set[str]] = {}
    for name, data in tasks.items():
        tags = json.scan_tags(data)
        modules = {_top_level(m) for m in tags.modules if m is not None}
        modules |= tags.packages
        code_keys = (("exercise_code", "ex_files"), ("solution_code", "sol_files"))
        for key, files_key in code_keys:
            files = data.get(files_key)
            files = files if isinstance(files, list) else []
            for i, code in enumerate(data.get(key) or []):
                if isinstance(code, str):
                    file = files[i] if i < len(files) else None
                    modules |= imported_modules(_python_code(code, file))
        for module in modules - _ALWAYS_ALLOWED - set(local) - {"__future__"}:
            used.setdefault(module, set()).add(name)
    return used


def undeclared(
    used: Mapping[str, set[str]], requires: Iterable[str]
) -> dict[str, set[str]]:
    """
    The distributions that provide the modules of ``used`` (see ``used_modules``) but
    aren't declared in ``requires``.

    Returns
    -------
    dict[str, set[str]]
        The distributions' names, mapped to the names of the task files that use them.
    """
    declared = import_names(requires)
    missing: dict[str, set[str]] = {}
    for module, files in used.items():
        if module not in declared:
            missing.setdefault(distribution_of(module), set()).update(files)
    return missing


def is_newer(version: str, than: str) -> bool:
    """Whether the course version ``version`` is newer than ``than``. Versions that
    aren't PEP 440 versions are compared as text."""
    try:
        return Version(version) > Version(than)
    except InvalidVersion:
        return version > than
