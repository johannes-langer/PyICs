"""
Links into JupyterLab (``pyics.notebook.links``): finding the server that serves a
notebook, and its URLs to a task's section.
"""

import itertools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pyics.notebook.links import (
    JupyterServer,
    find_server,
    heading_anchor,
    section_url,
)

LOCAL = "http://localhost:8888"

JUPYTER_TERMINAL = {
    "JUPYTER_SERVER_ROOT": "/home/student",
    "JUPYTER_SERVER_URL": "http://localhost:8888/",
}

_names = itertools.count()


def write_server(
    runtime_dir: Path,
    root_dir: Path,
    *,
    url: str = "http://localhost:8888/",
    base_url: str = "/",
    pid: int | None = None,
    started: float | None = None,
) -> Path:
    """Write a ``jpserver-*.json`` file as jupyter_server does, for a server running as
    this process (unless ``pid`` is given) that started at ``started``."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "base_url": base_url,
        "hostname": "localhost",
        "password": False,
        "pid": os.getpid() if pid is None else pid,
        "port": 8888,
        "root_dir": str(root_dir),
        "secure": False,
        "sock": "",
        "token": "secret-token",
        "url": url,
        "version": "2.17.0",
    }
    info_file = runtime_dir / f"jpserver-{next(_names)}.json"
    info_file.write_text(json.dumps(info, indent=2), encoding="utf-8")
    if started is not None:
        os.utime(info_file, (started, started))
    return info_file


def dead_pid() -> int:
    """The process ID of a process that has exited."""
    process = subprocess.Popen([sys.executable, "-c", ""])
    process.wait()
    return process.pid


@pytest.fixture
def runtime_dir(tmp_path: Path) -> Path:
    return tmp_path / "runtime"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "home"


# finding the server


def test_finds_the_server_of_a_nested_notebook(runtime_dir: Path, root: Path) -> None:
    write_server(runtime_dir, root)
    notebook = root / "course" / "week 1" / "lecture.ipynb"

    server = find_server(notebook, runtime_dir)

    assert server is not None
    assert server.url == "http://localhost:8888/"
    assert server.root_dir == root
    assert server.pid == os.getpid()
    url = section_url(server, notebook, cell_id="a1b2-c3")
    tree = "http://localhost:8888/lab/tree/course/week%201/lecture.ipynb"
    assert url == f"{tree}#cell-id=a1b2-c3"
    assert "secret-token" not in url


@pytest.mark.parametrize(
    "notebook",
    ["outside/lecture.ipynb", "home2/lecture.ipynb", "home"],
    ids=["elsewhere", "sibling with a common prefix", "the root itself"],
)
def test_no_server_for_a_notebook_outside_every_root(
    runtime_dir: Path, root: Path, tmp_path: Path, notebook: str
) -> None:
    write_server(runtime_dir, root)
    assert find_server(tmp_path / notebook, runtime_dir) is None


def test_no_server_without_runtime_files(tmp_path: Path) -> None:
    assert find_server(tmp_path / "lecture.ipynb", tmp_path / "missing") is None
    assert find_server(tmp_path / "lecture.ipynb", tmp_path) is None


def test_the_newest_server_wins(runtime_dir: Path, root: Path) -> None:
    notebook = root / "course" / "lecture.ipynb"
    write_server(runtime_dir, root, url="http://localhost:8888/", started=1000)
    write_server(runtime_dir, root, url="http://localhost:8890/", started=3000)
    write_server(
        runtime_dir, root / "course", url="http://localhost:8889/", started=2000
    )

    server = find_server(notebook, runtime_dir)
    assert server is not None and server.url == "http://localhost:8890/"
    assert server.started == 3000


def test_the_server_of_the_jupyter_terminal_wins(runtime_dir: Path, root: Path) -> None:
    """The TUI runs in a terminal of an older server: its notebook is open there."""
    notebook = root / "lecture.ipynb"
    write_server(runtime_dir, root, url="http://localhost:8888/", started=1000)
    write_server(runtime_dir, root, url="http://localhost:8890/", started=3000)

    server = find_server(notebook, runtime_dir, environ=JUPYTER_TERMINAL)
    assert server is not None and server.url == "http://localhost:8888/"


def test_ignores_unreadable_files_and_stopped_servers(
    runtime_dir: Path, root: Path
) -> None:
    notebook = root / "lecture.ipynb"
    write_server(runtime_dir, root, url="http://localhost:8888/", started=1000)
    # all newer than the one above
    write_server(runtime_dir, root, url="http://localhost:8890/", pid=dead_pid())
    socket = "http+unix://%2Ftmp%2Fjupyter.sock/"
    write_server(runtime_dir, root, url=socket)
    (runtime_dir / "jpserver-broken.json").write_text("{", encoding="utf-8")
    (runtime_dir / "jpserver-list.json").write_text("[]", encoding="utf-8")
    (runtime_dir / "jpserver-no-root.json").write_text(
        json.dumps({"url": "http://localhost:8891/", "pid": os.getpid()}),
        encoding="utf-8",
    )
    (runtime_dir / "jpserver-binary.json").write_bytes(b"\xff\xfe\x00")

    server = find_server(notebook, runtime_dir)
    assert server is not None and server.url == "http://localhost:8888/"


def test_runtime_dir_defaults_to_jupyters(
    runtime_dir: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_server(runtime_dir, root)
    monkeypatch.setenv("JUPYTER_RUNTIME_DIR", str(runtime_dir))
    assert find_server(root / "lecture.ipynb") is not None


@pytest.mark.parametrize(
    ("url", "base_url", "expected"),
    [
        (f"{LOCAL}/", "/", f"{LOCAL}/"),
        (LOCAL, "/", f"{LOCAL}/"),
        (f"{LOCAL}/jupyter/", "/jupyter/", f"{LOCAL}/jupyter/"),
        (f"{LOCAL}/jupyter", "/jupyter/", f"{LOCAL}/jupyter/"),
        # a URL without the base_url
        (f"{LOCAL}/", "/jupyter/", f"{LOCAL}/jupyter/"),
        (LOCAL, "jupyter", f"{LOCAL}/jupyter/"),
        # never a token
        ("https://hub:9999/user/a/?token=x#y", "/user/a/", "https://hub:9999/user/a/"),
    ],
)
def test_server_urls(
    runtime_dir: Path, root: Path, url: str, base_url: str, expected: str
) -> None:
    write_server(runtime_dir, root, url=url, base_url=base_url)
    notebook = root / "lecture.ipynb"

    server = find_server(notebook, runtime_dir)

    assert server is not None and server.url == expected
    assert section_url(server, notebook) == f"{expected}lab/tree/lecture.ipynb"


# section URLs


def test_special_characters_in_the_path(tmp_path: Path) -> None:
    # no trailing slash: URLs of servers made by hand work as well
    server = JupyterServer("http://localhost:8888/jupyter", tmp_path)
    notebook = tmp_path / "Übung 1" / "a #1?&%.ipynb"

    url = section_url(server, notebook, cell_id="c-1")

    assert url == (
        "http://localhost:8888/jupyter/lab/tree/"
        "%C3%9Cbung%201/a%20%231%3F%26%25.ipynb#cell-id=c-1"
    )


def test_notebook_outside_the_root_has_no_url(tmp_path: Path) -> None:
    server = JupyterServer("http://localhost:8888/", tmp_path / "home")
    with pytest.raises(ValueError):
        section_url(server, tmp_path / "lecture.ipynb", cell_id="c-1")


def test_heading_fallback(tmp_path: Path) -> None:
    """Without a cell id, the URL scrolls to the anchor cell's heading."""
    server = JupyterServer("http://localhost:8888/", tmp_path)
    notebook = tmp_path / "lecture.ipynb"
    base = "http://localhost:8888/lab/tree/lecture.ipynb"

    heading = "Exercise 2: Breadth-First Search"
    assert section_url(server, notebook, heading=heading) == (
        f"{base}#Exercise-2:-Breadth-First-Search"
    )
    # the cell id is more reliable: the heading may change or be duplicated
    assert section_url(server, notebook, cell_id="c-1", heading=heading) == (
        f"{base}#cell-id=c-1"
    )
    assert section_url(server, notebook) == base
    assert section_url(server, notebook, heading="") == base


def test_heading_fallback_with_special_characters(tmp_path: Path) -> None:
    """JupyterLab compares the fragment without decoding it: what a browser keeps as
    it is stays unencoded (``#``, ``?``, ``%``), the rest is percent-encoded (and can't
    match, as browsers encode it as well)."""
    server = JupyterServer("http://localhost:8888/", tmp_path)
    url = section_url(
        server, tmp_path / "lecture.ipynb", heading='Übung #1? 50% "a" < b'
    )
    assert url == (
        "http://localhost:8888/lab/tree/lecture.ipynb#%C3%9Cbung-#1?-50%-%22a%22-%3C-b"
    )


@pytest.mark.parametrize(
    ("heading", "anchor"),
    [
        ("Exercise 2: Breadth-First Search", "Exercise-2:-Breadth-First-Search"),
        ("## Exercise 2: Breadth-First Search ##", "Exercise-2:-Breadth-First-Search"),
        ("#  Spaces  inside ", "Spaces--inside"),
        ("Übung 3 — Tiefensuche (DFS)?", "Übung-3-—-Tiefensuche-(DFS)?"),
        ("C# basics", "C#-basics"),
        ("**Exercise** 2: `bfs(graph,  start)`", "Exercise-2:-bfs(graph,--start)"),
        ("Task 1: [BFS](https://example.org/bfs) &amp; *DFS*", "Task-1:-BFS-&-DFS"),
        ("***Very*** __important__ ~~not~~", "Very-important-not"),
        (r"Not \*emphasized\* \_either\_", "Not-*emphasized*-_either_"),
        ("snake_case_name and `*args`", "snake_case_name-and-*args"),
        ("Cost: $O(n_1 * n_2)$", "Cost:-$O(n_1-*-n_2)$"),
        ("![logo](logo.png) <b>Bold</b> text", "-Bold-text"),
    ],
)
def test_heading_anchor(heading: str, anchor: str) -> None:
    """JupyterLab's rule: the heading's rendered text, with each space replaced by -."""
    assert heading_anchor(heading) == anchor
