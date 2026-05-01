import queue
import sys
import termios
import threading
import tty
from typing import Callable


class KeyListener(queue.Queue):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

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

            while True:
                ch = sys.stdin.read(1)
                if ch == "\x03":  # ctrl-c
                    self.put(ch)
                    break
                self.put(ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def __enter__(self):
        self.thread = threading.Thread(target=self._key_listener, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        pass

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
