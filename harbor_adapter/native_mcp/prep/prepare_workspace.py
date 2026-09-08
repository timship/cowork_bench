#!/usr/bin/env python3
"""Agent-agnostic workspace prepare (no Strands / UA / OH / QC).

Copies initial_workspace, runs task preprocess (DB seed + mock HTTP),
writes minimal cli_context.json for verifier finalize.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from db_rewrite import rewrite_preprocess_db_connections, unresolved_connection_literals
from task_config_stub import minimal_harbor_task_config_dict

SHARED = Path("/workspace/cowork_shared")
CONTEXT = SHARED / ".cowork" / "cli_context.json"
TRAJ_LOG = Path("/logs/artifacts/cowork/traj_log.json")
PAYLOAD_RO = Path("/task_payload")
PAYLOAD = Path("/tmp/task_payload_rw")


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
        "task_config": minimal_harbor_task_config_dict(
            args.task,
            str(SHARED),
            str(TRAJ_LOG),
            single_turn_mode=True,
        ),
        "start_time": datetime.now().isoformat(),
    }
    CONTEXT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"workspace": str(SHARED), "context": str(CONTEXT)}))


if __name__ == "__main__":
    main()
