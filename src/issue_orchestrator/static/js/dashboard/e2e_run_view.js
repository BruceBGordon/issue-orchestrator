// Single owner for row-scoped command context.  Every typed Command
// targeting an expanded E2E run row (``switch_e2e_timeline_view``,
// ``create_e2e_untriaged_issues``, and any future row-mounted action)
// resolves through this function — no handler reaches into the DOM
// directly to find the row or its data.
//
// Returns a frozen context exposing the row, the validated run id,
// the mounted ``_e2eRunData``, and the row-scoped DOM accessors
// (timeline container, view switcher, agent select).  Returns
// ``null`` when:
//   - ``runId`` is not a positive integer Number (mirrors the
//     typed Command's strict-int Pydantic contract: a string
//     ``"88"`` or a boolean ``true`` must be rejected here even if
//     they slipped past upstream validation — accepting them would
//     silently re-introduce the silent-coercion class of bugs the
//     strict contract exists to prevent),
//   - the trigger doesn't resolve to a ``details.e2e-run-row``, or
//   - the row's ``data-e2e-run-id`` disagrees with the Command's
//     ``run_id`` (would mean the typed payload and the rendered
//     DOM disagree about the target run — refuse to act).
//
// Handlers MUST early-return on ``null``; ``createIssuesForUntriaged``
// additionally toasts before bailing because the user clicked
// something.  ``switchE2ETimelineView`` silently no-ops because a
// programmatic dispatcher call shouldn't surface UI noise.
//
// The single ownership rule this enforces: row-targeting policy
// lives in this function, nowhere else.
function resolveRowCommandContext(runId, triggerEl) {
    // Strict-Number gate: mirrors the typed Pydantic Command's
    // ``strict=True`` invariant.  Reject string, boolean, NaN,
    // null/undefined, etc. before any conversion — only a real
    // JS number that is a positive integer is accepted.
    if (typeof runId !== 'number' || !Number.isInteger(runId) || runId <= 0) {
        return null;
    }
    const row = triggerEl && typeof triggerEl.closest === 'function'
        ? triggerEl.closest('details.e2e-run-row')
        : null;
    if (!row) return null;
    // The row's data-e2e-run-id comes from the DOM as a string;
    // parse strictly and require integer-equality with the Command.
    const rawRowId = row.dataset && row.dataset.e2eRunId;
    if (typeof rawRowId !== 'string' || !/^[1-9][0-9]*$/.test(rawRowId)) {
        return null;
    }
    const rowRunId = Number(rawRowId);
    if (rowRunId !== runId) return null;
    const numericRunId = runId;

    return Object.freeze({
        runId: numericRunId,
        row,
        get data() { return row._e2eRunData || null; },
        setData(next) { row._e2eRunData = next; },
        timelineContainer() { return row.querySelector('.e2e-timeline-content'); },
        viewSwitcher() { return row.querySelector('.e2e-timeline-view-switcher'); },
        agentSelect() { return row.querySelector('.unified-run-agent'); },
        // Mark the row for re-load on the next ``expand_e2e_run``
        // dispatch.  Used after a row-scoped POST that mutates
        // server-side state (Create-issues) — the row needs fresh
        // detail to reflect the new linked issues.
        markUnloaded() {
            if (row.dataset) row.dataset.loaded = '';
        },
    });
}

const E2E_LABEL_OVERRIDES = Object.freeze({
    pytest: 'Pytest',
    command: 'Command',
    junit_xml: 'JUnit XML',
    html_report: 'HTML Report',
    json_report: 'JSON Report',
});

function _emptyE2EResultCategories() {
    return {
        untriaged: [],
        has_issue: [],
        flaky: [],
        fixed: [],
        passed: [],
        quarantined: [],
        skipped: [],
    };
}

async function _fetchE2ERunDetail(runId, view = 'user') {
    const response = await fetch(`/api/e2e-run-detail/${runId}?view=${encodeURIComponent(view)}`);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
        const message = payload && typeof payload === 'object'
            ? payload.error || payload.detail || 'Failed to load run details'
            : 'Failed to load run details';
        throw new Error(String(message));
    }
    if (!payload || typeof payload !== 'object') {
        throw new Error('Run detail payload was not an object');
    }
    return payload;
}

// ``showUnifiedRunView`` (the modal mount path) and
// ``renderUnifiedRunView`` (its renderer) were removed in issue
// #6334 along with ``#e2eDiagnosisModal``.  The new mount path lives
// in ``e2e_runs_list.js → loadE2ERunIntoRow``: it lazy-fetches via
// the same ``_fetchE2ERunDetail`` helper exported below, mounts
// ``renderE2EResultsPanel(data)`` inline in the run's row, and runs
// the same accessibility / timeline enhancements.  The dispatcher
// re-routes ``open_e2e_run`` to ``expandE2ERunRow`` — single owner
// for "navigate the user to run #N".

function normalizeE2ETimelineData(timelineData) {
    timelineData = timelineData || {};
    const lifecycle = timelineData.lifecycle && typeof timelineData.lifecycle === 'object'
        ? timelineData.lifecycle
        : null;
    return {
        events: Array.isArray(timelineData.events) ? timelineData.events : [],
        phase_toc: Array.isArray(timelineData.phase_toc) ? timelineData.phase_toc : [],
        cycles: Array.isArray(timelineData.cycles) ? timelineData.cycles : [],
        issue_affordances: Array.isArray(timelineData.issue_affordances) ? timelineData.issue_affordances : [],
        lifecycle,
        error: timelineData.error || timelineData.detail || '',
    };
}

function _resultCategories(data) {
    if (!data || typeof data !== 'object') return _emptyE2EResultCategories();
    const payload = data.results_by_category && typeof data.results_by_category === 'object'
        ? data.results_by_category
        : {};
    return {
        untriaged: Array.isArray(payload.untriaged) ? payload.untriaged : [],
        has_issue: Array.isArray(payload.has_issue) ? payload.has_issue : [],
        flaky: Array.isArray(payload.flaky) ? payload.flaky : [],
        fixed: Array.isArray(payload.fixed) ? payload.fixed : [],
        passed: Array.isArray(payload.passed) ? payload.passed : [],
        quarantined: Array.isArray(payload.quarantined) ? payload.quarantined : [],
        skipped: Array.isArray(payload.skipped) ? payload.skipped : [],
    };
}

// ``_allResultCases`` and ``_findResultCase`` removed in Phase C
// (PR #6319 Blocker 2) — they were only used by the deleted
// ``copyTestErrorFromRun`` handler.

function _humanizeSnakeCase(value) {
    const text = String(value || '').trim();
    if (!text) return '';
    const normalized = text.toLowerCase();
    if (Object.prototype.hasOwnProperty.call(E2E_LABEL_OVERRIDES, normalized)) {
        return E2E_LABEL_OVERRIDES[normalized];
    }
    return text
        .replace(/_/g, ' ')
        .replace(/\b\w/g, char => char.toUpperCase());
}

function _runStatusClass(status) {
    const normalized = String(status || '').toLowerCase();
    if (normalized === 'passed') return 'passed';
    if (normalized === 'warning') return 'warning';
    if (normalized === 'running') return 'running';
    if (normalized === 'failed' || normalized === 'error') return 'failed';
    return 'muted';
}

function _formatRunnerLabel(run) {
    const runnerKind = String(run && run.runner_kind || '').trim();
    if (!runnerKind) return 'Unknown runner';
    return _humanizeSnakeCase(runnerKind);
}

function _formatRunCommand(run) {
    const command = Array.isArray(run && run.command) ? run.command : [];
    if (command.length > 0) return command.join(' ');
    const pytestArgs = Array.isArray(run && run.pytest_args) ? run.pytest_args : [];
    if (pytestArgs.length > 0) return ['pytest', ...pytestArgs].join(' ');
    return '';
}

function _formatDurationSeconds(value) {
    if (typeof value !== 'number' || !Number.isFinite(value)) return '';
    return `${value.toFixed(1)}s`;
}

function _artifactButton(path, label, cssClass = 'issue-action-btn') {
    if (!path) return '';
    return `<button class="${cssClass}" data-artifact-path="${escapeAttr(path)}" onclick="openE2EArtifactFromButton(this); event.stopPropagation();">${escapeHtml(label)}</button>`;
}

function openE2EArtifactFromButton(button) {
    const path = button && button.dataset ? String(button.dataset.artifactPath || '').trim() : '';
    if (!path) {
        throw new Error('Artifact button missing data-artifact-path');
    }
    if (!window || typeof window.openPath !== 'function') {
        throw new Error('openPath is unavailable');
    }
    window.openPath(path);
}

// Legacy ``_e2eRowActionButton`` / ``runE2ERowActionFromButton``
// removed in Phase C (PR #6319 Blocker 2).  Per-row triage actions
// (Close issue, Create issue, Quarantine, Copy error) are no longer
// the row-action contract: Copy-error is now a built-in icon on the
// canonical viewer's failure card, the linked-issue affordance is
// the ``io.agent-context`` plugin block, and quarantine is deferred
// to issue #6318.

// ``_primaryRunReport`` removed in Phase C — it picked a primary
// report among the run's artifacts but had no remaining caller after
// the per-row triage UI went away.

function _runArtifactDescriptors(data) {
    const run = data && data.run ? data.run : {};
    const reports = Array.isArray(data && data.reports) ? data.reports : [];
    const artifacts = Array.isArray(data && data.artifacts) ? data.artifacts : [];
    const descriptors = [];
    // Dedupe by ``path``. The backend builds ``reports`` as a subset of
    // ``artifacts`` (and the raw-output log can also be re-collected as a
    // config-driven artifact), so the same file arrives on multiple arrays.
    // After the JSON round-trip those are distinct object instances, so an
    // identity check (``reports.includes(artifact)``) never filters them —
    // ``path`` is the only stable key across the arrays. First occurrence wins,
    // and because we push raw-output → report-class → remaining artifacts in
    // that order, each file keeps its most specific label/style (issue #6593).
    const seenPaths = new Set();
    const pushDescriptor = (path, label, cssClass) => {
        if (!path || seenPaths.has(path)) return;
        seenPaths.add(path);
        descriptors.push({ path, label, cssClass });
    };

    if (run.log_path) {
        pushDescriptor(run.log_path, 'Raw Output', 'issue-action-btn');
    }
    for (const report of reports) {
        if (!report || !report.path) continue;
        pushDescriptor(report.path, report.label || _humanizeSnakeCase(report.kind), 'issue-action-btn');
    }
    for (const artifact of artifacts) {
        if (!artifact || !artifact.path) continue;
        pushDescriptor(artifact.path, artifact.label || artifact.path, 'issue-action-btn subtle');
    }
    return descriptors;
}

function _renderArtifactDescriptorButtons(artifacts) {
    return artifacts
        .map((artifact) => _artifactButton(artifact.path, artifact.label, artifact.cssClass))
        .join('');
}

function _renderRunArtifactButtons(data) {
    return _renderArtifactDescriptorButtons(_runArtifactDescriptors(data));
}

// Normalize the run-detail payload's ``artifact_diagnostic`` into a plain
// ``{state, collected_count, configured_glob_count}`` object, or ``null`` when
// the field is absent (slimmer JS-vm payloads).  The backend classifies which
// of three situations produced the run's artifact set; the UI only renders the
// resulting hint (policy stays below the UI, per static/CLAUDE.md).
function _artifactDiagnostic(data) {
    const raw = data && data.artifact_diagnostic;
    if (!raw || typeof raw !== 'object') return null;
    const state = String(raw.state || '');
    if (state !== 'collected' && state !== 'globs_matched_nothing' && state !== 'not_configured') {
        return null;
    }
    return {
        state,
        collected_count: Number.isInteger(raw.collected_count) ? raw.collected_count : 0,
        configured_glob_count: Number.isInteger(raw.configured_glob_count) ? raw.configured_glob_count : 0,
    };
}

// Human-facing explanation for the two "no config-driven artifacts" states.
// ``collected`` needs no note — the artifact buttons speak for themselves — so
// this returns '' for it (and for a missing/unknown diagnostic).
function _renderArtifactDiagnosticNote(diagnostic) {
    if (!diagnostic) return '';
    if (diagnostic.state === 'not_configured') {
        return `<p class="e2e-artifacts-note e2e-artifacts-note-info">`
            + `No artifact globs are configured for this repo's E2E runner. `
            + `Set <code>e2e.artifact_paths</code> / <code>e2e.junit_xml_paths</code> to collect logs, summaries, and reports.`
            + `</p>`;
    }
    if (diagnostic.state === 'globs_matched_nothing') {
        const plural = diagnostic.configured_glob_count === 1 ? 'glob is' : 'globs are';
        return `<p class="e2e-artifacts-note e2e-artifacts-note-warn">`
            + `${diagnostic.configured_glob_count} artifact ${plural} configured, but none matched files in this run. `
            + `Check the E2E runner's <code>artifact_paths</code> / <code>junit_xml_paths</code> patterns.`
            + `</p>`;
    }
    return '';
}

// First-class "Run artifacts" section.  Issue #6593: collected
// ``e2e_run_artifacts`` records were reachable only inside the collapsed
// "Diagnostics" disclosure, so an operator staring at a failed testcase could
// not discover the raw E2E output log, container logs, summaries, or generated
// JUnit XML.  This section renders those drill-downs prominently — adjacent to
// the failure panel (mounted right above the canonical viewer body) — and,
// when no config-driven artifacts were collected, explains why via the typed
// backend diagnostic.  The raw-output log is always present, so the section
// never renders empty.
function _renderRunArtifactsSection(data) {
    const descriptors = _runArtifactDescriptors(data);
    const buttons = _renderArtifactDescriptorButtons(descriptors);
    const note = _renderArtifactDiagnosticNote(_artifactDiagnostic(data));
    if (!buttons && !note) return '';
    const count = descriptors.length;
    const countChip = count > 0
        ? `<span class="e2e-run-artifacts-count">${count} file${count === 1 ? '' : 's'}</span>`
        : '';
    return `
        <section class="e2e-run-artifacts" aria-label="Run artifacts">
            <div class="e2e-run-artifacts-head">
                <span class="e2e-run-artifacts-title">Run artifacts</span>
                ${countChip}
            </div>
            ${note}
            ${buttons ? `<div class="e2e-action-row e2e-run-artifacts-buttons">${buttons}</div>` : ''}
        </section>
    `;
}

// Legacy helpers removed in Phase C (PR #6319 Blocker 2):
//   ``_normalizeE2ERunLifecycle`` / ``_lifecycleSessionCommand`` /
//   ``_lifecycleValidationCommand`` / ``_lifecycleReviewTranscriptCommand``
//   — per-row inline lifecycle block; superseded by the
//   ``io.agent-context`` plugin's drawer-open affordance.
//   ``_flattenTestsByCategory`` / ``_lifecyclesByIssueNumber`` —
//   helpers for the old categorized panel.
//   ``_e2eCapturedOutputUrl`` — captured-output URL builder for the
//   legacy panel's lazy-fetch path.
//   ``_renderE2ETestRowActions`` / ``_renderE2EIssueLifecycleBlock`` —
//   per-row triage actions; replaced by the canonical viewer +
//   ``io.agent-context`` plugin.
//
// ``_renderLifecycleCommandButton`` / ``runLifecycleCommandFromButton``
// / ``runLifecycleCommand`` live in the shared
// ``static/js/dashboard/lifecycle_commands.js`` module (loaded before
// this file).

// Phase C (issue #6310 follow-up): the E2E run view's body is now the
// canonical validation viewer.  We translate the E2E run payload to the
// JUnit-canonical shape (via ``e2eRunToCanonicalPayload``), then mount
// the shared viewer.  The filter pills, bulk-action bar, and per-row
// triage actions from the legacy panel are gone — orchestrator-
// specific affordances (Open issue drawer, Create issue, Agent
// journey) live in the per-test ``io.agent-context`` plugin block now;
// the Copy-error button is a built-in canonical-viewer action.
//
// Quarantine UI is deferred to issue #6318 (its design wasn't ready;
// the existing quarantine API + sidebar entrypoints continue to work,
// they're just not surfaced in the redesigned run modal).
function renderE2EResultsPanel(data) {
    const canonical = e2eRunToCanonicalPayload(data);
    const untrackedCount = _untrackedFailureCount(data);
    const viewerHtml = renderCanonicalValidationViewer(canonical);
    // Issue #6334 round-2: thread the run id through every action
    // surface that used to read from the module-level
    // ``unifiedRunData`` singleton.  Typed Commands carry the run
    // id explicitly; the dispatcher resolves the row from
    // ``triggerEl`` and reads the row-scoped DOM (no global ids).
    const runId = (data && data.run && data.run.id) ? Number(data.run.id) : 0;

    return `
        <div class="e2e-canonical-panel">
            ${_renderRunSummaryChips(data, canonical)}
            ${untrackedCount > 0 ? _renderUntrackedFailuresBanner(untrackedCount, runId) : ''}
            ${_renderRunArtifactsSection(data)}
            <div class="e2e-canonical-body">${viewerHtml}</div>
            ${renderRunDetailsDisclosure(data, runId)}
        </div>
    `;
}

// Run-level summary chips.  Info display only — NOT filter pills.
// Layout: outcome chip + counts + (optional) command/duration meta.
function _renderRunSummaryChips(data, canonical) {
    const rawStatus = String(data && data.run && data.run.status || canonical.status || '').toLowerCase();
    const status = rawStatus === 'passed' || rawStatus === 'warning' ? rawStatus : 'failed';
    const totalCases = canonical.junit_cases.length;
    const activeCases = canonical.junit_cases.filter(c => c && c.is_quarantined !== true);
    const quarantinedCount = canonical.junit_cases.length - activeCases.length;
    const failedCount = canonical.failed_tests.length;
    const passedCount = activeCases.filter(c => c.outcome === 'passed').length;
    const skippedCount = activeCases.filter(c => c.outcome === 'skipped').length;
    const command = _formatRunCommand(data && data.run);
    const duration = data && data.run ? _formatDurationSeconds(data.run.duration_seconds) : '';

    const chips = [
        `<span class="e2e-run-chip e2e-run-chip-${status}">${status}</span>`,
        `<span class="e2e-run-chip">${totalCases} case${totalCases === 1 ? '' : 's'}</span>`,
    ];
    if (failedCount > 0) chips.push(`<span class="e2e-run-chip is-fail">${failedCount} failing</span>`);
    if (quarantinedCount > 0) chips.push(`<span class="e2e-run-chip muted">${quarantinedCount} quarantined</span>`);
    chips.push(`<span class="e2e-run-chip">${passedCount} passing</span>`);
    if (skippedCount > 0) chips.push(`<span class="e2e-run-chip muted">${skippedCount} skipped</span>`);

    const meta = (command || duration)
        ? `<span class="e2e-run-summary-meta">${command ? escapeHtml(command) : ''}${command && duration ? ' · ' : ''}${duration ? escapeHtml(duration) : ''}</span>`
        : '';
    return `<div class="e2e-run-summary">${chips.join('')}${meta}</div>`;
}

// Untracked-failures banner: rendered when at least one failing
// test has no linked issue.  Carries a typed
// ``CreateE2EUntriagedIssuesCommand`` and the row-scoped agent
// select; the handler resolves both through
// ``resolveRowCommandContext``.
function _renderUntrackedFailuresBanner(untrackedCount, runId) {
    const agentSelect = (window && window.dashboardData && Array.isArray(window.dashboardData.agents))
        ? window.dashboardData.agents.map(a => `<option value="${escapeAttr(a)}">${escapeHtml(a)}</option>`).join('')
        : '';
    const plural = untrackedCount === 1 ? 'test has' : 'tests have';
    const buttonLabel = `Create issue${untrackedCount === 1 ? '' : 's'}`;
    const command = {
        kind: 'create_e2e_untriaged_issues',
        label: buttonLabel,
        run_id: Number(runId),
    };
    const cmdAttr = escapeAttr(JSON.stringify(command));
    return `
        <div class="e2e-untracked-banner">
            <span class="e2e-untracked-banner-text">🎯 ${untrackedCount} failing ${plural} no linked issue</span>
            <div class="e2e-untracked-banner-actions">
                <select class="agent-select unified-run-agent">
                    <option value="">Select agent…</option>
                    ${agentSelect}
                </select>
                <button class="btn-primary" data-lifecycle-command="${cmdAttr}" onclick="runLifecycleCommandFromButton(this); event.stopPropagation();">${escapeHtml(buttonLabel)}</button>
            </div>
        </div>
    `;
}

function _untrackedFailureCount(data) {
    const untriaged = (data && data.results_by_category && Array.isArray(data.results_by_category.untriaged))
        ? data.results_by_category.untriaged
        : [];
    return untriaged.length;
}

function renderRunDetailsDisclosure(data, runId) {
    const run = data && data.run ? data.run : {};
    const command = _formatRunCommand(run);
    const numericRunId = Number(runId || (run && run.id) || 0);
    // Run artifacts moved to the first-class ``_renderRunArtifactsSection``
    // above the failure panel (issue #6593), so this diagnostics disclosure no
    // longer duplicates the artifact buttons — it carries runner metadata,
    // command, and the timeline diagnostics only.

    // Each view button carries a typed ``SwitchE2ETimelineViewCommand``;
    // the dispatcher routes through ``resolveRowCommandContext`` so
    // the click updates only this row's timeline container.
    function _viewButton(view, label, isActive) {
        const cmd = {
            kind: 'switch_e2e_timeline_view',
            label: `Switch diagnostics timeline to ${label}`,
            run_id: numericRunId,
            view,
        };
        const cmdAttr = escapeAttr(JSON.stringify(cmd));
        const cssClass = isActive ? 'e2e-view-btn active' : 'e2e-view-btn';
        return (
            `<button class="${cssClass}" ` +
            `data-lifecycle-command="${cmdAttr}" ` +
            `onclick="runLifecycleCommandFromButton(this); event.stopPropagation();" ` +
            `data-view="${view}">${escapeHtml(label)}</button>`
        );
    }

    return `
        <details class="run-details-disclosure run-diagnostics-row">
            <summary>
                <span class="rdd-summary-main">
                    <span class="rdd-summary-title">Diagnostics</span>
                    <span class="rdd-summary-hint">runner · command · timeline</span>
                </span>
                <span class="rdd-summary-chips" aria-hidden="true">
                    <span class="rdd-summary-chip">${escapeHtml(_formatRunnerLabel(run))}</span>
                    <span class="rdd-summary-chip">Timeline</span>
                </span>
            </summary>
            <div class="rdd-body">
                <div class="rdd-grid">
                    <div class="rdd-row"><span class="rdd-label">Runner</span><span class="rdd-value">${escapeHtml(_formatRunnerLabel(run))}</span></div>
                    <div class="rdd-row"><span class="rdd-label">Status</span><span class="rdd-value"><span class="e2e-run-status ${_runStatusClass(run.status)}">${escapeHtml(_humanizeSnakeCase(run.status || 'unknown'))}</span></span></div>
                    <div class="rdd-row"><span class="rdd-label">Started</span><span class="rdd-value">${escapeHtml(formatTimestamp(run.started_at) || 'Unknown')}</span></div>
                    <div class="rdd-row"><span class="rdd-label">Duration</span><span class="rdd-value">${escapeHtml(_formatDurationSeconds(run.duration_seconds) || '—')}</span></div>
                    ${run.commit_sha ? `<div class="rdd-row"><span class="rdd-label">Commit</span><span class="rdd-value"><code>${escapeHtml(String(run.commit_sha).substring(0, 12))}</code></span></div>` : ''}
                    ${run.branch ? `<div class="rdd-row"><span class="rdd-label">Branch</span><span class="rdd-value"><code>${escapeHtml(run.branch)}</code></span></div>` : ''}
                    <div class="rdd-row rdd-command-row"><span class="rdd-label">Command</span><span class="rdd-value">${command ? `<code class="e2e-run-command">${escapeHtml(command)}</code>` : 'Unavailable'}</span></div>
                </div>
                <div class="rdd-timeline">
                    <div class="rdd-section-title">Timeline diagnostics</div>
                    <div class="e2e-timeline-view-switcher">
                        ${_viewButton('user', 'Story', true)}
                        ${_viewButton('ops', 'Ops', false)}
                        ${_viewButton('debug', 'Debug', false)}
                    </div>
                    <div class="e2e-timeline-content"></div>
                </div>
            </div>
        </details>
    `;
}

function openE2ERunTimeline(runId) {
    // Expand the matching ``<details>`` row in the inline runs list
    // and auto-open the nested Diagnostics row (which holds the
    // timeline diagnostics).  Routed through the typed
    // Command pipeline (issue #6322, PR #6329 reviewer Blocker 2;
    // re-pointed at ``expandE2ERunRow`` in issue #6334) — single
    // owner for "open E2E run" navigation.
    return runLifecycleCommand({
        kind: 'open_e2e_run',
        label: 'Open E2E Run',
        run_id: Number(runId),
        expand_run_details: true,
    });
}

// ``renderUnifiedRunView`` was removed in issue #6334.  Its
// responsibilities (mount the canonical viewer, attach ARIA
// enhancements, render the diagnostics timeline, optionally expand the
// Diagnostics row) now live in
// ``loadE2ERunIntoRow`` (canonical viewer + ARIA + timeline) and
// ``expandE2ERunRow`` (post-mount disclosure expansion) in
// ``e2e_runs_list.js``.

function renderE2ETimeline(container, timelineData) {
    if (!container) return;
    const tl = normalizeE2ETimelineData(timelineData || {});
    applyLifecycleDataset(container, tl.lifecycle);
    container.innerHTML = `
        ${renderE2EIssueTimelineAffordances(tl.issue_affordances)}
        ${tl.error ? `<div class="timeline-empty e2e-timeline-error">${escapeHtml(tl.error)}</div>` : ''}
        <div class="e2e-timeline-events"></div>
    `;
    const eventsContainer = container.querySelector('.e2e-timeline-events');
    renderTimeline(eventsContainer, tl.events, tl.phase_toc, tl.cycles);
}

function renderE2EIssueTimelineAffordances(affordances) {
    // PR #6319 round 4: the run-level issue-timeline affordance now
    // routes through the shared typed-Command pipeline
    // (``_renderLifecycleCommandButton`` →
    // ``runLifecycleCommandFromButton`` → ``runLifecycleCommand``
    // → ``openIssueTimeline``).  The previous inline ``onclick``
    // path was a second owner for the same UI command — the
    // ``open_issue_timeline`` kind already has a typed shape with
    // ``scope_kind: 'e2e_run'`` / ``e2e_run_id``, so the dispatcher
    // can route it.
    const items = (Array.isArray(affordances) ? affordances : [])
        .map((affordance) => {
            const issueNumber = Number(affordance.issue_number);
            const runId = Number(affordance.run_id);
            if (!Number.isInteger(issueNumber) || !Number.isInteger(runId)) return '';
            const label = affordance.label ? String(affordance.label) : '';
            // The label inside the button matches the prior visual
            // shape: ``#N`` + optional human label, both rendered
            // as a single inline string.  ``_renderLifecycleCommandButton``
            // does its own escapeHtml, so the labelHtml chunk has
            // to be assembled into a plain string label (the typed
            // renderer doesn't take inner-HTML overrides).  To
            // preserve the two-span layout we emit a custom button
            // that still carries ``data-lifecycle-command`` — the
            // shared dispatcher reads only the data attribute.
            const cmd = {
                kind: 'open_issue_timeline',
                issue_number: issueNumber,
                scope_kind: 'e2e_run',
                e2e_run_id: runId,
                label: `Open cycle timeline for issue #${issueNumber}`,
            };
            const labelHtml = label
                ? `<span class="e2e-issue-timeline-label">${escapeHtml(label)}</span>`
                : '';
            const cmdAttr = escapeAttr(JSON.stringify(cmd));
            return `<button class="e2e-issue-timeline-btn"
                data-lifecycle-command="${cmdAttr}"
                onclick="runLifecycleCommandFromButton(this); event.stopPropagation();"
                title="Open cycle timeline for issue #${issueNumber}"
                aria-label="Open cycle timeline for issue #${issueNumber}">
                <span class="e2e-issue-timeline-number">#${issueNumber}</span>${labelHtml}
            </button>`;
        })
        .filter(Boolean);
    const body = items.length
        ? `<div class="e2e-issue-timeline-list">${items.join('')}</div>`
        : '<div class="e2e-empty-note">No linked issue timelines for this run.</div>';
    return `<section class="e2e-issue-timeline-affordances" aria-label="Issue timelines from this E2E run">
        <div class="e2e-issue-timeline-title">Issue timelines</div>
        ${body}
    </section>`;
}

// Typed-Command target for the Story/Ops/Debug view switcher.
// All row resolution + scoping flows through
// ``resolveRowCommandContext`` — no duplicate ``triggerEl.closest``
// or document-global queries.
async function switchE2ETimelineView(runId, view, triggerEl) {
    const ctx = resolveRowCommandContext(runId, triggerEl);
    if (!ctx) return;

    const switcher = ctx.viewSwitcher();
    if (switcher) {
        switcher.querySelectorAll('.e2e-view-btn').forEach((b) => b.classList.remove('active'));
        if (triggerEl) triggerEl.classList.add('active');
    }

    const container = ctx.timelineContainer();
    if (!container) return;
    container.innerHTML = '<div class="loading-spinner">Loading...</div>';

    try {
        const data = await _fetchE2ERunDetail(ctx.runId, view);
        ctx.setData(data);
        renderE2ETimeline(container, data);
    } catch (err) {
        container.innerHTML = `<div style="color: var(--danger);">Error: ${escapeHtml(err.message)}</div>`;
    }
}

// Legacy ``_copyTextToClipboard`` + ``copyTestErrorFromRun`` removed
// in Phase C (PR #6319 Blocker 2).  The canonical viewer renders a
// built-in Copy-error icon on every failed triage card and uses
// ``navigator.clipboard.writeText`` directly; that's the single
// owner now.

/**
 * Typed-Command target for the row's untracked-failures banner.
 * All row resolution flows through ``resolveRowCommandContext``;
 * the agent comes from the row-scoped ``.unified-run-agent`` select.
 * Refresh after POST re-fires the typed ``expand_e2e_run`` Command
 * so the row reload routes through the single owner.
 */
async function createIssuesForUntriaged(runId, triggerEl) {
    const ctx = resolveRowCommandContext(runId, triggerEl);
    if (!ctx) {
        showToast('Unable to resolve the active run row', true);
        return;
    }
    if (!ctx.data || !ctx.data.run) {
        showToast('Run data not loaded yet', true);
        return;
    }

    const agentSelect = ctx.agentSelect();
    const agent = agentSelect ? agentSelect.value : '';
    if (!agent) {
        showToast('Please select an agent', true);
        if (agentSelect) agentSelect.focus();
        return;
    }

    const untriaged = _resultCategories(ctx.data).untriaged || [];
    if (untriaged.length === 0) {
        showToast('No tests need action', true);
        return;
    }

    const nodeids = untriaged.map((t) => t.nodeid);

    try {
        const res = await fetch(
            `/control/e2e/create-issues/${ctx.runId}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}&mode=${encodeURIComponent(CONFIG_MODE)}`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ nodeids, agent }),
            },
        );
        const data = await res.json();

        if (!res.ok) {
            showToast(data.error || data.detail || 'Failed to create issues', true);
            return;
        }

        showToast(`Created parent issue #${data.parent_issue.number} with ${data.sub_issues.length} sub-issue(s)`);

        ctx.markUnloaded();
        runLifecycleCommand(
            {
                kind: 'expand_e2e_run',
                label: 'Expand E2E Run',
                run_id: ctx.runId,
            },
            ctx.row,
        );

        if (data.parent_issue.url) {
            window.open(data.parent_issue.url, '_blank');
        }
    } catch (err) {
        showToast('Failed to create issues: ' + err.message, true);
    }
}

// Legacy per-test action handlers removed in Phase C (PR #6319
// Blocker 2):
//   ``closeE2EIssue``           — Phase C dropped per-test Close-Issue.
//                                 Issues close at orchestrator publish
//                                 time, not from the dashboard.
//   ``showCreateIssueDropdown`` — Phase C dropped per-row Create-Issue
//                                 dropdown.  The bulk
//                                 ``createIssuesForUntriaged`` flow
//                                 (driven by the untracked-failures
//                                 banner) is the single owner now.
//   ``createSingleIssueWithAgent``  — same; superseded by the bulk
//                                 flow + the canonical viewer's
//                                 per-test plugin block (which
//                                 navigates to the per-issue drawer
//                                 for further triage).
