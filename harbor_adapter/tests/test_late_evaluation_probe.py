"""Reproduce late evaluation reconstruction before any design change.

These tests record what TaskConfig.from_dict and Evaluation.build do today.
They do not change scoring.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "harbor_adapter") not in sys.path:
    sys.path.insert(0, str(ROOT / "harbor_adapter"))

from utils.data_structures.task_config import Evaluation, TaskConfig
from utils.evaluation.evaluator import TaskEvaluator

DB_TASK = "insales-product-launch-dashboard"
NONDB_TASK = "kulinar-nutrition-ppt"


def _sanitized(task: str) -> dict:
    from native_mcp.prep.prepare_workspace import build_task_config_dict

    return build_task_config_dict(task, repo_root=ROOT)


def _full(task: str) -> dict:
    data = copy.deepcopy(_sanitized(task))
    previous = os.getcwd()
    os.chdir(ROOT)
    try:
        fresh = Evaluation.build(task)
    finally:
        os.chdir(previous)
    data["evaluation"] = {
        "groundtruth_workspace": fresh.groundtruth_workspace,
        "evaluation_command": fresh.evaluation_command,
    }
    return data


class FromDictSchemaTests(unittest.TestCase):
    def test_missing_evaluation_key_is_rejected(self) -> None:
        data = _sanitized(DB_TASK)
        del data["evaluation"]
        with self.assertRaises(KeyError):
            TaskConfig.from_dict(data)

    def test_null_evaluation_is_rejected(self) -> None:
        data = _sanitized(DB_TASK)
        data["evaluation"] = None
        with self.assertRaises(TypeError):
            TaskConfig.from_dict(data)

    def test_both_private_fields_none_is_accepted(self) -> None:
        for task in (DB_TASK, NONDB_TASK):
            restored = TaskConfig.from_dict(_sanitized(task))
            self.assertIsNone(restored.evaluation.evaluation_command)
            self.assertIsNone(restored.evaluation.groundtruth_workspace)
            self.assertEqual(restored.task_dir, task)

    def test_full_evaluation_is_accepted(self) -> None:
        for task in (DB_TASK, NONDB_TASK):
            restored = TaskConfig.from_dict(_full(task))
            self.assertIn("evaluation.main", restored.evaluation.evaluation_command)
            self.assertTrue(
                restored.evaluation.groundtruth_workspace.endswith("groundtruth_workspace")
            )

    def test_missing_task_dir_is_rejected(self) -> None:
        data = _sanitized(DB_TASK)
        del data["task_dir"]
        with self.assertRaises((KeyError, AssertionError, TypeError)):
            TaskConfig.from_dict(data)


class EvaluationBuildTests(unittest.TestCase):
    def test_field_name_is_cn_mode_not_cm_mode(self) -> None:
        source = (ROOT / "utils" / "evaluation" / "evaluator.py").read_text(encoding="utf-8")
        self.assertIn("cn_mode=getattr(task_config, \"cn_mode\", False)", source)
        self.assertNotIn("cm_mode", source)
        cfg = TaskConfig.from_dict(_sanitized(DB_TASK))
        self.assertFalse(cfg.cn_mode)
        self.assertFalse(hasattr(cfg, "cm_mode"))

    def test_build_restores_real_paths_when_tree_exists(self) -> None:
        previous = os.getcwd()
        try:
            os.chdir(ROOT)
            for task in (DB_TASK, NONDB_TASK):
                fresh = Evaluation.build(task, cn_mode=False)
                self.assertIsNotNone(fresh.evaluation_command)
                self.assertIn(f"tasks.finalpool.{task}.evaluation.main", fresh.evaluation_command)
                self.assertTrue(Path(fresh.groundtruth_workspace).is_dir())
        finally:
            os.chdir(previous)

    def test_build_returns_none_when_tree_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.getcwd()
            try:
                os.chdir(tmp)
                fresh = Evaluation.build(DB_TASK, cn_mode=False)
            finally:
                os.chdir(previous)
        self.assertIsNone(fresh.evaluation_command)
        self.assertIsNone(fresh.groundtruth_workspace)

    def test_wrong_task_dir_does_not_restore(self) -> None:
        previous = os.getcwd()
        try:
            os.chdir(ROOT)
            fresh = Evaluation.build("not-a-real-task", cn_mode=False)
        finally:
            os.chdir(previous)
        self.assertIsNone(fresh.evaluation_command)
        self.assertIsNone(fresh.groundtruth_workspace)


class EvaluatorReconstructionTests(unittest.TestCase):
    def _run(self, config: dict, *, cwd: Path) -> dict:
        calls = {"build": 0, "command": None}

        real_build = Evaluation.build

        def wrapped_build(task_dir: str, cn_mode: bool = False):
            calls["build"] += 1
            return real_build(task_dir, cn_mode=cn_mode)

        async def fake_run(command, debug=False, show_output=False):
            calls["command"] = command
            return "Pass: False\n", "", 1

        dump = {
            "config": config,
            "status": "success",
        }
        previous = os.getcwd()
        try:
            os.chdir(cwd)
            with mock.patch(
                "utils.evaluation.evaluator.Evaluation.build",
                side_effect=wrapped_build,
            ), mock.patch(
                "utils.evaluation.evaluator.run_command",
                side_effect=fake_run,
            ):
                result = asyncio.run(TaskEvaluator.evaluate_one(dump))
        finally:
            os.chdir(previous)
        result["_build_calls"] = calls["build"]
        result["_command"] = calls["command"]
        return result

    def test_sanitized_contract_rebuilds_and_reaches_grader(self) -> None:
        for task in (DB_TASK, NONDB_TASK):
            result = self._run(_sanitized(task), cwd=ROOT)
            self.assertEqual(result["_build_calls"], 1)
            self.assertIn(f"tasks.finalpool.{task}.evaluation.main", result["_command"])
            self.assertIn("groundtruth_workspace", result["_command"])
            self.assertEqual(result["verdict"], "False")
            self.assertEqual(result["grader_rc"], 1)

    def test_full_contract_does_not_call_build(self) -> None:
        result = self._run(_full(DB_TASK), cwd=ROOT)
        self.assertEqual(result["_build_calls"], 0)
        self.assertIn("evaluation.main", result["_command"])

    def test_sanitized_contract_without_tree_does_not_run_grader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(_sanitized(DB_TASK), cwd=Path(tmp))
        self.assertEqual(result["_build_calls"], 1)
        self.assertIsNone(result["_command"])
        self.assertIsNone(result["pass"])
        self.assertIsNone(result["verdict"])

    def test_public_context_has_no_private_fields(self) -> None:
        from native_mcp.prep.prepare_workspace import build_agent_context

        ctx = build_agent_context(
            DB_TASK,
            provider="strands",
            model="probe",
            workspace="/workspace/cowork_shared",
            log_file="/logs/artifacts/cowork/traj_log.json",
            start_time="2026-10-07T00:00:00",
        )
        blob = json.dumps(ctx)
        for forbidden in (
            "evaluation_command",
            "groundtruth_workspace",
            "evaluation",
            "grader",
        ):
            self.assertNotIn(forbidden, blob)


class GeneratedTreeLeakTests(unittest.TestCase):
    def test_generated_496_has_null_contract_and_no_agent_visible_values(self) -> None:
        root = Path(
            "/data/mashkurat/tmp/qwen36_cowork_cm_prompt_ab_20260924/"
            "cowork_bench_pr2_late_evaluation_gen496_20261007"
        )
        if not (root / "INDEX.json").is_file():
            self.skipTest("generated tree is not present")
        tokens = ("evaluation_command", "groundtruth_workspace", "evaluation.main")
        leaks = []
        tasks = [p for p in root.iterdir() if (p / "task.toml").is_file()]
        self.assertEqual(len(tasks), 496)
        for task in tasks:
            contract_path = task / "tests" / "grader_private" / "task_contract.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            evaluation = contract["evaluation"]
            self.assertIsNone(evaluation["evaluation_command"], task.name)
            self.assertIsNone(evaluation["groundtruth_workspace"], task.name)
            self.assertNotIn("evaluation.main", contract_path.read_text(encoding="utf-8"))
            for path in task.rglob("*"):
                if not path.is_file():
                    continue
                rel = path.relative_to(task).as_posix()
                if rel.startswith("tests/") or rel.startswith("solution/") or rel.startswith("environment/task_payload/"):
                    continue
                if path.suffix not in {".md", ".toml", ".yaml", ".yml", ".json", ".sh", ".py"}:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                for token in tokens:
                    if token in text:
                        leaks.append(f"{task.name}/{rel}:{token}")
        self.assertEqual(leaks, [])


class AllTaskSchemaTests(unittest.TestCase):
    def test_every_task_accepts_sanitized_config_and_build(self) -> None:
        from native_mcp.catalog import all_task_ids

        previous = os.getcwd()
        os.chdir(ROOT)
        try:
            ids = all_task_ids()
            self.assertEqual(len(ids), 496)
            for task in ids:
                restored = TaskConfig.from_dict(_sanitized(task))
                self.assertIsNone(restored.evaluation.evaluation_command)
                self.assertIsNone(restored.evaluation.groundtruth_workspace)
                fresh = Evaluation.build(task)
                self.assertIsNotNone(fresh.evaluation_command, task)
                self.assertIn(f"tasks.finalpool.{task}.evaluation.main", fresh.evaluation_command)
        finally:
            os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
