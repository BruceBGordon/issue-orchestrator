// Measure the BUNDLED xterm.js, so the Python viewport can be pinned against
// what the session viewer actually draws rather than against a model of it.
//
// The vendored bundle is the DOM build, but Terminal's constructor and write()
// path touch no DOM (only open() does), so it runs under plain node with a
// `self` alias. That makes the real renderer available as a measurement rig.
//
//   node tools/measure_xterm_widths.js widths      # advance per codepoint
//   node tools/measure_xterm_widths.js screens     # cursor + rows per probe
//
// Emit-only: it prints JSON for a human (or a test author) to read. Nothing
// imports it at runtime.

const path = require('node:path');

globalThis.self = globalThis;
const { Terminal } = require(
    path.join(__dirname, '../src/issue_orchestrator/static/vendor/xterm/xterm.js'),
);

function write(term, text) {
    return new Promise((resolve) => term.write(text, resolve));
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
        await write(term, '\x1b[H\x1b[2J');
        await write(term, 'A' + String.fromCodePoint(cp));
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
        await write(term, probe.text);
        const buffer = term.buffer.active;
        const rows = [];
        for (let y = 0; y < probe.rows; y += 1) {
            const line = buffer.getLine(y);
            rows.push(line ? line.translateToString(true) : '');
        }
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

const mode = process.argv[2] || 'screens';
(mode === 'widths' ? measureWidths() : measureScreens()).catch((error) => {
    console.error(error);
    process.exit(1);
});
