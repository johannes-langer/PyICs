import importlib.util as iutil
import sys
from abc import ABC, abstractmethod
from importlib.machinery import ModuleSpec
from os import PathLike
from types import ModuleType
from typing import Any, Protocol

from _pytest.assertion.util import assertrepr_compare, dummy_highlighter

import pyics.util.json_handling as json
from pyics.tests.capture import capture_stdout
from . import check_specs


class Test(ABC):
    """Base Class for Tests"""

    name: str
    file_path: PathLike

    @abstractmethod
    def __init__(self): ...

    @classmethod
    def from_dict(cls, test_dict: dict) -> "Test":
        return cls(**test_dict)

    def __call__(self) -> None:
        self.run()

    def load_solution(self) -> ModuleType:
        spec: ModuleSpec | None = iutil.spec_from_file_location(
            name="current_solution", location=self.file_path
        )
        if not isinstance(spec, ModuleSpec):
            raise ImportError("Could not import solution file. Has the file been moved?")
        if spec.loader is None:
            raise AttributeError("spec.loader cannot be None.")

        module = iutil.module_from_spec(spec)
        sys.modules["current_solution"] = module
        spec.loader.exec_module(module)

        return module

    def serialize(self) -> str | bytes:
        """turn this test object into a dictionary for export"""
        return json.dumps(self)

    @abstractmethod
    def run(self) -> None: ...


class PrintTest(Test):
    """console output of file needs to match"""

    expected: str  # printout is necessarily str

    def __init__(self, name: str, file_path: PathLike, expected: str, **kwargs):
        self.name = name
        self.file_path = file_path
        self.expected = expected

    def run(self) -> None:
        with capture_stdout() as buffer:
            self.load_solution()
        if (res := buffer.getvalue()) != self.expected:
            expl = list(
                assertrepr_compare(
                    "==",
                    res,
                    self.expected,
                    verbose=1,
                    highlighter=dummy_highlighter,
                    assertion_text_diff_style="ndiff",
                )
            ) or [f"{res!r} == {self.expected!r}"]

            raise AssertionError("\n".join(expl))


class SimpleTest(Test):
    """output of a function must match the desired output for some parameters"""

    function_name: str
    function_args: list[Any]
    function_kwargs: dict[str, Any]
    expected: Any

    def __init__(
        self,
        name: str,
        file_path: PathLike,
        function_name: str,
        expected: Any,
        *,
        function_args: list[Any] = [],
        function_kwargs: dict[str, Any] = {},
        **kwargs,
    ):
        self.name = name
        self.file_path = file_path
        self.function_name = function_name
        self.expected = expected
        self.function_args = function_args
        self.function_kwargs = function_kwargs

    def run(self) -> None:
        m = self.load_solution()
        func = getattr(m, self.function_name)
        res = func(*self.function_args, **self.function_kwargs)

        if res != self.expected:
            expl = list(
                assertrepr_compare(
                    "==",
                    res,
                    self.expected,
                    verbose=1,
                    highlighter=dummy_highlighter,
                    assertion_text_diff_style="ndiff",
                )
            ) or [f"{res!r} == {self.expected!r}"]

            raise AssertionError("\n".join(expl))


class StructureTest(Test):
    """Like SpecTest, but only targets typing, no symbol names."""

    def __init__(self, test_dict: dict): ...


class SpecTest(Test):
    """symbols in file match some protocol"""

    symbol_name: str
    expected: check_specs.SpecType

    def __init__(
        self,
        name: str,
        file_path: PathLike,
        symbol_name: str,
        expected: check_specs.SpecType,
        **kwargs,
    ):
        self.name = name
        self.file_path = file_path
        self.symbol_name = symbol_name
        self.expected = expected

    def run(self) -> None:
        m = self.load_solution()
        symb = getattr(m, self.symbol_name)

        if diff := check_specs.compare_spec(self.expected, symb):
            raise AssertionError("\n".join(diff))


class SizeTest(Test):
    """file has a maximum line number or size"""

    def __init__(self, test_dict: dict): ...
