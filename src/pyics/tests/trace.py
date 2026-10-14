"""
Call traces of the code under test, recorded with ``snoop``: every line that is run,
with the values of the variables it changes, and the calls it makes.
"""

import re
import sys
from collections import deque
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import CodeType

import snoop
from rich.style import Style
from rich.text import Text
from snoop.formatting import DefaultFormatter, Event
from snoop.tracer import FrameInfo
from snoop.utils import ArgDefaultDict

# the most lines a trace keeps: its first and last half, and how many were left out in
# between
MAX_TRACE_LINES = 300

# e.g. ``__builtins__`` and ``len(__builtins__)``
_DUNDER = re.compile(r"\b__\w+__\b")


class _Lines:
    """
    Keeps the first and last of the lines written to it, and counts the ones in between,
    so that e.g. the trace of an endless loop doesn't grow without bounds.
    """

    def __init__(self, max_lines: int):
        self._head_size = max_lines // 2
        self.head: list[str] = []
        self.tail: deque[str] = deque(maxlen=max_lines - self._head_size)
        self.omitted = 0
        self._partial = ""

    def write(self, s: str) -> None:
        *lines, self._partial = (self._partial + s).split("\n")
        for line in lines:
            self._add(line)

    def _add(self, line: str) -> None:
        if len(self.head) < self._head_size:
            self.head.append(line)
            return
        if len(self.tail) == self.tail.maxlen:
            self.omitted += 1
        self.tail.append(line)

    def flush(self) -> None:
        if self._partial:
            self._add(self._partial)
            self._partial = ""


class _FrameInfo(FrameInfo):
    """``FrameInfo`` leaving out dunder variables, e.g. the ``__builtins__`` of a
    module."""

    def get_local_reprs(self, *args, **kwargs) -> Generator[tuple[str, object]]:
        for source, value in super().get_local_reprs(*args, **kwargs):
            if not _DUNDER.search(source):
                yield source, value


class _Formatter(DefaultFormatter):
    """
    ``DefaultFormatter`` naming the files of calls without their directories, and
    showing where a module starts: at its first line, rather than at line 0, which
    modules start on before running their first line.
    """

    def format(self, event: Event) -> str:
        if event.event == "call" and event.line_no == 0:
            event.line_no = 1
            formatted = self.format_lines(event, self.format_start(event))
        else:
            formatted = super().format(event)
        if event.event == "call":
            path = event.code.co_filename
            formatted = formatted.replace(f'File "{path}"', f'File "{Path(path).name}"')
        return formatted


def _code_objects(code: CodeType) -> Iterator[CodeType]:
    """``code`` and the code objects defined in it, e.g. of functions, classes, their
    methods and lambdas."""
    yield code
    for const in code.co_consts:
        if isinstance(const, CodeType):
            yield from _code_objects(const)


class Trace:
    """
    Records the trace of the code it watches (see ``watch``) while it is run (see
    ``run``): of each call into that code, however deeply nested, but not of other
    code, such as libraries or pyics itself. Only the thread calling ``run`` is traced.

    At most ``max_lines`` lines are kept (see ``text``).
    """

    def __init__(self, max_lines: int = MAX_TRACE_LINES):
        self._lines = _Lines(max_lines)
        self._config = snoop.Config(
            out=self._lines.write, columns="", color=True, formatter_class=_Formatter
        )
        # every frame of the watched code is a target, so depth 1 traces all of them
        self._tracer = self._config.snoop(depth=1)
        self._tracer.frame_infos = ArgDefaultDict(_FrameInfo)

    def watch(self, code: CodeType) -> None:
        """Trace ``code`` and all code defined in it, e.g. a module's functions."""
        self._tracer.target_codes.update(_code_objects(code))

    def run[T](self, call: Callable[[], T]) -> T:
        """Return ``call()``, tracing the watched code it runs."""
        with self.active():
            return call()

    @contextmanager
    def active(self) -> Generator[None]:
        """Trace the watched code that is run in this context, by the calling thread
        (see ``run``)."""
        self._config.thread_local.__dict__.setdefault("depth", -1)
        previous = sys.gettrace()
        sys.settrace(self._tracer.trace)
        try:
            yield
        finally:
            sys.settrace(previous)
            self._lines.flush()

    @property
    def ansi(self) -> str:
        """The trace as snoop highlighted it, with ANSI escape codes, one trace line per
        line, so that it can be passed on as text, e.g. to another process (see
        ``ansi_text``). If it is longer than ``max_lines``, the lines in between its
        first and last ones are left out, and marked as such."""
        lines = list(self._lines.head)
        if self._lines.omitted:
            omitted = f"... {self._lines.omitted} lines omitted ..."
            lines.append(Style(dim=True).render(omitted))
        lines += self._lines.tail
        return "\n".join(lines)

    @property
    def text(self) -> Text:
        """The trace, highlighted (see ``ansi``)."""
        return ansi_text(self.ansi)


def ansi_text(ansi: str) -> Text:
    """
    The highlighted text of ``ansi``, text with ANSI escape codes such as a trace (see
    ``Trace.ansi``). Each line is decoded on its own, so that a color left open at the end
    of a line doesn't run on into the next.
    """
    return Text("\n").join(Text.from_ansi(line) for line in ansi.split("\n"))
