import sys
from pathlib import Path
from types import ModuleType

from pyics.util import paths
from pyics.util.paths import StrPath


def prepare_imports(path: StrPath) -> None:
    """
    Let the code of the file at ``path`` import the modules next to it, as it can when it
    is run as a script, or in Jupyter: its directory is put first on ``sys.path``. The
    modules imported from there before are forgotten, so that they are imported anew,
    with the students' latest changes, as a new kernel would.
    """
    directory = paths.absolute(path).parent
    entry = str(directory)
    if entry in sys.path:
        sys.path.remove(entry)
    sys.path.insert(0, entry)

    try:
        local = {p.stem if p.suffix == ".py" else p.name for p in directory.iterdir()}
    except OSError:
        return
    for name, module in list(sys.modules.items()):
        if name.partition(".")[0] in local and _imported_from(module, name, directory):
            del sys.modules[name]


def _imported_from(module: ModuleType, name: str, directory: Path) -> bool:
    """Whether ``module``, imported as ``name``, is a module (or package) of
    ``directory``, as found with it on ``sys.path``. Modules of directories below it
    don't count, e.g. a package in ``directory/src``."""
    file = getattr(module, "__file__", None)
    if not isinstance(file, str):
        return False
    base = directory.joinpath(*name.split("."))
    candidates = (base.with_name(base.name + ".py"), base / "__init__.py")
    try:
        found = Path(file).resolve()
        return any(found == c.resolve() for c in candidates)
    except OSError:
        return False
