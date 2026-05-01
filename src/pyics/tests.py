from abc import ABC, abstractmethod


class Test(ABC):
    """Base Class for Tests"""

    def __init__(self, test_dict: dict): ...

    @abstractmethod
    def serialize(self) -> dict: ...


class PrintTest(Test):
    """console output of file needs to match"""

    def __init__(self, test_dict: dict):
        super().__init__(test_dict)


class SimpleTest(Test):
    """output of a function must match the desired output for some parameters"""

    def __init__(self, test_dict: dict):
        super(SimpleTest, self).__init__(test_dict)


class StructureTest(Test):
    """file contains some code-snippet"""

    def __init__(self, test_dict: dict):
        super().__init__(test_dict)


class ProtocolTest(Test):
    """symbols in file match some protocol"""

    def __init__(self, test_dict: dict):
        super().__init__(test_dict)
