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
//   node tools/measure_xterm_widths.js autowrap    # DECAWM on/off + pending wrap
//   node tools/measure_xterm_widths.js pending     # parked-cursor resolution table
//   node tools/measure_xterm_widths.js state       # every reachable state channel
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

// DECAWM (CSI ?7 h/l) decides whether a long line wraps, and the last-column
// "pending wrap" position is a notorious divergence spot between terminals —
// so it is measured, in both states, against the cursor motions that resolve
// or clear a pending wrap, and against wide glyphs at the edge.
function autowrapProbes() {
    const ESC = '\u001b';
    const OFF = `${ESC}[?7l`;
    const ON = `${ESC}[?7h`;
    const fill = 'abcdefghij'; // exactly 10 columns
    const texts = {
        awm_on_overflow: [10, 3, 'abcdefghijkl'],
        awm_off_overflow: [10, 3, `${OFF}abcdefghijkl`],
        awm_on_pending_then_cr: [10, 3, `${fill}\rZ`],
        awm_on_pending_then_bs: [10, 3, `${fill}\bZ`],
        awm_on_pending_then_cuf: [10, 3, `${fill}${ESC}[CZ`],
        awm_on_pending_then_lf: [10, 3, `${fill}\nZ`],
        awm_on_pending_then_cup: [10, 3, `${fill}${ESC}[1;1HZ`],
        awm_on_pending_then_el: [10, 3, `${fill}${ESC}[KZ`],
        awm_off_pending_then_cr: [10, 3, `${OFF}${fill}\rZ`],
        awm_off_pending_then_lf: [10, 3, `${OFF}${fill}\nZ`],
        awm_off_then_on_midrow: [10, 3, `${OFF}abcdefghijkl${ON}MN`],
        awm_on_then_off_midrow: [10, 3, `abcdefgh${OFF}ijkl`],
        awm_on_wide_at_edge: [10, 3, 'abcdefghi\u6771'],
        awm_off_wide_at_edge: [10, 3, `${OFF}abcdefghi\u6771`],
        awm_off_wide_fits_then_narrow: [10, 3, `${OFF}abcdefgh\u6771Z`],
        awm_off_wide_overflow_run: [10, 3, `${OFF}abcdefghi\u6771\u4e9cQ`],
        awm_on_exact_fill_then_more: [10, 3, `${fill}XY`],
        awm_off_exact_fill_then_more: [10, 3, `${OFF}${fill}XY`],
        // The reported reproduction, at the width it was reported with.
        awm_off_footer_does_not_get_its_own_row: [
            120, 3, `${OFF}${'X'.repeat(120)}tab to queue message`,
        ],
        awm_on_footer_wraps_to_row_one: [
            120, 3, `${'X'.repeat(120)}tab to queue message`,
        ],
        awm_off_scroll_region_interaction: [
            10, 4, `${ESC}[2;3r${OFF}r0\r\nr1xxxxxxxxxxxx`,
        ],
        // Overwriting either half of a wide glyph blanks the other half, but
        // only when the write starts a fresh print run.
        wide_overwrite_second_half: [8, 2, `ab\u6771cd${ESC}[1;4HZ`],
        wide_overwrite_first_half: [8, 2, `ab\u6771cd${ESC}[1;3HZ`],
        wide_overwrite_same_run: [8, 2, `ab\u6771\bZ`],
        decstbm_homes_the_cursor: [10, 4, `${ESC}[2;3rHOME`],
    };
    const encoder = new TextEncoder();
    const probes = {};
    for (const [name, [cols, rows, text]] of Object.entries(texts)) {
        probes[name] = { cols, rows, bytes: Array.from(encoder.encode(text)) };
    }
    return probes;
}

async function measureAutowrap() {
    const out = {};
    for (const [name, probe] of Object.entries(autowrapProbes())) {
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

// Every operation that can resolve, preserve or clamp a cursor parked past
// the right edge. Each probe fills the row (parking the cursor), applies one
// operation, then prints Z — so the resulting cursor and rows say exactly what
// that operation did to the parked state. One probe per row of the resolution
// table in ``infra.pending_wrap``.
function pendingWrapProbes() {
    const ESC = '\u001b';
    const FILL = 'abcdefghij'; // exactly 10 columns
    const operations = {
        baseline_no_operation: '',
        carriage_return: '\r',
        line_feed: '\n',
        vertical_tab: '\u000b',
        form_feed: '\u000c',
        index_c1: '\u0084',
        next_line_c1: '\u0085',
        backspace: '\b',
        horizontal_tab: '\t',
        cursor_forward: `${ESC}[C`,
        cursor_back: `${ESC}[D`,
        cursor_up: `${ESC}[A`,
        cursor_down: `${ESC}[B`,
        cursor_position: `${ESC}[1;1H`,
        column_absolute: `${ESC}[5G`,
        row_absolute: `${ESC}[2d`,
        erase_in_line_to_end: `${ESC}[K`,
        erase_in_line_to_start: `${ESC}[1K`,
        erase_in_line_all: `${ESC}[2K`,
        erase_in_display_below: `${ESC}[J`,
        erase_in_display_above: `${ESC}[1J`,
        erase_in_display_all: `${ESC}[2J`,
        scroll_up: `${ESC}[S`,
        scroll_down: `${ESC}[T`,
        set_scroll_region: `${ESC}[2;3r`,
        restore_cursor: `${ESC}7${ESC}[1;1H${ESC}8`,
        reverse_index: `${ESC}M`,
        autowrap_off: `${ESC}[?7l`,
        autowrap_on: `${ESC}[?7h`,
        select_graphic_rendition: `${ESC}[0m`,
        operating_system_command: `${ESC}]0;title\u0007`,
        full_reset: `${ESC}c`,
        soft_reset: `${ESC}[!p`,
    };
    const encoder = new TextEncoder();
    const probes = {};
    for (const [name, operation] of Object.entries(operations)) {
        probes[`pending_${name}`] = {
            cols: 10, rows: 4,
            bytes: Array.from(encoder.encode(FILL + operation + 'Z')),
        };
        // The same operation with autowrap already off, so a resolution that
        // depends on the mode is visible rather than masked by wrapping.
        probes[`pending_nowrap_${name}`] = {
            cols: 10, rows: 4,
            bytes: Array.from(encoder.encode(`${ESC}[?7l` + FILL + operation + 'Z')),
        };
    }
    return probes;
}

async function measurePendingWrap() {
    const out = {};
    for (const [name, probe] of Object.entries(pendingWrapProbes())) {
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

// Every state channel the parser can reach, measured so each one can be
// modelled, ignored or refused on evidence rather than on reading.
function stateProbes() {
    const ESC = '\u001b';
    const SI = '\u000f';
    const SO = '\u000e';
    const texts = {
        decsc_parked_then_restore: [10, 3, `abcdefghij${ESC}7\rX${ESC}8Z`],
        decsc_midrow: [10, 3, `abc${ESC}7\rXY${ESC}8Z`],
        decsc_saves_row: [10, 4, `${ESC}[3;5H${ESC}7${ESC}[1;1H${ESC}8Z`],
        decrc_without_decsc: [10, 4, `${ESC}[2;3H${ESC}8Z`],
        ris_clears_saved_cursor: [10, 4, `${ESC}[3;5H${ESC}7${ESC}c${ESC}8Z`],
        decstr_clears_saved_cursor: [10, 4, `${ESC}[3;5H${ESC}7${ESC}[!p${ESC}8Z`],
        scosc_scorc: [10, 4, `${ESC}[3;5H${ESC}[s${ESC}[1;1H${ESC}[uZ`],
        scorc_without_scosc: [10, 4, `${ESC}[2;3H${ESC}[uZ`],
        reverse_index_midscreen: [10, 5, `r0\r\nr1\r\nr2${ESC}MX`],
        reverse_index_at_top: [10, 4, `r0\r\nr1${ESC}[1;1H${ESC}MX`],
        reverse_index_parked: [10, 4, `abcdefghij${ESC}MZ`],
        escape_index: [10, 4, `ab${ESC}DX`],
        escape_next_line: [10, 4, `ab${ESC}EX`],
        ascii_designation_is_inert: [10, 3, `${ESC}(Bqqq`],
        shift_in_is_inert: [10, 3, `ab${SI}cd`],
        cursor_style_is_inert: [10, 3, `ab${ESC}[4 qcd`],
        sgr_is_inert: [10, 3, `ab${ESC}[0mcd`],
        device_attributes_is_inert: [10, 3, `ab${ESC}[ccd`],
        keypad_mode_is_inert: [10, 3, `ab${ESC}=cd${ESC}>`],
        reset_mode_four_is_inert: [40, 3, `tab to queue message${ESC}[1;2H${ESC}[4lX`],
        // Refused channels, measured so the refusal is evidence-backed.
        insert_mode_shifts_the_row: [40, 3, `tab to queue message${ESC}[1;2H${ESC}[4hX`],
        clear_all_tab_stops: [20, 3, `${ESC}[3gab\tX`],
        set_tab_stop: [20, 3, `abc${ESC}H\r\tX`],
        line_drawing_charset: [10, 3, `${ESC}(0qqq`],
        shift_out_selects_g1: [10, 3, `${ESC})0ab${SO}qq`],
    };
    const encoder = new TextEncoder();
    const probes = {};
    for (const [name, [cols, rows, text]] of Object.entries(texts)) {
        probes[`state_${name}`] = { cols, rows, bytes: Array.from(encoder.encode(text)) };
    }
    return probes;
}

async function measureState() {
    const out = {};
    for (const [name, probe] of Object.entries(stateProbes())) {
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
const MODES = {
    widths: measureWidths,
    screens: measureScreens,
    controls: measureControls,
    autowrap: measureAutowrap,
    pending: measurePendingWrap,
    state: measureState,
};
(MODES[mode] || measureScreens)().catch((error) => {
    console.error(error);
    process.exit(1);
});
