# ADR 0031: Tech Lead as a tech lead with graduated, config-scoped authority

**Status:** Accepted
**Date:** 2026-07-10 (amended 2026-07-11: §2 gated-issue surfacing, #6778;
2026-07-14: §4 reaction model, #6780; accepted 2026-07-23: the model shipped —
typed decision artifact, board-snapshot observation surface, periodic + storm
health-review triggers, and per-action graduated authority are live, with the
``reset_retry`` executor wired (#6764) and act-level ``execute`` startup-guarded;
amended 2026-08-19: direct ``kill_hung_session`` authority wired with exact
session-generation revalidation)
**Milestone:** P1
**Tracks:** Issues #6760, #6761, #6762, #6763, #6764, #6778, #6780

## TL;DR

The tech lead facility was conceived as a tech lead — an agent that periodically
looks at groups of jobs, spots systemic problems ("five sessions are hanging
because of X"), and gets X fixed. What shipped is narrower and partly
disconnected: a batch PR-labeler whose findings go nowhere, plus a
failure-investigation path whose diagnosis evaporates on session exit. We fix
this **not** with a new subsystem but by giving tech lead the three organs it is
missing: an **output channel** (a typed decision artifact the orchestrator
validates and executes, mirroring the review-exchange contract), an
**observation surface** (a board-snapshot manifest of orchestrator state,
extending the existing PR-manifest pattern), and a **periodic trigger** (an
interval-driven health review). Authority is **graduated in config, per action
type**: the agent always proposes its full action set; configuration decides
which proposals the orchestrator executes and which it merely surfaces as
*would-have-done*. Trust becomes a dial the operator turns, not a code change.

## Context

Today tech lead has two triggers and no periodic behavior:

1. **Batch PR review.** When `tech_lead_review_threshold` PRs carry the
   `code-reviewed` label, the planner creates a "Batch Review" issue
   (`fact_gatherer.gather_tech_lead_facts()` →
   `planner._plan_tech_lead_issue_creation()`). The session receives a manifest of
   pre-downloaded PR diffs/metadata (`TechLeadManifestBuilder` +
   `ManifestDownloader` port). On completion the orchestrator performs exactly
   one act: adding `tech-lead-reviewed`/`tech-lead-failed` to the manifest PRs
   (`completion_action_planner._generate_tech_lead_actions()`). The findings the
   prompt asks for — "identify patterns and systemic issues" — have no channel:
   no comment, no issue, no report artifact. The prompt explicitly forbids the
   agent from creating them itself.

2. **Failure investigation.** Failed/timed-out sessions are queued
   (`planner._plan_discovered_failures()`, gated on
   `tech_lead_review_on_failure`) and launched as tech lead sessions. These have no
   manifest, so completion produces **nothing** orchestrator-side. The
   diagnosis is write-only.

Three further defects block building on this foundation:

- `TechLeadWorkflow`'s batch-trigger engine (`should_trigger_batch_tech_lead`, the
  30-minute cooldown, `BatchTechLeadDecision`) is dead code — exercised only by
  unit tests, never called in production. `TECH_LEAD_BATCH_TRIGGERED` never fires.
- Three prompt variants disagree on data source, permissions, and completion
  verb; the wizard-generated one promises orchestrator behavior (comment
  posting, label flips) that `_generate_tech_lead_actions()` does not perform.
- The agent's inputs cannot support the vision. It sees PR diffs or a one-line
  failure title — never session states/ages, blocked-queue reasons, timeline
  events, or logs, which is where hang-class and infrastructure-class problems
  actually show up.

Existing decisions constrain the fix:

- **Agent intent, orchestrator authority.** Agents express intent in records
  the orchestrator validates as untrusted input; agents never push, merge,
  mutate labels, or create issues directly. Any tech-lead "action" must be a
  *proposal* executed by the orchestrator.
- **ADR-0013 (labels as crash-safe truth)** — tech lead state transitions remain
  label-driven and restart-safe.
- **The review-exchange artifact contract** (review-report.md +
  review-decision.json, ADR-0027 lineage) already established the house
  pattern for "agent writes paired human/machine artifacts; the JSON is the
  authoritative contract." We reuse it rather than inventing a second shape.
- **Issues drive work.** The operator's actuator is the issue tracker; a
  tech-lead agent whose primary output is *well-formed issues* feeds its
  findings back into the same orchestration loop that fixes them.

## Decision

### 1. Output channel: a typed tech lead decision artifact

Tech Lead sessions complete by writing a paired artifact set, mirroring the
review exchange:

- **`tech-lead-report.md`** — the human-readable tech-lead report.
- **`tech-lead-decision.json`** — the authoritative contract, validated as
  untrusted input at completion time.

The decision carries **typed findings** (each with a classification —
`infra | task | agent | systemic` — and evidence references into the inputs it
was given) and **typed proposed actions**:

| Action type | Meaning | Default authority |
|---|---|---|
| `post_comment` | Diagnosis comment on an issue/PR | `execute` |
| `create_issue` | File a follow-up issue (labels, milestone per `tech_lead:` config) | `execute` |
| `escalate_to_human` | Route to the needs-human surface | `execute` (floor: cannot be disabled) |
| `flag_pattern` | Open/append a durable pattern case-file issue for a cross-job pattern (amended by #6781); requires a `pattern_signature` | `execute` |
| `reset_retry` | Reset-and-retry an issue from scratch (executor wired — #6764 first slice) | `propose` |
| `kill_hung_session` | Terminate the exact observed stuck session generation (executor wired) | `propose` |

The orchestrator parses the decision on session completion, applies the
authority filter (§2), executes allowed actions through the existing
action/applier vocabulary, and surfaces the rest. Malformed or contract-violating
decisions fail loudly: the session is marked tech-lead-failed and the parse error
is preserved. Completion verbs stay `coding-done completed|blocked` — the
artifact, not the CLI flags, carries the structure.

### 2. Graduated authority lives in configuration, per action type

```yaml
tech_lead:
  enabled: true                  # master switch for all new tech-lead work
  authority:
    post_comment: execute        # execute | propose
    create_issue: execute
    flag_pattern: execute
    reset_retry: propose         # shadow mode
    kill_hung_session: propose
```

Semantics:

- The agent **always proposes its full action set**; prompts do not change as
  trust grows. Graduation is flipping `propose` → `execute` in config (a
  settings-UI toggle, since these keys are in the settings schema).
- `propose` on `post_comment`/`flag_pattern` is **shadow mode**: the action is
  recorded visibly — in the report, as a structured event, and on the
  escalation surface — as *would-have-done*, giving the operator an audit
  trail to compare against their own judgment before granting authority.
- **Gated issues (amended by #6778).** Consequential proposals surface as
  *gated GitHub issues* instead of shadow records: `create_issue` proposals
  under `propose` authority are created carrying the `proposed-tech-lead` label,
  and act-level proposals (`reset_retry`/`kill_hung_session` under `propose`)
  become gated
  proposal issues. **Removing `proposed-tech-lead` is per-instance approval**:
  a proposed work issue flows into normal scheduling; an act-level proposal
  triggers execution of the **stored op** recorded orchestrator-side at
  creation (authority-store pattern, keyed by issue number, create-once).
  The issue body is human documentation only and is never re-parsed as a
  command — what the approver read and delabeled is exactly what runs. The
  scheduler's blocking-label layer excludes gate-labeled issues from pickup,
  and `proposed-tech-lead` joins the protected/orchestrator-owned label family
  (agents cannot propose or strip it). Ledger hygiene: one open proposal per
  (op, target); re-proposals comment on the existing issue. Ops execute at
  most once — the op row is discarded after terminal handling (outcome
  comment + close on execution, or stale-downgrade comment + close).
  Per-instance approval and config-level trust coexist.
- **Durable pattern case files (amended by #6781).** `flag_pattern` under
  `execute` is no longer event-only. Each flag_pattern action carries a
  required `pattern_signature` (a short stable slug; a decision without one is
  rejected). The orchestrator keeps a durable case-file ledger keyed by that
  signature: the first time a signature is observed it opens a **pattern
  case-file issue** (create-once, keyed by signature), and every repeat
  observation appends an **evidence comment** to that same case file rather
  than opening a new one. Evidence therefore *accrues* on one issue per
  pattern, and the open case files are projected into the board snapshot (§3)
  and the local tech lead board so the periodic health review (§4) can mine
  accumulated cross-job evidence. The `mode="pattern"` trace event still fires.
  Under `propose`, `flag_pattern` stays a shadow *would-have-done* record and
  opens no case file.
- Per-action flags, not a level scale: trust is not linear. An operator may
  trust issue-filing for months before trusting session-killing.
- Fail-safe: anything that mutates orchestrator runtime state defaults to
  `propose`. Setting `execute` on an action type whose executor is not yet
  wired (§5) is a **startup configuration error**, never a silent no-op.
  (Both current act-level actions are wired, so `execute` is honored with
  operation-specific execution-time revalidation.)
- Execution-time re-validation: act-level proposals are executed only if their
  recorded preconditions still hold (the board may have moved since the agent
  wrote the decision); otherwise they downgrade to surfaced proposals with an
  event.

### 3. Observation surface: the board-snapshot manifest

The manifest pattern extends beyond PR diffs. Tech Lead sessions receive, in
their `tech-lead-data/` directory, a typed **board snapshot**:

- active sessions (type, state, age, issue, terminal),
- pending/blocked queues with reasons,
- recent failures with paths to session artifacts and failure diagnoses —
  board CONTEXT, never act-level authority (see §4),
- the `problem_cohort` a health review owns act-level authority over (empty
  for every other flavor, and for a periodic health review),
- recent timeline extracts for affected issues,
- an orchestrator log tail.

All board data is local state — no new GitHub API traffic. Failure
investigation sessions, which today receive nothing, get the snapshot scoped
to the failed issue plus board context; batch sessions get it alongside the PR
manifest. The canonical prompt documents the layout; the agent stays
sandbox-compatible (reads local files, never queries GitHub).

### 4. Reaction triggers: investigations and health reviews

`tech_lead.health_review.interval_minutes` (absent/0 = disabled) drives a
planner-side trigger: when the interval elapses and no health review is active
or pending, queue a tech lead session of flavor `health-review` carrying the
board snapshot. The last-run marker is persisted so restarts do not
double-fire. Capacity/pause gating reuses `TechLeadWorkflow.should_launch_tech_lead()`.
Health-review completions flow through the decision artifact — there is no PR
manifest to label.

The interval is a periodic floor, not the only reaction trigger. Session
completion records `BLOCKED` alongside `FAILED` and `TIMED_OUT` as a typed,
timestamped problem fact. One deterministic reaction-policy owner classifies
the fact using the existing dependency evaluator and reverse dependency graph:

- a plain block on a tracked open dependency is explained healthy waiting and
  launches no investigation;
- a `blocked-failed` result, a dependency-satisfied-but-stuck issue, or a block
  with no tracked open dependency is unexplained; when that issue has
  downstream dependents, it queues an immediate failure investigation ahead
  of ordinary issue pickup;
- `tech_lead.health_review.storm_threshold` problem issues observed inside
  `tech_lead.health_review.storm_window_minutes` suppress their individual
  investigations and create one immediate, unscheduled health review. A zero
  threshold disables storm escalation without changing the interval trigger.

Suppression is bound to persistence, never merely to the decision to escalate.
The cohort is queued as individual investigations first, and only the anchor's
intake — the one owner that knows the anchor was actually created — retires the
investigations it supersedes. Every path that leaves the cohort without an
anchor (an open or pending health review, no capacity, a failed create, the
apply-time tech lead cooldown) therefore leaves the investigations queued, and
they consolidate into one health review on a later tick. A paused tick applies
nothing, so it retains its discovered facts instead of clearing them. A problem
is discovered exactly once, so any suppression not matched by a persisted
cohort would drop it permanently.

A storm-created anchor owns a typed problem cohort, persisted durably at
anchor creation and rehydrated by startup recovery, so the grant survives a
restart between creation and launch. The queued item hands that cohort across
the launch boundary as a `TechLeadLaunchScope`, and the orchestrator records
`TechLeadLaunchAuthority.problem_issue_numbers` from that grant, outside the
agent-writable worktree.

Authority is NOT inferred from the board snapshot. That snapshot's
`recent_failures` is deliberately broad CONTEXT — it merges the live failure
buffer, every pending failure investigation, and every pending cohort — so
reading authority back out of it widened a review's act-level scope to
unrelated issues that merely happened to be failing at launch, and handed a
periodic review a cohort it should never have. The snapshot therefore carries
the grant on its own dedicated `problem_cohort` surface, distinct from the
failures it displays.

A health review may issue act-level `reset_retry`/`kill_hung_session`
proposals only for that immutable cohort; general comments and escalations
remain anchor-scoped. A periodic health review owns no cohort: it walks the
board and proposes, but acts on nothing. Completion re-reads the worktree
snapshot's cohort surface solely as tamper evidence and rejects divergence
from the recorded authority; context failures exceeding the grant are expected
and are not tampering. Execution-time precondition checks still apply
independently to each cohort action.

### 4a. Finding promotion: the actuation lane for case files (#6957 amendment)

Pattern case files (#6781) accrue evidence but had no way to become work: a
correctly diagnosed orchestrator bug sat in a case file until a human noticed
the symptoms and hand-carried the finding into a fix, and
`tech_lead_shipped_fixes` never had a row. §2's gated op proposals already
solve per-instance consent for consequential tech-lead *acts*; this is the
equivalent lane for *findings*.

Configuration lives under `tech_lead.findings`:

```yaml
tech_lead:
  findings:
    promote: gated            # off | gated | auto   (default: gated)
    min_evidence: 2           # observations before promotion eligibility
    max_open_promoted: 3      # per target repo, cap on in-flight promotions
    route:                    # area label -> target that owns the fix
      completion-pipeline: issue-orchestrator/issue-orchestrator
      review-exchange:        # a target whose queue filters on its own labels
        repo: issue-orchestrator/issue-orchestrator
        scope_label: io-scope
        agent_label: agent:backend
      default: self
```

A route names more than a repository: an issue is only RUNNABLE in its target
when it carries that target's scheduling labels. A `self` route inherits the
managed repo's own contract (`filtering.label` plus
`review.tech_lead_follow_up_agent`) and may not redeclare it; a foreign target
declares its own, because the source repo's scope label means nothing there.

Lifecycle: **flag** (unchanged) → **promote** → **gate** → **run** (the target
repo's own pipeline, unchanged) → **close the loop**.

On the tick a signature crosses `min_evidence`, has no promotion row, fits its
routed target's cap, and is classified `fix:code`, the orchestrator files ONE
issue in the routed repo carrying `proposed-tech-lead` plus the route's full
scheduling contract (worker agent + scope label) and the area label. Removing
the gate label is the operator's whole approval — the issue is then discoverable
by the target's unchanged pipeline with nothing else missing. Closing the issue
is a decline, recorded permanently so it is never re-filed. When a promoted
issue closes and the pull request GitHub records as its CLOSER is merged, the
source orchestrator writes the `tech_lead_shipped_fixes` row, comments the fix
reference on the case file, and closes it. Merely being mentioned by a merged
PR is not closing linkage and never counts as a shipped fix — and only the
NEWEST close counts, so an issue closed by a merge, reopened, then closed by
hand is a decline.

`min_evidence` counts DISTINCT observations. Each `flag_pattern` observation
carries a stable identity — its source run, session, and decision action — and
the durable ledger records it create-once, so replaying a partially applied
decision after a crash can repeat an evidence comment but can never advance the
count twice.

Both cross-system creations — a case file, and a promotion filing — record a
durable **creation intent before the remote create** and finalize from THAT.
The GitHub issue exists before its ledger row does, and a marker lookup can find
the orphan again but cannot say which command wrote it; the retry is not
guaranteed to be the same command, because an ordinary finalization failure is
just a failed action and the next observation of that signature can be the one
that recovers it. So the intent carries what only the original command knew: for
a case file, the body's observation identity, classification, area, and
diagnosis; for a promotion, the evidence watermark its body documents. The
recovering action is then handled separately, as an ordinary append. An orphan
with no intent is durable-state loss rather than a crash window, and the lane
stops rather than guessing its metadata.

Because the intent is durable and `route` is ordinary editable configuration, an
operator can re-point an area while a filing is in flight — without the
signature's area changing at all. A recovery-only lookup in the intent's own
repo settles that: if the old route's issue exists it IS the promotion (one
signature promotes exactly once, so the recorded target stays authoritative and
the new route does not apply to it); if it is proven absent the stale intent is
retired and the current route takes over; if the lookup fails, nothing is
created and the next tick tries again. A re-routed signature is never stranded.

A signature's `fix_class` and `area` are immutable once recorded: an
unclassified row may be upgraded once, identical values are idempotent, and a
conflicting classification is rejected — before any action is produced, so no
evidence comment or sibling effect from that decision is ever applied. Planning
receives the full durable row precisely so the conflict is caught there rather
than mid-write.

Constraints that make this safe to leave on:

- **`fix:code` only.** The tech lead classifies each `flag_pattern` at flag
  time. A `fix:human` or unclassified finding is never promoted — a
  human-gated problem made runnable manufactures doomed rework.
- **At most one issue per signature, ever**, in either direction: a durable
  ledger keyed by signature carries promoted/declined/shipped, and later
  observations comment on the promoted issue rather than re-filing.
- **`max_open_promoted` is per-target work-in-progress backpressure**, and it
  bounds the lane's API cost too — loop closure polls at most that many issues
  per target PER TICK, enforced by a read budget rather than implied by the
  cap. The durable ledger outlives the setting, so lowering the cap (or
  restarting after a larger cohort was filed) slows coverage — the budget
  rotates so no in-flight promotion starves — instead of exceeding the budget.
- **Only reads cross repositories.** Every write the source orchestrator makes
  (case-file comment/close, shipped-fix memory, ledger state) lands in its own
  repo; the cross-repo writes are exactly two — create the issue, comment on
  it — behind a narrow port with no approve/merge/close capability.
- **Route targets are validated at startup**, not at promotion time: a target
  the token cannot file issues in is a loud doctor error rather than a lost
  actuation on the tick a pattern finally firms up.
- **One activation/readiness decision**, consumed by configuration validation,
  doctor, fact gathering, and route resolution alike. The lane is active only
  when the master tech-lead workflow is enabled, the promotion mode is not
  `off`, and a repository is configured. Turning `tech_lead.enabled` off stops
  the lane completely, including its cross-repo reads, and the durable rows are
  kept so re-enabling resumes where it stopped. An active lane's dependencies
  (a follow-up worker agent for any route that carries this repo's label) are
  startup errors, never tick-time exceptions.
- **`self`-routed promotions face the managed repo's own gates.** They carry
  its scope label so they are discoverable, and every other gate (dependencies,
  claims, review) applies unchanged. Promotion files issues, full stop.

### 5. Sequencing and scope boundaries

Hygiene precedes construction: the dead batch-trigger engine and its
false-confidence tests are deleted, the never-emitted `TECH_LEAD_BATCH_TRIGGERED`
event is removed, the missing tech lead keys join the settings schema, and the three
prompt variants collapse to one manifest-based contract (#6760). The decision
artifact and authority filter land next (#6761), then the board snapshot
(#6762), then the periodic trigger (#6763). Act-level executor wiring
(`reset_retry`, `kill_hung_session`) is deliberately last (#6764): the
vocabulary and shadow-mode surfacing ship first, so operators accumulate
would-have-done evidence before any execute flag exists to flip.

Non-goals: the tech lead agent never edits code, never pushes, never merges, and
never mutates labels or GitHub state directly — its writes are the two
artifact files; everything else is orchestrator-executed proposal. Dashboard
work is limited to surfacing the report/decision through the existing
issue-artifact pattern.

## Consequences

- Failure investigation becomes useful immediately: every failed session can
  end in a diagnosis comment on its issue, classified and evidence-linked —
  the first concrete slice of operator workload actually replaced.
- The operator's trust boundary is explicit, inspectable, and reversible; an
  incident response can be "set everything back to propose" in one config
  edit.
- The decision artifact adds a second consumer of the paired-artifact pattern,
  pressuring it toward a shared owner abstraction if a third appears
  (retrospective review is the likely candidate).
- Shadow mode produces structured would-have-done data; if we later want to
  score the agent's judgment against operator actions, the record already
  exists.
- Deleting the dead cooldown machinery removes the misleading tests; the
  periodic trigger re-introduces time-based logic wired and tested honestly.
