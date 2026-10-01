"""Mock-pages sidecar is limited to tarball tasks with an instruction port."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ADAPTER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADAPTER))

import generate_harbor_canonical as gen  # noqa: E402


class MockPagesSourceTest(unittest.TestCase):
    def test_timeout_default_stays_7200_pending_owner_review(self) -> None:
        text = (ADAPTER / "generate_harbor_canonical.py").read_text(encoding="utf-8")
        self.assertIn("[agent]\n            timeout_sec = 7200.0", text)
        self.assertNotIn("[agent]\n            timeout_sec = 1800.0", text)

    def test_tarball_and_instruction_port_add_sidecar(self) -> None:
        text = gen._compose(
            "insales-product-launch-dashboard",
            has_db=True,
            has_workspace=True,
            has_mock=True,
            mock_http_port=30207,
            has_public_gateway=True,
        )
        self.assertIn("python3 -m http.server 30207 --bind 127.0.0.1", text)
        self.assertIn("mock-pages:", text)
        self.assertIn('network_mode: "service:mcp-gateway-public"', text)
        self.assertIn(
            "./task_payload/files/mock_pages.tar.gz:/mock/mock_pages.tar.gz:ro",
            text,
        )
        self.assertNotIn("http.server 30151", text)

    def test_tarball_without_instruction_port_stays_on_main(self) -> None:
        text = gen._compose(
            "fetch-teamly-monitoring",
            has_db=True,
            has_workspace=True,
            has_mock=True,
            mock_http_port=None,
            has_public_gateway=True,
        )
        self.assertIn("http.server 30151", text)
        self.assertNotIn("mock-pages:", text)

    def test_no_tar_http_fixture_has_no_sidecar(self) -> None:
        spec = gen.PREPROCESS_HTTP_WITHOUT_TAR["canvas-faculty-workload-review"]
        text = gen._compose(
            "canvas-faculty-workload-review",
            has_db=True,
            has_workspace=True,
            has_mock=False,
            local_http=spec,
            mock_http_port=None,
            has_public_gateway=True,
        )
        self.assertIn(f"http.server {spec['port']}", text)
        self.assertIn("/opt/mock_pages", text)
        self.assertNotIn("mock-pages:", text)
        self.assertNotIn("mock_pages.tar.gz", text)

    def test_instruction_port_parser(self) -> None:
        self.assertEqual(gen._instruction_localhost_port("open http://localhost:30207 now"), 30207)
        self.assertIsNone(gen._instruction_localhost_port("no local server"))
