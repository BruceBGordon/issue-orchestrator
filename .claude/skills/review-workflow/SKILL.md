---
name: review-workflow
description: Understand the code review pipeline, tech lead batch reviews, failure investigation, and rework cycles. Use when working on review labels, code review agents, tech lead configuration, or the needs-rework flow.
---

# Review Workflow

This skill provides context for the multi-stage review pipeline.

## When to Use

- Working on code review pipeline
- Configuring tech lead batch reviews
- Understanding rework cycles
- Working with review-related labels
- Failure investigation flow

## Key Resources

Read this file for context:
- [Review Workflow](../../../docs/development/REVIEW_WORKFLOW.md) - Full workflow documentation

## Review Checklist: Cross-Cutting Policy

When reviewing changes that touch review/rework/tech lead/session launch paths, scan for:
- **Duplicated policy checks** spread across planner/launcher/execution (claims, provider availability, labels, gating).
- **Split responsibility** where correctness relies on multiple call sites remembering to call a check.
- **Label lifecycle consistency**: add/remove paths should be symmetric and account for all session types.
- **New entrypoints** that bypass existing gates or helpers.

If any of the above appear, recommend centralizing the policy into a shared helper/module and reusing it.

Review for the strongest bounded design, not merely for a working diff. If a direct fix bypasses an existing owner abstraction or should create a small owner/port/command abstraction, request that fix in the PR. Treat this as `Design Smell` when it risks drift and `Correctness Risk` when an invariant can be bypassed.

## Review Decision Policy (Strict)

Use a hardline merge bar. Do not soften medium-or-higher concerns into an approval.

- **Only nits may remain unaddressed at merge time.**
- If a comment identifies correctness, reliability, safety, architecture, contract, test coverage, observability, or maintainability risk, it is **not a nit**.
- Concerns marked "verify", "worth checking", or "might be an issue" are not informational notes. They require confirmation before approval.
- If uncertain whether something is a nit, treat it as non-nit until proven otherwise.

### Allowed Outcomes

- **Approve**: all non-nit concerns are resolved in-code, or conclusively disproven with evidence.
- **Request changes**: any non-nit concern remains unresolved, unverified, or deferred.

### Not Allowed

- "Approve with comments" when comments include unresolved non-nits.
- Approving while asking for follow-up on medium/high-risk items.
- Downgrading meaningful concerns to "minor" to avoid blocking.

### Nits (Examples)

- Wording tweaks in comments/docs that do not affect behavior.
- Optional formatting/style preferences with no readability or maintainability impact.
- Non-substantive naming preferences where current naming is clear and consistent.

### Non-Nits (Always Blocking Until Resolved)

- Control-flow changes that could alter behavior.
- Potential runtime exceptions or missing fields/attributes.
- Data/contract/schema mismatches or payload bloat risks.
- Missing or weak tests for changed behavior.
- Architectural drift from ports/adapters, DI, or lifecycle boundaries.

## Review Artifact Contract

Before PR creation, review exchange output must include a paired artifact set:

- `review-report.md` for human review. It should read like a PR review and include blocker/nit item IDs.
- `review-decision.json` for orchestration. This is the authoritative no-nonsense contract.

The markdown and JSON must describe the same item IDs. The dashboard/E2E issue detail should expose the report as the primary visible action and keep the JSON as a secondary/menu action.

Review artifact UI/actions must follow the typed command / owner-port pattern. Add tests for producer-to-command content and command-to-UI rendered content, including the primary report action and secondary/menu JSON action.

The decision JSON must include `abstraction_review`. Use `status: "no_issues"` when the bounded owner/port/command shape is sound. Use `status: "changes_requested"` with `A1`, `A2`, ... findings when the coder should add or reuse a bounded abstraction in this PR. Approved decisions must not carry required abstraction changes. Use `status: "deferred"` only with an existing follow-up issue and include `follow_up_issue_url`.

Nits are classified in the same reviewer pass as blockers. Do not add a separate nit pass. If the active nit policy is `address`, an approved decision with only nits enters the normal coder rework loop before PR creation. `surface` shows nits without blocking; `ignore` keeps them in artifacts only.

## Persistent Review Exchange Lifecycle

The review exchange must treat the artifact contract as authoritative for a
completed turn. If a role writes a valid response/report/decision for its
current turn and then the process exits, that completed turn is still
successful. When a later turn for the same role is required, respawn that role
with the same worktree and pair-scoped artifact paths before sending the next
prompt. Classify `*_no_completion` only when the current turn exits or times
out before valid artifacts are available.

This matters for approved-with-nits flows: with `review.nits.default_policy:
address`, the reviewer may correctly approve with nits, the orchestrator routes
the nits back to the coder, and a fresh reviewer process may be needed for the
next review turn if the provider is one-shot.

## Review Pipeline

```
[Internal Review] (optional, coder-owned)
  Every coder turn spawns one internal reviewer and iterates until approved;
  blocked/needs-human on unavailable reviewer, unresolved findings, or round limit
       │
Work Agent completes and validation passes
       │
[Review Exchange] (default: via-local-loop)
  Coder/reviewer alternate locally until approved, max rounds reached, or no-progress limit reached
       │
   ┌───┴───┐
Approved   Changes Requested
   │            │
   │       Back to coder/reviewer exchange
   │
[Draft-PR Review] (when review.exchange.mode=via-draft-pr)
  Label: needs-code-review → code-reviewed OR needs-rework
  Rework loop uses rework-cycle labels up to max_rework_cycles
       │
[Tech Lead Review] (batch, threshold-triggered)
  Label: code-reviewed → tech-lead-reviewed
       │
   Manual merge


Session FAILED/BLOCKED/TIMEOUT
       │
[Failure Investigation] (if tech_lead_review_on_failure: true)
  - Tech Lead agent reviews what went wrong
  - Uses _plan_discovered_failures()
  - Helps identify patterns in failures
```

## Reset From Scratch Boundaries

Reset and retry from scratch is a hard lifecycle boundary, not a normal rework
cycle. The reset path must:

- Remove the local worktree and fail the reset if it remains on disk.
- Delete the local/remote issue branch, and fail the scratch reset if remote
  branch deletion reports failure.
- Close/comment any open orchestrator PRs for the issue as superseded. GitHub
  has no native "superseded" PR state, so this is represented as a comment plus
  closed PR.
- Clear stale pending review, rework, and cleanup queues for the issue and any
  superseded PRs before requeueing.
- Start the next coding session on a fresh branch from base, mark the session
  manifest as `reset_from_scratch`, and set a review-cache boundary timestamp.

Cached local review approvals may only be reused for the same unchanged head SHA
and only when their review-exchange summary and validation record are at or
after the active review-cache boundary. Scratch retries must not reuse review or
validation artifacts from runs before the scratch boundary.

## Key Runtime Touchpoints

| Method | Purpose |
|--------|---------|
| `control/workflows/review_workflow.py` | Review launch decision policy |
| `control/workflows/rework_workflow.py` | Rework launch/escalation decision policy |
| `control/workflows/tech_lead_workflow.py` | Failure/batch tech lead decision policy |
| `control/github_workflow.py` | Review/rework discovery and queueing logic |
| `control/session_launcher.py` | Review session launch path and review exchange setup |
| `control/session_rework_launcher.py` | Rework session launch path |
| `control/review_exchange_loop.py` | Local coder/reviewer exchange loop |
| `control/planner.py::_plan_discovered_failures()` | Queue tech lead to investigate failures |
| `control/tech_lead_ledger_planning.py` | One planning owner for everything the tech-lead durable ledgers drive |
| `control/tech_lead_finding_promotion.py` | Case file → gated runnable issue → shipped fix (#6957) |
| `infra/orchestrator.py` | Runtime facade that delegates to the review workflow helpers |

## Finding Promotion (#6957)

A pattern case file (`flag_pattern`, #6781) that firms up becomes real work
instead of a dead end. On the tick a signature crosses `min_evidence`
observations, is classified `fix:code`, has no promotion row, and fits its
routed target's in-flight cap, the orchestrator files ONE gated issue in the
repo its `area` routes to. Removing `proposed-tech-lead` is the operator's whole
approval; closing it is a permanent decline. When the promoted issue closes with
a merged PR, the source orchestrator records the shipped fix, comments the fix
reference on the case file, and closes it.

Invariants worth knowing before changing anything here:

- `fix:human` and unclassified findings are NEVER promoted.
- One promoted issue per signature, ever — the durable ledger (not the issue
  body) is the authority, and `declined`/`shipped` are permanent.
- Only READS cross repositories; every write lands in the repo that owns it.
- Route targets are validated by doctor at startup, not at promotion time.

## Configuration

```yaml
review:
  enabled: true
  default: "agent:reviewer"
  max_rework_cycles: 10

  internal:
    enabled: false
    max_rounds: 5
    instructions: ".io/internal-review.md"

  nits:
    default_policy: "surface"     # ignore | surface | address
    by_agent: {}                  # e.g. agent:frontend: address

  exchange:
    mode: "via-local-loop"       # via-local-loop | via-draft-pr | via-mcp | auto
    loop:
      max_rounds: 10
      max_no_progress: 2
      require_validation: true

  # Tech Lead
  tech_lead_review_agent: "agent:tech-lead"
  tech_lead_review_threshold: 5        # Batch review after N PRs
  tech_lead_review_on_failure: true    # Investigate failures (default: true)

tech_lead:
  findings:                            # Finding promotion (#6957)
    promote: "gated"                   # off | gated | auto
    min_evidence: 2                    # observations before eligibility
    max_open_promoted: 3               # in-flight cap per target repo
    route:
      completion-pipeline: "issue-orchestrator/issue-orchestrator"
      default: "self"
```
