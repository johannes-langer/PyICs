"""
Links from the TUI's title to exercise notebooks (``pyics.notebook.links``): finding the
JupyterLab server that serves a notebook, its URLs to a task's section, ``file://``
links on paths, and that the links survive the CLI's drawing.
"""

import itertools
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text

from pyics.app.cli import CLI
from pyics.notebook.links import (
    JupyterServer,
    exercise_title,
    find_server,
    heading_anchor,
    path_text,
    section_url,
)

LOCAL = "http://localhost:8888"

PLAIN_TERMINAL: dict[str, str] = {"TERM_PROGRAM": "iTerm.app"}
VSCODE: dict[str, str] = {"TERM_PROGRAM": "vscode"}
JUPYTER_TERMINAL = {
    "JUPYTER_SERVER_ROOT": "/home/student",
    "JUPYTER_SERVER_URL": "http://localhost:8888/",
}

# OSC 8 hyperlinks as rich writes them: ESC ] 8 ; id=<id> ; <uri> ESC \
OSC8 = re.compile(r"\x1b\]8;[^;]*;([^\x1b]*)\x1b\\")

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


# paths and titles


def _links(text: Text) -> list[tuple[str, str | None]]:
    """The parts of ``text`` and their links."""
    return [
        (text.plain[span.start : span.end], getattr(span.style, "link", None))
        for span in text.spans
    ]


def test_path_is_a_file_link_in_other_terminals(tmp_path: Path) -> None:
    path = tmp_path / "week 1" / "lecture.ipynb"
    text = path_text(path, "week 1/lecture.ipynb", environ=PLAIN_TERMINAL)

    assert text.plain == "week 1/lecture.ipynb"
    [(_, uri)] = _links(text)
    assert uri == path.as_uri()
    assert uri is not None and uri.startswith("file://") and "week%201" in uri


def test_relative_path_links_to_the_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    text = path_text(Path(".exercises/bfs.ipynb"), ".exercises/bfs.ipynb", environ={})
    assert _links(text) == [
        (".exercises/bfs.ipynb", (tmp_path / ".exercises" / "bfs.ipynb").as_uri())
    ]


@pytest.mark.parametrize(
    "environ", [VSCODE, JUPYTER_TERMINAL], ids=["vscode", "jupyter"]
)
def test_path_is_plain_where_file_links_dont_help(
    tmp_path: Path, environ: dict[str, str]
) -> None:
    """VS Code makes the plain path clickable, xterm.js ignores file:// links."""
    text = path_text(tmp_path / "lecture.ipynb", "lecture.ipynb", environ=environ)
    assert text.plain == "lecture.ipynb"
    assert all(link is None for _, link in _links(text))


def test_title_links_the_section_into_jupyterlab(runtime_dir: Path, root: Path) -> None:
    write_server(runtime_dir, root)
    notebook = root / "lecture.ipynb"

    title = exercise_title(
        notebook,
        "lecture.ipynb",
        section_title="Exercise 2: Breadth-First Search",
        cell_id="c-1",
        environ=PLAIN_TERMINAL,
        runtime_dir=runtime_dir,
    )

    assert title.plain == "lecture.ipynb › Exercise 2: Breadth-First Search"
    assert _links(title) == [
        ("lecture.ipynb", notebook.as_uri()),
        (
            "Exercise 2: Breadth-First Search",
            "http://localhost:8888/lab/tree/lecture.ipynb#cell-id=c-1",
        ),
    ]


def test_title_in_the_jupyter_terminal(runtime_dir: Path, root: Path) -> None:
    write_server(runtime_dir, root)
    title = exercise_title(
        root / "lecture.ipynb",
        "lecture.ipynb",
        section_title="Exercise 2",
        cell_id="c-1",
        environ=JUPYTER_TERMINAL,
        runtime_dir=runtime_dir,
    )
    assert _links(title) == [
        ("Exercise 2", "http://localhost:8888/lab/tree/lecture.ipynb#cell-id=c-1")
    ]


@pytest.mark.parametrize(
    "environ", [PLAIN_TERMINAL, VSCODE], ids=["no server", "vscode"]
)
def test_title_without_a_section_link(
    runtime_dir: Path, root: Path, environ: dict[str, str]
) -> None:
    """In VS Code, the notebook is edited in VS Code, even if a JupyterLab serves it."""
    if environ is VSCODE:
        write_server(runtime_dir, root)
    title = exercise_title(
        root / "lecture.ipynb",
        "lecture.ipynb",
        section_title="Exercise 2",
        cell_id="c-1",
        environ=environ,
        runtime_dir=runtime_dir,
    )
    assert title.plain == "lecture.ipynb › Exercise 2"
    assert not any(
        link is not None and link.startswith("http") for _, link in _links(title)
    )


def test_title_of_a_missing_section(runtime_dir: Path, root: Path) -> None:
    write_server(runtime_dir, root)
    title = exercise_title(
        root / "lecture.ipynb",
        "lecture.ipynb",
        missing_section="ex2",
        environ=VSCODE,
        runtime_dir=runtime_dir,
    )
    assert title.plain == "lecture.ipynb › section 'ex2' not found"
    [span] = title.spans
    assert title.plain[span.start : span.end] == "section 'ex2' not found"
    assert span.style == "red"


def test_title_of_a_file_without_section(tmp_path: Path) -> None:
    title = exercise_title(tmp_path / "bfs.py", "bfs.py", environ=VSCODE)
    assert title.plain == "bfs.py"


# drawing


@pytest.fixture
def terminal_cli() -> CLI:
    """A CLI whose console writes to a terminal, as it does in practice: rich only
    writes styles and links to terminals, and pytest's captured output isn't one."""
    cli = CLI()
    cli.console = Console(
        force_terminal=True,
        color_system="truecolor",
        legacy_windows=False,
        width=100,
        height=30,
    )
    return cli


def _drawn_links(
    cli: CLI, capsys: pytest.CaptureFixture[str], titles: list[Text]
) -> list[str]:
    """The targets of the links in the drawn interface, with the main panel's title
    made of ``titles`` the way ``PyIC`` shows the exercise paths."""
    cli.update_main_content(
        Text("tests pending..."), Text(", ", style="dim").join(titles)
    )
    capsys.readouterr()
    cli.draw_interface()
    return [uri for uri in OSC8.findall(capsys.readouterr().out) if uri]


def test_drawn_title_links_to_the_section(
    terminal_cli: CLI, capsys: pytest.CaptureFixture[str], runtime_dir: Path, root: Path
) -> None:
    write_server(runtime_dir, root)
    notebook = root / "lecture.ipynb"
    title = exercise_title(
        notebook,
        "lecture.ipynb",
        section_title="Exercise 2",
        cell_id="c-1",
        environ=PLAIN_TERMINAL,
        runtime_dir=runtime_dir,
    )
    other = exercise_title(root / "helpers.py", "helpers.py", environ=PLAIN_TERMINAL)

    links = _drawn_links(terminal_cli, capsys, [title, other])

    assert "http://localhost:8888/lab/tree/lecture.ipynb#cell-id=c-1" in links
    assert notebook.as_uri() in links
    assert (root / "helpers.py").as_uri() in links


def test_drawn_title_without_a_server_has_no_section_link(
    terminal_cli: CLI, capsys: pytest.CaptureFixture[str], runtime_dir: Path, root: Path
) -> None:
    notebook = root / "lecture.ipynb"

    def drawn_links(environ: dict[str, str]) -> set[str]:
        title = exercise_title(
            notebook,
            "lecture.ipynb",
            section_title="Exercise 2",
            cell_id="c-1",
            environ=environ,
            runtime_dir=runtime_dir,
        )
        return set(_drawn_links(terminal_cli, capsys, [title]))

    assert drawn_links(PLAIN_TERMINAL) == {notebook.as_uri()}
    assert drawn_links(VSCODE) == set()
