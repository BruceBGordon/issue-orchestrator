// The payload→rendered-output half of the tech-lead activity surface
// (ADR-0033 / #6858). The producer→payload half lives in
// tests/unit/test_tech_lead_activity_view.py.
//
// These run at the JS-vm layer rather than in a browser: the panel's behaviour
// is "turn a typed payload into rows and update them in place", which needs no
// DOM engine to prove.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function escapeHtml(value) {
    return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
}

function loadModule(elements = {}, overrides = {}) {
    const context = {
        document: {
            getElementById: (id) => elements[id] || null,
        },
        escapeAttr: escapeHtml,
        escapeHtml,
        // The dashboard's shared formatter; stubbed so row text is assertable.
        formatTimestamp: (value) => (value ? `at ${value}` : ''),
        window: { dashboardData: {} },
        // The shared lifecycle-Command renderer lives in lifecycle_commands.js
        // (loaded before this module on the dashboard). Stubbed to the same
        // contract so the panel's use of it is assertable without the bundle.
        _renderLifecycleCommandButton: (command, fallbackLabel, cssClass) => (
            `<button class="${cssClass}" data-lifecycle-command="`
            + `${escapeHtml(JSON.stringify(command))}" `
            + `onclick="runLifecycleCommandFromButton(this); event.stopPropagation();">`
            + `${escapeHtml(command.label)}</button>`
        ),
        ...overrides,
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(
            path.join(
                __dirname,
                '../../src/issue_orchestrator/static/js/dashboard/tech_lead_activity.js',
            ),
            'utf8',
        ),
        context,
    );
    return context;
}

function entry(overrides = {}) {
    return {
        runKey: 'global:health_review',
        flavorLabel: 'Health review',
        phase: 'completed',
        phaseLabel: 'Completed',
        tone: 'good',
        startedAt: '2026-08-09T09:00:00',
        endedAt: '2026-08-09T09:30:00',
        subjectKind: 'board',
        subjectLabel: 'Whole board',
        subjectIssueNumber: 0,
        subjectTitle: '',
        anchorIssueNumber: 900,
        artifacts: [],
        artifactsNote: '',
        detail: 'Two hotspots are past budget',
        findings: 2,
        proposals: 1,
        runId: 'run-900',
        sessionName: 'tech-lead-900',
        ...overrides,
    };
}

test('a recorded run renders its flavor and its phase as TEXT, not colour alone', () => {
    const context = loadModule();

    const html = context.techLeadActivityRowsHtml({ entries: [entry()] });

    assert.match(html, /Health review/);
    assert.match(html, /Completed/);
    assert.match(html, /tla-phase--good/);
});

test('an empty history renders the server-published sentence', () => {
    const context = loadModule();

    const html = context.techLeadActivityRowsHtml({
        entries: [],
        emptyMessage: 'No tech-lead runs recorded yet.',
    });

    assert.match(html, /No tech-lead runs recorded yet\./);
});

test('a whole-board run names the board, and its anchor as an anchor', () => {
    // #6858 F5: the anchor is how the run was COORDINATED, never what it is
    // about. Rendering it as the subject made health reviews read as
    // investigations of their own bookkeeping issue.
    const context = loadModule();

    const html = context.techLeadActivityRowsHtml({ entries: [entry()] });

    assert.match(html, /Whole board/);
    assert.match(html, /via #900/);
});

test('a focused investigation references its subject issue', () => {
    const context = loadModule();

    const html = context.techLeadActivityRowsHtml({
        entries: [
            entry({
                flavorLabel: 'Failure investigation',
                subjectKind: 'issue',
                subjectLabel: '#42 Flaky merge queue',
                subjectIssueNumber: 42,
                subjectTitle: 'Flaky merge queue',
                anchorIssueNumber: 42,
            }),
        ],
    });

    assert.match(html, /#42 Flaky merge queue/);
    // The anchor IS the subject here, so it is not repeated as "via #42".
    assert.doesNotMatch(html, /via #42/);
});

test('agent-authored text is escaped before it reaches the panel', () => {
    const context = loadModule();

    const html = context.techLeadActivityRowsHtml({
        entries: [entry({ detail: '<img src=x onerror=alert(1)>' })],
    });

    assert.doesNotMatch(html, /<img/);
    assert.match(html, /&lt;img/);
});

test('produced counts are pluralized and omitted when a run produced nothing', () => {
    const context = loadModule();

    assert.equal(
        context.techLeadActivityProducedText(entry({ findings: 1, proposals: 2 })),
        '1 finding · 2 proposals',
    );
    assert.equal(
        context.techLeadActivityProducedText(entry({ findings: 0, proposals: 0 })),
        '',
    );
});

test('an update replaces only the rows, so an open panel keeps its state', () => {
    const list = { innerHTML: 'stale' };
    const count = { textContent: '' };
    const panel = { open: true };
    const context = loadModule({
        techLeadActivityPanel: panel,
        techLeadActivityList: list,
        techLeadActivityCount: count,
    });

    context.updateTechLeadActivityPanel({ entries: [entry(), entry()] });

    assert.match(list.innerHTML, /Health review/);
    assert.equal(count.textContent, '2');
    // The <details> node itself is never touched.
    assert.equal(panel.open, true);
});

test('the panel is left alone when the dashboard has not published a payload', () => {
    // "Not observed yet" is not "no runs": writing the empty sentence before the
    // first view-model arrives would assert something the engine never said.
    const list = { innerHTML: 'unchanged' };
    const context = loadModule({
        techLeadActivityPanel: { open: false },
        techLeadActivityList: list,
    });

    context.renderTechLeadActivityFromDashboardData();

    assert.equal(list.innerHTML, 'unchanged');
});

// --- The drill-down half (#6858 F4) -----------------------------------------
//
// The panel renders the server's typed lifecycle Commands and hands them to the
// dashboard's ONE dispatcher. These prove the payload→click path: a real button
// carrying the exact Command, and the dispatcher routing it to the existing
// openers rather than the panel assembling an endpoint of its own.

const REPLAY_COMMAND = {
    kind: 'open_session_recording',
    label: 'Session replay',
    issue_number: 900,
    run_dir: '/repo/.issue-orchestrator/state/tech-lead-runs/run-900__tech-lead-900',
};

const REPORT_COMMAND = {
    kind: 'open_review_artifact',
    label: 'Report',
    issue_number: 900,
    run_dir: '/repo/.issue-orchestrator/state/tech-lead-runs/run-900__tech-lead-900',
    artifact_path: 'tech-lead-data/tech-lead-report.md',
    artifact_type: 'tech_lead_report',
    render_mode: 'markdown',
};

test('preserved artifacts render as real buttons carrying the server Command', () => {
    const context = loadModule();

    const html = context.techLeadActivityRowsHtml({
        entries: [entry({ artifacts: [REPLAY_COMMAND, REPORT_COMMAND] })],
    });

    // Native <button>s with text labels: keyboard reachable, accessible name,
    // and never colour-only.
    assert.match(html, /<button class="issue-action-btn tla-action"/);
    assert.match(html, /Session replay/);
    assert.match(html, /Report/);
    // The Command travels verbatim — the panel never builds a URL or a path.
    assert.match(html, /open_session_recording/);
    assert.match(html, /tech-lead-runs/);
    assert.match(html, /tech_lead_report/);
    assert.doesNotMatch(html, /api\//);
});

test('a run with nothing preserved renders the note instead of dead buttons', () => {
    const context = loadModule();

    const html = context.techLeadActivityRowsHtml({
        entries: [
            entry({
                artifacts: [],
                artifactsNote: 'No artifacts were preserved for this run.',
            }),
        ],
    });

    assert.doesNotMatch(html, /tla-action/);
    assert.match(html, /No artifacts were preserved for this run\./);
});

test('a running run shows the pending note, not an empty action area', () => {
    const context = loadModule();

    const html = context.techLeadActivityRowsHtml({
        entries: [
            entry({
                phase: 'running',
                phaseLabel: 'Running',
                artifacts: [],
                artifactsNote: 'Artifacts are preserved when the run ends.',
            }),
        ],
    });

    assert.match(html, /Artifacts are preserved when the run ends\./);
});

test('the published Commands dispatch to the existing artifact openers', () => {
    // Loads the SHARED dispatcher and asserts the tech-lead Commands route to
    // the same handlers every other run-scoped drill-down uses.
    const calls = [];
    const context = {
        showToast: (message, severity) => calls.push(['toast', message, severity]),
        openAgentLogAction: (...args) => calls.push(['openAgentLogAction', ...args]),
        openReviewArtifact: (...args) => calls.push(['openReviewArtifact', ...args]),
        escapeAttr: escapeHtml,
        escapeHtml,
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(
            path.join(
                __dirname,
                '../../src/issue_orchestrator/static/js/dashboard/lifecycle_commands.js',
            ),
            'utf8',
        ),
        context,
    );
    Object.assign(context, {
        openAgentLogAction: (...args) => calls.push(['openAgentLogAction', ...args]),
        openReviewArtifact: (...args) => calls.push(['openReviewArtifact', ...args]),
    });

    context.runLifecycleCommandFromButton({
        dataset: { lifecycleCommand: JSON.stringify(REPLAY_COMMAND) },
    });
    context.runLifecycleCommandFromButton({
        dataset: { lifecycleCommand: JSON.stringify(REPORT_COMMAND) },
    });

    // Asserted field-by-field rather than with deepEqual: the vm realm gives
    // the recorded context objects a different Object.prototype, which strict
    // deep equality rejects even for identical shapes (tests/js/AGENTS.md).
    assert.equal(calls.length, 2);
    const [replay, report] = calls;
    assert.deepEqual(replay.slice(0, 5), [
        'openAgentLogAction', 900, REPLAY_COMMAND.run_dir, 'Session replay', 'toast',
    ]);
    assert.equal(replay[5].round_index, null);
    assert.equal(replay[5].session_role, null);
    assert.deepEqual(report, [
        'openReviewArtifact',
        900,
        REPORT_COMMAND.run_dir,
        'tech-lead-data/tech-lead-report.md',
        'tech_lead_report',
        'markdown',
    ]);
});
