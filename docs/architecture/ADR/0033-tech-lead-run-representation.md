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
  run identity a replay drill-down needs. It is opened by the launch authority
  and concluded from the **post-apply effective terminal status** — the same
  value the terminal trace event, the cached state machine and the session
  history are finalized from — so a run whose mandated action failed is never
  recorded as completed. Its subject comes from the canonical run **scope**, and
  the bookkeeping anchor a whole-repository run was coordinated *through* is
  recorded separately as an anchor: the shared coordination half must not
  masquerade as the local subject.
  It is stored in its own SQLite file (`tech_lead_runs.sqlite`) — deliberately
  not in the authority store, whose rows are load-bearing and deleted at each
  run's terminal — registered in the repository SQLite registry for startup
  integrity checks, pragma enforcement and backups.
- **The artifacts, not just the summary — done (#6858).** A run writes its
  evidence map, decision, report and terminal recording inside its worktree, and
  a failure investigation's worktree is disposable scratch that completion always
  removes. `TechLeadRunArtifactArchive` therefore copies the run's inspectable set
  into an engine-owned directory
  (`.issue-orchestrator/state/tech-lead-runs/<run>/`) at the terminal seam,
  preserving the run-relative layout, and the record carries a typed
  `TechLeadRunArtifacts` locator. `TechLeadActivityView` publishes that as the
  dashboard's existing typed inspection commands (`open_session_recording`,
  `open_review_artifact`), so the panel's buttons route through the one lifecycle
  dispatcher and the browser never reconstructs a path. The whole activity
  payload — container, entry, and the command union — is declared on the UI
  OpenAPI boundary, so the generated Python and TypeScript clients can name what
  the browser consumes instead of receiving it as an untyped extra.
  The archive is a BOUNDED owner, because its source is agent-authored and its
  destination is the operator's state volume:
  - the walk is anchored on an open descriptor for the run directory and opens
    every component `O_NOFOLLOW` relative to its parent, streaming from the
    descriptor it validated — the same discipline
    `control/validation_record_containment.py` applies to a single agent-supplied
    file, extracted for trees as `infra/contained_artifact_copy.py`. Nothing is
    reopened by pathname, so a file or ancestor swapped under the walk cannot
    redirect a read outside the run, and the byte ceiling is enforced on bytes
    READ so an appender cannot outgrow its admission;
  - discovery is iterative, lazy, and capped on entries, directories and depth, so
    a pathological tree costs a refused branch rather than the engine; one
    unreadable child never costs the artifacts already admitted;
  - per-file/aggregate/count caps apply and log what they drop;
  - a copy is staged in a PID-owned sibling directory and swapped in only when
    complete, so a failed retry cannot destroy a complete receipt.
    `reconcile()` restores a receipt a crash left renamed aside and reclaims
    scratch owned by processes that are gone — never a live engine's active stage;
  - retention keeps the newest runs while REPORTING what it removed, and the
    activity owner retires the matching record locators in the same breath, so no
    row advertises a drill-down into a directory that is gone.
- Writes on both are best-effort by contract: the run's product is its proposals,
  and losing the receipt must never lose the run. Because the *store* cannot know
  whether losing durability is acceptable, it fails loudly on an unusable
  database and the composition root makes the call — logging the loss and
  selecting the in-memory implementation, so a read-only state directory can
  never stop the Repository Engine from starting.

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
