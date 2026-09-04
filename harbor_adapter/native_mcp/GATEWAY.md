# Task-local MCP HTTP gateway

Each Harbor trial runs **four** Compose services: `main`, `workspace-prep`,
`postgres`, `mcp-gateway`. The gateway is not cluster-wide. PostgreSQL schema
and the shared workspace are per-trial.

## Behaviour

- Reads `mcp_manifest.json` (only `needed_mcp_servers` for that task).
- Starts each Cowork stdio server as a child process.
- Serves Streamable HTTP at `http://mcp-gateway:8000/mcp/<name>`.
- `/ready` is 200 iff every listed server initialized; otherwise 503.
- `/status` reports per-server ready/error/pid.
- Stderr and exit codes go to `/logs/mcp-gateway/<name>.stderr.log`.
- One dead child does not stop siblings; that path returns 503.

## Mounts

- `./native_mcp` → `/opt/harbor_native:ro`
- `./mcp_manifest.json` → `/opt/harbor_native_manifest/mcp_manifest.json:ro`

Do not nest the manifest file inside `/opt/harbor_native`. runc cannot create
that nested mountpoint on a read-only bind.

## Stock agents

OpenHands reads `shttp_servers` from `task.toml`. Qwen Code reads `httpUrl`.
No Cowork-specific agent class is imported on this path.
