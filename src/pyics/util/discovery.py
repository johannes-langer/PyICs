"""
Find task files and course manifests below a directory.
"""

import os
from collections.abc import Iterator
from pathlib import Path, PurePath
from typing import Any

from pyics.util import json_handling as json
from pyics.util import paths
from pyics.util.logging import get_logger

TASK_SUFFIX = ".pyicstask.json"
COURSE_SUFFIX = ".pyicscourse.json"
# never searched for task files or course manifests: large, and never contain any
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}


def find_files(root: Path, max_depth: int, suffix: str) -> Iterator[Path]:
    """The files whose names end in ``suffix`` at most ``max_depth`` directories below
    ``root``, skipping ``SKIP_DIRS``, relative to ``root``."""
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        if len(rel.parts) >= max_depth:
            dirnames.clear()
        else:
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for filename in sorted(filenames):
            if filename.endswith(suffix):
                yield rel / filename


def find_task_files(root: Path, max_depth: int) -> dict[Path, str]:
    """
    Find task files at most ``max_depth`` directories below ``root``, skipping
    ``SKIP_DIRS``. Files that can't be read or have no task name are skipped.

    Returns
    -------
    dict[Path, str]
        The task files' paths, relative to ``root``, mapped to their task names.
    """
    found: dict[Path, str] = {}
    for rel in find_files(root, max_depth, TASK_SUFFIX):
        try:
            with open(root / rel, encoding="utf-8") as f:
                name = json.load(f)["name"]
        except Exception:  # noqa: BLE001
            get_logger(__name__).debug(f"Skipping {root / rel}", exc_info=True)
            continue
        if isinstance(name, str):
            found[rel] = name
    return found


def find_courses(root: Path, max_depth: int) -> dict[Path, dict[str, Any]]:
    """
    Find course manifests at most ``max_depth`` directories below ``root``, skipping
    ``SKIP_DIRS``. Files that can't be read or have no tasks are skipped.

    Returns
    -------
    dict[Path, dict[str, Any]]
        The manifests' absolute paths, mapped to their contents.
    """
    found: dict[Path, dict[str, Any]] = {}
    for rel in find_files(root, max_depth, COURSE_SUFFIX):
        path = paths.absolute(root / rel)
        try:
            with open(path, encoding="utf-8") as f:
                manifest = json.load(f)
            tasks = manifest["tasks"]
        except Exception:  # noqa: BLE001
            get_logger(__name__).debug(f"Skipping {path}", exc_info=True)
            continue
        if isinstance(tasks, dict) and all(
            isinstance(v, (str, PurePath)) for v in tasks.values()
        ):
            found[path] = manifest
    return found
