from typing import Any


def load_ipython_extension(ip: Any) -> None:
    """
    Load pyics's IPython extension into the shell ``ip``, which runs the tests of a
    notebook's tasks whenever a cell runs (see ``pyics.notebook.extension``): by
    ``%load_ext pyics``, or at the start of the "Python (pyics)" kernel.
    """
    # imported here, so that importing pyics doesn't import IPython
    from pyics.notebook import extension

    extension.load_ipython_extension(ip)


def unload_ipython_extension(ip: Any) -> None:
    """Unload pyics's IPython extension from the shell ``ip`` (``%unload_ext
    pyics``)."""
    from pyics.notebook import extension

    extension.unload_ipython_extension(ip)
