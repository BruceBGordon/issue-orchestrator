// Provider-outage badge in queue/kanban rows (issue #5980, item 2 / F3).
//
// The badge renders from the server-precomputed ``card.provider_badge``
// (view_models/issue_card_labels.py::provider_badge); the tone/text/title
// *logic* is covered by the Python projection tests. These assert markup
// assembly and — critically — that BOTH row forms render it, so the compact
// card and the expanded list row cannot drift apart.
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
        localStorage: { getItem: () => null, setItem: () => {} },
        window: { dashboardData: { queueRefreshSeconds: 0 }, location: { href: 'http://example.test/' } },
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

function providerBadge(overrides = {}) {
    return {
        tone: 'blocked',
        label_text: 'Provider unavailable',
        title: 'Blocked by a provider outage — the orchestrator will not launch work',
        ...overrides,
    };
}

function card(overrides = {}) {
    return {
        issue_number: 5980,
        issue_label: '#5980',
        title: 'Stalled by outage',
        state_label: 'blocked',
        phase: 'Blocked',
        status: 'blocked',
        is_stale: false,
        show_stale_badge: false,
        orchestrator_labels: ['blocked:provider-unavailable'],
        ...overrides,
    };
}

test('no badge markup when the card carries no provider badge', () => {
    const { renderProviderBadgeHtml } = loadModule();
    assert.strictEqual(renderProviderBadgeHtml({}), '');
    assert.strictEqual(renderProviderBadgeHtml({ provider_badge: null }), '');
    assert.strictEqual(renderProviderBadgeHtml(null), '');
});

test('badge conveys status as text (not colour alone) with a decorative icon', () => {
    const { renderProviderBadgeHtml } = loadModule();

    const html = renderProviderBadgeHtml(card({ provider_badge: providerBadge() }));

    assert.match(html, /class="provider-badge provider-badge--blocked"/);
    // Visible text is the accessible name; the icon is hidden from AT.
    assert.match(html, /<span class="provider-badge-text">Provider unavailable<\/span>/);
    assert.match(html, /<span class="provider-badge-icon" aria-hidden="true">/);
    assert.match(html, /title="Blocked by a provider outage/);
});

test('badge escapes label text and title', () => {
    const { renderProviderBadgeHtml } = loadModule();

    const html = renderProviderBadgeHtml(card({
        provider_badge: providerBadge({
            label_text: '<script>x</script>',
            title: 'a "quoted" <b>title</b>',
        }),
    }));

    assert.doesNotMatch(html, /<script>/);
    assert.match(html, /&lt;script&gt;/);
    assert.match(html, /&quot;quoted&quot;/);
});

// --------------------------------------------------------------------------
// Both row forms
// --------------------------------------------------------------------------

test('compact card renders the provider badge for an affected issue', () => {
    const { renderCompactCardHtml } = loadModule();

    const html = renderCompactCardHtml(card({ provider_badge: providerBadge() }));

    assert.match(html, /card-line card-provider/);
    assert.match(html, /provider-badge-text">Provider unavailable</);
});

test('compact card omits the provider badge for an unaffected issue', () => {
    const { renderCompactCardHtml } = loadModule();

    const html = renderCompactCardHtml(card({
        orchestrator_labels: ['blocked:pr-closed'],
        provider_badge: null,
    }));

    assert.doesNotMatch(html, /card-provider/);
    assert.doesNotMatch(html, /provider-badge/);
});

test('expanded row renders the provider badge for an affected issue', () => {
    const { renderExpandedCardHtml } = loadModule();

    const html = renderExpandedCardHtml(
        card({ provider_badge: providerBadge() }),
        'blocked',
        false,
    );

    assert.match(html, /class="expanded-card"/);
    assert.match(html, /card-line card-provider/);
    assert.match(html, /provider-badge-text">Provider unavailable</);
});

test('expanded row omits the provider badge for an unaffected issue', () => {
    const { renderExpandedCardHtml } = loadModule();

    const html = renderExpandedCardHtml(
        card({ orchestrator_labels: ['blocked:pr-closed'], provider_badge: null }),
        'blocked',
        false,
    );

    assert.doesNotMatch(html, /card-provider/);
    assert.doesNotMatch(html, /provider-badge/);
});

test('both row forms render the badge from the same projection field', () => {
    // Regression rail: the two paths must read the SAME precomputed field, so a
    // future change to one form cannot silently leave the other behind.
    const { renderCompactCardHtml, renderExpandedCardHtml } = loadModule();
    const affected = card({ provider_badge: providerBadge() });

    const compact = renderCompactCardHtml(affected);
    const expanded = renderExpandedCardHtml(affected, 'queued', false);

    for (const html of [compact, expanded]) {
        assert.match(html, /provider-badge provider-badge--blocked/);
        assert.match(html, /Provider unavailable/);
    }
});

test('expanded row keeps its blocked-lane actions alongside the badge', () => {
    // The badge is additive: it must not displace the row's controls.
    const { renderExpandedCardHtml } = loadModule();

    const html = renderExpandedCardHtml(
        card({ provider_badge: providerBadge() }),
        'blocked',
        false,
    );

    assert.match(html, /provider-badge/);
    assert.match(html, /unblockSingle\(5980, this\)/);
    assert.match(html, /card-timeline-btn/);
    assert.match(html, /class="card-checkbox"/);
});

// --------------------------------------------------------------------------
// Fingerprints (card reuse)
// --------------------------------------------------------------------------

test('compact fingerprint changes when a card gains or loses the badge', () => {
    const compactCardState = require(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/compact_card_state.js'),
    );

    const affected = { ...card(), provider_signal: 'blocked:Provider unavailable:why' };
    const unaffected = { ...card(), provider_signal: '' };

    assert.notStrictEqual(
        compactCardState.computeCompactCardFingerprint(affected),
        compactCardState.computeCompactCardFingerprint(unaffected),
    );
});

test('expanded fingerprint changes when a row gains or loses the badge', () => {
    const expandedColumnState = require(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/expanded_column_state.js'),
    );

    const options = { columnId: 'blocked', viewedIssueNumbers: [] };
    const affected = [{ ...card(), provider_signal: 'blocked:Provider unavailable:why' }];
    const unaffected = [{ ...card(), provider_signal: '' }];

    assert.notStrictEqual(
        expandedColumnState.computeExpandedItemsFingerprint(affected, options),
        expandedColumnState.computeExpandedItemsFingerprint(unaffected, options),
    );
});
