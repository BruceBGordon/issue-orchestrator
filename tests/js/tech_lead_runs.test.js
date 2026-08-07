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

    // A container whose children we can inspect: the Settings path is rendered
    // as a real sibling control, so the test has to see insertions.
    function fakeParent() {
        return {
            children: [],
            insertBefore(node, _ref) {
                if (!this.children.includes(node)) this.children.push(node);
                node.parentNode = this;
            },
            removeChild(node) {
                this.children = this.children.filter((c) => c !== node);
                node.parentNode = null;
            },
        };
    }

    function fakeElement(id, parent) {
        const el = {
            id,
            dataset: {},
            style: {},
            disabled: false,
            hidden: false,
            title: '',
            textContent: '',
            type: '',
            className: '',
            attributes: {},
            parentNode: parent || null,
            classList: {
                _set: new Set(),
                add(name) { this._set.add(name); },
                remove(name) { this._set.delete(name); },
                toggle(name, on) { if (on) this._set.add(name); else this._set.delete(name); },
                contains(name) { return this._set.has(name); },
            },
            setAttribute(name, value) { this.attributes[name] = value; },
            removeAttribute(name) { delete this.attributes[name]; },
            addEventListener(event, handler) {
                listeners[`${this.id || 'anon'}:${event}`] = handler;
                // Also on the node itself: dynamically created controls are
                // given their id AFTER construction, so an id-keyed map alone
                // cannot find their handler.
                if (event === 'click') this.__clickHandler = handler;
            },
        };
        if (parent) parent.children.push(el);
        return el;
    }

    const globalHost = fakeParent();
    const drawerHost = fakeParent();
    elements.set('techLeadHealthReviewItem', fakeElement('techLeadHealthReviewItem', globalHost));
    elements.set('techLeadHealthReviewStatus', fakeElement('techLeadHealthReviewStatus', globalHost));
    elements.set(
        'issueDetailInvestigateTechLeadBtn',
        fakeElement('issueDetailInvestigateTechLeadBtn', drawerHost),
    );
    elements.set(
        'issueDetailTechLeadStatus',
        fakeElement('issueDetailTechLeadStatus', drawerHost),
    );
    for (const id of ['settingsMenu', 'contextMenu', 'menuInvestigateTechLead']) {
        elements.set(id, fakeElement(id));
    }

    const context = {
        console,
        document: {
            getElementById: (id) => elements.get(id) || null,
            createElement: (_tag) => {
                const el = fakeElement('');
                el.id = '';
                return el;
            },
            addEventListener: (event, handler) => { listeners[`document:${event}`] = handler; },
        },
        window: { dashboardData: { techLeadRuns: null } },
        // The open drawer's subject, as ``issue_detail_drawer.js`` publishes it.
        issueDetailData: null,
        showConfigDialog: () => calls.push(['showConfigDialog']),
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
    // Elements created during the run (the Settings link) must be findable by
    // id, exactly as they are in the real document.
    const originalGetById = context.document.getElementById;
    context.document.getElementById = (id) => {
        const found = originalGetById(id);
        if (found) return found;
        for (const host of [globalHost, drawerHost]) {
            const hit = host.children.find((child) => child.id === id);
            if (hit) return hit;
        }
        return null;
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
        running: true,
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

test('an unconfigured engine explains itself in VISIBLE, associated text', () => {
    // A natively disabled button is not keyboard focusable, so a `title`
    // tooltip is an explanation no keyboard or screen-reader user can reach.
    // The reason has to be on screen and programmatically associated (F7).
    const { context, elements } = loadModule();
    context.window.dashboardData.techLeadRuns = ready({ configured: false });

    context.refreshTechLeadRunControls();

    const item = elements.get('techLeadHealthReviewItem');
    const status = elements.get('techLeadHealthReviewStatus');
    assert.equal(item.disabled, true);
    assert.equal(item.attributes['aria-disabled'], 'true');
    assert.match(status.textContent, /No tech lead agent is configured/);
    assert.equal(item.attributes['aria-describedby'], status.id);
});

test('an unconfigured engine offers a keyboard-reachable Settings control', () => {
    const { context, elements, calls } = loadModule();
    context.window.dashboardData.techLeadRuns = ready({ configured: false });

    context.refreshTechLeadRunControls();

    const link = context.document.getElementById('techLeadHealthReviewItemSettingsLink');
    assert.ok(link, 'a Settings path must exist when configuration is missing');
    assert.equal(link.disabled, false, 'the remedy must stay operable');
    assert.equal(link.textContent, 'Open Settings');

    // It is a real control, not prose: activating it opens Settings.
    const handler = link.__clickHandler;
    assert.ok(handler, 'the Settings control must handle activation');
    handler({ preventDefault() {}, stopPropagation() {} });
    assert.deepEqual(calls.at(-1), ['showConfigDialog']);

    // ...and it disappears once configuration is no longer the problem.
    context.window.dashboardData.techLeadRuns = ready();
    context.refreshTechLeadRunControls();
    assert.equal(
        context.document.getElementById('techLeadHealthReviewItemSettingsLink'),
        null,
    );
    void elements;
});

test('a stopped engine says so instead of blaming configuration', () => {
    // "Start the engine" and "add a tech lead agent" are different remedies;
    // reporting a stopped engine as unconfigured sent operators to the wrong
    // place (#6994 round 1 F5).
    const { context, elements } = loadModule();
    context.window.dashboardData.techLeadRuns = ready({ running: false });

    context.refreshTechLeadRunControls();

    const status = elements.get('techLeadHealthReviewStatus');
    assert.match(status.textContent, /Repository Engine is not running/);
    assert.equal(
        context.document.getElementById('techLeadHealthReviewItemSettingsLink'),
        null,
        'a stopped engine is not a Settings problem',
    );
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


// ---------------------------------------------------------------------------
// The refresh owner (#6994 round 1 F6)
//
// Every surface that can change run state ends in one refresh, so no visible
// control can be left claiming "idle" for a run the server already queued.
// These assert on RENDERED state after a payload change, not on the fact that
// a refresh function was called.
// ---------------------------------------------------------------------------

test('an open drawer re-renders from idle to queued when the payload changes', () => {
    const { context, elements } = loadModule();
    context.window.dashboardData.techLeadRuns = ready();
    context.issueDetailData = { issue_number: 42 };
    const button = elements.get('issueDetailInvestigateTechLeadBtn');
    const status = elements.get('issueDetailTechLeadStatus');
    context.updateTechLeadIssueAction({ button, statusEl: status }, 42, true);
    assert.equal(button.disabled, false);
    assert.equal(status.textContent, '');

    context.window.dashboardData.techLeadRuns = ready({ queuedIssueNumbers: [42] });
    context.refreshTechLeadRunControls();

    assert.equal(button.disabled, true, 'the open drawer must not stay enabled');
    assert.equal(status.textContent, 'Tech lead queued');
});

test('the compact card action re-renders and relabels itself on refresh', () => {
    const { context, elements } = loadModule();
    context.window.dashboardData.techLeadRuns = ready();
    const button = elements.get('menuInvestigateTechLead');
    button.dataset.issue = '42';
    button.style.display = '';

    context.window.dashboardData.techLeadRuns = ready({ runningIssueNumbers: [42] });
    context.refreshTechLeadRunControls();

    assert.equal(button.disabled, true);
    assert.equal(button.textContent, 'Investigate with tech lead — Tech lead running');
});

test('a hidden targeted action is left alone by the refresh owner', () => {
    // Refreshing must not resurrect an action the surface deliberately hid.
    const { context, elements } = loadModule();
    context.window.dashboardData.techLeadRuns = ready({ queuedIssueNumbers: [42] });
    const button = elements.get('issueDetailInvestigateTechLeadBtn');
    button.style.display = 'none';
    context.issueDetailData = { issue_number: 42 };

    context.refreshTechLeadRunControls();

    assert.equal(button.style.display, 'none');
});

test('the targeted action stays VISIBLE but disabled when the engine cannot run it', () => {
    // Hiding the control when no tech lead is configured makes the capability
    // undiscoverable; it must stay on screen with its reason (#6994 R1 F7).
    const { context, elements } = loadModule();
    context.window.dashboardData.techLeadRuns = ready({ configured: false });
    const button = elements.get('issueDetailInvestigateTechLeadBtn');
    const status = elements.get('issueDetailTechLeadStatus');

    const visible = context.updateTechLeadIssueAction(
        { button, statusEl: status }, 42, true,
    );

    assert.equal(visible, true);
    assert.equal(button.style.display, '');
    assert.equal(button.disabled, true);
    assert.match(status.textContent, /No tech lead agent is configured/);
});

test('an admission response refreshes every visible surface, not just the one clicked', async () => {
    const { context, elements, calls } = loadModule();
    context.window.dashboardData.techLeadRuns = ready();
    context.issueDetailData = { issue_number: 42 };
    const drawerButton = elements.get('issueDetailInvestigateTechLeadBtn');
    const drawerStatus = elements.get('issueDetailTechLeadStatus');
    context.updateTechLeadIssueAction(
        { button: drawerButton, statusEl: drawerStatus }, 42, true,
    );
    // The server accepted the request; the next view model says so.
    context.refreshViewModel = async () => {
        calls.push(['refreshViewModel']);
        context.window.dashboardData.techLeadRuns = ready({ queuedIssueNumbers: [42] });
    };

    await context.investigateWithTechLead(42);

    assert.equal(drawerButton.disabled, true);
    assert.equal(drawerStatus.textContent, 'Tech lead queued');
});
