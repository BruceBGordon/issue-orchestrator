# Configuration Reference

This is the full configuration reference auto-generated from the settings schema.
For a short onboarding guide, see `docs/user/configuration.md`.

<!-- BEGIN AUTO-GENERATED CONFIG REFERENCE — regenerate via: pytest tests/unit/test_settings_schema.py::TestDriftDetection::test_config_reference_not_stale -->
# Settings Reference

_Auto-generated from settings schema._

## Concurrency

| Field | Type | Default | Description | Examples | Notes |
|-------|------|---------|-------------|----------|-------|
| `execution.concurrency.max_concurrent_sessions` | integer | `3` | Maximum parallel agent sessions | `1`, `3`, `5` | Set based on CPU, RAM, and how many concurrent sessions you can actively review. |
| `execution.concurrency.session_timeout_minutes` | integer | `45` | Kill sessions after this duration | `30`, `45`, `90` | Lower values fail faster for stuck sessions; higher values help long builds. |
| `ui.queue_refresh_seconds` | integer | `600` | How often to refresh the issue queue from GitHub (0 = manual only) | `0`, `300`, `600` | Use 0 to disable automatic refreshes and refresh manually in the UI. |
| `ui.fetch_layer.enabled` | boolean | `True` | Enable incremental refreshes between periodic full scans | `true`, `false` | Disable to force a full GitHub queue scan on every refresh. |
| `ui.fetch_layer.network_sync_seconds` | integer | `60` | How often to run GitHub network sync cycles (independent of control tick) | `15`, `60`, `120` | Lower values improve freshness; higher values reduce GitHub API calls. |
| `ui.fetch_layer.full_scan_interval_seconds` | integer | `1800` | Run a full queue scan at this interval even when incremental mode is enabled | `600`, `1800`, `3600` | Lower values discover new work faster; higher values reduce API usage. |
| `ui.fetch_layer.discovery_limit` | integer | `25` | Max issues fetched per incremental discovery pass | `0`, `25`, `50` | Set to 0 to disable discovery during incremental refreshes. |
| `ui.fetch_layer.max_hot_issues_per_cycle` | integer | `40` | Max existing queue issues to refresh by direct issue lookup per cycle | `20`, `40`, `100` | Higher values improve freshness but increase API usage. |
| `ui.fetch_layer.pr_scan_every_n_refreshes` | integer | `2` | Scan review/rework PRs every N queue refreshes | `1`, `2`, `3` | Use 1 for max freshness; increase to reduce PR API calls. |
| `ui.fetch_layer.dependency_scan_every_n_refreshes` | integer | `1` | Recompute dependency blocking every N queue refreshes | `1`, `2`, `3` | Use 1 for immediate dependency updates; increase to reduce load. |
| `ui.fetch_layer.visibility_aware_enabled` | boolean | `False` | Prioritize refresh for issues currently visible in the Flow board | `true`, `false` | Requires browser visibility hints from the Flow board. |
| `ui.fetch_layer.selective_sync_planner_enabled` | boolean | `False` | Enable cross-entity selective sync planning for queue refresh cycles | `true`, `false` | Use with telemetry to tune freshness versus API cost. |
| `scheduling.default_priority_tier` | integer | `1` | Default priority tier when none is specified (0-9) | `0`, `1`, `2` | Used when issue titles do not include a [P?-nnn] prefix. |

## E2E Runner

| Field | Type | Default | Description | Examples | Notes |
|-------|------|---------|-------------|----------|-------|
| `e2e.enabled` | boolean | `False` | Automatically run E2E tests when main branch changes | `true`, `false` | Keep disabled on repos without stable E2E tests. |
| `e2e.auto_run_interval_minutes` | integer | `30` | Min interval between auto runs (0 = disable) | `0`, `30`, `60` | Set to 0 to disable automatic runs and trigger manually. |
| `e2e.role` | string | `auto` | Role in multi-orchestrator setup | `auto`, `executor`, `reader`, `disabled` | Use executor on the single machine that should run tests. |
| `e2e.occupies_session_slot` | boolean | `False` | Treat an E2E run as a first-class worker workload | `true`, `false` | Off (default) keeps today's parallel behavior: E2E runs alongside agents. On, an E2E run counts against max_concurrent_sessions (not the reserved tech lead slot): it starts only when a worker slot is free, occupies one slot while running so the planner launches one fewer agent, and a due suite claims a slot ahead of new issues but behind in-flight reviews/reworks/validation-retries/tech_lead. Enable on resource-constrained machines where a second orchestrator workload would starve live agents. |
| `e2e.runner_kind` | string | `pytest` | Execution adapter used for E2E runs | `pytest`, `command` | Use pytest for live test events and retries; use command for arbitrary test runners that emit JUnit XML. |
| `e2e.pytest_args` | string | `tests/e2e -v` | Space-separated pytest arguments used when Runner Kind is pytest | `tests/e2e -v`, `tests/e2e -v --junitxml=.issue-orchestrator/e2e-results/pytest-junit.xml` | Used only when runner_kind=pytest. Add --junitxml and mirror the same path in junit_xml_paths when you want structured Results coverage in the dashboard. |
| `e2e.command` | string | `` | Space-separated command used when Runner Kind is command | `./scripts/run-e2e-suite.sh`, `npm run test:e2e -- --reporter=junit` | Used when runner_kind=command. The command runs inside the E2E worktree. |
| `e2e.junit_xml_paths` | string | `` | Relative JUnit XML files or globs to ingest after the run (one per line) | `.issue-orchestrator/e2e-results/pytest-junit.xml`, `test-results/junit.xml` | Leave empty for log-only runs. Missing configured reports fail the run loudly. Use the same path you passed to pytest --junitxml or your external test runner. |
| `e2e.artifact_paths` | string | `` | Additional report or artifact files to expose in the UI (one per line) | `playwright-report/index.html`, `test-results/**/*.zip`, `reports/**/*.html` | Paths are resolved relative to the E2E worktree after the run completes. Use this for native HTML reports, traces, screenshots, and similar debugging artifacts. |
| `e2e.allow_retry_once` | boolean | `True` | Retry failing tests to reduce flakiness | `true`, `false` | Applies to runner_kind=pytest. Command runners ignore this and report the original command result. |
| `e2e.stop_on_first_failure` | boolean | `False` | Add -x flag to stop test run on first failure | `true`, `false` | Applies to runner_kind=pytest. |
| `e2e.quarantine_file` | string | `tests/e2e/quarantine.txt` | Path to quarantine file for skipping known-flaky tests | `tests/e2e/quarantine.txt`, `tests/e2e/quarantine-local.txt` | Doctor verifies the file exists when E2E is enabled. |
| `e2e.auto_quarantine` | boolean | `True` | Automatically add failing tests to the quarantine list | `true`, `false` | Set false to require manual quarantine updates. |
| `e2e.auto_create_issues` | boolean | `True` | Automatically create GitHub issues for failed tests | `true`, `false` | Disable if you prefer manual triage of failures. |
| `e2e.issue_agent_label` | string | `agent:backend` | Agent label assigned to auto-created failure issues | `agent:backend`, `agent:tech-lead` | Must refer to an agent defined in the config. |

## Validation

| Field | Type | Default | Description | Examples | Notes |
|-------|------|---------|-------------|----------|-------|
| `validation.quick.cmd` | string (optional) | `None` | Fast command run by coding-done and review exchange loops | `./scripts/validate-fast.sh`, `make test-fast` | Keep this fast enough for agent/reviewer back-and-forth. Put repo-specific policy checks such as banned test skips here. |
| `validation.quick.timeout_seconds` | integer | `300` | Timeout for quick validation | `120`, `300`, `600` | Lower values keep review loops responsive. |
| `validation.publish.cmd` | string (optional) | `None` | Authoritative command run before push/publish | `./scripts/validate-pr.sh`, `./scripts/validate-pr-suite.sh` | This should match the repo's authoritative local PR/pre-push gate. If make validate-pr wraps the cache-aware verify hook, configure a private non-recursive suite command instead. |
| `validation.publish.timeout_seconds` | integer | `1800` | Timeout for publish validation | `600`, `1800`, `3600` | Allow enough time for the deeper publish gate. |
| `validation.publish.dirty_check` | string | `tracked` | Dirty-tree policy enforced before push actions | `tracked`, `unstaged`, `all`, `off` | Use tracked for normal agent worktrees. Use off only when another guard owns dirty-tree safety. |
| `validation.junit_xml_paths` | string | `` | Relative JUnit XML files or globs emitted by validation commands | `test-results.xml`, `build/test-results/test/*.xml` | When set, failed validations render a structured test-results view in the dashboard. |

## Filtering

| Field | Type | Default | Description | Examples | Notes |
|-------|------|---------|-------------|----------|-------|
| `filtering.label` | string (optional) | `None` | Only process issues with this label (optional) | `bot-ready`, `needs-triage` | Use a single label to gate which issues are eligible. |
| `filtering.milestones` | string | `` | Milestones to process (comma-separated string or YAML list) | `M1, M2`, `["M1", "M2"]` | Accepts a comma-separated string or a YAML list. Leave empty to allow all milestones. |
| `filtering.exclude_labels` | string | `` | Labels to exclude (comma-separated string or YAML list) | `test-data, skip`, `["test-data", "skip"]` | Accepts a comma-separated string or a YAML list. |
| `filtering.exclude_label_prefixes` | string | `` | Label prefixes to exclude (comma-separated string or YAML list) | `io:e2e:`, `["io:e2e:", "tmp:"]` | Exclude issues that have any label starting with one of these prefixes. |
| `filtering.fetch_limit` | integer | `100` | Max issues to fetch per API call | `50`, `100`, `200` | Lower values reduce API load; higher values reduce pagination. |
| `filtering.max_to_start` | integer | `0` | Stop after starting N issues (0 = unlimited) | `0`, `5`, `10` | Useful for dry runs or throttling initial ramp-up. |

## Milestones

| Field | Type | Default | Description | Examples | Notes |
|-------|------|---------|-------------|----------|-------|
| `milestones.order` | string | `` | Explicit ordered list of milestone titles. Does not filter; unlisted milestones are appended using the milestone sort strategy. | `M1, M2` | Use to override the default sort order without filtering. |

## Review

| Field | Type | Default | Description | Examples | Notes |
|-------|------|---------|-------------|----------|-------|
| `review.enabled` | boolean | `False` | Enable automated code review workflow | `true`, `false` | When enabled, a reviewer agent validates work agent PRs. |
| `review.default` | string (optional) | `None` | Agent label for code reviews (e.g., agent:reviewer) | `agent:reviewer` | Must match a label defined under agents. |
| `review.max_rework_cycles` | integer | `5` | Max times to re-queue work agent before escalating | `0`, `2`, `5` | Set to 0 to disable rework cycles (immediate escalation). |
| `review.max_consecutive_publish_failures` | integer | `3` | Escalate to needs-human after this many consecutive push/PR creation failures | `2`, `3`, `5` | After N consecutive publish failures for the same issue, escalate to needs-human instead of publish-failed. |
| `review.keep_current_approach_label` | string | `reviewer-keep-current-approach` | Label that tells reviewer to avoid alternative approaches | `reviewer-keep-current-approach` | Applied to issues where stability is preferred over refactors. |
| `review.retrospective.enabled` | boolean | `False` | Enable review-first audits for existing implementations | `true`, `false` | When enabled, issues carrying the retrospective trigger label are reviewed before any coder rework is launched. |
| `review.retrospective.trigger_label` | string | `retrospective-review` | Issue label that queues review of an existing implementation | `retrospective-review`, `lack-of-review-redo` | This label is the source of truth for review-first reruns. It may be applied to open or closed issues. |
| `review.retrospective.reviewed_label` | string | `retrospective-reviewed` | Issue label added after retrospective review approval | `retrospective-reviewed` | Added to the issue when the reviewer approves the existing implementation. |
| `review.retrospective.changes_requested_label` | string | `retrospective-changes-requested` | Issue label added when retrospective review asks for coder rework | `retrospective-changes-requested` | Added before the issue enters the normal coder rework and PR review lifecycle. |
| `review.run_audit.min_runtime_minutes` | integer | `20` | Automatically capture a run audit when runtime meets or exceeds this threshold (0 = disable) | `0`, `20`, `60` | Long runs get a persisted audit automatically; set to 0 to keep audits label-driven only. |
| `review.run_audit.on_timeout` | boolean | `True` | Automatically capture a run audit when a session times out | `true`, `false` | Keep enabled to preserve diagnostics for timed-out sessions even when they did not exceed the slow-run threshold cleanly. |
| `review.nits.default_policy` | string | `surface` | Default policy for reviewer nits before PR creation | `surface`, `address`, `ignore` | Nits are non-blocking review items. surface records and shows them without rework; address includes them in the normal coder rework loop before PR creation; ignore records them only in review artifacts. |
| `review.nits.by_agent` | object | `` | Per-coder-agent nit policy overrides | `{"agent:frontend": "address"}` | Keys are coder agent labels. Values override review.nits.default_policy for work produced by that agent. |
| `review.exchange.mode` | string | `via-local-loop` | Review exchange mode (via-mcp loop, local loop, or via-draft-pr review) | `via-local-loop`, `via-draft-pr`, `via-mcp`, `auto` | Local loop is the default; use via-draft-pr for GitHub-mediated review cycles. |
| `review.exchange.probe.schedule` | string | `daily` | When to run MCP round-trip validation | `daily`, `startup`, `interval`, `manual` | Use manual to disable automatic probes and run on demand. |
| `review.exchange.probe.interval_days` | integer | `1` | Interval for MCP round-trip validation when schedule=interval | `1`, `7`, `14` | Used only when schedule=interval. |
| `review.exchange.loop.max_rounds` | integer | `10` | Max coder/reviewer rounds before stopping the MCP loop | `5`, `10`, `20` | Higher values allow longer back-and-forth reviews. |
| `review.exchange.loop.max_no_progress` | integer | `2` | Max rounds where reviewer reports no progress before stopping | `1`, `2`, `3` | Limits loops when reviewer is not seeing improvements. |
| `review.exchange.loop.require_validation` | boolean | `True` | Require a validation record before reviewer can approve | `true`, `false` | Disable only if you accept reviewer approvals without validation. |
| `review.max_consecutive_review_exchange_failures` | integer | `3` | Escalate to needs-human after this many consecutive review-exchange runs ended in reviewer/coder no-completion timeouts. | `2`, `3`, `5` | Bounds the runaway loop where a reviewer agent keeps timing out without writing its verdict file. Each consecutive no-completion summary on the same coding session counts; any clean (non-error) summary, scratch-reset boundary, or different reason resets the count. |
| `review.post_publish.checks_pending_timeout_seconds` | number | `1800.0` | How long the orchestrator waits for required GitHub checks to finalize after reviewer approval before escalating to needs-human. | `1800`, `3600`, `5400` | Governs ONLY the 'waiting on CI' (WAIT_FOR_CHECKS) state: mergeable_state in {unstable, blocked} with the status-check rollup reading PENDING/EXPECTED/unknown is treated as 'CI still running', so the orchestrator waits rather than triggering rework and escalates a 'checks pending too long' timeout to needs-human only after this budget elapses. Two other post-approval states are NOT bounded by this timeout and escalate immediately. (1) Unreadable checks: when a decisive PR's status-check rollup cannot be read (the configured GitHub token is missing the Checks / commit-status read scope), the orchestrator does not wait — it raises a separate 'status_rollup_permission_denied' credential/scope diagnostic right away. Repeated rollup probing and logging for that case is throttled by the status-rollup permission backoff, a separate window, not by this pending-checks timeout. (2) Branch-protection blocks (rollup=SUCCESS but mergeable_state=blocked) also escalate immediately. Tune this only to control how long to wait on pending CI. |
| `review.tech_lead_review_agent` | string (optional) | `None` | Agent for batch reviews (optional) | `agent:tech-lead` | Must match a label defined under agents. |
| `review.tech_lead_follow_up_agent` | string (optional) | `None` | Worker agent that tech-lead-proposed follow-up issues route to | `agent:developer` | When a tech lead decision proposes a new follow-up issue, the orchestrator attaches this worker's agent label so normal discovery picks it up. Must match a worker label under agents. REQUIRED whenever review.tech_lead_review_agent is set: a configured tech lead agent makes create_issue proposals reachable, so leaving this unset fails startup validation instead of guessing by config order later. |
| `review.tech_lead_review_threshold` | integer | `0` | Trigger tech lead after N PRs (0 = manual only) | `0`, `5`, `10` | Set to 0 to only trigger tech lead manually. |
| `review.tech_lead_review_label` | string (optional) | `None` | Label marking PRs that await tech lead review (optional) | `needs-tech-lead-review` | Falls back to code_reviewed_label when not set. |
| `review.tech_lead_reviewed_label` | string | `tech-lead-reviewed` | Label added to manifest PRs after tech lead completes | `tech-lead-reviewed` | Added to every PR in the tech lead manifest on success. |
| `review.tech_lead_failed_label` | string | `tech-lead-failed` | Label added to manifest PRs when a tech lead session fails | `tech-lead-failed` | Added to every PR in the tech lead manifest on failure. |
| `review.tech_lead_review_on_failure` | boolean | `True` | Queue a tech lead investigation when sessions fail | `true`, `false` | Disable to only tech lead PR batches, not failures. |
| `tech_lead.dedup.enabled` | boolean | `True` | Check tech-lead follow-up proposals against all open issues | `true`, `false` | When enabled, the orchestrator maintains a rebuildable local fingerprint cache of every open GitHub issue. Agent-cited duplicates are verified against that corpus, and the lexical backstop gates strong matches instead of filing unchecked duplicates. Disable only to intentionally bypass this safety gate. |
| `tech_lead.dedup.similarity_threshold` | number | `0.72` | Lexical similarity at or above which a follow-up is gated | `0.72`, `0.8`, `0.9` | Higher values reduce false-positive duplicate gates; lower values catch looser shared-vocabulary matches. Must be greater than 0.0 and at most 1.0. |
| `tech_lead.authority.post_comment` | string | `execute` | Execute or surface tech-lead-proposed diagnosis comments | `execute`, `propose` | execute posts the proposed comment; propose (shadow mode) surfaces it as would-have-done. Allowed values: execute, propose. |
| `tech_lead.authority.create_issue` | string | `execute` | Execute or gate tech-lead-proposed follow-up issues | `execute`, `propose` | execute files the proposed follow-up issue directly; propose files it as a gated proposal issue carrying the proposed-tech-lead label, inert until an operator removes that label (per-instance approval, #6778). Allowed values: execute, propose. |
| `tech_lead.authority.flag_pattern` | string | `execute` | Open/append durable pattern case-file issues for recurring cross-job patterns | `execute`, `propose` | execute opens a durable pattern case-file issue the first time a pattern_signature is observed and appends an evidence comment to that same case file on every repeat observation (one case file per signature, #6781), so cross-job pattern evidence accrues for later health reviews to mine; it also emits the pattern trace event. propose (shadow mode) records only a would-have-done proposal and opens no case file. Every flag_pattern action MUST carry a pattern_signature (the case-file ledger key) or the tech lead decision is rejected. Allowed values: execute, propose. |
| `tech_lead.authority.reset_retry` | string | `propose` | Act-level: reset-and-retry an issue from scratch | `propose`, `execute` | execute runs the reset+retry-from-scratch owner after re-validating the proposal's preconditions at execution time; stale proposals downgrade to a surfaced record (#6764). propose (default) files each proposal as a gated GitHub issue carrying the proposed-tech-lead label; removing the label is per-instance approval and triggers the same re-validated execution (#6778). Allowed values: execute, propose. |
| `tech_lead.authority.kill_hung_session` | string | `propose` | Act-level: terminate a stuck session | `propose` | propose (default) files each proposal as a gated GitHub issue carrying the proposed-tech-lead label; removing the label is per-instance approval and executes the stored op after re-validating the target session is still active (#6778). Direct execute is not wired yet (#6764) and remains a startup configuration error. Allowed values: execute, propose. |
| `tech_lead.health_review.interval_minutes` | integer | `0` | Create a periodic health-review issue every N minutes (0 = disabled) | `0`, `240` | ADR-0031 §4: when the interval elapses the orchestrator files a health-review anchor issue for the tech lead agent to walk the board snapshot. Requires a configured tech lead agent. 0 disables. |
| `tech_lead.health_review.storm_threshold` | integer | `3` | Escalate this many recent blocked/failed issues into one health review (0 = disabled) | `0`, `3`, `5` | When the threshold is reached inside the configured storm window, the orchestrator creates one immediate, unscheduled health review and suppresses individual investigations for the cohort. The periodic interval remains independent. |
| `tech_lead.health_review.storm_window_minutes` | integer | `5` | Time window used to group blocked/failed problem issues | `1`, `5`, `15` | Problems observed within this window count toward the storm threshold. This window groups reactions; it does not delay ordinary failure investigations. |
| `tech_lead.stuck_sweep.enabled` | boolean | `False` | Re-inject terminally-stuck issues into reactive tech lead (0 = disabled) | `true`, `false` | ADR-0031 (#6823): a bounded, timer-gated backstop that finds open issues stuck in a terminal blocking state the normal loop cannot re-discover and re-injects each into the reactive-tech-lead pipeline as a recovered failure. Requires a configured tech lead agent and tech_lead_review_on_failure. Off by default. |
| `tech_lead.stuck_sweep.interval_minutes` | integer | `240` | Scan for stuck issues every N minutes (>= 1; disable via 'enabled') | `240`, `60`, `480` | How often the tech-lead sweep scans open issues for terminal stuck state. Only runs when the sweep is enabled. The scan is a broad exhaustive open-issue read, so this is a reconcile-for-strays BACKSTOP cadence, not a hot path — the 4h default keeps that read off the frequent paths; lower it only when faster recovery of stranded issues is worth the extra scans. |
| `tech_lead.stuck_sweep.max_recovery_attempts` | integer | `3` | Re-inject a stuck issue at most this many times before escalating | `1`, `3`, `5` | After this many recovery attempts a stuck issue is no longer re-injected (no infinite loop); it is surfaced as exhausted and needs human attention. The counter is durable across restarts. |
| `tech_lead.max_concurrent` | integer (optional) | `None` | Reserved concurrency slots for tech lead sessions (empty = share the worker budget) | `1`, `2` | Empty (the default) shares the worker budget (execution.concurrency.max_concurrent_sessions): tech lead counts against it and is planned from the shared capacity, exactly as before. A positive value is a SEPARATE additive tech lead budget: tech lead sessions run from their own slots and are NOT subtracted from the worker budget, so the tech lead can run even when workers are saturated. Total live agents are then bounded at max_concurrent_sessions + tech_lead.max_concurrent. |
| `tech_lead.max_expedited` | integer | `3` | Cap on outstanding tech-lead-expedited issues at the front of the worker queue (valid range 0-20; 0 disables the expedite lane) | `0`, `3`, `5` | When the tech lead files an urgent create_issue follow-up (expedite=true), the orchestrator jumps it to the front of the worker lane via the same priority queue operators use. This caps how many such issues can be outstanding at once so a noisy tech lead cannot starve normal work. Valid range is 0-20 (values outside it fail validation at both startup and in the settings form); further expedite requests past the cap are logged and fall back to normal priority. 0 disables expediting entirely. Under 'propose' authority an expedited follow-up jumps the lane only after its proposed-tech-lead gate is removed. |

## Merge Queue

| Field | Type | Default | Description | Examples | Notes |
|-------|------|---------|-------------|----------|-------|
| `merge_queue.enabled` | boolean | `False` | Enqueue approved PRs into GitHub's native merge queue | `true`, `false` | When enabled, approved PRs that have cleared the orchestrator gate are enqueued into the provider's merge queue instead of being reworked merely for being behind base. Requires a repo whose branch protection has the merge queue configured. |
| `merge_queue.provider` | string | `github` | Which merge queue backend to use | `github` | Only GitHub's native merge queue is supported today; the value is constrained to the allowed set so the settings form rejects unsupported providers before they reach the running config. |
| `merge_queue.enqueue_after` | string | `code-reviewed` | Orchestrator gate that must pass before a PR is enqueued | `code-reviewed`, `tech-lead-reviewed` | Names the approval gate the PR must clear before enqueue. code-reviewed is the reviewer-approval gate. |
| `merge_queue.failure_action` | string | `rework` | How to route a PR that fails the merge queue | `rework`, `needs_human` | rework sends the PR back to a coding agent; needs_human escalates it for manual attention. |

## Goal Pilot

| Field | Type | Default | Description | Examples | Notes |
|-------|------|---------|-------------|----------|-------|
| `goal_pilot.enabled` | boolean | `False` | Enable the Goal Pilot AI controller | `true`, `false` | Enable only when Goal Pilot prompts are configured and tested. |
| `goal_pilot.agent` | string (optional) | `None` | Agent label to run as Goal Pilot (e.g., agent:goal-pilot) | `agent:goal-pilot` | Must match a label defined under agents. |
| `goal_pilot.approval_policy` | string | `journeys_only` | How Goal Pilot applies repo changes | `journeys_only`, `gatekeeper`, `batch` | Batch mode bundles changes before approval; gatekeeper requests approval per change. |
| `goal_pilot.approval_batch_size` | integer | `10` | How many changes to bundle before approval (batch mode) | `5`, `10`, `25` | Used only when approval_policy=batch. |
| `goal_pilot.approval_batch_window_minutes` | integer | `60` | Max time to wait before asking for approval (batch mode) | `30`, `60`, `120` | Used only when approval_policy=batch. |

## Hooks

| Field | Type | Default | Description | Examples | Notes |
|-------|------|---------|-------------|----------|-------|
| `hooks.ai_gate.interval_days` | integer | `7` | Run AI gate tests every N days (0 = disabled) | `0`, `7`, `30` | Set to 0 to disable periodic AI gate tests. |
| `hooks.ai_gate.dangerous_allow_failure` | boolean | `False` | If true, warn only on AI gate failure; if false, block orchestrator start | `true`, `false` | Keep false in production to enforce hook integrity. |

## Advanced

| Field | Type | Default | Description | Examples | Notes |
|-------|------|---------|-------------|----------|-------|
| `sqlite_backup.enabled` | boolean | `True` | Enable automatic backups of local SQLite state | `true`, `false` | Disable only if backups are managed externally. |
| `sqlite_backup.cadence_hours` | integer | `24` | Minimum hours between backups | `6`, `24`, `48` | Lower values increase backup frequency. |
| `sqlite_backup.check_interval_minutes` | integer | `60` | How often to check whether backups are due | `30`, `60`, `120` | Checks are lightweight; keep reasonably frequent. |
| `sqlite_backup.retention_daily` | integer | `14` | Number of daily backups to keep | `7`, `14`, `30` | Set to 0 to disable daily backups. |
| `sqlite_backup.retention_weekly` | integer | `8` | Number of weekly backups to keep | `4`, `8`, `12` | Set to 0 to disable weekly backups. |
| `sqlite_backup.enforce_on_startup` | boolean | `True` | If cadence elapsed, force a backup on startup | `true`, `false` | Keeps backups current if the process was stopped for a while. |
| `timeline.max_records` | integer | `5000` | Max timeline events kept per issue before trimming | `2000`, `5000`, `10000` | Set to 0 to disable trimming; higher values keep more history but grow state files faster. |
| `provider_resilience.short_retry.max_attempts` | integer | `4` | Max attempts for transient provider failures | `2`, `4`, `6` | Higher values reduce failures but can prolong degraded runs. |
| `provider_resilience.short_retry.initial_backoff_seconds` | integer | `5` | Initial backoff for transient provider retries | `2`, `5`, `10` | Shorter backoffs retry faster but can amplify rate limits. |
| `provider_resilience.short_retry.max_backoff_seconds` | integer | `60` | Maximum backoff for transient provider retries | `30`, `60`, `120` | Caps exponential backoff to avoid excessive waiting. |
| `provider_resilience.short_retry.jitter` | boolean | `True` | Apply full jitter to provider retry backoff | `true`, `false` | Keep enabled to avoid synchronized retry storms. |
| `provider_resilience.circuit_breaker.cooldown_seconds` | integer | `1800` | Cooldown window before retrying provider after outage | `600`, `1800`, `3600` | Longer cooldowns reduce repeated failures during incidents. |
| `provider_resilience.circuit_breaker.max_cooldowns` | integer | `6` | Maximum cooldown escalation steps | `3`, `6`, `8` | Limits how long we will keep extending cooldowns. |
| `provider_resilience.circuit_breaker.label` | string | `blocked:provider-unavailable` | Label applied when provider is unavailable | `blocked:provider-unavailable` | Use a label that is visible and searchable in your workflow. |
| `execution.session_interactions.enabled` | boolean | `False` | Allow the orchestrator to auto-respond to trusted prompts in running agent sessions | `true`, `false` | Off by default. Enable only if you want runner-managed prompt responses such as Claude's initial trust confirmation. |
| `observability.session_no_output_seconds` | integer | `120` | Emit event after this much idle time | `60`, `120`, `300` | Lower values surface silent sessions sooner. |
| `observability.stale_escalation_ticks` | integer | `0` | Escalate after K consecutive stale ticks (0 = disabled) | `0`, `3`, `5` | Set to 0 to disable automatic escalation. |
| `observability.session_output_retention_days` | integer | `7` | Retention window in days for session run artifacts | `0`, `7`, `30` | Set to 0 to expire immediately; cleanup policy may still defer deletion. |
| `observability.session_output_retention_tier` | string | `hot` | Retention tier tag recorded in run manifests | `hot`, `cold` | Use hot for short-term troubleshooting and cold for longer forensic retention. |
| `ui.web_port` | integer | `0` | Port for the web dashboard. 0 = auto-assign free port (requires restart) | `0`, `8080`, `3000`, `9090` | 0 = auto-assign a free port. Use a fixed port for bookmarkable URLs. |
| `ui.control_api_port` | integer | `0` | 0 = auto-assign free port | `0`, `19080`, `19081` | 0 = auto-assign a free port. Allows multiple instances to coexist. |
| `ui.browser_session.ttl_seconds` | integer | `28800` | How long a Control Center login is valid before it expires and the operator must re-enter the admin token. Overridable at runtime via ISSUE_ORCHESTRATOR_SESSION_TTL_SECONDS. | `3600`, `28800`, `86400` | Minimum 60 s. Shorter values reduce the window a stolen cookie is useful; longer values reduce re-login friction. |
| `ui.browser_session.max` | integer | `1024` | Deprecated. Browser sessions are now stateless cookies validated by HMAC, so there is no in-memory table to cap. The field is still accepted for back-compat with operator YAML but the value is ignored at runtime. | `1024` | Deprecated and ignored as of the cross-process session change. Stateless cookies removed the in-memory cap; this field exists only so existing YAML continues to validate. Safe to remove from your config. |
| `ui.browser_session.sse_token_ttl_seconds` | integer | `60` | How long a /api/sse-token response is valid before the browser must request a fresh one. Tokens are single-use within their window. Overridable via ISSUE_ORCHESTRATOR_SSE_TOKEN_TTL_SECONDS. | `30`, `60`, `300` | Shorter is safer — a token in an access log or Referer header becomes useless faster. The browser re-requests on every reconnect so operator-visible reconnection latency is unchanged. |
| `ai_systems.allowed` | string | `` | Additional ai_system values allowed in config (comma-separated) | `codex, custom-system` | Use to allow new providers beyond ai_systems.yaml. |
| `worktrees.base` | string | `../` | Directory where git worktrees are created | `../`, `../worktrees`, `/tmp/worktrees` | Relative paths are resolved from the repo root. |
| `worktrees.base_branch_override` | string (optional) | `None` | Override the base branch for worktree creation (auto-detect if unset) | `main`, `master` | Use when your default branch is not auto-detected correctly. |
| `worktrees.seed_ref` | string (optional) | `None` | Optional local ref used to seed fresh issue worktrees before review/PR creation | `HEAD`, `main`, `fc42d4c` | Use for local iteration when fresh issue worktrees should inherit a specific local ref. |
| `worktrees.worktree_branch_on_recreate` | string | `delete` | What to do when recreating a worktree with existing branch | `delete`, `create_new_branch` | Use create_new_branch to keep the old branch intact. |
| `worktrees.setup` | string | `` | Commands to run in each new worktree after creation (one per line) | `npm install`, `pip install -e '.[dev]'`, `make setup` | Each command runs in the worktree directory. Leave empty if no setup needed. The orchestrator's own setup (hooks, coding-done, reviewer-done, Claude settings) is automatic. |
<!-- END AUTO-GENERATED CONFIG REFERENCE -->
