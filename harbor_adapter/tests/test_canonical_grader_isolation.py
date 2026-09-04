#!/usr/bin/env python3
"""Regression: Completion contract, DB tasks grader sidecar, and evaluator classification."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harbor_adapter"))

from generate_harbor_canonical import (  # noqa: E402
    _CLASSIFIER_PYTHON_SCRIPT,
    _compose,
    _task_toml,
    _verifier,
)


class GraderIsolationTests(unittest.TestCase):
    """Regression tests for DB grader sidecar isolation and handoff verification."""

    def test_db_compose_has_grader_on_db_net_only(self) -> None:
        """Verify DB docker-compose attaches grader exclusively to db_net and main mounts grader_out read-only."""
        yml = _compose("demo-task", has_db=True, has_workspace=True, has_mock=False)
        self.assertIn("grader:", yml)
        self.assertIn("grader_out:/grader_out:ro", yml)
        # main networks block should not list db_net as a main attachment.
        main = yml.split("workspace-prep:")[0]
        self.assertIn("- agent_net", main)
        self.assertNotIn("- db_net", main)
        grader = yml.split("\n  grader:")[1].split("mcp-gateway-public:")[0]
        self.assertIn("- db_net", grader)
        self.assertNotIn("- agent_net", grader)
        self.assertIn('COWORK_GRADER: "1"', grader)
        self.assertIn("PGHOST: postgres", grader)

    def test_no_db_compose_omits_grader(self) -> None:
        """Verify non-DB docker-compose does not include grader sidecar."""
        yml = _compose("demo-task", has_db=False, has_workspace=True, has_mock=False)
        self.assertNotIn("grader:", yml)
        self.assertNotIn("grader_out", yml)

    def test_task_toml_collects_grader_for_db(self) -> None:
        """Verify task.toml includes verifier.collect for grader on DB tasks only."""
        toml = _task_toml("demo-task", [{"name": "emails"}], has_db=True)
        self.assertIn('service = "grader"', toml)
        self.assertIn("bash /tests/test.sh", toml)
        toml2 = _task_toml("demo-task", [{"name": "excel"}], has_db=False)
        self.assertNotIn('service = "grader"', toml2)

    def test_verifier_handoff_and_grader_publish(self) -> None:
        """Verify verifier script contains handoff logic and syntax check passes."""
        sh = _verifier("demo-task", has_db=True)
        self.assertIn('"$HANDOFF_DIR/reward.txt"', sh)
        self.assertIn('COMPLETION="$HANDOFF_DIR/completion.json"', sh)
        self.assertIn("evaluator.stdout.log", sh)
        self.assertIn("evaluator.stderr.log", sh)
        self.assertIn("COWORK_GRADER", sh)
        self.assertIn("accepted grader sidecar reward", sh)
        self.assertIn("grader evaluator failed", sh)

        main = sh.split(
            "# Shared main has no PG env or DB network; it only consumes handoff.", 1
        )[1]
        self.assertIn("evaluation/main.py", sh)
        self.assertNotIn("evaluation/main.py", main)
        self.assertIn("completion marker is missing", main)

        with tempfile.NamedTemporaryFile("w", suffix=".sh") as script:
            script.write(sh)
            script.flush()
            result = subprocess.run(
                ["bash", "-n", script.name],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_file_only_verifier_keeps_direct_evaluation(self) -> None:
        """Verify file-only verifier executes direct evaluation in main."""
        sh = _verifier("demo-task", has_db=False)
        self.assertIn("evaluation/main.py", sh)
        self.assertNotIn("/grader_out/completion.json", sh)
        with tempfile.NamedTemporaryFile("w", suffix=".sh") as script:
            script.write(sh)
            script.flush()
            result = subprocess.run(
                ["bash", "-n", script.name],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)


class CompletionContractClassifierTests(unittest.TestCase):
    """Regression tests for the robust evaluator classification and completion marker creation."""

    def _run_classifier(
        self,
        mode: str,
        rc: int,
        stdout: str,
        stderr: str,
        result_json_data: dict | str | None = None,
        eval_res_data: dict | None = None,
    ) -> tuple[dict, str | None]:
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

            if result_json_data is not None:
                if isinstance(result_json_data, str):
                    result_json_path.write_text(result_json_data, encoding="utf-8")
                else:
                    result_json_path.write_text(
                        json.dumps(result_json_data), encoding="utf-8"
                    )

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
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            self.assertIn("CLASSIFICATION:", res.stdout)

            self.assertTrue(
                completion_out.exists(), "completion.json must be written atomically"
            )
            completion = json.loads(completion_out.read_text(encoding="utf-8"))

            reward_val = None
            if reward_out.exists():
                reward_val = reward_out.read_text(encoding="utf-8").strip()

            return completion, reward_val

    def test_oracle_rc0_reward1(self) -> None:
        """Happy path: evaluator exits 0, producing reward 1 with completed status."""
        stdout = "=== Check 1 ===\n[PASS] Output verified\nPASS\n"
        comp, reward = self._run_classifier("oracle", 0, stdout, "")
        self.assertEqual(comp["status"], "succeeded")
        self.assertEqual(comp["technical_status"], "completed")
        self.assertEqual(comp["reward"], 1)
        self.assertEqual(comp["evaluator_rc"], 0)
        self.assertTrue(comp["evaluator_completed"])
        self.assertIsNone(comp["error_type"])
        self.assertEqual(reward, "1")

    def test_oracle_rc1_failed_checks_reward0(self) -> None:
        """Content failure: evaluator exits 1 due to failed checks; completion is present with reward 0."""
        stdout = "=== Check 1 ===\n[FAIL] Missing row\nOverall: 19/26 checks passed (73.1%)\nCRITICAL FAIL\nFAIL\n"
        comp, reward = self._run_classifier("oracle", 1, stdout, "")
        self.assertEqual(comp["status"], "succeeded")
        self.assertEqual(comp["technical_status"], "completed")
        self.assertEqual(comp["reward"], 0)
        self.assertEqual(comp["evaluator_rc"], 1)
        self.assertTrue(comp["evaluator_completed"])
        self.assertIsNone(comp["error_type"])
        self.assertEqual(reward, "0")

    def test_oracle_python_traceback_error(self) -> None:
        """Infrastructure / traceback failure: status is failed/error, marker created."""
        stderr = (
            'Traceback (most recent call last):\n  File "main.py", line 40, in'
            ' <module>\nKeyError: "PGPASSWORD"\n'
        )
        comp, reward = self._run_classifier("oracle", 1, "", stderr)
        self.assertEqual(comp["status"], "failed")
        self.assertEqual(comp["technical_status"], "error")
        self.assertEqual(comp["reward"], 0)
        self.assertEqual(comp["evaluator_rc"], 1)
        self.assertFalse(comp["evaluator_completed"])
        self.assertEqual(comp["error_type"], "python_traceback")
        self.assertIn('KeyError: "PGPASSWORD"', comp["error_message"])
        self.assertIsNone(reward)

    def test_oracle_missing_result_json_with_checks(self) -> None:
        """When result JSON is missing but stdout contains check summary, treat as content failure."""
        stdout = "=== Check 1 ===\n[PASS] A\n[FAIL] B\nOverall: 1/2 checks passed\nFAIL\n"
        comp, reward = self._run_classifier("oracle", 1, stdout, "")
        self.assertFalse(comp["result_present"])
        self.assertEqual(comp["status"], "succeeded")
        self.assertEqual(comp["reward"], 0)
        self.assertEqual(reward, "0")

    def test_oracle_corrupted_result_json(self) -> None:
        """When result JSON is corrupted string, does not crash and handles gracefully."""
        stdout = "=== Check 1 ===\n[FAIL] B\nFAIL\n"
        comp, reward = self._run_classifier(
            "oracle", 1, stdout, "", result_json_data="{corrupted json"
        )
        self.assertFalse(comp["result_present"])
        self.assertEqual(comp["status"], "succeeded")
        self.assertEqual(comp["reward"], 0)
        self.assertEqual(reward, "0")

    def test_ua_pass_true_reward1(self) -> None:
        """UA mode: eval_res.json has pass: true -> reward 1."""
        comp, reward = self._run_classifier(
            "ua", 0, "run_eval done", "", eval_res_data={"pass": True}
        )
        self.assertEqual(comp["status"], "succeeded")
        self.assertEqual(comp["technical_status"], "completed")
        self.assertEqual(comp["reward"], 1)
        self.assertEqual(reward, "1")

    def test_ua_pass_false_reward0(self) -> None:
        """UA mode: eval_res.json has pass: false -> reward 0."""
        comp, reward = self._run_classifier(
            "ua", 0, "run_eval done", "", eval_res_data={"pass": False}
        )
        self.assertEqual(comp["status"], "succeeded")
        self.assertEqual(comp["technical_status"], "completed")
        self.assertEqual(comp["reward"], 0)
        self.assertEqual(reward, "0")

    def test_ua_missing_traj_log(self) -> None:
        """UA mode: missing traj_log.json produces error."""
        stderr = "Cowork traj_log.json not found\n"
        comp, reward = self._run_classifier("ua", 2, "", stderr)
        self.assertEqual(comp["status"], "failed")
        self.assertEqual(comp["technical_status"], "error")
        self.assertEqual(comp["error_type"], "missing_traj_log")
        self.assertIsNone(reward)


if __name__ == "__main__":
    unittest.main()
