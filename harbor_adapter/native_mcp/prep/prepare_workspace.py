#!/usr/bin/env python3
"""Agent-agnostic workspace prepare (no Strands / UA / OH / QC).

Copies initial_workspace, runs task preprocess (DB seed + mock HTTP),
and writes a public cli_context.json. The grader contract is not written here.
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
    """Schema-valid TaskConfig without precomputed evaluation paths.

    The private fields stay None. Evaluation.build runs in the verifier, after
    the agent has stopped and the task tree is mounted only there.
    """
    del repo_root
    here = Path(__file__).resolve()
    shell = here.parents[1] / "verifier_shell.py"
    if not shell.is_file():
        raise SystemExit("verifier shell is missing")
    import importlib.util

    spec = importlib.util.spec_from_file_location("cowork_verifier_shell", shell)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.sanitized_task_config(
        task, agent_workspace=agent_workspace, log_file=log_file
    )


PUBLIC_CONTEXT_KEYS = (
    "task",
    "provider",
    "model",
    "workspace",
    "log_file",
    "start_time",
)


def build_agent_context(
    task: str,
    *,
    provider: str,
    model: str,
    workspace: str,
    log_file: str,
    start_time: str,
) -> dict:
    """Runtime note for the agent workspace. No TaskConfig and no grader fields."""
    return {
        "task": task,
        "provider": provider,
        "model": model,
        "workspace": workspace,
        "log_file": log_file,
        "start_time": start_time,
    }


def reset_agent_context(path: Path = CONTEXT) -> None:
    """Drop any previous attempt before prepare can fail halfway."""
    if path.is_symlink() or path.exists():
        path.unlink()


def write_agent_context(path: Path, payload: dict) -> None:
    unexpected = set(payload) - set(PUBLIC_CONTEXT_KEYS)
    if unexpected:
        raise SystemExit(f"refusing private fields in agent context: {sorted(unexpected)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def resolve_task_config(task: str, agent_workspace: str, log_file: str) -> dict:
    """Return the sanitized shell. A precomputed contract file is refused."""
    beside = Path(__file__).resolve().parent / CONTRACT_NAME
    if beside.is_file():
        raise SystemExit("refusing precomputed task contract")
    return build_task_config_dict(
        task, agent_workspace=agent_workspace, log_file=log_file
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    args = parser.parse_args()

    SHARED.mkdir(parents=True, exist_ok=True)
    # Remove the previous attempt before preprocess. A failed prepare must not
    # leave the last run's context for the evaluator to read.
    reset_agent_context(CONTEXT)
    # Clean residual agent files but keep the volume.
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

    TRAJ_LOG.parent.mkdir(parents=True, exist_ok=True)
    payload = build_agent_context(
        args.task,
        provider="harbor-canonical",
        model="n/a",
        workspace=str(SHARED),
        log_file=str(TRAJ_LOG),
        start_time=datetime.now().isoformat(),
    )
    write_agent_context(CONTEXT, payload)
    print(json.dumps({"workspace": str(SHARED), "context": str(CONTEXT)}))


if __name__ == "__main__":
    main()
