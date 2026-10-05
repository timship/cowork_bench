#!/usr/bin/env python3
"""Agent-agnostic workspace prepare (no Strands / UA / OH / QC).

Copies initial_workspace, runs task preprocess (DB seed + mock HTTP),
writes cli_context.json whose task_config is TaskConfig.to_dict().
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    from db_rewrite import rewrite_preprocess_db_connections, unresolved_connection_literals
except ImportError:  # imported as native_mcp.prep.prepare_workspace
    from native_mcp.prep.db_rewrite import (
        rewrite_preprocess_db_connections,
        unresolved_connection_literals,
    )

SHARED = Path("/workspace/cowork_shared")
CONTEXT = SHARED / ".cowork" / "cli_context.json"
TRAJ_LOG = Path("/logs/artifacts/cowork/traj_log.json")
PAYLOAD_RO = Path("/task_payload")
PAYLOAD = Path("/tmp/task_payload_rw")
CONTRACT_NAME = "task_contract.json"


def build_task_config_dict(
    task: str,
    *,
    agent_workspace: str = str(SHARED),
    log_file: str = str(TRAJ_LOG),
    repo_root: Path | None = None,
) -> dict:
    """Serialize a TaskConfig. The dict schema is TaskConfig.to_dict(), not a hand-written subset.

    Evaluation and initialization paths come from Evaluation.build / Initialization.build
    against the source tree. task_root is kept relative so the file does not embed the
    generator host path; from_dict resolves it in the grader process.
    """
    root = repo_root
    if root is None:
        here = Path(__file__).resolve()
        candidate = here.parents[3]
        root = candidate if (candidate / "tasks" / "finalpool" / task).is_dir() else Path.cwd()

    repo = str(root)
    if repo not in sys.path:
        sys.path.insert(0, repo)
    from utils.data_structures.task_config import (  # noqa: WPS433
        Evaluation,
        Initialization,
        StopConditions,
        SystemPrompts,
        TaskConfig,
    )
    from utils.general.helper import read_json  # noqa: WPS433

    previous = os.getcwd()
    try:
        os.chdir(root)
        raw_path = Path("tasks/finalpool") / task / "task_config.json"
        raw = read_json(raw_path) if raw_path.is_file() else {}
        cfg = TaskConfig(
            task_dir=task,
            id=task,
            needed_mcp_servers=raw.get("needed_mcp_servers"),
            needed_local_tools=raw.get("needed_local_tools"),
            max_turns=raw.get("max_turns"),
            meta=raw.get("meta") or {},
            agent_workspace=agent_workspace,
            log_file=log_file,
            single_turn_mode=True,
            task_str="",
            evaluation=Evaluation.build(task),
            system_prompts=SystemPrompts(agent=None, user=None),
            initialization=Initialization.build(task),
            stop=StopConditions.build(raw.get("stop")),
            launch_time="",
        )
        data = cfg.to_dict()
    finally:
        os.chdir(previous)
    data["task_root"] = task
    data["agent_workspace"] = agent_workspace
    data["log_file"] = log_file
    data["task_str"] = ""
    return data


def resolve_task_config(task: str, agent_workspace: str, log_file: str) -> dict:
    """Load the generated contract, or build one when the source tree is present."""
    beside = Path(__file__).resolve().parent / CONTRACT_NAME
    if beside.is_file():
        data = json.loads(beside.read_text(encoding="utf-8"))
    else:
        data = build_task_config_dict(
            task, agent_workspace=agent_workspace, log_file=log_file
        )
    data["task_dir"] = task
    data["id"] = task
    data["agent_workspace"] = agent_workspace
    data["log_file"] = log_file
    return data


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    args = parser.parse_args()

    SHARED.mkdir(parents=True, exist_ok=True)
    # Clean residual agent files but keep volume.
    for child in list(SHARED.iterdir()):
        if child.name == ".cowork":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()

    # Payload mount is read-only; preprocess needs a writable copy for tmp/mock.
    if PAYLOAD.exists():
        shutil.rmtree(PAYLOAD)
    shutil.copytree(PAYLOAD_RO, PAYLOAD, ignore=shutil.ignore_patterns(".*"))

    initial = PAYLOAD / "initial_workspace"
    if initial.is_dir():
        for item in initial.iterdir():
            dest = SHARED / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

    preprocess = PAYLOAD / "preprocess" / "main.py"
    if preprocess.is_file():
        # Mock HTTP is a compose sidecar on agent_net; skip in-prep server.
        src = preprocess.read_text(encoding="utf-8")
        src = src.replace(
            "await setup_mock_server()",
            "print('[preprocess] mock HTTP delegated to main localhost:30151')",
        )
        src = rewrite_preprocess_db_connections(src)
        unresolved = unresolved_connection_literals(src)
        if unresolved:
            raise SystemExit(
                "preprocess contains unresolved PostgreSQL connection literals "
                f"at lines {unresolved}"
            )
        preprocess.write_text(src, encoding="utf-8")
        env = dict(**{k: v for k, v in __import__("os").environ.items()})
        rc = subprocess.call(
            [sys.executable, str(preprocess), "--agent_workspace", str(SHARED)],
            cwd=str(PAYLOAD),
            env=env,
        )
        if rc != 0:
            raise SystemExit(f"preprocess failed with code {rc}")

    CONTEXT.parent.mkdir(parents=True, exist_ok=True)
    TRAJ_LOG.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "task": args.task,
        "provider": "harbor-canonical",
        "model": "n/a",
        "workspace": str(SHARED),
        "log_file": str(TRAJ_LOG),
        "task_config": resolve_task_config(args.task, str(SHARED), str(TRAJ_LOG)),
        "start_time": datetime.now().isoformat(),
    }
    CONTEXT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"workspace": str(SHARED), "context": str(CONTEXT)}))


if __name__ == "__main__":
    main()
