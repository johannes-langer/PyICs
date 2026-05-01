from concurrent.futures import thread
import queue
import sys
import threading
import time
import signal
import shutil
from typing import Dict, Any

from pyfiglet import Figlet
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.progress import Progress, BarColumn, TextColumn
from rich.text import Text
from rich.table import Table
from rich.align import Align

__version__ = "0.1.0"

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
        self.hotkeys = {
            "ctrl-c": "Quit",
            "n": "next task",
            "p": "previous task",
            "s": "skip",
        }
        self.info_text = "Ready."
        self.main_content = Text("Main program outputs will appear here...", style="dim")
        self.percentage = 0.0

        figlet_text = Figlet(font="big").renderText("PyICs").strip("\n")

        self._needs_redraw = True
        signal.signal(signal.SIGWINCH, self._handle_resize)

    def _handle_resize(self, signum, frame):
        self._needs_redraw = True

    def __call__(self, *args: Any, **kwds: Any) -> Any:
        """
        Start up the cli Daemon
        """
        self.running = True
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()

    def generate_header(self) -> Panel:
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
            title=f"version {__version__}",
            title_align="left",
            border_style="blue",
            box=box.ROUNDED,
            padding=(0, 0),
        )

    def generate_footer(self) -> Panel:
        hotkeys_text = Text()
        for i, (key, action) in enumerate(self.hotkeys.items()):
            if i > 0:
                hotkeys_text.append(" | ", style="dim")
            hotkeys_text.append(f"<{key}>", style="bold cyan")
            hotkeys_text.append(f" {action}")

        self.progress.update(self.task_id, completed=self.percentage)

        group = Group(self.progress, Align.center(hotkeys_text))

        return Panel(group, border_style="blue", box=box.ROUNDED)

    def update_main_content(self, renderable):
        self.main_content = renderable
        self._needs_redraw = True

    def update_progress(self, percentage: float):
        self.percentage = percentage
        self._needs_redraw = True

    def update_hotkeys(self, hotkeys: dict):
        self.hotkeys = hotkeys
        self._needs_redraw = True

    def update_info(self, text: str):
        self.info_text = text
        self._needs_redraw = True

    def draw_interface(self):
        header_panel = self.generate_header()
        footer_panel = self.generate_footer()

        main_panel = Panel(
            self.main_content,
            title="Main Output",  # should display task-id/task-number later
            title_align="left",
            box=box.SIMPLE,
        )

        sys.stdout.write("\033[H\033[2J")
        sys.stdout.flush()

        self.console.print(header_panel)
        self.console.print(main_panel)
        self.console.print(footer_panel)
        self._needs_redraw = False

    def run(self):
        sys.stdout.write(HIDE_CURSOR)
        sys.stdout.flush()

        try:
            while self.running:
                self.draw_interface() if self._needs_redraw else None

                time.sleep(0.05)

        finally:
            sys.stdout.write(SHOW_CURSOR)
            sys.stdout.flush()
