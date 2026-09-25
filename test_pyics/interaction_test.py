"""
Simulate interaction with the UI and the test files.

The main interface runs like ``pyics`` runs it: keys are typed into a pseudo-terminal
standing in for the keyboard, frames are read from what the interface draws to stdout,
and exercise files are saved to have the current task's tests run.
"""

import os
import signal
import sys
import termios
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyics.app.cli import CLI
from pyics.app.pyics import PyIC
from pyics.data import Course, Task
from pyics.tests.tests import PrintTest, SimpleTest, Test

# a crash in one of the interface's threads fails the test, instead of only warning
pytestmark = pytest.mark.filterwarnings(
    "error::pytest.PytestUnhandledThreadExceptionWarning"
)

CTRL_C, ESC, UP, DOWN, LEFT = "\x03", "\x1b", "\x1b[A", "\x1b[B", "\x1b[D"
CLEAR_SCREEN = "\033[H\033[2J"  # starts every frame the CLI draws
TIMEOUT = 10  # seconds

ADD_TEMPLATE = "def add(a, b):\n    return 0\n"
ADD_SOLUTION = "def add(a, b):\n    return a + b\n"
GREET_MAIN = '\nif __name__ == "__main__":\n    print(greet("World"))\n'
GREET_TEMPLATE = 'def greet(name):\n    return ""\n' + GREET_MAIN
GREET_SOLUTION = 'def greet(name):\n    return f"Hello, {name}!"\n' + GREET_MAIN


def _task(
    ex: Path, instructions: str, template: str, solution: str, *tests: Test
) -> Path:
    """Write a task file like ``create_task`` does, named after the exercise file."""
    sol = ex.with_name(f"{ex.stem}_solution.py")
    ex.write_text(template)
    sol.write_text(solution)
    return Task.create_task_json(
        ex.with_suffix(""), ex.stem, instructions, ex, sol, list(tests)
    )


@pytest.fixture
def exercises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """The exercise files of a course of two tasks, in the working directory."""
    monkeypatch.chdir(tmp_path)
    add, greet = tmp_path / "add.py", tmp_path / "greet.py"
    task_files = [
        _task(
            add,
            "Implement add(a, b).",
            ADD_TEMPLATE,
            ADD_SOLUTION,
            SimpleTest("adds", add, "add", 3, function_args=[1, 2]),
            SimpleTest("adds negatives", add, "add", -3, function_args=[-1, -2]),
        ),
        _task(
            greet,
            "Implement greet(name).",
            GREET_TEMPLATE,
            GREET_SOLUTION,
            SimpleTest(
                "greets", greet, "greet", "Hello, World!", function_args=["World"]
            ),
            PrintTest("prints a greeting", greet, "Hello, World!"),
        ),
    ]
    Course.create_course_manifest(tmp_path / "course", task_files)
    return add, greet


def _save(path: Path, code: str) -> None:
    """Save ``code`` to the exercise file at ``path``, as an editor would."""
    # changes are detected by the modification time, which is only updated every few
    # milliseconds on some systems
    mtime = path.stat().st_mtime + 1
    path.write_text(code)
    os.utime(path, (mtime, mtime))


class _Stdout:
    """Stands in for stdout, keeping the last frame the CLI has drawn completely."""

    def __init__(self) -> None:
        self.frame = ""
        self._pending: list[str] = []

    def write(self, text: str) -> int:
        self._pending.append(text)
        return len(text)

    def flush(self) -> None:
        text = "".join(self._pending)
        self._pending.clear()
        if text.startswith(CLEAR_SCREEN):
            self.frame = text


class App:
    """The main interface, set up like ``pyics`` does."""

    def __init__(self, keyboard: int, stdout: _Stdout) -> None:
        self.keyboard = keyboard
        self.stdout = stdout
        self.cli = CLI()
        self.cli.console.size = (80, 40)  # tall enough to show all test results
        self.pyics = PyIC(self.cli)

    @property
    def screen(self) -> str:
        """The last frame drawn: plain text, apart from cursor movements."""
        return self.stdout.frame

    @property
    def course(self) -> Course:
        course = self.pyics.current_course
        assert course is not None
        return course

    def resize(self, width: int, height: int) -> None:
        """Resize the terminal."""
        self.cli.console.size = (width, height)
        os.kill(os.getpid(), signal.SIGWINCH)

    def press(self, *keys: str) -> None:
        for key in keys:
            os.write(self.keyboard, key.encode())
            # typing speed, so e.g. esc isn't read as the start of the next key
            time.sleep(0.05)

    def wait_for(self, condition: Callable[[], object]) -> None:
        deadline = time.monotonic() + TIMEOUT
        while not condition():
            if time.monotonic() > deadline:
                pytest.fail(f"Timed out. The last frame drawn was:\n{self.screen}")
            time.sleep(0.01)

    def quit(self) -> None:
        """Quit with ctrl-c, which is handled after all keys pressed before it."""
        self.press(CTRL_C)
        self.pyics.thread.join(TIMEOUT)
        assert not self.pyics.thread.is_alive()


@pytest.fixture
def app(exercises: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> Iterator[App]:
    """The main interface, running on the course of ``exercises``. Ready once it shows
    the results of the first task's tests."""
    keyboard, terminal = os.openpty()
    stdin = open(terminal)  # noqa: SIM115 - closed on teardown
    monkeypatch.setattr(sys, "stdin", stdin)
    # replaced where the CLI looks it up, as pytest's output capturing resets
    # sys.stdout between the setup, call and teardown of a test
    stdout = _Stdout()
    monkeypatch.setattr("pyics.app.cli.sys", SimpleNamespace(stdout=stdout))
    threads = set(threading.enumerate())

    app = App(keyboard, stdout)
    app.pyics()
    try:
        app.wait_for(lambda: "2 failed" in app.screen)
        yield app
    finally:
        app.pyics.running = app.cli.running = False
        for thread in set(threading.enumerate()) - threads:
            thread.join(TIMEOUT)
        # the app's own pytest runs remove this when they finish, but pytest expects it
        # to be set until this test is torn down
        os.environ.setdefault("PYTEST_CURRENT_TEST", "")
        stdin.close()
        os.close(keyboard)


# --- startup ---


def test_startup_shows_the_first_task(app: App, exercises: tuple[Path, Path]) -> None:
    add, _ = exercises
    assert app.cli.info_text == "Implement add(a, b)."
    assert app.cli.main_title.plain == add.as_posix()
    assert app.cli.percentage == 0
    shown = [
        "Implement add(a, b).",  # header
        "FF",  # main content
        "adds failed: 0 == 3",
        "adds negatives failed: 0 == -3",
        "<ctrl-c> Quit | <n> Next | <p> Previous | <↑/↓> Scroll",  # footer
    ]
    # a frame drawn while the interface is updated only shows part of the update
    app.wait_for(lambda: all(text in app.screen for text in shown))


# --- keys ---


def test_ctrl_c_quits_and_restores_the_terminal(app: App) -> None:
    assert not termios.tcgetattr(sys.stdin)[3] & termios.ICANON  # raw mode
    app.quit()
    assert not app.cli.running
    assert termios.tcgetattr(sys.stdin)[3] & termios.ICANON


@pytest.mark.parametrize(
    "key", ["n", "p", "x", LEFT, ESC], ids=["n", "p", "x", "left", "esc"]
)
def test_keys_without_effect_on_the_first_unsolved_task(app: App, key: str) -> None:
    app.press(key)
    app.quit()
    assert app.course.instructions == app.cli.info_text == "Implement add(a, b)."


def test_next_and_previous_move_between_tasks_once_solved(
    app: App, exercises: tuple[Path, Path]
) -> None:
    add, greet = exercises
    _save(add, ADD_SOLUTION)
    app.wait_for(lambda: "2 passed" in app.screen)

    app.press("n")
    app.wait_for(lambda: "Implement greet(name)." in app.screen)
    app.wait_for(lambda: "greets failed: '' == 'Hello, World!'" in app.screen)
    assert app.cli.main_title.plain == greet.as_posix()

    app.press("p")
    app.wait_for(lambda: "Implement add(a, b)." in app.screen)
    app.wait_for(lambda: "2 passed" in app.screen)  # the solution was kept
    app.wait_for(lambda: app.cli.percentage == 0.5)


def test_arrow_keys_scroll_the_main_content(app: App) -> None:
    app.resize(80, 24)  # leaves 8 lines for the main content
    app.wait_for(lambda: "2 failed" not in app.screen)
    top = app.screen
    app.press(DOWN)
    app.wait_for(lambda: app.cli.scroll_offset == 1)
    app.wait_for(lambda: app.screen != top)
    # as sent in application cursor-key mode, and by the mouse wheel
    app.press("\x1bOB")
    app.wait_for(lambda: app.cli.scroll_offset == 2)
    app.press(UP, UP)
    app.wait_for(lambda: app.cli.scroll_offset == 0)
    app.wait_for(lambda: app.screen == top)


# --- running tests on save ---


def test_saving_a_solution_solves_the_task(
    app: App, exercises: tuple[Path, Path]
) -> None:
    add, _ = exercises
    _save(add, ADD_SOLUTION)
    app.wait_for(lambda: "2 passed" in app.screen)
    assert "100%" in app.screen
    assert "FAILURES" not in app.screen
    app.wait_for(lambda: app.cli.percentage == 0.5)
    app.wait_for(lambda: "50%" in app.screen)  # the progress bar


def test_progress_follows_the_latest_save(
    app: App, exercises: tuple[Path, Path]
) -> None:
    add, _ = exercises
    _save(add, ADD_SOLUTION)
    app.wait_for(lambda: app.cli.percentage == 0.5)

    _save(add, "def add(a, b):\n    return abs(a) + abs(b)\n")
    app.wait_for(lambda: "1 failed, 1 passed" in app.screen)
    assert "adds negatives failed: 3 == -3" in app.screen
    app.wait_for(lambda: app.cli.percentage == 0)


def test_syntax_errors_fail_the_tests(app: App, exercises: tuple[Path, Path]) -> None:
    add, _ = exercises
    _save(add, "def add(a, b)\n    return a + b\n")
    app.wait_for(lambda: "adds failed: expected ':'" in app.screen)

    _save(add, ADD_SOLUTION)
    app.wait_for(lambda: "2 passed" in app.screen)


def test_solving_all_tasks(
    app: App, exercises: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    add, greet = exercises
    _save(add, ADD_SOLUTION)
    app.wait_for(lambda: "2 passed" in app.screen)
    app.press("n")
    app.wait_for(
        lambda: "prints a greeting failed: '' == 'Hello, World!'" in app.screen
    )

    _save(greet, GREET_SOLUTION)
    app.wait_for(lambda: "2 passed" in app.screen)
    app.wait_for(lambda: app.cli.percentage == 1)
    # the output of the print test isn't written over the interface
    assert "Hello, World!" not in capsys.readouterr().out
