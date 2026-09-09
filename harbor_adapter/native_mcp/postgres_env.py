"""Postgres env inheritance for MCP gateway / catalog manifests.

When a server has ``inherit_postgres``, values from the task ``pg.env`` /
process environment are authoritative. Hardcoded ``PG_*`` keys from MCP YAML
or manifest ``spec.env`` must not override them.
"""

from __future__ import annotations

from typing import Mapping

# Keys copied from the task/runtime environment when inherit_postgres is set.
# Keep HOST/USER/PASSWORD/DATABASE (+ port) aliases in sync with mcp_gateway.
INHERITED_POSTGRES_ENV_KEYS = frozenset(
    {
        "PGHOST",
        "PG_HOST",
        "PGPORT",
        "PG_PORT",
        "PGDATABASE",
        "PG_DATABASE",
        "PGUSER",
        "PG_USER",
        "PGPASSWORD",
        "PG_PASSWORD",
    }
)


def is_inherited_postgres_key(key: str) -> bool:
    """Return True if *key* would conflict with inherit_postgres injection."""
    return str(key).upper() in INHERITED_POSTGRES_ENV_KEYS


def filter_spec_env(
    spec_env: Mapping[str, object] | None,
    *,
    inherit_postgres: bool,
) -> dict[str, str]:
    """Return manifest/YAML env safe to apply on top of the process env.

    When ``inherit_postgres`` is True, conflicting Postgres credential keys are
    dropped so ``dict.update`` cannot overwrite task ``pg.env`` values.
    Other keys are preserved. Secrets are not logged here.
    """
    out: dict[str, str] = {}
    for key, value in (spec_env or {}).items():
        name = str(key)
        if inherit_postgres and is_inherited_postgres_key(name):
            continue
        out[name] = str(value)
    return out


def inject_inherited_postgres(
    env: dict[str, str],
    environ: Mapping[str, str],
) -> None:
    """Copy authoritative Postgres keys from *environ* into *env* (in place)."""
    for key in INHERITED_POSTGRES_ENV_KEYS:
        if key in environ:
            env[key] = environ[key]


def build_stdio_env(
    spec: Mapping[str, object],
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the subprocess env for one MCP server slot.

    Mirrors ``Gateway.start_one`` credential rules without starting processes.
    """
    import os

    base = environ if environ is not None else os.environ
    inherit = bool(spec.get("inherit_postgres"))
    env = {
        k: v
        for k, v in base.items()
        if not k.upper().startswith("PG") and not k.upper().startswith("POSTGRES")
    }
    if inherit:
        inject_inherited_postgres(env, base)
    env.update(filter_spec_env(spec.get("env") if isinstance(spec.get("env"), Mapping) else None, inherit_postgres=inherit))
    if not inherit:
        for key in list(env):
            if key.upper().startswith("PG") or key.upper().startswith("POSTGRES"):
                env.pop(key, None)
    return env
