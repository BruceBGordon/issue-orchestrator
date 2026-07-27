// Helper to hide settings menu (used by multiple functions)
function hideSettingsMenu() {
    const menu = document.getElementById('settingsMenu');
    if (menu) menu.classList.remove('visible');
}

function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(text);
    return div.innerHTML;
}

function escapeAttr(text) {
    if (text === null || text === undefined) return '';
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

const LIFECYCLE_DATASET_KEYS = Object.freeze({
    kind: 'lifecycleKind',
    iterations: 'lifecycleIterations',
});
window.LIFECYCLE_DATASET_KEYS = LIFECYCLE_DATASET_KEYS;

function applyLifecycleDataset(container, lifecycle) {
    if (!container) return;
    if (!lifecycle || typeof lifecycle !== 'object') {
        delete container.dataset[LIFECYCLE_DATASET_KEYS.kind];
        delete container.dataset[LIFECYCLE_DATASET_KEYS.iterations];
        return;
    }
    if (typeof lifecycle.kind !== 'string' || lifecycle.kind.length === 0) {
        console.error('Lifecycle payload missing kind', lifecycle);
        throw new Error('Lifecycle payload missing kind');
    }
    container.dataset[LIFECYCLE_DATASET_KEYS.kind] = lifecycle.kind;
    const iterations = Array.isArray(lifecycle.runs)
        ? lifecycle.runs.length
        : (lifecycle.current ? 1 : 0);
    container.dataset[LIFECYCLE_DATASET_KEYS.iterations] = String(iterations);
}

let terminalBackend = 'tmux';
let clientCapabilities = {
    focus_session: false,
    open_path: false,
    reveal_worktree: false,
    local_server_paths_only: true,
    host_platform: 'unknown',
};
let currentCommitSha = null;
let viewModel = null;
const issueRefreshInFlight = new Set();
const issueRefreshLastAttempt = new Map();
let flowRefreshObserver = null;
let networkSyncTimer = null;
let currentDiagnosticsRunDir = null;
const flowRefreshPrefsModal = document.getElementById('flowRefreshPrefsModal');
const FLOW_REFRESH_OVERRIDE_KEY = 'issue-orchestrator.flow-refresh.override.v1';
const NETWORK_SYNC_OVERRIDE_KEY = 'issue-orchestrator.network-sync.override.v1';
const GH_USAGE_UI_PREF_KEY = 'issue-orchestrator.github-usage.ui.v1';
const FLOW_FRESHNESS_PRESETS = {
    aggressive: { enabled: true, staleSeconds: 180, cooldownSeconds: 30 },
    balanced: { enabled: true, staleSeconds: 900, cooldownSeconds: 120 },
    economy: { enabled: true, staleSeconds: 3600, cooldownSeconds: 300 },
};
const FLOW_BUDGET_MULTIPLIER = { low: 1.7, medium: 1.0, high: 0.6 };
const issueRowState = window.issueRowState;
const expandedColumnState = window.expandedColumnState;
const compactCardState = window.compactCardState;
const uiActionContract = window.uiActionContract;
if (!issueRowState) {
    throw new Error('issueRowState helper not loaded');
}
if (!expandedColumnState) {
    throw new Error('expandedColumnState helper not loaded');
}
if (!compactCardState) {
    throw new Error('compactCardState helper not loaded');
}
if (!uiActionContract) {
    throw new Error('uiActionContract helper not loaded');
}

function applyDashboardTheme(theme) {
    // Delegate to the shared resolver so Dashboard and Settings apply the
    // same precedence (see static/js/embedded_nav.js).
    let storedTheme = null;
    let prefersDark = false;
    try {
        storedTheme = localStorage.getItem('theme');
        prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    } catch (_error) {
        storedTheme = null;
        prefersDark = false;
    }
    const effectiveTheme = window.embeddedNav.resolveEffectiveTheme({
        override: theme,
        search: window.location.search,
        storedTheme,
        prefersDark,
    });
    // Same-value writes on <html> still trigger a global style invalidation,
    // which on initial boot is visible as a whole-screen flash. Only write
    // when the value actually differs from what dashboard_boot.js stamped.
    if (document.documentElement.getAttribute('data-theme') !== effectiveTheme) {
        document.documentElement.setAttribute('data-theme', effectiveTheme);
    }
}

function setDashboardInitializing(isInitializing) {
    const status = document.getElementById('dashboardInitStatus');
    if (!status) return;
    status.classList.toggle('is-active', Boolean(isInitializing));
}

function markDashboardBooted() {
    if (typeof window.dashboardBoot?.clearBootingWhenStable === 'function') {
        window.dashboardBoot.clearBootingWhenStable(window);
    } else {
        document.documentElement.removeAttribute('data-booting');
    }
}

function navigateBackToRepositories() {
    window.parent.postMessage({ type: 'cc-back-to-repos' }, '*');
}

// When embedded in CC iframe, hide dashboard header and show embedded header in tab bar
const isEmbedded = new URLSearchParams(window.location.search).get('embedded') === '1';
if (isEmbedded && document.documentElement.getAttribute('data-embedded') !== 'true') {
    // Same-value writes still invalidate <html>-keyed CSS globally, so skip
    // when dashboard_boot.js already stamped this during head parse.
    document.documentElement.setAttribute('data-embedded', 'true');
}

const embeddedNav = window.embeddedNav;
if (!embeddedNav) {
    throw new Error('embeddedNav helper not loaded');
}

function goToSettings() {
    window.location.href = embeddedNav.buildHref('/settings', window.location.search);
}
if (isEmbedded) {
    document.addEventListener('DOMContentLoaded', () => {
        // Populate repo name from server-rendered data
        const repoEl = document.getElementById('embeddedRepoName');
        const desiredRepo = window.dashboardData?.repo || '';
        // The template now SSRs the repo name into this span. Skip the
        // write when it already matches so the first JS init doesn't
        // unnecessarily replace the text node and fire a MutationObserver
        // event during boot.
        if (repoEl && repoEl.textContent !== desiredRepo) {
            repoEl.textContent = desiredRepo;
        }
        // Back button: repositories in normal mode, collapse in expanded mode
        document.getElementById('embeddedBack')?.addEventListener('click', () => {
            const expanded = document.querySelector('.kanban-column.expanded[data-expanded="true"]');
            const columnId = expanded?.dataset?.column;
            if (columnId) {
                toggleColumnExpand(columnId);
            } else {
                navigateBackToRepositories();
            }
        });
        // (i) scope button → toggle scope-summary as a dropdown below tab bar
        document.getElementById('embeddedScopeBtn')?.addEventListener('click', (e) => {
            e.stopPropagation();
            const scope = document.querySelector('.scope-summary');
            if (scope) scope.classList.toggle('scope-open');
        });
        // Click anywhere (including inside scope) closes scope, except the (i) button itself
        document.addEventListener('click', (e) => {
            const scope = document.querySelector('.scope-summary');
            if (scope?.classList.contains('scope-open') && !e.target.closest('.embedded-scope-btn')) {
                scope.classList.remove('scope-open');
            }
        });
        updateEmbeddedBackButtonVisibility();
    });
}

function updateEmbeddedBackButtonVisibility() {
    if (!isEmbedded) return;
    const back = document.getElementById('embeddedBack');
    const label = document.getElementById('embeddedBackLabel');
    if (!back || !label) return;
    const hasExpandedColumn = Boolean(document.querySelector('.kanban-column.expanded[data-expanded="true"]'));
    const desiredText = hasExpandedColumn ? 'Back to dashboard' : 'Back to repositories';
    // Same-value writes still fire MutationObservers and trigger style
    // invalidation; this function gets called on every refresh, so guard
    // each write to avoid header flashes.
    if (back.style.display !== '') back.style.display = '';
    if (label.textContent !== desiredText) label.textContent = desiredText;
    if (back.getAttribute('aria-label') !== desiredText) {
        back.setAttribute('aria-label', desiredText);
    }
}

// Listen for messages from parent (CC iframe embedding)
window.addEventListener('message', (event) => {
    if (!event.data?.type) return;
    switch (event.data.type) {
        case 'theme':
            applyDashboardTheme(event.data.theme);
            break;
        case 'cc-refresh-from-github':
            refreshFromGitHub();
            break;
        case 'cc-open-flow-prefs':
            openFlowRefreshPrefs();
            break;
        case 'cc-repo-info':
            // Parent CC sends repo display name and config info
            if (event.data.repoName) {
                const el = document.getElementById('embeddedRepoName');
                if (el && el.textContent !== event.data.repoName) {
                    el.textContent = event.data.repoName;
                }
            }
            break;
    }
});
fetch('/api/info')
    .then(res => res.json())
    .then(data => {
        if (data.terminal_backend) {
            terminalBackend = data.terminal_backend;
        }
        if (data.client_capabilities) {
            clientCapabilities = {
                ...clientCapabilities,
                ...data.client_capabilities,
            };
        }
        if (data.commit_short) {
            currentCommitSha = data.commit_sha;
            const commitEl = document.getElementById('menuCommitSha');
            if (commitEl) {
                commitEl.textContent = data.commit_short;
            }
        }
        updateActionHints();
    })
    .catch(() => {});

function updateActionHints() {
    const rows = document.querySelectorAll('.issue-row');
    rows.forEach((row) => {
        const action = row.dataset.action;
        const issueAction = row.querySelector('.issue-action');
        let hint = '';
        if (action === 'focus') {
            hint = clientCapabilities.focus_session
                ? 'Focus terminal session'
                : 'View agent log';
        } else if (row.dataset.url) {
            hint = 'Open issue on GitHub';
        }
        if (issueAction) {
            issueAction.title = hint;
        }
    });
}

function copyCommitSha() {
    if (!currentCommitSha) {
        showToast('Commit SHA not available', 'error');
        return;
    }
    navigator.clipboard.writeText(currentCommitSha)
        .then(() => showToast('Commit SHA copied'))
        .catch(() => showToast('Failed to copy commit SHA', 'error'));
}

function cssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === 'function') {
        return window.CSS.escape(value);
    }
    return String(value).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
}

function updateStatusBadgeFromViewModel(vm) {
    if (!vm) return;

    const startupComplete = vm.startup_status === 'complete';
    const shutdownRequested = vm.shutdown_requested;
    const paused = vm.paused;
    const activeCount = vm.active_session_count || 0;

    let text = 'Running';
    let className = 'status-badge status-running';

    if (!startupComplete) {
        text = 'Starting...';
        className = 'status-badge status-starting';
    } else if (shutdownRequested && activeCount > 0) {
        text = `Shutting down... (${activeCount})`;
        className = 'status-badge status-paused';
    } else if (shutdownRequested) {
        text = 'Stopped';
        className = 'status-badge status-paused';
    } else if (paused && activeCount > 0) {
        text = `Pausing... (${activeCount})`;
        className = 'status-badge status-paused';
    } else if (paused) {
        text = 'Paused';
        className = 'status-badge status-paused';
    }

    // Update all status badges (standalone header + embedded tab bar).
    //
    // Two bugs to avoid:
    //  1. Unconditional textContent / class writes fire MutationObserver
    //     events and trigger style invalidation on every view-model refresh,
    //     even when the value is unchanged — visible as a periodic flash.
    //  2. `badge.className = className` REPLACES the full class string, so
    //     auxiliary classes the server stamped (like `embedded-badge` on
    //     #embeddedBadge) get silently wiped on the first refresh. Only the
    //     state class (status-running / status-paused / status-starting)
    //     should be swapped; everything else must be left alone.
    const stateClasses = ['status-running', 'status-paused', 'status-starting'];
    const desiredState = className.split(/\s+/)
        .find((cls) => stateClasses.includes(cls)) || null;
    document.querySelectorAll('.status-badge').forEach(badge => {
        if (badge.textContent !== text) {
            badge.textContent = text;
        }
        const currentState = stateClasses.find((cls) => badge.classList.contains(cls)) || null;
        if (currentState !== desiredState) {
            // Build the final class string and write it once, instead of
            // calling classList.remove three times + classList.add once
            // (each call fires its own MutationObserver event, stacking
            // four invalidations for one logical state change). Atomic
            // write = one MO event = one paint.
            const others = Array.from(badge.classList).filter(
                (cls) => !stateClasses.includes(cls)
            );
            const next = desiredState ? [...others, desiredState] : others;
            badge.className = next.join(' ');
        }
    });
}

function updatePauseMenuFromViewModel(vm) {
    const menuItem = document.getElementById('pauseResumeItem');
    if (!menuItem || !vm) return;
    const desired = vm.paused
        ? '<span aria-hidden="true">▶</span> Resume'
        : '<span aria-hidden="true">⏸</span> Pause';
    // Same-value innerHTML writes still tear down and rebuild the subtree,
    // firing childList mutations on every refresh — skip when unchanged.
    if (menuItem.innerHTML !== desired) {
        menuItem.innerHTML = desired;
    }
}

function buildEmptyStateHtml(vm) {
    if (!vm) {
        return 'No issues in queue';
    }
    if (vm.startup_status && vm.startup_status !== 'complete') {
        const msg = vm.startup_message || 'Starting up...';
        return '<div class="startup-loading">' +
            '<div class="startup-spinner"></div>' +
            '<span class="startup-loading-text">' + msg + '</span>' +
            '</div>';
    }
    if (vm.active_tab === 'history') {
        return 'No session history yet';
    }
    if (vm.active_tab === 'attention') {
        return 'No action needed - all systems running smoothly!';
    }
    if (vm.active_tab === 'e2e') {
        return '<div class=\"e2e-empty-state\">' +
            '<p>No E2E test activity</p>' +
            '<button class=\"issue-action-btn start-btn\" onclick=\"startE2E()\">' +
            '<span aria-hidden=\"true\">▶</span> Start E2E Tests' +
            '</button>' +
            '</div>';
    }
    return 'No issues in queue';
}

function ensureEmptyState(vm, hasRows) {
    const list = document.getElementById('issueList');
    if (!list) return;

    let emptyState = list.querySelector('.empty-state');
    if (!emptyState) {
        emptyState = document.createElement('div');
        emptyState.className = 'empty-state';
        list.appendChild(emptyState);
    }

    if (hasRows) {
        emptyState.style.display = 'none';
        return;
    }

    emptyState.style.display = 'block';
    emptyState.innerHTML = buildEmptyStateHtml(vm);
}

async function refreshIssueRows(vm, rowsOverride = null) {
    const list = document.getElementById('issueList');
    if (!list) return;

    let rows = rowsOverride;
    if (!Array.isArray(rows)) {
        const url = new URL('/api/issue-rows', window.location.origin);
        const params = new URL(window.location.href).searchParams;
        if (params.get('tab')) url.searchParams.set('tab', params.get('tab'));
        if (params.get('page')) url.searchParams.set('page', params.get('page'));
        if (params.get('e2e_page')) url.searchParams.set('e2e_page', params.get('e2e_page'));

        const res = await fetch(url.toString());
        if (!res.ok) return;
        const data = await res.json();
        rows = data.rows || [];
    }

    const nextIds = new Set(rows.map(row => String(row.issue_number)));
    const existingGroups = Array.from(list.querySelectorAll('.issue-row-group[data-issue]'));
    const existingById = new Map(existingGroups.map((group) => [group.dataset.issue, group]));
    existingGroups.forEach(group => {
        if (!nextIds.has(group.dataset.issue)) {
            group.remove();
        }
    });

    const header = list.querySelector('.issue-header');
    let insertAfter = header;
    rows.forEach(row => {
        const id = String(row.issue_number);
        const existing = existingById.get(id) || null;
        const nextFingerprint = issueRowState.computeIssueRowFingerprint(row);

        let node = existing;
        const shouldReplace = !existing || existing.dataset.rowFingerprint !== nextFingerprint;
        if (shouldReplace) {
            const wrapper = document.createElement('div');
            wrapper.innerHTML = row.html.trim();
            const newNode = wrapper.firstElementChild;
            if (!newNode) {
                return;
            }
            newNode.dataset.rowFingerprint = nextFingerprint;
            if (existing) {
                existing.replaceWith(newNode);
            }
            node = newNode;
        } else if (node) {
            node.dataset.rowFingerprint = nextFingerprint;
        }

        if (!node) {
            return;
        }

        if (node.previousElementSibling !== insertAfter) {
            insertAfter.after(node);
        }
        insertAfter = node;
    });

    formatDashboardTimestamps(list);
    ensureEmptyState(vm, rows.length > 0);
    updateActionHints();
    initVisibilityObserver();
    initFlowLazyVisibleRefresh();
}

// Tracked per request mode: a snapshot fetch (`/api/view-model-snapshot`)
// is a strict superset of a view-model fetch (`/api/view-model`) — it
// also carries the rows refreshIssueRows needs. So a snapshot can stand
// in for any caller, but a view-model-only call must NOT be reused by a
// caller asking for a snapshot, otherwise refreshIssueRows is silently
// skipped on list-changing SSE events (queue.changed, session.started, ...).
let _refreshInFlight = { snapshot: null, viewModel: null };

async function refreshViewModel({ reloadOnListChange = true } = {}) {
    // Coalesce concurrent calls of the same mode so the DOMContentLoaded
    // refresh and the SSE `onopen` refresh (which fire within
    // milliseconds of each other on dashboard open) share a single
    // request rather than each rebuilding the kanban DOM on its own.
    if (reloadOnListChange) {
        if (_refreshInFlight.snapshot) return _refreshInFlight.snapshot;
        _refreshInFlight.snapshot = (async () => {
            try {
                return await _refreshViewModelImpl({ reloadOnListChange: true });
            } finally {
                _refreshInFlight.snapshot = null;
            }
        })();
        return _refreshInFlight.snapshot;
    }
    // view-model: prefer a snapshot already in flight (it returns a
    // superset and will satisfy any view-model-only caller).
    if (_refreshInFlight.snapshot) return _refreshInFlight.snapshot;
    if (_refreshInFlight.viewModel) return _refreshInFlight.viewModel;
    _refreshInFlight.viewModel = (async () => {
        try {
            return await _refreshViewModelImpl({ reloadOnListChange: false });
        } finally {
            _refreshInFlight.viewModel = null;
        }
    })();
    return _refreshInFlight.viewModel;
}

async function _refreshViewModelImpl({ reloadOnListChange = true } = {}) {
    try {
        const endpoint = reloadOnListChange ? '/api/view-model-snapshot' : '/api/view-model';
        const url = new URL(endpoint, window.location.origin);
        const params = new URL(window.location.href).searchParams;
        if (params.get('tab')) url.searchParams.set('tab', params.get('tab'));
        if (params.get('page')) url.searchParams.set('page', params.get('page'));
        if (params.get('e2e_page')) url.searchParams.set('e2e_page', params.get('e2e_page'));

        const res = await fetch(url.toString());
        if (!res.ok) return;
        const payload = await res.json();
        if (reloadOnListChange) {
            if (!payload.view_model || !Array.isArray(payload.rows)) {
                throw new Error('Invalid /api/view-model-snapshot payload shape');
            }
            viewModel = payload.view_model;
        } else {
            viewModel = payload;
        }
        window.dashboardData = viewModel.dashboard_data || window.dashboardData;
        isPaused = !!viewModel.paused;
        updateStatusBadgeFromViewModel(viewModel);
        updatePauseMenuFromViewModel(viewModel);
        updateRefreshStatusFromViewModel(viewModel);
        setDashboardInitializing(viewModel.startup_status && viewModel.startup_status !== 'complete');
        applyNetworkSyncScheduler();
        renderGitHubUsage();
        renderProviderCircuitFromDashboardData();

        // Post status to parent CC when embedded
        if (isEmbedded && viewModel) {
            const usage = window.dashboardData?.githubUsage || {};
            const rate = usage.last_rate_limit_from_headers || {};
            const refresh = window.dashboardData?.refresh || {};
            const scope = window.dashboardData?.scope || {};
            window.parent.postMessage({
                type: 'dashboard-status',
                payload: {
                    ghUsage: {
                        callsPerMinute: Number(usage.calls_per_minute || 0),
                        totalCalls: Number(usage.total_calls || 0),
                        remaining: Number(rate.remaining || 0),
                        limit: Number(rate.limit || 0),
                    },
                    refresh: {
                        lastRefreshLabel: refresh.lastRefreshLabel || 'unknown',
                        lastRefreshAt: Number(refresh.lastRefreshAt || 0),
                        inProgress: Boolean(refresh.inProgress),
                        requested: Boolean(refresh.requested),
                    },
                    scope: {
                        repo: window.dashboardData?.repo || '',
                        inScopeTotal: Number(scope.in_scope_total || 0),
                        filterMilestones: scope.filter_milestones || [],
                        filterLabel: scope.filter_label || '',
                        excludeLabels: scope.exclude_labels || [],
                    },
                    counts: {
                        queued: Number(viewModel.queue_count || 0),
                        running: Number(viewModel.active_count || 0),
                        blocked: Number(viewModel.blocked_count || 0),
                        awaitingMerge: Number(viewModel.awaiting_merge_count || 0),
                        completed: Number(viewModel.completed_count || 0),
                    },
                    paused: Boolean(viewModel.paused),
                    shutdownRequested: Boolean(viewModel.shutdown_requested),
                    startupStatus: viewModel.startup_status || '',
                },
            }, '*');
        }

        // Update kanban column counts and compact cards from fresh view model
        if (viewModel.flow_columns) {
            for (const col of viewModel.flow_columns) {
                const colEl = document.querySelector(`[data-column="${col.id}"]`);
                if (!colEl) continue;
                const countEl = colEl.querySelector('.count');
                const visibleItems = filterSuppressedItems(col.items || [], col.id);
                const serverCount = Number(col.count);
                if (countEl) {
                    countEl.textContent = String(
                        Number.isFinite(serverCount) ? serverCount : visibleItems.length,
                    );
                }

                // Rebuild compact cards (skip if column is expanded — it has its own refresh)
                if (colEl.dataset.expanded !== 'true') {
                    const cardsEl = colEl.querySelector('.column-cards');
                    if (cardsEl) renderCompactCards(cardsEl, visibleItems);
                }
            }
        }

        // If a column is expanded, refresh its content
        const expandedCol = document.querySelector('.kanban-column.expanded');
        if (expandedCol) {
            loadExpandedColumn(expandedCol.dataset.column, { viewModel });
        }

        if (reloadOnListChange && viewModel.startup_status === 'complete') {
            await refreshIssueRows(viewModel, payload.rows);
        } else if (viewModel.startup_status && viewModel.startup_status !== 'complete') {
            ensureEmptyState(viewModel, false);
        }
    } catch (e) {
        console.error('Failed to refresh view-model:', e);
    }
}

document.addEventListener('DOMContentLoaded', async () => {
    try {
        applyDashboardTheme();
        // Upgrade server-rendered Settings links (e.g. the no-validation
        // warning banner) so ?embedded=1 and ?theme= survive the round-trip,
        // routed through the same embeddedNav owner as goToSettings().
        embeddedNav.applySettingsLinks(document, window.location.search);
        setDashboardInitializing(window.dashboardData?.startupComplete === false);
        updateActionHints();
        initFlowLazyVisibleRefresh();
        applyGitHubUsagePrefs();
        renderGitHubUsage();
        renderProviderCircuitFromDashboardData();
        applyNetworkSyncScheduler();
        // Await the first refresh so `data-booting` (which suppresses CSS
        // transitions) stays set through the initial DOM mutations. Without
        // this await, transitions are re-enabled mid-render and users see
        // the kanban cards flash as they're replaced.
        await refreshViewModel({ reloadOnListChange: false });
        initVisibilityObserver();
        const nextRun = document.getElementById('e2eNextRun');
        if (nextRun && nextRun.dataset.nextRunReason) {
            const nextInfo = {
                next_run_at: nextRun.dataset.nextRunAt,
                next_run_reason: nextRun.dataset.nextRunReason,
            };
            const formatted = formatNextRun(nextInfo);
            if (formatted) {
                nextRun.textContent = formatted;
            }
        }
    } catch (err) {
        console.error('[boot] Dashboard initialization failed:', err);
    } finally {
        markDashboardBooted();
    }
});

document.addEventListener('change', (event) => {
    if (event.target?.id === 'flowRefreshOverrideEnabled') {
        setFlowRefreshInputsEnabled(Boolean(event.target.checked));
    }
    if (event.target?.id === 'networkSyncCadenceSource' || event.target?.id === 'saveCadenceToConfig') {
        updateCadencePreferenceInputState();
    }
});
let visibilityObserver = null;
let visibilityPostTimer = null;
let lastVisibilityPayload = '';

function initVisibilityObserver() {
    if (visibilityObserver) {
        visibilityObserver.disconnect();
        visibilityObserver = null;
    }
    const enabled = Boolean(window.dashboardData && window.dashboardData.fetchLayerVisibilityAwareEnabled);
    if (!enabled) return;
    const cards = Array.from(document.querySelectorAll('.issue-card[data-issue]'));
    if (!cards.length) return;

    const visibleNumbers = new Set();
    visibilityObserver = new IntersectionObserver((entries) => {
        let changed = false;
        entries.forEach((entry) => {
            const issueNumber = parseInt(entry.target.dataset.issue || '0', 10);
            if (!issueNumber) return;
            if (entry.isIntersecting) {
                if (!visibleNumbers.has(issueNumber)) {
                    visibleNumbers.add(issueNumber);
                    changed = true;
                }
            } else if (visibleNumbers.delete(issueNumber)) {
                changed = true;
            }
        });
        if (changed) {
            scheduleVisibilityPost(Array.from(visibleNumbers));
        }
    }, { threshold: 0.25 });

    cards.forEach((card) => visibilityObserver.observe(card));
}

function scheduleVisibilityPost(visibleIssueNumbers) {
    const normalized = Array.from(new Set(visibleIssueNumbers.map(n => parseInt(n, 10)).filter(Boolean))).sort((a, b) => a - b);
    const payload = JSON.stringify(normalized);
    if (payload === lastVisibilityPayload) return;
    if (visibilityPostTimer) window.clearTimeout(visibilityPostTimer);
    visibilityPostTimer = window.setTimeout(() => {
        postVisibility(visibleIssueNumbers);
    }, 600);
}

async function postVisibility(visibleIssueNumbers) {
    const normalized = Array.from(new Set(visibleIssueNumbers.map(n => parseInt(n, 10)).filter(Boolean))).sort((a, b) => a - b);
    const payload = JSON.stringify(normalized);
    if (payload === lastVisibilityPayload) return;
    try {
        const res = await fetch('/api/refresh/visibility', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ issues: normalized }),
        });
        if (res.ok) {
            lastVisibilityPayload = payload;
        }
    } catch (err) {
        console.error('Failed to post visibility hint:', err);
    }
}
