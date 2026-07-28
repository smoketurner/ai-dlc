"""Credential redaction for log-, event-, and UI-bound text."""

from __future__ import annotations

from common.redaction import redact_secrets

CLONE_FAILURE = (
    "Command '['/usr/bin/git', 'clone', '--filter=blob:none', '--branch', 'main', "
    "'https://x-access-token:ghs_supersecrettoken@github.com/smoketurner/ai-dlc.git', "
    "'/workspace/repo']' returned non-zero exit status 128."
)


def test_redacts_token_from_clone_url() -> None:
    redacted = redact_secrets(CLONE_FAILURE)
    assert "ghs_supersecrettoken" not in redacted
    assert "x-access-token:<redacted>@github.com" in redacted
    assert "smoketurner/ai-dlc.git" in redacted


def test_redacts_bare_token_outside_a_url() -> None:
    redacted = redact_secrets("auth failed for ghp_abc123DEF456 on retry")
    assert "ghp_abc123DEF456" not in redacted
    assert redacted == "auth failed for <redacted> on retry"


def test_redacts_fine_grained_pat() -> None:
    assert "github_pat_11ABC" not in redact_secrets("token github_pat_11ABC_xyz used")


def test_redacts_archive_token_query_param() -> None:
    raw = "urlopen failed: https://codeload.github.com/o/r/legacy.tar.gz/abc?token=AABBCC"
    redacted = redact_secrets(raw)
    assert "AABBCC" not in redacted
    assert "<redacted>" in redacted


def test_redacts_archive_token_after_other_params() -> None:
    redacted = redact_secrets("url=https://codeload.example/x?ref=abc&token=SECRETXYZ&extra=1")
    assert "SECRETXYZ" not in redacted
    assert "extra=1" in redacted


def test_is_idempotent() -> None:
    once = redact_secrets(CLONE_FAILURE)
    assert redact_secrets(once) == once


def test_passes_through_text_without_credentials() -> None:
    plain = "git fetch origin main failed (exit 128) cwd=/workspace/repo"
    assert redact_secrets(plain) == plain


def test_leaves_empty_string_alone() -> None:
    assert redact_secrets("") == ""
