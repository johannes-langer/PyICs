"""
Test the checks of source code against structure rules.
"""

from pathlib import Path
from textwrap import dedent

import pytest

import pyics.util.json_handling as json
from pyics.tests.check_structure import (
    CONCEPTS,
    Rule,
    check_structure,
    validate_rules,
)
from pyics.tests.tests import StructureTest


def _check(rules: list[Rule] | Rule, source: str) -> list[str]:
    return check_structure(rules, dedent(source), use_rich_markup=False)


def _count(rule: Rule, source: str, n: int) -> list[str]:
    """messages for ``rule`` not finding exactly ``n`` nodes in ``source``"""
    return _check({**rule, "min": n, "max": n}, source)


SEARCH = """
import math
from collections import deque


def fib(n):
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)


def total(xs):
    s = 0
    for i in range(len(xs)):
        for x in xs:
            s += x
    return sorted(xs)


def is_even(n):
    return True if n == 0 else is_odd(n - 1)


def is_odd(n):
    return False if n == 0 else is_even(n - 1)
"""

TREE = """
class Tree:
    def __init__(self, children):
        self.children = children

    def size(self):
        return 1 + sum(child.size() for child in self.children)

    def depth(self):
        return 1 + max((self.depth() for _ in self.children), default=0)

    def leaves(self):
        return len(self.children)

    @staticmethod
    def make(n):
        return Tree([]) if n == 0 else Tree([Tree.make(n - 1)])


class Sub(Tree):
    def walk(self):
        self.children.append(None)
        return self.walk()


def outer(n):
    def helper(k):
        return helper(k - 1) if k else 0

    return helper(n)


def leaves(tree):
    return tree.leaves()
"""


# concepts #############################################################################


@pytest.mark.parametrize(
    ("scope", "n"),
    [
        ("fib", 2),
        ("total", 0),
        ("is_even", 1),  # mutual recursion
        ("is_odd", 1),
        (None, 4),
    ],
)
def test_recursion(scope: str | None, n: int):
    assert _count({"concept": "recursion", "scope": scope}, SEARCH, n) == []


@pytest.mark.parametrize(
    ("scope", "n"),
    [
        ("Tree.size", 1),  # on another instance
        ("Tree.depth", 1),  # on self
        ("Tree.leaves", 0),  # len() is not resolved to a method
        ("Tree.make", 1),  # via the class
        ("Tree.__init__", 0),
        ("Sub.walk", 1),  # .append() is not a method of Sub
        ("outer", 1),  # in a nested function
        ("outer.helper", 1),
        ("leaves", 0),  # a function calling a method with the same name
    ],
)
def test_recursion_in_classes(scope: str, n: int):
    assert _count({"concept": "recursion", "scope": scope}, TREE, n) == []


# (concept, source, number of nodes found)
CONCEPT_CASES = [
    ("loop", "for x in y: pass\nwhile x: pass\n[x for x in y]", 2),
    ("for_loop", "for x in y: pass\nwhile x: pass", 1),
    ("while_loop", "for x in y: pass\nwhile x: pass", 1),
    ("comprehension", "[x for x in y]\n{x for x in y}\n{x: 1 for x in y}\n(x for x in y)", 4),
    ("conditional", "if a: pass\nelif b: pass\nx = 1 if a else 2\nmatch x:\n case _: pass", 4),
    ("if", "if a: pass\nelif b: pass\nelse: pass", 2),
    ("ternary", "x = 1 if a else 2", 1),
    ("match", "match x:\n case 1: pass\n case _: pass", 1),
    ("function", "def f(): pass\nasync def g(): pass\nh = lambda: 1", 2),
    ("lambda", "h = lambda: 1", 1),
    ("class", "class A:\n class B: pass", 2),
    ("generator", "def f():\n yield 1\n yield from g()", 2),
    ("exception_handling", "try: pass\nexcept: pass", 1),
    ("raise", "raise ValueError", 1),
    ("with", "with open(f) as g: pass", 1),
    ("global", "def f():\n global x\n def g():\n  nonlocal y", 2),
]  # fmt: skip


@pytest.mark.parametrize(("concept", "source", "n"), CONCEPT_CASES)
def test_concepts(concept: str, source: str, n: int):
    assert _count({"concept": concept}, source, n) == []


def test_nested_loop():
    source = """
    for x in xs:
        while x:
            for y in x:
                pass
    for x in xs:
        def f():
            for y in x:  # not nested, as it is in another function
                pass
    """
    assert _count({"concept": "nested_loop"}, source, 2) == []


@pytest.mark.parametrize(
    ("name", "n"),
    [("sorted", 1), ("sort", 2), ("math.sqrt", 1), ("sqrt", 1), ("print", 0)],
)
def test_call(name: str, n: int):
    source = "sorted(xs)\nxs.sort()\nself.xs.sort()\nmath.sqrt(2)"
    assert _count({"concept": "call", "name": name}, source, n) == []


@pytest.mark.parametrize(
    ("name", "n"),
    [(None, 5), ("numpy", 3), ("numpy.linalg", 2), ("collections", 1), ("num", 0)],
)
def test_import(name: str | None, n: int):
    source = """
    import numpy as np
    import numpy.linalg, os
    from numpy.linalg import norm
    from collections import deque
    from . import sibling
    """
    rule = {"concept": "import"} | ({"name": name} if name else {})
    assert _count(rule, source, n) == []


def test_all_concepts_are_tested():
    tested = {"loop", "nested_loop", "recursion", "call", "import"}
    tested |= {concept for concept, _, _ in CONCEPT_CASES}
    assert tested == set(CONCEPTS)


# nodes and patterns ###################################################################


def test_node():
    source = "x = [a for a in b]\ny = (a for a in b)\nprint(x)"
    assert _count({"node": "ListComp"}, source, 1) == []
    assert _count({"node": ["ListComp", "GeneratorExp"]}, source, 2) == []
    assert _count({"node": "stmt"}, source, 3) == []  # abstract node classes


@pytest.mark.parametrize(
    ("pattern", "source", "n"),
    [
        # _ matches any expression, ... any number of arguments
        ("sorted(_)", "sorted(xs)\nsorted()\nsorted(a, b)", 1),
        ("sorted(...)", "sorted(xs)\nsorted()\nsorted(a, b)", 3),
        ("sorted(_, ...)", "sorted(xs)\nsorted()\nsorted(a, b)", 2),
        # keyword arguments are optional
        ("sorted(_)", "sorted(xs, key=len)", 1),
        ("sorted(_, key=len)", "sorted(xs)\nsorted(xs, key=len)", 1),
        # _ as an identifier
        ("_.sort()", "xs.sort()\nself.xs.sort()\nsort(xs)", 2),
        ("xs._()", "xs.sort()\nys.sort()", 1),
        # ... as a body, optional else branches
        ("for _ in range(len(_)): ...", "for i in range(len(xs)):\n print(i)", 1),
        ("for _ in range(len(_)): ...", "for i in range(len(xs)): pass\nelse: pass", 1),
        ("for _ in range(len(_)): ...", "for x in xs: pass", 0),
        ("while True: ...", "while True: break\nwhile x: break", 1),
        ("if _:\n    return _", "if a:\n return 1\nif b:\n c = 1\n return c", 1),
        ("if _: _", "if a:\n return 1\nif b:\n c = 1\n return c", 1),  # one statement
        # constants are matched exactly
        ("x == 1", "x == 1\nx == True\nx == 1.0", 1),
        ('print("_")', 'print("_")\nprint("hi")', 1),
        # function definitions: names, parameters and annotations
        ("def _(): ...", "def f(x: int = 1) -> int: pass\nasync def g(): pass", 1),
        ("def _(_, _): ...", "def f(a, b): pass\ndef g(a): pass", 1),
        ("lambda: _", "f = lambda x: x\ng = lambda: 1", 2),
        # a bare except, as opposed to any except
        ("try: ...\nexcept: ...", "try: pass\nexcept: pass", 1),
        ("try: ...\nexcept: ...", "try: pass\nexcept ValueError: pass", 0),
        ("_ if _ else _", "x = 1 if a else (2 if b else 3)", 2),
        ("    sorted(_)", "sorted(xs)", 1),  # indentation is removed
    ],
)  # fmt: skip
def test_pattern(pattern: str, source: str, n: int):
    assert _count({"pattern": pattern}, source, n) == []


# modifiers ############################################################################


def test_messages():
    assert _check(
        [
            {"concept": "recursion", "scope": "total"},
            {"concept": "loop", "scope": "total", "max": 0},
            {"concept": "loop", "min": 3},
            {"concept": "loop", "max": 1},
            {"concept": "loop", "min": 1, "max": 1},
            {"concept": "ternary", "min": 3, "max": 4},
            {"concept": "call", "name": "sorted", "max": 0},
            {"node": ["ListComp", "SetComp"]},
            {"pattern": "for _ in range(len(_)):\n    ...", "max": 0},
        ],
        SEARCH,
    ) == [
        "Expected recursive call in 'total', found none.",
        "Expected no loop in 'total', found 2 (lines 14, 15).",
        "Expected at least 3 × loop, found 2 (lines 14, 15).",
        "Expected at most 1 × loop, found 2 (lines 14, 15).",
        "Expected exactly 1 × loop, found 2 (lines 14, 15).",
        "Expected between 3 and 4 × conditional expression, found 2 (lines 21, 25).",
        "Expected no call to 'sorted', found 1 (line 17).",
        "Expected ListComp or SetComp node, found none.",
        "Expected no `for _ in range(len(_)): ...`, found 1 (line 14).",
    ]


def test_custom_message():
    rules = {"concept": "loop", "scope": "total", "max": 0, "message": "Use recursion."}
    assert _check(rules, SEARCH) == ["Use recursion."]
    assert _check({**rules, "scope": "fib"}, SEARCH) == []


def test_missing_scope():
    rules = [
        {"concept": "loop", "scope": "fibonacci"},
        {"concept": "loop", "scope": "fibonacci", "message": "Use a loop."},
        {"any": [{"concept": "loop"}, {"concept": "if", "scope": "fib.inner"}]},
    ]
    assert _check(rules, SEARCH) == [
        "No function or class 'fibonacci' found.",  # only once
        "No function or class 'fib.inner' found.",
    ]


def test_syntax_error():
    assert _check({"concept": "loop"}, "def f(:") == [
        "Could not parse the source: invalid syntax (line 1)."
    ]
    assert check_structure(
        {"concept": "loop"}, "def f(:", filename="f.py", use_rich_markup=False
    ) == ["Could not parse 'f.py': invalid syntax (line 1)."]


def test_rich_markup():
    [message] = check_structure({"pattern": "xs[b]", "max": 0}, "xs[b]\nxs[b]")
    assert message == "Expected no [b]`xs\\[b]`[/], found [b]2[/] (lines 1, 2)."


# combinators ##########################################################################


def test_any():
    loop_or_recursion = {"any": [{"concept": "loop"}, {"concept": "recursion"}]}
    assert _check({**loop_or_recursion, "scope": "fib"}, SEARCH) == []
    assert _check({**loop_or_recursion, "scope": "total"}, SEARCH) == []
    assert _check(
        {
            "any": [
                {"concept": "match"},
                {"all": [{"concept": "while_loop"}, {"concept": "lambda"}]},
            ]
        },
        SEARCH,
    ) == [
        (
            "None of the following holds:\n"
            "  - Expected match statement, found none.\n"
            "  - Expected while loop, found none.\n"
            "    Expected lambda, found none."
        )
    ]


def test_all():
    rules = {"all": [{"concept": "if"}, {"concept": "loop", "max": 0}], "scope": "fib"}
    assert _check(rules, SEARCH) == []
    assert _check({**rules, "scope": "total"}, SEARCH) == [
        "Expected if statement in 'total', found none.",
        "Expected no loop in 'total', found 2 (lines 14, 15).",
    ]
    assert _check({**rules, "scope": "total", "message": "Oops."}, SEARCH) == ["Oops."]


def test_not():
    assert _check({"not": {"concept": "match"}}, SEARCH) == []
    assert _check(
        {"scope": "fib", "not": {"any": [{"concept": "if"}, {"concept": "loop"}]}},
        SEARCH,
    ) == [
        (
            "Expected the following not to hold:\n"
            "  - any of:\n"
            "      - if statement in 'fib'\n"
            "      - loop in 'fib'"
        )
    ]


def test_scope_is_inherited():
    rules = {"scope": "fib", "all": [{"concept": "if"}, {"concept": "loop"}]}
    assert _check(rules, SEARCH) == ["Expected loop in 'fib', found none."]

    rules["all"][1]["scope"] = "total"  # a scope of its own overrides the inherited one
    assert _check(rules, SEARCH) == []


# validation ###########################################################################


@pytest.mark.parametrize(
    "rule",
    [
        "loop",
        {},
        {"concept": "loop", "node": "For"},
        {"concept": "nope"},
        {"concept": "call"},
        {"concept": "call", "name": 3},
        {"concept": "loop", "name": "x"},
        {"node": "Nope"},
        {"node": "__class__"},
        {"node": []},
        {"pattern": "def f(:"},
        {"pattern": "a = 1\nb = 2"},
        {"pattern": ["sorted(_)"]},
        {"concept": "loop", "min": -1},
        {"concept": "loop", "min": True},
        {"concept": "loop", "max": 1.5},
        {"concept": "loop", "min": 2, "max": 1},
        {"concept": "loop", "scope": "a..b"},
        {"concept": "loop", "scope": 3},
        {"concept": "loop", "message": 3},
        {"any": []},
        {"any": {"concept": "loop"}},
        {"all": [{"concept": "loop"}], "min": 1},
        {"not": {"concept": "nope"}},
    ],
)
def test_invalid_rules(rule):
    with pytest.raises(ValueError):
        validate_rules([rule])


def test_invalid_rules_type():
    with pytest.raises(TypeError):
        validate_rules("loop")  # pyright: ignore[reportArgumentType]


def test_validation_path():
    with pytest.raises(ValueError, match=r"^rules\[1\]\.any\[0\]\.concept: unknown"):
        validate_rules([{"concept": "loop"}, {"any": [{"concept": "nope"}]}])


def test_single_rule_is_wrapped():
    assert validate_rules({"concept": "loop"}) == [{"concept": "loop"}]


# StructureTest ########################################################################

RECURSIVE = """
def fib(n):
    return n if n < 2 else fib(n - 1) + fib(n - 2)

raise SystemExit  # never executed
"""

ITERATIVE = """
def fib(n):
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a
"""


def test_structure_test_json_round_trip(tmp_path: Path):
    solution = tmp_path / "solution.py"
    solution.write_text(RECURSIVE)

    rules = [
        {"concept": "recursion", "scope": "fib"},
        {"concept": "loop", "scope": "fib", "max": 0, "message": "No loops, please."},
    ]
    test = StructureTest("structure", solution, rules)
    loaded = json.loads(json.dumps(test))
    assert isinstance(loaded, StructureTest)
    assert loaded.rules == rules
    loaded.run()

    solution.write_text(ITERATIVE)
    with pytest.raises(AssertionError) as err:
        loaded.run()
    assert str(err.value) == (
        "Expected recursive call in [b]'fib'[/], found none.\nNo loops, please."
    )


def test_structure_test_validates_rules(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown concept"):
        StructureTest("structure", tmp_path / "solution.py", {"concept": "recursive"})
