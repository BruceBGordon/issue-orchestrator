// Behavioral tests for E2E runtime dashboard actions.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadE2ERuntime(overrides = {}) {
    const calls = [];
    const toasts = [];
    const context = {
        console,
        calls,
        toasts,
        URLSearchParams,
        setInterval: () => 1,
        clearInterval: () => {},
        setTimeout: () => 1,
        alert: () => {},
        confirm: () => true,
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        showToast: (message, severity) => toasts.push([String(message), severity]),
        // PR #6329 reviewer Blocker 2: ``showLatestE2ERunResults``,
        // ``showE2ERunResultsById``, and ``showE2ERunDetails`` route
        // through the typed Command pipeline (``runLifecycleCommand``),
        // not directly through ``showUnifiedRunView``.  Tests now
        // record the Command that was dispatched, then the stub
        // forwards to ``showUnifiedRunView`` so the recorded chain
        // matches what the real dispatcher does.
        runLifecycleCommand: (command) => {
            calls.push(['run_e2e_lifecycle_command', command]);
            if (command && command.kind === 'open_e2e_run' && command.run_id) {
                calls.push(['show_unified_run_view', command.run_id]);
            }
        },
        showUnifiedRunView: (runId) => calls.push(['show_unified_run_view', runId]),
        formatTimestamp: (value) => `local:${String(value).slice(0, 10)}`,
        document: {
            addEventListener: () => {},
            getElementById: () => null,
        },
        window: {
            location: { search: '' },
            dashboardData: {
                repoRoot: '/tmp/repo',
                configName: 'default.yaml',
                configMode: 'default',
                e2eRunning: false,
                e2eLastRun: null,
                e2eNeedsAttention: false,
                e2eFailedTests: [],
            },
        },
        ...overrides,
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(
            path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/e2e_runtime.js'),
            'utf8',
        ),
        context,
        { filename: 'e2e_runtime.js' },
    );
    Object.assign(context, overrides);
    return context;
}

test('start request carries the dashboard mode and config identity', async () => {
    const fetchCalls = [];
    const ctx = loadE2ERuntime({
        fetch: async (url, options = {}) => {
            fetchCalls.push([url, options]);
            return { ok: true, json: async () => ({}) };
        },
        window: {
            location: { search: '' },
            dashboardData: {
                repoRoot: '/tmp/repo',
                configName: 'main.yaml',
                configMode: 'codex',
                e2eRunning: false,
                e2eLastRun: null,
                e2eNeedsAttention: false,
                e2eFailedTests: [],
            },
        },
    });

    await ctx.startE2E();

    assert.equal(fetchCalls[0][0], '/control/e2e/start');
    assert.deepEqual(JSON.parse(fetchCalls[0][1].body), {
        repo_root: '/tmp/repo',
        config_name: 'main.yaml',
        mode: 'codex',
    });
});

function makeActionEvent(dataset) {
    const event = {
        prevented: false,
        stopped: false,
        target: {
            closest: (selector) => selector === '[data-action]' ? { dataset } : null,
        },
        preventDefault() { this.prevented = true; },
        stopPropagation() { this.stopped = true; },
    };
    return event;
}

test('latest run results action dispatches the typed open_e2e_run Command (PR #6329 Blocker 2)', () => {
    const ctx = loadE2ERuntime({
        window: {
            location: { search: '' },
            dashboardData: {
                repoRoot: '/tmp/repo',
                configName: 'default.yaml',
                e2eRunning: false,
                e2eLastRun: { id: 42, status: 'passed' },
                e2eNeedsAttention: false,
                e2eFailedTests: [],
            },
        },
    });

    ctx.showLatestE2ERunResults();

    // Single owner: typed Command first, then the stub forwards to
    // showUnifiedRunView.  Both halves are recorded.
    assert.deepEqual(ctx.calls, [
        ['run_e2e_lifecycle_command', {
            kind: 'open_e2e_run',
            label: 'Open E2E Run',
            run_id: 42,
            expand_run_details: false,
        }],
        ['show_unified_run_view', 42],
    ]);
    assert.deepEqual(ctx.toasts, []);
});

test('delegated latest results click dispatches the typed Command with live latest-run state', () => {
    const ctx = loadE2ERuntime({
        window: {
            location: { search: '' },
            dashboardData: {
                repoRoot: '/tmp/repo',
                configName: 'default.yaml',
                e2eRunning: false,
                e2eLastRun: { id: 43, status: 'passed' },
                e2eNeedsAttention: false,
                e2eFailedTests: [],
            },
        },
    });
    const event = makeActionEvent({ action: 'show-latest-e2e-run-results' });

    ctx.handleE2ERuntimeActionClick(event);

    assert.equal(event.prevented, true);
    assert.equal(event.stopped, true);
    assert.deepEqual(ctx.calls, [
        ['run_e2e_lifecycle_command', {
            kind: 'open_e2e_run',
            label: 'Open E2E Run',
            run_id: 43,
            expand_run_details: false,
        }],
        ['show_unified_run_view', 43],
    ]);
});

test('delegated run-history results click dispatches the typed Command with the rendered run id', () => {
    const ctx = loadE2ERuntime();
    const event = makeActionEvent({ action: 'show-e2e-run-results', runId: '88' });

    ctx.handleE2ERuntimeActionClick(event);

    assert.equal(event.prevented, true);
    assert.equal(event.stopped, true);
    assert.deepEqual(ctx.calls, [
        ['run_e2e_lifecycle_command', {
            kind: 'open_e2e_run',
            label: 'Open E2E Run',
            run_id: 88,
            expand_run_details: false,
        }],
        ['show_unified_run_view', 88],
    ]);
});

test('latest run results action shows a toast when no run exists', () => {
    const ctx = loadE2ERuntime();

    ctx.showLatestE2ERunResults();

    assert.deepEqual(ctx.calls, []);
    assert.deepEqual(ctx.toasts, [['No E2E run data available', true]]);
});

test('last run label renders started_at through the shared timestamp formatter', () => {
    const label = {
        dataset: { startedAt: '' },
        textContent: '',
    };
    loadE2ERuntime({
        document: {
            addEventListener: (event, handler) => {
                if (event === 'DOMContentLoaded') handler();
            },
            getElementById: (id) => (id === 'e2eLastRunLabel' ? label : null),
        },
        window: {
            location: { search: '' },
            dashboardData: {
                repoRoot: '/tmp/repo',
                configName: 'default.yaml',
                e2eRunning: false,
                e2eLastRun: { id: 44, status: 'passed', started_at: '2026-05-12T10:00:00Z' },
                e2eNeedsAttention: false,
                e2eFailedTests: [],
            },
        },
    });

    assert.equal(label.textContent, 'Last run: local:2026-05-12');
    assert.equal(label.dataset.startedAt, '2026-05-12T10:00:00Z');
});

test('stopped last-run label survives a later progress-label refresh', async () => {
    const label = {
        dataset: { startedAt: '2026-05-12T10:00:00Z' },
        textContent: '',
    };
    const controls = { innerHTML: '' };
    const ctx = loadE2ERuntime({
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        document: {
            addEventListener: () => {},
            getElementById: (id) => {
                if (id === 'e2eLastRunLabel') return label;
                if (id === 'e2eControls') return controls;
                return null;
            },
        },
        window: {
            location: { search: '' },
            dashboardData: {
                repoRoot: '/tmp/repo',
                configName: 'default.yaml',
                e2eRunning: true,
                e2eLastRun: { id: 44, status: 'running', started_at: '2026-05-12T10:00:00Z' },
                e2eNeedsAttention: false,
                e2eFailedTests: [],
            },
        },
    });

    await ctx.stopE2E();
    ctx.updateE2ELastRunLabel({ started_at: '2026-05-12T10:00:00Z' });

    assert.match(controls.innerHTML, /Stopped/);
    assert.equal(label.textContent, 'Stopped');
});
