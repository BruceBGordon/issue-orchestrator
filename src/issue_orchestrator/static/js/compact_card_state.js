(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.compactCardState = api;
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    function normalizeLabels(labels) {
        if (!Array.isArray(labels)) return [];
        return labels.map(String);
    }

    function computeCompactCardFingerprint(card) {
        // phase_age is intentionally excluded — see compute_compact_card_fingerprint
        // in view_models/dashboard_flow.py for the rationale.
        const cardId = card?.card_id ?? '';
        const issueNumber = card?.issue_number ?? '';
        const issueKey = card?.issue_key ?? '';
        const issueLabel = card?.issue_label ?? '';
        const title = card?.title ?? '';
        const stateLabel = card?.state_label ?? '';
        const phase = card?.phase ?? '';
        const summary = card?.summary ?? '';
        const stale = Boolean(card?.is_stale);
        const showStaleBadge = Boolean(card?.show_stale_badge);
        const staleReason = card?.stale_reason ?? '';
        const issueUrl = card?.issue_url ?? '';
        const prUrl = card?.pr_url ?? '';
        const githubUrl = card?.github_url ?? '';
        const githubLabel = card?.github_label ?? '';
        const githubTitle = card?.github_title ?? '';
        const githubAriaLabel = card?.github_aria_label ?? '';
        const labels = normalizeLabels(card?.orchestrator_labels).join(',');
        // The server precomputes stack_signal (stack_signal() in
        // view_models/dashboard.py), so both sides fingerprint identically.
        const stackSignal = card?.stack_signal ?? '';
        // provider_signal encodes the precomputed provider-outage badge
        // (provider_signal() in view_models/issue_card_labels.py). It carries no
        // cooldown/ETA, so an outage that is merely counting down does not
        // re-fingerprint the card; gaining or losing the badge does.
        const providerSignal = card?.provider_signal ?? '';
        // run_dir binds the reused card node to a specific run. If it changes
        // (e.g. a rework-<issue> slot replaced by a new run) the node must be
        // rebuilt so the stale data-run-dir — read by the launch-prompt
        // action — cannot linger. Mirrors compute_compact_card_fingerprint.
        const runDir = card?.run_dir ?? '';
        return [
            cardId,
            issueNumber,
            issueKey,
            issueLabel,
            title,
            stateLabel,
            phase,
            summary,
            stale,
            showStaleBadge,
            staleReason,
            issueUrl,
            prUrl,
            githubUrl,
            githubLabel,
            githubTitle,
            githubAriaLabel,
            labels,
            stackSignal,
            providerSignal,
            runDir,
        ].join('|');
    }

    return {
        computeCompactCardFingerprint,
    };
});
