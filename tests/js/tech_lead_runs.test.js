// Dashboard tech-lead run actions (#6994).
//
// Covers the three things that could break silently: the request each action
// builds, what it does with a failed response, and how the queued/running state
// and the paused / not-configured states shape the affordance. Server-side
// admission stays authoritative, so these assertions are about what the
// operator SEES and what the browser SENDS — never about what may run.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const staticDir = path.join(__dirname, '../../src/issue_orchestrator/static/js');
const dashboardDir = path.join(staticDir, 'dashboard');

function loadModule(overrides = {}) {
    const calls = [];
    const fetches = [];
    const listeners = {};
    const elements = new Map();

    function fakeElement(id) {
        return {
            id,
            dataset: {},
            style: {},
            disabled: false,
            title: '',
            textContent: '',
            attributes: {},
            classList: {
                _set: new Set(),
                add(name) { this._set.add(name); },
                remove(name) { this._set.delete(name); },
                toggle(name, on) { if (on) this._set.add(name); else this._set.delete(name); },
                contains(name) { return this._set.has(name); },
            },
            setAttribute(name, value) { this.attributes[name] = value; },
            removeAttribute(name) { delete this.attributes[name]; },
            addEventListener(event, handler) { listeners[`${id}:${event}`] = handler; },
        };
    }

    for (const id of [
        'techLeadHealthReviewItem',
        'techLeadHealthReviewStatus',
        'settingsMenu',
        'contextMenu',
        'menuInvestigateTechLead',
    ]) {
        elements.set(id, fakeElement(id));
    }

    const context = {
        console,
        document: {
            getElementById: (id) => elements.get(id) || null,
            addEventListener: (event, handler) => { listeners[`document:${event}`] = handler; },
        },
        window: { dashboardData: { techLeadRuns: null } },
        // Cross-chunk owner: ``issue_menus.js`` owns the actions menu's
        // open/closed state (class + aria-expanded together).
        closeSettingsMenu: () => calls.push(['closeSettingsMenu']),
        showToast: (message, type) => calls.push(['toast', message, type]),
        refreshViewModel: async () => calls.push(['refreshViewModel']),
        fetch: async (endpoint, init) => {
            fetches.push({ endpoint, init });
            return {
                ok: true,
                status: 200,
                json: async () => ({ detail: 'Queued.', admitted: true }),
            };
        },
        ...overrides,
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(path.join(staticDir, 'ui_action_contract.js'), 'utf8'),
        context,
    );
    vm.runInContext(
        fs.readFileSync(path.join(dashboardDir, 'tech_lead_runs.js'), 'utf8'),
        context,
    );
    return { context, calls, fetches, elements, listeners };
}

function ready(overrides = {}) {
    return {
        configured: true,
        paused: false,
        globalStatus: 'idle',
        globalStatusLabel: '',
        queuedIssueNumbers: [],
        runningIssueNumbers: [],
        globalBarrierActive: false,
        ...overrides,
    };
}

// ---------------------------------------------------------------------------
// Request builders (the contract both actions go through)
// ---------------------------------------------------------------------------

test('the global action posts a global_health_review scope to the one command surface', async () => {
    const { context, fetches, calls } = loadModule();
    context.window.dashboardData.techLeadRuns = ready();

    await context.runBoardHealthReview();

    assert.deepEqual(calls[0], ['closeSettingsMenu']);
    assert.equal(fetches.length, 1);
    assert.equal(fetches[0].endpoint, '/api/tech-lead/runs');
    assert.equal(fetches[0].init.method, 'POST');
    assert.deepEqual(JSON.parse(fetches[0].init.body), {
        scope: { kind: 'global_health_review' },
    });
});

test('the targeted action posts an issue scope carrying the issue number', async () => {
    const { context, fetches } = loadModule();
    context.window.dashboardData.techLeadRuns = ready();

    await context.investigateWithTechLead(42);

    assert.deepEqual(JSON.parse(fetches[0].init.body), {
        scope: { kind: 'issue', issue_number: 42 },
    });
});

test('the request builders live in the shared ui action contract', () => {
    const { context } = loadModule();

    assert.equal(typeof context.uiActionContract.buildGlobalHealthReviewRunRequest, 'function');
    assert.equal(typeof context.uiActionContract.buildIssueInvestigationRunRequest, 'function');
    assert.equal(context.uiActionContract.ENDPOINTS.TECH_LEAD_RUNS, '/api/tech-lead/runs');
});

test('a non-numeric issue number never reaches the network', async () => {
    const { context, fetches, calls } = loadModule();
    context.window.dashboardData.techLeadRuns = ready();

    await context.investigateWithTechLead('not-a-number');

    assert.equal(fetches.length, 0);
    assert.deepEqual(calls[0], [
        'toast',
        'Cannot investigate: no issue number for this card.',
        'warning',
    ]);
});

// ---------------------------------------------------------------------------
// Response handling — response.ok is checked and typed detail is surfaced
// ---------------------------------------------------------------------------

test('a rejected request surfaces the typed detail as a durable warning', async () => {
    const { context, calls } = loadModule({
        fetch: async () => ({
            ok: false,
            status: 409,
            json: async () => ({ detail: 'Issue #42 is no longer blocked.' }),
        }),
    });
    context.window.dashboardData.techLeadRuns = ready();

    await context.investigateWithTechLead(42);

    // 'warning' is a sticky toast in showToast — the reason stays readable.
    assert.deepEqual(calls, [['toast', 'Issue #42 is no longer blocked.', 'warning']]);
});

test('a server failure surfaces as a durable error toast', async () => {
    const { context, calls } = loadModule({
        fetch: async () => ({
            ok: false,
            status: 502,
            json: async () => ({ detail: 'anchor unavailable' }),
        }),
    });
    context.window.dashboardData.techLeadRuns = ready();

    await context.investigateWithTechLead(42);

    assert.deepEqual(calls, [['toast', 'anchor unavailable', 'error']]);
});

test('an unparseable rejection still names the status rather than failing silently', async () => {
    const { context, calls } = loadModule({
        fetch: async () => ({
            ok: false,
            status: 503,
            json: async () => { throw new Error('not json'); },
        }),
    });
    context.window.dashboardData.techLeadRuns = ready();

    await context.investigateWithTechLead(42);

    assert.equal(calls.length, 1);
    assert.match(calls[0][1], /HTTP 503/);
    assert.equal(calls[0][2], 'error');
});

test('an idempotent duplicate reads as information, not success or failure', async () => {
    const { context, calls } = loadModule({
        fetch: async () => ({
            ok: true,
            status: 200,
            json: async () => ({ detail: 'Already queued.', admitted: false }),
        }),
    });
    context.window.dashboardData.techLeadRuns = ready();

    await context.investigateWithTechLead(42);

    assert.deepEqual(calls[0], ['toast', 'Already queued.', 'info']);
});

test('a successful admission refreshes the view model', async () => {
    const { context, calls } = loadModule();
    context.window.dashboardData.techLeadRuns = ready();

    await context.investigateWithTechLead(42);

    assert.deepEqual(calls, [
        ['toast', 'Queued.', 'success'],
        ['refreshViewModel'],
    ]);
});

// ---------------------------------------------------------------------------
// Affordance state — disabled reasons and non-colour status text
// ---------------------------------------------------------------------------

test('the global menu item is disabled with a Settings pointer when unconfigured', () => {
    const { context, elements } = loadModule();
    context.window.dashboardData.techLeadRuns = ready({ configured: false });

    context.refreshTechLeadMenuState();

    const item = elements.get('techLeadHealthReviewItem');
    assert.equal(item.disabled, true);
    assert.equal(item.attributes['aria-disabled'], 'true');
    assert.match(item.title, /Settings/);
});

test('a paused engine disables the global action instead of promising a run', () => {
    const { context, elements } = loadModule();
    context.window.dashboardData.techLeadRuns = ready({ paused: true });

    context.refreshTechLeadMenuState();

    const item = elements.get('techLeadHealthReviewItem');
    assert.equal(item.disabled, true);
    assert.match(item.title, /paused/i);
});

test('a running global review shows non-colour status text on the menu item', () => {
    const { context, elements } = loadModule();
    context.window.dashboardData.techLeadRuns = ready({
        globalStatus: 'running',
        globalStatusLabel: 'Tech lead running',
    });

    context.refreshTechLeadMenuState();

    assert.equal(elements.get('techLeadHealthReviewStatus').textContent, 'Tech lead running');
    assert.equal(elements.get('techLeadHealthReviewItem').disabled, true);
});

test('a blocked-state action click while disabled warns and sends nothing', async () => {
    const { context, fetches, calls } = loadModule();
    context.window.dashboardData.techLeadRuns = ready({ paused: true });

    await context.runBoardHealthReview();

    assert.equal(fetches.length, 0);
    assert.deepEqual(calls[0], ['closeSettingsMenu']);
    assert.equal(calls[1][2], 'warning');
});

test('the targeted action is hidden for non-blocked issues and shown for blocked ones', () => {
    const { context } = loadModule();
    context.window.dashboardData.techLeadRuns = ready();
    const button = { style: {}, classList: { toggle() {}, }, setAttribute() {}, removeAttribute() {}, title: '' };
    const statusEl = { textContent: 'stale' };

    context.updateTechLeadIssueAction({ button, statusEl }, 42, false);
    assert.equal(button.style.display, 'none');
    assert.equal(statusEl.textContent, '');

    context.updateTechLeadIssueAction({ button, statusEl }, 42, true);
    assert.equal(button.style.display, '');
});

test('a queued investigation disables the targeted action with matching status text', () => {
    const { context } = loadModule();
    context.window.dashboardData.techLeadRuns = ready({ queuedIssueNumbers: [42] });
    const attrs = {};
    const button = {
        style: {},
        classList: { toggle() {} },
        setAttribute(k, v) { attrs[k] = v; },
        removeAttribute(k) { delete attrs[k]; },
        title: '',
        disabled: false,
    };
    const statusEl = { textContent: '' };

    context.updateTechLeadIssueAction({ button, statusEl }, 42, true);

    assert.equal(button.disabled, true);
    assert.equal(attrs['aria-disabled'], 'true');
    assert.equal(statusEl.textContent, 'Tech lead queued');
});

test('the compact menu and the drawer read the same per-issue status source', () => {
    const { context } = loadModule();
    context.window.dashboardData.techLeadRuns = ready({
        queuedIssueNumbers: [42],
        runningIssueNumbers: [73],
    });

    assert.equal(context.techLeadIssueStatus(42), 'queued');
    assert.equal(context.techLeadIssueStatus(73), 'running');
    assert.equal(context.techLeadIssueStatus(7), 'idle');
});
