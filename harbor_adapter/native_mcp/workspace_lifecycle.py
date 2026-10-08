#!/usr/bin/env python3
"""Task-side Cowork workspace prepare/finalize for stock Harbor agents.

Prepare runs the standard TaskConfig.build + preprocess path.
Finalize publishes a public completion record and never defaults to SUCCESS.
The verifier later writes traj_log.json and reconstructs evaluation.
Reward criteria are not modified.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))
from completion import CompletionInference, infer_completion  # noqa: E402

SHARED_WORKSPACE = Path("/workspace/cowork_shared")
CONTEXT_PATH = SHARED_WORKSPACE / ".cowork" / "cli_context.json"
TRAJ_LOG = Path("/logs/artifacts/cowork/traj_log.json")
AGENT_COMPLETION_PATH = TRAJ_LOG.with_name("agent_completion.json")
AGENT_DIR = Path("/logs/agent")
PUBLIC_CONTEXT_KEYS = {
    "task",
    "provider",
    "model",
    "workspace",
    "log_file",
    "start_time",
}
PUBLIC_COMPLETION_KEYS = {
    "task",
    "status",
    "start_time",
    "end_time",
    "completion",
}
COMPLETION_DETAIL_KEYS = {
    "confirmed",
    "reason",
    "framework",
    "evidence",
    "stop_reason",
}


def _build_config(task: str, provider: str, model: str):
    from utils.data_structures.task_config import TaskConfig

    safe_model = model.replace("/", "_")
    global_config = {
        "dump_path": "/logs/artifacts/cowork",
        "max_steps_under_single_turn_mode": 100,
    }
    cfg = TaskConfig.build(
        task,
        agent_short_name=f"{provider}/{safe_model}",
        global_task_config=global_config,
        single_turn_mode=True,
        cn_mode=False,
    )
    cfg.agent_workspace = str(SHARED_WORKSPACE)
    cfg.log_file = str(TRAJ_LOG)
    return cfg


def discard_public_completion() -> None:
    """Remove a stale completion before an attempt. A missing file is success."""
    path = AGENT_COMPLETION_PATH
    parent = path.parent
    if not parent.exists() and not parent.is_symlink():
        return
    if parent.is_symlink():
        raise SystemExit("refusing symlinked completion directory")
    if path.is_symlink() or (path.exists() and path.is_file()):
        path.unlink()
        return
    if path.exists():
        raise SystemExit("refusing non-file completion path")


def _require_completion_dir(path: Path) -> Path:
    if os.path.normpath(str(path)) != os.path.normpath(str(AGENT_COMPLETION_PATH)):
        raise SystemExit("refusing completion path")
    parent = path.parent
    if parent.is_symlink():
        raise SystemExit("refusing symlinked completion directory")
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise SystemExit("refusing completion directory")
    if os.path.normpath(str(parent)) != os.path.normpath(str(AGENT_COMPLETION_PATH.parent)):
        raise SystemExit("refusing completion path outside artifacts directory")
    if parent.resolve() != AGENT_COMPLETION_PATH.parent.resolve():
        raise SystemExit("refusing completion path outside artifacts directory")
    return parent


def _atomic_publish_completion(path: Path, payload: dict) -> None:
    """Write the completion in its directory, then replace the final name.

    A symlink at the final name is removed first so the replace cannot publish
    through it. The parent must already be the fixed artifacts directory.
    """
    parent = _require_completion_dir(path)
    if path.is_symlink():
        path.unlink()
    elif path.exists() and not path.is_file():
        raise SystemExit("refusing non-file completion path")
    temporary: Optional[Path] = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=".agent_completion.",
            suffix=".tmp",
            dir=parent,
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            path.unlink()
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    if path.is_symlink() or not path.is_file():
        raise SystemExit("refusing completion path outside artifacts directory")


async def prepare(task: str, provider: str, model: str) -> None:
    discard_public_completion()
    from strands_runner.agent import StrandsTaskAgent

    cfg = _build_config(task, provider, model)
    agent = StrandsTaskAgent(task_config=cfg, model=None, debug=True)
    workspace = await agent._setup_workspace()
    agent._run_preprocess()
    SHARED_WORKSPACE.mkdir(parents=True, exist_ok=True)
    if CONTEXT_PATH.exists() or CONTEXT_PATH.is_symlink():
        CONTEXT_PATH.unlink()
    payload = {
        "task": task,
        "provider": provider,
        "model": model.replace("/", "_"),
        "workspace": workspace,
        "log_file": cfg.log_file,
        "start_time": datetime.now().isoformat(),
    }
    _atomic_json(CONTEXT_PATH, payload)
    TRAJ_LOG.parent.mkdir(parents=True, exist_ok=True)
    print(json.dumps({"workspace": workspace, "context": str(CONTEXT_PATH)}))


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _load_object(path: Path, label: str) -> dict:
    if not path.exists():
        raise SystemExit(f"missing {label}: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"corrupt {label}: {path}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"corrupt {label}: {path}")
    return data


def _assert_public(data: dict, allowed: set[str], label: str) -> None:
    extra = set(data) - allowed
    if extra:
        raise SystemExit(f"refusing non-public field in {label}")
    for value in data.values():
        if isinstance(value, (dict, list)) and label == "workspace context":
            raise SystemExit(f"refusing nested data in {label}")


def finalize(
    status: str | None = None,
    *,
    workspace: Path = SHARED_WORKSPACE,
    agent_dir: Path = AGENT_DIR,
    context_path: Path = CONTEXT_PATH,
) -> dict:
    ctx = _load_object(context_path, "workspace context")
    _assert_public(ctx, PUBLIC_CONTEXT_KEYS, "workspace context")
    if "log_file" not in ctx or "start_time" not in ctx:
        raise SystemExit("corrupt workspace context: missing public fields")
    inferred = infer_completion(agent_dir, workspace=workspace)
    if status in ("success", "SUCCESS"):
        raise SystemExit(
            "refusing --status success; completion is inferred fail-closed "
            "from /logs/agent artifacts"
        )
    if status in ("failed", "FAILED") and inferred.confirmed:
        inferred = CompletionInference(
            confirmed=False,
            status="FAILED",
            reason="forced_failed",
            framework=inferred.framework,
            evidence=inferred.evidence + ["forced_failed"],
            stop_reason=inferred.stop_reason,
        )
    cowork_status = inferred.cowork_status
    completion_path = AGENT_COMPLETION_PATH
    payload = {
        "task": ctx.get("task"),
        "status": cowork_status,
        "start_time": ctx["start_time"],
        "end_time": datetime.now().isoformat(),
        "completion": {
            "confirmed": inferred.confirmed,
            "reason": inferred.reason,
            "framework": inferred.framework,
            "evidence": inferred.evidence,
            "stop_reason": inferred.stop_reason,
        },
    }
    _assert_public(payload, PUBLIC_COMPLETION_KEYS, "agent completion")
    _atomic_publish_completion(completion_path, payload)
    result = {
        "log_file": str(completion_path),
        "status": cowork_status,
        "reason": inferred.reason,
    }
    print(json.dumps(result))
    return result


def _assert_completion_detail(detail: object) -> None:
    if not isinstance(detail, dict):
        raise SystemExit("corrupt agent completion")
    if set(detail) - COMPLETION_DETAIL_KEYS:
        raise SystemExit("refusing non-public field in agent completion")
    evidence = detail.get("evidence")
    if not isinstance(evidence, list) or any(not isinstance(item, str) for item in evidence):
        raise SystemExit("corrupt agent completion")


def _assert_trusted_contract(contract: dict, task: str) -> None:
    from verifier_shell import assert_null_evaluation

    assert_null_evaluation(contract, task)


def _refuse_agent_writable_contract(contract_path: Path) -> None:
    if contract_path.is_symlink():
        raise SystemExit("refusing symlinked grader contract")
    resolved = contract_path.resolve()
    workspace = SHARED_WORKSPACE.resolve()
    if resolved == workspace or workspace in resolved.parents:
        raise SystemExit("refusing agent-writable contract")
    if resolved == AGENT_COMPLETION_PATH.resolve() or CONTEXT_PATH.resolve() == resolved:
        raise SystemExit("refusing agent-writable contract")


def assemble_trajectory(
    *,
    task: str,
    contract_path: Path,
    completion_path: Path,
    log_path: Path,
) -> dict:
    """Write traj_log.json from the grader-only sanitized contract.

    The contract is the file generated by TaskConfig.to_dict() with both
    evaluation fields null. cli_context.json and agent_completion.json are
    not a source for that config. Evaluation.build runs later, inside the
    grader, against the copied task tree.
    """
    if os.path.normpath(str(completion_path)) != os.path.normpath(str(AGENT_COMPLETION_PATH)):
        raise SystemExit("refusing completion path")
    if Path(completion_path).is_symlink():
        raise SystemExit("refusing symlinked agent completion")
    if log_path.is_symlink():
        raise SystemExit("refusing symlinked trajectory")
    _refuse_agent_writable_contract(contract_path)
    contract = _load_object(contract_path, "grader contract")
    _assert_trusted_contract(contract, task)
    from utils.data_structures.task_config import TaskConfig

    try:
        TaskConfig.from_dict(dict(contract))
    except Exception as exc:
        raise SystemExit("corrupt grader contract") from exc
    completion = _load_object(completion_path, "agent completion")
    _assert_public(completion, PUBLIC_COMPLETION_KEYS, "agent completion")
    for key in ("status", "start_time", "completion"):
        if key not in completion:
            raise SystemExit("corrupt agent completion")
    if completion["status"] not in ("success", "failed"):
        raise SystemExit("corrupt agent completion")
    _assert_completion_detail(completion["completion"])
    if completion.get("task") not in (None, task):
        raise SystemExit("completion task does not match")
    if log_path.exists():
        log_path.unlink()
    payload = {
        "config": contract,
        "status": completion["status"],
        "start_time": completion["start_time"],
        "end_time": completion.get("end_time") or datetime.now().isoformat(),
        "completion": completion["completion"],
    }
    _atomic_json(log_path, payload)
    result = {"log_file": str(log_path), "status": completion["status"]}
    print(json.dumps(result))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--task", required=True)
    prep.add_argument("--provider", default="harbor-native")
    prep.add_argument("--model", default="stock")
    done = sub.add_parser("finalize")
    asm = sub.add_parser("assemble")
    asm.add_argument("--task", required=True)
    asm.add_argument("--contract", type=Path, required=True)
    asm.add_argument("--completion", type=Path, required=True)
    asm.add_argument("--log", type=Path, required=True)
    done.add_argument(
        "--status",
        choices=("failed",),
        default=None,
        help="Optional override. SUCCESS cannot be forced; it is inferred.",
    )
    done.add_argument("--agent-dir", type=Path, default=AGENT_DIR)
    done.add_argument("--workspace", type=Path, default=SHARED_WORKSPACE)
    args = parser.parse_args()
    if args.command == "prepare":
        import asyncio

        asyncio.run(prepare(args.task, args.provider, args.model))
    elif args.command == "assemble":
        assemble_trajectory(
            task=args.task,
            contract_path=args.contract,
            completion_path=args.completion,
            log_path=args.log,
        )
    else:
        finalize(
            status=args.status,
            workspace=args.workspace,
            agent_dir=args.agent_dir,
            context_path=args.workspace / ".cowork" / "cli_context.json"
            if args.workspace != SHARED_WORKSPACE
            else CONTEXT_PATH,
        )


if __name__ == "__main__":
    main()
