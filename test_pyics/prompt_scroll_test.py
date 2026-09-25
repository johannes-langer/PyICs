"""
Scrolling the main content while a prompt is active.
"""

import queue

import pytest
from rich.text import Text

from pyics.app.cli import CLI
from pyics.app.prompts import Prompt, PromptCancelled, SelectInput, TextInput


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str]) -> CLI:
    cli = CLI()
    cli.console.size = (80, 30)  # leaves a viewport of ~15 lines for the main content
    return cli


def _run(cli: CLI, prompt: Prompt, keys: list[str]) -> list[int]:
    """Feed ``keys`` to ``prompt`` (then cancel), drawing a frame before each key is
    read. Returns the scroll offset of every frame."""
    offsets: list[int] = []

    class DrawingQueue(queue.Queue[str]):
        def get(self, *args, **kwargs) -> str:
            cli.draw_interface()
            offsets.append(cli.scroll_offset)
            return super().get(*args, **kwargs)

    q = DrawingQueue()
    for key in [*keys, "\x03"]:
        q.put(key)
    with pytest.raises(PromptCancelled):
        prompt.run(cli, q)
    return offsets


def test_scrolling_sticks_and_typing_returns_to_top(cli: CLI) -> None:
    details = Text("\n".join(f"line {i}" for i in range(50)))
    offsets = _run(cli, TextInput("param", details=details), ["DOWN"] * 5 + ["x"])
    assert offsets == [0, 1, 2, 3, 4, 5, 0]


def test_select_input_arrows_move_selection(cli: CLI) -> None:
    prompt = SelectInput("choose", [str(i) for i in range(3)])
    _run(cli, prompt, ["DOWN", "DOWN"])
    assert prompt.index == 2
