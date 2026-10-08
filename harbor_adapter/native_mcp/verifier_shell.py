"""Sanitized TaskConfig produced only through TaskConfig.to_dict().

Generation writes the result to tests/grader_private/task_contract.json.
That file is mounted for the grader after the agent stops. The two
evaluation fields stay null so Evaluation.build fills them later.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_TASK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def assert_null_evaluation(contract: dict, task: str) -> None:
    evaluation = contract.get("evaluation", None)
    if not isinstance(evaluation, dict):
        raise SystemExit("grader contract missing evaluation object")
    if set(evaluation) != {"groundtruth_workspace", "evaluation_command"}:
        raise SystemExit("grader contract evaluation shape")
    if evaluation["groundtruth_workspace"] is not None or evaluation["evaluation_command"] is not None:
        raise SystemExit("grader contract contains private evaluation")
    if contract.get("task_dir") != task:
        raise SystemExit("grader contract task does not match")


def sanitized_task_config(task: str, *, agent_workspace: str, log_file: str) -> dict:
    if not isinstance(task, str) or _TASK_NAME.fullmatch(task) is None:
        raise SystemExit("refusing task name")
    repo = Path(__file__).resolve().parents[2]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from utils.data_structures.task_config import (  # noqa: WPS433
        Evaluation,
        Initialization,
        StopConditions,
        SystemPrompts,
        TaskConfig,
    )

    cfg = TaskConfig(
        task_dir=task,
        id=task,
        needed_mcp_servers=None,
        needed_local_tools=None,
        task_root=task,
        task_str="",
        log_file=log_file,
        agent_workspace=agent_workspace,
        launch_time="",
        max_turns=None,
        max_steps_under_single_turn_mode=None,
        single_turn_mode=True,
        cn_mode=False,
        system_prompts=SystemPrompts(agent=None, user=None),
        initialization=Initialization(workspace=None, process_command=None),
        stop=StopConditions(
            user_phrases=["#### STOP"],
            tool_names=["local-claim_done"],
        ),
        evaluation=Evaluation(groundtruth_workspace=None, evaluation_command=None),
        meta={},
        local_token_key_session=None,
    )
    data = cfg.to_dict()
    evaluation = data.get("evaluation")
    if not isinstance(evaluation, dict):
        raise SystemExit("to_dict omitted evaluation")
    if evaluation.get("groundtruth_workspace") is not None:
        raise SystemExit("to_dict emitted a groundtruth path")
    if evaluation.get("evaluation_command") is not None:
        raise SystemExit("to_dict emitted an evaluation command")
    data["task_dir"] = task
    data["id"] = task
    data["task_root"] = task
    data["task_str"] = ""
    data["agent_workspace"] = agent_workspace
    data["log_file"] = log_file
    data["evaluation"] = {
        "groundtruth_workspace": None,
        "evaluation_command": None,
    }
    return data
