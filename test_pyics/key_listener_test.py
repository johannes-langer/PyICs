"""
Reading keys from the terminal: the tokens key presses are queued as, and the terminal's
mode in the meantime.
"""

import io
import os
import sys
import threading
from collections.abc import Iterator

import pytest
from prompt_toolkit.input import PipeInput, create_pipe_input

from pyics.app.key_listener import KeyListener
from pyics.util import terminal

if sys.platform != "win32":
    import termios

TIMEOUT = 10  # seconds

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminals only")


# --- tokens, with keys typed into a pipe ---


@pytest.fixture
def pipe(monkeypatch: pytest.MonkeyPatch) -> Iterator[PipeInput]:
    """A pipe standing in for the terminal's input, which keys are typed into as the
    terminal sends them."""
    with create_pipe_input() as pipe:
        monkeypatch.setattr("pyics.app.key_listener._terminal_input", lambda: pipe)
        yield pipe


def _listen(pipe: PipeInput, typed: str, count: int) -> list[str]:
    """The first ``count`` tokens queued for the keys ``typed``. They are typed before
    keys are read, as on Windows, the pipe parses keys in the thread they are typed in."""
    pipe.send_text(typed)
    with KeyListener() as q:
        return [q.get(timeout=TIMEOUT) for _ in range(count)]


@pytest.mark.parametrize(
    ("typed", "tokens"),
    [
        pytest.param(
            "\x1b[A\x1b[B\x1b[C\x1b[D", ["UP", "DOWN", "RIGHT", "LEFT"], id="arrows"
        ),
        # as sent in application cursor-key mode
        pytest.param(
            "\x1bOA\x1bOB\x1bOC\x1bOD", ["UP", "DOWN", "RIGHT", "LEFT"], id="ss3"
        ),
        # once nothing follows it
        pytest.param("\x1b", ["\x1b"], id="lone esc"),
        # ctrl-c, enter, and backspace as terminals send it, or ctrl-h
        pytest.param("\x03\r\x7f\x08", ["\x03", "\r", "\x7f", "\x08"], id="control"),
        pytest.param("aä😀", ["a", "ä", "😀"], id="unicode"),
        # e.g. delete and F5, which type no character
        pytest.param("\x1b[3~\x1b[15~x", ["\x1b[3~", "\x1b[15~", "x"], id="other keys"),
        pytest.param("\x1b[200~ab\rc\x1b[201~", ["a", "b", "\r", "c"], id="paste"),
        pytest.param("\x1bx", ["\x1b", "x"], id="alt"),
        # as xterm sends alt-up
        pytest.param("\x1b[1;3A", ["\x1b", "UP"], id="alt arrow"),
    ],
)
def test_keys_are_queued_as_tokens(
    pipe: PipeInput, typed: str, tokens: list[str]
) -> None:
    assert _listen(pipe, typed, len(tokens)) == tokens


def test_a_stdin_that_isnt_a_terminal_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rather than waiting for keys forever."""
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    errors: list[Exception] = []

    def listen() -> None:
        try:
            with KeyListener():
                pass
        except OSError as err:
            errors.append(err)

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    thread.join(TIMEOUT)
    assert not thread.is_alive()
    assert [str(err) for err in errors] == ["stdin is not a terminal"]


# --- the terminal's mode, with a pseudo-terminal as stdin ---


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """A pseudo-terminal standing in for stdin. Returns its other end, which keys are
    typed into."""
    keyboard, tty = os.openpty()
    stdin = open(tty, encoding="utf-8")  # noqa: SIM115 - closed on teardown
    monkeypatch.setattr(sys, "stdin", stdin)
    try:
        yield keyboard
    finally:
        stdin.close()
        os.close(keyboard)


@posix_only
def test_raw_mode_while_listening(tty: int) -> None:
    cooked = termios.ICANON | termios.ECHO | termios.ISIG
    assert termios.tcgetattr(sys.stdin)[3] & cooked == cooked
    with KeyListener():
        mode = termios.tcgetattr(sys.stdin)
        assert not mode[3] & cooked
        # output is still processed, so that "\n" starts a new line
        assert mode[1] & termios.OPOST
    assert termios.tcgetattr(sys.stdin)[3] & cooked == cooked


@posix_only
def test_keys_typed_into_the_terminal(tty: int) -> None:
    with KeyListener() as q:
        os.write(tty, "ä\x1b[A\x03".encode())
        assert [q.get(timeout=TIMEOUT) for _ in range(3)] == ["ä", "UP", "\x03"]


@posix_only
def test_the_terminal_mode_is_preserved(tty: int) -> None:
    """E.g. around running a task's tests while keys are read, which may change it."""
    with KeyListener():
        raw = termios.tcgetattr(sys.stdin)
        with terminal.preserve_terminal_mode():
            mode = termios.tcgetattr(sys.stdin)
            mode[3] |= termios.ICANON | termios.ECHO
            termios.tcsetattr(sys.stdin, termios.TCSANOW, mode)
        assert termios.tcgetattr(sys.stdin) == raw
