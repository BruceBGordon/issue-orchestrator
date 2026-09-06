---
name: issue-dependency-stacking
description: "Create or repair issue-orchestrator dependency and stack graphs using exact Depends-on:/Stack-after: directives, compatible milestones, agent labels, and gate verification. Use when splitting work into sequenced GitHub issues, making agent tickets runnable, stacking PRs, or diagnosing premature issue launches."
---

# Issue dependency and stacking

Treat issue-orchestrator directives as machine state. Prose such as "depends on",
"follows", ordering in a checklist, or a milestone sequence is documentation only.

## Choose the edge deliberately

Choose based on when successor work may start and which branch it must use:

| Required behavior | Directive | Successor base and gate |
| --- | --- | --- |
| The predecessor must merge or otherwise close before successor work starts | `Depends-on: #123` | Wait for issue 123 to close, then start from the updated default branch |
| The successor may start before the predecessor merges and must build on its changes | `Stack-after: #123` | Wait for a usable, validated, agent-reviewed predecessor branch, then base on that branch |

`Stack-after:` permits work, review, and publication before the predecessor
merges; it does not permit out-of-order merging. The successor stays merge-blocked
until the predecessor merges.

- Do not convert a normal dependency to a stack merely to preserve work that
  launched early. That changes the requested execution semantics.
- Give a stacked successor at most one unmerged stack predecessor. Multiple
  possible base branches are ambiguous; represent the stack as a chain instead.

If the requested timing is unclear, determine whether the successor may begin
before its predecessor merges. Do not silently infer stacking from words such as
"after" or "follows".

## Wire the graph before queueing agents

1. Resolve the exact issue identifiers and inspect their milestones. For normal
   dependencies, each predecessor must be in the successor's milestone or in the
   configured foundation milestone.
2. Create predecessors first. When creating a batch, either use the resulting
   GitHub issue numbers or assign stable external IDs such as `[M2-010]` in issue
   titles.
3. Put every edge on its own line in the successor body. Valid examples are:

   ```text
   Depends-on: #123
   Depends-on: owner/repository#456
   Depends-on: M2-010
   Stack-after: #789
   ```

   Do not put brackets around an external ID in a directive, and do not omit the
   `#` from a numeric GitHub issue reference.
4. Create or update the successor without an agent label. Persist and verify the
   directives first; add the intended `agent:*` label only after the graph is
   visible to issue-orchestrator. A milestone may be set earlier, but it must be
   compatible with every normal dependency.
5. For an issue already queued or running, stop scheduling or remove its agent
   label before changing its dependency graph when authorized. Updating the body
   alone can race an agent launch.

For a serial sequence A, B, C, express the graph explicitly:

```text
# B
Depends-on: #A

# C
Depends-on: #B
```

Use multiple directives when the graph has multiple direct predecessors. Do not
add redundant transitive edges unless each predecessor independently gates the
successor.

## Compare normal and stacked sequences

Use a normal dependency when B should begin only after A lands:

```text
# Issue B body
Depends-on: #A
```

Issue-orchestrator blocks B while A is open. Once A closes, B starts from the
updated default branch.

Use a stack when B is intentionally developed and reviewed on A's unmerged work:

```text
# Issue B body
Stack-after: #A
```

Issue-orchestrator waits until A exposes a usable, validated, agent-reviewed
branch. It then bases B on A's branch and may publish B's pull request, but it does
not merge B before A.

For a three-issue stack, make each issue depend on its immediate branch base:

```text
# Issue B body
Stack-after: #A

# Issue C body
Stack-after: #B
```

Do not add `Stack-after: #A` to C as well. One unmerged stack predecessor gives C
one unambiguous base branch.

### Fan-out is automatic; fan-in is not

One predecessor may have several stacked successors:

```text
# Issue B body
Stack-after: #A

# Issue C body
Stack-after: #A
```

Once A exposes a usable, validated, agent-reviewed branch, B and C become eligible
together and issue-orchestrator can run them concurrently, up to its configured
session capacity and subject to their other gates. Neither successor waits for a
human merge of A before starting or publishing. Human review and ordered merges
are still required later.

This fan-out is unambiguous because each successor has exactly one incoming stack
edge and therefore one base branch. The inverse shape is not automatic: do not
give D both `Stack-after: #B` and `Stack-after: #C` while B and C are unmerged.
Those sibling branches provide competing bases, so issue-orchestrator blocks the
fan-in. Either use normal `Depends-on:` edges so D starts after both siblings
close, or reshape the work into a linear stack with one explicit base.

## Verify before declaring an issue runnable

1. Re-read the issue from GitHub after every mutation, including its body,
   milestone, and labels. Inspect the exact directive lines, for example:

   ```bash
   gh issue view ISSUE --json body,labels,milestone --jq '.body' \
     | rg '^(Depends-on|Stack-after):'
   ```

2. Check issue-orchestrator's dependency projection or gate status. For a normal
   edge, an open predecessor must block work. For a stack edge, work must remain
   blocked until the predecessor branch is usable, validated, and agent-reviewed;
   the reported stack base must then be that predecessor branch, while merge stays
   blocked until the predecessor merges.
3. If the projection is unavailable, do not claim the dependency is enforced from
   body text alone. Use the parser from the active issue-orchestrator installation
   when available, and state what remains unverified.
4. Add the agent label only after the expected gate is confirmed. Re-read the
   issue once more to catch label or body races.

## Repair a malformed or late dependency

1. Replace ignored prose or malformed references with exact directive lines.
2. Inspect whether an agent, branch, pull request, or recovery tag already exists.
   Preserve that work; do not reset or delete it.
3. Decide whether preserved work should later be rebased onto the closed
   predecessor or intentionally rebuilt as a stack. Keep the original dependency
   semantics unless the user explicitly changes them.
4. Clear failure or blocking labels and requeue work only after both the dependency
   gate and execution infrastructure are healthy.
