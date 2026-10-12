"""
The "Python (pyics)" Jupyter kernel (``pyics_jupyter.kernelspec``) and ``python -m
pyics_jupyter install``, installed into temporary directories only.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pyics_jupyter.__main__ import main


def _check_spec(directory: Path, *, here: bool = True) -> None:
    """``directory`` holds a spec that runs an interpreter (this one, if installed
    ``here``, in this process) as an IPython kernel with the extension."""
    spec = json.loads((directory / "kernel.json").read_text(encoding="utf-8"))
    assert spec["display_name"] == "Python (pyics)"
    assert spec["language"] == "python"
    argv = spec["argv"]
    assert argv[0] == sys.executable or not here
    assert argv[argv.index("-m") + 1] == "ipykernel_launcher"
    assert "{connection_file}" in argv
    assert argv[-1] == "--ext=pyics_jupyter"


def test_install_writes_the_kernel_spec_into_a_prefix(tmp_path: Path) -> None:
    output = subprocess.run(
        [sys.executable, "-m", "pyics_jupyter", "install", "--prefix", str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    installed = tmp_path / "share/jupyter/kernels/pyics"
    _check_spec(installed, here=False)
    assert f'Installed the "Python (pyics)" kernel in {installed}.' in output
    assert 'choose "Python (pyics)" as the kernel of your exercise notebooks' in output


def test_install_is_for_the_user_by_default_or_into_this_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("JUPYTER_DATA_DIR", str(tmp_path / "data"))
    assert main(["install"]) == 0
    _check_spec(tmp_path / "data/kernels/pyics")
    # installing again replaces it
    assert main(["install", "--user"]) == 0

    monkeypatch.setattr(sys, "prefix", str(tmp_path / "env"))
    assert main(["install", "--sys-prefix"]) == 0
    _check_spec(tmp_path / "env/share/jupyter/kernels/pyics")

    with pytest.raises(SystemExit):
        main(["install", "--user", "--sys-prefix"])
    capsys.readouterr()

    (tmp_path / "file").write_text("")
    assert main(["install", "--prefix", str(tmp_path / "file")]) == 1
    assert "Could not install the kernel" in capsys.readouterr().err
