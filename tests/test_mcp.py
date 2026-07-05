"""MCP (Model Context Protocol) server over stdio."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _mcp_roundtrip(base_url, requests, token=None):
    """Feed line-delimited JSON-RPC requests to `agentchat mcp` and collect
    the JSON responses from stdout."""
    cmd = [sys.executable, "-m", "agentchat", "mcp", "--api", base_url]
    if token:
        cmd += ["--token", token]
    stdin = "".join(json.dumps(r) + "\n" for r in requests)
    proc = subprocess.run(
        cmd,
        input=stdin,
        cwd=str(REPO_ROOT),
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=20,
    )
    responses = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            responses.append(json.loads(line))
    return responses


def test_mcp_initialize_and_tools_list(server):
    responses = _mcp_roundtrip(
        server["base_url"],
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ],
    )
    by_id = {r["id"]: r for r in responses}
    assert by_id[1]["result"]["serverInfo"]["name"] == "agentchat"
    tools = {t["name"] for t in by_id[2]["result"]["tools"]}
    assert {"whoami", "list_threads", "read_messages", "send_message", "search"} <= tools


def test_mcp_tool_call_whoami(server, register):
    client, _ = register("mcpuser")
    responses = _mcp_roundtrip(
        server["base_url"],
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "whoami", "arguments": {}},
            },
        ],
        token=client.token,
    )
    by_id = {r["id"]: r for r in responses}
    result = by_id[2]["result"]
    text = json.dumps(result)
    assert client.username in text
