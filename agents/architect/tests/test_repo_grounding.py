"""Tests for ``architect.repo_grounding`` — repo lookup tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from architect import repo_grounding


def test_clone_target_repo_returns_none_for_empty_target() -> None:
    assert repo_grounding.clone_target_repo(None) is None
    assert repo_grounding.clone_target_repo("") is None


def test_list_repo_paths_returns_empty_when_no_repo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(repo_grounding, "REPO_PATH", tmp_path / "missing")
    assert repo_grounding.list_repo_paths() == []


def test_list_repo_paths_filters_by_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(repo_grounding, "REPO_PATH", tmp_path)

    def fake_git(*args: str, cwd: Path) -> str:
        del cwd
        assert args == ("ls-files",)
        return "src/foo.py\nsrc/bar.py\ntests/test_foo.py\nREADME.md\n"

    monkeypatch.setattr(repo_grounding, "git", fake_git)
    paths = repo_grounding.list_repo_paths(prefix="src/")
    assert paths == ["src/foo.py", "src/bar.py"]


def test_list_repo_paths_caps_at_max_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(repo_grounding, "REPO_PATH", tmp_path)

    output = "\n".join(f"file_{i}.py" for i in range(500)) + "\n"
    monkeypatch.setattr(repo_grounding, "git", lambda *_a, **_k: output)
    paths = repo_grounding.list_repo_paths(max_entries=10)
    assert len(paths) == 10
    assert paths[0] == "file_0.py"


def test_list_repo_paths_hard_caps_at_module_max(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Even when the caller passes a huge max_entries, MAX_LIST_ENTRIES wins."""
    monkeypatch.setattr(repo_grounding, "REPO_PATH", tmp_path)

    output = "\n".join(f"file_{i}.py" for i in range(1000)) + "\n"
    monkeypatch.setattr(repo_grounding, "git", lambda *_a, **_k: output)
    paths = repo_grounding.list_repo_paths(max_entries=10_000)
    assert len(paths) == repo_grounding.MAX_LIST_ENTRIES


def test_read_repo_file_returns_empty_when_no_repo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(repo_grounding, "REPO_PATH", tmp_path / "missing")
    assert repo_grounding.read_repo_file("foo.py") == ""


def test_read_repo_file_returns_file_contents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(repo_grounding, "REPO_PATH", tmp_path)
    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "foo.py"
    target.write_text("def hello(): return 1\n", encoding="utf-8")

    assert repo_grounding.read_repo_file("src/foo.py") == "def hello(): return 1\n"


def test_read_repo_file_caps_at_max_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(repo_grounding, "REPO_PATH", tmp_path)
    big = "x" * (repo_grounding.MAX_FILE_BYTES + 100)
    (tmp_path / "big.txt").write_text(big, encoding="utf-8")

    content = repo_grounding.read_repo_file("big.txt")
    assert len(content) == repo_grounding.MAX_FILE_BYTES


def test_read_repo_file_rejects_path_traversal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``..``-segment escapes outside the repo must read nothing."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    monkeypatch.setattr(repo_grounding, "REPO_PATH", repo_root)
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")

    assert repo_grounding.read_repo_file("../outside.txt") == ""


def test_read_repo_file_returns_empty_for_missing_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(repo_grounding, "REPO_PATH", tmp_path)
    assert repo_grounding.read_repo_file("does/not/exist.py") == ""


def test_read_repo_file_returns_empty_for_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(repo_grounding, "REPO_PATH", tmp_path)
    (tmp_path / "subdir").mkdir()
    assert repo_grounding.read_repo_file("subdir") == ""
