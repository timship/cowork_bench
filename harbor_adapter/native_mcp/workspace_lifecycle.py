#!/usr/bin/env python3
"""Task-side Cowork workspace prepare/finalize for stock Harbor agents.

Prepare runs the standard TaskConfig.build + preprocess path.
Finalize writes traj_log.json for evaluation/main.py but **never** defaults
to SUCCESS: status is inferred from Harbor agent artifacts (fail-closed).
Reward criteria in evaluation/ are not modified.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))
from completion import CompletionInference, infer_completion  # noqa: E402

SHARED_WORKSPACE = Path("/workspace/cowork_shared")
CONTEXT_PATH = SHARED_WORKSPACE / ".cowork" / "cli_context.json"
TRAJ_LOG = Path("/logs/artifacts/cowork/traj_log.json")
AGENT_DIR = Path("/logs/agent")


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


async def prepare(task: str, provider: str, model: str) -> None:
    from strands_runner.agent import StrandsTaskAgent

    cfg = _build_config(task, provider, model)
    agent = StrandsTaskAgent(task_config=cfg, model=None, debug=True)
    workspace = await agent._setup_workspace()
    agent._run_preprocess()
    CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SHARED_WORKSPACE.mkdir(parents=True, exist_ok=True)
    payload = {
        "task": task,
        "provider": provider,
        "model": model.replace("/", "_"),
        "workspace": workspace,
        "log_file": cfg.log_file,
        "task_config": cfg.to_dict(),
        "start_time": datetime.now().isoformat(),
    }
    CONTEXT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    TRAJ_LOG.parent.mkdir(parents=True, exist_ok=True)
    print(json.dumps({"workspace": workspace, "context": str(CONTEXT_PATH)}))


def finalize(
    status: str | None = None,
    *,
    workspace: Path = SHARED_WORKSPACE,
    agent_dir: Path = AGENT_DIR,
    context_path: Path = CONTEXT_PATH,
) -> dict:
    if not context_path.exists():
        raise SystemExit(f"missing workspace context: {context_path}")
    ctx = json.loads(context_path.read_text(encoding="utf-8"))
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
        )
    cowork_status = inferred.cowork_status
    log_path = Path(ctx["log_file"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "config": ctx["task_config"],
        "status": cowork_status,
        "start_time": ctx["start_time"],
        "end_time": datetime.now().isoformat(),
        "completion": {
            "confirmed": inferred.confirmed,
            "reason": inferred.reason,
            "framework": inferred.framework,
            "evidence": inferred.evidence,
        },
    }
    log_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    result = {"log_file": str(log_path), "status": cowork_status, "reason": inferred.reason}
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
