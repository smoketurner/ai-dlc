"""Git failures in ``implementer.repo_ops`` must not surface the install token."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from implementer import repo_ops
from implementer.repo_ops import RepoSession

TOKEN = "ghs_supersecrettoken"  # noqa: S105 - fixture-only fake token


@pytest.fixture
def session() -> RepoSession:
    return RepoSession(
        target_repo="owner/name",
        access_token=TOKEN,
        author_login="ai-dlc[bot]",
        author_email="ai-dlc-bot@users.noreply.github.com",
        on_behalf_of_user=False,
    )


def failing_run(returncode: int = 128, stderr: str = "fatal: repository not found") -> Any:
    """Build a ``subprocess.run`` stand-in that always fails.

    Honours ``check`` exactly as the real ``subprocess.run`` does, so a
    regression back to ``check=True`` raises ``CalledProcessError`` — whose
    string form embeds the full argv, token included.
    """

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if kwargs.get("check"):
            raise subprocess.CalledProcessError(returncode, cmd, output="", stderr=stderr)
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)

    return run


def test_failed_clone_raises_without_the_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session: RepoSession,
) -> None:
    monkeypatch.setattr(repo_ops, "repo_path", lambda: tmp_path / "repo")
    monkeypatch.setattr(repo_ops.subprocess, "run", failing_run())

    with pytest.raises(RuntimeError) as excinfo:
        repo_ops.clone_repo(session)

    message = str(excinfo.value)
    assert TOKEN not in message
    assert "owner/name" in message
    assert "repository not found" in message


def test_failed_clone_does_not_leak_via_called_process_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session: RepoSession,
) -> None:
    """A ``check=True`` regression would raise CalledProcessError carrying argv."""
    monkeypatch.setattr(repo_ops, "repo_path", lambda: tmp_path / "repo")
    monkeypatch.setattr(repo_ops.subprocess, "run", failing_run())

    with pytest.raises(RuntimeError) as excinfo:
        repo_ops.clone_repo(session)

    assert not isinstance(excinfo.value, subprocess.CalledProcessError)


def test_failed_run_git_redacts_tokenised_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``reset_existing_clone`` passes the clone URL to ``git remote set-url``."""
    monkeypatch.setattr(repo_ops, "repo_path", lambda: tmp_path)
    monkeypatch.setattr(repo_ops.subprocess, "run", failing_run(stderr="fatal: bad remote"))
    url = f"https://x-access-token:{TOKEN}@github.com/owner/name.git"

    with pytest.raises(RuntimeError) as excinfo:
        repo_ops.run_git("remote", "set-url", "origin", url, cwd=tmp_path)

    message = str(excinfo.value)
    assert TOKEN not in message
    assert "x-access-token:<redacted>@github.com" in message


def test_successful_run_git_returns_stdout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n", stderr="")

    monkeypatch.setattr(repo_ops, "repo_path", lambda: tmp_path)
    monkeypatch.setattr(repo_ops.subprocess, "run", run)

    assert repo_ops.run_git("rev-parse", "HEAD", cwd=tmp_path) == "abc123\n"
