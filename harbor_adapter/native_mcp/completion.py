"""Agent-neutral completion for stock Harbor agents (UA / Qwen / OpenHands / …).

Harbor does **not** put TrialResult into the task container before the
verifier. Finalize only sees ``/logs/agent/*`` and the workspace.

Contract (Aidar review):

- Missing agent artifacts or completion markers must **not** block ``run_eval``.
- After a normal agent-process exit, traj_log ``status`` is ``success`` so the
  shared evaluator gate runs content checks.
- Reward comes **only** from the evaluator (pass true/false). No soft-PASS from
  artifacts, finish tools, or ``.cowork/TURN_FINISHED``.
- Real runtime failures — timeout / max-steps, crash signatures, corrupt agent
  artifacts, forced ``--status failed`` — stay ``failed`` (technical fail;
  ``pass`` is null, not a content score).
- No agent-specific ``turn_finish`` / finish-action / UA log parsing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

TIMEOUT_MARKERS = (
    "AgentTimeoutError",
    "timed out after",
    "TimeoutError",
    "Reached maximum number of iterations",
    "reached max iterations",
    "max_iterations",
    "Maximum number of turns",
    "max turns reached",
    "Agent execution timed out",
)
CRASH_MARKERS = (
    "Traceback (most recent call last)",
    "NonZeroAgentExitCodeError",
    "Command failed (exit",
    "Fatal error",
    "Segmentation fault",
)


@dataclass
class CompletionInference:
    confirmed: bool
    status: str  # SUCCESS | FAILED (inference labels; traj uses cowork_status)
    reason: str
    framework: str | None = None
    evidence: list[str] = field(default_factory=list)

    @property
    def cowork_status(self) -> str:
        # Must match TaskStatus.SUCCESS.value / FAILED.value for run_eval.
        return "success" if self.confirmed else "failed"


def infer_completion(
    agent_dir: Path,
    workspace: Path | None = None,
) -> CompletionInference:
    """Allow eval unless a technical agent/runtime failure is evident.

    ``workspace`` is accepted for API compatibility; markers under it are
    ignored and never grant success by themselves.
    """
    _ = workspace  # intentionally unused — no TURN_FINISHED / marker gate

    if not agent_dir.exists():
        # Harbor may finalize with an empty/missing bind; still run evaluator.
        return CompletionInference(
            confirmed=True,
            status="SUCCESS",
            reason="missing_agent_dir",
            evidence=[str(agent_dir)],
        )

    timeout = _scan_text_markers(agent_dir, TIMEOUT_MARKERS)
    if timeout:
        return CompletionInference(
            confirmed=False,
            status="FAILED",
            reason="timeout_or_max_steps",
            evidence=timeout,
        )

    corrupt = _corrupt_artifacts(agent_dir)
    if corrupt:
        return CompletionInference(
            confirmed=False,
            status="FAILED",
            reason="corrupt_agent_artifact",
            framework=_guess_framework(agent_dir),
            evidence=corrupt,
        )

    crash = _scan_text_markers(agent_dir, CRASH_MARKERS)
    if crash:
        return CompletionInference(
            confirmed=False,
            status="FAILED",
            reason="exception_or_crash",
            framework=_guess_framework(agent_dir),
            evidence=crash,
        )

    evidence: list[str] = []
    if _has_any_agent_log(agent_dir):
        evidence.append("agent_logs_present")
    else:
        evidence.append("missing_agent_artifact")
    return CompletionInference(
        confirmed=True,
        status="SUCCESS",
        reason="agent_process_completed",
        framework=_guess_framework(agent_dir),
        evidence=evidence,
    )


def _load_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return None, f"{path}: empty"
        return json.loads(text), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{path}: {exc}"


def _candidate_files(agent_dir: Path, names: Iterable[str]) -> list[Path]:
    found: list[Path] = []
    for name in names:
        direct = agent_dir / name
        if direct.is_file():
            found.append(direct)
        found.extend(p for p in agent_dir.rglob(name) if p.is_file() and p not in found)
    return found


def _scan_text_markers(agent_dir: Path, markers: tuple[str, ...]) -> list[str]:
    hits: list[str] = []
    for path in agent_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".txt", ".log", ".json", ".jsonl", ""}:
            continue
        if path.stat().st_size > 4_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for marker in markers:
            if marker in text:
                hits.append(f"{path.name}: {marker}")
    return hits


def _corrupt_artifacts(agent_dir: Path) -> list[str]:
    bad: list[str] = []
    for path in _candidate_files(agent_dir, ("openhands.trajectory.json", "trajectory.json")):
        _, err = _load_json(path)
        if err:
            bad.append(err)
    sessions = agent_dir / "qwen-sessions"
    if sessions.is_dir():
        for path in sessions.rglob("*.jsonl"):
            try:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if line:
                        json.loads(line)
            except (OSError, json.JSONDecodeError) as exc:
                bad.append(f"{path}: {exc}")
    return bad


def _has_any_agent_log(agent_dir: Path) -> bool:
    names = (
        "openhands.txt",
        "openhands.trajectory.json",
        "trajectory.json",
        "qwen-code.txt",
    )
    if any((agent_dir / name).exists() for name in names):
        return True
    if (agent_dir / "qwen-sessions").exists():
        return True
    return any(agent_dir.rglob("events/*.json"))


def _guess_framework(agent_dir: Path) -> str | None:
    if (agent_dir / "openhands.txt").exists() or (
        agent_dir / "openhands.trajectory.json"
    ).exists():
        return "openhands"
    if (agent_dir / "qwen-code.txt").exists() or (agent_dir / "qwen-sessions").exists():
        return "qwen-code"
    return None
