import queue
import sys
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack

from prompt_toolkit.input import Input, create_input
from prompt_toolkit.key_binding import KeyPress
from prompt_toolkit.keys import Keys

# Tokens of keys that don't type a character. Terminals also send the arrow keys (or the
# scroll keys) for mouse-wheel scrolling while in the alt-screen buffer (xterm "alternate
# scroll mode"), so this doubles as mouse-wheel support.
_KEY_TOKENS: dict[str, str] = {
    Keys.Up: "UP",
    Keys.Down: "DOWN",
    Keys.Left: "LEFT",
    Keys.Right: "RIGHT",
    Keys.ScrollUp: "UP",
    Keys.ScrollDown: "DOWN",
    Keys.Escape: "\x1b",
}


def _terminal_input() -> Input:
    """The input of the terminal that stdin is, for the platform (see
    ``prompt_toolkit.input``). Raises if stdin isn't a terminal."""
    if sys.stdin is None or not sys.stdin.isatty():
        raise OSError("stdin is not a terminal")
    return create_input()


def _tokens(key_press: KeyPress) -> list[str]:
    """
    The tokens ``key_press`` is queued as: one of ``_KEY_TOKENS``, or else the text the
    terminal sent for it, e.g. ``"a"``, or the control character of enter or ctrl-c.
    Other keys that don't type a character (e.g. delete) are sent as escape sequences,
    which are queued as single tokens of several characters.
    """
    if key_press.key == Keys.BracketedPaste:
        # e.g. pasted text, as Windows detects it: typed character by character
        return list(key_press.data)
    token = _KEY_TOKENS.get(key_press.key, key_press.data)
    return [token] if token else []


class KeyListener(queue.Queue):
    """
    Queues the keys pressed while in this context as tokens (see ``_tokens``). The
    terminal is in raw mode in the meantime: keys aren't echoed, and ctrl-c is a key
    rather than a signal.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._raw_mode_ready = threading.Event()
        self._stop_event = threading.Event()
        # why keys can't be read, raised by __enter__
        self._error: Exception | None = None
        self.thread: threading.Thread | None = None

    def _key_listener(self) -> None:
        """
        KeyListener main functionality.
        """
        with ExitStack() as stack:
            try:
                keyboard = _terminal_input()
                stack.enter_context(keyboard.raw_mode())
            except Exception as err:  # noqa: BLE001 - raised by __enter__
                self._error = err
                return
            finally:
                # Unblock __enter__ only once raw mode is actually in effect (or setup
                # failed), so nothing else can race the terminal into a stale mode.
                self._raw_mode_ready.set()

            while not self._stop_event.is_set():
                key_presses = keyboard.read_keys()
                if not key_presses:
                    time.sleep(0.01)
                    # a lone esc can't be told from the start of an escape sequence
                    # (e.g. an arrow key) until nothing follows it
                    key_presses = keyboard.read_keys() or keyboard.flush_keys()
                for key_press in key_presses:
                    for token in _tokens(key_press):
                        self.put(token)

    def stop(self) -> None:
        """
        Signal the reader thread to stop and block until it has actually exited (and cooked
        terminal mode has been restored), so callers can rely on that being true the instant
        this returns.
        """
        self._stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=1.0)

    def __enter__(self):
        self.thread = threading.Thread(target=self._key_listener, daemon=True)
        self.thread.start()
        self._raw_mode_ready.wait()
        if self._error is not None:
            raise self._error
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()

    @staticmethod
    def hey_listen(q_param: str) -> Callable:
        """
        KeyListener nested decorator. I.e., this static method decorates the decorator
        (so that we can use the ``q_param`` parameter)

        Parameters
        ----------
        q_param: str
            The parameter name, under which the queue is passed to the decorated function.
        """

        def dec(func: Callable) -> Callable:
            def mod(*args, **kwargs):
                with KeyListener() as q:
                    func(*args, **kwargs, **{q_param: q})

            return mod

        return dec


hey_listen = KeyListener.hey_listen
