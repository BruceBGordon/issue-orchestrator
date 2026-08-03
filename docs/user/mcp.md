# MCP Server (`issue-orchestrator-mcp`)

Issue Orchestrator ships a [Model Context Protocol](https://modelcontextprotocol.io)
server so MCP clients — the VS Code extension, Claude Code, Claude Desktop, or
anything else that speaks MCP — can observe and control a running orchestrator.

> **Stability: experimental.** The project is in `0.x`. Tool names, arguments,
> and return shapes may change in any release until `1.0`. Pin a version if you
> build automation on top of these tools, and re-check this page after upgrades.

## What It Is

`issue-orchestrator-mcp` is a thin MCP front end over the orchestrator's
Control API. It does two kinds of work:

- **Supervisor-level operations** it performs itself in-process: reading
  supervisor status, launching or stopping an orchestrator, running doctor,
  detecting repos on the machine.
- **Engine-level operations** it forwards over HTTP to the running
  orchestrator's Control API (`pause`, `resume`, session inspection, and so on).

The transport is **stdio only**. The client launches the server as a
subprocess and talks to it over stdin/stdout; there is no network listener and
no HTTP transport. Logs go to stderr, so they never corrupt the protocol
stream.

Each server process is bound to **one repository** (`--repo-root`) and one
config file. The `orchestrator.repos*` and `orchestrator.state` tools are the
exception: they report on the repositories in the machine-wide registry, not
just the bound one. See
[Scope of the repository tools](#scope-of-the-repository-tools) for exactly
what that covers.

## Prerequisites

1. Install the Python package so the entrypoint is on your `PATH`:

   ```bash
   issue-orchestrator-mcp --help
   ```

2. Create a repo config at `.issue-orchestrator/config/default.yaml` in the
   repository you want to control. Start from
   [`examples/config.example.yaml`](../../examples/config.example.yaml) if
   you're new. **The config file must exist** — the server exits immediately
   with `Config file not found: <path>` otherwise.

See [Installation](installation.md) and [Quickstart](quickstart.md) for the
full setup.

## Running It

```bash
issue-orchestrator-mcp --repo-root /path/to/repo --auto-start
```

Run this way, the process sits waiting for MCP traffic on stdin — which is
mostly useful for a smoke test that the entrypoint resolves. In normal use your
MCP **client** launches it; you don't start it by hand.

### Command-line flags

| Flag | Default | Purpose |
|------|---------|---------|
| `--repo-root PATH` | current working directory | Repository this server controls. |
| `--config PATH` | *(unset)* | Explicit path to a config file. When given without `--repo-root`, the repo root is read from the config's `repo_root`. |
| `--config-name NAME` | `default.yaml` | Config filename to resolve under `<repo-root>/.issue-orchestrator/config/`. Ignored when `--config` is given. |
| `--instance-id ID` | *(unset)* | Selects one instance when a repo runs multiple orchestrators. |
| `--host HOST` | `127.0.0.1` | Host used for outgoing Control API calls. |
| `--auto-start` | off | Start the orchestrator on demand if it isn't running. Without it, engine-level tools fail with `Orchestrator not running`. |
| `--api-port PORT` | *(unset)* | Talk to a fixed Control API port, bypassing supervisor detection. Useful when the supervisor state is unavailable or under test. |

## Connecting a Client

The server is a standard stdio MCP server, so any client config that can spawn
a command works. Add an entry like this to your client's `mcpServers` map:

```json
{
  "mcpServers": {
    "issue-orchestrator": {
      "command": "issue-orchestrator-mcp",
      "args": [
        "--repo-root", "/path/to/your/repo",
        "--auto-start"
      ]
    }
  }
}
```

Where that JSON lives depends on the client — for example a project-local
`.mcp.json` or `~/.claude.json` for Claude Code, or
`claude_desktop_config.json` for Claude Desktop. Claude Code can also add it
for you:

```bash
claude mcp add issue-orchestrator -- issue-orchestrator-mcp \
  --repo-root /path/to/your/repo --auto-start
```

Two things worth knowing when the connection doesn't come up:

- **Use an absolute command path if the entrypoint lives in a virtualenv.**
  GUI-launched clients often don't inherit your shell `PATH`. Point `command`
  at `/path/to/.venv/bin/issue-orchestrator-mcp` instead of the bare name.
- **The subprocess needs a usable environment.** Clients that pass a minimal
  env must still forward `PATH` and `HOME`. The VS Code extension forwards an
  explicit allowlist rather than the whole environment, on purpose — see
  [Security and Operational Notes](#security-and-operational-notes).

### Authentication to the Control API

Every Control API route requires a bearer token, so engine-level tools need one
too. The server resolves it in this order:

1. `ISSUE_ORCHESTRATOR_API_TOKEN` from the environment.
2. `~/.issue-orchestrator/api-token`, if the file already exists.

The token file is created by orchestrator startup, not by the MCP server. When
you run the client as the same user that runs the orchestrator, the file path
usually resolves with no extra configuration. If your client scrubs the
environment and the file isn't there yet, start the orchestrator once (or set
`ISSUE_ORCHESTRATOR_API_TOKEN` explicitly in the client's `env` block).

### VS Code

The VS Code extension manages all of this for you — it spawns the server,
forwards the token, and drives the tools behind ordinary commands and views.
See [VS Code Integration](vscode.md).

## Tool Reference

All tools live under the `orchestrator.*` namespace and return a JSON object.

**Error shape.** Tools never raise across the protocol boundary. On failure
they return:

```json
{ "error": { "message": "Orchestrator not running", "type": "RuntimeError" } }
```

Two tools deviate from that shape:

- `orchestrator.start` adds a `ui_hint` object alongside `error` when it
  fails — `{"kind": "doctor", "url": "…"}`, where `url` is present only once a
  port is known. It points at the doctor report so a client can surface the
  reason the launch failed.
- `orchestrator.repos.start` reports its failures as a plain string in `error`
  (`{"error": "Repository path … is not a git checkout"}`) rather than an
  object. That covers both path validation and a failed launch. Every other
  tool uses the `{message, type}` object above — including
  `orchestrator.repos.stop`, which has no plain-string path at all and reports
  a refused stop through its `status` field instead.

### Lifecycle

| Tool | Arguments | Returns |
|------|-----------|---------|
| `orchestrator.status` | *(none)* | `{"supervisor": {"state", "pid", "port", "started_at", "recovered", "error"}}`. When the engine is running, also `"status"` (paused flag, active sessions, queue, pending reviews, tick info) and `"info"` (version, repo, commit, session counts, startup status). |
| `orchestrator.start` | *(none)* | If already running: `{"supervisor": {…}}`. Otherwise `{"launch": {"doctor", "launched", "status", "error"?, "supervisor"?}}` where `status` is `ok`, `doctor_error`, `doctor_warning`, or `launch_error`. |
| `orchestrator.stop` | `force: bool = false` | `{"stopped": bool}`. Stops the supervisor-managed orchestrator for this server's `--repo-root`. |
| `orchestrator.pause` | *(none)* | `{"status": "paused"}`. No new sessions launch; running sessions continue. |
| `orchestrator.resume` | *(none)* | `{"status": "resumed"}`. |
| `orchestrator.refresh` | `inflight_stable_ids: list[str] \| null = null` | `{"status": "refresh_requested", "refresh": {"requested": true, "in_progress": bool}}`. Requests an immediate GitHub issue refresh. `inflight_stable_ids` names issues the caller expects to appear, so the orchestrator can retry uncached when GitHub is eventually consistent. |
| `orchestrator.shutdown` | `force: bool = false`, `confirm: bool = false` | `{"status": "shutdown_requested" \| "force_shutdown", "active_sessions", "reason", "actor"}`. `force=true` **requires** `confirm=true`; otherwise returns an error of type `ConfirmationRequired`. |
| `orchestrator.snapshot` | *(none)* | One round trip for a full picture: `{"status", "info", "blocked", "stale", "dependency_problems", "excluded", "history"}`. |

### Sessions

Every session tool takes `issue_number: int` and targets the repository this
server is bound to.

| Tool | Arguments | Returns |
|------|-----------|---------|
| `orchestrator.session.worktree` | `issue_number` | `{"issue_number", "worktree_path", "session_name"}`. 404-style error when no worktree exists for the issue. |
| `orchestrator.session.manifest` | `issue_number` | `{"run_dir", "session_name", "manifest"}`, plus `"session_identity"`, `"analysis"` (`headline`/`detail`/`suggestions`), and `"validation_failure"` when those artifacts exist for the run. |
| `orchestrator.session.phases` | `issue_number` | `{"phases": [{"name", "display_name", "status", "status_icon", "started_at", "ended_at", "agent_label", "run_dir", "outcome", "validation_passed"}], "current_phase", "issue_number"}` — the linear history of runs (coding, review, rework, …) for the issue. |
| `orchestrator.session.claude_log` | `issue_number`, `limit: int = 200` | `{"log_path", "issue_number", "run_dir", "entry_count", "entries"}`. `entries` are parsed JSONL records from the agent's session log, truncated to `limit`. Unparseable lines come back as `{"_raw", "_parse_error": true}`. |
| `orchestrator.session.orchestrator_log` | `issue_number` | `{"filtered_log_path", "full_log_path", "issue_number"}`. Writes an issue-scoped tail (up to 500 lines) of the orchestrator log next to the run and returns its path. |
| `orchestrator.session.kill` | `issue_number` | `{"status": "terminated", "issue_number", "title", "killed_sessions", "hold_label", "errors"}`. Terminates the session **and applies a hold label** so the orchestrator does not immediately relaunch it. |
| `orchestrator.session.focus` | `issue_number` | `{"status": "focused", "issue_number"}`. Brings the agent's terminal to the foreground. Only meaningful for terminal backends that support focus (not the `subprocess` backend). |

### Repositories

These tools are **not** scoped to `--repo-root` — see
[Scope of the repository tools](#scope-of-the-repository-tools) below for what
they do and do not see.

| Tool | Arguments | Returns |
|------|-----------|---------|
| `orchestrator.state` | *(none)* | `{"dashboard": {"running", "pid", "port", "started_at"}, "repos": [...], "current_directory", "is_orchestrator_codebase", "cwd_is_git_repo"}` — the full system state behind the unified dashboard. |
| `orchestrator.repos` | *(none)* | `{"repos": [{"path", "name", "config_status", "orchestrator_state", "orchestrator_pid", "orchestrator_port", "configs", "selected_config", "is_current_dir"}]}`. |
| `orchestrator.repos.start` | `repo_path: str`, `config_name: str = "default.yaml"` | `{"status": "started", "pid", "port"}`, or a plain-string `{"error": "…"}` when the path fails validation (see below) or the launch fails. |
| `orchestrator.repos.stop` | `repo_path: str`, `force: bool = false` | `{"status": "stopped" \| "failed"}`. |

#### Scope of the repository tools

`orchestrator.state` and `orchestrator.repos` build their repository list from
exactly two sources, and nothing else:

1. **The persistent repo registry** — `~/.config/issue-orchestrator/repos.json`
   (or `$XDG_CONFIG_HOME/issue-orchestrator/repos.json`). Registered paths that
   no longer exist on disk are dropped from the result.
2. **The MCP process's own current working directory**, added at the front of
   the list when it is a Git checkout and isn't the orchestrator's own source
   tree. That is the directory the *client* spawned the server in — it is
   unrelated to `--repo-root`, which only scopes the engine-level and session
   tools.

For each of those repositories, the running/stopped state, PID, and port come
from the supervisor state for that path.

This is a registry lookup, not a machine-wide sweep. There is no process
enumeration and no scan for orchestrator lock files, so an orchestrator running
in a repository that is neither registered nor the server's working directory
**will not appear**. Register it first — the Control Center's *Add Repository*
button, or `POST /control/repos` on the Control API — if you want it visible
here.

### Diagnostics

| Tool | Arguments | Returns |
|------|-----------|---------|
| `orchestrator.urls` | *(none)* | `{"base_url", "dashboard_url", "events_url", "config_url"}`. Client-facing URLs — correctly rewritten for forwarded ports in [Codespaces](codespaces.md). |
| `orchestrator.doctor` | *(none)* | `{"overall": "ok" \| "warning" \| "error", "checks": [{"name", "status", "detail", "expandable"?}]}`. Runs the diagnostics in the MCP process itself, so it works even when the orchestrator is down. |

## Security and Operational Notes

The MCP server hands a client real control over agent sessions and processes.
The following constraints are deliberate; treat them as part of the contract
rather than incidental implementation.

**Transport is stdio only.** There is no HTTP transport, by design. Exposing
MCP over HTTP would meaningfully expand the attack surface, and the Control API
bearer token is not a substitute for a dedicated MCP authorization story. If an
HTTP transport is ever added, it must be an explicit opt-in with per-tool
authorization.

**No free-form text injection into agents.** `orchestrator.session.send` is
intentionally **not** registered. It previously let any MCP client write
arbitrary text into a running agent's prompt — a prompt-injection primitive
dressed up as a convenience method. It was removed and will not come back in
that shape. When a human needs to join a stuck session, the supported path is
attaching a PTY directly to the agent terminal, not a synthetic tool that types
on the operator's behalf.

**Destructive shutdown is confirm-gated.** `orchestrator.shutdown(force=true)`
kills running sessions mid-flight, so it is rejected unless the caller also
passes `confirm=true`. A graceful `force=false` shutdown needs no confirmation.
The distinction exists so a drive-by tool call can't tear the orchestrator
down.

**`orchestrator.repos.start` is path-guarded.** `repo_path` is caller-supplied
and therefore untrusted. It must resolve to an existing directory containing a
`.git` entry. Additionally, when the `ISSUE_ORCHESTRATOR_MCP_REPOS_ALLOWLIST`
environment variable is set, the path must resolve under one of its roots:

```bash
# Only these trees may be started via MCP (os.pathsep-separated).
export ISSUE_ORCHESTRATOR_MCP_REPOS_ALLOWLIST="$HOME/dev:$HOME/work"
```

Semantics:

| Env var value | Effect |
|---------------|--------|
| unset | No root restriction; the git-checkout check still applies. |
| set to one or more paths | Only paths under those roots may be started. |
| set but empty | **Every** path is rejected — a kill switch for `repos.start`. |

Set the allowlist whenever the MCP client is something you don't fully control.

The guard covers `repos.start` only. `orchestrator.repos.stop` takes the same
caller-supplied `repo_path` without that validation — stopping is not a
privilege escalation, so the worst outcome is a denial of service against an
orchestrator you already had the token to talk to.

**Control API token handling.** The bearer token authorizes every Control API
route. Clients that forward their whole environment to the MCP subprocess also
forward every other secret in it to whatever binary the command setting points
at. Forward an explicit allowlist instead — that is what the VS Code extension
does.

**Scope of the token.** The token protects against unrelated same-user
processes and cross-user access on a shared host. It is **not** a privilege
boundary against a deliberately malicious agent running as the same user:
orchestrator-launched agents keep the real `HOME` and can read the token file.
Real isolation requires OS-level containment (separate user, container, or
sandbox profile).

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| Server exits at startup with `Config file not found` | No `.issue-orchestrator/config/<name>.yaml` under `--repo-root`. Create one, or pass `--config`. |
| Client reports the command wasn't found | The entrypoint isn't on the client's `PATH`. Use an absolute path to the virtualenv's `issue-orchestrator-mcp`. |
| Every engine tool returns `Orchestrator not running` | The orchestrator isn't started and `--auto-start` wasn't passed. Add the flag, or start it separately. |
| Engine tools fail with a `401`-flavored error | No Control API token reached the subprocess. Start the orchestrator once so `~/.issue-orchestrator/api-token` exists, or set `ISSUE_ORCHESTRATOR_API_TOKEN` in the client's `env`. |
| `orchestrator.repos.start` returns "not under any configured … root" | `ISSUE_ORCHESTRATOR_MCP_REPOS_ALLOWLIST` is set and the path is outside it. |

For anything deeper, see [Troubleshooting](../development/TROUBLESHOOTING.md).

## Related

- [VS Code Integration](vscode.md) — the primary MCP client for this project
- [Configuration Reference](configuration_reference.md) — every config field
- [ADR-0025: PTY-first session UI and MCP control plane](../architecture/ADR/0025-pty-first-session-ui-mcp-control-plane.md)
