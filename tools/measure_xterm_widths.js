// Measure the BUNDLED xterm.js, so the Python viewport can be pinned against
// what the session viewer actually draws rather than against a model of it.
//
// The vendored bundle is the DOM build, but Terminal's constructor and write()
// path touch no DOM (only open() does), so it runs under plain node with a
// `self` alias. That makes the real renderer available as a measurement rig.
//
//   node tools/measure_xterm_widths.js widths      # advance per codepoint
//   node tools/measure_xterm_widths.js screens     # cursor + rows per probe
//   node tools/measure_xterm_widths.js controls    # C1 (U+0080-U+009F) behaviour
//
// Emit-only: it prints JSON for a human (or a test author) to read. Nothing
// imports it at runtime.

const path = require('node:path');

globalThis.self = globalThis;
const { Terminal } = require(
    path.join(__dirname, '../src/issue_orchestrator/static/vendor/xterm/xterm.js'),
);

function write(term, data) {
    return new Promise((resolve) => term.write(data, resolve));
}

function readViewport(buffer, rows) {
    const out = [];
    for (let y = 0; y < rows; y += 1) {
        const line = buffer.getLine(buffer.baseY + y);
        out.push(line ? line.translateToString(true) : '');
    }
    return out;
}

// Codepoints whose advance we care about: ASCII, CJK, Hangul, combining marks,
// zero-width joiners/selectors, and emoji planes. C0/C1 are excluded because
// the terminal interprets them as controls rather than printing them.
function sampleCodepoints() {
    const points = [];
    const ranges = [
        [0x20, 0x7e], [0xa0, 0x2ff], [0x300, 0x36f], [0x1100, 0x1200],
        [0x2e80, 0x2f00], [0x3000, 0x3100], [0x4e00, 0x4e40], [0xa4c0, 0xa4d0],
        [0xac00, 0xac40], [0xd7a0, 0xd7b0], [0xf900, 0xf910], [0xfe00, 0xfe20],
        [0xfe10, 0xfe70], [0xff00, 0xff70], [0xffe0, 0xffe8], [0x200b, 0x2010],
        [0x1f300, 0x1f320], [0x1f460, 0x1f480], [0x20000, 0x20010],
        [0x30000, 0x30010], [0xe0100, 0xe0110], [0x100000, 0x100010],
    ];
    for (const [start, end] of ranges) {
        for (let cp = start; cp <= end; cp += 1) points.push(cp);
    }
    return points;
}

// Advance is measured IN CONTEXT (after a plain "A"), because that is what
// decides the screen: a zero-width codepoint joins the cluster before it, and
// measuring it in isolation would report the terminal's no-preceding-cell
// fallback instead of the behaviour the viewport has to reproduce.
async function measureWidths() {
    const term = new Terminal({ cols: 20, rows: 4, allowProposedApi: true });
    const out = {};
    for (const cp of sampleCodepoints()) {
        await write(term, new TextEncoder().encode('\x1b[H\x1b[2J'));
        await write(term, new TextEncoder().encode('A' + String.fromCodePoint(cp)));
        out[cp] = term.buffer.active.cursorX - 1;
    }
    console.log(JSON.stringify(out));
}

const SCREENS = {
    family_zwj: { cols: 30, rows: 6, text: 'x'.repeat(23) + '\u{1F468}‍\u{1F469}‍\u{1F467}‍\u{1F466}' },
    family_zwj_alone: { cols: 30, rows: 6, text: '\u{1F468}‍\u{1F469}‍\u{1F467}‍\u{1F466}' },
    couple_zwj: { cols: 30, rows: 6, text: '\u{1F468}‍\u{1F469}' },
    emoji_vs16: { cols: 30, rows: 6, text: '⚠️!' },
    plain_emoji: { cols: 30, rows: 6, text: '\u{1F600}|' },
    cjk: { cols: 30, rows: 6, text: '東亜|' },
    combining: { cols: 30, rows: 6, text: 'éx' },
    wrap_30: { cols: 30, rows: 6, text: 'ab \u{1F468}‍\u{1F469}‍\u{1F467}‍\u{1F466}tab to queue message' },
    wrap_30_split: { cols: 30, rows: 6, text: 'x'.repeat(8) + '\u{1F468}‍\u{1F469}‍\u{1F467}‍\u{1F466}' + 'tab to queue message' },
    wide_wrap: { cols: 30, rows: 6, text: '界'.repeat(16) },
    narrow_wide: { cols: 1, rows: 3, text: '東亜' },
};

async function measureScreens() {
    const out = {};
    for (const [name, probe] of Object.entries(SCREENS)) {
        const term = new Terminal({ cols: probe.cols, rows: probe.rows, allowProposedApi: true });
        await write(term, new TextEncoder().encode(probe.text));
        const buffer = term.buffer.active;
        // getLine() indexes the whole buffer, scrollback included, so a probe
        // that scrolls must be read from baseY or it reports lines that have
        // already left the screen.
        const rows = readViewport(buffer, probe.rows);
        out[name] = {
            cols: probe.cols,
            text: probe.text,
            cursorX: buffer.cursorX,
            cursorY: buffer.cursorY,
            rows,
        };
    }
    console.log(JSON.stringify(out, null, 2));
}

// C1 controls (U+0080-U+009F) arrive in a recording as two UTF-8 bytes. Some
// are inert, two move the cursor, and six introduce a sequence — so they are
// measured as raw BYTES in three contexts: a plain row, mid-screen (where a
// cursor move is visible), and on the last row (where it scrolls).
function c1Probes() {
    const encoder = new TextEncoder();
    const bytes = (text) => Array.from(encoder.encode(text));
    const probes = {};
    for (let cp = 0x80; cp <= 0x9f; cp += 1) {
        const control = bytes(String.fromCodePoint(cp));
        const tag = cp.toString(16).toUpperCase();
        probes[`c1_${tag}_inline`] = {
            cols: 20, rows: 4, bytes: [...bytes('AB'), ...control, ...bytes('CD')],
        };
        probes[`c1_${tag}_midscreen`] = {
            cols: 10, rows: 5,
            bytes: [...bytes('r0\r\nr1\r\nr2'), ...control, ...bytes('X')],
        };
        probes[`c1_${tag}_lastrow`] = {
            cols: 10, rows: 3,
            bytes: [...bytes('a\r\nb\r\nc'), ...control, ...bytes('X')],
        };
    }
    // The reported reproduction: a NEL inside the marker span.
    probes.c1_nel_splits_the_marker = {
        cols: 40, rows: 4,
        bytes: [...bytes('tab to '), ...bytes('\u0085'), ...bytes('queue message')],
    };
    // 0x9B really does introduce a control sequence.
    probes.c1_csi_erases_the_display = {
        cols: 20, rows: 3,
        bytes: [...bytes('HELLO'), ...bytes('\u009b'), ...bytes('2J')],
    };
    return probes;
}

async function measureControls() {
    const out = {};
    for (const [name, probe] of Object.entries(c1Probes())) {
        const term = new Terminal({ cols: probe.cols, rows: probe.rows, allowProposedApi: true });
        await write(term, new Uint8Array(probe.bytes));
        const buffer = term.buffer.active;
        out[name] = {
            cols: probe.cols,
            bytes: probe.bytes,
            cursorX: buffer.cursorX,
            cursorY: buffer.cursorY,
            rows: readViewport(buffer, probe.rows),
        };
    }
    console.log(JSON.stringify(out, null, 2));
}

const mode = process.argv[2] || 'screens';
const MODES = { widths: measureWidths, screens: measureScreens, controls: measureControls };
(MODES[mode] || measureScreens)().catch((error) => {
    console.error(error);
    process.exit(1);
});
