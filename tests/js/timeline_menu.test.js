const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class FakeElement {
    constructor(rect = {}) {
        this.attrs = new Set();
        this.rect = {
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            width: 0,
            height: 0,
            ...rect,
        };
        this.style = {};
        this.dataset = {};
        this.listeners = [];
        this.offsetParent = null;
        this.trigger = null;
        this.items = null;
    }

    getBoundingClientRect() {
        return this.rect;
    }

    hasAttribute(name) {
        return this.attrs.has(name);
    }

    setAttribute(name) {
        this.attrs.add(name);
    }

    removeAttribute(name) {
        this.attrs.delete(name);
    }

    addEventListener(type, handler) {
        this.listeners.push({ type, handler });
    }

    querySelector(selector) {
        if (selector === '.timeline-event-menu-trigger') return this.trigger;
        if (selector === '.timeline-event-menu-items') return this.items;
        return null;
    }

    closest() {
        return null;
    }
}

function loadTimeline(overrides = {}) {
    const body = new FakeElement();
    const documentElement = new FakeElement();
    const openMenus = [];
    const context = {
        console,
        Element: FakeElement,
        document: {
            body,
            documentElement,
            querySelectorAll(selector) {
                assert.equal(selector, '.timeline-event-menu[open]');
                return openMenus.filter(menu => menu.hasAttribute('open'));
            },
        },
        window: {
            innerWidth: 800,
            innerHeight: 600,
            open: () => {},
        },
        openMenus,
        escapeHtml: value => String(value ?? ''),
        escapeAttr: value => String(value ?? ''),
        openPath: () => {},
        showToast: () => {},
        openReviewFeedback: () => {},
        openValidationFailure: () => {},
        openReviewTranscript: () => {},
        openAgentLogAction: () => {},
        copyAgentLogAction: () => {},
        viewClaudeLog: () => {},
        openFilteredOrchestratorLog: () => {},
        openSessionManifest: () => {},
        openTimelineEventDetails: () => {},
        openModal: () => {},
        modalOverlay: { classList: { add: () => {} } },
        formatTimestamp: (value, fallback = '') => value ? `local:${String(value).slice(0, 10)}` : fallback,
        ...overrides,
    };
    vm.createContext(context);
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/timeline.js'),
        'utf8',
    );
    vm.runInContext(source, context, { filename: 'timeline.js' });
    return context;
}

function makeMenu({ drawerLeft = 0, drawerTop = 0 } = {}) {
    const drawer = new FakeElement({ left: drawerLeft, top: drawerTop });
    const trigger = new FakeElement({
        top: 96,
        bottom: 120,
        right: 792,
    });
    const items = new FakeElement({
        width: 248,
        height: 36,
    });
    items.offsetParent = drawer;
    const menu = new FakeElement();
    menu.trigger = trigger;
    menu.items = items;
    return { menu, items };
}

function assertPositionForOffsetParent(offsetParentForContext, expected) {
    const context = loadTimeline();
    const { menu, items } = makeMenu();
    items.offsetParent = offsetParentForContext(context);

    context.positionTimelineEventMenu(menu);

    assert.equal(items.style.left, expected.left);
    assert.equal(items.style.top, expected.top);
}

test('positionTimelineEventMenu subtracts transformed fixed containing block offset', () => {
    const context = loadTimeline();
    const { menu, items } = makeMenu({ drawerLeft: 240 });

    context.positionTimelineEventMenu(menu);

    assert.equal(items.style.left, '304px');
    assert.equal(items.style.top, '124px');
});

test('positionTimelineEventMenu leaves viewport coordinates unmodified without fixed containing block', () => {
    assertPositionForOffsetParent(() => null, { left: '544px', top: '124px' });
    assertPositionForOffsetParent(
        context => context.document.body,
        { left: '544px', top: '124px' },
    );
    assertPositionForOffsetParent(
        context => context.document.documentElement,
        { left: '544px', top: '124px' },
    );
    assertPositionForOffsetParent(
        () => ({ getBoundingClientRect: () => ({ left: 240, top: 12 }) }),
        { left: '544px', top: '124px' },
    );
});

test('toggleTimelineEventMenu opens the clicked menu and closes other menus', () => {
    const context = loadTimeline();
    const { menu, items } = makeMenu({ drawerLeft: 240 });
    const otherMenu = new FakeElement();
    otherMenu.setAttribute('open');
    context.openMenus.push(menu, otherMenu);

    context.toggleTimelineEventMenu(menu);

    assert.equal(menu.hasAttribute('open'), true);
    assert.equal(otherMenu.hasAttribute('open'), false);
    assert.equal(items.style.left, '304px');

    context.toggleTimelineEventMenu(menu);

    assert.equal(menu.hasAttribute('open'), false);
});

test('bindTimelineEventActions binds the shared action delegate once', () => {
    const context = loadTimeline();
    const container = new FakeElement();

    context.bindTimelineEventActions(container);
    context.bindTimelineEventActions(container);

    assert.equal(container.dataset.timelineActionsBound, '1');
    assert.equal(container.listeners.length, 1);
    assert.equal(container.listeners[0].type, 'click');
    assert.equal(container.listeners[0].handler, context.handleTimelineEventActionsClick);
});

test('timeline event detail rows format timestamp fields locally', () => {
    const context = loadTimeline();

    const html = context._renderTimelineEventDetailRows({
        event: 'session.started',
        timestamp: '2026-05-12T10:00:00Z',
        finished_at: '2026-05-12T10:00:00Z',
        detail_value_kinds: {
            timestamp: 'timestamp',
            finished_at: 'timestamp',
        },
        summary: 'started',
    });

    assert.match(html, /<dt>timestamp<\/dt>/);
    assert.match(html, /<dt>finished_at<\/dt>/);
    assert.match(html, /local:2026-05-12/);
    assert.doesNotMatch(html, /detail_value_kinds/);
    assert.doesNotMatch(html, /2026-05-12T10:00:00Z/);
});

test('timeline event detail rows do not infer timestamps from field names', () => {
    const context = loadTimeline();

    const html = context._renderTimelineEventDetailRows({
        started_at: '2026-05-12T10:00:00Z',
    });

    assert.match(html, /2026-05-12T10:00:00Z/);
    assert.doesNotMatch(html, /local:2026-05-12/);
});

test('handleTimelineEventActionsClick toggles overflow menus through shared delegate', () => {
    const context = loadTimeline();
    const { menu, items } = makeMenu({ drawerLeft: 240 });
    const calls = [];
    menu.trigger.closest = (selector) => {
        if (selector === '.timeline-event-menu-trigger') return menu.trigger;
        if (selector === '.timeline-event-menu') return menu;
        return null;
    };
    context.openMenus.push(menu);

    context.handleTimelineEventActionsClick({
        target: menu.trigger,
        preventDefault: () => calls.push('preventDefault'),
        stopPropagation: () => calls.push('stopPropagation'),
    });

    assert.deepEqual(calls, ['preventDefault', 'stopPropagation']);
    assert.equal(menu.hasAttribute('open'), true);
    assert.equal(items.style.left, '304px');
});

test('handleTimelineEventActionsClick stops propagation on action-button clicks (Blocker 2 on PR #6315)', () => {
    // Reviewer Blocker 2 on PR #6315: without ``event.stopPropagation``,
    // an action-button click inside the per-issue drawer's validation
    // step row would bubble up to the row's onclick (which toggles the
    // inline canonical-viewer expansion).  The button click was meant
    // for the button, not the row — stop propagation so the row's
    // expansion isn't a second-effect.
    const context = loadTimeline();
    const actionPayload = JSON.stringify({ type: 'open_validation_failure', issue_number: 42 });
    const button = new FakeElement();
    button.attrs.add('class:timeline-action-btn');  // unused; closest() returns it directly
    button.dataset = { action: actionPayload };
    button.closest = (selector) => {
        if (selector === '.timeline-action-btn, .timeline-menu-item') return button;
        return null;
    };
    const calls = [];
    context.handleTimelineEventActionsClick({
        target: button,
        preventDefault: () => calls.push('preventDefault'),
        stopPropagation: () => calls.push('stopPropagation'),
    });
    assert.ok(calls.includes('stopPropagation'), 'stopPropagation must be called on action clicks');
});

test('handleTimelineEventActionsClick accepts a text-node target from the summary label', () => {
    const context = loadTimeline();
    const { menu, items } = makeMenu({ drawerLeft: 240 });
    const calls = [];
    menu.trigger.closest = (selector) => {
        if (selector === '.timeline-event-menu-trigger') return menu.trigger;
        if (selector === '.timeline-event-menu') return menu;
        return null;
    };
    context.openMenus.push(menu);

    context.handleTimelineEventActionsClick({
        target: { parentElement: menu.trigger },
        preventDefault: () => calls.push('preventDefault'),
        stopPropagation: () => calls.push('stopPropagation'),
    });

    assert.deepEqual(calls, ['preventDefault', 'stopPropagation']);
    assert.equal(menu.hasAttribute('open'), true);
    assert.equal(items.style.left, '304px');
});

test('timeline evidence action dispatches the exact-run pin command', () => {
    const context = loadTimeline();
    const calls = [];
    context.setTimelineEvidencePinned = action => calls.push(action);
    const action = {
        type: 'set_timeline_evidence_pin',
        issue_number: 42,
        run_dir: '/runs/failed-42',
        pinned: true,
    };

    context.runTimelineEventAction(action);

    assert.deepEqual(calls, [action]);
});

test('pinning timeline evidence posts through the typed request and refreshes its drawer', async () => {
    const calls = [];
    const context = loadTimeline({
        window: { confirm: () => true },
        issueDetailData: { issue_number: 42 },
        uiActionContract: {
            buildTimelineEvidencePinRequest: (...args) => {
                calls.push(['request', ...args]);
                return {
                    endpoint: '/api/issues/42/timeline-evidence/pin',
                    method: 'PUT',
                    body: { run_dir: '/runs/failed-42', pinned: true },
                };
            },
        },
        fetch: async (endpoint, options) => {
            calls.push(['fetch', endpoint, options]);
            return {
                ok: true,
                json: async () => ({ pinned: true }),
            };
        },
        showToast: message => calls.push(['toast', message]),
        openIssueDetail: async (...args) => calls.push(['refresh', ...args]),
    });

    await context.setTimelineEvidencePinned({
        issue_number: 42,
        run_dir: '/runs/failed-42',
        pinned: true,
        confirm_message: '',
        repo_root: '/repos/project',
    });

    assert.deepEqual(calls[0], [
        'request', 42, '/runs/failed-42', true, '/repos/project',
    ]);
    const fetchCall = calls.find(call => call[0] === 'fetch');
    assert.equal(fetchCall[1], '/api/issues/42/timeline-evidence/pin');
    assert.equal(fetchCall[2].method, 'PUT');
    assert.deepEqual(JSON.parse(fetchCall[2].body), {
        run_dir: '/runs/failed-42',
        pinned: true,
    });
    const refreshCall = calls.at(-1);
    assert.equal(refreshCall[0], 'refresh');
    assert.equal(refreshCall[1], 42);
    assert.equal(refreshCall[2], null);
    assert.equal(refreshCall[3].focus, 'timeline');
    assert.equal(refreshCall[3].repoRoot, '/repos/project');
});

test('expired evidence unpin confirmation can cancel before mutation', async () => {
    const calls = [];
    const context = loadTimeline({
        window: {
            confirm: message => {
                calls.push(['confirm', message]);
                return false;
            },
        },
        uiActionContract: {
            buildTimelineEvidencePinRequest: () => calls.push(['request']),
        },
        fetch: async () => calls.push(['fetch']),
    });

    await context.setTimelineEvidencePinned({
        issue_number: 42,
        run_dir: '/runs/failed-42',
        pinned: false,
        confirm_message: 'Unpinning removes this evidence now.',
    });

    assert.deepEqual(calls, [[
        'confirm',
        'Unpinning removes this evidence now.',
    ]]);
});

test('timeline evidence badge exposes visible text and status-independent help', () => {
    const context = loadTimeline();

    const html = context.renderTimelineEvidence({
        status: 'pinned',
        label: 'Pinned',
        help_text: 'Pinned evidence is retained until unpinned.',
    });

    assert.match(html, /timeline-evidence-pinned/);
    assert.match(html, />Pinned<\/span>/);
    assert.match(html, /title="Pinned evidence is retained until unpinned\."/);
    assert.match(html, /aria-label="Pinned evidence is retained until unpinned\."/);
});
