#!/usr/bin/env python3
"""Gateway routing, readiness, and isolated MCP failure tests."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "harbor_adapter"))

from native_mcp.mcp_gateway import Gateway, build_app, load_manifest  # noqa: E402


class ManifestTests(unittest.TestCase):
    def test_load_manifest_servers_key(self) -> None:
        path = Path(tempfile.mkdtemp()) / "m.json"
        path.write_text(json.dumps({"servers": [{"name": "a", "command": "true"}]}))
        self.assertEqual(load_manifest(path)[0]["name"], "a")


class GatewaySlotTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_dead_mcp_is_diagnosed_and_not_masked(self) -> None:
        from contextlib import AsyncExitStack

        dummy = Path(__file__).resolve().parent / "dummy_stdio_mcp.py"
        log_dir = Path(tempfile.mkdtemp())
        manifest = [
            {
                "name": "dummy",
                "command": sys.executable,
                "args": [str(dummy)],
                "cwd": str(dummy.parent),
                "env": {},
            },
            {
                "name": "dead",
                "command": "/no/such/mcp-binary",
                "args": [],
                "cwd": None,
                "env": {},
            },
        ]
        gw = Gateway(manifest, log_dir)
        async with AsyncExitStack() as stack:
            for spec in manifest:
                await gw.start_one(stack, spec, gw.slots[spec["name"]])
            payload = gw.status_payload()
            self.assertTrue(gw.slots["dummy"].ready)
            self.assertFalse(gw.slots["dead"].ready)
            self.assertIsNotNone(gw.slots["dead"].error)
            self.assertFalse(payload["ready"])
            stderr = Path(gw.slots["dead"].stderr_log or "")
            self.assertTrue(stderr.exists())
            self.assertTrue(stderr.read_text())


if __name__ == "__main__":
    unittest.main()
