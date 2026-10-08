import sys
from collections.abc import Mapping
from os import PathLike
from pathlib import Path
from typing import Any

from rich.markup import escape

import pyics.util.json_handling as json
from pyics.app.cli import CLI
from pyics.app.key_listener import KeyListener
from pyics.app.prompts import PromptCancelled, PromptSession
from pyics.data.course import Course
from pyics.data.task import Task
from pyics.scripts.create_course import (
    _course_flow,
    _CourseTask,
    _names_taken_elsewhere,
    _rename_in_courses,
)
from pyics.util import paths
from pyics.util.discovery import COURSE_SUFFIX, find_courses, find_task_files
from pyics.util.terminal import fullscreen


def _read_course(
    fp: Path,
    manifest: Mapping[str, Any],
    courses: Mapping[Path, Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[Path, int]]:
    """
    Read the course ``manifest`` (read from ``fp``) and its tasks' task files for
    ``_course_flow``. The tasks can't be renamed to the names of the other tasks in the
    other ``courses`` (see ``find_courses``) that include them.

    Returns
    -------
    tuple[dict[str, Any], dict[Path, int]]
        The course's ``name`` and ``tasks`` (as ``_CourseTask``), and the progress of
        its tasks, by their task files' absolute paths.
    """
    tasks: list[_CourseTask] = []
    progress: dict[Path, int] = {}
    solved = manifest.get("progress", [])
    for i, (key, rel) in enumerate(manifest["tasks"].items()):
        path = paths.resolve(rel, fp.parent)
        progress[paths.absolute(path)] = solved[i] if i < len(solved) else 0
        try:
            fields = Task.read_task_json(path)
        except Exception as err:  # noqa: BLE001
            problem = f"The task file can't be read ({err}), so it can only be removed."
            tasks.append(_CourseTask(path, key, None, problem=problem))
            continue
        taken = _names_taken_elsewhere(path, courses)
        tasks.append(_CourseTask(path, fields["name"], fields, taken))
    return {"name": manifest.get("name", ""), "tasks": tasks}, progress


def _edit_course(course_file: PathLike, max_depth: int = 3) -> None:
    """
    Console script to interactively edit a course manifest, going through the
    course-creation flow with every prompt prefilled from the course. Its tasks can be
    kept, edited (rewriting their task files) or removed, and more added like when
    creating a course. The manifest is rewritten in place, and each task keeps its
    progress.

    The other course manifests below the working directory that include a task that is
    renamed are kept consistent with it, like in ``edit_task``.

    Parameters
    ----------
    course_file: PathLike
        The ``.pyicscourse.json`` file to edit.
    max_depth: int
        How many directory levels below the working directory are searched for task
        files and course manifests, so the search stays short when run in e.g. the home
        directory.
    """
    course_file = Path(course_file)
    if not course_file.name.endswith(COURSE_SUFFIX):
        sys.exit(f"error: {course_file} is not a course manifest (*{COURSE_SUFFIX})")
    try:
        manifest = json.load_plain(course_file)
        if not isinstance(manifest["tasks"], dict):
            raise TypeError("its tasks are not a mapping of names to task files")
    except Exception as err:  # noqa: BLE001
        sys.exit(f"error: can't read the course manifest {course_file}: {err}")

    with fullscreen():
        cli = CLI()
        cli()
        cli.update_info("Searching for task files and course manifests...")

        try:
            # searched once up front, since the flow is replayed whenever the user
            # goes back
            found = find_task_files(Path.cwd(), max_depth)
            courses = find_courses(Path.cwd(), max_depth)
            courses.pop(paths.absolute(course_file), None)  # rewritten as a whole
            current, progress = _read_course(course_file, manifest, courses)
            info = f"Editing the course {escape(str(current['name']))}..."
            cli.update_info(info)

            with KeyListener() as q:
                fields = PromptSession(cli, q, info=info).run(
                    lambda s: _course_flow(s, found, current)
                )

            task_files: list[PathLike] = []
            renamed: dict[Path, None] = {}  # ordered set
            for path, task_fields in fields["tasks"]:
                if task_fields is not None:
                    path = Task.create_task_json(path, **task_fields)
                    for fp in _rename_in_courses(path, task_fields["name"], courses):
                        renamed[fp] = None
                task_files.append(path.absolute())

            fp = Course.create_course_manifest(
                course_file,
                task_files,
                name=fields["name"],
                progress=[progress.get(paths.absolute(p), 0) for p in task_files],
                metadata=manifest,  # e.g. its requires, which aren't edited here
            )
            message = f"Course written to {fp}"
            if renamed:
                message += f", and tasks renamed in {', '.join(map(str, renamed))}"
        except PromptCancelled:
            message = "Cancelled, the course was not changed."
        finally:
            cli.stop()
    # after leaving the alternate screen, so that it stays visible
    print(message)
