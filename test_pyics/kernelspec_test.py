"""
The "Python (pyics)" Jupyter kernel (``pyics.notebook.kernelspec``) and the
``pyics install_kernel`` subcommand, installed into temporary directories only.
"""

import json
import sys
from collections.abc import Callable
from pathlib import Path

import jupyter_client.kernelspec
import pytest

from pyics.notebook import kernelspec
from pyics.scripts import main


@pytest.fixture(autouse=True)
def jupyter_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """
    Jupyter's data directory for the user, and the prefix of this environment, as
    temporary directories, which are the only ones Jupyter looks for kernels in, so
    that no test sees or changes the real ones.
    """
    data, prefix = tmp_path / "jupyter-data", tmp_path / "env"
    monkeypatch.setenv("JUPYTER_DATA_DIR", str(data))
    monkeypatch.setenv("IPYTHONDIR", str(tmp_path / "ipython"))
    monkeypatch.setattr(sys, "prefix", str(prefix))

    def jupyter_path(*subdirs: str) -> list[str]:
        return [
            str(data.joinpath(*subdirs)),
            str(prefix.joinpath("share/jupyter", *subdirs)),
        ]

    monkeypatch.setattr(jupyter_client.kernelspec, "jupyter_path", jupyter_path)
    return data, prefix


def _spec(directory: Path) -> dict:
    return json.loads((directory / "kernel.json").read_text(encoding="utf-8"))


def _check_spec(spec: dict) -> None:
    """``spec`` runs this interpreter as an IPython kernel with the pyics extension."""
    assert spec["display_name"] == "Python (pyics)"
    assert spec["language"] == "python"
    argv = spec["argv"]
    assert argv[0] == sys.executable
    assert argv[argv.index("-m") + 1] == "ipykernel_launcher"
    assert "{connection_file}" in argv
    assert argv[-1] == "--ext=pyics"


def test_the_kernel_is_installed_for_the_user(
    jupyter_dirs: tuple[Path, Path],
) -> None:
    data, _ = jupyter_dirs
    assert not kernelspec.is_installed()
    installed = kernelspec.install_kernel()
    assert installed == data / "kernels" / "pyics"
    _check_spec(_spec(installed))
    assert kernelspec.is_installed()
    # installing again replaces it
    assert kernelspec.install_kernel() == installed


def test_the_kernel_can_be_installed_into_an_environment(
    jupyter_dirs: tuple[Path, Path], tmp_path: Path
) -> None:
    data, prefix = jupyter_dirs
    installed = kernelspec.install_kernel(sys_prefix=True)
    assert installed == prefix / "share/jupyter/kernels/pyics"
    _check_spec(_spec(installed))
    assert kernelspec.is_installed()
    assert not (data / "kernels").exists()

    other = kernelspec.install_kernel(prefix=tmp_path / "other")
    assert other == tmp_path / "other/share/jupyter/kernels/pyics"


def test_a_kernel_of_another_interpreter_doesnt_count(
    jupyter_dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = kernelspec.install_kernel()
    spec = _spec(installed)
    spec["argv"][0] = str(Path(sys.executable).parent / "other-python")
    (installed / "kernel.json").write_text(json.dumps(spec), encoding="utf-8")
    assert not kernelspec.is_installed()

    spec["argv"] = [
        sys.executable,
        "-m",
        "ipykernel_launcher",
        "-f",
        "{connection_file}",
    ]
    (installed / "kernel.json").write_text(json.dumps(spec), encoding="utf-8")
    assert not kernelspec.is_installed()

    (installed / "kernel.json").write_text("{", encoding="utf-8")
    assert not kernelspec.is_installed()


@pytest.fixture
def pyics(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Callable[..., str]:
    """Runs ``pyics`` with the given arguments, and returns what it printed."""

    def run(*args: str) -> str:
        monkeypatch.setattr("sys.argv", ["pyics", *args])
        main.run()
        return capsys.readouterr().out

    return run


def test_install_kernel_installs_for_the_user_by_default(
    pyics: Callable[..., str], jupyter_dirs: tuple[Path, Path]
) -> None:
    data, prefix = jupyter_dirs
    out = pyics("install_kernel")
    installed = data / "kernels" / "pyics"
    assert f'Installed the "Python (pyics)" kernel in {installed}.' in out
    _check_spec(_spec(installed))
    assert not prefix.exists()

    pyics("install_kernel", "--user")
    _check_spec(_spec(installed))


def test_install_kernel_into_the_environment(
    pyics: Callable[..., str], jupyter_dirs: tuple[Path, Path]
) -> None:
    data, prefix = jupyter_dirs
    out = pyics("install_kernel", "--sys_prefix")
    installed = prefix / "share/jupyter/kernels/pyics"
    assert str(installed) in out
    _check_spec(_spec(installed))
    assert not data.exists()


def test_install_kernel_reports_errors(
    pyics: Callable[..., str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(**kwargs: object) -> Path:
        raise PermissionError(13, "Permission denied", "/usr/share/jupyter")

    monkeypatch.setattr(kernelspec, "install_kernel", fail)
    with pytest.raises(SystemExit, match="Could not install the kernel"):
        pyics("install_kernel")
