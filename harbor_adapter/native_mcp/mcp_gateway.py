#!/usr/bin/env python3
"""Task-local MCP HTTP gateway.

Starts Cowork stdio MCP servers from a JSON manifest and exposes each at
``/mcp/<name>`` (Streamable HTTP). Manifest entries may instead set
``upstream`` to reverse-proxy another Streamable HTTP MCP (used so the
agent-facing public gateway can front an internal DB gateway).

Stderr / startup errors go under --log-dir.
"""
from __future__ import annotations

import argparse
import json
import os
import traceback
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route


@dataclass
class ServerSlot:
    name: str
    command: list[str] | None
    cwd: str | None
    upstream: str | None = None
    ready: bool = False
    error: str | None = None
    stderr_log: str | None = None
    pid: int | None = None
    manager: StreamableHTTPSessionManager | None = None
    asgi: Any = None


def _proxy(name: str, client: ClientSession) -> Server:
    app = Server(f"cowork-gateway:{name}")

    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        result = await client.list_tools()
        return list(result.tools)

    @app.call_tool()
    async def call_tool(tool_name: str, arguments: dict | None):
        result = await client.call_tool(tool_name, arguments or {})
        return result

    @app.list_resources()
    async def list_resources() -> list[types.Resource]:
        try:
            result = await client.list_resources()
        except Exception:
            return []
        return list(result.resources)

    @app.read_resource()
    async def read_resource(uri):
        return await client.read_resource(uri)

    return app


def _unavailable_app(slot: ServerSlot):
    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        body = json.dumps(
            {
                "error": "mcp_unavailable",
                "name": slot.name,
                "detail": slot.error or "not ready",
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return app


def _http_upstream_app(upstream_base: str, client: httpx.AsyncClient):
    """Reverse-proxy Streamable HTTP to an internal MCP URL (no trailing slash)."""
    base = upstream_base.rstrip("/")

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        path = scope.get("path") or ""
        # Mount strips the /mcp/<name> prefix; forward remainder to upstream.
        qs = scope.get("query_string", b"").decode()
        target = base + (path if path.startswith("/") else f"/{path}")
        if qs:
            target = f"{target}?{qs}"

        headers = {
            k.decode(): v.decode()
            for k, v in scope.get("headers") or []
            if k.decode().lower() not in ("host", "content-length")
        }
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break

        try:
            resp = await client.request(
                scope["method"],
                target,
                headers=headers,
                content=body,
                timeout=120.0,
            )
        except Exception as exc:
            err = json.dumps({"error": "upstream_proxy_failed", "detail": str(exc)}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 502,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(err)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": err})
            return

        out_headers = [
            (k.encode(), v.encode())
            for k, v in resp.headers.items()
            if k.lower() not in ("transfer-encoding", "connection", "content-encoding")
        ]
        await send(
            {
                "type": "http.response.start",
                "status": resp.status_code,
                "headers": out_headers,
            }
        )
        await send({"type": "http.response.body", "body": resp.content})

    return app


class Gateway:
    def __init__(self, manifest: list[dict], log_dir: Path):
        self.manifest = manifest
        self.log_dir = log_dir
        self.slots: dict[str, ServerSlot] = {}
        self._http: httpx.AsyncClient | None = None
        for spec in manifest:
            name = spec["name"]
            upstream = spec.get("upstream")
            if upstream:
                self.slots[name] = ServerSlot(
                    name=name,
                    command=None,
                    cwd=None,
                    upstream=str(upstream),
                    stderr_log=str(log_dir / f"{name}.stderr.log"),
                )
            else:
                cmd = [spec["command"], *list(spec.get("args") or [])]
                self.slots[name] = ServerSlot(
                    name=name,
                    command=cmd,
                    cwd=spec.get("cwd"),
                    stderr_log=str(log_dir / f"{name}.stderr.log"),
                )

    def status_payload(self) -> dict:
        servers = {}
        for name, slot in self.slots.items():
            servers[name] = {
                "ready": slot.ready,
                "error": slot.error,
                "stderr_log": slot.stderr_log,
                "pid": slot.pid,
                "command": slot.command,
                "cwd": slot.cwd,
                "upstream": slot.upstream,
            }
        ready = bool(self.slots) and all(s.ready for s in self.slots.values())
        return {"ready": ready, "n": len(self.slots), "servers": servers}

    async def start_upstream(self, stack: AsyncExitStack, slot: ServerSlot) -> None:
        assert slot.upstream
        log_path = Path(slot.stderr_log or self.log_dir / f"{slot.name}.stderr.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        errlog = log_path.open("w", encoding="utf-8")
        stack.enter_context(errlog)
        if self._http is None:
            self._http = httpx.AsyncClient(follow_redirects=True)
            stack.push_async_callback(self._http.aclose)
        # Probe upstream /ready via parent host if path is /mcp/<name>
        try:
            # upstream like http://mcp-gateway-db:8000/mcp/arxiv_local
            root = slot.upstream.rsplit("/mcp/", 1)[0]
            r = await self._http.get(f"{root}/ready", timeout=5.0)
            if r.status_code != 200:
                raise RuntimeError(f"upstream ready HTTP {r.status_code}")
            slot.asgi = _http_upstream_app(slot.upstream, self._http)
            slot.ready = True
            slot.error = None
            errlog.write(f"READY upstream {slot.name} -> {slot.upstream}\n")
            errlog.flush()
        except Exception as exc:
            slot.ready = False
            slot.error = f"{type(exc).__name__}: {exc}"
            errlog.write(slot.error + "\n")
            errlog.write(traceback.format_exc())
            errlog.flush()
            slot.asgi = _unavailable_app(slot)

    async def start_one(self, stack: AsyncExitStack, spec: dict, slot: ServerSlot) -> None:
        if slot.upstream:
            await self.start_upstream(stack, slot)
            return
        log_path = Path(slot.stderr_log or self.log_dir / f"{slot.name}.stderr.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        errlog = log_path.open("w", encoding="utf-8")
        stack.enter_context(errlog)
        # inherit_postgres: task pg.env is authoritative; never let manifest
        # PG_* (e.g. YAML PG_USER=eigent) overwrite via dict.update.
        try:
            from .postgres_env import build_stdio_env
        except ImportError:  # flat copy under environment/mcp_runtime
            from postgres_env import build_stdio_env

        env = build_stdio_env(spec, environ=os.environ)
        assert slot.command
        params = StdioServerParameters(
            command=slot.command[0],
            args=slot.command[1:],
            env=env,
            cwd=slot.cwd or None,
        )
        try:
            read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
            client = await stack.enter_async_context(ClientSession(read, write))
            await client.initialize()
            proxy = _proxy(slot.name, client)
            manager = StreamableHTTPSessionManager(
                app=proxy,
                json_response=True,
                stateless=True,
            )
            await stack.enter_async_context(manager.run())
            slot.manager = manager
            slot.asgi = manager.handle_request
            slot.ready = True
            slot.error = None
            errlog.write(f"READY {slot.name}\n")
            errlog.flush()
        except Exception as exc:
            slot.ready = False
            slot.error = f"{type(exc).__name__}: {exc}"
            errlog.write(slot.error + "\n")
            errlog.write(traceback.format_exc())
            errlog.flush()
            slot.asgi = _unavailable_app(slot)


def build_app(gateway: Gateway, stack: AsyncExitStack) -> Starlette:
    async def ready(request: Request) -> Response:
        payload = gateway.status_payload()
        return JSONResponse(payload, status_code=200 if payload["ready"] else 503)

    async def status(request: Request) -> JSONResponse:
        return JSONResponse(gateway.status_payload())

    mounts = []
    for name, slot in gateway.slots.items():
        mounts.append(Mount(f"/mcp/{name}", app=slot.asgi or _unavailable_app(slot)))

    return Starlette(
        routes=[
            Route("/ready", ready),
            Route("/health", ready),
            Route("/status", status),
            *mounts,
        ]
    )


def load_manifest(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        servers = data.get("servers") or data.get("mcp_servers") or []
    else:
        servers = data
    if not isinstance(servers, list) or not servers:
        raise SystemExit(f"manifest {path} has no servers")
    return servers


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-dir", type=Path, default=Path("/logs/mcp-gateway"))
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    gateway = Gateway(manifest, args.log_dir)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with AsyncExitStack() as stack:
            for spec in manifest:
                slot = gateway.slots[spec["name"]]
                await gateway.start_one(stack, spec, slot)
            app.router.routes[:] = build_app(gateway, stack).router.routes
            (args.log_dir / "status.json").write_text(
                json.dumps(gateway.status_payload(), indent=2), encoding="utf-8"
            )
            yield
            (args.log_dir / "status.json").write_text(
                json.dumps(gateway.status_payload(), indent=2), encoding="utf-8"
            )

    app = Starlette(
        lifespan=lifespan,
        routes=[Route("/ready", lambda r: JSONResponse({"ready": False}, status_code=503))],
    )
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
