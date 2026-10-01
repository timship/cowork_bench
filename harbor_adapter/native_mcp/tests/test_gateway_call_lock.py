"""The gateway lock serializes one ClientSession and keeps responses paired."""

from __future__ import annotations

import asyncio
import textwrap
import unittest
from pathlib import Path

GATEWAY = Path(__file__).resolve().parents[1] / "mcp_gateway.py"


def _load_call_tool():
    text = GATEWAY.read_text(encoding="utf-8")
    start = text.index("    async def call_tool(tool_name: str, arguments: dict | None):")
    end = text.index("    @app.list_resources()", start)
    snippet = textwrap.dedent(text[start:end])
    namespace = {"asyncio": asyncio}
    exec(snippet, namespace)
    return namespace["call_tool"]


class GatewayCallLockTest(unittest.TestCase):
    def test_lock_wraps_only_call_tool(self) -> None:
        text = GATEWAY.read_text(encoding="utf-8")
        self.assertIn("_tool_lock = asyncio.Lock()", text)
        call = text.split("async def call_tool", 1)[1].split("async def list_resources", 1)[0]
        listed = text.split("async def list_tools", 1)[1].split("return list(result.tools)", 1)[0]
        self.assertIn("async with _tool_lock:", call)
        self.assertIn("return await client.call_tool(tool_name, arguments or {})", call)
        self.assertNotIn("async with _tool_lock", listed)

    def test_concurrent_calls_keep_their_responses(self) -> None:
        call_tool = _load_call_tool()

        class Session:
            def __init__(self) -> None:
                self.inflight = 0
                self.max_inflight = 0

            async def call_tool(self, tool_name: str, arguments: dict | None):
                self.inflight += 1
                self.max_inflight = max(self.max_inflight, self.inflight)
                try:
                    await asyncio.sleep(0.01)
                    return {"tool": tool_name, "echo": arguments["request_id"]}
                finally:
                    self.inflight -= 1

        async def run() -> tuple[list[dict], int]:
            client = Session()
            lock = asyncio.Lock()
            namespace = {"client": client, "_tool_lock": lock, "asyncio": asyncio}

            async def bound(tool_name: str, arguments: dict | None):
                # The extracted function closes over client and _tool_lock.
                local = {"client": client, "_tool_lock": lock}
                exec_ns = {"asyncio": asyncio, **local}
                source = GATEWAY.read_text(encoding="utf-8")
                start = source.index("    async def call_tool")
                end = source.index("    @app.list_resources()", start)
                exec(textwrap.dedent(source[start:end]), exec_ns)
                return await exec_ns["call_tool"](tool_name, arguments)

            results = await asyncio.gather(
                *[bound("lookup", {"request_id": i}) for i in range(12)]
            )
            return list(results), client.max_inflight

        results, max_inflight = asyncio.run(run())
        self.assertEqual(max_inflight, 1)
        self.assertEqual([item["echo"] for item in results], list(range(12)))
        self.assertTrue(all(item["tool"] == "lookup" for item in results))
        self.assertIs(call_tool.__name__, "call_tool")
