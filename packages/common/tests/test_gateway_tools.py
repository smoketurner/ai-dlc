"""Tests for ``common.gateway_tools``.

Exercise the gateway MCP client factory and the post-agent
``call_gateway_tool`` helper without standing up a real MCP server or
AgentCore Identity. We patch ``streamablehttp_client`` and
``fetch_gateway_token`` to verify the wiring.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from common import gateway_tools

GATEWAY_URL = "https://gateway-x.example.com/mcp"
PROVIDER_NAME = "ai-dlc-dev-cognito-gateway-m2m"
SCOPE = "https://ai-dlc.dev/gateway/invoke"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default env: gateway URL, provider name, and scope all set."""
    monkeypatch.setenv("AIDLC_AGENT_GATEWAY_URL", GATEWAY_URL)
    monkeypatch.setenv("AIDLC_GATEWAY_OAUTH_PROVIDER_NAME", PROVIDER_NAME)
    monkeypatch.setenv("AIDLC_GATEWAY_OAUTH_SCOPE", SCOPE)


def test_gateway_url_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIDLC_AGENT_GATEWAY_URL", raising=False)
    with pytest.raises(EnvironmentError, match="AIDLC_AGENT_GATEWAY_URL"):
        gateway_tools.gateway_url()


def test_fetch_gateway_token_raises_when_provider_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIDLC_GATEWAY_OAUTH_PROVIDER_NAME", raising=False)
    with pytest.raises(EnvironmentError, match="AIDLC_GATEWAY_OAUTH_PROVIDER_NAME"):
        gateway_tools.fetch_gateway_token()


def test_transport_uses_explicit_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """When access_token= is passed, the transport uses it verbatim."""
    captured: dict[str, Any] = {}

    def fake_streamablehttp(*, url: str, headers: dict[str, str]) -> Any:
        captured["url"] = url
        captured["headers"] = headers
        return MagicMock()

    monkeypatch.setattr(gateway_tools, "streamablehttp_client", fake_streamablehttp)
    fetch = MagicMock()
    monkeypatch.setattr(gateway_tools, "fetch_gateway_token", fetch)

    client = gateway_tools.gateway_mcp_client(access_token="tok-explicit")  # noqa: S106
    client._transport_callable()

    assert captured["url"] == GATEWAY_URL
    assert captured["headers"] == {"Authorization": "Bearer tok-explicit"}
    fetch.assert_not_called()


def test_transport_falls_back_to_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an explicit token, the transport calls fetch_gateway_token()."""
    captured: dict[str, Any] = {}

    def fake_streamablehttp(*, url: str, headers: dict[str, str]) -> Any:
        captured["headers"] = headers
        return MagicMock()

    fetch = MagicMock(return_value="tok-fetched")
    monkeypatch.setattr(gateway_tools, "streamablehttp_client", fake_streamablehttp)
    monkeypatch.setattr(gateway_tools, "fetch_gateway_token", fetch)

    client = gateway_tools.gateway_mcp_client()
    client._transport_callable()

    assert captured["headers"] == {"Authorization": "Bearer tok-fetched"}
    fetch.assert_called_once()


def test_gateway_tools_returns_catalogue() -> None:
    """gateway_tools(client) returns whatever list_tools_sync emits, as a list."""
    fake_tool = object()
    client = MagicMock()
    client.list_tools_sync.return_value = [fake_tool]

    out = gateway_tools.gateway_tools(client)

    assert out == [fake_tool]
    client.list_tools_sync.assert_called_once_with()


def test_call_gateway_tool_uses_call_tool_sync() -> None:
    """call_gateway_tool dispatches to MCPClient.call_tool_sync with a fresh use-id."""
    client = MagicMock()
    sentinel = object()
    client.call_tool_sync.return_value = sentinel

    out = gateway_tools.call_gateway_tool(
        client,
        name="artifact_tool",
        arguments={"op": "put_artifact", "key": "k", "content": "c"},
    )

    assert out is sentinel
    kwargs = client.call_tool_sync.call_args.kwargs
    assert kwargs["name"] == "artifact_tool"
    assert kwargs["arguments"] == {"op": "put_artifact", "key": "k", "content": "c"}
    assert isinstance(kwargs["tool_use_id"], str)
    assert len(kwargs["tool_use_id"]) > 0


def test_call_gateway_tool_strips_none_arguments() -> None:
    """``None``-valued keys must be removed before the gateway validates the schema.

    AgentCore Gateway rejects ``null`` for fields declared ``type: string``,
    so callers pass optional fields through and rely on this filter.
    """
    client = MagicMock()
    client.call_tool_sync.return_value = object()

    gateway_tools.call_gateway_tool(
        client,
        name="repo-helper___repo_helper",
        arguments={
            "op": "open_pr",
            "repo": "owner/name",
            "requestor_sub": None,
            "head": "feature",
            "base": "main",
        },
    )

    kwargs = client.call_tool_sync.call_args.kwargs
    assert "requestor_sub" not in kwargs["arguments"]
    assert kwargs["arguments"] == {
        "op": "open_pr",
        "repo": "owner/name",
        "head": "feature",
        "base": "main",
    }


def test_extract_envelope_prefers_structured_content() -> None:
    """When structuredContent is present, the helper returns it directly."""
    envelope = {"ok": True, "op": "get_artifact", "result": {"key": "k", "content": "c"}}
    out = gateway_tools.extract_envelope({"structuredContent": envelope, "content": []})
    assert out is envelope


def test_extract_envelope_falls_back_to_text_block() -> None:
    """Servers that don't emit structuredContent still surface the dict as JSON text."""
    envelope = {"ok": True, "op": "get_artifact", "result": {"key": "k", "content": "c"}}
    out = gateway_tools.extract_envelope({"content": [{"text": json.dumps(envelope)}]})
    assert out == envelope


def test_extract_envelope_raises_on_garbage() -> None:
    """An empty result envelope produces a clear error."""
    with pytest.raises(RuntimeError, match="no parseable content"):
        gateway_tools.extract_envelope({"content": []})


def test_extract_envelope_skips_empty_text_blocks() -> None:
    """A leading empty text block must not crash json parsing — keep looking."""
    envelope = {"ok": True, "op": "get_artifact", "result": {"content": "x"}}
    out = gateway_tools.extract_envelope(
        {"content": [{"text": ""}, {"text": json.dumps(envelope)}]},
    )
    assert out == envelope


def test_extract_envelope_handles_string_structured_content() -> None:
    """Some gateway configs return ``structuredContent`` as a JSON string."""
    envelope = {"ok": True, "op": "get_artifact", "result": {"content": "x"}}
    out = gateway_tools.extract_envelope({"structuredContent": json.dumps(envelope)})
    assert out == envelope


def test_extract_envelope_raises_on_is_error() -> None:
    """``isError`` results carry the upstream message verbatim, not a JSON envelope."""
    err_result = {
        "isError": True,
        "content": [{"text": "AccessDeniedException: not authorized for s3:GetObject"}],
    }
    with pytest.raises(
        RuntimeError,
        match=r"isError=true.*AccessDeniedException",
    ):
        gateway_tools.extract_envelope(err_result)


def test_extract_envelope_skips_non_json_text_blocks() -> None:
    """Plain-text blocks (markdown body, error string) are skipped, not raised through."""
    envelope = {"ok": True, "op": "get_artifact", "result": {"content": "# Plan"}}
    out = gateway_tools.extract_envelope(
        {"content": [{"text": "# Plan body that isn't JSON"}, {"text": json.dumps(envelope)}]},
    )
    assert out == envelope


def test_extract_envelope_raises_when_all_text_blocks_unparseable() -> None:
    """If every block is non-JSON, surface the raw result in the RuntimeError."""
    with pytest.raises(RuntimeError, match="no parseable content"):
        gateway_tools.extract_envelope({"content": [{"text": "not json"}, {"text": ""}]})
