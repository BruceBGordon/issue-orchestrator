// Behavior tests for session-replay geometry validation (#7141 round 4).
//
// The backend has ONE owner deciding what a trustworthy `resize` row carries
// (infra/terminal_recording.screen_dimension), and the Python viewport and the
// viewer's geometry lookup both ask it. The browser viewer is a third call
// site of that policy in a language that cannot import the owner, so it
// restates it — and these tests pin the restatement against the same
// semantics the Python owner test asserts:
//
//   tests/unit/test_terminal_recording.py
//       ::test_first_terminal_geometry_rejects_untrustworthy_dimensions
//   tests/unit/infra/test_terminal_viewport.py::TestMalformedResizeRows
//
// The bounds themselves (500 rows / 1000 cols) are pinned across the language
// boundary by tests/unit/test_terminal_recording.py, which reads this file and
// fails if either side moves.

const test = require('node:test');
// Non-strict assert: vm.runInContext objects have cross-realm prototypes.
const assert = require('node:assert');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

function loadSessionReplay(overrides = {}) {
    const context = {
        document: {
            getElementById: () => null,
            querySelector: () => null,
            addEventListener: () => {},
        },
        window: { addEventListener: () => {} },
        ...overrides,
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(
            path.join(
                __dirname,
                '../../src/issue_orchestrator/static/js/dashboard/session_replay.js',
            ),
            'utf8',
        ),
        context,
    );
    return context;
}

const UNTRUSTWORTHY = [
    ['boolean true', { rows: true, cols: 40 }],
    ['boolean false', { rows: 40, cols: false }],
    ['zero rows', { rows: 0, cols: 40 }],
    ['zero cols', { rows: 40, cols: 0 }],
    ['negative rows', { rows: -5, cols: 40 }],
    ['negative cols', { rows: 40, cols: -1 }],
    ['rows past the bound', { rows: 501, cols: 40 }],
    ['cols past the bound', { rows: 40, cols: 1001 }],
    ['absurd cols', { rows: 40, cols: 10000000 }],
    ['numeric string', { rows: '40', cols: '120' }],
    ['float rows', { rows: 40.5, cols: 120 }],
    ['NaN', { rows: NaN, cols: 120 }],
    ['null rows', { rows: null, cols: 120 }],
    ['missing cols', { rows: 40 }],
];

test('untrustworthy geometry is rejected, never coerced', () => {
    const ctx = loadSessionReplay();
    for (const [label, candidate] of UNTRUSTWORTHY) {
        assert.strictEqual(
            ctx.normalizeSessionReplayGeometry(candidate),
            null,
            `expected ${label} to be rejected`,
        );
    }
});

test('geometry at the bounds is accepted', () => {
    const ctx = loadSessionReplay();
    assert.deepEqual(ctx.normalizeSessionReplayGeometry({ rows: 1, cols: 1 }), {
        rows: 1,
        cols: 1,
    });
    assert.deepEqual(
        ctx.normalizeSessionReplayGeometry({ rows: 500, cols: 1000 }),
        { rows: 500, cols: 1000 },
    );
    assert.deepEqual(ctx.normalizeSessionReplayGeometry({ rows: 40, cols: 120 }), {
        rows: 40,
        cols: 120,
    });
});

test('the initial-geometry fallback skips untrustworthy events', () => {
    const ctx = loadSessionReplay();
    // Backend geometry absent, so the resolver falls back to the raw events.
    const events = [
        { event_type: 'resize', rows: true, cols: 40 },
        { event_type: 'resize', rows: 0, cols: 40 },
        { event_type: 'resize', rows: 501, cols: 40 },
        { event_type: 'resize', rows: 24, cols: 80 },
    ];

    const geometry = ctx.resolveSessionReplayInitialGeometry(
        { initial_geometry: null },
        events,
    );

    assert.deepEqual(geometry, { rows: 24, cols: 80 });
});

test('the fallback returns null when every event is untrustworthy', () => {
    const ctx = loadSessionReplay();

    const geometry = ctx.resolveSessionReplayInitialGeometry({ initial_geometry: null }, [
        { event_type: 'resize', rows: true, cols: true },
        { event_type: 'resize', rows: -1, cols: -1 },
    ]);

    assert.strictEqual(geometry, null);
});

test('backend geometry is validated too, not trusted blindly', () => {
    const ctx = loadSessionReplay();

    const geometry = ctx.resolveSessionReplayInitialGeometry(
        { initial_geometry: { rows: true, cols: 40 } },
        [{ event_type: 'resize', rows: 24, cols: 80 }],
    );

    assert.deepEqual(geometry, { rows: 24, cols: 80 });
});

// `sessionReplayState` is declared with `let`, so it lives in the script scope
// rather than on the context object; the assignment has to run *inside* the
// same context to reach that binding.
function terminalHarness(ctx) {
    const resizes = [];
    const writes = [];
    ctx.__resizes = resizes;
    ctx.__writes = writes;
    vm.runInContext(
        `sessionReplayState = {
            terminal: {
                resize: (cols, rows) => __resizes.push([cols, rows]),
                write: (data) => __writes.push(data),
            },
            initialGeometry: { rows: 40, cols: 120 },
        };`,
        ctx,
    );
    const readGeometry = () =>
        vm.runInContext('JSON.stringify(sessionReplayState.initialGeometry)', ctx);
    return { resizes, writes, readGeometry };
}

test('playback never passes an untrustworthy resize to the terminal', () => {
    const ctx = loadSessionReplay();
    const { resizes, readGeometry } = terminalHarness(ctx);

    for (const [, candidate] of UNTRUSTWORTHY) {
        ctx.applyTerminalRecordingEvent({ event_type: 'resize', ...candidate });
    }

    assert.deepEqual(resizes, []);
    // The last good geometry stands; a bad row is absent, not a reshape.
    assert.strictEqual(readGeometry(), JSON.stringify({ rows: 40, cols: 120 }));
});

test('playback applies a trustworthy resize', () => {
    const ctx = loadSessionReplay();
    const { resizes, readGeometry } = terminalHarness(ctx);

    ctx.applyTerminalRecordingEvent({ event_type: 'resize', rows: 30, cols: 100 });

    assert.deepEqual(resizes, [[100, 30]]);
    assert.strictEqual(readGeometry(), JSON.stringify({ rows: 30, cols: 100 }));
});

test('a skipped resize does not swallow the output events around it', () => {
    const ctx = loadSessionReplay();
    const { resizes, writes } = terminalHarness(ctx);
    ctx.atob = (value) => value;

    ctx.applyTerminalRecordingEvent({ event_type: 'output', data_b64: 'first' });
    ctx.applyTerminalRecordingEvent({ event_type: 'resize', rows: 0, cols: 0 });
    ctx.applyTerminalRecordingEvent({ event_type: 'output', data_b64: 'second' });

    assert.strictEqual(resizes.length, 0);
    assert.strictEqual(writes.length, 2);
});
