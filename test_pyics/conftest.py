"""
Fixtures shared by the tests of distributing courses (``pyics.distribution``), and the
isolation of all tests from the user's pyics configuration.
"""

import importlib
import sys
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from pyics.data import Course, Task
from pyics.distribution import archive, sources
from pyics.tests.tests import SimpleTest

ADD_TEMPLATE = "def add(a, b):\n    return 0\n"
ADD_SOLUTION = "def add(a, b):\n    return a + b\n"


@pytest.fixture(autouse=True)
def isolated_config(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A configuration directory of its own, without sources, so that no test sees the
    user's sources, nor changes them."""
    config = tmp_path_factory.mktemp("pyics-config")
    monkeypatch.setenv(sources.CONFIG_DIR_ENV, str(config))
    return config


def write_course(
    root: Path,
    name: str = "Adding",
    *,
    metadata: dict[str, Any] | None = None,
    template: str = ADD_TEMPLATE,
    expected: Any = 3,
) -> Path:
    """
    Write a course of one task, "Add", to ``root``, laid out like the demo course: the
    manifest in ``root``, the task file in ``.tasks``, and its exercise and solution
    files in ``.exercises`` and ``.solutions``. Its test expects ``add(1, 2)`` to be
    ``expected``. Returns the manifest's path.
    """
    ex, sol = root / ".exercises/add.py", root / ".solutions/add.py"
    for path, code in ((ex, template), (sol, ADD_SOLUTION)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code)
    test = SimpleTest("adds", ex, "add", expected, function_args=[1, 2])
    task = Task.create_task_json(
        root / ".tasks/add", "Add", "Implement add(a, b).", ex, sol, [test]
    )
    return Course.create_course_manifest(
        root / "course", [task], name=name, metadata=metadata
    )


@pytest.fixture
def course_writer() -> Callable[..., Path]:
    """``write_course``."""
    return write_course


def publish(manifest: Path, library: Path, **pack: Any) -> Path:
    """Pack the course of ``manifest`` into the directory ``library``, named after its
    slug and version, and rewrite the library's index. Returns the index's path."""
    course = archive.read_course(manifest)
    info = course.info
    name = f"{info.slug}-{info.version}.zip" if info.version else f"{info.slug}.zip"
    archive.pack(course, library / name, **pack)
    index, _ = sources.build_index(library)
    return index


@pytest.fixture
def publisher() -> Callable[..., Path]:
    """``publish``."""
    return publish


def add_source(
    index: Path | str, name: str | None = None, **kwargs: Any
) -> sources.Source:
    """Add the index at ``index`` as a source, as ``pyics add_source`` does."""
    source, _, data = sources.prepare_source(str(index), name, **kwargs)
    sources.save_source(source, data)
    return source


@pytest.fixture
def source_adder() -> Callable[..., sources.Source]:
    """``add_source``."""
    return add_source


@pytest.fixture
def fake_package(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    """
    An installed distribution ``fake-package`` 1.2.0, standing in for a third-party
    package such as PyYAML (whose module is ``yaml``): its module ``fakepkg`` has a
    class ``Point`` and an enum ``Color``. Yields the distribution's name.
    """
    site = tmp_path_factory.mktemp("site")
    (site / "fakepkg").mkdir()
    (site / "fakepkg/__init__.py").write_text(
        textwrap.dedent(
            """
            from enum import Enum


            class Point:
                def __init__(self, x, y):
                    self.x, self.y = x, y

                def __eq__(self, other):
                    return isinstance(other, Point) and (self.x, self.y) == (other.x, other.y)


            class Color(Enum):
                RED = 1
            """
        )
    )
    info = site / "fake_package-1.2.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: fake-package\nVersion: 1.2.0\n"
    )
    (info / "top_level.txt").write_text("fakepkg\n")
    (info / "RECORD").write_text("fakepkg/__init__.py,,\n")
    monkeypatch.syspath_prepend(str(site))
    importlib.invalidate_caches()
    yield "fake-package"
    sys.modules.pop("fakepkg", None)
