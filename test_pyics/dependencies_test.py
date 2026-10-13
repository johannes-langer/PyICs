"""
The packages a course needs besides pyics (its ``requires``): checking whether they are
installed, how to install them, which modules its task files may make json_tricks
import, and finding the packages a course uses.
"""

import importlib.metadata
import json
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from pyics.data import Course, Task
from pyics.distribution import dependencies
from pyics.distribution.dependencies import (
    MissingDependencyError,
    ModuleNotAllowedError,
    ModulePolicy,
    Problem,
)
from pyics.tests.tests import SimpleTest
from pyics.util import json_handling
from pyics.util.discovery import find_courses


@pytest.fixture(autouse=True)
def no_test_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Task.__init__`` would run the task's tests with pytest."""
    monkeypatch.setattr(Task, "run_tests", lambda self: None)


# --- checking requirements ---


def test_met_requirements(fake_package: str) -> None:
    assert dependencies.check(["fake-package", "Fake_Package>=1.0,<2", "pytest"]) == []


def test_unmet_requirements(fake_package: str) -> None:
    assert dependencies.check(["fake-package>=2", "not-a-real-package-xyz", "a b"]) == [
        Problem("fake-package>=2", "1.2.0 is installed"),
        Problem("not-a-real-package-xyz", "not installed"),
        Problem("a b", "not a valid requirement", installable=False),
    ]


def test_requirements_for_other_platforms_are_skipped() -> None:
    other = "win32" if sys.platform != "win32" else "linux"
    assert (
        dependencies.check([f"not-a-real-package-xyz; sys_platform == '{other}'"]) == []
    )


def test_distribution_is_the_published_name() -> None:
    """pyics is published under another name than its module's, which is what has to
    be looked up for its version."""
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    assert dependencies.DISTRIBUTION == project["name"]
    assert dependencies.pyics_version() == importlib.metadata.version(project["name"])


def test_requires_pyics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dependencies, "pyics_version", lambda: "0.3.0")
    assert dependencies.check(requires_pyics=">=0.2") == []
    assert dependencies.check(requires_pyics=">=1.0") == [
        Problem("pyics>=1.0", "you have 0.3.0", installable=False)
    ]


def test_manifest_requirements_must_be_lists_of_strings() -> None:
    assert dependencies.manifest_requirements({}) == ([], None)
    with pytest.raises(ValueError, match="list of requirements"):
        dependencies.manifest_requirements({"requires": "numpy"})
    (problem,) = dependencies.check_manifest({"requires": [1]})
    assert not problem.installable


def test_install_command_uses_pip_of_this_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dependencies.importlib.util, "find_spec", lambda name: True)
    assert dependencies.install_command(["numpy>=1.26"]) == [
        sys.executable, "-m", "pip", "install", "numpy>=1.26"
    ]  # fmt: skip


def test_install_command_falls_back_to_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    """E.g. in an environment created by uv, which has no pip."""
    monkeypatch.setattr(dependencies.importlib.util, "find_spec", lambda name: None)
    assert dependencies.install_command(["numpy"]) == [
        "uv", "pip", "install", "--python", sys.executable, "numpy"
    ]  # fmt: skip


def test_install_hint() -> None:
    problems = [
        Problem("numpy>=1.26", "not installed"),
        Problem("pyics>=9", "you have 0.0.1", installable=False),
    ]
    hint = dependencies.install_hint(problems)
    command = dependencies.format_command(dependencies.install_command(["numpy>=1.26"]))
    assert f"Install it with: {command}" in hint
    assert "Upgrade pyics with: " in hint
    assert hint.endswith(f"-U {dependencies.DISTRIBUTION}")
    assert dependencies.describe(problems) == (
        "numpy>=1.26 (not installed) and pyics>=9 (you have 0.0.1)"
    )


# --- modules ---


def test_import_names(fake_package: str) -> None:
    assert "fakepkg" in dependencies.import_names(["fake-package>=1"])
    assert dependencies.distribution_of("fakepkg") == "fake-package"
    # not installed: guessed from the distribution's name
    assert "scikit_learn" in dependencies.import_names(["scikit-learn"])


def test_module_policy(fake_package: str) -> None:
    policy = ModulePolicy()
    assert policy.allows("pyics.tests.tests")
    assert policy.allows("collections")
    assert not policy.allows("fakepkg")
    assert not policy.allows(None)  # a class defined in a script
    assert ModulePolicy(["fake-package"]).allows("fakepkg.sub")


def _point_task(tmp_path: Path) -> Path:
    """A task whose test expects a ``fakepkg.Point``."""
    import fakepkg

    ex = tmp_path / "point.py"
    ex.write_text("def point():\n    return None\n")
    test = SimpleTest("points", ex, "point", fakepkg.Point(1, 2))
    return Task.create_task_json(
        tmp_path / "point", "Point", "Return a point.", ex, [], test
    )


def test_declared_modules_are_loaded(fake_package: str, tmp_path: Path) -> None:
    policy = ModulePolicy(["fake-package"])
    task = Task(_point_task(tmp_path), setup=False, module_policy=policy)
    assert type(task.tests[0].expected).__name__ == "Point"


def test_undeclared_modules_are_refused_before_they_are_imported(
    fake_package: str, tmp_path: Path
) -> None:
    task_file = _point_task(tmp_path)
    sys.modules.pop("fakepkg")
    with pytest.raises(ModuleNotAllowedError, match="'fakepkg'.*'fake-package'"):
        Task(task_file, setup=False, module_policy=ModulePolicy())
    assert "fakepkg" not in sys.modules


def test_a_course_declares_the_modules_of_its_tasks(
    fake_package: str, tmp_path: Path
) -> None:
    task_file = _point_task(tmp_path)
    manifest = Course.create_course_manifest(
        tmp_path / "c", [task_file], metadata={"requires": ["fake-package"]}
    )
    course = Course.load_course_manifest(manifest)
    assert course.requires == ["fake-package"]
    assert course.status is not None

    Course.create_course_manifest(tmp_path / "c", [task_file])
    with pytest.raises(ModuleNotAllowedError):
        Course.load_course_manifest(manifest)


def _ndarray_task(tmp_path: Path) -> Path:
    """A task whose test expects a numpy array, as json_tricks stores it."""
    fp = tmp_path / "array.pyicstask.json"
    test = json_handling.loads(json_handling.dumps(SimpleTest("a", "a.py", "f", 0)))
    data = json.loads(json_handling.dumps({"name": "Array", "tests": [test]}))
    data["tests"][0]["attributes"]["expected"] = {
        "__ndarray__": [1, 2],
        "dtype": "int64",
        "shape": [2],
    }
    data |= {"instructions": "", "ex_files": [], "sol_files": []}
    data |= {"exercise_code": [], "solution_code": []}
    fp.write_text(json.dumps(data))
    return fp


def test_a_missing_package_is_named_with_how_to_install_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    find_spec = dependencies.importlib.util.find_spec
    monkeypatch.setattr(
        dependencies.importlib.util,
        "find_spec",
        lambda name: None if name == "numpy" else find_spec(name),
    )
    policy = ModulePolicy(["numpy>=1.26"])
    with pytest.raises(MissingDependencyError) as info:
        Task(_ndarray_task(tmp_path), setup=False, module_policy=policy)
    command = dependencies.install_command(["numpy>=1.26"])
    assert str(info.value) == (
        "This course needs numpy>=1.26, which isn't installed. Install it with: "
        + dependencies.format_command(command)
    )


def test_numpy_arrays_are_loaded(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    task = Task(
        _ndarray_task(tmp_path), setup=False, module_policy=ModulePolicy(["numpy"])
    )
    assert isinstance(task.tests[0].expected, np.ndarray)


def test_finding_courses_imports_nothing(tmp_path: Path) -> None:
    """Manifests are read as plain data, so a manifest can't make pyics import a
    module."""
    module = "pyics_test_never_imported"
    (tmp_path / f"{module}.py").write_text("raise RuntimeError('imported')\n")
    manifest = {
        "name": {"__instance_type__": [module, "X"], "attributes": {}},
        "tasks": {"t": "t.pyicstask.json"},
        "progress": [0],
    }
    (tmp_path / "c.pyicscourse.json").write_text(json.dumps(manifest))
    sys.path.insert(0, str(tmp_path))
    try:
        found = find_courses(tmp_path, 1)
    finally:
        sys.path.remove(str(tmp_path))
    assert module not in sys.modules
    assert list(found.values())[0]["name"]["__instance_type__"] == [module, "X"]


# --- finding the packages a course uses ---


def test_imported_modules() -> None:
    code = (
        "import os, numpy.linalg as la\n"
        "from fakepkg.sub import thing\n"
        "from . import sibling\n"
        "from __future__ import annotations\n"
    )
    assert dependencies.imported_modules(code) == {
        "os", "numpy", "fakepkg", "__future__"
    }  # fmt: skip
    assert dependencies.imported_modules("def broken(:\n") == set()


def test_used_modules(fake_package: str, tmp_path: Path) -> None:
    tasks = {
        "point.pyicstask.json": json_handling.load_plain(_point_task(tmp_path)),
        "array.pyicstask.json": json_handling.load_plain(_ndarray_task(tmp_path)),
        "code.pyicstask.json": {
            "exercise_code": ["import helper\nimport requests\nimport json\n"],
            "solution_code": ["from __future__ import annotations\nimport pyics\n"],
        },
    }
    assert dependencies.used_modules(tasks, local={"helper"}) == {
        "fakepkg": {"point.pyicstask.json"},
        "numpy": {"array.pyicstask.json"},
        "requests": {"code.pyicstask.json"},
    }


def test_undeclared(fake_package: str) -> None:
    used = {"fakepkg": {"a"}, "not_installed_xyz": {"b"}, "numpy": {"c"}}
    missing = dependencies.undeclared(used, ["numpy>=1"])
    # the distributions that provide the modules, as far as they are installed
    assert missing == {"fake-package": {"a"}, "not_installed_xyz": {"b"}}


def test_is_newer() -> None:
    assert dependencies.is_newer("1.10.0", "1.9.0")
    assert not dependencies.is_newer("1.0", "1.0.0")
    assert dependencies.is_newer("b", "a")  # not PEP 440 versions


@pytest.fixture
def pip_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    class _Done:
        returncode = 0

    def run(command: list[str]) -> _Done:
        calls.append(command)
        return _Done()

    monkeypatch.setattr(dependencies.subprocess, "run", run)
    return calls


def test_install(pip_calls: list[list[str]]) -> None:
    assert dependencies.install(["numpy"]) == 0
    assert pip_calls == [dependencies.install_command(["numpy"])]


def test_metadata_is_kept_in_manifests(
    tmp_path: Path, course_writer: Callable[..., Path]
) -> None:
    metadata = {"slug": "adding", "version": "1.0", "requires": ["numpy"], "x": 1}
    manifest = course_writer(tmp_path, metadata=metadata)
    data = json_handling.load_plain(manifest)
    assert {k: data.get(k) for k in metadata} == {**metadata, "x": None}


@pytest.mark.parametrize("tag", ["__pathlib__", "__tuple__"])
def test_tags_cant_hide_modules(tag: str, tmp_path: Path) -> None:
    """json_tricks decodes the objects inside a tagged dict before the dict itself, so
    the modules they need are found in it, too."""
    task_file = tmp_path / "t.pyicstask.json"
    hidden = {"__instance_type__": ["pyics_test_never_imported", "X"], "attributes": {}}
    data = {
        "name": "T",
        "tests": [],
        "exercise_code": [],
        "solution_code": [],
        "help": {tag: "a" if tag == "__pathlib__" else [], "hidden": hidden},
    }
    task_file.write_text(json.dumps(data))
    with pytest.raises(ModuleNotAllowedError, match="pyics_test_never_imported"):
        Task(task_file, setup=False, module_policy=ModulePolicy())
