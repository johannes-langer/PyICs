from collections import Counter
from dataclasses import dataclass, field
import json
from pathlib import Path
import queue
import threading
import time
from collections.abc import Iterable
from typing import Any

from rich.markup import escape
from rich.text import Text


from pyics.app.actions import (
    Courses,
    Hint,
    KeyAction,
    Next,
    Previous,
    Quit,
    ScrollDown,
    ScrollUp,
    ToggleTrace,
)
from pyics.app.cli import CLI
from pyics.app.key_listener import hey_listen
from pyics.app.prompts import PromptBack, PromptCancelled, SelectInput
from pyics.data import Course
from pyics.data.task import wait_for_test_runs
from pyics.distribution import dependencies, sources
from pyics.distribution.archive import ORIGIN_FILE, ArchiveError
from pyics.distribution.dependencies import Problem
from pyics.distribution.pull import PullError, pull
from pyics.distribution.sources import CourseEntry, Source
from pyics.notebook import cells, links, sections
from pyics.util import paths
from pyics.util.discovery import COURSE_SUFFIX, find_courses

_SELECT_INFO = "Select a course..."
# how long to wait on shutdown for test runs to finish, which restore the terminal mode
# they started in
_SHUTDOWN_TIMEOUT = 5  # seconds


@dataclass(frozen=True)
class _LocalCourse:
    """A course in the working directory, as offered in the course selector."""

    path: Path
    # its requirements that aren't met, which keep it from being started
    problems: list[Problem] = field(default_factory=list)


@dataclass(frozen=True)
class _SourceCourse:
    """A course in one of the sources, offered in the course selector to be pulled."""

    source: Source
    entry: CourseEntry


def _exercise_title(path: Path, section: str | None) -> Text:
    """
    How the main panel's title shows the exercise file at ``path``: its path, and for a
    notebook whose task is about its ``section``, the section's title. A notebook's
    path and section title are links where the terminal supports them (see
    ``links.exercise_title``). The section is looked up in the saved notebook, so the
    title follows it when its cells are moved.
    """
    display = path.as_posix()
    if not cells.is_notebook(path):
        return Text(display)
    if section is None:
        return links.exercise_title(path, display)
    try:
        found = sections.get_section(cells.read(path), section)
    except sections.SectionNotFound:
        return links.exercise_title(path, display, missing_section=section)
    except OSError, cells.NotebookError:
        # e.g. while it is being saved; its tests tell what is wrong
        return links.exercise_title(path, display)
    return links.exercise_title(
        path, display, section_title=found.title, cell_id=found.anchor.id
    )


def _cant_start(name: str, problems: list[Problem]) -> str:
    """Why the course ``name`` can't be started, as markup for the info bar."""
    return (
        f"[red]Can't start {escape(name)}:[/red] it needs "
        f"{escape(dependencies.describe(problems))}. "
        f"{escape(dependencies.install_hint(problems))}"
    )


class PyIC:
    """PyIC main thread"""

    __current_course: Course | None

    def __init__(self, cli: CLI, *, max_depth: int = 2):
        """
        Parameters
        ----------
        cli: CLI
            The CLI to draw to.
        max_depth: int (optional, keyword only)
            Set the maximum depth to walk the working directory for courses. For example,
            with the default value (2), ``./subdir/subsubdir/c.pyicscourse.json`` is
            found, but not ``./subdir/subsubdir/subsubsubdir/c.pyicscourse.json``.
        """
        self.cwd = Path.cwd()
        self.cli = cli
        self.max_depth = max_depth
        self.running = False
        self.key_actions: dict[str, KeyAction] = {}
        self.__current_course: Course | None = None
        # the course that was left for the course selector, to go back to
        self.__previous_course: Course | None = None
        # the courses loaded in this session, by manifest path, so that a course that is
        # left and selected again resumes where it was left
        self.courses: dict[Path, Course] = {}
        # set once the current course is entered, to draw it as a whole
        self.__entered = False
        self.__shown_check_status = ""
        # set once a hint is revealed, to draw it
        self.__hint_revealed = False
        # shown in the header the next time the course selector is
        self.__select_info = _SELECT_INFO
        # printed once the interface has been left (see ``scripts.main``)
        self.exit_message: str | None = None

        self._register_actions()
        self.cli.update_hotkeys({a.ref: a.name for a in self.key_actions.values()})

    def _find_courses(self) -> dict[str, _LocalCourse]:
        """
        Find the courses in the working directory (see ``max_depth``), skipping
        manifests that can't be read or have no tasks.

        Returns
        -------
        dict[str, _LocalCourse]
            The courses, ordered by path, by their labels in the course selector: their
            course names, followed by their paths if names are taken several times.
        """
        entries: list[tuple[str, _LocalCourse]] = []
        for path, manifest in sorted(find_courses(self.cwd, self.max_depth).items()):
            if not manifest["tasks"]:
                continue
            name = manifest.get("name")
            if not isinstance(name, str) or not name:
                name = path.name.removesuffix(COURSE_SUFFIX)
            problems = dependencies.check_manifest(manifest)
            entries.append((name, _LocalCourse(path, problems)))

        counts = Counter(name for name, _ in entries)
        return {
            f"{name} ({self._display_path(c.path)})" if counts[name] > 1 else name: c
            for name, c in entries
        }

    def _pulled_courses(self, local: Iterable[_LocalCourse]) -> set[tuple[str, str]]:
        """The sources and slugs of the ``local`` courses that were pulled from a
        source (see ``pyics.distribution.pull``)."""
        pulled: set[tuple[str, str]] = set()
        for course in local:
            for directory in course.path.parents:
                if not directory.is_relative_to(self.cwd):
                    break
                try:
                    origin = json.loads(
                        (directory / ORIGIN_FILE).read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    continue
                if isinstance(origin, dict):
                    pulled.add((str(origin.get("source")), str(origin.get("slug"))))
                break
        return pulled

    def _find_source_courses(
        self, local: dict[str, _LocalCourse]
    ) -> dict[str, _SourceCourse]:
        """
        The courses in the sources that haven't been pulled to the working directory,
        as known from their indexes as they were fetched last, so that showing them
        needs no connection.

        Returns
        -------
        dict[str, _SourceCourse]
            The courses by their labels in the course selector: their titles, followed
            by their sources.
        """
        pulled = self._pulled_courses(local.values())
        found: dict[str, _SourceCourse] = {}
        for source, entry in sources.cached_courses():
            if (source.name, entry.slug) in pulled or (self.cwd / entry.slug).exists():
                continue
            label = f"{entry.title} (pull from {source.name})"
            if label in local or label in found:
                label = f"{entry.title} (pull {source.name}/{entry.slug})"
            found[label] = _SourceCourse(source, entry)
        return found

    def _source_course_hint(self, course: _SourceCourse) -> str:
        entry = course.entry
        lines = [entry.description] if entry.description else []
        version = f" {entry.version}" if entry.version else ""
        lines.append(
            f"Version{version} in {course.source.name}, pulled to ./{entry.slug} once "
            "selected."
        )
        if problems := dependencies.check(entry.requires, entry.requires_pyics):
            lines.append(f"Needs {dependencies.describe(problems)}.")
        return "\n".join(lines)

    def _pull_course(self, course: _SourceCourse) -> Path | None:
        """
        Pull ``course`` to the working directory, showing its progress in the info
        bar. Returns its manifest if it can be started, or None if it can't, with the
        reason why shown in the info bar.
        """
        entry = course.entry
        self.cli.update_info(f"Pulling {escape(entry.title)} from {course.source.name}...")
        try:
            pulled = pull(f"{course.source.name}/{entry.slug}", cwd=self.cwd)
        except (PullError, ArchiveError, OSError) as err:
            self.cli.update_info(
                f"[red]Can't pull {escape(entry.title)}:[/red] {escape(str(err))}"
            )
            return None
        if pulled.problems:
            self.cli.update_info(_cant_start(pulled.info.title, pulled.problems))
            return None
        return paths.absolute(pulled.manifest_path)

    def _display_path(self, path: Path) -> str:
        return paths.relative_to(path, self.cwd).as_posix()

    def _select_course(self, q: queue.Queue[str]) -> None:
        """
        Let the user select a course in the working directory and enter it. Cancelling
        (ctrl-c) quits, and going back (esc) returns to the course that was left for the
        selector, if any. Quits with an ``exit_message`` if there is no course.

        Parameters
        ----------
        q: queue.Queue[str]
            The queue which collects key presses
        """
        courses = self._find_courses()
        source_courses = self._find_source_courses(courses)
        if not courses and not source_courses:
            self.exit_message = (
                f"No course found in {self.cwd} "
                f"(searched {self.max_depth} directories deep)."
            )
            self.running = False
            return

        hints = {}
        for label, local in courses.items():
            hints[label] = self._display_path(local.path)
            if (course := self.courses.get(local.path)) is not None:
                hints[label] += f"\n{float(course.progress):.0%} solved"
            if local.problems:
                hints[label] += f"\nNeeds {dependencies.describe(local.problems)}."
        for label, source_course in source_courses.items():
            hints[label] = self._source_course_hint(source_course)
        options = [*courses, *source_courses]
        prompt = SelectInput("Select Course", options, hints=hints)
        previous = self.__previous_course
        for label, local in courses.items():
            if previous is not None and self.courses.get(local.path) is previous:
                prompt.set_answer(label)

        self.cli.update_info(self.__select_info)
        self.__select_info = _SELECT_INFO
        while True:
            try:
                label = prompt.run(self.cli, q, allow_back=previous is not None)
            except PromptBack:
                assert previous is not None
                self._enter_course(previous)
                return
            except PromptCancelled:
                self.running = False
                return

            if label in source_courses:
                pulled = self._pull_course(source_courses[label])
                if pulled is None:
                    # offered again as a course in the working directory, if it was
                    # pulled
                    self.__select_info = self.cli.info_text
                    return
                path = pulled
            elif courses[label].problems:
                self.cli.update_info(_cant_start(label, courses[label].problems))
                continue
            else:
                path = courses[label].path
            if (course := self.courses.get(path)) is None:
                try:
                    course = Course.load_course_manifest(path, check_progress=True)
                except Exception as err:  # noqa: BLE001
                    self.cli.update_info(
                        f"[red]{type(err).__name__}:[/red] {escape(str(err))}"
                    )
                    continue
                self.courses[path] = course
            self._enter_course(course)
            return

    def _enter_course(self, course: Course) -> None:
        """Make ``course`` the current course, to be drawn as a whole."""
        self.__current_course = course
        self.__previous_course = None
        self.__entered = True

    def leave_course(self) -> None:
        """Leave the current course for the course selector. The course keeps its
        state, and is resumed if it is selected again."""
        if self.__current_course is not None:
            self.__previous_course = self.__current_course
            self.__current_course = None

    def _register_actions(self) -> None:
        """ """
        key_actions: list[KeyAction] = [
            Quit(cli=self.cli, pyics=self),
            Next(pyics=self),
            Previous(pyics=self),
            ToggleTrace(pyics=self),
            Courses(pyics=self),
            ScrollUp(cli=self.cli, pyics=self),
            ScrollDown(cli=self.cli, pyics=self),
            Hint(pyics=self),
        ]
        for a in key_actions:
            self.key_actions[a.key] = a

    def __call__(self, *args: Any, **kwds: Any) -> Any:
        """ """
        self.running = True
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    @property
    def current_course(self) -> Course | None:
        """"""
        return self.__current_course

    def next_task(self) -> None:
        """
        Choose the next task in the self.tasks iteratable. Specifically load the task now
        to avoid populating memory with fully loaded tasks.
        """
        if self.current_course is not None:
            self.current_course.next()

    def reveal_hint(self) -> None:
        """Reveal the current task's next hint, if it has one left, to be drawn."""
        if self.current_course is not None and self.current_course.reveal_hint():
            self.__hint_revealed = True

    @hey_listen("q")
    def run(self, q: queue.Queue[str]):
        """ """
        self.cli()

        try:
            while self.running:
                if self.current_course is None:
                    self._select_course(q)
                    continue

                self.check_keypress(q)
                if self.current_course is not None:
                    self._update_course(self.current_course)

                time.sleep(0.05)
        finally:
            self.running = False
            for course in self.courses.values():
                course.stop_progress_check(_SHUTDOWN_TIMEOUT)
            # the key listener restores the terminal mode once this returns
            wait_for_test_runs(_SHUTDOWN_TIMEOUT)
            self.cli.stop()

    def _update_course(self, course: Course) -> None:
        """
        Tick the current ``course`` and draw what has changed, or all of it if it has
        just been entered.
        """
        entered, self.__entered = self.__entered, False
        hint_revealed, self.__hint_revealed = self.__hint_revealed, False

        if course.error is not None:
            # it can't be started, so it isn't resumed either
            self.courses = {p: c for p, c in self.courses.items() if c is not course}
            self.__current_course = None
            self.__select_info = (
                f"[red]Can't start {escape(course.name)}:[/red] "
                f"{escape(str(course.error))}"
            )
            return

        new_test_results, new_progress = course.tick()
        task_changed = course.task_changed
        if task_changed:
            course.clear_task_changed()

        if task_changed or entered:
            info = escape(course.name if course.loading else course.instructions)
            if not course.loading and (hint := course.kernel_hint) is not None:
                info += f"\n[dim]{escape(hint)}[/dim]"
            self.cli.update_info(info)

        if course.loading:
            if entered or course.check_status != self.__shown_check_status:
                self.__shown_check_status = course.check_status
                self.cli.update_main_content(course.status)
        elif new_test_results or task_changed or entered or hint_revealed:
            # clear before reading the status, so results that arrive in between
            # are picked up on the next tick instead of being dropped
            course.clear_test_results()
            if hint_revealed:
                # the hints are shown above the test results
                self.cli.scroll_offset = 0
            task_sections = course.sections
            titles = [
                _exercise_title(p, task_sections.get(paths.absolute(p)))
                for p in course.exercise_files
            ]
            self.cli.update_main_content(
                course.status, Text(", ", style="dim").join(titles)
            )

        if new_progress or entered:
            self.cli.update_progress(float(course.progress))

    def check_keypress(self, q: queue.Queue[str]) -> None:
        """
        Check if a key has been pressed, and if so, try to match it to known actions.

        Parameters
        ----------
        q: Queue
            The queue which collects key presses
        """
        try:
            key = q.get_nowait()

            self.key_actions.get(key, lambda: ...)()

        except queue.Empty:
            pass
