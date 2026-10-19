"""
The ``pyics`` subcommands that distribute courses, run as from the command line:
``pack``, ``build_index``, ``add_source``, ``sources``, ``search``, ``remove_source``,
``pull`` and ``update``.
"""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from venv_helpers import VirtualEnv

from pyics.distribution import archive, dependencies, sources
from pyics.scripts import main
from pyics.util import json_handling


@pytest.fixture
def pyics(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Callable[..., str]:
    """Runs ``pyics`` with the given arguments, answering its questions with
    ``answers``, and returns what it printed."""

    def run(*args: str, answers: tuple[str, ...] = ()) -> str:
        remaining = list(answers)

        def answer(question: str) -> str:
            print(question)
            if not remaining:
                raise EOFError
            return remaining.pop(0)

        monkeypatch.setattr("builtins.input", answer)
        monkeypatch.setattr("sys.argv", ["pyics", *args])
        main.run()
        return capsys.readouterr().out

    return run


@pytest.fixture
def author(tmp_path: Path, course_writer: Callable[..., Path]) -> Path:
    return course_writer(tmp_path / "author", "Search Algorithms")


@pytest.fixture
def library(tmp_path: Path, author: Path, pyics: Callable[..., str]) -> Path:
    """The library of the course of ``author``, version 1.0.0, with its index."""
    library = tmp_path / "library"
    library.mkdir()
    pyics("pack", str(author), "--version", "1.0.0", "--out", str(library))
    pyics("build_index", str(library), "--name", "Demo library")
    return library


def test_pack(
    author: Path,
    tmp_path: Path,
    pyics: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    out = pyics(
        "pack", str(author), "--version", "1.0.0", "--description", "DFS and BFS",
        "--tags", "graphs", "search", "--no_solutions",
    )  # fmt: skip
    assert "Packed Search Algorithms 1.0.0 into search-algorithms-1.0.0.zip" in out
    assert (tmp_path / "search-algorithms-1.0.0.zip").is_file()
    manifest = json_handling.load_plain(author)
    assert manifest["version"] == "1.0.0"
    assert manifest["tags"] == ["graphs", "search"]
    assert manifest["slug"] == "search-algorithms"  # fixed once packed
    assert manifest["requires_pyics"] == f">={dependencies.pyics_version()}"


def test_pack_offers_to_declare_the_packages_the_course_uses(
    tmp_path: Path,
    course_writer: Callable[..., Path],
    pyics: Callable[..., str],
    fake_package: str,
) -> None:
    template = "import fakepkg\nimport json\n\ndef add(a, b):\n    return 0\n"
    manifest = course_writer(tmp_path / "author", template=template)
    out = pyics("pack", str(manifest), "--out", str(tmp_path), answers=("n",))
    assert "fake-package (in .tasks/add.pyicstask.json)" in out
    assert "requires" not in json_handling.load_plain(manifest)

    pyics("pack", str(manifest), "--out", str(tmp_path), answers=("",))  # yes
    assert json_handling.load_plain(manifest)["requires"] == ["fake-package"]
    out = pyics("pack", str(manifest), "--out", str(tmp_path))
    assert "don't declare" not in out


def test_build_index(library: Path) -> None:
    index = json.loads((library / "index.json").read_text())
    assert index["name"] == "Demo library"
    assert index["courses"]["search-algorithms"]["version"] == "1.0.0"


def test_sources(library: Path, pyics: Callable[..., str]) -> None:
    assert "You haven't added any sources" in pyics("sources")
    index = str(library / "index.json")

    out = pyics("add_source", index, answers=("n",))
    assert "Only use courses from people you trust." in out
    assert "Add it as 'demo-library'? [y/N]" in out
    assert "Cancelled" in out and sources.load_sources() == []

    out = pyics("add_source", index, "--name", "demo", answers=("y",))
    assert "Added the source 'demo'" in out
    assert pyics("sources") == f"demo  {Path(index).as_uri()}\n"

    out = pyics("search", "search")
    assert "search-algorithms 1.0.0: Search Algorithms (from demo)" in out
    assert pyics("search", "nothing", "--offline") == "No course matches 'nothing'.\n"

    assert pyics("remove_source", "demo") == "Removed the source 'demo'.\n"
    with pytest.raises(SystemExit, match="error: There is no source named 'demo'"):
        pyics("remove_source", "demo")


def test_pull_and_update(
    library: Path,
    author: Path,
    tmp_path: Path,
    pyics: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pyics("add_source", str(library / "index.json"), "--yes")
    student = tmp_path / "student"
    student.mkdir()
    monkeypatch.chdir(student)

    out = pyics("pull", "search-algorithms")
    assert (
        f"Pulled Search Algorithms 1.0.0 into {student / 'search-algorithms'}." in out
    )
    assert "Start it with: pyics" in out
    with pytest.raises(SystemExit, match="update it with: pyics update"):
        pyics("pull", "search-algorithms")

    assert "is up to date (1.0.0)" in pyics("update", "search-algorithms")
    pyics("pack", str(author), "--version", "1.1.0", "--out", str(library))
    pyics("build_index", str(library))
    monkeypatch.chdir(student / "search-algorithms")
    assert "Updated Search Algorithms from 1.0.0 to 1.1.0" in pyics("update")


def test_pull_asks_before_pulling_by_url(
    library: Path,
    tmp_path: Path,
    pyics: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    url = (library / "search-algorithms-1.0.0.zip").as_uri()
    assert "Cancelled" in pyics("pull", url, answers=("n",))
    assert not (tmp_path / "search-algorithms").exists()
    assert "Pulled" in pyics("pull", url, "--dest", "here", "--yes")
    assert (tmp_path / "here/course.pyicscourse.json").is_file()


def test_pull_installs_missing_packages(
    author: Path,
    tmp_path: Path,
    pyics: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive.update_manifest(author, {"requires": ["not-a-real-package-xyz"]})
    pyics("pack", str(author), "--out", str(tmp_path / "a.zip"))
    installed: list[list[str]] = []
    monkeypatch.setattr(
        dependencies, "install", lambda reqs: installed.append(list(reqs)) or 0
    )
    monkeypatch.chdir(tmp_path)

    out = pyics("pull", str(tmp_path / "a.zip"), "--dest", "a")
    assert "needs not-a-real-package-xyz (not installed)" in out
    assert "Install it with: " in out
    assert installed == []

    out = pyics(
        "pull", str(tmp_path / "a.zip"), "--dest", "b", "--install_deps", "--yes"
    )
    assert installed == [["not-a-real-package-xyz"]]
    # it still isn't installed, as installing was only pretended
    assert "needs not-a-real-package-xyz (not installed)" in out


def test_errors_are_reported(pyics: Callable[..., str], tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="error: You haven't added any sources"):
        pyics("pull", "anything")
    with pytest.raises(SystemExit, match="is not a course manifest"):
        pyics("pack", str(tmp_path / "missing.json"))
    with pytest.raises(SystemExit, match="isn't a pulled course"):
        pyics("update", str(tmp_path))


def test_user_wide_sources_are_moved_to_the_environment(
    library: Path, virtual_env: VirtualEnv, pyics: Callable[..., str]
) -> None:
    url = (library / "index.json").as_uri()
    legacy = virtual_env.write_legacy(sources.Source("demo", url))
    warning = (
        f"Detected user-wide sources at {legacy}. Sources have been moved to this "
        "environment. Delete old source file? [y/N] "
    )

    out = pyics("sources", answers=("n",))
    assert out == f"{warning}\ndemo  {url}\n"
    assert legacy.is_file()
    # asked again while the file is there, without adding its sources again
    assert pyics("sources") == f"{warning}\n\ndemo  {url}\n"

    assert pyics("sources", answers=("y",)) == (
        f"{warning}\nDeleted {legacy}.\ndemo  {url}\n"
    )
    assert not legacy.exists()
    assert pyics("sources") == f"demo  {url}\n"
