"""Isolates the tests of pyics-jupyter from the user's pyics configuration and from the
plugins pyics found before."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from pyics import plugins
from pyics.distribution import sources


@pytest.fixture(autouse=True)
def isolated_config(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A configuration directory of its own, so that no test sees or changes the
    user's."""
    config = tmp_path_factory.mktemp("pyics-config")
    monkeypatch.setenv(sources.CONFIG_DIR_ENV, str(config))
    return config


@pytest.fixture(autouse=True)
def fresh_plugins() -> Iterator[None]:
    """pyics's plugin manager is created anew for each test, and forgotten after it, so
    that the plugins a test registers don't outlive it."""
    plugins.manager.cache_clear()
    yield
    plugins.manager.cache_clear()
