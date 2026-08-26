# Installation

## Prerequisites

- **Python 3.11+** (3.14 recommended; the build auto-detects the best available version)
- **[uv](https://docs.astral.sh/uv/)** — used for dependency management and lockfile resolution
- **GNU Make** — `make` on Linux, `gmake` on macOS (`brew install make`)
- **Git** — for worktree management
- **GitHub CLI (`gh`)** or a GitHub personal access token — for API access
- **A supported AI coding tool** — Claude Code, Cursor, or Codex CLI

## Install

```bash
git clone https://github.com/BruceBGordon/issue-orchestrator.git
cd issue-orchestrator
make venv
```

`make venv` creates a `.venv` with the best available Python, installs all dependencies from `uv.lock`, and installs the `issue-orchestrator` CLI as an editable package.

Activate the environment:

```bash
source .venv/bin/activate
```

Verify the install:

```bash
issue-orchestrator --help
```

This checkout is where the CLI is installed. The setup and start commands below should be run from the repository you want to automate, or by passing that path explicitly to `issue-orchestrator setup /path/to/repo`.

## GitHub auth

The orchestrator needs GitHub auth with repo access. The most common options are:

- existing `gh auth login` auth, which the orchestrator reads from GitHub CLI auth storage
- an environment variable such as `ISSUE_ORCH_GITHUB_TOKEN`
- the app-specific OS keychain entry created by `issue-orchestrator auth store`

Set a token explicitly via environment variable:

```bash
export ISSUE_ORCH_GITHUB_TOKEN=ghp_...
```

Or store it in the OS keychain:

```bash
issue-orchestrator auth store
```

See [GitHub Auth and Permissions](github-permissions.md) for required scopes
and alternative auth methods. If your repo enforces required PR approvals and
you need to approve agent-created PRs yourself, read the GitHub App section
before relying on a personal token.

## Optional: HTCondor for validation lanes

Validation lanes run as direct subprocesses by default — nothing extra
to install. Optionally, they can run through a personal
[HTCondor](https://htcondor.org) pool for admission control, per-lane
deadlines, and machine-wide exclusivity:

```bash
scripts/condor-personal.sh up   # one-time install + start (macOS or Linux)
```

then opt in per invocation (`LANE_EXECUTOR=condor make validate-pr`) or
in your validation config. See [HTCondor Validation Lanes](condor_lanes.md).

## Next steps

Change to the repository you want to automate, run `issue-orchestrator setup`, then follow the [Quickstart Guide](quickstart.md).
