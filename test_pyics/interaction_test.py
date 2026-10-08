"""
Simulate interaction with the UI and the test files.

The main interface runs like ``pyics`` runs it: keys are typed into a pipe standing in
for the keyboard, frames are read from what the interface draws to stdout, and exercise
files are saved to have the current task's tests run.
"""

import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

import pytest
from prompt_toolkit.input import PipeInput, create_pipe_input
from prompt_toolkit.key_binding import KeyPress

from pyics.app.cli import CLI
from pyics.app.pyics import PyIC
from pyics.data import Course, Task
from pyics.scripts import main
from pyics.tests.tests import PrintTest, SimpleTest, Test
from pyics.util.terminal import ALT_SCREEN_ENTER, ALT_SCREEN_LEAVE, SHOW_CURSOR

# a crash in one of the interface's threads fails the test, instead of only warning
pytestmark = pytest.mark.filterwarnings(
    "error::pytest.PytestUnhandledThreadExceptionWarning"
)

CTRL_C, ESC, UP, DOWN, LEFT = "\x03", "\x1b", "\x1b[A", "\x1b[B", "\x1b[D"
ENTER = "\r"
CLEAR_SCREEN = "\033[H\033[2J"  # starts every frame the CLI draws
TIMEOUT = 10  # seconds

ADD_TEMPLATE = "def add(a, b):\n    return 0\n"
ADD_SOLUTION = "def add(a, b):\n    return a + b\n"
GREET_MAIN = '\nif __name__ == "__main__":\n    print(greet(input("Name: ")))\n'
GREET_TEMPLATE = 'def greet(name):\n    return ""\n' + GREET_MAIN
GREET_SOLUTION = 'def greet(name):\n    return f"Hello, {name}!"\n' + GREET_MAIN
DOUBLE_TEMPLATE = "def double(x):\n    return 0\n"
DOUBLE_SOLUTION = "def double(x):\n    return 2 * x\n"


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
            PrintTest(
                "prints a greeting",
                greet,
                "Name: World\nHello, World!",
                inputs=["World"],
            ),
        ),
    ]
    Course.create_course_manifest(tmp_path / "course", task_files)
    return add, greet


@pytest.fixture
def other_course(exercises: tuple[Path, Path], tmp_path: Path) -> Path:
    """A second course, "other", of one task, in a subdirectory of the working
    directory. Listed after the course of ``exercises``."""
    double = tmp_path / "other" / "double.py"
    double.parent.mkdir()
    task_file = _task(
        double,
        "Implement double(x).",
        DOUBLE_TEMPLATE,
        DOUBLE_SOLUTION,
        SimpleTest("doubles", double, "double", 4, function_args=[2]),
    )
    return Course.create_course_manifest(double.parent / "other", [task_file])


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
        self.written: list[str] = []  # everything flushed, in order
        self._pending: list[str] = []

    def write(self, text: str) -> int:
        self._pending.append(text)
        return len(text)

    def flush(self) -> None:
        text = "".join(self._pending)
        self._pending.clear()
        self.written.append(text)
        if text.startswith(CLEAR_SCREEN):
            self.frame = text


class _Keyboard:
    """
    Stands in for the terminal's input, which the ``KeyListener`` reads keys from: keys
    are typed into a pipe as the terminal sends them. Typing and reading are serialized,
    as on Windows, the pipe parses keys in the thread they are typed in.
    """

    def __init__(self, pipe: PipeInput) -> None:
        self._pipe = pipe
        self._lock = threading.Lock()

    def send_text(self, text: str) -> None:
        with self._lock:
            self._pipe.send_text(text)

    def raw_mode(self) -> AbstractContextManager[None]:
        return self._pipe.raw_mode()

    def read_keys(self) -> list[KeyPress]:
        with self._lock:
            return self._pipe.read_keys()

    def flush_keys(self) -> list[KeyPress]:
        with self._lock:
            return self._pipe.flush_keys()


class App:
    """The main interface, set up like ``pyics`` does."""

    def __init__(self, keyboard: _Keyboard, stdout: _Stdout) -> None:
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

    @property
    def selecting(self) -> bool:
        """Whether the course selector is shown."""
        return "Select Course" in self.screen

    def resize(self, width: int, height: int) -> None:
        """Resize the terminal."""
        self.cli.console.size = (width, height)

    def press(self, *keys: str) -> None:
        for key in keys:
            self.keyboard.send_text(key)
            # typing speed, so e.g. esc isn't read as the start of the next key
            time.sleep(0.05)

    def wait_for(self, condition: Callable[[], object]) -> None:
        deadline = time.monotonic() + TIMEOUT
        while not condition():
            if time.monotonic() > deadline:
                pytest.fail(f"Timed out. The last frame drawn was:\n{self.screen}")
            time.sleep(0.01)

    def select(self, option: int = 0) -> None:
        """Select the ``option``-th course, once the course selector is shown."""
        self.wait_for(lambda: self.selecting)
        self.press(*[DOWN] * option, ENTER)

    def quit(self) -> None:
        """Quit with ctrl-c, which is handled after all keys pressed before it."""
        self.press(CTRL_C)
        self.wait_until_quit()

    def wait_until_quit(self) -> None:
        self.pyics.thread.join(TIMEOUT)
        assert not self.pyics.thread.is_alive()
        assert not self.cli.running


class _Terminal(NamedTuple):
    keyboard: _Keyboard  # typed into, to press keys
    stdout: _Stdout


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Terminal]:
    """A keyboard standing in for the terminal's input, and stdout as the CLI writes to
    it."""
    with create_pipe_input() as pipe:
        keyboard = _Keyboard(pipe)
        monkeypatch.setattr("pyics.app.key_listener._terminal_input", lambda: keyboard)
        # replaced where the CLI looks it up, as pytest's output capturing resets
        # sys.stdout between the setup, call and teardown of a test
        stdout = _Stdout()
        monkeypatch.setattr("pyics.app.cli.sys", SimpleNamespace(stdout=stdout))
        threads = set(threading.enumerate())
        try:
            yield _Terminal(keyboard, stdout)
        finally:
            for thread in set(threading.enumerate()) - threads:
                thread.join(TIMEOUT)
            # the app's own pytest runs remove this when they finish, but pytest
            # expects it to be set until this test is torn down
            os.environ.setdefault("PYTEST_CURRENT_TEST", "")


@pytest.fixture
def launch(terminal: _Terminal) -> Iterator[Callable[[], App]]:
    """Starts the main interface in the working directory, without waiting for
    anything. Several can be started one after the other, like sessions."""
    apps: list[App] = []

    def start() -> App:
        app = App(terminal.keyboard, terminal.stdout)
        app.pyics()
        apps.append(app)
        return app

    yield start
    for app in apps:
        if app.pyics.thread.is_alive():
            # cancels the course selector, if it is shown, or quits
            terminal.keyboard.send_text(CTRL_C)
        app.pyics.running = app.cli.running = False


@pytest.fixture
def app(exercises: tuple[Path, Path], launch: Callable[[], App]) -> App:
    """The main interface, running on the course of ``exercises``. Ready once it shows
    the results of the first task's tests."""
    app = launch()
    app.select()
    app.wait_for(lambda: "2 failed" in app.screen)
    return app


# --- startup ---


def test_startup_shows_the_first_task(app: App, exercises: tuple[Path, Path]) -> None:
    add, _ = exercises
    app.resize(100, 40)  # wide enough for the footer's hotkeys on one line
    assert app.cli.info_text == "Implement add(a, b)."
    assert str(app.cli.main_title) == add.as_posix()
    assert app.cli.percentage == 0
    shown = [
        "Implement add(a, b).",  # header
        "FF",  # main content
        "adds failed: 0 == 3",
        "adds negatives failed: 0 == -3",
        "<ctrl-c> Quit | <n> Next | <p> Previous | <t> Trace | <c> Courses | <↑/↓> Scroll",
    ]
    # a frame drawn while the interface is updated only shows part of the update
    app.wait_for(lambda: all(text in app.screen for text in shown))


# --- keys ---


def test_ctrl_c_quits(app: App) -> None:
    app.quit()
    assert not app.cli.running


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
    assert str(app.cli.main_title) == greet.as_posix()

    app.press("p")
    app.wait_for(lambda: "Implement add(a, b)." in app.screen)
    app.wait_for(lambda: "2 passed" in app.screen)  # the solution was kept
    app.wait_for(lambda: app.cli.percentage == 0.5)


def test_arrow_keys_scroll_the_main_content(app: App) -> None:
    app.resize(80, 24)  # leaves 7 lines for the main content
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
    app.wait_for(lambda: "prints a greeting failed: 'Name: World' ==" in app.screen)

    _save(greet, GREET_SOLUTION)
    app.wait_for(lambda: "2 passed" in app.screen)
    app.wait_for(lambda: app.cli.percentage == 1)
    # the output of the print test isn't written over the interface
    output = capsys.readouterr().out
    assert "Name: World" not in output
    assert "Hello, World!" not in output


# --- tracing failed tests ---


def test_t_traces_the_failed_tests_until_pressed_again(
    app: App, exercises: tuple[Path, Path]
) -> None:
    add, _ = exercises
    app.resize(80, 80)  # tall enough to show the traces
    assert "Press t to trace failed tests with snoop." in app.screen
    app.press("t")
    app.wait_for(lambda: "Return value from add: 0" in app.screen)
    assert app.course.tracing
    shown = [
        "Tracing failed tests with snoop, press t to stop.",
        'Call to add in File "add.py", line 1',
        "a = -1",
        "2 |     return 0",
    ]
    app.wait_for(lambda: all(text in app.screen for text in shown))

    # still traced once the exercise is saved
    _save(add, "def add(a, b):\n    return a - b\n")
    app.wait_for(lambda: "Return value from add: -1" in app.screen)

    app.press("t")
    app.wait_for(
        lambda: "Press t to trace failed tests with snoop." in app.screen
        and "Return value from add" not in app.screen
    )
    assert not app.course.tracing


def test_tracing_is_off_for_another_task(
    app: App, exercises: tuple[Path, Path]
) -> None:
    add, _ = exercises
    app.resize(80, 80)  # tall enough to show the traces
    app.press("t")
    app.wait_for(lambda: "Return value from add: 0" in app.screen)
    _save(add, ADD_SOLUTION)
    app.wait_for(lambda: "2 passed" in app.screen)

    app.press("n")
    app.wait_for(lambda: "greets failed: '' == 'Hello, World!'" in app.screen)
    app.wait_for(lambda: "Press t to trace failed tests with snoop." in app.screen)
    assert not app.course.tracing
    assert "Return value from greet" not in app.screen

    # both the function test and the print test are traced
    app.press("t")
    app.wait_for(lambda: "Return value from greet: ''" in app.screen)
    app.wait_for(lambda: "Call to <module>" in app.screen)

    app.press("p")
    app.wait_for(lambda: "Implement add(a, b)." in app.screen)
    assert not app.course.tracing


# --- course selection ---


def _write_manifest(path: Path, name: str, tasks: dict[str, str]) -> None:
    """Write a course manifest by hand, e.g. one that can't be loaded."""
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"name": name, "tasks": tasks, "progress": [0] * len(tasks)}
    path.write_text(json.dumps(manifest))


def _no_course_message() -> str:
    return f"No course found in {Path.cwd()} (searched 2 directories deep)."


def test_without_a_course_the_app_quits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launch: Callable[[], App]
) -> None:
    monkeypatch.chdir(tmp_path)
    app = launch()
    app.wait_until_quit()
    assert app.pyics.exit_message == _no_course_message()


def test_unusable_manifests_are_not_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launch: Callable[[], App]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "unreadable.pyicscourse.json").write_text("{")
    _write_manifest(tmp_path / "empty.pyicscourse.json", "Empty", {})
    task = {"t": "t.pyicstask.json"}
    _write_manifest(tmp_path / "a/b/c/deep.pyicscourse.json", "Too deep", task)
    _write_manifest(tmp_path / ".venv/venv.pyicscourse.json", "Skipped", task)

    app = launch()
    app.wait_until_quit()
    assert app.pyics.exit_message == _no_course_message()


def test_a_single_course_is_offered(
    exercises: tuple[Path, Path], launch: Callable[[], App]
) -> None:
    add, _ = exercises
    app = launch()
    shown = [
        "❯ course",
        "course.pyicscourse.json",  # the hint
        "<↑/↓> select | <enter> confirm | <ctrl-c> cancel",
    ]
    app.wait_for(lambda: all(text in app.screen for text in shown))
    assert app.cli.info_text == "Select a course..."
    assert app.pyics.current_course is None

    app.press(ENTER)
    app.wait_for(lambda: "2 failed" in app.screen)
    assert app.cli.info_text == "Implement add(a, b)."
    assert str(app.cli.main_title) == add.as_posix()


def test_selecting_one_of_several_courses(
    other_course: Path, launch: Callable[[], App]
) -> None:
    app = launch()
    app.wait_for(lambda: "❯ course" in app.screen and "  other" in app.screen)
    assert app.screen.index("course") < app.screen.index("other")

    app.select(1)
    app.wait_for(lambda: "doubles failed: 0 == 4" in app.screen)
    assert app.cli.info_text == "Implement double(x)."


def test_courses_of_the_same_name_are_told_apart(
    exercises: tuple[Path, Path], tmp_path: Path, launch: Callable[[], App]
) -> None:
    task_files = sorted(tmp_path.glob("*.pyicstask.json"))  # add, greet
    (tmp_path / "course.pyicscourse.json").unlink()
    Course.create_course_manifest(tmp_path / "a/same", task_files, name="Same")
    second = Course.create_course_manifest(
        tmp_path / "b/same", task_files[::-1], name="Same"
    )

    app = launch()
    app.wait_for(
        lambda: "Same (a/same.pyicscourse.json)" in app.screen
        and "Same (b/same.pyicscourse.json)" in app.screen
    )
    app.select(1)
    app.wait_for(lambda: "greets failed" in app.screen)
    assert [p.resolve() for p in app.pyics.courses] == [second.resolve()]


def test_a_course_that_cant_be_started_is_reported(
    exercises: tuple[Path, Path], tmp_path: Path, launch: Callable[[], App]
) -> None:
    # listed before the course of ``exercises``
    _write_manifest(
        tmp_path / "broken/broken.pyicscourse.json",
        "Broken",
        {"gone": "gone.pyicstask.json"},
    )
    app = launch()
    app.select(0)
    app.wait_for(
        lambda: app.selecting and app.cli.info_text.startswith("[red]Can't start Broken")
    )
    assert app.pyics.courses == {}

    app.select(1)
    app.wait_for(lambda: "2 failed" in app.screen)


def test_ctrl_c_in_the_course_selector_quits(
    exercises: tuple[Path, Path], launch: Callable[[], App]
) -> None:
    app = launch()
    app.wait_for(lambda: app.selecting)
    app.quit()
    assert app.pyics.exit_message is None


def test_c_goes_back_to_the_course_selector(other_course: Path, app: App) -> None:
    app.press("c")
    # the course that was left is selected, and its progress shown
    app.wait_for(
        lambda: app.selecting and "❯ course" in app.screen and "0% solved" in app.screen
    )
    assert "<esc> back" in app.screen
    assert app.cli.info_text == "Select a course..."

    app.select(1)
    app.wait_for(lambda: "doubles failed: 0 == 4" in app.screen)
    assert app.cli.info_text == "Implement double(x)."


def test_esc_in_the_course_selector_returns_to_the_course(app: App) -> None:
    course = app.course
    app.press("c")
    app.wait_for(lambda: app.selecting)
    app.press(ESC)
    app.wait_for(lambda: not app.selecting and "2 failed" in app.screen)
    assert app.course is course
    assert app.cli.info_text == "Implement add(a, b)."


def test_ctrl_c_in_the_course_selector_of_a_course_quits(app: App) -> None:
    app.press("c")
    app.wait_for(lambda: app.selecting)
    app.quit()


def test_a_course_is_resumed_where_it_was_left(
    app: App, exercises: tuple[Path, Path]
) -> None:
    add, _ = exercises
    _save(add, ADD_SOLUTION)
    app.wait_for(lambda: app.cli.percentage == 0.5)
    app.press("n")
    app.wait_for(lambda: "greets failed" in app.screen)
    course = app.course

    app.press("c")
    app.wait_for(lambda: app.selecting and "50% solved" in app.screen)
    app.press(ENTER)
    app.wait_for(lambda: not app.selecting and "greets failed" in app.screen)
    assert app.course is course
    assert app.cli.info_text == "Implement greet(name)."
    assert app.cli.percentage == 0.5


# --- progress from the exercise files ---


def test_progress_is_checked_from_the_exercise_files(
    exercises: tuple[Path, Path], tmp_path: Path, launch: Callable[[], App]
) -> None:
    """E.g. in a new session, as progress isn't stored in the course manifest."""
    add, _ = exercises
    _save(add, ADD_SOLUTION)
    app = launch()
    app.select()
    # starts on the first unsolved task
    app.wait_for(lambda: "greets failed" in app.screen)
    assert app.cli.info_text == "Implement greet(name)."
    app.wait_for(lambda: app.cli.percentage == 0.5)
    manifest = json.loads((tmp_path / "course.pyicscourse.json").read_text())
    assert manifest["progress"] == [0, 0]


def test_a_solved_course_starts_on_its_last_task(
    exercises: tuple[Path, Path], launch: Callable[[], App]
) -> None:
    add, greet = exercises
    _save(add, ADD_SOLUTION)
    _save(greet, GREET_SOLUTION)
    app = launch()
    app.select()
    app.wait_for(lambda: "2 passed" in app.screen)
    assert app.cli.info_text == "Implement greet(name)."
    app.wait_for(lambda: app.cli.percentage == 1)


def test_the_progress_check_creates_no_exercise_files(
    exercises: tuple[Path, Path], launch: Callable[[], App]
) -> None:
    _, greet = exercises
    greet.unlink()
    app = launch()
    app.select()
    app.wait_for(lambda: "2 failed" in app.screen)
    app.wait_for(lambda: app.course.check_status == "")  # all tasks checked
    assert not greet.exists()


def test_exercise_files_are_kept(
    exercises: tuple[Path, Path], launch: Callable[[], App]
) -> None:
    add, _ = exercises
    work = "def add(a, b):\n    return a\n"
    _save(add, work)
    app = launch()
    app.select()
    app.wait_for(lambda: "2 failed" in app.screen)
    app.press("c")
    app.select()
    app.wait_for(lambda: not app.selecting and "2 failed" in app.screen)
    app.quit()

    second_session = launch()
    second_session.select()
    second_session.wait_for(lambda: "2 failed" in second_session.screen)
    assert add.read_text() == work


# --- hints ---

ADD_HINTS = ["Python adds numbers with +.", "return a + b"]


@pytest.fixture
def hinted_app(exercises: tuple[Path, Path], launch: Callable[[], App]) -> App:
    """Like ``app``, with two hints for the first task (add); the second has none."""
    task_file = Path("add.pyicstask.json")
    fields = Task.read_task_json(task_file)
    Task.create_task_json(task_file, **{**fields, "help": ADD_HINTS})
    app = launch()
    app.select()
    app.wait_for(lambda: "2 failed" in app.screen)
    return app


def test_h_reveals_one_hint_at_a_time(hinted_app: App) -> None:
    app = hinted_app
    assert "<h> Hint" in app.screen
    assert "Hints" not in app.screen

    app.press("h")
    app.wait_for(lambda: "Hints 1/2" in app.screen)
    assert ADD_HINTS[0] in app.screen and ADD_HINTS[1] not in app.screen
    assert "2 failed" in app.screen  # below the hints

    app.press("h")
    app.wait_for(lambda: "Hints 2/2" in app.screen)
    assert ADD_HINTS[0] in app.screen and ADD_HINTS[1] in app.screen

    app.press("h")  # no hint left
    app.quit()
    assert app.course.hints == ADD_HINTS
    assert "Hints 2/2" in app.screen


def test_revealed_hints_stay_revealed(
    hinted_app: App, exercises: tuple[Path, Path]
) -> None:
    app = hinted_app
    add, _ = exercises
    app.press("h")
    app.wait_for(lambda: "Hints 1/2" in app.screen)

    _save(add, ADD_SOLUTION)  # reruns the tests
    app.wait_for(lambda: "2 passed" in app.screen)
    assert "Hints 1/2" in app.screen

    app.press("n", "h")  # the second task has no hints
    app.wait_for(lambda: "Implement greet(name)." in app.screen)
    app.wait_for(lambda: "greets failed" in app.screen)
    assert "Hints" not in app.screen

    app.press("p")
    app.wait_for(lambda: "Implement add(a, b)." in app.screen)
    app.wait_for(lambda: "Hints 1/2" in app.screen)


def test_a_revealed_hint_is_scrolled_into_view(hinted_app: App) -> None:
    app = hinted_app
    app.resize(80, 24)  # leaves 7 lines for the main content
    app.wait_for(lambda: "2 failed" not in app.screen)
    app.press(DOWN, DOWN)
    app.wait_for(lambda: app.cli.scroll_offset == 2)
    app.press("h")
    app.wait_for(lambda: app.cli.scroll_offset == 0)
    app.wait_for(lambda: ADD_HINTS[0] in app.screen)


# --- the pyics script ---


@pytest.fixture
def run_main(
    terminal: _Terminal, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], str]:
    """Runs the ``pyics`` script's ``main`` in the terminal until it returns, and returns
    everything it wrote to stdout. Cancels the course selector once shown."""
    # replaced where the alternate screen is entered and left, like for the CLI
    monkeypatch.setattr(
        "pyics.util.terminal.sys",
        SimpleNamespace(stdout=terminal.stdout, stdin=sys.stdin, platform=sys.platform),
    )
    monkeypatch.setattr(main, "setup_logging", lambda *args: None)

    def cancel_course_selector() -> None:
        deadline = time.monotonic() + TIMEOUT
        while "Select Course" not in terminal.stdout.frame:
            if time.monotonic() > deadline:
                return
            time.sleep(0.01)
        terminal.keyboard.send_text(CTRL_C)

    def run() -> str:
        threading.Thread(target=cancel_course_selector, daemon=True).start()
        main.main()
        return "".join(terminal.stdout.written)

    return run


def test_main_leaves_the_alternate_screen_once_quit(
    exercises: tuple[Path, Path], run_main: Callable[[], str]
) -> None:
    written = run_main()
    assert written.startswith(ALT_SCREEN_ENTER)
    assert "Select Course" in written
    # nothing is drawn once the alternate screen has been left
    assert written.endswith(SHOW_CURSOR + ALT_SCREEN_LEAVE)


def test_main_prints_why_it_quits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_main: Callable[[], str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    assert run_main().endswith(ALT_SCREEN_LEAVE)
    assert capsys.readouterr().out == _no_course_message() + "\n"
