#!/usr/bin/env python3
"""Regression tests for v2.0.7 fixes:
1. canvas-exam-calendar-report: evaluator imports without leading dots.
2. canvas-pdf-grade-gsheet: evaluator imports without leading dots.
3. fetch-kulinar-catering-cost-excel-word: compose has no mcp-gateway-db.
4. Tasks requiring DB MCP (e.g., arxiv-fetch-terminal-pipeline, canvas-pdf-grade-gsheet) still have mcp-gateway-db.
"""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from generate_harbor_canonical import convert_one
from native_mcp.catalog import load_catalog

PYTHON_EXEC = sys.executable


class TestV207Regression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load_catalog()

    def test_canvas_exam_calendar_report_evaluator_imports(self):
        """canvas-exam-calendar-report evaluator imports cleanly without relative import error."""
        src_eval = ROOT.parent / "tasks" / "finalpool" / "canvas-exam-calendar-report" / "evaluation" / "main.py"
        import ast
        tree = ast.parse(src_eval.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, f"Relative import found in {src_eval}: {node.module} with level={node.level}")

    def test_canvas_pdf_grade_gsheet_evaluator_imports(self):
        """canvas-pdf-grade-gsheet evaluator imports cleanly without relative import error."""
        src_eval = ROOT.parent / "tasks" / "finalpool" / "canvas-pdf-grade-gsheet" / "evaluation" / "main.py"
        import ast
        tree = ast.parse(src_eval.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, f"Relative import found in {src_eval}: {node.module} with level={node.level}")

    def test_fetch_kulinar_compose_excludes_mcp_gateway_db(self):
        """fetch-kulinar-catering-cost-excel-word has no mcp-gateway-db service in docker-compose.yaml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            out_root = Path(tmpdir)
            task = "fetch-kulinar-catering-cost-excel-word"
            res = convert_one(task, out_root, self.catalog)
            self.assertEqual(res["source_task"], task)

            compose_path = out_root / task / "environment" / "docker-compose.yaml"
            self.assertTrue(compose_path.is_file())
            compose_text = compose_path.read_text(encoding="utf-8")

            # Assert mcp-gateway-db service is NOT in compose
            self.assertNotIn("\n  mcp-gateway-db:", compose_text)

            # Assert mcp-gateway-workspace service IS in compose
            self.assertIn("\n  mcp-gateway-workspace:", compose_text)
            self.assertIn("\n  mcp-gateway-public:", compose_text)

            # Assert compose config is valid
            cfg_res = subprocess.run(
                ["docker", "compose", "-f", str(compose_path), "config", "-q"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(cfg_res.returncode, 0, f"docker compose config failed: {cfg_res.stderr}")

    def test_db_mcp_tasks_still_include_mcp_gateway_db(self):
        """Tasks with DB MCP (e.g. arxiv-fetch-terminal-pipeline, canvas-pdf-grade-gsheet) still have mcp-gateway-db."""
        tasks_with_db = ["arxiv-fetch-terminal-pipeline", "canvas-pdf-grade-gsheet"]
        with tempfile.TemporaryDirectory() as tmpdir:
            out_root = Path(tmpdir)
            for task in tasks_with_db:
                convert_one(task, out_root, self.catalog)
                compose_path = out_root / task / "environment" / "docker-compose.yaml"
                compose_text = compose_path.read_text(encoding="utf-8")

                self.assertIn("postgres:", compose_text)
                self.assertIn("mcp-gateway-db:", compose_text)
                self.assertIn("mcp-gateway-public:", compose_text)
                self.assertIn("mcp-gateway-db:\n        condition: service_healthy", compose_text)

                cfg_res = subprocess.run(
                    ["docker", "compose", "-f", str(compose_path), "config", "-q"],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(cfg_res.returncode, 0, f"docker compose config failed for {task}: {cfg_res.stderr}")


if __name__ == "__main__":
    unittest.main()
