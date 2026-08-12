# FAQ

## Getting Started

**Q1: Where does the config live, and how do I pick a mode or named config?**
A: By default, config lives at `.issue-orchestrator/config/modes/default/default.yaml`. Add sibling directories such as `modes/codex/` or `modes/claude/` for alternate complete config sets. In Control Center, pick the repo, then choose `Mode` and `Config` before starting the Repository Engine. These selectors are locked while the engine is running. In VS Code, set `issueOrchestrator.configPath` to the complete mode config path.

**Q2: What is the minimum config to run anything?**
A: You need at least one agent definition and a validation command. See the minimal example in [Configuration](configuration.md) under "TL;DR - Starter Config to Get Running."

**Q3: How does the repo root and repo name get set?**
A: The orchestrator auto-detects the repository from the git checkout. Override it only if detection isn't correct, using `repo.root` for the path and `repo.name` for the GitHub repo slug.

**Q4: Where do worktrees go by default, and can I move them?**
A: New repository Setup defaults to `../worktrees/<repo-name>` via
`worktrees.base`, keeping every repository's issue worktrees in a dedicated
peer collection outside the source checkout. The absolute path is shown before
Setup saves the YAML. You can edit it there or set `worktrees.base` manually.

If you use Claude Code, enable `execution.session_interactions.enabled: true` or let the setup wizard do it for you. That lets Issue Orchestrator auto-accept Claude's initial trust prompt in orchestrator-created worktrees. A dedicated worktree directory can still make the paths easier to find, but trust is stored per worktree path, not inherited from the parent directory.

**Q5: What happens if I change the worktree location after I've been running for a while?**
A: Existing worktrees stay where they were created. Changing `worktrees.base` only affects new worktrees. If you want everything under the new base, clean up old worktrees and let the orchestrator recreate them (or move them manually and make sure any in-flight sessions are stopped first).

**Q6: How do I limit which issues get picked up while I'm learning?**
A: Use `filtering.label`, `filtering.milestone`, `filtering.milestones`, or `filtering.issue` to scope the queue. You can also exclude labels with `filtering.exclude_labels`. Examples:

```yaml
# Only issues labeled "bot-ready"
filtering:
  label: "bot-ready"
```

```yaml
# Only issues in M1 or M2, but never "test-data"
filtering:
  milestones: ["M1", "M2"]
  exclude_labels: ["test-data"]
```

```yaml
# Only a single issue (by number)
filtering:
  issue: 123
```

**Q7: What are the guardrails, and which ones do I need to set up?**
A: The guardrails are the repo's safety hooks that prevent unsafe operations (for example, bypassing validation or pushing without completing). For a target repo, run `issue-orchestrator setup-guardrails`: it installs the repo-local pre-push gate plus the configured AI-agent hooks. If Control Center Doctor reports a **Repo Guardrails** failure, click **Repair Guardrails** to run the same flow from the UI. Keep `security.enforce_hooks` enabled so worktrees still get the orchestrator-side wrapper hooks. For details, see [Guardrails & Safety Model](../../docs/design/guardrails.md) and [Hook Enforcement](../architecture/hooks.md).

**Q8: What GitHub token does `issue-orchestrator` use, and what permissions does it need?**
A: `issue-orchestrator` resolves GitHub auth in two modes:

- If a repo config declares `repo.github.token_env` or `repo.github.keyring_service` / `repo.github.keyring_username`, those repo-scoped sources are authoritative.
- If a repo does not declare its own source, the global fallback order is `ISSUE_ORCH_GITHUB_TOKEN`, `GITHUB_TOKEN`, `GH_TOKEN`, then GitHub CLI `hosts.yml`, then the optional OS keychain entry created by `issue-orchestrator auth store`.

The common global setup is:

```bash
export ISSUE_ORCH_GITHUB_TOKEN="github_pat_..."
./scripts/start_control_center.sh
```

For a repo-specific setup, declare the source in config:

```yaml
repo:
  name: "BruceBGordon/tixmeup"
  github:
    token_env: "TIXMEUP_GITHUB_TOKEN"
    keyring_service: "tixmeup-github"
    keyring_username: "${USER}"
```

`doctor` and Control Center prereq/start checks validate access to `repo.name`,
not just `/user`. If the repo-scoped source is missing, startup fails with a
clear auth error instead of silently falling back to a different token.

When you launch a repository from Control Center, the repository engine is
started directly by the orchestrator supervisor. Target-repo wrapper scripts are
not run, so any token export that only happens inside such a script is not
available. Put the env var in the Control Center process environment, or declare
the Keychain fallback in the repo config as shown above.

For a fine-grained PAT, grant repository access only to the target repo and set these permissions:

| Permission | Access |
|---|---|
| Metadata | Read |
| Contents | Read and write |
| Issues | Read and write |
| Pull requests | Read and write |

No user permissions are required. `Actions`, `Administration`, `Projects`, `Packages`, `Deployments`, `Secrets`, and similar extra permissions are not needed for normal orchestrator operation.

This token mode attributes orchestrator-created branches and PRs to the token
owner. If your branch protection requires all PRs to be approved, GitHub will
not let that same user approve the PR. For strict protected-branch operation,
the recommended model is GitHub App auth so PRs are authored by an app/bot and
reviewed by a human. In the local/self-hosted model each user or org creates
its own GitHub App; a shared public app only works if there is a hosted service
holding the app private key. See [GitHub Auth and Permissions](github-permissions.md#protected-branch-mode-github-app).

**Q9: Do I need the `[M1-010]` prefix in issue titles?**
A: No. The `[M?-nnn]` prefix (e.g., `[M1-010]`) gives an issue a stable, human-readable identity key used in session tracking, dependency resolution, and logs. If your title has no prefix, the orchestrator automatically falls back to the GitHub issue number (e.g., `42`) as the identity. Everything works either way — sessions, the dashboard, dependencies, and filtering all function normally. Use the prefix when you want milestone-scoped naming or cross-references like `Depends-on: M1-010`; skip it if you're referencing dependencies by issue number (`Depends-on: #42`) or just getting started. See the [Creating Issues for Agents](tutorial.md#issue-identity-keys) section for details.

## Everyday Configuration

**Q10: How do I cap concurrency or change timeouts?**
A: Use `execution.concurrency.max_concurrent_sessions` and `execution.concurrency.session_timeout_minutes`.

**Q11: How do I enable code review and set a default reviewer?**
A: Guided Control Center Setup enables `agent:reviewer` and the bounded local
review/rework loop by default. For a hand-written config, set
`review.enabled: true`, then set `review.default` to a reviewer agent label
(for example, `agent:reviewer`) defined under `agents`. Guided Setup also
defaults the tech-lead threshold to `1`, which reviews every approved PR;
change it to a larger batch size or `0` for manual and failure-triggered
tech-lead reviews only.

**Q12: Can I reference environment variables in config?**
A: Yes. Any string can use `${VAR}` substitution. If the variable is missing, config loading fails with a clear error pointing to the field.

**Q13: Why does validation fail because the worktree is "dirty," and can I relax it?**
A: The guard prevents a mismatch between what you validated and what you push. Adjust `validation.publish.dirty_check` to `unstaged` or `off` if you intentionally want to allow that risk.

## Using the System

**Q14: Can I access this from VS Code (or derivatives like Cursor)?**
A: Yes. See [VS Code Integration](vscode.md). VS Code derivatives that support extensions generally work the same way, as long as they can run the extension and have the `issue-orchestrator-mcp` entrypoint on your PATH.

**Q15: An issue failed. Now what?**
A: Start with the Control Center: open the issue, check the last agent message, and look for the `blocked`/`validation-failed`/`needs-human` labels. Use the Doctor panel to validate config and environment. If the failure is due to validation, re-run the validation command in the worktree, fix the errors, and re-run `coding-done`. For deeper troubleshooting, see [Troubleshooting](../development/TROUBLESHOOTING.md).

**Q16: My issue ran but was blocked. Now what?**
A: Read the agent's last comment and the `blocked` label reason. Provide the missing input, then re-queue the issue by removing the `blocked` label and re-applying the agent label.

**Q17: Why isn't my issue showing up?**
A: Check the Control Center's **Excluded** tab first. Issues can be excluded by filters (`filtering.label`, `filtering.milestone(s)`, `filtering.issue`, `filtering.exclude_labels`), missing agent labels, or missing milestones. If it's excluded, the UI explains why.

## Advanced / Later-Stage Usage

**Q18: How do I control tech lead issue labels and priority?**
A: Use `tech_lead.explicit_labels` to always apply labels, `tech_lead.inherit_labels` to copy labels from linked issues/PRs, and `tech_lead.priority` to add a specific priority label (for example, `priority:high`).

**Q19: How is the tech lead milestone chosen, and can I override it?**
A: `tech_lead.milestone_strategy.inherit_from_issues` pulls from linked issues by default. Set `tech_lead.milestone_strategy.explicit` to force a specific milestone.

**Q20: I run multiple orchestrators. How do I avoid collisions?**
A: Think in three cases:

**Case A: Two repos, two orchestrators (same machine).**
Each repo has its own config. No collision because each orchestrator is bound to a different repo.

**Case B: Multiple machines for the same repo.**
Only do this with the claims system enabled. Each machine must set a unique `claims.claimant_id` (for example via `${ORCHESTRATOR_ID}`). Claims add labels like `io:claimed` and enforce lease rules so only one machine works an issue at a time.

**Case C: Multiple orchestrators for the same repo on one machine (advanced).**
This is mainly for development/testing. Use `ui.instances` with `claims.enabled: true` so each instance has a unique claimant ID. Don't do it without claims.

**Q21: How do I tune the review + rework loop?**
A: Use `review.max_rework_cycles` to cap rework (default: 5). When review is enabled, a coder agent produces changes, then a reviewer agent evaluates them. If the reviewer requests changes, the orchestrator opens a rework cycle. This repeats until the reviewer approves or the max is reached, at which point the issue is escalated.

**Q22: How do I manage issue dependencies, and what restrictions apply?**
A: Put dependency lines in the issue body using `Depends-on:`. An issue is runnable only when **all** dependencies are closed.

There are two ways to reference a dependency:

| Syntax | How it resolves | Example |
|---|---|---|
| `Depends-on: #123` | Direct GitHub issue number lookup | Any issue by number |
| `Depends-on: M2-010` | External ID lookup — finds the issue with `[M2-010]` in its title | Milestone-scoped key |

Both formats are subject to the **same milestone restriction**: the dependency must be in the **same milestone** as the depending issue, or in the **foundation milestone** (configured via `milestones.foundation`, default `M0`). The `#` vs bare ID difference is only about how the issue is *located*, not which milestones are allowed. Cross-milestone dependencies are flagged `CROSS_MILESTONE` and block the issue.

**Identity key uniqueness**: The full external ID string is the key. `M1-010` and `M2-010` are different identities — the sequence number (`010`) is scoped to its milestone prefix, not globally unique.

Examples:

```text
Depends-on: #123                    # GitHub issue #123 (same milestone or M0)
Depends-on: org/other-repo#456      # Cross-repo dependency
Depends-on: M2-010                  # Issue with [M2-010] in its title
```

If a dependency violates the milestone rule, the issue is marked blocked with a dependency reason in the UI.

**Q23: What are common dependency syntax mistakes?**
A: Watch out for these:

| What you wrote | What happens | Fix |
|---|---|---|
| `Depends-on: [M2-010]` | **Silently ignored** — brackets are not part of the dependency syntax | `Depends-on: M2-010` (no brackets) |
| `Depends-on: #010` | Resolves to GitHub issue **#10** (leading zeros stripped) — probably not what you meant if you were thinking of external ID `M?-010` | Use `Depends-on: M1-010` for external IDs, `Depends-on: #10` for issue numbers |
| `Depends-on: 123` | **Silently ignored** — bare numbers without `#` or `M` prefix don't match | `Depends-on: #123` |

The brackets `[...]` are only used in the **title prefix** (e.g., `[M2-010] Fix bug`). In `Depends-on:` lines, always write the external ID bare: `Depends-on: M2-010`.

**Q24: A failed/blocked issue offers Retry, Reset & retry, and Reset & retry from scratch — what's the difference?**
A: All three re-queue the issue; they differ in how much state they tear down first (increasing blast radius).

| Action | What it does | Use when |
|---|---|---|
| **Retry** | Clears the blocking label and re-queues. It does **not** delete the worktree or branch up front — but it does **not** preserve all work either: on relaunch the worktree is refreshed onto the base branch, which **discards uncommitted changes** and, if the rebase onto `main` fails, **hard-resets to `origin/<base>` (dropping local commits)**. Cleanly-rebasing committed work survives; uncommitted or conflicting work does not. | A transient failure (crash, flaky tooling) where the committed work on the branch is fine to continue from. |
| **Reset & retry** | Tears down local **and** remote state before re-queueing: **deletes the worktree**, **deletes the issue's local and remote branches**, **removes all orchestrator-owned labels**, and **clears the saved label persistence and session/completed-today history**. It does **not** supersede open PRs or clear the timeline — so an open PR is left behind with its branch deleted (orphaned). | The attempt is a write-off and you want the branch rebuilt from base, with no open PR that needs closing cleanly. |
| **Reset & retry from scratch** | Everything *Reset & retry* does, **plus** it **supersedes (comments + closes) any open PRs** and **clears the timeline / pending-review state**, and it runs strictly (any failed teardown step aborts loudly instead of being best-effort). The issue returns to a pristine "never attempted" state; a future attempt starts from a fresh branch off the base. | You want the prior attempt fully invalidated — including its open PR closed — and a clean-room redo. |

Rule of thumb: **Retry** resumes on the same branch (but uncommitted/conflicting local changes are dropped on relaunch); **Reset & retry** discards the local attempt and rebuilds the branch (note it orphans an open PR by deleting its branch); **Reset & retry from scratch** also closes open PRs and wipes history/timeline. Note that **both reset variants delete the worktree and the remote branch** — the difference is that "from scratch" additionally supersedes PRs and clears the timeline, and fails loudly rather than best-effort.

**Q25: In what order does the orchestrator pick up issues?**
A: There are two levels.

**By work type** (highest priority first): Reviews → Retrospective reviews → Reworks → Validation retries → Tech Lead → **New issues**. Completed work is reviewed/reworked before new coding starts, so a new issue only launches with whatever capacity is left after the higher tiers.

**Among ready new issues**, by this composite key (the first difference decides):

1. **Milestone** — `milestones.sort` (default `milestone_number`: ascending by the number in the milestone name, so `M5` before `M6`; an issue with no milestone sorts last). An explicit `milestones.order` list overrides this.
2. **Priority tier** — taken from an optional `[P<n>-nnn]` **title prefix** (`P0` is highest … `P9` lowest). Issues without that prefix all get `scheduling.default_priority_tier` (default `1`).
3. **Sequence** — the `nnn` from that title prefix.
4. **Issue number** — ascending, as the final tie-breaker.

An issue is only *eligible* in the first place once its `Depends-on:` dependencies are closed (see Q22).

**Gotcha:** the `priority:high` / `priority:medium` / `priority:low` **labels do not affect this order** — they exist for human/tech lead organization. Scheduling priority comes from the milestone and the `[P<n>-nnn]` title prefix, not the labels. So two issues in the same milestone with no `[P…]` prefix run in **issue-number order**, regardless of their priority labels — a newer issue (higher number) runs later even if it's labeled `priority:high`.
