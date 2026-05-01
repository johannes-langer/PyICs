from abc import ABC
from typing import Optional


class Action(ABC):
    """Base Class for actions to perform on key-press"""

    def __init__(self, name: str, key: str, ref: Optional[str] = None, **kwargs):
        """
        Compose an action object. Note that an action object should be callable without
        parameters (refer to how kwargs are used).

        Parameters
        ----------
        name: str
            Name of the Action
        key: str
            Hotkey associated with the action
        ref: str (optional)
            Optional key reference, in case the actual key-press is unwieldy.
        **kwargs
            kwargs to encode call parameters to specific values.
        """
        self.name = name
        self.key = key
        self.ref = ref if ref else self.key
        self.kwargs = kwargs

    def __call__(self) -> None:
        self.exec(**self.kwargs)

    @staticmethod
    def exec(**kwargs) -> None: ...


class Quit(Action):
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


class Demo(Action):
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
        cli.update_main_content("[dim]The hotkey for the demo action has been pressed.")
