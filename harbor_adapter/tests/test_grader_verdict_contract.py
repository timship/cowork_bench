"""Static control-flow contract for all 496 grader verdict lines.

This does not execute the graders and is not a runtime proof of pass rate.
"""

from __future__ import annotations

import ast
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


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    mapping: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            mapping[child] = node
    return mapping


def _pass_prints(source: str, tree: ast.AST) -> list[tuple[int, str, bool]]:
    """Return Pass prints as (lineno, statement, inside_except)."""
    parents = _parents(tree)
    found: list[tuple[int, str, bool]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _call_name(node) == "print"):
            continue
        segment = (ast.get_source_segment(source, node) or "").strip()
        if "Pass:" not in segment:
            continue
        inside_except = False
        current: ast.AST | None = node
        while current in parents:
            current = parents[current]
            if isinstance(current, ast.ExceptHandler):
                inside_except = True
        found.append((node.lineno, segment, inside_except))
    return found


def _previous_statement(lines: list[str], lineno: int) -> tuple[int, str]:
    index = lineno - 2
    while index >= 0 and not lines[index].strip():
        index -= 1
    if index < 0:
        return 0, ""
    return index + 1, lines[index].strip()


class GraderVerdictStaticTests(unittest.TestCase):
    def test_all_graders_emit_one_line_per_content_path(self) -> None:
        tasks = iter_task_dirs(TASKS)
        self.assertEqual(len(tasks), 496)
        mechanical = manual = 0
        for task_dir in tasks:
            source = (task_dir / "evaluation" / "main.py").read_text(encoding="utf-8")
            tree = ast.parse(source)
            plan = analyze_task(task_dir)
            self.assertFalse(plan.unresolved, task_dir.name)
            lines = source.splitlines()
            prints = _pass_prints(source, tree)
            self.assertTrue(all(not inside for _, _, inside in prints), task_dir.name)
            expected: dict[int, str] = {}
            content_sites = 0
            for site in plan.sites:
                line_no, previous = _previous_statement(lines, site.lineno)
                if site.action == "pass_true":
                    self.assertEqual(previous, PASS_TRUE, f"{task_dir.name}:{site.lineno}")
                    expected[line_no] = PASS_TRUE
                    content_sites += 1
                elif site.action == "pass_false":
                    self.assertEqual(previous, PASS_FALSE, f"{task_dir.name}:{site.lineno}")
                    expected[line_no] = PASS_FALSE
                    content_sites += 1
                elif site.action == "conditional":
                    self.assertEqual(previous, site.print_expr, f"{task_dir.name}:{site.lineno}")
                    expected[line_no] = site.print_expr
                    content_sites += 1
                elif site.action == "technical":
                    self.assertFalse(
                        previous.startswith('print("Pass:'),
                        f"{task_dir.name}:{site.lineno}",
                    )
                else:
                    self.fail(f"unresolved {task_dir.name}")
            if plan.implicit_pass_lineno is not None:
                verdict_line = plan.implicit_pass_lineno + 1
                self.assertEqual(lines[plan.implicit_pass_lineno].strip(), PASS_TRUE)
                expected[verdict_line] = PASS_TRUE
                content_sites += 1
            observed = {line_no: text for line_no, text, _ in prints}
            self.assertEqual(observed, expected, task_dir.name)
            self.assertEqual(len(observed), content_sites, task_dir.name)
            actions = {site.action for site in plan.sites}
            self.assertTrue(
                "pass_true" in actions or "conditional" in actions or plan.implicit_pass_lineno,
                task_dir.name,
            )
            self.assertTrue("pass_false" in actions or "conditional" in actions, task_dir.name)
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
