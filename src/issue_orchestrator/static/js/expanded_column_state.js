(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.expandedColumnState = api;
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    function getExpandedItemsFromViewModel(viewModel, columnId) {
        if (!viewModel || typeof viewModel !== 'object') return [];
        if (columnId === 'queued') {
            const queueItems = viewModel.queue_items || [];
            const awaitingMergeItems = viewModel.awaiting_merge_items || [];
            const awaitingNumbers = new Set(
                awaitingMergeItems.map((item) => Number(item.issue_number)),
            );
            return queueItems.filter((item) => !awaitingNumbers.has(Number(item.issue_number)));
        }
        if (columnId === 'blocked') return viewModel.blocked_items || [];
        if (columnId === 'awaiting-merge') return viewModel.awaiting_merge_items || [];
        if (columnId === 'completed') return viewModel.completed_items || [];
        if (columnId === 'running') return viewModel.active_items || [];
        return [];
    }

    function computeExpandedItemsFingerprint(items, options) {
        const columnId = options?.columnId || '';
        const viewedIssueNumbers = options?.viewedIssueNumbers || [];
        const viewed = new Set(viewedIssueNumbers);

        return (items || []).map((item) => {
            const issueNumber = Number(item.issue_number);
            const isViewed = columnId === 'blocked' && viewed.has(issueNumber);
            return [
                issueNumber,
                item.issue_key || '',
                item.issue_label || '',
                item.title || '',
                item.detail_label || '',
                item.status || '',
                item.issue_url || '',
                item.pr_url || '',
                // The expanded row renders the provider-outage badge, so the
                // list must rebuild when a row gains or loses it.
                item.provider_signal || '',
                isViewed ? '1' : '0',
            ].join('|');
        }).join('||');
    }

    function reconcileSelectedIssues(selectedIssueNumbers, items) {
        const present = new Set((items || []).map((item) => Number(item.issue_number)));
        return (selectedIssueNumbers || [])
            .map((value) => Number(value))
            .filter((issueNumber) => Number.isFinite(issueNumber) && present.has(issueNumber));
    }

    return {
        getExpandedItemsFromViewModel,
        computeExpandedItemsFingerprint,
        reconcileSelectedIssues,
    };
});
