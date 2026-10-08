"""
Plain console input and output for the scripts that don't run in the fullscreen
interface, e.g. ``pull``.
"""

import sys
from collections.abc import Sequence
from typing import NoReturn

from pyics.distribution import dependencies
from pyics.distribution.dependencies import Problem

TRUST_WARNING = (
    "Courses run code on your computer: their tests run your code together with "
    "theirs. Only use courses from people you trust."
)


def fail(message: object) -> NoReturn:
    """Exit with ``message`` as the error."""
    sys.exit(f"error: {message}")


def confirm(question: str, *, default: bool = False) -> bool:
    """Ask a yes/no ``question``. Without an answer (e.g. without a terminal to ask
    in), the ``default``."""
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input(question + suffix).strip().lower()
    except EOFError:
        print()
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def report_problems(title: str, problems: Sequence[Problem]) -> None:
    """Tell which requirements of the course ``title`` aren't met, and how to meet
    them."""
    if problems:
        print(f"{title} needs {dependencies.describe(problems)}.")
        print(dependencies.install_hint(problems))


def install_requirements(
    problems: Sequence[Problem], *, yes: bool = False
) -> list[Problem]:
    """
    Install the requirements of ``problems`` that can be installed, if the user agrees
    (or ``yes``).

    Returns
    -------
    list[Problem]
        The problems that remain.
    """
    requirements = [p.requirement for p in problems if p.installable]
    if not requirements:
        return list(problems)
    command = dependencies.format_command(dependencies.install_command(requirements))
    if not yes and not confirm(f"Install {', '.join(requirements)} with: {command}?"):
        return list(problems)
    if dependencies.install(requirements) != 0:
        print(f"Installing {', '.join(requirements)} failed.")
    return dependencies.check([p.requirement for p in problems if p.installable]) + [
        p for p in problems if not p.installable
    ]
