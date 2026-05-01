import os
from pathlib import Path


class Task:
    """Unified Task Representation"""

    def __init__(self, task_file: str | Path):
        """
        Parses a task form a task json-file.

        Parameters
        ----------
        task_file: str | Path
            Path to the task file
        """
        ...
        self.exercise_files: list[Path] = [Path()]
        self.last_mtimes = {
            f: os.path.getmtime(f) for f in self.exercise_files if os.path.exists(f)
        }

    def check_file_changes(self) -> None:
        """
        Check whether an exercise file has been changed by monitoring the last modified
        timestamp.
        """
        for filepath in self.exercise_files:
            self._check_file_changed(filepath)

    def _check_file_changed(self, filepath: Path):
        """ """
        if not os.path.exists(filepath):
            pass

        current_mtime = os.path.getmtime(filepath)
        if current_mtime > self.last_mtimes.get(filepath, 0):
            self.last_mtimes[filepath] = current_mtime
            self.run_file_on_change(filepath)

    def run_file_on_change(self, filepath: Path) -> None:
        """ """
