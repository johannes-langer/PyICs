"""
The "Python (pyics)" Jupyter kernel: an IPython kernel that runs in pyics's own
interpreter and loads the pyics extension at every start, restarts included, so that a
notebook's tests run whenever its cells run.
"""

import os
import platform
import sys
import tempfile
from pathlib import Path

from pyics.util.paths import StrPath

KERNEL_NAME = "pyics"
DISPLAY_NAME = "Python (pyics)"
# makes IPython load the extension (``InteractiveShellApp.extra_extensions``)
EXTENSION_ARGUMENT = "--ext=pyics"


def install_kernel(
    *, user: bool = True, sys_prefix: bool = False, prefix: StrPath | None = None
) -> Path:
    """
    Install the kernel spec of the "Python (pyics)" kernel (named "pyics"), replacing
    an existing one. It runs this interpreter (``sys.executable``), so the kernel has
    the same pyics, and the same packages, as the TUI.

    Parameters
    ----------
    user: bool (optional, keyword only)
        Install it for the current user (the default), in Jupyter's data directory.
        If False, and neither of the others is given, for all users of the system.
    sys_prefix: bool (optional, keyword only)
        Install it into this environment (``sys.prefix``) instead, where only Jupyter
        installed in it finds it.
    prefix: StrPath | None (optional, keyword only)
        Install it into this prefix instead (``<prefix>/share/jupyter/kernels``).

    Returns
    -------
    Path
        The directory the kernel spec was installed in.

    Raises
    ------
    OSError
        If it can't be installed, e.g. without the rights to install it for all users.
    """
    from ipykernel.kernelspec import write_kernel_spec
    from jupyter_client.kernelspec import KernelSpecManager

    if prefix is None and sys_prefix:
        prefix = sys.prefix
    python_arguments = None
    if platform.python_implementation() == "CPython":
        # as ``python -m ipykernel install`` does, so that the debugger can step into
        # the standard library
        python_arguments = ["-Xfrozen_modules=off"]

    with tempfile.TemporaryDirectory() as temp:
        source = write_kernel_spec(
            Path(temp) / KERNEL_NAME,
            overrides={"display_name": DISPLAY_NAME},
            extra_arguments=[EXTENSION_ARGUMENT],
            python_arguments=python_arguments,
        )
        installed = KernelSpecManager().install_kernel_spec(
            source,
            KERNEL_NAME,
            user=user and prefix is None,
            prefix=None if prefix is None else os.fspath(prefix),
        )
    return Path(installed)


def is_installed() -> bool:
    """Whether Jupyter finds the "Python (pyics)" kernel (see ``install_kernel``), and it
    runs this interpreter with the pyics extension: one installed from another
    environment would run another pyics."""
    from jupyter_client.kernelspec import KernelSpecManager

    try:
        spec = KernelSpecManager().get_kernel_spec(KERNEL_NAME)
    except Exception:  # not found (NoSuchKernel), or unreadable
        return False
    argv = list(spec.argv)
    return (
        bool(argv)
        and _same_path(argv[0], sys.executable)
        and EXTENSION_ARGUMENT in argv
    )


def _same_path(a: str, b: str) -> bool:
    """Whether ``a`` and ``b`` are the same path. Symbolic links aren't resolved: the
    interpreters of two virtual environments are often links to the same one."""
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
