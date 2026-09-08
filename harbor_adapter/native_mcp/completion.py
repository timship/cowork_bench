"""Fail-closed completion inference for stock Harbor OpenHands / Qwen Code.

Harbor does **not** put TrialResult into the task container before the
verifier. Collect hooks and tests/test.sh only see /logs/agent/* and the
workspace. Stock adapters also do not share one "task complete" API:

- OpenHands: explicit ``finish`` / ``task_complete`` action in its trajectory.
- Qwen Code: session jsonl has turns/tools; no first-class finish. Optional
  workspace marker ``.cowork/TURN_FINISHED`` is the only agent-agnostic proof.
- Exit code 0 is **not** completion (max iterations often exits 0).
- Harbor records AgentTimeoutError / NonZeroAgentExitCodeError on the host
  and still runs the verifier; those types are not visible in-container unless
  the agent log contains a timeout/crash signature.

This module never defaults to SUCCESS.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

FINISH_ACTIONS = {
    "finish",
    "task_complete",
    "taskcomplete",
    "attempt_completion",
    "complete_task",
    "submit",
}
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
TURN_FINISHED_REL = Path(".cowork") / "TURN_FINISHED"


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
    """Inspect agent artifacts. SUCCESS only with explicit completion proof."""
    if not agent_dir.exists():
        return CompletionInference(
            confirmed=False,
            status="FAILED",
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

    oh = _openhands_finish(agent_dir)
    if oh.confirmed:
        return oh
    qwen = _qwen_finish(agent_dir)
    if qwen.confirmed:
        return qwen
    marker = _workspace_marker(workspace) if workspace else None
    if marker and marker.confirmed:
        return marker

    if oh.reason == "corrupt_agent_artifact":
        return oh
    if qwen.reason == "corrupt_agent_artifact":
        return qwen
    corrupt = _corrupt_artifacts(agent_dir)
    if corrupt:
        return CompletionInference(
            confirmed=False,
            status="FAILED",
            reason="corrupt_agent_artifact",
            evidence=corrupt,
        )

    crash = _scan_text_markers(agent_dir, CRASH_MARKERS)
    if crash:
        return CompletionInference(
            confirmed=False,
            status="FAILED",
            reason="exception_or_crash",
            evidence=crash,
        )

    if oh.framework == "openhands" or qwen.framework == "qwen-code":
        return CompletionInference(
            confirmed=False,
            status="FAILED",
            reason="unconfirmed_exit",
            framework=oh.framework or qwen.framework,
            evidence=(oh.evidence or qwen.evidence)
            + ["exit_or_stop_without_finish_action"],
        )

    if _has_any_agent_log(agent_dir):
        return CompletionInference(
            confirmed=False,
            status="FAILED",
            reason="unconfirmed_exit",
            framework=_guess_framework(agent_dir),
            evidence=["exit_or_stop_without_finish_action"],
        )

    return CompletionInference(
        confirmed=False,
        status="FAILED",
        reason="missing_agent_artifact",
        evidence=[f"no recognized agent logs under {agent_dir}"],
    )


def _workspace_marker(workspace: Path) -> CompletionInference | None:
    path = workspace / TURN_FINISHED_REL
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return CompletionInference(
            confirmed=False,
            status="FAILED",
            reason="corrupt_agent_artifact",
            evidence=[f"empty {path}"],
        )
    return CompletionInference(
        confirmed=True,
        status="SUCCESS",
        reason="workspace_turn_finished_marker",
        framework="agnostic",
        evidence=[str(path)],
    )


def _openhands_finish(agent_dir: Path) -> CompletionInference:
    evidence: list[str] = []
    corrupt: list[str] = []
    for path in _candidate_files(
        agent_dir,
        (
            "openhands.trajectory.json",
            "trajectory.json",
        ),
    ):
        data, err = _load_json(path)
        if err:
            corrupt.append(err)
            continue
        if _json_has_finish(data):
            return CompletionInference(
                confirmed=True,
                status="SUCCESS",
                reason="openhands_finish_action",
                framework="openhands",
                evidence=[str(path)],
            )
        evidence.append(f"{path.name}: parsed, no finish action")

    events_dir = None
    for cand in agent_dir.rglob("events"):
        if cand.is_dir():
            events_dir = cand
            break
    if events_dir is not None:
        saw_event = False
        for event_file in sorted(events_dir.glob("*.json")):
            data, err = _load_json(event_file)
            if err:
                corrupt.append(err)
                continue
            saw_event = True
            if _json_has_finish(data):
                return CompletionInference(
                    confirmed=True,
                    status="SUCCESS",
                    reason="openhands_finish_action",
                    framework="openhands",
                    evidence=[str(event_file)],
                )
        if saw_event:
            evidence.append(f"{events_dir}: events without finish")

    if corrupt and not evidence:
        return CompletionInference(
            confirmed=False,
            status="FAILED",
            reason="corrupt_agent_artifact",
            framework="openhands",
            evidence=corrupt,
        )
    return CompletionInference(
        confirmed=False,
        status="FAILED",
        reason="openhands_no_finish",
        framework="openhands" if evidence or corrupt else None,
        evidence=evidence + corrupt,
    )


def _qwen_finish(agent_dir: Path) -> CompletionInference:
    sessions = agent_dir / "qwen-sessions"
    jsonl_files = list(sessions.rglob("*.jsonl")) if sessions.is_dir() else []
    jsonl_files += list(agent_dir.glob("*.jsonl"))
    if not jsonl_files:
        return CompletionInference(
            confirmed=False,
            status="FAILED",
            reason="qwen_no_session",
            framework=None,
        )
    corrupt: list[str] = []
    saw_turn = False
    for path in jsonl_files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            corrupt.append(f"{path}: {exc}")
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                corrupt.append(f"{path}: {exc}")
                continue
            saw_turn = True
            if _json_has_finish(event):
                return CompletionInference(
                    confirmed=True,
                    status="SUCCESS",
                    reason="qwen_finish_tool",
                    framework="qwen-code",
                    evidence=[str(path)],
                )
    if corrupt and not saw_turn:
        return CompletionInference(
            confirmed=False,
            status="FAILED",
            reason="corrupt_agent_artifact",
            framework="qwen-code",
            evidence=corrupt,
        )
    return CompletionInference(
        confirmed=False,
        status="FAILED",
        reason="qwen_no_finish_tool",
        framework="qwen-code",
        evidence=[f"parsed {len(jsonl_files)} jsonl file(s), no finish tool"] + corrupt,
    )


def _json_has_finish(data: Any) -> bool:
    if isinstance(data, dict):
        action = str(data.get("action") or data.get("type") or "").lower()
        if action in FINISH_ACTIONS:
            return True
        name = str(
            data.get("function_name")
            or data.get("name")
            or (data.get("function") or {}).get("name")
            or ""
        ).lower()
        if name in FINISH_ACTIONS:
            return True
        fc = data.get("functionCall") or {}
        if isinstance(fc, dict) and str(fc.get("name") or "").lower() in FINISH_ACTIONS:
            return True
        for key in ("history", "events", "steps", "messages", "parts"):
            if key in data and _json_has_finish(data[key]):
                return True
        for key in ("tool_calls", "toolCalls"):
            if key in data and _json_has_finish(data[key]):
                return True
        msg = data.get("message")
        if isinstance(msg, dict) and _json_has_finish(msg):
            return True
    elif isinstance(data, list):
        return any(_json_has_finish(item) for item in data)
    return False


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
