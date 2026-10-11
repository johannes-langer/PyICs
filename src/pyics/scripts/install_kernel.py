from pyics.notebook import kernelspec
from pyics.scripts.console import fail


def _install_kernel(user: bool = True, sys_prefix: bool = False) -> None:
    """
    Console script to install the "Python (pyics)" Jupyter kernel, which runs pyics's
    interpreter and runs a notebook's tests in pyics whenever one of its cells runs.

    Parameters
    ----------
    user: bool
        Install it for the current user, in Jupyter's data directory (the default).
    sys_prefix: bool
        Install it into the current environment instead, where only a Jupyter
        installed in the same environment finds it.
    """
    try:
        installed = kernelspec.install_kernel(user=user, sys_prefix=sys_prefix)
    except OSError as err:
        fail(f"Could not install the kernel: {err}")
    print(f'Installed the "{kernelspec.DISPLAY_NAME}" kernel in {installed}.')
    print(
        f'Choose "{kernelspec.DISPLAY_NAME}" as the kernel of your exercise notebooks, '
        "to see your tests' results in pyics whenever you run a cell."
    )
