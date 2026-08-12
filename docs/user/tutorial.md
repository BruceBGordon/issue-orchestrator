# Issue Orchestrator Tutorial

A hands-on guide to setting up and using issue-orchestrator to run AI agents on GitHub issues in parallel.

## Table of Contents

1. [What is Issue Orchestrator?](#what-is-issue-orchestrator)
2. [Prerequisites](#prerequisites)
3. [Installation](#installation)
4. [Quick Setup Walkthrough](#quick-setup-walkthrough)
5. [Understanding the System](#understanding-the-system)
6. [Creating Agent Prompts](#creating-agent-prompts)
7. [Setting Up Safety Hooks](#setting-up-safety-hooks)
8. [Creating Issues for Agents](#creating-issues-for-agents)
9. [Running the Orchestrator](#running-the-orchestrator)
10. [How an Issue Flows Through the System](#how-an-issue-flows-through-the-system)
11. [Monitoring and Managing Sessions](#monitoring-and-managing-sessions)
12. [Troubleshooting](#troubleshooting)
13. [Next Steps](#next-steps)

---

## What is Issue Orchestrator?

Issue Orchestrator automates the process of having AI agents (like Claude Code) work on GitHub issues in parallel. Instead of manually running Claude on one issue at a time, you can:

- Label issues with agent tags (e.g., `agent:backend`, `agent:frontend`)
- Run the orchestrator to automatically pick up and process those issues
- Monitor progress through a web dashboard
- Have agents create PRs and report their work in structured comments

Think of it as a supervisor that:
1. Pulls issues from your GitHub repo based on labels
2. Creates isolated git worktrees for each issue
3. Launches AI agent sessions to work on them
4. Monitors for completion, blocks, or questions
5. Cleans up and moves to the next issue

---

## Prerequisites

### Required

| Tool | Purpose | Installation |
|------|---------|--------------|
| **Python 3.11+** | Runs the orchestrator | `brew install python` or from python.org |
| **[uv](https://docs.astral.sh/uv/)** | Dependency management | `brew install uv` or see uv docs |
| **GNU Make** | Build automation | `brew install make` (macOS), pre-installed (Linux) |
| **git** | Version control | Usually pre-installed |
| **Claude CLI** | The AI agent | See [Claude Code docs](https://docs.anthropic.com/en/docs/claude-code) |

### Verify Your Setup

```bash
python3 --version  # Should be 3.11 or higher
git --version
claude --version
```

---

## Installation

```bash
git clone https://github.com/BruceBGordon/issue-orchestrator
cd issue-orchestrator
make venv
source .venv/bin/activate
```

Verify:

```bash
issue-orchestrator --help
```

---

## Quick Setup Walkthrough

### Step 1: Navigate to Your Project

```bash
cd /path/to/your/project
```

### Step 2: Run the Setup Wizard

```bash
issue-orchestrator setup
```

This creates `.issue-orchestrator/config/modes/default/default.yaml` interactively. Or create it manually — a minimal config:

```yaml
agents:
  "agent:dev":
    prompt: ".issue-orchestrator/prompts/worker.md"
    model: "sonnet"
    ai_system: "claude-code"
    timeout_minutes: 45

execution:
  concurrency:
    max_concurrent_sessions: 2

validation:
  quick:
    cmd: "make test"
    timeout_seconds: 300
  publish:
    cmd: "make test"
    timeout_seconds: 1800
    dirty_check: tracked
```

### Step 3: Set Your GitHub Token

```bash
export ISSUE_ORCH_GITHUB_TOKEN=ghp_...
```

See [GitHub Auth and Permissions](github-permissions.md) for required scopes.
If your repo requires PR approvals and you need to approve orchestrator-created
PRs yourself, a personal token is not enough: use the GitHub App
protected-branch model instead.

### Step 4: Create the Prompt File

```bash
mkdir -p .issue-orchestrator/prompts
```

Create `.issue-orchestrator/prompts/worker.md`:

```markdown
# Issue Worker

You are working on GitHub issue #{issue_number}: {issue_title}

## Your Task

1. Read the issue carefully
2. Explore the codebase to understand the context
3. Implement the solution
4. Write tests if appropriate
5. Commit your changes with clear messages

## Completion (REQUIRED)

When done, use the `coding-done` command:

### If you completed the work:
coding-done completed \
  --implementation "What you implemented" \
  --problems "Any issues encountered, or 'None'"

### If you're blocked:
coding-done blocked \
  --reason "Why you cannot proceed" \
  --attempted "What you tried"

Sessions that exit without calling `coding-done` are marked as failed.
```

### Step 5: Install Safety Hooks

```bash
issue-orchestrator setup-guardrails
```

This installs the repo-local pre-push gate plus the configured AI-agent hooks that prevent bypasses like `git push --no-verify`.

### Step 6: Verify Your Setup

```bash
issue-orchestrator doctor
```

This checks configuration, GitHub connectivity, token permissions, and hook installation.

---

## Understanding the System

### Agent Labels

Issues are routed to agents based on GitHub labels:

```
GitHub Issue Label    →    Config Key              →    Agent Settings
  "agent:backend"     →    agents["agent:backend"]  →    prompt, model, timeout
```

You define which agents exist by adding entries to your config:

```yaml
agents:
  "agent:backend":
    prompt: "prompts/backend.md"
    ai_system: "claude-code"
  "agent:frontend":
    prompt: "prompts/frontend.md"
    ai_system: "claude-code"
```

### Worktrees

Each issue gets its own [git worktree](https://git-scm.com/docs/git-worktree) — an isolated checkout:

```
your-repo/           <- main repo
your-repo-42/        <- worktree for issue #42
your-repo-57/        <- worktree for issue #57
```

This lets multiple agents work in parallel without conflicts.

### Priority

There are two priority signals:

1. **Scheduling order**: Title prefix `[P?-nnn]`
   - `P0` < `P1` < `P2` ... `P9` (lower runs first)
   - `-nnn` is the sequence within that tier
   - If missing, uses the configured default tier (P1 by default)
   - Example: `[P0-005] Fix critical bug`

2. **Labels** (`priority:high|medium|low`): Display/metadata only, not used by the scheduler.

### Status Labels

| Label | Meaning | Who Sets It |
|-------|---------|-------------|
| `in-progress` | Being worked on | Orchestrator |
| `blocked` | Agent hit a blocker | Agent (via `coding-done blocked`) |
| `needs-human` | Agent has a question | Agent (via `coding-done needs_human`) |

---

## Creating Agent Prompts

Prompts are markdown files that instruct the AI. They receive these template variables:

| Variable | Description | Example |
|----------|-------------|---------|
| `{issue_number}` | GitHub issue number | `42` |
| `{issue_title}` | Issue title | `Add user authentication` |
| `{worktree}` | Path to the worktree | `/home/dev/myapp-42` |
| `{prompt}` | Path to this prompt file | `prompts/backend.md` |

### Example: Backend Agent

`.issue-orchestrator/prompts/backend.md`:

```markdown
# Backend Agent

You are a backend engineer working on issue #{issue_number}: {issue_title}

## Guidelines

1. Follow existing code patterns in the codebase
2. Write tests for new functionality
3. Keep functions small and focused
4. Use type hints

## Workflow

1. Read the issue and understand requirements
2. Explore relevant files
3. Implement the solution
4. Write/update tests
5. Run tests to verify
6. Commit with clear messages

## Completion

Use `coding-done` when finished:

# Success
coding-done completed \
  --implementation "Added /users endpoint with JWT auth" \
  --problems "None"

# Blocked
coding-done blocked \
  --reason "Need database credentials" \
  --attempted "Checked .env, asked in issue comments"

# Need help
coding-done needs_human --question "Should we use OAuth or API keys?"

**Important**: Report ALL problems honestly.
```

### Multi-Agent Setup

```yaml
agents:
  "agent:backend":
    prompt: "prompts/backend.md"
    model: "sonnet"
    timeout_minutes: 45
    ai_system: "claude-code"

  "agent:frontend":
    prompt: "prompts/frontend.md"
    model: "sonnet"
    timeout_minutes: 30
    ai_system: "claude-code"

  "agent:docs":
    prompt: "prompts/docs.md"
    model: "haiku"
    skip_review: true
    ai_system: "claude-code"
```

---

## Setting Up Safety Hooks

Hooks prevent agents from bypassing safety guardrails. Without hooks, agents can skip pre-push tests with `--no-verify` or push broken code directly. Technical enforcement is the only reliable solution — prompt instructions are suggestions that agents can forget or work around.

```bash
issue-orchestrator setup-guardrails
```

Verify hooks work:

```bash
issue-orchestrator verify
issue-orchestrator verify --test-ai-gate  # Spawns agent to test blocking
```

If verification fails, the orchestrator won't start by default. See [Guardrails & Safety Model](../../docs/design/guardrails.md) for the full enforcement architecture.

---

## Creating Issues for Agents

For an issue to be picked up:

1. **Must have an agent label** matching a key in your config (e.g., `agent:backend`)
2. **Should have a clear description** so the agent understands the task

Optional:
- **Title identity key** `[M1-010]` for stable cross-reference identity (see below)
- **Title priority prefix** `[P1-010]` for scheduling order
- **Priority label** (`priority:high|medium|low`) for display/metadata

### Issue Identity Keys

Each issue needs a stable identity for session tracking, dependency resolution, and the dashboard. The orchestrator derives this from the issue title:

| Title format | Identity used | Example |
|---|---|---|
| `[M1-010] Add profile endpoint` | `M1-010` (from prefix) | Milestone-based key |
| `Add profile endpoint` | `42` (GitHub issue number) | Automatic fallback |

**The prefix is optional.** If your title has no `[M?-nnn]` prefix, the orchestrator automatically falls back to the native GitHub issue number as the identity. Everything works the same — sessions, the dashboard, filtering, and logs all use whichever identity is available.

**When to use the prefix:**
- You're organizing issues into milestones and want human-readable cross-references (e.g., `Depends-on: M1-010`)
- You want stable IDs that don't change if issues are transferred or renumbered
- **You're batch-creating issues** (from a planning agent, script, or import tool) — GitHub issue numbers aren't known until after creation, so you can't wire up `Depends-on: #123` dependencies in advance. Self-assigned keys like `[M1-010]` let you define the full dependency graph before any issues exist on GitHub.

**When you can skip it:**
- You reference dependencies by issue number (`Depends-on: #42`) and issues already exist
- You don't need milestone-scoped naming
- You're just getting started and want to keep things simple

The prefix format is `[M<digits>-<3 digits>]` — for example `[M1-010]`, `[M2-005]`, `[M10-999]`. It must appear at the very start of the title.

### Example Issue

**Title**: `[P1-010] Add user profile endpoint`

**Labels**: `agent:backend`

**Body**:
```markdown
## Summary
Add a GET /users/{id}/profile endpoint that returns user profile information.

## Requirements
- Return user's name, email, bio, and avatar URL
- Return 404 if user doesn't exist
- Include rate limiting (10 requests/minute)

## Acceptance Criteria
- [ ] Endpoint returns correct data for existing users
- [ ] Returns 404 with appropriate message for missing users
- [ ] Rate limiting works correctly
- [ ] Tests cover success and error cases
```

### Tips for Good Agent Issues

1. **Be specific** — agents work best with clear, bounded tasks
2. **Provide context** — link to relevant files or docs
3. **Include acceptance criteria** — how will we know it's done?
4. **One task per issue** — don't bundle multiple unrelated changes

---

## Running the Orchestrator

### Basic Usage

```bash
issue-orchestrator start
```

This launches the orchestrator with the web dashboard at http://localhost:8080.

### Common Options

```bash
# Custom port
issue-orchestrator start --port 3000

# Limit to specific milestone
issue-orchestrator start --milestone "v2.0"

# Only process N issues then stop
issue-orchestrator start --max-issues 5

# Dry run — show what would happen without doing it
issue-orchestrator start --dry-run

# Enable debug logging
issue-orchestrator start --debug

# Run without dashboard (for CI/scripts)
issue-orchestrator start --no-dashboard
```

### Other Commands

```bash
issue-orchestrator status          # Show current status
issue-orchestrator pause           # Finish current work, don't start new
issue-orchestrator resume          # Resume after pause
issue-orchestrator doctor          # Run diagnostics
issue-orchestrator init            # Initialize GitHub labels
issue-orchestrator setup-guardrails # Install repo guardrails + AI hooks
```

---

## How an Issue Flows Through the System

### 1. Issue Labeled

```
GitHub Issue #42: "[P1-010] Add user profile endpoint"
Labels: [agent:backend]
```

### 2. Orchestrator Fetches and Prioritizes

The orchestrator polls GitHub, finds issues with agent labels, and sorts by priority.

### 3. Worktree Created

```
Creating worktree for issue #42...
  Worktree: ../your-repo-42
  Branch: 42-add-user-profile-endpoint
```

### 4. Issue Labeled `in-progress`

### 5. Agent Session Launched

The orchestrator starts Claude in the worktree with the configured prompt. The agent reads the issue, explores the code, implements the solution, writes tests, and commits.

### 6. Agent Calls `coding-done`

```bash
coding-done completed \
  --implementation "Added GET /users/{id}/profile endpoint with rate limiting" \
  --problems "None"
```

This writes a structured `completion.json` file in the worktree. The agent does **not** push or create PRs — the orchestrator handles that.

### 7. Orchestrator Processes Completion

The orchestrator:
1. Reads `completion.json`
2. Pushes the branch
3. Creates a draft PR
4. Posts a structured comment on the issue
5. Queues code review (if configured)
6. Removes `in-progress` label

### 8. Review (if configured)

If `review.enabled: true`, a reviewer agent evaluates the PR. If changes are requested, a rework cycle begins automatically. See [Review Workflow](../development/REVIEW_WORKFLOW.md).

### 9. Human Merges

The PR is ready for human review and merge. The orchestrator never merges PRs.

---

## Monitoring and Managing Sessions

### Web Dashboard

The default dashboard at http://localhost:8080 shows:

- **Active sessions** — agents currently working, with runtime and status
- **Issue queue** — pending issues sorted by priority
- **Blocked issues** — issues needing human attention
- **Controls** — pause/resume, refresh, settings

### Status Check

```bash
issue-orchestrator status
```

---

## Troubleshooting

### Issue Not Being Picked Up

1. **Check labels**: `gh issue view 42 --json labels` — must have an agent label matching your config
2. **Check for `in-progress`**: Remove it to retry: `gh issue edit 42 --remove-label "in-progress"`
3. **Check filters**: Review `filtering` in your config

### Agent Session Fails

1. Check session logs in the web dashboard
2. Check if `coding-done` was called (sessions without it are marked failed)
3. Common causes: missing dependencies, prompt file not found, API rate limits

### Hooks Not Blocking

```bash
issue-orchestrator verify  # Diagnose hook issues
```

### Session Times Out

Adjust in config:
```yaml
agents:
  "agent:backend":
    timeout_minutes: 90
```

Or break complex issues into smaller ones.

For more, see [Troubleshooting](../development/TROUBLESHOOTING.md).

---

## Next Steps

- **Customize prompts** for your project's conventions and testing framework
- **Enable code review**: set `review.enabled: true` with a reviewer agent — see [Review Workflow](../development/REVIEW_WORKFLOW.md)
- **Expose your tests in the dashboard**: JUnit XML, artifacts, and framework recipes — see [Client Test Integrations](test-integrations.md)
- **Configure E2E testing**: see [E2E Runner](e2e.md)
- **Explore Goal Pilot** *(planned)*: autonomous goal-driven orchestration — see [Goal Pilot](goal_pilot.md)
- **Full configuration**: see [Configuration](configuration.md) and [Configuration Reference](configuration_reference.md)
