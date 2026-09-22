import queue
from abc import ABC, abstractmethod
from typing import Any

from rich.console import RenderableType
from rich.text import Text

from pyics.app.cli import CLI, CURSOR_MARKER
from pyics.app.key_listener import KeyListener

_SUBMIT_KEYS = ("\r", "\n")
_BACKSPACE_KEYS = ("\x7f", "\x08")


class PromptCancelled(Exception):
    """Raised when a prompt is cancelled by the user (ctrl-c). Aborts the whole prompt, not
    just the current field."""


class Prompt(ABC):
    """
    Base class for blocking, single-field CLI input widgets rendered into a CLI's main
    content panel.

    A Prompt drives its own render/poll loop on whichever thread calls ``run()`` (blocking
    it until the field is submitted or cancelled), while the CLI's own background render
    thread keeps repainting the screen from the state ``run()`` pushes via
    ``cli.update_main_content``. This is what lets a script's main thread be "blocked by
    input requests" instead of needing its own event loop (see scripts/create_task.py).
    """

    def __init__(self, cli: CLI, label: str) -> None:
        self.cli = cli
        self.label = label

    @abstractmethod
    def render(self) -> RenderableType:
        """
        Build the renderable to show in the main content panel for the current state.
        Embed ``pyics.app.cli.CURSOR_MARKER`` at the point the real terminal cursor should
        track, if this widget wants a visible caret.
        """

    @abstractmethod
    def handle_key(self, key: str) -> bool:
        """
        Handle one key token read from the KeyListener queue (a single decoded character,
        or a resolved multi-byte token such as ``"UP"``/``"LEFT"``).

        Returns
        -------
        bool
            True once the field is complete and ``result()`` is ready to be read.
        """

    @abstractmethod
    def hotkeys(self) -> dict[str, str]:
        """Hotkey hints (``{key_label: action_name}``) to show in the footer while this
        prompt is active."""

    @abstractmethod
    def result(self) -> Any:
        """The finished value, once ``handle_key`` has returned True."""

    def cursor_visible(self) -> bool:
        """Whether the real terminal cursor should actually be shown at the CURSOR_MARKER
        position (as opposed to the marker existing purely to keep a selection scrolled
        into view). True by default; list-style widgets override this to False."""
        return True

    def run(self, q: queue.Queue[str] | None = None) -> Any:
        """
        Block until the field is submitted or cancelled, and return its value.

        Parameters
        ----------
        q: queue.Queue[str] (optional)
            An already-running KeyListener's queue to read from, for reuse inside a larger
            interactive session that already owns one (e.g. the main app, reading keys via
            ``@hey_listen``). Must be drained synchronously from the same call stack that
            would otherwise be polling it -- only one consumer may read a given queue at a
            time. If omitted, a KeyListener is opened and closed for the duration of this
            call, which is the right choice for standalone scripts.
        """
        if q is None:
            with KeyListener() as owned_q:
                return self._run(owned_q)
        return self._run(q)

    def _run(self, q: queue.Queue[str]) -> Any:
        old_hotkeys = self.cli.hotkeys
        old_content = self.cli.main_content
        old_title = self.cli.main_title
        old_cursor_visible = self.cli.cursor_visible

        try:
            self.cli.cursor_visible = self.cursor_visible()
            self.cli.update_hotkeys(self.hotkeys())
            self.cli.update_main_content(self.render(), title=self.label)

            while True:
                try:
                    key = q.get(timeout=0.05)
                except queue.Empty:
                    continue

                if key == "\x03":
                    raise PromptCancelled()

                if self.handle_key(key):
                    return self.result()

                self.cli.update_main_content(self.render(), title=self.label)
        finally:
            # restored directly (not via update_main_content/update_hotkeys) to avoid
            # re-wrapping already-wrapped state
            self.cli.hotkeys = old_hotkeys
            self.cli.main_content = old_content
            self.cli.main_title = old_title
            self.cli.cursor_visible = old_cursor_visible
            self.cli._needs_redraw = True


class TextInput(Prompt):
    """Single- or multi-line text entry, with a real blinking terminal cursor tracking the
    caret inside the main content panel."""

    def __init__(self, cli: CLI, label: str, *, multiline: bool = False, initial: str = ""):
        super().__init__(cli, label)
        self.multiline = multiline
        self.buffer = initial
        self.cursor_index = len(initial)

    def render(self) -> RenderableType:
        text = (
            self.buffer[: self.cursor_index]
            + CURSOR_MARKER
            + self.buffer[self.cursor_index :]
        )
        return Text(text)

    def hotkeys(self) -> dict[str, str]:
        if self.multiline:
            return {"^D": "submit", "ctrl-c": "cancel"}
        return {"enter": "submit", "ctrl-c": "cancel"}

    def handle_key(self, key: str) -> bool:
        if key in _SUBMIT_KEYS:
            if self.multiline:
                self._insert("\n")
                return False
            return True

        if self.multiline and key == "\x04":  # ctrl-d
            return True

        if key in _BACKSPACE_KEYS:
            if self.cursor_index > 0:
                self.buffer = (
                    self.buffer[: self.cursor_index - 1] + self.buffer[self.cursor_index :]
                )
                self.cursor_index -= 1
            return False

        if key == "LEFT":
            self.cursor_index = max(0, self.cursor_index - 1)
            return False

        if key == "RIGHT":
            self.cursor_index = min(len(self.buffer), self.cursor_index + 1)
            return False

        if len(key) == 1 and key.isprintable() and key != CURSOR_MARKER:
            self._insert(key)

        return False

    def _insert(self, s: str) -> None:
        self.buffer = self.buffer[: self.cursor_index] + s + self.buffer[self.cursor_index :]
        self.cursor_index += len(s)

    def result(self) -> str:
        return self.buffer


class SelectInput(Prompt):
    """Select one option from a fixed list, navigated with the up/down arrow keys."""

    def __init__(self, cli: CLI, label: str, options: list[str], *, initial_index: int = 0):
        if not options:
            raise ValueError("SelectInput requires at least one option")
        super().__init__(cli, label)
        self.options = options
        self.index = max(0, min(initial_index, len(options) - 1))

    def cursor_visible(self) -> bool:
        return False

    def render(self) -> RenderableType:
        text = Text()
        for i, option in enumerate(self.options):
            if i:
                text.append("\n")
            if i == self.index:
                text.append(CURSOR_MARKER)
                text.append("❯ ", style="bold blue")
                text.append(option, style="reverse bold blue")
            else:
                text.append("  ")
                text.append(option)
        return text

    def hotkeys(self) -> dict[str, str]:
        return {"↑/↓": "select", "enter": "confirm", "ctrl-c": "cancel"}

    def handle_key(self, key: str) -> bool:
        if key in _SUBMIT_KEYS:
            return True
        if key == "UP":
            self.index = max(0, self.index - 1)
        elif key == "DOWN":
            self.index = min(len(self.options) - 1, self.index + 1)
        return False

    def result(self) -> str:
        return self.options[self.index]


def prompt_text(
    cli: CLI,
    label: str,
    *,
    multiline: bool = False,
    initial: str = "",
    q: queue.Queue[str] | None = None,
) -> str:
    """Block for a single/multi-line text field, rendered into ``cli``'s main content
    panel with a live cursor, and return the submitted text."""
    return TextInput(cli, label, multiline=multiline, initial=initial).run(q)


def prompt_select(
    cli: CLI,
    label: str,
    options: list[str],
    *,
    q: queue.Queue[str] | None = None,
) -> str:
    """Block for the user to pick one of ``options`` (up/down + enter), rendered into
    ``cli``'s main content panel, and return the chosen option."""
    return SelectInput(cli, label, options).run(q)
