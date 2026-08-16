const test = require('node:test');
const assert = require('node:assert');
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

function loadModule() {
    const context = {
        compactCardState: {
            computeCompactCardFingerprint: () => 'fingerprint',
        },
        cssEscape: (value) => String(value),
        document: {},
        escapeAttr: escapeHtml,
        escapeHtml,
        formatDashboardTimestamps: () => {},
        localStorage: {
            getItem: () => null,
            setItem: () => {},
        },
        window: {
            dashboardData: { queueRefreshSeconds: 0 },
            location: { href: 'http://example.test/' },
        },
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(
            path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/kanban_columns.js'),
            'utf8',
        ),
        context,
    );
    return context;
}

function staleCard(overrides = {}) {
    return {
        issue_number: 4057,
        issue_label: 'M9-012 · #4057',
        title: 'Completed terminal item',
        state_label: 'completed',
        phase: 'Done',
        is_stale: true,
        stale_reason: 'Older than 15m stale threshold',
        show_stale_badge: true,
        issue_url: 'https://example.test/issues/4057',
        github_url: 'https://example.test/issues/4057',
        orchestrator_labels: [],
        ...overrides,
    };
}

test('live refresh shows the compact-column footer when items are omitted', () => {
    const { syncColumnOverflowFooter } = loadModule();
    const attributes = new Map();
    const label = { textContent: '' };
    const footer = {
        hidden: true,
        querySelector: (selector) => selector === '.column-overflow-label' ? label : null,
        setAttribute: (name, value) => attributes.set(name, String(value)),
    };
    const column = {
        querySelector: (selector) => selector === '.column-overflow-footer' ? footer : null,
    };

    syncColumnOverflowFooter(column, {
        id: 'blocked',
        title: 'Blocked',
        count: 13,
        hidden_count: 1,
    });

    assert.equal(footer.hidden, false);
    assert.equal(label.textContent, '1 more');
    assert.equal(attributes.get('aria-label'), 'Show full Blocked list, including 1 more item');
});

test('live refresh hides the compact-column footer when the full list is visible', () => {
    const { syncColumnOverflowFooter } = loadModule();
    const label = { textContent: 'stale' };
    const footer = {
        hidden: false,
        querySelector: (selector) => selector === '.column-overflow-label' ? label : null,
        setAttribute: () => {},
    };
    const column = {
        querySelector: (selector) => selector === '.column-overflow-footer' ? footer : null,
    };

    syncColumnOverflowFooter(column, {
        id: 'blocked',
        title: 'Blocked',
        count: 12,
        hidden_count: 0,
    });

    assert.equal(footer.hidden, true);
    assert.equal(label.textContent, '0 more');
});

test('live refresh preserves focus before hiding a footer that no longer has overflow', () => {
    const context = loadModule();
    const transitions = [];
    let isHidden = true;
    const label = { textContent: '' };
    const expandButton = {
        focus: () => {
            transitions.push('focus-expand');
            context.document.activeElement = expandButton;
        },
    };
    const footer = {
        querySelector: (selector) => selector === '.column-overflow-label' ? label : null,
        setAttribute: () => {},
    };
    Object.defineProperty(footer, 'hidden', {
        get: () => isHidden,
        set: (value) => {
            isHidden = value;
            transitions.push(`hidden-${value}`);
        },
    });
    const column = {
        querySelector: (selector) => {
            if (selector === '.column-overflow-footer') return footer;
            if (selector === '.column-expand-btn') return expandButton;
            return null;
        },
    };

    context.syncColumnOverflowFooter(column, {
        id: 'blocked',
        title: 'Blocked',
        count: 13,
        hidden_count: 1,
    });
    assert.equal(isHidden, false);
    assert.equal(label.textContent, '1 more');

    context.document.activeElement = footer;
    transitions.length = 0;

    context.syncColumnOverflowFooter(column, {
        id: 'blocked',
        title: 'Blocked',
        count: 12,
        hidden_count: 0,
    });

    assert.deepEqual(transitions, ['focus-expand', 'hidden-true']);
    assert.equal(context.document.activeElement, expandButton);
});

test('compact-column footer dispatches to the existing full-list expansion', () => {
    const context = loadModule();
    const calls = [];
    let focused = false;
    context.toggleColumnExpand = (columnId) => calls.push(columnId);
    const column = {
        dataset: { column: 'blocked' },
        querySelector: (selector) => selector === '.column-expand-btn'
            ? { focus: () => { focused = true; } }
            : null,
    };
    const button = {
        closest: (selector) => selector === '.kanban-column'
            ? column
            : null,
    };

    context.expandColumnFromOverflow(button);

    assert.deepEqual(calls, ['blocked']);
    assert.equal(focused, true);
});

test('compact card hides stale warning chrome when display flag is false', () => {
    const { renderCompactCardHtml } = loadModule();

    const html = renderCompactCardHtml(staleCard({ show_stale_badge: false }));

    assert.match(html, /data-stale="true"/);
    assert.match(html, /data-show-stale-badge="false"/);
    assert.doesNotMatch(html, /class="stale-dot"/);
    assert.doesNotMatch(html, /badge-stale/);
});

test('compact card shows stale warning chrome when display flag is true', () => {
    const { renderCompactCardHtml } = loadModule();

    const html = renderCompactCardHtml(staleCard());

    assert.match(html, /data-stale="true"/);
    assert.match(html, /data-show-stale-badge="true"/);
    assert.match(html, /class="stale-dot"/);
    assert.match(html, /badge-stale/);
});

test('compact card menu button carries run_dir so the prompt action opens the launch prompt', () => {
    const { renderCompactCardHtml } = loadModule();

    const html = renderCompactCardHtml(staleCard({
        state_label: 'running',
        show_stale_badge: false,
        run_dir: '/runs/rework-454',
    }));

    assert.match(html, /data-run-dir="\/runs\/rework-454"/);
});

test('compact card menu button renders an empty run_dir when the card has none', () => {
    const { renderCompactCardHtml } = loadModule();

    const html = renderCompactCardHtml(staleCard({ show_stale_badge: false }));

    assert.match(html, /data-run-dir=""/);
});

const ISO = '2026-06-09T01:52:59.902460+00:00';

test('compact card timestamp phase-age is marked for the shared local-time localizer', () => {
    const { renderCompactCardHtml } = loadModule();

    const html = renderCompactCardHtml(staleCard({
        phase: 'Done',
        phase_age: ISO,
        time_is_timestamp: true,
        show_stale_badge: false,
    }));

    // The raw UTC value is carried on the marker (localized client-side), not
    // rendered as the only visible text.
    assert.match(html, new RegExp(`data-dashboard-timestamp="${ISO.replace(/[+]/g, '\\+')}"`));
    assert.match(html, /data-dashboard-timestamp-fallback="-"/);
    // Separator stays outside the marked element so localization (which
    // overwrites textContent) cannot eat it.
    assert.match(html, /<span aria-hidden="true"> &middot; <\/span><span data-dashboard-timestamp=/);
});

test('compact card relative-label phase-age renders as-is without a timestamp marker', () => {
    const { renderCompactCardHtml } = loadModule();

    const html = renderCompactCardHtml(staleCard({
        phase: 'Coding',
        phase_age: '5 min',
        time_is_timestamp: false,
        show_stale_badge: false,
    }));

    assert.doesNotMatch(html, /data-dashboard-timestamp/);
    assert.match(html, /<span class="card-phase-age"> &middot; 5 min<\/span>/);
});

function makeAgeEl(markedRaw) {
    const marked = markedRaw === undefined ? null : { dataset: { dashboardTimestamp: markedRaw } };
    return {
        innerHTML: '',
        textContent: '',
        querySelector(sel) {
            assert.equal(sel, '[data-dashboard-timestamp]');
            return marked;
        },
    };
}

function makeNode(ageEl) {
    return {
        querySelector(sel) {
            assert.equal(sel, '.card-phase-age');
            return ageEl;
        },
    };
}

test('syncCompactCardPhaseAge leaves an unchanged timestamp untouched (no flash)', () => {
    const ctx = loadModule();
    let localized = 0;
    ctx.formatDashboardTimestamps = () => { localized += 1; };

    const ageEl = makeAgeEl(ISO); // marker already present for this timestamp
    ageEl.innerHTML = 'sentinel';
    ctx.syncCompactCardPhaseAge(makeNode(ageEl), { phase_age: ISO, time_is_timestamp: true });

    assert.equal(ageEl.innerHTML, 'sentinel'); // not rewritten
    assert.equal(localized, 0); // localizer not re-run
});

test('syncCompactCardPhaseAge rebuilds and localizes when the timestamp changes', () => {
    const ctx = loadModule();
    const localizedWith = [];
    ctx.formatDashboardTimestamps = (el) => { localizedWith.push(el); };

    const ageEl = makeAgeEl('2026-01-01T00:00:00+00:00'); // stale marker
    const node = makeNode(ageEl);
    ctx.syncCompactCardPhaseAge(node, { phase_age: ISO, time_is_timestamp: true });

    assert.match(ageEl.innerHTML, /data-dashboard-timestamp=/);
    assert.match(ageEl.innerHTML, new RegExp(ISO.replace(/[+]/g, '\\+')));
    assert.deepEqual(localizedWith, [ageEl]); // delegated to the one localizer
});

test('syncCompactCardPhaseAge updates relative labels in place without the localizer', () => {
    const ctx = loadModule();
    let localized = 0;
    ctx.formatDashboardTimestamps = () => { localized += 1; };

    const ageEl = makeAgeEl(); // no marker — relative-label card
    ctx.syncCompactCardPhaseAge(makeNode(ageEl), { phase_age: '12 min', time_is_timestamp: false });

    assert.equal(ageEl.textContent, ' · 12 min');
    assert.equal(localized, 0);
});
