from typing import Dict, Any, List, Optional
from utils.data_structures.task_config import TaskConfig, Evaluation
from utils.general.helper import run_command, read_json, write_json
from utils.evaluation.verdict import parse_grader_stdout
import logging
import os

_SYNTAX_MARKERS = (
    "SyntaxError:",
    "ImportError:",
    "ModuleNotFoundError:",
    "NameError:",
)


def _technical(details: str, grader_rc: int) -> Dict[str, Any]:
    return {
        "pass": None,
        "verdict": None,
        "grader_rc": grader_rc,
        "failure": "evaluator_failure",
        "details": details,
    }


def _content_outcome(stdout: str, stderr: str, returncode: Optional[int]) -> Dict[str, Any]:
    """Accept a content result only from one grader verdict line plus its exit code."""
    rc = returncode if isinstance(returncode, int) else 2
    combined = f"{stdout}\n{stderr}"
    parsed = parse_grader_stdout(stdout)
    if "Traceback (most recent call last):" in combined:
        return _technical("traceback", rc if rc > 1 or rc < 0 else 2)
    if any(marker in combined for marker in _SYNTAX_MARKERS):
        return _technical("syntax_or_import_error", rc if rc > 1 or rc < 0 else 2)
    if rc < 0 or rc > 1:
        return _technical(parsed.problem or "evaluator_failure", rc)
    if parsed.problem or parsed.value is None:
        return _technical(parsed.problem or "missing", 2)
    if parsed.value == "True" and rc == 0:
        return {
            "pass": True,
            "verdict": "True",
            "grader_rc": 0,
            "details": "All evaluation checks passed, and task status is success",
        }
    if parsed.value == "False" and rc == 1:
        return {
            "pass": False,
            "verdict": "False",
            "grader_rc": 1,
            "failure": "content_evaluation_failed",
            "details": "Content evaluation failed",
        }
    return _technical("verdict_exit_mismatch", 2)


class TaskEvaluator:
    """Task evaluator"""
    
    @staticmethod
    async def evaluate_one(dump_line: Dict[str, Any]) -> Dict[str, Any]:
        """
        Single task evaluation
        Expected content to be checked:
        - user response: check all outputs from user side
        - response: check all outputs from llm
        - tool calls: check all tool calls from llm
        - tool outputs: check all tool outputs
        ====== The following checks need to be started from config ======
        - local status: check files in specific workspace directory (e.g. saved some things, modified some things etc)
        - remote status: manually call MCP server to check if remote status is normally modified [not sure if possible]
        Use the above content to determine whether the task execution is successful or not
        """
        task_config = TaskConfig.from_dict(dump_line['config'])
        task_status = dump_line['status']
        # Prepare information for evaluation
        # Per-task graders open --res_log_file with "w" and dump their own report
        # into it. Pointing that at the agent's traj_log.json destroyed the run
        # record (config + status + trajectory) on every graded task — 1565 of the
        # shipped logs are already unusable for regrading. All 496 graders only
        # write this file, none read it, so give them their own path.
        res_log_file = os.path.join(os.path.dirname(task_config.log_file) or ".",
                                    "eval_report.json")
        agent_workspace = task_config.agent_workspace
        groundtruth_workspace = task_config.evaluation.groundtruth_workspace
        eval_command = task_config.evaluation.evaluation_command
        # Strip weekday name (e.g. "Sunday") from launch_time for eval-script compatibility
        launch_time = " ".join((task_config.launch_time or "").split()[:2])
        print(f"launch time in eval is {launch_time}")

        # Groundtruth isolation: in the split agent/eval flow the agent phase ran
        # with a sanitized task tree (no groundtruth_workspace/ or evaluation/), so
        # the config serialized into the log has these as None. Re-resolve them
        # against the CURRENT tree (the eval container mounts the full tasks dir).
        # For the legacy single-pass flow this is a no-op (paths already correct).
        if eval_command is None or groundtruth_workspace is None:
            fresh = Evaluation.build(task_config.task_dir,
                                     cn_mode=getattr(task_config, "cn_mode", False))
            eval_command = eval_command or fresh.evaluation_command
            groundtruth_workspace = groundtruth_workspace or fresh.groundtruth_workspace

        # First check task status: only SUCCESS is possible to pass; otherwise return pass = None
        # Same string as TaskStatus.SUCCESS. Importing task_agent pulls the agent stack.
        if task_status != "success":
            return {
                "pass": None,
                "details": f"Task status: {task_status}, only SUCCESS counts as pass; pass is null"
            }

        # Fail closed. An unresolvable grader means we cannot judge this run, which
        # is never the same thing as the run being correct. Evaluation.build resolves
        # the grader through a repo-root-relative path (task_config.py), so it yields
        # None whenever the grader is invoked from another cwd or the task has since
        # left tasks/finalpool — both reachable, and both used to return pass=True
        # with zero checks executed.
        if eval_command is None:
            return {
                "pass": None,
                "verdict": None,
                "grader_rc": 2,
                "failure": "evaluator_failure",
                "details": (f"Evaluation command could not be resolved for task "
                            f"'{task_config.task_dir}' — grader missing or cwd is not "
                            f"the repo root; refusing to report a pass."),
            }

        # Evaluate all content (only when task status is SUCCESS)
        if eval_command is not None:
            # try:
            args = f"--res_log_file {res_log_file} --agent_workspace {agent_workspace} --groundtruth_workspace {groundtruth_workspace} --launch_time \"{launch_time}\""
            command = f"{eval_command} {args}"
            output, error, returncode = await run_command(command, debug=True)
            print("== Evaluation STDOUT ==")
            print(output)
            print("== Evaluation STDERR ==")
            print(error)
            return _content_outcome(output or "", error or "", returncode)

        return _technical("evaluator_failure", grader_rc=2)
    
    @staticmethod
    async def evaluate_from_log_file(log_file_path: str, allow_resume: bool = False) -> Dict[str, Any]:
        """Evaluate task from log file"""
        try:            
            if not os.path.exists(log_file_path):
                return {
                    "pass": None,
                    "verdict": None,
                    "grader_rc": 2,
                    "failure": "log_file_not_found",
                    "details": f"Log file not found: {log_file_path}"
                }
            # if allow_resume AND we can load pre exist eval res, we just load it
            eval_file_path = os.path.join(os.path.dirname(log_file_path),"eval_res.json")
            if allow_resume and os.path.exists(eval_file_path):
                eval_res = read_json(eval_file_path)
                return eval_res
            # otherwise, we do real eval and store the eval result
            dump_line = read_json(log_file_path)
            eval_res = await TaskEvaluator.evaluate_one(dump_line)
            write_json(eval_res, eval_file_path)
            return eval_res
            
        except Exception as e:
            logging.error(f"Error evaluating from log file {log_file_path}: {e}")
            return {
                "pass": None,
                "verdict": None,
                "grader_rc": 2,
                "failure": "evaluation_error",
                "details": str(e)
            }
    
    @staticmethod
    async def batch_evaluate(run_results: List[Dict[str, Any]], allow_resume: bool=False) -> List[Dict[str, Any]]:
        """Batch evaluate task results"""
        eval_results = []
        
        for run_result in run_results:
            eval_result = {
                "task_config_path": run_result["task_config_path"],
                "task_id": run_result.get("task_id", "unknown"),
            }
            
            if not run_result.get("success", False):
                eval_result["evaluation"] = {
                    "pass": False,
                    "failure": "task_execution_failed",
                    "details": run_result.get("error", "Unknown error")
                }
            else:
                log_file = run_result.get("log_file")
                if log_file:
                    eval_result["evaluation"] = await TaskEvaluator.evaluate_from_log_file(log_file, allow_resume = allow_resume)
                else:
                    eval_result["evaluation"] = {
                        "pass": False,
                        "failure": "no_log_file",
                        "details": "No log file generated"
                    }
            
            eval_result["pass"] = eval_result["evaluation"]["pass"]
            eval_results.append(eval_result)
        
        return eval_results