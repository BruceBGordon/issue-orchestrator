# ADR 0033: Tech-lead run representation — shared coordination, local visibility

**Status:** Proposed
**Date:** 2026-07-19
**Tracking issue:** #6858
**Related:** ADR-0031 (tech lead / graduated authority), ADR-0013 (health-review marker + labels-as-truth)

## Context

The tech-lead (ADR-0031; the agent currently named "tech lead") has become a
first-class actor: its own reserved concurrency slot, graduated authority, and a
whole-system health remit (failure investigations, periodic health reviews,
batch reviews). But how a tech-lead *run* is represented is unresolved, and it
collides with two hard requirements.

Today's representation is inconsistent:

- health reviews and batch reviews run on a real GitHub **anchor issue** (e.g.
  "Health Review — walk the floor"), carrying a marker label that is the
  crash-safe dedup/recovery key (ADR-0013, labels-as-truth);
- failure investigations run *on the focus issue itself* (or, after the
  decoupled-scratch change, in a throwaway worktree keyed to the focus issue).

Two forces constrain any redesign:

1. **Multi-client footprint.** The orchestrator serves arbitrary *client* repos,
   not just our own. Minting a real GitHub issue per run spams the client's
   tracker with our internal bookkeeping — notifications, metrics, webhooks,
   optics. Tolerable when dogfooding our own repo; a product defect on a
   client's board.
2. **Multi-orchestrator coordination.** More than one orchestrator may run
   against a single repo, possibly on different machines. Exactly one must own
   and launch a given run — the same "only one grabs the issue" claim problem we
   already solve for issues (`test_claim_coordination`). Crucially, separate
   orchestrators share **only the GitHub repo** — no common database or
   filesystem — so coordination must ride shared GitHub truth. A record kept in
   one orchestrator's local store is invisible to its peers.

These pull in opposite directions: footprint wants the run *off* GitHub;
coordination requires a *shared* (i.e. GitHub) point.

## Decision

**Separate coordination from visibility; they have different owners.**

- **Launch coordination = shared / GitHub.** Which orchestrator owns and
  launches a run rides GitHub labels-as-truth, with the same claim +
  stale-detection rigor as issue claims (not the current scan-then-create
  dedup, which races). **Minimize the shared footprint** — a thin claim, not a
  fat issue-per-run.
- **Run record / visibility = local.** The session log, the evidence-map the run
  saw, its decision, and the proposals it filed live on the *winning
  orchestrator's* dashboard — a "tech-lead activity" surface. The data already
  exists (the session `terminal-recording.jsonl`, the same capture session-replay
  uses). Peers need to know *"claimed by X until T,"* not the full session.
- **Unify the three flavors** under one run model that **references** its subject
  (a focus issue, a PR manifest, or the whole board) rather than running *as* it
  — the identity-layer analogue of decoupled-scratch: investigate the subject
  without *being* the subject.
- **Two surfaces, two owners.** The dashboard is *our* surface — the run's
  existence and detail live there. GitHub is the *client's* surface — only the
  run's **output** (proposals, `proposed-tech-lead` issues) belongs there, by
  design. Any GitHub footprint for the run beyond the thin claim is
  **config-opt-in** (e.g. a client who wants the health-review report posted as
  an issue).

## Consequences

- **Multi-orchestrator-correct**: a single owner via a shared claim; no duplicate
  runs across instances.
- **Client board stays clean**: no per-run bookkeeping issues; the tool's
  activity is visible on the tool's own dashboard, not the client's tracker.
- The current real-anchor approach is *already* multi-orchestrator-correct (its
  marker label coordinates via GitHub) — it is simply heavier on the board than
  necessary. This ADR keeps the coordination and sheds the weight.
- New work (tracked in #6858): the minimal shared-coordination mechanism; claim +
  stale-detection on run-launch; a local run-record store + dashboard view;
  folding failure-investigation into the unified run model; a footprint config
  knob.

## Implementation status

- **Shared coordination — done (#6994).** `TechLeadRunLedgerStore` is the
  minimal shared mechanism: one compare-and-swap ledger cell, not an issue per
  run. `TechLeadRunOwnership` applies claim + lease + stale detection, and
  `TechLeadLaunchAuthority` is the single gate a session may start behind. The
  three flavors are unified under one run model whose scopes
  (`GlobalHealthReviewScope`, `GlobalBatchReviewScope`,
  `IssueInvestigationScope`) *reference* their subject.
- **Local visibility — done (#6858).** `TechLeadRunRecord` is the run as the
  winning engine remembers it: scope, phase, what it produced, and the session
  run identity a replay drill-down needs. It is opened by the launch authority,
  concluded at completion finalization beside the launch-authority retention
  owner, and stored in its own SQLite file (`tech_lead_runs.sqlite`) —
  deliberately not in the authority store, whose rows are load-bearing and
  deleted at each run's terminal. `TechLeadActivityView` projects it onto the
  dashboard's "Tech lead activity" panel. Writes are best-effort by contract:
  the run's product is its proposals, and losing the receipt must never lose
  the run.

## Open questions

- **Footprint config knob.** The whole-repository anchor issue is still a real
  GitHub object. Now that the run record exists, the anchor's remaining job is
  coordination — which the ledger already does — so retiring it (or making it
  config-opt-in, per the decision above) is the next step. Tracked separately.

## Alternatives considered

- **Real GitHub issue per run (current anchors, extended to all flavors).**
  Coordination-correct and maximally expedient, but pollutes every client's
  board. Rejected as the *end state*; retained as the working stepping-stone.
- **Purely local / virtual run (no GitHub object).** Clean board, but a local
  record is invisible to peer orchestrators, so it **cannot coordinate across
  instances** — two orchestrators would both launch. Rejected as
  multi-orchestrator-unsafe.
