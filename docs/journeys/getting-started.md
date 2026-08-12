# Getting Started

You want to try Issue Orchestrator — get it running, see what it does, then decide if it's for you.

There are two repo-state paths:
- `New repo`
- `Existing repo`

And two execution styles:
- `Human-driven` — follow this page directly
- `Agent-guided` — let your AI assistant drive the same steps; see [Agent-Guided Onboarding](agent-guided-onboarding.md)

## Before you start

You'll need:
- Python 3.11+, [uv](https://docs.astral.sh/uv/), GNU Make, Git
- GitHub auth that can access your repo: `gh auth login`, `ISSUE_ORCH_GITHUB_TOKEN`, `GITHUB_TOKEN`, `GH_TOKEN`, or `issue-orchestrator auth store`
- **An AI coding tool**: Claude Code, Cursor, or Codex CLI. The orchestrator launches these to work on issues. If you don't have one installed, nothing will run.

Personal-token auth is enough for a first trial. For strict branch protection
where you need to approve agent-created PRs yourself, plan for the GitHub App
protected-branch model described in [GitHub Auth and Permissions](../user/github-permissions.md#protected-branch-mode-github-app).

## 1. Install

```bash
git clone https://github.com/BruceBGordon/issue-orchestrator.git
cd issue-orchestrator
make venv
source .venv/bin/activate
```

Full details: [Installation](../user/installation.md)

Running in GitHub Codespaces instead? See [Codespaces](../user/codespaces.md).

This checkout is where the CLI lives. Run the remaining commands from the repository you want to automate.

## 2. Run the setup wizard

```bash
cd /path/to/your/project
issue-orchestrator setup
```

The wizard asks about your repo, agents, validation, and review preferences. For a first trial in a fresh repo, choose `New project - set up from scratch`. If you're pointing the orchestrator at a repo that already has labels/issues/config to preserve, choose `Existing project - I have labels/issues already`. You can always re-run the wizard later.

If you're using an AI assistant to drive setup, tell it which repo-state path to use and have it continue through `setup-guardrails`, `init`, publishing the generated onboarding files to the worktree seed ref (or setting `worktrees.seed_ref: HEAD` for local iteration), `doctor`, and one real issue run instead of stopping after config generation.

If you choose `codex`, leave the wizard's Codex model prompt blank for the safest first run. That lets the installed Codex CLI choose the correct default for your account instead of pinning a stale model name.

The wizard creates `.issue-orchestrator/config/modes/default/default.yaml`. You can also write this by hand — see [Configuration](../user/configuration.md) for a minimal starter config.

## 3. Set Up Repo Guardrails

```bash
issue-orchestrator setup-guardrails
```

The wizard can install these for you. If you skipped that prompt, run this before starting the orchestrator. This installs the repo-local pre-push gate plus the configured AI-agent hooks, and rerunning it refreshes those managed hook files if they drift. It prevents agent bypasses like `git push --no-verify` and gives `doctor` something concrete to verify. If Control Center blocks startup because the **Repo Guardrails** Doctor check failed, use **Repair Guardrails** in the Doctor modal; it runs the same guardrail setup flow. See [Guardrails](../design/guardrails.md) for why this matters.

## 4. Initialize labels and repo state

```bash
issue-orchestrator init
```

This creates or refreshes the orchestrator labels in your GitHub repo. It is safe to rerun, and it removes guesswork about whether setup already created the labels.

## 5. Commit the generated onboarding files

Run `git status`, review the generated onboarding files, and publish them to the worktree seed ref before starting the orchestrator. By default that means commit and push them to your default branch. For purely local evaluation, set `worktrees.seed_ref: HEAD` and then commit them locally.

Agent worktrees are seeded from the configured ref, not from your working tree. If `.prompts/` or related setup files only exist as local changes outside that ref, the first agent session will not inherit them.

## 6. Verify everything works

```bash
issue-orchestrator doctor
```

Doctor checks GitHub connectivity, token permissions, config validity, AI hook installation, and repo guardrails state. Run it after the onboarding files are published to the worktree seed ref, or after you set `worktrees.seed_ref: HEAD` for local iteration. `issue-orchestrator start` reruns the same preflight checks and exits on errors.

## 7. Label a GitHub issue

Add an agent label (e.g., `agent:dev`) to a GitHub issue in your repo. The orchestrator picks up issues by label — no label, no work.

## 8. Start the orchestrator

```bash
issue-orchestrator start
```

Open `http://localhost:8080` to watch the dashboard. You'll see your issue move through the pipeline: queued, running, review, done.

The browser may ask for the local admin token from `~/.issue-orchestrator/api-token` the first time you open the Control Center. This is not another GitHub credential; it protects the local Control API that can manage repository engines, worktrees, agent sessions, logs, and configuration.

If you are using `claude-code`, let the setup wizard enable trusted session interactions. That writes `execution.session_interactions.enabled: true`, allowing orchestrator-created worktrees to auto-accept Claude's initial trust prompt. If you leave it disabled, the first interactive session in each new worktree may pause for manual trust approval. A dedicated worktree base still keeps those paths predictable, but pre-approving the parent worktree directory does not automatically trust future child worktrees.

## What happens under the hood

The orchestrator:
1. Fetches labeled issues from GitHub
2. Creates an isolated git worktree for each issue
3. Launches your AI tool in that worktree with a prompt
4. Waits for the agent to call `coding-done` or `reviewer-done` (the structured completion commands)
5. Runs validation (tests, linting)
6. Queues code review if configured
7. Creates a PR when everything passes
8. You merge

For the full walkthrough with examples, prompt templates, and multi-agent setup, read the [Tutorial](../user/tutorial.md).

## What to read next

| If you want to... | Read |
|---|---|
| Let an AI drive onboarding | [Agent-Guided Onboarding](agent-guided-onboarding.md) |
| Understand all config options | [Configuration](../user/configuration.md) then [Configuration Reference](../user/configuration_reference.md) |
| Set up code review | [Review Workflow](../development/REVIEW_WORKFLOW.md) |
| Use the VS Code extension | [VS Code Integration](../user/vscode.md) |
| Expose your repo's test results in the dashboard | [Client Test Integrations](../user/test-integrations.md) |
| Run E2E tests automatically | [E2E Runner](../user/e2e.md) |
| Common questions | [FAQ](../user/faq.md) |
| Something broke | [Troubleshooting](../development/TROUBLESHOOTING.md) |
