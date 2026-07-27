from __future__ import annotations

from pathlib import Path
import re

from issue_orchestrator.view_models.dashboard_assets import DASHBOARD_CSS_CHUNKS
from issue_orchestrator.view_models.dashboard_assets import DASHBOARD_JS_CHUNKS


ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_JS = ROOT / "src" / "issue_orchestrator" / "static" / "js" / "dashboard.js"
DASHBOARD_JS_DIR = ROOT / "src" / "issue_orchestrator" / "static" / "js" / "dashboard"
DASHBOARD_CSS_DIR = ROOT / "src" / "issue_orchestrator" / "static" / "css" / "dashboard"
DASHBOARD_TEMPLATE = ROOT / "src" / "issue_orchestrator" / "templates" / "dashboard.html"
ISSUE_ROW_TEMPLATE = ROOT / "src" / "issue_orchestrator" / "templates" / "issue_row.html"
DASHBOARD_VIEW_MODEL = ROOT / "src" / "issue_orchestrator" / "view_models" / "dashboard.py"
DASHBOARD_E2E_VIEW_MODEL = ROOT / "src" / "issue_orchestrator" / "view_models" / "dashboard_e2e.py"
UI_ACTION_CONTRACT_JS = ROOT / "src" / "issue_orchestrator" / "static" / "js" / "ui_action_contract.js"
BROWSER_AUTH_JS = ROOT / "src" / "issue_orchestrator" / "static" / "js" / "browser_auth.js"
THEME_RESOLUTION_JS = ROOT / "src" / "issue_orchestrator" / "static" / "js" / "theme_resolution.js"
DASHBOARD_BOOT_JS = ROOT / "src" / "issue_orchestrator" / "static" / "js" / "dashboard_boot.js"


def _read(path: Path) -> str:
    if path == DASHBOARD_JS:
        return _read_dashboard_js_bundle()
    return path.read_text(encoding="utf-8")


def _read_dashboard_js_bundle() -> str:
    return "\n".join(
        (DASHBOARD_JS_DIR / chunk).read_text(encoding="utf-8")
        for chunk in DASHBOARD_JS_CHUNKS
    )


def _read_dashboard_css_bundle() -> str:
    return "\n".join(
        _read(DASHBOARD_CSS_DIR / chunk)
        for chunk in DASHBOARD_CSS_CHUNKS
    )


def _css_rule_bodies(source: str, selector: str) -> list[str]:
    pattern = re.compile(
        rf"(?m)^{re.escape(selector)}\s*\{{(?P<body>.*?)^\}}",
        re.DOTALL,
    )
    return [match.group("body") for match in pattern.finditer(source)]


def _last_css_rule_body(source: str, selector: str) -> str:
    bodies = _css_rule_bodies(source, selector)
    assert bodies, f"CSS rule for {selector!r} not found"
    return bodies[-1]


def _html_element(html: str, tag: str, element_id: str) -> tuple[dict[str, str], str]:
    """Return ``(attributes, inner_text)`` of the first ``<tag>`` element
    carrying ``id="element_id"``.

    Attribute order is irrelevant — the id may appear anywhere in the
    opening tag — so callers can assert on class / id / tabindex / text
    intent without pinning the exact serialized attribute string (which a
    formatter reorder or an added attribute would otherwise break).
    """
    match = re.search(
        rf'<{tag}\b([^>]*\bid="{re.escape(element_id)}"[^>]*)>(.*?)</{tag}>',
        html,
        re.DOTALL,
    )
    assert match, f"no <{tag} id={element_id!r}>...</{tag}> element found"
    attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', match.group(1)))
    return attrs, match.group(2).strip()


def _function_body(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.find(marker)
    assert start != -1, f"Function '{name}' not found"
    brace = source.find("{", start)
    assert brace != -1, f"Function '{name}' body start not found"
    depth = 0
    i = brace
    while i < len(source):
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[brace : i + 1]
        i += 1
    raise AssertionError(f"Function '{name}' body end not found")


def test_dashboard_css_uses_direct_split_stylesheets_without_compat_entrypoint() -> None:
    compat_entrypoint = DASHBOARD_CSS_DIR.parent / "dashboard.css"

    assert not compat_entrypoint.exists()
    assert DASHBOARD_CSS_CHUNKS == (
        "base.css",
        "cards.css",
        "issue_detail.css",
        "overlays.css",
        # ``validation_viewer.css`` carries the ``cvv-*`` rules for the
        # canonical validation viewer (issue #6310 follow-up).  Loaded
        # after ``overlays.css`` so any class-name overlaps with the
        # legacy ``diag-*`` rules resolve in the viewer's favour.
        "validation_viewer.css",
        "e2e_run_detail.css",
    )
    assert ".issue-detail-drawer" in _read_dashboard_css_bundle()


def test_toast_severity_variants_have_css_rules() -> None:
    css = _read_dashboard_css_bundle()

    for severity in ("info", "success", "warning", "error"):
        assert re.search(rf"#toast\.{severity}\s*\{{[^}}]*border-color:", css, re.DOTALL)
        assert re.search(rf"#toast\.{severity}\s*\{{[^}}]*background:", css, re.DOTALL)


def test_toast_is_bottom_centered_not_corner_or_cursor_anchored() -> None:
    """Toast placement regression guard for issue #5855.

    The toast used to live in the bottom-right corner, so feedback landed far
    from where the user was looking and read as "nothing happened". A briefly
    considered fix was to anchor the toast to the cursor/click position. We
    revisited that and settled on a fixed **bottom-center** placement instead
    (paired with sticky errors/warnings). Every other toast trait is guarded
    elsewhere, but the placement itself — the actual subject of the issue — was
    not, so a revert to a corner anchor or a cursor-anchored rewrite would slip
    through. Lock the resolution in here.
    """
    css = _read_dashboard_css_bundle()

    base = _last_css_rule_body(css, "#toast")
    # Horizontally centered against the viewport, bottom-anchored.
    assert "left: 50%;" in base
    assert "transform: translate(-50%" in base
    assert "bottom:" in base
    # No corner/edge anchor: the old bottom-right placement pinned `right:`,
    # and a top anchor would move it away from the settled bottom placement.
    assert "right:" not in base
    assert "top:" not in base
    # The visible state must keep the horizontal centering (only the vertical
    # slide-in offset changes), so the toast never drifts off-center.
    visible = _last_css_rule_body(css, "#toast.visible")
    assert "transform: translate(-50%, 0)" in visible

    # Placement is fixed, not derived from pointer/click coordinates. The
    # revisited cursor-anchored approach was rejected; showToast must never
    # read the event's mouse position to place the toast.
    js = _read(DASHBOARD_JS)
    show_toast = _function_body(js, "showToast")
    for cursor_anchor in ("clientX", "clientY", "pageX", "pageY", "getBoundingClientRect"):
        assert cursor_anchor not in show_toast, (
            "showToast must not cursor-anchor the toast (issue #5855 settled on "
            f"fixed bottom-center placement); found {cursor_anchor!r}"
        )


def test_e2e_run_history_css_contract_prevents_clipped_rows() -> None:
    """Cheap guardrail for the Run History layout contract.

    The true "is it clipped?" check needs a browser layout engine, so
    Playwright owns that proof. This unit guardrail catches the common
    regression cheaply: resurrecting a nested 65vh scroller or removing
    the row/focus sizing rules from the E2E run-history stylesheet.
    """
    css = _read_dashboard_css_bundle()
    cards_css = _read(DASHBOARD_CSS_DIR / "cards.css")

    assert ".e2e-runs-list" not in cards_css
    assert ".e2e-run-item" not in cards_css

    list_rule = _last_css_rule_body(css, ".e2e-runs-list")
    assert "max-height: none;" in list_rule
    assert "overflow: visible;" in list_rule

    row_rule = _last_css_rule_body(css, ".e2e-run-row")
    assert "overflow: visible;" in row_rule
    assert "scroll-margin-top: 12px;" in row_rule

    summary_rule = _last_css_rule_body(css, ".e2e-run-row-summary")
    assert "min-height: 44px;" in summary_rule
    assert "line-height: 1.35;" in summary_rule

    assert re.search(
        r"\.e2e-run-row-summary:focus-visible\s*\{[^}]*"
        r"outline:\s*2px solid var\(--accent\);",
        css,
        re.DOTALL,
    )


def test_show_toast_uses_module_timer_and_click_dismiss() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "showToast")

    assert "let toastTimer = null;" in js
    assert "window.dashboardToastTimer" not in js
    assert "toast.addEventListener('click'" in body
    assert "clearTimeout(toastTimer)" in body
    assert "hideToast(toast)" in body


def test_show_toast_errors_and_warnings_are_sticky() -> None:
    """Errors/warnings must require explicit dismiss so the user can read or
    copy diagnostic detail (e.g. GitHub API reason text). Auto-dismissing a
    toast that carries useful info is bad UX.
    """
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "showToast")

    assert "toastType === 'error' || toastType === 'warning'" in body
    assert "toast-close" in body
    # Auto-dismiss timer must be gated on non-sticky severity.
    assert "if (!sticky)" in body
    # The non-sticky branch still uses the module timer.
    assert "setTimeout(() => hideToast(toast), 3000)" in body
    # The toast-level click handler must early-return when sticky, so
    # clicking the message body cannot dismiss a diagnostic toast.
    assert "if (toast.classList.contains('sticky')) return;" in body
    # CSS must style the close button affordance.
    css = _read_dashboard_css_bundle()
    assert "#toast .toast-close" in css
    assert "#toast.sticky" in css


def test_show_toast_normalizes_supported_severities() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "normalizeToastType")

    assert "if (type === true) return 'error';" in body
    assert "if (type === false || type === null || type === undefined) return 'info';" in body
    assert "['error', 'warning', 'success', 'info'].includes(type)" in body


def test_start_e2e_errors_use_explicit_error_toasts() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "startE2E")

    assert "showToast('Failed to stop running E2E', 'error')" in body
    assert "showToast(data.detail || data.error || 'Failed to start E2E', 'error')" in body
    assert "showToast('Failed to start E2E: ' + err.message, 'error')" in body
    assert "showToast(data.detail || data.error || 'Failed to start E2E', true)" not in body
    assert "showToast('Failed to start E2E: ' + err.message, true)" not in body


def test_unblock_paths_use_unblock_api() -> None:
    contract_js = _read(UI_ACTION_CONTRACT_JS)
    assert "/api/unblock-retry" in contract_js
    assert "buildUnblockRequest" in contract_js
    assert "issues" in contract_js
    assert "/api/bulk-retry" in contract_js
    assert "buildBulkRetryRequest" in contract_js
    assert "/api/bulk-cancel-queued" in contract_js
    assert "buildBulkCancelQueuedRequest" in contract_js


def test_blocked_bulk_buttons_default_disabled_in_template() -> None:
    html = _read(DASHBOARD_TEMPLATE)
    assert re.search(r'onclick="bulkUnblock\(\)"\s+disabled', html)
    assert re.search(r'onclick="bulkResetRetry\(\)"\s+disabled', html)
    assert re.search(r'onclick="bulkResetRetryFromScratch\(\)"\s+disabled', html)
    assert re.search(r'onclick="bulkMarkViewed\(\)"\s+disabled', html)
    assert re.search(r'onclick="bulkClearViewed\(\)"\s+disabled', html)


def test_validation_warning_banner_is_gated_and_accessible() -> None:
    # Issue #4109: a persistent warning must render when validation is not
    # configured. It is gated on ``validation_configured`` (so it disappears
    # when validation is set), carries an alert role for assistive tech, and
    # pairs an icon with text so colour is never the only status signal.
    html = _read(DASHBOARD_TEMPLATE)
    assert "{% if not validation_configured %}" in html
    match = re.search(r"<div[^>]*\bclass=\"validation-warning-banner\"[^>]*>", html)
    assert match is not None
    banner_tag = match.group(0)
    assert 'role="alert"' in banner_tag
    assert "validation-warning-icon" in html
    assert "No validation configured" in html


def test_validation_warning_banner_has_css_in_both_themes() -> None:
    # Colour cannot be the only signal and the banner must be styled (not an
    # unstyled block). The amber caution palette resolves in light and dark
    # via themed custom properties, so a single rule set covers both themes.
    css = _read_dashboard_css_bundle()
    assert ".validation-warning-banner" in css
    assert ".validation-warning-icon" in css


def test_validation_warning_settings_link_routes_through_embedded_nav() -> None:
    # Issue #4109 regression: the warning banner's Settings link must forward
    # the Control Center embedded context (?embedded=1 & ?theme=) exactly like
    # the settings-menu button's goToSettings(). It stays a semantic <a> with a
    # base href="/settings" (works without JS / standalone), and the shared
    # embeddedNav owner upgrades the href on load — the URL-preservation rule
    # is never duplicated here.
    html = _read(DASHBOARD_TEMPLATE)
    # Every dashboard link to /settings must be routed through the owner
    # (marked with data-embedded-settings-link); no ad-hoc raw link may sneak
    # back in and drop the embedded context.
    settings_anchors = re.findall(r'<a\b[^>]*\bhref="/settings"[^>]*>', html)
    assert settings_anchors, "expected a /settings link in the warning banner"
    for anchor in settings_anchors:
        assert "data-embedded-settings-link" in anchor, (
            f"raw /settings link bypasses embeddedNav owner: {anchor}"
        )

    # The dashboard boot hands the document to the owner so marked links get
    # the embedded context applied, reusing the same rule as goToSettings().
    js = _read(DASHBOARD_JS)
    assert (
        "embeddedNav.applySettingsLinks(document, window.location.search)" in js
    )

    # The owner actually exposes the link-upgrade behavior (single source of
    # the propagation rule, shared with buildHref).
    nav = _read(EMBEDDED_NAV_JS)
    assert "function applySettingsLinks(" in nav
    assert "buildHref('/settings', search)" in nav


def test_issue_detail_status_is_live_region() -> None:
    html = _read(DASHBOARD_TEMPLATE)
    match = re.search(r"<div[^>]*\bid=\"issueDetailStatus\"[^>]*>", html)
    assert match is not None
    status_tag = match.group(0)
    assert 'role="status"' in status_tag
    assert 'aria-live="polite"' in status_tag
    assert 'aria-atomic="true"' in status_tag


def test_in_round_progress_step_has_text_affordance_not_colour_only() -> None:
    # Issue #6428: the live in-round progress row must carry a textual badge
    # with an accessible status role — status must not be signalled by colour
    # alone. The renderer is the io.agent-context lifecycle plugin.
    agent_context_js = (DASHBOARD_JS_DIR / "plugins" / "agent_context.js").read_text(
        encoding="utf-8"
    )
    assert "in_round_progress" in agent_context_js
    assert "journey-progress-badge" in agent_context_js
    assert 'role="status"' in agent_context_js
    assert "In progress" in agent_context_js


def test_in_round_progress_css_neutralises_done_colour_and_respects_reduced_motion() -> None:
    css = _read_dashboard_css_bundle()
    assert ".journey-step-in-progress" in css
    assert ".journey-progress-badge" in css
    # The pulsing dot must be disabled when the user prefers reduced motion.
    assert "@keyframes journey-progress-pulse" in css
    assert "prefers-reduced-motion: reduce" in css


def test_completed_and_awaiting_merge_bulk_buttons_default_disabled_in_template() -> None:
    html = _read(DASHBOARD_TEMPLATE)
    assert re.search(r'onclick="bulkRetryAwaitingMerge\(\)"\s+disabled', html)
    assert re.search(r'onclick="bulkRetryCompleted\(\)"\s+disabled', html)
    # awaiting-merge column also has reset & retry buttons
    # Find the awaiting-merge section and verify buttons exist there
    am_start = html.find("column.id == 'awaiting-merge'")
    am_end = html.find("column.id == 'completed'", am_start)
    am_section = html[am_start:am_end]
    assert "bulkResetRetry()" in am_section
    assert "bulkResetRetryFromScratch()" in am_section


def test_issue_detail_uses_timeline_label_not_journey() -> None:
    html = _read(DASHBOARD_TEMPLATE)

    # The visually-hidden "Timeline" heading names the drawer's timeline
    # region for assistive tech. Prove class + id + text intent while
    # tolerating attribute reordering (the old exact-string assertion
    # broke on any attribute shuffle even though the a11y intent held).
    heading_attrs, heading_text = _html_element(html, "h3", "issueDetailTimelineHeading")
    assert heading_text == "Timeline"
    assert set(heading_attrs.get("class", "").split()) >= {
        "issue-detail-section-title",
        "visually-hidden",
    }

    # The timeline region is labelled by that heading and is keyboard
    # focusable, so a keyboard user lands on it — prove the aria wiring
    # and tabindex hold together.
    journey_attrs, _ = _html_element(html, "div", "issueDetailJourney")
    assert journey_attrs.get("aria-labelledby") == "issueDetailTimelineHeading"
    assert journey_attrs.get("tabindex") == "-1"

    # The legacy "Journey" heading and raw-events / focus / GitHub chrome
    # must stay gone regardless of how their attributes were written.
    assert ">Journey</h3>" not in html
    assert 'id="issueDetailRawEvents"' not in html
    assert 'id="issueDetailFocusBtn"' not in html
    assert 'id="issueDetailGitHubBtn"' not in html


def test_issue_detail_template_includes_retry_publish_button() -> None:
    html = _read(DASHBOARD_TEMPLATE)
    assert 'id="issueDetailRetryPublishBtn"' in html


def test_issue_detail_timeline_filters_are_grouped_button_controls() -> None:
    js = _read(DASHBOARD_JS)
    css = _read_dashboard_css_bundle()
    body = _function_body(js, "_renderJourneyRuns")
    assert 'role="radiogroup"' not in body
    assert 'role="radio"' not in body
    assert "aria-checked=" not in body
    assert "aria-pressed=" in body
    assert "All runs" in body
    assert "Raw events" in body
    assert ".journey-filter-group" in css


def test_issue_detail_template_drops_top_of_drawer_validation_section() -> None:
    # The top-of-drawer flat validation list (which dumped 600+ passed
    # cases as a non-scrollable wall of rows) was replaced by per-cycle
    # validation badges in the journey timeline. Make sure the old
    # markup stays gone — re-adding it would resurrect the bug we fixed.
    html = _read(DASHBOARD_TEMPLATE)
    assert 'id="issueDetailValidation"' not in html
    assert 'id="issueDetailValidationBtn"' not in html
    assert 'id="issueDetailValidationStructured"' not in html
    assert 'id="issueDetailValidationTitle"' not in html
    assert 'id="issueDetailValidationTests"' not in html
    assert 'id="issueDetailValidationReason"' not in html


def test_dashboard_loads_ui_state_helpers_before_dashboard_js() -> None:
    html = _read(DASHBOARD_TEMPLATE)
    idx_issue_row = html.find('/static/js/issue_row_state.js')
    idx_expanded = html.find('/static/js/expanded_column_state.js')
    idx_compact = html.find('/static/js/compact_card_state.js')
    idx_action_contract = html.find('/static/js/ui_action_contract.js')
    idx_xterm_css = html.find('/static/vendor/xterm/xterm.css')
    idx_xterm_js = html.find('/static/vendor/xterm/xterm.js')
    idx_xterm_fit = html.find('/static/vendor/xterm/addon-fit.js')
    idx_chunk_loop = html.find("{% for chunk in dashboard_js_chunks %}")
    idx_dashboard = html.find('/static/js/dashboard.js')
    assert idx_issue_row != -1
    assert idx_expanded != -1
    assert idx_compact != -1
    assert idx_action_contract != -1
    assert idx_xterm_css != -1
    assert idx_xterm_js != -1
    assert idx_xterm_fit != -1
    assert idx_chunk_loop != -1
    assert idx_dashboard != -1
    assert idx_issue_row < idx_dashboard
    assert idx_expanded < idx_dashboard
    assert idx_compact < idx_dashboard
    assert idx_action_contract < idx_dashboard
    assert idx_xterm_css < idx_dashboard
    assert idx_xterm_js < idx_dashboard
    assert idx_xterm_fit < idx_dashboard
    assert idx_xterm_fit < idx_chunk_loop < idx_dashboard


def test_dashboard_refreshes_on_history_reconciled_sse() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "wireEventListeners")

    assert "'history.reconciled'" in body
    assert "refreshViewModel({ reloadOnListChange: true })" in body


def test_dashboard_refreshes_list_on_issue_unblocked_sse() -> None:
    """Reset+retry emits issue.unblocked; the dashboard must reload rows."""
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "wireEventListeners")

    assert "'issue.unblocked'" in body
    issue_unblocked_index = body.index("'issue.unblocked'")
    refresh_index = body.index("refreshViewModel({ reloadOnListChange: true })")
    assert issue_unblocked_index < refresh_index


def test_card_focus_renders_combined_issue_label_in_template() -> None:
    """Cards must show the formatted label (M9-009 · #274) instead of bare #number."""
    html = _read(DASHBOARD_TEMPLATE)
    assert "card.issue_label or '#' ~ card.issue_number" in html
    # Bare "#{{ card.issue_number }}" used to lead the focus button;
    # ensure the regression doesn't sneak back into the focus title/body.
    assert "#{{ card.issue_number }} {{ card.title }}" not in html


def test_card_focus_renders_combined_issue_label_in_js() -> None:
    """Client-side card renderers must mirror the template's label fallback."""
    js = _read(DASHBOARD_JS)
    assert "card.issue_label" in js
    assert "item.issue_label" in js


def test_compact_cards_use_fingerprint_delta_path() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderCompactCards")
    assert "computeCompactCardFingerprint" in body
    assert "dataset.cardFingerprint" in body
    assert "card.card_id" in body
    assert "dataset.cardId" in body


def test_compact_cards_sync_phase_age_in_place_when_fingerprint_matches() -> None:
    """phase_age is excluded from the fingerprint to avoid replacing every
    running card on every tick. The fingerprint-matched branch must still
    keep the displayed time string current — sync .card-phase-age
    textContent in place rather than replacing the node.
    """
    js = _read(DASHBOARD_JS)
    sync_body = _function_body(js, "syncCompactCardPhaseAge")
    assert ".card-phase-age" in sync_body
    assert "card.phase_age" in sync_body
    render_body = _function_body(js, "renderCompactCards")
    assert "syncCompactCardPhaseAge" in render_body
    # The HTML the JS produces must wrap phase + age in spans the sync helper
    # can target without replacing the line.
    card_html_body = _function_body(js, "renderCompactCardHtml")
    assert "card-phase-text" in card_html_body
    assert "card-phase-age" in card_html_body


def test_compact_card_phase_age_has_single_render_source() -> None:
    """The phase-age display string must be produced in exactly one place
    (compactCardPhaseAgeInnerHtml), used by both the initial builder and the
    in-place sync — not duplicated across them. Timestamp localization is
    delegated to the one shared [data-dashboard-timestamp] localizer rather
    than re-implemented per render site.
    """
    js = _read(DASHBOARD_JS)
    builder = _function_body(js, "renderCompactCardHtml")
    sync = _function_body(js, "syncCompactCardPhaseAge")
    helper = _function_body(js, "compactCardPhaseAgeInnerHtml")

    # Both render paths delegate to the single helper.
    assert "compactCardPhaseAgeInnerHtml(card)" in builder
    assert "compactCardPhaseAgeInnerHtml(card)" in sync
    # The marker is emitted only in the one helper, gated on time_is_timestamp.
    assert "data-dashboard-timestamp" in helper
    assert "time_is_timestamp" in helper
    assert "data-dashboard-timestamp" not in builder
    # Timestamp cards are handed to the shared localizer; the sync does not
    # format time itself.
    assert "formatDashboardTimestamps" in sync


def test_first_refresh_holds_data_booting_through_dom_mutations() -> None:
    """`data-booting` suppresses CSS transitions during boot. The boot
    handler must `await` the first refreshViewModel before clearing
    `data-booting` — otherwise transitions re-enable while cards are
    being placed and the user sees the dashboard-open flash.
    """
    js = _read(DASHBOARD_JS)
    # The DOMContentLoaded handler must be async (awaiting refreshViewModel).
    needle = "document.addEventListener('DOMContentLoaded', async () =>"
    assert needle in js, "DOMContentLoaded handler must be async to await first refresh"
    handler_start = js.index(needle)
    # Walk braces to find matching close so we don't misinterpret nested
    # closures as the handler boundary.
    depth = 0
    body_start = js.index("{", handler_start)
    i = body_start
    while i < len(js):
        ch = js[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    handler = js[handler_start:i + 1]
    assert "await refreshViewModel" in handler
    assert "markDashboardBooted" in handler
    assert handler.index("await refreshViewModel") < handler.index("markDashboardBooted")


def test_refresh_view_model_coalesces_concurrent_calls() -> None:
    """DOMContentLoaded and SSE `onopen` both fire `refreshViewModel`
    within milliseconds of each other on dashboard open. Without dedup
    we issue two `/api/view-model` requests and trigger two waves of DOM
    mutations — even if both find matching fingerprints, that's wasted
    work and a needless second pass over status badges / refresh status.

    The dedup must be split by request mode: a snapshot caller (which
    needs the row payload for refreshIssueRows) must not be handed a
    view-model-only promise, otherwise list-changing SSE events
    (queue.changed, session.started, ...) silently skip refreshIssueRows.
    """
    js = _read(DASHBOARD_JS)
    assert "_refreshInFlight" in js, "expected concurrent-call dedup state in refreshViewModel"
    assert "_refreshInFlight.snapshot" in js
    assert "_refreshInFlight.viewModel" in js
    # The view-model branch may piggyback on a snapshot (snapshot is a
    # superset), but the snapshot branch must NEVER reuse a view-model.
    snapshot_branch_start = js.index("if (reloadOnListChange) {")
    snapshot_branch_end = js.index("// view-model:", snapshot_branch_start)
    snapshot_branch = js[snapshot_branch_start:snapshot_branch_end]
    assert "_refreshInFlight.viewModel" not in snapshot_branch, (
        "snapshot caller must not be served by a view-model-only in-flight "
        "promise — refreshIssueRows would be skipped"
    )


def test_expanded_cards_render_label_badges() -> None:
    js = _read(DASHBOARD_JS)
    snippet = _function_body(js, "renderExpandedCardHtml")
    assert "orchestrator_labels" in snippet
    assert "badge-orch" in snippet
    assert "card-badges" in snippet
    assert "resetRetrySingle" in snippet
    assert "retryExpandedSingle" in snippet
    assert "hasPrClosedBlock(item)" in snippet
    assert "retryPrClosedSingle" in snippet
    assert "closePrClosedIssue" in snippet


def test_pr_closed_block_cards_offer_only_retry_and_close() -> None:
    js = _read(DASHBOARD_JS)
    snippet = _function_body(js, "renderExpandedCardHtml")
    assert "columnId === 'blocked' && isPrClosedBlock" in snippet
    assert "Close Issue" in snippet
    assert "columnId === 'blocked' && !isPrClosedBlock" in snippet
    assert "Reset & Retry From Scratch" in snippet
    assert "retryPrClosedSingle" in snippet
    assert "closePrClosedIssue" in snippet


def test_pr_closed_block_detector_handles_prefixed_labels() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "hasPrClosedBlock")
    assert "blocked:pr-closed" in body
    assert "endsWith(':blocked:pr-closed')" in body


def test_session_replay_seek_reuses_terminal_for_forward_progress() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "replaySessionToIndex")
    assert "if (!sessionReplayState.terminal)" in body
    assert "if (clampedIndex < sessionReplayState.playbackIndex)" in body
    assert "for (let index = sessionReplayState.playbackIndex; index < clampedIndex; index += 1)" in body


def test_session_replay_bootstraps_from_recorded_geometry() -> None:
    js = _read(DASHBOARD_JS)
    init_body = _function_body(js, "initializeSessionReplay")
    create_body = _function_body(js, "createSessionReplayTerminal")
    fit_body = _function_body(js, "fitSessionReplayTerminal")
    assert "resolveSessionReplayInitialGeometry" in init_body
    assert "sessionReplayState.initialGeometry" in create_body
    assert "terminalOptions.rows = sessionReplayState.initialGeometry.rows" in create_body
    assert "terminalOptions.cols = sessionReplayState.initialGeometry.cols" in create_body
    assert "if (sessionReplayState.initialGeometry) return;" in fit_body


def test_session_replay_renders_phase_chapters_when_present() -> None:
    """Guardrail: session_replay.js wires ``payload.chapters`` and
    ``payload.recording_event_index`` into the player.

    The persistent runner writes a chapters.json sidecar so the
    timeline view can scrub directly to "Round N → Coder Prompt".
    The terminal-recording route returns these as ``chapters`` and
    ``recording_event_index`` when phase-scoped. Without explicit
    consumption here, future edits could drop the wiring and the
    sidecar becomes dead weight.
    """
    js = _read(DASHBOARD_JS)
    init_body = _function_body(js, "initializeSessionReplay")
    refresh_body = _function_body(js, "refreshAgentLog")
    chapters_body = _function_body(js, "renderSessionReplayChapters")

    assert "payload.chapters" in init_body
    assert "payload.recording_event_index" in init_body
    assert "renderSessionReplayChapters(sessionReplayState)" in init_body

    # Refresh path picks up new chapters added by later rounds without
    # reopening the modal.
    assert "data.chapters" in refresh_body
    assert "renderSessionReplayChapters(sessionReplayState)" in refresh_body

    # Chapter renderer translates absolute recording indices into the
    # current slice's local playback index. Without that subtraction,
    # clicking "Round 2 Prompt" would seek to the wrong event.
    assert "state.recordingEventIndex" in chapters_body
    assert (
        "Number(chapter.recording_event_index) - baseIndex" in chapters_body
    )


def test_session_replay_resize_event_does_not_fit_over_recorded_geometry() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "applyTerminalRecordingEvent")
    assert "sessionReplayState.initialGeometry = { rows: event.rows, cols: event.cols }" in body
    assert "sessionReplayState.terminal.resize(event.cols, event.rows);" in body
    assert "fitSessionReplayTerminal();" not in body


def test_session_replay_compresses_idle_gaps_during_playback() -> None:
    """Guardrail: playback delays route through a capped, pure helper.

    The #6583 "looks frozen at 0 / N events" symptom was a raw offset gap
    honored at 1x — a multi-minute idle pause stalled the scrubber. The step
    scheduler must delegate to ``computeSessionReplayStepDelay``, which clamps
    the idle gap to ``SESSION_REPLAY_MAX_IDLE_MS`` before applying speed so
    progress keeps advancing and early output is reachable quickly.
    """
    js = _read(DASHBOARD_JS)
    assert "const SESSION_REPLAY_MAX_IDLE_MS = 1000;" in js
    delay_body = _function_body(js, "computeSessionReplayStepDelay")
    assert "SESSION_REPLAY_MAX_IDLE_MS" in delay_body
    assert "Math.min(" in delay_body
    schedule_body = _function_body(js, "scheduleSessionReplayStep")
    assert "computeSessionReplayStepDelay(" in schedule_body


def test_session_replay_reports_explicit_playback_state_and_empty_distinction() -> None:
    """Guardrail: the viewer names its state and distinguishes blank-vs-empty.

    ``updateSessionReplayUi`` must drive a single source of truth
    (``describeSessionReplayPlayback``) for both the ``aria-live`` status label
    and the ``data-playback-state`` hook, and must surface an explicit
    empty-state overlay when zero events are loaded so a blank terminal with
    events is never confused with a capture gap.
    """
    js = _read(DASHBOARD_JS)
    update_body = _function_body(js, "updateSessionReplayUi")
    assert "describeSessionReplayPlayback(" in update_body
    assert "shellEl.dataset.playbackState = playback.key" in update_body
    assert "updateSessionReplayEmptyState(total === 0)" in update_body

    describe_body = _function_body(js, "describeSessionReplayPlayback")
    for key in ("'empty'", "'start'", "'playing'", "'paused'", "'end'"):
        assert key in describe_body, f"missing playback state {key}"

    open_body = _function_body(js, "openAgentLog")
    assert 'id="sessionReplayShell"' in open_body
    # Accessibility: the scrubber has an accessible name and the status region
    # announces state changes politely.
    assert 'aria-label="Replay position (events)"' in open_body
    assert 'role="status" aria-live="polite"' in open_body


def test_session_replay_empty_overlay_is_positioned_over_terminal() -> None:
    css = _read_dashboard_css_bundle()
    empty_rule = _last_css_rule_body(css, ".session-replay-empty")
    assert "position: absolute" in empty_rule
    assert "pointer-events: none" in empty_rule
    terminal_rule = _last_css_rule_body(css, ".session-replay-terminal")
    assert "position: relative" in terminal_rule


def test_unblock_handlers_use_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    for fn in (
        "unblockSingle",
        "retryPrClosedSingle",
        "bulkUnblock",
        "unblockFromDrawer",
        "unblockSelectedIssues",
    ):
        body = _function_body(js, fn)
        assert "uiActionContract.buildUnblockRequest" in body
        assert "/api/unblock-retry" not in body


def test_close_issue_handler_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    contract_js = _read(UI_ACTION_CONTRACT_JS)
    body = _function_body(js, "closePrClosedIssue")
    assert "buildCloseIssueRequest" in contract_js
    assert "CLOSE_ISSUE" in contract_js
    assert "uiActionContract.buildCloseIssueRequest" in body
    assert "/api/issues/" not in body


def test_retry_publish_handler_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "retryPublishFromDrawer")
    assert "uiActionContract.buildRetryPublishRequest" in body
    assert "/api/issues/" not in body


def test_render_issue_detail_toggles_retry_publish_button_from_actions() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderIssueDetail")
    assert "issueDetailRetryPublishBtn" in body
    assert "action.id === 'retry_publish'" in body


def test_render_issue_detail_title_preserves_issue_number() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderIssueDetail")
    title_body = _function_body(js, "formatIssueDetailTitle")
    assert "formatIssueDetailTitle(d)" in body
    assert "`#${detail.issue_number}`" in title_body
    assert "return `${issueNumber}: ${title}`" in title_body


def test_copy_journey_timeline_formats_raw_timestamps_locally() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "copyJourneyTimeline")
    assert "formatJourneyHeaderTimestamp(run.timestamp || '', run.time_label || '')" in body
    assert "formatJourneyHeaderTimestamp(c.timestamp || '', c.time_label || '')" in body
    assert "formatJourneyStepTimestamp(s.timestamp || '', s.time_label || '')" in body


def test_journey_cycle_header_renders_validation_badge() -> None:
    """Each cycle in the journey timeline carries a validation badge.

    Phase B (issue #6310 follow-up): the badge is no longer a modal
    trigger.  It is an in-drawer affordance that, when clicked, expands
    the corresponding cycle's validation event and scrolls it into
    view.  The body of that expansion is the canonical viewer.  No more
    modal.

    Three rendered states:
    - passed → green button, click expands inline detail.
    - failed → red button, click expands inline detail.
    - not_validated → amber static span (anti-pattern marker; no click).
    - pending → no badge.
    """
    js = _read(DASHBOARD_JS)
    badge_body = _function_body(js, "_renderCycleValidationBadge")
    runs_body = _function_body(js, "_renderJourneyRuns")
    cycle_summary_body = _function_body(js, "_renderIssueLifecycleCycleSummary")

    # The cycle header includes the badge.
    assert "renderCycleValidationBadge: _renderCycleValidationBadge" in runs_body
    assert "${validationBadge}" in cycle_summary_body

    # Typed badge: state-driven, drawer-routed.
    assert "badge.state" in badge_body
    assert "is-passed" in badge_body
    assert "is-failed" in badge_body
    assert "is-not-validated" in badge_body
    # The non-validated span never has a clickable command attached.
    assert "data-validation-state=\"not_validated\"" in badge_body
    # Pending falls through to no badge.
    assert "if (state === 'pending') return ''" in badge_body
    # Phase B (issue #6310 follow-up): the badge now routes through the
    # plugin-owned inline-expansion handler, not the modal-opening
    # ``runLifecycleCommand`` pipeline.
    assert "runHierarchicalTimelineHostCapability('handleCycleValidationBadgeClick', this)" in badge_body
    assert 'data-issue-number="${escapeAttr(_issueNumber || \'\')}"' in badge_body
    # Drawer no longer pops a modal — the inline expansion is the canonical
    # viewer mounted right under the validation event row.
    assert "openValidationFailure(" not in badge_body

    css = _read_dashboard_css_bundle()
    assert ".journey-cycle-validation-badge" in css
    assert ".journey-cycle-validation-badge.is-passed" in css
    assert ".journey-cycle-validation-badge.is-failed" in css
    assert ".journey-cycle-validation-badge.is-not-validated" in css


def test_cycle_validation_badge_derived_from_raw_events() -> None:
    """The per-cycle validation badge is a typed ``CycleValidationBadge``
    (issue #6310 AC-2) derived from canonical event classifiers in the
    lifecycle projection.  Drawer renders ``state`` directly; for
    passed/failed states it dispatches the typed
    ``OpenValidationDetailsCommand`` through the existing
    ``runLifecycleCommand`` Command pipeline."""
    from issue_orchestrator.view_models.journey_projection import (
        derive_cycle_validation_badge,
    )
    from issue_orchestrator.view_models.lifecycle_semantics import (
        OpenValidationDetailsCommand,
    )

    passed_events = [
        {"event": "agent.coding_started", "run_dir": "/tmp/run-1"},
        {
            "event": "validation.passed",
            "run_dir": "/tmp/run-1",
            "artifacts": [{"kind": "validation", "path": "/tmp/run-1/validation.json"}],
        },
    ]
    badge = derive_cycle_validation_badge(passed_events, issue_number=4124)
    assert badge.state == "passed"
    assert badge.command == OpenValidationDetailsCommand(
        issue_number=4124, run_dir="/tmp/run-1"
    )

    failed_events = [{"event": "validation.failed", "run_dir": "/tmp/run-2"}]
    badge = derive_cycle_validation_badge(failed_events, issue_number=4124)
    assert badge.state == "failed"
    assert badge.command == OpenValidationDetailsCommand(
        issue_number=4124, run_dir="/tmp/run-2"
    )

    # Terminated coding cycle without a validation event = anti-pattern.
    # All canonical completed/blocked/failed/publish-failed events count
    # as terminal — the badge policy must not drift from the lifecycle
    # projection's ``CODING_TERMINAL_EVENTS`` set.
    for terminal_event in (
        "session.completed",
        "agent.coding_completed",
        "observation.completion_detected",
        "session.failed",
        "session.timeout",
        "session.blocked",
        "publish.failed",
    ):
        terminated_unvalidated = [
            {"event": "agent.coding_started", "run_dir": "/tmp/run-3"},
            {"event": terminal_event, "run_dir": "/tmp/run-3"},
        ]
        badge = derive_cycle_validation_badge(
            terminated_unvalidated, issue_number=4124
        )
        assert badge.state == "not_validated", (
            f"Terminal event {terminal_event!r} without validation must "
            "project not_validated (drift from CODING_TERMINAL_EVENTS)"
        )
        assert badge.command is None, (
            "not_validated must not carry a command (no dialog to open)"
        )

    # Cycle still running (no terminal event yet) MUST NOT surface the
    # "Not validated" anti-pattern marker — validation has not had its
    # chance to run.  Returning ``pending`` lets the frontend draw no
    # badge for in-flight work.
    running = [
        {"event": "session.started", "run_dir": "/tmp/run-4"},
        {"event": "agent.coding_started", "run_dir": "/tmp/run-4"},
    ]
    badge = derive_cycle_validation_badge(running, issue_number=4124)
    assert badge.state == "pending"
    assert badge.command is None

    # Failed wins when both fire — the latest-event-wins reverse scan picks
    # up the failure (final retry was the outcome).
    later_failed = [
        {"event": "validation.passed", "run_dir": "/tmp/run-4"},
        {"event": "validation.failed", "run_dir": "/tmp/run-4"},
    ]
    later_badge = derive_cycle_validation_badge(later_failed, issue_number=4124)
    assert later_badge.state == "failed"


def test_cycle_validation_badge_typed_model_rejects_invalid_state_command_combos() -> None:
    """``CycleValidationBadge`` validates the state↔command invariant
    (issue #6310 AC-2)."""
    import pytest

    from issue_orchestrator.view_models.lifecycle_semantics import (
        CycleValidationBadge,
        OpenValidationDetailsCommand,
    )

    valid_command = OpenValidationDetailsCommand(issue_number=1, run_dir="/tmp/r")

    # passed/failed without command → rejected
    for state in ("passed", "failed"):
        with pytest.raises(ValueError, match="command required"):
            CycleValidationBadge(state=state, command=None)  # type: ignore[arg-type]

    # pending/not_validated with command → rejected
    for state in ("pending", "not_validated"):
        with pytest.raises(ValueError, match="command must be absent"):
            CycleValidationBadge(state=state, command=valid_command)  # type: ignore[arg-type]


def test_cycle_validation_badge_shares_event_sets_with_lifecycle() -> None:
    """Pin the shared-owner abstraction (issue #6310 AC-4): the badge
    derives from canonical event sets owned by ``lifecycle_event_sets``.
    No parallel ``_CYCLE_*`` aliases live in ``issue_detail`` anymore —
    ``journey_projection.derive_cycle_validation_badge`` consumes the
    canonical sets directly."""
    from issue_orchestrator.view_models import (  # type: ignore[attr-defined]
        issue_detail as _issue_detail,
        journey_projection as _journey,
        lifecycle_event_sets as _classifiers,
        lifecycle_projection as _lifecycle,
    )

    # ``issue_detail`` no longer re-exports the per-cycle classifier
    # frozensets; the typed badge derives them directly.
    assert not hasattr(_issue_detail, "_CYCLE_CODING_TERMINAL_EVENTS")
    assert not hasattr(_issue_detail, "_CYCLE_VALIDATION_PASSED_EVENTS")
    assert not hasattr(_issue_detail, "_CYCLE_VALIDATION_FAILED_EVENTS")
    # No dict-shaped ``_cycle_validation_summary`` either — replaced by
    # the typed ``derive_cycle_validation_badge`` in
    # ``journey_projection``.
    assert not hasattr(_issue_detail, "_cycle_validation_summary")
    assert callable(_journey.derive_cycle_validation_badge)

    # lifecycle_projection re-publishes canonical sets by identity.
    assert _lifecycle.CODING_TERMINAL_EVENTS is _classifiers.CODING_TERMINAL_EVENTS
    assert _lifecycle.VALIDATION_PASSED_EVENTS is _classifiers.VALIDATION_PASSED_EVENTS
    assert _lifecycle.VALIDATION_FAILED_EVENTS is _classifiers.VALIDATION_FAILED_EVENTS
    # Public re-export must equal the private set it aliases (no
    # accidental decoupling of the public name).
    assert _lifecycle.VALIDATION_PASSED_EVENTS is _lifecycle._VALIDATION_PASSED_EVENTS  # noqa: SLF001
    assert _lifecycle.VALIDATION_FAILED_EVENTS is _lifecycle._VALIDATION_FAILED_EVENTS  # noqa: SLF001
    # Sanity: canonical event names are in the right sets.
    assert "validation.passed" in _classifiers.VALIDATION_PASSED_EVENTS
    assert "session.validation_passed" in _classifiers.VALIDATION_PASSED_EVENTS
    assert "validation.failed" in _classifiers.VALIDATION_FAILED_EVENTS
    assert "session.validation_failed" in _classifiers.VALIDATION_FAILED_EVENTS
    assert "session.validation_retry_needed" in _classifiers.VALIDATION_FAILED_EVENTS
    assert {
        "session.completed",
        "agent.coding_completed",
        "observation.completion_detected",
        "session.blocked",
        "session.failed",
        "publish.failed",
    } <= _classifiers.CODING_TERMINAL_EVENTS


def test_open_validation_failure_uses_dedicated_dialog_endpoint() -> None:
    js = _read(DASHBOARD_JS)
    fetch_body = _function_body(js, "openValidationFailure")
    # The fetch entry point hits the dedicated dialog endpoint and delegates
    # rendering to renderValidationDialog (the pure data → DOM mapping).
    assert "/api/dialog/validation-failure/" in fetch_body
    assert "renderValidationDialog(" in fetch_body
    assert "data.actions" not in fetch_body

    render_body = _function_body(js, "renderValidationDialog")
    # Render shape — these markers prove the modal scaffolding is intact
    # and that the body delegates to the canonical viewer (issue #6310
    # follow-up, Phase A).  The legacy ``diag-validation-grid`` two-pane
    # body is replaced by ``renderCanonicalValidationViewer`` which
    # produces the ``cvv-root`` wrapper.  The viewer takes the
    # action-section renderer as an explicit ``options.renderActionSections``
    # dependency (reviewer Blocker 2 on PR #6314) — the dialog passes
    # ``renderValidationFailureActionSections`` from session_dialogs.js.
    assert "Validation Results" in render_body
    assert "renderCanonicalValidationViewer(data" in render_body
    assert "renderActionSections: renderValidationFailureActionSections" in render_body
    assert "renderValidationFailureActionSections" in js  # used inside the dialog


def test_session_prompt_handlers_use_ui_action_contract() -> None:
    # The run-scoped launch-prompt actions (issue #6588 F2) must build their
    # request through the shared contract owner, not hardcode the endpoint, so
    # future endpoint/query changes have a single source of truth.
    js = _read(DASHBOARD_JS)
    contract_js = _read(UI_ACTION_CONTRACT_JS)
    assert "buildSessionPromptRequest" in contract_js
    assert "SESSION_PROMPT" in contract_js
    for fn in ("refreshInlineSessionPrompt", "openLaunchPromptDialog"):
        body = _function_body(js, fn)
        assert "uiActionContract.buildSessionPromptRequest" in body
        assert "/api/session/prompt/" not in body


def test_timeline_prioritizes_validation_details_for_validation_failures() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderTimelineEventActions")
    assert "'open_validation_failure'" in body


def test_reset_handler_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "resetSelectedIssues")
    assert "uiActionContract.buildResetRetryRequest" in body
    assert "/api/reset-retry" not in body


def test_bulk_reset_handler_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "bulkResetRetry")
    assert "uiActionContract.buildResetRetryRequest" in body
    assert "/api/reset-retry" not in body


def test_bulk_reset_from_scratch_handler_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "bulkResetRetryFromScratch")
    assert "uiActionContract.buildResetRetryRequest" in body
    assert "fromScratch: true" in body
    assert "/api/reset-retry" not in body


def test_retrospective_review_handlers_use_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    preview_body = _function_body(js, "previewRetrospectiveReview")
    execute_body = _function_body(js, "executeRetrospectiveReview")
    assert "uiActionContract.buildRetrospectiveReviewPreflightRequest" in preview_body
    assert "uiActionContract.buildRetrospectiveReviewExecuteRequest" in execute_body
    assert "/api/retrospective-review" not in preview_body
    assert "/api/retrospective-review" not in execute_body


def test_retrospective_review_confirmation_discloses_review_first_boundary() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "executeRetrospectiveReview")
    assert "start with reviewer audit" in body
    assert "Closed issues stay closed unless the reviewer requests changes" in body
    assert "will not delete worktrees" in body
    assert "delete branches" in body
    assert "supersede PRs" in body
    assert "start a coder unless changes are requested" in body


def test_retrospective_review_modal_is_labelled_and_status_announced() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openRetrospectiveReviewDialog")
    assert "Review Existing Implementation" in body
    assert 'for="retrospectiveReviewIssues"' in body
    assert 'aria-describedby="retrospectiveReviewHelp retrospectiveReviewBoundary"' in body
    assert 'role="status" aria-live="polite"' in body
    assert "Skipped issues are unchanged" in body
    assert "reviewer audit of the existing implementation" in body


def test_retrospective_review_summary_does_not_show_dead_reopen_count() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderRetrospectiveReviewPreflight")
    assert "0 will reopen" not in body
    assert "will reopen" not in body


def test_reset_from_scratch_confirmations_disclose_full_boundary() -> None:
    js = _read(DASHBOARD_JS)
    for fn_name in (
        "bulkResetRetryFromScratch",
        "resetRetrySingleFromScratch",
        "resetSelectedIssuesFromScratch",
    ):
        body = _function_body(js, fn_name)
        assert "supersede open orchestrator PRs" in body
        assert "Prior review approvals and validation artifacts will not be reused" in body
        assert "NEW branch" in body


def test_single_reset_handler_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    marker = "async function performResetRetry("
    start = js.find(marker)
    assert start != -1
    snippet = js[start : start + 1400]
    assert "uiActionContract.buildResetRetryRequest" in snippet
    assert "/api/reset-retry" not in snippet


def test_deprioritize_handler_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "bulkDeprioritize")
    assert "uiActionContract.buildBulkDeprioritizeRequest" in body
    assert "/api/bulk-deprioritize" not in body


def test_cancel_queued_handlers_use_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    for fn in ("cancelQueuedSingle", "bulkCancelQueued"):
        body = _function_body(js, fn)
        assert "uiActionContract.buildBulkCancelQueuedRequest" in body
        assert "/api/bulk-cancel-queued" not in body


def test_bulk_retry_completed_handler_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "bulkRetryCompleted")
    assert "uiActionContract.buildBulkRetryRequest" in body
    assert "/api/bulk-retry" not in body


def test_bulk_retry_awaiting_merge_handler_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "bulkRetryAwaitingMerge")
    assert "uiActionContract.buildUnblockRequest" in body
    assert "/api/unblock-retry" not in body


def test_bulk_reset_retry_reads_from_blocked_and_awaiting_merge() -> None:
    """Both bulk reset handlers must collect issues from blocked AND awaiting-merge."""
    js = _read(DASHBOARD_JS)
    for fn_name in ("bulkResetRetry", "bulkResetRetryFromScratch"):
        body = _function_body(js, fn_name)
        assert "getSelectedIssueNumbers('blocked')" in body, f"{fn_name} missing blocked selection"
        assert "getSelectedIssueNumbers('awaiting-merge')" in body, f"{fn_name} missing awaiting-merge selection"
        assert "'awaiting-merge'" in body, f"{fn_name} missing awaiting-merge in optimistic requeue"


def test_retry_handler_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "retryIssue")
    assert "uiActionContract.buildIssueRetryRequest" in body
    assert "/api/issues/" not in body


def test_reveal_worktree_menu_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    assert "uiActionContract.buildRevealWorktreeRequest" in js
    assert "/api/finder/" not in js


def test_open_log_file_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openLogFile")
    assert "uiActionContract.buildHostOpenPathRequest" in body
    assert "/api/host/open-path" not in body


def test_session_replay_uses_terminal_recording_endpoint_and_emulator() -> None:
    js = _read(DASHBOARD_JS)
    contract_js = _read(UI_ACTION_CONTRACT_JS)
    open_body = _function_body(js, "openAgentLog")
    refresh_body = _function_body(js, "refreshAgentLog")
    assert "uiActionContract.buildTerminalRecordingRequest" in open_body
    assert "uiActionContract.buildTerminalRecordingRequest" in refresh_body
    assert "/api/log/local/" not in open_body
    assert "/api/log/local/" not in refresh_body
    assert "round_index" in open_body
    assert "session_role" in open_body
    assert "round_index" in contract_js
    assert "session_role" in contract_js


def test_review_transcript_uses_dedicated_endpoint_not_terminal_replay() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openReviewTranscript")
    assert "/api/session/review-transcript/" in body
    assert "/api/session/terminal-recording/" not in body
    assert "Review Transcript #" in body
    assert "round_index" in body
    assert "transcript_role" in body


def test_timeline_prefers_session_recording_before_review_transcript() -> None:
    js = _read(DASHBOARD_JS)
    timeline_body = _function_body(js, "renderTimelineEventActions")
    short_label_body = _function_body(js, "_timelineActionShortLabel")
    assert timeline_body.index("'open_agent_log'") < timeline_body.index("'open_review_transcript'")
    assert "Session Recording" in short_label_body
    assert "Review Transcript" in short_label_body


def test_timeline_session_recording_labels_keep_role_context() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "_timelineActionShortLabel")
    assert "Reviewer Recording" in body
    assert "Coding Recording" in body
    assert "Rework Recording" in body


def test_timeline_unavailable_artifact_actions_keep_backend_label_and_can_be_primary() -> None:
    js = _read(DASHBOARD_JS)
    render_body = _function_body(js, "renderTimelineEventActions")
    label_body = _function_body(js, "_timelineActionShortLabel")
    assert "item.action.primary !== true" in render_body
    assert "type === 'show_actions_error') return label || 'What is missing?'" in label_body


def test_cycle_artifact_popover_does_not_invent_run_level_session_transcript() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "toggleArtifactPopover")
    assert "Cycle Session Recording" not in body
    assert "View session transcript" not in body
    assert "openAgentLogAction" not in body


def test_session_replay_terminal_wrap_allows_scroll_for_fixed_geometry() -> None:
    css = _read_dashboard_css_bundle()
    assert ".session-replay-terminal {" in css
    assert "overflow: auto;" in css


def test_host_action_contract_exposes_host_neutral_builders() -> None:
    contract_js = _read(UI_ACTION_CONTRACT_JS)
    assert "buildRevealWorktreeRequest" in contract_js
    assert "REVEAL_WORKTREE" in contract_js
    assert "ENDPOINTS.REVEAL_WORKTREE" in contract_js
    assert "buildHostOpenPathRequest" in contract_js
    assert "HOST_OPEN_PATH" in contract_js


def test_requeue_paths_use_optimistic_requeue_helper() -> None:
    js = _read(DASHBOARD_JS)
    for fn in (
        "unblockSingle",
        "bulkUnblock",
        "bulkResetRetry",
        "bulkResetRetryFromScratch",
        "retryPrClosedSingle",
        "retryExpandedSingle",
        "bulkRetryAwaitingMerge",
        "bulkRetryCompleted",
        "retryIssue",
        "unblockFromDrawer",
        "unblockSelectedIssues",
        "resetSelectedIssues",
    ):
        body = _function_body(js, fn)
        assert "applyOptimisticRequeue(" in body
        assert "location.reload()" not in body
    marker = "async function performResetRetry("
    start = js.find(marker)
    assert start != -1
    snippet = js[start : start + 1400]
    assert "applyOptimisticRequeue(" in snippet
    assert "location.reload()" not in snippet


def test_menu_retry_handler_uses_contract_and_refresh_not_reload() -> None:
    js = _read(DASHBOARD_JS)
    marker = "menuRetry?.addEventListener('click'"
    start = js.find(marker)
    assert start != -1
    snippet = js[start : start + 1400]
    assert "uiActionContract.buildIssueRetryRequest" in snippet
    assert "applyOptimisticRequeue(" in snippet
    assert "await refreshViewModel()" in snippet
    assert "location.reload()" not in snippet


def test_context_menu_orchestrator_log_avoids_legacy_agent_log_endpoint() -> None:
    js = _read(DASHBOARD_JS)
    marker = "menuLog?.addEventListener('click'"
    start = js.find(marker)
    assert start != -1
    snippet = js[start : start + 700]
    assert "openFilteredOrchestratorLog(" in snippet
    assert "/api/log/" not in snippet


def test_timeline_overflow_menu_renders_as_floating_popover() -> None:
    css = _read_dashboard_css_bundle()
    # The overflow menu must float over the row using position:fixed so it
    # escapes ancestor overflow:auto containers (e.g. the e2e diagnosis modal
    # body). JS sets the top/left from the trigger's getBoundingClientRect.
    assert ".timeline-event-menu-items {" in css
    items_block = css.split(".timeline-event-menu-items {", 1)[1].split("}", 1)[0]
    assert "position: fixed;" in items_block
    assert "display: none;" in items_block
    assert "overflow: auto;" in items_block
    assert ".timeline-event-menu[open] .timeline-event-menu-items {" in css
    open_items_block = css.split(
        ".timeline-event-menu[open] .timeline-event-menu-items {", 1
    )[1].split("}", 1)[0]
    assert "display: grid;" in open_items_block

    # The legacy nested "More ▾" disclosure rules must be gone.
    assert ".timeline-more-items" not in css
    assert ".timeline-more-trigger" not in css
    assert ".timeline-more-menu" not in css

    js = _read(DASHBOARD_JS)
    body = _function_body(js, "positionTimelineEventMenu")
    # Must read the trigger's viewport rect and clamp to the viewport so
    # the popover stays clickable even when triggers sit near a panel edge.
    assert "getBoundingClientRect" in body
    assert "window.innerWidth" in body
    assert "window.innerHeight" in body
    assert "_timelineEventMenuFixedOffset(items)" in body
    assert "fixedOffset.left" in body
    assert "fixedOffset.top" in body


def test_session_diagnostics_tracks_timeout_and_session_settings_action() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openSessionManifest")
    assert "currentDiagnosticsRunDir" in body
    assert "'timeout'" in body


def test_timeline_status_helpers_render_timed_out_as_failure() -> None:
    js = _read(DASHBOARD_JS)
    status_class_body = _function_body(js, "getStatusClass")
    format_status_body = _function_body(js, "formatStatus")

    assert "'timed_out'" in status_class_body
    assert "'timed_out': 'Timed Out'" in format_status_body


def test_context_menu_retry_statuses_include_awaiting_merge() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "showContextMenu")
    assert "normalizedStatus = statusLower.replace(/_/g, '-')" in body
    assert "effectiveHistoryStatus = (isCompactCardMenu && columnId) ? columnId : normalizedStatus" in body
    assert "'awaiting-merge'" in body


def test_compact_card_context_menu_action_mapping_is_column_consistent() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "showContextMenu")
    assert "const isBlockedHistory = effectiveHistoryStatus === 'blocked' || effectiveHistoryStatus === 'needs-human';" in body
    assert "const isPrClosedBlock = hasPrClosedBlock" in body
    assert "const resetRetryStatuses = new Set(['blocked', 'awaiting-merge']);" in body
    assert "const otherRetryStatuses = new Set(['failed', 'completed', 'timed-out']);" in body
    assert "if (isPrClosedBlock)" not in body
    assert "menuUnblock.style.display = isBlockedHistory && !isPrClosedBlock ? '' : 'none';" in body
    assert "menuCloseIssue.style.display = isPrClosedBlock ? '' : 'none';" in body
    assert "menuResetRetry.style.display = 'none';" in body
    assert "menuResetRetry.style.display = '';" in body
    assert "menuResetRetryScratch.style.display = '';" in body
    assert "menuRetry.style.display = '';" in body
    assert "menuCloseIssue.style.display = 'none';" in body
    assert "setMenuVisible(menuLog, !isCompactCardMenu && !isBlockedHistory);" in body
    assert "setMenuVisible(menuAgentLog, !isCompactCardMenu && !isBlockedHistory);" in body
    assert "setMenuVisible(menuPR, Boolean(prUrl || row.dataset.issueUrl));" in body
    assert "menuPR.textContent = prUrl ? 'Open PR ↗' : 'Open Issue ↗';" in body
    assert "setMenuVisible(menuIssue, Boolean(prUrl && row.dataset.issueUrl));" in body


def test_context_menu_includes_reset_retry_from_scratch_label() -> None:
    html = _read(DASHBOARD_TEMPLATE)
    assert "Reset and Retry From Scratch" in html


def test_dashboard_menu_includes_retrospective_review_action() -> None:
    html = _read(DASHBOARD_TEMPLATE)
    assert "retrospective_review.js" in DASHBOARD_JS_CHUNKS
    assert "openRetrospectiveReviewDialog()" in html
    assert "Review Existing Implementation" in html
    assert 'aria-label="Close dialog"' in html
    assert 'role="dialog" aria-modal="true" aria-labelledby="modalTitle"' in html


def test_compact_menu_infers_column_id_from_parent_column() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openCompactCardActionsMenu")
    assert "button?.closest('.kanban-column')?.dataset?.column" in body
    assert "columnId: String(columnId || '')" in body


def test_compact_menu_preserves_orchestrator_labels() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openCompactCardActionsMenu")
    assert "button?.dataset?.orchestratorLabels" in body
    assert "const orchestratorLabels =" in body
    assert "orchestratorLabels," in body


def test_overlay_positioning_uses_shared_clamp_helpers() -> None:
    js = _read(DASHBOARD_JS)
    assert "function clampPagePoint(" in js
    assert "function clampClientPoint(" in js
    context_body = _function_body(js, "showContextMenu")
    assert "const clamped = clampPagePoint(" in context_body
    confirm_body = _function_body(js, "showConfirm")
    assert "const clamped = clampClientPoint(" in confirm_body


def test_context_menu_open_action_prefers_pr_then_issue_url() -> None:
    js = _read(DASHBOARD_JS)
    assert "const targetUrl = currentRow.dataset.prUrl || currentRow.dataset.issueUrl;" in js
    assert "window.open(targetUrl, '_blank');" in js
    assert "menuIssue?.addEventListener('click'" in js
    assert "window.open(currentRow.dataset.issueUrl, '_blank');" in js


def test_compact_card_primary_github_link_uses_view_model_fields() -> None:
    js = _read(DASHBOARD_JS)
    render_body = _function_body(js, "renderCompactCardHtml")
    helper_body = _function_body(js, "buildCompactGithubLink")
    assert "const ghLink = buildCompactGithubLink(card);" in render_body
    assert "card.github_url" in helper_body
    assert "card.github_label" in helper_body
    assert "'PR ↗'" in helper_body
    assert "card-pr-link" in helper_body


def test_bulk_open_prs_uses_pr_links_not_issue_links() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "bulkOpenPRs")
    assert "card.querySelector('.card-pr-link')" in body
    assert "card.querySelector('.card-gh')" not in body


def test_embedded_back_hidden_when_column_expanded() -> None:
    js = _read(DASHBOARD_JS)
    assert "function updateEmbeddedBackButtonVisibility()" in js
    # The label/aria-label writes are guarded with a value check (only write
    # when different) to avoid same-value MutationObserver fires that compound
    # into a header flash on every refresh. The desired text is computed once
    # then applied conditionally; assert on both halves.
    assert (
        "const desiredText = hasExpandedColumn ? 'Back to dashboard' : 'Back to repositories';"
        in js
    )
    assert "if (label.textContent !== desiredText) label.textContent = desiredText;" in js
    assert "embeddedBackLabel" in _read(DASHBOARD_TEMPLATE)
    body = _function_body(js, "toggleColumnExpand")
    assert "updateEmbeddedBackButtonVisibility();" in body
    assert "document.body.classList.toggle('column-focus-mode', !isExpanded);" in body


def test_update_bulk_bar_keeps_retry_columns_visible() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "updateBulkBar")
    assert "blocked', 'awaiting-merge', 'completed'" in body


def test_e2e_tab_hidden_in_column_focus_mode() -> None:
    css = _read_dashboard_css_bundle()
    assert "body.column-focus-mode #tab-e2e" in css
    assert "display: none;" in css


def test_embedded_back_controls_share_primary_button_style() -> None:
    css = _read_dashboard_css_bundle()
    assert ".embedded-back {" in css
    assert "color: var(--text);" in css


SETTINGS_TEMPLATE = ROOT / "src" / "issue_orchestrator" / "templates" / "settings.html"
EMBEDDED_NAV_JS = ROOT / "src" / "issue_orchestrator" / "static" / "js" / "embedded_nav.js"
EMBEDDED_NAV_TEST_JS = ROOT / "tests" / "js" / "embedded_nav.test.js"


def test_dashboard_settings_nav_uses_shared_embedded_nav_helper() -> None:
    # Regression: Dashboard → Settings must go through the shared embeddedNav
    # helper so ?embedded=1 AND ?theme= are both forwarded. Ad-hoc string
    # concatenation here previously dropped theme on the round-trip.
    js = _read(DASHBOARD_JS)
    assert "function goToSettings()" in js
    body = _function_body(js, "goToSettings")
    assert "embeddedNav.buildHref('/settings', window.location.search)" in body
    # Old ad-hoc helper must be gone so we have a single owner of the rule.
    assert "function withEmbeddedFlag" not in js
    tmpl = _read(DASHBOARD_TEMPLATE)
    assert 'onclick="goToSettings()"' in tmpl
    assert "window.location.href='/settings'" not in tmpl
    # Shared module must be loaded before the dashboard bundle.
    assert '<script src="/static/js/embedded_nav.js"></script>' in tmpl


def test_browser_auth_shows_overlay_instead_of_silent_reload_on_401() -> None:
    """A 401 from /api/ or /control/ must surface a visible 'Session expired'
    overlay so users can sign in again. The previous behavior — calling
    ``location.reload()`` silently — produced a white screen when the reload
    landed on a non-recoverable URL or hit a render race, leaving the user
    with no UI to recover. See PR fixing the white-screen-on-session-expiry bug.
    """
    js = _read(BROWSER_AUTH_JS)
    assert "showAuthExpiredOverlay" in js
    assert "io-auth-expired-overlay" in js
    assert "Session expired" in js
    assert "Sign in" in js
    # Lock out the regression: no silent reload on auth-expiry path.
    assert ".reload()" not in js, (
        "browser_auth.js must not silently reload on 401 — show the "
        "session-expired overlay so the user can recover"
    )


def test_browser_auth_helper_is_shared_by_control_center_and_dashboard() -> None:
    dashboard = _read(DASHBOARD_TEMPLATE)
    control_center = _read(ROOT / "src" / "issue_orchestrator" / "templates" / "control_center.html")

    assert '<meta name="io-csrf-token" content="{{ csrf_token }}">' in dashboard
    assert '<meta name="io-csrf-token" content="{{ csrf_token }}">' in control_center
    assert '<meta name="io-browser-auth-required" content="{{ browser_auth_required }}">' in dashboard
    assert '<meta name="io-browser-auth-required" content="{{ browser_auth_required }}">' in control_center
    assert '<script src="/static/js/browser_auth.js"></script>' in dashboard
    # Control Center adds a ``?v={{ static_version }}`` cache-buster on
    # static URLs (see infra/static_version.py); the dashboard shape is
    # unchanged. We pin the literal pre-render text here so a regression
    # on either surface fails this guardrail explicitly.
    assert '<script src="/static/js/browser_auth.js?v={{ static_version }}"></script>' in control_center
    assert dashboard.index('/static/js/browser_auth.js') < dashboard.index('/static/js/embedded_nav.js')
    assert dashboard.index('/static/js/browser_auth.js') < dashboard.index("{% for chunk in dashboard_js_chunks %}")
    assert control_center.index('/static/js/browser_auth.js') < control_center.index('/static/js/control_center.js')


def test_dashboard_pause_resume_surfaces_auth_failures() -> None:
    js = _read_dashboard_js_bundle()
    body = _function_body(js, "togglePause")
    helper_body = _function_body(js, "setPauseBadgeState")

    assert "const res = await fetch('/api/resume', { method: 'POST' });" in body
    assert "const res = await fetch('/api/pause', { method: 'POST' });" in body
    assert "if (!res.ok)" in body
    assert "setPauseBadgeState(false, 'Resuming...')" in body
    assert "setPauseBadgeState(true, 'Pausing...')" in body
    assert "readActionError(res)" in body
    assert "showToast(`Resume failed: ${message}`, true)" in body
    assert "showToast(`Pause failed: ${message}`, true)" in body
    resume_fetch = "const res = await fetch('/api/resume'"
    pause_fetch = "const res = await fetch('/api/pause'"
    resume_failure = body[
        body.index(resume_fetch) : body.index(
            "await refreshViewModel", body.index(resume_fetch)
        )
    ]
    pause_failure = body[
        body.index(pause_fetch) : body.index(
            "await refreshViewModel", body.index(pause_fetch)
        )
    ]
    assert resume_failure.index("setPauseBadgeState(true);") < resume_failure.index(
        "readActionError(res)"
    )
    assert pause_failure.index("setPauseBadgeState(false);") < pause_failure.index(
        "readActionError(res)"
    )
    assert "document.querySelectorAll('.status-badge').forEach" in helper_body
    # State class swap is now atomic (single className= write, preserving
    # auxiliary classes like embedded-badge) instead of three classList
    # removes + one add — that fired four MutationObserver events per
    # refresh and stacked into a visible header flash. Assert the new
    # state-only computation + atomic write are present.
    assert (
        "const stateClasses = ['status-paused', 'status-running', 'status-starting'];"
        in helper_body
    )
    assert "badge.className = [...others, desiredClass].join(' ');" in helper_body
    assert "updatePauseMenuFromViewModel({ paused })" in helper_body


def test_dashboard_bundle_loaded_marker_supports_browser_waits() -> None:
    legacy_wrapper = DASHBOARD_JS.read_text(encoding="utf-8")

    assert "window.dashboardBundleLoaded = true;" in legacy_wrapper


def test_dashboard_first_paint_boot_runs_before_stylesheets() -> None:
    tmpl = _read(DASHBOARD_TEMPLATE)
    css = _read_dashboard_css_bundle()
    js = _read(DASHBOARD_JS)
    boot_js = _read(DASHBOARD_BOOT_JS)
    embedded_nav_js = _read(EMBEDDED_NAV_JS)

    assert '<script src="/static/js/theme_resolution.js"></script>' in tmpl
    assert '<script src="/static/js/dashboard_boot.js"></script>' in tmpl
    assert tmpl.index('/static/js/theme_resolution.js') < tmpl.index(
        '/static/js/dashboard_boot.js'
    )
    assert '<link rel="stylesheet" href="/static/css/dashboard.css">' not in tmpl
    assert "{% for chunk in dashboard_css_chunks %}" in tmpl
    assert '<link rel="stylesheet" href="/static/css/dashboard/{{ chunk }}">' in tmpl
    assert tmpl.index('/static/js/dashboard_boot.js') < tmpl.index(
        '/static/css/dashboard/'
    )
    assert "id=\"dashboardInitStatus\"" in tmpl
    assert "Initializing orchestrator" in tmpl
    assert ".dashboard-init-status.is-active" in css
    assert "html[data-booting=\"true\"] .dashboard-init-status" not in css
    assert "html[data-embedded=\"true\"] body > .container > header" in css
    assert "html[data-embedded=\"true\"] .scope-summary" in css
    assert "window.dashboardBoot.clearBootingWhenStable(window)" in js
    assert "try {" in js
    assert "finally" in js
    assert "VALID_THEME_VALUES" in _read(THEME_RESOLUTION_JS)
    assert "VALID_THEME_VALUES" not in boot_js
    assert "VALID_THEME_VALUES" not in embedded_nav_js
    assert "header.style.display" not in js
    assert "scope-embedded" not in js


def test_dashboard_sse_requires_authenticated_stream_helper() -> None:
    js = _read_dashboard_js_bundle()
    connect_body = _function_body(js, "connectEventStream")
    reconnect_body = _function_body(js, "scheduleReconnect")

    assert "window.openAuthenticatedSseStream('/api/events')" in connect_body
    assert "new EventSource('/api/events')" not in connect_body
    assert "authenticated SSE helper is not loaded" in connect_body
    assert "Event stream disconnected... reconnecting" in reconnect_body
    assert "Engine restarting... reconnecting" not in reconnect_body


def test_settings_page_uses_shared_embedded_nav_helper() -> None:
    # Regression: Settings back-link and Cancel must go through embeddedNav
    # so the Dashboard round-trip keeps both embedded=1 and theme.
    tmpl = _read(SETTINGS_TEMPLATE)
    assert '<script src="/static/js/theme_resolution.js"></script>' in tmpl
    assert '<script src="/static/js/embedded_nav.js"></script>' in tmpl
    assert tmpl.index('/static/js/theme_resolution.js') < tmpl.index(
        '/static/js/embedded_nav.js'
    )
    assert 'id="backToDashboardLink"' in tmpl
    assert 'id="cancelSettingsBtn"' in tmpl
    assert 'onclick="cancelSettings()"' in tmpl
    assert "window.embeddedNav.buildHref('/', window.location.search)" in tmpl
    # Dict-editor JSON embeds must go through tojson (XSS neutralization);
    # the old tabs_for_js/schemas_for_js client-side schema bootstrap is
    # gone by design (form encoding is server-classified, see
    # test_settings_form_dispatches_cover_the_classifier_kind_set).
    assert "{{ control.value_options | tojson }}" in tmpl
    assert "{{ tab_values[field_name] | tojson }}" in tmpl
    assert "tabs_for_js" not in tmpl
    assert "schemas_for_js" not in tmpl
    # Old ad-hoc helpers / literals must be gone.
    assert "settingsIsEmbedded" not in tmpl
    assert "'/?embedded=1'" not in tmpl
    assert "onclick=\"window.location.href='/'\"" not in tmpl


def test_theme_resolution_uses_shared_embedded_nav_helper() -> None:
    # Cross-path rule drift guard: both Dashboard (applyDashboardTheme) and
    # Settings (applyTheme) must delegate theme resolution to
    # embeddedNav.resolveEffectiveTheme so the url > stored > system
    # precedence applies the same way on both surfaces. Previously Settings
    # ignored ?theme=, which left embedded Settings rendering the user's
    # local theme instead of the CC-supplied one.
    js = _read(DASHBOARD_JS)
    dashboard_body = _function_body(js, "applyDashboardTheme")
    assert "embeddedNav.resolveEffectiveTheme" in dashboard_body
    # The inlined ad-hoc precedence must be gone from Dashboard.
    assert "localStorage.getItem('theme')" in dashboard_body
    assert "urlTheme" not in dashboard_body

    tmpl = _read(SETTINGS_TEMPLATE)
    # Rough body extraction — the inline script has `function applyTheme()`.
    settings_apply_start = tmpl.find("function applyTheme()")
    assert settings_apply_start != -1, "applyTheme not found in settings.html"
    settings_apply_end = tmpl.find("\n        }", settings_apply_start)
    settings_apply = tmpl[settings_apply_start:settings_apply_end]
    assert "window.embeddedNav.resolveEffectiveTheme" in settings_apply
    # The old inlined system-only fallback must be gone.
    assert "storedTheme === 'system'" not in settings_apply


SETTINGS_FORM_CONTROLS_JS = (
    ROOT / "src" / "issue_orchestrator" / "static" / "js" / "settings_form_controls.js"
)


def test_settings_form_dispatches_cover_the_classifier_kind_set() -> None:
    """Template render and JS collect must cover exactly the closed control
    kind set owned by classify_form_control().

    Regression for "nits_by_agent: Input should be a valid dictionary": the
    template's catch-all `else -> text input` silently mis-rendered the
    first dict-typed registry field, so every settings save posted a
    Python-repr string that strict POST validation rejected. The catch-all
    is gone; a kind missing from either dispatch must fail HERE.
    """
    from issue_orchestrator.infra.settings_schema import FORM_CONTROL_KINDS

    tmpl = _read(SETTINGS_TEMPLATE)
    js = _read(SETTINGS_FORM_CONTROLS_JS)
    for kind in FORM_CONTROL_KINDS:
        assert f"control.kind == '{kind}'" in tmpl, (
            f"settings.html has no render branch for control kind {kind!r}"
        )
        assert f"{kind}: (el)" in js, (
            f"settings_form_controls.js has no collector for control kind {kind!r}"
        )
    # The template must fail loudly on an unknown kind, never fall back to
    # a text input; the JS collector throws via the dispatch lookup.
    assert "unsupported_settings_control_kind(control.kind)" in tmpl
    assert "Unsupported settings control kind" in js
    # The form must not re-interpret the JSON schema client-side.
    assert "SCHEMA_FIELDS" not in tmpl
    assert "anyOf" not in tmpl


def test_all_dashboard_js_node_tests_pass() -> None:
    """Run every tests/js/*.test.js file via node --test in one invocation.

    Centralized auto-discovery, on purpose: previously each new JS test had
    to be hand-listed in a pytest wrapper, which led to a silent gap where
    `tests/js/*.test.js` files could land on disk and never run in CI (the
    PR #6274 review caught this for `validation_dialog_render.test.js`,
    but the same was already true for several pre-existing files —
    `compact_card_state`, `expanded_column_state`, `issue_row_state`,
    `ui_action_contract`, `e2e_run_view_actions`).

    Add a new file to tests/js/, it runs in CI. No further wiring.
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    assert node, "node runtime is required to validate JS test files"
    js_test_dir = ROOT / "tests" / "js"
    assert js_test_dir.exists(), f"JS test dir missing: {js_test_dir}"
    test_files = sorted(js_test_dir.glob("*.test.js"))
    assert test_files, f"no JS tests found in {js_test_dir}"

    result = subprocess.run(
        [node, "--test", *[str(p) for p in test_files]],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0, (
        f"node --test failed (exit {result.returncode})\n"
        f"ran: {[p.name for p in test_files]}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_dashboard_js_node_test_sources_present() -> None:
    """Source-tree guard: the JS files the node tests depend on must exist.

    The test_all_dashboard_js_node_tests_pass runner above will fail loudly
    if these go missing, but a focused existence check produces a clearer
    failure message ("shared helper missing: …") than a node stack trace.
    """
    assert THEME_RESOLUTION_JS.exists(), f"theme resolver missing: {THEME_RESOLUTION_JS}"
    assert EMBEDDED_NAV_JS.exists(), f"embedded nav helper missing: {EMBEDDED_NAV_JS}"
    assert EMBEDDED_NAV_TEST_JS.exists(), f"embedded nav test missing: {EMBEDDED_NAV_TEST_JS}"
    assert DASHBOARD_BOOT_JS.exists(), f"dashboard boot helper missing: {DASHBOARD_BOOT_JS}"
    assert BROWSER_AUTH_JS.exists(), f"browser auth helper missing: {BROWSER_AUTH_JS}"


def test_journey_cycle_labels_use_run_local_numbering() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "_defaultIssueLifecycleCycleLabel")
    assert "cycle.cycle_in_run || cycle.cycle || cycle.cycle_number || (ctx.cycleIndex + 1)" in body


def test_journey_renders_server_supplied_scratch_run_and_cycle_labels() -> None:
    js = _read(DASHBOARD_JS)
    run_body = _function_body(js, "_defaultIssueLifecycleRunLabel")
    cycle_body = _function_body(js, "_defaultIssueLifecycleCycleLabel")
    summary_body = _function_body(js, "_renderIssueLifecycleCycleSummary")
    assert "run.run_label" in run_body
    assert "cycle.cycle_label" in cycle_body
    assert "escapeHtml(cycleLabel)" in summary_body


def test_journey_copy_uses_server_supplied_scratch_run_and_cycle_labels() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "copyJourneyTimeline")
    assert "run.run_label" in body
    assert "c.cycle_label" in body


def test_journey_renders_phase_group_headers() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "_renderIssueLifecycleCycleBody")
    assert "cycle && cycle.phase_groups" in body
    assert "journey-phase-header" in body


def test_journey_timeline_uses_native_disclosure_hierarchy() -> None:
    js = _read(DASHBOARD_JS)
    generic_src = (DASHBOARD_JS_DIR / "hierarchical_timeline.js").read_text(encoding="utf-8")
    plugin_src = (DASHBOARD_JS_DIR / "plugins" / "agent_context.js").read_text(encoding="utf-8")
    drawer_body = _function_body(js, "_renderJourneyRuns")
    plugin_body = _function_body(plugin_src, "renderIssueLifecycleTimeline")
    assert "renderIssueLifecycleTimeline(runs, {" in drawer_body
    assert "function renderIssueLifecycleTimeline" not in generic_src
    assert "function renderIssueLifecycleTimeline" in plugin_src
    assert "renderHierarchicalTimelineNode({" in plugin_body
    assert "className: 'journey-run unified-timeline-node'" in plugin_body
    assert "className: 'journey-cycle unified-timeline-node'" in plugin_body
    assert "summaryClassName: 'journey-cycle-header unified-timeline-summary'" in plugin_body
    assert "caretClassName: 'journey-cycle-toggle'" in plugin_body
    assert "_journeyDisclosureCommandAttr" not in drawer_body
    assert "sync_journey_disclosure" not in drawer_body
    assert 'ontoggle="runLifecycleCommandFromToggle(this)"' not in drawer_body
    assert 'onclick="toggleJourneyCycle' not in drawer_body


def test_issue_lifecycle_renderer_uses_plugin_owned_host_capabilities() -> None:
    generic_src = (DASHBOARD_JS_DIR / "hierarchical_timeline.js").read_text(encoding="utf-8")
    plugin_src = (DASHBOARD_JS_DIR / "plugins" / "agent_context.js").read_text(encoding="utf-8")
    drawer_src = (DASHBOARD_JS_DIR / "issue_detail_drawer.js").read_text(encoding="utf-8")

    assert "registerHierarchicalTimelineHostCapability" in generic_src
    assert "function renderIssueLifecycleTimeline" not in generic_src
    assert "function renderIssueLifecycleTimeline" in plugin_src
    assert "async function toggleValidationEventInline" in plugin_src
    assert "async function toggleValidationEventInline" not in drawer_src
    assert "function _handleCycleValidationBadgeClick" in plugin_src
    assert "function _handleCycleValidationBadgeClick" not in drawer_src
    assert "runHierarchicalTimelineHostCapability" in generic_src
    assert "'handleCycleValidationBadgeClick'" in plugin_src
    assert "runHierarchicalTimelineHostCapability('handleCycleValidationBadgeClick', this)" in drawer_src
    assert "formatJourneyHeaderTimestamp" not in plugin_src
    assert "formatJourneyStepTimestamp" not in plugin_src
    assert "renderTimelineEventActions" not in plugin_src
    assert "getHierarchicalTimelineHostCapability(name)" in plugin_src
    assert "registerHierarchicalTimelineHostCapabilities({" in drawer_src
    assert "_lazyDashboardFunction('renderCanonicalValidationViewer')" in drawer_src
    assert "_lazyDashboardFunction('renderValidationFailureActionSections')" in drawer_src


def test_journey_timeline_disclosure_uses_shared_renderer_not_sync_command() -> None:
    js = _read(DASHBOARD_JS)
    lifecycle_js = (DASHBOARD_JS_DIR / "lifecycle_commands.js").read_text(encoding="utf-8")
    toggle_body = _function_body(lifecycle_js, "runLifecycleCommandFromToggle")
    dispatcher_body = _function_body(lifecycle_js, "runLifecycleCommand")
    renderer_body = _function_body(js, "renderHierarchicalTimelineNode")
    css = _read_dashboard_css_bundle()
    assert "sync_journey_disclosure" not in js
    assert "syncJourneyDisclosureState" not in js
    assert "_journeyDisclosureCommandAttr" not in js
    assert "sync_journey_disclosure" not in lifecycle_js
    assert "detailsEl.open !== true" in toggle_body
    assert "detailsEl.dataset.loaded === '1'" in toggle_body
    assert "runLifecycleCommand(command, detailsEl)" in toggle_body
    assert 'ontoggle="runLifecycleCommandFromToggle(this)"' in renderer_body
    assert "_renderLifecycleCommandAttr(node.command)" in renderer_body
    assert ".journey-cycle-body.collapsed" not in css
    assert ".hierarchical-timeline-caret::before" in css
    assert 'details[open] > summary .hierarchical-timeline-caret::before' in css
    assert "Unsupported lifecycle command" in dispatcher_body


def test_toggle_journey_cycle_uses_native_details_state() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "toggleJourneyCycle")
    assert "const cycleNode = document.getElementById(cycleId);" in body
    assert "cycleNode.tagName !== 'DETAILS'" in body
    assert "cycleNode.open = !cycleNode.open;" in body
    assert "syncJourneyDisclosureState" not in body


def test_journey_artifact_affordance_is_semantic_button() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "_renderJourneyRuns")
    assert '<button type="button" class="journey-cycle-artifacts-btn"' in body
    assert 'aria-label="Open artifacts for ${escapeAttr(cycleLabel)}"' in body
    assert "event.preventDefault(); event.stopPropagation(); toggleArtifactPopover" in body


def test_journey_disclosure_rows_have_keyboard_focus_styles() -> None:
    js = _read(DASHBOARD_JS)
    step_body = _function_body(js, "_renderIssueLifecycleStep")
    css = _read_dashboard_css_bundle()
    assert ".journey-cycle-header:focus-visible" in css
    assert ".journey-cycle-header::-webkit-details-marker" in css
    assert ".journey-cycle-artifacts-btn:focus-visible" in css
    assert '<button type="button" class="journey-step-inline-toggle"' in step_body
    assert 'aria-controls="${escapeAttr(bodyId)}"' in js
    assert 'aria-expanded="false"' in js
    assert ".journey-step-inline-toggle:focus-visible" in css


def test_inline_validation_rows_reserve_timestamp_space_before_narrative() -> None:
    css = _read_dashboard_css_bundle()
    toggle_body = _last_css_rule_body(css, ".journey-step-inline-toggle")
    time_body = _last_css_rule_body(css, ".journey-step-inline-toggle .journey-time")
    main_body = _last_css_rule_body(css, ".journey-step-inline-toggle .journey-main")

    assert "flex-wrap: wrap;" in toggle_body
    assert "flex: 0 0 auto;" in time_body
    assert "max-width: 100%;" in time_body
    assert "flex: 1 1 220px;" in main_body
    assert "min-width: 0;" in main_body


def test_journey_disclosure_toggle_closes_open_timeline_menus() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "toggleJourneyCycle")
    assert "closeTimelineEventMenus();" in body


def test_review_feedback_modal_includes_review_comment_events_and_details() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openReviewFeedback")
    assert "review.comment_added" in body
    assert "evt.detail" in body
    assert "Open review comment on GitHub" in body


def test_review_feedback_modal_resolves_requested_issue_detail() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openReviewFeedback")
    assert "issueDetailData.issue_number === issueNumber" in body
    # Review feedback always fetches with ops view so review.comment_added
    # events (ops-only) are included regardless of the user's current view.
    assert "fetch(`/api/issue-detail/${issueNumber}?view=ops`)" in body


def test_review_feedback_modal_includes_exchange_round_events() -> None:
    """Review exchange round feedback must be rendered in the feedback modal."""
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openReviewFeedback")
    assert "review_exchange.round_completed" in body
    assert "reviewer_response_text" in body
    assert "Review exchange rounds" in body


def test_session_diagnostics_actions_use_primary_plus_visible_secondary_actions() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderGroupedDialogActions")
    assert "primaryTypes" in body
    assert "diag-secondary-actions" in body
    assert "Artifacts & Logs ▾" not in body
    assert "Issue-Scoped Orchestrator Log" in js
    assert "Copy Session Recording" in js
    assert "openSessionManifest(action.issue_number, action.run_dir || null)" in js


def test_diagnostics_action_errors_render_inline_in_modal() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openSessionManifest")
    assert "diagActionMessage" in body
    assert "showToast(data.error" not in body
    assert "reportActionError(" in js
    assert "'inline'" in js


def test_timeline_event_actions_use_primary_plus_overflow_menu() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderTimelineEventActions")
    assert "primaryTypes" in body
    assert "timeline-event-actions" in body
    assert "timeline-event-menu-trigger" in body
    assert "Event Details" in body
    assert "timeline-event-menu-items" in body
    assert 'role="menu"' in body
    assert "timeline-menu-item" in body
    # Nested "More" disclosure was removed in favor of a single popover.
    assert "timeline-more-menu" not in body
    assert "More ▾" not in body
    assert "_timelineActionShortLabel" in js


def test_timeline_events_pass_detail_context_to_action_menu() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderTimeline")
    actions_body = _function_body(js, "renderTimelineEventActions")
    assert "const detailIds = []" in body
    assert "renderTimelineEventActions(evt.actions || [], evt, detailIds)" in body
    assert "renderTimelineChildren(evt.children, detailIds)" in body
    assert "_clearTimelineEventDetails(container)" in body
    assert "openTimelineEventDetails" in js
    assert "show_event_details" in js
    assert "timelineEventDetailsById" in js
    assert "detail_id: _registerTimelineEventDetails(eventDetail, detailIds)" in actions_body
    assert "event: _timelineEventDetailsPayload(eventDetail)" not in actions_body
    assert "timeline-event-detail-overlay" in js
    assert "timeline-event-detail-overlay" in _read_dashboard_css_bundle()


def test_timeline_modal_delegate_handles_menu_items() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderTimeline")
    bind_body = _function_body(js, "bindTimelineEventActions")
    handler_body = _function_body(js, "handleTimelineEventActionsClick")
    assert "bindTimelineEventActions(container)" in body
    assert "container.addEventListener('click', handleTimelineEventActionsClick)" in bind_body
    assert ".timeline-action-btn, .timeline-menu-item" in handler_body
    assert "timeline-event-menu-trigger" in handler_body
    assert "toggleTimelineEventMenu(ownerMenu)" in handler_body
    assert "event.preventDefault()" in handler_body


def test_journey_action_delegate_handles_menu_items_and_closes_menus() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "_renderJourneyRuns")
    handler_body = _function_body(js, "handleTimelineEventActionsClick")
    assert "bindTimelineEventActions(container)" in body
    assert ".timeline-action-btn, .timeline-menu-item" in handler_body
    assert "toggleTimelineEventMenu(ownerMenu)" in handler_body
    assert "event.preventDefault()" in handler_body
    assert "closeTimelineEventMenus();" in handler_body


def test_canonical_viewer_mounts_bind_timeline_action_delegate_for_plugin_menus() -> None:
    js = _read(DASHBOARD_JS)
    row_loader_body = _function_body(js, "loadE2ERunIntoRow")
    validation_modal_body = _function_body(js, "openValidationFailure")
    assert "bindTimelineEventActions(body)" in row_loader_body
    assert "bindTimelineEventActions(dialogBody)" in validation_modal_body


def test_timeline_renders_issue_affordances_for_navigation() -> None:
    """E2E test events with issue_affordances render as clickable links to issue detail.

    Each affordance is a ``{issue_number, run_id}`` object; the anchor
    forwards both to ``openIssueDetail`` so the click is routed to the
    explicit ``/api/e2e-run/{run_id}/issue-detail/{N}`` endpoint.
    """
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderTimeline")
    assert "issue_affordances" in body
    assert "openIssueDetail" in body
    assert "timeline-issue-links" in body
    # The anchor must pass run_id to openIssueDetail so the dashboard
    # can route to the explicit e2e endpoint.
    assert "e2eRunId" in body


def test_open_issue_detail_routes_to_explicit_e2e_endpoint() -> None:
    """openIssueDetail(N, _, {e2eRunId}) hits the explicit e2e endpoint.

    We scan the whole file rather than using ``_function_body`` because
    ``openIssueDetail`` has an ``opts = {}`` default parameter whose
    braces confuse the helper's naive body extractor.
    """
    js = _read(DASHBOARD_JS)
    # Isolate the block from "async function openIssueDetail" to the next
    # top-level "async function " declaration so our assertions are
    # actually inside openIssueDetail.
    start = js.find("async function openIssueDetail(")
    assert start != -1, "openIssueDetail not found"
    next_fn = js.find("\nasync function ", start + 1)
    if next_fn == -1:
        next_fn = js.find("\nfunction ", start + 1)
    block = js[start:next_fn if next_fn != -1 else len(js)]

    assert "/api/e2e-run/" in block
    assert "issue-detail/" in block
    assert "e2eRunId" in block


def test_issue_detail_timeline_view_preserves_e2e_run_route() -> None:
    """Story/Ops/Debug/Raw switches must keep E2E issue detail run-scoped."""
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "setTimelineView")
    assert "currentIssueDetailE2ERunId" in body
    assert "/api/e2e-run/${e2eRunId}/issue-detail/${issueNumber}?view=${view}" in body
    assert "/api/issue-detail/${issueNumber}?view=${view}" in body
    assert "setTimelineView('raw')" in js


def test_issue_detail_drawer_stacks_above_modal_overlay() -> None:
    """Issue detail drawer must render ABOVE .modal-overlay (z-index 30).

    Without this, clicking an issue affordance from inside the E2E
    run drawer opens the issue drawer underneath the run modal — the
    drawer is technically visible but its interactive region is
    covered. We previously hacked around this by auto-dismissing the
    run modal; the CSS fix lets both drawers coexist so closing the
    issue drawer returns to the run drawer.
    """
    css = _read_dashboard_css_bundle()
    # Extract the .issue-detail-drawer rule block.
    start = css.find(".issue-detail-drawer {")
    assert start != -1, ".issue-detail-drawer rule not found"
    end = css.find("}", start)
    block = css[start:end]
    # Strip block comments so the regex can't match a z-index that
    # appears in documentation rather than the real declaration.
    import re

    block_no_comments = re.sub(r"/\*.*?\*/", "", block, flags=re.DOTALL)
    match = re.search(r"z-index:\s*(\d+)", block_no_comments)
    assert match, ".issue-detail-drawer has no z-index"
    drawer_z = int(match.group(1))
    assert drawer_z > 30, (
        f".issue-detail-drawer z-index must be > 30 (modal-overlay) "
        f"so it stacks above the e2e run modal; got {drawer_z}"
    )


def test_timeline_renders_affordance_label_with_hover_title() -> None:
    """Affordances render as ``label (N)`` with full branch in hover title.

    The matcher derives a compact label from the GitHub branch name
    (strip milestone prefix, collapse duplicate tokens, cap at 24
    chars). The renderer shows ``label (N)`` inline and puts the full
    branch name in the anchor's ``title`` so users can reclaim the
    untruncated detail on hover. Falls back to bare ``#N`` when no
    label is present (e.g. non-e2e contexts).
    """
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderTimeline")
    # Must read the new structured fields.
    assert "a.label" in body
    assert "a.branch_name" in body
    assert "title=" in body
    # Fallback to bare "#N" when label is absent.
    assert "`#${a.issue_number}`" in body


def test_timeline_surfaces_failure_longrepr_inline_on_failed_rows() -> None:
    """e2e.test_completed events with a longrepr must render the
    failure message inline so users can see WHY a test failed without
    leaving the run drawer. The renderer keys on ``evt.longrepr`` and
    ``evt.outcome === 'failed'`` / ``evt.status === 'error'`` to decide
    whether to emit the expandable detail block.

    Also pins the "terminal row only" restriction: the failure block
    must only render on ``e2e.test_completed`` events, not on the
    matching ``e2e.test_started`` row (both share the same nodeid and
    would otherwise display the same failure twice).
    """
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderTimeline")
    # The inline failure-detail block referenced by its dedicated class:
    assert "timeline-failure-detail" in body
    assert "timeline-failure-longrepr" in body
    # Triggered by the longrepr field — if the worker stops emitting
    # it or the endpoint stops promoting it, this assertion keeps the
    # failure-surfacing contract intact.
    assert "evt.longrepr" in body
    # And the renderer must look at the outcome/status to know when to
    # expand; otherwise every event would show a failure block.
    assert "'failed'" in body or '"failed"' in body
    assert "'error'" in body or '"error"' in body
    # Terminal-row gate — failure must NOT render on test_started rows.
    assert "'e2e.test_completed'" in body


def test_drawer_elevation_covers_all_modal_overlays_except_run_modal() -> None:
    """Every ``.modal-overlay`` opened from within the issue drawer
    must render above it.

    Issue #6334 retired ``#e2eDiagnosisModal``: the E2E run view is
    no longer a modal — it mounts inline in the runs-list row.  The
    drawer-vs-modal stacking contract therefore collapses to one
    rule: any ``.modal-overlay.visible`` sibling of a visible
    ``#issueDetailDrawer`` elevates above the drawer.  No exceptions
    needed.

    Without the default rule, new modals silently render behind the
    drawer (this was the reviewer-caught regression on the timeline
    Focus flow).  The companion guardrail
    ``test_css_bundle_drops_e2e_diagnosis_modal_rules`` enforces the
    modal-only rules are gone from the bundle, so we can rely on
    that absence here rather than asserting on it.
    """
    css = _read_dashboard_css_bundle()
    # Default: generic .modal-overlay elevates above the drawer.
    assert ":has(#issueDetailDrawer.visible) .modal-overlay.visible" in css, (
        "Missing DEFAULT elevation rule — generic .modal-overlay elements "
        "opened from the issue drawer (timeline Focus, session replay, "
        "validation failure, etc.) will render behind the drawer."
    )


def test_e2e_timeline_has_view_switcher() -> None:
    """The Story/Ops/Debug timeline view switcher lives in the
    Diagnostics row and emits typed ``switch_e2e_timeline_view``
    Commands (no inline ``switchE2ETimelineView()`` calls in HTML).
    """
    js = _read(DASHBOARD_JS)
    disclosure_body = _function_body(js, "renderRunDetailsDisclosure")
    assert "e2e-timeline-view-switcher" in disclosure_body
    assert "'switch_e2e_timeline_view'" in disclosure_body
    assert "'user'" in disclosure_body
    assert "'ops'" in disclosure_body
    assert "'debug'" in disclosure_body
    # No inline-onclick to the handler — buttons go through the
    # typed-Command dispatcher.
    assert "switchE2ETimelineView(" not in disclosure_body


def test_e2e_run_timeline_is_directly_addressable() -> None:
    """The Timeline entrypoint auto-expands the Diagnostics row.

    Issue #6334 re-pointed ``open_e2e_run`` at the inline runs-list
    driver (``expandE2ERunRow``).  ``openE2ERunTimeline`` still
    dispatches the typed Command with ``expand_run_details: true``;
    the dispatcher routes that through ``expandE2ERunRow`` which
    opens the matching row and then auto-opens the inner
    Diagnostics disclosure inside the row body.
    """
    js = _read(DASHBOARD_JS)
    legacy_entry = _function_body(js, "openE2ERunTimeline")
    expand_body = _function_body(js, "expandE2ERunRow")
    assert "function openE2ERunTimeline(runId)" in js
    # Routes through the typed Command pipeline.
    assert "runLifecycleCommand" in legacy_entry
    assert "'open_e2e_run'" in legacy_entry
    assert "expand_run_details: true" in legacy_entry
    # ``expandE2ERunRow`` honors the expand-run-details intent —
    # opens the row, then opens the inner .run-details-disclosure.
    # Issue #6334 round-2 swapped the id selector for a class selector
    # so two expanded rows don't collide.
    assert "options.expandRunDetails" in expand_body
    assert "'.run-details-disclosure'" in expand_body
    # The canonical viewer mount inside the row body uses the same
    # ``renderE2ETimeline`` call; ``loadE2ERunIntoRow`` (in
    # ``e2e_runs_list.js``) is the owner now.
    load_body = _function_body(js, "loadE2ERunIntoRow")
    assert "renderE2ETimeline(timelineContainer," in load_body


def test_e2e_run_timeline_renders_run_level_issue_links() -> None:
    """Run-level issue affordances open cycle-aware E2E issue
    timelines through the shared typed-Command pipeline
    (PR #6319 round 4): the affordance no longer wires its own
    ``openIssueTimeline(...)`` ``onclick`` — that was a second owner
    for the same UI command.  It now carries
    ``data-lifecycle-command`` with the typed shape
    ``{kind: 'open_issue_timeline', issue_number, scope_kind: 'e2e_run',
    e2e_run_id}`` and routes through
    ``runLifecycleCommandFromButton`` → ``runLifecycleCommand``
    → ``openIssueTimeline``.
    """
    js = _read(DASHBOARD_JS)
    timeline_body = _function_body(js, "renderE2ETimeline")
    affordance_body = _function_body(js, "renderE2EIssueTimelineAffordances")
    assert "tl.issue_affordances" in timeline_body
    assert "e2e-issue-timeline-affordances" in affordance_body
    # Typed command on the button, dispatched through the shared owner.
    assert "data-lifecycle-command=" in affordance_body
    assert "'open_issue_timeline'" in affordance_body
    assert "scope_kind: 'e2e_run'" in affordance_body
    assert "e2e_run_id: runId" in affordance_body
    assert "runLifecycleCommandFromButton(this)" in affordance_body
    # No second-owner direct onclick path.
    assert "openIssueTimeline(${issueNumber}" not in affordance_body
    css = _read_dashboard_css_bundle()
    assert ".e2e-issue-timeline-affordances" in css
    assert ".e2e-issue-timeline-btn" in css


def test_e2e_run_modal_uses_canonical_viewer_body() -> None:
    """Phase C of #6310 follow-up: the E2E run modal body is the
    canonical validation viewer.  The legacy filter pills / bulk-action
    bar / per-row tech_lead actions are gone; per-test orchestrator
    affordances live in the ``io.agent-context`` plugin block
    rendered inside the canonical viewer.
    """
    js = _read(DASHBOARD_JS)
    results_body = _function_body(js, "renderE2EResultsPanel")

    # The panel translates the orchestrator-categorized run-detail
    # payload to the JUnit-canonical viewer payload, then mounts the
    # shared viewer + the run-details footer.
    #
    # Issue #6334 round-2: the panel threads ``runId`` into the
    # untracked-failures banner + the run-details disclosure so
    # each typed Command they emit carries the right run_id.
    assert "e2eRunToCanonicalPayload(data)" in results_body
    assert "renderCanonicalValidationViewer(canonical)" in results_body
    assert "renderRunDetailsDisclosure(data, runId)" in results_body
    # Run-level summary chips + untracked-failures banner + the first-class
    # run-artifacts drill-down section are the run-scoped surfaces above the body.
    assert "_renderRunSummaryChips(data" in results_body
    assert "_renderUntrackedFailuresBanner(untrackedCount, runId)" in results_body
    # Issue #6593: collected artifacts render as first-class drill-downs
    # adjacent to the failure panel, not only inside the Diagnostics disclosure.
    assert "_renderRunArtifactsSection(data)" in results_body

    # The legacy "test-results-panel" container is gone — the new wrapper
    # is .e2e-canonical-panel.  test_results_list / test-results-list
    # references are also gone from the panel body.
    assert "test-results-panel" not in results_body
    assert 'class="test-results-list"' not in results_body
    # Filter pills + bulk-action bar are gone.
    assert "renderTestResultsFilters" not in results_body
    assert "bulk-action-bar" not in results_body
    assert "filterTestResults" not in results_body

    # After mounting, the E2E view calls the canonical-viewer ARIA
    # enhancer (matching the modal + drawer paths).  Issue #6334
    # moved the mount owner from ``renderUnifiedRunView`` (the
    # dropped modal renderer) to ``loadE2ERunIntoRow`` (the inline
    # row's lazy loader) — that's where the enhancer fires now.
    load_body = _function_body(js, "loadE2ERunIntoRow")
    assert "enhanceCanonicalValidationViewerAccessibility(cvvRoot)" in load_body

    # The translator is the seam where categories collapse onto JUnit
    # outcomes.  It must be reachable as a top-level symbol.
    assert "function e2eRunToCanonicalPayload" in js


# Legacy test_results_panel.js guardrails were deleted in Phase C
# (PR #6319 Blocker 2) — the panel module + its row-renderer helpers
# (``_renderTestRow``, ``_renderTestRowExpand``, ``_renderTestRowActions``,
# ``_renderHistoryCluster``, ``_renderTestResultPills``,
# ``toggleTestRowExpand``, ``_maybeLoadCapturedOutput``,
# ``_autoLoadVisibleCapturedOutput``, ``_e2eCapturedOutputUrl``) were
# removed from the production bundle.  The new E2E run modal mounts
# the canonical viewer instead, covered by
# ``test_e2e_run_modal_uses_canonical_viewer_body`` above and the
# Playwright smoke at ``tests/e2e_web/test_e2e_canonical_view.py``.


def test_e2e_run_evidence_disclosure_holds_metadata_and_timeline() -> None:
    """Diagnostics row carries runner/command/timeline diagnostics.

    Issue #6593 moved run artifacts out of this collapsed disclosure and into a
    first-class ``_renderRunArtifactsSection`` above the failure panel (covered
    by ``test_e2e_run_artifacts_section_is_first_class_drilldown``), so the
    disclosure no longer duplicates the artifact buttons.
    """
    js = _read(DASHBOARD_JS)
    disclosure_body = _function_body(js, "renderRunDetailsDisclosure")
    assert "<details" in disclosure_body
    # Issue #6334 round-2: disclosure uses CLASS not id (two
    # expanded rows have one each — id would collide).
    assert 'class="run-details-disclosure run-diagnostics-row"' in disclosure_body
    assert 'id="runDetailsDisclosure"' not in disclosure_body
    assert "rdd-grid" in disclosure_body
    assert "Runner" in disclosure_body
    assert "Command" in disclosure_body
    assert "Diagnostics" in disclosure_body
    assert "Run details &amp; artifacts" not in disclosure_body
    assert "Run evidence" not in disclosure_body
    assert "Timeline diagnostics" in disclosure_body
    # Same class-not-id rule for the timeline container.
    assert 'class="e2e-timeline-content"' in disclosure_body
    assert 'id="e2eTimelineContent"' not in disclosure_body
    # Artifacts are no longer rendered inside the diagnostics disclosure.
    assert "rdd-artifacts" not in disclosure_body
    assert "_runArtifactDescriptors" not in disclosure_body
    css = _read_dashboard_css_bundle()
    assert ".run-details-disclosure" in css
    assert ".rdd-summary-chip" in css


def test_e2e_run_artifacts_section_is_first_class_drilldown() -> None:
    """Issue #6593: collected artifacts are first-class drill-downs.

    The run-artifacts section renders the same typed openPath-backed artifact
    buttons the Diagnostics disclosure used to own, plus a per-state diagnostic
    note distinguishing "collected" / "globs matched nothing" / "not configured".
    """
    js = _read(DASHBOARD_JS)
    section_body = _function_body(js, "_renderRunArtifactsSection")
    note_body = _function_body(js, "_renderArtifactDiagnosticNote")
    diagnostic_body = _function_body(js, "_artifactDiagnostic")
    artifact_descriptor_body = _function_body(js, "_runArtifactDescriptors")
    artifact_button_body = _function_body(js, "_artifactButton")
    artifact_open_body = _function_body(js, "openE2EArtifactFromButton")

    # Section renders the run artifact descriptors as drill-down buttons.
    assert 'class="e2e-run-artifacts"' in section_body
    assert "_runArtifactDescriptors(data)" in section_body
    assert "_renderArtifactDescriptorButtons(descriptors)" in section_body
    assert "_renderArtifactDiagnosticNote(_artifactDiagnostic(data))" in section_body

    # Raw output is always available; artifact buttons route through openPath
    # via the host action handler (data-artifact-path contract, no inline
    # file:// onclick).
    assert "Raw Output" in artifact_descriptor_body
    assert "data-artifact-path" in artifact_button_body
    assert "openPath('" not in artifact_button_body
    assert "button.dataset.artifactPath" in artifact_open_body

    # The diagnostic note distinguishes the three collection states.
    assert "not_configured" in note_body
    assert "globs_matched_nothing" in note_body
    assert "not_configured" in diagnostic_body

    css = _read_dashboard_css_bundle()
    assert ".e2e-run-artifacts" in css
    # Colour is not the only status signal, and focus stays visible.
    assert ".e2e-run-artifacts .issue-action-btn:focus-visible" in css
    # Phase C: ``.test-results-headline`` / ``.test-results-filters``
    # / ``.trr-*`` CSS classes were specific to the deleted
    # ``test_results_panel.js`` panel and are no longer in the
    # bundle.  The run-details disclosure (this test's actual
    # subject) is unchanged.


# ``test_e2e_run_modal_actions_use_data_action_dispatch`` and
# ``test_e2e_result_category_owns_client_grouping`` were deleted in
# Phase C (PR #6319 Blocker 2) — both tested helpers that lived in
# the legacy ``test_results_panel.js`` and the per-row action
# dispatcher in ``e2e_run_view.js`` (``_e2eRowActionButton`` /
# ``runE2ERowActionFromButton`` / ``_testResultCategory`` /
# ``_testOutcomeState`` / ``_testFilterGroup`` / etc.), all of which
# are gone now.  The action contract for the new E2E view is the
# canonical viewer + ``io.agent-context`` plugin (typed-Command
# dispatch), covered by the new
# ``test_e2e_run_modal_uses_canonical_viewer_body`` above and the
# Playwright smoke ``test_e2e_canonical_view.py``.


def test_e2e_legacy_tech_lead_paths_are_absent_from_bundle() -> None:
    """Round-3 reviewer ask on PR #6319: the legacy run-details and
    test-detail UI in ``e2e_triage.js`` (and the ``currentRunDetails``
    state) must not survive in the shipped dashboard bundle.  The
    canonical ``showUnifiedRunView`` + ``unifiedRunData`` own the run
    modal end-to-end now; any vestige of the old categorized
    ``test-result-item`` / ``test-detail-view`` UI would render
    unstyled obsolete markup if it ever fired.
    """
    js = _read(DASHBOARD_JS)
    dead_symbols = [
        # Legacy run-details + test-detail entry points.
        "function showE2ERunDetailsLegacy",
        "function showRunTestDetail",
        # Per-test action helpers that only made sense inside the
        # deleted detail view.
        "function rerunTest",
        "function copyTestCommand",
        "function rerunCurrentTest",
        "function copyCurrentTestCommand",
        # Legacy state.
        "let currentRunDetails",
    ]
    for symbol in dead_symbols:
        assert symbol not in js, (
            f"Legacy e2e_triage.js symbol {symbol!r} leaked back into the bundle"
        )
    # Legacy CSS class names + URL contract that those functions rendered.
    legacy_markers = [
        # The old per-test row's class name.
        'class="test-result-item ',
        # The per-test drill-down container.
        'class="test-detail-view"',
        'class="test-detail-header"',
        'class="test-detail-info"',
        'class="test-detail-actions"',
        # The categorized-results endpoint that the legacy view called.
        "enhanced=false",
    ]
    for marker in legacy_markers:
        assert marker not in js, (
            f"Legacy e2e_triage.js marker {marker!r} leaked into the bundle"
        )
    # The ``currentRunDetails`` global is gone; the canonical
    # ``unifiedRunData`` owns the current-run id now.
    assert "currentRunDetails" not in js


def test_e2e_legacy_panel_selectors_are_absent_from_css_bundle() -> None:
    """Round-2 reviewer ask on PR #6319: the legacy panel CSS classes
    must not ship in the dashboard CSS bundle after the panel itself
    is gone.  Without this guardrail nothing prevents dead selectors
    from creeping back in — and the partial shipping of an obsolete
    UI is exactly what the reviewer flagged.

    The canonical viewer (``cvv-*`` classes) and the run-details
    disclosure (``run-details-*`` classes) own the live E2E run
    modal styling.  Everything below was scoped to the deleted
    ``test_results_panel.js`` and its row-action contract.
    """
    css = _read_dashboard_css_bundle()
    dead_selectors = [
        # filter pills + headline scaffold
        ".test-results-panel",
        ".test-results-headline",
        ".test-results-filters",
        ".test-results-list",
        ".trf-chip",
        # row + row internals
        ".trr-row",
        ".trr-row-main",
        ".trr-row-copy",
        ".trr-row-actions",
        ".trr-caret",
        ".trr-expand",
        ".trr-error-text",
        ".trr-expand-heading",
        ".trr-captured-output",
        ".trr-captured-channel",
        ".trr-captured-channel-label",
        ".trr-captured-text",
        ".trr-captured-status",
        ".trr-captured-error",
        ".trr-lifecycle",
        ".trr-lifecycle-heading",
        ".trr-lifecycle-cycles",
        ".trr-lifecycle-actions",
        # headline status accents
        ".trh-stat",
        ".trh-passed",
        ".trh-failed",
        ".trh-action",
        ".trh-warning",
        ".trh-skipped",
        ".trh-quarantined",
        # per-row content
        ".test-result-pill",
        ".test-result-pills",
        ".test-result-flaky-note",
        ".test-history-label",
        ".test-history-glyphs",
        ".test-history-flake",
        ".test-failure-summary",
        ".test-row-copy",
        ".test-suite",
        ".test-source",
        # per-row inline lifecycle chip
        ".e2e-lifecycle-chip",
    ]
    leaks: list[str] = []
    for selector in dead_selectors:
        # Match the selector at a position that's actually a CSS rule
        # (i.e. followed by ``{``, ``,``, ``:``, ``.``, ``>``, whitespace,
        # ``[``, or end-of-line) — so a class name appearing only inside
        # a comment block (the deletion-marker comments we left behind)
        # doesn't trip the guard.
        for line in css.splitlines():
            stripped = line.strip()
            if stripped.startswith(("/*", "//", "*")):
                continue
            if selector in line:
                leaks.append(f"{selector!r} on: {line.strip()}")
                break
    assert not leaks, (
        "Legacy test-results-panel CSS selectors leaked back into the "
        "dashboard bundle:\n  " + "\n  ".join(leaks)
    )


def test_e2e_header_badge_uses_failed_evidence_over_passed_status() -> None:
    """The E2E tab badge must not look healthy when parsed failed tests exist."""
    js = _read(DASHBOARD_JS)
    state_body = _function_body(js, "e2eBadgeStateFromStatus")
    update_body = _function_body(js, "updateE2EHeaderBadge")
    css = _read_dashboard_css_bundle()

    assert "failedTestCount > 0" in state_body
    assert "data?.needs_attention" in state_body
    assert "status === 'failed'" in state_body
    assert "status === 'passed'" in state_body
    assert "e2eLastStatusData" in js
    assert "...e2eLastStatusData" in update_body
    assert "badge.classList.remove('running', 'passed', 'failed', 'warning', 'idle')" in update_body
    assert ".tab-badge.failed" in css
    assert ".tab-badge.passed" in css


def test_dashboard_templates_expose_direct_timeline_affordances() -> None:
    """Issue rows still offer Timeline controls; the dashboard runs list
    renders inline via the typed RecentE2ERunsPayload (#6334).

    Issue #6334 dropped the SSR ``e2e-run-results-btn`` / ``card-focus``
    chip loop in dashboard.html — the runs list now mounts client-side
    from the typed payload embedded as inline JSON.  The
    ``open_run_command`` typed contract still lives on issue_row.html's
    View buttons.
    """
    dashboard = _read(DASHBOARD_TEMPLATE)
    issue_row = _read(ISSUE_ROW_TEMPLATE)
    # The dashboard's runs list mounts client-side from the typed payload.
    assert 'id="recentE2ERunsData" type="application/json"' in dashboard
    assert 'id="e2eRunsListRoot"' in dashboard
    # ``openE2ERunTimeline(run.e2e_run_id)`` is no longer a dashboard
    # inline-onclick; rows dispatch ``expand_e2e_run`` instead.
    assert "openE2ERunTimeline({{ run.e2e_run_id }})" not in dashboard
    assert ">Open run<" not in dashboard
    # No hand-built JSON for the typed Command kinds (regression guard).
    assert 'data-lifecycle-command=\'{"kind":"open_e2e_run"' not in dashboard
    assert 'data-lifecycle-command=\'{"kind":"expand_e2e_run"' not in dashboard
    # The "View Results" top-level button (latest run) is still in
    # dashboard.html via the e2e_summary chip — that's not in the
    # runs-list loop and stayed put.
    assert 'data-action="show-latest-e2e-run-results"' in dashboard
    # Issue rows continue to expose direct Timeline controls + the
    # typed-Command View button (issue_row.html keeps the chip shape
    # the runs list no longer needs).
    assert (
        'data-lifecycle-command="{{ issue.open_run_command | tojson | forceescape }}"'
        in issue_row
    )
    assert "runLifecycleCommandFromButton(this)" in issue_row
    assert "openE2ERunTimeline({{ issue.e2e_run_id }})" in issue_row
    assert "openIssueTimeline({{ issue.issue_number }}, this); event.stopPropagation();" in issue_row
    assert "openTimelineModal({{ issue.issue_number }})" not in issue_row


def test_e2e_latest_results_affordance_uses_formatted_run_modal() -> None:
    """Latest/passed E2E runs should open the formatted run view via the typed Command.

    PR #6329 reviewer Blocker 2: ``showLatestE2ERunResults`` no
    longer calls ``showUnifiedRunView`` directly — it dispatches
    the typed ``open_e2e_run`` Command so every "open E2E run"
    entrypoint has a single owner.  The dispatcher still calls
    ``showUnifiedRunView`` internally; this function just stops
    bypassing the pipeline.
    """
    dashboard = _read(DASHBOARD_TEMPLATE)
    js = _read(DASHBOARD_JS)
    latest_body = _function_body(js, "showLatestE2ERunResults")

    assert "Last Run Diagnosis" not in dashboard
    assert 'data-action="show-latest-e2e-run-results"' in dashboard
    assert "showE2EDiagnosis()" not in dashboard
    # Now goes through the typed Command pipeline.
    assert "runLifecycleCommand" in latest_body
    assert "'open_e2e_run'" in latest_body
    # No direct ``showUnifiedRunView(runId)`` bypass.
    assert "showUnifiedRunView(runId)" not in latest_body
    assert "showE2EDiagnosis" not in latest_body


def test_issue_cards_have_cycle_aware_timeline_affordance() -> None:
    """Issue card timeline buttons should open the cycle-aware drawer."""
    js = _read(DASHBOARD_JS)
    dashboard = _read(DASHBOARD_TEMPLATE)
    css = _read_dashboard_css_bundle()
    assert "card-timeline-btn" in js
    assert "Open timeline for issue #${n}" in js
    assert "function openIssueTimeline(issueNumber, triggerEl = null, opts = {})" in js
    assert "openIssueTimeline(${n}, this);event.stopPropagation();" in js
    assert "card-detail-chevron" not in js
    assert "issueDetailJourney" in js
    assert "card-timeline-btn" in dashboard
    assert "Open timeline for issue #{{ card.issue_number }}" in dashboard
    assert "openIssueTimeline({{ card.issue_number }}, this);event.stopPropagation();" in dashboard
    assert "card-detail-chevron" not in dashboard
    assert ".card-timeline-btn" in css
    assert ".card-detail-chevron" not in css


def test_server_rendered_issue_cards_render_summary_once() -> None:
    """Server-rendered cards should match the client renderer's summary hierarchy."""
    dashboard = _read(DASHBOARD_TEMPLATE)
    web_templates = _read(ROOT / "src" / "issue_orchestrator" / "entrypoints" / "web_templates.py")
    js = _read(DASHBOARD_JS)
    assert "{% elif card.summary %}" not in dashboard
    assert '{% if card.queue_wait_reason %}' in dashboard
    assert '<div class="card-line card-muted">{{ card.summary }}</div>' in dashboard
    assert 'select_autoescape(["html"])' in web_templates
    assert 'escapeHtml(String(card.summary))' in js


def test_e2e_timeline_view_switcher_refetches() -> None:
    """View switcher re-fetches its row's timeline.

    Row-targeting policy is centralized in ``resolveRowCommandContext``;
    the handler doesn't duplicate the ``closest('details.e2e-run-row')``
    lookup or the row-scoped DOM queries.
    """
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "switchE2ETimelineView")
    assert "function switchE2ETimelineView(runId, view, triggerEl)" in js
    assert "_fetchE2ERunDetail(ctx.runId, view)" in body
    assert "renderE2ETimeline" in body
    # The handler routes through the single owner abstraction.
    assert "resolveRowCommandContext(runId, triggerEl)" in body
    # No duplicate row-targeting policy.
    assert "triggerEl.closest('details.e2e-run-row')" not in body
    assert "row.querySelector('.e2e-timeline-content')" not in body
    # No module-level ``unifiedRunData`` singleton.
    assert "unifiedRunData" not in body


def test_row_command_context_is_the_single_owner_of_row_targeting() -> None:
    """``resolveRowCommandContext`` is the only place row-targeting
    policy lives.

    The two row-mounted handlers (``switchE2ETimelineView``,
    ``createIssuesForUntriaged``) MUST route through it.  A future
    refactor that resurrects ``triggerEl.closest('details.e2e-run-row')``
    or duplicate ``row.querySelector('.e2e-timeline-content')`` calls
    outside this function reintroduces scattered ownership.
    """
    js = _read(DASHBOARD_JS)
    # The abstraction exists.
    assert "function resolveRowCommandContext(runId, triggerEl)" in js
    # The abstraction validates: strict-number + integer + ge=1 +
    # trigger resolves to a row + row dataset agrees with the typed
    # Command's run_id.  The strict-Number gate mirrors the typed
    # Pydantic Command's ``strict=True`` invariant: ``"88"`` and
    # ``true`` must reject even though ``Number()`` would coerce
    # them.
    ctx_body = _function_body(js, "resolveRowCommandContext")
    assert "typeof runId !== 'number'" in ctx_body
    assert "Number.isInteger(runId)" in ctx_body
    assert "runId <= 0" in ctx_body
    assert "triggerEl.closest('details.e2e-run-row')" in ctx_body
    assert "rowRunId !== runId" in ctx_body
    # Frozen context shape — handlers can't mutate it.
    assert "Object.freeze" in ctx_body
    # Both handlers route through the abstraction.
    for fn_name in ("switchE2ETimelineView", "createIssuesForUntriaged"):
        body = _function_body(js, fn_name)
        assert "resolveRowCommandContext(runId, triggerEl)" in body, (
            f"{fn_name} must route row resolution through resolveRowCommandContext"
        )
        # No duplicate row-targeting policy in either handler.
        assert "triggerEl.closest" not in body, (
            f"{fn_name} must not duplicate triggerEl.closest lookup"
        )


def test_dashboard_and_e2e_ui_preserve_lifecycle_payloads() -> None:
    js = _read(DASHBOARD_JS)
    e2e_body = _function_body(js, "normalizeE2ETimelineData")
    render_e2e_body = _function_body(js, "renderE2ETimeline")
    issue_detail_body = _function_body(js, "renderIssueDetail")
    dataset_body = _function_body(js, "applyLifecycleDataset")

    assert "lifecycle" in e2e_body
    assert "applyLifecycleDataset(container, tl.lifecycle)" in render_e2e_body
    assert "applyLifecycleDataset(issueDetailDrawer, d.lifecycle || null)" in issue_detail_body
    assert "LIFECYCLE_DATASET_KEYS" in js
    assert "window.LIFECYCLE_DATASET_KEYS = LIFECYCLE_DATASET_KEYS" in js
    assert "dataset[LIFECYCLE_DATASET_KEYS.kind]" in dataset_body
    assert "dataset[LIFECYCLE_DATASET_KEYS.iterations]" in dataset_body
    assert "throw new Error('Lifecycle payload missing kind')" in dataset_body


def test_timeline_children_render_with_full_treatment() -> None:
    """renderTimelineChildren renders phase groups, actions, artifacts, and detail."""
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderTimelineChildren")
    # Phase grouping (same as main timeline)
    assert "formatPhaseLabel" in body
    assert "timeline-group-header" in body
    assert "timeline-group-body" in body
    # Full event rendering (not flat list)
    assert "timeline-event-header" in body
    assert "renderTimelineArtifacts" in body
    assert "renderTimelineEventActions" in body
    assert "timeline-summary" in body
    assert "timeline-detail" in body
    # Collapsible wrapper
    assert "timeline-children" in body
    assert "orchestrator event" in body


def test_review_feedback_modal_can_filter_to_specific_timeline_entry() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openReviewFeedback")
    assert "context = null" in js
    assert "_matchesReviewFeedbackContext" in js
    assert "_matchesReviewFeedbackContext(e, context)" in body


def test_journey_renders_local_timestamps_from_raw_event_times() -> None:
    js = _read(DASHBOARD_JS)
    header_body = _function_body(js, "_formatIssueLifecycleHeaderTimestamp")
    step_timestamp_body = _function_body(js, "_formatIssueLifecycleStepTimestamp")
    run_summary_body = _function_body(js, "_renderIssueLifecycleRunSummary")
    cycle_summary_body = _function_body(js, "_renderIssueLifecycleCycleSummary")
    step_body = _function_body(js, "_renderIssueLifecycleStep")
    assert "_issueLifecycleHostFunction(options, 'formatHeaderTimestamp')" in header_body
    assert "_issueLifecycleHostFunction(options, 'formatStepTimestamp')" in step_timestamp_body
    assert "_formatIssueLifecycleHeaderTimestamp(run.timestamp" in run_summary_body
    assert "_formatIssueLifecycleHeaderTimestamp(cycle.timestamp" in cycle_summary_body
    assert "_formatIssueLifecycleStepTimestamp(" in step_body


def test_dashboard_uses_single_local_timestamp_formatter() -> None:
    chunks = list(DASHBOARD_JS_CHUNKS)
    assert chunks[0] == "timestamp_formatting.js"
    timestamp_src = _read(DASHBOARD_JS_DIR / "timestamp_formatting.js")
    assert "function formatLocalTimestamp(" in timestamp_src
    assert "function formatTimestamp(" in timestamp_src
    assert "function formatJourneyHeaderTimestamp(" in timestamp_src
    assert "function formatJourneyStepTimestamp(" in timestamp_src
    assert "function formatDashboardTimestamps(" in timestamp_src

    for chunk in DASHBOARD_JS_CHUNKS:
        if chunk == "timestamp_formatting.js":
            continue
        src = _read(DASHBOARD_JS_DIR / chunk)
        assert "function formatTimestamp(" not in src, (
            f"{chunk} must not define a competing timestamp formatter"
        )
        assert "function formatJourneyHeaderTimestamp(" not in src
        assert "function formatJourneyStepTimestamp(" not in src
        assert "toLocaleTimeString(" not in src, (
            f"{chunk} must use formatTimestamp(), not ad hoc time-only rendering"
        )
        assert not re.search(r"new Date\([^)]*\)\.toLocaleString\(", src), (
            f"{chunk} must use formatTimestamp(), not raw Date#toLocaleString"
        )

    session_dialogs = _read(DASHBOARD_JS_DIR / "session_dialogs.js")
    e2e_runs_list = _read(DASHBOARD_JS_DIR / "e2e_runs_list.js")
    e2e_runtime = _read(DASHBOARD_JS_DIR / "e2e_runtime.js")
    timeline_src = _read(DASHBOARD_JS_DIR / "timeline.js")
    core = _read(DASHBOARD_JS_DIR / "core.js")
    dashboard_template = _read(DASHBOARD_TEMPLATE)
    issue_row = _read(ISSUE_ROW_TEMPLATE)
    dashboard_view_model = _read(DASHBOARD_VIEW_MODEL)
    dashboard_e2e = _read(DASHBOARD_E2E_VIEW_MODEL)
    assert "row.value_kind === 'timestamp'" in session_dialogs
    assert "formatTimestamp(rawValue, rawValue)" in session_dialogs
    assert "rowName" not in session_dialogs
    assert "['started', 'ended', 'retention expires']" not in session_dialogs
    assert "detail_value_kinds" in timeline_src
    assert "String(data.started_at || '-')" not in session_dialogs
    assert "formatTimestamp(summary.started_at)" in e2e_runs_list
    assert "_formatRelative(" not in e2e_runs_list
    assert "formatE2ELastRunLabel(" in e2e_runtime
    assert "formatTimestamp(run.started_at)" in e2e_runtime
    assert "renderE2ELastRunTimestamp(" in e2e_runtime
    assert "formatDashboardTimestamps(list)" in core
    assert 'id="e2eLastRunLabel"' in dashboard_template
    assert "data-started-at=" in dashboard_template
    assert "e2e-last-run-time" in dashboard_template
    assert "data-dashboard-timestamp=" in dashboard_template
    assert "e2e_summary.last_run_label" not in dashboard_template
    assert "data-dashboard-timestamp=" in issue_row
    assert "Loading..." not in issue_row
    assert "_history_time_fields(" in dashboard_view_model
    assert "_format_history_time" not in dashboard_view_model
    assert " min @ " not in dashboard_view_model
    assert '"time_is_timestamp"' in dashboard_view_model
    assert "_relative_time" not in dashboard_e2e
    assert '"relative_time"' not in dashboard_e2e


def test_journey_layout_uses_content_column_for_actions_and_detail() -> None:
    css = _read_dashboard_css_bundle()
    assert ".journey-main" in css
    assert ".journey-summary-row" in css
    assert "grid-template-columns: minmax(72px, max-content) minmax(0, 1fr)" in css


def test_toggle_journey_cycle_closes_open_timeline_menus() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "toggleJourneyCycle")
    assert "closeTimelineEventMenus();" in body


def test_journey_empty_state_uses_diagnostic_when_timeline_missing() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "_renderJourneyRuns")
    assert "timelineDiagnostic" in body
    assert "expected_history_missing" in body
    assert "Timeline data missing" in body
    assert "Expected timeline store" in body


def test_journey_empty_state_falls_back_to_no_activity_when_no_diagnostic() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "_renderJourneyRuns")
    assert "No activity recorded." in body


def test_expanded_cards_list_uses_start_aligned_grid() -> None:
    """Cards in expanded view should not stretch to fill the container."""
    css = _read_dashboard_css_bundle()
    # Find the .expanded-cards-list rule
    start = css.find(".expanded-cards-list {")
    assert start != -1, ".expanded-cards-list rule not found in dashboard.css"
    end = css.find("}", start)
    rule = css[start : end + 1]
    assert "align-content: start" in rule
    assert "grid-auto-rows: max-content" in rule


def test_expanded_column_state_handles_running_column() -> None:
    """expanded_column_state.js must map 'running' to active_items."""
    js = Path(
        ROOT / "src" / "issue_orchestrator" / "static" / "js" / "expanded_column_state.js"
    ).read_text(encoding="utf-8")
    assert "columnId === 'running'" in js
    assert "active_items" in js


def test_render_compact_cards_removes_empty_placeholder_when_items_exist() -> None:
    """renderCompactCards must remove .column-empty and .skeleton-card when items arrive."""
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderCompactCards")
    # The function must remove placeholders before inserting real cards
    assert ".column-empty" in body
    assert ".skeleton-card" in body
    assert "remove()" in body


def test_e2e_run_note_rendered_in_template() -> None:
    """The e2e runs list must surface ``run.note`` so fixture errors
    (e.g. GH activity guard) are visible in the list.

    Issue #6334 moved the runs list from the SSR Jinja loop to the
    client-side ``e2e_runs_list.js`` chunk.  The renderer reads
    ``summary.note`` from the typed ``RecentE2ERunSummary`` payload
    and emits a ``.e2e-run-row-note`` block; the field is preserved
    on the typed model (``RecentE2ERunSummary.note: str | None``)
    and threaded through the builder.
    """
    chunk = (DASHBOARD_JS_DIR / "e2e_runs_list.js").read_text(encoding="utf-8")
    assert "summary.note" in chunk, (
        "e2e_runs_list.js must render summary.note so fixture errors "
        "remain visible in the list (#6334)"
    )
    assert "e2e-run-row-note" in chunk, (
        "e2e_runs_list.js must use the .e2e-run-row-note class for styling"
    )


def test_e2e_run_note_has_error_styling() -> None:
    """The runs-list note class must have visible styling.

    Issue #6334 renamed the class to ``.e2e-run-row-note`` along
    with the row-based layout.
    """
    css = _read_dashboard_css_bundle()
    assert ".e2e-run-row-note" in css, (
        "Dashboard CSS must define .e2e-run-row-note for fixture error display"
    )


def test_e2e_warning_badge_state_in_css() -> None:
    """The tab-badge must have a warning state style."""
    css = _read_dashboard_css_bundle()
    assert ".tab-badge.warning" in css, (
        "Dashboard CSS must style .tab-badge.warning for retry-needed runs"
    )


def test_e2e_header_updater_handles_warning_status() -> None:
    """The live header badge updater must recognize 'warning' status."""
    js = _read(DASHBOARD_JS)
    assert "'warning'" in js, (
        "Dashboard JS must handle 'warning' run status in the header updater"
    )
    assert "badge.classList.remove('running', 'passed', 'failed', 'warning')" in js or \
           "'warning'" in js, (
        "Badge classList.remove must include 'warning' to avoid stale classes"
    )


def test_flaky_analysis_uses_modal_for_all_outcomes() -> None:
    """showFlakyTestsList must use openModal for errors, empty, and success."""
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "showFlakyTestsList")
    # All outcomes must use openModal — not showToast or alert
    assert "showToast" not in body, (
        "showFlakyTestsList must not use showToast — toasts are too easy to miss"
    )
    assert "alert(" not in body, (
        "showFlakyTestsList must use openModal, not alert()"
    )
    assert "openModal(" in body, (
        "showFlakyTestsList must use openModal for all user-visible feedback"
    )


def test_flaky_analysis_parses_response_as_text_first() -> None:
    """showFlakyTestsList must read response as text then JSON.parse,
    not res.json() which consumes the body and prevents fallback."""
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "showFlakyTestsList")
    assert "res.text()" in body, (
        "Must read body as text first so non-JSON responses can be displayed"
    )
    assert "JSON.parse(" in body, (
        "Must parse text as JSON rather than using res.json()"
    )


def test_show_unified_run_view_has_a_single_owner() -> None:
    """No direct callers of ``showUnifiedRunView`` survive.

    Issue #6334 retired ``showUnifiedRunView`` (the modal-driver) when
    it dropped ``#e2eDiagnosisModal``.  Open-E2E-run navigation now
    routes through ``expandE2ERunRow`` (the inline runs-list driver)
    via the typed ``open_e2e_run`` Command — same single-owner
    contract as before, new owner.

    This guardrail walks every JS file under
    ``static/js/dashboard/`` and asserts that no live code path
    references ``showUnifiedRunView(``.  Comments are ignored so
    historical deletion notes don't trip the guard.  Any new direct
    caller — or a re-added definition — fires this test.
    """
    import re

    direct_callers: list[tuple[str, int, str]] = []

    for js_path in sorted(DASHBOARD_JS_DIR.rglob("*.js")):
        relpath = js_path.relative_to(DASHBOARD_JS_DIR).as_posix()
        text = js_path.read_text(encoding="utf-8")
        # Strip block comments so deletion notes describing the
        # removed function don't trip the guard.
        text_no_block = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        for lineno, line in enumerate(text_no_block.splitlines(), start=1):
            stripped = re.sub(r"//.*$", "", line)
            if "showUnifiedRunView(" in stripped:
                direct_callers.append((relpath, lineno, line.strip()))

    assert direct_callers == [], (
        "showUnifiedRunView() must only be called from the typed-Command "
        "dispatcher in lifecycle_commands.js (and its definition in "
        "e2e_run_view.js).  Found direct callers:\n  "
        + "\n  ".join(f"{f}:{ln}: {src}" for f, ln, src in direct_callers)
    )


def test_no_hand_built_open_e2e_run_json_anywhere() -> None:
    """The legacy hand-built ``data-lifecycle-command`` JSON shape
    for ``open_e2e_run`` is gone everywhere.

    PR #6329: every ``open_run_command`` payload renders through
    the view-model's typed dict (``OpenE2ERunCommand.model_dump()``)
    and the Jinja ``| tojson | forceescape`` filter chain.  No
    template or JS file should contain a literal
    ``{"kind":"open_e2e_run",...}`` string.
    """
    import re

    hand_built_pattern = re.compile(
        r'(?:data-lifecycle-command\s*=\s*[\'"]|\{)\s*\{?\s*"kind"\s*:\s*"open_e2e_run"'
    )
    # Search the production template + JS surfaces.  Tests and
    # mockups may legitimately reference the shape (e.g. our
    # JS-vm cheap-integration test builds a representative chip
    # to extract from), so the guard scopes to production files.
    production_files = [
        ROOT / "src" / "issue_orchestrator" / "templates" / "dashboard.html",
        ROOT / "src" / "issue_orchestrator" / "templates" / "issue_row.html",
    ]
    production_files.extend(sorted(DASHBOARD_JS_DIR.rglob("*.js")))

    hits: list[tuple[str, int, str]] = []
    for path in production_files:
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if hand_built_pattern.search(line):
                hits.append((path.as_posix(), lineno, line.strip()))

    assert hits == [], (
        "No production file may hand-build the open_e2e_run JSON payload "
        "(the view model + tojson filter own the serialization).  "
        "Found:\n  " + "\n  ".join(f"{f}:{ln}: {src}" for f, ln, src in hits)
    )


def test_inline_agent_attempts_expander_is_wired_through_typed_command_pipeline() -> None:
    """Issue #6322 follow-up: the linked-failure drill-in inside the
    canonical viewer is the inline ``▸ Attempts on issue #N`` expander
    in ``inline_agent_attempts.js`` — and it routes through the same
    typed-Command pipeline as every other affordance in the
    dashboard.  Not the legacy ``open_issue_timeline`` teleport.  Not
    a bespoke ``ontoggle="_handleAgentAttemptsToggle(this)"`` handler
    on each ``<details>``.  One owner, one dispatcher, one
    Pydantic-validated payload.

    This guardrail pins five contracts so a future refactor can't
    silently regress:

      1. ``inline_agent_attempts.js`` ships in the dashboard JS bundle
         and is loaded BEFORE ``plugins/agent_context.js`` — the
         plugin checks for ``renderInlineAgentAttemptsExpander`` at
         render time, so the symbol must be in scope.
      2. The plugin renders the expander (no legacy
         ``open_issue_timeline`` Command for linked-failure drill-in).
      3. The expander emits a typed
         ``open_inline_agent_attempts`` Command (the
         ``OpenInlineAgentAttemptsCommand`` Pydantic shape) on the
         ``<details>`` element, routed via
         ``ontoggle="runLifecycleCommandFromToggle(this)"``.
      4. The dispatcher in ``lifecycle_commands.js`` has the matching
         branch.
      5. The lazy-fetch helper hits
         ``/api/issue-detail/{n}?view=ops`` — the only contract the
         backend ops-view payload satisfies.
      6. The expanded plugin body renders through the
         ``io.agent-context`` plugin's ``renderIssueLifecycleTimeline`` so it
         shares dashboard timeline rows, menus, and validation event hosts.
    """
    assert "inline_agent_attempts.js" in DASHBOARD_JS_CHUNKS, (
        "inline_agent_attempts.js must be in DASHBOARD_JS_CHUNKS"
    )
    chunks = list(DASHBOARD_JS_CHUNKS)
    shared_idx = chunks.index("hierarchical_timeline.js")
    inline_idx = chunks.index("inline_agent_attempts.js")
    plugin_idx = chunks.index("plugins/agent_context.js")
    assert shared_idx < inline_idx, (
        "hierarchical_timeline.js must load BEFORE inline_agent_attempts.js — "
        "the inline expander depends on the shared row shell being available"
    )
    assert inline_idx < plugin_idx, (
        "inline_agent_attempts.js must load BEFORE plugins/agent_context.js — "
        "the plugin's render-time check for renderInlineAgentAttemptsExpander "
        f"will see undefined otherwise.  Got order: {chunks}"
    )
    assert plugin_idx < chunks.index("issue_detail_drawer.js"), (
        "plugins/agent_context.js must load BEFORE issue_detail_drawer.js — "
        "the drawer mounts the plugin-owned renderIssueLifecycleTimeline"
    )

    plugin_src = (
        DASHBOARD_JS_DIR / "plugins" / "agent_context.js"
    ).read_text(encoding="utf-8")
    assert "renderInlineAgentAttemptsExpander(issueNumber)" in plugin_src, (
        "agent_context.js must invoke renderInlineAgentAttemptsExpander() "
        "for the inline drill-in"
    )
    assert "open_issue_timeline" not in plugin_src, (
        "agent_context.js must NOT emit an open_issue_timeline typed Command "
        "(issue #6322: linked-failure drill-in is the inline expander)"
    )
    assert "Open issue drawer" not in plugin_src, (
        "Legacy 'Open issue drawer' affordance must not survive in agent_context.js"
    )

    inline_src = (DASHBOARD_JS_DIR / "inline_agent_attempts.js").read_text(encoding="utf-8")
    # Typed-Command pipeline.
    assert (
        'ontoggle="runLifecycleCommandFromToggle(this)"' in inline_src
    ), "expander must dispatch via runLifecycleCommandFromToggle (shared pipeline)"
    assert (
        "'open_inline_agent_attempts'" in inline_src
    ), "expander must emit the typed Command kind 'open_inline_agent_attempts'"
    assert (
        "renderIssueLifecycleTimeline(reversed, {" in inline_src
    ), "plugin body must use the plugin-owned issue lifecycle renderer"
    assert (
        "agent-context-cycle\">" not in inline_src
    ), "plugin must not keep a bespoke cycle renderer parallel to the dashboard timeline"
    # Legacy bespoke handler is gone.
    assert (
        "_handleAgentAttemptsToggle" not in inline_src
    ), "legacy _handleAgentAttemptsToggle bespoke handler must not survive"
    # Public surface.
    assert (
        "window.renderInlineAgentAttemptsExpander" in inline_src
    ), "expander helper must be published on window"
    assert (
        "window.loadInlineAgentAttempts" in inline_src
    ), "typed-Command handler must be published on window"
    # Lazy-fetch URL.
    assert (
        "/api/issue-detail/${issueNumber}?view=ops" in inline_src
    ), "lazy-fetch must hit /api/issue-detail/{issueNumber}?view=ops"

    # Dispatcher wiring.
    dispatcher_src = (DASHBOARD_JS_DIR / "lifecycle_commands.js").read_text(encoding="utf-8")
    assert (
        "'open_inline_agent_attempts'" in dispatcher_src
    ), "lifecycle_commands.js must dispatch 'open_inline_agent_attempts'"
    assert (
        "loadInlineAgentAttempts(command.issue_number" in dispatcher_src
    ), "dispatcher must call loadInlineAgentAttempts(issue, triggerEl)"
    assert (
        "function runLifecycleCommandFromToggle" in dispatcher_src
    ), "lifecycle_commands.js must define runLifecycleCommandFromToggle"


# ─── Issue #6334: drop the #e2eDiagnosisModal frame ────────────────────


def test_template_drops_e2e_diagnosis_modal_markup() -> None:
    """The ``#e2eDiagnosisModal`` overlay was the legacy modal for
    rendering E2E run details.  Issue #6334 replaced it with the
    inline runs-as-rows layout — the modal markup must not survive
    in the template.

    Re-adding a modal here would resurrect the modal-vs-drawer
    z-index war (see issue_detail.css comment) and the
    ``data-e2e-run-view-active`` body cloak that #6334 removed.
    """
    html = _read(DASHBOARD_TEMPLATE)
    assert (
        "e2eDiagnosisModal" not in html
    ), "dashboard.html must not contain #e2eDiagnosisModal — runs render inline (#6334)"
    assert (
        "e2eDiagnosisContent" not in html
    ), "dashboard.html must not contain #e2eDiagnosisContent (modal body)"
    assert (
        "e2eDiagnosisAgent" not in html
    ), "dashboard.html must not contain #e2eDiagnosisAgent (modal dead control)"
    assert (
        "data-e2e-run-view-active" not in html
    ), "dashboard.html must not set data-e2e-run-view-active (modal cloak attribute)"


def test_template_mounts_inline_runs_list_root() -> None:
    """The runs-as-rows panel mounts into ``#e2eRunsListRoot`` and reads
    its initial payload from ``#recentE2ERunsData`` (typed JSON).

    The JS chunk ``e2e_runs_list.js`` reads the inline JSON on
    DOMContentLoaded — no initial round-trip required.
    """
    html = _read(DASHBOARD_TEMPLATE)
    assert (
        'id="e2eRunsListRoot"' in html
    ), "Run History panel must mount into #e2eRunsListRoot (#6334)"
    assert (
        'id="recentE2ERunsData"' in html
    ), "Run History panel must embed inline JSON in #recentE2ERunsData (#6334)"
    assert (
        'type="application/json"' in html
    ), "inline runs-list payload must declare type=application/json"


def test_css_bundle_drops_e2e_diagnosis_modal_rules() -> None:
    """Issue #6334 deleted every ``#e2eDiagnosisModal`` selector and the
    companion ``body[data-e2e-run-view-active]`` cloak.  The CSS
    bundle (every chunk concatenated) must carry no live ``modal``
    rules for the dropped overlay.

    Carrying dead selectors keeps the bundle bigger than it needs to
    be and (worse) leaves the modal showable via a one-line JS
    revert.  This guardrail makes the drop sticky.
    """
    css = _read_dashboard_css_bundle()
    # The ``#`` selector itself must not appear in any rule.
    assert (
        "#e2eDiagnosisModal" not in css
        or _modal_only_in_comments(css)
    ), "dashboard CSS must not carry #e2eDiagnosisModal rules (#6334)"
    assert (
        "data-e2e-run-view-active" not in css
        or _cloak_only_in_comments(css)
    ), "dashboard CSS must not carry body[data-e2e-run-view-active] cloak rules (#6334)"


def _modal_only_in_comments(css: str) -> bool:
    """Allow ``#e2eDiagnosisModal`` to appear in a /* … */ comment
    (so the deletion note in overlays.css doesn't break this test)."""
    # Strip /* … */ block comments, then verify the symbol is gone.
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return "#e2eDiagnosisModal" not in stripped


def _cloak_only_in_comments(css: str) -> bool:
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return "data-e2e-run-view-active" not in stripped


def test_js_bundle_drops_legacy_e2e_diagnosis_modal_callers() -> None:
    """Companion to the CSS guardrail: the JS bundle must not retain
    the dead modal-driver functions (``showE2EDiagnosis``,
    ``closeE2EDiagnosisModal``, ``renderE2EDiagnosis``,
    ``createE2EDiagnosticIssue``) that issue #6334 removed.  These
    referenced DOM ids that no longer exist; carrying them would
    leave runtime errors latent if anyone re-introduced a caller.
    """
    js = _read_dashboard_js_bundle()
    # Strip JS line comments + block comments so deletion notes in
    # the source don't break the guardrail.
    stripped = re.sub(r"/\*.*?\*/", "", js, flags=re.DOTALL)
    stripped = re.sub(r"//[^\n]*", "", stripped)
    for symbol in (
        "function showE2EDiagnosis(",
        "function renderE2EDiagnosis(",
        "function closeE2EDiagnosisModal(",
        "function createE2EDiagnosticIssue(",
    ):
        assert symbol not in stripped, (
            f"{symbol!r} must not survive in the dashboard JS bundle (#6334)"
        )


def test_e2e_runs_list_chunk_registered_after_e2e_run_view() -> None:
    """``e2e_runs_list.js`` calls ``renderE2EResultsPanel`` /
    ``renderE2ETimeline`` (defined in ``e2e_run_view.js``), so it
    must load AFTER ``e2e_run_view.js`` in the bundle.  It also uses
    ``hierarchical_timeline.js`` for the row shell, so the shared
    renderer must load before both timeline consumers.  A re-order that
    drops these invariants would break the inline canonical viewer mount
    or resurrect duplicate disclosure markup.
    """
    chunks = list(DASHBOARD_JS_CHUNKS)
    assert "e2e_run_view.js" in chunks
    assert "hierarchical_timeline.js" in chunks
    assert "issue_detail_drawer.js" in chunks
    assert "e2e_runs_list.js" in chunks, (
        "e2e_runs_list.js must be registered in DASHBOARD_JS_CHUNKS (#6334)"
    )
    assert chunks.index("hierarchical_timeline.js") > chunks.index("lifecycle_commands.js")
    assert chunks.index("hierarchical_timeline.js") < chunks.index("issue_detail_drawer.js")
    assert chunks.index("hierarchical_timeline.js") < chunks.index("e2e_runs_list.js")
    assert chunks.index("e2e_runs_list.js") > chunks.index("e2e_run_view.js"), (
        "e2e_runs_list.js must load AFTER e2e_run_view.js — its lazy "
        "loader calls renderE2EResultsPanel/renderE2ETimeline (#6334)"
    )


def test_e2e_runs_list_uses_typed_command_pipeline() -> None:
    """Issue #6334: each ``<details>`` row in the runs-as-rows panel
    must carry a typed ``expand_e2e_run`` Command in
    ``data-lifecycle-command`` and dispatch on ``ontoggle`` through
    the shared ``runLifecycleCommandFromToggle`` pipeline via
    ``hierarchical_timeline.js`` — the same single-owner contract every
    native disclosure affordance uses.

    Prevents drift back to a bespoke ``ontoggle="_handleE2ERunRow(...)"``
    handler reading from a one-off attribute (the bug PR #6333
    fixed for the inline Attempts expander).
    """
    src = (DASHBOARD_JS_DIR / "e2e_runs_list.js").read_text(encoding="utf-8")
    renderer_src = (DASHBOARD_JS_DIR / "hierarchical_timeline.js").read_text(encoding="utf-8")
    assert (
        "'expand_e2e_run'" in src or '"expand_e2e_run"' in src
    ), "e2e_runs_list.js must emit the typed kind 'expand_e2e_run'"
    assert (
        "renderHierarchicalTimelineNode({" in src
    ), "row <details> shell must be rendered by the shared hierarchical renderer"
    assert (
        "command," in src
    ), "row must pass its typed Command to the shared hierarchical renderer"
    assert (
        'ontoggle="runLifecycleCommandFromToggle' in renderer_src
    ), "shared row renderer must dispatch via runLifecycleCommandFromToggle"
    assert (
        "_renderLifecycleCommandAttr(node.command)" in renderer_src
    ), "shared row renderer must use the lifecycle attr helper"

    # Dispatcher wiring.
    dispatcher_src = (DASHBOARD_JS_DIR / "lifecycle_commands.js").read_text(encoding="utf-8")
    attr_body = _function_body(dispatcher_src, "_renderLifecycleCommandAttr")
    assert (
        'data-lifecycle-command="' in attr_body
    ), "shared lifecycle attr helper must emit data-lifecycle-command"
    assert (
        "'expand_e2e_run'" in dispatcher_src
    ), "lifecycle_commands.js must dispatch 'expand_e2e_run'"
    assert (
        "loadE2ERunIntoRow(command.run_id" in dispatcher_src
    ), "dispatcher must call loadE2ERunIntoRow(run_id, triggerEl)"
    assert (
        "expandE2ERunRow(command.run_id" in dispatcher_src
    ), "dispatcher must re-route open_e2e_run to expandE2ERunRow(run_id, options)"
    assert (
        "showUnifiedRunView(command.run_id" not in dispatcher_src
    ), "open_e2e_run must NOT route to the dropped showUnifiedRunView modal call (#6334)"


def test_recent_e2e_runs_builder_emits_typed_payload() -> None:
    """The runs-list builder in ``dashboard_e2e.py`` must produce
    typed ``RecentE2ERunSummary`` rows with a matching
    ``ExpandE2ERunCommand``.

    Builder smoke test catches drift between the DB row shape
    (``E2ERun``) and the typed payload — adding a new ``E2ERun``
    field, for instance, can't quietly break the row renderer
    without surfacing here.
    """
    from types import SimpleNamespace

    from issue_orchestrator.view_models.dashboard_e2e import build_recent_e2e_runs

    class _DB:
        def list_runs(self, orchestrator_id, limit):
            assert orchestrator_id == "test"
            return [
                SimpleNamespace(
                    id=1,
                    started_at="2026-05-12T10:00:00Z",
                    finished_at="2026-05-12T10:05:00Z",
                    status="passed",
                    duration_seconds=300.0,
                    commit_sha="abc1234",
                    branch="main",
                    runner_kind="pytest",
                    command=["pytest", "tests/e2e"],
                    pytest_args=[],
                    note=None,
                ),
                SimpleNamespace(
                    id=2,
                    started_at="2026-05-12T11:00:00Z",
                    finished_at=None,
                    status="running",
                    duration_seconds=None,
                    commit_sha=None,
                    branch="feature",
                    runner_kind="pytest",
                    command=[],
                    pytest_args=["-v"],
                    note="Retry of #1",
                ),
            ]

        def get_test_summary(self, run_id):
            return {"counts": {"passed": 36, "failed": 1, "passed_on_retry": 2,
                               "quarantined": 0, "skipped": 1, "total": 40}}

    config = SimpleNamespace(orchestrator_id="test")
    payload = build_recent_e2e_runs(_DB(), config, limit=50)
    assert len(payload.runs) == 2

    first = payload.runs[0]
    assert first.run_id == 1
    assert first.outcome.label == "Passed"
    assert first.outcome.tone == "passed"
    # passed + passed_on_retry collapse into the single "passed"
    # bucket on the row — the canonical viewer keeps the per-retry
    # detail.
    assert first.results.passed == 38
    assert first.results.failed == 1
    assert first.expand_command.kind == "expand_e2e_run"
    assert first.expand_command.run_id == 1
    assert first.command_summary == "pytest tests/e2e"

    # Running run — outcome tone is ``in_progress``, not ``passed``.
    # OutcomeBadge contract: unknown / non-terminal mustn't be green.
    second = payload.runs[1]
    assert second.outcome.label == "Running"
    assert second.outcome.tone == "in_progress"
    assert second.note == "Retry of #1"
    # Empty command + pytest_args=['-v'] should fall back to
    # ``pytest -v`` for display.
    assert second.command_summary == "pytest -v"


def test_recent_e2e_runs_payload_is_empty_without_an_e2e_db(tmp_path) -> None:
    """``build_recent_e2e_runs_payload`` owns opening the dashboard's e2e DB.

    The dashboard builder must not need to know where ``e2e.db`` lives, and
    the three "no DB to read" shapes (no config, E2E disabled, DB file absent)
    must all yield an empty typed payload rather than raising — a fresh repo
    still renders a dashboard.
    """
    from types import SimpleNamespace

    from issue_orchestrator.view_models.dashboard_e2e import build_recent_e2e_runs_payload

    assert build_recent_e2e_runs_payload(None).runs == ()

    disabled = SimpleNamespace(e2e=SimpleNamespace(enabled=False), repo_root=tmp_path)
    assert build_recent_e2e_runs_payload(disabled).runs == ()

    # Enabled but the DB file has never been created.
    enabled_no_db = SimpleNamespace(e2e=SimpleNamespace(enabled=True), repo_root=tmp_path)
    assert build_recent_e2e_runs_payload(enabled_no_db).runs == ()


# ---------------------------------------------------------------------------
# CSRF bootstrap parity across every full HTML page
# ---------------------------------------------------------------------------
#
# Every top-level HTML page served behind dashboard/Control-Center auth must
# bootstrap the shared browser-auth adapter, or its mutating fetches (Save,
# Resume, Create issue, ...) go out with no ``X-CSRF-Token`` header and the
# auth gate rejects them with ``missing or invalid csrf token``. This is the
# bug that hit the settings page. Rather than rely on each new page
# remembering the three pieces, this guardrail auto-discovers every full-page
# template and asserts the bootstrap is present — so the next page added
# cannot silently regress.

TEMPLATES_DIR = ROOT / "src" / "issue_orchestrator" / "templates"

# A full page declares ``<!doctype html>`` as its first markup. Anchoring at
# the file start (rather than a loose substring match) keeps a fragment that
# merely *mentions* a doctype in a comment or example from being misclassified
# as a full page that must carry the CSRF meta tags.
_DOCTYPE_AT_START = re.compile(r"^\s*<!doctype html>", re.IGNORECASE)


def _full_page_templates() -> list[Path]:
    """Every template that is a complete HTML document (not a fragment)."""
    return sorted(
        p
        for p in TEMPLATES_DIR.glob("*.html")
        if _DOCTYPE_AT_START.match(p.read_text(encoding="utf-8"))
    )


def test_every_full_page_template_bootstraps_csrf_auth() -> None:
    pages = _full_page_templates()
    # Sanity: discovery actually found the known pages, so an empty glob
    # can't make this test vacuously pass.
    names = {p.name for p in pages}
    assert {"dashboard.html", "settings.html", "control_center.html"} <= names, names

    for page in pages:
        html = page.read_text(encoding="utf-8")
        assert 'name="io-csrf-token"' in html, (
            f"{page.name} is a full HTML page but has no io-csrf-token meta tag; "
            "mutating fetches will fail CSRF. Bootstrap it like dashboard.html."
        )
        assert 'name="io-browser-auth-required"' in html, (
            f"{page.name} is missing the io-browser-auth-required meta tag."
        )
        assert "browser_auth.js" in html, (
            f"{page.name} does not load browser_auth.js, so the authenticated-"
            "fetch wrapper that attaches X-CSRF-Token is never installed."
        )


def test_issue_row_fragment_is_not_treated_as_a_full_page() -> None:
    # Pins the fragment/full-page distinction the guardrail relies on: a
    # partial rendered into an already-bootstrapped page must NOT be required
    # to carry its own CSRF meta tags.
    assert ISSUE_ROW_TEMPLATE not in _full_page_templates()


# --- Stack dependency gates (#6597) ---------------------------------------

def test_issue_detail_stack_section_uses_native_disclosure() -> None:
    # The stack section must be a native <details>/<summary> so the
    # expanded/collapsed relationship is keyboard-operable and exposed to
    # assistive tech without hand-rolled ARIA.
    html = _read(DASHBOARD_TEMPLATE)
    assert 'id="issueDetailStack"' in html
    assert "<details" in html and 'id="issueDetailStack"' in html
    assert 'id="issueDetailStackSummary"' in html
    # The summary is the accessible name for the region.
    assert "<summary id=\"issueDetailStackSummary\"" in html


def test_stack_chip_status_is_text_not_colour_only() -> None:
    # The compact card stack chip must carry a textual status; colour alone is
    # not an acceptable signal. The status *text* is precomputed server-side
    # (test_dependency_gate_view.py::stack_chip asserts the ready/blocked/stale
    # words); the JS renders that precomputed text into a status element.
    body = _function_body(
        _read(DASHBOARD_JS_DIR / "kanban_columns.js"), "renderStackChipHtml"
    )
    assert "stack-chip-status" in body
    assert "status_text" in body, "chip must render the precomputed textual status"


def test_stack_chip_icon_is_decorative_aria_hidden() -> None:
    body = _function_body(
        _read(DASHBOARD_JS_DIR / "kanban_columns.js"), "renderStackChipHtml"
    )
    assert 'stack-chip-icon" aria-hidden="true"' in body


def test_stack_drawer_gate_state_is_labelled_text() -> None:
    # Drawer gate rows must show an open/blocked word (not colour only) and the
    # decorative status icon must be aria-hidden.
    source = _read(DASHBOARD_JS_DIR / "issue_detail_drawer.js")
    body = _function_body(source, "_stackGateItemHtml")
    assert "stack-gate-state" in body
    assert "isOpen ? 'open' : 'blocked'" in body
    assert 'stack-gate-icon" aria-hidden="true"' in body


def test_stack_section_summary_has_visible_focus_style() -> None:
    css = _read_dashboard_css_bundle()
    focus_body = _last_css_rule_body(css, ".issue-detail-section summary:focus-visible")
    assert "outline" in focus_body


def test_stack_chip_and_rows_wrap_to_avoid_clipping() -> None:
    # Long refs/reasons must wrap rather than clip at narrow widths.
    css = _read_dashboard_css_bundle()
    assert "flex-wrap: wrap" in _last_css_rule_body(css, ".stack-chip")
    gate_body = _last_css_rule_body(css, ".stack-gate,\n.stack-edge")
    assert "flex-wrap: wrap" in gate_body
    assert "overflow-wrap: anywhere" in gate_body


def test_stack_status_colours_use_theme_variables_for_contrast() -> None:
    # Tone colours must come from the shared light/dark theme variables so
    # contrast holds in both themes (never hard-coded hex).
    css = _read_dashboard_css_bundle()
    blocked_body = _last_css_rule_body(css, ".stack-gate--blocked .stack-gate-state")
    assert "var(--danger)" in blocked_body
    open_body = _last_css_rule_body(css, ".stack-gate--open .stack-gate-state")
    assert "var(--ok)" in open_body


# ---------------------------------------------------------------------------
# Provider circuit-breaker outage banner + health panel (issue #5980)
# ---------------------------------------------------------------------------

def test_provider_circuit_banner_uses_a_scoped_assertive_announcer() -> None:
    # Accessibility (issue #5980): the whole banner must NOT be the assertive
    # live region. If it were, every countdown tick and every rebuild of the
    # interactive <details> panel would re-announce the outage and could drop
    # focus. The alert is a dedicated, visually hidden announcer; the banner
    # container is a passive wrapper that only toggles visibility.
    template = _read(DASHBOARD_TEMPLATE)
    match = re.search(r'<div id="providerCircuitBanner"[^>]*>', template)
    assert match, "providerCircuitBanner container not found"
    banner_tag = match.group(0)
    assert "display:none" in banner_tag
    assert 'role="alert"' not in banner_tag, "the whole banner must not be an assertive alert region"
    assert "aria-live" not in banner_tag

    announcer = re.search(r'<span id="providerCircuitAnnouncer"[^>]*>', template)
    assert announcer, "dedicated providerCircuitAnnouncer live region not found"
    announcer_tag = announcer.group(0)
    assert 'role="alert"' in announcer_tag
    assert 'aria-live="assertive"' in announcer_tag

    # The interactive health panel lives OUTSIDE the alert region and starts
    # collapsed so live updates never re-announce it.
    assert re.search(
        r'<details id="providerCircuitDetails"[^>]*\shidden(\s|>)', template
    ), "circuit details panel must be a native <details> that starts hidden"

    # The announcer must be visually hidden so it is not a duplicate visible
    # line -- it exists only to be announced.
    css = _read_dashboard_css_bundle()
    announcer_css = _last_css_rule_body(css, ".pcircuit-announcer")
    assert "position: absolute" in announcer_css
    assert "clip: rect(0, 0, 0, 0)" in announcer_css


def test_provider_circuit_chunk_is_registered_and_wired() -> None:
    # The render chunk must be bundled and invoked on both initial load and
    # every live refresh, or the banner would never appear.
    assert "provider_circuit.js" in DASHBOARD_JS_CHUNKS
    bundle = _read_dashboard_js_bundle()
    assert "function updateProviderCircuitBanner(" in bundle
    assert "function providerCircuitRowsHtml(" in bundle
    assert "function renderProviderCircuitEntryRow(" in bundle
    core = _read(DASHBOARD_JS_DIR / "core.js")
    # Wired into the live refresh path and the initial DOMContentLoaded render.
    assert core.count("renderProviderCircuitFromDashboardData()") >= 2


def test_provider_circuit_events_trigger_live_refresh() -> None:
    # Outage enter/exit must refresh the view model so the banner is not stale
    # until the next unrelated event.
    source = _read(DASHBOARD_JS_DIR / "issue_metadata.js")
    assert "'provider.outage_entered'" in source
    assert "'provider.outage_exited'" in source


def test_provider_circuit_status_is_text_not_colour_only() -> None:
    # Each provider row carries a word status ("Unavailable"/"Recovering"), and
    # the banner icon is decorative/aria-hidden — colour is never the only cue.
    source = _read(DASHBOARD_JS_DIR / "provider_circuit.js")
    body = _function_body(source, "renderProviderCircuitEntryRow")
    assert "'Unavailable'" in body
    assert "'Recovering'" in body
    # The decorative icon lives in the static template skeleton, aria-hidden.
    template = _read(DASHBOARD_TEMPLATE)
    assert 'class="pcircuit-icon" aria-hidden="true"' in template


def test_provider_circuit_banner_uses_theme_variables_for_contrast() -> None:
    # Banner colours must come from shared theme variables so contrast holds in
    # both light and dark themes (never hard-coded hex).
    css = _read_dashboard_css_bundle()
    banner_body = _last_css_rule_body(css, ".provider-circuit-banner")
    assert "var(--danger)" in banner_body
    assert "var(--bg-panel)" in banner_body


def test_provider_circuit_error_text_wraps_to_avoid_clipping() -> None:
    # Long provider error summaries must wrap, not clip, at narrow widths.
    css = _read_dashboard_css_bundle()
    error_body = _last_css_rule_body(css, ".pcircuit-error")
    assert "word-break: break-word" in error_body


def test_provider_circuit_summary_toggle_has_visible_focus_style() -> None:
    css = _read_dashboard_css_bundle()
    focus_body = _last_css_rule_body(
        css, ".pcircuit-details > summary.pcircuit-summary-toggle:focus-visible"
    )
    assert "outline" in focus_body


def test_provider_badge_renders_in_both_row_forms() -> None:
    # Issue #5980 item 2: the provider-outage badge is rendered by BOTH the
    # compact kanban card and the expanded list row, from the same shared
    # helper — so the two paths cannot drift. Behavior is covered by
    # tests/js/provider_badge_row.test.js; this is the structural rail.
    js = _read(DASHBOARD_JS)
    assert "function renderProviderBadgeHtml(" in js
    assert "renderProviderBadgeHtml(card)" in _function_body(js, "renderCompactCardHtml")
    assert "renderProviderBadgeHtml(item)" in _function_body(js, "renderExpandedCardHtml")


def test_provider_badge_status_is_text_not_colour_only() -> None:
    # The visible badge text is the accessible name; the icon is decorative.
    body = _function_body(_read(DASHBOARD_JS), "renderProviderBadgeHtml")
    assert "badge.label_text" in body
    assert 'class="provider-badge-icon" aria-hidden="true"' in body


def test_provider_badge_uses_theme_variables_for_contrast() -> None:
    # Both light and dark themes take their contrast from shared variables.
    css = _read_dashboard_css_bundle()
    badge_body = _last_css_rule_body(css, ".provider-badge")
    assert "var(--border)" in badge_body
    assert "var(--bg-panel)" in badge_body
    tone_body = _last_css_rule_body(css, ".provider-badge--blocked .provider-badge-text")
    assert "var(--danger)" in tone_body


def test_provider_badge_text_wraps_to_avoid_clipping() -> None:
    css = _read_dashboard_css_bundle()
    text_body = _last_css_rule_body(css, ".provider-badge-text")
    assert "overflow-wrap: anywhere" in text_body


def test_provider_impact_events_trigger_live_row_refresh() -> None:
    # Issue #5980: when an issue gains or loses the provider-blocked state its
    # row badge must appear/clear without a manual reload.
    source = _read(DASHBOARD_JS_DIR / "issue_metadata.js")
    assert "'provider.issue_blocked'" in source
    assert "'provider.issue_unblocked'" in source
