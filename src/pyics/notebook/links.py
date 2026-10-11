"""
Links into a running JupyterLab that open an exercise notebook at a task's section.

Jupyter servers don't open files in hidden directories (such as ``.exercises``) unless
they run with ``--ContentsManager.allow_hidden=True``, which the TUI can't tell.
"""

import html
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath
from urllib.parse import quote, urlsplit, urlunsplit

from jupyter_core.paths import jupyter_runtime_dir

from pyics.util.paths import StrPath

# The characters a browser keeps as they are in a URL's fragment: printable ASCII except
# the URL standard's fragment percent-encode set (space, ", <, >, `). JupyterLab compares
# the fragment with heading ids without decoding it, so these have to stay unencoded to
# match. The others can't match either way, and are encoded to keep the URL valid.
_FRAGMENT_SAFE = "".join(chr(c) for c in range(0x21, 0x7F) if chr(c) not in '"<>`')


@dataclass(frozen=True)
class JupyterServer:
    """
    A running Jupyter server, as its ``jpserver-<pid>.json`` file describes it.

    Attributes
    ----------
    url: str
        The server's URL including its ``base_url``, ending with "/" and never with a
        token, e.g. ``http://localhost:8888/``.
    root_dir: Path
        The directory the server serves.
    started: float | None
        When the server started (its info file's modification time), if known.
    pid: int | None
        The server's process ID, if known.
    """

    url: str
    root_dir: Path
    started: float | None = None
    pid: int | None = None


def find_server(
    notebook: StrPath,
    runtime_dir: StrPath | None = None,
    *,
    environ: Mapping[str, str] = os.environ,
) -> JupyterServer | None:
    """
    Find the running Jupyter server that serves ``notebook``.

    Of the servers whose ``root_dir`` contains the notebook, that is the server whose
    terminal the TUI runs in (its ``JUPYTER_SERVER_URL``), else the newest one.

    Parameters
    ----------
    notebook: StrPath
        The notebook's path.
    runtime_dir: StrPath | None (optional)
        The directory of the servers' ``jpserver-*.json`` files. Default is Jupyter's
        runtime directory (``jupyter_core.paths.jupyter_runtime_dir``, which
        ``JUPYTER_RUNTIME_DIR`` overrides).
    environ: Mapping[str, str] (optional, keyword only)
        The environment the TUI runs in. Default is ``os.environ``.

    Returns
    -------
    JupyterServer | None
        The server, or None if no running server serves the notebook. Files that can't
        be read, servers whose process isn't running, and servers that can't be opened
        in a browser (on a Unix socket) are ignored.
    """
    directory = Path(jupyter_runtime_dir() if runtime_dir is None else runtime_dir)
    try:
        info_files = sorted(directory.glob("jpserver-*.json"))
    except OSError:
        return None

    servers = []
    for info_file in info_files:
        server = _read_server(info_file)
        if server is None or _relative_path(notebook, server.root_dir) is None:
            continue
        # a stopped server's file stays behind if it was killed
        if server.pid is None or _pid_running(server.pid):
            servers.append(server)
    if not servers:
        return None

    own_url = _server_url({"url": environ.get("JUPYTER_SERVER_URL")})
    return max(servers, key=lambda s: (s.url == own_url, s.started or 0.0))


def section_url(
    server: JupyterServer,
    notebook: StrPath,
    *,
    cell_id: str | None = None,
    heading: str | None = None,
) -> str:
    """
    The URL that opens ``notebook`` in JupyterLab, scrolled to a cell.

    ``<server url>lab/tree/<path from root_dir>#cell-id=<cell_id>`` (JupyterLab 4), or
    with ``#<heading anchor>`` instead (see ``heading_anchor``) if the cell has no id.
    The URL never includes the server's token: the browser is already logged in.

    Parameters
    ----------
    server: JupyterServer
        The server that serves the notebook (see ``find_server``).
    notebook: StrPath
        The notebook's path, inside the server's ``root_dir``.
    cell_id: str | None (optional, keyword only)
        The id of the cell to scroll to (nbformat >= 4.5).
    heading: str | None (optional, keyword only)
        The text of a markdown heading to scroll to, used if ``cell_id`` is None.
        Without either, the URL opens the notebook at the top.

    Raises
    ------
    ValueError
        If the notebook isn't inside the server's ``root_dir``.
    """
    relative = _relative_path(notebook, server.root_dir)
    if relative is None:
        raise ValueError(f"{notebook} isn't inside {server.root_dir}")
    base = server.url if server.url.endswith("/") else server.url + "/"
    url = base + "lab/tree/" + quote(relative.as_posix(), safe="/")

    if cell_id:
        fragment = f"cell-id={cell_id}"
    else:
        fragment = heading_anchor(heading) if heading else ""
    return f"{url}#{quote(fragment, safe=_FRAGMENT_SAFE)}" if fragment else url


def heading_anchor(heading: str) -> str:
    """
    The id JupyterLab gives a markdown heading, which a URL fragment can scroll to.

    JupyterLab takes the heading's rendered text and replaces each space with "-"
    (``createHeaderId`` in ``@jupyterlab/rendermime``), keeping all other characters:
    ``Exercise 2: Breadth-First Search`` becomes ``Exercise-2:-Breadth-First-Search``.
    The rendered text is approximated here: the ``#`` markers, emphasis, links, images,
    HTML tags and backslash escapes are removed and HTML entities decoded, while code
    spans and math are kept as they are.

    Parameters
    ----------
    heading: str
        The heading's markdown, with or without its ``#`` markers.
    """
    return _rendered_text(heading).replace(" ", "-")


def _read_server(info_file: Path) -> JupyterServer | None:
    """The server a ``jpserver-*.json`` file describes, or None if it can't be read."""
    try:
        info = json.loads(info_file.read_text(encoding="utf-8"))
        started = info_file.stat().st_mtime
    except OSError, ValueError:
        return None
    if not isinstance(info, dict):
        return None
    url = _server_url(info)
    root_dir = info.get("root_dir")
    if url is None or not isinstance(root_dir, str) or not root_dir:
        return None
    pid = info.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        pid = None
    return JupyterServer(url, Path(root_dir), started, pid)


def _server_url(info: Mapping[str, object]) -> str | None:
    """
    The URL of a server's info, with its ``base_url`` and a trailing "/", and without a
    query (a token) or fragment. None if it isn't an http(s) URL (e.g. a Unix socket's).
    """
    url = info.get("url")
    if not isinstance(url, str):
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    path = parts.path.rstrip("/") + "/"
    # jupyter_server's url includes the base_url already; older or hand-written files
    # may not
    base_url = info.get("base_url")
    if isinstance(base_url, str) and base_url.strip("/"):
        base = "/" + base_url.strip("/") + "/"
        if not path.endswith(base):
            path = path.rstrip("/") + base
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _relative_path(notebook: StrPath, root_dir: StrPath) -> PurePath | None:
    """
    ``notebook``'s path relative to the directory ``root_dir``, or None if it isn't
    inside it. Symbolic links are resolved if the paths don't match as given (e.g.
    ``/tmp`` and ``/private/tmp`` on macOS).
    """
    path, root = Path(os.path.abspath(notebook)), Path(os.path.abspath(root_dir))
    if path != root and path.is_relative_to(root):
        return path.relative_to(root)
    try:
        path, root = Path(os.path.realpath(path)), Path(os.path.realpath(root))
    except OSError, ValueError:
        return None
    if path != root and path.is_relative_to(root):
        return path.relative_to(root)
    return None


def _pid_running(pid: int) -> bool:
    """
    Whether the process ``pid`` is running. True if that can't be told: a server is only
    ignored when it has certainly stopped.
    """
    if sys.platform == "win32":
        return _pid_running_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError, OverflowError:
        return False
    except OSError:
        return True  # e.g. denied: it runs as another user
    return True


# Windows API constants for ``_pid_running_windows``
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_STILL_ACTIVE = 259


def _pid_running_windows(pid: int) -> bool:
    """
    ``_pid_running`` on Windows, where ``os.kill(pid, 0)`` would terminate the process:
    the process exists and hasn't exited yet.
    """
    import ctypes
    from ctypes import wintypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # denied: it exists, but runs elevated or as another user
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except OSError, ctypes.ArgumentError:
        return True


# Inline markdown whose content is kept as it is: a backslash escape (1), a code span
# (2, 3), or math (4, 5), which JupyterLab protects from the markdown parser.
_LITERAL = re.compile(r"\\([!-/:-@\[-`{-~])|(`+)(.+?)(?<!`)\2(?!`)|(\$\$?)(.+?)\4")
_PLACEHOLDER = re.compile(r"\x00(\d+)\x00")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_AUTOLINK = re.compile(r"<([a-zA-Z][a-zA-Z0-9+.-]*:[^\s<>]*)>")
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_EMPHASIS = re.compile(r"(\*{1,3}|~~)(?=\S)(.+?)(?<=\S)\1")
# underscores only emphasize at word boundaries, so snake_case names stay as they are
_UNDERSCORE_EMPHASIS = re.compile(r"(?<!\w)(_{1,3})(?=\S)(.+?)(?<=\S)\1(?!\w)")


def _rendered_text(markdown: str) -> str:
    """The text of a markdown heading as rendered, as far as headings need it."""
    text = markdown.strip()
    if opening := re.match(r"#{1,6}(?=[ \t]|$)", text):
        # an ATX heading: its markers, including the optional closing sequence
        text = re.sub(r"(?:^|[ \t]+)#+[ \t]*$", "", text[opening.end() :].strip())

    literals: list[str] = []

    def keep(match: re.Match[str]) -> str:
        if match[1] is not None:
            literal = match[1]
        elif match[3] is not None:
            literal = match[3]
            # CommonMark strips one space on each side of a code span's content
            if literal.startswith(" ") and literal.endswith(" ") and literal.strip():
                literal = literal[1:-1]
        else:
            literal = match[0]
        literals.append(literal)
        return f"\x00{len(literals) - 1}\x00"

    text = _LITERAL.sub(keep, text)
    text = _IMAGE.sub("", text)  # an image has no text
    text = _LINK.sub(r"\1", text)
    text = _AUTOLINK.sub(r"\1", text)
    text = _HTML_TAG.sub("", text)
    previous = None
    while previous != text:  # nested emphasis
        previous = text
        text = _EMPHASIS.sub(r"\2", text)
        text = _UNDERSCORE_EMPHASIS.sub(r"\2", text)
    text = html.unescape(text)
    # not stripped again: the space next to a removed image is part of the heading's text
    return _PLACEHOLDER.sub(lambda m: literals[int(m[1])], text)
