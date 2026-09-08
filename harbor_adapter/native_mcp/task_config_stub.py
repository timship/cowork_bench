"""Minimal Harbor traj ``task_config`` compatible with ``TaskConfig.from_dict``.

Stock ``prepare_workspace`` must not call ``TaskConfig.build`` (no full
``tasks/finalpool`` tree / docs on the prep container). The grader-side
evaluator already re-resolves ``evaluation`` when both fields are null — see
``TaskEvaluator.evaluate_one``. This module only supplies the nested keys
``from_dict`` requires, matching ``TaskConfig.to_dict`` shape.
"""

from __future__ import annotations

from typing import Any, Mapping, MutableMapping


# Nested keys required by TaskConfig.from_dict (utils/data_structures/task_config.py).
_REQUIRED_NESTED = ("evaluation", "system_prompts", "initialization", "stop")

# Defaults aligned with StopConditions.build(None) and Evaluation null rebuild.
_EVALUATION_STUB = {
    "groundtruth_workspace": None,
    "evaluation_command": None,
}
_SYSTEM_PROMPTS_STUB = {"agent": None, "user": None}
_INITIALIZATION_STUB = {"workspace": None, "process_command": None}
_STOP_STUB = {
    "user_phrases": ["#### STOP"],
    "tool_names": ["local-claim_done"],
}


def minimal_harbor_task_config_dict(
    task_id: str,
    agent_workspace: str,
    log_file: str,
    *,
    single_turn_mode: bool = True,
    cn_mode: bool = False,
) -> dict[str, Any]:
    """Return a ``to_dict``-shaped stub safe for ``TaskConfig.from_dict``.

    ``task_str`` must be ``""`` (not omitted / None): ``TaskConfig.__post_init__``
    otherwise opens ``tasks/finalpool/.../docs/task.md``, which is absent on
    the agent-facing payload.
    """
    return {
        "id": task_id,
        "task_dir": task_id,
        "agent_workspace": agent_workspace,
        "log_file": log_file,
        "single_turn_mode": single_turn_mode,
        "cn_mode": cn_mode,
        "task_str": "",
        "needed_mcp_servers": None,
        "needed_local_tools": None,
        "task_root": None,
        "launch_time": None,
        "max_turns": None,
        "max_steps_under_single_turn_mode": None,
        "meta": {},
        "local_token_key_session": None,
        "system_prompts": dict(_SYSTEM_PROMPTS_STUB),
        "initialization": dict(_INITIALIZATION_STUB),
        "stop": {
            "user_phrases": list(_STOP_STUB["user_phrases"]),
            "tool_names": list(_STOP_STUB["tool_names"]),
        },
        "evaluation": dict(_EVALUATION_STUB),
    }


def upgrade_task_config_for_eval(task_config: Mapping[str, Any]) -> dict[str, Any]:
    """Fill only missing keys required for ``from_dict``; never overwrite filled values.

    Raises ``TypeError`` if ``task_config`` is not a mapping (caller should
    fail-closed — do not treat corrupt config as success).
    """
    if not isinstance(task_config, Mapping):
        raise TypeError(
            f"task_config must be a mapping, got {type(task_config).__name__}"
        )

    out: dict[str, Any] = dict(task_config)

    # Identity / path defaults only when absent.
    task_id = out.get("id") or out.get("task_dir")
    if "task_dir" not in out and task_id is not None:
        out["task_dir"] = task_id
    if "id" not in out and task_id is not None:
        out["id"] = task_id

    if "task_str" not in out or out["task_str"] is None:
        out["task_str"] = ""

    if "cn_mode" not in out:
        out["cn_mode"] = False
    if "single_turn_mode" not in out:
        out["single_turn_mode"] = True
    if "meta" not in out:
        out["meta"] = {}

    _ensure_nested(out, "evaluation", _EVALUATION_STUB)
    _ensure_nested(out, "system_prompts", _SYSTEM_PROMPTS_STUB)
    _ensure_nested(out, "initialization", _INITIALIZATION_STUB)
    _ensure_nested(out, "stop", _STOP_STUB)

    return out


def needs_eval_upgrade(task_config: Mapping[str, Any]) -> bool:
    """True when any from_dict-required nested block is missing or incomplete."""
    if not isinstance(task_config, Mapping):
        return True
    for key, stub in (
        ("evaluation", _EVALUATION_STUB),
        ("system_prompts", _SYSTEM_PROMPTS_STUB),
        ("initialization", _INITIALIZATION_STUB),
        ("stop", _STOP_STUB),
    ):
        block = task_config.get(key)
        if not isinstance(block, Mapping):
            return True
        for sub in stub:
            if sub not in block:
                return True
    # Missing or explicit None — empty string is valid for Harbor prep.
    if task_config.get("task_str") is None:
        return True
    return False


def _ensure_nested(
    out: MutableMapping[str, Any],
    key: str,
    stub: Mapping[str, Any],
) -> None:
    existing = out.get(key)
    if not isinstance(existing, Mapping):
        out[key] = {k: _copy_val(v) for k, v in stub.items()}
        return
    merged = dict(existing)
    for sub_key, default in stub.items():
        if sub_key not in merged:
            merged[sub_key] = _copy_val(default)
    out[key] = merged


def _copy_val(value: Any) -> Any:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return dict(value)
    return value
