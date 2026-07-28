# Quality Guardrails

This repository uses ratcheted quality guardrails for architecture and control-plane drift.

The goal is not to pretend the current codebase has no debt. The goal is to make new debt visible, fail PRs that make tracked metrics worse, and then reduce the baseline through focused cleanup PRs.

This grew out of the control-architecture discussion around issue #6362 and the follow-up decision to stop relying on longer agent prompts for quality. The intended model is mechanical: whole-repo ratchets for systemic drift, changed-code checks for local regressions, and analyzer-backed rules once repo-local metrics prove useful.

## Current Guardrails

Run:

```bash
make quality-guardrails
```

The command runs `tools/quality_guardrails.py` against `tools/quality_guardrails.yml` and compares the results with `quality/guardrails-baseline.json`.

The first rule set tracks:

- oversized control hotspots
- Ruff C901 complexity findings, including existing `noqa`-suppressed debt
- Ruff `noqa` suppressions themselves, so new analyzer bypasses require explicit acceptance
- Semgrep owner-boundary findings for direct runtime-state mutation sites
- Semgrep typed-seam findings for raw dict/list-of-dict public payload return and parameter surfaces
- Semgrep semantic-vocabulary findings for raw lifecycle/status string literals
- UI OpenAPI route drift: contracted browser endpoints must exist and use the generated response model, while legacy uncontracted dashboard routes are ratcheted so new browser JSON routes cannot bypass `docs/api/ui-openapi.json` silently
- branch sites that mention lifecycle/control vocabulary

These are proxies for the failure pattern captured in issue #6362: control policy spreading across multiple owners, projections, and execution paths.

Dependency-boundary checks remain in import-linter rather than the ratchet file when a boundary can be enforced as a hard gate. Existing debt is listed as explicit `ignore_imports` entries, which makes each exception visible while blocking new dependency edges.

Analyzer-backed rules use mature tools for source-language semantics and keep this repository's custom code limited to normalization and ratcheting. The Ruff complexity guardrail runs Ruff's C901 rule with `ignore_noqa` enabled so existing suppressed complexity debt is visible in the baseline. Normal `lint-complexity` still blocks unsuppressed Ruff complexity findings, and Ruff's `PGH004` rule blocks blanket `# noqa` suppressions.

Semgrep-backed rules live under `tools/semgrep/`. Semgrep owns AST pattern matching for repo-specific invariants; `tools/quality_guardrails.py` only invokes Semgrep, normalizes its JSON findings, and ratchets the resulting metric IDs. Semgrep is installed from its own locked uv project into `.venv-semgrep` by `make semgrep-venv` and `make worktree-setup`, so Semgrep's CLI dependencies do not constrain the main project lock. The runner verifies the Semgrep binary reports the pinned version before collecting findings; `QUALITY_GUARDRAILS_SEMGREP_BIN` is only an override for tests or explicit local experiments and is subject to the same version check. The first Semgrep rules track direct mutation of orchestrator runtime state collections, raw dict/list-of-dict return and parameter annotations at public payload seams, and known lifecycle/status vocabulary literals so new state words cannot spread without explicit review and baseline acceptance. The typed-seam rule covers both builtin generic spellings and common `typing` aliases such as `Dict`, `List`, and `Optional`.

The `noqa` suppression ratchet scans Python comment tokens, not raw source lines, so string literals that mention `# noqa` are ignored. Suppression metric IDs are based on the normalized suppression comment rather than the full line of code. Editing code before an unchanged `# noqa` comment should not create a new suppression metric; duplicate identical suppression comments in the same file fall back to a line-number suffix.

Lifecycle/control vocabulary is matched on lexical tokens and configured phrases, not raw substrings. For example, `statusCode`, `sessionState`, `session_state`, and `review-exchange` can match configured terms, while unrelated tokens such as `prestatus` do not.

JavaScript branch-site scanning uses a lightweight lexical pass rather than a full parser. It ignores comments and string literals when finding branch keywords, then checks multi-line `if`/`while`/`switch` conditions and `case` clauses for configured control terms. Vendored JavaScript bundles are excluded from this repo-local architecture metric.

UI OpenAPI route scanning reads `docs/api/ui-openapi.json` and FastAPI route decorators under `src/issue_orchestrator/entrypoints/`. If a schema path is removed from the server or stops naming the generated `response_model`, the check fails as new drift. Existing dashboard `/api/*` routes not yet represented in OpenAPI are tracked as baseline debt; adding another uncontracted browser JSON route fails until the endpoint is added to the schema or the ratchet is explicitly accepted. Browser-facing route decorators must use string-literal paths so the checker can compare them to the OpenAPI path keys; dynamic path expressions are tracked as explicit guardrail findings rather than being skipped.

Route metadata a decorator does not carry is named in prose rather than with a placeholder sentinel: a route with no `response_model` keyword reports `found (no response_model)`, and a decorator with neither a positional path nor a `path=` keyword reports `uses dynamic path (unknown route path expression)`. Concrete dynamic paths still render as quoted source (`uses dynamic path 'API_DYNAMIC'`). Metric IDs are unaffected — a pathless decorator is still keyed by `dynamic-path:<METHOD> <file>:<line>` — so the wording can be improved without invalidating baseline entries.

## Ratchet Model

Existing violations are stored in the baseline. A PR fails when it:

- introduces a new tracked metric that is already over the configured threshold
- increases a tracked metric above its baseline value

Rules may define `new_metric_min_value` so small new files can be reported without failing the ratchet immediately. For example, the lifecycle/control branch-site rule starts failing unbaselined files at three matching branch sites.

Improvements do not fail. If a cleanup PR removes policy sites or shrinks a hotspot, regenerate the baseline and commit the lower value.

```bash
python tools/quality_guardrails.py --update-baseline
```

For ordinary PRs that intentionally add exactly one tracked metric, prefer targeted acceptance instead of regenerating the whole baseline:

```bash
python tools/quality_guardrails.py --accept control_policy_branch_sites:src/issue_orchestrator/control/new_owner.py
```

Targeted acceptance updates only the named key, then re-runs the ratchet comparison. Any unrelated increases remain violations.

Stale baseline entries are ignored by the normal ratchet so cleanup PRs can reduce metrics without failing. Check for stale entries explicitly when maintaining the baseline:

```bash
make quality-guardrails-stale
```

This target is intentionally manual maintenance, not part of `make lint-arch`: cleanup PRs should be allowed to reduce metrics first, then prune stale baseline keys deliberately. Stale-only findings exit `3`; ratchet violations exit `2` and take precedence when both are present.

When a stale entry should be removed, prune the specific key rather than regenerating the whole baseline:

```bash
python tools/quality_guardrails.py --prune control_policy_branch_sites:src/issue_orchestrator/control/old_path.py
```

Targeted pruning only removes keys that are already stale. It refuses to prune current metrics.

The stale-entry reader treats the committed baseline as generated data. Missing baseline fields fail fast instead of being reported as `unknown`.

## Adding Guardrails

Add guardrails in small PRs:

1. Add the checker in report/ratchet form.
2. Baseline the current repository state.
3. Fail only new or worsened findings.
4. Create separate cleanup PRs to reduce the baseline.
5. Promote mature checks to hard gates when the baseline reaches zero or a defensible threshold.

Good guardrail candidates:

- semantic status/reason vocabulary duplication
- owner-boundary bypasses for labels, sessions, artifacts, and cache state
- raw untyped command/event/artifact payloads at public seams
- dead legacy UI/control surfaces
- dependency topology and change-coupling hot spots

Prefer mechanical checks over prompt instructions. If a rule is important enough to rely on, encode it in tooling.
