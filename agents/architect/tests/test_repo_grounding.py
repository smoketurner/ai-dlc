"""Tests for ``architect.repo_grounding`` — repo lookup tools."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from architect import repo_grounding

MEMORY_BUCKET = "ai-dlc-test-memory-md"
PROJECT_SLUG = "ai-dlc"
PROJECT_KEY = f"projects/{PROJECT_SLUG}/MEMORY.md"


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


# ---------------------------------------------------------------------------
# sync_memory_md_from_clone
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_bucket(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    """moto-backed S3 + a clean per-project memory bucket + cleared client cache."""
    monkeypatch.setenv("AIDLC_MEMORY_MD_BUCKET", MEMORY_BUCKET)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setattr(repo_grounding, "REPO_PATH", tmp_path)
    repo_grounding.s3_client.cache_clear()
    with mock_aws():
        boto3.client("s3").create_bucket(Bucket=MEMORY_BUCKET)
        yield
    repo_grounding.s3_client.cache_clear()


def test_sync_writes_combined_object_with_both_sources(
    memory_bucket: None,
    tmp_path: Path,
) -> None:
    """Both files present — body has one section per source, in declared order."""
    del memory_bucket
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "MEMORY.md").write_text("memory body\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("claude body\n", encoding="utf-8")

    repo_grounding.sync_memory_md_from_clone(
        project_slug=PROJECT_SLUG,
        target_repo="owner/repo",
    )

    body = (
        boto3.client("s3", region_name="us-east-1")
        .get_object(Bucket=MEMORY_BUCKET, Key=PROJECT_KEY)["Body"]
        .read()
        .decode("utf-8")
    )
    assert "## docs/MEMORY.md" in body
    assert "memory body" in body
    assert "## CLAUDE.md" in body
    assert "claude body" in body
    # docs/MEMORY.md section appears before CLAUDE.md (declared source order).
    assert body.index("## docs/MEMORY.md") < body.index("## CLAUDE.md")
    assert "Source repo: owner/repo" in body
    # No body-level timestamp — keeps idempotency stable across runs.
    assert "Synced at" not in body


def test_sync_writes_partial_object_when_only_one_source_exists(
    memory_bucket: None,
    tmp_path: Path,
) -> None:
    """Only CLAUDE.md present — body has one section, no MEMORY.md placeholder."""
    del memory_bucket
    (tmp_path / "CLAUDE.md").write_text("claude only\n", encoding="utf-8")

    repo_grounding.sync_memory_md_from_clone(project_slug=PROJECT_SLUG)

    body = (
        boto3.client("s3", region_name="us-east-1")
        .get_object(Bucket=MEMORY_BUCKET, Key=PROJECT_KEY)["Body"]
        .read()
        .decode("utf-8")
    )
    assert "## CLAUDE.md" in body
    assert "claude only" in body
    assert "## docs/MEMORY.md" not in body


def test_sync_skips_put_when_no_sources_exist(
    memory_bucket: None,
    tmp_path: Path,
) -> None:
    """No grounding files in the clone — bucket stays empty."""
    del memory_bucket
    del tmp_path  # repo path is empty, no files written

    repo_grounding.sync_memory_md_from_clone(project_slug=PROJECT_SLUG)

    listed = boto3.client("s3", region_name="us-east-1").list_objects_v2(
        Bucket=MEMORY_BUCKET,
        Prefix=f"projects/{PROJECT_SLUG}/",
    )
    assert listed.get("KeyCount", 0) == 0


def test_sync_is_idempotent_on_unchanged_content(
    memory_bucket: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second sync with identical body skips put_object (head_object MD5 match).

    The composed body has no timestamp by design (see
    ``compose_memory_md_body``) precisely so two syncs over the same
    cloned content produce byte-identical bodies and the second one
    short-circuits on ETag match.
    """
    del memory_bucket
    (tmp_path / "CLAUDE.md").write_text("stable content\n", encoding="utf-8")

    put_calls: list[str] = []
    real_put = repo_grounding.s3_client().put_object

    def counting_put(**kwargs: Any) -> Any:
        put_calls.append(str(kwargs.get("Key", "")))
        return real_put(**kwargs)

    monkeypatch.setattr(repo_grounding.s3_client(), "put_object", counting_put)

    repo_grounding.sync_memory_md_from_clone(project_slug=PROJECT_SLUG)
    repo_grounding.sync_memory_md_from_clone(project_slug=PROJECT_SLUG)

    # First call writes; second call sees matching ETag and skips.
    assert put_calls == [PROJECT_KEY]


def test_sync_no_op_when_bucket_env_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Without ``AIDLC_MEMORY_MD_BUCKET`` the sync logs a warning and returns."""
    monkeypatch.delenv("AIDLC_MEMORY_MD_BUCKET", raising=False)
    monkeypatch.setattr(repo_grounding, "REPO_PATH", tmp_path)
    (tmp_path / "CLAUDE.md").write_text("anything\n", encoding="utf-8")

    # Should not raise even though no bucket is configured.
    repo_grounding.sync_memory_md_from_clone(project_slug=PROJECT_SLUG)
