#!/usr/bin/env python3
"""Completion contract: eval-allowed unless technical failure; reward from evaluator only."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "harbor_adapter"))

from native_mcp.completion import CompletionInference, infer_completion  # noqa: E402
from native_mcp import workspace_lifecycle as wl  # noqa: E402

finalize = wl.finalize


def _agent_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="cowork-completion-"))


def _ctx(workspace: Path, log_file: Path) -> None:
    ctx = workspace / ".cowork"
    ctx.mkdir(parents=True)
    (ctx / "cli_context.json").write_text(
        json.dumps(
            {
                "task": "demo",
                "log_file": str(log_file),
                "task_config": {"agent_workspace": str(workspace)},
                "start_time": "t0",
            }
        ),
        encoding="utf-8",
    )


class CompletionInferenceTests(unittest.TestCase):
    def test_normal_exit_without_finish_allows_eval(self) -> None:
        d = _agent_dir()
        (d / "openhands.trajectory.json").write_text(
            json.dumps({"history": [{"action": "run", "args": {"command": "ls"}}]}),
            encoding="utf-8",
        )
        (d / "openhands.txt").write_text("Agent finished with exit code 0\n", encoding="utf-8")
        got = infer_completion(d)
        self.assertTrue(got.confirmed)
        self.assertEqual(got.status, "SUCCESS")
        self.assertEqual(got.reason, "agent_process_completed")
        self.assertEqual(got.cowork_status, "success")

    def test_missing_agent_dir_allows_eval(self) -> None:
        got = infer_completion(Path("/tmp/cowork-no-such-agent-dir"))
        self.assertTrue(got.confirmed)
        self.assertEqual(got.reason, "missing_agent_dir")
        self.assertEqual(got.cowork_status, "success")

    def test_empty_agent_dir_allows_eval(self) -> None:
        d = _agent_dir()
        got = infer_completion(d)
        self.assertTrue(got.confirmed)
        self.assertEqual(got.reason, "agent_process_completed")
        self.assertIn("missing_agent_artifact", got.evidence)

    def test_turn_finished_marker_is_ignored(self) -> None:
        d = _agent_dir()
        ws = Path(tempfile.mkdtemp(prefix="cowork-ws-"))
        marker = ws / ".cowork" / "TURN_FINISHED"
        marker.parent.mkdir(parents=True)
        marker.write_text("done\n", encoding="utf-8")
        (d / "qwen-code.txt").write_text("qwen --yolo exit 0\n", encoding="utf-8")
        got = infer_completion(d, workspace=ws)
        self.assertTrue(got.confirmed)
        self.assertEqual(got.reason, "agent_process_completed")
        self.assertNotEqual(got.reason, "workspace_turn_finished_marker")

    def test_timeout_or_max_steps_is_technical_fail(self) -> None:
        d = _agent_dir()
        (d / "openhands.txt").write_text(
            "Reached maximum number of iterations (100)\nAgentTimeoutError\n",
            encoding="utf-8",
        )
        (d / "openhands.trajectory.json").write_text(
            json.dumps({"history": [{"action": "run"}]}),
            encoding="utf-8",
        )
        got = infer_completion(d)
        self.assertFalse(got.confirmed)
        self.assertEqual(got.status, "FAILED")
        self.assertEqual(got.reason, "timeout_or_max_steps")
        self.assertEqual(got.cowork_status, "failed")

    def test_exception_crash_is_technical_fail(self) -> None:
        d = _agent_dir()
        (d / "agent.log").write_text(
            "Traceback (most recent call last):\n  File \"x.py\", line 1\nFatal error\n",
            encoding="utf-8",
        )
        got = infer_completion(d)
        self.assertFalse(got.confirmed)
        self.assertEqual(got.reason, "exception_or_crash")

    def test_corrupt_trajectory_is_technical_fail(self) -> None:
        d = _agent_dir()
        (d / "openhands.trajectory.json").write_text("{not-json", encoding="utf-8")
        got = infer_completion(d)
        self.assertFalse(got.confirmed)
        self.assertEqual(got.reason, "corrupt_agent_artifact")

    def test_qwen_session_without_finish_allows_eval(self) -> None:
        d = _agent_dir()
        sess = d / "qwen-sessions"
        sess.mkdir()
        (sess / "chat.jsonl").write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "parts": [
                            {"functionCall": {"name": "write_file", "args": {}}}
                        ]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        got = infer_completion(d)
        self.assertTrue(got.confirmed)
        self.assertEqual(got.reason, "agent_process_completed")
        self.assertEqual(got.framework, "qwen-code")

    def test_openhands_finish_still_allows_eval_but_not_required(self) -> None:
        d = _agent_dir()
        (d / "openhands.trajectory.json").write_text(
            json.dumps(
                {
                    "history": [
                        {"action": "run", "args": {"command": "ls"}},
                        {"action": "finish", "args": {"outputs": "done"}},
                    ]
                }
            ),
            encoding="utf-8",
        )
        got = infer_completion(d)
        self.assertTrue(got.confirmed)
        self.assertEqual(got.reason, "agent_process_completed")


class FinalizeContractTests(unittest.TestCase):
    def test_finalize_success_without_artifact(self) -> None:
        ws = Path(tempfile.mkdtemp(prefix="cowork-fin-"))
        log = ws / "traj_log.json"
        _ctx(ws, log)
        agent = _agent_dir()  # empty — no finish / no marker
        result = finalize(
            status=None,
            workspace=ws,
            agent_dir=agent,
            context_path=ws / ".cowork" / "cli_context.json",
        )
        self.assertEqual(result["status"], "success")
        dumped = json.loads(log.read_text(encoding="utf-8"))
        self.assertEqual(dumped["status"], "success")
        self.assertIn("completion", dumped)

    def test_finalize_missing_agent_dir_is_success_for_eval(self) -> None:
        ws = Path(tempfile.mkdtemp(prefix="cowork-miss-"))
        log = ws / "traj_log.json"
        _ctx(ws, log)
        result = finalize(
            status=None,
            workspace=ws,
            agent_dir=Path("/tmp/cowork-no-such-agent-dir-finalize"),
            context_path=ws / ".cowork" / "cli_context.json",
        )
        self.assertEqual(result["status"], "success")

    def test_finalize_timeout_is_failed(self) -> None:
        ws = Path(tempfile.mkdtemp(prefix="cowork-to-"))
        log = ws / "traj_log.json"
        _ctx(ws, log)
        agent = _agent_dir()
        (agent / "openhands.txt").write_text("AgentTimeoutError\n", encoding="utf-8")
        result = finalize(
            status=None,
            workspace=ws,
            agent_dir=agent,
            context_path=ws / ".cowork" / "cli_context.json",
        )
        self.assertEqual(result["status"], "failed")
        dumped = json.loads(log.read_text(encoding="utf-8"))
        self.assertEqual(dumped["status"], "failed")

    def test_cowork_status_lowercase_matches_task_status(self) -> None:
        ok = CompletionInference(confirmed=True, status="SUCCESS", reason="x")
        bad = CompletionInference(confirmed=False, status="FAILED", reason="y")
        self.assertEqual(ok.cowork_status, "success")
        self.assertEqual(bad.cowork_status, "failed")

    def test_corrupt_context_json_is_not_success(self) -> None:
        ws = Path(tempfile.mkdtemp(prefix="cowork-badjson-"))
        ctx = ws / ".cowork"
        ctx.mkdir(parents=True)
        (ctx / "cli_context.json").write_text("{not-json", encoding="utf-8")
        agent = _agent_dir()
        with self.assertRaises(SystemExit) as raised:
            finalize(
                status=None,
                workspace=ws,
                agent_dir=agent,
                context_path=ctx / "cli_context.json",
            )
        self.assertIn("corrupt", str(raised.exception).lower())

    def test_finalize_upgrades_legacy_minimal_config(self) -> None:
        ws = Path(tempfile.mkdtemp(prefix="cowork-upg-"))
        log = ws / "traj_log.json"
        _ctx(ws, log)
        agent = _agent_dir()
        finalize(
            status=None,
            workspace=ws,
            agent_dir=agent,
            context_path=ws / ".cowork" / "cli_context.json",
        )
        dumped = json.loads(log.read_text(encoding="utf-8"))
        cfg = dumped["config"]
        self.assertIn("evaluation", cfg)
        self.assertEqual(cfg["evaluation"]["groundtruth_workspace"], None)
        self.assertEqual(cfg["evaluation"]["evaluation_command"], None)
        self.assertEqual(cfg["task_str"], "")
        self.assertIn("system_prompts", cfg)
        self.assertIn("initialization", cfg)
        self.assertIn("stop", cfg)

    def test_finalize_preserves_full_config_fields(self) -> None:
        ws = Path(tempfile.mkdtemp(prefix="cowork-full-"))
        log = ws / "traj_log.json"
        ctx = ws / ".cowork"
        ctx.mkdir(parents=True)
        full = {
            "id": "demo",
            "task_dir": "demo",
            "agent_workspace": str(ws),
            "log_file": str(log),
            "single_turn_mode": True,
            "cn_mode": False,
            "task_str": "keep-me",
            "evaluation": {
                "groundtruth_workspace": "/custom/gt",
                "evaluation_command": "python3 -m custom.eval",
            },
            "system_prompts": {"agent": "A", "user": "U"},
            "initialization": {"workspace": "/init", "process_command": "prep"},
            "stop": {"user_phrases": ["STOP"], "tool_names": ["done"]},
            "meta": {"x": 1},
        }
        (ctx / "cli_context.json").write_text(
            json.dumps(
                {
                    "task": "demo",
                    "log_file": str(log),
                    "task_config": full,
                    "start_time": "t0",
                }
            ),
            encoding="utf-8",
        )
        agent = _agent_dir()
        finalize(
            status=None,
            workspace=ws,
            agent_dir=agent,
            context_path=ctx / "cli_context.json",
        )
        dumped = json.loads(log.read_text(encoding="utf-8"))
        self.assertEqual(dumped["config"]["task_str"], "keep-me")
        self.assertEqual(
            dumped["config"]["evaluation"]["evaluation_command"],
            "python3 -m custom.eval",
        )
        self.assertEqual(dumped["config"]["system_prompts"]["agent"], "A")
        self.assertEqual(dumped["config"]["meta"], {"x": 1})


class EvalGateRewardContractTests(unittest.TestCase):
    """status→evaluate_one gate + classifier: reward only from evaluator pass."""

    def _evaluate_gate(self, status: str) -> dict:
        success_value = "success"
        if status != success_value:
            return {
                "pass": None,
                "details": f"Task status: {status}, only SUCCESS counts as pass; pass is null",
            }
        return {"pass": "PENDING_CONTENT"}

    def _run_classifier(self, *args, **kwargs):
        from generate_harbor_canonical import _CLASSIFIER_PYTHON_SCRIPT  # noqa: WPS433
        import subprocess

        mode, rc, stdout, stderr = args[:4]
        eval_res_data = kwargs.get("eval_res_data")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            stdout_path = tmp / "evaluator.stdout.log"
            stderr_path = tmp / "evaluator.stderr.log"
            result_json_path = tmp / "oracle-result.json"
            eval_res_path = tmp / "eval_res.json"
            completion_out = tmp / "completion.json"
            reward_out = tmp / "reward.txt"
            stdout_path.write_text(stdout, encoding="utf-8")
            stderr_path.write_text(stderr, encoding="utf-8")
            if eval_res_data is not None:
                eval_res_path.write_text(json.dumps(eval_res_data), encoding="utf-8")
            script_path = tmp / "classify.py"
            script_path.write_text(_CLASSIFIER_PYTHON_SCRIPT, encoding="utf-8")
            cmd = [
                sys.executable,
                str(script_path),
                mode,
                str(rc),
                str(stdout_path),
                str(stderr_path),
                str(result_json_path),
                str(eval_res_path),
                str(completion_out),
                str(reward_out),
            ]
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            completion = json.loads(completion_out.read_text(encoding="utf-8"))
            reward_val = (
                reward_out.read_text(encoding="utf-8").strip()
                if reward_out.exists()
                else None
            )
            return completion, reward_val

    def test_no_artifact_success_status_opens_eval_gate(self) -> None:
        ws = Path(tempfile.mkdtemp(prefix="cowork-gate-"))
        log = ws / "traj_log.json"
        _ctx(ws, log)
        finalize(
            status=None,
            workspace=ws,
            agent_dir=_agent_dir(),
            context_path=ws / ".cowork" / "cli_context.json",
        )
        dumped = json.loads(log.read_text(encoding="utf-8"))
        gate = self._evaluate_gate(dumped["status"])
        self.assertEqual(gate["pass"], "PENDING_CONTENT")

    def test_technical_fail_keeps_pass_null(self) -> None:
        ws = Path(tempfile.mkdtemp(prefix="cowork-gate-fail-"))
        log = ws / "traj_log.json"
        _ctx(ws, log)
        agent = _agent_dir()
        (agent / "x.log").write_text("AgentTimeoutError\n", encoding="utf-8")
        finalize(
            status=None,
            workspace=ws,
            agent_dir=agent,
            context_path=ws / ".cowork" / "cli_context.json",
        )
        dumped = json.loads(log.read_text(encoding="utf-8"))
        gate = self._evaluate_gate(dumped["status"])
        self.assertIsNone(gate["pass"])

    def test_evaluator_pass_true_reward_1_without_artifact(self) -> None:
        comp, reward = self._run_classifier(
            "ua", 0, "run_eval done", "", eval_res_data={"pass": True}
        )
        self.assertEqual(comp["reward"], 1)
        self.assertEqual(reward, "1")
        self.assertEqual(comp["status"], "succeeded")

    def test_evaluator_pass_false_reward_0_without_artifact(self) -> None:
        comp, reward = self._run_classifier(
            "ua", 0, "run_eval done", "", eval_res_data={"pass": False}
        )
        self.assertEqual(comp["reward"], 0)
        self.assertEqual(reward, "0")

    def test_no_soft_pass_without_eval_res(self) -> None:
        comp, reward = self._run_classifier(
            "ua", 2, "", "Cowork traj_log.json not found\n"
        )
        self.assertEqual(comp["status"], "failed")
        self.assertIsNone(reward)
        self.assertNotEqual(comp.get("reward"), 1)

        comp2, reward2 = self._run_classifier("ua", 1, "no eval_res", "oops")
        self.assertEqual(comp2["status"], "failed")
        self.assertIsNone(reward2)
        self.assertNotEqual(comp2.get("reward"), 1)


class RunEvalOncePackagingTests(unittest.TestCase):
    def test_db_and_non_db_run_eval_once_in_verifier(self) -> None:
        from generate_harbor_canonical import convert_one, load_catalog

        catalog = load_catalog()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            convert_one("arxiv-fetch-terminal-pipeline", out, catalog)
            convert_one("kulinar-terminal-menu-generator", out, catalog)
            for task in (
                "arxiv-fetch-terminal-pipeline",
                "kulinar-terminal-menu-generator",
            ):
                sh = (out / task / "tests" / "test.sh").read_text(encoding="utf-8")
                toml = (out / task / "task.toml").read_text(encoding="utf-8")
                self.assertEqual(sh.count("run_eval.py"), 1)
                self.assertEqual(toml.count("workspace_lifecycle.py finalize"), 1)


if __name__ == "__main__":
    unittest.main()
