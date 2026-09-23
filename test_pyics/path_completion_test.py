"""
Tab completion of file paths in ``PathInput`` prompts.
"""

from pathlib import Path

import pytest

from pyics.app.prompts import PathInput, _complete_path


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "task_1.py").touch()
    (tmp_path / "task_2.py").touch()
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "inner.py").touch()
    (tmp_path / "solution.py").touch()
    (tmp_path / ".hidden").touch()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_unique_file_completes_fully(tree: Path) -> None:
    assert _complete_path("sol") == ("solution.py", [])


def test_unique_dir_gets_trailing_slash(tree: Path) -> None:
    assert _complete_path("tasks/") == ("tasks/inner.py", [])
    assert _complete_path("taskz") == ("taskz", [])
    (tree / "task_1.py").unlink()
    (tree / "task_2.py").unlink()
    assert _complete_path("ta") == ("tasks/", [])


def test_ambiguous_extends_to_common_prefix(tree: Path) -> None:
    assert _complete_path("t") == ("task", ["task_1.py", "task_2.py", "tasks/"])


def test_absolute_dir_is_kept(tree: Path) -> None:
    assert _complete_path(f"{tree}/so") == (f"{tree}/solution.py", [])


def test_dotfiles_hidden_unless_prefix_is_dot(tree: Path) -> None:
    assert ".hidden" not in _complete_path("")[1]
    assert _complete_path(".h") == (".hidden", [])


def test_no_match_or_missing_dir_is_unchanged(tree: Path) -> None:
    assert _complete_path("nothing") == ("nothing", [])
    assert _complete_path("missing/fi") == ("missing/fi", [])


def test_home_is_expanded_only_for_lookup(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tree))
    assert _complete_path("~") == ("~/", [])
    assert _complete_path("~/sol") == ("~/solution.py", [])


def test_path_input_tab_keeps_text_after_cursor(tree: Path) -> None:
    prompt = PathInput(None, "path", initial="sol")  # type: ignore[arg-type]
    prompt.buffer = "sol|rest"
    prompt.cursor_index = 3
    prompt.handle_key("\t")
    assert prompt.buffer == "solution.py|rest"
    assert prompt.cursor_index == len("solution.py")


def test_path_input_candidates_cleared_on_typing(tree: Path) -> None:
    prompt = PathInput(None, "path", initial="t")  # type: ignore[arg-type]
    prompt.handle_key("\t")
    assert prompt.buffer == "task"
    assert prompt.candidates == ["task_1.py", "task_2.py", "tasks/"]
    prompt.render()
    prompt.handle_key("s")
    assert prompt.candidates == []
    assert prompt.buffer == "tasks"
