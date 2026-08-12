# Tech Lead Data Sources Contract

This document defines the data sources available to tech lead agents, how to
access them, and safety rules for their use.

Tech Lead agents operate on **local files only**. The orchestrator pre-fetches
everything you need before the session starts; you never call `gh` or the
GitHub API, and you never mutate GitHub state (comments, labels, issues, PRs).
The orchestrator executes all GitHub operations after you complete.

## Data Sources

### Tech Lead Manifest (Authoritative)

The primary input. The orchestrator writes it into your session directory:

| Source | Access | What It Tells You |
|--------|--------|-------------------|
| Manifest | `cat "$ISSUE_ORCHESTRATOR_RUN_DIR/tech-lead-data/manifest.json"` | Which PRs to review, with local file names |
| PR metadata | `cat .../tech-lead-data/pr-{N}-meta.json` | Title, body, branch, author |
| PR diff | `cat .../tech-lead-data/pr-{N}-diff.txt` | Actual code changes |

The manifest is the definitive list of PRs in scope. Review exactly those PRs -
no more, no less. On success the orchestrator labels exactly these PRs.

### Board Snapshot (Authoritative)

Written at launch for both tech lead flavors - a point-in-time snapshot of
orchestrator state, not live state:

| Source | Access | What It Tells You |
|--------|--------|-------------------|
| Board snapshot | `cat "$ISSUE_ORCHESTRATOR_RUN_DIR/tech-lead-data/board-snapshot.json"` | Active sessions (type/state/age), pending queues with reasons, blocked issues, recent failures, per-issue timeline extracts, orchestrator log tail |

Batch reviews use it to spot cross-PR and systemic patterns worth
`flag_pattern`/`create_issue` proposals; failure investigations start from
their focus issue and use it for board context.

### Orchestrator Configuration (Authoritative)

| Source | Access | What It Tells You |
|--------|--------|-------------------|
| Config file | `cat .issue-orchestrator/config/modes/<mode>/*.yaml` | Agent definitions, timeouts, label names, review workflow |
| Agent prompts | `cat .prompts/<agent>.md` or path from config | What agents are instructed to do |

### Local Logs and Session Artifacts (Advisory)

Use for investigation; they may be incomplete, rotated, or stale.

| Source | Access | What It Tells You |
|--------|--------|-------------------|
| Orchestrator log | `cat ~/.issue-orchestrator.log` | Infrastructure errors, label failures, session lifecycle |
| State file | `cat .issue-orchestrator/state.json` | Session history, pending reviews (may be stale) |
| Session run dirs | `ls .issue-orchestrator/sessions/` | Completion records, validation outcomes |

### Worktree State (Advisory)

| Source | Access | What It Tells You |
|--------|--------|-------------------|
| Git status | `git status` in this worktree | Uncommitted work, branch state |
| Git log | `git log` in this worktree | What was committed for this session |

## Safety Rules

### Never Do

1. **Don't call `gh`** - not for reads, not for writes; all PR data you need is local
2. **Don't post comments, edit labels, or create issues/PRs** - the orchestrator owns all GitHub mutations
3. **Don't merge or approve PRs** - those are human decisions
4. **Don't delete worktrees** - they may contain uncommitted work

### Always Do

1. **Start from the manifest** - it is the definitive review scope
2. **Verify advisory sources against the manifest and config** before drawing conclusions
3. **Make improvements locally** - edit prompts/docs in this worktree and commit; the orchestrator publishes your branch
4. **Report everything else in your completion record** - `coding-done completed --implementation "..." --problems "..."`

## What Happens After You Complete

- `coding-done completed`: the orchestrator adds the configured
  `tech_lead_reviewed_label` (default `tech-lead-reviewed`) to every PR in the
  manifest and publishes any commits on your branch.
- Session failure: manifest PRs get the `tech_lead_failed_label`
  (default `tech-lead-failed`).
- No comments are posted, no other labels are flipped, and no issues are
  created on your behalf.
