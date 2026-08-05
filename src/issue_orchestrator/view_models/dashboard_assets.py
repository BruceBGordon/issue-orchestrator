"""Static asset manifests for dashboard template rendering."""

from __future__ import annotations

DASHBOARD_JS_CHUNKS: tuple[str, ...] = (
    # Single owner for dashboard-local timestamp rendering.  Keep this
    # first so every later chunk can call formatTimestamp().
    "timestamp_formatting.js",
    "core.js",
    # ``provider_circuit.js`` renders the provider outage banner + health
    # panel (issue #5980) from ``window.dashboardData.providerCircuit``.
    # Loaded after ``core.js`` so ``escapeHtml`` / ``escapeAttr`` and after
    # ``timestamp_formatting.js`` so ``formatTimestamp`` are in scope.
    "provider_circuit.js",
    "session_replay.js",
    # ``validation_viewer.js`` defines the canonical JUnit viewer and
    # the Phase-0 plugin registry (issue #6310 follow-up).  Loaded
    # before ``session_dialogs.js`` (which calls into the viewer to
    # render the validation dialog body) and before any plugin module
    # (which call ``registerValidationPlugin`` at load time).
    "validation_viewer.js",
    # ``lifecycle_commands.js`` owns the shared typed-Command renderer
    # and dispatcher (``_renderLifecycleCommandButton`` /
    # ``runLifecycleCommand`` /
    # ``runLifecycleCommandFromButton``).  Loaded before command emitters
    # so the dependency is declared rather than implicit.
    "lifecycle_commands.js",
    # ``hierarchical_timeline.js`` owns the native ``<details>/<summary>``
    # shell and host-capability registry.  Orchestrator-specific lifecycle
    # rendering is contributed by ``plugins/agent_context.js`` below.
    "hierarchical_timeline.js",
    # ``inline_agent_attempts.js`` exposes the ``▸ Attempts on issue #N``
    # expander that the ``io.agent-context`` plugin renders.  Its expanded
    # body uses the plugin-owned issue lifecycle renderer at runtime, so this
    # chunk must load before ``plugins/agent_context.js`` but does not own the
    # renderer itself.
    "inline_agent_attempts.js",
    # Plugin modules register themselves at load time with the registry
    # defined in ``validation_viewer.js``.  Today the only Phase-0
    # plugin is the issue-orchestrator agent-context renderer — Phase C
    # populates ``case.extras`` to invoke it from the E2E view.  For
    # ``general-case`` consumers (tixmeup et al.) can load their own plugin
    # JS after ``validation_viewer.js`` and register a separate namespace.
    # The viewer renders ``case.extras`` in payload order and treats this
    # plugin as optional: absent or non-first ``io.agent-context`` extras do
    # not change generic JUnit rendering.
    "plugins/agent_context.js",
    "session_dialogs.js",
    "controls_refresh.js",
    "kanban_columns.js",
    "issue_metadata.js",
    "issue_menus.js",
    "issue_detail_modals.js",
    "issue_detail_drawer.js",
    "timeline.js",
    "diagnostics_actions.js",
    "shell_actions.js",
    "retrospective_review.js",
    "e2e_runtime.js",
    "e2e_triage.js",
    # ``e2e_canonical_payload.js`` provides the pure translator from
    # the orchestrator's per-category E2E run-detail shape to the
    # JUnit-canonical payload that ``renderCanonicalValidationViewer``
    # consumes (Phase C of #6310 follow-up).  Loaded before
    # ``e2e_run_view.js`` so the symbol is in scope at call time.
    "e2e_canonical_payload.js",
    "e2e_run_view.js",
    # ``e2e_runs_list.js`` renders the inline runs-as-rows panel
    # (issue #6334) that replaced ``#e2eDiagnosisModal``.  Loaded
    # AFTER ``e2e_run_view.js`` so its lazy detail loader can
    # call ``renderE2EResultsPanel`` / ``renderE2ETimeline`` /
    # ``enhanceCanonicalValidationViewerAccessibility`` without a
    # forward reference.
    "e2e_runs_list.js",
)

DASHBOARD_CSS_CHUNKS: tuple[str, ...] = (
    "base.css",
    "cards.css",
    "issue_detail.css",
    "overlays.css",
    # ``validation_viewer.css`` scopes the canonical validation viewer's
    # ``cvv-*`` classes (issue #6310 follow-up).  Loaded after
    # ``overlays.css`` so the viewer's chip / row styling overrides
    # legacy ``diag-`` rules where they share class names — but the
    # ``cvv-`` prefix means there's no actual overlap.
    "validation_viewer.css",
    "e2e_run_detail.css",
)
