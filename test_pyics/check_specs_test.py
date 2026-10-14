"""
Test the comparison of symbols against specifications.
"""

from pathlib import Path
from typing import Optional

import pytest

import pyics.util.json_handling as json
from pyics.tests.check_specs import compare_spec, to_spec_dict
from pyics.tests.tests import SpecTest


def _compare(expected, other, **kwargs) -> list[str]:
    return compare_spec(expected, other, use_rich_markup=False, **kwargs)


class Node:
    value: int

    def __init__(self, value: int, *, label: str = "", parent: "Node | None" = None):
        self.value = value
        self.label = label
        self.parent = parent

    def add(self, other: "Node", times: int) -> "Node":
        return self

    @property
    def depth(self) -> int:
        return 0

    @depth.setter
    def depth(self, value: int) -> None: ...

    @staticmethod
    def make(value: int) -> "Node":
        return Node(value)

    @classmethod
    def root(cls) -> "Node":
        return cls(0)


def search(edges: list[tuple[str, str]], start: str, *args, depth: int = 3) -> bool:
    return True


# conversion ###########################################################################


def test_function_to_dict():
    assert to_spec_dict(search) == {
        "is_class": False,
        "edges": "list[tuple[str, str]]",
        "start": "str",
        "args": {"type": None, "kind": "VAR_POSITIONAL"},
        "depth": {"type": "int", "kind": "KEYWORD_ONLY", "default": 3},
        "return": "bool",
    }


def test_class_to_dict():
    spec = to_spec_dict(Node)
    assert spec["value"] == "int"
    assert spec["label"] is None
    assert spec["__init__"] == {
        "is_class": False,
        "value": "int",
        "label": {"type": "str", "kind": "KEYWORD_ONLY", "default": ""},
        "parent": {"type": "Node | None", "kind": "KEYWORD_ONLY", "default": None},
        "return": None,
    }
    # self/cls are dropped, staticmethods keep all parameters
    assert spec["add"] == {
        "is_class": False,
        "other": "Node",
        "times": "int",
        "return": "Node",
    }
    assert spec["make"] == {"is_class": False, "value": "int", "return": "Node"}
    assert spec["root"] == {"is_class": False, "return": "Node"}
    assert spec["depth"] == {
        "is_class": False,
        "is_property": True,
        "return": "int",
        "setter": True,
        "deleter": False,
    }


def test_nested_class():
    class Outer:
        class Inner:
            x: int

        alias = int  # not a nested class

    spec = to_spec_dict(Outer)
    assert spec["Inner"] == {"is_class": True, "x": "int"}
    assert "alias" not in spec


class Base:
    x: int

    class Inner:
        z: int

    def __init__(self, x: int, y: str):
        self.x = x
        self.y = y

    def shared(self) -> int:
        return 1

    def replaced(self) -> int:
        return 1

    def hidden(self) -> int:
        return 1

    @property
    def p(self) -> int:
        return 1


class Child(Base):
    hidden = None  # pyright: ignore[reportAssignmentType] - a method replaced by no method

    def own(self) -> None: ...

    def replaced(self, factor: int = 1) -> int:
        return factor


def test_inherited_members_to_dict():
    spec = to_spec_dict(Child)
    assert spec["x"] == "int"
    assert spec["y"] is None
    assert spec["__init__"] == to_spec_dict(Base)["__init__"]
    assert spec["shared"] == {"is_class": False, "return": "int"}
    assert spec["p"] == to_spec_dict(Base)["p"]
    assert spec["own"] == {"is_class": False, "return": "None"}
    assert spec["Inner"] == {"is_class": True, "z": "int"}


def test_overridden_member_wins():
    spec = to_spec_dict(Child)
    assert spec["replaced"] == {
        "is_class": False,
        "factor": {"type": "int", "default": 1},
        "return": "int",
    }
    assert "hidden" not in spec


def test_spec_is_json_serializable():
    spec = to_spec_dict(Node)
    assert json.loads(json.dumps(spec)) == spec


@pytest.mark.parametrize(
    "spec",
    [
        {"x": "int"},
        {"is_class": False, "x": 3},
        {"is_class": False, "x": {"type": "int", "typo": 1}},
        {"is_class": False, "x": {"kind": "NOT_A_KIND"}},
        {"is_class": True, "return": "int"},
        {"is_class": True, "m": {"is_class": False, "x": 3}},
    ],
)
def test_invalid_spec_dicts(spec):
    with pytest.raises(ValueError):
        to_spec_dict(spec)


def test_invalid_spec_type():
    with pytest.raises(TypeError):
        to_spec_dict(3)  # pyright: ignore[reportArgumentType]


# named comparison #####################################################################


def test_matching_specs():
    assert _compare(Node, Node) == []
    assert _compare(search, search, exact=True) == []
    assert _compare(to_spec_dict(Node), Node, exact=True) == []


def test_kind_mismatch():
    assert _compare(Node, search) == ["Expected class, got <class 'function'>"]
    assert _compare(search, Node) == [
        "Expected a function/callable, got <class 'type'>"
    ]


def test_function_differences():
    def other(edges: list, depth: int = 4, *, start: str, extra=None) -> bool: ...

    assert _compare(search, other, exact=True) == [
        "Parameter 'edges' type mismatch. Expected list[tuple[str, str]], got list.",
        "Parameter 'start' kind mismatch. Expected positional or keyword, got keyword-only.",
        "Missing parameter: 'args'.",
        "Parameter 'depth' default value mismatch. Expected 3, got 4.",
        "Parameter 'depth' kind mismatch. Expected keyword-only, got positional or keyword.",
        "Additional parameter: 'extra'.",
    ]


def test_missing_type_hints():
    def other(edges, start, *args, depth=3): ...

    assert _compare(search, other) == [
        "Parameter 'edges' is missing its type hint.",
        "Parameter 'start' is missing its type hint.",
        "Parameter 'depth' is missing its type hint.",
        "Return value is missing its type hint.",
    ]
    assert _compare(search, other, flag_type_hints=False) == []


def test_defaults():
    spec = {"is_class": False, "x": {"default": 1}, "y": None}

    def missing(x, y=2): ...

    assert _compare(spec, missing) == ["Parameter 'x' is missing its default value 1."]
    assert _compare(spec, missing, exact=True) == [
        "Parameter 'x' is missing its default value 1.",
        "Parameter 'y' should not have a default value.",
    ]


def test_class_differences():
    class Other:
        value: str

        def __init__(self, value: str):
            self.value = value
            self.extra = 1
            self.depth = 3

        def add(self, other: "Other"): ...

        @staticmethod
        def root(): ...

    assert _compare(Node, Other, exact=True) == [
        "Attribute 'value' type mismatch. Expected int, got str.",
        "Missing attribute: 'label'.",
        "Missing attribute: 'parent'.",
        "Additional attribute: 'extra'.",
        "Method '__init__': Parameter 'value' type mismatch. Expected int, got str.",
        "Method '__init__': Missing parameter: 'label'.",
        "Method '__init__': Missing parameter: 'parent'.",
        "Method 'add': Parameter 'other' type mismatch. Expected Node, got Other.",
        "Method 'add': Missing parameter: 'times'.",
        "Method 'add': Return value is missing its type hint.",
        "'depth' should be a property, but found an attribute.",
        "Missing method: 'make'.",
        "Method 'root': Return value is missing its type hint.",
    ]


def test_inherited_spec_matches():
    spec = {
        k: v for k, v in to_spec_dict(Base).items() if k not in ("replaced", "hidden")
    }
    assert _compare(spec, Child) == []
    assert _compare(Child, Child, exact=True) == []


def test_implicit_init():
    class Plain:
        pass

    init = {"is_class": False, "return": None}
    assert _compare({"is_class": True, "__init__": init}, Plain, exact=True) == []
    assert _compare(
        {"is_class": True, "__init__": {**init, "x": "int"}}, Plain, exact=True
    ) == ["Method '__init__': Missing parameter: 'x'."]


def test_property_differences():
    class Other:
        @property
        def depth(self) -> str: ...

        @depth.deleter
        def depth(self): ...

    spec = {"is_class": True, "depth": to_spec_dict(Node)["depth"]}
    assert _compare(spec, Other) == [
        "Property 'depth': Getter return value type mismatch. Expected int, got str.",
        "Property 'depth': Missing setter.",
        "Property 'depth': Should not have a deleter.",
    ]


def test_hand_written_dict_equals_converted_class():
    class Spec:
        def __init__(
            self, value: int, *, label: str = "", parent: "Node | None" = None
        ):
            self.value = value

    spec = {
        "is_class": True,
        "value": None,
        "__init__": {
            "is_class": False,
            "value": "int",
            "label": {"type": "str", "kind": "keyword_only", "default": ""},
            "parent": {
                "type": "Optional[Node]",
                "kind": "KEYWORD_ONLY",
                "default": None,
            },
            "return": None,
        },
    }

    class Other:
        def __init__(self, value: str, label: str = ""): ...

    assert _compare(spec, Node) == []
    assert _compare(spec, Other) == _compare(Spec, Other) != []


def test_type_normalization():
    def a(x: Optional[int], nodes: list[Node]) -> "Node": ...

    spec = {
        "is_class": False,
        "x": "int | None",
        "nodes": "list[ Node ]",
        "return": "Node",
    }
    assert _compare(spec, a) == []
    assert _compare(a, a) == []


def test_rich_markup():
    def a(x: list[str]): ...

    assert compare_spec({"is_class": False, "x": "list[int]"}, a) == [
        r"Parameter [b]'x'[/] type mismatch. Expected [b]list\[int][/], got [b]list\[str][/]."
    ]


# comparison by type ###################################################################


def test_by_type_ignores_names_and_order():
    def other(
        depth_limit: int, start_node: str, all_edges: list[tuple[str, str]], x
    ) -> bool: ...

    assert _compare(search, other, by_type=True) == []
    assert _compare(search, other, by_type=True, exact=True) == []

    def fewer(a: int, b: str) -> bool: ...

    assert _compare(search, fewer, by_type=True) == [
        "Missing parameter of type list[tuple[str, str]].",
        "Missing parameter.",
    ]


def test_by_type_class():
    class Other:
        count: int

        def __init__(self, v: int, *, name: str = "", up: "Other | None" = None):
            self.count = v
            self.name = name
            self.up = up

        def add(self, times: int, n: "Other") -> "Other": ...

    spec = {
        "is_class": True,
        "value": "int",
        "extra": "float",
        "__init__": {"is_class": False, "a": "int", "b": "str", "return": None},
        "add": {"is_class": False, "x": "int", "return": "str"},
    }
    assert _compare(spec, Other, by_type=True, exact=True) == [
        "Missing attribute of type float.",
        "Additional attribute.",
        "Additional attribute.",
        "Method '__init__': Additional parameter of type Other | None.",
        "Method 'add': Additional parameter of type Other.",
        "Method 'add': Return value type mismatch. Expected str, got Other.",
    ]


# SpecTest #############################################################################

SOLUTION = """
class Node:
    value: int

    def __init__(self, value: int, *, label: str = "", parent: "Node | None" = None):
        self.value = value
        self.label = label
        self.parent = parent

    def add(self, other: "Node", times: int) -> "Node":
        return self

    @property
    def depth(self) -> int:
        return 0

    @depth.setter
    def depth(self, value: int) -> None: ...

    @staticmethod
    def make(value: int) -> "Node":
        return Node(value)

    @classmethod
    def root(cls) -> "Node":
        return cls(0)
"""


@pytest.mark.parametrize("by_type", [False, True])
def test_spec_test_json_round_trip(tmp_path: Path, by_type: bool):
    solution = tmp_path / "solution.py"
    solution.write_text(SOLUTION)

    test = SpecTest("spec", solution, "Node", Node, by_type=by_type)
    loaded = json.loads(json.dumps(test))
    assert isinstance(loaded, SpecTest)
    assert loaded.expected == test.expected
    assert loaded.by_type == by_type
    loaded.run()

    solution.write_text(SOLUTION.replace("times: int", "times: float"))
    with pytest.raises(AssertionError, match=r"Method \[b\]'add'"):
        loaded.run()


def test_spec_test_of_a_method(tmp_path: Path):
    solution = tmp_path / "solution.py"
    solution.write_text(SOLUTION)

    test = json.loads(json.dumps(SpecTest("spec", solution, "Node.add", Node.add)))
    test.run()

    solution.write_text(SOLUTION.replace("def add", "def plus"))
    with pytest.raises(AssertionError, match=r"solution.py has no symbol 'Node.add'"):
        test.run()


FLAT_SOLUTION = """
class Child:
    x: int

    class Inner:
        z: int

    def __init__(self, x: int, y: str):
        self.x = x
        self.y = y

    def shared(self) -> int:
        return 1

    def replaced(self, factor: int = 1) -> int:
        return factor

    @property
    def p(self) -> int:
        return 1

    def own(self) -> None: ...
"""


def test_spec_test_with_inheritance(tmp_path: Path):
    solution = tmp_path / "solution.py"
    solution.write_text(FLAT_SOLUTION)

    test = json.loads(json.dumps(SpecTest("spec", solution, "Child", Child)))
    test.run()

    solution.write_text(FLAT_SOLUTION.replace("def shared", "def other"))
    with pytest.raises(AssertionError, match=r"Missing method: \[b\]'shared'"):
        test.run()
