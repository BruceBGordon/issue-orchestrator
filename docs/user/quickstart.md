# Quickstart

This guide gets you from zero to a running orchestrator in a target repo. For prerequisites and install details, see [Installation](installation.md).

If you want your AI assistant to do the driving instead of following these steps manually, use the [Agent-Guided Onboarding](../journeys/agent-guided-onboarding.md) path. The repo-state choice is still the same: `new repo` or `existing repo`.

## 1. Install and activate the CLI

```bash
make venv
source .venv/bin/activate
```

Install the CLI in the `issue-orchestrator` checkout, then run the remaining commands from the repository you want to automate.

## 2. Ensure GitHub auth is available

```bash
export ISSUE_ORCH_GITHUB_TOKEN=ghp_...
```

You can also rely on an existing `gh auth login` session; the orchestrator reads GitHub CLI auth from its normal auth storage. `issue-orchestrator auth store` remains the app-specific keychain fallback. Whatever source you use must be able to access the target repo. See [GitHub Auth and Permissions](github-permissions.md) for details.

If the Control Center already discovered the repository, you can instead select
**Setup** and complete its guided GitHub stage. It explains the personal-user
and GitHub App options, waits while you complete GitHub-side steps, and requires
read-only verification before Preview and Save.

Personal-token auth makes orchestrator PRs appear under that user. If your repo
requires approvals and you need to approve agent-created PRs yourself, plan for
the GitHub App protected-branch model in [GitHub Auth and Permissions](github-permissions.md#protected-branch-mode-github-app).

You also need a supported AI coding tool installed. If Claude Code, Cursor, or Codex CLI is missing, sessions will not launch.

## 3. Move to your target repo and run the setup wizard

```bash
cd /path/to/your/project
issue-orchestrator setup
```

This creates `.issue-orchestrator/config/modes/default/default.yaml` with your repo settings and agent definitions. If you prefer to stay elsewhere, run `issue-orchestrator setup /path/to/your/project` instead. You can also copy and edit the [example config](../../examples/config.example.yaml) directly.

Alternatively, in the Control Center select **Setup** for the discovered
repository. That workflow creates the same repository setup, defaults worktrees
to the dedicated peer directory `../worktrees/<repository-directory>`, and
configures a default worker → reviewer/rework → tech-lead pipeline. The
Configuration step lets you select model and effort for each role, set the
tech-lead cadence (`1` reviews every approved PR), and confirm detected
repository-specific quick and publish commands. When setup cannot detect both
validation gates, it waits for you to enter them. It then walks through GitHub
authorization before showing the YAML preview. The CLI wizard expects GitHub
authorization to be available before it starts; the Control Center workflow is
the guided, wait-and-verify path.

After saving, the workflow points to independent features you might configure
later, including specialized reviewers, other AI providers, E2E, merge queue,
Goal Pilot, and tech-lead health automation.

If you choose `codex`, leave the wizard's Codex model prompt blank for the safest first run. That lets the installed Codex CLI choose the right default for your account.

A minimal config looks like:

```yaml
agents:
  "agent:dev":
    prompt: ".prompts/dev.md"
    provider: "claude-code"
    ai_system: "claude-code"
    model: "sonnet"

validation:
  quick:
    cmd: "python -m pytest -q"
    timeout_seconds: 300
  publish:
    cmd: "python -m pytest -q"
    timeout_seconds: 1800
    dirty_check: tracked
```

Use validation commands that actually exist in the target repo. Do not leave placeholder commands in place for the first run. `quick` should be fast feedback for agents; `publish` should be the repo's authoritative local pre-push gate.

## 4. Set up repo guardrails

```bash
issue-orchestrator setup-guardrails
```

The setup wizard can install these for you. If you skipped that prompt, run this before starting the orchestrator. Rerunning it is safe and refreshes the managed hook files.

## 5. Initialize labels and repo state

```bash
issue-orchestrator init
```

This creates or refreshes the orchestrator labels in GitHub. It is safe to rerun.

## 6. Commit the generated onboarding files

Run `git status`, review the generated onboarding files, and publish them to the worktree seed ref before starting the orchestrator. By default that means commit and push them to your default branch. If you are doing local-only evaluation, set `worktrees.seed_ref: HEAD` and then commit them locally.

Agent worktrees are seeded from the configured ref, not from your working tree. If `.prompts/` or related setup files only exist as local changes outside that ref, the first agent session will not inherit them.

## 7. Verify the setup before starting

```bash
issue-orchestrator doctor
```

Doctor checks GitHub connectivity, token permissions, config validity, AI hook installation, and repo guardrails state. Run it after the onboarding files are published to the worktree seed ref, or after you set `worktrees.seed_ref: HEAD` for local iteration. `issue-orchestrator start` runs the same preflight checks and exits on errors.

## 8. Label a GitHub issue

Add the `agent:dev` label to a GitHub issue in your repo. The orchestrator picks up issues by label.

## 9. Start the orchestrator

```bash
issue-orchestrator start
```

This launches the orchestrator with the web dashboard. Open `http://localhost:8080` to watch issues move through the pipeline.

The browser may ask for the local admin token from `~/.issue-orchestrator/api-token` the first time you open the Control Center. This is not another GitHub credential; it protects the local Control API that can manage repository engines, worktrees, agent sessions, logs, and configuration.

If you are using `claude-code`, let the setup wizard enable trusted session interactions. That writes `execution.session_interactions.enabled: true`, allowing orchestrator-created worktrees to auto-accept Claude's initial trust prompt. If you leave it disabled, the first interactive session in each new worktree may pause for manual trust approval. A dedicated worktree base still keeps those paths predictable, but pre-approving the parent worktree directory does not automatically trust future child worktrees.

## What happens next

The orchestrator will:

1. Fetch labeled issues from GitHub
2. Create isolated git worktrees for each issue
3. Launch agent sessions to work on the code
4. Run your configured validation command on completed work
5. Queue code review (if configured)
6. Create a draft PR when review passes
7. Wait for a human to merge

See [Configuration](configuration.md) for the full set of options, or the [Configuration Reference](configuration_reference.md) for every field.
