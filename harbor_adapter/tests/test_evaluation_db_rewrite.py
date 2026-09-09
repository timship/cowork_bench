"""Regression: evaluation packaging must not hardcode Postgres user eigent."""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harbor_adapter"))

from generate_harbor_canonical import (  # noqa: E402
    _rewrite_evaluation_db_secrets,
    convert_one,
    load_catalog,
)


ARXIV_DB_CONFIG_SNIPPET = '''\
DB_CONFIG = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": 5432,
    "dbname": "cowork_gym",
    "user": "eigent",
    "password": "camel",
}
'''


class EvaluationDbRewriteTests(unittest.TestCase):
    def test_arxiv_like_db_config_uses_required_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            eval_dir = Path(tmp) / "evaluation"
            eval_dir.mkdir()
            path = eval_dir / "main.py"
            path.write_text(
                "import os\n"
                + ARXIV_DB_CONFIG_SNIPPET
                + "\ndef check():\n    import psycopg2\n    psycopg2.connect(**DB_CONFIG)\n",
                encoding="utf-8",
            )
            _rewrite_evaluation_db_secrets(eval_dir)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("eigent", text)
            self.assertNotIn("camel", text)
            self.assertNotIn("localhost", text)
            self.assertIn('os.environ["PGUSER"]', text)
            self.assertIn('os.environ["PGPASSWORD"]', text)
            self.assertIn('os.environ["PGHOST"]', text)
            self.assertIn('os.environ["PGDATABASE"]', text)
            self.assertIn('int(os.environ.get("PGPORT", "5432"))', text)
            # No soft default that hides a missing PGUSER.
            self.assertIsNone(
                re.search(r'os\.environ\.get\(\s*["\']PGUSER["\']', text)
            )

    def test_generated_arxiv_research_evaluation_has_no_eigent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            catalog = load_catalog()
            convert_one("arxiv-research-workflow-pipeline", out, catalog)
            main = (
                out
                / "arxiv-research-workflow-pipeline"
                / "tests"
                / "evaluation"
                / "main.py"
            )
            self.assertTrue(main.is_file())
            text = main.read_text(encoding="utf-8")
            self.assertNotIn('"user": "eigent"', text)
            self.assertNotIn("'user': 'eigent'", text)
            self.assertNotIn("eigent", text)
            self.assertIn('os.environ["PGUSER"]', text)
            self.assertIn('os.environ["PGPASSWORD"]', text)
            self.assertIn('os.environ["PGHOST"]', text)
            self.assertIn('os.environ["PGDATABASE"]', text)


if __name__ == "__main__":
    unittest.main()
