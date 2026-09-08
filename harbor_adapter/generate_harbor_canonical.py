#!/usr/bin/env python3
"""Generate Harbor-canonical Cowork tasks (all 496) from finalpool sources.

Layout follows the approved PoC (public MCP facade + db/workspace gateways):
  agent → mcp-gateway-public → mcp-gateway-db / mcp-gateway-workspace

Minimal delta vs harbor-native rc packaging:
  - agent-agnostic instruction (docs/task.md core only)
  - task.toml lists only mcp-gateway-public streamable-http URLs
  - dual gateway compose; no PG* on main
  - workspace_lifecycle under tests/verifier only
  - solution/solve.sh Oracle for tasks with groundtruth_workspace
  - no Strands / ask_user / per-MCP HTTP rewrite
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import sys
import textwrap

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harbor_adapter"))

from native_mcp.catalog import (  # noqa: E402
    LOCAL_SERVERS,
    SHARED_WS,
    SOURCE_TASKS,
    TASK_PAYLOAD,
    all_task_ids,
    load_catalog,
    needs_db,
    servers_for_task,
    split_servers,
)
from native_mcp.prep.db_rewrite import rewrite_preprocess_db_connections  # noqa: E402

NATIVE_DIR = Path(__file__).resolve().parent / "native_mcp"
PREP_SRC = NATIVE_DIR / "prep" / "prepare_workspace.py"

# Built locally from harbor_adapter/Dockerfile.main and Dockerfile.postgres;
# see harbor_adapter/README.md. Override with --main-image / --postgres-image to
# point at your own registry, and pin by digest for reproducible runs.
MAIN_IMAGE = "cowork-harbor-main:local"
POSTGRES_IMAGE = "cowork-postgres:local"
MANIFEST_DB = "/opt/harbor_native_manifest/mcp_manifest.json"
PUBLIC_GW = "mcp-gateway-public"
PUBLIC_BASE = f"http://{PUBLIC_GW}:8000/mcp"

# Exact list of tasks without GitHub groundtruth_workspace (do not invent Oracle).
NO_GROUNDTRUTH: frozenset[str] = frozenset(
    {
        "canvas-enrollment-gsheet",
        "canvas-module-completion-teamly-gcal",
        "canvas-quiz-remediation-forms-gcal",
        "ch-support-csat-forms-gsheet-email",
        "ecommerce-commodity-impact",
        "insales-customer-order-gsheet-email",
        "insales-customer-survey-form",
        "insales-vip-customer-gsheet-gcal-email",
        "kulinar-diet-forms-teamly-excel",
        "kulinar-event-catering-excel-word",
        "kulinar-event-menu-planner",
        "kulinar-nutrition-benchmark",
        "kulinar-scholarly-health-study",
        "kulinar-weekly-gsheet-gcal",
        "kulinar-weekly-plan-gsheet-gcal",
        "market-competitive-intelligence-report",
        "moex-earnings-calendar-alert",
        "moex-market-news-digest-teamly-forms-email",
        "pw-scholarly-trend-analysis-excel-ppt",
        "rzd-kulinar-novgorod-trip-gcal-word",
        "sf-hr-attrition-gcal",
        "sf-hr-turnover-teamly",
        "sf-sales-discount-gsheet-email",
        "sf-support-agent-performance-gsheet-gcal",
        "yf-gold-performance-gsheet-notion",
    }
)

# Source dirs that exist but contain only .gitkeep (no usable Oracle payload).
EMPTY_GROUNDTRUTH: frozenset[str] = frozenset(
    {
        "rzd-canvas-fieldtrip-novgorod-gcal-word-email",
        "rzd-hr1c-training-trip-kazan-excel-email-gcal",
        "rzd-kulinar-team-trip-spb-catering-excel-gcal",
        "scholarly-fetch-gsheet-citation",
    }
)

WORKSPACE_HINT = (
    "\n\nРабочая директория: `/workspace/cowork_shared`. "
    "Пиши все файлы только туда. Сервисы доступны через MCP; "
    "к PostgreSQL напрямую не ходи.\n"
)


def _has_mock_http(source: Path) -> bool:
    return (source / "files" / "mock_pages.tar.gz").is_file()


def _usable_groundtruth(source: Path) -> bool:
    """Return True if groundtruth_workspace has at least one real file (not only .gitkeep)."""
    root = source / "groundtruth_workspace"
    if not root.is_dir():
        return False
    skip = {".gitkeep", ".gitignore"}
    return any(f.is_file() and f.name not in skip for f in root.rglob("*"))


def _rewrite_evaluation_db_secrets(evaluation_dir: Path) -> None:
    """Point live SQL checks at PGPASSWORD/PGHOST from grader env (no literals)."""
    if not evaluation_dir.is_dir():
        return
    patterns = [
        # "password": "anything" / 'password': 'anything'
        (
            re.compile(r'(["\']password["\']\s*:\s*)["\'][^"\']*["\']', re.I),
            r'\1__import__("os").environ["PGPASSWORD"]',
        ),
        # password="..." keyword args
        (
            re.compile(r'(\bpassword\s*=\s*)["\'][^"\']*["\']', re.I),
            r'\1__import__("os").environ["PGPASSWORD"]',
        ),
        # host defaults that ignore PGHOST
        (
            re.compile(
                r'(["\']host["\']\s*:\s*)(?:os\.environ\.get\(\s*["\']PGHOST["\']\s*,\s*)?["\']localhost["\']\)?'
            ),
            r'\1os.environ.get("PGHOST", "postgres")',
        ),
    ]
    for py in evaluation_dir.rglob("*.py"):
        original = py.read_text(encoding="utf-8", errors="ignore")
        updated = original
        for rx, repl in patterns:
            updated = rx.sub(repl, updated)
        if "import os" not in updated and "PGPASSWORD" in updated:
            updated = "import os\n" + updated
        if updated != original:
            py.write_text(updated, encoding="utf-8")


def _preprocess_needs_pg(source: Path) -> bool:
    prep = source / "preprocess" / "main.py"
    if not prep.is_file():
        return False
    text = prep.read_text(encoding="utf-8", errors="ignore")
    keys = (
        "psycopg",
        "PGPASSWORD",
        "PGHOST",
        "cowork_pg",
        "get_conn",
        "DB_CONFIG",
        "DB_CONN",
    )
    return any(k in text for k in keys)


def _server_entry(s: dict) -> dict:
    return {
        "name": s["name"],
        "command": s["command"],
        "args": s["args"],
        "cwd": s["cwd"],
        "inherit_postgres": bool(s.get("inherit_postgres") or s.get("needs_db")),
        "env": s.get("env") or {},
    }


def _task_toml(task: str, servers: list[dict], *, has_db: bool) -> str:
    blocks = [
        textwrap.dedent(
            f"""\
            schema_version = "1.1"

            [task]
            name = "cowork-bench-canonical/{task}"
            description = "Cowork Bench Harbor-canonical task (agent-agnostic; public MCP facade)."
            authors = []
            keywords = ["cowork-bench", "mcp", "harbor-canonical"]

            [metadata]
            source_task = "{task}"
            packaging = "harbor-canonical-public-facade"
            has_groundtruth = {str(task not in NO_GROUNDTRUTH and task not in EMPTY_GROUNDTRUTH).lower()}

            [verifier]
            timeout_sec = 900.0
            environment_mode = "shared"

            [agent]
            timeout_sec = 7200.0

            [environment]
            docker_image = "{MAIN_IMAGE}"
            network_mode = "public"
            build_timeout_sec = 1800.0
            workdir = "/workspace"
            cpus = 4
            memory_mb = 12288
            storage_mb = 20480
            gpus = 0

            # No PostgreSQL credentials on the agent.
            [environment.env]
            COWORK_TASK = "{task}"
            COWORK_SHARED_WORKSPACE = "{SHARED_WS}"
            """
        ).rstrip()
    ]
    for server in servers:
        blocks.append(
            "\n".join(
                [
                    "",
                    "[[environment.mcp_servers]]",
                    f'name = "{server["name"]}"',
                    'transport = "streamable-http"',
                    f'url = "{PUBLIC_BASE}/{server["name"]}"',
                ]
            )
        )
    blocks.append("\n\n[verifier.env]\n\n[solution.env]\n")
    body = "\n".join(blocks)
    finalize_cmd = (
        "PYTHONPATH=/tests/verifier:/workspace /opt/venv/bin/python3 "
        "/tests/verifier/workspace_lifecycle.py finalize"
    )
    # /tests and agent-log RO bind live on grader only; finalize before test.sh.
    # has_db is retained for call-site compatibility; collect target is always grader.
    _ = has_db
    body += textwrap.dedent(
        f"""

        [[verifier.collect]]
        service = "grader"
        command = "{finalize_cmd}"
        timeout_sec = 60.0

        [[verifier.collect]]
        service = "grader"
        command = "bash /tests/test.sh"
        timeout_sec = 900.0
        """
    )
    return body


def _compose(
    task: str,
    *,
    has_db: bool,
    has_db_gw: bool | None = None,
    has_workspace: bool,
    has_mock: bool,
) -> str:
    if has_db_gw is None:
        has_db_gw = has_db
    mock_vol = (
        "      - ./task_payload/files/mock_pages.tar.gz:/mock/mock_pages.tar.gz:ro\n"
        if has_mock
        else ""
    )
    if has_mock:
        mock_cmd = (
            "    command:\n"
            "      - bash\n"
            "      - -lc\n"
            "      - |\n"
            "        set -euo pipefail\n"
            "        mkdir -p /tmp/mock\n"
            "        tar -xzf /mock/mock_pages.tar.gz -C /tmp/mock\n"
            "        python3 -m http.server 30151 --bind 127.0.0.1 "
            "--directory /tmp/mock/mock_pages >/tmp/mock-http.log 2>&1 &\n"
            "        exec sleep infinity\n"
        )
    else:
        mock_cmd = "    command:\n      - sleep\n      - infinity\n"

    prep_network = "db_net" if has_db else "agent_net"
    prep_env_file = "    env_file:\n      - ./pg.env\n" if has_db else ""
    prep_depends = (
        "    depends_on:\n      postgres:\n        condition: service_healthy\n"
        if has_db
        else ""
    )

    postgres_block = ""
    if has_db:
        postgres_block = f"""
  postgres:
    image: "{POSTGRES_IMAGE}"
    networks:
      db_net:
        aliases:
          - cowork_pg
          - postgres
    env_file:
      - ./pg.env
    tmpfs:
      - /var/lib/postgresql/data:rw,size=2g
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U $$POSTGRES_USER -d $$POSTGRES_DB"]
      interval: 2s
      timeout: 5s
      retries: 90
      start_period: 2s
"""
    if has_db_gw:
        postgres_block += f"""
  mcp-gateway-db:
    image: "{MAIN_IMAGE}"
    working_dir: /workspace
    networks:
      - db_net
      - mcp_internal
    env_file:
      - ./pg.env
    environment:
      COWORK_TASK: "{task}"
      COWORK_SHARED_WORKSPACE: "{SHARED_WS}"
      LOCAL_SERVERS_PATH: {LOCAL_SERVERS}
      PYTHON_BIN: /opt/venv/bin/python3
      UV_NO_SYNC: "1"
      HOME: /tmp/cowork-home
      PGHOST: postgres
      PG_HOST: postgres
      PGPORT: "5432"
      PG_PORT: "5432"
    volumes:
      - cowork_workspace:{SHARED_WS}
      - ./mcp_runtime:/opt/harbor_native:ro
      - ./task_payload:{TASK_PAYLOAD}:ro
      - ./mcp_manifest_db.json:{MANIFEST_DB}:ro
    command:
      - /opt/venv/bin/python3
      - /opt/harbor_native/mcp_gateway.py
      - --manifest
      - {MANIFEST_DB}
      - --host
      - 0.0.0.0
      - --port
      - "8000"
      - --log-dir
      - /logs/mcp-gateway-db
    healthcheck:
      test:
        - CMD
        - /opt/venv/bin/python3
        - -c
        - "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2).status==200 else 1)"
      interval: 3s
      timeout: 5s
      retries: 60
      start_period: 15s
    depends_on:
      postgres:
        condition: service_healthy
      workspace-prep:
        condition: service_completed_successfully
"""

    workspace_block = ""
    if has_workspace:
        workspace_block = f"""
  mcp-gateway-workspace:
    image: "{MAIN_IMAGE}"
    working_dir: /workspace
    networks:
      - mcp_workspace_net
    environment:
      COWORK_TASK: "{task}"
      COWORK_SHARED_WORKSPACE: "{SHARED_WS}"
      LOCAL_SERVERS_PATH: {LOCAL_SERVERS}
      PYTHON_BIN: /opt/venv/bin/python3
      UV_NO_SYNC: "1"
      HOME: /tmp/cowork-home
    volumes:
      - cowork_workspace:{SHARED_WS}
      - ./mcp_runtime:/opt/harbor_native:ro
      - ./mcp_manifest_workspace.json:{MANIFEST_DB}:ro
    command:
      - /opt/venv/bin/python3
      - /opt/harbor_native/mcp_gateway.py
      - --manifest
      - {MANIFEST_DB}
      - --host
      - 0.0.0.0
      - --port
      - "8000"
      - --log-dir
      - /logs/mcp-gateway-workspace
    healthcheck:
      test:
        - CMD
        - /opt/venv/bin/python3
        - -c
        - "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2).status==200 else 1)"
      interval: 3s
      timeout: 5s
      retries: 60
      start_period: 15s
    depends_on:
      workspace-prep:
        condition: service_completed_successfully
"""

    public_depends = []
    if has_db_gw:
        public_depends.append(
            "      mcp-gateway-db:\n        condition: service_healthy"
        )
    if has_workspace:
        public_depends.append(
            "      mcp-gateway-workspace:\n        condition: service_healthy"
        )
    public_depends_s = "\n".join(public_depends)

    # Harbor mounts trial agent/artifacts binds onto main only
    # (write_mounts_compose_file → services.main.volumes). Grader gets the same
    # host agent dir via Harbor's legacy HOST_AGENT_LOGS_PATH compose env
    # (legacy_log_mount_env_vars), read-only for completion inference.
    grader_main_vols = (
        "      - cowork_artifacts:/logs/artifacts/cowork\n"
        "      - grader_out:/grader_out:ro\n"
    )
    if has_db:
        grader_networks = "      - db_net\n"
        grader_env_file = "    env_file:\n      - ./pg.env\n"
        grader_pg_env = (
            '      PGHOST: postgres\n'
            '      PG_HOST: postgres\n'
            '      PGPORT: "5432"\n'
            '      PG_PORT: "5432"\n'
        )
        grader_depends = (
            "    depends_on:\n"
            "      postgres:\n"
            "        condition: service_healthy\n"
            "      workspace-prep:\n"
            "        condition: service_completed_successfully\n"
        )
        grader_net_comment = "db_net only (agent/main cannot reach it)."
    else:
        grader_networks = "      - grader_net\n"
        grader_env_file = ""
        grader_pg_env = ""
        grader_depends = (
            "    depends_on:\n"
            "      workspace-prep:\n"
            "        condition: service_completed_successfully\n"
        )
        grader_net_comment = "grader_net only (not on agent_net)."

    grader_service = f"""
  # Harbor verifier-phase grader: {grader_net_comment}
  # Collect runs finalize + tests/test.sh here; /tests stays off main.
  # HOST_AGENT_LOGS_PATH is injected by Harbor 0.22 for the trial agent bind.
  grader:
    image: "{MAIN_IMAGE}"
    working_dir: /workspace
    networks:
{grader_networks}{grader_env_file}    environment:
      COWORK_TASK: "{task}"
      COWORK_SHARED_WORKSPACE: "{SHARED_WS}"
      COWORK_GRADER: "1"
      LOCAL_SERVERS_PATH: {LOCAL_SERVERS}
      PYTHON_BIN: /opt/venv/bin/python3
      UV_NO_SYNC: "1"
      HOME: /tmp/cowork-home
{grader_pg_env}    volumes:
      - cowork_workspace:{SHARED_WS}
      - cowork_artifacts:/logs/artifacts/cowork
      - grader_out:/grader_out
      - ../tests:/tests:ro
      - ${{HOST_AGENT_LOGS_PATH:-/tmp/cowork-harbor-unset-agent-logs}}:/logs/agent:ro
    command:
      - sleep
      - infinity
{grader_depends}"""
    volumes_extra = "  cowork_artifacts:\n  grader_out:\n"

    return f"""# Agent sees ONLY mcp-gateway-public (task.toml).
# mcp-gateway-db: db_net + mcp_internal — DB MCP (not on agent_net).
# mcp-gateway-workspace: mcp_workspace_net only — no Postgres.
# Healthchecks cover task services only (not Polar / LLM).

networks:
  agent_net: {{}}
  db_net: {{}}
  mcp_internal: {{}}
  mcp_workspace_net: {{}}
  grader_net: {{}}

services:
  main:
    image: "{MAIN_IMAGE}"
    networks:
      - agent_net
    depends_on:
      workspace-prep:
        condition: service_completed_successfully
      mcp-gateway-public:
        condition: service_healthy
    environment:
      COWORK_TASK: "{task}"
      COWORK_SHARED_WORKSPACE: "{SHARED_WS}"
      LOCAL_SERVERS_PATH: {LOCAL_SERVERS}
      PYTHON_BIN: /opt/venv/bin/python3
      UV_NO_SYNC: "1"
      HOME: /tmp/cowork-home
      UV_VENV_CLEAR: "1"
    volumes:
      - cowork_workspace:{SHARED_WS}
{grader_main_vols}{mock_vol}      - ./agent_opt_stub:/opt/harbor_native:ro
    extra_hosts:
      - "host.docker.internal:host-gateway"
    shm_size: "2g"
{mock_cmd}
  workspace-prep:
    image: "{MAIN_IMAGE}"
    working_dir: /workspace
    networks:
      - {prep_network}
{prep_env_file}    environment:
      COWORK_TASK: "{task}"
      PYTHONPATH: /task_payload
    volumes:
      - cowork_workspace:{SHARED_WS}
      - ./prep:/opt/prep:ro
      - ./task_payload:/task_payload:ro
    command:
      - /opt/venv/bin/python3
      - /opt/prep/prepare_workspace.py
      - --task
      - {task}
{prep_depends}{postgres_block}{workspace_block}{grader_service}
  mcp-gateway-public:
    image: "{MAIN_IMAGE}"
    working_dir: /workspace
    networks:
      - agent_net
      - mcp_internal
      - mcp_workspace_net
    environment:
      COWORK_TASK: "{task}"
      COWORK_SHARED_WORKSPACE: "{SHARED_WS}"
      LOCAL_SERVERS_PATH: {LOCAL_SERVERS}
      PYTHON_BIN: /opt/venv/bin/python3
      UV_NO_SYNC: "1"
      HOME: /tmp/cowork-home
    volumes:
      - ./mcp_runtime:/opt/harbor_native:ro
      - ./mcp_manifest_public.json:{MANIFEST_DB}:ro
    command:
      - /opt/venv/bin/python3
      - /opt/harbor_native/mcp_gateway.py
      - --manifest
      - {MANIFEST_DB}
      - --host
      - 0.0.0.0
      - --port
      - "8000"
      - --log-dir
      - /logs/mcp-gateway-public
    healthcheck:
      test:
        - CMD
        - /opt/venv/bin/python3
        - -c
        - "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2).status==200 else 1)"
      interval: 3s
      timeout: 5s
      retries: 60
      start_period: 20s
    depends_on:
{public_depends_s}

volumes:
  cowork_workspace:
{volumes_extra}"""


_CLASSIFIER_PYTHON_SCRIPT = r"""import sys, os, json
from datetime import datetime, timezone

mode = sys.argv[1]
try:
    eval_rc = int(sys.argv[2])
except Exception:
    eval_rc = 1
stdout_path = sys.argv[3]
stderr_path = sys.argv[4]
result_json_path = sys.argv[5]
eval_res_path = sys.argv[6]
completion_out = sys.argv[7]
reward_out = sys.argv[8]

stdout_txt = ""
if os.path.isfile(stdout_path):
    try:
        with open(stdout_path, "r", encoding="utf-8", errors="replace") as f:
            stdout_txt = f.read()
    except Exception:
        pass

stderr_txt = ""
if os.path.isfile(stderr_path):
    try:
        with open(stderr_path, "r", encoding="utf-8", errors="replace") as f:
            stderr_txt = f.read()
    except Exception:
        pass

combined = stdout_txt + "\n" + stderr_txt

result_present = False
result_valid = False
if os.path.isfile(result_json_path):
    try:
        with open(result_json_path, "r", encoding="utf-8") as f:
            d = json.load(f)
        result_present = True
        if isinstance(d, dict) and ("total_checks" in d or "checks" in d or "score" in d or "passed" in d):
            result_valid = True
    except Exception:
        pass

has_traceback = "Traceback (most recent call last):" in combined
syntax_or_import = any(
    err in combined
    for err in ["SyntaxError:", "ImportError:", "ModuleNotFoundError:", "NameError:"]
)
timestamp = datetime.now(timezone.utc).isoformat()

if mode == "oracle":
    has_checks = any(
        marker in combined
        for marker in ["[PASS]", "[FAIL]", "checks passed", "Overall:", "FAIL", "PASS", "CRITICAL FAIL", "accuracy"]
    )
    if eval_rc == 0 and not has_traceback and not syntax_or_import:
        status = "succeeded"
        tech_status = "completed"
        reward = 1
        eval_completed = True
        err_type = None
        err_msg = None
    elif has_traceback or syntax_or_import:
        status = "failed"
        tech_status = "error"
        reward = 0
        eval_completed = False
        err_type = "python_traceback" if has_traceback else "syntax_or_import_error"
        tb_lines = [l.strip() for l in combined.splitlines() if l.strip()]
        err_msg = tb_lines[-1] if tb_lines else "Unhandled Python error"
    elif has_checks or result_valid:
        status = "succeeded"
        tech_status = "completed"
        reward = 0
        eval_completed = True
        err_type = None
        err_msg = "Content checks failed"
    else:
        status = "failed"
        tech_status = "error"
        reward = 0
        eval_completed = False
        err_type = "evaluator_crash"
        err_msg = f"Evaluator exited with rc={eval_rc} without checks output"
elif mode == "ua":
    traj_missing = "Cowork traj_log.json not found" in combined
    if traj_missing:
        status = "failed"
        tech_status = "error"
        reward = 0
        eval_completed = False
        err_type = "missing_traj_log"
        err_msg = "Cowork traj_log.json not found"
    else:
        eval_res_data = None
        if os.path.isfile(eval_res_path):
            try:
                with open(eval_res_path, "r", encoding="utf-8") as f:
                    eval_res_data = json.load(f)
            except Exception:
                pass
        if isinstance(eval_res_data, dict) and eval_res_data.get("pass") is True:
            status = "succeeded"
            tech_status = "completed"
            reward = 1
            eval_completed = True
            err_type = None
            err_msg = None
        elif isinstance(eval_res_data, dict) and eval_res_data.get("pass") is False:
            status = "succeeded"
            tech_status = "completed"
            reward = 0
            eval_completed = True
            err_type = None
            err_msg = "Content evaluation failed"
        elif has_traceback:
            status = "failed"
            tech_status = "error"
            reward = 0
            eval_completed = False
            err_type = "python_traceback"
            tb_lines = [l.strip() for l in combined.splitlines() if l.strip()]
            err_msg = tb_lines[-1] if tb_lines else "run_eval traceback"
        else:
            status = "failed"
            tech_status = "error"
            reward = 0
            eval_completed = False
            err_type = "eval_contract_fail"
            err_msg = f"Invalid eval_res.json (rc={eval_rc})"

record = {
    "version": "2.0.6",
    "timestamp": timestamp,
    "status": status,
    "technical_status": tech_status,
    "reward": reward,
    "evaluator_rc": eval_rc,
    "evaluator_completed": eval_completed,
    "result_present": result_present,
    "error_type": err_type,
    "error_message": err_msg[:500] if err_msg else None,
}

tmp_file = completion_out + ".tmp"
os.makedirs(os.path.dirname(os.path.abspath(completion_out)), exist_ok=True)
with open(tmp_file, "w", encoding="utf-8") as f:
    json.dump(record, f, indent=2)
os.replace(tmp_file, completion_out)

if status == "succeeded" and reward is not None:
    os.makedirs(os.path.dirname(os.path.abspath(reward_out)), exist_ok=True)
    with open(reward_out, "w", encoding="utf-8") as f:
        f.write(f"{reward}\n")

print(
    f"CLASSIFICATION: status={status} technical_status={tech_status} "
    f"reward={reward} rc={eval_rc} error_type={err_type}"
)
"""


def _verifier(task: str, *, has_db: bool) -> str:
    """Generate verifier script: grader evaluates; main only consumes handoff.

    ``has_db`` is kept for call-site compatibility; both categories use the
    isolated grader + ``/grader_out`` handoff contract.
    """
    _ = has_db
    return textwrap.dedent(
        f"""\
            #!/bin/bash
            set -uo pipefail

            TASK={task!r}
            ROOT=/workspace/tasks/finalpool/$TASK
            mkdir -p /logs/verifier "$ROOT"

            HANDOFF_DIR=/grader_out
            COMPLETION="$HANDOFF_DIR/completion.json"
            REWARD_FILE="$HANDOFF_DIR/reward.txt"
            EVAL_LOG=/logs/verifier/cowork-eval.log
            EVAL_STDOUT=/logs/verifier/evaluator.stdout.log
            EVAL_STDERR=/logs/verifier/evaluator.stderr.log
            RESULT_JSON=/logs/verifier/oracle-result.json
            EVAL_RES=""

            # Harbor runs this branch in the isolated grader container.
            if [ -n "${{COWORK_GRADER:-}}" ]; then
              mkdir -p "$HANDOFF_DIR"
              rm -f "$REWARD_FILE" "$COMPLETION" "$COMPLETION.tmp"
              rm -f "$EVAL_LOG" "$EVAL_STDOUT" "$EVAL_STDERR" "$RESULT_JSON"
              rm -rf "$ROOT/evaluation" "$ROOT/groundtruth_workspace" "$ROOT/groundtruth_workspace_cn"
              if [ -d /tests/evaluation ]; then
                cp -a /tests/evaluation "$ROOT/evaluation"
              fi
              if [ -d /tests/groundtruth_workspace ]; then
                cp -a /tests/groundtruth_workspace "$ROOT/groundtruth_workspace"
              fi
              if [ -d /tests/groundtruth_workspace_cn ]; then
                cp -a /tests/groundtruth_workspace_cn "$ROOT/groundtruth_workspace_cn"
              fi

              ORACLE_WORKSPACE=/logs/artifacts/cowork/oracle_workspace
              if [ -f "$ORACLE_WORKSPACE/.oracle-ready" ]; then
                MODE="oracle"
                set +e
                PYTHONPATH=/workspace /opt/venv/bin/python3 -u "$ROOT/evaluation/main.py" \\
                  --agent_workspace "$ORACLE_WORKSPACE" \\
                  --groundtruth_workspace "$ROOT/groundtruth_workspace" \\
                  --res_log_file "$RESULT_JSON" \\
                  >"$EVAL_STDOUT" 2>"$EVAL_STDERR"
                EVAL_RC=$?
                set -e
              else
                MODE="ua"
                LOG=$(/opt/venv/bin/python3 -c "import glob; p=sorted(glob.glob('/logs/artifacts/cowork/**/traj_log.json',recursive=True)); print(p[0] if p else '')")
                if [ -z "$LOG" ]; then
                  printf '%s\n' "Cowork traj_log.json not found" >"$EVAL_STDERR"
                  : >"$EVAL_STDOUT"
                  EVAL_RC=2
                else
                  EVAL_RES="$(dirname "$LOG")/eval_res.json"
                  set +e
                  PYTHONPATH=/workspace /opt/venv/bin/python3 -u /workspace/scripts/run_eval.py \\
                    --log_file "$LOG" >"$EVAL_STDOUT" 2>"$EVAL_STDERR"
                  EVAL_RC=$?
                  set -e
                fi
              fi

              cat "$EVAL_STDOUT" "$EVAL_STDERR" >"$EVAL_LOG" 2>/dev/null || true
              cp "$EVAL_STDOUT" "$HANDOFF_DIR/evaluator.stdout.log" 2>/dev/null || true
              cp "$EVAL_STDERR" "$HANDOFF_DIR/evaluator.stderr.log" 2>/dev/null || true
              cp "$EVAL_LOG" "$HANDOFF_DIR/cowork-eval.log" 2>/dev/null || true
              if [ -f "$RESULT_JSON" ]; then
                cp "$RESULT_JSON" "$HANDOFF_DIR/oracle-result.json" 2>/dev/null || true
              fi

              # Robust completion classification and atomic marker creation
              /opt/venv/bin/python3 - "$MODE" "$EVAL_RC" "$EVAL_STDOUT" "$EVAL_STDERR" "$RESULT_JSON" "$EVAL_RES" "$COMPLETION" "$REWARD_FILE" <<'PY'
{_CLASSIFIER_PYTHON_SCRIPT.strip()}
PY

              exit 0
            fi

            # Shared main has no PG env or DB network; it only consumes handoff.
            deadline=$((SECONDS + 900))
            while [ ! -f "$COMPLETION" ] && [ "$SECONDS" -lt "$deadline" ]; do
              sleep 1
            done
            if [ ! -f "$COMPLETION" ]; then
              echo "grader handoff completion marker is missing" >&2
              exit 2
            fi

            HANDOFF=$(/opt/venv/bin/python3 - "$COMPLETION" <<'PY'
import sys, json
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        item = json.load(handle)
    status = item.get("status")
    reward = item.get("reward")
    tech_status = item.get("technical_status", "unknown")
    err_type = item.get("error_type") or "none"
    err_msg = item.get("error_message") or ""
    if status not in ("succeeded", "failed"):
        raise ValueError("invalid status")
    print(status, reward if reward is not None else "none", tech_status, err_type, err_msg)
except Exception:
    sys.exit(2)
PY
            ) || {{
              echo "grader handoff completion marker is invalid" >&2
              exit 2
            }}

            read -r HANDOFF_STATUS HANDOFF_REWARD HANDOFF_TECH HANDOFF_ERR_TYPE HANDOFF_ERR_MSG <<<"$HANDOFF"
            rm -rf /logs/verifier/* 2>/dev/null || true
            cp -a "$HANDOFF_DIR"/. /logs/verifier/ 2>/dev/null || true

            if [ "$HANDOFF_STATUS" = "failed" ] || [ "$HANDOFF_TECH" = "error" ]; then
              echo "grader evaluator failed: error_type=$HANDOFF_ERR_TYPE message=$HANDOFF_ERR_MSG" >&2
              echo 0 > /logs/verifier/reward.txt
              exit 2
            fi

            if [ "$HANDOFF_STATUS" != "succeeded" ]; then
              echo "grader handoff did not complete successfully" >&2
              exit 2
            fi

            printf '%s\n' "$HANDOFF_REWARD" > /logs/verifier/reward.txt
            echo "accepted grader sidecar reward=$HANDOFF_REWARD"
            exit 0
            """
    )


def _solve_sh(task: str) -> str:
    return textwrap.dedent(
        f"""\
        #!/bin/bash
        # Harbor Oracle: apply groundtruth_workspace into the shared workspace.
        set -euo pipefail

        SRC="/solution/groundtruth_workspace"
        WS="${{COWORK_SHARED_WORKSPACE:-{SHARED_WS}}}"
        ORACLE="/logs/artifacts/cowork/oracle_workspace"

        if [ ! -d "$SRC" ]; then
          echo "missing $SRC" >&2
          exit 1
        fi

        mkdir -p "$WS" "$ORACLE" /logs/artifacts/cowork
        cp -a "$SRC"/. "$WS"/
        cp -a "$SRC"/. "$ORACLE"/
        touch "$ORACLE/.oracle-ready"

        mkdir -p "$WS/.cowork"
        python3 - <<'PY'
        import json
        from datetime import datetime
        from pathlib import Path
        ws = Path({SHARED_WS!r})
        ctx = {{
          "task": {task!r},
          "provider": "oracle",
          "model": "solve.sh",
          "workspace": str(ws),
          "log_file": "/logs/artifacts/cowork/traj_log.json",
          "task_config": {{
            "id": {task!r},
            "task_dir": {task!r},
            "agent_workspace": str(ws),
            "log_file": "/logs/artifacts/cowork/traj_log.json",
            "single_turn_mode": True,
          }},
          "start_time": datetime.now().isoformat(),
        }}
        (ws / ".cowork").mkdir(parents=True, exist_ok=True)
        (ws / ".cowork" / "cli_context.json").write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")
        Path("/logs/artifacts/cowork").mkdir(parents=True, exist_ok=True)
        Path("/logs/artifacts/cowork/traj_log.json").write_text(
          json.dumps({{
            "config": ctx["task_config"],
            "status": "success",
            "start_time": ctx["start_time"],
            "end_time": datetime.now().isoformat(),
            "completion": {{"confirmed": True, "reason": "oracle_solve_sh", "framework": "oracle", "evidence": ["solution/solve.sh"]}},
          }}, ensure_ascii=False, indent=2),
          encoding="utf-8",
        )
        print("oracle applied groundtruth_workspace ->", ws)
        PY
        """
    )


def _copy_payload(source: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    skip = {
        "evaluation",
        "groundtruth_workspace",
        "groundtruth_workspace_cn",
        "docs",
        "dumps",
    }
    for item in source.iterdir():
        if item.name in skip:
            continue
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def _pg_env_example() -> str:
    return (
        "# Copy to pg.env (gitignored). Do not commit real secrets.\n"
        "POSTGRES_USER=\n"
        "POSTGRES_PASSWORD=\n"
        "POSTGRES_DB=\n"
        "PGUSER=\n"
        "PG_USER=\n"
        "PGPASSWORD=\n"
        "PG_PASSWORD=\n"
        "PGDATABASE=\n"
        "PG_DATABASE=\n"
        "PGHOST=postgres\n"
        "PG_HOST=postgres\n"
        "PGPORT=5432\n"
        "PG_PORT=5432\n"
    )


def _generate_pg_env_sh() -> str:
    return textwrap.dedent(
        """\
        #!/usr/bin/env bash
        set -euo pipefail
        ROOT="$(cd "$(dirname "$0")" && pwd)"
        OUT="${1:-$ROOT/pg.env}"
        : "${POSTGRES_USER:?set POSTGRES_USER}"
        : "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}"
        : "${POSTGRES_DB:?set POSTGRES_DB}"
        cat >"$OUT" <<EOF
        POSTGRES_USER=${POSTGRES_USER}
        POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
        POSTGRES_DB=${POSTGRES_DB}
        PGUSER=${POSTGRES_USER}
        PG_USER=${POSTGRES_USER}
        PGPASSWORD=${POSTGRES_PASSWORD}
        PG_PASSWORD=${POSTGRES_PASSWORD}
        PGDATABASE=${POSTGRES_DB}
        PG_DATABASE=${POSTGRES_DB}
        PGHOST=postgres
        PG_HOST=postgres
        PGPORT=5432
        PG_PORT=5432
        EOF
        chmod 600 "$OUT"
        echo "Wrote $OUT"
        """
    )


def convert_one(task: str, output_root: Path, catalog: dict) -> dict:
    """Convert one Cowork task to canonical Harbor packaging."""
    source = SOURCE_TASKS / task
    if not (source / "task_config.json").is_file():
        raise FileNotFoundError(f"missing task_config.json for {task}")

    servers = servers_for_task(task, catalog)
    db_servers, ws_servers = split_servers(servers)
    # Re-check by name for safety (yaml heuristics + DB_MCP_NAMES).
    if not db_servers:
        db_servers = [s for s in servers if needs_db(s["name"], catalog)]
        ws_servers = [s for s in servers if s not in db_servers]
    has_db_gw = bool(db_servers)
    has_db = has_db_gw or _preprocess_needs_pg(source)
    has_workspace = bool(ws_servers)
    if not has_db and not has_workspace:
        has_workspace = True
    has_mock = _has_mock_http(source)
    has_gt = (
        _usable_groundtruth(source)
        and task not in NO_GROUNDTRUTH
        and task not in EMPTY_GROUNDTRUTH
    )

    target = output_root / task
    if target.exists():
        shutil.rmtree(target)
    env_dir = target / "environment"
    (env_dir / "mcp_runtime").mkdir(parents=True)
    (env_dir / "prep").mkdir(parents=True)
    (env_dir / "agent_opt_stub").mkdir(parents=True)
    (env_dir / "task_payload").mkdir(parents=True)
    (target / "tests" / "verifier").mkdir(parents=True)
    (target / "solution").mkdir(parents=True)

    instruction = (source / "docs" / "task.md").read_text(
        encoding="utf-8"
    ).rstrip() + "\n"
    instruction = instruction + WORKSPACE_HINT
    for banned in (
        "TURN_FINISHED",
        "turn_finish",
        "OpenHands",
        "Qwen Code",
        "Strands",
        "Universal Agent",
        "ask_user",
    ):
        if banned.lower() in instruction.lower():
            raise ValueError(
                f"{task}: instruction still contains agent-specific marker {banned!r}"
            )
    (target / "instruction.md").write_text(instruction, encoding="utf-8")
    (target / "task.toml").write_text(
        _task_toml(task, servers, has_db=has_db), encoding="utf-8"
    )
    (env_dir / "docker-compose.yaml").write_text(
        _compose(
            task,
            has_db=has_db,
            has_db_gw=has_db_gw,
            has_workspace=has_workspace,
            has_mock=has_mock,
        ),
        encoding="utf-8",
    )

    db_manifest = {
        "task": task,
        "role": "db",
        "servers": [_server_entry(s) for s in db_servers],
    }
    ws_manifest = {
        "task": task,
        "role": "workspace",
        "servers": [_server_entry(s) for s in ws_servers],
    }
    public_servers = []
    for s in db_servers:
        public_servers.append(
            {
                "name": s["name"],
                "upstream": f"http://mcp-gateway-db:8000/mcp/{s['name']}",
            }
        )
    for s in ws_servers:
        public_servers.append(
            {
                "name": s["name"],
                "upstream": f"http://mcp-gateway-workspace:8000/mcp/{s['name']}",
            }
        )
    public_manifest = {"task": task, "role": "public-facade", "servers": public_servers}

    (env_dir / "mcp_manifest_db.json").write_text(
        json.dumps(db_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (env_dir / "mcp_manifest_workspace.json").write_text(
        json.dumps(ws_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (env_dir / "mcp_manifest_public.json").write_text(
        json.dumps(public_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    # Combined legacy name for debugging only (not mounted by compose).
    (env_dir / "mcp_manifest.json").write_text(
        json.dumps(
            {"task": task, "servers": [_server_entry(s) for s in servers]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    if has_db:
        # Docker Compose requires env_file to exist even when it is empty.
        # Runtime credentials are injected separately and never packaged.
        (env_dir / "pg.env").write_bytes(b"")
        (env_dir / "pg.env.example").write_text(_pg_env_example(), encoding="utf-8")
        gen = env_dir / "generate_pg_env.sh"
        gen.write_text(_generate_pg_env_sh(), encoding="utf-8")
        gen.chmod(0o755)
        (env_dir / ".gitignore").write_text("pg.env\n.pg_password\n", encoding="utf-8")

    (env_dir / "agent_opt_stub" / "README.md").write_text(
        "Stub mount for /opt/harbor_native on main.\n"
        "Lifecycle lives under tests/verifier only — not agent-visible.\n",
        encoding="utf-8",
    )
    shutil.copy2(PREP_SRC, env_dir / "prep" / "prepare_workspace.py")
    shutil.copy2(PREP_SRC.parent / "db_rewrite.py", env_dir / "prep" / "db_rewrite.py")
    shutil.copy2(
        NATIVE_DIR / "task_config_stub.py",
        env_dir / "prep" / "task_config_stub.py",
    )
    for name in ("__init__.py", "catalog.py", "mcp_gateway.py"):
        shutil.copy2(NATIVE_DIR / name, env_dir / "mcp_runtime" / name)

    test_sh = target / "tests" / "test.sh"
    test_sh.write_text(_verifier(task, has_db=has_db), encoding="utf-8")
    test_sh.chmod(0o755)
    shutil.copytree(
        source / "evaluation",
        target / "tests" / "evaluation",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    _rewrite_evaluation_db_secrets(target / "tests" / "evaluation")
    for dirname in ("groundtruth_workspace", "groundtruth_workspace_cn"):
        src = source / dirname
        if src.exists():
            shutil.copytree(
                src,
                target / "tests" / dirname,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
    for name in ("workspace_lifecycle.py", "completion.py", "task_config_stub.py"):
        shutil.copy2(NATIVE_DIR / name, target / "tests" / "verifier" / name)

    _copy_payload(source, env_dir / "task_payload")
    preprocess_dir = env_dir / "task_payload" / "preprocess"
    for preprocess in sorted(preprocess_dir.rglob("*.py")):
        preprocess.write_text(
            rewrite_preprocess_db_connections(preprocess.read_text(encoding="utf-8")),
            encoding="utf-8",
        )

    if has_gt:
        shutil.copytree(
            source / "groundtruth_workspace",
            target / "solution" / "groundtruth_workspace",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        solve = target / "solution" / "solve.sh"
        solve.write_text(_solve_sh(task), encoding="utf-8")
        solve.chmod(0o755)
    else:
        (target / "solution" / "NO_GROUNDTRUTH").write_text(
            f"{task}\nOracle intentionally absent — no verified ground truth.\n",
            encoding="utf-8",
        )

    mcp_names = [s["name"] for s in servers]
    flags = {
        "clickhouse": "clickhouse" in mcp_names
        or task.startswith("ch-")
        or "clickhouse" in task,
        "hr1c": "hr1c" in mcp_names or "hr1c" in task,
        "multi_workspace": len(ws_servers) >= 2,
        "has_mock_http": has_mock,
    }
    summary = {
        "source_task": task,
        "needed_mcp_servers": mcp_names,
        "db_mcp": [s["name"] for s in db_servers],
        "workspace_mcp": [s["name"] for s in ws_servers],
        "gateway_urls": [f"{PUBLIC_BASE}/{s['name']}" for s in servers],
        "has_db": has_db,
        "has_workspace": has_workspace,
        "has_oracle": has_gt,
        "no_groundtruth": task in NO_GROUNDTRUTH
        or task in EMPTY_GROUNDTRUTH
        or not has_gt,
        "empty_groundtruth": task in EMPTY_GROUNDTRUTH
        or (
            (source / "groundtruth_workspace").is_dir()
            and not _usable_groundtruth(source)
        ),
        "compose_services": [
            "main",
            "workspace-prep",
            *(["postgres", "mcp-gateway-db"] if has_db else []),
            *(["mcp-gateway-workspace"] if has_workspace else []),
            "mcp-gateway-public",
        ],
        "workspace": SHARED_WS,
        **flags,
    }
    (target / "metadata.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def main() -> int:
    """Entry point for generating Harbor-canonical Cowork dataset."""
    import argparse

    global MAIN_IMAGE, POSTGRES_IMAGE, SOURCE_TASKS

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "datasets" / "cowork_harbor_canonical_v1",
        help="Output directory for generated Harbor tasks (default: <repo>/datasets/cowork_harbor_canonical_v1)",
    )
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--tasks", nargs="*", default=None)
    parser.add_argument("--main-image", default=MAIN_IMAGE)
    parser.add_argument("--postgres-image", default=POSTGRES_IMAGE)
    parser.add_argument("--source-tasks", type=Path, default=None)
    args = parser.parse_args()
    MAIN_IMAGE = args.main_image
    POSTGRES_IMAGE = args.postgres_image
    if args.source_tasks:
        # catalog.SOURCE_TASKS is used by servers_for_task; patch module attr.
        import native_mcp.catalog as cat

        cat.SOURCE_TASKS = args.source_tasks
        SOURCE_TASKS = args.source_tasks  # noqa: F841 — keep local alias consistent

    catalog = load_catalog()
    if args.all:
        ids = all_task_ids()
    elif args.tasks:
        ids = args.tasks
    else:
        ids = all_task_ids()

    args.output.mkdir(parents=True, exist_ok=True)
    index: list[dict] = []
    errors: list[str] = []
    for task in ids:
        try:
            index.append(convert_one(task, args.output, catalog))
            print(f"OK {task}")
        except Exception as exc:
            errors.append(f"{task}: {exc}")
            print(f"FAIL {task}: {exc}")

    report = {
        "n": len(index),
        "n_fail": len(errors),
        "with_oracle": sum(1 for r in index if r.get("has_oracle")),
        "no_groundtruth": sorted(
            r["source_task"] for r in index if r.get("no_groundtruth")
        ),
        "clickhouse": sorted(r["source_task"] for r in index if r.get("clickhouse")),
        "hr1c": sorted(r["source_task"] for r in index if r.get("hr1c")),
        "errors": errors,
        "ids": [r["source_task"] for r in index],
        "tasks": index,
    }
    (args.output / "INDEX.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output / "NO_GROUNDTRUTH.txt").write_text(
        "\n".join(sorted(NO_GROUNDTRUTH)) + "\n", encoding="utf-8"
    )
    (args.output / "EMPTY_GROUNDTRUTH.txt").write_text(
        "\n".join(sorted(EMPTY_GROUNDTRUTH)) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {k: report[k] for k in report if k not in {"tasks", "ids"}},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
