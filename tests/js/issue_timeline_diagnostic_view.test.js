// JS-vm tests for the timeline lens the drawer requests per entrypoint.
//
// Issue #6421 retired ``GET /api/timeline/{issue_number}``, which applied no
// view filter, and repointed the no-run-dir Diagnose affordance at the
// contracted ``/api/issue-detail/{issue_number}``.  That replacement DOES
// filter by view, so Diagnose has to ask for the broad lens explicitly or it
// silently hides the Ops/Debug-only evidence it exists to show.
//
// These load both chunks (``issue_detail_modals.js`` owns the shared
// ``timelineView`` state, ``issue_detail_drawer.js`` reads it) in their
// bundle order, so the lexical ``let timelineView`` binding behaves the way
// it does in the browser.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const JS_DIR = path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard');
const MODALS_JS = path.join(JS_DIR, 'issue_detail_modals.js');
const DRAWER_JS = path.join(JS_DIR, 'issue_detail_drawer.js');

function _element() {
    return {
        className: '',
        disabled: false,
        innerHTML: '',
        style: {},
        textContent: '',
        classList: {add() {}, contains() { return false; }, remove() {}},
        focus() {},
        getAttribute() { return null; },
        hasAttribute() { return false; },
        querySelectorAll() { return []; },
        setAttribute() {},
    };
}

function loadDrawer() {
    const elements = {};
    const fetchCalls = [];
    const manifestCalls = [];
    const context = {
        // Cross-chunk owner: ``tech_lead_runs.js`` owns the targeted tech-lead
        // action's visibility/state; the drawer only names its elements (#6994).
        resetTechLeadIssueAction: () => {},
        updateTechLeadIssueAction: () => false,
        console,
        document: {
            activeElement: _element(),
            addEventListener() {},
            getElementById(id) {
                if (!elements[id]) elements[id] = _element();
                return elements[id];
            },
            querySelector() { return null; },
            removeEventListener() {},
        },
        fetch: async (url) => {
            fetchCalls.push(url);
            return {ok: true, json: async () => ({issue_number: 123, events: []})};
        },
        openSessionManifest: (issueNumber, runDir) => manifestCalls.push([issueNumber, runDir]),
    };
    context.window = context;
    vm.createContext(context);
    for (const [file, filename] of [[MODALS_JS, 'issue_detail_modals.js'], [DRAWER_JS, 'issue_detail_drawer.js']]) {
        vm.runInContext(fs.readFileSync(file, 'utf8'), context, {filename});
    }
    // ``renderIssueDetail`` is declared by the drawer chunk, so it has to be
    // stubbed after load (see tests/js/AGENTS.md gotcha 2).
    context.renderIssueDetail = () => {};
    // ``timelineView`` is a script-level ``let``: it lives in the context's
    // lexical scope, not as a context property, so read it by evaluation.
    const currentView = () => vm.runInContext('timelineView', context);
    return {context, fetchCalls, manifestCalls, currentView};
}

test('Diagnose without a run dir opens the timeline under the broad lens', async () => {
    const {context, fetchCalls, manifestCalls, currentView} = loadDrawer();

    context.openDiagnoseFromCycle(123);
    // ``openDiagnoseFromCycle`` is sync; let the drawer's fetch settle.
    await new Promise((resolve) => setImmediate(resolve));

    // Ops-only events (validation.completed) and debug-only events
    // (issue.labels_changed) are absent from the default ``user`` lens, so
    // Diagnose must not fall back to it.
    assert.deepEqual(fetchCalls, ['/api/issue-detail/123?view=debug']);
    assert.deepEqual(manifestCalls, []);
    // The toggle reads the same shared state the request used, so the drawer
    // cannot render "Story" as active while showing debug-lens events.
    assert.equal(currentView(), 'debug');
});

test('Diagnose with a run dir still routes to run-scoped diagnostics', async () => {
    const {context, fetchCalls, manifestCalls, currentView} = loadDrawer();

    context.openDiagnoseFromCycle(123, '/runs/coding-1');
    await new Promise((resolve) => setImmediate(resolve));

    assert.deepEqual(manifestCalls, [[123, '/runs/coding-1']]);
    assert.deepEqual(fetchCalls, []);
    assert.equal(currentView(), 'user');
});

test('opening the timeline without an explicit view keeps the selected lens', async () => {
    const {context, fetchCalls, currentView} = loadDrawer();

    await context.openIssueTimeline(456);

    assert.deepEqual(fetchCalls, ['/api/issue-detail/456?view=user']);
    assert.equal(currentView(), 'user');
});

test('applyTimelineView is the single writer and ignores unsupported views', async () => {
    const {context, fetchCalls, currentView} = loadDrawer();

    assert.equal(context.applyTimelineView('ops'), 'ops');
    assert.equal(currentView(), 'ops');
    // Matches the server-side ``normalize_timeline_view`` coercion: an
    // unrecognised value never becomes the requested lens.
    assert.equal(context.applyTimelineView('nonsense'), 'ops');
    assert.equal(currentView(), 'ops');

    // A later explicit-view open still wins over the retained selection.
    context.openDiagnoseFromCycle(789);
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(fetchCalls, ['/api/issue-detail/789?view=debug']);
    assert.equal(currentView(), 'debug');
});
