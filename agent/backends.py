"""The two execution paths the agent can take — kept strictly separate:

* ``PolicyMcpClient``  — MCP client -> MCP server (policy search). Talks the MCP
  protocol over stdio (the server runs as a subprocess) or Streamable HTTP.
* ``TuitionGatewayClient`` — HTTP client -> API Gateway -> Tuition REST API.
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent


class PolicyMcpClient:
    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        self.tools: list[Any] = []

    async def start(self, url: str | None = None) -> None:
        """Spawns the MCP server over stdio, or connects to ``url`` (Streamable HTTP)."""
        self._stack = AsyncExitStack()
        url = url or os.environ.get("MCP_SERVER_URL")
        if url:
            from mcp.client.streamable_http import streamable_http_client
            read, write, *_ = await self._stack.enter_async_context(streamable_http_client(url))
        else:
            params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server"],
                                           cwd=str(ROOT), env={**os.environ, "PYTHONPATH": str(ROOT)})
            read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        self.tools = (await self.session.list_tools()).tools

    async def call(self, name: str, arguments: dict) -> dict:
        assert self.session is not None, "MCP client not started"
        result = await self.session.call_tool(name, arguments)
        if getattr(result, "structured_content", None):
            payload = result.structured_content
        else:
            text = "".join(getattr(c, "text", "") for c in result.content)
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = {"text": text}
        if getattr(result, "is_error", False):
            return {"error": payload}
        return payload

    async def close(self) -> None:
        if self._stack:
            await self._stack.aclose()


class GatewayError(Exception):
    def __init__(self, status: int, body: dict):
        super().__init__(f"{status}: {body}")
        self.status, self.body = status, body


class TuitionGatewayClient:
    """Everything goes through the gateway's versioned routes (/api/v1/...)."""

    def __init__(self, base_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.http = httpx.AsyncClient(base_url=base_url or os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000"),
                                      transport=transport, timeout=15)

    async def _send(self, method: str, path: str, token: str | None = None, **kw) -> tuple[int, dict]:
        headers = kw.pop("headers", {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = await self.http.request(method, path, headers=headers, **kw)
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text}
        return r.status_code, body

    async def login(self, username: str, password: str) -> dict:
        status, body = await self._send("POST", "/api/v1/auth/token",
                                        json={"username": username, "password": password})
        if status != 200:
            raise GatewayError(status, body)
        return body

    async def query_tuition(self, token: str, student_no: str, term: str | None) -> tuple[int, dict]:
        params = {"term": term} if term else {}
        return await self._send("GET", f"/api/v1/banking/tuition/{student_no}", token, params=params)

    async def pay_tuition(self, token: str, student_no: str, term: str, amount: float,
                          transaction_id: str) -> tuple[int, dict]:
        return await self._send("POST", "/api/v1/banking/payments", token,
                                headers={"X-Payment-Channel": "ai-assistant"},
                                json={"studentNo": student_no, "term": term,
                                      "amount": f"{amount:.2f}", "transactionId": transaction_id})

    async def close(self) -> None:
        await self.http.aclose()
