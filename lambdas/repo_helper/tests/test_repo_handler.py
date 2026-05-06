"""Unit tests for the repo_helper Lambda handler.

The GitHub API is mocked via ``httpx.MockTransport``. ``handler.github_client``
is replaced with a client whose transport routes requests to a callback that
asserts URL/method/body shape and returns canned JSON. The auth module's
``installation_token_for_repo`` is monkeypatched so tests never touch
Secrets Manager or the App-JWT machinery.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import repo_helper.auth as auth_mod
import repo_helper.handler as h
from aws_lambda_powertools.utilities.typing import LambdaContext


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
