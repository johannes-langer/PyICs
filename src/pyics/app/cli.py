import importlib.metadata
import signal
import sys
import threading
import time
from typing import Any

from pyfiglet import Figlet
from rich import box
from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.padding import Padding
from rich.progress import BarColumn, Progress, TextColumn
from rich.segment import Segment
from rich.table import Table
from rich.text import Text, TextType

LINE_UP = "\033[1A"
LINE_CLEAR = "\x1b[2K"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
ALT_SCREEN_ENTER = "\033[?1049h"
ALT_SCREEN_LEAVE = "\033[?1049l"


class CLI:
    def __init__(self) -> None:
        self.running = False
        self.console = Console()
        self.progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(
                bar_width=None,
                finished_style="blue",
                pulse_style="blue",
                complete_style="blue",
            ),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            expand=True,
        )
        self.task_id = self.progress.add_task("Progress", total=1)
        self.hotkeys = {}

        # main-content viewport scroll state, in lines
        self.scroll_offset = 0
        self._max_scroll_offset = 0

        self.update_info("Ready")
        self.update_main_content(
            Text("Main program outputs will appear here...", style="dim"), "Main Output"
        )
        self.update_progress(0.0)

        signal.signal(signal.SIGWINCH, self._handle_resize)

    def _handle_resize(self, signum, frame):
        """
        Resize handler to attach to the SIGWINCH signal.
        """
        self._needs_redraw = True

    def __call__(self, *args: Any, **kwds: Any) -> Any:
        """
        Start up the cli Daemon.
        """
        self.running = True
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()

    def generate_header(self) -> Panel:
        """
        Construct the header from the info-text attribute.
        """
        figlet_text = Figlet(font="big").renderText("PyICs").strip("\n")

        table = Table(
            expand=True, box=box.MINIMAL, show_header=False, style="blue", padding=(0, 2)
        )
        table.add_column(
            vertical="middle",
            min_width=(len_figlet := len(figlet_text.split("\n")[0])),
            max_width=len_figlet,
        )
        table.add_column(vertical="middle", ratio=1)
        table.add_row(
            Text(figlet_text, style="blue", no_wrap=True),
            Text(self.info_text, style="white"),
        )

        return Panel(
            table,
            title=f"[blue]PyICs[white] {importlib.metadata.version('pyics')}",
            title_align="left",
            border_style="blue",
            box=box.ROUNDED,
            padding=(0, 0),
        )

    def generate_footer(self) -> Panel:
        """
        Construct the footer from the hotkey and progress bar attributes.
        """
        hotkeys_text = Text()
        for i, (key, action) in enumerate(self.hotkeys.items()):
            if i > 0:
                hotkeys_text.append(" | ", style="dim")
            hotkeys_text.append(f"<{key}>", style="bold cyan")
            hotkeys_text.append(f" {action}")

        self.progress.update(self.task_id, completed=self.percentage)

        group = Group(self.progress, Align.center(hotkeys_text))

        return Panel(group, border_style="blue", box=box.ROUNDED)

    def update_main_content(self, renderable: RenderableType, title: TextType = ""):
        """
        Update the content of the main panel.

        Parameters
        ----------
        renderable: RenderableType
            New content to put inside the main panel.
        title: TextType (optional)
            Set a frame title for the main content. If a title other than "" (default)
            is provided, adds top padding to renderable.
        """
        self.main_content = Padding(renderable, (0 if title == "" else 1, 0, 0, 0))
        self.main_title = title
        self._needs_redraw = True

    def add_to_main_content(self, renderable: RenderableType) -> None:
        """
        _Add_ renderable below current main content.

        **Important:** anything added to the main content this way will be overwritten
        the next time a `ùpdate_main_content`` method call occurs.

        Parameters
        ----------
        renderable: RenderableType
            New content to _add_ to the main content.
        """
        self.main_content = Group(self.main_content, Padding(renderable, (1, 0, 0, 0)))
        self._needs_redraw = True

    def update_progress(self, percentage: float):
        """
        Update the progress displayed in the progress bar in the footer.

        Parameters
        ----------
        percentage: float
            New progress percentage (as a fraction!)
        """
        self.percentage = percentage
        self._needs_redraw = True

    def update_hotkeys(self, hotkeys: dict):
        """
        Update the hotkeys displayed in the footer. This intentionally _replaces_ the dict,
        because that way it is easy for the controller to both remove and add or update
        components.

        Parameters
        ----------
        hotkeys: dict
            Hotkey dict.
        """
        self.hotkeys = hotkeys
        self._needs_redraw = True

    def update_info(self, text: str):
        """
        Update the info_text. The info text is shown in the right part of the header panel.

        Parameters
        ----------
        text: str
            The new info-text to draw. Supports ``rich`` markup.
        """
        self.info_text = text
        self._needs_redraw = True

    def scroll(self, delta: int) -> None:
        """
        Move the main-content viewport by ``delta`` lines, clamped to the scrollable range.

        Parameters
        ----------
        delta: int
            Number of lines to scroll by. Positive scrolls down, negative scrolls up.
        """
        self.scroll_offset = max(
            0, min(self.scroll_offset + delta, self._max_scroll_offset)
        )
        self._needs_redraw = True

    def _render_lines(self, renderable: RenderableType) -> list[list[Segment]]:
        """Render a renderable to its lines, uncropped by the console's current height."""
        options = self.console.options.update(height=None)
        return self.console.render_lines(renderable, options, pad=False)

    def _lines_to_text(self, lines: list[list[Segment]]) -> str:
        """Join rendered lines back into a single ANSI-encoded string."""
        segments: list[Segment] = []
        for i, line in enumerate(lines):
            if i:
                segments.append(Segment.line())
            segments.extend(line)
        return self.console._render_buffer(segments)

    def draw_interface(self):
        """
        Draw the PyICs interface. Header and footer are pinned to the top/bottom of the
        terminal; only the main content scrolls, windowed to the space left between them.
        """
        header_panel = self.generate_header()
        footer_panel = self.generate_footer()

        main_panel = Panel(
            self.main_content,
            title=self.main_title,  # should display task-id/task-number later
            title_align="left",
            box=box.SIMPLE,
        )

        term_height = self.console.size.height

        header_lines = self._render_lines(header_panel)
        footer_lines = self._render_lines(footer_panel)
        viewport_height = max(0, term_height - len(header_lines) - len(footer_lines))

        main_lines = self._render_lines(main_panel)
        self._max_scroll_offset = max(0, len(main_lines) - viewport_height)
        self.scroll_offset = max(0, min(self.scroll_offset, self._max_scroll_offset))
        visible_main_lines = main_lines[
            self.scroll_offset : self.scroll_offset + viewport_height
        ]

        sys.stdout.write("\033[H\033[2J")
        sys.stdout.write(self._lines_to_text(header_lines))
        sys.stdout.write("\n")
        sys.stdout.write(self._lines_to_text(visible_main_lines))
        sys.stdout.write(f"\033[{term_height - len(footer_lines) + 1};1H")
        sys.stdout.write(self._lines_to_text(footer_lines))
        sys.stdout.flush()
        self._needs_redraw = False

    def run(self):
        """
        Main CLI loop. While running, checks if a redraw is necessary (either updated
        content or window-resize), and redraws the interface when necessary.
        """
        sys.stdout.write(HIDE_CURSOR)
        sys.stdout.flush()

        try:
            while self.running:
                self.draw_interface() if self._needs_redraw else None

                time.sleep(0.05)

        finally:
            sys.stdout.write(SHOW_CURSOR)
            sys.stdout.flush()
