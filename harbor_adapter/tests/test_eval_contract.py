#!/usr/bin/env python3
"""Evaluator contract: producers emit TaskConfig.to_dict(); invalid logs still fail."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "harbor_adapter"))

from generate_harbor_canonical import _solve_sh  # noqa: E402
from native_mcp.completion import infer_completion  # noqa: E402
from native_mcp.prep.prepare_workspace import (  # noqa: E402
    build_agent_context,
    build_task_config_dict,
    write_agent_context,
)
from native_mcp.workspace_lifecycle import assemble_trajectory, finalize  # noqa: E402
from utils.data_structures.task_config import Evaluation, TaskConfig  # noqa: E402

SMOKE_TASKS = (
    "insales-product-launch-dashboard",
    "canvas-faculty-workload-review",
    "terminal-kulinar-pw-nutrition-gsheet-word",
)

OLD_CONFIG = {
    "id": "demo",
    "task_dir": "demo",
    "agent_workspace": "/workspace/cowork_shared",
    "log_file": "/logs/artifacts/cowork/traj_log.json",
    "single_turn_mode": True,
}


def _evaluator():
    """Import evaluator.py without pulling CAMEL through task_agent.

    evaluator.py only needs TaskStatus from that module. The enum values match
    utils.roles.task_agent.TaskStatus. The evaluator source is not modified.
    """
    import enum
    import types

    if "utils.evaluation.evaluator" in sys.modules:
        return sys.modules["utils.evaluation.evaluator"].TaskEvaluator
    agent = types.ModuleType("utils.roles.task_agent")

    class TaskStatus(enum.Enum):
        SUCCESS = "success"
        FAILED = "failed"
        MAX_TURNS_REACHED = "max_turns_reached"
        INTERRUPTED = "interrupted"

    agent.TaskStatus = TaskStatus
    sys.modules["utils.roles.task_agent"] = agent
    from utils.evaluation.evaluator import TaskEvaluator

    return TaskEvaluator


class TaskConfigContractTests(unittest.TestCase):
    def test_old_minimal_config_raises_evaluation_keyerror(self) -> None:
        with self.assertRaises(KeyError) as caught:
            TaskConfig.from_dict(dict(OLD_CONFIG))
        self.assertEqual(caught.exception.args[0], "evaluation")

    def test_built_config_round_trips_and_has_evaluation_paths(self) -> None:
        for task in SMOKE_TASKS:
            with self.subTest(task=task):
                data = build_task_config_dict(task, repo_root=ROOT)
                restored = TaskConfig.from_dict(dict(data))
                previous = os.getcwd()
                os.chdir(ROOT)
                try:
                    fresh = Evaluation.build(task)
                finally:
                    os.chdir(previous)
                self.assertIsNone(restored.evaluation.groundtruth_workspace)
                self.assertIsNone(restored.evaluation.evaluation_command)
                self.assertTrue(fresh.groundtruth_workspace)
                self.assertIn(f"tasks.finalpool.{task}.evaluation.main", fresh.evaluation_command)
                self.assertNotIn("evaluation.main", json.dumps(data))
                self.assertIn("system_prompts", data)
                self.assertIn("initialization", data)
                self.assertIn("stop", data)
                self.assertEqual(data["task_str"], "")
                self.assertNotIn(str(ROOT), json.dumps(data))

    def test_solve_sh_loads_contract_instead_of_five_key_config(self) -> None:
        script = _solve_sh(SMOKE_TASKS[0])
        self.assertNotIn("/solution/task_contract.json", script)
        self.assertNotIn("evaluation.main", script)
        self.assertIn('"evaluation_command": None', script)
        self.assertIn('"single_turn_mode": True', script)

    def test_finalize_writes_lowercase_status_and_strands_stop_reason(self) -> None:
        task = SMOKE_TASKS[0]
        config = build_task_config_dict(task, repo_root=ROOT)
        ws = Path(tempfile.mkdtemp(prefix="cowork-contract-"))
        log = ws / "traj_log.json"
        ctx = ws / ".cowork"
        ctx.mkdir()
        write_agent_context(
            ctx / "cli_context.json",
            build_agent_context(
                task,
                provider="harbor-canonical",
                model="n/a",
                workspace=str(ws),
                log_file=str(log),
                start_time="t0",
            ),
        )
        agent = Path(tempfile.mkdtemp(prefix="cowork-strands-"))
        (agent / "strands-result.json").write_text(
            json.dumps({"stop_reason": "end_turn", "error": None}),
            encoding="utf-8",
        )
        inferred = infer_completion(agent)
        self.assertEqual(inferred.stop_reason, "end_turn")
        from native_mcp import workspace_lifecycle as wl

        artifacts = Path(tempfile.mkdtemp(prefix="cowork-artifacts-"))
        previous = wl.AGENT_COMPLETION_PATH
        wl.AGENT_COMPLETION_PATH = artifacts / "agent_completion.json"
        self.addCleanup(lambda: setattr(wl, "AGENT_COMPLETION_PATH", previous))
        result = finalize(
            status=None,
            workspace=ws,
            agent_dir=agent,
            context_path=ctx / "cli_context.json",
        )
        self.assertEqual(result["status"], "success")
        public = json.loads(wl.AGENT_COMPLETION_PATH.read_text(encoding="utf-8"))
        self.assertEqual(public["status"], "success")
        self.assertEqual(public["completion"]["stop_reason"], "end_turn")
        self.assertEqual(public["completion"]["framework"], "strands")
        self.assertNotIn("evaluation", json.dumps(public))
        planted = ws / "task_contract.json"
        planted.write_text(
            json.dumps({"evaluation": {"evaluation_command": "/tmp/evil-grader", "groundtruth_workspace": "/tmp/evil-gt"}}),
            encoding="utf-8",
        )
        trusted_dir = Path(tempfile.mkdtemp(prefix="cowork-trusted-"))
        trusted = trusted_dir / "task_contract.json"
        trusted.write_text(json.dumps(config), encoding="utf-8")
        assemble_trajectory(
            task=task,
            contract_path=trusted,
            completion_path=wl.AGENT_COMPLETION_PATH,
            log_path=log,
        )
        dumped = json.loads(log.read_text(encoding="utf-8"))
        self.assertEqual(dumped["status"], "success")
        self.assertEqual(dumped["completion"]["stop_reason"], "end_turn")
        self.assertNotIn("evaluation.main", json.dumps(dumped))
        restored = TaskConfig.from_dict(dict(dumped["config"]))
        self.assertIsNone(restored.evaluation.evaluation_command)
        self.assertTrue(planted.is_file())

    def test_invalid_config_is_still_rejected(self) -> None:
        TaskEvaluator = _evaluator()
        broken = build_task_config_dict(SMOKE_TASKS[0], repo_root=ROOT)
        del broken["evaluation"]
        with self.assertRaises(KeyError) as caught:
            TaskConfig.from_dict(dict(broken))
        self.assertEqual(caught.exception.args[0], "evaluation")
        log = Path(tempfile.mkdtemp(prefix="cowork-bad-")) / "traj_log.json"
        log.write_text(
            json.dumps({"config": broken, "status": "success"}),
            encoding="utf-8",
        )
        outcome = asyncio.run(TaskEvaluator.evaluate_from_log_file(str(log)))
        self.assertIsNone(outcome["pass"])
        self.assertEqual(outcome["failure"], "evaluation_error")
        self.assertEqual(outcome["details"], "'evaluation'")

    def test_evaluate_one_reaches_fixture_grader(self) -> None:
        TaskEvaluator = _evaluator()
        task = SMOKE_TASKS[0]
        sentinel = Path(tempfile.mkdtemp(prefix="cowork-grader-")) / "reached"
        grader = sentinel.parent / "grader.py"
        grader.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            f"Path({str(sentinel)!r}).write_text('ok', encoding='utf-8')\n"
            "print('Pass:    True')\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        config = build_task_config_dict(task, repo_root=ROOT)
        config["evaluation"]["evaluation_command"] = f"{sys.executable} {grader}"
        dump = {"config": config, "status": "success"}
        TaskConfig.from_dict(dict(config))
        outcome = asyncio.run(TaskEvaluator.evaluate_one(dump))
        self.assertTrue(sentinel.is_file())
        self.assertTrue(outcome["pass"])


if __name__ == "__main__":
    unittest.main()
