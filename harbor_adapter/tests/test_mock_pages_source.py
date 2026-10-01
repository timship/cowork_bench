"""Local HTTP fixtures are derived from task sources and visible to MCP tools."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ADAPTER = Path(__file__).resolve().parents[1]
ROOT = ADAPTER.parent
SOURCE_TASKS = ROOT / "tasks" / "finalpool"
sys.path.insert(0, str(ADAPTER))

import generate_harbor_canonical as gen  # noqa: E402


def _instruction(task: str) -> str:
    return (SOURCE_TASKS / task / "docs" / "task.md").read_text(encoding="utf-8")


def _spec(task: str) -> dict | None:
    return gen._mock_http_spec(SOURCE_TASKS / task, _instruction(task))


class MockPagesSourceTest(unittest.TestCase):
    def test_timeout_default_stays_7200_pending_owner_review(self) -> None:
        text = (ADAPTER / "generate_harbor_canonical.py").read_text(encoding="utf-8")
        self.assertIn("[agent]\n            timeout_sec = 7200.0", text)
        self.assertNotIn("[agent]\n            timeout_sec = 1800.0", text)

    def test_tarball_fixture_runs_in_main_and_workspace_namespace(self) -> None:
        spec = _spec("insales-product-launch-dashboard")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["port"], 30207)
        text = gen._compose(
            "insales-product-launch-dashboard",
            has_db=True,
            has_workspace=True,
            mock_http=spec,
        )
        self.assertEqual(text.count("python3 -m http.server 30207"), 2)
        self.assertIn('network_mode: "service:mcp-gateway-workspace"', text)
        self.assertIn(
            "./task_payload/files/mock_pages.tar.gz:/mock/mock_pages.tar.gz:ro",
            text,
        )
        self.assertIn("--directory /tmp/mock/mock_pages", text)

    def test_no_tar_fixture_also_gets_workspace_sidecar(self) -> None:
        spec = _spec("canvas-faculty-workload-review")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["port"], 30220)
        self.assertEqual(
            spec["volume"],
            "./task_payload/tmp/mock_pages:/mock/mock_pages:ro",
        )
        text = gen._compose(
            "canvas-faculty-workload-review",
            has_db=True,
            has_workspace=True,
            mock_http=spec,
        )
        self.assertEqual(text.count("python3 -m http.server 30220"), 2)
        self.assertIn('network_mode: "service:mcp-gateway-workspace"', text)
        self.assertNotIn("tar -xzf", text)

    def test_formerly_unlisted_http_task_is_discovered(self) -> None:
        spec = _spec("arxiv-survey-presentation")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["port"], 30231)
        self.assertEqual(
            spec["volume"],
            "./task_payload/files/mock_pages:/mock/mock_pages:ro",
        )

    def test_tar_top_directory_is_taken_from_archive(self) -> None:
        task4 = _spec("terminal-kulinar-pw-nutrition-gsheet-word")
        task5 = _spec("terminal-fetch-sf-hr-gcal-excel-email")
        self.assertEqual(task4["directory"], "/tmp/mock/task4_mock_pages")
        self.assertEqual(task5["directory"], "/tmp/mock/task5_mock_pages")

    def test_preprocess_port_fallback(self) -> None:
        self.assertEqual(_spec("fetch-teamly-monitoring")["port"], 30154)
        self.assertEqual(_spec("kulinar-scholarly-health-study")["port"], 30155)

    def test_non_http_task_has_no_fixture(self) -> None:
        self.assertIsNone(_spec("sf-hr-performance-ppt"))

    def test_all_http_sources_are_covered(self) -> None:
        specs = {}
        for source in SOURCE_TASKS.iterdir():
            if not source.is_dir():
                continue
            instruction_path = source / "docs" / "task.md"
            instruction = (
                instruction_path.read_text(encoding="utf-8")
                if instruction_path.is_file()
                else ""
            )
            spec = gen._mock_http_spec(source, instruction)
            if spec:
                specs[source.name] = spec
                self.assertTrue(1024 <= spec["port"] <= 65535)
                self.assertTrue(spec["directory"].startswith(("/mock/", "/tmp/mock/")))
        self.assertEqual(len(specs), 128)


if __name__ == "__main__":
    unittest.main()
