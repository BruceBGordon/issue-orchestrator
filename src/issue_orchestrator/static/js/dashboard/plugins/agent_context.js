// Phase-0 plugin: issue-orchestrator agent context.
//
// Registered under ``io.agent-context``.  Activated when a JUnit test
// case carries an entry like:
//
//   case.extras = [{
//     namespace: "io.agent-context",
//     payload: {
//       issue_number: 4503,
//       issue_title: "fixture: cohort split",
//       final_state: "blocked",
//       summary: "agent retried 2x then blocked on validation",
//     }
//   }]
//
// The linked-issue drill-in is the inline ``▸ Attempts on issue #N``
// expander defined in ``inline_agent_attempts.js`` (issue #6322):
// rather than teleporting to a per-issue drawer, the user expands the
// agent's Attempts → Cycles tree inline beneath the failure summary.
// Pure-JUnit consumers without that module (tixmeup et al.) see the
// summary fields and nothing else — the expander silently drops in.
//
// See ``docs/journeys/validation-viewer-redesign.md`` for the why.

(function () {
    const root = typeof window !== 'undefined' ? window : globalThis;
    const canRegisterValidationPlugin = typeof registerValidationPlugin === 'function';

    function _callTimelineOption(options, name, ...args) {
        const fn = options && options[name];
        return typeof fn === 'function' ? fn(...args) : null;
    }

    function _issueLifecycleHostFunction(options, name) {
        const host = options && options.host && typeof options.host === 'object'
            ? options.host
            : null;
        const direct = host && host[name];
        if (typeof direct === 'function') return direct;
        if (typeof getHierarchicalTimelineHostCapability === 'function') {
            const capability = getHierarchicalTimelineHostCapability(name);
            if (typeof capability === 'function') return capability;
        }
        return null;
    }

    const _ISSUE_LIFECYCLE_VALIDATION_EVENT_NAMES = new Set([
        'validation.passed',
        'validation.failed',
        'session.validation_passed',
        'session.validation_failed',
    ]);

    function _defaultIssueLifecycleRunLabel(run, ctx) {
        return run.run_label || `Run ${run.run_number || (ctx.runIndex + 1)}`;
    }

    function _defaultIssueLifecycleCycleLabel(cycle, ctx) {
        const displayCycleNumber = cycle.cycle_in_run || cycle.cycle || cycle.cycle_number || (ctx.cycleIndex + 1);
        return cycle.cycle_label || `Cycle ${displayCycleNumber}`;
    }

    function _issueLifecycleOutcomeBadge(outcome, fallbackLabel = 'In progress') {
        return typeof readHierarchicalOutcomeBadge === 'function'
            ? readHierarchicalOutcomeBadge(outcome, fallbackLabel)
            : { label: fallbackLabel, tone: 'neutral', toneClass: '' };
    }

    function _issueLifecycleToneGlyph(tone) {
        return typeof hierarchicalToneGlyph === 'function'
            ? hierarchicalToneGlyph(tone)
            : (tone === 'neutral' ? '·' : '✓');
    }

    function _formatIssueLifecycleHeaderTimestamp(timestamp, fallback, options) {
        const formatter = _issueLifecycleHostFunction(options, 'formatHeaderTimestamp');
        return formatter ? formatter(timestamp, fallback) : (fallback || timestamp || '');
    }

    function _formatIssueLifecycleStepTimestamp(timestamp, fallback, options) {
        const formatter = _issueLifecycleHostFunction(options, 'formatStepTimestamp');
        return formatter ? formatter(timestamp, fallback) : (fallback || timestamp || '');
    }

    function _isIssueLifecycleValidationStep(step, options) {
        const custom = _callTimelineOption(options, 'isValidationStep', step);
        if (typeof custom === 'boolean') return custom;
        if (!step || typeof step !== 'object') return false;
        const name = String(step.event || '').toLowerCase();
        return _ISSUE_LIFECYCLE_VALIDATION_EVENT_NAMES.has(name);
    }

    function _defaultIssueLifecycleStepActions(step, options) {
        const raw = _callTimelineOption(options, 'filterStepActions', step);
        const actions = Array.isArray(raw)
            ? raw
            : (Array.isArray(step && step.actions) ? step.actions : []);
        const filtered = _isIssueLifecycleValidationStep(step, options)
            ? actions.filter((action) => !action || action.type !== 'open_validation_failure')
            : actions;
        const renderActions = _issueLifecycleHostFunction(options, 'renderEventActions');
        return renderActions ? renderActions(filtered) : '';
    }

    function _findIssueLifecycleCycleRunDir(cycle, options) {
        const custom = _callTimelineOption(options, 'findCycleRunDir', cycle);
        if (typeof custom === 'string') return custom;
        if (!cycle) return '';
        if (cycle.artifacts && typeof cycle.artifacts === 'object' && typeof cycle.artifacts.run_dir === 'string') {
            return cycle.artifacts.run_dir;
        }
        const phaseGroups = Array.isArray(cycle.phase_groups) ? cycle.phase_groups : [];
        for (const group of phaseGroups) {
            for (const step of (group.steps || [])) {
                if (step && typeof step.run_dir === 'string' && step.run_dir) return step.run_dir;
            }
        }
        for (const step of (cycle.steps || [])) {
            if (step && typeof step.run_dir === 'string' && step.run_dir) return step.run_dir;
        }
        return '';
    }

    function _issueLifecycleValidationToggleAttrs(stepId, bodyId, issueNumber, stepRunDir) {
        const baseAttrs = [
            `id="${escapeAttr(`${stepId}-toggle`)}"`,
            `aria-expanded="false"`,
            `aria-controls="${escapeAttr(bodyId)}"`,
        ];
        if (!Number.isInteger(issueNumber) || issueNumber <= 0) {
            return `${baseAttrs.join(' ')} disabled aria-disabled="true"`;
        }
        const toggleCall = `toggleValidationEventInline(${JSON.stringify(stepId)}, ${issueNumber}, ${JSON.stringify(stepRunDir)})`;
        return `${baseAttrs.join(' ')} onclick="${escapeAttr(toggleCall)}"`;
    }

    function _renderIssueLifecycleRunSummary(run, ctx, options) {
        const outcome = _issueLifecycleOutcomeBadge(run && run.outcome);
        const runLabel = _callTimelineOption(options, 'runLabel', run, ctx) || _defaultIssueLifecycleRunLabel(run, ctx);
        const icon = options && options.showToneIcon
            ? `<span class="cvv-ico cvv-ico-${outcome.tone}" aria-hidden="true">${_issueLifecycleToneGlyph(outcome.tone)}</span>`
            : '';
        const meta = _callTimelineOption(options, 'runMeta', run, ctx) || '';
        return `
        ${icon}
        <span class="journey-cycle-label">${escapeHtml(runLabel)}</span>
        <span class="journey-cycle-outcome ${outcome.toneClass}">\u2014 ${escapeHtml(outcome.label)}</span>
        ${meta}
        <span class="journey-cycle-time">${escapeHtml(_formatIssueLifecycleHeaderTimestamp(run.timestamp || '', run.time_label || '', options))}</span>`;
    }

    function _renderIssueLifecycleCycleSummary(cycle, ctx, options) {
        const cycleLabel = _callTimelineOption(options, 'cycleLabel', cycle, ctx) || _defaultIssueLifecycleCycleLabel(cycle, ctx);
        const outcome = _issueLifecycleOutcomeBadge(cycle && cycle.outcome);
        const agentPill = cycle && cycle.agent ? `<span class="journey-cycle-agent">(${escapeHtml(cycle.agent)})</span>` : '';
        const retryCount = Number(cycle && cycle.retry_count ? cycle.retry_count : 0);
        const retryInfo = retryCount > 0
            ? `<span class="journey-cycle-retries">${retryCount} ${retryCount === 1 ? 'retry' : 'retries'}</span>`
            : '';
        const icon = options && options.showToneIcon
            ? `<span class="cvv-ico cvv-ico-${outcome.tone}" aria-hidden="true">${_issueLifecycleToneGlyph(outcome.tone)}</span>`
            : '';
        const validationBadge = _callTimelineOption(
            options,
            'renderCycleValidationBadge',
            cycle && cycle.validation,
            options && options.issueNumber,
            cycle,
            ctx,
        ) || '';
        const summaryExtras = _callTimelineOption(options, 'renderCycleSummaryExtras', cycle, ctx) || '';
        const trailing = _callTimelineOption(options, 'renderCycleTrailingActions', cycle, ctx) || '';
        return `
        ${icon}
        <span class="journey-cycle-label">${escapeHtml(cycleLabel)}</span>
        ${agentPill}
        ${retryInfo}
        <span class="journey-cycle-outcome ${outcome.toneClass}">\u2014 ${escapeHtml(outcome.label)}</span>
        ${validationBadge}
        ${summaryExtras}
        <span class="journey-cycle-time">${escapeHtml(_formatIssueLifecycleHeaderTimestamp(cycle.timestamp || '', cycle.time_label || '', options))}</span>
        ${trailing}`;
    }

    function _renderIssueLifecycleStep(step, ctx, options) {
        const stepId = `${ctx.cycleId}-step-${ctx.stepIndex}`;
        const inProgress = !!(step && step.in_round_progress);
        const progressClass = inProgress ? ' journey-step-in-progress' : '';
        const statusClass = (step && step.status ? 'status-' + escapeAttr(step.status) : '') + progressClass;
        // Live-progress rows get a textual "In progress" badge (not colour
        // alone) so the Story timeline visibly advances during an in-round
        // rework instead of freezing on "Code review started" (issue #6428).
        const progressBadge = inProgress
            ? '<span class="journey-progress-badge" role="status">'
              + '<span class="journey-progress-dot" aria-hidden="true"></span>In progress</span>'
            : '';
        const detail = step && step.detail ? `<div class="journey-detail">${escapeHtml(step.detail)}</div>` : '';
        const actions = _defaultIssueLifecycleStepActions(step, options);
        const evidence = typeof renderTimelineEvidence === 'function'
            ? renderTimelineEvidence(step && step.evidence)
            : '';
        const narrative = escapeHtml((step && (step.narrative || step.event)) || '');
        const timestamp = escapeHtml(_formatIssueLifecycleStepTimestamp(
            (step && step.timestamp) || '',
            (step && step.time_label) || '',
            options,
        ));

        if (_isIssueLifecycleValidationStep(step, options)) {
            const bodyId = `${stepId}-body`;
            const stepRunDir = String((step && step.run_dir) || ctx.cycleRunDir || '');
            const issueNumber = Number(options && options.issueNumber);
            const toggleAttrs = _issueLifecycleValidationToggleAttrs(stepId, bodyId, issueNumber, stepRunDir);
            const validationDetail = step && step.detail
                ? `<span class="journey-detail">${escapeHtml(step.detail)}</span>`
                : '';
            return `<div class="journey-step journey-step-validation ${statusClass}" id="${stepId}">
            <div class="journey-step-row">
                <button type="button" class="journey-step-inline-toggle" ${toggleAttrs}>
                    <span class="journey-step-caret" aria-hidden="true">▸</span>
                    <span class="journey-time">${timestamp}</span>
                    <span class="journey-main">
                        <span class="journey-summary-row">
                            <span class="journey-narrative">${narrative}</span>
                            ${evidence}
                        </span>
                        ${validationDetail}
                    </span>
                </button>
                ${actions ? `<div class="journey-step-inline-actions">${actions}</div>` : ''}
            </div>
            <div class="journey-step-validation-body collapsed" id="${bodyId}" data-loaded="0" data-run-dir="${escapeAttr(stepRunDir)}" role="region" aria-labelledby="${escapeAttr(`${stepId}-toggle`)}" aria-hidden="true"></div>
        </div>`;
        }

        return `<div class="journey-step ${statusClass}">
        <span class="journey-time">${timestamp}</span>
        <div class="journey-main">
            <div class="journey-summary-row">
                <span class="journey-narrative">${narrative}</span>
                ${progressBadge}
                ${evidence}
                ${actions}
            </div>
            ${detail}
        </div>
    </div>`;
    }

    function _renderIssueLifecycleCycleBody(cycle, ctx, options) {
        const phaseGroups = Array.isArray(cycle && cycle.phase_groups) && cycle.phase_groups.length > 0
            ? cycle.phase_groups
            : [{ key: 'events', label: '', steps: (cycle && cycle.steps) || [] }];
        let html = '';
        let stepCounter = 0;
        for (const group of phaseGroups) {
            html += '<div class="journey-phase-group">';
            if (group.label) {
                html += `<div class="journey-phase-header">${escapeHtml(group.label)}</div>`;
            }
            const steps = Array.isArray(group.steps) ? group.steps : [];
            for (const step of steps) {
                html += _renderIssueLifecycleStep(step, {
                    ...ctx,
                    stepIndex: stepCounter++,
                }, options);
            }
            html += '</div>';
        }
        return html;
    }

    function renderIssueLifecycleTimeline(runs, options) {
        options = options || {};
        const rows = Array.isArray(runs) ? runs : [];
        if (rows.length === 0) {
            return options.emptyHtml || '<div class="timeline-empty">No activity recorded.</div>';
        }
        if (typeof renderHierarchicalTimelineNode !== 'function') {
            return '<div class="timeline-empty">Timeline renderer unavailable.</div>';
        }
        const baseId = String(options.baseId || 'issue-lifecycle');
        const nodes = [];
        for (let runIndex = 0; runIndex < rows.length; runIndex++) {
            const run = rows[runIndex] || {};
            const runId = `${baseId}-run-${runIndex}`;
            let runBodyHtml = '';
            const cycles = Array.isArray(run.cycles) ? run.cycles : [];
            for (let cycleIndex = 0; cycleIndex < cycles.length; cycleIndex++) {
                const cycle = cycles[cycleIndex] || {};
                const cycleId = `${baseId}-cycle-${runIndex}-${cycleIndex}`;
                const ctx = {
                    run,
                    cycle,
                    runIndex,
                    cycleIndex,
                    runId,
                    cycleId,
                    cycleRunDir: _findIssueLifecycleCycleRunDir(cycle, options),
                };
                runBodyHtml += renderHierarchicalTimelineNode({
                    id: cycleId,
                    className: 'journey-cycle unified-timeline-node',
                    summaryClassName: 'journey-cycle-header unified-timeline-summary',
                    bodyClassName: 'journey-cycle-body',
                    bodyId: `${cycleId}-body`,
                    caretClassName: 'journey-cycle-toggle',
                    open: _callTimelineOption(options, 'cycleExpanded', cycle, ctx) ?? Boolean(cycle.expanded),
                    summaryHtml: _renderIssueLifecycleCycleSummary(cycle, ctx, options),
                    bodyHtml: _renderIssueLifecycleCycleBody(cycle, ctx, options),
                });
            }
            const runCtx = { run, runIndex, runId, totalRuns: rows.length };
            nodes.push({
                id: runId,
                className: 'journey-run unified-timeline-node',
                summaryClassName: 'journey-cycle-header unified-timeline-summary',
                bodyClassName: 'journey-cycle-body',
                bodyId: `${runId}-body`,
                caretClassName: 'journey-cycle-toggle',
                open: _callTimelineOption(options, 'runExpanded', run, runCtx) ?? Boolean(run.expanded),
                summaryHtml: _renderIssueLifecycleRunSummary(run, runCtx, options),
                bodyHtml: runBodyHtml,
            });
        }
        const rendered = renderHierarchicalTimelineList(nodes);
        return options.listClassName
            ? `<div class="${escapeAttr(options.listClassName)}">${rendered}</div>`
            : rendered;
    }

    async function toggleValidationEventInline(stepId, issueNumber, runDir) {
        const n = Number(issueNumber);
        if (!Number.isInteger(n) || n <= 0) return;
        const step = document.getElementById(stepId);
        const body = document.getElementById(`${stepId}-body`);
        if (!step || !body) return;
        const toggle = step.querySelector(':scope > .journey-step-row > .journey-step-inline-toggle');
        const caret = toggle
            ? toggle.querySelector(':scope > .journey-step-caret')
            : step.querySelector(':scope > .journey-step-row > .journey-step-caret');
        const wasCollapsed = body.classList.contains('collapsed');
        body.classList.toggle('collapsed');
        if (caret) caret.textContent = wasCollapsed ? '▾' : '▸';
        if (toggle) toggle.setAttribute('aria-expanded', wasCollapsed ? 'true' : 'false');
        body.setAttribute('aria-hidden', wasCollapsed ? 'false' : 'true');

        if (wasCollapsed && body.dataset.loaded !== '1') {
            body.dataset.loaded = '1';
            body.innerHTML = '<div class="cvv-loading">Loading validation details…</div>';
            try {
                const effectiveRunDir = runDir || body.dataset.runDir || '';
                const params = new URLSearchParams();
                if (effectiveRunDir) params.set('run_dir', effectiveRunDir);
                const suffix = params.toString() ? `?${params.toString()}` : '';
                const res = await fetch(`/api/dialog/validation-failure/${n}${suffix}`);
                const data = await res.json().catch(() => ({}));
                if (!res.ok || data.error) {
                    const message = data.error || `Failed to load validation details (HTTP ${res.status})`;
                    body.innerHTML = `<div class="cvv-error">${escapeHtml(String(message))}</div>`;
                    body.dataset.loaded = 'error';
                    return;
                }

                const renderViewer = _issueLifecycleHostFunction(null, 'renderCanonicalValidationViewer');
                if (!renderViewer) {
                    body.innerHTML = '<div class="cvv-error">Validation viewer is unavailable.</div>';
                    body.dataset.loaded = 'error';
                    return;
                }
                const viewerOptions = {};
                const renderActionSections = _issueLifecycleHostFunction(null, 'renderValidationFailureActionSections');
                if (renderActionSections) {
                    viewerOptions.renderActionSections = renderActionSections;
                }
                body.innerHTML = renderViewer(data, viewerOptions);
                const enhancer = _issueLifecycleHostFunction(null, 'enhanceCanonicalValidationViewerAccessibility');
                const cvvRoot = body.querySelector('.cvv-root');
                if (enhancer && cvvRoot) enhancer(cvvRoot);
            } catch (err) {
                const message = err && err.message ? err.message : String(err);
                body.innerHTML = `<div class="cvv-error">Failed to load validation details: ${escapeHtml(message)}</div>`;
                body.dataset.loaded = 'error';
            }
        }
    }

    function _handleCycleValidationBadgeClick(button) {
        if (!button) return;
        const cycle = button.closest('.journey-cycle');
        if (!cycle) return;
        if (cycle.tagName === 'DETAILS' && !cycle.open) {
            cycle.open = true;
        }
        const stepEl = cycle.querySelector(':scope > .journey-cycle-body .journey-step-validation');
        if (!stepEl) return;
        const stepBody = stepEl.querySelector(':scope > .journey-step-validation-body');
        const issueNum = Number(button.dataset.issueNumber || '');
        if (stepBody && stepBody.classList.contains('collapsed') && Number.isInteger(issueNum) && issueNum > 0) {
            const runDir = stepBody.dataset.runDir || '';
            toggleValidationEventInline(stepEl.id, issueNum, runDir);
        }
        stepEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }

    if (typeof registerHierarchicalTimelineHostCapability === 'function') {
        registerHierarchicalTimelineHostCapability(
            'handleCycleValidationBadgeClick',
            () => _handleCycleValidationBadgeClick,
        );
    }

    root.renderIssueLifecycleTimeline = renderIssueLifecycleTimeline;
    root.toggleValidationEventInline = toggleValidationEventInline;
    root._handleCycleValidationBadgeClick = _handleCycleValidationBadgeClick;

    if (!canRegisterValidationPlugin) return;

    registerValidationPlugin('io.agent-context', function renderAgentContext(payload) {
        if (!payload || typeof payload !== 'object') return '';
        const issueNumber = Number(payload.issue_number);
        if (!Number.isInteger(issueNumber) || issueNumber <= 0) return '';
        const title = typeof payload.issue_title === 'string' ? payload.issue_title : '';
        const finalState = typeof payload.final_state === 'string' ? payload.final_state : '';
        const summary = typeof payload.summary === 'string' ? payload.summary : '';

        // Status chip — use the canonical viewer's chip styling.  Map
        // common orchestrator final states to outcome colors.
        const stateClass = finalState === 'completed'
            ? 'cvv-chip-passed'
            : finalState === 'blocked' || finalState === 'failed'
            ? 'cvv-chip-failed'
            : finalState === 'errored'
            ? 'cvv-chip-error'
            : '';
        const stateChip = finalState
            ? `<span class="cvv-chip ${stateClass}">${escapeHtml(finalState)}</span>`
            : '';

        // Inline "Agent attempts" expander (issue #6322): instead of
        // teleporting to the per-issue drawer, the user expands the
        // attempts inline.  Lazy-fetches the issue-detail payload
        // and renders Attempts → Cycles in place.  No fallback
        // ``↗`` to open as a fresh root — the user said they don't
        // want it; if it becomes painful, file a follow-up.
        //
        // Pure-JUnit consumers without the inline-attempts bundle
        // (e.g. tixmeup) see the summary fields and nothing else —
        // the expander silently drops in.
        let attemptsHtml = '';
        if (typeof renderInlineAgentAttemptsExpander === 'function') {
            attemptsHtml = `<div class="agent-context-actions">${renderInlineAgentAttemptsExpander(issueNumber)}</div>`;
        }

        let html = '<div class="cvv-plugin agent-context">';
        html += '<div class="cvv-plugin-header">';
        html += `<span class="cvv-plugin-tag">↳ Linked issue · driven by orchestrator</span>`;
        html += '</div>';
        html += '<div class="cvv-plugin-body">';
        html += `<div class="agent-context-row"><strong>Issue:</strong> <code>#${issueNumber}</code>`;
        if (title) html += ` — ${escapeHtml(title)}`;
        html += '</div>';
        if (finalState) {
            html += `<div class="agent-context-row"><strong>Final state:</strong> ${stateChip}</div>`;
        }
        if (summary) {
            html += `<div class="agent-context-row"><strong>Summary:</strong> ${escapeHtml(summary)}</div>`;
        }
        html += attemptsHtml;
        html += '</div></div>';
        return html;
    });
})();
