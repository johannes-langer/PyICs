import glob
import os
import queue
import textwrap
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, TypeVar

from rich.columns import Columns
from rich.console import Group, RenderableType
from rich.markup import escape
from rich.syntax import Syntax
from rich.text import Text

from pyics.app.cli import CLI, CURSOR_MARKER

_SUBMIT_KEYS = ("\r", "\n")
_BACKSPACE_KEYS = ("\x7f", "\x08")
_CANCEL_KEY = "\x03"
_BACK_KEY = "\x1b"  # a lone esc; KeyListener delivers escape sequences as other tokens
_SCROLL_KEYS = {"UP": -1, "DOWN": 1}

T = TypeVar("T")


class PromptCancelled(Exception):
    """Raised when the user cancels a prompt (ctrl-c). Aborts the whole flow, not just the
    current field."""


class PromptBack(Exception):
    """Raised when the user asks to go back to the previous prompt (esc). Only raised by
    prompts run with ``allow_back=True``; see ``PromptSession``."""


class Prompt(ABC):
    """
    Base class for blocking, single-field input widgets rendered into a CLI's main content
    panel.

    ``run()`` blocks the calling thread until the field is submitted, while the CLI's own
    render thread keeps repainting the content ``run()`` pushes. This lets a script's main
    thread simply be "blocked by input requests" (see scripts/create_task.py).
    """

    hotkeys: dict[str, str]
    # whether the terminal cursor is shown at CURSOR_MARKER, rather than the marker only
    # keeping the viewport scrolled to it
    show_cursor = True
    # whether up/down (and the mouse wheel) scroll the main content, if the widget itself
    # doesn't use them
    scroll_with_arrows = True

    def __init__(self, label: str, *, details: RenderableType | None = None) -> None:
        self.label = label
        self.details = details

    @abstractmethod
    def render_body(self) -> RenderableType:
        """The widget in its current state. Embed ``CURSOR_MARKER`` where the terminal
        cursor should be."""

    @abstractmethod
    def handle_key(self, key: str) -> bool:
        """Handle one key token from the KeyListener. Returns True once submitted."""

    @abstractmethod
    def answer(self) -> str:
        """The current answer."""

    @abstractmethod
    def set_answer(self, answer: str) -> None:
        """Prefill the widget with an answer."""

    def parse(self, answer: str) -> Any:
        """Convert an answer to this prompt's value. Raises if the answer is invalid."""
        return answer

    def render(self) -> RenderableType:
        # details go below the widget, so long ones can't push the input (and the
        # label in the panel title) out of the cursor-following viewport
        if self.details is None:
            return self.render_body()
        return Group(self.render_body(), Text(""), self.details)

    def run(self, cli: CLI, q: queue.Queue[str], *, allow_back: bool = False) -> str:
        """
        Block until the field is submitted and return the answer. The CLI's previous main
        content, hotkeys and cursor state are restored afterwards.

        Parameters
        ----------
        cli: CLI
            CLI to render into.
        q: queue.Queue[str]
            A running KeyListener's queue. Nothing else may read from it while this runs.
        allow_back: bool (optional)
            If True, esc raises ``PromptBack``. ctrl-c always raises ``PromptCancelled``.
        """
        old_state = (cli.hotkeys, cli.main_content, cli.main_title, cli.cursor_visible)
        try:
            cli.cursor_visible = self.show_cursor
            cli.scroll_offset = 0
            back = {"esc": "back"} if allow_back else {}
            scroll = {"↑/↓": "scroll"} if self.scroll_with_arrows else {}
            cli.update_hotkeys({**back, **scroll, **self.hotkeys, "ctrl-c": "cancel"})
            cli.update_main_content(self.render(), title=self.label)
            while True:
                key = q.get()
                if key == _CANCEL_KEY:
                    raise PromptCancelled()
                if allow_back and key == _BACK_KEY:
                    raise PromptBack()
                if self.scroll_with_arrows and key in _SCROLL_KEYS:
                    # not re-rendered, so the viewport doesn't jump back to the cursor
                    cli.scroll(_SCROLL_KEYS[key])
                    continue
                if self.handle_key(key):
                    return self.answer()
                cli.update_main_content(self.render(), title=self.label)
        finally:
            # restored directly, since update_main_content would re-wrap the old content
            cli.hotkeys, cli.main_content, cli.main_title, cli.cursor_visible = old_state
            cli._needs_redraw = True


class TextInput(Prompt):
    """Single- or multi-line text entry, with the terminal cursor tracking the caret."""

    def __init__(
        self,
        label: str,
        *,
        multiline: bool = False,
        initial: str = "",
        details: RenderableType | None = None,
    ) -> None:
        super().__init__(label, details=details)
        self.multiline = multiline
        self.hotkeys = {"ctrl-d" if multiline else "enter": "submit"}
        self.set_answer(initial)

    def answer(self) -> str:
        return self.buffer

    def set_answer(self, answer: str) -> None:
        self.buffer = answer
        self.cursor_index = len(answer)

    def _format(self, text: str) -> RenderableType:
        return Text(text)

    def render_body(self) -> RenderableType:
        i = self.cursor_index
        return self._format(self.buffer[:i] + CURSOR_MARKER + self.buffer[i:])

    def handle_key(self, key: str) -> bool:
        i = self.cursor_index
        if key in _SUBMIT_KEYS and self.multiline:
            self._insert("\n")
        elif key in _SUBMIT_KEYS or (self.multiline and key == "\x04"):  # ctrl-d
            return True
        elif key in _BACKSPACE_KEYS:
            if i > 0:
                self.buffer = self.buffer[: i - 1] + self.buffer[i:]
                self.cursor_index -= 1
        elif key == "LEFT":
            self.cursor_index = max(0, i - 1)
        elif key == "RIGHT":
            self.cursor_index = min(len(self.buffer), i + 1)
        elif len(key) == 1 and key.isprintable() and key != CURSOR_MARKER:
            self._insert(key)
        return False

    def _insert(self, s: str) -> None:
        i = self.cursor_index
        self.buffer = self.buffer[:i] + s + self.buffer[i:]
        self.cursor_index += len(s)


class CodeInput(TextInput):
    """``TextInput`` with live Python syntax highlighting."""

    def _format(self, text: str) -> RenderableType:
        return Syntax(text, "python", background_color="default")


def _common_prefix(strings: list[str]) -> str:
    """Longest string that all ``strings`` start with. Compared character by character,
    unlike ``os.path.commonpath``, which only matches whole path components."""
    low, high = min(strings), max(strings)
    for i, char in enumerate(low):
        if char != high[i]:
            return low[:i]
    return low


def _complete_path(text: str) -> tuple[str, list[str]]:
    """
    Bash-style tab completion of a (partial) file path. ``~`` is expanded, and dotfiles are
    only matched if the partial name starts with ``.``.

    Returns
    -------
    tuple[str, list[str]]
        The path extended to the longest common prefix of its matches, and the matching
        names to show if there are several (directories suffixed with ``/``). Matches are
        separated by ``/``, which Windows accepts as well.
    """
    text = os.path.expanduser(text)
    matches = [
        m.replace(os.sep, "/") + ("/" if os.path.isdir(m) else "")
        for m in sorted(glob.glob(glob.escape(text) + "*"))
    ]
    if len(matches) <= 1:
        return (matches[0] if matches else text), []
    head = text[: max(text.rfind("/"), text.rfind(os.sep)) + 1]
    return _common_prefix(matches), [m[len(head) :] for m in matches]


class PathInput(TextInput):
    """Single-line ``TextInput`` for file paths, with bash-style tab completion. If the
    completion is ambiguous, the candidates are listed below the input."""

    def __init__(
        self, label: str, *, initial: str = "", details: RenderableType | None = None
    ) -> None:
        super().__init__(label, initial=initial, details=details)
        self.hotkeys = {"tab": "complete", **self.hotkeys}
        self.candidates: list[str] = []

    def render_body(self) -> RenderableType:
        text = super().render_body()
        if not self.candidates:
            return text
        return Group(text, Text(""), Columns(Text(c, style="dim") for c in self.candidates))

    def handle_key(self, key: str) -> bool:
        if key == "\t":
            self.buffer, self.candidates = _complete_path(self.buffer)
            self.cursor_index = len(self.buffer)
            return False
        self.candidates = []
        return super().handle_key(key)


class SelectInput(Prompt):
    """Select one of ``options`` with the up/down arrow keys. If ``hints`` has a hint for
    the selected option, it is shown dimmed below it, separated by blank lines."""

    show_cursor = False
    scroll_with_arrows = False  # up/down move the selection, which the viewport follows

    def __init__(
        self,
        label: str,
        options: list[str],
        *,
        hints: dict[str, str] | None = None,
        details: RenderableType | None = None,
    ) -> None:
        if not options:
            raise ValueError("SelectInput requires at least one option")
        super().__init__(label, details=details)
        self.hotkeys = {"↑/↓": "select", "enter": "confirm"}
        self.options = options
        self.hints = hints or {}
        self.index = 0

    def answer(self) -> str:
        return self.options[self.index]

    def set_answer(self, answer: str) -> None:
        if answer in self.options:
            self.index = self.options.index(answer)

    def parse(self, answer: str) -> str:
        if answer not in self.options:
            raise ValueError(f"{answer!r} is not one of the options")
        return answer

    def render_body(self) -> RenderableType:
        text = Text()
        for i, option in enumerate(self.options):
            if i:
                text.append("\n")
            if i != self.index:
                text.append("  " + option)
                continue
            text.append(CURSOR_MARKER + "❯ ", style="bold blue")
            text.append(option, style="reverse bold blue")
            if hint := self.hints.get(option):
                text.append("\n\n" + textwrap.indent(hint, "  "), style="dim")
                if i < len(self.options) - 1:
                    text.append("\n")
        return text

    def handle_key(self, key: str) -> bool:
        if key == "UP":
            self.index = max(0, self.index - 1)
        elif key == "DOWN":
            self.index = min(len(self.options) - 1, self.index + 1)
        return key in _SUBMIT_KEYS


class ReorderInput(Prompt):
    """Reorder ``options`` with the arrow keys: space picks up the selected option, up/down
    then move it, and space drops it again. Answers are the new order, as comma-separated
    indices into ``options``; ``parse`` returns the reordered options."""

    show_cursor = False
    scroll_with_arrows = False  # up/down move the selection, which the viewport follows

    def __init__(
        self, label: str, options: list[str], *, details: RenderableType | None = None
    ) -> None:
        if not options:
            raise ValueError("ReorderInput requires at least one option")
        super().__init__(label, details=details)
        self.hotkeys = {
            "↑/↓": "select/move",
            "space": "pick up/drop",
            "enter": "confirm",
        }
        self.options = options
        self.order = list(range(len(options)))
        self.index = 0
        self.grabbed = False

    def _indices(self, answer: str) -> list[int]:
        order = [int(i) for i in answer.split(",")]
        if sorted(order) != list(range(len(self.options))):
            raise ValueError(f"{answer!r} is not an order of the options")
        return order

    def answer(self) -> str:
        return ",".join(map(str, self.order))

    def set_answer(self, answer: str) -> None:
        try:
            self.order = self._indices(answer)
        except ValueError:
            pass

    def parse(self, answer: str) -> list[str]:
        return [self.options[i] for i in self._indices(answer)]

    def render_body(self) -> RenderableType:
        text = Text()
        for i, option in enumerate(self.options[j] for j in self.order):
            if i:
                text.append("\n")
            if i != self.index:
                text.append("  " + option)
            elif self.grabbed:
                text.append(CURSOR_MARKER + "↕ ", style="bold yellow")
                text.append(option, style="reverse bold yellow")
            else:
                text.append(CURSOR_MARKER + "❯ ", style="bold blue")
                text.append(option, style="reverse bold blue")
        return text

    def handle_key(self, key: str) -> bool:
        if key in ("UP", "DOWN"):
            i = self.index
            j = max(0, min(len(self.options) - 1, i + (1 if key == "DOWN" else -1)))
            if self.grabbed:
                self.order[i], self.order[j] = self.order[j], self.order[i]
            self.index = j
        elif key == " ":
            self.grabbed = not self.grabbed
        return key in _SUBMIT_KEYS


class PromptSession:
    """
    Runs a multi-prompt flow that the user can step back through, one prompt at a time.

    Every answer is recorded. Going back (esc) discards the last answer and replays the
    flow from the start: recorded answers are returned without showing their prompts, and
    the first unanswered prompt is shown live, prefilled with the discarded answer. This
    keeps flows plain imperative code (loops, branches, nested helpers), at the cost of one
    requirement: a flow must ask the same prompts given the same answers. Side effects in a
    flow are re-run on every replay.
    """

    def __init__(self, cli: CLI, q: queue.Queue[str], *, info: str = "") -> None:
        self.cli = cli
        self.q = q
        self.info = info
        self._answers: list[str] = []
        self._position = 0
        self._prefill: str | None = None

    def run(self, flow: Callable[[PromptSession], T]) -> T:
        """Run ``flow(self)`` to completion, replaying it whenever the user goes back."""
        while True:
            self._position = 0
            try:
                return flow(self)
            except PromptBack:
                self._prefill = self._answers.pop()
                self.cli.update_info(self.info)

    def ask(self, prompt: Prompt, parse: Callable[[Any], T] = lambda value: value) -> T:
        """
        Ask ``prompt`` and return its answer, converted by ``prompt.parse`` and then
        ``parse``. If either raises, the error is shown in the info bar and the prompt is
        asked again, keeping the rejected input.
        """
        if self._position < len(self._answers):
            try:
                value = parse(prompt.parse(self._answers[self._position]))
            except Exception:  # noqa: BLE001
                # the recorded answer is no longer valid (e.g. a loaded file changed), so
                # neither are the ones after it
                del self._answers[self._position :]
                self._prefill = None
            else:
                self._position += 1
                return value

        if self._prefill is not None:
            prompt.set_answer(self._prefill)
            self._prefill = None
        while True:
            answer = prompt.run(self.cli, self.q, allow_back=self._position > 0)
            try:
                value = parse(prompt.parse(answer))
                break
            except Exception as err:  # noqa: BLE001
                self.cli.update_info(
                    f"[red]{type(err).__name__}:[/red] {escape(str(err))}"
                )

        self.cli.update_info(self.info)
        self._answers.append(answer)
        self._position += 1
        return value
