"""
The "Python (pyics)" Jupyter kernel: an IPython kernel that runs in this environment's
interpreter, with the same pyics as the TUI, and loads the extension at every start,
restarts included (see ``extension``).
"""

import os
import tempfile
from pathlib import Path

from pyics.util.paths import StrPath

KERNEL_NAME = "pyics"
DISPLAY_NAME = "Python (pyics)"
# makes IPython load the extension (``InteractiveShellApp.extra_extensions``)
EXTENSION_ARGUMENT = "--ext=pyics_jupyter"


def install(*, user: bool = True, prefix: StrPath | None = None) -> Path:
    """
    Install the kernel spec of the "Python (pyics)" kernel (named "pyics"), replacing
    an existing one: for the current ``user`` (in Jupyter's data directory), or into
    ``prefix`` (``<prefix>/share/jupyter/kernels``) if given, or else for all users.
    Returns the directory it was installed in.

    Raises
    ------
    OSError
        If it can't be installed, e.g. without the rights to install it for all users.
    """
    from ipykernel.kernelspec import write_kernel_spec
    from jupyter_client.kernelspec import KernelSpecManager

    with tempfile.TemporaryDirectory() as temp:
        spec = write_kernel_spec(
            Path(temp) / KERNEL_NAME,
            overrides={"display_name": DISPLAY_NAME},
            extra_arguments=[EXTENSION_ARGUMENT],
        )
        installed = KernelSpecManager().install_kernel_spec(
            spec,
            KERNEL_NAME,
            user=user and prefix is None,
            prefix=None if prefix is None else os.fspath(prefix),
        )
    return Path(installed)
