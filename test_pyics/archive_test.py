"""
Course archives: packing a course, and checking archives before they are used, so that
an archive can't write files outside of the directory it is pulled to, nor make pyics
import modules its course doesn't declare.
"""

import io
import json
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from pyics.data import Course, Task
from pyics.distribution import archive
from pyics.distribution.archive import ArchiveError
from pyics.tests.tests import SimpleTest
from pyics.util import json_handling


def _zip(members: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def _names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return zf.namelist()


def _member(path: Path, name: str) -> dict:
    with zipfile.ZipFile(path) as zf:
        return json.loads(zf.read(name))


# --- packing ---


def test_pack_holds_the_manifest_and_task_files(
    tmp_path: Path, course_writer: Callable[..., Path]
) -> None:
    manifest = course_writer(tmp_path / "author")
    Course.create_course_manifest(
        manifest, [tmp_path / "author/.tasks/add.pyicstask.json"], progress=[1]
    )
    out = tmp_path / "adding.zip"
    files = archive.pack(archive.read_course(manifest), out)
    # not the exercise and solution files, which are created from the task files
    assert (
        files == _names(out) == [".tasks/add.pyicstask.json", "course.pyicscourse.json"]
    )
    assert _member(out, "course.pyicscourse.json")["progress"] == [0]
    task = _member(out, ".tasks/add.pyicstask.json")
    assert task["solution_code"] == ["def add(a, b):\n    return a + b\n"]


def test_pack_is_reproducible(
    tmp_path: Path, course_writer: Callable[..., Path]
) -> None:
    course = archive.read_course(course_writer(tmp_path / "author"))
    archive.pack(course, tmp_path / "a.zip")
    archive.pack(course, tmp_path / "b.zip")
    assert (tmp_path / "a.zip").read_bytes() == (tmp_path / "b.zip").read_bytes()


def test_pack_without_solutions(
    tmp_path: Path, course_writer: Callable[..., Path]
) -> None:
    course = archive.read_course(course_writer(tmp_path / "author"))
    archive.pack(course, tmp_path / "a.zip", no_solutions=True)
    task = _member(tmp_path / "a.zip", ".tasks/add.pyicstask.json")
    assert task["sol_files"] == task["solution_code"] == []
    assert task["exercise_code"]


def test_pack_includes_other_files(
    tmp_path: Path, course_writer: Callable[..., Path]
) -> None:
    root = tmp_path / "author"
    course = archive.read_course(course_writer(root))
    (root / "data").mkdir()
    (root / "data/graph.csv").write_text("a,b\n")
    (root / "data/__pycache__").mkdir()
    (root / "data/__pycache__/x.pyc").write_bytes(b"")
    (root / "helper.py").write_text("")
    files = archive.pack(course, tmp_path / "a.zip", include=[root / "data", root])
    assert files == [
        ".tasks/add.pyicstask.json",
        "course.pyicscourse.json",
        "data/graph.csv",
        "helper.py",
    ]


def test_pack_refuses_to_include_another_course(
    tmp_path: Path, course_writer: Callable[..., Path]
) -> None:
    course = archive.read_course(course_writer(tmp_path / "a"))
    other = course_writer(tmp_path / "b")
    with pytest.raises(ArchiveError, match="another course's manifest"):
        archive.pack(course, tmp_path / "a.zip", include=[other])


def test_pack_keeps_the_layout_of_sibling_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Like the courses of ``portable_paths_test``: the manifest in ``.courses``, its
    tasks in ``.tasks``."""
    monkeypatch.chdir(tmp_path)
    Path("a.py").write_text("")
    task = Task.create_task_json(Path(".tasks/a"), "A", "Do it", Path("a.py"))
    manifest = Course.create_course_manifest(Path(".courses/c"), [task])
    files = archive.pack(archive.read_course(manifest), tmp_path / "c.zip")
    assert files == [".courses/c.pyicscourse.json", ".tasks/a.pyicstask.json"]


def test_update_manifest_keeps_the_rest(
    tmp_path: Path, course_writer: Callable[..., Path]
) -> None:
    manifest = course_writer(tmp_path, metadata={"description": "old"})
    archive.update_manifest(manifest, {"version": "1.0", "description": None})
    data = json_handling.load_plain(manifest)
    assert data["version"] == "1.0" and "description" not in data
    assert data["tasks"] == {"Add": Path(".tasks/add.pyicstask.json")}


def test_course_info(tmp_path: Path, course_writer: Callable[..., Path]) -> None:
    metadata = {"version": "1.2", "tags": ["x"], "requires": ["numpy"]}
    course = archive.read_course(
        course_writer(tmp_path, "Search Algorithms", metadata=metadata)
    )
    info = course.info
    assert (info.title, info.slug, info.version) == (
        "Search Algorithms",
        "search-algorithms",
        "1.2",
    )
    assert (info.tags, info.requires) == (["x"], ["numpy"])
    with pytest.raises(ArchiveError, match="slug"):
        archive.course_info({"name": "A", "slug": "Not A Slug"})


def test_read_archive(tmp_path: Path, course_writer: Callable[..., Path]) -> None:
    course = archive.read_course(
        course_writer(tmp_path / "a", metadata={"version": "2"})
    )
    archive.pack(course, tmp_path / "a.zip")
    assert archive.read_archive(tmp_path / "a.zip").version == "2"
    with pytest.raises(ArchiveError, match="2 course manifests"):
        archive.read_archive(
            _zip({"a.pyicscourse.json": "{}", "b.pyicscourse.json": "{}"})
        )


# --- extracting ---


@pytest.mark.parametrize(
    "name",
    ["../evil.py", "/evil.py", "a/../../evil.py", "C:/evil.py", "a\\..\\evil.py"],
)
def test_extract_refuses_paths_outside_of_the_directory(
    tmp_path: Path, name: str
) -> None:
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(ArchiveError, match="unsafe path"):
        archive.extract(_zip({"ok.txt": "", name: "print('evil')"}), dest)
    assert list(dest.iterdir()) == []  # checked before anything is extracted
    assert not (tmp_path / "evil.py").exists()


def test_extract_refuses_links(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = 0o120777 << 16  # a symbolic link
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(ArchiveError, match="link"):
        archive.extract(buffer.getvalue(), tmp_path)


def test_extract_refuses_duplicates(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError, match="twice"):
        archive.extract(_zip({"a.txt": "", "A.txt": ""}), tmp_path)


def test_extract_limits_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(archive, "MAX_UNPACKED_BYTES", 10)
    with pytest.raises(ArchiveError, match="unpacks to more than"):
        archive.extract(_zip({"a.txt": "x" * 11}), tmp_path)
    monkeypatch.setattr(archive, "MAX_MEMBERS", 1)
    with pytest.raises(ArchiveError, match="more than 1 entries"):
        archive.extract(_zip({"a.txt": "", "b.txt": ""}), tmp_path)


def test_extract_refuses_other_files(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError, match="isn't a zip archive"):
        archive.extract(b"not a zip", tmp_path)


def test_extract(tmp_path: Path) -> None:
    files = archive.extract(_zip({"a/b.txt": "b", "c.txt": "c"}), tmp_path)
    assert files == ["a/b.txt", "c.txt"]
    assert (tmp_path / "a/b.txt").read_text() == "b"


# --- checking courses ---


@pytest.fixture
def course_dir(tmp_path: Path, course_writer: Callable[..., Path]) -> Path:
    """A course as pulled: its manifest and task files, without exercise and solution
    files."""
    course = archive.read_course(course_writer(tmp_path / "author"))
    archive.pack(course, tmp_path / "a.zip")
    root = tmp_path / "pulled"
    archive.extract((tmp_path / "a.zip").read_bytes(), root)
    return root


def _edit_task(root: Path, **changes: object) -> None:
    fp = root / ".tasks/add.pyicstask.json"
    data = json.loads(fp.read_text())
    data.update(changes)
    fp.write_text(json.dumps(data))


def test_check_course(course_dir: Path) -> None:
    checked = archive.check_course(course_dir)
    assert checked.manifest_path == course_dir / "course.pyicscourse.json"
    assert checked.info.slug == "adding"
    assert checked.exercise_files == {course_dir / ".exercises/add.py"}


@pytest.mark.parametrize(
    "path", ["../../evil.py", "/tmp/evil.py", "../../../../../../evil.py"]
)
def test_exercise_files_outside_of_the_course_are_refused(
    course_dir: Path, path: str
) -> None:
    _edit_task(course_dir, ex_files=[{"__pathlib__": path}])
    with pytest.raises(ArchiveError, match="outside of the course's directory"):
        archive.check_course(course_dir)


def test_solution_files_outside_of_the_course_are_refused(course_dir: Path) -> None:
    _edit_task(course_dir, sol_files=["../../evil.py"])
    with pytest.raises(ArchiveError, match="outside of the course's directory"):
        archive.check_course(course_dir)


def test_tested_files_outside_of_the_course_are_refused(course_dir: Path) -> None:
    fp = course_dir / ".tasks/add.pyicstask.json"
    data = json.loads(fp.read_text())
    data["tests"][0]["attributes"]["file_path"] = {"__pathlib__": "../../x.py"}
    fp.write_text(json.dumps(data))
    with pytest.raises(ArchiveError, match="outside of the course's directory"):
        archive.check_course(course_dir)


def test_files_that_would_overwrite_the_course_are_refused(course_dir: Path) -> None:
    _edit_task(course_dir, sol_files=["../course.pyicscourse.json"])
    with pytest.raises(ArchiveError, match="would overwrite a file of the course"):
        archive.check_course(course_dir)


def test_task_files_outside_of_the_course_are_refused(course_dir: Path) -> None:
    fp = course_dir / "course.pyicscourse.json"
    data = json.loads(fp.read_text())
    data["tasks"] = {"Add": "../elsewhere.pyicstask.json"}
    fp.write_text(json.dumps(data))
    with pytest.raises(ArchiveError, match="outside of the course's directory"):
        archive.check_course(course_dir)


def test_undeclared_modules_are_refused(course_dir: Path) -> None:
    fp = course_dir / ".tasks/add.pyicstask.json"
    data = json.loads(fp.read_text())
    data["tests"][0]["attributes"]["expected"] = {
        "__instance_type__": ["fakepkg", "Point"],
        "attributes": {"x": 1, "y": 2},
    }
    fp.write_text(json.dumps(data))
    with pytest.raises(
        ArchiveError, match="'fakepkg', which the course doesn't declare"
    ):
        archive.check_course(course_dir)

    manifest = course_dir / "course.pyicscourse.json"
    archive.update_manifest(manifest, {"requires": ["fakepkg"]})
    archive.check_course(course_dir)  # declared, even if not installed


def test_manifests_holding_objects_are_refused(course_dir: Path) -> None:
    manifest = course_dir / "course.pyicscourse.json"
    archive.update_manifest(manifest, {"name": {"__instance_type__": ["os", "X"]}})
    with pytest.raises(ArchiveError, match="holds objects"):
        archive.check_course(course_dir)


def test_courses_need_exactly_one_manifest(course_dir: Path) -> None:
    manifest = course_dir / "course.pyicscourse.json"
    (course_dir / "copy.pyicscourse.json").write_text(manifest.read_text())
    with pytest.raises(ArchiveError, match="2 course manifests"):
        archive.check_course(course_dir)
    (course_dir / "copy.pyicscourse.json").unlink()
    manifest.unlink()
    with pytest.raises(ArchiveError, match="0 course manifests"):
        archive.check_course(course_dir)


def test_missing_task_files_are_refused(course_dir: Path) -> None:
    (course_dir / ".tasks/add.pyicstask.json").unlink()
    with pytest.raises(ArchiveError, match="task file of 'Add' is missing"):
        archive.check_course(course_dir)


def test_exercise_files(course_dir: Path) -> None:
    assert archive.exercise_files(course_dir) == {course_dir / ".exercises/add.py"}


def test_local_modules(tmp_path: Path, course_writer: Callable[..., Path]) -> None:
    root = tmp_path / "author"
    course = archive.read_course(course_writer(root))
    (root / "helper.py").write_text("")
    (root / "pkg").mkdir()
    (root / "pkg/__init__.py").write_text("")
    assert {"add", "helper", "pkg"} <= archive.local_modules(course)


def test_tests_of_packed_courses_still_run(
    tmp_path: Path, course_writer: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The paths in the task files stay valid relative to each other."""
    course = archive.read_course(course_writer(tmp_path / "author"))
    archive.pack(course, tmp_path / "a.zip")
    root = tmp_path / "elsewhere"
    archive.extract((tmp_path / "a.zip").read_bytes(), root)
    monkeypatch.setattr(Task, "run_tests", lambda self: None)
    task = Task(root / ".tasks/add.pyicstask.json")
    assert (root / ".exercises/add.py").read_text().startswith("def add")
    assert task.tests[0].file_path == root / ".exercises/add.py"
    assert isinstance(task.tests[0], SimpleTest)
