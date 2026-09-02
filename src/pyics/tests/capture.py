import io
import sys
import threading
from contextlib import contextmanager
from typing import Generator


class _ThreadLocalStdout:
    """`sys.stdout` replacement that only redirects the calling thread's writes."""

    def __init__(self, real):
        self._real = real
        self._local = threading.local()

    def _stack(self) -> list[io.StringIO]:
        return self._local.__dict__.setdefault("stack", [])

    def write(self, s: str) -> int:
        stack = self._stack()
        return stack[-1].write(s) if stack else self._real.write(s)

    def flush(self) -> None:
        if not self._stack():
            self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


_install_lock = threading.Lock()


def _ensure_installed() -> _ThreadLocalStdout:
    with _install_lock:
        if not isinstance(sys.stdout, _ThreadLocalStdout):
            sys.stdout = _ThreadLocalStdout(sys.stdout)
        return sys.stdout


@contextmanager
def capture_stdout() -> Generator[io.StringIO]:
    """Capture this thread's stdout writes only; other threads are unaffected."""
    proxy = _ensure_installed()
    buf = io.StringIO()
    stack = proxy._stack()
    stack.append(buf)
    try:
        yield buf
    finally:
        stack.pop()
