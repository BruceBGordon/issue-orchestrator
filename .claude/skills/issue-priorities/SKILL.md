---
name: issue-priorities
description: Set or change an issue's priority, milestone, or agent assignment so the orchestrator actually picks it up and works it in the intended order. Use when asked to prioritize an issue, queue an issue for an agent, ask why an issue is never picked up, triage a backlog, or file a new issue that should be worked.
---

# Issue Priorities and Queue Placement

Three pieces of issue metadata decide whether an issue is worked and when. Two
of them behave differently from what their names suggest, and the difference is
silent — a mis-set issue looks correctly prioritized in the GitHub UI and is
scheduled exactly as it was before.

Read this before setting priority on anything.

## The one-paragraph version

An issue is invisible to the orchestrator until it carries an `agent:*` label.
Once visible, it is ordered by **milestone, then the `[Px-nnn]` prefix in its
title, then issue number** — and the `priority:high` / `priority:medium` /
`priority:low` labels take no part in that ordering at all.

## What each piece actually does

### `agent:*` label — required for pickup

The `agents:` block in `.issue-orchestrator/config/modes/<mode>/main.yaml` is
keyed by label name (`agent:backend`, `agent:frontend`, `agent:mobile`, …). The
label **is** the routing key: it selects the prompt, provider, model, timeout
and reviewer for the session.

No `agent:*` label means the planner never considers the issue, no matter what
else it carries. This is the single most common reason a "high priority" issue
sits untouched.

> The GitHub descriptions on these labels still read *"Test data for
> integration tests"*. That text is stale. They are the real production routing
> labels.

### Milestone — the primary sort key, and a trap when absent

`MilestoneNumberStrategy.get_sort_key` (`control/scheduler.py`) extracts the
first integer from the milestone name, so `M9` sorts before `M10` numerically
rather than alphabetically.

An issue **with no milestone returns `float("inf")` and sorts behind every
milestoned issue in the repo**. For a backlog of this size that is
indistinguishable from never. Assigning a milestone is usually the change that
actually moves an issue up the queue.

### Title `[Px-nnn]` prefix — the real priority dial

`_get_priority_value` and `_get_sequence_value` parse the tier and sequence out
of the **issue title**, not from any label:

```
\[P(\d)-\d+\]      tier regex     P0 is highest, P9 lowest
\[P\d-(\d+)\]      sequence regex ascending within a tier
```

No match falls back to `scheduling.default_priority_tier`, which is **1**
(`infra/settings_schema.py`; its own doc note says *"Used when issue titles do
not include a [P?-nnn] prefix"*).

**A bare `[P0]` does not match.** The regex requires the `-nnn` sequence, so a
title like `[P0] Fix the thing` is decorative — it silently schedules at the
default tier while reading as top priority to every human who sees it.

### `priority:*` labels — a human signal only

`priority:high`, `priority:medium` and `priority:low` are provisioned by
`LabelManager.repository_initialization_labels` and carried by many issues, but
**nothing in `sort_by_priority` reads them**. Set them so the backlog is legible
to people; never report that setting one changed scheduling.

## Full sort order

`Scheduler.sort_by_priority` builds the key in this order:

1. **Milestone** — configured order map if present, else the strategy's key; no
   milestone sorts last.
2. **Priority tier** — `[P(\d)-` from the title, else `default_priority_tier`.
3. **Sequence** — `-(\d+)]` from the title, else `float("inf")` (sorts last
   within its tier).
4. **Issue number**, ascending, as the final tie-break.

`pick_next_batch` then takes from the front, after any explicit
`priority_overrides` (issue numbers, jumped to the head of the batch).

### The ordering trap: prerequisites filed later

Because ties break by **ascending issue number**, a prerequisite filed *after*
its dependent sorts *behind* it. Filing #7160 as a blocker for #7152 puts the
blocker second by default.

Explicit sequence numbers are the only fix — give the prerequisite the lower
`-nnn`.

## How to do it

### Prioritize an existing issue

1. **Check the work belongs to this repo.** An agent launched here works in
   this repo's worktree. An issue whose real work lives in another repository
   burns a session and completes nothing — label it there, or leave it as a
   tracker. This check comes first because it is the one that wastes the most.
2. Add the `agent:*` label that routes to the right prompt/provider.
3. Assign a milestone, or accept that it sorts last.
4. Add the `priority:*` label for the humans.
5. Only if it must jump its milestone's queue, add a `[Px-nnn]` title prefix.
   Raising an issue above the rest of its milestone is a judgment call about
   what gets displaced — surface it rather than deciding silently.

```bash
gh issue edit <N> --repo <owner>/<repo> \
  --add-label "agent:backend" \
  --add-label "priority:high" \
  --milestone "M11: Validation, Test Infra, And Dev Guardrails"
```

### Allocate a sequence number

Sequences run as one counter per tier across the whole repo, not per milestone.
Check **open and closed** issues so a closed issue's number is not reused:

```bash
gh issue list --repo <owner>/<repo> --state all --limit 600 \
  --json number,title,state \
  --jq '.[] | select(.title | test("\\[P0-")) | "\(.number)\t\(.state)\t\(.title)"'
```

Take the next unused number. Duplicates are not fatal — the issue-number
tie-break keeps the order deterministic — but they make the backlog misleading.

### Audit for malformed prefixes

Finds titles that look prioritized but do not match the tier regex:

```bash
gh issue list --repo <owner>/<repo> --state open --limit 400 \
  --json number,title --jq '.[] | "\(.number)\t\(.title)"' \
| python3 -c '
import re, sys
canonical = re.compile(r"\[P(\d)-\d+\]")
suspect = re.compile(r"(\[\s*[Pp]\s*\d|\(\s*[Pp]\d|\[[Pp]\d[^-\]]|\[[Pp]\d-\D)")
for line in sys.stdin:
    number, title = line.rstrip("\n").split("\t", 1)
    if not canonical.search(title) and suspect.search(title):
        print(f"#{number}  {title}")
'
```

When repairing one, **copy the existing title and change only the prefix**.
Retyping the tail from a truncated listing silently rewrites the issue's
wording.

### Diagnose "why was this never picked up?"

In order, stopping at the first hit:

1. No `agent:*` label → invisible to the planner. This is nearly always it.
2. No milestone → sorts behind every milestoned issue.
3. A blocking label (`blocked`, `blocked-*`, `needs-human`) → held deliberately;
   see the `troubleshooting` skill.
4. Ordered correctly but behind a long queue → check tier and sequence, and
   confirm `max_concurrent_sessions` is not saturated.

## Reference

| Thing | Where |
|---|---|
| Sort key construction | `src/issue_orchestrator/control/scheduler.py` — `sort_by_priority` |
| Tier / sequence regexes | same file — `_get_priority_value`, `_get_sequence_value` |
| Milestone strategy | same file — `MilestoneNumberStrategy`, `DueDateStrategy` |
| Batch selection and overrides | same file — `pick_next_batch` |
| Default tier | `src/issue_orchestrator/infra/settings_schema.py` — `default_priority_tier` |
| Label families and provisioning | `src/issue_orchestrator/control/label_manager.py` |
| Agent routing keys | `.issue-orchestrator/config/modes/<mode>/main.yaml` — `agents:` |
