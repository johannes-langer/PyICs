"""
Notebooks (``.ipynb``) as exercise files: reading them (``pyics.notebook.cells``),
running their code cells as a module and turning them into Python source
(``pyics.notebook.loader``), the tests that check them, storing them in task files,
finding the packages they import, and rerunning a task's tests when its notebook is
saved.
"""

import copy
import inspect
import io
import json as std_json
import os
import queue
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest
from notebook_helpers import (
    code,
    make_notebook,
    markdown,
    notebook_text,
    stream,
    write_notebook,
)
from rich.console import Console
from rich.text import Text

from pyics.app.prompts import PromptSession
from pyics.data import Course, Task
from pyics.notebook import cells, loader
from pyics.notebook.cells import Cell, NotebookError
from pyics.scripts.create_task import _edit_files
from pyics.tests.check_structure import check_structure
from pyics.tests.pytest_integration import LiveTestCollectionPlugin, RichFailureRepr
from pyics.tests.tests import (
    MarkupAssertionError,
    PrintTest,
    SimpleTest,
    StructureTest,
    Test,
)
from pyics.tests.trace import Trace

REPO = Path(__file__).parent.parent
TIMEOUT = 10  # seconds

BFS = """\
def bfs(graph, start):
    seen = [start]
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for neighbor in graph.get(node, []):
            if neighbor not in seen:
                seen.append(neighbor)
                queue.append(neighbor)
    return seen"""

GRAPH = {"A": ["B", "C"], "B": ["D"], "C": ["D"]}


@pytest.fixture
def chdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The working directory, e.g. where magics would write files, and where pytest
    runs started by a test write their cache."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _failures(tests: list[Test]) -> dict[str, RichFailureRepr]:
    """Run ``tests`` like a ``Task`` does, and return their failures by test name."""
    plugin = LiveTestCollectionPlugin(tests)
    pytest.main(["-p", "no:terminal", "--capture=no"], plugins=[plugin])
    failures = {}
    for report in plugin.reports:
        if report.when == "call" and report.failed:
            assert isinstance(report.longrepr, RichFailureRepr)
            failures[report.nodeid.removeprefix("::")] = report.longrepr
    return failures


def _plain(err: BaseException) -> str:
    """The message of ``err`` as the main panel shows it, without markup."""
    if isinstance(err, MarkupAssertionError):
        return Text.from_markup(str(err)).plain
    return str(err)


# --- reading notebooks ---


def test_cells_are_read_with_their_position_id_tags_and_source(tmp_path: Path) -> None:
    nb = make_notebook(
        [
            markdown("# BFS", tags=["pyics:bfs"]),
            "import math\nx = 1",
            code("download()", tags=["skip-execution"], id="dl"),
        ]
    )
    nb["cells"][1]["source"] = "import math\nx = 1"  # stored as a string, too
    path = write_notebook(tmp_path / "bfs.ipynb", nb)

    assert cells.cells(cells.read(path)) == [
        Cell(0, "markdown", "cell-1", ("pyics:bfs",), "# BFS"),
        Cell(1, "code", "cell-2", (), "import math\nx = 1"),
        Cell(2, "code", "dl", ("skip-execution",), "download()"),
    ]
    code_cells = cells.code_cells(cells.read(path))
    assert [cell.number for cell in code_cells] == [2, 3]


def test_notebooks_are_recognized_by_their_suffix() -> None:
    assert cells.is_notebook("ex/bfs.ipynb")
    assert cells.is_notebook(Path("BFS.IPYNB"))
    assert not cells.is_notebook("bfs.py")
    assert not cells.is_notebook("ipynb")


def test_nbformat_3_is_refused_with_how_to_convert_it(chdir: Path) -> None:
    path = chdir / "old.ipynb"
    v3 = {"metadata": {}, "nbformat": 3, "nbformat_minor": 0, "worksheets": []}
    path.write_text(std_json.dumps(v3))
    message = (
        "Can't read old.ipynb: nbformat 3 isn't supported. Open and save it in "
        "Jupyter to convert it."
    )
    with pytest.raises(NotebookError) as excinfo:
        cells.read(path)
    assert str(excinfo.value) == message

    # as the main panel shows it
    failures = _failures([SimpleTest("bfs", path, "bfs", [])])
    assert failures["bfs"].text.plain == f"bfs failed: {message}"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ('{"cells": [', "it isn't valid JSON"),
        ("[1, 2]", "it isn't a Jupyter notebook"),
        ('{"nbformat": 4, "cells": {}}', "its cells aren't a list of cells"),
    ],
)
def test_other_files_arent_notebooks(text: str, reason: str) -> None:
    with pytest.raises(NotebookError, match=f"^Can't read the notebook: {reason}"):
        cells.parse(text)


# --- storing notebooks ---


def _run_notebook_text(source: str = "print('hi')") -> str:
    """A notebook as Jupyter saves it once it has been run."""
    return notebook_text(
        make_notebook(
            [
                markdown("# Greeting", metadata={"collapsed": False}),
                code(
                    source,
                    outputs=[stream("hi\n")],
                    execution_count=3,
                    tags=["keep"],
                    metadata={
                        "execution": {"iopub.status.busy": "2026-10-11T09:00:00Z"},
                        "scrolled": True,
                    },
                ),
            ],
            metadata={
                "kernelspec": {"name": "python3", "display_name": "Python 3"},
                "language_info": {"name": "python", "version": "3.14.0"},
                "title": "Greetings",
            },
        )
    )


def test_normalized_notebooks_keep_only_what_their_author_wrote() -> None:
    normalized = cells.normalize(_run_notebook_text())
    nb = std_json.loads(normalized)
    assert nb["metadata"] == {"kernelspec": cells.KERNELSPEC, "title": "Greetings"}
    greeting, printing = nb["cells"]
    assert greeting == {
        "cell_type": "markdown",
        "id": "cell-1",
        "metadata": {},
        "source": ["# Greeting"],
    }
    assert printing["outputs"] == []
    assert printing["execution_count"] is None
    assert printing["metadata"] == {"tags": ["keep"]}

    # normalizing is idempotent, and keeps the cells
    assert cells.normalize(normalized) == normalized
    original = cells.cells(cells.parse(_run_notebook_text()))
    assert cells.cells(cells.parse(normalized)) == original


def _notebook_text(source: str = "print('hi')") -> str:
    """The notebook of ``_run_notebook_text`` before it has been run."""
    return notebook_text(
        make_notebook(
            [markdown("# Greeting"), code(source, tags=["keep"])],
            metadata={"title": "Greetings"},
        )
    )


def test_a_notebook_that_was_merely_run_is_stored_the_same() -> None:
    assert cells.normalize(_run_notebook_text()) == cells.normalize(_notebook_text())
    changed = _run_notebook_text("print('hello')")
    assert cells.normalize(changed) != cells.normalize(_notebook_text())


def test_cell_text() -> None:
    text = notebook_text(
        [markdown("# Exercise\n\nWrite bfs."), "import math\n\nx = 1\n", "y = 2"]
    )
    assert cells.as_cell_text(text) == (
        "# %% [markdown] cell 1\n# # Exercise\n#\n# Write bfs.\n\n"
        "# %% cell 2\nimport math\n\nx = 1\n\n"
        "# %% cell 3\ny = 2\n"
    )


def test_task_files_store_notebooks_normalized(chdir: Path) -> None:
    ex, sol = Path("ex.ipynb"), Path("sol.ipynb")
    ex.write_text(_run_notebook_text())
    sol.write_text(_run_notebook_text("print('solution')"))
    task_file = Task.create_task_json(Path("t"), "T", "", ex, sol)

    fields = Task.read_task_json(task_file)
    stored = fields["file_contents"]
    assert stored[ex] == cells.normalize(ex.read_text())
    assert stored[sol] == cells.normalize(sol.read_text())

    # rewriting the task keeps it as it is
    original = task_file.read_text()
    Task.create_task_json(task_file, **fields)
    assert task_file.read_text() == original

    # the files are created from the stored contents, which load as the same notebook
    ex.unlink()
    task = Task(task_file, setup=False)
    task.create_files([stored[ex]], [stored[sol]])
    assert ex.read_text() == stored[ex]
    assert cells.cells(cells.read(ex)) == cells.cells(cells.parse(_run_notebook_text()))


def _edit_shows(path: Path, stored: str) -> str:
    """What editing a task shows for its exercise notebook at ``path``, whose contents
    stored in the task file are ``stored``. Keeps the notebook as it is."""
    q: queue.Queue[str] = queue.Queue()
    for key in ["\r", "\r", "\x03"]:  # keep it, add no other file, (ctrl-c)
        q.put(key)
    cli = MagicMock()
    s = PromptSession(cli, q)
    files, contents, _ = s.run(
        lambda s: _edit_files(s, "exercise", [path], {path: stored})
    )
    assert (files, contents) == ([path], {path: stored})
    assert q.get_nowait() == "\x03", "all prompts were answered"

    calls = cli.update_main_content.call_args_list
    title = f"Exercise file {path}"
    renderable = next(c.args[0] for c in reversed(calls) if c.kwargs["title"] == title)
    console = Console(width=100, record=True, file=io.StringIO())
    console.print(renderable)
    return console.export_text()


def test_editing_a_task_ignores_that_its_notebook_was_run(chdir: Path) -> None:
    path = chdir / "ex.ipynb"
    stored = cells.normalize(_notebook_text())
    path.write_text(_run_notebook_text())  # with outputs, kernel info, ...
    assert "Update" not in _edit_shows(path, stored)

    path.write_text(_run_notebook_text("print('hello')"))
    shown = _edit_shows(path, stored)
    assert "Update" in shown
    assert "The file on disk differs from the stored contents" in shown


# --- running notebooks ---


def test_magics_and_shell_commands_dont_stop_the_run(chdir: Path) -> None:
    path = write_notebook(
        chdir / "magics.ipynb",
        [
            "%matplotlib inline\n%pip install numpy\nimport os",
            "!ls\nfiles = !ls\nos?",
            "%%writefile written.txt\nnot Python at all",
            "%%time\ncount = 1",
            "%%timeit\ncount += 1",
            "%%capture captured\nprint('hidden')\ncount += 1",
            "%time count += 1",
            "%%bash\nfor f in *; do echo $f; done",
            "def double(x):\n    return 2 * x\n\ndisplay(double(2))",
        ],
    )
    run = loader.run_notebook(path)
    assert run.errors == []
    module = run.module
    # line magics, shell commands and other cell magics do nothing
    assert module.files == []
    assert not (chdir / "written.txt").exists()
    # %%time, %%timeit, %%capture and %time run their code once
    assert module.count == 4
    assert module.captured.stdout == "hidden\n"
    assert run.outputs[5] == ""  # captured
    assert run.outputs[8] == "4\n"  # displayed

    SimpleTest("doubles", path, "double", 4, function_args=[2]).run()


def test_plots_are_drawn_without_blocking(chdir: Path) -> None:
    pytest.importorskip("matplotlib")
    path = write_notebook(
        chdir / "plots.ipynb",
        [
            "%matplotlib inline\nimport matplotlib.pyplot as plt",
            "plt.plot([1, 2, 3])\nplt.show()",
            "done = True",
        ],
    )
    run = loader.run_notebook(path)
    assert run.errors == []
    assert run.module.done


def test_matplotlib_draws_with_agg_while_a_notebook_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Also when it has already been imported, e.g. by an earlier run. Figures are
    closed once the notebook has run."""
    calls: list[tuple[str, str]] = []
    matplotlib, pyplot = ModuleType("matplotlib"), ModuleType("matplotlib.pyplot")
    matplotlib.use = lambda backend: calls.append(("use", backend))  # type: ignore[attr-defined]
    pyplot.close = lambda which: calls.append(("close", which))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "matplotlib", matplotlib)
    monkeypatch.setitem(sys.modules, "matplotlib.pyplot", pyplot)
    monkeypatch.setenv("MPLBACKEND", "TkAgg")

    path = write_notebook(
        tmp_path / "plots.ipynb", ["import os\nbackend = os.environ['MPLBACKEND']"]
    )
    assert loader.load_module(path).backend == "Agg"
    assert os.environ["MPLBACKEND"] == "TkAgg"
    assert calls == [("use", "Agg"), ("close", "all")]


NAME_ERROR_CELLS = [
    markdown("# Exercise 1: BFS"),  # cell 1
    "from collections import deque",
    BFS,  # cell 3
    markdown("# Exercise 2: DFS"),
    "def dfs(graph, start):\n    ...",
    markdown("# Exercise 3"),
    "print(qeue)",  # cell 7
    "after = True",
]


def test_an_error_in_a_cell_doesnt_stop_the_run(tmp_path: Path) -> None:
    path = write_notebook(tmp_path / "bfs.ipynb", NAME_ERROR_CELLS)
    run = loader.run_notebook(path)
    assert run.module.after
    assert [error.cell.number for error in run.errors] == [7]
    assert run.errors[0].describe() == (
        "cell 7 raised NameError: name 'qeue' is not defined"
    )
    assert loader.last_errors() == run.errors
    loader.clear_last_errors()
    assert loader.last_errors() == []


def test_an_error_in_a_cell_is_named_by_the_tests_that_fail(chdir: Path) -> None:
    path = write_notebook(chdir / "bfs.ipynb", NAME_ERROR_CELLS)
    script = chdir / "script.py"
    script.write_text("for x in []:\n    pass\n")
    failures = _failures(
        [
            SimpleTest(
                "bfs", path, "bfs", ["A", "B", "C", "D"], function_args=[GRAPH, "A"]
            ),
            SimpleTest(
                "dfs", path, "dfs", ["A", "B", "D", "C"], function_args=[GRAPH, "A"]
            ),
            # a test after it that doesn't run the notebook doesn't name its errors
            StructureTest("no loops", script, {"concept": "loop", "max": 0}),
        ]
    )
    assert list(failures) == ["dfs", "no loops"]
    assert failures["dfs"].text.plain == (
        "dfs failed: None == ['A', 'B', 'D', 'C']\n"
        "cell 7 raised NameError: name 'qeue' is not defined"
    )
    assert "cell 7" not in failures["no loops"].text.plain


def test_syntax_errors_are_errors_of_their_cell(tmp_path: Path) -> None:
    path = write_notebook(
        tmp_path / "bfs.ipynb", ["x = 1", "\n\ndef broken(:\n    pass", "y = x + 1"]
    )
    run = loader.run_notebook(path)
    assert run.module.y == 2
    (error,) = run.errors
    assert isinstance(error.error, SyntaxError)
    assert error.describe().startswith("cell 2 raised SyntaxError: ")
    # the line in the cell, leading blank lines included
    assert error.describe().endswith(" (line 3)")


def test_cells_tagged_skip_execution_dont_run(tmp_path: Path) -> None:
    path = write_notebook(
        tmp_path / "train.ipynb",
        [
            "ran = []",
            code("ran.append('training')\nraise RuntimeError", tags=["skip-execution"]),
            "ran.append('evaluation')",
        ],
    )
    run = loader.run_notebook(path)
    assert run.module.ran == ["evaluation"]
    assert run.errors == []
    assert 1 not in run.outputs


def test_exiting_ends_the_run(tmp_path: Path) -> None:
    path = write_notebook(
        tmp_path / "exits.ipynb",
        ["print('before')", "import sys\nsys.exit()", "print('after')"],
    )
    run = loader.run_notebook(path, as_main=True)
    assert run.output() == "before\n"
    assert run.errors == []
    PrintTest("t", path, "before").run()


def test_notebooks_run_as_their_own_module(tmp_path: Path) -> None:
    path = write_notebook(tmp_path / "names.ipynb", ["name = __name__"])
    module = loader.load_module(path)
    assert module.name == "current_solution"
    assert sys.modules["current_solution"] is module
    assert loader.load_module(path, as_main=True).name == "__main__"
    run = loader.run_notebook(path, module_name="other_solution")
    assert sys.modules["other_solution"] is run.module
    del sys.modules["other_solution"]


def test_the_output_of_cells_never_reaches_the_console(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_notebook(tmp_path / "prints.ipynb", ["print('hi')", "display(1)\n2"])
    run = loader.run_notebook(path)
    assert run.outputs == {0: "hi\n", 1: "1\n2\n"}
    assert capsys.readouterr().out == ""


def test_tracebacks_show_the_cell_and_its_lines(tmp_path: Path) -> None:
    path = write_notebook(
        tmp_path / "bfs.ipynb",
        [
            markdown("# BFS"),
            "from collections import deque",
            "def fail():\n    x = 1\n    raise ValueError(f'boom {x}')",
            "%matplotlib inline\nfail()",
        ],
    )
    run = loader.run_notebook(path)
    (error,) = run.errors
    assert error.describe() == "cell 4 raised ValueError: boom 1"
    text = "".join(traceback.format_exception(error.error))
    assert f'File "{path}:cell 4", line 2, in <module>\n    fail()' in text
    assert f'File "{path}:cell 3", line 3, in fail' in text
    assert "raise ValueError(f'boom {x}')" in text
    assert inspect.getsource(run.module.fail).startswith("def fail():\n    x = 1\n")

    with pytest.raises(ValueError, match="boom 1") as excinfo:
        SimpleTest("fails", path, "fail", None).run()
    assert "raise ValueError(f'boom {x}')" in "".join(
        traceback.format_exception(excinfo.value)
    )


def test_traces_show_the_cell_and_its_lines(tmp_path: Path) -> None:
    path = write_notebook(
        tmp_path / "bfs.ipynb",
        [
            "%matplotlib inline\nfrom collections import deque",
            BFS,
            "print(bfs({}, 'A'))",
        ],
    )
    trace = Trace()
    test = SimpleTest("t", path, "bfs", ["A", "B"], function_args=[{"A": ["B"]}, "A"])
    test.run(trace=trace)
    text = trace.text.plain
    assert '>>> Call to bfs in File "bfs.ipynb:cell 2", line 1' in text
    assert "node = queue.popleft()" in text
    assert "<<< Return value from bfs: ['A', 'B']" in text
    assert "<module>" not in text  # loading the notebook isn't traced

    # a script is traced as a whole, magics included
    trace = Trace()
    PrintTest("t", path, "['A']").run(trace=trace)
    text = trace.text.plain
    assert '>>> Call to <module> in File "bfs.ipynb:cell 1", line 1' in text
    assert "get_ipython().run_line_magic('matplotlib', 'inline')" in text
    assert "SOURCE IS UNAVAILABLE" not in text


def test_changed_cells_are_shown_as_they_are_now(tmp_path: Path) -> None:
    path = tmp_path / "bfs.ipynb"
    for value in ("first", "second"):
        write_notebook(path, [f"def fail():\n    raise ValueError({value!r})"])
        with pytest.raises(ValueError) as excinfo:
            SimpleTest("fails", path, "fail", None).run()
        text = "".join(traceback.format_exception(excinfo.value))
        assert f"raise ValueError({value!r})" in text


def test_input_answers_show_up_in_the_cell_that_asks(tmp_path: Path) -> None:
    path = write_notebook(
        tmp_path / "greet.ipynb",
        ["name = input('Name: ')", "print(f'Hello, {name}!')"],
    )
    PrintTest("t", path, "Name: Bob\nHello, Bob!", inputs=["Bob"]).run()


# --- PrintTest ---

PRINT_CELLS = [
    markdown("# Output"),
    "print('Hello')",
    "x = 6 * 7\nx",  # shown, as Jupyter shows it as Out[3]
    "x;",  # hidden by the semicolon
    "x  # a comment after it",
    "None",  # not shown
    "'text'",
    "display(x, [1, 2]);",
    "print('done')",  # returns None
    "if x:\n    x",  # not a final expression
]


def test_print_test_compares_the_output_of_all_cells(tmp_path: Path) -> None:
    path = write_notebook(tmp_path / "out.ipynb", PRINT_CELLS)
    expected = "Hello\n42\n42\n'text'\n42\n[1, 2]\ndone"
    PrintTest("t", path, expected).run()

    with pytest.raises(AssertionError) as excinfo:
        PrintTest("t", path, expected.replace("'text'", "text")).run()
    assert "- text" in str(excinfo.value).splitlines()  # expected
    assert "+ 'text'" in str(excinfo.value).splitlines()  # printed


def test_print_test_names_the_cells_that_raised(chdir: Path) -> None:
    path = write_notebook(
        chdir / "greet.ipynb", ["name = input('Name: ')", "print(f'Hello, {name}!')"]
    )
    failures = _failures([PrintTest("greets", path, "Name: Bob\nHello, Bob!")])
    lines = failures["greets"].text.plain.splitlines()
    assert lines[-2:] == [
        "cell 1 raised EOFError: EOF when reading a line, no inputs left",
        "cell 2 raised NameError: name 'name' is not defined",
    ]


# --- StructureTest ---

STRUCTURE_CELLS = [
    markdown("# Sums"),
    "%time numbers = [1, 2, 3]",
    "def total(xs):\n    s = 0\n    for x in xs:\n        s += x\n    return s",  # 3
    "!echo hi",
    "%%time\nfor x in numbers:\n    print(x)",  # cell 5
    "%%bash\nfor f in *; do echo $f; done",  # not Python
    "def broken(:\n    pass",  # cell 7
]


def test_python_source_of_a_notebook(tmp_path: Path) -> None:
    path = write_notebook(tmp_path / "sums.ipynb", STRUCTURE_CELLS)
    source = loader.python_source(path)
    assert [cell.number for cell in source.cells] == [2, 3, 4, 5]
    assert [(cell.number, err.lineno) for cell, err in source.unparsable] == [(7, 1)]
    lines = source.source.splitlines()
    assert lines[0] == "get_ipython().run_line_magic('time', 'numbers = [1, 2, 3]')"
    assert lines[7:10] == ["# %%time", "for x in numbers:", "    print(x)"]
    assert source.locate(1) == "cell 2, line 1"
    assert source.locate(4) == "cell 3, line 3"
    assert source.locate(9) == "cell 5, line 2"
    # also of a notebook that has been read
    read = loader.python_source(cells.read(path))
    assert (read.source, read.cells, read.starts) == (
        source.source,
        source.cells,
        source.starts,
    )


def test_structure_messages_name_the_cell_and_line(tmp_path: Path) -> None:
    path = write_notebook(tmp_path / "sums.ipynb", STRUCTURE_CELLS)
    with pytest.raises(MarkupAssertionError) as excinfo:
        StructureTest("no loops", path, {"concept": "loop", "max": 0}).run()
    lines = _plain(excinfo.value).splitlines()
    assert lines[0] == "Expected no loop, found 2 (cell 3, line 3; cell 5, line 2)."
    # left out, which may be why the test fails
    assert lines[1].startswith("Could not parse cell 7: ")
    assert lines[1].endswith(" (line 1).")

    # a rule that holds doesn't mention the cell that can't be parsed
    StructureTest("defines", path, {"concept": "function", "min": 1}).run()


def test_check_structure_names_lines_with_locate() -> None:
    def locate(line: int) -> str:
        return f"cell 2, line {line}"

    rules = {"concept": "loop", "max": 0}
    assert check_structure(
        rules, "for x in y:\n    pass\n", locate=locate, use_rich_markup=False
    ) == ["Expected no loop, found 1 (cell 2, line 1)."]
    assert check_structure(
        rules, "def f(:\n", filename="ex.ipynb", locate=locate, use_rich_markup=False
    ) == ["Could not parse 'ex.ipynb': invalid syntax (cell 2, line 1)."]


# --- the app: rerunning the tests when a notebook is saved ---


def _wait_for(condition: Callable[[], object]) -> None:
    deadline = time.monotonic() + TIMEOUT
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def _as_notebook(instructions: str, code: str) -> str:
    """The Python file ``code`` as a notebook below ``instructions``, a cell for each of
    its parts separated by two blank lines (e.g. functions)."""
    parts = [markdown(instructions), *code.strip().split("\n\n\n")]
    return notebook_text(parts)


def _copy_task(fields: dict, root: Path, ex: Path, ex_code: str, sol_code: str) -> Path:
    """Write the task of ``fields`` (see ``Task.read_task_json``) to ``root``, with
    ``ex`` as its exercise file, and solution file of the same kind in
    ``.solutions``."""
    sol = root / ".solutions" / ex.name
    tests = []
    for test in fields["tests"]:
        test = copy.copy(test)
        test.file_path = ex
        tests.append(test)
    return Task.create_task_json(
        root / ".tasks" / ex.stem,
        fields["name"],
        fields["instructions"],
        ex,
        sol,
        tests,
        file_contents={ex: ex_code, sol: sol_code},
        help=fields["help"],
    )


@pytest.fixture
def search_course(chdir: Path) -> Iterator[Path]:
    """
    A copy of the repository's search course, whose BFS exercise is a notebook. The DFS
    exercise has been solved already, and the BFS notebook hasn't been created yet.
    Yields the course manifest.
    """
    dfs = Task.read_task_json(REPO / ".tasks/dfs.pyicstask.json")
    bfs = Task.read_task_json(REPO / ".tasks/bfs.pyicstask.json")
    (dfs_ex,), (dfs_sol,) = dfs["ex_files"], dfs["sol_files"]
    (bfs_ex,), (bfs_sol,) = bfs["ex_files"], bfs["sol_files"]
    contents = {**dfs["file_contents"], **bfs["file_contents"]}

    ex = chdir / ".exercises"
    tasks = [
        _copy_task(dfs, chdir, ex / "dfs.py", contents[dfs_ex], contents[dfs_sol]),
        _copy_task(
            bfs,
            chdir,
            ex / "bfs.ipynb",
            _as_notebook(bfs["instructions"], contents[bfs_ex]),
            _as_notebook(bfs["instructions"], contents[bfs_sol]),
        ),
    ]
    manifest = Course.create_course_manifest(
        chdir / "search_course", tasks, name="Search Algorithms"
    )
    (ex / "dfs.py").parent.mkdir()
    (ex / "dfs.py").write_text(contents[dfs_sol])

    threads = set(threading.enumerate())
    yield manifest
    # a pytest run changes back to the directory it started in when it finishes
    for thread in set(threading.enumerate()) - threads:
        thread.join(TIMEOUT)


def _status(course: Course) -> str:
    console = Console(width=100, record=True, file=io.StringIO())
    console.print(course.status)
    return console.export_text()


def test_a_notebook_exercise_is_tested_again_when_it_is_saved(
    search_course: Path,
) -> None:
    course = Course.load_course_manifest(search_course, check_progress=True)
    _wait_for(lambda: not course.loading and not course.check_status)
    notebook = search_course.parent / ".exercises/bfs.ipynb"
    # the course starts on BFS, whose notebook it created
    assert course.exercise_files == [notebook]
    assert course.progress == 0.5
    _wait_for(lambda: course.tick()[0] and "2 failed, 1 passed" in _status(course))
    assert "Search path test failed" in _status(course)

    # saved by Jupyter: the solution, with outputs
    solution = std_json.loads(
        (search_course.parent / ".solutions/bfs.ipynb").read_text()
    )
    solution["cells"][-1]["outputs"] = [stream("Terminal node A\n")]
    mtime = notebook.stat().st_mtime + 2  # mtimes may only change every few ms
    notebook.write_text(std_json.dumps(solution, indent=1))
    os.utime(notebook, (mtime, mtime))
    _wait_for(lambda: (course.tick(), course.progress == 1)[1])
    assert "3 passed" in _status(course)

    # the progress check on opening the course finds both solved
    reopened = Course.load_course_manifest(search_course, check_progress=True)
    _wait_for(lambda: not reopened.loading and not reopened.check_status)
    assert reopened.progress == 1
