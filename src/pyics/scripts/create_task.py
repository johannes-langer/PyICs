import ast
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
from rich.markup import escape
from rich.syntax import Syntax
from rich.text import Text

import pyics.util.json_handling as json
from pyics.app.cli import ALT_SCREEN_ENTER, ALT_SCREEN_LEAVE, CLI, SHOW_CURSOR
from pyics.app.key_listener import KeyListener
from pyics.app.prompts import PromptCancelled, PromptSession
from pyics.data.task import Task
from pyics.tests import Test
from pyics.tests.tests import SimpleTest

_TOTAL_STEPS = 5
_INFO = "Creating a new task..."

_EDIT = "Edit input"
_EVAL = "Run as Python expression (eval)"
_FILE = "Load from file"

_SELECT_SYMBOL = "Select a symbol from the file"
_ENTER_VALUE = "Enter value"
_LEAVE_EMPTY = "Leave empty (use default)"
_EMPTY = object()  # returned by _prompt_param for a parameter left empty


def _prompt_files(s: PromptSession, kind: str) -> list[PathLike]:
    """
    Interactively collect a list of file paths, one at a time.

    Parameters
    ----------
    s: PromptSession
        Session to ask the prompts in.
    kind: str
        Label for what's being collected (e.g. "exercise" or "solution"), used in prompts.
    """
    files: list[PathLike] = []
    while True:
        files.append(Path(s.path(f"{kind.capitalize()} file path")))
        again = s.select(f"Add another {kind} file?", ["yes", "no"])
        if again == "no":
            return files


def _load_symbols(s: PromptSession, label: str) -> tuple[Path, dict[str, Any]]:
    """
    Ask for a Python file and execute it, re-asking until it runs.

    Returns
    -------
    tuple[Path, dict[str, Any]]
        The file's path, and the variables it defines (excluding dunders and modules).
    """
    while True:
        fp = Path(s.path(label))
        namespace: dict[str, Any] = {}
        try:
            exec(compile(fp.read_text(), str(fp), "exec"), namespace)  # noqa: S102
        except Exception as err:  # noqa: BLE001
            s.cli.update_info(
                f"[red]Could not run {escape(str(fp))}:[/red] {escape(str(err))}"
            )
            continue

        return fp, {
            k: v
            for k, v in namespace.items()
            if not k.startswith("__") and not isinstance(v, ModuleType)
        }


def _select_symbol(s: PromptSession, label: str, fp: Path, symbols: dict[str, Any]) -> str:
    """Let the user pick which of ``symbols`` (defined in ``fp``) holds ``label``, with a
    short preview of each value as its hint."""
    return s.select(
        f"Which symbol in {fp} holds the value for '{label}'?",
        list(symbols),
        option_hints=[reprlib.repr(v) for v in symbols.values()],
    )


def _prompt_value_from_file(s: PromptSession, label: str) -> Any:
    """
    Load a value by executing a user-supplied Python file and picking one of the names it
    defines: ``label`` itself if defined, otherwise whichever symbol the user selects.
    """
    while True:
        fp, symbols = _load_symbols(s, f"Path to file defining '{label}'")
        if label in symbols:
            return symbols[label]
        if symbols:
            return symbols[_select_symbol(s, label, fp, symbols)]
        s.cli.update_info(f"[red]No variables found in {escape(str(fp))}.[/red]")


def _prompt_test_value(s: PromptSession, label: str, *, default: str = "") -> Any:
    """
    Prompt for a Python value, parsed as a literal. Only if that fails does the user get
    an explicit choice to edit, ``eval`` the input, or load the value from a file.
    """
    raw = default
    try:
        while True:
            raw = s.code(label, initial=raw)
            try:
                value = ast.literal_eval(raw)
            except (ValueError, TypeError, SyntaxError) as err:
                header = Group(
                    Syntax(raw, "python", background_color="default", line_numbers=False),
                    Text(f"{type(err).__name__}: {err}", style="red"),
                )
                choice = s.select(
                    f"Couldn't parse '{label}' as a literal",
                    [_EDIT, _EVAL, _FILE],
                    header=header,
                )
                if choice == _EDIT:
                    continue
                if choice == _EVAL:
                    try:
                        value = eval(raw, {"__builtins__": builtins}, {})
                    except Exception as err:  # noqa: BLE001
                        s.cli.update_info(f"[red]eval failed:[/red] {escape(str(err))}")
                        continue
                else:
                    value = _prompt_value_from_file(s, label)

            # Checked here rather than left to Task.create_task_json, which only runs
            # after every other field has been entered.
            try:
                json.dumps(value)
            except TypeError:
                s.cli.update_info(
                    f"[red]'{label}' can't be stored in a task file:[/red] "
                    f"unsupported type {escape(type(value).__name__)}"
                )
                continue
            return value
    finally:
        s.cli.update_info(_INFO)


def _check_param_value(param: inspect.Parameter, value: Any) -> str | None:
    """Why ``value`` can't be used for the test constructor parameter ``param``, or
    ``None`` if it can."""
    if param.annotation is str and not isinstance(value, str):
        return f"expected str, got {type(value).__name__}"
    # Checked here rather than left to Task.create_task_json, which only runs after every
    # other field has been entered.
    try:
        json.dumps(value)
    except TypeError:
        return f"unsupported type {type(value).__name__}"
    return None


def _match_ex_file(value: Any, ex_files: list[PathLike]) -> PathLike | None:
    """The exercise file that ``value`` (a path) refers to, if any."""
    if not isinstance(value, (str, PathLike)):
        return None
    target = Path(value).absolute()
    return next((f for f in ex_files if Path(f).absolute() == target), None)


def _enter_param(s: PromptSession, param: inspect.Parameter) -> Any:
    """Prompt for a parameter's value directly: plain text for ``str`` parameters,
    otherwise a Python value (see ``_prompt_test_value``), prefilled with the default."""
    has_default = param.default is not inspect.Parameter.empty
    if param.annotation is str:
        label = _PARAM_LABELS.get(param.name, param.name)
        return s.text(label, initial=param.default if has_default else "")
    default = repr(param.default) if has_default else ""
    return _prompt_test_value(s, param.name, default=default)


def _prompt_param(
    s: PromptSession,
    param: inspect.Parameter,
    fp: Path | None,
    unused: dict[str, Any],
) -> Any:
    """
    Get a value for ``param``, which the info file (if any) doesn't define usably: pick one
    of the ``unused`` symbols (which is then removed from it), enter the value, or, for
    optional parameters, leave it empty (returning ``_EMPTY``).
    """
    while True:
        options = [_SELECT_SYMBOL] if unused else []
        options.append(_ENTER_VALUE)
        if param.default is not inspect.Parameter.empty:
            options.append(_LEAVE_EMPTY)

        if len(options) == 1:
            choice = options[0]
        else:
            header = Text(f"'{param.name}' isn't defined in {fp}.") if fp else None
            choice = s.select(
                f"How do you want to set '{param.name}'?", options, header=header
            )

        if choice == _LEAVE_EMPTY:
            return _EMPTY
        if choice == _ENTER_VALUE:
            return _enter_param(s, param)

        assert fp is not None  # only offered if there are symbols
        symbol = _select_symbol(s, param.name, fp, unused)
        if err := _check_param_value(param, unused[symbol]):
            s.cli.update_info(
                f"[red]'{symbol}' can't be used for '{param.name}':[/red] {escape(err)}"
            )
            continue
        s.cli.update_info(_INFO)
        return unused.pop(symbol)


def _prompt_test(s: PromptSession, test_cls: type[Test], ex_files: list[PathLike]) -> Test:
    """
    Interactively define a test of type ``test_cls``, by asking for each parameter of its
    constructor.

    The user may reference a Python file first: its variables whose names exactly match a
    parameter are used for that parameter, and the remaining ones are offered for any
    parameter the file leaves undefined (each symbol for one parameter at most).
    ``file_path`` must always be one of ``ex_files``.
    """
    params = [
        p
        for p in inspect.signature(test_cls).parameters.values()
        if p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
    ]

    fp: Path | None = None
    symbols: dict[str, Any] = {}
    if s.select("Reference a file with test information?", ["yes", "no"]) == "yes":
        fp, symbols = _load_symbols(s, "Test information file")
    param_names = {p.name for p in params}
    unused = {k: v for k, v in symbols.items() if k not in param_names}

    kwargs: dict[str, Any] = {}
    try:
        for param in params:
            if param.name == "file_path":
                match = _match_ex_file(symbols.get("file_path"), ex_files)
                ex_file = match or s.select(
                    _PARAM_LABELS["file_path"], [str(f) for f in ex_files]
                )
                kwargs["file_path"] = Path(ex_file).absolute()
                continue

            if param.name in symbols:
                err = _check_param_value(param, symbols[param.name])
                if err is None:
                    kwargs[param.name] = symbols[param.name]
                    continue
                s.cli.update_info(
                    f"[red]'{param.name}' from {escape(str(fp))} can't be used:[/red] "
                    f"{escape(err)}"
                )

            value = _prompt_param(s, param, fp, unused)
            if value is not _EMPTY:
                kwargs[param.name] = value
    finally:
        s.cli.update_info(_INFO)

    return test_cls(**kwargs)


# Prompt labels for well-known test constructor parameters; others use the parameter name.
_PARAM_LABELS = {
    "name": "Test name",
    "file_path": "Target exercise file",
    "function_name": "Function name",
}

# Test types the CLI can create, as ``{option: (hint, test class)}``. Register new test
# types here to offer them in the test-type selection; their constructor parameters are
# prompted for by ``_prompt_test``.
_TEST_TYPES: dict[str, tuple[str, type[Test]]] = {
    "SimpleTest": (
        (
            "Call a function from an exercise file with fixed arguments and compare its\n"
            "return value to an expected value."
        ),
        SimpleTest,
    ),
}
_NO_MORE_TESTS = "Done"


def _prompt_tests(s: PromptSession, ex_files: list[PathLike]) -> list[Test]:
    """Interactively define any number of tests, choosing the test type for each."""
    tests: list[Test] = []
    while True:
        options = [*_TEST_TYPES, _NO_MORE_TESTS]
        hints: list[str | None] = [hint for hint, _ in _TEST_TYPES.values()]
        hints.append(f"Finish adding tests ({len(tests)} added so far).")
        kind = s.select(
            "Which kind of test do you want to add?", options, option_hints=hints
        )
        if kind == _NO_MORE_TESTS:
            return tests
        _, test_cls = _TEST_TYPES[kind]
        tests.append(_prompt_test(s, test_cls, ex_files))


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
    name = s.text("Task name")

    s.cli.update_progress(1 / _TOTAL_STEPS)
    instructions = s.text("Instructions", multiline=True)

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
            fields = PromptSession(cli, q).run(_task_flow)

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
