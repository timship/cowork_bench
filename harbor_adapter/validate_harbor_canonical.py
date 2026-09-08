#!/usr/bin/env python3
"""Static validation for Harbor-canonical generated dataset."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

AGENT_LEAKS = re.compile(
    r"(?i)\b(strands|universal\s+agent|openhands|qwen[\s_-]?code|turn_finish|TURN_FINISHED|ask_user)\b"
)
SECRET_LEAKS = re.compile(
    r"(?i)(PGPASSWORD\s*[:=]\s*[\"']?[^\s\"']+|POSTGRES_PASSWORD\s*[:=]\s*[\"']?[^\s\"']+"
    r"|password[\"']?\s*:\s*[\"'][^\"']+[\"'])"
)
# Known historical demo password must never appear in generated packaging.
KNOWN_BAD = re.compile(r"(?i)\bcamel\b")

EXPECTED_NO_GT = {
    "canvas-enrollment-gsheet",
    "canvas-module-completion-teamly-gcal",
    "canvas-quiz-remediation-forms-gcal",
    "ch-support-csat-forms-gsheet-email",
    "ecommerce-commodity-impact",
    "insales-customer-order-gsheet-email",
    "insales-customer-survey-form",
    "insales-vip-customer-gsheet-gcal-email",
    "kulinar-diet-forms-teamly-excel",
    "kulinar-event-catering-excel-word",
    "kulinar-event-menu-planner",
    "kulinar-nutrition-benchmark",
    "kulinar-scholarly-health-study",
    "kulinar-weekly-gsheet-gcal",
    "kulinar-weekly-plan-gsheet-gcal",
    "market-competitive-intelligence-report",
    "moex-earnings-calendar-alert",
    "moex-market-news-digest-teamly-forms-email",
    "pw-scholarly-trend-analysis-excel-ppt",
    "rzd-kulinar-novgorod-trip-gcal-word",
    "sf-hr-attrition-gcal",
    "sf-hr-turnover-teamly",
    "sf-sales-discount-gsheet-email",
    "sf-support-agent-performance-gsheet-gcal",
    "yf-gold-performance-gsheet-notion",
}

# Usable Oracle payload missing (dir only/.gitkeep) — not the GitHub-25 list.
EXPECTED_EMPTY_GT = {
    "rzd-canvas-fieldtrip-novgorod-gcal-word-email",
    "rzd-hr1c-training-trip-kazan-excel-email-gcal",
    "rzd-kulinar-team-trip-spb-catering-excel-gcal",
    "scholarly-fetch-gsheet-citation",
}

EXPECTED_NO_ORACLE = EXPECTED_NO_GT | EXPECTED_EMPTY_GT


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _validate_pg_env(compose: str, env_path: Path) -> list[str]:
    """Validate the packaged env_file without creating or deleting files."""
    references_pg_env = bool(re.search(r"(?m)^\s*-\s+\./pg\.env\s*$", compose))
    if references_pg_env and not env_path.exists():
        return ["compose references ./pg.env but environment/pg.env is missing"]
    if references_pg_env and env_path.stat().st_size != 0:
        return ["environment/pg.env must be zero-byte"]
    if not references_pg_env and env_path.exists():
        return ["environment/pg.env exists without a compose reference"]
    return []


def validate_task(task_dir: Path, *, skip_docker: bool = False) -> list[str]:
    errs: list[str] = []
    tid = task_dir.name
    required = [
        "instruction.md",
        "task.toml",
        "environment/docker-compose.yaml",
        "environment/mcp_manifest_public.json",
        "environment/mcp_runtime/mcp_gateway.py",
        "environment/prep/prepare_workspace.py",
        "environment/prep/task_config_stub.py",
        "tests/test.sh",
        "tests/verifier/workspace_lifecycle.py",
        "tests/verifier/task_config_stub.py",
        "metadata.json",
    ]
    for rel in required:
        if not (task_dir / rel).exists():
            errs.append(f"missing {rel}")

    instr = _read(task_dir / "instruction.md") if (task_dir / "instruction.md").exists() else ""
    if AGENT_LEAKS.search(instr):
        errs.append("agent-specific leak in instruction.md")

    toml = _read(task_dir / "task.toml") if (task_dir / "task.toml").exists() else ""
    if "mcp-gateway-public" not in toml:
        errs.append("task.toml missing mcp-gateway-public")
    if re.search(r"mcp-gateway-db|mcp-gateway-workspace", toml):
        errs.append("task.toml exposes internal gateway")
    if re.search(r"(?i)\bPG(PASSWORD|USER|HOST|DATABASE)\b", toml):
        errs.append("task.toml contains PG env")
    if "ask_user" in toml or AGENT_LEAKS.search(toml):
        errs.append("task.toml agent-specific leak")
    if 'transport = "streamable-http"' not in toml:
        errs.append("task.toml missing streamable-http")

    compose = (
        _read(task_dir / "environment" / "docker-compose.yaml")
        if (task_dir / "environment" / "docker-compose.yaml").exists()
        else ""
    )
    if "agent_net" not in compose or "mcp_workspace_net" not in compose:
        errs.append("compose missing required networks")
    if "mcp-gateway-public" not in compose:
        errs.append("compose missing mcp-gateway-public")
    # main must not get PG env
    main_block = re.search(r"(?ms)^  main:.*?(?=^  [a-z]|\Z)", compose)
    if main_block:
        mb = main_block.group(0)
        if re.search(r"(?i)PGPASSWORD|POSTGRES_PASSWORD|PGUSER:|PGHOST:", mb):
            errs.append("main service has PG env")
        if "env_file:" in mb and "pg.env" in mb:
            errs.append("main mounts pg.env")
    if "strands" in compose.lower():
        errs.append("compose mentions strands")
    # workspace gateway must not be on db_net
    ws_block = re.search(
        r"(?ms)^  mcp-gateway-workspace:.*?(?=^  [a-z0-9-]+:|\Z)", compose
    )
    if ws_block and re.search(r"(?m)^\s+- db_net\s*$", ws_block.group(0)):
        errs.append("mcp-gateway-workspace attached to db_net")

    # secrets / camel in packaging (allow prepare rewrite of source preprocess copies under task_payload)
    for path in task_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix in {".png", ".jpg", ".pdf", ".tar", ".gz", ".zip", ".pyc"}:
            continue
        rel = path.relative_to(task_dir).as_posix()
        if rel.startswith("environment/task_payload/"):
            continue  # source copies may still contain historical literals until prep rewrite
        if rel.startswith("tests/evaluation/") or rel.startswith("tests/groundtruth"):
            continue
        if rel.startswith("solution/groundtruth_workspace/"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if KNOWN_BAD.search(text) and "prepare_workspace.py" not in rel:
            # prepare_workspace may contain replacement needles for source preprocess
            if "password" in text.lower() or "POSTGRES" in text:
                errs.append(f"known password literal in {rel}")
        if AGENT_LEAKS.search(text) and rel.endswith((".md", ".toml", ".yaml", ".yml", ".json", ".sh")):
            if "metadata.json" in rel:
                continue
            errs.append(f"agent leak in {rel}")

    meta = json.loads(_read(task_dir / "metadata.json")) if (task_dir / "metadata.json").exists() else {}
    has_oracle = (task_dir / "solution" / "solve.sh").is_file()
    no_gt_marker = (task_dir / "solution" / "NO_GROUNDTRUTH").is_file()
    if tid in EXPECTED_NO_ORACLE:
        if has_oracle:
            errs.append("unexpected Oracle for no-GT/empty-GT task")
        if not no_gt_marker:
            errs.append("missing NO_GROUNDTRUTH marker")
    else:
        if not has_oracle:
            errs.append("missing Oracle solve.sh for GT task")
        if no_gt_marker:
            errs.append("NO_GROUNDTRUTH marker on GT task")

    # Docker Compose requires every referenced env_file to exist. The
    # packaged pg.env is intentionally zero-byte; runtime credentials are
    # injected outside the dataset.
    compose_path = task_dir / "environment" / "docker-compose.yaml"
    if compose_path.exists():
        env_dir = task_dir / "environment"
        pg_env = env_dir / "pg.env"
        errs.extend(_validate_pg_env(compose, pg_env))
        if not skip_docker:
            try:
                subprocess.run(
                    ["docker", "compose", "-f", str(compose_path), "config", "-q"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except FileNotFoundError:
                errs.append("docker not available for compose config")
            except subprocess.CalledProcessError as exc:
                errs.append(
                    f"compose config failed: {(exc.stderr or exc.stdout or '')[:300]}"
                )
            except Exception as exc:
                errs.append(f"compose config error: {exc}")

    _ = meta

    # Grader sidecar isolation for DB tasks.
    compose = task_dir / "environment" / "docker-compose.yaml"
    if compose.is_file():
        cy = compose.read_text(encoding="utf-8", errors="ignore")
        has_postgres = "\n  postgres:" in cy or "\n  postgres:\n" in cy
        if has_postgres:
            if "\n  grader:" not in cy and "\ngrader:" not in cy:
                errs.append("DB task missing grader sidecar")
            if "grader_out:/grader_out:ro" not in cy:
                errs.append("main must mount grader_out read-only")
            toml = (task_dir / "task.toml").read_text(encoding="utf-8", errors="ignore")
            if 'service = "grader"' not in toml:
                errs.append("task.toml missing verifier.collect for grader")
            # Finalize must run on grader (where /tests is mounted), not main.
            if (
                "workspace_lifecycle.py finalize" in toml
                and 'service = "main"' in toml
            ):
                # Split collects: reject finalize bound to main when postgres present.
                _assert_finalize_not_on_main(toml, errs)
    return errs


def _assert_finalize_not_on_main(toml: str, errs: list[str]) -> None:
    """Ensure finalize collect is not attached to main for DB tasks."""
    blocks = toml.split("[[verifier.collect]]")
    for block in blocks[1:]:
        if "workspace_lifecycle.py finalize" not in block:
            continue
        if 'service = "main"' in block:
            errs.append("finalize collect must use service=grader on DB tasks")
        if 'service = "grader"' not in block:
            errs.append("finalize collect missing service=grader on DB tasks")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="Generated dataset root (default: cwd)",
    )
    parser.add_argument(
        "--skip-docker",
        action="store_true",
        help="Skip `docker compose config` checks (static-only validation).",
    )
    args = parser.parse_args()
    root = Path(args.root)
    tasks = sorted(
        p for p in root.iterdir() if p.is_dir() and (p / "task.toml").exists()
    )
    all_errs: dict[str, list[str]] = {}
    for t in tasks:
        e = validate_task(t, skip_docker=args.skip_docker)
        if e:
            all_errs[t.name] = e
            print(f"FAIL {t.name}: {e}")
        else:
            print(f"OK {t.name}")

    with_oracle = sum(1 for t in tasks if (t / "solution" / "solve.sh").is_file())
    no_gt = sorted(t.name for t in tasks if (t / "solution" / "NO_GROUNDTRUTH").is_file())
    report = {
        "n_tasks": len(tasks),
        "n_ok": len(tasks) - len(all_errs),
        "n_fail": len(all_errs),
        "with_oracle": with_oracle,
        "no_groundtruth": no_gt,
        "expected_no_gt": sorted(EXPECTED_NO_GT),
        "expected_empty_gt": sorted(EXPECTED_EMPTY_GT),
        "no_gt_match_expected": set(no_gt) == EXPECTED_NO_ORACLE,
        "skip_docker": bool(args.skip_docker),
        "failures": all_errs,
    }
    out = root / "STATIC_VALIDATION.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in report if k != "failures"}, indent=2, ensure_ascii=False))
    return 1 if all_errs else 0


if __name__ == "__main__":
    raise SystemExit(main())
