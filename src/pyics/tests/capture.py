import builtins
import io
import sys
import threading
from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager


class _ThreadLocalStdout:
    """`sys.stdout` (or `sys.stderr`) replacement that only redirects the calling thread's
    writes."""

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


def _ensure_installed(stream: str = "stdout") -> _ThreadLocalStdout:
    with _install_lock:
        if not isinstance(getattr(sys, stream), _ThreadLocalStdout):
            setattr(sys, stream, _ThreadLocalStdout(getattr(sys, stream)))
        return getattr(sys, stream)


@contextmanager
def capture_stdout() -> Generator[io.StringIO]:
    """Capture this thread's stdout writes only; other threads are unaffected."""
    with _capture("stdout") as buf:
        yield buf


@contextmanager
def capture_stderr() -> Generator[io.StringIO]:
    """Capture this thread's stderr writes only; other threads are unaffected."""
    with _capture("stderr") as buf:
        yield buf


@contextmanager
def _capture(stream: str) -> Generator[io.StringIO]:
    proxy = _ensure_installed(stream)
    buf = io.StringIO()
    stack = proxy._stack()
    stack.append(buf)
    try:
        yield buf
    finally:
        stack.pop()


class _ThreadLocalInput:
    """`builtins.input` replacement that answers the calling thread's calls from provided
    inputs, if any."""

    def __init__(self, real):
        self._real = real
        self._local = threading.local()

    def _stack(self) -> list[Iterator[str]]:
        return self._local.__dict__.setdefault("stack", [])

    def __call__(self, prompt: object = "") -> str:
        stack = self._stack()
        if not stack:
            return self._real(prompt)

        sys.stdout.write(str(prompt))
        try:
            answer = next(stack[-1])
        except StopIteration:
            raise EOFError("EOF when reading a line, no inputs left") from None
        sys.stdout.write(answer + "\n")  # echoed, as when typed into a console
        return answer


def _ensure_input_installed() -> _ThreadLocalInput:
    with _install_lock:
        if not isinstance(builtins.input, _ThreadLocalInput):
            builtins.input = _ThreadLocalInput(builtins.input)
        return builtins.input


@contextmanager
def provide_input(inputs: Iterable[str]) -> Generator[None]:
    """
    Answer this thread's ``input()`` calls with ``inputs``, in order, writing each to
    stdout after the prompt, like a console shows typed answers. Once they have run out,
    ``input()`` raises ``EOFError``, like at the end of stdin. Other threads are
    unaffected.
    """
    proxy = _ensure_input_installed()
    stack = proxy._stack()
    stack.append(iter(inputs))
    try:
        yield
    finally:
        stack.pop()
