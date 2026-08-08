// JS-vm tests for the dashboard issue-detail timeline hierarchy.
//
// The dashboard timeline and E2E run history share the same native
// disclosure-row renderer.  These tests keep the drawer-specific
// content honest without reintroducing a second owner for the row shell.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const DASHBOARD_JS_DIR = path.join(
    __dirname,
    '../../src/issue_orchestrator/static/js/dashboard',
);

function _extractFunction(source, signaturePrefix) {
    const start = source.indexOf(signaturePrefix);
    if (start < 0) throw new Error(`function not found: ${signaturePrefix}`);
    const after = source.indexOf('\n}\n', start);
    if (after < 0) throw new Error(`function close not found: ${signaturePrefix}`);
    return source.slice(start, after + 3);
}

function _escapeHtml(value) {
    return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function loadRenderSlice(overrides = {}) {
    const source = fs.readFileSync(path.join(DASHBOARD_JS_DIR, 'issue_detail_drawer.js'), 'utf8');
    const hierarchicalSource = fs.readFileSync(path.join(DASHBOARD_JS_DIR, 'hierarchical_timeline.js'), 'utf8');
    const pluginSource = fs.readFileSync(path.join(DASHBOARD_JS_DIR, 'plugins/agent_context.js'), 'utf8');
    const slice = [
        _extractFunction(source, 'function _collectRunIdsFromJourneyRuns'),
        _extractFunction(source, 'function _rawEventBelongsToSelectedRuns'),
        _extractFunction(source, 'function renderIssueRawTimelineEvents'),
        _extractFunction(source, 'function _renderJourneyRuns'),
        _extractFunction(source, 'function toggleJourneyCycle'),
    ].join('\n');
    const listeners = [];
    const context = {
        // Cross-chunk owner: ``tech_lead_runs.js`` owns the targeted tech-lead
        // action's visibility/state; the drawer only names its elements (#6994).
        resetTechLeadIssueAction: () => {},
        updateTechLeadIssueAction: () => false,
        console,
        journeyFilter: 'all',
        timelineView: 'user',
        issueDetailData: { issue_number: 4503, summary: {} },
        escapeHtml: _escapeHtml,
        escapeAttr: _escapeHtml,
        formatStepLabel: (value) => String(value || 'event'),
        formatStatus: (value) => String(value || ''),
        formatTimestamp: (value) => String(value || ''),
        filterRuns: (runs) => runs,
        formatJourneyHeaderTimestamp: (_timestamp, label) => label || '13:05',
        formatJourneyStepTimestamp: (_timestamp, label) => label || '13:05',
        _renderCycleValidationBadge: () => '',
        renderTimelineEventActions: () => '',
        _renderLifecycleCommandAttr: (command) =>
            `data-lifecycle-command="${String(command.kind || '')}"`,
        handleTimelineEventActionsClick: () => {},
        bindTimelineEventActions: (container) => {
            if (container.dataset.timelineActionsBound === '1') return;
            container.addEventListener('click', () => {});
            container.dataset.timelineActionsBound = '1';
        },
        closeTimelineEventMenus: () => {},
        document: { getElementById: () => null },
        listeners,
        ...overrides,
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(hierarchicalSource, context, { filename: 'hierarchical_timeline.js' });
    vm.runInContext(pluginSource, context, { filename: 'plugins/agent_context.js' });
    vm.runInContext(slice, context, { filename: 'issue_detail_drawer.js (_renderJourneyRuns slice)' });
    return context;
}

function renderJourney(payload, overrides = {}) {
    const ctx = loadRenderSlice(overrides);
    const container = {
        innerHTML: '',
        dataset: {},
        addEventListener: (type, handler) => ctx.listeners.push({ type, handler }),
    };
    ctx.issueDetailData.summary = payload.summary || {};
    ctx._renderJourneyRuns(container, payload.runs || []);
    return { html: container.innerHTML, listeners: ctx.listeners };
}

test('issue detail timeline renders runs and cycles through the shared disclosure renderer', () => {
    const { html, listeners } = renderJourney({
        runs: [
            {
                run_number: 1,
                run_label: 'Run 1',
                expanded: false,
                outcome: { label: 'Blocked', tone: 'failed' },
                cycles: [
                    {
                        cycle_in_run: 1,
                        cycle_label: 'Cycle 1',
                        expanded: false,
                        outcome: { label: 'Validation failed', tone: 'failed' },
                        phase_groups: [],
                    },
                ],
            },
        ],
        summary: {},
    });

    assert.match(html, /<details class="journey-run unified-timeline-node" id="journey-run-0">/);
    assert.match(html, /<summary class="journey-cycle-header unified-timeline-summary">/);
    assert.match(html, /<span class="journey-cycle-toggle hierarchical-timeline-caret" aria-hidden="true"><\/span>/);
    assert.match(html, /<details class="journey-cycle unified-timeline-node" id="journey-cycle-0-0">/);
    assert.match(html, /<div class="journey-cycle-body" id="journey-run-0-body">/);
    assert.match(html, /<div class="journey-cycle-body" id="journey-cycle-0-0-body">/);
    assert.doesNotMatch(html, /data-lifecycle-command="/);
    assert.doesNotMatch(html, /sync_journey_disclosure/);
    assert.doesNotMatch(html, /journey-cycle-body collapsed/);
    assert.doesNotMatch(html, /onclick="toggleJourneyCycle/);
    assert.doesNotMatch(html, /data-timeline-command/);
    assert.strictEqual(listeners.length, 1);
    assert.strictEqual(listeners[0].type, 'click');
});

test('cycle artifact affordance is a keyboard-reachable button', () => {
    const { html } = renderJourney({
        runs: [
            {
                run_number: 1,
                run_label: 'Run 1',
                expanded: true,
                outcome: { label: 'In progress', tone: 'in_progress' },
                cycles: [
                    {
                        cycle_in_run: 1,
                        cycle_label: 'Cycle 1',
                        expanded: true,
                        outcome: { label: 'Completed', tone: 'passed' },
                        artifacts: { has_review_feedback: true },
                        phase_groups: [],
                    },
                ],
            },
        ],
        summary: {},
    });

    assert.match(html, /<button type="button" class="journey-cycle-artifacts-btn"/);
    assert.match(html, /aria-label="Open artifacts for Cycle 1"/);
    assert.doesNotMatch(html, /<span class="journey-cycle-artifacts-btn"/);
});

test('toggleJourneyCycle delegates disclosure state to native details', () => {
    const cycleNode = { tagName: 'DETAILS', open: false };
    const calls = [];
    const ctx = loadRenderSlice({
        closeTimelineEventMenus: () => calls.push('closed-menus'),
        document: {
            getElementById: (id) => (id === 'journey-cycle-0-0' ? cycleNode : null),
        },
    });

    ctx.toggleJourneyCycle('journey-cycle-0-0');
    assert.deepEqual(calls, ['closed-menus']);
    assert.strictEqual(cycleNode.open, true);

    ctx.toggleJourneyCycle('journey-cycle-0-0');
    assert.deepEqual(calls, ['closed-menus', 'closed-menus']);
    assert.strictEqual(cycleNode.open, false);
});

test('timeline source calls the shared issue lifecycle renderer for runs and cycles', () => {
    const source = fs.readFileSync(path.join(DASHBOARD_JS_DIR, 'issue_detail_drawer.js'), 'utf8');
    const pluginSource = fs.readFileSync(path.join(DASHBOARD_JS_DIR, 'plugins/agent_context.js'), 'utf8');
    const body = _extractFunction(source, 'function _renderJourneyRuns');
    assert.ok(body.includes('renderIssueLifecycleTimeline(runs, {'));
    assert.ok(pluginSource.includes("className: 'journey-run unified-timeline-node'"));
    assert.ok(pluginSource.includes("className: 'journey-cycle unified-timeline-node'"));
});

test('raw timeline view renders raw event rows instead of lifecycle runs', () => {
    const { html } = renderJourney({
        events: [
            {
                event: 'session.started',
                status: 'running',
                timestamp: '2026-05-14T10:14:03Z',
                summary: 'coder launched',
                run_id: 'run-3',
            },
            {
                event: 'validation.failed',
                status: 'failed',
                timestamp: '2026-05-14T10:26:44Z',
                summary: '1 failed',
                run_id: 'run-3',
            },
        ],
        runs: [
            {
                run_number: 3,
                run_label: 'Run 3',
                run_id: 'run-3',
                cycles: [],
            },
        ],
        summary: {},
    }, {
        timelineView: 'raw',
        issueDetailData: {
            issue_number: 4503,
            summary: {},
            events: [
                {
                    event: 'session.started',
                    status: 'running',
                    timestamp: '2026-05-14T10:14:03Z',
                    summary: 'coder launched',
                    run_id: 'run-3',
                },
                {
                    event: 'validation.failed',
                    status: 'failed',
                    timestamp: '2026-05-14T10:26:44Z',
                    summary: '1 failed',
                    run_id: 'run-3',
                },
            ],
        },
    });

    assert.doesNotMatch(html, /role="radiogroup"/);
    assert.doesNotMatch(html, /role="radio"/);
    assert.match(html, /Raw events/);
    assert.match(html, /aria-pressed="true" onclick="setTimelineView\('raw'\)"/);
    assert.match(html, /class="journey-raw-events"/);
    assert.match(html, /session.started/);
    assert.match(html, /validation.failed/);
    assert.doesNotMatch(html, /<details class="journey-run/);
});
