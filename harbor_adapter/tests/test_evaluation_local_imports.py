"""Regression: evaluator local sibling imports must be packaged and importable under -m."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harbor_adapter"))

from generate_harbor_canonical import convert_one, load_catalog  # noqa: E402
from validate_harbor_canonical import (  # noqa: E402
    _flat_sibling_imports,
    _validate_evaluation_local_imports,
    validate_task,
)

# Tasks known to use flat sibling imports (``from check_local import ...``).
SIBLING_IMPORT_TASKS = (
    "canvas-pdf-grade-gsheet",
    "canvas-exam-calendar-report",
    "moex-sector-rotation-dashboard",
)


def _local_mods(eval_dir: Path) -> set[str]:
    return {p.stem for p in eval_dir.glob("*.py") if p.stem not in {"__init__"}}


class EvaluationLocalImportPackagingTests(unittest.TestCase):
    def test_canvas_pdf_packages_check_local_and_pythonpath(self) -> None:
        catalog = load_catalog()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            convert_one("canvas-pdf-grade-gsheet", out, catalog)
            task = out / "canvas-pdf-grade-gsheet"
            eval_dir = task / "tests" / "evaluation"
            self.assertTrue((eval_dir / "check_local.py").is_file())
            self.assertTrue((eval_dir / "check_gsheet.py").is_file())
            self.assertTrue((eval_dir / "check_email.py").is_file())

            main = eval_dir / "main.py"
            mods = _flat_sibling_imports(main, _local_mods(eval_dir))
            self.assertIn("check_local", mods)

            test_sh = (task / "tests" / "test.sh").read_text(encoding="utf-8")
            self.assertIn("$ROOT/evaluation", test_sh)
            self.assertIn('PYTHONPATH="$ROOT/evaluation:/workspace"', test_sh)
            self.assertIsNone(
                __import__("re").search(
                    r"(?m)^\s*PYTHONPATH=/workspace\s+/opt/venv/bin/python3", test_sh
                )
            )

            errs = _validate_evaluation_local_imports(task)
            self.assertEqual(errs, [])
            # Full static validate without docker compose network calls.
            self.assertEqual(validate_task(task, skip_docker=True), [])

    def test_sibling_import_tasks_all_have_modules_and_pythonpath(self) -> None:
        catalog = load_catalog()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for tid in SIBLING_IMPORT_TASKS:
                with self.subTest(task=tid):
                    convert_one(tid, out, catalog)
                    task = out / tid
                    eval_dir = task / "tests" / "evaluation"
                    main = eval_dir / "main.py"
                    self.assertTrue(main.is_file(), tid)
                    for mod in _flat_sibling_imports(main, _local_mods(eval_dir)):
                        self.assertTrue(
                            (eval_dir / f"{mod}.py").is_file(),
                            f"{tid} missing {mod}.py",
                        )
                    self.assertEqual(_validate_evaluation_local_imports(task), [])

    def test_flat_import_resolves_when_evaluation_on_pythonpath(self) -> None:
        """Simulate run_eval's ``python -m ...evaluation.main`` import rules."""
        catalog = load_catalog()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            convert_one("canvas-pdf-grade-gsheet", out, catalog)
            eval_dir = out / "canvas-pdf-grade-gsheet" / "tests" / "evaluation"
            # Without evaluation on path, check_local is not a top-level module.
            self.assertIsNone(importlib.util.find_spec("check_local"))
            # With packaging PYTHONPATH semantics, the sibling module is discoverable.
            sys.path.insert(0, str(eval_dir))
            try:
                spec2 = importlib.util.find_spec("check_local")
                self.assertIsNotNone(spec2)
                self.assertTrue(str(spec2.origin).endswith("check_local.py"))
            finally:
                sys.path.remove(str(eval_dir))

    def test_validator_flags_missing_pythonpath_fix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "dummy-task"
            eval_dir = task / "tests" / "evaluation"
            eval_dir.mkdir(parents=True)
            (eval_dir / "check_local.py").write_text("def check_local(*a): pass\n", encoding="utf-8")
            (eval_dir / "main.py").write_text(
                "from check_local import check_local\n", encoding="utf-8"
            )
            (task / "tests" / "test.sh").write_text(
                "PYTHONPATH=/workspace /opt/venv/bin/python3 -u /workspace/scripts/run_eval.py\n",
                encoding="utf-8",
            )
            errs = _validate_evaluation_local_imports(task)
            self.assertTrue(any("PYTHONPATH" in e for e in errs), errs)


if __name__ == "__main__":
    unittest.main()
