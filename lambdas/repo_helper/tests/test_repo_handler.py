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


def test_mint_clone_token_returns_authenticated_url_with_install_token(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    """Default path: no requestor_sub → installation token embedded in clone URL."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/repos/smoketurner/ai-dlc/pulls/42"
        return httpx.Response(
            200,
            json={
                "number": 42,
                "html_url": "https://github.com/smoketurner/ai-dlc/pull/42",
                "state": "open",
                "merged": False,
                "title": "T",
                "head": {"sha": "deadbeef"},
            },
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {
            "input": {
                "op": "mint_clone_token",
                "repo": "smoketurner/ai-dlc",
                "pr_number": 42,
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert out["op"] == "mint_clone_token"
    assert out["result"] == {
        "clone_url": "https://x-access-token:ghs_fake_install@github.com/smoketurner/ai-dlc.git",
        "head_sha": "deadbeef",
    }


def test_mint_clone_token_uses_user_obo_when_requestor_sub_set(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    """When requestor_sub is supplied, the embedded token is the user-OBO bearer."""

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "number": 7,
                "state": "open",
                "merged": False,
                "title": "T",
                "head": {"sha": "abc123"},
            },
        )

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {
            "input": {
                "op": "mint_clone_token",
                "repo": "o/r",
                "pr_number": 7,
                "requestor_sub": "cognito-sub-xyz",
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert out["result"]["clone_url"] == "https://x-access-token:ghs_fake_user@github.com/o/r.git"
    assert out["result"]["head_sha"] == "abc123"


def test_mint_clone_token_missing_pr_returns_error_envelope(
    patch_client: Callable[[httpx.MockTransport], None],
) -> None:
    """A 404 from GitHub propagates as a github_http_error envelope, not an exception."""

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    patch_client(httpx.MockTransport(respond))
    out = h.handler(
        {"input": {"op": "mint_clone_token", "repo": "o/r", "pr_number": 999}},
        ctx(),
    )
    assert out["ok"] is False
    assert out["error"]["kind"] == "github_http_error"
    assert out["error"]["detail"]["status_code"] == 404


def test_mint_clone_token_validates_repo_format() -> None:
    out = h.handler(
        {"input": {"op": "mint_clone_token", "repo": "no-slash", "pr_number": 1}},
        ctx(),
    )
    assert out["ok"] is False
    assert out["error"]["kind"] == "validation_error"


def test_mint_clone_token_requires_positive_pr_number() -> None:
    out = h.handler(
        {"input": {"op": "mint_clone_token", "repo": "o/r", "pr_number": 0}},
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


SPEC_DOCS_S3 = {
    "specs/add-healthz/requirements.md": b"# Requirements\n",
    "specs/add-healthz/design.md": b"# Design\n",
    "specs/add-healthz/tasks.md": b"# Tasks\n",
}


class _FakeS3Body:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def read(self) -> bytes:
        return self.content


class _FakeS3:
    def __init__(self, docs: dict[str, bytes]) -> None:
        self.docs = docs
        self.requested: list[tuple[str, str]] = []

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self.requested.append((Bucket, Key))
        return {"Body": _FakeS3Body(self.docs[Key])}


def _miss_404(_: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={"message": "Not Found"})


def _ok_main_ref(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"object": {"sha": "mainsha"}})


def _ok_base_commit(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"tree": {"sha": "basetree"}})


def _ok_blob(_: httpx.Request) -> httpx.Response:
    return httpx.Response(201, json={"sha": "blob"})


def _ok_new_tree(_: httpx.Request) -> httpx.Response:
    return httpx.Response(201, json={"sha": "newtree"})


def _ok_new_commit(_: httpx.Request) -> httpx.Response:
    return httpx.Response(201, json={"sha": "newcommit"})


def _ok_patched_ref(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"object": {"sha": "newcommit"}})


def _spec_pr_routes(
    *, branch: str, base: str, slug: str, run_id: str
) -> dict[
    tuple[str, str],
    Callable[[httpx.Request], httpx.Response],
]:
    """Build the GitHub-API responder map ``open_spec_pr`` should walk.

    Lookup misses the branch first → falls back to ``base`` ref → tree /
    commit / ref dance → PR open. ``create_branch_check`` and
    ``open_pr_check`` assert their inbound shapes inline.
    """
    new_branch_ref = f"/repos/o/r/git/refs/heads/{branch}"
    spec_files = {f"docs/specs/{slug}/{n}.md" for n in ("requirements", "design", "tasks")}

    def create_branch_check(req: httpx.Request) -> httpx.Response:
        assert json.loads(req.content) == {"ref": f"refs/heads/{branch}", "sha": "mainsha"}
        return httpx.Response(201, json={"object": {"sha": "mainsha"}})

    def tree_check(req: httpx.Request) -> httpx.Response:
        assert {e["path"] for e in json.loads(req.content)["tree"]} == spec_files
        return _ok_new_tree(req)

    def commit_check(req: httpx.Request) -> httpx.Response:
        assert json.loads(req.content)["message"] == f"spec: {slug}"
        return _ok_new_commit(req)

    def open_pr_check(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["head"] == branch
        assert body["base"] == base
        assert run_id in body["body"]
        return httpx.Response(
            201,
            json={
                "number": 5,
                "html_url": "https://github.com/o/r/pull/5",
                "state": "open",
            },
        )

    return {
        ("GET", new_branch_ref): _miss_404,
        ("GET", f"/repos/o/r/git/refs/heads/{base}"): _ok_main_ref,
        ("POST", "/repos/o/r/git/refs"): create_branch_check,
        ("GET", "/repos/o/r/git/commits/mainsha"): _ok_base_commit,
        ("POST", "/repos/o/r/git/blobs"): _ok_blob,
        ("POST", "/repos/o/r/git/trees"): tree_check,
        ("POST", "/repos/o/r/git/commits"): commit_check,
        ("PATCH", new_branch_ref): _ok_patched_ref,
        ("POST", "/repos/o/r/pulls"): open_pr_check,
    }


def _route_with(
    routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]],
) -> Callable[[httpx.Request], httpx.Response]:
    """Adapter that routes httpx requests via ``(method, path) → handler``."""

    def respond(request: httpx.Request) -> httpx.Response:
        handler = routes.get((request.method, request.url.path))
        if handler is None:
            msg = f"unexpected request: {request.method} {request.url.path}"
            raise AssertionError(msg)
        return handler(request)

    return respond


def test_open_spec_pr_reads_s3_branches_commits_and_opens(
    patch_client: Callable[[httpx.MockTransport], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compound op walks: read S3 → branch → blobs → tree → commit → ref → PR."""
    monkeypatch.setenv("AIDLC_ARTIFACTS_BUCKET", "test-artifacts")
    fake_s3 = _FakeS3(SPEC_DOCS_S3)
    monkeypatch.setattr(h, "s3_client", lambda: fake_s3)

    routes = _spec_pr_routes(
        branch="aidlc/spec/add-healthz",
        base="main",
        slug="add-healthz",
        run_id="run-xyz",
    )
    patch_client(httpx.MockTransport(_route_with(routes)))
    out = h.handler(
        {
            "input": {
                "op": "open_spec_pr",
                "repo": "o/r",
                "spec_slug": "add-healthz",
                "spec_s3_prefix": "specs/add-healthz/",
                "run_id": "run-xyz",
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert out["op"] == "open_spec_pr"
    assert out["result"]["pr_url"] == "https://github.com/o/r/pull/5"
    assert out["result"]["pr_number"] == 5
    assert out["result"]["branch"] == "aidlc/spec/add-healthz"
    assert {key for _, key in fake_s3.requested} == set(SPEC_DOCS_S3)


def test_open_spec_pr_includes_source_issue_url_in_body(
    patch_client: Callable[[httpx.MockTransport], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the run was triggered by an issue, the URL goes into the PR body.

    Gives GitHub a backlink between the source issue and the spec PR
    without using a closing keyword (the issue stays open until task
    PRs are merged).
    """
    monkeypatch.setenv("AIDLC_ARTIFACTS_BUCKET", "test-artifacts")
    monkeypatch.setattr(h, "s3_client", lambda: _FakeS3(SPEC_DOCS_S3))
    captured: dict[str, str] = {}

    def open_pr_capture(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)["body"]
        return httpx.Response(
            201,
            json={"number": 5, "html_url": "https://github.com/o/r/pull/5", "state": "open"},
        )

    routes = _spec_pr_routes(
        branch="aidlc/spec/add-healthz",
        base="main",
        slug="add-healthz",
        run_id="run-xyz",
    )
    routes[("POST", "/repos/o/r/pulls")] = open_pr_capture
    patch_client(httpx.MockTransport(_route_with(routes)))
    out = h.handler(
        {
            "input": {
                "op": "open_spec_pr",
                "repo": "o/r",
                "spec_slug": "add-healthz",
                "spec_s3_prefix": "specs/add-healthz/",
                "run_id": "run-xyz",
                "source_issue_url": "https://github.com/o/r/issues/33",
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert "Source issue: https://github.com/o/r/issues/33" in captured["body"]
    # No closing keyword — the issue stays open until task PRs land.
    assert "fixes" not in captured["body"].lower()
    assert "closes" not in captured["body"].lower()


def _existing_branch_routes() -> dict[
    tuple[str, str],
    Callable[[httpx.Request], httpx.Response],
]:
    """Routes for the ``branch already exists`` happy path."""
    branch_ref = "/repos/o/r/git/refs/heads/aidlc/spec/x"
    return {
        ("GET", branch_ref): lambda _: httpx.Response(
            200,
            json={"object": {"sha": "branchhead"}},
        ),
        ("GET", "/repos/o/r/git/commits/branchhead"): lambda _: httpx.Response(
            200,
            json={"tree": {"sha": "tree"}},
        ),
        ("POST", "/repos/o/r/git/blobs"): lambda _: httpx.Response(201, json={"sha": "blob"}),
        ("POST", "/repos/o/r/git/trees"): lambda _: httpx.Response(201, json={"sha": "newtree"}),
        ("POST", "/repos/o/r/git/commits"): lambda _: httpx.Response(
            201,
            json={"sha": "newcommit"},
        ),
        ("PATCH", branch_ref): lambda _: httpx.Response(
            200,
            json={"object": {"sha": "newcommit"}},
        ),
        ("POST", "/repos/o/r/pulls"): lambda _: httpx.Response(
            201,
            json={
                "number": 7,
                "html_url": "https://github.com/o/r/pull/7",
                "state": "open",
            },
        ),
    }


def test_open_spec_pr_short_circuits_when_tree_unchanged(
    patch_client: Callable[[httpx.MockTransport], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``create_tree`` returns the base tree SHA, skip commit + PR.

    A re-run that produces docs identical to a previously-merged spec
    yields the same tree on the GitHub side. ``open_spec_pr`` returns
    ``no_change: true`` so the state-router can advance straight to
    ``spec_approved`` instead of opening a 0-file-change PR.
    """
    monkeypatch.setenv("AIDLC_ARTIFACTS_BUCKET", "test-artifacts")
    monkeypatch.setattr(h, "s3_client", lambda: _FakeS3(SPEC_DOCS_S3))

    def tree_returns_base(_: httpx.Request) -> httpx.Response:
        # Same SHA as ``_ok_base_commit`` returns for the tree → no diff.
        return httpx.Response(201, json={"sha": "basetree"})

    routes = _spec_pr_routes(
        branch="aidlc/spec/add-healthz",
        base="main",
        slug="add-healthz",
        run_id="run-xyz",
    )
    routes[("POST", "/repos/o/r/git/trees")] = tree_returns_base

    def fail_if_called(req: httpx.Request) -> httpx.Response:
        msg = f"unexpected request after no_change short-circuit: {req.method} {req.url.path}"
        raise AssertionError(msg)

    routes[("POST", "/repos/o/r/git/commits")] = fail_if_called
    routes[("PATCH", "/repos/o/r/git/refs/heads/aidlc/spec/add-healthz")] = fail_if_called
    routes[("POST", "/repos/o/r/pulls")] = fail_if_called

    patch_client(httpx.MockTransport(_route_with(routes)))
    out = h.handler(
        {
            "input": {
                "op": "open_spec_pr",
                "repo": "o/r",
                "spec_slug": "add-healthz",
                "spec_s3_prefix": "specs/add-healthz/",
                "run_id": "run-xyz",
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert out["result"] == {
        "no_change": True,
        "spec_slug": "add-healthz",
        "branch": "aidlc/spec/add-healthz",
        "base_commit_sha": "mainsha",
    }


def test_open_spec_pr_reuses_existing_branch(
    patch_client: Callable[[httpx.MockTransport], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the branch ref already exists, ``open_spec_pr`` reuses it."""
    monkeypatch.setenv("AIDLC_ARTIFACTS_BUCKET", "test-artifacts")

    class FakeBody:
        def read(self) -> bytes:
            return b"# doc\n"

    class FakeS3:
        def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
            return {"Body": FakeBody()}

    monkeypatch.setattr(h, "s3_client", FakeS3)
    patch_client(httpx.MockTransport(_route_with(_existing_branch_routes())))
    out = h.handler(
        {
            "input": {
                "op": "open_spec_pr",
                "repo": "o/r",
                "spec_slug": "x",
                "spec_s3_prefix": "specs/x/",
                "run_id": "rid",
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert out["result"]["pr_number"] == 7
