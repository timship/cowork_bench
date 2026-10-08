"""Control-flow contract for the grader's own Pass line.

The wrapper does not invent a verdict. Each grader prints ``Pass: True`` or
``Pass: False`` only after its existing content decision. Exception handlers,
missing inputs, missing groundtruth, and exits before that decision print nothing.

This module classifies ``sys.exit`` sites. It does not change thresholds,
check order, or calculations.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

PASS_TRUE = 'print("Pass: True")'
PASS_FALSE = 'print("Pass: False")'

# These tests were read one by one. They leave the grader before a content
# decision: missing workspace, unreadable source, empty required seed data,
# or a workbook/groundtruth that could not be opened.
MANUAL_TECHNICAL_TESTS = frozenset(
    {
        "not workspace",
        "not args.agent_workspace",
        "expected is None",
        "not responses",
        "missing",
        "g_rows is None",
        "len(prices) != 5",
        "fatal",
        "gt is None",
        "not db_regions",
    }
)

# These tests are content decisions whose names do not match the uniform
# critical/accuracy pattern. The branch already exits 1 after the checks.
MANUAL_CONTENT_TESTS = frozenset(
    {
        "not pdf_ok",
        "not spreadsheets",
        "failed",
    }
)

CONTENT_NAME = re.compile(
    r"(critical|crit_|crit\b|CRIT|accuracy|fail_count|FAIL_COUNT|"
    r"overall|success|all_errors|file_errors|all_ok|pct\b|passed|threshold)",
    re.I,
)


@dataclass(frozen=True)
class ExitSite:
    lineno: int
    action: str  # pass_true, pass_false, conditional, technical
    source: str
    manual: bool
    exit_arg: str
    print_expr: str = ""


@dataclass
class TaskPlan:
    task: str
    path: Path
    sites: list[ExitSite] = field(default_factory=list)
    implicit_pass_lineno: int | None = None
    implicit_pass_manual: bool = False

    @property
    def change_class(self) -> str:
        manual = self.implicit_pass_manual or any(site.manual for site in self.sites)
        return "manual" if manual else "mechanical"

    @property
    def unresolved(self) -> bool:
        return any(site.action == "unresolved" for site in self.sites)


def _call_name(node: ast.AST) -> str:
    func = getattr(node, "func", None)
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    return ""


def _is_exit(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _call_name(node) in {"sys.exit", "exit"}


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    mapping: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            mapping[child] = node
    return mapping


def _ancestors(parents: dict[ast.AST, ast.AST], node: ast.AST) -> list[ast.AST]:
    found: list[ast.AST] = []
    current: ast.AST | None = node
    while current in parents:
        current = parents[current]
        found.append(current)
    return found


def _is_main_guard(node: ast.If) -> bool:
    return "__name__" in ast.unparse(node.test)


def _nearest_if(ancestors: list[ast.AST]) -> ast.If | None:
    for node in ancestors:
        if isinstance(node, ast.If) and not _is_main_guard(node):
            return node
    return None


def _enclosing_function(ancestors: list[ast.AST]) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ancestors:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def _in_except(ancestors: list[ast.AST]) -> bool:
    return any(isinstance(node, ast.ExceptHandler) for node in ancestors)


def _contains(statements: list[ast.stmt], node: ast.AST) -> bool:
    return any(
        getattr(statement, "lineno", 10**9) <= node.lineno <= getattr(statement, "end_lineno", -1)
        for statement in statements
    )


def _is_path_check(test: ast.AST) -> bool:
    for node in ast.walk(test):
        if isinstance(node, ast.Attribute) and node.attr in {"exists", "isfile", "isdir"}:
            return True
    return False


def _is_zero_checks(test: ast.AST) -> bool:
    text = ast.unparse(test)
    if text.strip() in {"total == 0", "total_checks == 0", "not results", "not checks"}:
        return True
    return bool(
        re.search(
            r"(total(_checks)?|PASS_COUNT \+ FAIL_COUNT|len\((results|checks)\))\s*==\s*0",
            text,
        )
    )


def _block_and_statement(
    parents: dict[ast.AST, ast.AST], node: ast.AST
) -> tuple[list[ast.stmt] | None, ast.stmt | None]:
    current: ast.AST | None = node
    while current in parents:
        parent = parents[current]
        for field in ("body", "orelse", "finalbody"):
            statements = getattr(parent, field, None)
            if not isinstance(statements, list):
                continue
            for statement in statements:
                if _contains([statement], node):
                    return statements, statement
        current = parent
    return None, None


def _preceding_exit_zero(statements: list[ast.stmt], statement: ast.stmt) -> bool:
    index = statements.index(statement)
    for earlier in statements[:index]:
        if not isinstance(earlier, ast.If):
            continue
        for node in ast.walk(earlier):
            if _is_exit(node) and node.args and ast.unparse(node.args[0]).replace(" ", "") == "0":
                return True
    return False


def _conditional_print(text: str, node: ast.IfExp) -> str:
    segment = ast.get_source_segment(text, node.test)
    if not segment:
        segment = ast.unparse(node.test)
    return f'print("Pass: True" if ({segment}) else "Pass: False")'


def analyze_source(task: str, path: Path, source: str) -> TaskPlan:
    tree = ast.parse(source)
    parents = _parents(tree)
    plan = TaskPlan(task=task, path=path)
    for node in ast.walk(tree):
        if not (_is_exit(node) and node.args):
            continue
        ancestors = _ancestors(parents, node)
        argument = ast.unparse(node.args[0])
        compact = argument.replace(" ", "")
        if _in_except(ancestors):
            plan.sites.append(
                ExitSite(node.lineno, "technical", "except_handler", False, argument)
            )
            continue
        if isinstance(node.args[0], ast.IfExp):
            body = ast.unparse(node.args[0].body).replace(" ", "")
            orelse = ast.unparse(node.args[0].orelse).replace(" ", "")
            if body == "0" and orelse == "1":
                plan.sites.append(
                    ExitSite(
                        node.lineno,
                        "conditional",
                        ast.unparse(node.args[0].test),
                        False,
                        argument,
                        _conditional_print(source, node.args[0]),
                    )
                )
            else:
                plan.sites.append(
                    ExitSite(node.lineno, "unresolved", argument, True, argument)
                )
            continue
        if compact == "0":
            plan.sites.append(ExitSite(node.lineno, "pass_true", "exit_0_branch", False, argument))
            continue
        if compact != "1":
            plan.sites.append(ExitSite(node.lineno, "unresolved", argument, True, argument))
            continue
        function = _enclosing_function(ancestors)
        if function is not None and function.name == "fail_critical":
            plan.sites.append(
                ExitSite(
                    node.lineno,
                    "pass_false",
                    "fail_critical_content_branch",
                    True,
                    argument,
                )
            )
            continue
        nearest = _nearest_if(ancestors)
        if nearest is None:
            statements, statement = _block_and_statement(parents, node)
            if statements is not None and statement is not None and _preceding_exit_zero(statements, statement):
                plan.sites.append(
                    ExitSite(node.lineno, "pass_false", "fallthrough_after_pass_exit", False, argument)
                )
            else:
                plan.sites.append(
                    ExitSite(node.lineno, "unresolved", "exit_1_without_branch", True, argument)
                )
            continue
        test_text = ast.unparse(nearest.test)
        if test_text in MANUAL_TECHNICAL_TESTS or _is_path_check(nearest.test) or _is_zero_checks(nearest.test):
            manual = test_text in MANUAL_TECHNICAL_TESTS
            plan.sites.append(
                ExitSite(node.lineno, "technical", test_text, manual, argument)
            )
            continue
        if test_text in MANUAL_CONTENT_TESTS or CONTENT_NAME.search(test_text):
            manual = test_text in MANUAL_CONTENT_TESTS
            plan.sites.append(
                ExitSite(node.lineno, "pass_false", test_text, manual, argument)
            )
            continue
        plan.sites.append(ExitSite(node.lineno, "unresolved", test_text, True, argument))

    # Two graders report success by falling off the end after a pass message.
    if task in {"moex-sector-rotation-dashboard", "ppt-clickhouse-executive"}:
        for lineno, line in enumerate(source.splitlines(), 1):
            if "Pass all tests!" in line:
                plan.implicit_pass_lineno = lineno
                plan.implicit_pass_manual = True
                break
        if plan.implicit_pass_lineno is None:
            plan.sites.append(ExitSite(0, "unresolved", "missing_implicit_pass", True, ""))
    return plan


def analyze_task(task_dir: Path) -> TaskPlan:
    path = task_dir / "evaluation" / "main.py"
    return analyze_source(task_dir.name, path, path.read_text(encoding="utf-8"))


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" "))]


def apply_plan(plan: TaskPlan, source: str) -> str:
    """Insert verdict prints. Existing lines are not rewritten."""
    if plan.unresolved:
        raise RuntimeError(f"{plan.task} has an unresolved exit")
    lines = source.splitlines(keepends=True)
    inserts: dict[int, list[str]] = {}

    def add(lineno: int, statement: str) -> None:
        if lineno < 1:
            raise RuntimeError(f"{plan.task} bad insert line")
        pad = _indent(lines[lineno - 1])
        inserts.setdefault(lineno, []).append(f"{pad}{statement}\n")

    for site in plan.sites:
        if site.action == "pass_true":
            add(site.lineno, PASS_TRUE)
        elif site.action == "pass_false":
            add(site.lineno, PASS_FALSE)
        elif site.action == "conditional":
            add(site.lineno, site.print_expr)
    if plan.implicit_pass_lineno is not None:
        # After the existing success message, still on the success path.
        pad = _indent(lines[plan.implicit_pass_lineno - 1])
        inserts.setdefault(plan.implicit_pass_lineno + 1, []).insert(0, f"{pad}{PASS_TRUE}\n")

    for lineno in sorted(inserts, reverse=True):
        for extra in reversed(inserts[lineno]):
            lines.insert(lineno - 1, extra)
    return "".join(lines)


def iter_task_dirs(root: Path) -> list[Path]:
    return sorted(
        path for path in root.iterdir() if (path / "evaluation" / "main.py").is_file()
    )
