# VS Code Integration (From Zero)

This extension gives you a native VS Code experience for Issue Orchestrator: live status, queues, sessions, and controls, all inside your editor.

## Prerequisites

1. **Install Issue Orchestrator (Python package)** so the MCP entrypoint exists on your PATH:
   - You should be able to run `issue-orchestrator-mcp --help`.
   - See [MCP Server](mcp.md) for what that entrypoint is and what it exposes.
2. **Create a repo config** in your repo:
   - `.issue-orchestrator/config/modes/default/default.yaml`
   - Start from `examples/config.example.yaml` if you’re new.

## Install the Extension

From the repo root:

```bash
make install-vscode-extensions
cd packages/vscode
npm run compile
```

If you prefer manual steps, run `npm install` in `packages/vscode` instead of `make install-vscode-extensions`.

Then install the extension into VS Code:
- **Extension Development Host** (recommended for development):
  - Open the repo in VS Code
  - Run the `Run Extension` launch config
- Or package and install the extension locally if you prefer.

## First Run (What You Do in VS Code)

1. Open your repo in VS Code.
2. Open the **Issue Orchestrator** view from the Activity Bar.
3. Use the Command Palette:
   - `Issue Orchestrator: Start`
   - `Issue Orchestrator: Refresh`

The extension starts the MCP server and (by default) auto-starts the orchestrator if it isn’t running.

## What You Can Do In-Editor

- See **Active**, **Queue**, **Blocked**, and **History** sessions.
- Open worktrees, PRs, issues, and logs.
- Open a read-only **session console** for a running agent, or **focus** its
  terminal when the client supports it, and drive the agent from that terminal
  directly.
- Pause/Resume/Stop the orchestrator without leaving VS Code.
- Open the web **Dashboard** inside VS Code or in your browser.
- Run diagnostics in the **Doctor** panel (re-run, copy report, open Control Center/Dashboard) and surface issues in the Problems panel.

The extension deliberately cannot inject text into a running agent's prompt.
The MCP server does not register a session-send tool, because any client
holding the transport could use it to steer an agent — see
[the MCP security notes](mcp.md#security-and-operational-notes). Focusing the terminal keeps
that in your hands rather than the client's.

## Settings You’ll Actually Use

Configure in VS Code Settings:

- `issueOrchestrator.repoRoot`: repo to control (defaults to first workspace)
- `issueOrchestrator.configPath`: config file path
- `issueOrchestrator.autoStart`: auto-start orchestrator when connecting
- `issueOrchestrator.instanceId`: if you run multiple orchestrators

## Control Center From VS Code

Use the Command Palette:
- `Issue Orchestrator: Open Control Center`
- `Issue Orchestrator: Stop Control Center`

The Control Center runs in a VS Code terminal and serves the web UI.

## MCP Server (Optional)

The extension drives the same MCP server you can connect any other MCP client
to. You can run it manually as a smoke check that the entrypoint resolves and
your config loads:

```bash
issue-orchestrator-mcp --repo-root /path/to/repo --auto-start
```

It will then sit waiting for MCP traffic on stdin; press `Ctrl-C` to stop it.

The extension does **not** attach to a process you started yourself. The
transport is stdio, so the extension always spawns its own
`issue-orchestrator-mcp` subprocess (the command is the
`issueOrchestrator.mcpCommand` setting) and talks to it over that pipe. Leaving
a manual one running is harmless but unused.

For the full flag list, the `orchestrator.*` tool reference, a copy-paste
client config, and the security posture (stdio-only transport, the repos
allowlist, confirm-gated shutdown), see [MCP Server](mcp.md).

## Extension Tests

`make test-vscode` launches a VS Code Extension Development Host with isolated
test profiles under the system temp directory.
