# Choosing Tests

Tests decide when a task is solved, and their failure messages are the feedback students get. PyICs has five kinds of test. Most tasks combine two or three.

## Which test do I need?

| You want to check… | Use | Runs student code? |
|---|---|---|
| what a function returns, for given arguments | [`SimpleTest`](#simpletest-return-values) | yes |
| a sequence of calls, methods of an object, or an expected exception | [`SimpleTest`](#simpletest-return-values) | yes |
| that a function or class has the right name, parameters, defaults or type hints | [`SpecTest`](#spectest-signatures) | loads the file |
| _how_ it's written: recursion, no loops, a `match`, no `sorted()`, no imports… | [`StructureTest`](#structuretest-how-code-is-written) | no, only parses it |
| what a script prints, possibly answering `input()` | [`PrintTest`](#printtest-console-output) | yes, as a script |
| anything else: float tolerances, mutated arguments, files written, many cases at once | [`GeneralTest`](#generaltest-your-own-pytest-code) | yes, via pytest |

A typical task pairs a `SpecTest` (does `depth_first_search` exist, with the right parameters?) with a few `SimpleTest`s (does it return the right paths?) and maybe a `StructureTest` (without recursion?). The `SpecTest` catches a misspelled function name with a clear message before the `SimpleTest`s fail with a less helpful one.

If none of the tests can express what you need, please [open an issue](https://github.com/johannes-langer/PyICs/issues) describing it.

## Options every test has

| Parameter | Default | Meaning |
|---|---|---|
| `name` | required | Shown in the results when the test fails. Write it for students: "Finds the shortest path from A to G". |
| `file_path` | required | The exercise file the test checks. |
| `soft` | `False` | A soft test that fails is shown in **yellow**, as a warning, and doesn't keep the task from counting as solved. Good for style checks and optional extras. |

Most tests also take these:

| Parameter | Tests | Default | Meaning |
|---|---|---|---|
| `traceable` | `SimpleTest`, `PrintTest`, `GeneralTest` | `True` | Whether students can trace the test with <kbd>t</kbd>. Turn it off only if the trace would give away the solution, e.g. its arguments. |
| `isolated` | `SimpleTest`, `PrintTest`, `GeneralTest` | `False` | Run in a temporary, empty working directory that is deleted afterwards, e.g. when student code writes files. Otherwise tests run in the exercise file's directory, so relative paths like `open("edges.csv")` work. |
| `section` | `StructureTest`, `PrintTest` | the task's section | For notebooks: which part of the notebook to check (see [Jupyter Notebooks](Jupyter-Notebooks#several-exercises-in-one-notebook-sections)). |

Don't isolate a test whose code reads files next to the exercise: the temporary directory starts empty.

---

## `SimpleTest`: return values

Calls a function of the exercise file and compares the result with `==` (numpy arrays and pandas objects are compared as a whole).

```python
from pyics.tests import SimpleTest

SimpleTest("adds two numbers", "add.py", "add", 3, function_args=[1, 2])
```

| Parameter | Meaning |
|---|---|
| `function_name` | The function to call. Can be dotted, e.g. `Stack.from_list`. |
| `expected` | The expected return value, or `Raises(ValueError)` for a call that must raise. |
| `function_args`, `function_kwargs` | Positional and keyword arguments. |
| `constructor`, `constructor_args`, `constructor_kwargs` | Create an object first (e.g. `"Stack"`) and call its methods instead. |

`function_name`, the arguments and `expected` can also be **lists, one item per call**, to test a sequence of calls on the same module or object. `Make("Graph", edges)` creates an object of the student's class while the test runs.

**Full details and examples:** [SimpleTest Reference](SimpleTest-Reference).

---

## `SpecTest`: signatures

Checks that a symbol of the exercise file matches an expected signature, without calling it.

| Parameter | Meaning |
|---|---|
| `symbol_name` | The function or class to check. Can be dotted, e.g. `Graph.out_degree` for a method. |
| `expected` | A function or class with the expected signature, typically your solution's (from the [test information file](Creating-Tasks#the-test-information-file)). It is stored as a plain description of the signature, not as code. |
| `by_type` | If `True`, parameters and attributes are matched by their **type hints** only, ignoring their names and order. The `symbol_name` still has to match. |

What is compared:

- **Functions**: parameter names, kinds (positional, keyword-only…), default values, type hints, and the return type.
- **Classes**: attributes and their types, methods (including `__init__`, `__call__`, `__str__` and `__repr__`), properties (with or without setters and deleters), and nested classes, including inherited ones. Other dunder methods are ignored.

```python
# in the test information file: the expected signature
def depth_first_search(graph: list[tuple[str, str, int]], start: str, goal: str) -> tuple[str, ...]:
    ...
```

Then, in `create_task`, choose `SpecTest`, reference this file, set `symbol_name` to `depth_first_search`, and type `depth_first_search` as the expression for `expected`. (Only variables named like a parameter, e.g. `expected`, are selected automatically.)

Use a `SpecTest` early in a task: "`dfs.py` has no symbol 'depth_first_search'" is clearer feedback than a failing call.

---

## `StructureTest`: how code is written

Checks the _structure_ of the code with Python's `ast` module. The file is parsed, never run, so this works even when the code crashes.

```python
from pyics.tests import StructureTest

StructureTest(
    name="fib is recursive and has no loops",
    file_path="fib.py",
    rules=[
        {"concept": "recursion", "scope": "fib"},
        {"concept": "loop", "scope": "fib", "max": 0, "message": "Solve it without loops."},
    ],
)
```

Rules can look for a **concept** (`loop`, `recursion`, `comprehension`, `match`, `call` of a name, `import` of a module, …), an **AST node** (`JoinedStr` for f-strings, …), or a **code pattern** with wildcards (`sorted(...)`, `for _ in range(len(_)): ...`), and can be combined with `all`, `any` and `not`. `"max": 0` forbids something; `scope` limits a rule to one function or class; `message` replaces the generated message with your own hint.

Rules are checked when the test is created, so a typo is caught right away rather than by your students.

**Full details and examples:** [StructureTest Reference](StructureTest-Reference).

---

## `PrintTest`: console output

Runs the exercise file as a script (with `__name__ == "__main__"`) and compares what it prints.

| Parameter | Meaning |
|---|---|
| `expected` | The expected output. Entered over several lines in `create_task` (<kbd>ctrl</kbd>+<kbd>d</kbd> submits). |
| `inputs` | Answers for the script's `input()` calls, in order, e.g. `["Bob", "42"]`. They appear in the output as if typed into a console. |
| `traceable`, `isolated` | See [above](#options-every-test-has). |

How output is compared:

- For a **Python file**, the output must **match** `expected`. Whitespace that isn't visible in a console (e.g. at the ends of lines) is ignored.
- For a **notebook**, the output only has to **contain** `expected`, because students also output other things while they work. Expect enough to tell a right answer from a wrong one: `Sum: 3` rather than `3`, which is also found in `13`. See [Jupyter Notebooks](Jupyter-Notebooks#how-tests-behave-on-notebooks).
- A test that expects no output fails if there is any.

```python
from pyics.tests import PrintTest

PrintTest("greets by name", "greet.py", "Name: Bob\nHello, Bob!", inputs=["Bob"])
```

---

## `GeneralTest`: your own pytest code

For everything else, write pytest tests. Each test function becomes its own row in the results.

```python
import pytest


def test_area(solution):
    assert solution.area(2) == pytest.approx(12.566, rel=1e-3)
```

The fixture `solution` is the exercise file loaded as a module. There are also `load_solution`, `run_solution`, `exercise_path` and `capsys`. In `create_task`, the source is read from a file or typed in, and it is checked against your solution right away.

**Full details and examples:** [GeneralTest Reference](GeneralTest-Reference).

---

## Writing good feedback

- **One check per test** gives the clearest feedback. Several `SimpleTest`s with clear names beat one big `GeneralTest`, unless the cases really belong together.
- **Test edge cases separately**, e.g. "returns None when there is no path", and consider making optional edge cases soft.
- **Use `message`** in `StructureTest` rules to turn a technical message into a hint.
- **Check your tests on the solution and on the starter code.** Every test should pass on the solution and fail on the starter code. Otherwise the task is solved before students start.
