# Harbor-canonical MCP packaging

This document describes the packaging that produces self-contained Harbor
tasks any stock agent can run from `task.toml` `[[environment.mcp_servers]]`.

## What stays identical to the upstream benchmark

- 496 task IDs
- `tasks/finalpool/<id>/docs/task.md` (copied as the prefix of `instruction.md`)
- preprocess, fixtures, evaluation, groundtruth
- PostgreSQL image `postgres:2ac6085` (same seed dump)
- MCP stdio implementations under `local_servers/` and `configs/mcp_servers/`

## What canonical changes

| Upstream Cowork | This packaging |
|---|---|
| MCP stdio children inside the runtime | Same stdio binaries behind task-local HTTP gateways |
| MCP list supplied at runtime by the host | MCP URLs declared in `task.toml`; no repo checkout needed on the host |
| Agent-specific instruction tail | Agent-agnostic `instruction.md` |
| PostgreSQL credentials alongside the agent | No `PG*` on `main`; grading runs in a `grader` sidecar on `db_net` |

Compose is seven services: `postgres`, `workspace-prep`, `grader`,
`mcp-gateway-db`, `mcp-gateway-workspace`, `mcp-gateway-public`, `main`.
The agent sees only `mcp-gateway-public`, which reverse-proxies the internal
db/workspace gateways.

## Agent isolation

`main` is on `agent_net` only: it cannot resolve `postgres` or `grader`, carries
no `PG*` environment, and mounts `/grader_out` read-only. The `grader` service is
on `db_net` only and runs `bash /tests/test.sh` via `[[verifier.collect]]`,
publishing `reward.txt` for `main` to read back.

## Credentials

Generated tasks ship a **zero-byte** `environment/pg.env`. Real credentials are
injected outside the dataset — the packaged tree deliberately contains no
secrets. A DB task will not start until `pg.env` is populated: `postgres` exits
with code 1 because its healthcheck runs `pg_isready -U $POSTGRES_USER` with the
variable unset.

Use `environment/generate_pg_env.sh` (reads `POSTGRES_USER` / `POSTGRES_PASSWORD`
/ `POSTGRES_DB` from the environment) or write the file directly.

## Generate / validate

```bash
python3 harbor_adapter/generate_harbor_canonical.py \
  --output datasets/cowork_harbor_canonical_v1 \
  --all

python3 harbor_adapter/validate_harbor_canonical.py \
  datasets/cowork_harbor_canonical_v1

python3 -m unittest discover -s harbor_adapter/tests -t .
python3 -m unittest discover -s harbor_adapter/native_mcp/tests -t harbor_adapter
```

Dataset-level tests are skipped unless `CANONICAL_DATASET_ROOT` points at a
generated tree:

```bash
CANONICAL_DATASET_ROOT=datasets/cowork_harbor_canonical_v1 \
  python3 -m unittest discover -s harbor_adapter/tests -t .
```

Generated 496 trees are gitignored. Do not commit them.

## Oracle

467 of 496 tasks get a `solution/solve.sh`; the remaining 29 have no groundtruth
(25) or an empty one (4). `solve.sh` copies `groundtruth_workspace` and does not
reproduce service side effects, so tasks whose evaluator checks a sent email or a
created calendar event score `reward=0` even with valid documents. Treat the
Oracle as a file-level reference, not a proven upper bound, until the Oracle
contract is settled.
