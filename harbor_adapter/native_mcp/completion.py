"""Agent-neutral completion for stock Harbor agents (UA / Qwen / OpenHands / …).

Harbor does **not** put TrialResult into the task container before the
verifier. Finalize only sees ``/logs/agent/*`` and the workspace.

Contract (Aidar review):

- Missing agent artifacts or completion markers must **not** block ``run_eval``.
- After a normal agent-process exit, traj_log ``status`` is ``success`` so the
  shared evaluator gate runs content checks.
- Reward comes **only** from the evaluator (pass true/false). No soft-PASS from
  artifacts, finish tools, or ``.cowork/TURN_FINISHED``.
- Technical ``failed`` only for:

  - confirmed timeout / max-steps from structured process-lifecycle sidecars,
  - corrupt / unreadable required agent trajectory artifacts,
  - confirmed process crash / nonzero exit from the same sidecars,
  - forced ``--status failed`` from the finalize CLI.

- Free-text strings that tools or user commands may emit (including
  ``Traceback``, ``TimeoutError``, ``AgentTimeoutError``,
  ``Fatal error``, ``Segmentation fault``,
  ``NonZeroAgentExitCodeError``, ``Command failed (exit``) are **never**
  treated as conclusive process crash/timeout when they appear only inside
  trajectory / tool output / arbitrary agent log text.
- No agent-specific ``turn_finish`` / finish-action / UA log parsing.

Exit-code gap: stock Harbor UA trials observed in sample50 do **not** expose
an agent process ``exit_code`` inside ``/logs/agent``. Host ``result.json``
also lacks it (``agent_execution`` is timestamps only). Until Harbor writes a
structured sidecar (see ``_LIFECYCLE_EXIT_FILES``), normal completion is
inferred whenever corrupt checks pass and no sidecar reports failure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Optional Harbor/agent lifecycle sidecars. Only these filenames are consulted
# for process exit / timeout — never trajectory or free-text log scans.
_LIFECYCLE_EXIT_FILES = (
    "agent_exit.json",
    "harbor_agent_exit.json",
    "agent_process.json",
)

# Structured status/reason tokens that confirm harness timeout / max-steps.
_TIMEOUT_STATUS_TOKENS = frozenset(
    {
        "timeout",
        "timed_out",
        "timedout",
        "agent_timeout",
        "max_steps",
        "max_iterations",
        "max_turns",
        "maximum_iterations",
        "maximum_turns",
    }
)

_TIMEOUT_EXCEPTION_TOKENS = (
    "AgentTimeoutError",
    "TimeoutError",
    "timeout",
    "timed out",
    "max iterations",
    "max turns",
    "maximum number of iterations",
    "maximum number of turns",
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

    timeout = _structured_timeout(agent_dir)
    if timeout:
        return CompletionInference(
            confirmed=False,
            status="FAILED",
            reason="timeout_or_max_steps",
            framework=_guess_framework(agent_dir),
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

    lifecycle_crash = _structured_process_failure(agent_dir)
    if lifecycle_crash:
        return CompletionInference(
            confirmed=False,
            status="FAILED",
            reason="exception_or_crash",
            framework=_guess_framework(agent_dir),
            evidence=lifecycle_crash,
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


def _iter_lifecycle_sidecars(agent_dir: Path) -> Iterable[tuple[Path, dict[str, Any]]]:
    for name in _LIFECYCLE_EXIT_FILES:
        path = agent_dir / name
        if not path.is_file():
            continue
        data, err = _load_json(path)
        if err or not isinstance(data, dict):
            continue
        yield path, data


def _exception_blob(data: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("exception", "exception_info", "error", "error_type", "exc_type"):
        val = data.get(key)
        if val is None:
            continue
        parts.append(val if isinstance(val, str) else json.dumps(val, ensure_ascii=False))
    return " ".join(parts)


def _structured_timeout(agent_dir: Path) -> list[str]:
    """Return evidence if a structured lifecycle sidecar reports timeout/max-steps.

    Ignores trajectory / tool logs / free-text agent logs entirely.
    """
    evidence: list[str] = []
    for path, data in _iter_lifecycle_sidecars(agent_dir):
        if data.get("timed_out") is True or data.get("timeout") is True:
            evidence.append(f"{path.name}: timed_out/timeout flag")
            continue

        for key in ("status", "reason", "completion_reason", "stop_reason"):
            raw = data.get(key)
            if raw is None:
                continue
            token = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
            if token in _TIMEOUT_STATUS_TOKENS:
                evidence.append(f"{path.name}: {key}={raw}")

        blob = _exception_blob(data).lower()
        if blob and any(tok.lower() in blob for tok in _TIMEOUT_EXCEPTION_TOKENS):
            evidence.append(f"{path.name}: timeout exception={_exception_blob(data)}")
    return evidence


def _structured_process_failure(agent_dir: Path) -> list[str]:
    """Return evidence if a structured lifecycle sidecar reports process failure.

    Ignores trajectory / tool logs entirely. Missing sidecars mean "unknown
    exit" and are treated as non-failure (evaluator still runs).
    Timeout-shaped exceptions are handled by ``_structured_timeout`` first.
    """
    evidence: list[str] = []
    for name in _LIFECYCLE_EXIT_FILES:
        path = agent_dir / name
        if not path.is_file():
            continue
        data, err = _load_json(path)
        if err:
            evidence.append(err)
            continue
        if not isinstance(data, dict):
            evidence.append(f"{path.name}: expected object")
            continue
        if data.get("exception") or data.get("exception_info"):
            evidence.append(
                f"{path.name}: exception={data.get('exception') or data.get('exception_info')}"
            )
        raw_code = data.get("exit_code", data.get("returncode", data.get("return_code")))
        if raw_code is None:
            continue
        try:
            code = int(raw_code)
        except (TypeError, ValueError):
            evidence.append(f"{path.name}: non-integer exit_code={raw_code!r}")
            continue
        if code != 0:
            evidence.append(f"{path.name}: exit_code={code}")
    return evidence


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
