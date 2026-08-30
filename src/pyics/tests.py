import importlib.util as iutil
import sys
from abc import ABC, abstractmethod
from importlib.machinery import ModuleSpec
from os import PathLike
from types import ModuleType
from typing import Any


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

    @abstractmethod
    def serialize(self) -> dict:
        """turn this test object into a dictionary for export"""
        ...

    @abstractmethod
    def run(self) -> None: ...


class PrintTest(Test):
    """console output of file needs to match"""

    def __init__(self, test_dict: dict): ...


class SimpleTest(Test):
    """output of a function must match the desired output for some parameters"""

    function_name: str
    args: list[Any]
    kwargs: dict[str, Any]
    expected: Any

    def __init__(
        self,
        name: str,
        file_path: PathLike,
        function_name: str,
        expected: Any,
        /,
        function_args: list[Any] = [],
        function_kwargs: dict[str, Any] = {},
    ):
        self.name = name
        self.file_path = file_path
        self.function_name = function_name
        self.args = function_args
        self.kwargs = function_kwargs
        self.expected = expected

    def serialize(self) -> dict: ...

    def run(self) -> None:
        m = self.load_solution()
        func = getattr(m, self.function_name)
        res = func(*self.args, **self.kwargs)

        assert res == self.expected


class StructureTest(Test):
    """file contains some code-snippet"""

    def __init__(self, test_dict: dict): ...


class ProtocolTest(Test):
    """symbols in file match some protocol"""

    def __init__(self, test_dict: dict): ...


class SizeTest(Test):
    """file has a maximum line number or size"""

    def __init__(self, test_dict: dict): ...
