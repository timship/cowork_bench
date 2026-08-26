"""StrandsTaskAgent — Strands-based runner with the same contract as
utils/roles/task_agent.TaskAgent (init(task_config, model, max_steps),
async run() -> TaskStatus). The log format on disk matches CAMEL's exactly
so utils/evaluation/evaluator.py works unchanged.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from strands import Agent, tool
from strands.agent.conversation_manager.summarizing_conversation_manager import (
    DEFAULT_SUMMARIZATION_PROMPT,
    SummarizingConversationManager,
)
from strands.hooks import BeforeModelCallEvent, HookProvider, HookRegistry, MessageAddedEvent

from utils.data_structures.task_config import TaskConfig
from utils.general.helper import copy_folder_contents, print_color

from .local_tools import build_local_tools
from .mcp_clients import build_mcp_clients


@tool
def _summary_noop() -> str:
    """Заглушка: даёт summary-агенту непустой tools (некоторые шлюзы отвергают tools:[])."""
    return "ok"


# strands/event_loop drives the agent loop through `recurse_event_loop` — one
# Python recursion level per step (~3.05 frames measured). At CPython's default
# limit of 1000 the loop dies with RecursionError at ~322 steps, which is what
# silently capped every earlier Strands run and recorded it as status=failed.
# Give the configured budget room to be the actual binding constraint.
_FRAMES_PER_STEP = 8      # measured ~3.05; 8 leaves headroom for MCP/tool nesting
_RECURSION_BASE = 1000    # non-loop frames (imports, asyncio, evaluator)
_RECURSION_CEILING = 20000  # past this the 8MB C stack, not this limit, is the wall


def _raise_recursion_limit(max_steps: Optional[int]) -> int:
    """Lift sys.recursionlimit so that max_steps, not CPython, bounds the run.

    The unbounded opt-out still gets headroom: leaving CPython's default in place
    would silently reinstate the ~322-step wall. Even at the ceiling the 8MB C
    stack caps the loop near ~1663 steps — a recursive event loop has no truly
    unbounded mode.
    """
    if not max_steps or max_steps <= 0:
        needed = _RECURSION_CEILING
    else:
        needed = min(max_steps * _FRAMES_PER_STEP + _RECURSION_BASE, _RECURSION_CEILING)
    if needed > sys.getrecursionlimit():
        sys.setrecursionlimit(needed)
    return sys.getrecursionlimit()


class _StepBudgetExceeded(Exception):
    """Raised from the hook once the agent has spent its step budget."""


def _budget_exhausted(exc: BaseException) -> Optional[_StepBudgetExceeded]:
    """Unwrap a budget stop: strands wraps hook exceptions in EventLoopException."""
    return _find_cause(exc, _StepBudgetExceeded)


def _find_cause(exc: BaseException, cls) -> Optional[BaseException]:
    """Walk the exception chain, including strands' EventLoopException wrapper."""
    seen = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, cls):
            return cur
        seen.add(id(cur))
        cur = getattr(cur, "original_exception", None) or cur.__cause__ or cur.__context__
    return None


# Failures that are the harness's fault, not the model's. Matched on the message
# because litellm surfaces most of them as generic wrappers by the time they
# reach here; the exact classes live behind strands_runner/model.py.
_INFRA_ERROR_MARKERS = (
    "llm stream stalled",
    "no user query found in messages",
    "cannot summarize: insufficient messages",
    "apiconnectionerror",
    "internalservererror",
    "serviceunavailable",
    "midstreamfallbackerror",
    "connection error",
    "request size exceeded",
    "no endpoints found",
)


def _classify_error(exc: BaseException) -> "TaskStatus":
    """Separate infrastructure breakage from the model honestly failing."""
    if _find_cause(exc, RecursionError) is not None:
        return TaskStatus.INFRA_ERROR
    msg = str(exc).lower()
    if any(m in msg for m in _INFRA_ERROR_MARKERS):
        return TaskStatus.INFRA_ERROR
    return TaskStatus.FAILED


class _StepBudget(HookProvider):
    """Enforces max_steps for the Strands event loop, which has no built-in turn cap.

    A *step* is one assistant message produced by the model — the same unit CAMEL's
    ``max_iteration`` bounds, so the two runners stay comparable. Counting model calls
    directly would be wrong: strands/event_loop retries throttled calls inside the same
    loop iteration, and a rate-limit retry is not an agent step.

    The budget is checked before the *next* model call, so the tools requested by the
    final permitted step still run — the agent is cut off, not truncated mid-step.
    """

    def __init__(self, max_steps: Optional[int]):
        # Non-positive / unset means "unbounded" — explicit opt-out, not an accident.
        self.max_steps = max_steps if (max_steps and max_steps > 0) else None
        self.steps = 0

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(MessageAddedEvent, self._on_message)
        registry.add_callback(BeforeModelCallEvent, self._on_before_model_call)

    def _on_message(self, event: MessageAddedEvent) -> None:
        msg = event.message
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
        if role == "assistant":
            self.steps += 1

    def _on_before_model_call(self, event: BeforeModelCallEvent) -> None:
        if self.max_steps is not None and self.steps >= self.max_steps:
            raise _StepBudgetExceeded(f"step budget exhausted: {self.steps}/{self.max_steps}")


class TaskStatus(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    MAX_TURNS_REACHED = "max_turns_reached"
    INTERRUPTED = "interrupted"
    # The harness broke, not the model: stalled stream, provider 5xx, gateway
    # rejection, or the recursive event loop exhausting the Python stack. These
    # used to land in FAILED, charging infra flakiness to the model's score.
    # evaluator.py maps every non-SUCCESS status to pass=null, so adding a value
    # changes no verdict — it only makes the cause legible downstream.
    INFRA_ERROR = "infra_error"


# Phase 2: Russian global system prompt is loaded from prompts/global_system_bench.md
# and concatenated with the task-specific system prompt:
#     <now_line> + <global_system_bench> + <task.system_prompts.agent>
# This mirrors how strands/agent/core.py assembles its prompts (see core.py:299-310).
_GLOBAL_SYS_PROMPT_PATH = Path(__file__).parent / "prompts" / "global_system_bench.md"

# Fallback used if global_system_bench.md is missing (should not happen in production).
_FALLBACK_SYS_PROMPT_TMPL = (
    "You are a helpful AI assistant. Your workspace directory is: {workspace}\n"
    "Complete the user's task using the provided tools, then end your turn."
)


def _load_global_system_bench() -> str:
    try:
        return _GLOBAL_SYS_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


# Lines mentioning claim_done in task system prompts get scrubbed — we use
# end_turn as the completion signal, not a sentinel tool. Pattern catches
# both "claim_done" and "local-claim_done" with surrounding context.
_CLAIM_DONE_LINE_RE = re.compile(r"^.*claim[_-]?done.*$\n?", flags=re.IGNORECASE | re.MULTILINE)


def _serialize_messages(messages) -> list:
    """Strands Messages → JSON-friendly list. Strands content is a list of
    blocks ({text}, {toolUse}, {toolResult}); we serialize as-is so traj.json
    captures the full multi-modal trajectory."""
    out = []
    for m in messages or []:
        try:
            out.append({"role": getattr(m, "role", None) or m.get("role"),
                        "content": getattr(m, "content", None) or m.get("content")})
        except Exception:
            out.append(str(m))
    # default=str handles dataclasses, datetime, bytes-fallback inside content blocks
    return json.loads(json.dumps(out, default=str, ensure_ascii=False))


def _extract_tool_calls(messages) -> list:
    """Flatten toolUse blocks across the trajectory — convenient for analysis,
    matches the shape evaluator's traj.json tool_calls field used to take."""
    calls = []
    for m in messages or []:
        content = getattr(m, "content", None) or (m.get("content") if isinstance(m, dict) else None) or []
        for blk in content:
            if not isinstance(blk, dict):
                continue
            if "toolUse" in blk:
                calls.append(blk["toolUse"])
    return json.loads(json.dumps(calls, default=str, ensure_ascii=False))


class StrandsTaskAgent:
    def __init__(self, task_config: TaskConfig, model, max_steps: int = 300, debug: bool = False):
        self.task_config = task_config
        self.model = model
        self.max_steps = max_steps  # enforced via _StepBudget (Strands has no built-in turn cap)
        self.debug = debug
        self._workspace: Optional[str] = None
        self._budget: Optional[_StepBudget] = None

    async def _setup_workspace(self) -> str:
        workspace = os.path.abspath(self.task_config.agent_workspace)
        # Drop stale artifacts from previous runs (fixed dump path is reused).
        if os.path.isdir(workspace):
            shutil.rmtree(workspace, ignore_errors=True)
        os.makedirs(workspace, exist_ok=True)
        init = self.task_config.initialization
        if init and init.workspace and os.path.exists(str(init.workspace)):
            await copy_folder_contents(str(init.workspace), workspace)
        for srv, d in [("arxiv_local", "arxiv_local_storage"),
                       ("memory", "memory"),
                       ("playwright_with_chunk", ".playwright_output")]:
            if srv in self.task_config.needed_mcp_servers:
                os.makedirs(os.path.join(workspace, d), exist_ok=True)
        return workspace

    def _run_preprocess(self):
        init = self.task_config.initialization
        if not (init and init.process_command):
            return
        cmd = init.process_command
        cmd += f" --agent_workspace {self.task_config.agent_workspace}"
        lt = self.task_config.launch_time or ""
        lt_clean = " ".join(lt.split()[:2])
        cmd += f" --launch_time \"{lt_clean}\""
        print_color("[preprocess] running...", "yellow")
        r = subprocess.run(cmd, shell=True, capture_output=not self.debug, text=True)
        if r.returncode != 0:
            print_color(f"[preprocess] failed:\n{r.stderr or ''}", "red")
            raise RuntimeError(f"preprocess failed (exit {r.returncode})")
        print_color("[preprocess] done.", "green")

    def _build_system_prompt(self, workspace: str) -> str:
        # Phase 2 — Russian. Layered: now_line + global_system_bench + task prompt.
        # Order matches strands/agent/core.py:299-310 (date → global → task).
        now_line = f"Текущее время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}.\n"

        global_bench = _load_global_system_bench()
        if not global_bench:
            global_bench = _FALLBACK_SYS_PROMPT_TMPL.format(workspace=workspace)

        sp = self.task_config.system_prompts
        task_prompt = ""
        if sp and sp.agent:
            # Scrub any claim_done instructions — we use stop_reason=="end_turn"
            # as the completion signal, not a benchmark-specific sentinel tool.
            task_prompt = _CLAIM_DONE_LINE_RE.sub("", sp.agent).strip()

        workspace_line = f"\nРабочая директория агента: {workspace}\n"

        parts = [now_line.rstrip(), global_bench.rstrip(), workspace_line.rstrip()]
        if task_prompt:
            parts.append(task_prompt.rstrip())
        return "\n\n".join(parts)

    def _save_log(self, status: TaskStatus, start_time: datetime,
                  result=None, messages=None, error: Optional[str] = None):
        log_path = self.task_config.log_file
        if not log_path:
            return
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)

        # traj_log.json: evaluator reads this — needs `config` + `status`.
        record = {
            "config": self.task_config.to_dict(),
            "status": status.value,
            "start_time": start_time.isoformat(),
            "end_time": datetime.now().isoformat(),
        }
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)

        # traj.json: full trajectory for analysis (not read by evaluator).
        traj_path = str(Path(log_path).parent / "traj.json")
        # steps_used comes from the budget hook, not from len(messages): the
        # summarizing conversation manager drops older turns, so counting the
        # surviving assistant messages undercounts long runs.
        traj = {
            "status": status.value,
            "start_time": start_time.isoformat(),
            "end_time": datetime.now().isoformat(),
            "stop_reason": getattr(result, "stop_reason", None) if result else None,
            "max_steps": self._budget.max_steps if self._budget else self.max_steps,
            "steps_used": self._budget.steps if self._budget else None,
            # Without this an infra failure is indistinguishable in traj.json from
            # the model honestly failing the task.
            "error": error,
            "messages": _serialize_messages(messages),
            "tool_calls": _extract_tool_calls(messages),
        }
        with open(traj_path, "w", encoding="utf-8") as f:
            json.dump(traj, f, ensure_ascii=False, indent=2, default=str)

    async def run(self) -> TaskStatus:
        start_time = datetime.now()
        status = TaskStatus.FAILED
        result = None
        error: Optional[str] = None
        agent: Optional[Agent] = None

        try:
            workspace = await self._setup_workspace()
            self._workspace = workspace
            self._run_preprocess()

            task_src_dir = os.path.abspath(os.path.join("tasks/finalpool", self.task_config.task_dir))
            http_mcp_urls = None
            http_mcp_timeout = float(os.environ.get("MCP_HTTP_TIMEOUT", "600"))
            raw_urls = os.environ.get("MCP_HTTP_URLS", "")
            if raw_urls:
                http_mcp_urls = {}
                for item in raw_urls.split(","):
                    if "=" in item:
                        k, v = item.split("=", 1)
                        http_mcp_urls[k.strip()] = v.strip()

            mcp_clients = build_mcp_clients(
                self.task_config.needed_mcp_servers, workspace,
                task_dir=task_src_dir,
                http_mcp_urls=http_mcp_urls,
                http_mcp_timeout=http_mcp_timeout,
            )

            # Strands MCPClient is a context manager — ExitStack composes N of them.
            with contextlib.ExitStack() as stack:
                for c in mcp_clients:
                    stack.enter_context(c)
                mcp_tools = []
                for c in mcp_clients:
                    mcp_tools.extend(c.list_tools_sync())

                local_tools = build_local_tools(
                    workspace, self.task_config.needed_local_tools or [],
                )
                all_tools = mcp_tools + local_tools

                print_color(
                    f"[strands] Total tools: {len(all_tools)} (MCP: {len(mcp_tools)}, local: {len(local_tools)})",
                    "cyan",
                )
                tool_names = []
                for t in all_tools:
                    n = getattr(t, "tool_name", None) or getattr(t, "name", None) or repr(t)
                    tool_names.append(n)
                print_color(f"[strands] Tool names: {tool_names}", "cyan")

                sys_prompt = self._build_system_prompt(workspace)
                summarizer = Agent(
                    model=self.model,
                    system_prompt=DEFAULT_SUMMARIZATION_PROMPT,
                    tools=[_summary_noop],
                )
                budget = _StepBudget(self.max_steps)
                self._budget = budget
                rec_limit = _raise_recursion_limit(self.max_steps)
                print_color(
                    f"[strands] step budget: {budget.max_steps or 'unbounded'} "
                    f"(recursionlimit={rec_limit})", "cyan")
                agent = Agent(
                    model=self.model,
                    system_prompt=sys_prompt,
                    tools=all_tools,
                    hooks=[budget],
                    conversation_manager=SummarizingConversationManager(
                        summarization_agent=summarizer,
                        summary_ratio=0.3,
                        preserve_recent_messages=10,
                        proactive_compression={"compression_threshold": 0.7},
                    ),
                )

                task_str = self.task_config.task_str
                print_color(f"\n[task] {task_str[:300]}\n", "yellow")
                try:
                    result = await agent.invoke_async(task_str)
                except Exception as e:
                    hit = _budget_exhausted(e)
                    if hit is None:
                        raise
                    print_color(f"[strands] {hit}", "yellow")
                    self._save_log(TaskStatus.MAX_TURNS_REACHED, start_time,
                                   result=None, messages=getattr(agent, "messages", None),
                                   error=str(hit))
                    return TaskStatus.MAX_TURNS_REACHED

                stop_reason = getattr(result, "stop_reason", None)
                if self.debug:
                    print_color(f"[strands] stop_reason={stop_reason}", "cyan")

                # Completion signal: model voluntarily ended its turn. The
                # evaluator (eval_res.json) decides whether the produced
                # artifacts actually solve the task — orchestration success
                # and task-correctness are separated on purpose.
                if stop_reason == "end_turn":
                    status = TaskStatus.SUCCESS
                    print_color("[strands] Agent ended turn cleanly.", "green")
                elif stop_reason == "max_tokens":
                    status = TaskStatus.MAX_TURNS_REACHED
                    print_color("[strands] Hit max_tokens.", "yellow")
                else:
                    print_color(f"[strands] Unexpected stop_reason={stop_reason}", "red")

        except KeyboardInterrupt:
            status = TaskStatus.INTERRUPTED
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            status = _classify_error(e)
            colour = "yellow" if status is TaskStatus.INFRA_ERROR else "red"
            print_color(f"[strands] {status.value}: {error}", colour)
            if self.debug:
                traceback.print_exc()

        messages = getattr(agent, "messages", None) if agent is not None else None
        self._save_log(status, start_time, result=result, messages=messages, error=error)
        return status
