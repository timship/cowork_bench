#!/usr/bin/env python3
"""Regression: inherit_postgres must not be overridden by YAML PG_USER=eigent."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "harbor_adapter"))
sys.path.insert(0, str(ROOT / "harbor_adapter" / "native_mcp"))

from native_mcp.postgres_env import (  # noqa: E402
    INHERITED_POSTGRES_ENV_KEYS,
    build_stdio_env,
    filter_spec_env,
)
from native_mcp.catalog import resolve_server  # noqa: E402


class FilterSpecEnvTests(unittest.TestCase):
    def test_inherit_drops_conflicting_pg_keys_keeps_others(self) -> None:
        filtered = filter_spec_env(
            {
                "PG_USER": "eigent",
                "PG_HOST": "cowork_pg",
                "PG_PORT": "5432",
                "PG_DATABASE": "cowork_gym",
                "TEAMLY_EXTRA": "keep-me",
                "NODE_ENV": "production",
            },
            inherit_postgres=True,
        )
        self.assertNotIn("PG_USER", filtered)
        self.assertNotIn("PG_HOST", filtered)
        self.assertEqual(filtered["TEAMLY_EXTRA"], "keep-me")
        self.assertEqual(filtered["NODE_ENV"], "production")

    def test_non_inherit_keeps_pg_keys(self) -> None:
        filtered = filter_spec_env(
            {"PG_USER": "eigent", "OTHER": "x"},
            inherit_postgres=False,
        )
        self.assertEqual(filtered["PG_USER"], "eigent")
        self.assertEqual(filtered["OTHER"], "x")


class TeamlyGatewayEnvTests(unittest.TestCase):
    """teamly YAML historically hardcodes PG_USER=eigent; task pg.env wins."""

    def test_teamly_like_manifest_uses_task_pg_user(self) -> None:
        task_environ = {
            "PATH": "/usr/bin",
            "HOME": "/tmp",
            "PGHOST": "postgres",
            "PG_HOST": "postgres",
            "PGPORT": "5432",
            "PG_PORT": "5432",
            "PGDATABASE": "cowork_gym",
            "PG_DATABASE": "cowork_gym",
            "PGUSER": "smoke_user",
            "PG_USER": "smoke_user",
            "PGPASSWORD": "task-secret-not-logged",
            "PG_PASSWORD": "task-secret-not-logged",
            "UNRELATED": "1",
        }
        spec = {
            "name": "teamly",
            "inherit_postgres": True,
            "command": "node",
            "args": ["index.js"],
            "env": {
                "PG_HOST": "cowork_pg",
                "PG_PORT": "5432",
                "PG_DATABASE": "cowork_gym",
                "PG_USER": "eigent",
                "TEAMLY_SPACE": "docs",
            },
        }
        env = build_stdio_env(spec, environ=task_environ)
        self.assertEqual(env["PG_USER"], "smoke_user")
        self.assertEqual(env["PGUSER"], "smoke_user")
        self.assertEqual(env["PG_HOST"], "postgres")
        self.assertEqual(env["PGHOST"], "postgres")
        self.assertEqual(env["PG_DATABASE"], "cowork_gym")
        self.assertEqual(env["TEAMLY_SPACE"], "docs")
        # Ready path would use the same user as task Postgres; never eigent.
        self.assertNotEqual(env["PG_USER"], "eigent")
        for key in INHERITED_POSTGRES_ENV_KEYS:
            if key in env and "PASSWORD" in key.upper():
                self.assertEqual(env[key], "task-secret-not-logged")

    def test_without_inherit_strips_all_pg_from_child(self) -> None:
        environ = {
            "PATH": "/usr/bin",
            "PG_USER": "smoke_user",
            "PGPASSWORD": "x",
        }
        spec = {
            "name": "terminal",
            "inherit_postgres": False,
            "env": {"PG_USER": "eigent", "FOO": "bar"},
        }
        env = build_stdio_env(spec, environ=environ)
        self.assertEqual(env["FOO"], "bar")
        self.assertNotIn("PG_USER", env)
        self.assertNotIn("PGPASSWORD", env)


class CatalogResolveTeamlyTests(unittest.TestCase):
    def test_resolve_server_strips_pg_user_for_db_mcp(self) -> None:
        spec = {
            "name": "teamly",
            "params": {
                "command": "node",
                "args": ["${local_servers_paths}/teamly-mcp/build/index.js"],
                "env": {
                    "PG_HOST": "cowork_pg",
                    "PG_PORT": "5432",
                    "PG_DATABASE": "cowork_gym",
                    "PG_USER": "eigent",
                    "PG_PASSWORD": "camel",
                    "TEAMLY_EXTRA": "keep",
                },
            },
        }
        resolved = resolve_server("teamly", spec, task="arxiv-research-workflow-pipeline")
        self.assertTrue(resolved["inherit_postgres"])
        self.assertNotIn("PG_USER", resolved["env"])
        self.assertNotIn("PG_HOST", resolved["env"])
        self.assertNotIn("PG_PASSWORD", resolved["env"])  # passwords stripped earlier too
        self.assertEqual(resolved["env"].get("TEAMLY_EXTRA"), "keep")


if __name__ == "__main__":
    unittest.main()
