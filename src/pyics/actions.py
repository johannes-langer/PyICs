from abc import ABC, abstractmethod
from typing import Any, Optional

from pyics.logging import get_logger


class Action(ABC):
    """A parameterless, kwargs-bound unit of work."""

    def __init__(self, name: str, **kwargs):
        """
        Compose an Action object. Note that an Action object should be callable without
        parameters (refer to how kwargs are used).

        Parameters
        ----------
        name: str
            Name of the Action
        **kwargs
            kwargs to encode call parameters to specific values.
        """
        self.name = name
        self.kwargs = kwargs

    def __call__(self):
        get_logger(__name__).info(f"Triggered {self.name} action.")
        self.exec(**self.kwargs)

    @staticmethod
    @abstractmethod
    def exec(**kwargs) -> None: ...


class KeyAction(Action):
    """An Action bound to a hotkey"""

    def __init__(self, name: str, key: str, ref: Optional[str] = None, **kwargs):
        """
        Compose a KeyAction object. Note that an Action object should be callable without
        parameters (refer to how kwargs are used).

        Parameters
        ----------
        name: str
            Name of the KeyAction
        key: str
            Hotkey associated with the action
        ref: str (optional)
            Optional key reference, in case the actual key-press is unwieldy.
        **kwargs
            kwargs to encode call parameters to specific values.
        """
        super().__init__(name, **kwargs)
        self.key = key
        self.ref = ref if ref else self.key

    def __call__(self) -> None:
        self.exec(**self.kwargs)


class Quit(KeyAction):
    """
    Quit application on ctrl-c press
    """

    def __init__(self, name: str = "Quit", key: str = "\x03", ref="ctrl-c", **kwargs):
        """
        Parameters
        ----------
        cli: CLI (kwarg)
            CLI object
        pyics: PyICs (kwarg)
            PyICs object
        """
        super().__init__(name, key, ref=ref, **kwargs)

    @staticmethod
    def exec(**kwargs):
        cli = kwargs["cli"]
        pyics = kwargs["pyics"]

        cli.running = False
        pyics.running = False


class Demo(KeyAction):
    """
    Perform a demo action
    """

    def __init__(self, name: str = "Demo", key: str = "d", **kwargs):
        """
        Parameters
        ----------
        cli: CLI (kwarg)
            CLI object
        """
        super().__init__(name, key, **kwargs)

    @staticmethod
    def exec(**kwargs) -> None:
        cli = kwargs["cli"]
        pyics = kwargs["pyics"]
        pyics.next_task()
        cli.update_main_content(pyics.current_task.status)
        cli.update_info(pyics.current_task.info)


class Next(KeyAction):
    """Move to the next task."""

    def __init__(self, name: str = "Next", key: str = "n", **kwargs):
        super().__init__(name, key, **kwargs)

    @staticmethod
    def exec(**kwargs) -> None:
        """TODO"""
        ...


class Previous(KeyAction):
    """Go back to the previous task."""

    def __init__(self, name: str = "Previous", key: str = "p", **kwargs):
        super().__init__(name, key, **kwargs)

    @staticmethod
    def exec(**kwargs) -> None:
        """TODO"""
        ...


class ResetTask(KeyAction):
    """Reset the current task."""

    def __init__(self, name: str = "Reset Task", key: str = "r", **kwargs):
        super().__init__(name, key, **kwargs)

    @staticmethod
    def exec(**kwargs) -> None:
        """TODO"""
        ...


class ScrollUp(KeyAction):
    """Scroll the main content viewport up."""

    def __init__(
        self, name: str = "Scroll", key: str = "UP", ref: str = "\u2191/\u2193", **kwargs
    ):
        super().__init__(name, key, ref=ref, **kwargs)

    @staticmethod
    def exec(**kwargs) -> None:
        kwargs["cli"].scroll(-1)


class ScrollDown(KeyAction):
    """Scroll the main content viewport down."""

    def __init__(
        self, name: str = "Scroll", key: str = "DOWN", ref: str = "\u2191/\u2193", **kwargs
    ):
        super().__init__(name, key, ref=ref, **kwargs)

    @staticmethod
    def exec(**kwargs) -> None:
        kwargs["cli"].scroll(1)
