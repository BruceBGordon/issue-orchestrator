const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const expandedColumnState = require('../../src/issue_orchestrator/static/js/expanded_column_state.js');

test('getExpandedItemsFromViewModel returns column-specific items', () => {
    const vm = {
        queue_items: [{ issue_number: 1 }],
        blocked_items: [{ issue_number: 2 }],
        awaiting_merge_items: [{ issue_number: 3 }],
        completed_items: [{ issue_number: 4 }],
    };

    assert.deepEqual(expandedColumnState.getExpandedItemsFromViewModel(vm, 'queued'), [{ issue_number: 1 }]);
    assert.deepEqual(expandedColumnState.getExpandedItemsFromViewModel(vm, 'blocked'), [{ issue_number: 2 }]);
    assert.deepEqual(expandedColumnState.getExpandedItemsFromViewModel(vm, 'awaiting-merge'), [{ issue_number: 3 }]);
    assert.deepEqual(expandedColumnState.getExpandedItemsFromViewModel(vm, 'completed'), [{ issue_number: 4 }]);
    assert.deepEqual(expandedColumnState.getExpandedItemsFromViewModel(vm, 'running'), []);
    assert.deepEqual(expandedColumnState.getExpandedItemsFromViewModel(vm, 'unknown'), []);
});

test('getExpandedItemsFromViewModel filters queued items already shown in awaiting-merge', () => {
    const vm = {
        queue_items: [{ issue_number: 10 }, { issue_number: 20 }, { issue_number: 30 }],
        awaiting_merge_items: [{ issue_number: 20 }],
    };

    assert.deepEqual(
        expandedColumnState.getExpandedItemsFromViewModel(vm, 'queued'),
        [{ issue_number: 10 }, { issue_number: 30 }],
    );
});

test('dashboard refresh keeps server column count instead of preview length', () => {
    const dashboardJs = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/core.js'),
        'utf8',
    );

    assert.match(dashboardJs, /const serverCount = Number\(col\.count\);/);
    assert.match(dashboardJs, /Number\.isFinite\(serverCount\) \? serverCount : visibleItems\.length/);
    assert.match(dashboardJs, /syncColumnOverflowFooter\(colEl, col\)/);
    assert.doesNotMatch(dashboardJs, /countEl\.textContent = visibleItems\.length/);
});

test('computeExpandedItemsFingerprint is stable for logically equal inputs', () => {
    const items = [
        { issue_number: 10, title: 'A', detail_label: 'd', status: 'blocked', issue_url: 'u1', pr_url: '' },
        { issue_number: 20, title: 'B', detail_label: 'e', status: 'blocked', issue_url: 'u2', pr_url: 'p2' },
    ];

    const first = expandedColumnState.computeExpandedItemsFingerprint(items, {
        columnId: 'queued',
        viewedIssueNumbers: [],
    });
    const second = expandedColumnState.computeExpandedItemsFingerprint(items, {
        columnId: 'queued',
        viewedIssueNumbers: [],
    });

    assert.equal(first, second);
});

test('computeExpandedItemsFingerprint changes when issue label gains a logical key', () => {
    // Without the label in the fingerprint, the same issue_number with a new
    // "M9-009 · #4057" label would be treated as identical and the rebuild
    // would be skipped, leaving the bare "#4057" rendered.
    const before = expandedColumnState.computeExpandedItemsFingerprint(
        [{ issue_number: 4057, title: 'T', detail_label: 'd', status: 'queued', issue_label: '#4057' }],
        { columnId: 'queued', viewedIssueNumbers: [] },
    );
    const after = expandedColumnState.computeExpandedItemsFingerprint(
        [{ issue_number: 4057, title: 'T', detail_label: 'd', status: 'queued', issue_key: 'M9-009', issue_label: 'M9-009 · #4057' }],
        { columnId: 'queued', viewedIssueNumbers: [] },
    );
    assert.notEqual(before, after);
});

test('computeExpandedItemsFingerprint reflects blocked viewed state', () => {
    const items = [{ issue_number: 10, title: 'A', detail_label: 'd', status: 'blocked' }];

    const unviewed = expandedColumnState.computeExpandedItemsFingerprint(items, {
        columnId: 'blocked',
        viewedIssueNumbers: [],
    });
    const viewed = expandedColumnState.computeExpandedItemsFingerprint(items, {
        columnId: 'blocked',
        viewedIssueNumbers: [10],
    });

    assert.notEqual(unviewed, viewed);
});

test('getExpandedItemsFromViewModel returns active_items for running column', () => {
    const vm = {
        active_items: [{ issue_number: 5 }, { issue_number: 6 }],
        queue_items: [{ issue_number: 1 }],
    };

    assert.deepEqual(
        expandedColumnState.getExpandedItemsFromViewModel(vm, 'running'),
        [{ issue_number: 5 }, { issue_number: 6 }],
    );
});

test('reconcileSelectedIssues keeps only selected issues still present', () => {
    const selected = [10, 20, 30];
    const items = [{ issue_number: 20 }, { issue_number: 40 }];

    assert.deepEqual(expandedColumnState.reconcileSelectedIssues(selected, items), [20]);
});

test('reconcileSelectedIssues ignores non-numeric selections', () => {
    const selected = ['x', 5, null];
    const items = [{ issue_number: 5 }];

    assert.deepEqual(expandedColumnState.reconcileSelectedIssues(selected, items), [5]);
});
