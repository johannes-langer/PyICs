import builtins
import copy
import difflib
import inspect
import reprlib
from collections.abc import Collection, Mapping
from os import PathLike
from pathlib import Path, PurePath
from types import ModuleType
from typing import Any

from rich.console import Group, RenderableType
from rich.markup import escape
from rich.syntax import Syntax
from rich.text import Text

import pyics.util.json_handling as json
from pyics.app.cli import CLI
from pyics.app.key_listener import KeyListener
from pyics.app.prompts import (
    CodeInput,
    PathInput,
    PromptCancelled,
    PromptSession,
    SelectInput,
    TextInput,
)
from pyics.data.task import Task
from pyics.tests import Test, check_specs, check_structure
from pyics.tests.tests import PrintTest, SimpleTest, SpecTest, StructureTest
from pyics.util import paths
from pyics.util.terminal import fullscreen

_TOTAL_STEPS = 5
_INFO = "Creating a new task..."
_ENTER_TEXT = "Enter text"

# Test types the CLI can create. Their constructor parameters are prompted for by
# ``_prompt_test``, and the first line of their docstring is shown as a hint.
_TEST_TYPES: list[type[Test]] = [SimpleTest, SpecTest, StructureTest, PrintTest]
_NO_MORE_TESTS = "Done"

# What to do with a file or test of a task that is being edited. Keeping is preselected,
# so that confirming every prompt leaves the task as it was.
_KEEP = "Keep"
_UPDATE = "Update"
_REPLACE = "Replace"
_EDIT = "Edit"
_REMOVE = "Remove"
_TEST_HINTS = {
    _KEEP: "Keep the test as it is.",
    _EDIT: "Go through the test's parameters, prefilled with its current values.",
    _REMOVE: "Remove the test from the task.",
}
# lines of a diff shown when a file on disk differs from its contents in the task file
_MAX_DIFF_LINES = 30
# name of the variable that holds a parameter's current value when editing a test, if
# its repr can't be used to prefill the input
_CURRENT = "current"


def _confirm(s: PromptSession, label: str, *, default: bool = True) -> bool:
    prompt = SelectInput(label, ["yes", "no"])
    prompt.set_answer("yes" if default else "no")
    return s.ask(prompt) == "yes"


def _check_file(raw: str) -> Path:
    """Return ``raw`` as a path (with ``~`` expanded) if it is an existing file, raise
    otherwise."""
    path = Path(raw).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"no file at {raw!r}")
    return path


def _check_new_file(raw: str, files: Collection[Path]) -> Path:
    """Return ``raw`` as a path if it is an existing file that isn't one of ``files``,
    raise otherwise."""
    path = _check_file(raw)
    if paths.absolute(path) in {paths.absolute(f) for f in files}:
        raise ValueError(f"{raw!r} is already one of the task's files")
    return path


def _prompt_files(
    s: PromptSession, kind: str, kept: list[Path] | None = None
) -> list[Path]:
    """
    Interactively collect a list of existing ``kind`` (e.g. "exercise") file paths, one
    at a time. When editing a task, the list starts with the task's ``kept`` files, and
    adding more is not preselected.
    """
    files = list(kept or [])
    editing = kept is not None

    # we do not need a solution file necessarily, nor more than one exercise file.
    if files or kind == "solution":
        label = f"Add another {kind} file?" if files else f"Add a {kind} file?"
        if not _confirm(s, label, default=not editing):
            return files

    while True:
        files.append(
            s.ask(
                PathInput(f"{kind.capitalize()} file path"),
                lambda raw: _check_new_file(raw, files),
            )
        )
        if not _confirm(s, f"Add another {kind} file?", default=not editing):
            return files


def _read(path: PathLike) -> str | None:
    """The contents of the file at ``path``, or None if it can't be read."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return None


def _diff(stored: str, on_disk: str) -> RenderableType:
    """How a file ``on_disk`` differs from its ``stored`` contents, as a unified diff of
    at most ``_MAX_DIFF_LINES`` lines."""
    lines = list(
        difflib.unified_diff(
            stored.splitlines(), on_disk.splitlines(), "stored", "on disk", lineterm=""
        )
    )
    shown = Syntax("\n".join(lines[:_MAX_DIFF_LINES]), "diff", background_color="default")
    if len(lines) <= _MAX_DIFF_LINES:
        return shown
    more = Text(f"... {len(lines) - _MAX_DIFF_LINES} more lines", style="dim")
    return Group(shown, more)


def _edit_files(
    s: PromptSession, kind: str, current: list[Path], stored: Mapping[Path, str]
) -> tuple[list[Path], dict[Path, str], dict[Path, Path | None]]:
    """
    Ask what to do with each of the ``current`` ``kind`` files of a task that is being
    edited, then for more files, like ``_prompt_files``. A file can be kept with the
    contents ``stored`` in the task file, updated to store its (changed) contents on
    disk instead, replaced by another file, or removed from the task.

    Returns
    -------
    tuple[list[Path], dict[Path, str], dict[Path, Path | None]]
        The files; the stored contents to keep, by path; and what became of each of the
        ``current`` files, by its absolute path: the file itself, its replacement, or
        None if it was removed.
    """
    kept: list[Path] = []
    contents: dict[Path, str] = {}
    moved: dict[Path, Path | None] = {}

    for i, path in enumerate(current):
        in_task, on_disk = stored.get(path), _read(path)

        options, hints, details = [], {}, None
        if in_task is not None:
            options.append(_KEEP)
            hints[_KEEP] = "Keep the file, with the contents stored in the task file."
            if on_disk is None:
                details = Text(
                    "The file isn't on disk. It is created from the stored contents when "
                    "the task is loaded.",
                    style="dim",
                )
        if on_disk is not None and on_disk != in_task:
            options.append(_UPDATE)
            hints[_UPDATE] = "Keep the file, and store its contents on disk instead."
            if in_task is not None:
                details = Group(
                    Text("The file on disk differs from the stored contents:"),
                    _diff(in_task, on_disk),
                )
        if in_task is None and on_disk is None:
            details = Text(
                "The file isn't on disk, and the task file stores no contents for it.",
                style="red",
            )
        options += [_REPLACE, _REMOVE]
        hints[_REPLACE] = "Use another file in its place." + (
            " Its tests are moved to the new file." if kind == "exercise" else ""
        )
        hints[_REMOVE] = "Remove the file from the task. The file itself is kept." + (
            " Its tests need to be edited or removed." if kind == "exercise" else ""
        )

        label = f"{kind.capitalize()} file {escape(str(path))}"
        choice = s.ask(SelectInput(label, options, hints=hints, details=details))
        if choice == _REMOVE:
            moved[paths.absolute(path)] = None
            continue

        new_path = path
        if choice == _REPLACE:
            # not one of the files that are already kept, or still to be
            others = [*kept, *current[i + 1 :]]
            prompt = PathInput(f"New {kind} file path", initial=str(path))
            new_path = s.ask(
                prompt, lambda raw, others=others: _check_new_file(raw, others)
            )
        elif choice == _KEEP:
            contents[path] = stored[path]
        # updated or replaced files are read when the task file is written
        moved[paths.absolute(path)] = new_path
        kept.append(new_path)

    return _prompt_files(s, kind, kept), contents, moved


def _load_namespace(raw: str) -> dict[str, Any]:
    """Execute the Python file at ``raw`` and return the variables it defines (excluding
    dunders and modules)."""
    path = _check_file(raw)
    namespace: dict[str, Any] = {}
    source = path.read_text(encoding="utf-8")
    exec(compile(source, str(path), "exec"), namespace)  # noqa: S102
    return {
        k: v
        for k, v in namespace.items()
        if not k.startswith("__") and not isinstance(v, ModuleType)
    }


def _check(test_cls: type[Test], param: inspect.Parameter, value: Any) -> Any:
    """Return ``value`` if it can be used for the ``test_cls`` constructor parameter
    ``param``, raise otherwise."""
    if test_cls is SpecTest and param.name == "expected":
        # e.g. the solution's function itself, stored as its spec dict. Also validates
        # spec dicts now, rather than when the test is constructed.
        value = check_specs.to_spec_dict(value)
    if test_cls is StructureTest and param.name == "rules":
        value = check_structure.validate_rules(value)
    if param.annotation is str and not isinstance(value, str):
        raise TypeError(f"expected str, got {type(value).__name__}")
    if param.annotation == list[str] and not (
        isinstance(value, list) and all(isinstance(v, str) for v in value)
    ):
        raise TypeError(f"expected a list of str, got {reprlib.repr(value)}")
    # checked now, rather than when the task file is written after all other fields
    json.dumps(value)
    return value


def _evaluates_to(code: str, value: Any) -> bool:
    """Whether ``code`` evaluates to ``value``, e.g. whether a ``repr`` is valid Python
    that can prefill an input for ``value``."""
    try:
        return bool(eval(code, {"__builtins__": builtins}) == value)
    except Exception:  # noqa: BLE001
        return False


def _prompt_param(
    s: PromptSession,
    test_cls: type[Test],
    param: inspect.Parameter,
    symbols: dict[str, Any],
    problem: str,
    current: Any = inspect.Parameter.empty,
) -> Any:
    """
    Ask for the value of the ``test_cls`` constructor parameter ``param``, prefilled with
    its ``current`` value when editing a test, otherwise with its default: plain text for
    ``str`` parameters (on several lines for the expected output of a ``PrintTest``),
    otherwise a Python expression, evaluated with ``symbols`` in scope. ``problem``, if
    not empty, is shown below the input.
    """
    lines = [Text(problem, style="red")] if problem else []
    editing = current is not param.empty
    value = current if editing else param.default
    has_value = value is not param.empty

    if param.annotation is str:
        str_symbols = {k: v for k, v in symbols.items() if isinstance(v, str)}
        if str_symbols:
            hints = {k: reprlib.repr(v) for k, v in str_symbols.items()}
            prompt = SelectInput(param.name, [*str_symbols, _ENTER_TEXT], hints=hints)
            if editing:  # the current value is prefilled in the text input
                prompt.set_answer(_ENTER_TEXT)
            choice = s.ask(prompt)
            if choice != _ENTER_TEXT:
                return str_symbols[choice]

        initial = value if isinstance(value, str) else ""
        details = Group(*lines) if lines else None
        multiline = test_cls is PrintTest and param.name == "expected"
        return s.ask(
            TextInput(param.name, multiline=multiline, initial=initial, details=details)
        )

    initial = repr(value) if has_value else ""
    if has_value and not _evaluates_to(initial, value):
        # e.g. an object whose repr isn't valid Python: prefilled by referencing it
        symbols = {**symbols, _CURRENT: value}
        initial = _CURRENT

    if test_cls is StructureTest and param.name == "rules":
        concepts = ", ".join(check_structure.CONCEPTS)
        lines.append(Text(f"Concepts: {concepts}", style="dim"))
    if symbols:
        lines.append(Text("Variables in scope:", style="dim"))
        lines.extend(
            Text(f"  {k} = {reprlib.repr(v)}", style="dim") for k, v in symbols.items()
        )
    prompt = CodeInput(
        param.name, initial=initial, details=Group(*lines) if lines else None
    )
    scope = {"__builtins__": builtins, **symbols}
    return s.ask(prompt, lambda raw: _check(test_cls, param, eval(raw, dict(scope))))


def _prompt_test(
    s: PromptSession,
    test_cls: type[Test],
    ex_files: list[Path],
    current: Test | None = None,
) -> Test:
    """
    Interactively define a test of type ``test_cls``, by asking for each parameter of its
    constructor. When editing the test ``current``, each one is prefilled with its
    current value.

    The user may reference a Python file first: its variables whose names match a
    parameter are used for it directly, and all of them are in scope for the expressions
    entered for the other parameters. ``file_path`` is always one of ``ex_files``.
    """
    symbols: dict[str, Any] = {}
    if _confirm(s, "Reference a file with test information?", default=current is None):
        symbols = s.ask(PathInput("Test information file"), _load_namespace)

    kwargs: dict[str, Any] = {}
    for param in inspect.signature(test_cls).parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        value = getattr(current, param.name, param.empty) if current else param.empty

        if param.name == "file_path":
            prompt = SelectInput(param.name, [str(f) for f in ex_files])
            if value is not param.empty:
                options = {paths.absolute(f): str(f) for f in ex_files}
                prompt.set_answer(options.get(paths.absolute(value), ""))  # pyright: ignore[reportArgumentType]
            kwargs[param.name] = Path(s.ask(prompt)).absolute()
            continue

        problem = ""
        if param.name in symbols:
            try:
                kwargs[param.name] = _check(test_cls, param, symbols[param.name])
                continue
            except (TypeError, ValueError) as err:
                problem = f"'{param.name}' from the test information file: {err}"
        kwargs[param.name] = _prompt_param(s, test_cls, param, symbols, problem, value)

    return test_cls(**kwargs)


def _describe_test(test: Test, problem: str = "") -> RenderableType:
    """The type and parameters of ``test``, below ``problem`` if it isn't empty."""
    lines = [Text(problem, style="red")] if problem else []
    lines.append(Text(type(test).__name__, style="dim"))
    for param in inspect.signature(type(test)).parameters.values():
        value = getattr(test, param.name, param.empty)
        if value is param.empty:
            continue
        text = str(value) if isinstance(value, PurePath) else reprlib.repr(value)
        lines.append(Text(f"  {param.name} = {text}", style="dim"))
    return Group(*lines)


def _prompt_tests(
    s: PromptSession, ex_files: list[Path], current: list[Test] | None = None
) -> list[Test]:
    """
    Interactively define any number of tests, choosing the test type for each. When
    editing a task, its ``current`` tests are kept, edited or removed first, and adding
    more is not preselected. A test whose file isn't one of ``ex_files`` can't be kept
    as it is.
    """
    tests: list[Test] = []
    ex = {paths.absolute(f) for f in ex_files}
    for i, test in enumerate(current or [], 1):
        options, problem = [_KEEP, _EDIT, _REMOVE], ""
        if paths.absolute(test.file_path) not in ex:
            options.remove(_KEEP)
            problem = (
                f"Its file {test.file_path} isn't one of the task's exercise files, so "
                "the test needs to be edited or removed."
            )
        label = f"Test {i} of {len(current or [])}: {escape(str(test.name))}"
        details = _describe_test(test, problem)
        choice = s.ask(SelectInput(label, options, hints=_TEST_HINTS, details=details))
        if choice == _KEEP:
            tests.append(test)
        elif choice == _EDIT:
            tests.append(_prompt_test(s, type(test), ex_files, current=test))

    test_types = {cls.__name__: cls for cls in _TEST_TYPES}
    hints = {
        name: (inspect.getdoc(cls) or "").splitlines()[0]
        for name, cls in test_types.items()
    }
    while True:
        hints[_NO_MORE_TESTS] = f"Finish adding tests ({len(tests)} added so far)."
        prompt = SelectInput(
            "Which kind of test do you want to add?",
            [*test_types, _NO_MORE_TESTS],
            hints=hints,
        )
        if current is not None:
            prompt.set_answer(_NO_MORE_TESTS)
        kind = s.ask(prompt)
        if kind == _NO_MORE_TESTS:
            return tests
        tests.append(_prompt_test(s, test_types[kind], ex_files))


def _repoint(test: Test, moved: Mapping[Path, Path | None]) -> Test:
    """``test``, or a copy of it testing the file its file was replaced by (see
    ``_edit_files``)."""
    new = moved.get(paths.absolute(test.file_path))
    if new is None:
        return test
    test = copy.copy(test)  # the task's own test is kept, since flows are replayed
    test.file_path = new
    return test


def _check_name(name: str, taken_names: Collection[str]) -> str:
    """Return ``name`` if it can be used for a task (or course), raise otherwise."""
    if not name:
        raise ValueError("the name may not be empty")
    if name in taken_names:
        raise ValueError(f"the course already has a task named {name!r}")
    return name


def _task_flow(
    s: PromptSession,
    *,
    taken_names: Collection[str] = (),
    current: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """
    The full task-creation flow. Replayed by ``PromptSession`` whenever the user goes
    back, so it must ask the same prompts given the same answers. The task name may not
    be one of ``taken_names``.

    With ``current``, the fields of an existing task (see ``Task.read_task_json``), the
    task is edited instead: every prompt is prefilled with its current value, so that
    confirming all of them leaves the task as it was. Changes to its exercise files
    carry over to its tests: those of a replaced file test the new file, and those of a
    removed file need to be edited or removed.

    Returns
    -------
    dict[str, Any]
        Keyword arguments for ``Task.create_task_json``.
    """
    s.cli.update_progress(0 / _TOTAL_STEPS)
    initial = current["name"] if current is not None else ""
    name = s.ask(
        TextInput("Task name", initial=initial), lambda n: _check_name(n, taken_names)
    )

    s.cli.update_progress(1 / _TOTAL_STEPS)
    initial = current["instructions"] if current is not None else ""
    instructions = s.ask(TextInput("Instructions", multiline=True, initial=initial))

    file_contents: dict[Path, str] = {}
    moved: dict[Path, Path | None] = {}

    s.cli.update_progress(2 / _TOTAL_STEPS)
    if current is None:
        ex_files = _prompt_files(s, "exercise")
    else:
        ex_files, ex_contents, moved = _edit_files(
            s, "exercise", current["ex_files"], current["file_contents"]
        )
        file_contents.update(ex_contents)

    s.cli.update_progress(3 / _TOTAL_STEPS)
    if current is None:
        sol_files = _prompt_files(s, "solution")
    else:
        sol_files, sol_contents, _ = _edit_files(
            s, "solution", current["sol_files"], current["file_contents"]
        )
        file_contents.update(sol_contents)

    s.cli.update_progress(4 / _TOTAL_STEPS)
    if current is None:
        tests = _prompt_tests(s, ex_files)
    else:
        current_tests = [_repoint(test, moved) for test in current["tests"]]
        tests = _prompt_tests(s, ex_files, current_tests)

    s.cli.update_progress(5 / _TOTAL_STEPS)
    return {
        "name": name,
        "instructions": instructions,
        "ex_files": ex_files,
        "sol_files": sol_files,
        "tests": tests or None,
        "file_contents": file_contents,
    }


def _create_task(fp: PathLike | None = None) -> None:
    """
    Console script to interactively create a task file.

    Parameters
    ----------
    fp: PathLike
        Output path for the resulting ``.pyicstask.json`` file.
    """
    with fullscreen():
        cli = CLI()
        cli()
        cli.update_info(_INFO)

        try:
            with KeyListener() as q:
                fields = PromptSession(cli, q, info=_INFO).run(_task_flow)

            if not fp:
                fp = Path(f"./{fields['name']}")
            fp = Task.create_task_json(fp, **fields)
            cli.update_info(f"Task written to {escape(str(fp))}")
        except PromptCancelled:
            cli.update_info("Cancelled, no task was created.")
        finally:
            cli.stop()
