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

function loadModule(elements = {}) {
    const context = {
        document: {
            getElementById: (id) => elements[id] || null,
        },
        escapeAttr: escapeHtml,
        escapeHtml,
        // The dashboard's shared formatter; stubbed so row text is assertable.
        formatTimestamp: (value) => (value ? `at ${value}` : ''),
        window: { dashboardData: {} },
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
        subjectIssueNumber: 0,
        subjectTitle: '',
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

test('a whole-board run renders no subject reference', () => {
    const context = loadModule();

    const html = context.techLeadActivityRowsHtml({ entries: [entry()] });

    assert.doesNotMatch(html, /tla-subject/);
});

test('a focused investigation references its subject issue', () => {
    const context = loadModule();

    const html = context.techLeadActivityRowsHtml({
        entries: [
            entry({
                flavorLabel: 'Failure investigation',
                subjectIssueNumber: 42,
                subjectTitle: 'Flaky merge queue',
            }),
        ],
    });

    assert.match(html, /#42 Flaky merge queue/);
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
