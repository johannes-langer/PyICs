# SimpleTest reference

A `SimpleTest` calls a function of the exercise file and compares what it returns with
the expected value. The value is compared with `==`, except numpy arrays and pandas
objects, which are compared as a whole. Beyond that single call, a `SimpleTest` can make
a sequence of calls, call the methods of an object, and expect exceptions.

The implementation is in `src/pyics/tests/tests.py`.

```python
from pyics.tests import SimpleTest

SimpleTest("adds", "add.py", "add", 3, function_args=[1, 2])
```

## Sequences of calls

`function_name`, `function_args`, `function_kwargs` and `expected` can be lists, with
one item per call. The calls run in order on the same module, so state left by one call
is seen by the next. The test stops at the first call that fails, and its message shows
the calls before it:

```python
SimpleTest(
    "count counts",
    "counter.py",
    ["count", "count", "count"],
    [1, 2, 3],
)
```

If the third call returns 2, the test fails with:

```
count() -> 1
count() -> 2
count() returned 2, expected 3
2 == 3
```

`function_args` and `function_kwargs` can be left out, or given as `[]` and `{}`; every
call then gets no arguments. Otherwise each must have exactly one item per call, or the
test is rejected with a `ValueError` when it is created. A single call is stored as
before, so older versions of pyics still read it.

### Functions that keep state

Repeat a call to catch a function that keeps state between calls, e.g. in a mutable
default argument:

```python
# def add_item(item, items=[]):
#     items.append(item)
#     return items
SimpleTest(
    "add_item starts with an empty list",
    "items.py",
    ["add_item", "add_item"],
    [[1], [1]],
    function_args=[[1], [1]],
)
```

If a call fails after an identical earlier call returned the expected value, the
message says so:

```
add_item(1) -> [1]
add_item(1) returned [1, 1], expected [1]
...
An earlier identical call returned the expected value: the function keeps state between
calls, e.g. in a mutable default argument or a global variable.
```

## Objects

With a `constructor`, a class (or function) of the exercise file is called once per
run, with `constructor_args` and `constructor_kwargs`. The calls are then looked up on
the object it returns:

```python
SimpleTest(
    "push and pop",
    "stack.py",
    ["push", "push", "pop", "pop"],
    [None, None, 2, 1],
    function_args=[[1], [2], [], []],
    constructor="Stack",
)
```

```
Stack()
.push(1) -> None
.push(2) -> None
.pop() returned 1, expected 2
1 == 2
```

## Objects as arguments

An argument that is an object of a class of the exercise file is given as `Make(...)`:
the name of the class (or function, maybe dotted) and its arguments. Only these are
stored, and the object is created while the test runs, from the students' class:

```python
from pyics.tests import Make, SimpleTest

edges = [("A", "B"), ("B", "C")]
SimpleTest(
    "path from A to C",
    "graphs.py",
    "dfs",
    ["A", "B", "C"],
    function_args=[Make("Graph", edges), "A", "C"],
)
```

While the test runs, this calls `dfs(Graph(edges), "A", "C")`, with the `Graph` of
`graphs.py`.

`Make` works in `function_args`, `function_kwargs`, `constructor_args`,
`constructor_kwargs` and `expected` (compared with the class's `==`), also inside lists,
tuples and dicts, and its own arguments can be `Make` values. Each call gets new objects.
If creating one fails, e.g. because the exercise file has no such class, the test fails
and names the call.

Objects of classes that the test information file defines can't be stored in a task,
since that file isn't there when the course runs. `create_task` rejects them and suggests
`Make` instead, which is in scope when you enter a value.

## Dotted names, attributes and properties

Names are looked up attribute by attribute. A name can reach an object the exercise
file defines (`account.deposit`), a class method or static method (`Stack.from_list`,
also as the `constructor`), or an attribute of the constructed object.

A name that isn't callable, e.g. a property or an attribute, has its value compared
instead. It can't be given arguments:

```python
SimpleTest(
    "deposit adds to the balance",
    "bank.py",
    ["deposit", "balance"],
    [None, 15],
    function_args=[[5], []],
    constructor="Account",
    constructor_args=[10],
)
```

## Expected exceptions

An `expected` value of `Raises(...)` expects the call to raise that exception, or one
of its subclasses. `Raises` takes the exception class or its name. While the test runs,
the name is looked up in the exercise file first, so exceptions that students define
work too, and then among the builtins:

```python
from pyics.tests import Raises, SimpleTest

SimpleTest("pop from empty", "stack.py", "pop", Raises(IndexError), constructor="Stack")
SimpleTest(
    "overdraw",
    "bank.py",
    ["withdraw"],
    [Raises("InsufficientFunds")],
    function_args=[[100]],
    constructor="Account",
    constructor_args=[10],
)
# a constructor that must raise: call the class itself
SimpleTest("no negative balance", "bank.py", "Account", Raises(ValueError), function_args=[-1])
```

The test fails if the call returns instead, or raises another exception:

```
expected .pop() to raise KeyError, but it raised IndexError: pop from empty list
```

An unexpected exception during a sequence, or in the constructor, fails the test and
names the call, e.g. `call 3 of 5 (.pop()) raised IndexError: pop from empty list`. A
single call of a module function lets the exception propagate, as it always has.

In `create_task`, `Raises` is in scope when you enter an expected value.

## Working directory and `isolated`

Tests run the student's code in the directory of the exercise file, as students run it
(in Jupyter, or as a script from its directory). So code that reads a data file next to
it by a relative path, e.g. `open("edges.csv")`, finds it, and so does the test
information file in `create_task`. Files the code writes end up there, too.

With `isolated=True`, the test runs in a temporary working directory of its own instead,
which is deleted afterwards. Use it when the student's code writes files that shouldn't
pile up. `PrintTest` and `GeneralTest` take `isolated` too.

Two things to keep in mind:

- The working directory is process-wide. While a test runs, the whole app runs in the
  test's directory, so the app keeps its own paths absolute. It only compares files'
  modification times, so a save made during the test is picked up afterwards.
- Isolated tests start in an empty directory. Data files that the code reads by a
  relative path aren't there, so don't isolate tests whose code reads files next to it.

## Soft tests

With `soft=True`, a failing test is shown in yellow, as a warning, and doesn't keep its
task from counting as solved. Use it for checks that point out something worth fixing,
but shouldn't block the student, e.g. an edge case beyond the task. All tests take
`soft`.
