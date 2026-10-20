"""
A virtual environment pyics runs in, for the tests of where sources are stored (see the
``virtual_env`` fixture). Imported by test modules as ``from venv_helpers import ...``,
like ``notebook_helpers``.
"""

import json
from pathlib import Path
from typing import NamedTuple

from pyics.distribution import sources


class VirtualEnv(NamedTuple):
    """A virtual environment pyics runs in (see ``virtual_env``)."""

    prefix: Path
    # the user's configuration and cache directories, shared by all environments
    user_config: Path
    user_cache: Path

    @property
    def legacy_file(self) -> Path:
        """The user's sources file, as older versions of pyics stored it."""
        return self.user_config / sources.SOURCES_FILE

    def write_legacy(self, *legacy: sources.Source) -> Path:
        """Store the sources ``legacy`` for the user, as older versions of pyics did.
        Returns the file."""
        self.user_config.mkdir(parents=True, exist_ok=True)
        data = {"version": 1, "sources": [s.to_json() for s in legacy]}
        self.legacy_file.write_text(json.dumps(data))
        return self.legacy_file
