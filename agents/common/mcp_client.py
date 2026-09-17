"""MCP-over-Gateway client for the tenant-scoped retrieval tool.

The agent never invokes the Lambda directly. Gateway validates the same Cognito
access token that Runtime accepted, while the retrieval Lambda validates the
opaque HMAC capability supplied as the tool's session_token argument.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from typing import Any

GATEWAY_URL = os.environ["GATEWAY_URL"].rstrip("/")
# The AgentCore Gateway exposes each target's tool namespaced as
# "<target-name>___<tool-name>". The retrieval target is registered as
# "retrieval-tool" with an inline tool "retrieval", so the callable name is
# "retrieval-tool___retrieval". Calling the bare "retrieval" returns JSON-RPC
# -32602 "Unknown tool". Override via env only if the target name changes.
RETRIEVAL_TOOL_NAME = os.environ.get("RETRIEVAL_TOOL_NAME", "retrieval-tool___retrieval")
# The AgentCore Gateway MCP endpoint negotiates a specific protocol version and
# rejects anything else with JSON-RPC -32600 "Unsupported protocol version".
# It currently supports 2025-03-26, so default to that. Override only if the
# Gateway's supported version changes.
MCP_PROTOCOL_VERSION = os.environ.get("MCP_PROTOCOL_VERSION", "2025-03-26")


class GatewayMcpError(RuntimeError):
    """Gateway rejected or could not execute an MCP request."""


def _decode_sse(body: str) -> dict[str, Any]:
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise GatewayMcpError("Gateway returned an empty SSE response")


def _post(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        GATEWAY_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8")
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise GatewayMcpError(f"Gateway rejected MCP request: HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise GatewayMcpError("Gateway MCP request failed") from exc

    result = _decode_sse(body) if "text/event-stream" in content_type else json.loads(body)
    if "error" in result:
        raise GatewayMcpError(f"Gateway MCP error: {result['error']}")
    return result


def _request(method: str, params: dict[str, Any], access_token: str) -> dict[str, Any]:
    return _post(
        {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
        },
        access_token,
    )


def _tool_result(response: dict[str, Any]) -> dict[str, Any]:
    result = response.get("result", response)
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        text = "".join(
            item.get("text", "")
            for item in result["content"]
            if isinstance(item, dict) and item.get("type") == "text"
        )
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise GatewayMcpError("retrieval tool returned non-JSON content") from exc
            if isinstance(parsed, dict):
                return parsed
    if isinstance(result, dict) and {"results", "count"}.intersection(result):
        return result
    raise GatewayMcpError("retrieval tool response has no structured result")


def retrieve(
    *,
    query: str,
    session_token: str,
    access_token: str,
    permit_id: str | None = None,
) -> dict[str, Any]:
    """Initialize an MCP session and call the single in-scope retrieval tool."""
    _request(
        "initialize",
        {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "agentcore-compliance-demo-runtime", "version": "1"},
        },
        access_token,
    )
    arguments: dict[str, Any] = {"query": query, "session_token": session_token}
    if permit_id:
        arguments["permit_id"] = permit_id
    return _tool_result(_request("tools/call", {"name": RETRIEVAL_TOOL_NAME, "arguments": arguments}, access_token))
