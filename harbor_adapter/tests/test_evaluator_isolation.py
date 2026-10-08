#!/usr/bin/env python3
"""Grader contract stays off every agent-readable path.

The leak assertion is intentionally compatible with the pre-fix tree: on that
tree the workspace context is the full TaskConfig, so the assertion fails.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(os.environ["COWORK_ISOLATION_ROOT"]) if os.environ.get("COWORK_ISOLATION_ROOT") else Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "harbor_adapter"))

LEAK_TOKENS = ('"evaluation"', "evaluation_command", "groundtruth_workspace")
AGENT_MOUNT_SERVICES = (
    "main",
    "workspace-prep",
    "mcp-gateway-workspace",
    "mcp-gateway-public",
    "mock-pages",
)
DB_TASK = "insales-product-launch-dashboard"


def _trusted_contract_file(task: str) -> Path:
    from native_mcp.prep.prepare_workspace import build_task_config_dict

    directory = Path(tempfile.mkdtemp(prefix="cowork-trusted-contract-"))
    path = directory / "task_contract.json"
    path.write_text(
        json.dumps(build_task_config_dict(task, repo_root=ROOT)),
        encoding="utf-8",
    )
    return path
NO_GT_TASK = "canvas-enrollment-gsheet"
EMPTY_GT_TASK = "scholarly-fetch-gsheet-citation"


def _visible_agent_context(task: str) -> dict:
    from native_mcp.prep import prepare_workspace as pw

    if hasattr(pw, "build_agent_context"):
        return pw.build_agent_context(
            task,
            provider="harbor-canonical",
            model="n/a",
            workspace="/workspace/cowork_shared",
            log_file="/logs/artifacts/cowork/traj_log.json",
            start_time="t0",
        )
    return {
        "task": task,
        "task_config": pw.resolve_task_config(
            task,
            "/workspace/cowork_shared",
            "/logs/artifacts/cowork/traj_log.json",
        ),
    }


def _service_block(compose: str, name: str) -> str:
    import re

    match = re.search(rf"(?ms)^  {re.escape(name)}:.*?(?=^  [a-z0-9-]+:|\Z)", compose)
    return match.group(0) if match else ""


def _assert_agent_tree_clean(test: unittest.TestCase, task_dir: Path) -> None:
    prep_contract = task_dir / "environment" / "prep" / "task_contract.json"
    test.assertFalse(prep_contract.exists(), prep_contract)
    compose = (task_dir / "environment" / "docker-compose.yaml").read_text(encoding="utf-8")
    for service in AGENT_MOUNT_SERVICES:
        block = _service_block(compose, service)
        if not block:
            continue
        test.assertNotIn("grader_private", block)
        test.assertNotIn("task_contract", block)
        test.assertNotIn("../tests:", block)
    for path in task_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(task_dir).as_posix()
        if rel.startswith("tests/") or rel.startswith("solution/"):
            continue
        if path.suffix not in {".md", ".toml", ".yaml", ".yml", ".json", ".sh"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in LEAK_TOKENS:
            test.assertNotIn(token, text, f"{rel} contains {token}")


class LeakOnHeadTests(unittest.TestCase):
    def test_agent_workspace_has_no_evaluation(self) -> None:
        ctx = _visible_agent_context(DB_TASK)
        blob = json.dumps(ctx)
        self.assertNotIn("task_config", ctx)
        for token in LEAK_TOKENS:
            self.assertNotIn(token, blob)


class EvaluatorIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from generate_harbor_canonical import SOURCE_TASKS, _preprocess_needs_pg
        from native_mcp.catalog import load_catalog, needs_db

        catalog = load_catalog()
        chosen = None
        for task_dir in sorted(SOURCE_TASKS.iterdir()):
            cfg_path = task_dir / "task_config.json"
            if not cfg_path.is_file():
                continue
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            servers = cfg.get("needed_mcp_servers") or []
            if any(needs_db(name, catalog) for name in servers):
                continue
            if _preprocess_needs_pg(task_dir):
                continue
            chosen = task_dir.name
            break
        if not chosen:
            raise AssertionError("no non-db task in the source tree")
        cls.non_db_task = chosen
        cls.catalog = catalog

    def _generate(self, task: str) -> Path:
        from generate_harbor_canonical import convert_one

        root = Path(tempfile.mkdtemp(prefix="cowork-iso-"))
        convert_one(task, root, self.catalog)
        return root / task

    def _public_and_contract(self, task: str):
        from native_mcp.prep.prepare_workspace import build_agent_context, build_task_config_dict
        from utils.data_structures.task_config import TaskConfig

        public = build_agent_context(
            task,
            provider="harbor-canonical",
            model="n/a",
            workspace="/workspace/cowork_shared",
            log_file="/logs/artifacts/cowork/traj_log.json",
            start_time="t0",
        )
        contract = build_task_config_dict(task, repo_root=ROOT)
        restored = TaskConfig.from_dict(dict(contract))
        return public, contract, restored

    def test_agent_readable_mounts_have_no_private_contract(self) -> None:
        task_dir = self._generate(DB_TASK)
        _assert_agent_tree_clean(self, task_dir)
        compose = (task_dir / "environment" / "docker-compose.yaml").read_text(encoding="utf-8")
        gateway = _service_block(compose, "mcp-gateway-workspace")
        self.assertNotIn("/tests", gateway)
        self.assertNotIn("cowork_artifacts", gateway)
        main = _service_block(compose, "main")
        self.assertNotIn("../tests:", main)
        self.assertIn("rm -f /logs/artifacts/cowork/traj_log.json", main)
        self.assertIn("discard_public_completion()", main)

    def test_terminal_and_filesystem_cannot_reach_grader_contract(self) -> None:
        workspace = "/workspace/cowork_shared"
        contract = "/tests/grader_private/task_contract.json"
        self.assertFalse(os.path.abspath(contract).startswith(os.path.abspath(workspace)))
        terminal = (ROOT / "configs" / "mcp_servers" / "terminal.yaml").read_text(encoding="utf-8")
        filesystem = (ROOT / "configs" / "mcp_servers" / "filesystem.yaml").read_text(encoding="utf-8")
        self.assertIn('ALLOWED_DIR: "${agent_workspace}"', terminal)
        self.assertIn('"${agent_workspace}"', filesystem)
        task_dir = self._generate(DB_TASK)
        compose = (task_dir / "environment" / "docker-compose.yaml").read_text(encoding="utf-8")
        for service in ("mcp-gateway-workspace", "mcp-gateway-public", "main", "workspace-prep"):
            block = _service_block(compose, service)
            self.assertNotIn("grader_private", block)
            self.assertNotIn("../tests:", block)

    def test_grader_only_contract_is_full_task_config(self) -> None:
        from utils.data_structures.task_config import Evaluation, TaskConfig

        task_dir = self._generate(DB_TASK)
        path = task_dir / "tests" / "grader_private" / "task_contract.json"
        self.assertTrue(path.is_file())
        self.assertFalse((task_dir / "solution" / "task_contract.json").exists())
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("evaluation", stored)
        self.assertIsNone(stored["evaluation"]["evaluation_command"])
        self.assertIsNone(stored["evaluation"]["groundtruth_workspace"])
        self.assertNotIn("evaluation.main", path.read_text(encoding="utf-8"))
        _public, contract, restored = self._public_and_contract(DB_TASK)
        self.assertIn("evaluation", contract)
        self.assertIsNone(restored.evaluation.evaluation_command)
        self.assertIsNone(restored.evaluation.groundtruth_workspace)
        previous = os.getcwd()
        os.chdir(ROOT)
        try:
            fresh = Evaluation.build(DB_TASK)
        finally:
            os.chdir(previous)
        self.assertIn("evaluation.main", fresh.evaluation_command)
        self.assertTrue(fresh.groundtruth_workspace)
        TaskConfig.from_dict(dict(contract))
        grader = _service_block(
            (task_dir / "environment" / "docker-compose.yaml").read_text(encoding="utf-8"),
            "grader",
        )
        self.assertIn("../tests:/tests:ro", grader)
        script = (task_dir / "tests" / "test.sh").read_text(encoding="utf-8")
        self.assertIn("/tests/grader_private/task_contract.json", script)
        self.assertIn('--contract "$CONTRACT"', script)
        self.assertIn('--task "$TASK"', script)
        self.assertIn("assemble", script)

    def test_valid_content_pass_and_fail(self) -> None:
        passed = _classify("ua", 0, _grader_region("Pass:    True\n"), "", {"pass": True})
        failed = _classify("ua", 1, _grader_region("Pass:    False\n"), "", {"pass": False})
        self.assertEqual(passed["technical_status"], "completed")
        self.assertEqual(passed["reward"], 1)
        self.assertTrue(passed["evaluator_completed"])
        self.assertIsNone(passed["error_type"])
        self.assertEqual(failed["technical_status"], "completed")
        self.assertEqual(failed["reward"], 0)
        self.assertEqual(failed["error_message"], "Content evaluation failed")
        self.assertTrue(failed["evaluator_completed"])
        self.assertIsNone(failed["error_type"])

    def test_grader_crash_without_pass_false_is_technical(self) -> None:
        crashed = _classify("ua", 1, "grader exited\n", "", {"pass": False})
        self.assertEqual(crashed["technical_status"], "error")
        self.assertEqual(crashed["error_type"], "evaluator_failure")
        self.assertFalse(crashed["evaluator_completed"])
        self.assertNotEqual(crashed["error_message"], "Content evaluation failed")
        missing_verdict = _classify("ua", 0, "run_eval done\n", "", {"pass": True})
        self.assertEqual(missing_verdict["technical_status"], "error")
        self.assertEqual(missing_verdict["error_type"], "evaluator_failure")
        self.assertFalse(missing_verdict["evaluator_completed"])

    def test_traceback_instead_of_verdict_is_technical(self) -> None:
        crashed = _classify(
            "ua",
            1,
            "",
            "Traceback (most recent call last):\nRuntimeError: boom\n",
            None,
        )
        self.assertEqual(crashed["technical_status"], "error")
        self.assertEqual(crashed["error_type"], "python_traceback")
        self.assertFalse(crashed["evaluator_completed"])

    def test_missing_context_is_technical_error(self) -> None:
        from native_mcp.workspace_lifecycle import finalize

        workspace = Path(tempfile.mkdtemp(prefix="cowork-missing-"))
        with self.assertRaises(SystemExit) as caught:
            finalize(
                workspace=workspace,
                agent_dir=Path(tempfile.mkdtemp(prefix="cowork-agent-")),
                context_path=workspace / ".cowork" / "cli_context.json",
            )
        self.assertIn("missing workspace context", str(caught.exception))

    def test_corrupt_context_is_technical_error(self) -> None:
        from native_mcp.workspace_lifecycle import finalize

        workspace = Path(tempfile.mkdtemp(prefix="cowork-corrupt-"))
        context = workspace / ".cowork" / "cli_context.json"
        context.parent.mkdir(parents=True)
        context.write_text("{", encoding="utf-8")
        with self.assertRaises(SystemExit) as caught:
            finalize(
                workspace=workspace,
                agent_dir=Path(tempfile.mkdtemp(prefix="cowork-agent-")),
                context_path=context,
            )
        self.assertIn("corrupt workspace context", str(caught.exception))

    def test_stale_context_is_removed_before_prepare(self) -> None:
        from native_mcp.prep.prepare_workspace import reset_agent_context, write_agent_context
        from native_mcp.workspace_lifecycle import finalize

        workspace = Path(tempfile.mkdtemp(prefix="cowork-stale-"))
        context = workspace / ".cowork" / "cli_context.json"
        context.parent.mkdir(parents=True)
        context.write_text(
            json.dumps({"task_config": {"evaluation": {"evaluation_command": "secret"}}}),
            encoding="utf-8",
        )
        reset_agent_context(context)
        self.assertFalse(context.exists())
        with self.assertRaises(SystemExit):
            finalize(
                workspace=workspace,
                agent_dir=Path(tempfile.mkdtemp(prefix="cowork-agent-")),
                context_path=context,
            )
        write_agent_context(
            context,
            _visible_agent_context(DB_TASK),
        )
        self.assertNotIn("evaluation", context.read_text(encoding="utf-8"))
        self.assertFalse(context.with_name(context.name + ".tmp").exists())

    def test_repeated_finalize_does_not_publish_contract(self) -> None:
        from native_mcp.workspace_lifecycle import assemble_trajectory, finalize

        workspace = Path(tempfile.mkdtemp(prefix="cowork-repeat-"))
        log = workspace / "traj_log.json"
        context = workspace / ".cowork" / "cli_context.json"
        context.parent.mkdir(parents=True)
        context.write_text(json.dumps(_visible_agent_context(DB_TASK) | {"log_file": str(log)}), encoding="utf-8")
        agent = Path(tempfile.mkdtemp(prefix="cowork-agent-"))
        (agent / "openhands.trajectory.json").write_text(
            json.dumps({"history": [{"action": "finish"}]}),
            encoding="utf-8",
        )
        public_path = _bind_completion(self)
        finalize(workspace=workspace, agent_dir=agent, context_path=context)
        finalize(workspace=workspace, agent_dir=agent, context_path=context)
        public = json.loads(public_path.read_text(encoding="utf-8"))
        self.assertNotIn("evaluation", json.dumps(public))
        self.assertFalse(log.exists())
        planted = workspace / "task_contract.json"
        planted.write_text(
            json.dumps({"evaluation": {"evaluation_command": "/tmp/evil-grader"}}),
            encoding="utf-8",
        )
        context.write_text(
            json.dumps({"evaluation_command": "/tmp/evil-from-context", "task": "other-task"}),
            encoding="utf-8",
        )
        from native_mcp import workspace_lifecycle as wl

        previous_workspace = wl.SHARED_WORKSPACE
        wl.SHARED_WORKSPACE = workspace
        self.addCleanup(lambda: setattr(wl, "SHARED_WORKSPACE", previous_workspace))
        with self.assertRaises(SystemExit) as refused_contract:
            assemble_trajectory(
                task=DB_TASK,
                contract_path=planted,
                completion_path=public_path,
                log_path=log,
            )
        self.assertIn("refusing agent-writable contract", str(refused_contract.exception))
        wl.SHARED_WORKSPACE = previous_workspace
        trusted = _trusted_contract_file(DB_TASK)
        first = assemble_trajectory(
            task=DB_TASK, contract_path=trusted, completion_path=public_path, log_path=log
        )
        second = assemble_trajectory(
            task=DB_TASK, contract_path=trusted, completion_path=public_path, log_path=log
        )
        self.assertEqual(first["status"], second["status"])
        dumped = json.loads(log.read_text(encoding="utf-8"))
        self.assertIsNone(dumped["config"]["evaluation"]["evaluation_command"])
        self.assertIsNone(dumped["config"]["evaluation"]["groundtruth_workspace"])
        self.assertNotIn("/tmp/evil-grader", json.dumps(dumped))
        self.assertNotIn("evil-from-context", json.dumps(dumped))
        self.assertTrue(planted.is_file())

    def test_db_task_keeps_contract_on_grader_mount(self) -> None:
        task_dir = self._generate(DB_TASK)
        _assert_agent_tree_clean(self, task_dir)
        compose = (task_dir / "environment" / "docker-compose.yaml").read_text(encoding="utf-8")
        self.assertIn("\n  grader:", compose)
        self.assertIn("../tests:/tests:ro", _service_block(compose, "grader"))

    def test_non_db_task_has_no_grader_mount_during_agent(self) -> None:
        task_dir = self._generate(self.non_db_task)
        _assert_agent_tree_clean(self, task_dir)
        compose = (task_dir / "environment" / "docker-compose.yaml").read_text(encoding="utf-8")
        self.assertNotIn("\n  grader:", compose)
        self.assertNotIn("../tests:", compose)
        script = (task_dir / "tests" / "test.sh").read_text(encoding="utf-8")
        self.assertIn("/tests/grader_private/task_contract.json", script)
        private = json.loads((task_dir / "tests" / "grader_private" / "task_contract.json").read_text(encoding="utf-8"))
        self.assertIsNone(private["evaluation"]["evaluation_command"])
        self.assertIsNone(private["evaluation"]["groundtruth_workspace"])

    def test_task_without_groundtruth_still_isolates_contract(self) -> None:
        public, contract, restored = self._public_and_contract(NO_GT_TASK)
        from utils.data_structures.task_config import Evaluation

        self.assertNotIn("evaluation", json.dumps(public))
        self.assertIsNone(restored.evaluation.groundtruth_workspace)
        self.assertIsNone(restored.evaluation.evaluation_command)
        previous = os.getcwd()
        os.chdir(ROOT)
        try:
            fresh = Evaluation.build(NO_GT_TASK)
        finally:
            os.chdir(previous)
        self.assertTrue(fresh.evaluation_command)
        self.assertIsNone(fresh.groundtruth_workspace)
        task_dir = self._generate(NO_GT_TASK)
        _assert_agent_tree_clean(self, task_dir)
        self.assertTrue((task_dir / "solution" / "NO_GROUNDTRUTH").is_file())
        self.assertFalse((task_dir / "solution" / "task_contract.json").exists())
        private = json.loads((task_dir / "tests" / "grader_private" / "task_contract.json").read_text(encoding="utf-8"))
        self.assertIsNone(private["evaluation"]["evaluation_command"])

    def test_empty_groundtruth_keeps_path_in_grader_contract_only(self) -> None:
        from utils.data_structures.task_config import Evaluation

        public, _contract, restored = self._public_and_contract(EMPTY_GT_TASK)
        self.assertNotIn("groundtruth_workspace", json.dumps(public))
        self.assertIsNone(restored.evaluation.groundtruth_workspace)
        self.assertIsNone(restored.evaluation.evaluation_command)
        previous = os.getcwd()
        os.chdir(ROOT)
        try:
            fresh = Evaluation.build(EMPTY_GT_TASK)
        finally:
            os.chdir(previous)
        self.assertTrue(fresh.groundtruth_workspace)
        self.assertTrue(Path(fresh.groundtruth_workspace).is_dir())
        task_dir = self._generate(EMPTY_GT_TASK)
        _assert_agent_tree_clean(self, task_dir)
        self.assertTrue((task_dir / "solution" / "NO_GROUNDTRUTH").is_file())
        private = json.loads((task_dir / "tests" / "grader_private" / "task_contract.json").read_text(encoding="utf-8"))
        self.assertIsNone(private["evaluation"]["groundtruth_workspace"])


def _classify(mode: str, rc: int, stdout: str, stderr: str, eval_res) -> dict:
    import subprocess

    from generate_harbor_canonical import _CLASSIFIER_PYTHON_SCRIPT

    tmp = Path(tempfile.mkdtemp(prefix="cowork-class-"))
    stdout_path = tmp / "stdout"
    stderr_path = tmp / "stderr"
    result_path = tmp / "result.json"
    eval_path = tmp / "eval_res.json"
    completion = tmp / "completion.json"
    reward = tmp / "reward.txt"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    if eval_res is not None:
        eval_path.write_text(json.dumps(eval_res), encoding="utf-8")
    script = tmp / "classify.py"
    script.write_text(_CLASSIFIER_PYTHON_SCRIPT, encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            str(script),
            mode,
            str(rc),
            str(stdout_path),
            str(stderr_path),
            str(result_path),
            str(eval_path),
            str(completion),
            str(reward),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completion.read_text(encoding="utf-8"))


def _grader_region(body: str) -> str:
    text = body if body.endswith("\n") or body == "" else body + "\n"
    return "== Evaluation STDOUT ==\n" + text + "== Evaluation STDERR ==\n"


def _bind_completion(test: unittest.TestCase) -> Path:
    from native_mcp import workspace_lifecycle as wl

    directory = Path(tempfile.mkdtemp(prefix="cowork-artifacts-"))
    previous = wl.AGENT_COMPLETION_PATH
    wl.AGENT_COMPLETION_PATH = directory / "agent_completion.json"
    test.addCleanup(lambda: setattr(wl, "AGENT_COMPLETION_PATH", previous))
    return wl.AGENT_COMPLETION_PATH


def _finish_agent() -> Path:
    agent = Path(tempfile.mkdtemp(prefix="cowork-agent-"))
    (agent / "openhands.trajectory.json").write_text(
        json.dumps({"history": [{"action": "finish"}]}),
        encoding="utf-8",
    )
    return agent


def _public_context(workspace: Path, log_file: str) -> Path:
    context = workspace / ".cowork" / "cli_context.json"
    context.parent.mkdir(parents=True, exist_ok=True)
    context.write_text(
        json.dumps(
            {
                "task": DB_TASK,
                "provider": "harbor-canonical",
                "model": "n/a",
                "workspace": str(workspace),
                "log_file": log_file,
                "start_time": "t0",
            }
        ),
        encoding="utf-8",
    )
    return context


class CompletionTamperTests(unittest.TestCase):
    def test_precreated_file_and_symlink_cannot_redirect_publication(self) -> None:
        from native_mcp.workspace_lifecycle import finalize

        workspace = Path(tempfile.mkdtemp(prefix="cowork-tamper-"))
        diverted = workspace / "diverted" / "traj_log.json"
        context = _public_context(workspace, str(diverted))
        completion = _bind_completion(self)
        completion.write_text('{"status": "success", "reward": 1}\n', encoding="utf-8")
        finalize(workspace=workspace, agent_dir=_finish_agent(), context_path=context)
        published = json.loads(completion.read_text(encoding="utf-8"))
        self.assertEqual(published["status"], "success")
        self.assertNotIn("reward", published)
        self.assertFalse(diverted.exists())
        stolen = workspace / "stolen.json"
        stolen.write_text('{"reward": 1, "evaluation_command": "/tmp/evil"}\n', encoding="utf-8")
        completion.unlink()
        completion.symlink_to(stolen)
        finalize(workspace=workspace, agent_dir=_finish_agent(), context_path=context)
        self.assertEqual(
            stolen.read_text(encoding="utf-8"),
            '{"reward": 1, "evaluation_command": "/tmp/evil"}\n',
        )
        self.assertFalse(completion.is_symlink())
        replaced = json.loads(completion.read_text(encoding="utf-8"))
        self.assertEqual(replaced["status"], "success")
        self.assertNotIn("reward", replaced)
        self.assertNotIn("evaluation", json.dumps(replaced))

    def test_log_file_in_context_does_not_select_the_destination(self) -> None:
        from native_mcp.workspace_lifecycle import finalize

        workspace = Path(tempfile.mkdtemp(prefix="cowork-logfile-"))
        diverted = workspace / "elsewhere" / "agent_completion.json"
        context = _public_context(workspace, str(diverted))
        completion = _bind_completion(self)
        finalize(workspace=workspace, agent_dir=_finish_agent(), context_path=context)
        self.assertTrue(completion.is_file())
        self.assertFalse(diverted.exists())

    def test_missing_corrupt_stale_retry_and_foreign_destination(self) -> None:
        from native_mcp.workspace_lifecycle import (
            assemble_trajectory,
            discard_public_completion,
            finalize,
        )
        from native_mcp import workspace_lifecycle as wl

        workspace = Path(tempfile.mkdtemp(prefix="cowork-stale-completion-"))
        completion = _bind_completion(self)
        completion.write_text('{"status": "success", "reward": 1}\n', encoding="utf-8")
        discard_public_completion()
        self.assertFalse(completion.exists())
        context = _public_context(workspace, str(workspace / "evil" / "traj_log.json"))
        completion.write_text("stale-not-json", encoding="utf-8")
        finalize(workspace=workspace, agent_dir=_finish_agent(), context_path=context)
        first = json.loads(completion.read_text(encoding="utf-8"))
        self.assertEqual(first["status"], "success")
        self.assertFalse(list(completion.parent.glob(".agent_completion.*.tmp")))
        discard_public_completion()
        self.assertFalse(completion.exists())
        failed = Path(tempfile.mkdtemp(prefix="cowork-failed-agent-"))
        (failed / "openhands.txt").write_text("exit 0 without finish\n", encoding="utf-8")
        finalize(workspace=workspace, agent_dir=failed, context_path=context)
        second = json.loads(completion.read_text(encoding="utf-8"))
        self.assertEqual(second["status"], "failed")
        self.assertNotEqual(second, first)
        log = workspace / "traj_log.json"
        trusted = _trusted_contract_file(DB_TASK)
        completion.unlink()
        with self.assertRaises(SystemExit) as missing:
            assemble_trajectory(
                task=DB_TASK,
                contract_path=trusted,
                completion_path=completion,
                log_path=log,
            )
        self.assertIn("missing agent completion", str(missing.exception))
        completion.write_text("{", encoding="utf-8")
        with self.assertRaises(SystemExit) as corrupt:
            assemble_trajectory(
                task=DB_TASK,
                contract_path=trusted,
                completion_path=completion,
                log_path=log,
            )
        self.assertIn("corrupt agent completion", str(corrupt.exception))
        foreign = workspace / "foreign.json"
        foreign.write_text(json.dumps(second), encoding="utf-8")
        with self.assertRaises(SystemExit) as refused:
            assemble_trajectory(
                task=DB_TASK,
                contract_path=trusted,
                completion_path=foreign,
                log_path=log,
            )
        self.assertIn("refusing completion path", str(refused.exception))
        self.assertFalse(log.exists())
        proc = __import__("subprocess").run(
            [
                sys.executable,
                str(ROOT / "harbor_adapter" / "native_mcp" / "workspace_lifecycle.py"),
                "assemble",
                "--task",
                DB_TASK,
                "--contract",
                str(trusted),
                "--completion",
                str(foreign),
                "--log",
                str(log),
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("refusing completion path", proc.stderr + proc.stdout)
        self.assertFalse(log.exists())
        forged = {
            "task": DB_TASK,
            "status": "success",
            "start_time": "t0",
            "end_time": "t1",
            "reward": 1,
            "evaluation_command": "/tmp/evil-grader",
            "completion": {
                "confirmed": True,
                "reason": "forged",
                "framework": "openhands",
                "evidence": ["finish"],
                "stop_reason": None,
            },
        }
        completion.write_text(json.dumps(forged), encoding="utf-8")
        with self.assertRaises(SystemExit) as extra:
            assemble_trajectory(
                task=DB_TASK,
                contract_path=trusted,
                completion_path=completion,
                log_path=log,
            )
        self.assertIn("refusing non-public field", str(extra.exception))
        self.assertFalse(log.exists())
        innocent = {
            "task": DB_TASK,
            "status": "success",
            "start_time": "t0",
            "end_time": "t1",
            "completion": {
                "confirmed": True,
                "reason": "forged-evidence",
                "framework": "openhands",
                "evidence": ["evaluation_command=/tmp/evil-grader", "reward=1"],
                "stop_reason": None,
            },
        }
        completion.write_text(json.dumps(innocent), encoding="utf-8")
        assemble_trajectory(
            task=DB_TASK,
            contract_path=trusted,
            completion_path=wl.AGENT_COMPLETION_PATH,
            log_path=log,
        )
        dumped = json.loads(log.read_text(encoding="utf-8"))
        self.assertIsNone(dumped["config"]["evaluation"]["evaluation_command"])
        self.assertIsNone(dumped["config"]["evaluation"]["groundtruth_workspace"])
        self.assertNotIn("/tmp/evil-grader", json.dumps(dumped["config"]))
        self.assertNotIn("reward", dumped)

    def test_parent_symlink_is_refused(self) -> None:
        from native_mcp.workspace_lifecycle import _atomic_publish_completion
        from native_mcp import workspace_lifecycle as wl

        root = Path(tempfile.mkdtemp(prefix="cowork-parent-link-"))
        real = root / "real"
        real.mkdir()
        linked = root / "linked"
        linked.symlink_to(real)
        previous = wl.AGENT_COMPLETION_PATH
        wl.AGENT_COMPLETION_PATH = linked / "agent_completion.json"
        self.addCleanup(lambda: setattr(wl, "AGENT_COMPLETION_PATH", previous))
        with self.assertRaises(SystemExit) as caught:
            _atomic_publish_completion(wl.AGENT_COMPLETION_PATH, {"status": "success"})
        self.assertIn("symlinked completion directory", str(caught.exception))
        self.assertFalse((real / "agent_completion.json").exists())


class GeneratedEvaluatorPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from generate_harbor_canonical import convert_one
        from native_mcp.catalog import load_catalog

        task = "kulinar-nutrition-ppt"
        if not (ROOT / "tasks" / "finalpool" / task).is_dir():
            raise AssertionError(f"missing non-db task {task}")
        cls.task = task
        out = Path(tempfile.mkdtemp(prefix="cowork-nondb-gen-"))
        convert_one(task, out, load_catalog())
        cls.task_dir = out / task
        script = (cls.task_dir / "tests" / "test.sh").read_text(encoding="utf-8")
        marker = "import sys, os, json, re\n"
        start = script.index(marker)
        end = script.index("\nPY\n", start)
        cls.classifier_source = script[start:end]
        cls.verifier_script = script

    def test_generated_script_order_and_fixed_completion(self) -> None:
        script = self.verifier_script
        self.assertLess(script.index("workspace_lifecycle.py assemble"), script.index("scripts/run_eval.py"))
        self.assertIn("PUBLIC=/logs/artifacts/cowork/agent_completion.json", script)
        self.assertIn('rm -f "$REWARD_FILE"', script)
        self.assertNotIn("echo 0 >", script)
        self.assertNotIn('ctx["log_file"]', script)
        compose = (self.task_dir / "environment" / "docker-compose.yaml").read_text(encoding="utf-8")
        self.assertNotIn("../tests:", compose)
        self.assertNotIn("\n  grader:", compose)
        toml = (self.task_dir / "task.toml").read_text(encoding="utf-8")
        self.assertIn("workspace_lifecycle.py finalize", toml)
        private = json.loads((self.task_dir / "tests" / "grader_private" / "task_contract.json").read_text(encoding="utf-8"))
        self.assertIsNone(private["evaluation"]["evaluation_command"])
        self.assertEqual(
            [path.relative_to(self.task_dir).as_posix() for path in self.task_dir.rglob("task_contract.json")],
            ["tests/grader_private/task_contract.json"],
        )
        self.assertIn('--contract "$CONTRACT"', script)
        self.assertIn('--task "$TASK"', script)
        self.assertIn('cd "${COWORK_EVAL_ROOT:-/workspace}"', script)

    def test_run_eval_matrix_through_generated_classifier(self) -> None:
        cases = [
            ("pass", "import sys\nprint('Pass:    True')\nsys.exit(0)\n", 0, "completed", 1),
            ("fail", "import sys\nprint('Pass:    False')\nsys.exit(1)\n", 1, "completed", 0),
            ("rc0-missing", "import sys\nprint('Overall: PASS')\nsys.exit(0)\n", 2, "error", None),
            ("rc1-missing", "import sys\nprint('Overall: FAIL')\nsys.exit(1)\n", 2, "error", None),
            ("rc2", "import sys\nprint('boom')\nsys.exit(2)\n", 2, "error", None),
            ("traceback", "raise RuntimeError('boom')\n", 2, "error", None),
            (
                "conflict",
                "import sys\nprint('Pass:    True')\nprint('Pass:    False')\nsys.exit(0)\n",
                2,
                "error",
                None,
            ),
            (
                "duplicate",
                "import sys\nprint('Pass:    True')\nprint('Pass:    True')\nsys.exit(0)\n",
                2,
                "error",
                None,
            ),
            ("rc0-false", "import sys\nprint('Pass:    False')\nsys.exit(0)\n", 2, "error", None),
            ("rc1-true", "import sys\nprint('Pass:    True')\nsys.exit(1)\n", 2, "error", None),
            ("signal", "import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n", None, "error", None),
            ("timeout-code", "import sys\nsys.exit(124)\n", 124, "error", None),
        ]
        for name, source, expect_rc, tech, reward in cases:
            with self.subTest(name=name):
                outcome = _run_generated_eval(self.classifier_source, source)
                if name == "signal":
                    self.assertGreaterEqual(outcome["rc"], 128)
                else:
                    self.assertEqual(outcome["rc"], expect_rc)
                self.assertNotIn("\nPass:", "\n" + _outside_region(outcome["stdout"]))
                self.assertEqual(outcome["completion"]["technical_status"], tech)
                if tech == "completed":
                    self.assertEqual(outcome["completion"]["reward"], reward)
                    self.assertEqual(outcome["reward_file"], f"{reward}\n")
                    self.assertTrue(outcome["completion"]["evaluator_completed"])
                else:
                    self.assertEqual(outcome["completion"]["status"], "failed")
                    self.assertFalse(outcome["completion"]["evaluator_completed"])
                    self.assertIsNone(outcome["reward_file"])
                    self.assertNotEqual(outcome["completion"]["error_message"], "Content evaluation failed")
                if name == "traceback":
                    self.assertEqual(outcome["completion"]["error_type"], "python_traceback")
                if name == "conflict":
                    self.assertIn("conflicting", outcome["completion"]["error_message"])
                if name == "duplicate":
                    self.assertIn("duplicate", outcome["completion"]["error_message"])

    def test_synthetic_wrapper_line_outside_region_is_not_content_fail(self) -> None:
        stdout = _grader_region("grader crashed\n") + "Pass:    False\n"
        classified = _classify_source(
            self.classifier_source,
            "ua",
            1,
            stdout,
            "",
            {"pass": False},
        )
        self.assertEqual(classified["completion"]["technical_status"], "error")
        self.assertFalse(classified["completion"]["evaluator_completed"])
        self.assertIsNone(classified["reward_file"])
        self.assertNotEqual(classified["completion"]["error_message"], "Content evaluation failed")

    def test_nondb_script_runs_completion_assemble_evaluate(self) -> None:
        import subprocess

        from native_mcp.workspace_lifecycle import finalize

        workspace = Path(tempfile.mkdtemp(prefix="cowork-nondb-run-"))
        artifacts = Path(tempfile.mkdtemp(prefix="cowork-nondb-artifacts-"))
        verifier = Path(tempfile.mkdtemp(prefix="cowork-nondb-verifier-"))
        fake = Path(tempfile.mkdtemp(prefix="cowork-nondb-cwd-"))
        task_root = fake / "tasks" / "finalpool" / self.task
        sentinel = fake / "grader-ran"
        fixture = (
            "import sys\n"
            f"open({str(sentinel)!r}, 'w').write('ran')\n"
            "print('Pass:    True')\n"
            "sys.exit(0)\n"
        )
        copied = self.task_dir / "tests" / "evaluation" / "main.py"
        copied.parent.mkdir(parents=True, exist_ok=True)
        copied.write_text(fixture, encoding="utf-8")
        context = _public_context(workspace, str(workspace / "evil" / "not-the-completion.json"))
        published_context = json.loads(context.read_text(encoding="utf-8"))
        published_context["task"] = self.task
        context.write_text(json.dumps(published_context), encoding="utf-8")
        from native_mcp import workspace_lifecycle as wl

        previous = wl.AGENT_COMPLETION_PATH
        wl.AGENT_COMPLETION_PATH = artifacts / "agent_completion.json"
        self.addCleanup(lambda: setattr(wl, "AGENT_COMPLETION_PATH", previous))
        forged = artifacts / "agent_completion.json"
        forged.write_text(
            json.dumps({"status": "success", "reward": 1, "evaluation_command": "/tmp/evil"}),
            encoding="utf-8",
        )
        finalize(workspace=workspace, agent_dir=_finish_agent(), context_path=context)
        published = json.loads(forged.read_text(encoding="utf-8"))
        self.assertNotIn("reward", published)
        self.assertNotIn("evaluation", json.dumps(published))
        script = _localize_verifier(
            self.verifier_script,
            tests_dir=self.task_dir / "tests",
            artifacts=artifacts,
            verifier=verifier,
            task_root=task_root,
        )
        lifecycle = (self.task_dir / "tests" / "verifier" / "workspace_lifecycle.py").read_text(encoding="utf-8")
        lifecycle = lifecycle.replace("/logs/artifacts/cowork", str(artifacts))
        (self.task_dir / "tests" / "verifier" / "workspace_lifecycle.py").write_text(lifecycle, encoding="utf-8")
        localized = workspace / "test.sh"
        localized.write_text(script, encoding="utf-8")
        env = {**os.environ, "COWORK_EVAL_ROOT": str(fake), "PYTHON_BIN": sys.executable}
        passed = subprocess.run(
            ["bash", str(localized)], capture_output=True, text=True, env=env
        )
        eval_log = verifier / "cowork-eval.log"
        extra = eval_log.read_text(encoding="utf-8", errors="replace")[-1500:] if eval_log.is_file() else ""
        self.assertEqual(passed.returncode, 0, extra + passed.stderr[-1500:] + passed.stdout[-500:])
        self.assertEqual((verifier / "reward.txt").read_text(encoding="utf-8"), "1\n")
        handoff = json.loads((verifier / "completion.json").read_text(encoding="utf-8"))
        self.assertEqual(handoff["technical_status"], "completed")
        self.assertEqual(handoff["reward"], 1)
        self.assertTrue(sentinel.is_file())
        traj = json.loads((artifacts / "traj_log.json").read_text(encoding="utf-8"))
        self.assertIsNone(traj["config"]["evaluation"]["evaluation_command"])
        self.assertIsNone(traj["config"]["evaluation"]["groundtruth_workspace"])
        self.assertNotIn("/tmp/evil", json.dumps(traj))
        copied.write_text("import sys\nprint('crash')\nsys.exit(1)\n", encoding="utf-8")
        crashed = subprocess.run(
            ["bash", str(localized)], capture_output=True, text=True, env=env
        )
        self.assertEqual(crashed.returncode, 2, crashed.stderr[-2000:] + crashed.stdout[-2000:])
        self.assertFalse((verifier / "reward.txt").exists())
        crashed_handoff = json.loads((verifier / "completion.json").read_text(encoding="utf-8"))
        self.assertEqual(crashed_handoff["technical_status"], "error")
        self.assertFalse(crashed_handoff["evaluator_completed"])
        self.assertNotEqual(crashed_handoff["error_message"], "Content evaluation failed")

    def test_db_script_reconstructs_evaluation_after_the_agent(self) -> None:
        import subprocess

        from generate_harbor_canonical import convert_one
        from native_mcp.catalog import load_catalog
        from native_mcp.workspace_lifecycle import finalize

        out = Path(tempfile.mkdtemp(prefix="cowork-db-gen-"))
        convert_one(DB_TASK, out, load_catalog())
        task_dir = out / DB_TASK
        script_text = (task_dir / "tests" / "test.sh").read_text(encoding="utf-8")
        self.assertIn("COWORK_GRADER", script_text)
        self.assertIn("/tests/grader_private/task_contract.json", script_text)
        self.assertIn('--contract "$CONTRACT"', script_text)
        workspace = Path(tempfile.mkdtemp(prefix="cowork-db-run-"))
        artifacts = Path(tempfile.mkdtemp(prefix="cowork-db-artifacts-"))
        verifier = Path(tempfile.mkdtemp(prefix="cowork-db-verifier-"))
        handoff = Path(tempfile.mkdtemp(prefix="cowork-db-handoff-"))
        fake = Path(tempfile.mkdtemp(prefix="cowork-db-cwd-"))
        task_root = fake / "tasks" / "finalpool" / DB_TASK
        sentinel = fake / "grader-ran"
        copied = task_dir / "tests" / "evaluation" / "main.py"
        copied.write_text(
            "import sys\n"
            f"open({str(sentinel)!r}, 'w').write('ran')\n"
            "print('Pass:    True')\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        context = _public_context(workspace, str(workspace / "evil" / "traj_log.json"))
        from native_mcp import workspace_lifecycle as wl

        previous = wl.AGENT_COMPLETION_PATH
        wl.AGENT_COMPLETION_PATH = artifacts / "agent_completion.json"
        self.addCleanup(lambda: setattr(wl, "AGENT_COMPLETION_PATH", previous))
        finalize(workspace=workspace, agent_dir=_finish_agent(), context_path=context)
        localized = _localize_verifier(
            script_text,
            tests_dir=task_dir / "tests",
            artifacts=artifacts,
            verifier=verifier,
            task_root=task_root,
        ).replace("/grader_out", str(handoff))
        lifecycle = (task_dir / "tests" / "verifier" / "workspace_lifecycle.py").read_text(encoding="utf-8")
        (task_dir / "tests" / "verifier" / "workspace_lifecycle.py").write_text(
            lifecycle.replace("/logs/artifacts/cowork", str(artifacts)),
            encoding="utf-8",
        )
        script_path = workspace / "test.sh"
        script_path.write_text(localized, encoding="utf-8")
        env = {
            **os.environ,
            "COWORK_EVAL_ROOT": str(fake),
            "COWORK_GRADER": "1",
            "PYTHON_BIN": sys.executable,
        }
        passed = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, env=env)
        self.assertEqual(passed.returncode, 0, passed.stderr[-1500:] + passed.stdout[-500:])
        self.assertEqual((handoff / "reward.txt").read_text(encoding="utf-8"), "1\n")
        self.assertTrue(sentinel.is_file())
        traj = json.loads((artifacts / "traj_log.json").read_text(encoding="utf-8"))
        self.assertIsNone(traj["config"]["evaluation"]["evaluation_command"])
        self.assertNotIn("evaluation.main", json.dumps(traj))
        copied.write_text("import sys\nprint('crash')\nsys.exit(1)\n", encoding="utf-8")
        crashed = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, env=env)
        self.assertEqual(crashed.returncode, 0, crashed.stderr[-800:] + crashed.stdout[-800:])
        sidecar = json.loads((handoff / "completion.json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["technical_status"], "error")
        self.assertFalse((handoff / "reward.txt").exists())
        main_env = {key: value for key, value in env.items() if key != "COWORK_GRADER"}
        main_side = subprocess.run(
            ["bash", str(script_path)], capture_output=True, text=True, env=main_env
        )
        self.assertEqual(main_side.returncode, 2, main_side.stderr[-800:] + main_side.stdout[-400:])
        self.assertFalse((verifier / "reward.txt").exists())


def _outside_region(stdout: str) -> str:
    from utils.evaluation.verdict import STDERR_MARK, STDOUT_MARK

    start = stdout.find(STDOUT_MARK)
    end = stdout.find(STDERR_MARK)
    if start < 0 or end < 0:
        return stdout
    return stdout[:start] + stdout[end + len(STDERR_MARK):]


def _classify_source(source: str, mode: str, rc: int, stdout: str, stderr: str, eval_res) -> dict:
    import subprocess

    tmp = Path(tempfile.mkdtemp(prefix="cowork-class-"))
    stdout_path = tmp / "stdout"
    stderr_path = tmp / "stderr"
    result_path = tmp / "result.json"
    eval_path = tmp / "eval_res.json"
    completion = tmp / "completion.json"
    reward = tmp / "reward.txt"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    if eval_res is not None:
        eval_path.write_text(json.dumps(eval_res), encoding="utf-8")
    script = tmp / "classify.py"
    script.write_text(source, encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            str(script),
            mode,
            str(rc),
            str(stdout_path),
            str(stderr_path),
            str(result_path),
            str(eval_path),
            str(completion),
            str(reward),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    reward_file = reward.read_text(encoding="utf-8") if reward.exists() else None
    return {
        "completion": json.loads(completion.read_text(encoding="utf-8")),
        "reward_file": reward_file,
    }


def _run_generated_eval(classifier_source: str, grader_source: str) -> dict:
    import subprocess

    from native_mcp.prep.prepare_workspace import build_task_config_dict

    work = Path(tempfile.mkdtemp(prefix="cowork-run-eval-"))
    grader = work / "grader.py"
    grader.write_text(grader_source, encoding="utf-8")
    log = work / "traj_log.json"
    config = build_task_config_dict(DB_TASK, repo_root=ROOT)
    config["evaluation"]["evaluation_command"] = f"{sys.executable} {grader}"
    config["log_file"] = str(log)
    log.write_text(
        json.dumps({"config": config, "status": "success"}),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_eval.py"), "--log_file", str(log)],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": _pythonpath()},
        capture_output=True,
        text=True,
    )
    eval_path = work / "eval_res.json"
    eval_res = json.loads(eval_path.read_text(encoding="utf-8")) if eval_path.is_file() else None
    classified = _classify_source(classifier_source, "ua", proc.returncode, proc.stdout, proc.stderr, eval_res)
    return {
        "rc": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "eval_res": eval_res,
        "completion": classified["completion"],
        "reward_file": classified["reward_file"],
    }


def _pythonpath() -> str:
    current = os.environ.get("PYTHONPATH", "")
    if not current:
        return str(ROOT)
    return str(ROOT) + os.pathsep + current


def _localize_verifier(script: str, *, tests_dir: Path, artifacts: Path, verifier: Path, task_root: Path) -> str:
    localized = script
    localized = localized.replace("/opt/venv/bin/python3", sys.executable)
    localized = localized.replace("/workspace/scripts/run_eval.py", str(ROOT / "scripts" / "run_eval.py"))
    localized = localized.replace("/tests", str(tests_dir))
    localized = localized.replace(
        f"PYTHONPATH={tests_dir}/verifier:/workspace",
        f"PYTHONPATH={tests_dir / 'verifier'}:{_pythonpath()}",
    )
    localized = localized.replace("PYTHONPATH=/workspace", f"PYTHONPATH={_pythonpath()}")
    localized = localized.replace("/logs/artifacts/cowork", str(artifacts))
    localized = localized.replace("/logs/verifier", str(verifier))
    localized = localized.replace("ROOT=/workspace/tasks/finalpool/$TASK", f"ROOT={task_root}")
    return localized


if __name__ == "__main__":
    unittest.main()
