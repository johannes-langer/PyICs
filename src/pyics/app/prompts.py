import os
import queue
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, TypeVar

from rich.columns import Columns
from rich.console import Group, RenderableType
from rich.syntax import Syntax
from rich.text import Text

from pyics.app.cli import CLI, CURSOR_MARKER
from pyics.app.key_listener import KeyListener

_SUBMIT_KEYS = ("\r", "\n")
_BACKSPACE_KEYS = ("\x7f", "\x08")
_BACK_KEY = "\x1b"  # a lone esc; KeyListener delivers escape sequences as other tokens
_TAB_KEY = "\t"
_MAX_SHOWN_CANDIDATES = 50

T = TypeVar("T")


class PromptCancelled(Exception):
    """Raised when a prompt is cancelled by the user (ctrl-c). Aborts the whole prompt, not
    just the current field."""


class PromptBack(Exception):
    """Raised when the user asks to go back to the previous prompt (esc). Only raised by
    prompts run with ``allow_back=True``; see ``PromptSession``."""


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

    def run(self, q: queue.Queue[str] | None = None, *, allow_back: bool = False) -> Any:
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
        allow_back: bool (optional)
            If True, esc raises ``PromptBack`` (and is shown as a hotkey). The caller must
            handle it; ``PromptSession`` does.
        """
        if q is None:
            with KeyListener() as owned_q:
                return self._run(owned_q, allow_back)
        return self._run(q, allow_back)

    def _run(self, q: queue.Queue[str], allow_back: bool) -> Any:
        old_hotkeys = self.cli.hotkeys
        old_content = self.cli.main_content
        old_title = self.cli.main_title
        old_cursor_visible = self.cli.cursor_visible

        try:
            self.cli.cursor_visible = self.cursor_visible()
            hotkeys = self.hotkeys()
            if allow_back:
                hotkeys = {"esc": "back", **hotkeys}
            self.cli.update_hotkeys(hotkeys)
            self.cli.update_main_content(self.render(), title=self.label)

            while True:
                try:
                    key = q.get(timeout=0.05)
                except queue.Empty:
                    continue

                if key == "\x03":
                    raise PromptCancelled()

                if allow_back and key == _BACK_KEY:
                    raise PromptBack()

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
            return {"ctrl-d": "submit", "ctrl-c": "cancel"}
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
        self.buffer = (
            self.buffer[: self.cursor_index] + s + self.buffer[self.cursor_index :]
        )
        self.cursor_index += len(s)

    def result(self) -> str:
        return self.buffer


class CodeInput(TextInput):
    """``TextInput`` with live Python syntax highlighting."""

    def render(self) -> RenderableType:
        code = (
            self.buffer[: self.cursor_index]
            + CURSOR_MARKER
            + self.buffer[self.cursor_index :]
        )
        return Syntax(code, "python", background_color="default", line_numbers=False)


def _complete_path(text: str) -> tuple[str, list[str]]:
    """
    Bash-style tab completion of a (partial) file path.

    Only the last path component is completed; everything before it is kept exactly as
    typed (``~`` is expanded for the lookup, but not in the returned text). Dotfiles are
    only offered if the partial name itself starts with ``.``.

    Returns
    -------
    tuple[str, list[str]]
        The completed text, and the candidates to show if the completion is ambiguous
        (directories suffixed with ``/``), or an empty list otherwise.
    """
    if text == "~":
        return "~/", []

    head, sep, prefix = text.rpartition("/")
    dir_text = head + sep

    try:
        with os.scandir(os.path.expanduser(dir_text or ".")) as it:
            matches = sorted(
                (entry.name, entry.is_dir())
                for entry in it
                if entry.name.startswith(prefix)
                and (prefix.startswith(".") or not entry.name.startswith("."))
            )
    except OSError:
        return text, []

    if not matches:
        return text, []

    if len(matches) == 1:
        name, is_dir = matches[0]
        return dir_text + name + ("/" if is_dir else ""), []

    common = os.path.commonprefix([name for name, _ in matches])
    return dir_text + common, [name + ("/" if is_dir else "") for name, is_dir in matches]


class PathInput(TextInput):
    """Single-line ``TextInput`` for file paths, with bash-style tab completion: tab
    extends the path to the longest common prefix of the matching entries, and lists them
    below the input if there are several."""

    def __init__(self, cli: CLI, label: str, *, initial: str = ""):
        super().__init__(cli, label, initial=initial)
        self.candidates: list[str] = []

    def render(self) -> RenderableType:
        text = super().render()
        if not self.candidates:
            return text
        columns = Columns(
            [Text(c, style="dim") for c in self.candidates[:_MAX_SHOWN_CANDIDATES]]
        )
        hidden = len(self.candidates) - _MAX_SHOWN_CANDIDATES
        if hidden > 0:
            return Group(text, Text(""), columns, Text(f"… and {hidden} more", style="dim"))
        return Group(text, Text(""), columns)

    def hotkeys(self) -> dict[str, str]:
        return {"tab": "complete", **super().hotkeys()}

    def handle_key(self, key: str) -> bool:
        if key == _TAB_KEY:
            completed, self.candidates = _complete_path(self.buffer[: self.cursor_index])
            self.buffer = completed + self.buffer[self.cursor_index :]
            self.cursor_index = len(completed)
            return False

        self.candidates = []
        return super().handle_key(key)


class SelectInput(Prompt):
    """Select one option from a fixed list, navigated with the up/down arrow keys.

    If ``option_hints`` is given (same order as ``options``), the hint for the currently
    selected option is shown dimmed below it, separated from its neighbours by blank lines.
    A falsy hint (``None``/``""``) shows nothing for that option.

    If ``header`` is given, it is rendered above the option list.
    """

    def __init__(
        self,
        cli: CLI,
        label: str,
        options: list[str],
        *,
        initial_index: int = 0,
        option_hints: list[str | None] | None = None,
        header: RenderableType | None = None,
    ):
        if not options:
            raise ValueError("SelectInput requires at least one option")
        if option_hints is not None and len(option_hints) != len(options):
            raise ValueError("option_hints must have the same length as options")
        super().__init__(cli, label)
        self.options = options
        self.option_hints = option_hints
        self.header = header
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
                hint = self.option_hints[i] if self.option_hints else None
                if hint:
                    text.append("\n\n")
                    text.append(
                        "\n".join("  " + line for line in hint.splitlines()), style="dim"
                    )
                    if i < len(self.options) - 1:
                        text.append("\n")
            else:
                text.append("  ")
                text.append(option)
        if self.header is not None:
            return Group(self.header, Text(""), text)
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


def prompt_code(
    cli: CLI,
    label: str,
    *,
    multiline: bool = False,
    initial: str = "",
    q: queue.Queue[str] | None = None,
) -> str:
    """Like ``prompt_text``, but with live Python syntax highlighting."""
    return CodeInput(cli, label, multiline=multiline, initial=initial).run(q)


def prompt_path(
    cli: CLI,
    label: str,
    *,
    initial: str = "",
    q: queue.Queue[str] | None = None,
) -> str:
    """Like ``prompt_text`` (single-line), but with tab completion of file paths."""
    return PathInput(cli, label, initial=initial).run(q)


def prompt_select(
    cli: CLI,
    label: str,
    options: list[str],
    *,
    option_hints: list[str | None] | None = None,
    header: RenderableType | None = None,
    q: queue.Queue[str] | None = None,
) -> str:
    """Block for the user to pick one of ``options`` (up/down + enter), rendered into
    ``cli``'s main content panel, and return the chosen option. ``option_hints``, if given,
    are shown dimmed below the currently selected option; ``header``, if given, is shown
    above the options."""
    return SelectInput(
        cli, label, options, option_hints=option_hints, header=header
    ).run(q)


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

    def __init__(self, cli: CLI, q: queue.Queue[str]) -> None:
        self.cli = cli
        self.q = q
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

    def _ask(
        self,
        make_prompt: Callable[[str | None], Prompt],
        is_valid: Callable[[str], bool] = lambda _: True,
    ) -> str:
        if self._position < len(self._answers):
            answer = self._answers[self._position]
            if is_valid(answer):
                self._position += 1
                return answer
            # the flow no longer offers this answer (e.g. a loaded file changed), so the
            # recorded answers from here on can't be trusted
            del self._answers[self._position :]
            self._prefill = None

        prefill, self._prefill = self._prefill, None
        answer = make_prompt(prefill).run(self.q, allow_back=self._position > 0)
        self._answers.append(answer)
        self._position += 1
        return answer

    def text(self, label: str, *, multiline: bool = False, initial: str = "") -> str:
        """Session version of ``prompt_text``."""
        return self._ask(
            lambda prefill: TextInput(
                self.cli,
                label,
                multiline=multiline,
                initial=initial if prefill is None else prefill,
            )
        )

    def code(self, label: str, *, multiline: bool = False, initial: str = "") -> str:
        """Session version of ``prompt_code``."""
        return self._ask(
            lambda prefill: CodeInput(
                self.cli,
                label,
                multiline=multiline,
                initial=initial if prefill is None else prefill,
            )
        )

    def path(self, label: str, *, initial: str = "") -> str:
        """Session version of ``prompt_path``."""
        return self._ask(
            lambda prefill: PathInput(
                self.cli, label, initial=initial if prefill is None else prefill
            )
        )

    def select(
        self,
        label: str,
        options: list[str],
        *,
        option_hints: list[str | None] | None = None,
        header: RenderableType | None = None,
    ) -> str:
        """Session version of ``prompt_select``."""
        return self._ask(
            lambda prefill: SelectInput(
                self.cli,
                label,
                options,
                initial_index=options.index(prefill) if prefill in options else 0,
                option_hints=option_hints,
                header=header,
            ),
            is_valid=lambda answer: answer in options,
        )
