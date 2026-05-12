"""Unit tests for the repo_helper Lambda handler.

The GitHub API is mocked via ``httpx.MockTransport``. ``handler.github_client``
is replaced with a client whose transport routes requests to a callback that
asserts URL/method/body shape and returns canned JSON.
``common.github_app.installation_token_for_repo`` is monkeypatched so tests
never touch Secrets Manager or the App-JWT machinery.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import repo_helper.handler as h
from aws_lambda_powertools.utilities.typing import LambdaContext

import common.github_app as auth_mod


def ctx() -> LambdaContext:
    """Minimal stand-in for LambdaContext — covers the fields powertools reads."""
    return cast(
        "LambdaContext",
        SimpleNamespace(
            function_name="repo_helper-test",
            memory_limit_in_mb=128,
            invoked_function_arn="arn:aws:lambda:us-east-1:000000000000:function:t",
            aws_request_id="rid-1",
        ),
    )


@pytest.fixture(autouse=True)
def stub_token_for_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the App-JWT / AgentCore Identity / installation-token dance in unit tests.

    Returns ``ghs_fake_user`` when a requestor JWT is supplied;
    ``ghs_fake_install`` otherwise, so tests can assert which auth path
    was taken via the Authorization header.
    """

    def fake_token_for_call(*, repo: str, requestor_sub: str | None) -> str:
        del repo
        return "ghs_fake_user" if requestor_sub else "ghs_fake_install"

    monkeypatch.setattr(h, "token_for_call", fake_token_for_call)
    monkeypatch.setattr(auth_mod, "token_for_call", fake_token_for_call)


@pytest.fixture
def patch_client(monkeypatch: pytest.MonkeyPatch) -> Callable[[httpx.MockTransport], None]:
    """Swap `handler.github_client` for a MockTransport-backed client.

    The mock client picks its Authorization header from ``token_for_call``
    so tests can assert which auth path was exercised.
    """

    def _patch(transport: httpx.MockTransport) -> None:
        def fake_client(*, repo: str, requestor_sub: str | None) -> httpx.Client:
            token = h.token_for_call(repo=repo, requestor_sub=requestor_sub)
            return httpx.Client(
                base_url=h.GITHUB_API,
                transport=transport,
                headers={
                    "Accept": h.ACCEPT_HEADER,
                    "Authorization": f"Bearer {token}",
                    "User-Agent": h.USER_AGENT,
                    "X-GitHub-Api-Version": h.API_VERSION,
                },
            )

        monkeypatch.setattr(h, "github_client", fake_client)

    return _patch


def test_invalid_event() -> None:
    out = h.handler({}, ctx())
    assert out["ok"] is False
    assert out["error"]["kind"] == "invalid_event"


def test_unknown_op() -> None:
    out = h.handler({"input": {"op": "delete_repo"}}, ctx())
    assert out["ok"] is False
    assert out["error"]["kind"] == "unknown_op"


def test_validation_error_missing_required() -> None:
    out = h.handler(
        {"input": {"op": "open_pr", "repo": "smoketurner/ai-dlc", "base": "main"}},
        ctx(),
    )
    assert out["ok"] is False
    assert out["error"]["kind"] == "validation_error"


def test_create_branch_validates_repo_format() -> None:
    out = h.handler(
        {"input": {"op": "create_branch", "repo": "no-slash", "branch": "x", "base": "main"}},
        ctx(),
    )
    assert out["ok"] is False
    assert out["error"]["kind"] == "validation_error"


def test_commit_files_requires_at_least_one_file() -> None:
    out = h.handler(
        {
            "input": {
                "op": "commit_files",
                "repo": "smoketurner/ai-dlc",
                "branch": "main",
                "message": "msg",
                "files": [],
            },
        },
        ctx(),
    )
    assert out["ok"] is False
    assert out["error"]["kind"] == "validation_error"


def test_token_field_is_rejected_in_input() -> None:
    """Auth is no longer caller-provided — extra fields like `token` must be rejected."""
    out = h.handler(
        {
            "input": {
                "op": "open_pr",
                "repo": "o/r",
                "base": "main",
                "head": "x",
                "title": "t",
                "body": "b",
                "token": "ghs_legacy",
            },
        },
        ctx(),
    )
    assert out["ok"] is False
    assert out["error"]["kind"] == "validation_error"


def test_requestor_sub_routes_through_user_token(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    """When `requestor_sub` is set, the call goes out with the user-on-behalf-of token."""
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            201,
            json={
                "number": 1,
                "html_url": "https://github.com/o/r/pull/1",
                "state": "open",
            },
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {
            "input": {
                "op": "open_pr",
                "repo": "o/r",
                "base": "main",
                "head": "x",
                "title": "t",
                "body": "b",
                "requestor_sub": "cognito-sub-abc123",
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert seen[0].headers["authorization"] == "Bearer ghs_fake_user"


def test_requestor_sub_absent_falls_back_to_installation_token(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    """When `requestor_sub` is absent, the call goes out with the installation token."""
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            201,
            json={
                "number": 1,
                "html_url": "https://github.com/o/r/pull/1",
                "state": "open",
            },
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {
            "input": {
                "op": "open_pr",
                "repo": "o/r",
                "base": "main",
                "head": "x",
                "title": "t",
                "body": "b",
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert seen[0].headers["authorization"] == "Bearer ghs_fake_install"


def test_open_pr_calls_github_and_returns_pr_url(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "POST"
        assert request.url.path == "/repos/smoketurner/ai-dlc/pulls"
        body = json.loads(request.content)
        assert body == {"title": "Add foo", "body": "Body", "head": "feature/foo", "base": "main"}
        return httpx.Response(
            201,
            json={
                "number": 42,
                "html_url": "https://github.com/smoketurner/ai-dlc/pull/42",
                "state": "open",
            },
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {
            "input": {
                "op": "open_pr",
                "repo": "smoketurner/ai-dlc",
                "base": "main",
                "head": "feature/foo",
                "title": "Add foo",
                "body": "Body",
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert out["op"] == "open_pr"
    assert out["result"] == {
        "pr_number": 42,
        "pr_url": "https://github.com/smoketurner/ai-dlc/pull/42",
        "state": "open",
    }
    assert len(seen) == 1
    assert seen[0].headers["authorization"] == "Bearer ghs_fake_install"


def test_comment_pr_posts_issue_comment(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/repos/smoketurner/ai-dlc/issues/42/comments"
        assert json.loads(request.content) == {"body": "looks good"}
        return httpx.Response(
            201,
            json={
                "id": 12345,
                "html_url": "https://github.com/smoketurner/ai-dlc/pull/42#issuecomment-12345",
                "body": "looks good",
            },
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {
            "input": {
                "op": "comment_pr",
                "repo": "smoketurner/ai-dlc",
                "pr_number": 42,
                "body": "looks good",
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert out["result"]["comment_id"] == 12345


def test_create_branch_uses_base_sha(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/repos/o/r/git/refs/heads/main":
            return httpx.Response(200, json={"object": {"sha": "abc123"}})
        if request.method == "POST" and request.url.path == "/repos/o/r/git/refs":
            assert json.loads(request.content) == {
                "ref": "refs/heads/feature/x",
                "sha": "abc123",
            }
            return httpx.Response(
                201,
                json={"ref": "refs/heads/feature/x", "object": {"sha": "abc123"}},
            )
        msg = f"unexpected request: {request.method} {request.url}"
        raise AssertionError(msg)

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {
            "input": {
                "op": "create_branch",
                "repo": "o/r",
                "branch": "feature/x",
                "base": "main",
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert out["result"] == {"branch": "feature/x", "ref": "refs/heads/feature/x", "sha": "abc123"}


def test_get_pr_returns_state_and_merged(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/repos/o/r/pulls/7"
        return httpx.Response(
            200,
            json={
                "number": 7,
                "html_url": "https://github.com/o/r/pull/7",
                "state": "closed",
                "merged": True,
                "title": "T",
                "head": {"sha": "deadbeef"},
            },
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {"input": {"op": "get_pr", "repo": "o/r", "pr_number": 7}},
        ctx(),
    )
    assert out["ok"] is True
    assert out["result"]["state"] == "closed"
    assert out["result"]["merged"] is True
    assert out["result"]["head_sha"] == "deadbeef"


def test_commit_files_walks_git_data_api(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    seen: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/repos/o/r/git/refs/heads/topic":
            return httpx.Response(200, json={"object": {"sha": "headcommit"}})
        if request.method == "GET" and request.url.path == "/repos/o/r/git/commits/headcommit":
            return httpx.Response(200, json={"tree": {"sha": "basetree"}})
        if request.method == "POST" and request.url.path == "/repos/o/r/git/blobs":
            body = json.loads(request.content)
            assert body["encoding"] == "base64"
            decoded = base64.b64decode(body["content"]).decode("utf-8")
            sha = f"blob-{decoded}"
            return httpx.Response(201, json={"sha": sha})
        if request.method == "POST" and request.url.path == "/repos/o/r/git/trees":
            body = json.loads(request.content)
            assert body["base_tree"] == "basetree"
            assert {e["path"] for e in body["tree"]} == {"a.txt", "b.txt"}
            assert all(e["mode"] == "100644" for e in body["tree"])
            return httpx.Response(201, json={"sha": "newtree"})
        if request.method == "POST" and request.url.path == "/repos/o/r/git/commits":
            body = json.loads(request.content)
            assert body["tree"] == "newtree"
            assert body["parents"] == ["headcommit"]
            assert body["message"] == "feat: add"
            return httpx.Response(201, json={"sha": "newcommit"})
        if request.method == "PATCH" and request.url.path == "/repos/o/r/git/refs/heads/topic":
            assert json.loads(request.content) == {"sha": "newcommit", "force": False}
            return httpx.Response(200, json={"object": {"sha": "newcommit"}})
        msg = f"unexpected request: {request.method} {request.url}"
        raise AssertionError(msg)

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {
            "input": {
                "op": "commit_files",
                "repo": "o/r",
                "branch": "topic",
                "message": "feat: add",
                "files": [
                    {"path": "a.txt", "content": "AA"},
                    {"path": "b.txt", "content": "BB"},
                ],
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert out["result"] == {"branch": "topic", "commit_sha": "newcommit", "files_written": 2}
    # 7 calls: get-ref, get-commit, blob x2, tree, commit, patch-ref.
    assert len(seen) == 7


def test_github_http_error_is_envelope(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {"input": {"op": "get_pr", "repo": "o/r", "pr_number": 99}},
        ctx(),
    )
    assert out["ok"] is False
    assert out["error"]["kind"] == "github_http_error"
    detail: dict[str, Any] = out["error"]["detail"]
    assert detail["status_code"] == 404
    assert detail["body"] == {"message": "Not Found"}


def test_comment_issue_posts_to_issues_endpoint(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/repos/o/r/issues/7/comments"
        assert json.loads(request.content) == {"body": "triage: deferred"}
        return httpx.Response(
            201,
            json={
                "id": 999,
                "html_url": "https://github.com/o/r/issues/7#issuecomment-999",
                "body": "triage: deferred",
            },
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {
            "input": {
                "op": "comment_issue",
                "repo": "o/r",
                "issue_number": 7,
                "body": "triage: deferred",
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert out["result"]["comment_id"] == 999


def test_label_issue_adds_labels_additively(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/repos/o/r/issues/7/labels"
        assert json.loads(request.content) == {"labels": ["aidlc:deferred"]}
        return httpx.Response(
            200,
            json=[
                {"name": "bug"},
                {"name": "aidlc:deferred"},
            ],
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {
            "input": {
                "op": "label_issue",
                "repo": "o/r",
                "issue_number": 7,
                "labels": ["aidlc:deferred"],
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert out["result"]["labels"] == ["bug", "aidlc:deferred"]


def test_get_file_returns_decoded_content(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    """Contents API base64-decoded into UTF-8 text."""
    body_text = "# Project memory\n\n- Always quote sources.\n"
    encoded = base64.b64encode(body_text.encode("utf-8")).decode("ascii")

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/repos/o/r/contents/AGENTS.md"
        assert dict(request.url.params) == {"ref": "main"}
        return httpx.Response(
            200,
            json={
                "name": "AGENTS.md",
                "path": "AGENTS.md",
                "sha": "abc123",
                "size": len(body_text),
                "encoding": "base64",
                "content": encoded,
            },
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {"input": {"op": "get_file", "repo": "o/r", "path": "AGENTS.md"}},
        ctx(),
    )
    assert out["ok"] is True
    assert out["result"] == {
        "exists": True,
        "content": body_text,
        "sha": "abc123",
        "ref": "main",
    }


def test_get_file_returns_exists_false_on_404(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    """Missing file is not an error — the caller wants to distinguish missing vs failed."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(404, json={"message": "Not Found"})

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {
            "input": {
                "op": "get_file",
                "repo": "o/r",
                "path": "missing.md",
                "ref": "feature-branch",
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert out["result"] == {"exists": False, "content": "", "sha": "", "ref": "feature-branch"}


def test_get_issue_returns_title_body_and_labels(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/repos/o/r/issues/7"
        return httpx.Response(
            200,
            json={
                "number": 7,
                "html_url": "https://github.com/o/r/issues/7",
                "title": "Add /version",
                "body": "Return container SHA from IMAGE_SHA.",
                "state": "open",
                "labels": [{"name": "aidlc:ready"}, {"name": "enhancement"}],
                "user": {"login": "alice"},
            },
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {"input": {"op": "get_issue", "repo": "o/r", "issue_number": 7}},
        ctx(),
    )
    assert out["ok"] is True
    assert out["result"]["title"] == "Add /version"
    assert out["result"]["labels"] == ["aidlc:ready", "enhancement"]
    assert out["result"]["user"] == "alice"


def test_create_issue_posts_to_issues_endpoint(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/repos/o/r/issues"
        assert json.loads(request.content) == {
            "title": "Adopt scoped rule files",
            "body": "Split MEMORY.md by directory.",
        }
        return httpx.Response(
            201,
            json={
                "number": 51,
                "html_url": "https://github.com/o/r/issues/51",
                "state": "open",
                "labels": [],
            },
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {
            "input": {
                "op": "create_issue",
                "repo": "o/r",
                "title": "Adopt scoped rule files",
                "body": "Split MEMORY.md by directory.",
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert out["op"] == "create_issue"
    assert out["result"] == {
        "issue_number": 51,
        "issue_url": "https://github.com/o/r/issues/51",
        "state": "open",
        "labels": [],
    }


def test_create_issue_includes_labels_when_set(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["labels"] == ["aidlc-spawned", "adopt"]
        return httpx.Response(
            201,
            json={
                "number": 52,
                "html_url": "https://github.com/o/r/issues/52",
                "state": "open",
                "labels": [
                    {"name": "aidlc-spawned"},
                    {"name": "adopt"},
                ],
            },
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {
            "input": {
                "op": "create_issue",
                "repo": "o/r",
                "title": "Adopt X",
                "body": "Body",
                "labels": ["aidlc-spawned", "adopt"],
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert out["result"]["labels"] == ["aidlc-spawned", "adopt"]


def test_create_issue_prepends_parent_backlink(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    captured: dict[str, str] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)["body"]
        return httpx.Response(
            201,
            json={
                "number": 53,
                "html_url": "https://github.com/o/r/issues/53",
                "state": "open",
                "labels": [],
            },
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {
            "input": {
                "op": "create_issue",
                "repo": "o/r",
                "title": "T",
                "body": "Detail.",
                "parent_issue_url": "https://github.com/o/r/issues/34",
                "requestor": "jplock",
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert captured["body"].startswith(
        "> Spawned from https://github.com/o/r/issues/34 by @jplock\n\n",
    )
    assert captured["body"].endswith("Detail.")


def test_create_issue_backlink_omits_attribution_without_requestor(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    captured: dict[str, str] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)["body"]
        return httpx.Response(
            201,
            json={
                "number": 54,
                "html_url": "https://github.com/o/r/issues/54",
                "state": "open",
                "labels": [],
            },
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {
            "input": {
                "op": "create_issue",
                "repo": "o/r",
                "title": "T",
                "body": "Body.",
                "parent_issue_url": "https://github.com/o/r/issues/34",
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert captured["body"].startswith("> Spawned from https://github.com/o/r/issues/34\n\n")


def test_create_issue_validates_repo_format() -> None:
    out = h.handler(
        {"input": {"op": "create_issue", "repo": "no-slash", "title": "T", "body": "B"}},
        ctx(),
    )
    assert out["ok"] is False
    assert out["error"]["kind"] == "validation_error"


def test_list_issue_comments_returns_chronological_thread(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/repos/o/r/issues/34/comments"
        assert request.url.params["per_page"] == "100"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 4408803319,
                    "user": {"login": "jplock", "type": "User"},
                    "body": "/aidlc go",
                    "created_at": "2026-05-08T18:12:29Z",
                    "updated_at": "2026-05-08T18:12:29Z",
                    "html_url": "https://github.com/o/r/issues/34#issuecomment-4408803319",
                },
                {
                    "id": 4409066197,
                    "user": {"login": "ai-dlc-dev", "type": "Bot"},
                    "body": "## Synthesis...",
                    "created_at": "2026-05-08T18:56:53Z",
                    "updated_at": "2026-05-08T18:56:53Z",
                    "html_url": "https://github.com/o/r/issues/34#issuecomment-4409066197",
                },
            ],
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {"input": {"op": "list_issue_comments", "repo": "o/r", "issue_number": 34}},
        ctx(),
    )
    assert out["ok"] is True
    comments = out["result"]["comments"]
    assert len(comments) == 2
    assert comments[0]["user"] == "jplock"
    assert comments[1]["user_type"] == "Bot"
    assert comments[1]["body"].startswith("## Synthesis")


def test_list_issue_comments_passes_since_filter(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    captured: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=[])

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {
            "input": {
                "op": "list_issue_comments",
                "repo": "o/r",
                "issue_number": 1,
                "since": "2026-05-08T00:00:00Z",
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert captured[0].url.params.get("since") == "2026-05-08T00:00:00Z"


def test_list_issues_filters_out_pull_requests(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/repos/o/r/issues"
        assert request.url.params["state"] == "open"
        assert request.url.params["labels"] == "aidlc:ready"
        return httpx.Response(
            200,
            json=[
                {
                    "number": 7,
                    "html_url": "https://github.com/o/r/issues/7",
                    "title": "Real issue",
                    "labels": [{"name": "aidlc:ready"}],
                },
                {
                    "number": 8,
                    "html_url": "https://github.com/o/r/pull/8",
                    "title": "A PR",
                    "labels": [{"name": "aidlc:ready"}],
                    "pull_request": {"url": "..."},
                },
            ],
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {
            "input": {
                "op": "list_issues",
                "repo": "o/r",
                "labels": ["aidlc:ready"],
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    issues = out["result"]["issues"]
    assert len(issues) == 1
    assert issues[0]["issue_number"] == 7


def test_get_pr_diff_returns_per_file_patches(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    """Happy path: one page of files, projected into the agent-facing shape."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/smoketurner/ai-dlc/pulls/42":
            return httpx.Response(200, json={"head": {"sha": "deadbeef"}})
        assert request.url.path == "/repos/smoketurner/ai-dlc/pulls/42/files"
        return httpx.Response(
            200,
            json=[
                {
                    "filename": "src/foo.py",
                    "status": "modified",
                    "additions": 3,
                    "deletions": 1,
                    "patch": "@@ -1 +1,3 @@\n-x\n+x\n+y\n+z",
                },
                {
                    "filename": "tests/test_foo.py",
                    "status": "added",
                    "additions": 10,
                    "deletions": 0,
                    "patch": "@@ -0,0 +1,10 @@\n+def test_foo():\n+    ...",
                },
            ],
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {
            "input": {
                "op": "get_pr_diff",
                "repo": "smoketurner/ai-dlc",
                "pr_number": 42,
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert out["op"] == "get_pr_diff"
    result = out["result"]
    assert result["head_sha"] == "deadbeef"
    assert result["files_truncated"] is False
    assert len(result["files"]) == 2
    assert result["files"][0]["filename"] == "src/foo.py"
    assert result["files"][0]["status"] == "modified"
    assert result["files"][0]["additions"] == 3
    assert result["files"][0]["deletions"] == 1
    assert result["files"][0]["truncated"] is False
    assert "+y" in result["files"][0]["patch"]


def test_get_pr_diff_truncates_oversized_patch(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    """A patch larger than GET_PR_DIFF_PATCH_TAIL_BYTES is tail-truncated."""
    big_patch = "@@ header @@\n" + ("x" * (h.GET_PR_DIFF_PATCH_TAIL_BYTES + 500))

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/1"):
            return httpx.Response(200, json={"head": {"sha": "abc"}})
        return httpx.Response(
            200,
            json=[
                {
                    "filename": "big.py",
                    "status": "modified",
                    "additions": 500,
                    "deletions": 0,
                    "patch": big_patch,
                }
            ],
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {"input": {"op": "get_pr_diff", "repo": "o/r", "pr_number": 1}},
        ctx(),
    )
    assert out["ok"] is True
    file_entry = out["result"]["files"][0]
    assert file_entry["truncated"] is True
    assert len(file_entry["patch"].encode("utf-8")) <= h.GET_PR_DIFF_PATCH_TAIL_BYTES


def test_get_pr_diff_marks_binary_file_with_no_patch(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    """A file the API returns without a patch (binary, too large) is flagged truncated."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/1"):
            return httpx.Response(200, json={"head": {"sha": "abc"}})
        return httpx.Response(
            200,
            json=[
                {
                    "filename": "logo.png",
                    "status": "added",
                    "additions": 0,
                    "deletions": 0,
                }
            ],
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {"input": {"op": "get_pr_diff", "repo": "o/r", "pr_number": 1}},
        ctx(),
    )
    assert out["ok"] is True
    file_entry = out["result"]["files"][0]
    assert file_entry["patch"] is None
    assert file_entry["truncated"] is True


def test_get_pr_diff_paginates_and_caps_files(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    """Pages through results and stops at GET_PR_DIFF_FILE_CAP files."""
    pages_seen: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/1"):
            return httpx.Response(200, json={"head": {"sha": "abc"}})
        page = int(request.url.params["page"])
        pages_seen.append(page)
        return httpx.Response(
            200,
            json=[
                {
                    "filename": f"page{page}-file{i}.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 0,
                    "patch": "@@ -1 +1 @@\n-a\n+b",
                }
                for i in range(h.GET_PR_DIFF_PER_PAGE)
            ],
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {"input": {"op": "get_pr_diff", "repo": "o/r", "pr_number": 1}},
        ctx(),
    )
    assert out["ok"] is True
    result = out["result"]
    assert len(result["files"]) == h.GET_PR_DIFF_FILE_CAP
    assert result["files_truncated"] is True
    assert pages_seen == [1, 2, 3]


def test_get_pr_diff_stops_when_short_page_returned(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    """A page shorter than per_page signals end-of-results — no further fetches."""
    pages_seen: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/1"):
            return httpx.Response(200, json={"head": {"sha": "abc"}})
        page = int(request.url.params["page"])
        pages_seen.append(page)
        # Return a single file (well under per_page=100), so the loop should stop.
        return httpx.Response(
            200,
            json=[
                {
                    "filename": "one.py",
                    "status": "added",
                    "additions": 1,
                    "deletions": 0,
                    "patch": "@@ +1 @@\n+x",
                }
            ],
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {"input": {"op": "get_pr_diff", "repo": "o/r", "pr_number": 1}},
        ctx(),
    )
    assert out["ok"] is True
    assert pages_seen == [1]
    assert out["result"]["files_truncated"] is False


def test_get_pr_diff_validates_repo_format() -> None:
    out = h.handler(
        {"input": {"op": "get_pr_diff", "repo": "no-slash", "pr_number": 1}},
        ctx(),
    )
    assert out["ok"] is False
    assert out["error"]["kind"] == "validation_error"


def test_get_pr_archive_url_returns_signed_codeload_url(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    """Happy path: GitHub 302s to codeload; we surface the Location header."""
    signed = "https://codeload.github.com/o/r/legacy.tar.gz/abc?token=SIGNED"

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/o/r/pulls/7":
            return httpx.Response(200, json={"head": {"sha": "abc"}})
        assert request.url.path == "/repos/o/r/tarball/abc"
        return httpx.Response(302, headers={"location": signed})

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {"input": {"op": "get_pr_archive_url", "repo": "o/r", "pr_number": 7}},
        ctx(),
    )
    assert out["ok"] is True
    assert out["op"] == "get_pr_archive_url"
    assert out["result"] == {"head_sha": "abc", "archive_url": signed}


def test_get_pr_archive_url_missing_pr_returns_error_envelope(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    """A 404 on the PR lookup propagates as a github_http_error envelope."""

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {"input": {"op": "get_pr_archive_url", "repo": "o/r", "pr_number": 999}},
        ctx(),
    )
    assert out["ok"] is False
    assert out["error"]["kind"] == "github_http_error"
    assert out["error"]["detail"]["status_code"] == 404


def test_get_pr_archive_url_validates_pr_number() -> None:
    out = h.handler(
        {"input": {"op": "get_pr_archive_url", "repo": "o/r", "pr_number": 0}},
        ctx(),
    )
    assert out["ok"] is False
    assert out["error"]["kind"] == "validation_error"


def test_list_pr_comments_calls_issue_comments_endpoint(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "GET"
        assert request.url.path == "/repos/o/r/issues/42/comments"
        assert request.url.params.get("per_page") == "100"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 1001,
                    "user": {"login": "alice", "type": "User"},
                    "body": "@ai-dlc[bot] please fix",
                    "created_at": "2026-05-06T12:00:00Z",
                    "updated_at": "2026-05-06T12:00:00Z",
                    "html_url": "https://github.com/o/r/pull/42#issuecomment-1001",
                },
                {
                    "id": 1002,
                    "user": {"login": "ai-dlc[bot]", "type": "Bot"},
                    "body": "ack",
                    "created_at": "2026-05-06T12:01:00Z",
                    "updated_at": "2026-05-06T12:01:00Z",
                    "html_url": "https://github.com/o/r/pull/42#issuecomment-1002",
                },
            ],
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {"input": {"op": "list_pr_comments", "repo": "o/r", "pr_number": 42}},
        ctx(),
    )
    assert out["ok"] is True
    comments = out["result"]["comments"]
    assert len(comments) == 2
    assert comments[0]["user"] == "alice"
    assert comments[0]["user_type"] == "User"
    assert comments[1]["user_type"] == "Bot"


def test_list_pr_comments_passes_since_filter(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    captured: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=[])

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {
            "input": {
                "op": "list_pr_comments",
                "repo": "o/r",
                "pr_number": 1,
                "since": "2026-01-01T00:00:00Z",
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert captured[0].url.params.get("since") == "2026-01-01T00:00:00Z"


def test_list_pr_review_comments_calls_pulls_endpoint(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/repos/o/r/pulls/42/comments"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 7,
                    "user": {"login": "alice", "type": "User"},
                    "body": "this branch is wrong",
                    "path": "src/handler.py",
                    "line": 42,
                    "original_line": 40,
                    "commit_id": "abcdef0",
                    "in_reply_to_id": None,
                    "pull_request_review_id": 999,
                    "created_at": "2026-05-06T12:00:00Z",
                    "html_url": "https://github.com/o/r/pull/42#discussion_r7",
                },
            ],
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {"input": {"op": "list_pr_review_comments", "repo": "o/r", "pr_number": 42}},
        ctx(),
    )
    assert out["ok"] is True
    comment = out["result"]["comments"][0]
    assert comment["path"] == "src/handler.py"
    assert comment["line"] == 42
    assert comment["commit_id"] == "abcdef0"
    assert comment["pull_request_review_id"] == 999


def test_reply_pr_review_comment_posts_to_replies_endpoint(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/repos/o/r/pulls/42/comments/7/replies"
        assert json.loads(request.content) == {"body": "fixed in next commit"}
        return httpx.Response(
            201,
            json={
                "id": 8,
                "html_url": "https://github.com/o/r/pull/42#discussion_r8",
                "in_reply_to_id": 7,
            },
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {
            "input": {
                "op": "reply_pr_review_comment",
                "repo": "o/r",
                "pr_number": 42,
                "comment_id": 7,
                "body": "fixed in next commit",
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert out["result"] == {
        "comment_id": 8,
        "comment_url": "https://github.com/o/r/pull/42#discussion_r8",
        "in_reply_to_id": 7,
    }


def test_reply_pr_review_comment_requires_body() -> None:
    out = h.handler(
        {
            "input": {
                "op": "reply_pr_review_comment",
                "repo": "o/r",
                "pr_number": 1,
                "comment_id": 1,
                "body": "",
            },
        },
        ctx(),
    )
    assert out["ok"] is False
    assert out["error"]["kind"] == "validation_error"


def test_list_check_runs_calls_commits_endpoint(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/repos/o/r/commits/abcdef0/check-runs"
        return httpx.Response(
            200,
            json={
                "total_count": 2,
                "check_runs": [
                    {
                        "id": 1,
                        "name": "CI / lint",
                        "status": "completed",
                        "conclusion": "success",
                        "html_url": "https://github.com/o/r/runs/1",
                        "details_url": "https://example.com/1",
                        "started_at": "2026-05-06T12:00:00Z",
                        "completed_at": "2026-05-06T12:01:00Z",
                        "output": {"title": "ok", "summary": "no issues"},
                    },
                    {
                        "id": 2,
                        "name": "CI / test",
                        "status": "completed",
                        "conclusion": "failure",
                        "html_url": "https://github.com/o/r/runs/2",
                        "details_url": "https://example.com/2",
                        "started_at": "2026-05-06T12:00:00Z",
                        "completed_at": "2026-05-06T12:02:00Z",
                        "output": {"title": "test failed", "summary": "1 of 50 failed"},
                    },
                ],
            },
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {"input": {"op": "list_check_runs", "repo": "o/r", "ref": "abcdef0"}},
        ctx(),
    )
    assert out["ok"] is True
    runs = out["result"]["check_runs"]
    assert len(runs) == 2
    assert runs[1]["conclusion"] == "failure"
    assert runs[1]["output"]["summary"] == "1 of 50 failed"


def test_list_check_runs_filters_by_conclusion(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "total_count": 3,
                "check_runs": [
                    {"id": 1, "name": "a", "status": "completed", "conclusion": "success"},
                    {"id": 2, "name": "b", "status": "completed", "conclusion": "failure"},
                    {"id": 3, "name": "c", "status": "completed", "conclusion": "timed_out"},
                ],
            },
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {
            "input": {
                "op": "list_check_runs",
                "repo": "o/r",
                "ref": "main",
                "filter_conclusions": ["failure", "timed_out"],
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    runs = out["result"]["check_runs"]
    assert len(runs) == 2
    assert {r["id"] for r in runs} == {2, 3}


def test_list_check_runs_truncates_long_summary(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    long_summary = "x" * 10_000

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "total_count": 1,
                "check_runs": [
                    {
                        "id": 1,
                        "name": "CI",
                        "status": "completed",
                        "conclusion": "failure",
                        "output": {"title": "fail", "summary": long_summary},
                    },
                ],
            },
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {"input": {"op": "list_check_runs", "repo": "o/r", "ref": "main"}},
        ctx(),
    )
    assert out["ok"] is True
    assert len(out["result"]["check_runs"][0]["output"]["summary"]) == 4096


def test_get_check_state_returns_passed_when_all_success(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    """Every run + suite at the PR's head sha conclusion=success → passed."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/o/r/pulls/42":
            return httpx.Response(200, json={"head": {"sha": "abcdef0"}})
        if request.url.path == "/repos/o/r/commits/abcdef0/check-runs":
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {"id": 1, "status": "completed", "conclusion": "success"},
                        {"id": 2, "status": "completed", "conclusion": "success"},
                    ],
                },
            )
        if request.url.path == "/repos/o/r/commits/abcdef0/check-suites":
            return httpx.Response(
                200,
                json={
                    "check_suites": [
                        {"id": 9, "status": "completed", "conclusion": "success"},
                    ],
                },
            )
        msg = f"unexpected request: {request.method} {request.url.path}"
        raise AssertionError(msg)

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {"input": {"op": "get_check_state", "repo": "o/r", "pr_number": 42}},
        ctx(),
    )
    assert out["ok"] is True
    assert out["op"] == "get_check_state"
    assert out["result"]["state"] == "passed"
    assert out["result"]["head_sha"] == "abcdef0"
    assert out["result"]["run_count"] == 2
    assert out["result"]["suite_count"] == 1


def test_get_check_state_returns_failed_on_any_failure(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    """One failed run trips the aggregate to failed regardless of others."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/o/r/pulls/1":
            return httpx.Response(200, json={"head": {"sha": "abc"}})
        if request.url.path == "/repos/o/r/commits/abc/check-runs":
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {"id": 1, "status": "completed", "conclusion": "success"},
                        {"id": 2, "status": "completed", "conclusion": "failure"},
                    ],
                },
            )
        if request.url.path == "/repos/o/r/commits/abc/check-suites":
            return httpx.Response(200, json={"check_suites": []})
        msg = f"unexpected request: {request.method} {request.url.path}"
        raise AssertionError(msg)

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {"input": {"op": "get_check_state", "repo": "o/r", "pr_number": 1}},
        ctx(),
    )
    assert out["ok"] is True
    assert out["result"]["state"] == "failed"


@pytest.mark.parametrize(
    "conclusion",
    ["failure", "timed_out", "cancelled", "action_required", "stale"],
)
def test_get_check_state_treats_all_failure_modes_as_failed(
    patch_client: Callable[[httpx.MockTransport], None],
    conclusion: str,
) -> None:
    """Every conclusion in CHECK_FAILED_CONCLUSIONS aggregates to failed."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/1"):
            return httpx.Response(200, json={"head": {"sha": "abc"}})
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(
                200,
                json={
                    "check_runs": [{"id": 1, "status": "completed", "conclusion": conclusion}],
                },
            )
        return httpx.Response(200, json={"check_suites": []})

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {"input": {"op": "get_check_state", "repo": "o/r", "pr_number": 1}},
        ctx(),
    )
    assert out["ok"] is True
    assert out["result"]["state"] == "failed"


def test_get_check_state_returns_pending_on_incomplete_run(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    """Any run still in_progress / queued → pending."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/1"):
            return httpx.Response(200, json={"head": {"sha": "abc"}})
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {"id": 1, "status": "completed", "conclusion": "success"},
                        {"id": 2, "status": "in_progress", "conclusion": None},
                    ],
                },
            )
        return httpx.Response(200, json={"check_suites": []})

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {"input": {"op": "get_check_state", "repo": "o/r", "pr_number": 1}},
        ctx(),
    )
    assert out["ok"] is True
    assert out["result"]["state"] == "pending"


def test_get_check_state_returns_pending_when_no_checks(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    """Empty check lists → pending (no reports yet)."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/1"):
            return httpx.Response(200, json={"head": {"sha": "abc"}})
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": []})
        return httpx.Response(200, json={"check_suites": []})

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {"input": {"op": "get_check_state", "repo": "o/r", "pr_number": 1}},
        ctx(),
    )
    assert out["ok"] is True
    assert out["result"]["state"] == "pending"


def test_get_check_state_validates_repo_format() -> None:
    out = h.handler(
        {"input": {"op": "get_check_state", "repo": "no-slash", "pr_number": 1}},
        ctx(),
    )
    assert out["ok"] is False
    assert out["error"]["kind"] == "validation_error"


def test_get_check_state_failure_in_suite_wins_over_passing_run(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    """Suite-level failure trips the aggregate even when runs are green."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/1"):
            return httpx.Response(200, json={"head": {"sha": "abc"}})
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(
                200,
                json={
                    "check_runs": [{"id": 1, "status": "completed", "conclusion": "success"}],
                },
            )
        return httpx.Response(
            200,
            json={
                "check_suites": [
                    {"id": 9, "status": "completed", "conclusion": "failure"},
                ],
            },
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {"input": {"op": "get_check_state", "repo": "o/r", "pr_number": 1}},
        ctx(),
    )
    assert out["ok"] is True
    assert out["result"]["state"] == "failed"


def test_open_spec_pr_op_no_longer_registered() -> None:
    """The legacy spec PR opcode was removed in the single-PR-per-issue refactor."""
    out = h.handler({"input": {"op": "open_spec_pr"}}, ctx())
    assert out["ok"] is False
    assert out["error"]["kind"] == "unknown_op"
