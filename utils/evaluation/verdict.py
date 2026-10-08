"""Strict parse of one grader verdict line.

A content result is one line in the grader's own stdout:

    Pass:    True
    Pass:    False

The wrapper may echo that stdout between the evaluation markers. It must not
invent a Pass line from the process exit code. Duplicate or conflicting lines
are not a verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STDOUT_MARK = "== Evaluation STDOUT =="
STDERR_MARK = "== Evaluation STDERR =="
VERDICT_RE = re.compile(r"^Pass:[ \t]+(True|False)[ \t]*$")


@dataclass(frozen=True)
class VerdictParse:
    value: str | None
    problem: str | None


def parse_verdict_lines(lines: list[str] | None) -> VerdictParse:
    found: list[str] = []
    for line in lines or []:
        match = VERDICT_RE.match(line)
        if match:
            found.append(match.group(1))
    if not found:
        return VerdictParse(None, "missing")
    if len(found) > 1:
        problem = "duplicate" if len(set(found)) == 1 else "conflict"
        return VerdictParse(None, problem)
    return VerdictParse(found[0], None)


def parse_grader_stdout(stdout: str) -> VerdictParse:
    return parse_verdict_lines((stdout or "").splitlines())


def grader_stdout_lines(wrapped: str) -> list[str] | None:
    """Return the grader stdout region, excluding any wrapper summary."""
    lines = (wrapped or "").splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == STDOUT_MARK and start is None:
            start = index + 1
        elif start is not None and line.strip() == STDERR_MARK:
            return lines[start:index]
    return None


def parse_wrapped_evaluator_stdout(wrapped: str) -> VerdictParse:
    region = grader_stdout_lines(wrapped)
    if region is None:
        return VerdictParse(None, "missing")
    return parse_verdict_lines(region)
