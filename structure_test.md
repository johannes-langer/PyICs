# StructureTest reference

A `StructureTest` checks *how* a solution is written: for example, that it uses recursion,
avoids loops, uses a `match` statement, or doesn't call `sorted()`. The exercise file is
parsed with Python's `ast` module and never executed, so the test also works when the
code crashes on import.

The implementation is in `src/pyics/tests/check_structure.py`.

```python
from pyics.tests import StructureTest

StructureTest(
    name="Recursive fib",
    file_path="fib.py",
    rules=[
        {"concept": "recursion", "scope": "fib"},
        {"concept": "loop", "scope": "fib", "max": 0, "message": "Solve it without loops."},
    ],
)
```

`rules` is a single rule dict or a list of them, and every rule must hold. The rules are
checked when the test is created, so a typo in a concept or node name, or a pattern that
doesn't parse, raises a `ValueError` immediately. It doesn't wait until a student runs
the task.

To try rules without creating a test, call `check_structure(rules, source)`. It returns
the failure messages, and an empty list means everything holds.

---

## Rule format

Each rule has **exactly one** of these keys:

| Key | Looks for | Example |
|---|---|---|
| `concept` | a named construct from the [concept list](#concepts) | `{"concept": "recursion"}` |
| `node` | one or more `ast` node classes, by name ([nodes](#nodes)) | `{"node": ["ListComp", "GeneratorExp"]}` |
| `pattern` | a code snippet with wildcards ([patterns](#patterns)) | `{"pattern": "sorted(...)"}` |
| `all` / `any` / `not` | a combination of other rules ([combinators](#combinators)) | `{"any": [rule, rule]}` |

These optional keys can be added to any `concept`, `node` or `pattern` rule:

| Key | Default | Meaning |
|---|---|---|
| `scope` | whole file | Only search inside this function or class, including everything nested in it. Nested names use dots: `"fib"`, `"Graph.search"`, `"outer.helper"`. If the student's file doesn't define it, the test fails with *No function or class 'fib' found.* |
| `min` | `1`, or `0` if only `max` is given | The fewest matches allowed. |
| `max` | no limit | The most matches allowed. **`"max": 0` forbids the construct.** |
| `message` | generated | Replaces the failure message, e.g. with a hint for the student. |

How counts read:

| Rule | Meaning |
|---|---|
| `{"concept": "loop"}` | at least one loop |
| `{"concept": "loop", "max": 0}` | no loops |
| `{"concept": "loop", "max": 1}` | at most one loop (zero is fine) |
| `{"concept": "loop", "min": 2}` | at least two loops |
| `{"concept": "loop", "min": 1, "max": 1}` | exactly one loop |

Generated failure messages include line numbers:

```
Expected recursive call in 'fib', found none.
Expected no loop in 'total', found 2 (lines 14, 15).
Expected at most 1 × `sorted(...)`, found 3 (lines 2, 5, 8).
```

---

## Concepts

`{"concept": "<name>"}`. Each match found counts once toward `min`/`max`.

| Concept | Matches | Notes |
|---|---|---|
| `loop` | `for`, `async for`, `while` | Comprehensions are **not** loops, see `comprehension`. |
| `for_loop` | `for`, `async for` | |
| `while_loop` | `while` | |
| `nested_loop` | a loop inside another loop of the same function | A loop inside a function that is defined in a loop does not count. |
| `comprehension` | list, set and dict comprehensions, generator expressions | |
| `conditional` | `if` / `elif`, `x if c else y`, `match` | |
| `if` | `if` statements | Each `elif` counts as another `if`. |
| `ternary` | `x if c else y` | |
| `match` | `match` statements (Python's switch) | |
| `recursion` | recursive **call sites** | See [recursion](#how-recursion-is-detected). `fib(n-1) + fib(n-2)` counts twice. |
| `function` | `def`, `async def` | Lambdas are not counted. |
| `lambda` | `lambda` | |
| `class` | `class` definitions | |
| `generator` | `yield`, `yield from` | |
| `exception_handling` | `try` statements | |
| `raise` | `raise` statements | |
| `with` | `with`, `async with` | |
| `global` | `global`, `nonlocal` | |
| `call` | calls of a function or method, **`name` required** | See below. |
| `import` | `import` / `from … import`, **`name` optional** | See below. |

### Concepts with parameters

**`call`** matches calls by name:

```python
{"concept": "call", "name": "sorted"}      # sorted(xs)
{"concept": "call", "name": "sort"}        # xs.sort(), self.items.sort()
{"concept": "call", "name": "math.sqrt"}   # math.sqrt(2), matched by dotted name
{"concept": "call", "name": "print", "max": 0}   # no print() calls
```

**`import`** matches imports of a module or any of its submodules:

```python
{"concept": "import", "max": 0}                      # no imports at all
{"concept": "import", "name": "numpy", "max": 0}     # import numpy, import numpy.linalg,
                                                     # from numpy.linalg import norm
{"concept": "import", "name": "collections"}         # but not "collectionsx"
```

### How recursion is detected

The analysis is static. It builds a call graph of the functions and methods defined in the
file, and counts a call as recursive if the called function can call back to the caller.
This covers:

- **Direct recursion:** `fib` calls `fib`.
- **Mutual recursion:** `is_even` calls `is_odd`, which calls `is_even`. Both count.
- **Methods:** a method call is resolved by name within the same class. `self.depth()`
  inside `Tree.depth`, `child.size()` inside `Tree.size`, and `self.left.insert(v)`
  inside `Node.insert` all count. `Tree.make(n)` resolves to `Tree`'s method, and
  methods inherited from base classes in the same file are found too.
- **Nested helpers:** `def outer(): def helper(): helper()` counts, and a rule with
  `"scope": "outer"` includes the helper.

It does **not** detect:

- calls through aliases (`f = fib; f(n)`)
- functions passed as arguments or looked up with `getattr`
- recursion through code in other files

It also can't tell whether a call ever runs. A recursive call in dead code
(`if False: fib(0)`) still counts.

The name-based method resolution can also give a false positive: if another class in
the file has a method with the same name, a call to it from inside such a method counts
as recursion.

---

## Nodes

`{"node": "<ast class name>"}` or `{"node": ["Name1", "Name2"]}` counts every node of
those classes. Use this for syntax that no concept covers. The names are those of Python's
[`ast` module](https://docs.python.org/3/library/ast.html#node-classes). A list matches
any of the names.

```python
{"node": "JoinedStr"}                        # f-strings
{"node": "AugAssign", "min": 1}              # x += 1 and friends
{"node": ["ListComp", "GeneratorExp"]}
{"node": "Slice", "max": 0}                  # no slicing
{"node": "ExceptHandler", "max": 1}          # at most one except clause
{"node": "Assert"}
```

Abstract base classes match all of their subclasses, e.g. `"stmt"` (every statement),
`"expr"` (every expression), `"operator"` or `"cmpop"`. For instance
`{"node": "stmt", "max": 10}` limits a solution to at most 10 statements, counting
statements at every nesting level.

To find the class name for a piece of syntax, dump it:

```bash
python -c "import ast; print(ast.dump(ast.parse('x = f\"{a}\"'), indent=2))"
```

---

## Patterns

`{"pattern": "<code>"}` matches a code snippet against the structure of the source,
ignoring formatting and comments. The snippet must be **a single statement or
expression**. Leading indentation is removed, so indented triple-quoted strings work.

### Wildcards

| Write | Matches |
|---|---|
| `_` (as a name or expression) | any single expression, e.g. `sorted(_)` matches `sorted(xs)` and `sorted(a + b)` |
| `_` (as a whole statement) | any single statement, e.g. `if _: _` |
| `_` (as an identifier) | any name in that position, e.g. `_.sort()`, `xs._()`, `def _(): ...` |
| `...` (in an argument, element or statement list) | zero or more items, e.g. `f(...)`, `f(_, ...)`, `for _ in _: ...` |

Anything that isn't a wildcard must match **exactly**:

- **Names are literal.** `for i in range(_): ...` only matches a loop variable called
  `i`. Use `for _ in range(_): ...` to accept any name.
- **Constants must have the same value and type.** `x == 1` does not match `x == True`
  or `x == 1.0`, and `"_"` in a string is just the text `_`.
- **Operators are exact.** `_ += _` matches `x += 1` but not `x -= 1`.
- **List lengths are exact unless `...` is used.** `sorted(_)` needs exactly one
  positional argument, while `sorted(...)` accepts any number.

### Optional parts

If the pattern leaves one of these parts out, the source may contain anything there:

`else` branches (`orelse`), `finally` blocks, `except` clauses (only when the pattern
has none), decorators, base classes, keyword arguments, parameter annotations, return
annotations, default values, `*args`/`**kwargs`, keyword-only and positional-only
parameters, type parameters, `as` names (`with … as f`, `except E as e`,
`import x as y`), `raise … from …`, the message of an `assert`, match-case guards, and
f-string format specs.

The field names are `annotation`, `asname`, `bases`, `cause`, `decorator_list`,
`defaults`, `finalbody`, `format_spec`, `guard`, `handlers`, `keywords`, `kw_defaults`,
`kwarg`, `kwonlyargs`, `msg`, `name` (only when it is empty, e.g. `except E:` without
`as`), `optional_vars`, `orelse`, `posonlyargs`, `returns`, `type_params` and `vararg`.

A function or lambda pattern **without parameters** matches any parameters:
`def _(): ...` matches every function definition. Once the pattern lists parameters,
they must match: `def _(_, _): ...` matches functions with exactly two positional
parameters.

### Examples

| Pattern | Matches | Doesn't match |
|---|---|---|
| `sorted(...)` | `sorted(xs)`, `sorted(xs, key=len)`, `sorted()` | `xs.sort()` |
| `sorted(_)` | `sorted(xs)`, `sorted(xs, key=len)` | `sorted()`, `sorted(a, b)` |
| `sorted(_, key=len)` | `sorted(xs, key=len)` | `sorted(xs)` |
| `_.sort()` | `xs.sort()`, `self.xs.sort()` | `sort(xs)`, `xs.sort(key=f)`† |
| `_(...)` | every call | |
| `for _ in range(len(_)): ...` | `for i in range(len(xs)): …`, with or without `else` | `for x in xs: …` |
| `while True: ...` | any `while True:` loop | `while x: …` |
| `if _: ...` | every `if` and every `elif` | |
| `if _:\n    return _` | an `if` whose body is only `return <value>` | a body with more statements |
| `if _: _` | an `if` whose body is a single statement | |
| `_ if _ else _` | every conditional expression, nested ones counted separately | |
| `len(_) == 0` | `if len(xs) == 0:` | `if not xs:` |
| `return` | a bare `return` | `return x` |
| `return _` | `return x` | a bare `return` |
| `_ += 1` | `x += 1` | `x += 2`, `x -= 1` |
| `_[_]` | `xs[0]`, `d["k"]`, and slices like `xs[1:2]` | |
| `print("_")` | `print("_")` only | `print("hi")` |
| `def _(_, _): ...` | `def f(a, b): …`, also with annotations and defaults | `def g(a): …` |
| `lambda: _` | every lambda | |
| `class _: ...` | every class, with or without base classes | |
| `with open(...): ...` | `with open(f) as g: …` | |
| `import numpy` | `import numpy`, `import numpy as np` | `from numpy import array` |
| `try: ...\nexcept: ...` | a `try` with exactly one **bare** `except:` | `except ValueError:` |

† Keyword arguments are optional, so `_.sort()` **does** match `xs.sort(key=f)`. It
doesn't match `xs.sort(f)`, because that has a positional argument.

### Limits of patterns

- A pattern can't require that something is **absent**. For example, `sorted(_)` also
  matches `sorted(xs, key=len)`, because keyword arguments are optional.
- Fragments that aren't valid code on their own, such as an `except` clause, a `case`
  clause or a comprehension's `for` part, can't be written as patterns. Use `node`
  instead (`ExceptHandler`, `match_case`, `comprehension`).
- Every matching node counts separately, including nested ones:
  `fib(_)` counts twice in `fib(n - 1) + fib(n - 2)`.

---

## Combinators

| Rule | Holds if |
|---|---|
| `{"all": [r1, r2, …]}` | every rule holds (a test's top-level list already works like this) |
| `{"any": [r1, r2, …]}` | at least one rule holds |
| `{"not": r}` | `r` does not hold |

Combinators accept `message`, and they accept `scope`, which applies to every rule inside
them that has no `scope` of its own. They can be nested.

```python
# iterate somehow in `total`: a loop or recursion
{"scope": "total", "any": [{"concept": "loop"}, {"concept": "recursion"}]}

# neither an if nor a loop in `fib`
{"scope": "fib", "not": {"any": [{"concept": "if"}, {"concept": "loop"}]}}

# either a match statement, or no conditionals at all
{"any": [{"concept": "match"}, {"concept": "conditional", "max": 0}],
 "message": "Use a match statement instead of if/elif chains."}
```

A missing `scope` is always reported, even inside `any` or `not`.

---

## Creating a StructureTest with `create_task`

In `uv run create_task`, choose **StructureTest** when asked which kind of test to add.
The prompts are:

- `name`: text.
- `file_path`: choose one of the exercise files.
- `rules`: a Python expression, e.g.
  `[{"concept": "recursion", "scope": "fib"}, {"pattern": "sorted(...)", "max": 0}]`.
  The available concept names are listed below the input. If a rule is invalid, the
  error is shown and the prompt is asked again.

The rules can also come from a test information file: a variable named `rules` in that
file is used directly.

## How it's stored

In the task file, a `StructureTest` stores the rules exactly as written, as plain JSON:

```json
{
  "__instance_type__": ["pyics.tests.tests", "StructureTest"],
  "attributes": {
    "name": "recursive fib",
    "file_path": {"__pathlib__": "/…/fib.py"},
    "rules": [
      {"concept": "recursion", "scope": "fib"},
      {"concept": "loop", "scope": "fib", "max": 0, "message": "Solve it without loops."}
    ]
  }
}
```

Task files refer to concepts by name, so renaming a concept in `CONCEPTS` breaks existing
task files that use it.
