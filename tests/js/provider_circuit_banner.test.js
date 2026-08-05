// JS-vm tests for the provider circuit-breaker outage banner (issue #5980).
//
// The banner renders from ``window.dashboardData.providerCircuit`` and must:
//   - stay hidden when no circuit is open and the read succeeded,
//   - surface the summary + per-provider disclosure when a circuit opens,
//   - distinguish "Unavailable" (open) from "Recovering" (closed-but-tracked),
//   - surface a warning when the circuit read itself failed,
//   - and (accessibility, issue #5980) update WITHOUT re-announcing the
//     assertive alert region on every countdown tick or collapsing an
//     operator's expanded <details> panel across live refreshes.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// A minimal stub element with a write-counting ``textContent`` so tests can
// prove the assertive announcer is written only on semantic changes.
function makeElement(extra) {
    const el = Object.assign(
        { _text: '', textContentWrites: 0, style: { display: '' }, innerHTML: '' },
        extra || {},
    );
    Object.defineProperty(el, 'textContent', {
        get() { return this._text; },
        set(value) { this._text = value; this.textContentWrites += 1; },
    });
    return el;
}

function makeClassList() {
    const set = new Set();
    return {
        add: (c) => set.add(c),
        remove: (c) => set.delete(c),
        contains: (c) => set.has(c),
        toggle: (c, force) => {
            const on = force === undefined ? !set.has(c) : !!force;
            if (on) { set.add(c); } else { set.delete(c); }
            return on;
        },
    };
}

function loadModule(dashboardData) {
    const els = {
        providerCircuitBanner: makeElement(),
        providerCircuitAnnouncer: makeElement({ dataset: {} }),
        providerCircuitBody: makeElement({ classList: makeClassList() }),
        providerCircuitSummary: makeElement(),
        providerCircuitDetails: makeElement({ hidden: true, open: false }),
        providerCircuitList: makeElement(),
    };
    const context = {
        console,
        window: { dashboardData: dashboardData || null },
        document: {
            getElementById: (id) => (Object.prototype.hasOwnProperty.call(els, id) ? els[id] : null),
        },
        escapeHtml: (value) => String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;'),
        escapeAttr: (value) => String(value)
            .replace(/&/g, '&amp;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;'),
        formatTimestamp: (value) => (value ? `AT:${value}` : ''),
    };
    vm.createContext(context);
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/provider_circuit.js'),
        'utf8',
    );
    vm.runInContext(source, context, { filename: 'provider_circuit.js' });
    return { context, els };
}

function openCircuit() {
    return {
        any_open: true,
        open_count: 1,
        open_providers: ['anthropic'],
        summary_text: 'Provider outage: anthropic unavailable — next retry in 4m 12s.',
        next_retry_at: '2026-07-10T12:04:12+00:00',
        entries: [
            {
                provider: 'anthropic',
                is_open: true,
                status_label: 'Unavailable',
                cooldown_remaining_label: '4m 12s',
                next_retry_at: '2026-07-10T12:04:12+00:00',
                consecutive_outages: 3,
                last_error_summary: 'HTTP 529 overloaded',
            },
        ],
    };
}

function unavailableCircuit() {
    return {
        any_open: false,
        open_count: 0,
        open_providers: [],
        summary_text: 'Provider circuit status unavailable — could not read circuit state.',
        next_retry_at: null,
        entries: [],
        status_unavailable: true,
    };
}

// --- Pure render helpers -------------------------------------------------

test('renders per-provider rows for an open circuit', () => {
    const { context } = loadModule();
    const rows = context.providerCircuitRowsHtml(openCircuit());
    assert.match(rows, /pcircuit-badge--open">Unavailable</);
    assert.match(rows, /Retry in 4m 12s/);
    assert.match(rows, /Attempts: 3/);
    assert.match(rows, /HTTP 529 overloaded/);
    // Absolute retry time formatted via the shared timestamp helper.
    assert.match(rows, /AT:2026-07-10T12:04:12/);
});

test('escapes untrusted provider error text in rows', () => {
    const { context } = loadModule();
    const circuit = openCircuit();
    circuit.entries[0].last_error_summary = '<script>alert(1)</script>';
    const rows = context.providerCircuitRowsHtml(circuit);
    assert.doesNotMatch(rows, /<script>alert/);
    assert.match(rows, /&lt;script&gt;/);
});

test('renders a recovering provider without a cooldown', () => {
    const { context } = loadModule();
    const circuit = {
        any_open: true,
        open_count: 1,
        open_providers: ['openai'],
        summary_text: 'Provider outage: openai unavailable — next retry in 1m.',
        next_retry_at: '2026-07-10T12:01:00+00:00',
        entries: [
            {
                provider: 'openai', is_open: true, status_label: 'Unavailable',
                cooldown_remaining_label: '1m', next_retry_at: '2026-07-10T12:01:00+00:00',
                consecutive_outages: 1, last_error_summary: null,
            },
            {
                provider: 'gemini', is_open: false, status_label: 'Recovering',
                cooldown_remaining_label: null, next_retry_at: null,
                consecutive_outages: 2, last_error_summary: null,
            },
        ],
    };
    const rows = context.providerCircuitRowsHtml(circuit);
    assert.match(rows, /pcircuit-badge--recovering">Recovering</);
    assert.match(rows, /pcircuit-row--recovering/);
});

test('the announcement is concise and countdown-free', () => {
    const { context } = loadModule();
    const msg = context.providerCircuitAnnouncement(openCircuit());
    assert.match(msg, /Provider outage in progress: anthropic unavailable\./);
    // The volatile countdown must not be in the assertive announcement.
    assert.doesNotMatch(msg, /4m 12s/);
    assert.strictEqual(context.providerCircuitAnnouncement(null), '');
});

test('the signature is stable across countdown-only changes but tracks providers', () => {
    const { context } = loadModule();
    const a = openCircuit();
    const b = openCircuit();
    b.summary_text = 'Provider outage: anthropic unavailable — next retry in 3m 58s.';
    b.entries[0].cooldown_remaining_label = '3m 58s';
    // Same open providers -> same signature despite a different countdown.
    assert.strictEqual(context.providerCircuitSignature(a), context.providerCircuitSignature(b));
    // A different open-provider set -> a different signature.
    const c = openCircuit();
    c.open_providers = ['anthropic', 'openai'];
    assert.notStrictEqual(context.providerCircuitSignature(a), context.providerCircuitSignature(c));
    // Provider order is normalised so re-ordering does not falsely re-announce.
    const d = openCircuit();
    d.open_providers = ['openai', 'anthropic'];
    assert.strictEqual(context.providerCircuitSignature(c), context.providerCircuitSignature(d));
    // A read failure is a distinct signature.
    assert.strictEqual(context.providerCircuitSignature(unavailableCircuit()), 'unavailable');
});

test('nothing shows for a healthy fleet', () => {
    const { context } = loadModule();
    assert.strictEqual(context.providerCircuitIsActive({ any_open: false, entries: [], status_unavailable: false }), false);
    assert.strictEqual(context.providerCircuitIsActive(null), false);
    assert.strictEqual(context.providerCircuitRowsHtml(null), '');
});

// --- DOM behaviour -------------------------------------------------------

test('updateProviderCircuitBanner shows the banner and announces an open circuit', () => {
    const { context, els } = loadModule();
    context.updateProviderCircuitBanner(openCircuit());
    assert.strictEqual(els.providerCircuitBanner.style.display, 'flex');
    assert.match(els.providerCircuitSummary.textContent, /Provider outage: anthropic/);
    // The assertive announcer got the concise, countdown-free message.
    assert.match(els.providerCircuitAnnouncer.textContent, /Provider outage in progress: anthropic unavailable\./);
    assert.strictEqual(els.providerCircuitDetails.hidden, false);
    assert.match(els.providerCircuitList.innerHTML, /HTTP 529 overloaded/);
});

test('updateProviderCircuitBanner hides and clears the banner when healthy', () => {
    const { context, els } = loadModule();
    context.updateProviderCircuitBanner(openCircuit());
    context.updateProviderCircuitBanner({ any_open: false, entries: [], status_unavailable: false });
    assert.strictEqual(els.providerCircuitBanner.style.display, 'none');
    assert.strictEqual(els.providerCircuitSummary.textContent, '');
    assert.strictEqual(els.providerCircuitList.innerHTML, '');
    assert.strictEqual(els.providerCircuitDetails.hidden, true);
    // Announcer cleared so a *new* outage will announce again.
    assert.strictEqual(els.providerCircuitAnnouncer.textContent, '');
    assert.strictEqual(els.providerCircuitAnnouncer.dataset.circuitSig, '');
});

test('updateProviderCircuitBanner surfaces an unavailable read as a warning', () => {
    const { context, els } = loadModule();
    context.updateProviderCircuitBanner(unavailableCircuit());
    assert.strictEqual(els.providerCircuitBanner.style.display, 'flex');
    assert.match(els.providerCircuitSummary.textContent, /status unavailable/);
    assert.match(els.providerCircuitAnnouncer.textContent, /status unavailable/);
    assert.ok(els.providerCircuitBody.classList.contains('pcircuit-body--unavailable'));
    // No per-provider rows for a failed read.
    assert.strictEqual(els.providerCircuitDetails.hidden, true);
});

test('a countdown-only refresh does not re-announce the alert region', () => {
    const { context, els } = loadModule();
    context.updateProviderCircuitBanner(openCircuit());
    const writesAfterFirst = els.providerCircuitAnnouncer.textContentWrites;
    assert.ok(writesAfterFirst >= 1);

    // Same outage, later countdown value: the visible summary updates but the
    // assertive announcer must NOT be written again.
    const later = openCircuit();
    later.summary_text = 'Provider outage: anthropic unavailable — next retry in 3m 58s.';
    later.entries[0].cooldown_remaining_label = '3m 58s';
    context.updateProviderCircuitBanner(later);

    assert.strictEqual(els.providerCircuitAnnouncer.textContentWrites, writesAfterFirst);
    assert.match(els.providerCircuitSummary.textContent, /3m 58s/);

    // A genuinely new outage state (a second provider opens) DOES re-announce.
    const escalated = openCircuit();
    escalated.open_providers = ['anthropic', 'openai'];
    context.updateProviderCircuitBanner(escalated);
    assert.strictEqual(els.providerCircuitAnnouncer.textContentWrites, writesAfterFirst + 1);
});

test('repeated refreshes do not collapse an operator-opened details panel', () => {
    const { context, els } = loadModule();
    context.updateProviderCircuitBanner(openCircuit());
    assert.strictEqual(els.providerCircuitDetails.hidden, false);

    // Operator expands the health panel.
    els.providerCircuitDetails.open = true;

    // Several live refreshes with ticking countdowns arrive.
    const later = openCircuit();
    later.entries[0].cooldown_remaining_label = '3m 58s';
    context.updateProviderCircuitBanner(later);
    const later2 = openCircuit();
    later2.entries[0].cooldown_remaining_label = '3m 41s';
    context.updateProviderCircuitBanner(later2);

    // The <details> node was never replaced, so it is still open and its rows
    // were updated in place.
    assert.strictEqual(els.providerCircuitDetails.open, true);
    assert.strictEqual(els.providerCircuitDetails.hidden, false);
    assert.match(els.providerCircuitList.innerHTML, /3m 41s/);
});

test('renderProviderCircuitFromDashboardData reads the shared dashboard data', () => {
    const { context, els } = loadModule({ providerCircuit: openCircuit() });
    context.renderProviderCircuitFromDashboardData();
    assert.strictEqual(els.providerCircuitBanner.style.display, 'flex');
    assert.match(els.providerCircuitList.innerHTML, /anthropic/);
});
