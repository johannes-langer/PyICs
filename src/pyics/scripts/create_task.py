import builtins
import copy
import difflib
import inspect
import reprlib
import sys
from collections.abc import Collection, Mapping
from os import PathLike
from pathlib import Path, PurePath
from types import ModuleType
from typing import Any

from rich.console import Group, RenderableType
from rich.markup import escape
from rich.syntax import Syntax
from rich.text import Text

import pyics.util.json_handling as json
from pyics.app.cli import CLI
from pyics.app.key_listener import KeyListener
from pyics.app.prompts import (
    CodeInput,
    PathInput,
    PromptCancelled,
    PromptSession,
    SelectInput,
    TextInput,
)
from pyics.data.task import Task
from pyics.notebook import cells, loader, sections
from pyics.notebook.sections import Section
from pyics.tests import Test, check_specs, check_structure
from pyics.tests.capture import capture_stderr, capture_stdout
from pyics.tests.pytest_integration import run_tests
from pyics.tests.results import TestResult
from pyics.tests.tests import (
    INHERIT,
    Call,
    GeneralTest,
    Inherit,
    PrintTest,
    Raises,
    SimpleTest,
    SpecTest,
    StructureTest,
    check_source,
)
from pyics.util import paths
from pyics.util.terminal import fullscreen

_TOTAL_STEPS = 6
_INFO = "Creating a new task..."
_ENTER_TEXT = "Enter text"

# Test types the CLI can create. Their constructor parameters are prompted for by
# ``_prompt_test`` (a ``SimpleTest``'s by ``_prompt_simple_test``), and the first line
# of their docstring is shown as a hint.
_TEST_TYPES: list[type[Test]] = [
    SimpleTest,
    SpecTest,
    StructureTest,
    PrintTest,
    GeneralTest,
]
_NO_MORE_TESTS = "Done"

# What to do with a file or test of a task that is being edited. Keeping is preselected,
# so that confirming every prompt leaves the task as it was.
_KEEP = "Keep"
_UPDATE = "Update"
_REPLACE = "Replace"
_EDIT = "Edit"
_REMOVE = "Remove"
_TEST_HINTS = {
    _KEEP: "Keep the test as it is.",
    _EDIT: "Go through the test's parameters, prefilled with its current values.",
    _REMOVE: "Remove the test from the task.",
}
_HINT_ACTIONS = {
    _KEEP: "Keep the hint as it is.",
    _EDIT: "Change the hint, prefilled with its current text.",
    _REMOVE: "Remove the hint from the task.",
}
# lines of a diff shown when a file on disk differs from its contents in the task file
_MAX_DIFF_LINES = 30
# name of the variable that holds a parameter's current value when editing a test, if
# its repr can't be used to prefill the input
_CURRENT = "current"
# the options for the section of a task, and of a test, that isn't one of the
# notebook's sections
_NO_SECTION = "No section (whole notebook)"
_WHOLE_NOTEBOOK = "Whole notebook"
# the module a notebook with test information runs as (see ``_load_namespace``)
_INFO_MODULE = "pyics_test_information"
# what a ``SimpleTest`` calls (see ``_prompt_simple_test``)
_MODULE_CALLS = "Call functions of the module"
_OBJECT_CALLS = "Create an object and call its methods"
_CALL_HINTS = {
    _MODULE_CALLS: "Call functions (or classes) of the exercise file, one after the "
    "other.",
    _OBJECT_CALLS: "Create an object by calling a class (or function) of the exercise "
    "file, then call its methods, one after the other.",
}
_RAISES_HINT = (
    "Raises(ValueError) expects the call to raise a ValueError, also one the "
    "exercise file defines, e.g. Raises('InsufficientFunds')."
)
# where the source of a ``GeneralTest`` comes from (see ``_prompt_source``)
_KEEP_SOURCE = "Keep stored source"
_FROM_FILE = "From a file"
_TYPE_IT = "Type it"
_SOURCE_HINTS = {
    _KEEP_SOURCE: "Keep the test's source as it is.",
    _FROM_FILE: "Read the source from a Python file of pytest tests.",
    _TYPE_IT: "Type the source in, as pytest code.",
}
_SOURCE_DETAILS = (
    "pytest tests: functions named test..., or classes named Test..., which can use "
    "pytest's fixtures, and solution, load_solution, run_solution, exercise_path and "
    "capsys (see general_test.md)."
)
# what to do with a ``GeneralTest`` once it is checked against the solution
_EDIT_AGAIN = "Edit again"
_DISCARD = "Discard"
_CHECK_HINTS = {
    _KEEP: "Keep the test as it is.",
    _EDIT_AGAIN: "Change the test's source, and check it again.",
    _DISCARD: "Leave the test out of the task.",
}
_SKIP = "Skip"
# the results of checking a ``GeneralTest`` against a solution, by its source and the
# solution's path and contents, since flows are replayed (see ``PromptSession``)
_CHECKED: dict[tuple[Any, ...], list[TestResult]] = {}


def _confirm(s: PromptSession, label: str, *, default: bool = True) -> bool:
    prompt = SelectInput(label, ["yes", "no"])
    prompt.set_answer("yes" if default else "no")
    return s.ask(prompt) == "yes"


def _check_file(raw: str) -> Path:
    """Return ``raw`` as a path (with ``~`` expanded) if it is an existing file, raise
    otherwise."""
    path = Path(raw).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"no file at {raw!r}")
    return path


def _check_new_file(raw: str, files: Collection[Path]) -> Path:
    """Return ``raw`` as a path if it is an existing file that isn't one of ``files``,
    raise otherwise."""
    path = _check_file(raw)
    if paths.absolute(path) in {paths.absolute(f) for f in files}:
        raise ValueError(f"{raw!r} is already one of the task's files")
    return path


def _prompt_files(
    s: PromptSession, kind: str, kept: list[Path] | None = None
) -> list[Path]:
    """
    Interactively collect a list of existing ``kind`` (e.g. "exercise") file paths, one
    at a time. When editing a task, the list starts with the task's ``kept`` files, and
    adding more is not preselected.
    """
    files = list(kept or [])
    editing = kept is not None

    # we do not need a solution file necessarily, nor more than one exercise file.
    if files or kind == "solution":
        label = f"Add another {kind} file?" if files else f"Add a {kind} file?"
        if not _confirm(s, label, default=not editing):
            return files

    while True:
        files.append(
            s.ask(
                PathInput(f"{kind.capitalize()} file path"),
                lambda raw: _check_new_file(raw, files),
            )
        )
        if not _confirm(s, f"Add another {kind} file?", default=not editing):
            return files


def _read(path: PathLike) -> str | None:
    """The contents of the file at ``path``, or None if it can't be read."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return None


def _as_stored(path: PathLike, text: str | None) -> str | None:
    """``text``, the contents of the file at ``path``, as a task file stores them:
    normalized for a notebook (see ``cells.normalize``), so that e.g. its outputs don't
    count as changes. As it is if it can't be read as a notebook."""
    if text is None or not cells.is_notebook(path):
        return text
    try:
        return cells.normalize(text)
    except cells.NotebookError:
        return text


def _diff(stored: str, on_disk: str, path: PathLike | None = None) -> RenderableType:
    """How a file ``on_disk`` differs from its ``stored`` contents, as a unified diff of
    at most ``_MAX_DIFF_LINES`` lines. A notebook (by its ``path``) is compared by the
    text of its cells (see ``cells.as_cell_text``) rather than as JSON."""
    if path is not None and cells.is_notebook(path):
        try:
            stored, on_disk = cells.as_cell_text(stored), cells.as_cell_text(on_disk)
        except cells.NotebookError:
            pass  # compared as it is
        else:
            if stored == on_disk:
                return Text(
                    "Only its metadata differs (e.g. the tags of cells), not the text "
                    "of its cells.",
                    style="dim",
                )
    lines = list(
        difflib.unified_diff(
            stored.splitlines(), on_disk.splitlines(), "stored", "on disk", lineterm=""
        )
    )
    shown = Syntax("\n".join(lines[:_MAX_DIFF_LINES]), "diff", background_color="default")
    if len(lines) <= _MAX_DIFF_LINES:
        return shown
    more = Text(f"... {len(lines) - _MAX_DIFF_LINES} more lines", style="dim")
    return Group(shown, more)


def _notebook_sections(path: Path, contents: Mapping[Path, str]) -> dict[str, Section]:
    """The sections of the notebook at ``path`` as the task file will store it: the
    ``contents`` kept for it (by absolute path), if any, else the file on disk. None if
    it can't be read."""
    key = paths.absolute(path)
    text = contents[key] if key in contents else _read(path)
    if text is None:
        return {}
    try:
        return sections.find_sections(cells.parse(text))
    except cells.NotebookError:
        return {}


def _section_options(
    found: Mapping[str, Section], current: str | None
) -> tuple[dict[str, str], dict[str, str]]:
    """
    The sections ``found`` in a notebook as options to choose from, and the
    ``current`` section too if the notebook doesn't have it.

    Returns
    -------
    tuple[dict[str, str], dict[str, str]]
        The section names by option, and hints by option.
    """
    options: dict[str, str] = {}
    hints: dict[str, str] = {}
    for name, section in found.items():
        label = name if section.title == name else f"{name} ({section.title})"
        first, last = section.cells[0].number, section.cells[-1].number
        where = f"Cell {first}" if first == last else f"Cells {first} to {last}"
        options[label] = name
        hints[label] = f"{where} of the notebook."
    if current is not None and current not in found:
        label = f"{current} (not found in the notebook)"
        options[label] = current
        hints[label] = (
            "The notebook has no section of this name: its tests fail until a cell "
            f"starts it again, tagged '{sections.TAG_PREFIX}{current}'."
        )
    return options, hints


def _prompt_sections(
    s: PromptSession,
    ex_files: list[Path],
    contents: Mapping[Path, str],
    current: Mapping[Path, str],
) -> dict[Path, str]:
    """
    Ask which section of each of the exercise notebooks ``ex_files`` the task is about
    (see ``pyics.notebook.sections``), prefilled with its ``current`` section (by
    absolute path) when editing a task. Not asked for a notebook without sections,
    unless the task has one for it. Sections are looked up in the ``contents`` kept for
    a notebook (by path), else in the file on disk.

    Returns
    -------
    dict[Path, str]
        The section of each notebook that has one, by its path in ``ex_files``.
    """
    kept = {paths.absolute(path): text for path, text in contents.items()}
    chosen: dict[Path, str] = {}
    for path in ex_files:
        if not cells.is_notebook(path):
            continue
        found = _notebook_sections(path, kept)
        section = current.get(paths.absolute(path))
        if not found and section is None:
            continue
        options, hints = _section_options(found, section)
        hints[_NO_SECTION] = (
            "Its tests check the whole notebook, unless they have a section of their "
            "own."
        )
        prompt = SelectInput(
            f"Section of the task in {escape(str(path))}",
            [_NO_SECTION, *options],
            hints=hints,
        )
        prompt.set_answer(next((o for o, n in options.items() if n == section), ""))
        choice = s.ask(prompt)
        if choice != _NO_SECTION:
            chosen[path] = options[choice]
    return chosen


def _prompt_test_section(
    s: PromptSession,
    path: Path,
    task_section: str | None,
    found: Mapping[str, Section],
    current: str | None | Inherit,
) -> str | None | Inherit:
    """
    Ask for the section of the notebook at ``path`` a test is about: its task's section
    ``task_section`` (``INHERIT``, so it follows the task's section), the whole
    notebook (None), or another of the sections ``found`` in it. Prefilled with the
    test's ``current`` section. Not asked if the notebook has no sections, and neither
    the task nor the test has one: the test then inherits its task's section.
    """
    if not found and task_section is None and current is INHERIT:
        return INHERIT
    task_option = f"The task's section ({task_section or 'none: whole notebook'})"
    choices: dict[str, str | None | Inherit] = {
        task_option: INHERIT,
        _WHOLE_NOTEBOOK: None,
    }
    hints = {
        task_option: "The test checks the section the task is about, also once that "
        "is changed.",
        _WHOLE_NOTEBOOK: "The test checks the whole notebook, whichever section the "
        "task is about.",
    }
    # the task's own section is offered as such, unless the test has it already
    others = {
        name: section
        for name, section in found.items()
        if name != task_section or name == current
    }
    options, section_hints = _section_options(
        others, current if isinstance(current, str) else None
    )
    choices.update(options)
    hints.update(section_hints)

    prompt = SelectInput("section", list(choices), hints=hints)
    prompt.set_answer(next((o for o, c in choices.items() if c == current), ""))
    return choices[s.ask(prompt)]


def _edit_files(
    s: PromptSession, kind: str, current: list[Path], stored: Mapping[Path, str]
) -> tuple[list[Path], dict[Path, str], dict[Path, Path | None]]:
    """
    Ask what to do with each of the ``current`` ``kind`` files of a task that is being
    edited, then for more files, like ``_prompt_files``. A file can be kept with the
    contents ``stored`` in the task file, updated to store its (changed) contents on
    disk instead, replaced by another file, or removed from the task.

    Returns
    -------
    tuple[list[Path], dict[Path, str], dict[Path, Path | None]]
        The files; the stored contents to keep, by path; and what became of each of the
        ``current`` files, by its absolute path: the file itself, its replacement, or
        None if it was removed.
    """
    kept: list[Path] = []
    contents: dict[Path, str] = {}
    moved: dict[Path, Path | None] = {}

    for i, path in enumerate(current):
        in_task = _as_stored(path, stored.get(path))
        on_disk = _as_stored(path, _read(path))

        options, hints, details = [], {}, None
        if in_task is not None:
            options.append(_KEEP)
            hints[_KEEP] = "Keep the file, with the contents stored in the task file."
            if on_disk is None:
                details = Text(
                    "The file isn't on disk. It is created from the stored contents when "
                    "the task is loaded.",
                    style="dim",
                )
        if on_disk is not None and on_disk != in_task:
            options.append(_UPDATE)
            hints[_UPDATE] = "Keep the file, and store its contents on disk instead."
            if in_task is not None:
                details = Group(
                    Text("The file on disk differs from the stored contents:"),
                    _diff(in_task, on_disk, path),
                )
        if in_task is None and on_disk is None:
            details = Text(
                "The file isn't on disk, and the task file stores no contents for it.",
                style="red",
            )
        options += [_REPLACE, _REMOVE]
        hints[_REPLACE] = "Use another file in its place." + (
            " Its tests are moved to the new file." if kind == "exercise" else ""
        )
        hints[_REMOVE] = "Remove the file from the task. The file itself is kept." + (
            " Its tests need to be edited or removed." if kind == "exercise" else ""
        )

        label = f"{kind.capitalize()} file {escape(str(path))}"
        choice = s.ask(SelectInput(label, options, hints=hints, details=details))
        if choice == _REMOVE:
            moved[paths.absolute(path)] = None
            continue

        new_path = path
        if choice == _REPLACE:
            # not one of the files that are already kept, or still to be
            others = [*kept, *current[i + 1 :]]
            prompt = PathInput(f"New {kind} file path", initial=str(path))
            new_path = s.ask(
                prompt, lambda raw, others=others: _check_new_file(raw, others)
            )
        elif choice == _KEEP:
            contents[path] = stored[path]
        # updated or replaced files are read when the task file is written
        moved[paths.absolute(path)] = new_path
        kept.append(new_path)

    return _prompt_files(s, kind, kept), contents, moved


def _load_namespace(raw: str) -> tuple[dict[str, Any], str]:
    """
    Execute the Python file at ``raw``, or run the code cells of the notebook at it (see
    ``loader.run_notebook``), and return the variables it defines (excluding dunders and
    modules, and a notebook's stand-ins for ``get_ipython`` and ``display``).

    Returns
    -------
    tuple[dict[str, Any], str]
        The variables, and the errors of the notebook's cells as a problem to show (see
        ``_prompt_param``), or an empty string. An error in a cell doesn't stop the
        notebook's run, but the variables it would have defined are missing.
    """
    path = _check_file(raw)
    problem = ""
    if cells.is_notebook(path):
        previous = sys.modules.get(_INFO_MODULE)
        try:
            run = loader.run_notebook(path, module_name=_INFO_MODULE)
        finally:
            if previous is None:
                sys.modules.pop(_INFO_MODULE, None)
            else:
                sys.modules[_INFO_MODULE] = previous
        namespace = vars(run.module)
        if run.errors:
            more = f" (and {len(run.errors) - 1} more)" if len(run.errors) > 1 else ""
            problem = f"In the test information file, {run.errors[0].describe()}{more}"
    else:
        namespace = {}
        source = path.read_text(encoding="utf-8")
        exec(compile(source, str(path), "exec"), namespace)  # noqa: S102
    return {
        k: v
        for k, v in namespace.items()
        if not k.startswith("__")
        and not isinstance(v, ModuleType)
        # the loader's stand-ins, rather than what the notebook imports or defines
        and getattr(v, "__module__", None) != loader.__name__
    }, problem


def _check(test_cls: type[Test], param: inspect.Parameter, value: Any) -> Any:
    """Return ``value`` if it can be used for the ``test_cls`` constructor parameter
    ``param``, raise otherwise."""
    if test_cls is SpecTest and param.name == "expected":
        # e.g. the solution's function itself, stored as its spec dict. Also validates
        # spec dicts now, rather than when the test is constructed.
        value = check_specs.to_spec_dict(value)
    if test_cls is StructureTest and param.name == "rules":
        value = check_structure.validate_rules(value)
    if param.annotation is str and not isinstance(value, str):
        raise TypeError(f"expected str, got {type(value).__name__}")
    if param.annotation == list[str] and not (
        isinstance(value, list) and all(isinstance(v, str) for v in value)
    ):
        raise TypeError(f"expected a list of str, got {reprlib.repr(value)}")
    if param.annotation is bool and not isinstance(value, bool):
        raise TypeError(f"expected True or False, got {reprlib.repr(value)}")
    if param.annotation is list and not isinstance(value, (list, tuple)):
        raise TypeError(f"expected a list of arguments, got {reprlib.repr(value)}")
    if param.annotation is dict and not (
        isinstance(value, dict) and all(isinstance(k, str) for k in value)
    ):
        raise TypeError(
            f"expected a dict of keyword arguments, got {reprlib.repr(value)}"
        )
    # checked now, rather than when the task file is written after all other fields
    json.dumps(value)
    return value


def _evaluates_to(code: str, value: Any) -> bool:
    """Whether ``code`` evaluates to ``value``, e.g. whether a ``repr`` is valid Python
    that can prefill an input for ``value``. ``Raises`` is in scope, as it is for the
    expected values of a ``SimpleTest``."""
    try:
        return bool(eval(code, {"__builtins__": builtins, "Raises": Raises}) == value)
    except Exception:  # noqa: BLE001
        return False


def _prompt_param(
    s: PromptSession,
    test_cls: type[Test],
    param: inspect.Parameter,
    symbols: dict[str, Any],
    problem: str,
    current: Any = inspect.Parameter.empty,
    *,
    label: str | None = None,
) -> Any:
    """
    Ask for the value of the ``test_cls`` constructor parameter ``param``, prefilled with
    its ``current`` value when editing a test, otherwise with its default: plain text for
    ``str`` parameters (on several lines for the expected output of a ``PrintTest``),
    otherwise a Python expression, evaluated with ``symbols`` in scope (and ``Raises``,
    for the expected value of a ``SimpleTest``). ``problem``, if not empty, is shown
    below the input. The prompt is labelled ``label``, by default the parameter's name.
    """
    label = param.name if label is None else label
    lines = [Text(problem, style="red")] if problem else []
    editing = current is not param.empty
    value = current if editing else param.default
    has_value = value is not param.empty

    if param.annotation is str:
        str_symbols = {k: v for k, v in symbols.items() if isinstance(v, str)}
        if str_symbols:
            hints = {k: reprlib.repr(v) for k, v in str_symbols.items()}
            prompt = SelectInput(label, [*str_symbols, _ENTER_TEXT], hints=hints)
            if editing:  # the current value is prefilled in the text input
                prompt.set_answer(_ENTER_TEXT)
            choice = s.ask(prompt)
            if choice != _ENTER_TEXT:
                return str_symbols[choice]

        initial = value if isinstance(value, str) else ""
        details = Group(*lines) if lines else None
        multiline = test_cls is PrintTest and param.name == "expected"
        return s.ask(
            TextInput(label, multiline=multiline, initial=initial, details=details)
        )

    initial = repr(value) if has_value else ""
    if has_value and not _evaluates_to(initial, value):
        # e.g. an object whose repr isn't valid Python: prefilled by referencing it
        symbols = {**symbols, _CURRENT: value}
        initial = _CURRENT

    scope: dict[str, Any] = {"__builtins__": builtins}
    if test_cls is StructureTest and param.name == "rules":
        concepts = ", ".join(check_structure.CONCEPTS)
        lines.append(Text(f"Concepts: {concepts}", style="dim"))
    if test_cls is SimpleTest and param.name == "expected":
        scope["Raises"] = Raises
        lines.append(Text(_RAISES_HINT, style="dim"))
    if symbols:
        lines.append(Text("Variables in scope:", style="dim"))
        lines.extend(
            Text(f"  {k} = {reprlib.repr(v)}", style="dim") for k, v in symbols.items()
        )
    prompt = CodeInput(label, initial=initial, details=Group(*lines) if lines else None)
    scope.update(symbols)
    return s.ask(prompt, lambda raw: _check(test_cls, param, eval(raw, dict(scope))))


def _prompt_file_path(
    s: PromptSession, ex_files: list[Path], current: Test | None = None
) -> Path:
    """Ask which of ``ex_files`` a test is about, prefilled with the file of the test
    ``current`` when editing it."""
    prompt = SelectInput("file_path", [str(f) for f in ex_files])
    if current is not None:
        options = {paths.absolute(f): str(f) for f in ex_files}
        prompt.set_answer(options.get(paths.absolute(current.file_path), ""))
    return Path(s.ask(prompt)).absolute()


def _prompt_test(
    s: PromptSession,
    test_cls: type[Test],
    ex_files: list[Path],
    current: Test | None = None,
    *,
    task_sections: Mapping[Path, str] | None = None,
    contents: Mapping[Path, str] | None = None,
    sol_files: list[Path] | None = None,
) -> Test | None:
    """
    Interactively define a test of type ``test_cls``, by asking for each parameter of its
    constructor. When editing the test ``current``, each one is prefilled with its
    current value.

    The user may reference a Python file or a notebook first: its variables whose names
    match a parameter are used for it directly, and all of them are in scope for the
    expressions entered for the other parameters. If cells of the notebook raised an
    error, the prompts for the other parameters say so. ``file_path`` is always one of
    ``ex_files``.

    A test of a notebook can be about a ``section`` of it (see ``_prompt_test_section``):
    by default the task's, i.e. its section in ``task_sections`` (by path). Sections are
    looked up in the ``contents`` kept for the notebook (by path), else in the file on
    disk.

    A ``SimpleTest`` is asked for by ``_prompt_simple_test``. A ``GeneralTest``'s
    source is asked for by ``_prompt_source``, and the test is then checked against
    one of the solution files ``sol_files`` (see ``_check_general_test``).

    Returns
    -------
    Test | None
        The test, or None if a ``GeneralTest`` was discarded once it was checked.
    """
    task_sections = {paths.absolute(p): n for p, n in (task_sections or {}).items()}
    kept = {paths.absolute(p): text for p, text in (contents or {}).items()}
    symbols: dict[str, Any] = {}
    info_problem = ""
    if _confirm(s, "Reference a file with test information?", default=current is None):
        symbols, info_problem = s.ask(
            PathInput("Test information file"), _load_namespace
        )
    if test_cls is SimpleTest:
        assert current is None or isinstance(current, SimpleTest)
        return _prompt_simple_test(s, ex_files, current, symbols, info_problem)

    kwargs: dict[str, Any] = {}
    for param in inspect.signature(test_cls).parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        value = getattr(current, param.name, param.empty) if current else param.empty

        if param.name == "file_path":
            kwargs[param.name] = _prompt_file_path(s, ex_files, current)
            continue

        if param.name == "section":
            # a test that isn't given one inherits its task's, storing nothing
            path = kwargs["file_path"]
            if cells.is_notebook(path):
                current_section = INHERIT if value is param.empty else value
                section = _prompt_test_section(
                    s,
                    path,
                    task_sections.get(paths.absolute(path)),
                    _notebook_sections(path, kept),
                    current_section,
                )
                if section is not INHERIT:
                    kwargs[param.name] = section
            continue

        problem = info_problem
        if test_cls is GeneralTest and param.name == "source":
            source = symbols.get("source")
            try:
                kwargs[param.name] = check_source(source)  # type: ignore[arg-type]
                continue
            except (TypeError, ValueError, SyntaxError) as err:
                if param.name in symbols:
                    problem = f"'source' from the test information file: {err}"
            stored = None if value is param.empty else value
            kwargs[param.name] = _prompt_source(s, stored, stored=True, problem=problem)
            continue

        if param.name in symbols:
            try:
                kwargs[param.name] = _check(test_cls, param, symbols[param.name])
                continue
            except (TypeError, ValueError) as err:
                problem = f"'{param.name}' from the test information file: {err}"
        kwargs[param.name] = _prompt_param(s, test_cls, param, symbols, problem, value)

    test = test_cls(**kwargs)
    if isinstance(test, GeneralTest):
        return _check_general_test(s, test, sol_files or [], task_sections)
    return test


def _check_dotted_name(raw: str) -> str:
    """Return ``raw`` if it can name an attribute, maybe dotted (e.g. ``Stack`` or
    ``shapes.Circle``), raise otherwise."""
    if not raw or not all(part.isidentifier() for part in raw.split(".")):
        raise ValueError(f"{raw!r} is not a name, e.g. Stack or shapes.Circle")
    return raw


def _prompt_constructor(
    s: PromptSession, symbols: Mapping[str, Any], current: str | None, problem: str
) -> str:
    """
    Ask for the name of the class or function that creates the object whose methods a
    ``SimpleTest`` calls: one of the classes and functions of the test information
    file (its ``symbols``), or a name that is entered. A ``constructor`` variable of
    the file that is a name is used directly. Prefilled with the ``current`` name when
    editing a test.
    """
    if isinstance(name := symbols.get("constructor"), str):
        try:
            return _check_dotted_name(name)
        except ValueError as err:
            problem = f"'constructor' from the test information file: {err}"
    found = {
        k: v for k, v in symbols.items() if inspect.isclass(v) or inspect.isfunction(v)
    }
    if found:
        hints = {
            k: f"{'Class' if inspect.isclass(v) else 'Function'} of the test "
            "information file."
            for k, v in found.items()
        }
        prompt = SelectInput("constructor", [*found, _ENTER_TEXT], hints=hints)
        if current is not None:
            prompt.set_answer(current if current in found else _ENTER_TEXT)
        choice = s.ask(prompt)
        if choice != _ENTER_TEXT:
            return choice
    lines = [Text(problem, style="red")] if problem else []
    lines.append(
        Text(
            "The class (or function) of the exercise file that creates the object.",
            style="dim",
        )
    )
    prompt = TextInput("constructor", initial=current or "", details=Group(*lines))
    return s.ask(prompt, _check_dotted_name)


def _prompt_simple_test(
    s: PromptSession,
    ex_files: list[Path],
    current: SimpleTest | None,
    symbols: dict[str, Any],
    problem: str,
) -> SimpleTest:
    """
    Interactively define a ``SimpleTest``: its name and file, whether it calls the
    functions of the module or the methods of an object (then also the constructor
    and its arguments), the calls, one after the other, and whether it is traceable
    and isolated. A call is stored as a single call, several as a sequence.

    Each call after the first is prefilled with the one before, so that a call is
    repeated by confirming each of its prompts. When editing the test ``current``,
    everything is prefilled with its current values, including its calls. The
    variables ``symbols`` of the test information file are used as in
    ``_prompt_test``, those for the calls only for the first one; ``problem`` is shown
    below the prompts, if not empty.
    """
    empty = inspect.Parameter.empty

    def ask(
        name: str,
        annotation: Any,
        default: Any = empty,
        value: Any = empty,
        *,
        from_symbols: bool = True,
        label: str | None = None,
    ) -> Any:
        param = inspect.Parameter(
            name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation
        )
        shown = problem
        if from_symbols and name in symbols:
            try:
                return _check(SimpleTest, param, symbols[name])
            except (TypeError, ValueError) as err:
                shown = f"'{name}' from the test information file: {err}"
        return _prompt_param(s, SimpleTest, param, symbols, shown, value, label=label)

    def current_value(name: str) -> Any:
        return getattr(current, name) if current is not None else empty

    name = ask("name", str, value=current_value("name"))
    file_path = _prompt_file_path(s, ex_files, current)

    kind = SelectInput("What does the test call?", list(_CALL_HINTS), hints=_CALL_HINTS)
    if current is not None and current.constructor is not None:
        kind.set_answer(_OBJECT_CALLS)
    constructor: dict[str, Any] = {}
    if s.ask(kind) == _OBJECT_CALLS:
        stored = current.constructor if current is not None else None
        constructor["constructor"] = _prompt_constructor(s, symbols, stored, problem)
        has_stored = stored is not None
        for arg, annotation, default in (
            ("constructor_args", list, []),
            ("constructor_kwargs", dict, {}),
        ):
            value = current_value(arg) if has_stored else empty
            constructor[arg] = ask(arg, annotation, default, value)

    stored_calls = current.calls() if current is not None else []
    calls: list[Call] = []
    while True:
        i = len(calls)
        # prefilled with the stored call, or the call before
        previous = stored_calls[i] if i < len(stored_calls) else None
        previous = previous or (calls[-1] if calls else None)
        values = previous or Call(empty, empty, empty, empty)  # type: ignore[arg-type]
        first = i == 0

        def label(param: str, first: bool = first, i: int = i) -> str | None:
            return None if first else f"call {i + 1}: {param}"

        call = Call(
            ask(
                "function_name",
                str,
                value=values.name,
                from_symbols=first,
                label=label("function_name"),
            ),
            ask(
                "function_args",
                list,
                [],
                values.args,
                from_symbols=first,
                label=label("function_args"),
            ),
            ask(
                "function_kwargs",
                dict,
                {},
                values.kwargs,
                from_symbols=first,
                label=label("function_kwargs"),
            ),
            ask(
                "expected",
                Any,
                value=values.expected,
                from_symbols=first,
                label=label("expected"),
            ),
        )
        calls.append(call)
        more = len(calls) < len(stored_calls)
        if not _confirm(s, "Add another call?", default=more):
            break

    traceable = ask("traceable", bool, True, current_value("traceable"))
    isolated = ask("isolated", bool, False, current_value("isolated"))

    if len(calls) == 1:  # stored as a single call
        (call,) = calls
        function_name, args, kwargs, expected = call
    else:
        function_name = [c.name for c in calls]
        args = [list(c.args) for c in calls]
        kwargs = [c.kwargs for c in calls]
        expected = [c.expected for c in calls]
    return SimpleTest(
        name,
        file_path,
        function_name,
        expected,
        function_args=list(args),
        function_kwargs=kwargs,
        traceable=traceable,
        isolated=isolated,
        **constructor,
    )


def _read_source(raw: str) -> str:
    """The source of a ``GeneralTest`` in the Python file at ``raw``, if it is one
    (see ``check_source``), raise otherwise."""
    return check_source(_check_file(raw).read_text(encoding="utf-8"))


def _prompt_source(
    s: PromptSession,
    current: str | None = None,
    *,
    stored: bool = False,
    problem: str = "",
) -> str:
    """
    Ask for the source of a ``GeneralTest`` (see ``check_source``): from a Python file,
    or typed in. With a ``current`` source, the stored one when editing a test
    (``stored``), it can be kept, which is preselected; otherwise typing it in is
    preselected, prefilled with it. ``problem``, if not empty, is shown below the
    prompts.
    """
    lines = [Text(problem, style="red")] if problem else []
    lines.append(Text(_SOURCE_DETAILS, style="dim"))
    options = [_FROM_FILE, _TYPE_IT]
    if stored and current is not None:
        options.insert(0, _KEEP_SOURCE)
    prompt = SelectInput("source", options, hints=_SOURCE_HINTS, details=Group(*lines))
    if current is not None:
        prompt.set_answer(_KEEP_SOURCE if stored else _TYPE_IT)
    choice = s.ask(prompt)
    if choice == _KEEP_SOURCE and current is not None:
        return current
    if choice == _FROM_FILE:
        return s.ask(PathInput("Test source file", details=Group(*lines)), _read_source)
    typed = CodeInput(
        "source", multiline=True, initial=current or "", details=Group(*lines)
    )
    return s.ask(typed, check_source)


def _choose_solution(
    s: PromptSession, test: Test, sol_files: list[Path]
) -> Path | None:
    """The solution file to check ``test`` against: the only one of ``sol_files`` on
    disk, or the one with the name of the test's file, otherwise the one the user
    chooses. None if there is none, or the user skips the check."""
    candidates = [f for f in sol_files if Path(f).is_file()]
    if len(candidates) <= 1:
        return candidates[0] if candidates else None
    same_name = [f for f in candidates if Path(f).name == Path(test.file_path).name]
    if len(same_name) == 1:
        return same_name[0]
    prompt = SelectInput(
        "Solution file to check the test against",
        [*(str(f) for f in candidates), _SKIP],
        hints={_SKIP: "Don't check the test."},
    )
    choice = s.ask(prompt)
    return None if choice == _SKIP else Path(choice)


def _run_against(
    test: GeneralTest, solution: Path, section: str | None
) -> list[TestResult]:
    """
    The results of running ``test`` on ``solution`` instead of its exercise file,
    about its ``section`` of a notebook, if any. Their output is captured, so that it
    doesn't show in the CLI. Cached by the test's source and the solution's path and
    contents, since flows are replayed.
    """
    path = paths.absolute(solution)
    key = (test.source, test.isolated, path, path.read_bytes(), section)
    if key not in _CHECKED:
        checked = copy.copy(test)
        checked.file_path = path
        sections = {path: section} if section is not None else None
        with capture_stdout(), capture_stderr():
            _CHECKED[key] = run_tests([checked], sections=sections)
    return _CHECKED[key]


def _check_general_test(
    s: PromptSession,
    test: GeneralTest,
    sol_files: list[Path],
    task_sections: Mapping[Path, str],
) -> GeneralTest | None:
    """
    Run ``test`` on one of the solution files ``sol_files`` (see ``_choose_solution``),
    in its task's section (``task_sections``, by absolute path) if it has one, and ask
    whether to keep it, edit its source again (then it is checked again), or discard
    it, showing how many of its tests passed, and the failures.

    Returns
    -------
    GeneralTest | None
        The test, or None if it is discarded.
    """
    solution = _choose_solution(s, test, sol_files)
    if solution is None:
        return test
    section = task_sections.get(paths.absolute(test.file_path))
    while True:
        try:
            results = _run_against(test, solution, section)
        except OSError as err:  # e.g. the solution file was removed meanwhile
            details: RenderableType = Text(f"Could not check the test: {err}", "red")
            summary = "not checked"
        else:
            calls = [r for r in results if r.when == "call"]
            counts = {
                outcome: sum(r.outcome == outcome for r in calls)
                for outcome in ("passed", "failed", "skipped")
            }
            summary = ", ".join(
                f"{n} {outcome}"
                for outcome, n in counts.items()
                if n or outcome != "skipped"
            )
            failures: list[RenderableType] = []
            for r in calls:
                if r.outcome == "passed":
                    continue
                failures.append(Text(f"{r.nodeid} {r.outcome}", style="bold red"))
                failures.append(r.message_text)
            details = (
                Group(*failures)
                if failures
                else Text("All of its tests pass on the solution.", style="dim")
            )
        label = f"Checked on {escape(str(solution))}: {summary}"
        options = [_KEEP, _EDIT_AGAIN, _DISCARD]
        prompt = SelectInput(label, options, hints=_CHECK_HINTS, details=details)
        choice = s.ask(prompt)
        if choice == _KEEP:
            return test
        if choice == _DISCARD:
            return None
        test = copy.copy(test)
        test.source = _prompt_source(s, test.source)


# parameters of a ``SimpleTest`` that ``_describe_test`` shows as its calls
_CALL_PARAMS = {
    "function_name",
    "function_args",
    "function_kwargs",
    "expected",
    "constructor",
    "constructor_args",
    "constructor_kwargs",
}


def _describe_test(test: Test, problem: str = "") -> RenderableType:
    """The type and parameters of ``test``, below ``problem`` if it isn't empty. The
    calls of a ``SimpleTest`` are shown as code (see ``SimpleTest.describe_calls``),
    and the source of a ``GeneralTest`` by its number of lines."""
    lines = [Text(problem, style="red")] if problem else []
    lines.append(Text(type(test).__name__, style="dim"))
    for param in inspect.signature(type(test)).parameters.values():
        if isinstance(test, SimpleTest) and param.name in _CALL_PARAMS:
            if param.name == "function_name":
                lines.append(Text("  calls:", style="dim"))
                lines.extend(
                    Text(f"    {line}", style="dim") for line in test.describe_calls()
                )
            continue
        value = getattr(test, param.name, param.empty)
        if value is param.empty or value is INHERIT:
            continue
        if isinstance(test, GeneralTest) and param.name == "source":
            text = f"{len(str(value).splitlines())} lines of pytest code"
        elif isinstance(value, PurePath):
            text = str(value)
        else:
            text = reprlib.repr(value)
        lines.append(Text(f"  {param.name} = {text}", style="dim"))
    return Group(*lines)


def _prompt_tests(
    s: PromptSession,
    ex_files: list[Path],
    current: list[Test] | None = None,
    *,
    task_sections: Mapping[Path, str] | None = None,
    contents: Mapping[Path, str] | None = None,
    sol_files: list[Path] | None = None,
) -> list[Test]:
    """
    Interactively define any number of tests, choosing the test type for each. When
    editing a task, its ``current`` tests are kept, edited or removed first, and adding
    more is not preselected. A test whose file isn't one of ``ex_files`` can't be kept
    as it is. ``task_sections``, ``contents`` and ``sol_files`` are passed on to
    ``_prompt_test``.
    """
    tests: list[Test] = []
    ex = {paths.absolute(f) for f in ex_files}
    for i, test in enumerate(current or [], 1):
        options, problem = [_KEEP, _EDIT, _REMOVE], ""
        if paths.absolute(test.file_path) not in ex:
            options.remove(_KEEP)
            problem = (
                f"Its file {test.file_path} isn't one of the task's exercise files, so "
                "the test needs to be edited or removed."
            )
        label = f"Test {i} of {len(current or [])}: {escape(str(test.name))}"
        details = _describe_test(test, problem)
        choice = s.ask(SelectInput(label, options, hints=_TEST_HINTS, details=details))
        if choice == _KEEP:
            tests.append(test)
        elif choice == _EDIT:
            edited = _prompt_test(
                s,
                type(test),
                ex_files,
                current=test,
                task_sections=task_sections,
                contents=contents,
                sol_files=sol_files,
            )
            if edited is not None:
                tests.append(edited)

    test_types = {cls.__name__: cls for cls in _TEST_TYPES}
    hints = {
        name: (inspect.getdoc(cls) or "").splitlines()[0]
        for name, cls in test_types.items()
    }
    while True:
        hints[_NO_MORE_TESTS] = f"Finish adding tests ({len(tests)} added so far)."
        prompt = SelectInput(
            "Which kind of test do you want to add?",
            [*test_types, _NO_MORE_TESTS],
            hints=hints,
        )
        if current is not None:
            prompt.set_answer(_NO_MORE_TESTS)
        kind = s.ask(prompt)
        if kind == _NO_MORE_TESTS:
            return tests
        test = _prompt_test(
            s,
            test_types[kind],
            ex_files,
            task_sections=task_sections,
            contents=contents,
            sol_files=sol_files,
        )
        if test is not None:
            tests.append(test)


def _repoint(test: Test, moved: Mapping[Path, Path | None]) -> Test:
    """``test``, or a copy of it testing the file its file was replaced by (see
    ``_edit_files``)."""
    new = moved.get(paths.absolute(test.file_path))
    if new is None:
        return test
    test = copy.copy(test)  # the task's own test is kept, since flows are replayed
    test.file_path = new
    return test


def _check_hint(hint: str) -> str:
    """Return ``hint`` if it can be used as a hint, raise otherwise."""
    if not hint.strip():
        raise ValueError("the hint may not be empty")
    return hint


def _prompt_hints(s: PromptSession, current: list[str] | None = None) -> list[str]:
    """
    Interactively define any number of hints, which the student can reveal one at a
    time, in order. When editing a task, its ``current`` hints are kept, edited or
    removed first. Adding a hint is never preselected, so confirming every prompt adds
    none.
    """
    hints: list[str] = []
    for i, hint in enumerate(current or [], 1):
        label = f"Hint {i} of {len(current or [])}"
        details = Text(hint, style="dim")
        options = [_KEEP, _EDIT, _REMOVE]
        choice = s.ask(
            SelectInput(label, options, hints=_HINT_ACTIONS, details=details)
        )
        if choice == _KEEP:
            hints.append(hint)
        elif choice == _EDIT:
            prompt = TextInput(f"Hint {len(hints) + 1}", multiline=True, initial=hint)
            hints.append(s.ask(prompt, _check_hint))

    while _confirm(s, "Add another hint?" if hints else "Add a hint?", default=False):
        prompt = TextInput(f"Hint {len(hints) + 1}", multiline=True)
        hints.append(s.ask(prompt, _check_hint))
    return hints


def _check_name(name: str, taken_names: Collection[str]) -> str:
    """Return ``name`` if it can be used for a task (or course), raise otherwise."""
    if not name:
        raise ValueError("the name may not be empty")
    if name in taken_names:
        raise ValueError(f"the course already has a task named {name!r}")
    return name


def _task_flow(
    s: PromptSession,
    *,
    taken_names: Collection[str] = (),
    current: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """
    The full task-creation flow. Replayed by ``PromptSession`` whenever the user goes
    back, so it must ask the same prompts given the same answers. The task name may not
    be one of ``taken_names``.

    With ``current``, the fields of an existing task (see ``Task.read_task_json``), the
    task is edited instead: every prompt is prefilled with its current value, so that
    confirming all of them leaves the task as it was. Changes to its exercise files
    carry over to its tests: those of a replaced file test the new file, and those of a
    removed file need to be edited or removed. The same goes for the task's sections.

    Returns
    -------
    dict[str, Any]
        Keyword arguments for ``Task.create_task_json``.
    """
    s.cli.update_progress(0 / _TOTAL_STEPS)
    initial = current["name"] if current is not None else ""
    name = s.ask(
        TextInput("Task name", initial=initial), lambda n: _check_name(n, taken_names)
    )

    s.cli.update_progress(1 / _TOTAL_STEPS)
    initial = current["instructions"] if current is not None else ""
    instructions = s.ask(TextInput("Instructions", multiline=True, initial=initial))

    s.cli.update_progress(2 / _TOTAL_STEPS)
    hints = _prompt_hints(s, current["help"] if current is not None else None)

    file_contents: dict[Path, str] = {}
    moved: dict[Path, Path | None] = {}

    s.cli.update_progress(3 / _TOTAL_STEPS)
    if current is None:
        ex_files = _prompt_files(s, "exercise")
    else:
        ex_files, ex_contents, moved = _edit_files(
            s, "exercise", current["ex_files"], current["file_contents"]
        )
        file_contents.update(ex_contents)
    # those of replaced notebooks are offered for the new notebook
    current_sections: dict[Path, str] = {}
    for path, section in (current or {}).get("sections", {}).items():
        new_path = moved.get(paths.absolute(path), path)
        if new_path is not None:
            current_sections[paths.absolute(new_path)] = section
    task_sections = _prompt_sections(s, ex_files, file_contents, current_sections)

    s.cli.update_progress(4 / _TOTAL_STEPS)
    if current is None:
        sol_files = _prompt_files(s, "solution")
    else:
        sol_files, sol_contents, _ = _edit_files(
            s, "solution", current["sol_files"], current["file_contents"]
        )
        file_contents.update(sol_contents)

    s.cli.update_progress(5 / _TOTAL_STEPS)
    current_tests = None
    if current is not None:
        current_tests = [_repoint(test, moved) for test in current["tests"]]
    tests = _prompt_tests(
        s,
        ex_files,
        current_tests,
        task_sections=task_sections,
        contents=file_contents,
        sol_files=sol_files,
    )

    s.cli.update_progress(6 / _TOTAL_STEPS)
    return {
        "name": name,
        "instructions": instructions,
        "ex_files": ex_files,
        "sol_files": sol_files,
        "tests": tests or None,
        "file_contents": file_contents,
        "help": hints,
        "sections": task_sections,
    }


def _create_task(fp: PathLike | None = None) -> None:
    """
    Console script to interactively create a task file.

    Parameters
    ----------
    fp: PathLike
        Output path for the resulting ``.pyicstask.json`` file.
    """
    with fullscreen():
        cli = CLI()
        cli()
        cli.update_info(_INFO)

        try:
            with KeyListener() as q:
                fields = PromptSession(cli, q, info=_INFO).run(_task_flow)

            if not fp:
                fp = Path(f"./{fields['name']}")
            fp = Task.create_task_json(fp, **fields)
            cli.update_info(f"Task written to {escape(str(fp))}")
        except PromptCancelled:
            cli.update_info("Cancelled, no task was created.")
        finally:
            cli.stop()
