# ADR 0034: SandboxScope — orchestrator-computed per-session agent sandbox

**Status:** Proposed
**Date:** 2026-07-19
**Tracking issue:** #6859
**Related:** ADR-0031 (tech lead), ADR-0033 / #6858 (tech-lead run representation)

## Context

Every claude-code agent (backend/frontend/mobile/tech lead) runs
`permission_mode: bypassPermissions` — full local machine access, no OS
isolation. The god-view (ADR-0031 evidence-map) makes the **tech-lead the
broadest-read agent**, which makes it the *worst* one to leave un-sandboxed
(broad read + any egress = an exfiltration channel). The current containment
(env-var scrubbing + hooks) is defense-in-depth, **not** a boundary — the code
says so explicitly (`execution/agent_runner_env.py`: "not isolation… same-user
agents share HOME… real isolation requires OS-level").

Two forces make this actionable now:

1. **Remove yolo** — a real safety requirement, and not just for the tech-lead:
   *every* agent runs yolo today.
2. **Enable a fair codex/claude-code A/B** for the tech-lead — codex's default
   `workspace-write` sandbox *blinds* the god-view (can't reach the substrate),
   and the two CLIs must run under the *same* posture for the comparison to
   measure reasoning rather than sandbox luck.

The tension we resolved: the god-view needs *broad* reads; a sandbox wants
*bounded* access. The insight is that **bounded is the security property, not
read-only** — and the substrate scope is already computed (the evidence-map).

## Decision

Introduce **`SandboxScope`** as a first-class owner in the **session-launch**
path, beside worktree-prep / decoupled-scratch / provider-args.

- The orchestrator **computes a per-session scope** (`read/write roots`,
  `egress`) from the **agent role**; a **provider adapter** translates it to
  each CLI's flags. It is applied to **all** agents — killing yolo system-wide.
  This is more *orchestrator-authoritative* than a static `bypassPermissions`
  in config: the orchestrator grants exactly what a session needs, per run.
- **Mechanism, symmetric across CLIs:** bounded `--add-dir` /
  `additionalDirectories` of the specific paths a role needs (read **and** write
  is acceptable — bounded is the property that matters), write confined to the
  worktree/scratch, **no arbitrary egress**. claude-code may *optionally* tighten
  reads to read-only (`sandbox.filesystem.allowRead/allowWrite`); not required.
- **Per-role policies:** coder → its worktree; reviewer → the PR worktree;
  **tech-lead → the evidence-map paths (state dir, dbs, run-dirs, base repo,
  logs) + scratch, no egress.** The evidence-map already enumerates the
  tech-lead's read set, so it *is* the read policy.
- **Linked-worktree Git metadata:** a worker or reviewer receives write access
  to its exact worktree plus only the linked Git administrative paths required
  to stage and commit that worktree. Shared configuration, hooks, packed
  objects, other refs, and other worktree records remain read-only. Claude Code
  and Codex consume the same provider-neutral resolved Git access policy.
- **Default placement:** new repository Setup proposes
  `../worktrees/<repository-directory>`, a dedicated peer collection outside
  the source checkout. The selected relative value and its resolved absolute
  path are shown before save and persisted explicitly.
- **Egress is a per-role flag** (`none | model-only | model+web`). The tech-lead
  defaults **model-only (no web)**: the broadest-read agent gets no egress, and
  web hypotheses become gated "explore" issues (see below). `model+web` is
  documented for later, or for a scoped worker that picks up an explore issue.
- **Credentials denied** in the sandbox (GITHUB_TOKEN, cloud creds) — turns the
  existing env-scrub into a real boundary.

### Why bounded-writable rather than read-only

The "an investigation must not mutate its subject" invariant is already enforced
by **decoupled-scratch** (the tech-lead runs in a throwaway worktree and reads
focus branches via *shared git objects*, not a writable checkout). Exfiltration
is killed by **no-egress**. So within a bounded, no-egress scope, writability
costs ~nothing (trusted first-party agent; the dbs are the orchestrator's own; a
corruption is a bug, not a breach). Bounded `--add-dir` is therefore *tighter*
than codex `disk-full-read-access` — it excludes `$HOME`, other repos, tixmeup —
so it is the better choice even though it is writable.

### Web = no egress for the tech-lead

The broadest-read agent is the worst to give an egress channel, and web research
belongs to *fixing* (the scoped worker's job), not *diagnosing* (the tech-lead's
job). So the tech-lead does not browse; when it hypothesizes an external cause it
files a gated "explore" issue describing what to research — the same
propose-don't-acquire pattern as "file an issue to instrument a missing signal."

## Consequences

- Yolo removed **system-wide**, not just for the tech-lead.
- Filesystem/network **reach becomes orchestrator-authoritative** and
  per-session, consistent with "agents carry intent, orchestrator has authority."
- The codex/claude-code A/B runs under the **same computed posture** —
  equivalence by construction.
- The evidence-map gains a second consumer: it defines both *what the tech-lead
  reads* and *what its sandbox allows*. Composes with decoupled-scratch (scratch
  = the write-root).
- The linked-worktree metadata resolver is another shared policy owner: provider
  adapters translate one exact Git access result instead of independently
  widening filesystem scope.
- Provider asymmetry to document: claude-code can bound reads read-only; codex's
  read is coarser (workspace or full-disk), so codex uses bounded writable
  `--add-dir`. Equivalent enough for the comparison; claude-code tighter in prod.

## Capability check (verified 2026-07-19)

- **claude-code:** real OS sandbox (macOS Seatbelt / Linux bubblewrap);
  `--add-dir`/`additionalDirectories` (r+w) or `sandbox.filesystem.allowRead/
  allowWrite` (bounded read-only); `network.allowedDomains` + deny
  `WebSearch`/`Bash(curl)`; `credentials.envVars: deny`; `permission_mode:
  dontAsk` (non-yolo + unattended).
- **codex:** `-s read-only|workspace-write|danger-full-access`; `--add-dir`
  (writable extras); `-C/--cd`; `-a never`; broad read via
  `sandbox_permissions=["disk-full-read-access"]`.

## Alternatives considered

- **Keep bypassPermissions (yolo).** Rejected — not isolation; the tech-lead's
  broad read makes it the highest-risk agent to leave open.
- **codex `disk-full-read-access` for the tech-lead.** Rejected as the default —
  unbounded read; bounded `--add-dir` is tighter.
- **Insist on read-only substrate.** Rejected as a *requirement* — decoupled-
  scratch + no-egress already provide the protections read-only would; kept as
  an optional claude-code hardening.
- **Tech-lead-specific sandbox hack.** Rejected — the sandbox is a session-launch
  concern for every agent; a bolt-on would scatter the policy.
