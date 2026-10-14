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

## `isolated`

With `isolated=True`, the test runs in a temporary working directory of its own, which
is deleted afterwards. Use it when the student's code writes files that shouldn't pile
up. `PrintTest` and `GeneralTest` take `isolated` too.

Two things to keep in mind:

- The working directory is process-wide. While an isolated test runs, the whole app runs
  in the temporary directory. The app only compares files' modification times, so a save
  made during the test is picked up afterwards.
- Isolated tests start in an empty directory. Data files that the code reads by a
  relative path aren't there, so don't isolate tests whose code reads files next to it.
