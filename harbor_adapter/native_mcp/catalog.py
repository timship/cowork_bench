"""Resolve Cowork stdio MCP specs for Harbor-canonical HTTP packaging."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

try:
    from .postgres_env import filter_spec_env
except ImportError:  # flat copy under environment/mcp_runtime
    from postgres_env import filter_spec_env

ROOT = Path(__file__).resolve().parents[2]
SOURCE_TASKS = ROOT / "tasks" / "finalpool"
MCP_YAML_DIR = ROOT / "configs" / "mcp_servers"
SHARED_WS = "/workspace/cowork_shared"
LOCAL_SERVERS = "/opt/local_servers"
TASK_PAYLOAD = "/task_payload"

# MCP that talk to Postgres (or PG-backed adapters like ClickHouse/hr1c).
# Derived from local_servers that use psycopg / PG_* — keep workspace-only
# tools (terminal/memory/filesystem/office binaries) off db_net.
DB_MCP_NAMES = {
    "arxiv_local",
    "arxiv-latex",
    "scholarly",
    "clickhouse",
    "canvas",
    "forms",
    "hr1c",
    "insales",
    "rzd",
    "teamly",
    "woocommerce",
    "youtube",
    "youtube-transcript",
    "emails",
    "google_calendar",
    "google_sheet",
    "moex-finance",
    "yahoo-finance",
}


def pg_env_from_environ() -> dict[str, str]:
    """Build PG* map from environment only — no hardcoded passwords."""
    required = ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise ValueError(
            "missing required Postgres env vars (use environment/pg.env): "
            + ", ".join(missing)
        )
    host = os.environ["PGHOST"]
    port = os.environ.get("PGPORT", "5432")
    db = os.environ["PGDATABASE"]
    user = os.environ["PGUSER"]
    password = os.environ["PGPASSWORD"]
    return {
        "PGHOST": host,
        "PG_HOST": host,
        "PGPORT": port,
        "PG_PORT": port,
        "PGDATABASE": db,
        "PG_DATABASE": db,
        "PGUSER": user,
        "PG_USER": user,
        "PGPASSWORD": password,
        "PG_PASSWORD": password,
    }


def expand(value: Any, *, task: str = "") -> Any:
    if not isinstance(value, str):
        return value
    return (
        value.replace("${local_servers_paths}", LOCAL_SERVERS)
        .replace("${agent_workspace}", SHARED_WS)
        .replace("${task_dir}", TASK_PAYLOAD)
        .replace("cowork_pg", "postgres")
    )


def load_catalog(yaml_dir: Path | None = None) -> dict[str, dict]:
    yaml_dir = yaml_dir or MCP_YAML_DIR
    out: dict[str, dict] = {}
    for path in sorted(yaml_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        name = data.get("name") or path.stem
        record = {**data, "yaml_stem": path.stem, "yaml_path": str(path)}
        out[name] = record
        out[path.stem] = record
    return out


def catalog_names(catalog: dict[str, dict] | None = None) -> list[str]:
    catalog = catalog or load_catalog()
    names = sorted({spec.get("name") for spec in catalog.values() if spec.get("name")})
    return names


def _yaml_needs_db(spec: dict) -> bool:
    name = str(spec.get("name") or "")
    if name in DB_MCP_NAMES:
        return True
    params = spec.get("params") or {}
    env = params.get("env") or {}
    for key, val in {**env}.items():
        ku = str(key).upper()
        if ku.startswith("PG") or "PASSWORD" in ku:
            return True
        if isinstance(val, str) and ("postgres" in val.lower() or "cowork_pg" in val):
            return True
    return False


def needs_db(server_name: str, catalog: dict[str, dict] | None = None) -> bool:
    catalog = catalog or load_catalog()
    if server_name in DB_MCP_NAMES:
        return True
    spec = catalog.get(server_name)
    if not spec:
        return False
    return _yaml_needs_db(spec)


def resolve_server(
    name: str,
    spec: dict,
    *,
    task: str = "",
    include_pg: bool | None = None,
) -> dict:
    params = spec.get("params") or {}
    command = expand(params.get("command", ""), task=task)
    args = [expand(v, task=task) for v in params.get("args", [])]
    cwd = expand(params.get("cwd") or SHARED_WS, task=task)
    # Strip any password literals from yaml; DB gateway injects via inherit_postgres.
    # Also drop conflicting PG_* when inherit_postgres so generated manifests cannot
    # override task pg.env (runtime gateway filters again as a second line of defense).
    raw_env = {
        k: expand(v, task=task)
        for k, v in (params.get("env") or {}).items()
        if not str(k).upper().endswith("PASSWORD") and "PASSWORD" not in str(k).upper()
    }
    db = (spec.get("name") or name) in DB_MCP_NAMES or _yaml_needs_db(spec)
    if include_pg is None:
        include_pg = db
    inherit = bool(db)
    env = filter_spec_env(raw_env, inherit_postgres=inherit)
    _ = include_pg  # reserved for runtime injection; manifests use inherit_postgres
    return {
        "name": spec.get("name") or name,
        "command": command,
        "args": args,
        "cwd": cwd,
        "env": env,
        "yaml_stem": spec.get("yaml_stem"),
        "needs_db": db,
        "inherit_postgres": inherit,
    }


def servers_for_task(task: str, catalog: dict[str, dict] | None = None) -> list[dict]:
    catalog = catalog or load_catalog()
    cfg = json.loads((SOURCE_TASKS / task / "task_config.json").read_text(encoding="utf-8"))
    wanted = cfg.get("needed_mcp_servers") or []
    resolved: list[dict] = []
    missing: list[str] = []
    for name in wanted:
        spec = catalog.get(name)
        if not spec:
            missing.append(name)
            continue
        resolved.append(resolve_server(name, spec, task=task))
    if missing:
        raise ValueError(f"{task}: no MCP yaml for {missing}")
    return resolved


def split_servers(servers: list[dict]) -> tuple[list[dict], list[dict]]:
    db = [s for s in servers if s.get("needs_db")]
    workspace = [s for s in servers if not s.get("needs_db")]
    return db, workspace


def all_task_ids() -> list[str]:
    return sorted(
        p.name
        for p in SOURCE_TASKS.iterdir()
        if p.is_dir() and (p / "task_config.json").exists()
    )


def needed_mcp_map() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for task in all_task_ids():
        cfg = json.loads((SOURCE_TASKS / task / "task_config.json").read_text())
        out[task] = list(cfg.get("needed_mcp_servers") or [])
    return out
