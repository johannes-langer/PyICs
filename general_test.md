# GeneralTest reference

A `GeneralTest` stores pytest code. Use it for what the other tests can't express:
checking several related things at once, float tolerances, arguments that get mutated,
the output of a single function call, or many cases of one check. pytest collects the
tests from the code, so fixtures (also those that `yield`), `parametrize`, test classes,
`pytest.raises` and `pytest.approx` work as usual, and assertions explain themselves as
they do in pytest.

The implementation is in `src/pyics/tests/general.py`.

```python
from pathlib import Path

from pyics.tests import GeneralTest

GeneralTest("items", "items.py", Path("test_items.py").read_text())
```

The source must define at least one test: a top-level function named `test...`, or a
class named `Test...`.

## Writing the source

```python
import pytest


@pytest.fixture(autouse=True)
def in_tmp_path(tmp_path, monkeypatch):
    # files the student's code writes end up in a temporary directory
    monkeypatch.chdir(tmp_path)


def test_starts_with_an_empty_list(load_solution):
    # a fresh module for each call, so earlier calls can't leave state behind
    module = load_solution()
    assert module.add_item(1) == [1]
    assert module.add_item(2) == [2], "add_item keeps the items of earlier calls"


@pytest.mark.parametrize(("radius", "area"), [(1, 3.14159), (2, 12.56637)])
def test_area(solution, radius, area):
    assert solution.area(radius) == pytest.approx(area)


def test_saves(solution):
    solution.save("notes.txt", "hi")
    assert open("notes.txt").read() == "hi"
```

Each collected test is its own row in the results, e.g. `::items::test_area[1-3.14159]`.
Skipped tests count as not passed, like failed ones.

## PyICs fixtures

Besides pytest's own fixtures, the tests can use:

| Fixture | Provides |
|---|---|
| `solution` | The exercise file loaded as a module, anew for each test. |
| `load_solution` | A function that loads the exercise file as a new module on every call. |
| `run_solution(inputs=(), section=INHERIT)` | Runs the exercise file as a script, as a `PrintTest` does, answering its `input()` calls with `inputs`, and returns its output. For a notebook, `section` picks the section whose output is returned. By default it is the task's section. |
| `exercise_path` | The absolute path of the exercise file. |
| `capsys` | Captures what the test writes to stdout and stderr: `capsys.readouterr()` returns `(out, err)`. |

pyics's `capsys` replaces pytest's, which would also capture the app's own output while
the tests run. It only offers `readouterr()`. `capfd` isn't supported.

## `pyics.testing`

`pyics.testing` has helpers for test code:

- `capture_stdout()`: captures what the test writes to stdout, in a `StringIO`.
- `provide_input(inputs)`: answers `input()` calls with `inputs`, in order.

```python
from pyics.testing import capture_stdout, provide_input


def test_greets(solution):
    with capture_stdout() as out, provide_input(["Bob"]):
        solution.greet()
    assert out.getvalue() == "Name: Bob\nHello, Bob!\n"
```

## Notes

- A source that can't be collected (e.g. a syntax error, or an error when its module
  runs), or that defines no tests, shows up as one failed row, so a broken test never
  goes missing silently. The same goes for an exercise file that raises while a fixture
  loads it.
- `traceable` and `isolated` work as for the other tests: tracing traces the code each
  test runs, and `isolated` runs each test in a temporary working directory of its own.
- Tests that use `load_solution` or `run_solution` are skipped while the tests run on a
  notebook's unsaved code (e.g. in pyics-jupyter's live runs). They run once the
  notebook is saved.
- `create_task` checks a new or edited `GeneralTest` against the solution file. It shows
  how many of its tests pass, and the failures, and then asks whether to keep the test,
  edit it again, or discard it.
- `pack` finds the modules that the source imports, besides pytest, and offers to
  declare the packages that provide them.
