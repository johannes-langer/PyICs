"""
Rendering of the CLI's header, and redrawing the interface.
"""

import pytest
from rich.markup import escape
from rich.text import Text

from pyics.app.cli import CLI


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str]) -> CLI:
    cli = CLI()
    cli.console.size = (100, 30)
    return cli


def _header_text(cli: CLI) -> str:
    with cli.console.capture() as capture:
        cli.console.print(cli.generate_header())
    return capture.get()


def test_info_markup_is_rendered(cli: CLI) -> None:
    cli.update_info("[red]TypeError:[/red] boom")
    text = _header_text(cli)
    assert "TypeError: boom" in text
    assert "[red]" not in text


def test_escaped_info_is_shown_verbatim(cli: CLI) -> None:
    cli.update_info(escape("list[int] at /tmp/a:b:c"))
    assert "list[int] at /tmp/a:b:c" in _header_text(cli)


def test_changes_made_while_drawing_are_drawn_next(
    cli: CLI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E.g. test results, arriving from another thread while a frame is drawn."""
    cli.draw_interface()
    assert not cli._needs_redraw  # nothing changed

    lines_to_text = cli._lines_to_text

    def update_after_content_was_read(lines):
        cli.update_main_content(Text("new results"))
        return lines_to_text(lines)

    monkeypatch.setattr(cli, "_lines_to_text", update_after_content_was_read)
    cli.draw_interface()
    assert cli._needs_redraw
