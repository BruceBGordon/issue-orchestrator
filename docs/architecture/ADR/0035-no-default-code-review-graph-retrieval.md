# ADR 0035: Do not adopt code-review-graph retrieval by default

**Status:** Accepted
**Date:** 2026-09-06

## Context

We evaluated code-review-graph (CRG), with optional igraph community analysis,
to reduce remote agent token use by doing more repository analysis locally.
Local full builds took roughly 16–17 seconds for issue-orchestrator (IO) and
2–3 seconds for PorchPin; installing igraph added negligible measured build
time. The adoption question is whether retrieval saves agent work after
including instructions, tool definitions, queries, results and source verification.

The initial 12-run experiment covered two investigations. Mixed results and
an audit finding incorrect diff scope and excessive overview output motivated
a second round. Four calibration runs compared corrected standard MCP tools
with a compact interface; the compact interface used 13.3% more uncached input
plus output by equal-task geometric mean and was not selected.

The second round used six new tasks: lookup, impact analysis and commit review
in each repository, with two fresh runs per condition (24 runs). Conditions
used the same source snapshots, `gpt-6-astra` with high reasoning, CRG 2.3.8
and igraph 1.0.0 where applicable, bounded retrieval, and source-based blind
grading against rubrics fixed before answers existed.

| Measure | Graph condition versus ordinary search |
| --- | --- |
| Uncached input + output, equal-task geometric change | **+5.3%** |
| Total input + output, equal-task geometric change | **+16.4%** |
| Mean blinded quality | **19.29/20**, versus **19.54/20** baseline |
| IO impact analysis, uncached input + output | **−22.8%**, equal quality; savings in both runs |

The predeclared 10% overall token-reduction target was not met. The graph arm
made 116 shell calls plus 26 MCP calls, versus 126 shell calls for baseline:
graph retrieval often supplemented rather than displaced source investigation.
Uncached input plus output is an unweighted token measure, not a dollar estimate.

Six selected tasks and two repetitions do not establish a universal result.
Quality scores are model judgments, not runtime correctness guarantees. A
provider-capacity rejection succeeded on a later identical serial retry;
provider load and uncontrolled caching limit latency comparisons. No second-round
agent called community analysis, so these results do **not** isolate igraph's
benefit or prove it useless.

## Decision

**Stop this optimization effort and do not make CRG/igraph retrieval part of
the default agent workflow.** Do not introduce a routing skill to justify
adoption based on the successful cases in this experiment.

A skill would need to identify beneficial tasks before execution. Selecting
winners afterward does not demonstrate that ability; incorrect routing and
additional instructions can erase savings. Local CPU and SQLite execution
costs are not the limiting expense, and faster identical query results do not
themselves reduce model tokens. Both measured graphs already excluded the
obvious heavy dependency/build paths and used SQLite WAL mode.

Revisit only if recurring real work demonstrates a concrete retrieval gap.
Any new evaluation should fix representative tasks and acceptance criteria
in advance, include routing overhead and mistakes, preserve answer quality,
and separate ordinary CRG retrieval from igraph community analysis.

## Consequences and preserved evidence

Ordinary search and source inspection remain the default. We accept that some
impact investigations may miss a measured saving in exchange for avoiding
unproven workflow complexity. This decision records non-adoption; it does not
remove installed tools or change existing agent configuration.

This ADR preserves the decision and principal results in repository history.
Detailed reports, prompts, scripts, provider usage logs, blind scores, source
identities and artifact manifests remain in these **local, machine-specific**
archives under `~/.local/share/codex-experiments/`:

- `crg-token-ab-2026-09-06/`: initial experiment, `report.md` and
  `configuration-audit.md` documenting its limitations.
- `crg-token-ab-round2-2026-09-06/`: broader experiment, `report.md`,
  `summary.json`, `validation.json`, and retained failed/retried attempts.

The raw archives are not versioned with this ADR; reproducing the experiments
requires the recorded source snapshots and recreating checkout-specific graphs
and environments. The decision above remains understandable without them.
