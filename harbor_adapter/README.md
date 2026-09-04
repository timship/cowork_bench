# Cowork Bench → Harbor

This adapter packages the 496 Cowork Bench tasks as self-contained Harbor tasks.
Task texts, fixtures, evaluators, and the PostgreSQL seed are unchanged; MCP is
exposed over HTTP so any stock Harbor agent can run the benchmark from
`task.toml`.

## Images

```bash
docker build -t cowork-pack:latest .
docker build -f harbor_adapter/Dockerfile.main -t cowork-harbor-main:local .
docker build -f harbor_adapter/Dockerfile.postgres -t cowork-postgres:local .
```

## Generate tasks

```bash
python3 harbor_adapter/generate_harbor_canonical.py \
  --output datasets/cowork_harbor_canonical_v1 --all

python3 harbor_adapter/validate_harbor_canonical.py \
  datasets/cowork_harbor_canonical_v1
```

Generated tasks reference `cowork-harbor-main:local` and `cowork-postgres:local`
by default. Point them elsewhere with `--main-image` / `--postgres-image`, and
pin by digest for reproducible runs. Generated trees are gitignored; do not
commit them.

## Credentials

Generated tasks ship a zero-byte `environment/pg.env`, so the packaged dataset
contains no secrets. A DB task will not start until it is populated —
`postgres` exits with code 1 because its healthcheck runs
`pg_isready -U $POSTGRES_USER` with the variable unset.

```bash
POSTGRES_USER=... POSTGRES_PASSWORD=... POSTGRES_DB=... \
  <task>/environment/generate_pg_env.sh
```

## Run

MCP URLs come from each `task.toml`, so no Cowork-specific agent class is
imported. Point Harbor at a generated task and pick any installed agent:

```bash
harbor run -p datasets/cowork_harbor_canonical_v1/<task> \
  -a openhands -m openai/<served-model> --env docker -n 1
```

Pin agent versions to what the image provides — Qwen Code `0.19.9`, OpenHands
`0.62.0` on Python `3.12.11`; see `harbor_adapter/agent_configs/`. Stock installs
resolve `@latest` and drift.

Heavy Docker layers live in the Docker daemon's storage. On shared workers keep
the checkout, generated tasks, and Harbor jobs on a large volume, and point
temporary and cache variables there as well.

## What runs in a trial

Compose is seven services: `postgres`, `workspace-prep`, `grader`,
`mcp-gateway-db`, `mcp-gateway-workspace`, `mcp-gateway-public`, `main`.

- `main` is the agent runtime, on `agent_net` only. It carries no `PG*`
  environment, cannot resolve `postgres` or `grader`, and mounts `/grader_out`
  read-only.
- The agent sees a single MCP facade, `mcp-gateway-public`, which reverse-proxies
  the internal db and workspace gateways running the original stdio servers.
- `postgres` holds an offline copy of the service state the benchmark uses
  (calendar, mail, sheets, forms, and other task backends). MCP tools read and
  mutate that state, and many evaluators check it.
- `grader` runs on `db_net` only and executes the evaluator via
  `[[verifier.collect]]`, publishing `reward.txt` back to `main`.

## Oracle

467 of 496 tasks ship `solution/solve.sh`; the remaining 29 have no groundtruth
(25) or an empty one (4). `solve.sh` copies `groundtruth_workspace` and does not
replay service side effects, so a task whose evaluator checks a sent email or a
created calendar event scores `reward=0` even with correct documents. Treat the
Oracle as a file-level reference, not a proven upper bound.

## Tests

```bash
python3 -m unittest discover -s harbor_adapter/tests -t .
python3 -m unittest discover -s harbor_adapter/native_mcp/tests -t harbor_adapter

# dataset-level checks, skipped without a generated tree
CANONICAL_DATASET_ROOT=datasets/cowork_harbor_canonical_v1 \
  python3 -m unittest discover -s harbor_adapter/tests -t .
```

## Upstream traps fixed by this adapter

- The direct filesystem MCP extends `../../tsconfig.json`, which resolves to
  `/opt/tsconfig.json` in the image. Upstream does not copy that file and npm's
  failure is accidentally swallowed; the base Dockerfile now supplies it.
- Upstream calls `playwright install` before a Python Playwright CLI exists, and
  ignores that failure. The Harbor image installs Chromium through the vendored
  Node Playwright MCP and adds `--no-sandbox` for container execution.
- A bare Compose validation reports that `main` has no image. This is expected:
  Harbor injects the `task.toml` image through its generated Compose overlay.
- LiteLLM tries to download a current pricing table at startup. With restricted
  egress it times out and uses its bundled fallback; this warning does not fail
  a trial.
