// JS-vm tests for issue-detail fetch failures.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const DRAWER_JS = path.join(
    __dirname,
    '../../src/issue_orchestrator/static/js/dashboard/issue_detail_drawer.js',
);

function _element() {
    return {
        className: '',
        disabled: false,
        focused: false,
        innerHTML: '',
        style: {},
        textContent: '',
        classList: {
            add() {},
            contains() {
                return false;
            },
            remove() {},
        },
        focus() {
            this.focused = true;
        },
        getAttribute() {
            return null;
        },
        hasAttribute() {
            return false;
        },
        querySelectorAll() {
            return [];
        },
        setAttribute() {},
    };
}

function loadDrawer(fetchImpl) {
    const elements = {};
    const fetchCalls = [];
    const context = {
        // Cross-chunk owner: ``tech_lead_runs.js`` owns the targeted tech-lead
        // action's visibility/state; the drawer only names its elements (#6994).
        resetTechLeadIssueAction: () => {},
        updateTechLeadIssueAction: () => false,
        console,
        // Owned by issue_detail_modals.js, which this harness deliberately
        // does not load. Focus-return behavior is covered by
        // tests/js/issue_detail_focus_return.test.js.
        captureIssueDetailReturnFocus() {},
        restoreIssueDetailReturnFocus() {},
        isIssueDetailDrawerOpen() { return false; },
        currentIssueDetailFocus: null,
        document: {
            activeElement: _element(),
            addEventListener() {},
            getElementById(id) {
                if (!elements[id]) elements[id] = _element();
                return elements[id];
            },
        },
        fetch: async (url) => {
            fetchCalls.push(url);
            return fetchImpl(url);
        },
        issueDetailData: null,
        issueDetailDrawer: _element(),
        lastIssueDetailTrigger: null,
        renderIssueDetail() {},
        timelineView: 'user',
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(DRAWER_JS, 'utf8'), context, {
        filename: 'issue_detail_drawer.js',
    });
    return { context, elements, fetchCalls };
}

test('openIssueDetail surfaces timeline projection failures distinctly', async () => {
    const { context, elements, fetchCalls } = loadDrawer(async () => ({
        ok: false,
        json: async () => ({
            error: 'timeline_projection_failed',
            detail: 'completed coding attempt started_at must not be after completed_at',
        }),
    }));

    await context.openIssueDetail(290);

    assert.equal(fetchCalls[0], '/api/issue-detail/290?view=user');
    assert.equal(
        elements.issueDetailStatus.textContent,
        'Timeline projection failed: completed coding attempt started_at must not be after completed_at',
    );
});

test('openIssueDetail keeps generic unavailable text for opaque failures', async () => {
    const { context, elements } = loadDrawer(async () => ({
        ok: false,
        json: async () => {
            throw new Error('not json');
        },
    }));

    await context.openIssueDetail(123);

    assert.equal(elements.issueDetailStatus.textContent, 'Issue detail unavailable.');
});
