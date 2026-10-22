"""
Pulling courses from sources, URLs and paths, and updating pulled courses without
touching the student's work.
"""

import functools
import http.server
import json
import shutil
import threading
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from pyics.data import Course, Task
from pyics.distribution import archive, dependencies, pull
from pyics.distribution.archive import ORIGIN_FILE, PYICS_DIR, ArchiveError, origin_path
from pyics.distribution.pull import PullError
from pyics.distribution.sources import Source


@pytest.fixture(autouse=True)
def no_test_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Task.__init__`` would run the task's tests with pytest."""
    monkeypatch.setattr(Task, "run_tests", lambda self: None)


@pytest.fixture
def author(tmp_path: Path, course_writer: Callable[..., Path]) -> Path:
    """The manifest of the course "Adding" 1.0.0, as its author has it."""
    return course_writer(tmp_path / "author", metadata={"version": "1.0.0"})


@pytest.fixture
def library(
    tmp_path: Path,
    author: Path,
    publisher: Callable[..., Path],
    source_adder: Callable[..., Source],
) -> Path:
    """A library with the course of ``author``, added as the source "lib". Returns the
    library's directory."""
    library = tmp_path / "library"
    source_adder(publisher(author, library), "lib")
    return library


@pytest.fixture
def student(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The student's working directory."""
    student = tmp_path / "student"
    student.mkdir()
    monkeypatch.chdir(student)
    return student


def _leftovers(directory: Path) -> list[str]:
    """Temporary directories left behind by pulls."""
    return [p.name for p in directory.iterdir() if p.name.startswith(".pyics-pull-")]


# --- pulling ---


def test_pull_from_a_source(library: Path, student: Path) -> None:
    pulled = pull.pull("adding")
    assert pulled.directory == student / "adding"
    assert (
        pulled.manifest_path == student / "adding/pyics_files/course.pyicscourse.json"
    )
    assert (pulled.info.title, pulled.info.version) == ("Adding", "1.0.0")
    assert pulled.problems == []

    origin = json.loads((student / "adding/pyics_files" / ORIGIN_FILE).read_text())
    assert origin["source"] == "lib"
    assert origin["slug"] == "adding"
    assert origin["course_version"] == "1.0.0"
    assert origin["files"] == [
        "pyics_files/.tasks/add.pyicstask.json",
        "pyics_files/course.pyicscourse.json",
    ]
    assert [p.name for p in (student / "adding").iterdir()] == [PYICS_DIR]

    course = Course.load_course_manifest(pulled.manifest_path)
    assert course.instructions == "Implement add(a, b)."
    # the student's files are next to pyics_files, not in it
    assert (student / "adding/.exercises/add.py").is_file()
    assert (student / "adding/.solutions/add.py").is_file()
    assert sorted(p.name for p in (student / "adding/pyics_files").iterdir()) == [
        ".pyics-origin.json",
        ".tasks",
        "course.pyicscourse.json",
    ]


def test_pull_keeps_flat_task_files_flat(
    tmp_path: Path, student: Path, course_writer: Callable[..., Path]
) -> None:
    """Task files next to the manifest stay next to it in pyics_files."""
    root = tmp_path / "flat"
    manifest = course_writer(root)
    task = root / ".tasks/add.pyicstask.json"
    data = json.loads(task.read_text())
    for key in ("ex_files", "sol_files"):
        data[key] = [{"__pathlib__": f["__pathlib__"][3:]} for f in data[key]]
    for test in data["tests"]:
        attrs = test["attributes"]
        attrs["file_path"] = {"__pathlib__": attrs["file_path"]["__pathlib__"][3:]}
    (root / "add.pyicstask.json").write_text(json.dumps(data))
    task.unlink()
    archive.update_manifest(manifest, {"tasks": {"Add": "add.pyicstask.json"}})
    zip_path = tmp_path / "flat.zip"
    archive.pack(archive.read_course(manifest), zip_path)

    pulled = pull.pull(str(zip_path))
    assert (pulled.directory / "pyics_files/add.pyicstask.json").is_file()
    Course.load_course_manifest(pulled.manifest_path)
    assert (pulled.directory / ".exercises/add.py").is_file()


def test_pull_an_archive_laid_out_already(
    library: Path, student: Path, tmp_path: Path
) -> None:
    """A course packed from a pulled course isn't put in pyics_files twice."""
    pulled = pull.pull("adding")
    Course.load_course_manifest(pulled.manifest_path)
    zip_path = tmp_path / "again.zip"
    archive.pack(archive.read_course(pulled.manifest_path), zip_path)
    again = pull.pull(str(zip_path), Path("again"))
    assert again.manifest_path == student / "again/pyics_files/course.pyicscourse.json"
    Course.load_course_manifest(again.manifest_path)
    assert (student / "again/.exercises/add.py").is_file()


def test_pull_a_course_of_a_source(library: Path, student: Path) -> None:
    assert pull.pull("lib/adding").directory == student / "adding"


def test_pull_to_another_directory(library: Path, student: Path) -> None:
    (student / "empty").mkdir()
    assert pull.pull("adding", Path("empty")).directory == student / "empty"
    assert pull.pull("adding", Path("new/dir")).directory == student / "new/dir"


def test_pull_refuses_existing_directories(library: Path, student: Path) -> None:
    pull.pull("adding")
    with pytest.raises(PullError, match="pyics update"):
        pull.pull("adding")
    (student / "taken").mkdir()
    (student / "taken/file").write_text("")
    with pytest.raises(PullError, match="exists already"):
        pull.pull("adding", Path("taken"))


def test_pull_an_archive_by_path_and_url(
    library: Path, student: Path, tmp_path: Path
) -> None:
    zip_path = library / "adding-1.0.0.zip"
    pulled = pull.pull(str(zip_path))
    assert pulled.directory == student / "adding"  # by its manifest's slug
    origin = json.loads(origin_path(pulled.directory).read_text())
    assert origin["source"] is None and origin["url"] == zip_path.as_uri()
    assert pull.pull(zip_path.as_uri(), Path("again")).info.title == "Adding"


def test_pull_checks_the_hash(library: Path, student: Path) -> None:
    zip_path = library / "adding-1.0.0.zip"
    with zipfile.ZipFile(zip_path, "a") as zf:
        zf.writestr("extra.txt", "changed")
    with pytest.raises(PullError, match="doesn't match its hash"):
        pull.pull("adding")
    assert list(student.iterdir()) == []


def test_pull_refuses_unsafe_archives(student: Path, tmp_path: Path) -> None:
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("course.pyicscourse.json", "{}")
        zf.writestr("../evil.py", "")
    with pytest.raises(ArchiveError, match="unsafe path"):
        pull.pull(str(zip_path))
    assert list(student.iterdir()) == []
    assert not (tmp_path / "evil.py").exists()


def test_pull_refuses_courses_for_newer_pyics(
    author: Path, student: Path, tmp_path: Path
) -> None:
    archive.update_manifest(author, {"requires_pyics": ">=999"})
    zip_path = tmp_path / "adding.zip"
    archive.pack(archive.read_course(author), zip_path)
    with pytest.raises(PullError, match=r"needs pyics>=999 \(you have"):
        pull.pull(str(zip_path))
    assert _leftovers(student) == [] and list(student.iterdir()) == []


def test_pull_reports_missing_requirements(
    author: Path, student: Path, tmp_path: Path
) -> None:
    archive.update_manifest(author, {"requires": ["not-a-real-package-xyz"]})
    zip_path = tmp_path / "adding.zip"
    archive.pack(archive.read_course(author), zip_path)
    pulled = pull.pull(str(zip_path))
    assert pulled.problems == [
        dependencies.Problem("not-a-real-package-xyz", "not installed")
    ]
    assert pulled.directory.is_dir()  # pulled nonetheless, to install them after


# --- updating ---


def _new_version(author: Path, library: Path, publisher: Callable[..., Path]) -> None:
    """Publish version 2.0.0 of the course of ``author``, with new instructions, a data
    file, and without the file ``old.txt``."""
    task = author.parent / ".tasks/add.pyicstask.json"
    data = json.loads(task.read_text())
    data["instructions"] = "Implement add(a, b), again."
    task.write_text(json.dumps(data, indent=4))
    archive.update_manifest(author, {"version": "2.0.0"})
    (author.parent / "data.txt").write_text("new")
    publisher(author, library, include=[author.parent / "data.txt"])


def test_update_when_up_to_date(library: Path, student: Path) -> None:
    pull.pull("adding")
    updated = pull.update(Path("adding"))
    assert updated.up_to_date
    assert updated.info.version == "1.0.0"


def test_update_keeps_the_students_work(
    library: Path, student: Path, author: Path, publisher: Callable[..., Path]
) -> None:
    pulled = pull.pull("adding")
    Course.load_course_manifest(pulled.manifest_path)  # creates the exercise file
    exercise = student / "adding/.exercises/add.py"
    exercise.write_text("def add(a, b):\n    return a + b  # my work\n")
    (student / "adding/data.txt").write_text("mine")  # not pulled

    _new_version(author, library, publisher)
    updated = pull.update(student / "adding/.exercises")  # from within the course
    assert not updated.up_to_date
    assert (updated.old_version, updated.info.version) == ("1.0.0", "2.0.0")
    assert updated.kept == ["data.txt"]
    assert (student / "adding/data.txt").read_text() == "mine"
    assert exercise.read_text().endswith("# my work\n")
    task_file = student / "adding/pyics_files/.tasks/add.pyicstask.json"
    task = json.loads(task_file.read_text())
    assert task["instructions"] == "Implement add(a, b), again."
    origin = json.loads(origin_path(student / "adding").read_text())
    assert origin["course_version"] == "2.0.0"
    assert "data.txt" not in origin["files"]


def test_update_removes_files_the_new_version_dropped(
    library: Path, student: Path, author: Path, publisher: Callable[..., Path]
) -> None:
    (author.parent / "old.txt").write_text("old")
    publisher(author, library, include=[author.parent / "old.txt"])
    pull.pull("adding")
    assert (student / "adding/old.txt").is_file()

    _new_version(author, library, publisher)
    pull.update(Path("adding"))
    assert not (student / "adding/old.txt").exists()
    assert (student / "adding/data.txt").read_text() == "new"


def _pretend_pulled(course: Path, file: str) -> None:
    """Record ``file`` in the origin file of ``course`` as if it had been pulled."""
    origin_file = origin_path(course)
    origin = json.loads(origin_file.read_text())
    origin["files"].append(file)
    origin_file.write_text(json.dumps(origin))


def test_update_never_overwrites_exercise_files(
    library: Path, student: Path, author: Path, publisher: Callable[..., Path]
) -> None:
    """Even if the new version has a file at the place of an exercise file, and the
    file was pulled."""
    pull.pull("adding")
    exercise = student / "adding/.exercises/add.py"
    exercise.parent.mkdir()
    exercise.write_text("# my work\n")
    _pretend_pulled(student / "adding", ".exercises/add.py")

    archive.update_manifest(author, {"version": "2.0.0"})
    publisher(author, library)
    with zipfile.ZipFile(library / "adding-2.0.0.zip", "a") as zf:
        zf.writestr(".exercises/add.py", "# overwritten\n")
    pull.sources.build_index(library)
    updated = pull.update(Path("adding"))
    assert updated.info.version == "2.0.0"
    assert updated.kept == [".exercises/add.py"]
    assert exercise.read_text() == "# my work\n"


def test_update_never_removes_exercise_files(
    library: Path, student: Path, author: Path, publisher: Callable[..., Path]
) -> None:
    """Even if the file was pulled, and the new version doesn't have it."""
    pull.pull("adding")
    exercise = student / "adding/.exercises/add.py"
    exercise.parent.mkdir()
    exercise.write_text("# my work\n")
    _pretend_pulled(student / "adding", ".exercises/add.py")

    archive.update_manifest(author, {"version": "2.0.0"})
    publisher(author, library)
    pull.update(Path("adding"))
    assert exercise.read_text() == "# my work\n"


def test_update_with_force(library: Path, student: Path) -> None:
    pull.pull("adding")
    task_file = student / "adding/pyics_files/.tasks/add.pyicstask.json"
    task_file.unlink()
    assert pull.update(Path("adding"), force=True).up_to_date is False
    assert task_file.is_file()


def test_update_from_within_pyics_files(library: Path, student: Path) -> None:
    pull.pull("adding")
    updated = pull.update(student / "adding/pyics_files")
    assert updated.directory == student / "adding"
    assert updated.up_to_date


def test_update_moves_courses_pulled_by_older_versions(
    library: Path, student: Path, author: Path, publisher: Callable[..., Path]
) -> None:
    """A course pulled before pyics_files existed: its manifest, task files and origin
    file next to the exercise files."""
    pulled = pull.pull("adding")
    origin = json.loads(origin_path(pulled.directory).read_text())
    shutil.rmtree(pulled.directory)
    data = (library / "adding-1.0.0.zip").read_bytes()
    origin["files"] = archive.extract(data, pulled.directory)
    (pulled.directory / ORIGIN_FILE).write_text(json.dumps(origin))
    Course.load_course_manifest(pulled.directory / "course.pyicscourse.json")
    exercise = student / "adding/.exercises/add.py"
    exercise.write_text("# my work\n")
    assert pull.find_origin(student / "adding/.exercises")[0] == pulled.directory

    _new_version(author, library, publisher)
    updated = pull.update(Path("adding"))
    assert (
        updated.manifest_path == student / "adding/pyics_files/course.pyicscourse.json"
    )
    assert exercise.read_text() == "# my work\n"
    assert origin_path(pulled.directory).is_file()
    for old in (ORIGIN_FILE, "course.pyicscourse.json", ".tasks"):
        assert not (pulled.directory / old).exists()


def test_update_after_the_source_was_removed(
    library: Path, student: Path, author: Path, publisher: Callable[..., Path]
) -> None:
    pull.pull("adding")
    pull.sources.remove_source("lib")
    _new_version(author, library, publisher)
    assert pull.update(Path("adding")).info.version == "2.0.0"


def test_update_an_archive_pulled_by_path(
    library: Path, student: Path, author: Path, tmp_path: Path
) -> None:
    zip_path = tmp_path / "adding.zip"
    archive.pack(archive.read_course(author), zip_path)
    pull.pull(str(zip_path))
    assert pull.update(Path("adding")).up_to_date
    archive.update_manifest(author, {"version": "2.0.0"})
    archive.pack(archive.read_course(author), zip_path)
    assert pull.update(Path("adding")).info.version == "2.0.0"


def test_update_outside_of_a_pulled_course(student: Path) -> None:
    with pytest.raises(PullError, match="isn't a pulled course"):
        pull.update(student)


# --- over http ---


@pytest.fixture
def server(library: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Serves the library over plain http on localhost. Yields its URL."""
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

    handler = functools.partial(Quiet, directory=str(library))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_pull_over_http(
    server: str, student: Path, source_adder: Callable[..., Source]
) -> None:
    with pytest.raises(pull.SourceError, match="isn't secure"):
        source_adder(f"{server}/index.json", "web")
    source_adder(f"{server}/index.json", "web", allow_http=True)
    assert pull.pull("web/adding").info.title == "Adding"


def test_pull_an_archive_over_http(server: str, student: Path) -> None:
    url = f"{server}/adding-1.0.0.zip"
    with pytest.raises(PullError, match="isn't secure"):
        pull.pull(url)
    assert pull.pull(url, allow_http=True).directory == student / "adding"
    with pytest.raises(PullError, match="404"):
        pull.pull(f"{server}/missing.zip", Path("other"), allow_http=True)
