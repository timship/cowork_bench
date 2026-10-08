"""Static control-flow contract for all 496 grader verdict lines.

This does not execute the graders and is not a runtime proof of pass rate.
"""

from __future__ import annotations

import ast
import subprocess
import unittest
from pathlib import Path

from harbor_adapter.grader_verdict_contract import (
    PASS_FALSE,
    PASS_TRUE,
    analyze_task,
    iter_task_dirs,
)
from utils.evaluation.evaluator import _content_outcome
from utils.evaluation.verdict import parse_verdict_lines

ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / "tasks" / "finalpool"


def _call_name(node: ast.AST) -> str:
    func = getattr(node, "func", None)
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    return ""


def _added_lines(rel: str) -> tuple[list[str], list[str]]:
    old = subprocess.check_output(
        ["git", "-C", str(ROOT), "show", f"HEAD:{rel}"],
        text=True,
    )
    new = (ROOT / rel).read_text(encoding="utf-8")
    import difflib

    added: list[str] = []
    deleted: list[str] = []
    for line in difflib.ndiff(old.splitlines(), new.splitlines()):
        if line.startswith("- "):
            deleted.append(line[2:])
        elif line.startswith("+ "):
            added.append(line[2:])
    return added, deleted


class GraderVerdictStaticTests(unittest.TestCase):
    def test_all_graders_emit_one_line_per_content_path(self) -> None:
        tasks = iter_task_dirs(TASKS)
        self.assertEqual(len(tasks), 496)
        mechanical = manual = 0
        for task_dir in tasks:
            rel = f"tasks/finalpool/{task_dir.name}/evaluation/main.py"
            added, deleted = _added_lines(rel)
            self.assertEqual(deleted, [], task_dir.name)
            for line in added:
                text = line.strip()
                self.assertTrue(
                    text in {PASS_TRUE, PASS_FALSE}
                    or (
                        text.startswith('print("Pass: True" if (')
                        and text.endswith(') else "Pass: False")')
                    ),
                    f"{task_dir.name}: {text}",
                )
            source = (task_dir / "evaluation" / "main.py").read_text(encoding="utf-8")
            tree = ast.parse(source)
            parents: dict[ast.AST, ast.AST] = {}
            for node in ast.walk(tree):
                for child in ast.iter_child_nodes(node):
                    parents[child] = node
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and _call_name(node) == "print"):
                    continue
                segment = ast.get_source_segment(source, node) or ""
                if "Pass:" not in segment:
                    continue
                current: ast.AST | None = node
                while current in parents:
                    current = parents[current]
                    self.assertNotIsInstance(
                        current, ast.ExceptHandler, f"{task_dir.name}:{node.lineno}"
                    )
            plan = analyze_task(task_dir)
            self.assertFalse(plan.unresolved, task_dir.name)
            lines = source.splitlines()
            content_sites = 0
            for site in plan.sites:
                previous = ""
                index = site.lineno - 2
                while index >= 0 and not lines[index].strip():
                    index -= 1
                if index >= 0:
                    previous = lines[index].strip()
                if site.action == "pass_true":
                    self.assertEqual(previous, PASS_TRUE, f"{task_dir.name}:{site.lineno}")
                    content_sites += 1
                elif site.action == "pass_false":
                    self.assertEqual(previous, PASS_FALSE, f"{task_dir.name}:{site.lineno}")
                    content_sites += 1
                elif site.action == "conditional":
                    self.assertEqual(previous, site.print_expr, f"{task_dir.name}:{site.lineno}")
                    content_sites += 1
                elif site.action == "technical":
                    self.assertFalse(previous.startswith('print("Pass:'), f"{task_dir.name}:{site.lineno}")
                else:
                    self.fail(f"unresolved {task_dir.name}")
            if plan.implicit_pass_lineno is not None:
                self.assertEqual(lines[plan.implicit_pass_lineno].strip(), PASS_TRUE)
                content_sites += 1
            actions = {site.action for site in plan.sites}
            self.assertTrue("pass_true" in actions or "conditional" in actions or plan.implicit_pass_lineno)
            self.assertTrue("pass_false" in actions or "conditional" in actions)
            self.assertEqual(len(added), content_sites, task_dir.name)
            if plan.change_class == "manual":
                manual += 1
            else:
                mechanical += 1
        self.assertEqual(mechanical + manual, 496)
        self.assertEqual(manual, 17)

    def test_wrapper_does_not_synthesize_pass_line(self) -> None:
        source = (ROOT / "scripts" / "run_eval.py").read_text(encoding="utf-8")
        self.assertNotIn("Pass:", source)


class StrictParserTests(unittest.TestCase):
    def test_unique_pass_and_fail(self) -> None:
        self.assertEqual(parse_verdict_lines(["Pass: True"]).value, "True")
        self.assertEqual(parse_verdict_lines(["Pass: False"]).value, "False")
        passed = _content_outcome("checks\nPass: True\n", "", 0)
        failed = _content_outcome("checks\nPass: False\n", "", 1)
        self.assertTrue(passed["pass"])
        self.assertFalse(failed["pass"])

    def test_crash_duplicate_and_conflict_are_technical(self) -> None:
        crash = _content_outcome(
            "Traceback (most recent call last):\nValueError: boom\n",
            "",
            1,
        )
        self.assertIsNone(crash["pass"])
        missing = _content_outcome("Overall: FAIL\n", "", 1)
        self.assertIsNone(missing["pass"])
        duplicate = _content_outcome("Pass: False\nPass: False\n", "", 1)
        self.assertIsNone(duplicate["pass"])
        conflict = _content_outcome("Pass: True\nPass: False\n", "", 0)
        self.assertIsNone(conflict["pass"])
        mismatch = _content_outcome("Pass: True\n", "", 1)
        self.assertIsNone(mismatch["pass"])


if __name__ == "__main__":
    unittest.main()
