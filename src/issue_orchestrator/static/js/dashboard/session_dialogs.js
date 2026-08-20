function formatLogStreamObservation(obs) {
    if (!obs || typeof obs !== 'object') return '';
    const fmt = (fileObs) => {
        if (!fileObs || typeof fileObs !== 'object') return 'n/a';
        const exists = fileObs.exists ? 'yes' : 'no';
        const bytes = Number.isFinite(fileObs.bytes) ? `${fileObs.bytes}B` : '?';
        const mtime = Number.isFinite(fileObs.mtime_epoch)
            ? formatTimestamp(fileObs.mtime_epoch * 1000)
            : '—';
        return `${exists}, ${bytes}, ${mtime}`;
    };
    return `Stream observation - recording: ${fmt(obs.terminal_recording)} | stdout: ${fmt(obs.provider_stdout)} | stderr: ${fmt(obs.provider_stderr)}`;
}

async function refreshInlineSessionPrompt(issueNumber, runDir = null) {
    const promptMeta = document.getElementById('logPromptMeta');
    const promptPre = document.getElementById('logPromptPre');
    if (!promptMeta || !promptPre) return;
    if (!runDir) {
        promptMeta.textContent = 'Launch prompt unavailable (missing run context).';
        promptPre.textContent = '';
        return;
    }
    try {
        const req = uiActionContract.buildSessionPromptRequest(issueNumber, runDir);
        const res = await fetch(req.endpoint, { method: req.method });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.error) {
            promptMeta.textContent = data.error || `Launch prompt unavailable (HTTP ${res.status})`;
            promptPre.textContent = '';
            return;
        }
        const label = data.label || 'Launch prompt';
        promptMeta.textContent = data.prompt_path ? `${label}: ${data.prompt_path}` : label;
        promptPre.textContent = data.content || '';
    } catch (err) {
        promptMeta.textContent = `Launch prompt unavailable: ${err instanceof Error ? err.message : String(err)}`;
        promptPre.textContent = '';
    }
}

// Context-menu "View Agent Prompt" for an active session opens the run-scoped
// launch prompt (the prompt the agent was actually launched with) in a modal,
// labelled as the launch prompt. Falls back handled by the caller when no
// run_dir is available (non-active rows use the static agent template).
async function openLaunchPromptDialog(issueNumber, runDir) {
    let data = {};
    try {
        const req = uiActionContract.buildSessionPromptRequest(issueNumber, runDir);
        const res = await fetch(req.endpoint, { method: req.method });
        data = await res.json().catch(() => ({}));
        if (!res.ok || data.error) {
            showToast(data.error || `Launch prompt unavailable (HTTP ${res.status})`, 'error');
            return;
        }
    } catch (err) {
        showToast(`Launch prompt unavailable: ${err instanceof Error ? err.message : String(err)}`, 'error');
        return;
    }
    const label = data.label || 'Launch prompt';
    const pathLine = data.prompt_path
        ? `<div class="session-replay-meta">${escapeHtml(`${label}: ${data.prompt_path}`)}</div>`
        : '';
    const html = `<div class="diag-modal">${pathLine}<pre class="launch-prompt-content">${escapeHtml(data.content || '')}</pre></div>`;
    openModal(`Launch Prompt #${issueNumber}`, html);
}

async function openSessionManifest(issueNumber, runDir = null) {
    const params = new URLSearchParams();
    if (runDir) params.set('run_dir', runDir);
    const suffix = params.toString() ? `?${params.toString()}` : '';
    const res = await fetch(`/api/dialog/session-diagnostics/${issueNumber}${suffix}`);
    const data = await res.json();
    const modalEl = modalOverlay.querySelector('.modal');
    if (data.error) {
        document.getElementById('modalTitle').textContent = `Session Diagnostics #${issueNumber}`;
        document.getElementById('modalBody').innerHTML = `<div class="diag-action-message" style="display:block;">${escapeHtml(data.error)}</div>`;
        modalEl.classList.add('diagnostics-modal');
        document.getElementById('modalOverlay').classList.add('visible');
        return;
    }

    const rows = data.rows || [];
    const actions = data.actions || [];
    currentDiagnosticsRunDir = runDir || ((actions.find(action => action && action.run_dir) || {}).run_dir || null);
    const rowByLabel = new Map(rows.map(row => [String(row.label || '').toLowerCase(), String(row.value || '')]));
    const worktree = rowByLabel.get('worktree') || '';

    const hasWorktree = worktree && worktree !== '-';
    const hasDiagnostic = actions.some(action => action.type === 'open_path' && (action.label || '').toLowerCase().includes('diagnostic'));
    const hasValidation = actions.some(action => action.type === 'open_path' && (action.label || '').toLowerCase().includes('validation'));

    const chips = [
        `<span class="diag-chip ${hasWorktree ? 'is-ok' : 'is-muted'}">${hasWorktree ? 'Worktree Present' : 'Worktree Unavailable'}</span>`,
        `<span class="diag-chip ${hasDiagnostic ? 'is-ok' : 'is-muted'}">${hasDiagnostic ? 'Diagnostic Available' : 'No Diagnostic Yet'}</span>`,
        `<span class="diag-chip ${hasValidation ? 'is-ok' : 'is-muted'}">${hasValidation ? 'Validation Captured' : 'No Validation Artifact'}</span>`,
    ].join('');

    const overviewKeys = new Set([
        'session',
        'started',
        'run id',
        'backend',
        'agent',
        'claude session',
        'retention tier',
        'retention expires',
        'retention pinned',
    ]);
    const launchKeys = new Set([
        'task',
        'branch',
        'provider',
        'model',
        'permission mode',
        'timeout',
        'provider args',
        'launch args',
        'prompt mode',
    ]);
    const overviewRows = rows.filter(row => overviewKeys.has(String(row.label || '').toLowerCase()));
    const launchRows = rows.filter(row => launchKeys.has(String(row.label || '').toLowerCase()));
    const pathRows = rows.filter(row => {
        const key = String(row.label || '').toLowerCase();
        return !overviewKeys.has(key) && !launchKeys.has(key);
    });
    const analysis = data.analysis && typeof data.analysis === 'object' ? data.analysis : null;
    const followUpIssues = Array.isArray(data.follow_up_issues) ? data.follow_up_issues : [];

    const hasActions = actions.length > 0;

    let html = '<div class="diag-modal">';
    html += '<div id="diagActionMessage" class="diag-action-message"></div>';
    html += '<div class="diag-header">';
    html += `<div class="diag-header-title">Issue #${issueNumber} Diagnostics</div>`;
    html += `<div class="diag-chip-row">${chips}</div>`;
    html += '</div>';

    html += '<div class="diag-grid">';
    html += '<section class="diag-section">';
    html += '<div class="diag-section-title">Session Overview</div>';
    html += renderDialogRows(overviewRows);
    html += '</section>';
    html += '<section class="diag-section">';
    html += '<div class="diag-section-title">Launch</div>';
    html += renderDialogRows(launchRows, { monospace: true });
    html += '</section>';
    html += '<section class="diag-section">';
    html += '<div class="diag-section-title">Paths</div>';
    html += renderDialogRows(pathRows, { monospace: true });
    html += '</section>';
    html += '</div>';

    if (analysis && analysis.headline) {
        html += '<section class="diag-section diag-analysis">';
        html += '<div class="diag-section-title">Current Diagnosis</div>';
        html += `<div class="diag-analysis-headline">${escapeHtml(String(analysis.headline || ''))}</div>`;
        if (analysis.detail) {
            html += `<div class="diag-analysis-detail">${escapeHtml(String(analysis.detail))}</div>`;
        }
        if (Array.isArray(analysis.suggestions) && analysis.suggestions.length > 0) {
            html += '<ul class="diag-analysis-suggestions">';
            for (const suggestion of analysis.suggestions) {
                html += `<li>${escapeHtml(String(suggestion))}</li>`;
            }
            html += '</ul>';
        }
        html += '</section>';
    }

    if (followUpIssues.length > 0) {
        html += '<section class="diag-section diag-analysis">';
        html += '<div class="diag-section-title">Proposed Follow-up Issues</div>';
        html += '<div class="diag-analysis-detail">Ancillary work discovered during the run and deferred to keep the assigned issue time-bounded.</div>';
        html += '<ul class="diag-analysis-suggestions">';
        for (const item of followUpIssues) {
            const title = escapeHtml(String(item.title || 'Untitled follow-up'));
            const reason = escapeHtml(String(item.reason || 'No reason provided.'));
            const evidence = item.evidence ? ` <span class="diag-followup-evidence">${escapeHtml(String(item.evidence))}</span>` : '';
            const labels = Array.isArray(item.suggested_labels) && item.suggested_labels.length > 0
                ? ` <span class="diag-followup-labels">labels: ${escapeHtml(item.suggested_labels.join(', '))}</span>`
                : '';
            const blocking = item.blocking ? ' <span class="diag-followup-blocking">blocking</span>' : '';
            html += `<li><strong>${title}</strong><div>${reason}${blocking}${evidence}${labels}</div></li>`;
        }
        html += '</ul>';
        html += '</section>';
    }

    if (hasActions) {
        html += '<div class="diag-actions">';
        html += renderGroupedDialogActions(actions);
        html += '</div>';
    } else {
        html += '<div class="diag-empty">No diagnostic actions available for this run.</div>';
    }

    html += '<div class="diag-footnote">Tip: this view is for deep troubleshooting and artifact access.</div>';
    html += '</div>';

    modalEl.classList.add('diagnostics-modal');
    openModal(data.title || `Session Diagnostics #${issueNumber}`, html);
}

// Pure data → DOM mapping. Takes the dialog payload (the "command result"
// from /api/dialog/validation-failure/) and returns {title, html, runDir}.
// No fetch, no globals beyond rendering helpers — so unit tests can call
// this directly with hand-rolled payloads instead of stubbing the network
// and going through the openValidationFailure entry point.
function renderValidationDialog(data, issueNumber, runDir = null) {
    const actionSections = Array.isArray(data.action_sections) ? data.action_sections : [];
    const resolvedRunDir = runDir || firstRunDirFromActionSections(actionSections);
    const failedTests = Array.isArray(data.failed_tests) ? data.failed_tests : [];
    const stdoutExcerpt = Array.isArray(data.stdout_excerpt) ? data.stdout_excerpt : [];
    const stderrExcerpt = Array.isArray(data.stderr_excerpt) ? data.stderr_excerpt : [];
    const status = data.status === 'passed' ? 'passed' : 'failed';
    const summaryRows = Array.isArray(data.summary_rows) && data.summary_rows.length > 0
        ? data.summary_rows
        : [
            { label: 'Outcome', value: status === 'passed' ? 'Passed' : 'Failed' },
            { label: 'Reason', value: String(data.reason || (status === 'passed' ? 'Validation passed' : 'Validation failed')) },
            { label: 'Suite', value: String(data.suite || '-') },
            { label: 'Command', value: String(data.command || '-') },
            { label: 'Exit Code', value: String(data.exit_code ?? '-') },
            { label: 'Started', value: formatTimestamp(data.started_at, '-') },
            { label: 'Ended', value: formatTimestamp(data.ended_at, '-') },
        ];

    // Header chip row stays as the at-a-glance summary; body delegates
    // to the canonical viewer (``validation_viewer.js``) which renders
    // the rich per-test triage cards + browse-by-file + per-test
    // detail.  Issue #6310 follow-up: this is the Phase-A swap — the
    // entry point still opens a modal, but the body is now the shared
    // viewer that Phase B/C will mount elsewhere too.
    let html = '<div class="diag-modal diag-validation-shell">';
    html += '<div class="diag-header">';
    html += '<div class="diag-header-title">Validation Results</div>';
    html += `<div class="diag-chip-row">${renderValidationFailureChips(status, failedTests, stdoutExcerpt, stderrExcerpt, actionSections)}</div>`;
    html += '</div>';

    html += '<section class="diag-section diag-validation-summary">';
    html += '<div class="diag-section-title">Results</div>';
    html += renderDialogRows(summaryRows, { monospace: true });
    html += '</section>';

    html += '<section class="diag-section">';
    // Pass the action-section renderer explicitly — the viewer no longer
    // reaches into session_dialogs.js globals (issue #6310 follow-up
    // reviewer Blocker 2).
    html += renderCanonicalValidationViewer(data, {
        renderActionSections: renderValidationFailureActionSections,
    });
    html += '</section>';

    html += '<div class="diag-footnote">Validation details come from the run-scoped validation record and log artifacts.</div>';
    html += '</div>';

    return {
        title: data.title || `Validation Failure #${issueNumber}`,
        html,
        runDir: resolvedRunDir,
    };
}

async function openValidationFailure(issueNumber, runDir = null, mode = 'modal') {
    const params = new URLSearchParams();
    if (runDir) params.set('run_dir', runDir);
    const suffix = params.toString() ? `?${params.toString()}` : '';
    const res = await fetch(`/api/dialog/validation-failure/${issueNumber}${suffix}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.error) {
        const message = data.error || `Failed to load validation details (HTTP ${res.status})`;
        if (mode === 'inline') {
            showToast(message, 'error');
            return;
        }
        openModal(`Validation Failure #${issueNumber}`, `<div class="diag-action-message" style="display:block;">${escapeHtml(message)}</div>`);
        return;
    }

    const rendered = renderValidationDialog(data, issueNumber, runDir);
    currentDiagnosticsRunDir = rendered.runDir;
    openModal(rendered.title, rendered.html);
    // Phase D (issue #6310 follow-up): enhance the just-mounted
    // canonical viewer with ARIA tree semantics + keyboard nav.  The
    // renderer already bakes role/aria-expanded into the HTML; the
    // enhancer fills in aria-level / aria-setsize / aria-posinset and
    // wires the delegated keydown handler.
    const dialogCvv = document.querySelector('#modalBody .cvv-root');
    if (dialogCvv) enhanceCanonicalValidationViewerAccessibility(dialogCvv);
    const dialogBody = document.getElementById('modalBody');
    if (dialogBody && typeof bindTimelineEventActions === 'function') {
        bindTimelineEventActions(dialogBody);
    }
}

function renderValidationFailureChips(status, failedTests, stdoutExcerpt, stderrExcerpt, actionSections) {
    const artifactCount = (actionSections || []).reduce((count, section) => {
        const sectionActions = Array.isArray(section && section.actions) ? section.actions : [];
        return count + sectionActions.length;
    }, 0);
    const outcomeChipClass = status === 'passed' ? 'is-ok' : 'is-warn';
    const outcomeLabel = status === 'passed' ? 'passed' : 'failed';
    const chips = [
        `<span class="diag-chip ${outcomeChipClass}">${outcomeLabel}</span>`,
        `<span class="diag-chip is-muted">${failedTests.length} failing test${failedTests.length === 1 ? '' : 's'}</span>`,
        `<span class="diag-chip ${stdoutExcerpt.length > 0 ? 'is-ok' : 'is-muted'}">${stdoutExcerpt.length > 0 ? 'stdout excerpt captured' : 'no stdout excerpt'}</span>`,
        `<span class="diag-chip ${stderrExcerpt.length > 0 ? 'is-ok' : 'is-muted'}">${stderrExcerpt.length > 0 ? 'stderr captured' : 'no stderr captured'}</span>`,
    ];
    if (artifactCount > 0) {
        chips.push(`<span class="diag-chip is-ok">${artifactCount} artifact action${artifactCount === 1 ? '' : 's'}</span>`);
    }
    return chips.join('');
}

function firstRunDirFromActionSections(actionSections) {
    for (const section of actionSections || []) {
        const actions = Array.isArray(section && section.actions) ? section.actions : [];
        const runScopedAction = actions.find(action => action && action.run_dir);
        if (runScopedAction && runScopedAction.run_dir) {
            return runScopedAction.run_dir;
        }
    }
    return null;
}

function renderValidationFailureActionSections(actionSections) {
    const sections = Array.isArray(actionSections) ? actionSections : [];
    if (sections.length === 0) {
        return '<div class="diag-empty">No artifact actions available for this validation run.</div>';
    }
    let html = '<div class="diag-validation-action-groups">';
    for (const section of sections) {
        const title = escapeHtml(String((section && section.title) || 'Artifacts'));
        const actions = Array.isArray(section && section.actions) ? section.actions : [];
        if (actions.length === 0) continue;
        html += '<div class="diag-validation-action-group">';
        html += `<div class="diag-validation-action-label">${title}</div>`;
        html += '<div class="diag-validation-action-buttons">';
        for (const action of actions) {
            html += renderDialogActionWithLabel(action);
        }
        html += '</div>';
        html += '</div>';
    }
    html += '</div>';
    return html;
}

function renderDialogRows(rows, options = {}) {
    const useMonospace = !!options.monospace;
    if (!rows || rows.length === 0) {
        return '<div class="diag-empty">No data available.</div>';
    }
    let html = '<div class="diag-rows">';
    for (const row of rows) {
        const label = escapeHtml(String(row.label || ''));
        const rawValue = formatDialogRowValue(row);
        const value = escapeHtml(rawValue);
        html += '<div class="diag-row">';
        html += `<span class="diag-row-label">${label}</span>`;
        if (useMonospace) {
            html += `<code class="diag-row-value is-monospace">${value}</code>`;
        } else {
            html += `<span class="diag-row-value">${value}</span>`;
        }
        html += '</div>';
    }
    html += '</div>';
    return html;
}

function formatDialogRowValue(row) {
    const rawValue = String((row && row.value) || '-');
    if (row && row.value_kind === 'timestamp') {
        return formatTimestamp(rawValue, rawValue);
    }
    return rawValue;
}

function renderDialogAction(action) {
    return renderDialogActionWithLabel(action);
}

function renderGroupedDialogActions(actions) {
    const items = (actions || []).map(action => ({
        action,
        label: _dialogActionShortLabel(action),
    }));
    if (items.length === 0) return '';

    const primaryTypes = [
        'open_validation_failure',
        'open_agent_log',
        'open_review_feedback',
        'open_review_transcript',
    ];
    const primary = [];
    const used = new Set();
    for (const type of primaryTypes) {
        const item = items.find(candidate => String(candidate.action?.type || '') === type);
        if (!item) continue;
        primary.push(item);
        used.add(item);
    }
    for (const item of items) {
        if (used.has(item)) continue;
        if (!item.action || item.action.primary !== true) continue;
        primary.push(item);
        used.add(item);
    }

    const secondary = items.filter(item => !used.has(item));

    let html = '<section class="diag-section">';
    html += '<div class="diag-section-title">Actions</div>';
    html += '<div class="diag-primary-actions">';
    for (const item of primary) {
        html += renderDialogActionWithLabel(item.action, item.label);
    }
    html += '</div>';
    if (secondary.length > 0) {
        html += '<div class="diag-secondary-actions">';
        for (const item of secondary) {
            html += renderDialogActionMenuItem(item.action, item.label);
        }
        html += '</div>';
    }
    html += '</section>';
    return html;
}

function _dialogActionShortLabel(action) {
    if (!action) return 'Action';
    const type = String(action.type || '');
    const label = String(action.label || '');
    if (type === 'open_agent_log') return 'Session Recording';
    if (type === 'open_review_transcript') return 'Review Transcript';
    if (type === 'open_review_artifact') {
        if (action.artifact_type === 'review_decision') return 'Decision JSON';
        return label || 'Review report';
    }
    if (type === 'open_validation_failure') return 'Validation Details';
    if (type === 'copy_agent_log') return 'Copy Session Recording';
    if (type === 'view_claude_log') return 'Claude Log';
    if (type === 'open_orchestrator_log') return 'Issue-Scoped Orchestrator Log';
    if (type === 'open_review_feedback') return 'Review Feedback';
    if (type === 'open_session_diagnostics') return label || 'Diagnostics';
    if (type === 'open_path') {
        const normalized = label.replace(/^Open\s+/i, '').replace(/\s+↗$/, '').trim();
        if (/^completion$/i.test(normalized)) return 'Completion Record';
        if (/^validation$/i.test(normalized)) return 'Validation Record';
        if (/^run dir$/i.test(normalized)) return 'Run Directory';
        return normalized || 'Path';
    }
    return label || 'Action';
}

function renderDialogActionWithLabel(action, labelOverride = null) {
    return _renderDialogActionButton(action, labelOverride, 'btn-secondary');
}

function renderDialogActionMenuItem(action, labelOverride = null) {
    return _renderDialogActionButton(action, labelOverride, 'diag-more-item');
}

// Affordance-glyph convention (issue #6322): the rendered button
// label gets a trailing glyph that tells the user what'll happen
// before they click.
//   ``↗`` — external viewer (browser tab, OS app, editor, anything
//           outside this scroll context).  Maps to handlers that
//           leave the page or open a file via the OS.
//   ``⧉`` — modal viewer.  Maps to handlers that open
//           ``#modalOverlay`` (the dashboard's in-page overlay).
//           Honest about today's behavior; migration to pages is
//           filed as follow-up work.
//   (none) — local action; does the thing in place, no navigation
//           and no overlay.  Example: ``copy_agent_log`` writes
//           to the clipboard.
// The glyph is appended to whatever label the action carried in,
// so backend label assertions still pass on the bare string.
//
// Mapping rationale per action type:
//   open_path                  → OS host opens the file via
//                                ``uiActionContract.buildHostOpenPathRequest``
//                                — external viewer → ``↗``.
//   open_orchestrator_log      → fetches a filtered log, then
//                                ``openPath()`` opens it externally
//                                — external viewer → ``↗``.
//   open_validation_failure    → ``openModal()`` on success
//                                — modal → ``⧉``.
//   open_review_transcript     → ``openModal()`` with the transcript
//                                — modal → ``⧉``.
//   open_review_feedback       → toggles ``#modalOverlay.visible``
//                                — modal → ``⧉``.
//   open_session_diagnostics   → ``openSessionManifest()`` calls
//                                ``openModal()`` — modal → ``⧉``.
//   open_agent_log             → opens the session recording in the
//                                session-replay modal — modal → ``⧉``.
//   view_claude_log            → opens the Claude log viewer modal
//                                — modal → ``⧉``.
//   copy_agent_log             → writes to clipboard, no UI change
//                                — local action → (none).
const _AFFORDANCE_GLYPH_BY_ACTION_TYPE = {
    // External — opens in an OS app / external viewer.
    open_path: ' ↗',
    open_orchestrator_log: ' ↗',
    // Modal — opens ``#modalOverlay`` on this page.  Reviewer flagged
    // the four mid-list entries on PR #6325 round 1: they were
    // mapped to ``↗`` but their handlers actually open modals.
    // Corrected.
    open_validation_failure: ' ⧉',
    open_review_transcript: ' ⧉',
    open_review_artifact: ' ⧉',
    open_review_feedback: ' ⧉',
    open_session_diagnostics: ' ⧉',
    open_agent_log: ' ⧉',
    view_claude_log: ' ⧉',
    // Local action — no glyph.
    copy_agent_log: '',
};

function _affordanceGlyphForAction(action) {
    const type = action && action.type;
    return Object.prototype.hasOwnProperty.call(_AFFORDANCE_GLYPH_BY_ACTION_TYPE, type)
        ? _AFFORDANCE_GLYPH_BY_ACTION_TYPE[type]
        : '';
}

function _renderDialogActionButton(action, labelOverride, cssClass) {
    if (!action) return '';
    const baseLabel = escapeHtml(labelOverride || action.label || 'Action');
    const label = baseLabel + _affordanceGlyphForAction(action);
    const fallbackRunDir = action.run_dir || currentDiagnosticsRunDir || null;
    if (action.type === 'open_path') {
        return `<button class="${cssClass}" onclick="openPath('${escapeHtml(action.path)}')">${label}</button>`;
    }
    if (action.type === 'open_validation_failure') {
        if (!fallbackRunDir) return '';
        return `<button class="${cssClass}" onclick="openValidationFailure(${action.issue_number}, ${JSON.stringify(String(fallbackRunDir))}, 'inline')">${label}</button>`;
    }
    if (action.type === 'open_agent_log') {
        if (!fallbackRunDir) return '';
        const runDirFirstArg = `${JSON.stringify(String(fallbackRunDir))}, `;
        const contextLiteral = JSON.stringify({
            round_index: Number.isInteger(Number(action.round_index)) ? Number(action.round_index) : null,
            session_role: action.session_role || null,
        });
        return `<button class="${cssClass}" onclick="openAgentLogAction(${action.issue_number}, ${runDirFirstArg}'Session Recording', 'inline', ${contextLiteral})">${label}</button>`;
    }
    if (action.type === 'open_review_transcript') {
        if (!fallbackRunDir) return '';
        const roundIndexLiteral = Number.isInteger(Number(action.round_index))
            ? String(Number(action.round_index))
            : 'null';
        const roleLiteral = JSON.stringify(action.transcript_role || null);
        return `<button class="${cssClass}" onclick="openReviewTranscript(${action.issue_number}, ${JSON.stringify(String(fallbackRunDir))}, { round_index: ${roundIndexLiteral}, transcript_role: ${roleLiteral} }, 'inline')">${label}</button>`;
    }
    if (action.type === 'open_review_artifact') {
        if (!fallbackRunDir || !action.artifact_path || !action.artifact_type) return '';
        const runDirLiteral = escapeAttr(JSON.stringify(String(fallbackRunDir)));
        const pathLiteral = escapeAttr(JSON.stringify(String(action.artifact_path)));
        const typeLiteral = escapeAttr(JSON.stringify(String(action.artifact_type)));
        const modeLiteral = escapeAttr(JSON.stringify(String(action.render_mode || '')));
        return `<button class="${cssClass}" onclick="openReviewArtifact(${action.issue_number}, ${runDirLiteral}, ${pathLiteral}, ${typeLiteral}, ${modeLiteral})">${label}</button>`;
    }
    if (action.type === 'copy_agent_log') {
        if (!fallbackRunDir) return '';
        return `<button class="${cssClass}" onclick="copyAgentLogAction(${action.issue_number}, ${JSON.stringify(String(fallbackRunDir))})">${label}</button>`;
    }
    if (action.type === 'view_claude_log') {
        if (!fallbackRunDir) return '';
        return `<button class="${cssClass}" onclick="viewClaudeLog(${action.issue_number}, ${JSON.stringify(String(fallbackRunDir))}, 'inline')">${label}</button>`;
    }
    if (action.type === 'open_orchestrator_log') {
        if (fallbackRunDir) {
            return `<button class="${cssClass}" onclick="openFilteredOrchestratorLog(${action.issue_number}, ${JSON.stringify(String(fallbackRunDir))}, 'inline')">${label}</button>`;
        }
        return `<button class="${cssClass}" onclick="openFilteredOrchestratorLog(${action.issue_number}, null, 'inline')">${label}</button>`;
    }
    if (action.type === 'open_review_feedback') {
        return `<button class="${cssClass}" onclick="openReviewFeedback(${action.issue_number})">${label}</button>`;
    }
    if (action.type === 'open_session_diagnostics') {
        if (fallbackRunDir) {
            return `<button class="${cssClass}" onclick="openSessionManifest(${action.issue_number}, ${JSON.stringify(String(fallbackRunDir))})">${label}</button>`;
        }
        return `<button class="${cssClass}" onclick="openSessionManifest(${action.issue_number})">${label}</button>`;
    }
    return '';
}

function _renderReviewMarkdownInline(text) {
    return escapeHtml(String(text || '')).replace(/`([^`]+)`/g, '<code>$1</code>');
}

function _renderReviewMarkdown(content) {
    const lines = String(content || '').split(/\r?\n/);
    let html = '<div class="review-artifact-markdown">';
    let listOpen = false;
    let fenceOpen = false;
    let fenceLines = [];

    const closeList = () => {
        if (!listOpen) return;
        html += '</ul>';
        listOpen = false;
    };
    const closeFence = () => {
        if (!fenceOpen) return;
        html += `<pre class="review-artifact-code">${escapeHtml(fenceLines.join('\n'))}</pre>`;
        fenceOpen = false;
        fenceLines = [];
    };

    for (const line of lines) {
        const trimmed = line.trim();
        if (trimmed.startsWith('```')) {
            if (fenceOpen) {
                closeFence();
            } else {
                closeList();
                fenceOpen = true;
                fenceLines = [];
            }
            continue;
        }
        if (fenceOpen) {
            fenceLines.push(line);
            continue;
        }
        if (!trimmed) {
            closeList();
            continue;
        }
        const heading = /^(#{1,3})\s+(.+)$/.exec(trimmed);
        if (heading) {
            closeList();
            const level = Math.min(heading[1].length + 2, 5);
            html += `<h${level}>${_renderReviewMarkdownInline(heading[2])}</h${level}>`;
            continue;
        }
        const bullet = /^[-*]\s+(.+)$/.exec(trimmed);
        if (bullet) {
            if (!listOpen) {
                html += '<ul>';
                listOpen = true;
            }
            html += `<li>${_renderReviewMarkdownInline(bullet[1])}</li>`;
            continue;
        }
        closeList();
        html += `<p>${_renderReviewMarkdownInline(trimmed)}</p>`;
    }
    closeFence();
    closeList();
    html += '</div>';
    return html;
}

function _renderReviewArtifactContent(data, artifactType, renderMode) {
    const content = String((data && data.content) || '');
    const mode = String(renderMode || data?.content_type || '').toLowerCase();
    const isDecision = String(artifactType || '').endsWith('_decision');
    if (isDecision || mode.includes('json')) {
        let pretty = content;
        try {
            pretty = JSON.stringify(JSON.parse(content), null, 2);
        } catch (_err) {
            // Preserve the original content if the server-side JSON validator
            // rejected stale data after the action was rendered.
        }
        return `<pre class="review-artifact-json">${escapeHtml(pretty)}</pre>`;
    }
    return _renderReviewMarkdown(content);
}

// Pure: what the viewer calls this artifact. A table rather than a ternary
// because the endpoint now serves the tech lead's report/decision pair too
// (#6858 F4), and a two-way ternary would have titled a tech-lead report
// "Review report" — the wrong agent's name on the wrong artifact.
function _reviewArtifactTitleName(artifactType) {
    const titles = {
        review_report: 'Review report',
        review_decision: 'Decision JSON',
        tech_lead_report: 'Tech lead report',
        tech_lead_decision: 'Tech lead decision',
    };
    return titles[String(artifactType || '')] || 'Run artifact';
}

async function openReviewArtifact(issueNumber, runDir, artifactPath, artifactType, renderMode = null) {
    if (!runDir || !artifactPath || !artifactType) {
        showToast('Review artifact is missing run context.', 'error');
        return;
    }
    const title = `${_reviewArtifactTitleName(artifactType)} #${issueNumber}`;
    openModal(title, '<div class="timeline-loading">Loading review artifact...</div>');
    const modalEl = modalOverlay.querySelector('.modal');
    if (modalEl) modalEl.classList.add('review-artifact-modal');
    try {
        const req = uiActionContract.buildReviewArtifactRequest(
            issueNumber,
            runDir,
            artifactPath,
            artifactType,
        );
        const res = await fetch(req.endpoint, { method: req.method });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.error) {
            const message = data.error || `Failed to load review artifact (HTTP ${res.status})`;
            openModal(title, `<div class="diag-action-message" style="display:block;">${escapeHtml(message)}</div>`);
            return;
        }
        const meta = [
            data.artifact_path ? `Path: ${escapeHtml(String(data.artifact_path))}` : '',
            data.content_type ? `Type: ${escapeHtml(String(data.content_type))}` : '',
        ].filter(Boolean).join(' · ');
        const metaHtml = meta ? `<div class="review-artifact-meta">${meta}</div>` : '';
        openModal(
            title,
            `<div class="review-artifact-viewer">${metaHtml}${_renderReviewArtifactContent(data, artifactType, renderMode)}</div>`,
        );
        if (modalEl) modalEl.classList.add('review-artifact-modal');
    } catch (err) {
        openModal(
            title,
            `<div class="diag-action-message" style="display:block;">${escapeHtml(err instanceof Error ? err.message : String(err))}</div>`,
        );
    }
}

async function sendAgentInput(issueNumber) {
    const input = document.getElementById('agentInput');
    if (!input) {
        showToast('Input field not found', 'error');
        return;
    }
    const text = input.value.trim();
    if (!text) {
        showToast('Please enter a message', 'error');
        return;
    }
    try {
        const res = await fetch(`/api/send/${issueNumber}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text }),
        });
        const data = await res.json();
        if (data.error) {
            showToast(data.error, 'error');
            return;
        }
        showToast(`Sent input to #${issueNumber}`);
        closeModal();
    } catch (err) {
        showToast(`Failed to send input: ${err.message}`, 'error');
    }
}

// Row click handler
async function handleClick(row) {
    const action = row.dataset.action;
    const issueNumber = row.dataset.issue;
    const url = row.dataset.url;
    const e2eRunId = row.dataset.e2eRunId;
    const isE2e = row.dataset.isE2e === 'true';

    // E2E runs open the unified run view via the typed Command
    // pipeline (issue #6322, PR #6329 reviewer Blocker 2).  All
    // user-facing "open E2E run" navigation must serialize through
    // ``open_e2e_run`` so there's a single owner — no parallel
    // direct ``showUnifiedRunView()`` calls bypassing the dispatcher.
    if (isE2e && e2eRunId) {
        runLifecycleCommand({
            kind: 'open_e2e_run',
            label: 'Open E2E Run',
            run_id: parseInt(e2eRunId, 10),
            expand_run_details: false,
        });
        return;
    }

    if (action === 'focus') {
        if (!clientCapabilities.focus_session) {
            try {
                await openSessionManifest(issueNumber);
            } catch (err) {
                showToast('Failed to open session diagnostics', 'error');
            }
            return;
        }
        try {
            const res = await fetch(`/api/focus/${issueNumber}`, { method: 'POST' });
            const data = await res.json();
            if (data.status === 'focused') {
                showToast(`Focused session #${issueNumber}`);
            } else if (data.error) {
                showToast(`Could not focus: ${data.error}`, 'error');
            }
        } catch (err) {
            showToast('Failed to focus session', 'error');
        }
    } else if (url) {
        window.open(url, '_blank');
    }
}

// Kill session handler (inline button)
async function killSession(issueNumber, event) {
    event.stopPropagation();
    if (!confirm(`Terminate session #${issueNumber}?\n\nThis will stop the active agent and place the issue on hold.\nIt will not run again until you explicitly retry/unblock it.`)) return;
    try {
        const res = await fetch(`/api/kill/${issueNumber}`, { method: 'POST' });
        const data = await res.json();
        if (data.status === 'terminated') {
            showToast(`Terminated #${issueNumber} (on hold)`);
            location.reload();
        } else {
            showToast(data.error || 'Failed to terminate session', true);
        }
    } catch (e) {
        showToast('Failed to terminate session: ' + e.message, true);
    }
}

// Auto-refresh during startup
if (!window.dashboardData.startupComplete) {
setTimeout(() => location.reload(), 1000);  // Refresh every 1s during startup
}

// Controls
