#!/usr/bin/env python3
"""Unit tests for Harbor traj task_config stub + from_dict / status gate.

Avoids importing ``utils.roles.task_agent`` (pulls optional ``camel`` deps).
``TaskStatus.SUCCESS.value`` is documented as ``\"success\"`` in that module;
tests assert the same literal the evaluator compares against.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harbor_adapter"))
sys.path.insert(0, str(ROOT))

from native_mcp.task_config_stub import (  # noqa: E402
    minimal_harbor_task_config_dict,
    needs_eval_upgrade,
    upgrade_task_config_for_eval,
)
from utils.data_structures.task_config import TaskConfig  # noqa: E402

# Mirror utils.roles.task_agent.TaskStatus without importing camel-heavy module.
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_FAILED = "failed"


class TaskConfigStubTests(unittest.TestCase):
    def test_minimal_stub_accepted_by_from_dict(self) -> None:
        stub = minimal_harbor_task_config_dict(
            "demo-task",
            "/workspace/cowork_shared",
            "/logs/artifacts/cowork/traj_log.json",
        )
        self.assertIn("evaluation", stub)
        self.assertEqual(stub["evaluation"]["groundtruth_workspace"], None)
        self.assertEqual(stub["evaluation"]["evaluation_command"], None)
        self.assertEqual(stub["task_str"], "")
        cfg = TaskConfig.from_dict(dict(stub))
        self.assertEqual(cfg.task_dir, "demo-task")
        self.assertIsNone(cfg.evaluation.evaluation_command)

    def test_upgrade_fills_legacy_minimal_only(self) -> None:
        legacy = {
            "id": "demo",
            "task_dir": "demo",
            "agent_workspace": "/ws",
            "log_file": "/logs/traj_log.json",
            "single_turn_mode": True,
        }
        self.assertTrue(needs_eval_upgrade(legacy))
        upgraded = upgrade_task_config_for_eval(legacy)
        self.assertIn("evaluation", upgraded)
        self.assertEqual(upgraded["task_str"], "")
        self.assertEqual(upgraded["agent_workspace"], "/ws")
        TaskConfig.from_dict(dict(upgraded))

    def test_upgrade_does_not_clobber_full_config(self) -> None:
        full = minimal_harbor_task_config_dict("demo", "/ws", "/log")
        full["task_str"] = "keep"
        full["evaluation"] = {
            "groundtruth_workspace": "/gt",
            "evaluation_command": "python3 -m eval",
        }
        full["meta"] = {"k": "v"}
        self.assertFalse(needs_eval_upgrade(full))
        upgraded = upgrade_task_config_for_eval(full)
        self.assertEqual(upgraded["task_str"], "keep")
        self.assertEqual(upgraded["evaluation"]["evaluation_command"], "python3 -m eval")
        self.assertEqual(upgraded["meta"], {"k": "v"})

    def test_upgrade_rejects_non_mapping(self) -> None:
        with self.assertRaises(TypeError):
            upgrade_task_config_for_eval("not-a-dict")  # type: ignore[arg-type]

    def test_lowercase_success_matches_task_status_value(self) -> None:
        from native_mcp.completion import CompletionInference

        ok = CompletionInference(confirmed=True, status="SUCCESS", reason="x")
        self.assertEqual(ok.cowork_status, TASK_STATUS_SUCCESS)
        self.assertEqual(ok.cowork_status, "success")

    def test_uppercase_status_does_not_match_success_gate(self) -> None:
        # Evaluator gate: task_status != TaskStatus.SUCCESS.value → pass is None.
        self.assertNotEqual("SUCCESS", TASK_STATUS_SUCCESS)
        self.assertNotEqual("FAILED", TASK_STATUS_FAILED)
        self.assertEqual("failed", TASK_STATUS_FAILED)

    def test_prepare_workspace_module_builds_compatible_stub(self) -> None:
        prep_dir = ROOT / "harbor_adapter" / "native_mcp" / "prep"
        sys.path.insert(0, str(prep_dir))
        try:
            from task_config_stub import minimal_harbor_task_config_dict as prep_stub
        finally:
            sys.path.pop(0)
        cfg = prep_stub("t", "/ws", "/log")
        TaskConfig.from_dict(dict(cfg))


class GeneratedPackagingSmokeTests(unittest.TestCase):
    def test_convert_one_db_finalize_on_grader_and_stub_copied(self) -> None:
        from generate_harbor_canonical import convert_one
        from native_mcp.catalog import load_catalog

        catalog = load_catalog()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            task = "arxiv-fetch-terminal-pipeline"
            convert_one(task, out, catalog)
            toml = (out / task / "task.toml").read_text(encoding="utf-8")
            blocks = toml.split("[[verifier.collect]]")
            fin = [b for b in blocks[1:] if "workspace_lifecycle.py finalize" in b]
            self.assertEqual(len(fin), 1)
            self.assertIn('service = "grader"', fin[0])
            self.assertNotIn('service = "main"', fin[0])
            prep = (
                out / task / "environment" / "prep" / "prepare_workspace.py"
            ).read_text(encoding="utf-8")
            self.assertIn("minimal_harbor_task_config_dict", prep)
            self.assertTrue(
                (out / task / "environment" / "prep" / "task_config_stub.py").is_file()
            )
            self.assertTrue(
                (out / task / "tests" / "verifier" / "task_config_stub.py").is_file()
            )
            sh = (out / task / "tests" / "test.sh").read_text(encoding="utf-8")
            self.assertIn('MODE="ua"', sh)
            self.assertIn("run_eval.py", sh)
            self.assertNotIn("Host-side (EXTERNAL)", sh)
            compose = (
                out / task / "environment" / "docker-compose.yaml"
            ).read_text(encoding="utf-8")
            self.assertNotIn("18000", compose)
            pg = out / task / "environment" / "pg.env"
            self.assertTrue(pg.is_file())
            self.assertEqual(pg.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
