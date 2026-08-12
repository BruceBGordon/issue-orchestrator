// JS-vm coverage for the row-scoped typed Commands introduced in
// PR #6336 round-2 (issue #6334).  Reviewer found that the legacy
// module-level ``unifiedRunData`` + document-global ids
// (``#e2eTimelineContent``, ``#runDetailsDisclosure``,
// ``#unifiedRunAgent``) broke as soon as two rows could be expanded
// at once.  The new ownership model:
//
//   * Story/Ops/Debug buttons emit ``switch_e2e_timeline_view``.
//     The dispatcher routes to a row-scoped handler that resolves
//     the row from ``triggerEl`` and updates only that row's
//     ``.e2e-timeline-content`` container.
//
//   * The untracked-failures "Create issue(s)" button emits
//     ``create_e2e_untriaged_issues``.  Handler reads the agent
//     from the row-scoped ``.unified-run-agent`` select.
//
// These tests run fast and don't need Playwright — every assertion
// goes through the actual rendered HTML + the real dispatcher.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const DASHBOARD_JS_DIR = path.join(
    __dirname,
    '../../src/issue_orchestrator/static/js/dashboard',
);

function _baseStubs() {
    return {
        console,
        URLSearchParams,
        Map,
        Set,
        Promise,
        Number,
        setTimeout: (fn) => fn,
        document: {
            readyState: 'complete',
            addEventListener: () => {},
            getElementById: () => null,
            querySelector: () => null,
            querySelectorAll: () => [],
        },
        navigator: {},
        escapeHtml: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        escapeAttr: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;'),
        _humanizeSnakeCase: (s) => String(s || ''),
        showToast: () => {},
        formatTimestamp: () => '',
        applyLifecycleDataset: () => {},
        renderTimeline: () => {},
        renderE2EIssueTimelineAffordances: () => '',
        renderCanonicalValidationViewer: () => '<div class="cvv-root"></div>',
        e2eRunToCanonicalPayload: (data) => ({
            status: data && data.run ? data.run.status : 'unknown',
            junit_cases: [],
            failed_tests: [],
        }),
        REPO_ROOT: '/tmp/repo',
        CONFIG_NAME: 'default.yaml',
        CONFIG_MODE: 'default',
    };
}

function _loadFullStack(extra = {}) {
    const ctx = { ..._baseStubs(), ...extra };
    ctx.window = ctx;
    vm.createContext(ctx);
    for (const file of ['lifecycle_commands.js', 'hierarchical_timeline.js', 'e2e_run_view.js', 'e2e_runs_list.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(DASHBOARD_JS_DIR, file), 'utf8'),
            ctx,
            { filename: file },
        );
    }
    return ctx;
}

// ── Helpers for building the in-test DOM ──────────────────────────
//
// The handlers use ``triggerEl.closest('details.e2e-run-row')`` so we
// build a minimal node graph that supports ``closest`` and
// ``querySelector``.  Each row carries its own ``_e2eRunData``,
// timeline container, and agent select — never shared between rows.

function _fakeRow(runId, opts = {}) {
    const timeline = {
        innerHTML: '',
        querySelector: () => null,
    };
    const switcher = {
        buttons: [],
        querySelectorAll: function () { return this.buttons; },
    };
    const agentSelect = {
        value: opts.agent || '',
        focus: () => {},
    };
    const row = {
        nodeName: 'DETAILS',
        open: true,
        dataset: { e2eRunId: String(runId), loaded: '1' },
        classList: { contains: (c) => c === 'e2e-run-row' },
        _e2eRunData: opts.data || {
            run: { id: runId },
            results_by_category: {
                untriaged: opts.untriaged || [],
                has_issue: [], flaky: [], fixed: [],
                passed: [], quarantined: [], skipped: [],
            },
        },
        querySelector(sel) {
            if (sel === '.e2e-timeline-view-switcher') return switcher;
            if (sel === '.e2e-timeline-content') return timeline;
            if (sel === '.unified-run-agent') return agentSelect;
            return null;
        },
        _timeline: timeline,
        _agentSelect: agentSelect,
        _switcher: switcher,
    };
    return row;
}

function _fakeButtonInRow(row, view) {
    const btn = {
        classList: {
            _classes: new Set(['e2e-view-btn']),
            add(c) { this._classes.add(c); },
            remove(c) { this._classes.delete(c); },
            contains(c) { return this._classes.has(c); },
        },
        dataset: { view: view || 'user' },
        // ``closest('details.e2e-run-row')`` returns the row.
        closest(sel) {
            if (sel === 'details.e2e-run-row') return row;
            return null;
        },
    };
    row._switcher.buttons.push(btn);
    return btn;
}

// ── Layer A: typed Command emission on Story/Ops/Debug buttons ────

test('renderRunDetailsDisclosure: diagnostics row carries typed switch_e2e_timeline_view Commands', () => {
    const ctx = _loadFullStack();
    const data = {
        run: {
            id: 88,
            started_at: '2026-05-12T00:00:00Z',
            status: 'passed',
            log_path: '/tmp/run.log',
        },
    };
    const html = ctx.renderRunDetailsDisclosure(data, 88);
    assert.ok(html.includes('Diagnostics'));
    assert.ok(!html.includes('Run details &amp; artifacts'));
    // Issue #6593: artifacts moved to the first-class run-artifacts section;
    // the diagnostics disclosure no longer carries an artifact chip or buttons.
    assert.ok(!html.includes('artifact'));
    assert.ok(!html.includes('data-artifact-path'));
    // Three view buttons; each carries the typed Command with its
    // own ``view``.
    const matches = [...html.matchAll(/data-lifecycle-command="([^"]+)"/g)];
    const decoded = matches.map((m) => JSON.parse(
        m[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&'),
    ));
    const views = decoded.map((c) => c.view);
    assert.deepStrictEqual(views, ['user', 'ops', 'debug']);
    for (const cmd of decoded) {
        assert.strictEqual(cmd.kind, 'switch_e2e_timeline_view');
        assert.strictEqual(cmd.run_id, 88);
    }
    // Disclosure uses a class, not an id — multi-row safe.
    assert.ok(html.includes('class="run-details-disclosure run-diagnostics-row"'));
    assert.ok(!html.includes('id="runDetailsDisclosure"'));
    // Timeline container uses a class too.
    assert.ok(html.includes('class="e2e-timeline-content"'));
    assert.ok(!html.includes('id="e2eTimelineContent"'));
    // Inline onclick routes through the shared dispatcher.
    assert.ok((html.match(/runLifecycleCommandFromButton\(this\)/g) || []).length === 3);
});

test('renderUntrackedFailuresBanner: button carries typed create_e2e_untriaged_issues Command + row-scoped agent class', () => {
    const ctx = _loadFullStack({
        dashboardData: { agents: ['agent:web', 'agent:vscode'] },
    });
    const banner = ctx._renderUntrackedFailuresBanner(2, 88);
    const match = banner.match(/data-lifecycle-command="([^"]+)"/);
    assert.ok(match, 'banner button must carry data-lifecycle-command');
    const cmd = JSON.parse(match[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&'));
    assert.strictEqual(cmd.kind, 'create_e2e_untriaged_issues');
    assert.strictEqual(cmd.run_id, 88);
    // Agent select uses a row-scoped class, NOT the legacy
    // ``#unifiedRunAgent`` id (which collided across rows).
    assert.ok(banner.includes('class="agent-select unified-run-agent"'));
    assert.ok(!banner.includes('id="unifiedRunAgent"'));
});

// ── Layer B: dispatcher → row-scoped handler ──────────────────────

test('switch_e2e_timeline_view dispatch: handler updates ONLY the row whose button fired', async () => {
    const ctx = _loadFullStack();
    const calls = [];
    ctx.fetch = (url) => {
        calls.push(url);
        return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve({ run: { id: 88, status: 'passed' } }),
        });
    };

    // Two expanded rows, two distinct ``_e2eRunData`` payloads.
    const rowA = _fakeRow(88);
    const rowB = _fakeRow(99);
    const btnInA = _fakeButtonInRow(rowA, 'ops');

    ctx.runLifecycleCommand(
        {
            kind: 'switch_e2e_timeline_view',
            label: 'Switch diagnostics timeline to Ops',
            run_id: 88,
            view: 'ops',
        },
        btnInA,
    );

    // Loading state lands in row A's container, NOT row B's.
    assert.ok(rowA._timeline.innerHTML.includes('Loading'), 'row A timeline must show loading');
    assert.strictEqual(rowB._timeline.innerHTML, '', 'row B timeline must NOT be touched');

    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));

    // The fetch targets run 88 with view=ops — taken from the typed
    // Command, not from any module-level singleton.
    assert.deepStrictEqual(calls, ['/api/e2e-run-detail/88?view=ops']);

    // ``active`` class toggled within row A's switcher only.
    assert.ok(btnInA.classList.contains('active'));
});

test('switch_e2e_timeline_view dispatch: two rows expanded, each updates independently', async () => {
    const ctx = _loadFullStack();
    const calls = [];
    ctx.fetch = (url) => {
        calls.push(url);
        return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve({ run: { id: Number(url.match(/(\d+)\?/)[1]) } }),
        });
    };

    const rowA = _fakeRow(88);
    const rowB = _fakeRow(99);
    const btnA = _fakeButtonInRow(rowA, 'debug');
    const btnB = _fakeButtonInRow(rowB, 'ops');

    // Fire on A then B.  Each Command names its own run id.
    ctx.runLifecycleCommand(
        { kind: 'switch_e2e_timeline_view', label: 'x', run_id: 88, view: 'debug' },
        btnA,
    );
    ctx.runLifecycleCommand(
        { kind: 'switch_e2e_timeline_view', label: 'x', run_id: 99, view: 'ops' },
        btnB,
    );

    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));

    // Both rows fetched THEIR own detail.  Two calls, two runs, two
    // views — no cross-row contamination.
    assert.deepStrictEqual(calls.sort(), [
        '/api/e2e-run-detail/88?view=debug',
        '/api/e2e-run-detail/99?view=ops',
    ]);
});

test('create_e2e_untriaged_issues dispatch: reads agent from THIS row, fetches with THIS run_id', async () => {
    const ctx = _loadFullStack();
    const calls = [];
    ctx.fetch = (url, init) => {
        calls.push({ url, body: init && init.body ? JSON.parse(init.body) : null });
        return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve({
                parent_issue: { number: 5000, url: '' },
                sub_issues: [{ number: 5001 }],
            }),
        });
    };

    const rowA = _fakeRow(88, {
        agent: 'agent:web',
        untriaged: [
            { nodeid: 'tests/foo.py::test_a' },
            { nodeid: 'tests/foo.py::test_b' },
        ],
    });
    const rowB = _fakeRow(99, {
        agent: 'agent:vscode',  // different agent in row B
        untriaged: [{ nodeid: 'tests/bar.py::test_c' }],
    });
    const btnInA = {
        closest: (sel) => sel === 'details.e2e-run-row' ? rowA : null,
    };

    ctx.runLifecycleCommand(
        {
            kind: 'create_e2e_untriaged_issues',
            label: 'Create issues',
            run_id: 88,
        },
        btnInA,
    );

    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));

    assert.strictEqual(calls.length, 1);
    assert.ok(calls[0].url.startsWith('/control/e2e/create-issues/88'),
        `expected /control/e2e/create-issues/88 url, got ${calls[0].url}`);
    // Agent comes from THIS row's select — not row B's.
    assert.strictEqual(calls[0].body.agent, 'agent:web');
    assert.deepStrictEqual(calls[0].body.nodeids, [
        'tests/foo.py::test_a',
        'tests/foo.py::test_b',
    ]);
});

test('create_e2e_untriaged_issues dispatch: no agent selected → toast, no fetch', async () => {
    const ctx = _loadFullStack();
    const toasts = [];
    ctx.showToast = (msg) => toasts.push(msg);
    ctx.fetch = () => {
        throw new Error('fetch should not fire when agent is empty');
    };

    const row = _fakeRow(88, {
        agent: '',  // empty
        untriaged: [{ nodeid: 'tests/foo.py::test_a' }],
    });
    const btn = {
        closest: (sel) => sel === 'details.e2e-run-row' ? row : null,
    };

    ctx.runLifecycleCommand(
        { kind: 'create_e2e_untriaged_issues', label: 'x', run_id: 88 },
        btn,
    );

    await new Promise((resolve) => setImmediate(resolve));
    assert.ok(toasts.some((t) => /agent/i.test(t)),
        `expected an agent-required toast, got: ${JSON.stringify(toasts)}`);
});

test('create_e2e_untriaged_issues dispatch: row without _e2eRunData → toast, no fetch', async () => {
    const ctx = _loadFullStack();
    const toasts = [];
    ctx.showToast = (msg) => toasts.push(msg);
    ctx.fetch = () => {
        throw new Error('fetch should not fire without run data');
    };

    const row = _fakeRow(88);
    row._e2eRunData = null;  // not yet loaded
    const btn = {
        closest: (sel) => sel === 'details.e2e-run-row' ? row : null,
    };

    ctx.runLifecycleCommand(
        { kind: 'create_e2e_untriaged_issues', label: 'x', run_id: 88 },
        btn,
    );

    await new Promise((resolve) => setImmediate(resolve));
    assert.ok(toasts.some((t) => /not loaded/i.test(t)),
        `expected a "not loaded" toast, got: ${JSON.stringify(toasts)}`);
});

// ── Layer C: no global state survives the round-2 fix ─────────────

test('JS bundle does not write the legacy ``window.unifiedRunData`` singleton', () => {
    const ctx = _loadFullStack();
    // Run the row loader and assert that ``window.unifiedRunData``
    // stays undefined throughout.  The legacy modal kept this as a
    // top-level binding; PR #6336 round-2 deletes the entire write.
    assert.strictEqual(ctx.unifiedRunData, undefined,
        'no top-level unifiedRunData binding may exist on the module');
    assert.strictEqual(ctx.window.unifiedRunData, undefined,
        'no window.unifiedRunData write may survive');
});

// ── Layer D: single owner of row-targeting policy ────────────────
//
// ``resolveRowCommandContext`` is the one place row-targeting policy
// lives.  The two row-mounted handlers MUST go through it; if a
// future refactor duplicates ``triggerEl.closest('details.e2e-run-row')``
// or ``row.querySelector('.e2e-timeline-content')`` outside this
// function, that's the bug PR #6336 round-3 review flagged.

test('row-targeting policy is centralized in resolveRowCommandContext', () => {
    const ctx = _loadFullStack();
    // The abstraction exists and is publishable for tests.
    assert.strictEqual(typeof ctx.resolveRowCommandContext, 'function',
        'resolveRowCommandContext must be defined in e2e_run_view.js');

    // The two row-mounted handlers must NOT duplicate the row
    // lookup.  Read the function bodies and assert they contain
    // exactly one call site each — to ``resolveRowCommandContext``
    // — and zero direct ``triggerEl.closest('details.e2e-run-row')``
    // calls outside the abstraction.
    const source = ctx.switchE2ETimelineView.toString()
        + '\n----\n'
        + ctx.createIssuesForUntriaged.toString();
    assert.ok(
        /resolveRowCommandContext\(\s*runId\s*,\s*triggerEl\s*\)/.test(source),
        'handlers must resolve row context via resolveRowCommandContext',
    );
    assert.ok(
        !source.includes("triggerEl.closest('details.e2e-run-row')"),
        'handlers must not duplicate the closest() lookup outside the abstraction',
    );
    assert.ok(
        !source.includes("row.querySelector('.e2e-timeline-content')"),
        'handlers must not duplicate the timeline-container query',
    );
    assert.ok(
        !source.includes("row.querySelector('.unified-run-agent')"),
        'handlers must not duplicate the agent-select query',
    );
});

test('resolveRowCommandContext: rejects non-positive integer run_id', () => {
    const ctx = _loadFullStack();
    const row = _fakeRow(88);
    const triggerEl = { closest: (sel) => sel === 'details.e2e-run-row' ? row : null };
    assert.strictEqual(ctx.resolveRowCommandContext(0, triggerEl), null);
    assert.strictEqual(ctx.resolveRowCommandContext(-1, triggerEl), null);
    assert.strictEqual(ctx.resolveRowCommandContext(NaN, triggerEl), null);
    assert.strictEqual(ctx.resolveRowCommandContext(1.5, triggerEl), null,
        'non-integer numbers must reject');
});

test('resolveRowCommandContext: rejects non-number run_id (strict contract mirror)', () => {
    // The typed Pydantic Command has ``strict=True`` on ``run_id``;
    // the runtime abstraction enforces the same contract so a
    // coerced ``"88"`` or boolean payload cannot silently route to
    // the wrong run.  ``Number(true) === 1`` and ``Number("88") === 88``
    // are exactly the silent-coercion bugs this guard prevents.
    const ctx = _loadFullStack();
    const row88 = _fakeRow(88);
    const row1 = _fakeRow(1);
    const triggerInRow88 = { closest: (sel) => sel === 'details.e2e-run-row' ? row88 : null };
    const triggerInRow1 = { closest: (sel) => sel === 'details.e2e-run-row' ? row1 : null };

    // Stringified payload: a stale producer sent ``"88"`` instead
    // of ``88``.  Reject — even though ``row88.dataset.e2eRunId``
    // matches the string after coercion.
    assert.strictEqual(ctx.resolveRowCommandContext('88', triggerInRow88), null,
        'string run_id must reject (strict contract mirror)');
    // Boolean payload: ``Number(true) === 1`` would route the
    // action to row 1.  Reject.
    assert.strictEqual(ctx.resolveRowCommandContext(true, triggerInRow1), null,
        'boolean run_id must reject (strict contract mirror)');
    assert.strictEqual(ctx.resolveRowCommandContext(false, triggerInRow88), null);
    // null / undefined / objects / arrays all rejected.
    assert.strictEqual(ctx.resolveRowCommandContext(null, triggerInRow88), null);
    assert.strictEqual(ctx.resolveRowCommandContext(undefined, triggerInRow88), null);
    assert.strictEqual(ctx.resolveRowCommandContext({}, triggerInRow88), null);
    assert.strictEqual(ctx.resolveRowCommandContext([88], triggerInRow88), null);
});

test('resolveRowCommandContext: rejects trigger that does not resolve to a row', () => {
    const ctx = _loadFullStack();
    const orphanTrigger = { closest: () => null };
    assert.strictEqual(ctx.resolveRowCommandContext(88, orphanTrigger), null);
    assert.strictEqual(ctx.resolveRowCommandContext(88, null), null);
    assert.strictEqual(ctx.resolveRowCommandContext(88, {}), null);
});

test('resolveRowCommandContext: rejects Command run_id that disagrees with the row dataset', () => {
    // The typed Command says run 88 but the trigger is inside a row
    // whose ``data-e2e-run-id`` is 99 — the abstraction MUST refuse
    // to act on this mismatch (it would be the typed payload and
    // the DOM disagreeing about which run is targeted).
    const ctx = _loadFullStack();
    const row99 = _fakeRow(99);
    const triggerInRow99 = {
        closest: (sel) => sel === 'details.e2e-run-row' ? row99 : null,
    };
    assert.strictEqual(ctx.resolveRowCommandContext(88, triggerInRow99), null,
        'typed-Command run_id disagreeing with row.dataset.e2eRunId must reject');
});

test('resolveRowCommandContext: returns a frozen context with row-scoped accessors', () => {
    const ctx = _loadFullStack();
    const row = _fakeRow(88, { agent: 'agent:web', untriaged: [{ nodeid: 'x' }] });
    const trigger = { closest: (sel) => sel === 'details.e2e-run-row' ? row : null };
    const result = ctx.resolveRowCommandContext(88, trigger);
    assert.ok(result, 'matching run_id + valid trigger must produce a context');
    assert.strictEqual(result.runId, 88);
    assert.strictEqual(result.row, row);
    assert.strictEqual(result.data, row._e2eRunData);
    assert.strictEqual(result.timelineContainer(), row._timeline);
    assert.strictEqual(result.viewSwitcher(), row._switcher);
    assert.strictEqual(result.agentSelect(), row._agentSelect);
    // ``markUnloaded`` clears the row's ``loaded`` flag so the next
    // expand re-fetches.
    row.dataset.loaded = '1';
    result.markUnloaded();
    assert.strictEqual(row.dataset.loaded, '');
    // The context is frozen — handlers can't accidentally mutate
    // its shape.
    assert.ok(Object.isFrozen(result));
});

test('handlers dispatched via the typed pipeline still hit row-scoped state', async () => {
    // Reviewer's explicit ask: "Update the JS-vm tests to continue
    // going through the typed dispatcher so the command-pattern-to-UI
    // layer remains covered."  This test re-confirms that — even
    // after introducing the abstraction — a typed Command dispatched
    // through ``runLifecycleCommand`` still flows through
    // ``resolveRowCommandContext`` and reaches the row.
    const ctx = _loadFullStack();
    const calls = [];
    ctx.fetch = (url) => {
        calls.push(url);
        return Promise.resolve({
            ok: true, status: 200,
            json: () => Promise.resolve({ run: { id: 88, status: 'failed' } }),
        });
    };
    const row = _fakeRow(88);
    const trigger = _fakeButtonInRow(row, 'ops');

    ctx.runLifecycleCommand(
        { kind: 'switch_e2e_timeline_view', label: 'x', run_id: 88, view: 'ops' },
        trigger,
    );
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepStrictEqual(calls, ['/api/e2e-run-detail/88?view=ops']);
    assert.ok(row._timeline.innerHTML !== '', 'row timeline must be updated via the abstraction');
});
