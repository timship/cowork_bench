"""Tests for AST-guided PostgreSQL preprocess rewriting."""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harbor_adapter"))

from native_mcp.prep.db_rewrite import (  # noqa: E402
    rewrite_preprocess_db_connections,
    unresolved_connection_literals,
)

CONNECTION_CASES = [
    pytest.param(
        'DB = {"host": "localhost", "dbname": "cowork_gym", "user": "eigent", "password": "camel"}\n'
        "import psycopg2\npsycopg2.connect(**DB)\n",
        id="mapping",
    ),
    pytest.param(
        'DB = dict(host="localhost", port=5432, dbname="cowork_gym", user="eigent", password="camel")\n'
        "import psycopg2\npsycopg2.connect(**DB)\n",
        id="dict-keywords",
    ),
    pytest.param(
        "import psycopg2\n"
        'psycopg2.connect(host="localhost", port=5432, dbname="cowork_gym", '
        'user="eigent", password="camel")\n',
        id="direct-keywords",
    ),
    pytest.param(
        'import psycopg2\npsycopg2.connect("postgresql+psycopg2://eigent:camel@localhost/cowork_gym")\n',
        id="postgres-dsn",
    ),
    pytest.param(
        "from sqlalchemy import create_engine\n"
        'engine = create_engine("postgresql://eigent:camel@localhost/cowork_gym")\n',
        id="sqlalchemy-dsn",
    ),
]


@pytest.mark.parametrize("source", CONNECTION_CASES)
def test_rewrites_every_supported_connection_form(source: str) -> None:
    rewritten = rewrite_preprocess_db_connections(source)

    ast.parse(rewritten)
    assert unresolved_connection_literals(rewritten) == []
    assert "PGHOST" in rewritten
    assert "PGDATABASE" in rewritten
    assert "PGUSER" in rewritten
    assert "PGPASSWORD" in rewritten
    assert "camel" not in rewritten


def test_env_based_connection_is_left_intact() -> None:
    source = (
        "import os\nimport psycopg2\n"
        'DB = {"host": os.environ["PGHOST"], "dbname": os.environ["PGDATABASE"], '
        '"user": os.environ["PGUSER"], "password": os.environ["PGPASSWORD"]}\n'
        "psycopg2.connect(**DB)\n"
    )

    assert rewrite_preprocess_db_connections(source) == source
    assert unresolved_connection_literals(source) == []


def test_generated_dataset_has_no_unresolved_preprocess_connections() -> None:
    dataset_root = os.environ.get("CANONICAL_DATASET_ROOT")
    if not dataset_root:
        pytest.skip("CANONICAL_DATASET_ROOT is unavailable")
    root = Path(dataset_root)
    tasks = sorted(root.glob("*/environment/docker-compose.yaml"))
    assert len(tasks) == 496

    db_tasks = []
    for compose in tasks:
        task = compose.parent.parent
        source = task / "environment" / "task_payload" / "preprocess" / "main.py"
        if "./pg.env" in compose.read_text(encoding="utf-8"):
            db_tasks.append(source)
    assert len(db_tasks) == 490

    for source_path in db_tasks:
        rewritten = rewrite_preprocess_db_connections(
            source_path.read_text(encoding="utf-8")
        )
        assert unresolved_connection_literals(rewritten) == [], source_path
