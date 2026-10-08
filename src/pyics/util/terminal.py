"""
Terminal handling that works the same on all platforms: the escape codes the interface is
drawn with, the alternate screen it is drawn on, and the terminal's mode.
"""

import sys
from collections.abc import Iterator
from contextlib import contextmanager

from prompt_toolkit.input import create_input

LINE_UP = "\033[1A"
LINE_CLEAR = "\x1b[2K"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
ALT_SCREEN_ENTER = "\033[?1049h"
ALT_SCREEN_LEAVE = "\033[?1049l"


@contextmanager
def fullscreen() -> Iterator[None]:
    """
    Draw on the alternate screen while in this context. The cursor is shown again when
    it is left.

    On Windows, the console only processes escape codes once that is switched on, which
    is done first. ``rich`` checks for it when a ``Console`` is created, so the ``CLI``
    has to be created inside this context.
    """
    if sys.platform == "win32":
        import colorama

        colorama.just_fix_windows_console()

    sys.stdout.write(ALT_SCREEN_ENTER)
    sys.stdout.flush()
    try:
        yield
    finally:
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.write(ALT_SCREEN_LEAVE)
        sys.stdout.flush()


@contextmanager
def preserve_terminal_mode() -> Iterator[None]:
    """
    Restore the terminal's mode when this context is left, e.g. around code that may
    change it. In the meantime, the terminal is in raw mode, as it is while keys are read
    (see ``KeyListener``). Does nothing if stdin isn't a terminal.
    """
    if sys.stdin is None or not sys.stdin.isatty():
        yield
        return

    with create_input().raw_mode():
        yield
