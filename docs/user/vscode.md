# VS Code Integration (From Zero)

This extension gives you a native VS Code experience for Issue Orchestrator: live status, queues, sessions, and controls, all inside your editor.

## Prerequisites

1. **Install Issue Orchestrator (Python package)** so the MCP entrypoint exists on your PATH:
   - You should be able to run `issue-orchestrator-mcp --help`.
   - See [MCP Server](mcp.md) for what that entrypoint is and what it exposes.
2. **Create a repo config** in your repo:
   - `.issue-orchestrator/config/default.yaml`
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
- Open a live **session console** and send messages to running agents.
- Pause/Resume/Stop the orchestrator without leaving VS Code.
- Open the web **Dashboard** inside VS Code or in your browser.
- Run diagnostics in the **Doctor** panel (re-run, copy report, open Control Center/Dashboard) and surface issues in the Problems panel.

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
to. You can run it manually:

```bash
issue-orchestrator-mcp --repo-root /path/to/repo --auto-start
```

The extension will detect and use it if running.

For the full flag list, the `orchestrator.*` tool reference, a copy-paste
client config, and the security posture (stdio-only transport, the repos
allowlist, confirm-gated shutdown), see [MCP Server](mcp.md).

## Extension Tests

`make test-vscode` launches a VS Code Extension Development Host with isolated
test profiles under the system temp directory.
