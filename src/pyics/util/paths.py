import os
from os import PathLike
from pathlib import Path


def relative_to(path: PathLike, base: PathLike) -> Path:
    """
    Express ``path`` relative to the directory ``base`` (e.g. ``../ex/a.py``), so that it
    can be stored in a file located in ``base``. Relative inputs are interpreted relative
    to the working directory.

    If no relative path exists (on Windows, ``path`` and ``base`` on different drives),
    returns ``path`` as an absolute path.
    """
    try:
        return Path(os.path.relpath(Path(path).absolute(), Path(base).absolute()))
    except ValueError:
        return Path(path).absolute()


def resolve(path: PathLike, base: PathLike) -> Path:
    """
    Inverse of ``relative_to``: the path ``path`` points to when read from a file located
    in the directory ``base``, normalized (``a/../b`` becomes ``b``). Absolute paths are
    kept as they are.
    """
    return Path(os.path.normpath(Path(base) / path))
