"""
pyics-jupyter: live results for pyics. Whenever a cell of an exercise notebook runs,
the IPython extension (see ``extension``) runs the tests of the notebook's tasks in its
kernel, and the plugin (see ``plugin``) shows their results in pyics's TUI.

The "Python (pyics)" kernel (see ``kernelspec``) loads the extension at every start;
other IPython kernels load it with ``%load_ext pyics_jupyter``. The extension is
imported only then, so that pyics, which imports the plugin, doesn't import IPython.
"""

from typing import Any


def load_ipython_extension(ip: Any) -> None:
    """Load the extension into the IPython shell ``ip``."""
    from pyics_jupyter import extension

    extension.load_ipython_extension(ip)


def unload_ipython_extension(ip: Any) -> None:
    """Unload the extension from the IPython shell ``ip``."""
    from pyics_jupyter import extension

    extension.unload_ipython_extension(ip)
