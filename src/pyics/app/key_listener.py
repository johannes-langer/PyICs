import os
import queue
import select
import sys
import termios
import threading
import tty
from collections.abc import Callable

# Map recognized CSI (arrow-key) escape sequences to single-token key names. Terminals
# also emit these for mouse-wheel scrolling while in the alt-screen buffer (xterm
# "alternate scroll mode"), so this doubles as mouse-wheel support.
_CSI_KEYS = {
    "\x1b[A": "UP",
    "\x1b[B": "DOWN",
    "\x1b[C": "RIGHT",
    "\x1b[D": "LEFT",
    "\x1bOA": "UP",  # application cursor-key mode variants
    "\x1bOB": "DOWN",
    "\x1bOC": "RIGHT",
    "\x1bOD": "LEFT",
}


class KeyListener(queue.Queue):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._raw_mode_ready = threading.Event()
        self._stop_event = threading.Event()

    @staticmethod
    def _read_byte(fd: int) -> str:
        """Read a single raw byte directly from the fd, bypassing sys.stdin's internal
        buffering so it can't race ahead of select() (which peeks the same fd)."""
        return os.read(fd, 1).decode(errors="replace")

    def _read_escape_sequence(self, fd: int) -> str:
        """
        Coalesce a multi-byte escape sequence (e.g. an arrow key) following a lone ESC
        byte into a single token, since it would otherwise be split into separate
        single-char reads.

        Parameters
        ----------
        fd: int
            File descriptor to read further bytes from.
        """
        if not select.select([fd], [], [], 0.01)[0]:
            return "\x1b"

        ch = self._read_byte(fd)
        if ch == "O":  # SS3 sequence (application cursor-key mode): exactly one more byte
            if select.select([fd], [], [], 0.01)[0]:
                ch += self._read_byte(fd)
            return _CSI_KEYS.get("\x1b" + ch, "\x1b" + ch)
        if ch != "[":
            return "\x1b" + ch

        seq = "\x1b["
        while select.select([fd], [], [], 0.01)[0]:
            c = self._read_byte(fd)
            seq += c
            if c.isalpha() or c == "~":
                break

        return _CSI_KEYS.get(seq, seq)

    def _key_listener(self) -> None:
        """
        KeyListener main functionality.
        """
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)

        try:
            tty.setraw(fd)

            # Re-enable output processing (OPOST) to fix newline rendering
            mode = termios.tcgetattr(fd)
            mode[1] |= termios.OPOST
            termios.tcsetattr(fd, termios.TCSADRAIN, mode)
        finally:
            # Unblock __enter__ only once raw mode is actually in effect (or setup
            # failed), so nothing else can race the terminal into a stale mode.
            self._raw_mode_ready.set()

        try:
            while not self._stop_event.is_set():
                if not select.select([fd], [], [], 0.05)[0]:
                    continue
                ch = self._read_byte(fd)
                if ch == "\x1b":
                    self.put(self._read_escape_sequence(fd))
                    continue
                self.put(ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def stop(self) -> None:
        """
        Signal the reader thread to stop and block until it has actually exited (and cooked
        terminal mode has been restored), so callers can rely on that being true the instant
        this returns.
        """
        self._stop_event.set()
        if hasattr(self, "thread"):
            self.thread.join(timeout=1.0)

    def __enter__(self):
        self.thread = threading.Thread(target=self._key_listener, daemon=True)
        self.thread.start()
        self._raw_mode_ready.wait()
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
