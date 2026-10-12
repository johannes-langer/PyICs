"""
``python -m pyics_jupyter install [--user | --sys-prefix | --prefix DIR]``: install the
"Python (pyics)" Jupyter kernel (see ``kernelspec``), for the current user by default.
"""

import argparse
import sys
from collections.abc import Sequence

from pyics_jupyter import kernelspec

NEXT_STEPS = (
    f'Next, choose "{kernelspec.DISPLAY_NAME}" as the kernel of your exercise '
    "notebooks (Kernel > Change Kernel in JupyterLab, Select Kernel in VS Code) and "
    "run pyics next to them: it shows your tests' results whenever you run a cell. "
    "Save the notebook to count your progress."
)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line ``argv`` (by default ``sys.argv``). Returns the exit
    code."""
    parser = argparse.ArgumentParser(
        prog="python -m pyics_jupyter", description="Live results for pyics."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser(
        "install", help=f'install the "{kernelspec.DISPLAY_NAME}" Jupyter kernel'
    )
    where = install.add_mutually_exclusive_group()
    where.add_argument(
        "--user", action="store_true", help="for the current user (the default)"
    )
    where.add_argument(
        "--sys-prefix",
        action="store_true",
        help="into this environment, for a Jupyter installed in it",
    )
    where.add_argument("--prefix", metavar="DIR", help="into the prefix DIR")
    args = parser.parse_args(argv)

    try:
        installed = kernelspec.install(
            prefix=sys.prefix if args.sys_prefix else args.prefix
        )
    except OSError as err:
        print(f"Could not install the kernel: {err}", file=sys.stderr)
        return 1
    print(f'Installed the "{kernelspec.DISPLAY_NAME}" kernel in {installed}.')
    print(NEXT_STEPS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
