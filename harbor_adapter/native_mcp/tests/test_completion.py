#!/usr/bin/env python3
"""Fail-closed completion inference tests. No model, no evaluator changes."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "harbor_adapter"))

from native_mcp.completion import infer_completion  # noqa: E402
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
    def test_openhands_finish_is_success(self) -> None:
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
        self.assertEqual(got.status, "SUCCESS")
        self.assertEqual(got.reason, "openhands_finish_action")

    def test_exit_zero_without_finish_is_not_success(self) -> None:
        d = _agent_dir()
        (d / "openhands.trajectory.json").write_text(
            json.dumps({"history": [{"action": "run", "args": {"command": "ls"}}]}),
            encoding="utf-8",
        )
        (d / "openhands.txt").write_text("Agent finished with exit code 0\n", encoding="utf-8")
        got = infer_completion(d)
        self.assertFalse(got.confirmed)
        self.assertNotEqual(got.status, "SUCCESS")
        self.assertEqual(got.reason, "unconfirmed_exit")

    def test_timeout_or_max_steps_is_failure(self) -> None:
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

    def test_missing_agent_dir_is_failure(self) -> None:
        got = infer_completion(Path("/tmp/cowork-no-such-agent-dir"))
        self.assertFalse(got.confirmed)
        self.assertEqual(got.reason, "missing_agent_dir")

    def test_corrupt_trajectory_is_failure(self) -> None:
        d = _agent_dir()
        (d / "openhands.trajectory.json").write_text("{not-json", encoding="utf-8")
        got = infer_completion(d)
        self.assertFalse(got.confirmed)
        self.assertEqual(got.reason, "corrupt_agent_artifact")

    def test_qwen_session_without_finish_is_not_success(self) -> None:
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
        self.assertFalse(got.confirmed)
        self.assertEqual(got.status, "FAILED")
        self.assertEqual(got.reason, "unconfirmed_exit")
        self.assertEqual(got.framework, "qwen-code")

    def test_qwen_finish_tool_is_success(self) -> None:
        d = _agent_dir()
        sess = d / "qwen-sessions"
        sess.mkdir()
        (sess / "chat.jsonl").write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "parts": [
                            {"functionCall": {"name": "task_complete", "args": {}}}
                        ]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        got = infer_completion(d)
        self.assertTrue(got.confirmed)
        self.assertEqual(got.status, "SUCCESS")
        self.assertEqual(got.framework, "qwen-code")

    def test_workspace_marker_allows_qwen_success(self) -> None:
        d = _agent_dir()
        ws = Path(tempfile.mkdtemp(prefix="cowork-ws-"))
        marker = ws / ".cowork" / "TURN_FINISHED"
        marker.parent.mkdir(parents=True)
        marker.write_text("done\n", encoding="utf-8")
        (d / "qwen-code.txt").write_text("qwen --yolo exit 0\n", encoding="utf-8")
        got = infer_completion(d, workspace=ws)
        self.assertTrue(got.confirmed)
        self.assertEqual(got.reason, "workspace_turn_finished_marker")


class FinalizeContractTests(unittest.TestCase):
    def test_finalize_does_not_default_to_success(self) -> None:
        ws = Path(tempfile.mkdtemp(prefix="cowork-fin-"))
        log = ws / "traj_log.json"
        _ctx(ws, log)
        agent = _agent_dir()
        (agent / "openhands.txt").write_text("exit 0 without finish\n", encoding="utf-8")
        result = finalize(
            status=None,
            workspace=ws,
            agent_dir=agent,
            context_path=ws / ".cowork" / "cli_context.json",
        )
        self.assertEqual(result["status"], "FAILED")
        dumped = json.loads(log.read_text(encoding="utf-8"))
        self.assertEqual(dumped["status"], "FAILED")
        self.assertNotEqual(dumped["status"], "SUCCESS")

    def test_finalize_writes_success_only_when_inferred(self) -> None:
        ws = Path(tempfile.mkdtemp(prefix="cowork-ok-"))
        log = ws / "traj_log.json"
        _ctx(ws, log)
        agent = _agent_dir()
        (agent / "openhands.trajectory.json").write_text(
            json.dumps({"history": [{"action": "finish"}]}),
            encoding="utf-8",
        )
        result = finalize(
            status=None,
            workspace=ws,
            agent_dir=agent,
            context_path=ws / ".cowork" / "cli_context.json",
        )
        self.assertEqual(result["status"], "SUCCESS")
        dumped = json.loads(log.read_text(encoding="utf-8"))
        self.assertEqual(dumped["status"], "SUCCESS")
        self.assertIn("completion", dumped)


if __name__ == "__main__":
    unittest.main()
