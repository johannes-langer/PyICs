import builtins
import inspect
import reprlib
import sys
from os import PathLike
from pathlib import Path
from types import ModuleType
from typing import Any

import jsonargparse
from rich.console import Group
from rich.text import Text

import pyics.util.json_handling as json
from pyics.app.cli import ALT_SCREEN_ENTER, ALT_SCREEN_LEAVE, CLI, SHOW_CURSOR
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
from pyics.tests import Test
from pyics.tests.tests import SimpleTest

_TOTAL_STEPS = 5
_INFO = "Creating a new task..."

# Test types the CLI can create. Their constructor parameters are prompted for by
# ``_prompt_test``, and the first line of their docstring is shown as a hint.
_TEST_TYPES: list[type[Test]] = [SimpleTest]
_NO_MORE_TESTS = "Done"


def _confirm(s: PromptSession, label: str) -> bool:
    return s.ask(SelectInput(label, ["yes", "no"])) == "yes"


def _prompt_files(s: PromptSession, kind: str) -> list[Path]:
    """Interactively collect a list of ``kind`` (e.g. "exercise") file paths, one at a
    time."""
    files: list[Path] = []
    while True:
        files.append(Path(s.ask(PathInput(f"{kind.capitalize()} file path"))))
        if not _confirm(s, f"Add another {kind} file?"):
            return files


def _load_namespace(path: str) -> dict[str, Any]:
    """Execute the Python file at ``path`` and return the variables it defines (excluding
    dunders and modules)."""
    namespace: dict[str, Any] = {}
    exec(compile(Path(path).read_text(), path, "exec"), namespace)  # noqa: S102
    return {
        k: v
        for k, v in namespace.items()
        if not k.startswith("__") and not isinstance(v, ModuleType)
    }


def _check(param: inspect.Parameter, value: Any) -> Any:
    """Return ``value`` if it can be used for the test constructor parameter ``param``,
    raise otherwise."""
    if param.annotation is str and not isinstance(value, str):
        raise TypeError(f"expected str, got {type(value).__name__}")
    # checked now, rather than when the task file is written after all other fields
    json.dumps(value)
    return value


def _prompt_param(
    s: PromptSession, param: inspect.Parameter, symbols: dict[str, Any], problem: str
) -> Any:
    """
    Ask for the value of ``param``, prefilled with its default: plain text for ``str``
    parameters, otherwise a Python expression, evaluated with ``symbols`` in scope.
    ``problem``, if not empty, is shown below the input.
    """
    lines = [Text(problem, style="red")] if problem else []
    has_default = param.default is not param.empty

    if param.annotation is str:
        initial = param.default if has_default else ""
        details = Group(*lines) if lines else None
        return s.ask(TextInput(param.name, initial=initial, details=details))

    if symbols:
        lines.append(Text("Variables in scope:", style="dim"))
        lines.extend(
            Text(f"  {k} = {reprlib.repr(v)}", style="dim") for k, v in symbols.items()
        )
    prompt = CodeInput(
        param.name,
        initial=repr(param.default) if has_default else "",
        details=Group(*lines) if lines else None,
    )
    scope = {"__builtins__": builtins, **symbols}
    return s.ask(prompt, lambda raw: _check(param, eval(raw, dict(scope))))


def _prompt_test(s: PromptSession, test_cls: type[Test], ex_files: list[Path]) -> Test:
    """
    Interactively define a test of type ``test_cls``, by asking for each parameter of its
    constructor.

    The user may reference a Python file first: its variables whose names match a
    parameter are used for it directly, and all of them are in scope for the expressions
    entered for the other parameters. ``file_path`` is always one of ``ex_files``.
    """
    symbols: dict[str, Any] = {}
    if _confirm(s, "Reference a file with test information?"):
        symbols = s.ask(PathInput("Test information file"), _load_namespace)

    kwargs: dict[str, Any] = {}
    for param in inspect.signature(test_cls).parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue

        if param.name == "file_path":
            ex_file = s.ask(SelectInput(param.name, [str(f) for f in ex_files]))
            kwargs[param.name] = Path(ex_file).absolute()
            continue

        problem = ""
        if param.name in symbols:
            try:
                kwargs[param.name] = _check(param, symbols[param.name])
                continue
            except TypeError as err:
                problem = f"'{param.name}' from the test information file: {err}"
        kwargs[param.name] = _prompt_param(s, param, symbols, problem)

    return test_cls(**kwargs)


def _prompt_tests(s: PromptSession, ex_files: list[Path]) -> list[Test]:
    """Interactively define any number of tests, choosing the test type for each."""
    test_types = {cls.__name__: cls for cls in _TEST_TYPES}
    hints = {
        name: (inspect.getdoc(cls) or "").splitlines()[0]
        for name, cls in test_types.items()
    }
    tests: list[Test] = []
    while True:
        hints[_NO_MORE_TESTS] = f"Finish adding tests ({len(tests)} added so far)."
        kind = s.ask(
            SelectInput(
                "Which kind of test do you want to add?",
                [*test_types, _NO_MORE_TESTS],
                hints=hints,
            )
        )
        if kind == _NO_MORE_TESTS:
            return tests
        tests.append(_prompt_test(s, test_types[kind], ex_files))


def _task_flow(s: PromptSession) -> dict[str, Any]:
    """
    The full task-creation flow. Replayed by ``PromptSession`` whenever the user goes
    back, so it must ask the same prompts given the same answers.

    Returns
    -------
    dict[str, Any]
        Keyword arguments for ``Task.create_task_json``.
    """
    s.cli.update_progress(0 / _TOTAL_STEPS)
    name = s.ask(TextInput("Task name"))

    s.cli.update_progress(1 / _TOTAL_STEPS)
    instructions = s.ask(TextInput("Instructions", multiline=True))

    s.cli.update_progress(2 / _TOTAL_STEPS)
    ex_files = _prompt_files(s, "exercise")

    s.cli.update_progress(3 / _TOTAL_STEPS)
    sol_files = _prompt_files(s, "solution")

    s.cli.update_progress(4 / _TOTAL_STEPS)
    # TODO: PrintTest, StructureTest, SpecTest and SizeTest can't be created from the
    # CLI yet (see _TEST_TYPES).
    tests = _prompt_tests(s, ex_files)

    s.cli.update_progress(5 / _TOTAL_STEPS)
    return {
        "name": name,
        "instructions": instructions,
        "ex_files": ex_files,
        "sol_files": sol_files,
        "tests": tests or None,
    }


def _create_task(fp: PathLike = Path("./new_task")) -> None:
    """
    Console script to interactively create a task file.

    Parameters
    ----------
    fp: PathLike
        Output path for the resulting ``.pyicstask.json`` file.
    """
    sys.stdout.write(ALT_SCREEN_ENTER)
    sys.stdout.flush()

    cli = CLI()
    cli()
    cli.update_info(_INFO)

    try:
        with KeyListener() as q:
            fields = PromptSession(cli, q, info=_INFO).run(_task_flow)

        Task.create_task_json(fp, **fields)
        cli.update_info(f"Task written to {fp}")
    except PromptCancelled:
        cli.update_info("Cancelled, no task was created.")
    finally:
        cli.running = False
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.write(ALT_SCREEN_LEAVE)
        sys.stdout.flush()


def create_task() -> None:
    jsonargparse.auto_cli(_create_task)
