// JS-vm tests for where focus lands when the issue-detail drawer closes.
//
// Issue #6421 retired the separate ``#timelineModal`` teleport, so the
// diagnostic entrypoint now RELOADS the already-visible drawer under the debug
// lens instead of opening a second surface.  That made ``openIssueDetail``
// re-entrant while open, and an unconditional
// ``lastIssueDetailTrigger = triggerEl || document.activeElement`` would then
// overwrite the real opener: the Diagnose link is removed by
// ``closeArtifactPopover`` before its handler runs, so the focused node at that
// moment is a detached element (and on a plain reload it is the drawer's own
// close button).  Closing would strand focus instead of returning it to the
// card/timeline control the user came from.
//
// Both chunks load in bundle order: ``issue_detail_modals.js`` owns the shared
// drawer state and the focus-return owner, ``issue_detail_drawer.js`` calls it.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const JS_DIR = path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard');
const MODALS_JS = path.join(JS_DIR, 'issue_detail_modals.js');
const DRAWER_JS = path.join(JS_DIR, 'issue_detail_drawer.js');

// Elements track focus through the shared ``document`` stub, and ``classList``
// is stateful so the drawer's real open/closed transition is observable.
function _element(doc, name) {
    const classes = new Set();
    return {
        name,
        className: '',
        disabled: false,
        innerHTML: '',
        style: {},
        textContent: '',
        classList: {
            add(cls) { classes.add(cls); },
            contains(cls) { return classes.has(cls); },
            remove(cls) { classes.delete(cls); },
        },
        focus() { doc.activeElement = this; },
        getAttribute() { return null; },
        hasAttribute() { return false; },
        querySelectorAll() { return []; },
        setAttribute() {},
        scrollIntoView() {},
    };
}

function loadDrawer() {
    const elements = {};
    const doc = {
        addEventListener() {},
        getElementById(id) {
            if (!elements[id]) elements[id] = _element(doc, id);
            return elements[id];
        },
        querySelector() { return null; },
        removeEventListener() {},
    };
    // A detached node standing in for "nothing meaningful is focused".
    doc.body = _element(doc, 'body');
    doc.activeElement = doc.body;

    const context = {

        // Cross-chunk owner: ``tech_lead_runs.js`` owns the targeted tech-lead

        // action's visibility/state; the drawer only names its elements (#6994).

        resetTechLeadIssueAction: () => {},

        updateTechLeadIssueAction: () => false,
        console,
        document: doc,
        fetch: async () => ({ok: true, json: async () => ({issue_number: 123, events: []})}),
        openSessionManifest() {},
    };
    context.window = context;
    vm.createContext(context);
    for (const [file, filename] of [[MODALS_JS, 'issue_detail_modals.js'], [DRAWER_JS, 'issue_detail_drawer.js']]) {
        vm.runInContext(fs.readFileSync(file, 'utf8'), context, {filename});
    }
    // ``renderIssueDetail`` is declared by the drawer chunk, so it has to be
    // stubbed after load (see tests/js/AGENTS.md gotcha 2).
    context.renderIssueDetail = () => {};
    const detached = (name) => _element(doc, name);
    return {context, doc, elements, detached};
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

// Drawer visibility is asserted through the observable DOM state, not through
// the implementation's own ``isIssueDetailDrawerOpen`` predicate: a renamed or
// missing helper would otherwise throw and preempt the focus assertions these
// tests exist to make, turning a behavioural regression into a TypeError.
const drawerIsOpen = (elements) => elements.issueDetailDrawer.classList.contains('visible');

test('in-drawer Diagnose keeps the original opener as the focus-return target', async () => {
    const {context, doc, elements, detached} = loadDrawer();
    const sentinel = detached('kanban-card-timeline-btn');
    sentinel.focus();

    await context.openIssueTimeline(123, sentinel);
    assert.equal(drawerIsOpen(elements), true);
    // The drawer moved focus to its own close button on open.
    assert.equal(doc.activeElement, elements.issueDetailCloseBtn);

    // The user opens the cycle popover and clicks Diagnose.  The inline
    // handler calls ``closeArtifactPopover()`` first, so by the time
    // ``openDiagnoseFromCycle`` runs the focused link is already detached.
    const removedPopoverLink = detached('diagnose-link');
    doc.activeElement = removedPopoverLink;

    context.openDiagnoseFromCycle(123);
    await settle();
    assert.equal(drawerIsOpen(elements), true);

    context.closeIssueDetail();

    assert.equal(doc.activeElement, sentinel);
    assert.notEqual(doc.activeElement, removedPopoverLink);
    assert.notEqual(doc.activeElement, elements.issueDetailCloseBtn);
    assert.notEqual(doc.activeElement, doc.body);
});

test('opening a closed drawer without a trigger captures the focused element', async () => {
    const {context, doc, detached} = loadDrawer();
    const opener = detached('issue-row-timeline-btn');
    opener.focus();

    await context.openIssueDetail(123);
    context.closeIssueDetail();

    assert.equal(doc.activeElement, opener);
});

test('an explicit trigger re-targets focus return even while the drawer is open', async () => {
    const {context, doc, detached} = loadDrawer();
    const firstOpener = detached('issue-row-timeline-btn');
    const secondOpener = detached('kanban-card-focus-btn');

    await context.openIssueTimeline(123, firstOpener);
    await context.openIssueDetail(456, secondOpener);
    context.closeIssueDetail();

    assert.equal(doc.activeElement, secondOpener);
});

test('a reopen after close captures the new opener, not the stale one', async () => {
    const {context, doc, elements, detached} = loadDrawer();
    const firstOpener = detached('issue-row-timeline-btn');

    await context.openIssueTimeline(123, firstOpener);
    context.closeIssueDetail();
    assert.equal(drawerIsOpen(elements), false);

    const laterOpener = detached('kanban-card-timeline-btn');
    laterOpener.focus();
    await context.openIssueDetail(456);
    context.closeIssueDetail();

    assert.equal(doc.activeElement, laterOpener);
});
