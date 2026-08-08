// One keyboard gesture must send exactly ONE request (#6994 round 4 F15).
//
// `addKeyboardSupport` synthesises a click, so an element wired twice turns one
// Enter/Space into two activations — two POSTs, two toasts — while a pointer
// user gets one. That is a silent behavioural split between input methods, and
// it is invisible to a harness that keeps only the LAST listener per event or
// that loads `tech_lead_runs.js` on its own.
//
// So this harness deliberately does neither: it keeps EVERY listener, and it
// loads the real chunks in the real order the dashboard bundle uses
// (`issue_metadata.js` -> `issue_menus.js` -> `tech_lead_runs.js`), so a second
// registration from any chunk would show up here as a second request.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const staticDir = path.join(__dirname, '../../src/issue_orchestrator/static/js');
const dashboardDir = path.join(staticDir, 'dashboard');

// The production order, read from the same list the view model publishes, so a
// reordering of the bundle cannot silently invalidate this test.
const ASSETS = path.join(
    __dirname,
    '../../src/issue_orchestrator/view_models/dashboard_assets.py',
);
const CHUNKS = ['issue_metadata.js', 'issue_menus.js', 'tech_lead_runs.js'];

function bundleOrder() {
    const source = fs.readFileSync(ASSETS, 'utf8');
    return CHUNKS.slice().sort(
        (a, b) => source.indexOf(`"${a}"`) - source.indexOf(`"${b}"`),
    );
}

// Drain the microtask queue the async command surface runs on. A macrotask
// boundary, not a delay: nothing here waits for time to pass.
const settled = () => new Promise((resolve) => setImmediate(resolve));

function loadDashboard() {
    const fetches = [];
    const toasts = [];
    const documentListeners = {};

    // A node that keeps EVERY listener for an event, because the defect under
    // test is precisely "two listeners for one gesture".
    function makeElement(id) {
        const listeners = {};
        return {
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
            parentNode: null,
            children: [],
            listeners,
            classList: {
                _set: new Set(),
                add(name) { this._set.add(name); },
                remove(name) { this._set.delete(name); },
                toggle(name, on) { if (on) this._set.add(name); else this._set.delete(name); },
                contains(name) { return this._set.has(name); },
            },
            setAttribute(name, value) { this.attributes[name] = value; },
            removeAttribute(name) { delete this.attributes[name]; },
            getAttribute(name) { return this.attributes[name]; },
            addEventListener(event, handler) {
                (listeners[event] = listeners[event] || []).push(handler);
            },
            removeEventListener() {},
            insertBefore(node) {
                if (!this.children.includes(node)) this.children.push(node);
                node.parentNode = this;
            },
            removeChild(node) {
                this.children = this.children.filter((child) => child !== node);
                node.parentNode = null;
            },
            contains() { return false; },
            querySelectorAll() { return []; },
            getBoundingClientRect() { return { width: 0, height: 0 }; },
            // Dispatch to EVERY click listener, exactly as a real element does.
            click() {
                for (const handler of listeners.click || []) {
                    handler(clickEvent());
                }
            },
            // Dispatch to EVERY keydown listener — the whole point of the test.
            press(key) {
                for (const handler of listeners.keydown || []) {
                    handler(keyEvent(key));
                }
            },
            focus() {},
        };
    }

    function clickEvent() {
        return { preventDefault() {}, stopPropagation() {}, target: null };
    }

    function keyEvent(key) {
        return { key, preventDefault() {}, stopPropagation() {} };
    }

    const elements = new Map();
    const element = (id) => {
        if (!elements.has(id)) elements.set(id, makeElement(id));
        return elements.get(id);
    };

    const context = {
        console: { log() {}, warn() {}, error() {}, debug() {} },
        document: {
            // Every id the chunks ask for resolves, so module-scope wiring runs
            // exactly as it does in the browser.
            getElementById: (id) => element(id),
            createElement: () => makeElement(''),
            querySelectorAll: () => [],
            querySelector: () => null,
            addEventListener: (event, handler) => {
                (documentListeners[event] = documentListeners[event] || []).push(handler);
            },
            body: makeElement('body'),
        },
        window: {
            dashboardData: {
                startupComplete: true,
                techLeadRuns: {
                    globalStatus: 'idle',
                    globalStatusLabel: '',
                    healthReviewStatus: 'idle',
                    healthReviewStatusLabel: '',
                    globalBarrierNote: '',
                    queuedIssueNumbers: [],
                    runningIssueNumbers: [],
                    globalBarrierActive: false,
                    unavailableReason: '',
                    needsSettings: false,
                },
            },
            setTimeout: () => 0,
            clearTimeout: () => {},
            setInterval: () => 0,
            clearInterval: () => {},
            addEventListener: () => {},
            location: { href: '', reload() {} },
            scrollX: 0,
            scrollY: 0,
            innerWidth: 1280,
            innerHeight: 800,
        },
        setTimeout: () => 0,
        clearTimeout: () => {},
        setInterval: () => 0,
        clearInterval: () => {},
        EventSource: function EventSourceStub() {
            return { close() {}, addEventListener() {} };
        },
        issueDetailData: null,
        fetch: async (endpoint, init) => {
            fetches.push({ endpoint, init });
            return { ok: true, status: 200, json: async () => ({ detail: 'Queued.', admitted: true }) };
        },
        showToast: (message, kind) => toasts.push([message, kind]),
        refreshViewModel: async () => {},
        closeSettingsMenu: () => {},
        showConfigDialog: () => {},
        escapeHtml: (value) => String(value),
        escapeAttr: (value) => String(value),
    };
    context.window.document = context.document;
    context.globalThis = context;
    vm.createContext(context);

    vm.runInContext(
        fs.readFileSync(path.join(staticDir, 'ui_action_contract.js'), 'utf8'),
        context,
    );
    for (const chunk of bundleOrder()) {
        vm.runInContext(
            fs.readFileSync(path.join(dashboardDir, chunk), 'utf8'),
            context,
        );
    }
    for (const handler of documentListeners.DOMContentLoaded || []) handler();

    return { context, element, fetches, toasts };
}

test('the real bundle order is metadata, menus, then tech-lead runs', () => {
    assert.deepEqual(bundleOrder(), [
        'issue_metadata.js',
        'issue_menus.js',
        'tech_lead_runs.js',
    ]);
});

test('the harness keeps every listener, so a duplicate registration is visible', () => {
    const { element } = loadDashboard();
    const item = element('menuInvestigateTechLead');

    // If this were 0 the test would prove nothing; if the fix regresses it
    // becomes 2 and the request assertion below fails.
    assert.equal((item.listeners.keydown || []).length, 1);
    assert.equal((item.listeners.click || []).length, 1);
});

for (const key of ['Enter', ' ']) {
    test(`activating the card menu action with "${key}" sends exactly one request`, async () => {
        const { element, fetches, toasts } = loadDashboard();
        const item = element('menuInvestigateTechLead');
        item.dataset.issue = '42';

        item.press(key);
        await settled();

        assert.equal(fetches.length, 1, 'one keypress must be one request');
        assert.equal(fetches[0].endpoint, '/api/tech-lead/runs');
        assert.deepEqual(JSON.parse(fetches[0].init.body), {
            scope: { kind: 'issue', issue_number: 42 },
        });
        assert.equal(toasts.length, 1, 'one keypress must be one toast');
    });
}

test('pointer and keyboard activation send the same single request', async () => {
    const keyboard = loadDashboard();
    keyboard.element('menuInvestigateTechLead').dataset.issue = '42';
    keyboard.element('menuInvestigateTechLead').press('Enter');
    await settled();

    const pointer = loadDashboard();
    pointer.element('menuInvestigateTechLead').dataset.issue = '42';
    pointer.element('menuInvestigateTechLead').click();
    await settled();

    assert.equal(keyboard.fetches.length, pointer.fetches.length);
    assert.deepEqual(
        JSON.parse(keyboard.fetches[0].init.body),
        JSON.parse(pointer.fetches[0].init.body),
    );
});

test('a key that is not an activation key sends nothing', async () => {
    const { element, fetches } = loadDashboard();
    const item = element('menuInvestigateTechLead');
    item.dataset.issue = '42';

    item.press('a');
    await settled();

    assert.equal(fetches.length, 0);
});

test('wiring the same element twice still yields one activation', () => {
    // The helper itself enforces "exactly once", so a future chunk that also
    // registers this element cannot reintroduce the double request.
    const { context, element } = loadDashboard();
    const item = element('menuInvestigateTechLead');

    context.addKeyboardSupport(item);
    context.addKeyboardSupport(item);

    assert.equal((item.listeners.keydown || []).length, 1);
});
