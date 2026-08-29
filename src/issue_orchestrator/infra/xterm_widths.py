"""The bundled xterm.js character-width model, ported exactly.

The kill-evidence discriminator only means anything if the screen it
reconstructs is the screen the session viewer draws. That viewer is the
vendored ``static/vendor/xterm/xterm.js``, so its width model — not Python's
``unicodedata``, and not a plausible-looking approximation of either — is the
contract this module implements.

They disagree, and not subtly. The vendored bundle ships xterm's **UnicodeV6**
provider (the ``@xterm/addon-unicode11`` addon is not vendored), under which an
emoji is **one** cell wide, while ``unicodedata.east_asian_width`` reports
``W`` and every wcwidth-shaped guess says two. A model built on the latter put
👨‍👩‍👧‍👦 at two cells where xterm puts it at four, which is how a footer that
xterm wraps stayed on one row here and produced a verdict from a screen the
real viewer never draws (#7141 round 4).

Ported from the vendored bundle's ``UnicodeV6`` module and verified against
measurements taken from that same bundle running headlessly — see
``tools/measure_xterm_widths.js`` and the fixture it generates, which
``tests/unit/infra/test_xterm_widths.py`` replays codepoint by codepoint.

Two rules make up the model:

``wcwidth``
    C0 controls are zero, ASCII is one, the BMP comes from a table of wide and
    combining ranges, and outside the BMP only planes 2 and 3 are wide — which
    is why emoji, sitting in plane 1, are narrow here.

``cluster_advance``
    xterm's ``charProperties`` plus the subtraction its input handler applies.
    A zero-width codepoint following a non-empty cluster *joins* it: the
    cluster keeps its width and the codepoint advances the cursor by nothing.
    A non-zero-width codepoint never joins, which is exactly why a ZWJ sequence
    of four emoji measures four cells and not one.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# UnicodeV6 tables, transcribed from the vendored bundle
# ---------------------------------------------------------------------------

# Wide BMP spans, as the bundle's Uint8Array fills. Inclusive here; the source
# writes them as JavaScript ``fill(2, start, endExclusive)``.
_WIDE_BMP_RANGES: tuple[tuple[int, int], ...] = (
    (0x1100, 0x115F),
    (0x2329, 0x232A),
    (0x2E80, 0xA4CF),
    (0xAC00, 0xD7A3),
    (0xF900, 0xFAFF),
    (0xFE10, 0xFE19),
    (0xFE30, 0xFE6F),
    (0xFF00, 0xFF60),
    (0xFFE0, 0xFFE6),
)

# The bundle punches this single hole back out of the 0x2E80-0xA4CF fill.
_NARROW_BMP_EXCEPTIONS: frozenset[int] = frozenset({0x303F})

# Zero-width BMP spans (combining marks, joiners, variation selectors), applied
# after the wide fills exactly as the bundle applies them.
_ZERO_WIDTH_BMP_RANGES: tuple[tuple[int, int], ...] = (
    (0x0300, 0x036F), (0x0483, 0x0486), (0x0488, 0x0489), (0x0591, 0x05BD),
    (0x05BF, 0x05BF), (0x05C1, 0x05C2), (0x05C4, 0x05C5), (0x05C7, 0x05C7),
    (0x0600, 0x0603), (0x0610, 0x0615), (0x064B, 0x065E), (0x0670, 0x0670),
    (0x06D6, 0x06E4), (0x06E7, 0x06E8), (0x06EA, 0x06ED), (0x070F, 0x070F),
    (0x0711, 0x0711), (0x0730, 0x074A), (0x07A6, 0x07B0), (0x07EB, 0x07F3),
    (0x0901, 0x0902), (0x093C, 0x093C), (0x0941, 0x0948), (0x094D, 0x094D),
    (0x0951, 0x0954), (0x0962, 0x0963), (0x0981, 0x0981), (0x09BC, 0x09BC),
    (0x09C1, 0x09C4), (0x09CD, 0x09CD), (0x09E2, 0x09E3), (0x0A01, 0x0A02),
    (0x0A3C, 0x0A3C), (0x0A41, 0x0A42), (0x0A47, 0x0A48), (0x0A4B, 0x0A4D),
    (0x0A70, 0x0A71), (0x0A81, 0x0A82), (0x0ABC, 0x0ABC), (0x0AC1, 0x0AC5),
    (0x0AC7, 0x0AC8), (0x0ACD, 0x0ACD), (0x0AE2, 0x0AE3), (0x0B01, 0x0B01),
    (0x0B3C, 0x0B3C), (0x0B3F, 0x0B3F), (0x0B41, 0x0B43), (0x0B4D, 0x0B4D),
    (0x0B56, 0x0B56), (0x0B82, 0x0B82), (0x0BC0, 0x0BC0), (0x0BCD, 0x0BCD),
    (0x0C3E, 0x0C40), (0x0C46, 0x0C48), (0x0C4A, 0x0C4D), (0x0C55, 0x0C56),
    (0x0CBC, 0x0CBC), (0x0CBF, 0x0CBF), (0x0CC6, 0x0CC6), (0x0CCC, 0x0CCD),
    (0x0CE2, 0x0CE3), (0x0D41, 0x0D43), (0x0D4D, 0x0D4D), (0x0DCA, 0x0DCA),
    (0x0DD2, 0x0DD4), (0x0DD6, 0x0DD6), (0x0E31, 0x0E31), (0x0E34, 0x0E3A),
    (0x0E47, 0x0E4E), (0x0EB1, 0x0EB1), (0x0EB4, 0x0EB9), (0x0EBB, 0x0EBC),
    (0x0EC8, 0x0ECD), (0x0F18, 0x0F19), (0x0F35, 0x0F35), (0x0F37, 0x0F37),
    (0x0F39, 0x0F39), (0x0F71, 0x0F7E), (0x0F80, 0x0F84), (0x0F86, 0x0F87),
    (0x0F90, 0x0F97), (0x0F99, 0x0FBC), (0x0FC6, 0x0FC6), (0x102D, 0x1030),
    (0x1032, 0x1032), (0x1036, 0x1037), (0x1039, 0x1039), (0x1058, 0x1059),
    (0x1160, 0x11FF), (0x135F, 0x135F), (0x1712, 0x1714), (0x1732, 0x1734),
    (0x1752, 0x1753), (0x1772, 0x1773), (0x17B4, 0x17B5), (0x17B7, 0x17BD),
    (0x17C6, 0x17C6), (0x17C9, 0x17D3), (0x17DD, 0x17DD), (0x180B, 0x180D),
    (0x18A9, 0x18A9), (0x1920, 0x1922), (0x1927, 0x1928), (0x1932, 0x1932),
    (0x1939, 0x193B), (0x1A17, 0x1A18), (0x1B00, 0x1B03), (0x1B34, 0x1B34),
    (0x1B36, 0x1B3A), (0x1B3C, 0x1B3C), (0x1B42, 0x1B42), (0x1B6B, 0x1B73),
    (0x1DC0, 0x1DCA), (0x1DFE, 0x1DFF), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x2063), (0x206A, 0x206F), (0x20D0, 0x20EF), (0x302A, 0x302F),
    (0x3099, 0x309A), (0xA806, 0xA806), (0xA80B, 0xA80B), (0xA825, 0xA826),
    (0xFB1E, 0xFB1E), (0xFE00, 0xFE0F), (0xFE20, 0xFE23), (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),
)

# Zero-width spans outside the BMP.
_ZERO_WIDTH_NON_BMP_RANGES: tuple[tuple[int, int], ...] = (
    (0x10A01, 0x10A03), (0x10A05, 0x10A06), (0x10A0C, 0x10A0F),
    (0x10A38, 0x10A3A), (0x10A3F, 0x10A3F), (0x1D167, 0x1D169),
    (0x1D173, 0x1D182), (0x1D185, 0x1D18B), (0x1D1AA, 0x1D1AD),
    (0x1D242, 0x1D244), (0xE0001, 0xE0001), (0xE0020, 0xE007F),
    (0xE0100, 0xE01EF),
)

# Outside the BMP only these planes are wide. Plane 1 — where every emoji
# lives — is deliberately absent; that is the whole reason emoji are narrow.
_WIDE_NON_BMP_RANGES: tuple[tuple[int, int], ...] = (
    (0x20000, 0x2FFFD),
    (0x30000, 0x3FFFD),
)


def _fill(table: bytearray, start: int, end: int, value: int) -> None:
    """Inclusive span fill, mirroring the bundle's ``Uint8Array.fill``."""
    table[start : end + 1] = bytes([value]) * (end - start + 1)


def _build_bmp_table() -> bytearray:
    """Build the BMP width table in the bundle's own order.

    Order is load-bearing: the wide fills go down first, the single narrow
    exception is punched back out, and the zero-width spans are applied last —
    exactly as the vendored module does it.
    """
    table = bytearray(b"\x01" * 0x10000)
    _fill(table, 0x00, 0x1F, 0)
    _fill(table, 0x7F, 0x9F, 0)
    for start, end in _WIDE_BMP_RANGES:
        _fill(table, start, end, 2)
    for index in _NARROW_BMP_EXCEPTIONS:
        table[index] = 1
    for start, end in _ZERO_WIDTH_BMP_RANGES:
        _fill(table, start, end, 0)
    return table


_BMP_WIDTHS = _build_bmp_table()


def _in_ranges(codepoint: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    """Membership test that leans on the tables being sorted and disjoint.

    ``test_xterm_widths`` pins that invariant, because the early return below
    silently returns the wrong answer if a future edit lands a range out of
    order.
    """
    for start, end in ranges:
        if codepoint < start:
            return False
        if codepoint <= end:
            return True
    return False


def wcwidth(codepoint: int) -> int:
    """Cells this codepoint occupies, per the vendored xterm's UnicodeV6."""
    if codepoint < 0x20:
        return 0
    if codepoint < 0x7F:
        return 1
    if codepoint < 0x10000:
        return _BMP_WIDTHS[codepoint]
    if _in_ranges(codepoint, _ZERO_WIDTH_NON_BMP_RANGES):
        return 0
    if _in_ranges(codepoint, _WIDE_NON_BMP_RANGES):
        return 2
    return 1


@dataclass(frozen=True)
class ClusterState:
    """What the previous codepoint left behind for the next one to join."""

    width: int
    should_join: bool


#: The state at the start of a line, and after anything that breaks a cluster.
EMPTY_CLUSTER = ClusterState(width=0, should_join=False)


def cluster_advance(codepoint: int, previous: ClusterState) -> tuple[int, ClusterState]:
    """Cells the cursor advances, plus the state to carry to the next codepoint.

    xterm's ``charProperties`` decides whether this codepoint joins the cluster
    already on screen, and its input handler subtracts the joined cluster's
    width so the pair occupies one cell run rather than two. A zero-width
    codepoint joins and advances nothing; anything with width joins nothing and
    advances normally — which is why a four-emoji ZWJ sequence is four cells,
    not one.
    """
    width = wcwidth(codepoint)
    should_join = width == 0 and previous.width != 0
    if should_join:
        if previous.width == 0:
            should_join = False
        elif previous.width > width:
            width = previous.width
    if should_join:
        return width - previous.width, ClusterState(width, should_join=True)
    # A zero-width codepoint with nothing to join takes a cell of its own
    # rather than vanishing — measured, not assumed: a lone combining mark
    # after a cursor move lands in its own cell and advances the cursor.
    return max(width, 1), ClusterState(width=width, should_join=False)
