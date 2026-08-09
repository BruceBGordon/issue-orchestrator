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

// --- Refresh must not cost the operator their place (#6858 F11) --------------
//
// The panel used to assign ``list.innerHTML`` on every view-model refresh, so a
// focused Session replay / Report / Decision button was destroyed and recreated
// underneath a keyboard user mid-tab. These prove the two halves of the fix:
// unchanged rows are never rewritten, and a row that IS rewritten hands focus
// back to the same action.

function activityWithActions(overrides = {}) {
    return {
        entries: [entry({ artifacts: [REPLAY_COMMAND, REPORT_COMMAND], ...overrides })],
        emptyMessage: 'No tech-lead runs recorded yet.',
    };
}

// A hand-rolled list node with only the surface the updater touches: children
// with a row key and a SETTABLE outerHTML that rebuilds that row's action slots
// (as a real DOM does when a node is replaced), plus a querySelector resolving
// the [data-tla-row][data-tla-action] slot the focus restore looks for. Rebuilding
// on assignment is what makes the focus assertion meaningful: the button focus
// lands on afterwards is a NEW object, not the one that was focused before.
function fakeList(rowsHtml) {
    const list = { children: [] };

    function buildSlots(row, html) {
        row.slots = {};
        const count = (html.match(/data-tla-action="/g) || []).length;
        for (let index = 0; index < count; index += 1) {
            const slot = {
                dataset: { tlaRow: row.dataset.tlaRow, tlaAction: String(index) },
            };
            const button = {
                dataset: {},
                parentElement: slot,
                focused: false,
                focus() { this.focused = true; },
            };
            slot.button = button;
            slot.querySelector = () => button;
            row.slots[String(index)] = slot;
        }
    }

    function makeRow(html) {
        const key = (/data-tla-row="([^"]*)"/.exec(html) || ['', ''])[1];
        const row = { dataset: { tlaRow: key }, slots: {}, rewrites: 0 };
        let current = '';
        Object.defineProperty(row, 'outerHTML', {
            get: () => current,
            set: (value) => {
                current = value;
                row.rewrites += 1;
                buildSlots(row, value);
            },
        });
        row.outerHTML = html;
        row.rewrites = 0;
        return row;
    }

    let innerHTML = '';
    Object.defineProperty(list, 'innerHTML', {
        get: () => innerHTML,
        set: (value) => {
            innerHTML = value;
            list.children = (value.match(/<li[\s\S]*?<\/li>/g) || []).map(makeRow);
        },
    });
    list.querySelector = (selector) => {
        const match = /\[data-tla-row="([^"]*)"\]\[data-tla-action="([^"]*)"\]/.exec(selector);
        if (!match) return null;
        const [, rowKey, actionIndex] = match;
        const row = list.children.find(child => child.dataset.tlaRow === rowKey);
        return row ? (row.slots[actionIndex] || null) : null;
    };
    list.innerHTML = rowsHtml;
    return list;
}

function panelWith(list, activeElement = null) {
    const elements = {
        techLeadActivityPanel: { open: true },
        techLeadActivityList: list,
        techLeadActivityCount: { textContent: '' },
    };
    return loadModule(elements, {
        document: {
            getElementById: (id) => elements[id] || null,
            activeElement,
        },
    });
}

test('an unchanged refresh rewrites no row markup at all', () => {
    const activity = activityWithActions();
    const list = fakeList(loadModule().techLeadActivityRowsHtml(activity));
    const nodes = list.children;

    panelWith(list).updateTechLeadActivityPanel(activity);

    // Same nodes, never rewritten: the rows the operator is tabbing through are
    // untouched, so nothing inside them could have lost focus.
    assert.equal(list.children, nodes);
    assert.deepEqual(list.children.map(row => row.rewrites), [0]);
});

test('a changed row hands keyboard focus back to the same action', () => {
    const activity = activityWithActions();
    const list = fakeList(loadModule().techLeadActivityRowsHtml(activity));
    // The operator is on the Report button (action slot 1) when a refresh lands...
    const focused = list.children[0].slots['1'].button;
    const panel = panelWith(list, focused);
    // ...and this run has since concluded differently, so its row DOES change.
    const changed = activityWithActions({
        phase: 'failed', phaseLabel: 'Failed', tone: 'bad',
    });

    panel.updateTechLeadActivityPanel(changed);

    assert.equal(list.children[0].rewrites, 1, 'the changed row is rewritten');
    const restored = list.children[0].slots['1'].button;
    assert.notEqual(restored, focused, 'the rewritten row has a new control');
    assert.equal(restored.focused, true, 'focus returns to the equivalent action');
});

test('the focused action is identified from the button inside its slot', () => {
    const context = loadModule();
    const slot = { dataset: { tlaRow: 'run-900::tech-lead-900', tlaAction: '2' } };
    const button = { dataset: {}, parentElement: slot };

    // Field-by-field: a vm-realm object fails strict deep equality even when
    // shape-equal (tests/js/AGENTS.md).
    const found = context.techLeadActivityFocusedAction(button);
    assert.equal(found.row, 'run-900::tech-lead-900');
    assert.equal(found.action, '2');
    assert.equal(context.techLeadActivityFocusedAction(null), null);
    assert.equal(context.techLeadActivityFocusedAction({ dataset: {} }), null);
});

test('a refresh that changes the row SET rebuilds, and rows keep their identity', () => {
    const context = loadModule();
    const one = activityWithActions();
    const two = {
        entries: [
            entry({ runId: 'run-901', sessionName: 'tech-lead-901', artifacts: [] }),
            ...one.entries,
        ],
        emptyMessage: 'No tech-lead runs recorded yet.',
    };

    const stable = context.techLeadActivityRowPlan(one, ['run-900::tech-lead-900']);
    const grown = context.techLeadActivityRowPlan(two, ['run-900::tech-lead-900']);

    assert.equal(stable.rebuild, false);
    assert.equal(grown.rebuild, true);
    assert.match(grown.html, /data-tla-row="run-901::tech-lead-901"/);
});
