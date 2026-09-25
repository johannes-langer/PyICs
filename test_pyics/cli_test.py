"""
Rendering of the CLI's header.
"""

import pytest
from rich.markup import escape

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
