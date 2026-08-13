// ============================================
// State
// ============================================
const state = {
    repos: [],
    discoveredRepos: [],
    discoveredLoadedAt: 0,
    currentView: 'repositories',
    activeRepo: null,  // { path, config, mode, source: 'registered'|'discovered' }
    theme: localStorage.getItem('theme') || 'system',
    dashboardStatus: null
};
const pendingRepoStarts = new Set();
const pendingRepoStops = new Set();
const SHUTDOWN_TIMEOUT_PRESETS_SECONDS = [60, 120, 300, 600, 1200, 1800, 3600];
const REPO_START_POLL_INTERVAL_MS = 250;
const REPO_START_TIMEOUT_MS = 15000;
let shutdownExpectClose = false;
let shutdownCloseAttempted = false;
let doctorModalContext = { repoRoot: null, configName: null, mode: null, title: null, data: null };
let setupWizardController = null;

const DISCOVERED_STALE_MS = 5 * 60 * 1000;

// Load recently used repo from localStorage
function loadRecentRepo() {
    try {
        const saved = localStorage.getItem('recentRepo');
        if (saved) return JSON.parse(saved);
    } catch (e) {}
    return null;
}

function saveRecentRepo(path, config, mode = 'default') {
    localStorage.setItem('recentRepo', JSON.stringify({ path, config, mode }));
}

function getDefaultMode(repoPath) {
    try {
        const defaults = JSON.parse(localStorage.getItem('repoDefaultModes') || '{}');
        return defaults[repoPath];
    } catch (e) {}
    return null;
}

function setDefaultModeForRepo(repoPath, mode) {
    try {
        const defaults = JSON.parse(localStorage.getItem('repoDefaultModes') || '{}');
        defaults[repoPath] = mode;
        localStorage.setItem('repoDefaultModes', JSON.stringify(defaults));
    } catch (e) {}
}

function getRepoModes(repo) {
    if (repo?.modes?.length) return repo.modes;
    return repo?.configs?.length ? ['default'] : [];
}

function getSelectedRepoMode(repo, preferredMode = null) {
    const modes = getRepoModes(repo);
    if (getRepoState(repo) !== 'not running' && repo?.active_mode) return repo.active_mode;
    if (!modes.length) return preferredMode || 'default';
    if (repo.selected_mode && modes.includes(repo.selected_mode)) return repo.selected_mode;
    if (preferredMode && modes.includes(preferredMode)) return preferredMode;
    const savedMode = getDefaultMode(repo.path);
    if (savedMode && modes.includes(savedMode)) return savedMode;
    return modes[0];
}

function getRepoConfigs(repo, mode = null) {
    const selectedMode = mode && getRepoModes(repo).includes(mode)
        ? mode
        : getSelectedRepoMode(repo);
    const configured = repo?.mode_configs?.[selectedMode]
        || (selectedMode === 'default' ? (repo?.configs || []) : []);
    if (
        getRepoState(repo) !== 'not running'
        && repo?.active_mode === selectedMode
        && repo?.active_config
        && !configured.includes(repo.active_config)
    ) {
        return [repo.active_config, ...configured];
    }
    return configured;
}

// Load default config for a repo from localStorage
function getDefaultConfig(repoPath, mode = 'default') {
    try {
        const defaults = JSON.parse(localStorage.getItem('repoDefaultConfigs') || '{}');
        return defaults[repoPath]?.[mode] || null;
    } catch (e) {}
    return null;
}

function setDefaultConfigForRepo(repoPath, configName, mode = 'default') {
    try {
        const defaults = JSON.parse(localStorage.getItem('repoDefaultConfigs') || '{}');
        defaults[repoPath] = defaults[repoPath] || {};
        defaults[repoPath][mode] = configName;
        localStorage.setItem('repoDefaultConfigs', JSON.stringify(defaults));
    } catch (e) {}
}

function clearDefaultConfigForRepo(repoPath, mode = 'default') {
    try {
        const defaults = JSON.parse(localStorage.getItem('repoDefaultConfigs') || '{}');
        if (defaults[repoPath]) {
            delete defaults[repoPath][mode];
            if (Object.keys(defaults[repoPath]).length === 0) delete defaults[repoPath];
        }
        localStorage.setItem('repoDefaultConfigs', JSON.stringify(defaults));
    } catch (e) {}
}

function isRepoConfigAvailable(repo, configName, mode = null) {
    return Boolean(
        configName
        && getRepoConfigs(repo, mode).includes(configName)
    );
}

function getSelectedRepoConfig(repo, preferredConfig = null, mode = null) {
    const configs = getRepoConfigs(repo, mode);
    const selectedMode = mode && getRepoModes(repo).includes(mode)
        ? mode
        : getSelectedRepoMode(repo);
    if (
        getRepoState(repo) !== 'not running'
        && repo?.active_mode === selectedMode
        && repo?.active_config
    ) return repo.active_config;
    if (configs.length === 0) {
        return preferredConfig;
    }
    if (
        repo.selected_mode === selectedMode
        && repo.selected_config
        && configs.includes(repo.selected_config)
    ) return repo.selected_config;
    if (preferredConfig !== null && preferredConfig !== undefined) {
        return configs.includes(preferredConfig) ? preferredConfig : null;
    }
    const savedDefault = getDefaultConfig(repo.path, selectedMode);
    if (savedDefault !== null && savedDefault !== undefined) {
        return configs.includes(savedDefault) ? savedDefault : null;
    }
    return configs[0];
}

function getRepoConfigIssue(repo, preferredConfig = null, mode = null) {
    const configs = getRepoConfigs(repo, mode);
    const selectedMode = mode && getRepoModes(repo).includes(mode)
        ? mode
        : getSelectedRepoMode(repo);
    if (configs.length === 0) {
        return null;
    }
    if (
        repo.selected_mode === selectedMode
        && repo.selected_config
        && configs.includes(repo.selected_config)
    ) return null;
    if (preferredConfig && !configs.includes(preferredConfig)) {
        return `Selected config is unavailable: ${preferredConfig}`;
    }
    const savedDefault = getDefaultConfig(repo.path, selectedMode);
    if (savedDefault && !configs.includes(savedDefault)) {
        return `Saved config is unavailable: ${savedDefault}`;
    }
    return null;
}

function buildRepoConfigOptions(repo, currentConfig, mode = null) {
    const options = [];
    const configs = getRepoConfigs(repo, mode);
    if (configs.length && !currentConfig) {
        options.push('<option value="" selected>Choose config...</option>');
    }
    for (const configName of configs) {
        options.push(
            `<option value="${escapeHtml(configName)}" ${configName === currentConfig ? 'selected' : ''}>${escapeHtml(configName)}</option>`
        );
    }
    return options.join('');
}

function buildRepoModeOptions(repo, currentMode) {
    const modes = [...getRepoModes(repo)];
    if (currentMode && !modes.includes(currentMode)) modes.unshift(currentMode);
    return modes.map(mode => (
        `<option value="${escapeHtml(mode)}" ${mode === currentMode ? 'selected' : ''}>${escapeHtml(mode)}</option>`
    )).join('');
}

function getValidRepoConfig(repo, preferredConfig = null, mode = null) {
    const configs = getRepoConfigs(repo, mode);
    if (configs.length === 0) {
        return preferredConfig;
    }
    const selected = getSelectedRepoConfig(repo, preferredConfig, mode);
    return selected && configs.includes(selected) ? selected : null;
}

function requiresExplicitRepoConfigSelection(repo, selectedConfig, mode = null) {
    return Boolean(
        getRepoConfigs(repo, mode).length
        && !selectedConfig
        && getRepoConfigIssue(repo, selectedConfig, mode)
    );
}

// ============================================
// Theme Management
// ============================================
function applyTheme(theme) {
    state.theme = theme;
    localStorage.setItem('theme', theme);

    let effectiveTheme = theme;
    if (theme === 'system') {
        effectiveTheme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }

    document.documentElement.setAttribute('data-theme', effectiveTheme);

    // Update theme selector buttons
    document.querySelectorAll('.theme-btn').forEach(btn => {
        const isActive = btn.dataset.theme === theme;
        btn.classList.toggle('active', isActive);
        btn.setAttribute('aria-checked', isActive);
    });

    // Push effective theme to embedded dashboard iframe
    const iframe = document.getElementById('activityIframe');
    if (iframe && iframe.contentWindow) {
        try {
            iframe.contentWindow.postMessage({ type: 'theme', theme: effectiveTheme }, '*');
        } catch (e) { /* cross-origin, ignore */ }
    }
}

// Listen for system theme changes
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (state.theme === 'system') {
        applyTheme('system');
    }
});

// ============================================
// Repo Dropdown (Context Selector)
// ============================================
async function loadDiscoveredRepos() {
    try {
        const response = await fetch('/control/repos/discover');
        const data = await response.json();
        state.discoveredRepos = data.discovered || [];
        state.discoveredLoadedAt = Date.now();
    } catch (error) {
        console.error('Failed to load discovered repos:', error);
        state.discoveredRepos = [];
    }
}

function getRepoStateClass(repo, isDiscovered = false) {
    if (isDiscovered) return 'discovered';
    const status = repo.status || {};
    if (status.paused) return 'paused';
    if (status.state === 'running' || status.state === 'partial') return 'running';
    return 'not running';
}

function getRepoStateText(repo, isDiscovered = false) {
    if (isDiscovered) {
        if (repo.status === 'needs_setup') return 'needs setup';
        if (repo.status === 'ready') return 'ready';
        return '';
    }
    const status = repo.status || {};
    if (status.paused) return 'paused';
    if (status.state === 'running' || status.state === 'partial') return 'running';
    return 'not running';
}

function selectRepo(path, config = null, source = 'registered', mode = null) {
    const repo = source === 'discovered'
        ? state.discoveredRepos.find(r => r.path === path)
        : state.repos.find(r => r.path === path);

    if (!repo) return;

    // Determine config to use
    const selectedMode = getSelectedRepoMode(repo, mode);
    const selectedConfig = getSelectedRepoConfig(repo, config, selectedMode);

    state.activeRepo = { path, config: selectedConfig, mode: selectedMode, source };
    saveRecentRepo(path, selectedConfig, selectedMode);

    updateDropdownDisplay();
    updateConfigSelector(repo);
    updateToolsScopeNote();
    closeDropdown();
}

function updateDropdownDisplay() {
    const dot = document.getElementById('dropdownStatusDot');
    const nameEl = document.getElementById('dropdownRepoName');
    const stateEl = document.getElementById('dropdownRepoState');
    if (!dot || !nameEl || !stateEl) return;

    if (!state.activeRepo) {
        dot.className = 'status-dot stopped';
        nameEl.textContent = 'No repo selected';
        stateEl.textContent = '';
        return;
    }

    const isDiscovered = state.activeRepo.source === 'discovered';
    const repo = isDiscovered
        ? state.discoveredRepos.find(r => r.path === state.activeRepo.path)
        : state.repos.find(r => r.path === state.activeRepo.path);

    if (!repo) {
        dot.className = 'status-dot stopped';
        nameEl.textContent = state.activeRepo.path.split('/').pop();
        stateEl.textContent = '· unknown';
        return;
    }

    const stateClass = getRepoStateClass(repo, isDiscovered);
    const stateText = getRepoStateText(repo, isDiscovered);

    dot.className = `status-dot ${stateClass}`;
    nameEl.textContent = repo.name;
    stateEl.textContent = stateText ? `· ${stateText}` : '';
}

function updateConfigSelector(repo) {
    const selector = document.getElementById('configSelector');
    const select = document.getElementById('configSelect');
    const setDefaultCheckbox = document.getElementById('setDefaultConfig');
    if (!selector || !select || !setDefaultCheckbox) return;

    const currentMode = getSelectedRepoMode(repo, state.activeRepo?.mode);
    const configs = getRepoConfigs(repo, currentMode);
    const currentConfig = getSelectedRepoConfig(repo, state.activeRepo?.config, currentMode);
    if (!repo || (configs.length <= 1 && currentConfig)) {
        selector.style.display = 'none';
        return;
    }

    // Populate config options
    select.innerHTML = buildRepoConfigOptions(repo, currentConfig, currentMode);

    // Check if current is the default
    const savedDefault = getDefaultConfig(repo.path, currentMode);
    setDefaultCheckbox.checked = savedDefault === currentConfig;

    selector.style.display = 'flex';
}

function renderDropdownMenu() {
    const menu = document.getElementById('repoDropdownMenu');
    if (!menu) return;

    // Categorize repos
    const running = state.repos.filter(r => {
        const s = r.status || {};
        return s.state === 'running' || s.state === 'partial' || s.paused;
    });
    const notRunning = state.repos.filter(r => {
        const s = r.status || {};
        return s.state !== 'running' && s.state !== 'partial' && !s.paused;
    });
    const discovered = state.discoveredRepos;

    let html = `
        <div class="repo-dropdown-actions">
            <button class="btn btn-sm" id="rescanReposDropdown">Rescan</button>
        </div>
    `;
    let hasItems = false;

    // Running section
    if (running.length > 0) {
        hasItems = true;
        html += `<div class="dropdown-section">
            <div class="dropdown-section-label">Running</div>
            ${running.map(r => renderDropdownItem(r, false)).join('')}
        </div>`;
    }

    // Not running section
    if (notRunning.length > 0) {
        hasItems = true;
        html += `<div class="dropdown-section">
            <div class="dropdown-section-label">Stopped (registered)</div>
            ${notRunning.map(r => renderDropdownItem(r, false)).join('')}
        </div>`;
    }

    // Discovered section
    if (discovered.length > 0) {
        hasItems = true;
        html += `<div class="dropdown-section">
            <div class="dropdown-section-label">Discovered</div>
            ${discovered.map(r => renderDropdownItem(r, true)).join('')}
        </div>`;
    }

    if (!hasItems) {
        html += '<div class="dropdown-empty">No repositories found</div>';
    }

    menu.innerHTML = html;

    const rescanBtn = menu.querySelector('#rescanReposDropdown');
    if (rescanBtn) {
        rescanBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            rescanRepos();
        });
    }

    // Attach click handlers
    menu.querySelectorAll('.dropdown-item').forEach(item => {
        item.addEventListener('click', async () => {
            const path = item.dataset.path;
            const source = item.dataset.source;
            const isDiscovered = source === 'discovered';
            const repo = isDiscovered
                ? state.discoveredRepos.find(r => r.path === path)
                : state.repos.find(r => r.path === path);

            if (isDiscovered && repo?.status === 'needs_setup') {
                // Open setup wizard for this repo
                closeDropdown();
                await openRepositorySetup(path, item);
                return;
            }

            selectRepo(path, null, source);
        });
    });
}

function renderDropdownItem(repo, isDiscovered) {
    const stateClass = getRepoStateClass(repo, isDiscovered);
    const isSelected = state.activeRepo?.path === repo.path;
    const meta = isDiscovered
        ? (repo.configs?.length ? repo.configs[0] : repo.status)
        : (repo.configs?.length ? repo.configs[0] : '');

    let badge = '';
    if (isDiscovered && repo.status === 'needs_setup') {
        badge = '<span class="dropdown-item-badge needs-setup">needs setup</span>';
    }

    const checkIcon = isSelected
        ? '<svg class="check-icon" viewBox="0 0 16 16"><path fill="currentColor" fill-rule="evenodd" d="M13.78 4.22a.75.75 0 010 1.06l-7.25 7.25a.75.75 0 01-1.06 0L2.22 9.28a.75.75 0 011.06-1.06L6 10.94l6.72-6.72a.75.75 0 011.06 0z"/></svg>'
        : '';

    return `
        <div class="dropdown-item ${isSelected ? 'selected' : ''}"
             data-path="${escapeHtml(repo.path)}"
             data-source="${isDiscovered ? 'discovered' : 'registered'}"
             role="option"
             aria-selected="${isSelected}">
            <span class="status-dot ${stateClass}"></span>
            <div class="dropdown-item-content">
                <div class="dropdown-item-name">${escapeHtml(repo.name)}</div>
                ${meta ? `<div class="dropdown-item-meta">${escapeHtml(meta)}</div>` : ''}
            </div>
            ${badge}
            ${checkIcon}
        </div>
    `;
}

function toggleDropdown() {
    const container = document.getElementById('repoDropdownContainer');
    const trigger = document.getElementById('repoDropdownTrigger');
    if (!container || !trigger) return;
    const isOpen = container.classList.toggle('open');
    trigger.setAttribute('aria-expanded', isOpen);

    if (isOpen) {
        renderDropdownMenu();
        // Load discovered repos in background if stale
        const isStale = Date.now() - state.discoveredLoadedAt > DISCOVERED_STALE_MS;
        if (state.discoveredRepos.length === 0 || isStale) {
            loadDiscoveredRepos().then(renderDropdownMenu);
        }
    }
}

function closeDropdown() {
    const container = document.getElementById('repoDropdownContainer');
    const trigger = document.getElementById('repoDropdownTrigger');
    if (!container || !trigger) return;
    container.classList.remove('open');
    trigger.setAttribute('aria-expanded', 'false');
}

// Initialize active repo from recent or first available
function initializeActiveRepo() {
    const recent = loadRecentRepo();
    if (recent) {
        // Check if recent repo still exists
        const inRegistered = state.repos.find(r => r.path === recent.path);
        const inDiscovered = state.discoveredRepos.find(r => r.path === recent.path);
        if (inRegistered) {
            selectRepo(recent.path, recent.config, 'registered', recent.mode || null);
            return;
        } else if (inDiscovered) {
            selectRepo(recent.path, recent.config, 'discovered', recent.mode || null);
            return;
        }
    }

    // Fall back to current launch directory repo when available.
    const currentDirRepo = state.repos.find(r => r.is_current_dir);
    if (currentDirRepo) {
        selectRepo(currentDirRepo.path, null, 'registered');
        return;
    }

    // Then first running, then first registered.
    const running = state.repos.find(r => {
        const s = r.status || {};
        return s.state === 'running' || s.state === 'partial';
    });
    if (running) {
        selectRepo(running.path, null, 'registered');
        return;
    }

    if (state.repos.length > 0) {
        selectRepo(state.repos[0].path, null, 'registered');
    }
}

// ============================================
// View Navigation
// ============================================
function switchView(viewName, repoPath = null) {
    // Owner-boundary readiness gate. The repo-card "Open dashboard"
    // button is disabled when isRepoFullyReady is false, but other
    // paths reach switchView('activity', path) without that check —
    // ?repo= deep links, the reconnect-to-active-engine helper, etc.
    // If we let those through while the engine is mid-startup, the
    // iframe loads against a not-yet-settled engine and produces the
    // SSE-driven flash sequence the readiness gate exists to avoid.
    // Route to the repositories view instead so the user sees the
    // "Initializing…" badge and can re-open when ready.
    if (viewName === 'activity' && repoPath) {
        const targetRepo = state.repos.find((r) => r.path === repoPath);
        if (targetRepo && !isRepoFullyReady(targetRepo)) {
            viewName = 'repositories';
            repoPath = null;
        }
    }
    state.currentView = viewName;

    // Exit maximize mode when leaving activity view
    if (viewName !== 'activity') {
        document.body.classList.remove('maximized');
        document.body.classList.remove('repo-focused');
    } else {
        document.body.classList.add('repo-focused');
    }

    // Update nav items
    document.querySelectorAll('.nav-item[data-view]').forEach(item => {
        const isActive = item.dataset.view === viewName;
        item.classList.toggle('active', isActive);
        item.setAttribute('aria-current', isActive ? 'page' : 'false');
    });

    // Update view containers
    document.querySelectorAll('.view-container').forEach(container => {
        container.classList.remove('active');
    });

    const viewContainer = document.getElementById(viewName + 'View');
    if (viewContainer) {
        viewContainer.classList.add('active');
    }

    // Toggle headers: consolidated for activity, global for everything else
    const globalHeader = document.querySelector('.header');
    const consolidatedHeader = document.getElementById('consolidatedHeader');
    const activityFooter = document.getElementById('activityFooter');
    const subtitle = document.querySelector('.header-subtitle');
    const activeSummary = document.getElementById('activeSummary');

    if (viewName === 'activity') {
        globalHeader.style.display = 'none';
        consolidatedHeader.classList.add('active');
        activityFooter.classList.add('active');
    } else {
        globalHeader.style.display = '';
        consolidatedHeader.classList.remove('active');
        activityFooter.classList.remove('active');
        // Only show subtitle + summary in repos view
        if (subtitle) subtitle.style.display = viewName === 'repositories' ? '' : 'none';
        if (activeSummary) activeSummary.style.display = viewName === 'repositories' ? '' : 'none';
    }

    // Update header title
    const titles = {
        repositories: 'Repository Engines',
        activity: 'Repository Engine',
        tools: 'Tools',
        settings: 'Settings',
        goalPilot: 'Goal Pilot'
    };
    document.getElementById('viewTitle').textContent = titles[viewName] || viewName;
    updateToolsScopeNote();

    // Handle activity view specifics
    if (viewName === 'activity' && repoPath) {
        loadActivityView(repoPath);
    }
    if (viewName === 'goalPilot') {
        goalPilotConfig();
        goalPilotLoadRuns();
    }
}

// ============================================
// Consolidated Header Utilities
// ============================================
function closeConsolidatedDropdowns() {
    const popover = document.getElementById('scopePopover');
    const menu = document.getElementById('actionMenu');
    if (popover) popover.classList.remove('visible');
    if (menu) menu.classList.remove('visible');
    document.getElementById('scopeInfoBtn')?.classList.remove('active');
    const actionMenuButton = document.getElementById('actionMenuBtn');
    actionMenuButton?.classList.remove('active');
    actionMenuButton?.setAttribute('aria-expanded', 'false');
}

function closeSidebarAppMenu() {
    const menu = document.getElementById('sidebarAppMenu');
    const btn = document.getElementById('sidebarAppMenuBtn');
    if (menu) menu.classList.remove('visible');
    if (btn) {
        btn.classList.remove('active');
        btn.setAttribute('aria-expanded', 'false');
    }
}

function toggleMaximize() {
    document.body.classList.toggle('maximized');
    closeConsolidatedDropdowns();
}

// Footer sync label — recomputes "Xm ago" locally every 5s
function formatSyncAge(epochSeconds) {
    if (!epochSeconds) return 'never';
    const ageSec = Math.max(0, Math.floor(Date.now() / 1000 - epochSeconds));
    if (ageSec < 5) return 'just now';
    if (ageSec < 60) return ageSec + 's ago';
    if (ageSec < 3600) return Math.floor(ageSec / 60) + 'm ago';
    return Math.floor(ageSec / 3600) + 'h ago';
}

function updateFooterSync(refresh) {
    const el = document.getElementById('footerSync');
    if (!el) return;
    if (refresh?.inProgress) {
        el.textContent = 'Syncing...';
    } else if (refresh?.lastRefreshAt) {
        el.textContent = 'Sync ' + formatSyncAge(refresh.lastRefreshAt);
    } else {
        el.textContent = 'Sync ' + (refresh?.lastRefreshLabel || '--');
    }
}

// Tick the sync age every 5s so it stays fresh
setInterval(() => {
    if (state.dashboardStatus?.refresh) {
        updateFooterSync(state.dashboardStatus.refresh);
    }
}, 5000);

// Listen for messages from embedded dashboard iframe
window.addEventListener('message', (event) => {
    if (!event.data?.type) return;
    // Dashboard requests navigation back to repositories
    if (event.data.type === 'cc-back-to-repos') {
        switchView('repositories');
        return;
    }
    if (event.data.type !== 'dashboard-status') return;
    const p = event.data.payload;
    if (!p) return;

    // Cache for scope popover
    state.dashboardStatus = p;

    // Update footer
    const footerGH = document.getElementById('footerGHUsage');
    const footerSync = document.getElementById('footerSync');
    const footerRepo = document.getElementById('footerRepo');
    const footerConfig = document.getElementById('footerConfig');

    if (footerRepo && p.scope.repo) {
        const repoShort = p.scope.repo.length > 30 ? '...' + p.scope.repo.slice(-27) : p.scope.repo;
        footerRepo.dataset.full = p.scope.repo;
        footerRepo.dataset.short = repoShort;
        footerRepo.title = p.scope.repo;
        if (!footerRepo.classList.contains('expanded')) footerRepo.textContent = repoShort;
        else footerRepo.textContent = p.scope.repo;
    }
    if (footerConfig && state.activeRepo && state.activeRepo.config) {
        footerConfig.textContent = state.activeRepo.config;
    }
    if (footerGH) footerGH.textContent = 'GH ' + p.ghUsage.callsPerMinute + '/min';
    updateFooterSync(p.refresh);

    // Update consolidated badge from live dashboard data
    const badge = document.getElementById('consolidatedBadge');
    if (badge) {
        if (p.shutdownRequested) {
            badge.textContent = 'Stopping...';
            badge.className = 'repo-card-badge paused';
        } else if (p.paused) {
            badge.textContent = 'Paused';
            badge.className = 'repo-card-badge paused';
        } else if (p.startupStatus !== 'complete') {
            badge.textContent = 'Starting...';
            badge.className = 'repo-card-badge paused';
        } else {
            badge.textContent = 'Running';
            badge.className = 'repo-card-badge running';
        }
    }
});

// Escape key exits maximize mode
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && document.body.classList.contains('maximized')) {
        e.stopPropagation();
        document.body.classList.remove('maximized');
    }
});

// ============================================
// Repository Loading
// ============================================
let deepLinkHandled = false;

async function loadRepos(silent = false) {
    try {
        // Load registered repos
        const response = await fetch('/control/repos');
        if (!response.ok) {
            const body = await response.text().catch(() => '');
            console.error('Failed to load repos: HTTP', response.status, body);
            if (!silent) showToast(`Failed to load repositories (HTTP ${response.status})`, 'error');
            return;
        }
        const data = await response.json();
        state.repos = data.repos || [];

        // Also load discovered repos (in parallel on first load)
        if (!deepLinkHandled) {
            await loadDiscoveredRepos();
        }

        renderRepos();
        updateRunningCount();
        updateActiveSummary();
        updateRecoveryBanner();
        updateDropdownDisplay();
        updateToolsScopeNote();
        maybeStartFastRepoPoll();

        // Check for deep-link in URL (only on first load)
        if (!deepLinkHandled) {
            deepLinkHandled = true;

            // Initialize active repo from recent/first available
            initializeActiveRepo();

            const params = new URLSearchParams(window.location.search);
            const repoPath = params.get('repo');
            if (repoPath) {
                // Clean the URL so refreshes land on repos view
                const cleanUrl = window.location.pathname;
                window.history.replaceState({}, '', cleanUrl);

                const repo = state.repos.find(r => r.path === repoPath);
                const repoState = repo?.status?.state;
                if (repo && (repoState === 'running' || repo?.status?.paused)) {
                    selectRepo(repoPath, null, 'registered');
                    switchView('activity', repoPath);
                }
            }
        }
    } catch (error) {
        console.error('Failed to load repos:', error);
        if (!silent) showToast(`Failed to load repositories: ${error.message}`, 'error');
    }
}

function resolveActiveRepo() {
    if (!state.activeRepo) return null;
    const path = state.activeRepo.path || state.activeRepo;
    const source = state.activeRepo.source || 'registered';
    let repo = null;
    if (source === 'discovered') {
        repo = state.discoveredRepos.find(r => r.path === path) || null;
    }
    if (!repo) {
        repo = state.repos.find(r => r.path === path) || null;
    }
    return { path, source, repo };
}

function repoHasConfig(repo, source) {
    if (!repo) return false;
    if (repo.configs && repo.configs.length > 0) return true;
    if (source === 'discovered' && repo.status === 'legacy') return true;
    if (repo.status && (repo.status.state === 'running' || repo.status.state === 'partial' || repo.status.paused)) {
        return true;
    }
    return false;
}

function getToolRepoPath(options = {}) {
    const requireConfig = options.requireConfig === true;
    const active = resolveActiveRepo();
    if (active) {
        if (!requireConfig || repoHasConfig(active.repo, active.source)) {
            return active.path;
        }
    }

    if (requireConfig) {
        const configured = state.repos.find(r => r.configs && r.configs.length > 0);
        if (configured) return configured.path;
        const discoveredConfigured = state.discoveredRepos.find(r => r.configs && r.configs.length > 0);
        if (discoveredConfigured) return discoveredConfigured.path;
        return null;
    }

    return state.repos[0]?.path || null;
}

function updateToolsScopeNote() {
    const el = document.getElementById('toolsScopeNote');
    if (!el) return;
    const active = resolveActiveRepo();
    if (!active?.path) {
        el.textContent = 'Tools scope: no repository selected.';
        return;
    }
    const hasConfig = repoHasConfig(active.repo, active.source);
    const configNote = hasConfig ? 'config available' : 'config not found';
    el.textContent = `Tools scope: ${active.path} (${configNote}).`;
}

async function loadSystemState() {
    try {
        const response = await fetch('/api/state');
        const data = await response.json();
        const portEl = document.getElementById('settingsDashboardPort');
        const dirEl = document.getElementById('settingsCurrentDirectory');
        const dotEl = document.getElementById('settingsDashboardStatusDot');

        if (portEl) {
            portEl.textContent = data.dashboard?.port ? String(data.dashboard.port) : 'Not running';
        }
        if (dirEl) {
            dirEl.textContent = data.current_directory || 'Unknown';
        }
        if (dotEl) {
            dotEl.className = `status-dot ${data.dashboard?.running ? 'ok' : 'warning'}`;
        }
    } catch (error) {
        const portEl = document.getElementById('settingsDashboardPort');
        const dirEl = document.getElementById('settingsCurrentDirectory');
        const dotEl = document.getElementById('settingsDashboardStatusDot');
        if (portEl) portEl.textContent = 'Unavailable';
        if (dirEl) dirEl.textContent = 'Unavailable';
        if (dotEl) dotEl.className = 'status-dot warning';
    }
}

async function rescanRepos() {
    try {
        showToast('Rescanning repositories...', 'info');
        await loadDiscoveredRepos();
        await loadRepos();
        renderDropdownMenu();
        showToast('Repository scan complete', 'success');
    } catch (error) {
        showToast('Failed to rescan repositories', 'error');
    }
}

// Helper to extract orchestrator state from backend format
function getRepoState(repo) {
    const status = repo.status || {};
    if (status.paused) return 'paused';
    if (status.state === 'running') return 'running';
    if (status.state === 'partial') return 'running';
    return 'not running';
}

function getRepoPort(repo) {
    return repo.status?.port || null;
}

function isRepoDashboardReady(repo) {
    // "Engine process is alive and serving" — used by waitForRepoToBeReady
    // to decide when the start-up RPC has succeeded. Does NOT require
    // startup_status === complete (the supervisor flips to running
    // before the engine's initial GitHub fetch finishes; that fetch
    // can outlast the start-RPC timeout).
    if (!repo) return false;
    const repoState = getRepoState(repo);
    return (repoState === 'running' || repoState === 'paused') && Boolean(repo.dashboard_url);
}

function isRepoFullyReady(repo) {
    // "Engine has finished its startup sequence" — used to gate the
    // per-repo Open dashboard button. The Control Center server
    // probes each engine's /api/status (cross-process, in-supervisor)
    // and stamps startup_status onto repo.status, so the frontend
    // just reads it from the existing /control/repos snapshot.
    if (!isRepoDashboardReady(repo)) return false;
    return repo.status?.startup_status === 'complete';
}

// While any repo is alive but mid-startup (state===running/paused but
// startup_status not yet "complete"), poll /control/repos faster than
// the default 30 s interval so the Open button transitions to enabled
// shortly after the engine reports ready, instead of after the next
// long poll. Self-stops as soon as no repo is in that transitional
// state.
const FAST_REPO_POLL_INTERVAL_MS = 2000;
let fastRepoPollTimer = null;

function anyRepoStillStarting() {
    return state.repos.some((r) => {
        const s = getRepoState(r);
        if (!(s === 'running' || s === 'paused')) return false;
        return r.status?.startup_status !== 'complete';
    });
}

function maybeStartFastRepoPoll() {
    if (fastRepoPollTimer !== null) return;
    if (!anyRepoStillStarting()) return;
    fastRepoPollTimer = window.setInterval(async () => {
        await loadRepos(true);
        if (!anyRepoStillStarting()) {
            window.clearInterval(fastRepoPollTimer);
            fastRepoPollTimer = null;
        }
    }, FAST_REPO_POLL_INTERVAL_MS);
}

async function waitForRepoToBeReady(path) {
    const deadline = Date.now() + REPO_START_TIMEOUT_MS;
    while (Date.now() < deadline) {
        await loadRepos(true);
        const repo = state.repos.find(r => r.path === path);
        if (!repo) {
            throw new Error(`Repository disappeared while starting: ${path}`);
        }
        if (isRepoDashboardReady(repo)) {
            return repo;
        }
        const repoError = repo.status?.error;
        if (repoError) {
            throw new Error(repoError);
        }
        await new Promise(resolve => window.setTimeout(resolve, REPO_START_POLL_INTERVAL_MS));
    }

    await loadRepos(true);
    const repo = state.repos.find(r => r.path === path);
    if (repo?.status?.error) {
        throw new Error(repo.status.error);
    }
    throw new Error('Repository engine did not become ready in time');
}

function buildDashboardUrlFromBase(baseUrl, options = {}) {
    if (!baseUrl) return null;
    const url = new URL(baseUrl);
    if (options.embedded) url.searchParams.set('embedded', '1');
    if (options.theme) url.searchParams.set('theme', options.theme);
    return url.toString();
}

function getRepoDashboardUrl(repo, options = {}) {
    return buildDashboardUrlFromBase(repo.dashboard_url, options);
}

function needsSetup(repo) {
    return !getRepoModes(repo).some(mode => getRepoConfigs(repo, mode).length > 0);
}

async function persistRepoLaunchSelection(path, mode, configName) {
    const response = await fetch('/control/repos/select-config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            repo_root: path,
            mode,
            config_name: configName,
        }),
    });
    if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || error.error || 'Failed to select configuration');
    }
}

async function applyRepoModeSelection(repo, mode) {
    if (!repo || !getRepoModes(repo).includes(mode)) return null;
    const config = getSelectedRepoConfig(repo, null, mode);
    if (!config) return null;
    await persistRepoLaunchSelection(repo.path, mode, config);
    repo.selected_mode = mode;
    repo.selected_config = config;
    setDefaultModeForRepo(repo.path, mode);
    setDefaultConfigForRepo(repo.path, config, mode);
    if (state.activeRepo?.path === repo.path) {
        state.activeRepo.mode = mode;
        state.activeRepo.config = config;
        saveRecentRepo(repo.path, config, mode);
    }
    return config;
}

async function applyRepoConfigSelection(repo, mode, config) {
    if (!repo) return false;
    if (config) {
        await persistRepoLaunchSelection(repo.path, mode, config);
        repo.selected_mode = mode;
        repo.selected_config = config;
        setDefaultConfigForRepo(repo.path, config, mode);
    } else {
        clearDefaultConfigForRepo(repo.path, mode);
    }
    if (state.activeRepo?.path === repo.path) {
        state.activeRepo.config = config || null;
        state.activeRepo.mode = mode;
        saveRecentRepo(repo.path, config, mode);
    }
    return true;
}

async function handleRepoCardModeChange(event) {
    const path = event.currentTarget.dataset.path;
    const mode = event.currentTarget.value;
    const repo = state.repos.find(item => item.path === path);
    const previous = getSelectedRepoMode(repo, getDefaultMode(path));
    try {
        const config = await applyRepoModeSelection(repo, mode);
        if (!config) return;
        showToast(`Mode for ${path.split('/').pop()} set to ${mode}`, 'success');
        if (state.currentView === 'activity' && state.activeRepo?.path === path) {
            loadActivityView(path);
        } else {
            renderRepos();
        }
    } catch (error) {
        event.currentTarget.value = previous;
        showToast(error.message, 'error');
        if (state.currentView === 'activity' && state.activeRepo?.path === path) {
            loadActivityView(path);
        } else if (state.currentView === 'repositories') {
            renderRepos();
        }
    }
}

async function handleMenuModeChange(event) {
    if (!state.activeRepo?.path) return;
    const repo = state.repos.find(item => item.path === state.activeRepo.path);
    const previous = state.activeRepo.mode;
    try {
        const config = await applyRepoModeSelection(repo, event.target.value);
        if (!config) return;
        loadActivityView(state.activeRepo.path);
    } catch (error) {
        event.target.value = previous;
        showToast(error.message, 'error');
        loadActivityView(state.activeRepo.path);
    }
}

async function handleRepoCardConfigChange(event) {
    const path = event.currentTarget.dataset.path;
    if (!path) return;
    const repo = state.repos.find(item => item.path === path);
    const mode = getSelectedRepoMode(repo, getDefaultMode(path));
    const previous = getSelectedRepoConfig(repo, null, mode) || '';
    const config = event.currentTarget.value;
    try {
        await applyRepoConfigSelection(repo, mode, config);
        showToast(
            config
                ? `Default config for ${path.split('/').pop()} set to ${config}`
                : `Config selection cleared for ${path.split('/').pop()}`,
            'success',
        );
    } catch (error) {
        event.currentTarget.value = previous;
        showToast(error.message, 'error');
    }
    if (state.currentView === 'activity' && state.activeRepo?.path === path) {
        loadActivityView(path);
    } else if (state.currentView === 'repositories') {
        renderRepos();
    }
}

async function handleMenuConfigChange(event) {
    if (!state.activeRepo?.path) return;
    const path = state.activeRepo.path;
    const mode = state.activeRepo.mode;
    const previous = state.activeRepo.config || '';
    try {
        await applyRepoConfigSelection(
            state.repos.find(item => item.path === path),
            mode,
            event.target.value,
        );
    } catch (error) {
        event.target.value = previous;
        showToast(error.message, 'error');
    }
    loadActivityView(path);
}

function isRepoStartPending(path) {
    return pendingRepoStarts.has(path);
}

function isRepoStopPending(path) {
    return pendingRepoStops.has(path);
}

function renderRepos() {
    const container = document.getElementById('reposContent');
    const discovered = state.discoveredRepos || [];
    const hasRegistered = state.repos.length > 0;
    const hasDiscovered = discovered.length > 0;

    if (!hasRegistered && !hasDiscovered) {
        container.innerHTML = `
            <div class="empty-state">
                <svg viewBox="0 0 16 16" aria-hidden="true">
                    <path fill="currentColor" fill-rule="evenodd" d="M2 2.5A2.5 2.5 0 014.5 0h8.75a.75.75 0 01.75.75v12.5a.75.75 0 01-.75.75h-2.5a.75.75 0 110-1.5h1.75v-2h-8a1 1 0 00-.714 1.7.75.75 0 01-1.072 1.05A2.495 2.495 0 012 11.5v-9zm10.5-1V9h-8c-.356 0-.694.074-1 .208V2.5a1 1 0 011-1h8zM5 12.25v3.25a.25.25 0 00.4.2l1.45-1.087a.25.25 0 01.3 0L8.6 15.7a.25.25 0 00.4-.2v-3.25a.25.25 0 00-.25-.25h-3.5a.25.25 0 00-.25.25z"/>
                </svg>
                <h3>No repositories found</h3>
                <p>Run issue-orchestrator from a git repository to get started, or click Rescan Repositories.</p>
            </div>
        `;
        return;
    }

    let html = '';
    if (hasRegistered) {
        html += `<div class="repos-grid">${state.repos.map(renderRepoCard).join('')}</div>`;
    }
    if (hasDiscovered) {
        html += `
            <div class="discovered-section">
                <div class="discovered-section-header">Discovered Repositories</div>
                <div class="repos-grid">${discovered.map(renderDiscoveredRepoCard).join('')}</div>
            </div>
        `;
    }
    container.innerHTML = html;

    // Attach event listeners
    container.querySelectorAll('[data-action]').forEach(btn => {
        btn.addEventListener('click', handleRepoAction);
    });
    container.querySelectorAll('select[data-action="select-mode"]').forEach(select => {
        select.addEventListener('change', handleRepoCardModeChange);
    });
    container.querySelectorAll('select[data-action="select-config"]').forEach(select => {
        select.addEventListener('change', handleRepoCardConfigChange);
    });
}

function renderDiscoveredRepoCard(repo) {
    const isReady = repo.status === 'ready';
    const isLegacy = repo.status === 'legacy';
    const badgeClass = isReady ? 'ready' : (isLegacy ? 'legacy' : 'needs-setup');
    const badgeText = isReady ? 'Ready' : (isLegacy ? 'Legacy config' : 'Needs Setup');

    const configMarkup = repo.configs?.length ? `
        <div class="repo-card-config">
            <span>Config:</span> ${escapeHtml(repo.configs.join(', '))}
        </div>
    ` : '';

    let actions = '';
    if (isReady) {
        actions = `<button class="btn btn-primary" data-action="register" data-path="${escapeHtml(repo.path)}">Add Repository</button>`;
    } else {
        actions = `<button class="btn btn-primary" data-action="setup" data-path="${escapeHtml(repo.path)}">Setup</button>`;
    }

    return `
        <div class="repo-card discovered">
            <div class="repo-card-header">
                <div>
                    <div class="repo-card-title">${escapeHtml(repo.name)}</div>
                    <div class="repo-card-path">${escapeHtml(repo.path)}</div>
                </div>
                <span class="repo-card-badge ${badgeClass}">${badgeText}</span>
            </div>
            ${configMarkup}
            <div class="repo-card-actions">
                ${actions}
            </div>
        </div>
    `;
}

function renderRepoCard(repo) {
    const repoState = getRepoState(repo);
    const isRunning = repoState === 'running';
    const isPaused = repoState === 'paused';
    const isNotRunning = repoState === 'not running';
    const isNeedsSetup = needsSetup(repo);
    const fullyReady = isRepoFullyReady(repo);

    let badgeClass = 'stopped';
    let badgeText = 'Not running';
    if (isRunning) { badgeClass = 'running'; badgeText = 'Running'; }
    if (isPaused) { badgeClass = 'paused'; badgeText = 'Paused'; }
    // While the engine is alive but mid-startup, the badge does double
    // duty as a status + readiness indicator: "Initializing" reads
    // clearly, and the Open button below stays disabled until it
    // transitions to "Running" / "Paused".
    if ((isRunning || isPaused) && !fullyReady) {
        badgeClass = 'starting';
        badgeText = 'Initializing…';
    }
    if (isNeedsSetup) { badgeClass = 'needs-setup'; badgeText = 'Needs Setup'; }

    const port = getRepoPort(repo);
    const stats = isRunning || isPaused ? `
        <div class="repo-card-stats">
            <span class="repo-card-stat">
                <svg viewBox="0 0 16 16"><path fill-rule="evenodd" d="M7.775 3.275a.75.75 0 001.06 1.06l1.25-1.25a2 2 0 112.83 2.83l-2.5 2.5a2 2 0 01-2.83 0 .75.75 0 00-1.06 1.06 3.5 3.5 0 004.95 0l2.5-2.5a3.5 3.5 0 00-4.95-4.95l-1.25 1.25zm-4.69 9.64a2 2 0 010-2.83l2.5-2.5a2 2 0 012.83 0 .75.75 0 001.06-1.06 3.5 3.5 0 00-4.95 0l-2.5 2.5a3.5 3.5 0 004.95 4.95l1.25-1.25a.75.75 0 00-1.06-1.06l-1.25 1.25a2 2 0 01-2.83 0z"/></svg>
                Port ${port || 'N/A'}
            </span>
        </div>
    ` : '';

    const selectedMode = getSelectedRepoMode(repo);
    const selectedConfig = getSelectedRepoConfig(repo, null, selectedMode);
    const configIssue = getRepoConfigIssue(repo, selectedConfig, selectedMode);
    const requiresConfigSelection = requiresExplicitRepoConfigSelection(
        repo,
        selectedConfig,
        selectedMode,
    );
    const selectionDisabled = isNotRunning ? '' : 'disabled';
    const modeMarkup = getRepoModes(repo).length > 1 ? `
        <div class="repo-card-config">
            <label>Mode:
                <select data-action="select-mode" data-path="${escapeHtml(repo.path)}" aria-label="Configuration mode for ${escapeHtml(repo.name)}" ${selectionDisabled}>
                    ${buildRepoModeOptions(repo, selectedMode)}
                </select>
            </label>
        </div>
    ` : '';
    const configMarkup = getRepoConfigs(repo, selectedMode).length ? `
        <div class="repo-card-config">
            <label>Config:
                <select data-action="select-config" data-path="${escapeHtml(repo.path)}" aria-label="Configuration file for ${escapeHtml(repo.name)}" ${selectionDisabled}>
                    ${buildRepoConfigOptions(repo, selectedConfig, selectedMode)}
                </select>
            </label>
            ${configIssue ? `<div class="repo-card-config-warning">${escapeHtml(configIssue)}</div>` : ''}
        </div>
    ` : '';

    let actions = '';
    if (isNeedsSetup) {
        actions = `<button class="btn btn-primary" data-action="setup" data-path="${escapeHtml(repo.path)}">Setup</button>`;
    } else if (isNotRunning) {
        const pendingStart = isRepoStartPending(repo.path);
        actions = `
            <button class="btn" data-action="view" data-path="${escapeHtml(repo.path)}" disabled>Open dashboard</button>
            <button class="btn btn-primary" data-action="start" data-path="${escapeHtml(repo.path)}" ${(pendingStart || requiresConfigSelection) ? 'disabled' : ''}>${pendingStart ? 'Starting...' : 'Start engine'}</button>
            <button class="btn" data-action="start-paused" data-path="${escapeHtml(repo.path)}" ${(pendingStart || requiresConfigSelection) ? 'disabled' : ''}>${pendingStart ? 'Starting paused...' : 'Start paused'}</button>
        `;
    } else {
        const pendingStop = isRepoStopPending(repo.path);
        // Engine process is alive (state===running/paused), but the
        // engine's startup sequence (initial GitHub fetch + reconcile)
        // may still be in flight. Keep Open disabled until ready;
        // the badge above reads "Initializing…" during the wait.
        // Pause/Resume is also disabled during Initializing — the
        // engine isn't ready to cleanly handle a state-change RPC
        // mid-startup. Stop stays enabled as an escape hatch in case
        // the user decides not to wait.
        const openDisabled = fullyReady ? '' : 'disabled';
        const pauseResumeDisabled = fullyReady ? '' : 'disabled';
        actions = `
            <button class="btn" data-action="view" data-path="${escapeHtml(repo.path)}" ${openDisabled}>Open dashboard</button>
            <button class="btn" data-action="${isPaused ? 'resume' : 'pause'}" data-path="${escapeHtml(repo.path)}" ${pauseResumeDisabled}>${isPaused ? 'Resume engine' : 'Pause engine'}</button>
            <button class="btn btn-danger btn-sm" data-action="stop" data-path="${escapeHtml(repo.path)}" ${pendingStop ? 'disabled' : ''}>${pendingStop ? 'Stopping...' : 'Stop engine'}</button>
        `;
    }

    return `
        <div class="repo-card ${repo.is_current_dir ? 'current-dir' : ''}">
            <div class="repo-card-header">
                <div>
                    <div class="repo-card-title">${escapeHtml(repo.name)}</div>
                    <div class="repo-card-path">${escapeHtml(repo.path)}</div>
                </div>
                <span class="repo-card-badge ${badgeClass}">${badgeText}</span>
            </div>
            ${modeMarkup}
            ${configMarkup}
            ${stats}
            <div class="repo-card-actions">
                ${actions}
            </div>
        </div>
    `;
}

function updateRunningCount() {
    const runningCount = state.repos.filter(r => getRepoState(r) === 'running').length;
    const pausedCount = state.repos.filter(r => getRepoState(r) === 'paused').length;
    const count = runningCount + pausedCount;
    document.getElementById('runningCount').textContent = count;
}

function updateActiveSummary() {
    const el = document.getElementById('activeSummary');
    if (!el) return;
    const runningCount = state.repos.filter(r => getRepoState(r) === 'running').length;
    const pausedCount = state.repos.filter(r => getRepoState(r) === 'paused').length;
    const stoppedCount = state.repos.filter(r => getRepoState(r) === 'not running').length;
    el.textContent = `Registered engines: ${runningCount} Running · ${pausedCount} Paused · ${stoppedCount} Stopped`;
}

function updateRecoveryBanner() {
    const banner = document.getElementById('recoveryBanner');
    const title = document.getElementById('recoveryBannerTitle');
    const meta = document.getElementById('recoveryBannerMeta');
    if (!banner || !title || !meta) return;

    const orphaned = state.repos.filter(r => r.status?.runtime_health === 'orphaned');
    const staleLocks = state.repos.filter(r => r.status?.runtime_health === 'stale_lock');
    const unresponsive = state.repos.filter(r => r.status?.runtime_health === 'unresponsive');

    const needsRecovery = orphaned.length + staleLocks.length + unresponsive.length;
    banner.classList.toggle('active', needsRecovery > 0);
    if (needsRecovery === 0) return;

    const parts = [];
    if (orphaned.length > 0) parts.push(`${orphaned.length} orphaned`);
    if (staleLocks.length > 0) parts.push(`${staleLocks.length} stale lock`);
    if (unresponsive.length > 0) parts.push(`${unresponsive.length} unresponsive`);
    title.textContent = 'Recovery attention needed';
    meta.textContent = parts.join(' · ');
}

function reconnectToActiveEngine() {
    const activeRepo = state.repos.find(r => {
        const s = r.status || {};
        return s.state === 'running' || s.state === 'partial' || s.paused;
    });
    if (!activeRepo) {
        showToast('No active repository engines to reconnect', 'warning');
        return;
    }
    switchView('activity', activeRepo.path);
}

async function cleanRecoveryState() {
    try {
        let response = await fetch('/control/orchestrator/reconcile', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });
        let data = await response.json();

        const orphanedCount = (data.orphaned_detected || []).length;
        const unresponsiveCount = (data.unresponsive_detected || []).length;
        const shouldOfferStop = orphanedCount > 0 || unresponsiveCount > 0;

        if (shouldOfferStop) {
            const prompt = `Detected ${orphanedCount} orphaned and ${unresponsiveCount} unresponsive engine(s). Stop them now?`;
            if (confirm(prompt)) {
                response = await fetch('/control/orchestrator/reconcile', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        stop_orphaned: true,
                        stop_unresponsive: true,
                    }),
                });
                data = await response.json();
            }
        }

        const reconciled = (data.reconciled_stale_locks || []).length;
        const stoppedOrphaned = (data.stopped_orphaned || []).length;
        const stoppedUnresponsive = (data.stopped_unresponsive || []).length;
        showToast(
            `Reconciled ${reconciled} stale lock(s), stopped ${stoppedOrphaned} orphaned, stopped ${stoppedUnresponsive} unresponsive`,
            'success',
        );
        await loadRepos();
    } catch (error) {
        showToast(`Recovery cleanup failed: ${error.message}`, 'error');
    }
}

// ============================================
// Activity View
// ============================================
function resolveRepoActivitySelection(repo) {
    const previousSelection = state.activeRepo?.path === repo.path
        ? state.activeRepo
        : null;
    const mode = getSelectedRepoMode(repo, previousSelection?.mode);
    return {
        mode,
        config: getSelectedRepoConfig(repo, previousSelection?.config, mode),
    };
}

function loadActivityView(repoPath) {
    const repo = state.repos.find(r => r.path === repoPath);
    if (!repo) {
        showToast('Repository not found', 'error');
        switchView('repositories');
        return;
    }

    const selection = resolveRepoActivitySelection(repo);
    state.activeRepo = {
        path: repo.path,
        config: selection.config,
        mode: selection.mode,
        source: 'registered'
    };
    const repoState = getRepoState(repo);
    const isPaused = repoState === 'paused';

    // Update consolidated header
    document.getElementById('consolidatedRepoName').textContent = repo.name;
    // Populate footer context immediately
    const footerRepo = document.getElementById('footerRepo');
    const footerConfig = document.getElementById('footerConfig');
    if (footerRepo) {
        const repoShort = repo.name.length > 30 ? '...' + repo.name.slice(-27) : repo.name;
        footerRepo.dataset.full = repo.path;
        footerRepo.dataset.short = repoShort;
        footerRepo.textContent = repoShort;
        footerRepo.title = repo.path;
        footerRepo.classList.remove('expanded');
    }
    if (footerConfig && state.activeRepo.config) {
        footerConfig.textContent = state.activeRepo.config;
    }
    const badge = document.getElementById('consolidatedBadge');
    badge.textContent = isPaused ? 'Paused' : (repoState === 'running' ? 'Running' : 'Not running');
    const badgeClass = isPaused ? 'paused' : (repoState === 'running' ? 'running' : 'stopped');
    badge.className = `repo-card-badge ${badgeClass}`;

    // Update menu pause/resume label
    const menuPR = document.getElementById('menuPauseResume');
    if (menuPR) {
        menuPR.innerHTML = isPaused
            ? '<span class="menu-icon">▶</span> Resume engine'
            : '<span class="menu-icon">⏸</span> Pause engine';
    }

    // Update config selector in menu
    const modeSelect = document.getElementById('menuModeSelect');
    const modeWrap = document.getElementById('menuModeWrap');
    if (modeSelect && modeWrap) {
        const modes = getRepoModes(repo);
        if (modes.length > 1) {
            modeSelect.innerHTML = buildRepoModeOptions(repo, state.activeRepo.mode);
            modeSelect.disabled = repoState !== 'not running';
            modeWrap.style.display = '';
        } else {
            modeWrap.style.display = 'none';
        }
    }
    const configSelect = document.getElementById('menuConfigSelect');
    const configWrap = document.getElementById('menuConfigWrap');
    if (configSelect && configWrap) {
        const configs = getRepoConfigs(repo, state.activeRepo.mode);
        if (configs.length > 1 || !state.activeRepo.config) {
            configSelect.innerHTML = buildRepoConfigOptions(
                repo,
                state.activeRepo.config,
                state.activeRepo.mode,
            );
            configSelect.disabled = repoState !== 'not running';
            configWrap.style.display = '';
        } else {
            configWrap.style.display = 'none';
        }
    }


    // Load iframe
    const iframe = document.getElementById('activityIframe');
    const loading = document.getElementById('activityLoading');
    iframe.style.display = 'none';
    iframe.src = 'about:blank';

    // Set iframe source to orchestrator dashboard
    const port = getRepoPort(repo);
    const configIssue = getRepoConfigIssue(
        repo,
        state.activeRepo.config,
        state.activeRepo.mode,
    );
    const requiresConfigSelection = requiresExplicitRepoConfigSelection(
        repo,
        state.activeRepo.config,
        state.activeRepo.mode,
    );
    if (pendingRepoStarts.has(repo.path)) {
        loading.innerHTML = '<div class="spinner"></div><p>Starting repository engine...</p><p>Waiting for engine to become ready.</p>';
        loading.style.display = 'block';
    } else if (port && repoState !== 'not running') {
        // No loading spinner during normal repo open: the repo card
        // already kept Open disabled until startup_complete, so the
        // wait inside the iframe is just the brief boot window
        // (data-booting suppresses content visibility), then the
        // dashboard postMessages ready and we reveal. A spinner card
        // appearing/disappearing on top of that is itself a visible
        // event. The pendingRepoStarts branch above keeps the
        // "Starting repository engine..." spinner because the user
        // shouldn't reach this code path before that's resolved.
        loading.style.display = 'none';

        let loadTimedOut = false;
        const timeout = setTimeout(() => {
            loadTimedOut = true;
            loading.innerHTML = `
                <p>Engine on port ${port} is not responding.</p>
                <button class="btn btn-sm" onclick="loadActivityView('${escapeHtml(repo.path).replace(/'/g, "\\'")}')">Retry</button>
            `;
            // The normal-open path keeps loading hidden (no spinner
            // during a healthy reveal). On failure we have to make the
            // error UI visible — otherwise the user gets a blank pane
            // instead of the "not responding" / Retry message.
            loading.style.display = 'block';
        }, 8000);

        // Reveal on iframe.onload. The dashboard's own
        // visibility:hidden-on-.container suppression (active while
        // data-booting=true) keeps the inner content invisible across
        // the brief post-onload boot window, so revealing here shows
        // the iframe's themed body bg, not raw mutations. Open is
        // already gated on startup_status === complete via the
        // repo-card readiness check, so the SSE reconnect storm that
        // used to drive cold-engine flashes is not in play here.
        iframe.onload = () => {
            clearTimeout(timeout);
            if (!loadTimedOut) {
                loading.style.display = 'none';
                iframe.style.display = 'block';
                try {
                    iframe.contentWindow.postMessage({
                        type: 'cc-repo-info',
                        repoName: repo.name,
                    }, '*');
                } catch (e) { /* cross-origin */ }
            }
        };
        iframe.onerror = () => {
            clearTimeout(timeout);
            loading.innerHTML = `
                <p>Failed to connect to engine on port ${port}.</p>
                <button class="btn btn-sm" onclick="loadActivityView('${escapeHtml(repo.path).replace(/'/g, "\\'")}')">Retry</button>
            `;
            // Same reason as the timeout path above: surface the error
            // since the normal-open path leaves loading hidden.
            loading.style.display = 'block';
        };
        const iframeTheme = state.theme === 'system'
            ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
            : state.theme;
        iframe.src = getRepoDashboardUrl(repo, { embedded: true, theme: iframeTheme });
    } else {
        const pendingStart = isRepoStartPending(repo.path);
        loading.innerHTML = `
            <p>Repository engine is not running.</p>
            ${configIssue ? `<p>${escapeHtml(configIssue)}</p>` : ''}
            ${requiresConfigSelection ? '<p>Select a valid config before starting this repository engine.</p>' : ''}
            <button class="btn btn-primary btn-sm" data-action="start" data-path="${escapeHtml(repo.path)}" ${(pendingStart || requiresConfigSelection) ? 'disabled' : ''}>${pendingStart ? 'Starting...' : 'Start engine'}</button>
            <button class="btn btn-sm" data-action="start-paused" data-path="${escapeHtml(repo.path)}" ${(pendingStart || requiresConfigSelection) ? 'disabled' : ''}>${pendingStart ? 'Starting paused...' : 'Start paused'}</button>
        `;
        loading.style.display = 'block';
        // Attach action handlers to the inline buttons
        loading.querySelectorAll('[data-action]').forEach(btn => {
            btn.addEventListener('click', handleRepoAction);
        });
    }
}

// ============================================
// Repository Actions
// ============================================
async function openRepositorySetup(path, triggerElement = null) {
    const command = controlCenterSetupCommands.buildOpenSetupCommand(path);
    await controlCenterSetupCommands.runOpenSetupCommand(
        command,
        setupWizardController,
        triggerElement,
    );
}

async function handleRepoAction(e) {
    const action = e.currentTarget.dataset.action;
    const path = e.currentTarget.dataset.path;

    switch (action) {
        case 'start':
            await startRepo(path);
            break;
        case 'start-paused':
            await startRepo(path, null, true);
            break;
        case 'stop':
            showRepoStopModal(path, { forceImmediate: false });
            break;
        case 'stop-force':
            showRepoStopModal(path, { forceImmediate: true });
            break;
        case 'view':
            switchView('activity', path);
            break;
        case 'pause':
            await pauseRepo(path);
            break;
        case 'resume':
            await resumeRepo(path);
            break;
        case 'setup':
            await openRepositorySetup(path, e.currentTarget);
            break;
        case 'register':
            await registerDiscoveredRepo(path);
            break;
    }
}

async function registerDiscoveredRepo(path) {
    try {
        const response = await fetch('/control/repos', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ repo_root: path })
        });
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.error || 'Failed to register repository');
        }
        showToast(`Added ${path.split('/').pop()}`, 'success');
        // Refresh both lists so it moves from discovered to registered
        await loadDiscoveredRepos();
        await loadRepos();
        renderDropdownMenu();
    } catch (error) {
        showToast(error.message, 'error');
    }
}

async function startRepo(path, configName = null, startPaused = false, modeName = null) {
    if (pendingRepoStarts.has(path)) {
        return;
    }
    pendingRepoStarts.add(path);
    try {
        if (state.currentView === 'repositories') {
            renderRepos();
        } else if (state.currentView === 'activity' && state.activeRepo?.path === path) {
            loadActivityView(path);
        }
        // Determine config to use:
        // 1. Explicit configName parameter
        // 2. Active repo's config (if same path)
        // 3. Saved default config for this repo
        // 4. First available config from repo
        // 5. Fallback to 'default.yaml'
        let config = configName;
        let mode = modeName;
        if (!config && state.activeRepo?.path === path) {
            config = state.activeRepo.config;
        }
        if (!mode && state.activeRepo?.path === path) {
            mode = state.activeRepo.mode;
        }
        const repo = state.repos.find(r => r.path === path);
        if (repo) {
            mode = getSelectedRepoMode(repo, mode || getDefaultMode(path));
            config = getValidRepoConfig(repo, config, mode);
        }
        if (!config) {
            throw new Error('Select a valid config before starting this repository engine');
        }

        const response = await fetch('/control/orchestrator/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ repo_root: path, config_name: config, mode, start_paused: startPaused })
        });

        if (!response.ok) {
            const error = await response.json();
            if (error.error === 'doctor_failed' && error.doctor) {
                const repoLabel = path.split('/').pop();
                showDoctorResultsModal(
                    `Start Blocked — ${repoLabel}`,
                    error.doctor,
                    error.detail || 'Pre-flight checks failed',
                    'fail',
                    { repoRoot: path, configName: config, mode },
                );
                throw new Error((error.detail || 'Pre-flight checks failed') + '. See Doctor Results.');
            }
            throw new Error(error.detail || 'Failed to start');
        }

        // Select this repo as active
        selectRepo(path, config, 'registered', mode);

        await waitForRepoToBeReady(path);
        await loadRepos();
        // No success toast: the repo card's badge already shows the
        // engine transitioning Initializing… → Running / Paused, and
        // the Open button enabling. A duplicate toast is just noise.
    } catch (error) {
        showToast(error.message, 'error');
    } finally {
        pendingRepoStarts.delete(path);
        if (state.currentView === 'repositories') {
            renderRepos();
        } else if (state.currentView === 'activity' && state.activeRepo?.path === path) {
            loadActivityView(path);
        }
    }
}

async function pauseRepo(path) {
    try {
        const response = await fetch('/control/orchestrator/pause', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ repo_root: path })
        });
        if (!response.ok) {
            throw new Error('Failed to pause repository engine');
        }
        showToast('Repository engine paused', 'success');
        await loadRepos();
    } catch (error) {
        showToast(error.message, 'error');
    }
}

async function resumeRepo(path) {
    try {
        const response = await fetch('/control/orchestrator/resume', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ repo_root: path })
        });
        if (!response.ok) {
            throw new Error('Failed to resume repository engine');
        }
        showToast('Repository engine resumed', 'success');
        await loadRepos();
    } catch (error) {
        showToast(error.message, 'error');
    }
}

function getRepoStopGracefulTimeoutSeconds() {
    const raw = document.getElementById('repoStopGracefulTimeout')?.value;
    const parsed = parseInt(raw || '120', 10);
    return Number.isFinite(parsed) && parsed >= 2 ? parsed : 120;
}

function updateRepoStopOptionControls() {
    const forceImmediate = Boolean(document.getElementById('repoStopModeForce')?.checked);
    const timeoutRow = document.getElementById('repoStopTimeoutRow');
    if (timeoutRow) timeoutRow.style.display = forceImmediate ? 'none' : 'flex';
    const confirmBtn = document.getElementById('confirmRepoStop');
    if (confirmBtn) {
        confirmBtn.textContent = forceImmediate ? 'Force stop engine' : 'Stop engine';
    }
}

function closeRepoStopModal() {
    document.getElementById('repoStopModal')?.classList.remove('active');
    const modal = document.getElementById('repoStopModal');
    if (modal) {
        delete modal.dataset.repoPath;
    }
}

function showRepoStopModal(path, options = {}) {
    if (!path) return;
    const forceImmediate = Boolean(options.forceImmediate);
    const repo = state.repos.find(r => r.path === path);
    const repoName = repo?.name || path;
    const modal = document.getElementById('repoStopModal');
    const summary = document.getElementById('repoStopSummaryText');
    const running = document.getElementById('repoStopRunningText');
    const modeGraceful = document.getElementById('repoStopModeGraceful');
    const modeForce = document.getElementById('repoStopModeForce');
    const timeoutSelect = document.getElementById('repoStopGracefulTimeout');
    if (!modal || !summary || !running || !modeGraceful || !modeForce) return;

    summary.textContent = `Stop repository engine for ${repoName}.`;
    running.textContent = forceImmediate
        ? 'This will attempt immediate force stop.'
        : '';
    modal.dataset.repoPath = path;
    modeForce.checked = forceImmediate;
    modeGraceful.checked = !forceImmediate;
    if (timeoutSelect) {
        timeoutSelect.value = localStorage.getItem('cc.shutdown.graceful-timeout-seconds') || '120';
    }
    updateRepoStopOptionControls();
    modal.classList.add('active');
}

async function confirmRepoStop() {
    const modal = document.getElementById('repoStopModal');
    const path = modal?.dataset.repoPath;
    if (!path) {
        showToast('Missing repository path for stop request', 'error');
        return;
    }
    const forceImmediate = Boolean(document.getElementById('repoStopModeForce')?.checked);
    const gracefulTimeoutSeconds = getRepoStopGracefulTimeoutSeconds();
    localStorage.setItem('cc.shutdown.graceful-timeout-seconds', String(gracefulTimeoutSeconds));
    closeRepoStopModal();
    await stopRepo(path, {
        force: forceImmediate,
        gracefulTimeoutSeconds,
    });
}

async function stopRepo(path, options = {}) {
    if (!path) {
        showToast('Missing repository path for stop request', 'error');
        return;
    }
    if (pendingRepoStops.has(path)) return;

    const repo = state.repos.find(r => r.path === path);
    const force = Boolean(options.force);
    const gracefulTimeoutSeconds = Number.isFinite(options.gracefulTimeoutSeconds)
        ? Math.max(2, options.gracefulTimeoutSeconds)
        : (parseInt(localStorage.getItem('cc.shutdown.graceful-timeout-seconds') || '120', 10) || 120);
    const forceIfTimeout = typeof options.forceIfTimeout === 'boolean'
        ? options.forceIfTimeout
        : !force;

    pendingRepoStops.add(path);
    if (state.currentView === 'repositories') {
        renderRepos();
    } else if (state.currentView === 'activity' && state.activeRepo?.path === path) {
        loadActivityView(path);
    }

    try {
        const stopReason = force
            ? 'operator force-stopped engine via Control Center'
            : 'operator stopped engine via Control Center';
        const response = await fetch('/control/orchestrator/stop', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                repo_root: path,
                force,
                graceful_timeout_seconds: gracefulTimeoutSeconds,
                force_if_timeout: forceIfTimeout,
                reason: stopReason,
                actor: 'control-center.web-ui',
            }),
        });

        let payload = null;
        try {
            payload = await response.json();
        } catch (_) {
            payload = null;
        }

        if (!response.ok) {
            const detail = payload?.detail || payload?.error;
            if (payload?.error === 'global_shutdown_in_progress') {
                throw new Error('Global shutdown is in progress. Use global shutdown controls instead.');
            }
            throw new Error(detail || `Failed to stop repository engine (HTTP ${response.status})`);
        }

        if (payload?.status === 'not_running') {
            showToast('Repository engine was already stopped', 'info');
        } else {
            showToast(
                force
                    ? 'Repository engine force-stopped'
                    : `Repository engine stopping (graceful timeout ~${formatGracefulTimeout(gracefulTimeoutSeconds)}, then force if needed)`,
                'success'
            );
        }

        if (state.currentView === 'activity' && state.activeRepo?.path === path) {
            switchView('repositories');
        }
    } catch (error) {
        const message = error instanceof TypeError
            ? 'Failed to reach Control Center while stopping engine. Refresh and try again.'
            : (error?.message || 'Failed to stop repository engine');
        showToast(message, 'error');
    } finally {
        pendingRepoStops.delete(path);
        try {
            await loadRepos(true);
        } catch (_) {
            // Best effort: UI already showed the stop error above.
        }
        if (state.currentView === 'repositories') {
            renderRepos();
        } else if (state.currentView === 'activity' && state.activeRepo?.path === path) {
            loadActivityView(path);
        }
    }
}

// ============================================
// Doctor
// ============================================
function _renderDoctorResults(data) {
    if (!data) {
        return '';
    }
    let html = '';
    if (data.checks) {
        const renderExpandable = (check) => {
            if (!check.expandable || !check.expandable.per_db) return '';
            const rows = Object.entries(check.expandable.per_db)
                .map(([label, detail]) => {
                    const rawStatus = detail.status || 'info';
                    const statusClass = rawStatus === 'ok'
                        ? 'ok'
                        : rawStatus === 'error'
                            ? 'error'
                            : rawStatus === 'overdue'
                                ? 'warning'
                                : rawStatus === 'missing'
                                    ? 'info'
                                    : rawStatus === 'disabled' || rawStatus === 'skipped'
                                        ? 'info'
                                        : 'warning';
                    const message = detail.detail || '';
                    return `<div class="doctor-subcheck ${statusClass}">${escapeHtml(label)}: ${escapeHtml(message)}</div>`;
                })
                .join('');
            return rows ? `<div class="doctor-subchecks">${rows}</div>` : '';
        };

        const renderCheck = (name, check) => {
            const status = check.status || (check.ok || check.passed ? 'ok' : 'error');
            const icon = status === 'ok' ? '✓' : status === 'warning' ? '!' : status === 'info' ? 'i' : '✗';
            const message = check.message || check.detail || (status === 'ok' ? 'OK' : 'Failed');
            const subchecks = renderExpandable(check);
            html += `<div class="doctor-check ${status === 'ok' ? 'pass' : status === 'warning' ? 'warn' : status === 'info' ? 'warn' : 'fail'}">${icon} ${escapeHtml(name)}: ${escapeHtml(message)}</div>${subchecks}`;
        };

        if (Array.isArray(data.checks)) {
            for (const check of data.checks) {
                renderCheck(check.name || 'Check', check);
            }
        } else {
            for (const [name, check] of Object.entries(data.checks)) {
                renderCheck(name, check);
            }
        }
    }
    if (data.all_ok !== undefined) {
        html += `<div class="doctor-check ${data.all_ok ? 'pass' : 'fail'}">Overall: ${data.all_ok ? 'All checks passed' : 'Some checks failed'}</div>`;
    }
    return html || 'All checks passed!';
}

function getDoctorCheckEntries(data) {
    if (!data?.checks) return [];
    if (Array.isArray(data.checks)) {
        return data.checks.map(check => ({ name: check.name || 'Check', check }));
    }
    return Object.entries(data.checks).map(([name, check]) => ({ name, check }));
}

function getDoctorCheckStatus(check) {
    return check.status || (check.ok || check.passed ? 'ok' : 'error');
}

function hasRepairableRepoGuardrails(data) {
    return getDoctorCheckEntries(data).some(({ name, check }) => {
        const status = getDoctorCheckStatus(check);
        return name === 'Repo Guardrails' && (status === 'error' || status === 'warning');
    });
}

function updateDoctorRepairAction(data) {
    const button = document.getElementById('repairGuardrailsBtn');
    if (!button) return;
    const canRepair = Boolean(doctorModalContext.repoRoot && hasRepairableRepoGuardrails(data));
    button.style.display = canRepair ? '' : 'none';
    button.disabled = false;
    button.textContent = 'Repair Guardrails';
}

function showDoctorResultsModal(title, data, prefixMessage = null, prefixClass = 'info', context = {}) {
    const modal = document.getElementById('doctorModal');
    const modalTitle = document.getElementById('doctorModalTitle');
    const results = document.getElementById('doctorResults');
    doctorModalContext = {
        repoRoot: context.repoRoot || null,
        configName: context.configName || null,
        mode: context.mode || 'default',
        title,
        data,
    };
    modal.classList.add('active');
    modalTitle.textContent = title;
    const prefix = prefixMessage
        ? `<div class="doctor-check ${prefixClass}">${escapeHtml(prefixMessage)}</div>`
        : '';
    results.innerHTML = prefix + _renderDoctorResults(data);
    updateDoctorRepairAction(data);
}

async function runDoctor() {
    const targetPath = state.activeRepo?.path;
    if (!targetPath) {
        showToast('Select a repository first', 'warning');
        return;
    }
    const repo = state.repos.find(r => r.path === targetPath)
        || state.discoveredRepos.find(r => r.path === targetPath);
    const configName = repo
        ? getValidRepoConfig(repo, state.activeRepo?.config, state.activeRepo?.mode)
        : state.activeRepo?.config;
    const mode = repo
        ? getSelectedRepoMode(repo, state.activeRepo?.mode)
        : (state.activeRepo?.mode || 'default');
    const context = { repoRoot: targetPath, configName, mode };
    const results = document.getElementById('doctorResults');
    showDoctorResultsModal(
        `Doctor — ${targetPath.split('/').pop()}`,
        null,
        `Running diagnostics for ${targetPath}...`,
        'info',
        context,
    );
    try {
        const response = await fetch(`/control/orchestrator/doctor?repo_root=${encodeURIComponent(targetPath)}&config_name=${encodeURIComponent(configName)}&mode=${encodeURIComponent(mode)}`);
        showDoctorResultsModal(`Doctor — ${targetPath.split('/').pop()}`, await response.json(), null, 'info', context);
    } catch (error) {
        results.innerHTML = `<div class="doctor-check fail">✗ Failed to run diagnostics: ${escapeHtml(error.message)}</div>`;
    }
}

async function repairGuardrails() {
    const targetPath = doctorModalContext.repoRoot;
    if (!targetPath) {
        showToast('Select a repository first', 'warning');
        return;
    }
    const confirmed = window.confirm(
        'Repair repo guardrails now?\n\n'
        + 'This will overwrite issue-orchestrator managed hook files if they drifted. '
        + 'Existing project pre-push hooks are preserved as pre-push.project when possible.'
    );
    if (!confirmed) {
        return;
    }

    const button = document.getElementById('repairGuardrailsBtn');
    const results = document.getElementById('doctorResults');
    const context = {
        repoRoot: targetPath,
        configName: doctorModalContext.configName,
        mode: doctorModalContext.mode,
    };
    if (button) {
        button.disabled = true;
        button.textContent = 'Repairing...';
    }
    if (results) {
        results.innerHTML = `<div class="doctor-check info">Repairing repo guardrails for ${escapeHtml(targetPath)}...</div>` + _renderDoctorResults(doctorModalContext.data);
    }

    try {
        const response = await fetch('/control/orchestrator/guardrails/repair', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ repo_root: targetPath, config_name: doctorModalContext.configName, mode: doctorModalContext.mode })
        });
        const repairResult = await response.json();
        if (!response.ok) {
            throw new Error(repairResult.detail || repairResult.error || 'Failed to repair guardrails');
        }

        const doctorResponse = await fetch(`/control/orchestrator/doctor?repo_root=${encodeURIComponent(targetPath)}&config_name=${encodeURIComponent(doctorModalContext.configName)}&mode=${encodeURIComponent(doctorModalContext.mode)}`);
        const doctorResult = await doctorResponse.json();
        const fileCount = (repairResult.installed_files || []).length;
        showDoctorResultsModal(
            doctorModalContext.title || `Doctor — ${targetPath.split('/').pop()}`,
            doctorResult,
            `Guardrails repaired (${fileCount} file${fileCount === 1 ? '' : 's'} written). Review and commit changed files if applicable.`,
            'pass',
            context,
        );
        showToast('Repo guardrails repaired', 'success');
    } catch (error) {
        showDoctorResultsModal(
            doctorModalContext.title || `Doctor — ${targetPath.split('/').pop()}`,
            doctorModalContext.data,
            `Repair failed: ${error.message}`,
            'fail',
            context,
        );
        showToast(error.message, 'error');
    }
}

async function runSystemCheck() {
    const modal = document.getElementById('doctorModal');
    const modalTitle = document.getElementById('doctorModalTitle');
    const results = document.getElementById('doctorResults');
    doctorModalContext = { repoRoot: null, configName: null, mode: null, title: 'System Check', data: null };
    updateDoctorRepairAction(null);
    modal.classList.add('active');
    modalTitle.textContent = 'System Check';
    results.innerHTML = '<div class="doctor-check info">Checking prerequisites...</div>';
    try {
        const response = await fetch('/control/setup/prereqs');
        results.innerHTML = _renderDoctorResults(await response.json());
    } catch (error) {
        results.innerHTML = `<div class="doctor-check fail">✗ Failed to run system check: ${escapeHtml(error.message)}</div>`;
    }
}

// ============================================
// Shutdown
// ============================================
function formatGracefulTimeout(seconds) {
    const s = Number(seconds) || 0;
    if (s >= 3600 && s % 3600 === 0) return `${s / 3600}h`;
    if (s >= 60 && s % 60 === 0) return `${s / 60}m`;
    return `${Math.max(1, Math.round(s / 60))}m`;
}

function getShutdownGracefulTimeoutSeconds() {
    const raw = document.getElementById('shutdownGracefulTimeout')?.value;
    const parsed = parseInt(raw || '120', 10);
    return Number.isFinite(parsed) && parsed >= 60 ? parsed : 120;
}

function showControlCenterClosedFallback() {
    document.body.innerHTML = `
        <div style="display:flex;align-items:center;justify-content:center;height:100vh;
                    background:var(--bg-main,#0f1722);color:var(--text-primary,#d7e2ef);
                    font-family:'Segoe UI','SF Pro Text','Helvetica Neue',sans-serif;text-align:center;">
            <div>
                <div style="font-size:48px;margin-bottom:16px;opacity:0.5;">&#x2713;</div>
                <h1 style="font-size:20px;font-weight:600;margin:0 0 8px;">Control Center closed</h1>
                <p style="color:var(--text-secondary,#90a5bf);margin:0;font-size:14px;">
                    Shutdown has completed.<br>
                    You can close this tab.
                </p>
            </div>
        </div>
    `;
}

function attemptControlCenterTabClose() {
    if (shutdownCloseAttempted) return;
    shutdownCloseAttempted = true;
    setTimeout(() => {
        window.close();
        setTimeout(() => {
            showControlCenterClosedFallback();
        }, 200);
    }, 350);
}

function updateShutdownOptionControls() {
    const stopEngines = document.getElementById('shutdownStopOrchestrators');
    const modeGraceful = document.getElementById('shutdownModeGraceful');
    const modeForce = document.getElementById('shutdownModeForce');
    const timeoutRow = document.getElementById('shutdownTimeoutRow');
    const enabled = Boolean(stopEngines?.checked);
    const forceImmediate = Boolean(modeForce?.checked);
    if (timeoutRow) timeoutRow.style.display = enabled && !forceImmediate ? 'flex' : 'none';
    if (modeGraceful) {
        modeGraceful.disabled = !enabled;
        if (!enabled) modeGraceful.checked = true;
    }
    if (modeForce) {
        modeForce.disabled = !enabled;
        if (!enabled) modeForce.checked = false;
    }
}

function _clearShutdownModalAnchor() {
    const backdrop = document.getElementById('shutdownModal');
    const modal = backdrop?.querySelector('.modal');
    backdrop?.classList.remove('anchor-near-trigger');
    if (modal) {
        modal.style.left = '';
        modal.style.top = '';
    }
}

function hideShutdownModal() {
    const backdrop = document.getElementById('shutdownModal');
    backdrop?.classList.remove('active');
    _clearShutdownModalAnchor();
}

function _positionShutdownModalNear(anchorRect) {
    const backdrop = document.getElementById('shutdownModal');
    const modal = backdrop?.querySelector('.modal');
    if (!backdrop || !modal || !anchorRect) return;
    backdrop.classList.add('anchor-near-trigger');
    const gap = 10;
    const padding = 8;
    const rect = modal.getBoundingClientRect();
    let left = anchorRect.left;
    let top = anchorRect.bottom + gap;
    if (left + rect.width > window.innerWidth - padding) {
        left = window.innerWidth - rect.width - padding;
    }
    if (left < padding) left = padding;
    if (top + rect.height > window.innerHeight - padding) {
        top = anchorRect.top - rect.height - gap;
    }
    if (top < padding) top = padding;
    modal.style.left = `${Math.round(left)}px`;
    modal.style.top = `${Math.round(top)}px`;
}

function isRepoEngineActive(repo) {
    const status = repo?.status || {};
    const stateValue = String(status.state || '').toLowerCase();
    if (status.paused) return true;
    if (stateValue === 'running' || stateValue === 'partial') return true;
    // Treat transition states as active so we don't skip confirmation while status settles.
    if (stateValue === 'starting' || stateValue === 'stopping') return true;
    const path = repo?.path;
    if (path && (state.pendingStarts?.has(path) || state.pendingStops?.has(path))) return true;
    return false;
}

async function showShutdownModal(anchorRect = null) {
    let reposRefreshed = true;
    try {
        await loadRepos(true);
    } catch (_) {
        // If Control Center API is unavailable, do not auto-close based on stale state.
        reposRefreshed = false;
    }

    const summary = document.getElementById('shutdownSummaryText');
    const running = document.getElementById('shutdownRunningText');
    const stopEngines = document.getElementById('shutdownStopOrchestrators');
    const modeGraceful = document.getElementById('shutdownModeGraceful');
    const modeForce = document.getElementById('shutdownModeForce');
    const timeoutSelect = document.getElementById('shutdownGracefulTimeout');
    if (stopEngines) stopEngines.checked = false;
    if (modeGraceful) modeGraceful.checked = true;
    if (modeForce) modeForce.checked = false;
    if (timeoutSelect) {
        timeoutSelect.value = localStorage.getItem('cc.shutdown.graceful-timeout-seconds') || '120';
    }
    updateShutdownOptionControls();

    const active = state.repos.filter((repo) => isRepoEngineActive(repo));

    // Nothing running — close immediately, but only when state is freshly confirmed.
    if (reposRefreshed && active.length === 0) {
        showToast('Shutting down...');
        confirmShutdown();
        return;
    }

    if (summary) {
        summary.textContent = 'This closes the Control Center window and dashboard server. Repository engines keep running independently.';
    }
    if (running) {
        if (!reposRefreshed) {
            running.textContent = 'Could not verify running engines. Choose options explicitly.';
        } else if (active.length === 1) {
            running.textContent = `1 repository engine remains active: ${active[0].name}.`;
        } else {
            const names = active.slice(0, 3).map(r => r.name).join(', ');
            const suffix = active.length > 3 ? ` (+${active.length - 3} more)` : '';
            running.textContent = `${active.length} repository engines remain active: ${names}${suffix}.`;
        }
    }
    const shutdownModal = document.getElementById('shutdownModal');
    shutdownModal.classList.add('active');
    if (anchorRect) {
        _positionShutdownModalNear(anchorRect);
    } else {
        _clearShutdownModalAnchor();
    }
}

async function confirmShutdown() {
    try {
        let stopOrchestrators = Boolean(document.getElementById('shutdownStopOrchestrators')?.checked);
        const forceOrchestrators = Boolean(document.getElementById('shutdownModeForce')?.checked) && stopOrchestrators;
        const gracefulTimeoutSeconds = getShutdownGracefulTimeoutSeconds();
        localStorage.setItem('cc.shutdown.graceful-timeout-seconds', String(gracefulTimeoutSeconds));
        try {
            await loadRepos(true);
        } catch (_) {
            // Best-effort refresh; fall back to current state.
        }
        const activeCount = state.repos.filter((repo) => isRepoEngineActive(repo)).length;
        if (activeCount === 0) {
            stopOrchestrators = false;
        }
        if (stopOrchestrators) {
            const confirmMsg = forceOrchestrators
                ? 'Close Control Center and force-stop running repository engines now?'
                : `Close Control Center and stop running repository engines (graceful timeout ~${formatGracefulTimeout(gracefulTimeoutSeconds)}, then force if needed)?`;
            if (!window.confirm(confirmMsg)) {
                return;
            }
        }
        const response = await fetch('/control/shutdown', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                stop_orchestrators: stopOrchestrators,
                force_orchestrators: forceOrchestrators,
                graceful_timeout_seconds: gracefulTimeoutSeconds,
            }),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(payload?.error || `Failed to close Control Center (HTTP ${response.status})`);
        }
        const superseded = Array.isArray(payload?.superseded_engine_shutdowns)
            ? payload.superseded_engine_shutdowns
            : [];
        if (superseded.length > 0) {
            showToast('There were other shutdowns in progress; global shutdown superseded them.', 'warning');
        }
        hideShutdownModal();
        if (stopOrchestrators) {
            shutdownExpectClose = true;
            showToast('Global shutdown started. You can monitor or adjust it from the shutdown panel.', 'info');
            await refreshShutdownHud();
            return;
        }
        attemptControlCenterTabClose();
    } catch (error) {
        const message = error?.message || 'unknown error';
        console.error('Failed to close Control Center:', error);
        showToast(`Failed to close Control Center: ${message}`, 'error');
    }
}

async function refreshShutdownHud() {
    const hud = document.getElementById('shutdownHud');
    const hudState = document.getElementById('shutdownHudState');
    const hudMeta = document.getElementById('shutdownHudMeta');
    const abortBtn = document.getElementById('shutdownHudAbort');
    const changeBtn = document.getElementById('shutdownHudChange');
    const forceBtn = document.getElementById('shutdownHudForce');
    if (!hud || !hudState || !hudMeta || !abortBtn || !changeBtn || !forceBtn) return;
    try {
        const response = await fetch('/control/shutdown/state');
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        const payload = await response.json();
        const op = payload?.global_shutdown;
        if (!op) {
            hud.classList.remove('visible');
            return;
        }

        const stateLabel = String(op.state || 'unknown').replaceAll('_', ' ');
        hudState.textContent = `State: ${stateLabel}`;
        const currentRepo = op.current_repo ? `Current: ${op.current_repo}` : 'Current: —';
        const progress = `${op.completed_repos || 0}/${op.total_repos || 0}`;
        const failed = Array.isArray(op.failed_orchestrators) ? op.failed_orchestrators : [];
        const forceMode = Boolean(op.force_orchestrators || op.force_now_requested);
        const modeLabel = forceMode
            ? 'Mode: immediate force'
            : `Mode: graceful (~${formatGracefulTimeout(op.graceful_timeout_seconds || 120)}), then force if needed`;
        const failedSuffix = failed.length > 0
            ? ` · Failed: ${failed.length} (${failed.slice(0, 2).join(', ')}${failed.length > 2 ? ', …' : ''})`
            : '';
        hudMeta.textContent = `${currentRepo} · Progress: ${progress} · ${modeLabel}${failedSuffix}`;
        hud.classList.add('visible');

        const inProgress = op.state === 'in_progress';
        abortBtn.disabled = !inProgress;
        changeBtn.disabled = !inProgress;
        forceBtn.disabled = !inProgress;
        if (op.state === 'completed' && op.stop_orchestrators) {
            hudState.textContent = 'State: completed';
            hudMeta.textContent = 'Control Center is closing...';
            attemptControlCenterTabClose();
        }
    } catch (_) {
        // If Control Center is gone, avoid stale "in progress" text in a lingering tab.
        if (shutdownExpectClose) {
            showControlCenterClosedFallback();
            return;
        }
        hud.classList.add('visible');
        hudState.textContent = 'State: control center unavailable';
        hudMeta.textContent = 'Shutdown status cannot be refreshed. The Control Center may already be closed.';
        abortBtn.disabled = true;
        changeBtn.disabled = true;
        forceBtn.disabled = true;
    }
}

async function requestShutdownAbort() {
    try {
        const response = await fetch('/control/shutdown/abort', { method: 'POST' });
        if (!response.ok) {
            throw new Error('No shutdown in progress');
        }
        showToast('Shutdown abort requested.', 'warning');
        await refreshShutdownHud();
    } catch (error) {
        showToast(error.message || 'Failed to abort shutdown', 'error');
    }
}

async function requestShutdownForceNow() {
    try {
        const response = await fetch('/control/shutdown/force', { method: 'POST' });
        if (!response.ok) {
            throw new Error('No shutdown in progress');
        }
        showToast('Force-stop escalation requested.', 'warning');
        await refreshShutdownHud();
    } catch (error) {
        showToast(error.message || 'Failed to force shutdown', 'error');
    }
}

async function requestShutdownChangeTimeout() {
    const current = getShutdownGracefulTimeoutSeconds();
    const raw = window.prompt('Set graceful timeout (minutes, 1-60):', String(Math.max(1, Math.round(current / 60))));
    if (raw === null) return;
    const parsedMinutes = parseInt(raw, 10);
    if (!Number.isFinite(parsedMinutes) || parsedMinutes < 1 || parsedMinutes > 60) {
        showToast('Timeout must be between 1 and 60 minutes.', 'error');
        return;
    }
    const parsed = parsedMinutes * 60;
    try {
        const response = await fetch('/control/shutdown/update', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ graceful_timeout_seconds: parsed }),
        });
        if (!response.ok) {
            throw new Error('No shutdown in progress');
        }
        showToast(`Shutdown timeout updated to ${formatGracefulTimeout(parsed)}.`, 'success');
        await refreshShutdownHud();
    } catch (error) {
        showToast(error.message || 'Failed to update shutdown timeout', 'error');
    }
}

function setupShutdownHudDraggable() {
    const hud = document.getElementById('shutdownHud');
    const head = hud?.querySelector('.shutdown-hud-head');
    if (!hud || !head) return;

    const savedPos = localStorage.getItem('cc.shutdown.hud.position');
    if (savedPos) {
        try {
            const parsed = JSON.parse(savedPos);
            if (Number.isFinite(parsed.left) && Number.isFinite(parsed.top)) {
                hud.style.left = `${parsed.left}px`;
                hud.style.top = `${parsed.top}px`;
                hud.style.right = 'auto';
                hud.style.bottom = 'auto';
            }
        } catch (_) {
            // Ignore invalid stored value
        }
    }

    let dragging = false;
    let offsetX = 0;
    let offsetY = 0;

    head.addEventListener('mousedown', (event) => {
        if (event.target.closest('button')) return;
        const rect = hud.getBoundingClientRect();
        dragging = true;
        offsetX = event.clientX - rect.left;
        offsetY = event.clientY - rect.top;
        hud.style.right = 'auto';
        hud.style.bottom = 'auto';
        event.preventDefault();
    });

    document.addEventListener('mousemove', (event) => {
        if (!dragging) return;
        const nextLeft = Math.min(
            Math.max(8, event.clientX - offsetX),
            Math.max(8, window.innerWidth - hud.offsetWidth - 8),
        );
        const nextTop = Math.min(
            Math.max(8, event.clientY - offsetY),
            Math.max(8, window.innerHeight - hud.offsetHeight - 8),
        );
        hud.style.left = `${nextLeft}px`;
        hud.style.top = `${nextTop}px`;
    });

    document.addEventListener('mouseup', () => {
        if (!dragging) return;
        dragging = false;
        const rect = hud.getBoundingClientRect();
        localStorage.setItem(
            'cc.shutdown.hud.position',
            JSON.stringify({ left: rect.left, top: rect.top }),
        );
    });
}

// ============================================
// Toasts
// ============================================
function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(() => {
        toast.style.opacity = '0';
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

// ============================================
// Utilities
// ============================================
function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function parseJsonInput(text, fallback) {
    if (!text || !text.trim()) return fallback;
    return JSON.parse(text);
}

function parseLines(text) {
    if (!text) return [];
    return text.split('\n').map(line => line.trim()).filter(Boolean);
}

function setOutput(id, data) {
    const el = document.getElementById(id);
    if (!el) return;
    if (typeof data === 'string') {
        el.textContent = data;
        return;
    }
    el.textContent = JSON.stringify(data, null, 2);
}

function renderGoalPilotStatus(status, skillsCount = null) {
    const container = document.getElementById('goalPilotStatusOutput');
    if (!container) return;
    if (!status || !status.run) {
        container.innerHTML = '<div class="goal-pilot-output">No status available.</div>';
        return;
    }

    const run = status.run;
    const snapshot = status.latest_snapshot;
    const actions = status.actions || [];
    const notes = status.notes || [];

    const goals = run.goals || [];
    const doneCriteria = run.done_criteria || {};
    const recentActions = actions.slice(-5).reverse();
    const recentNotes = notes.slice(-5).reverse();

    const summaryItems = snapshot && snapshot.summary ? Object.entries(snapshot.summary) : [];
    const lastAction = recentActions[0];
    const lastNote = recentNotes[0];
    const actionsCount = actions.length;
    const notesCount = notes.length;
    const skillsValue = skillsCount === null ? '—' : String(skillsCount);

    container.innerHTML = `
        <div class="goal-pilot-hero">
            <div class="goal-pilot-metric">
                <div class="goal-pilot-metric-value">${escapeHtml(run.status || 'unknown')}</div>
                <div class="goal-pilot-metric-label">Run Status</div>
            </div>
            <div class="goal-pilot-metric">
                <div class="goal-pilot-metric-value">${goals.length}</div>
                <div class="goal-pilot-metric-label">Goals</div>
            </div>
            <div class="goal-pilot-metric">
                <div class="goal-pilot-metric-value">${actionsCount}</div>
                <div class="goal-pilot-metric-label">Actions Logged</div>
            </div>
            <div class="goal-pilot-metric">
                <div class="goal-pilot-metric-value">${skillsValue}</div>
                <div class="goal-pilot-metric-label">Skills Active</div>
            </div>
        </div>

        <div class="goal-pilot-status-grid">
            <div class="goal-pilot-status-item">
                <div class="goal-pilot-status-label">Run Context</div>
                <div class="goal-pilot-status-value">
                    <span class="goal-pilot-pill">${escapeHtml(run.status || 'unknown')}</span>
                    <div style="margin-top: 6px;">${escapeHtml(run.name || 'Unnamed run')}</div>
                    <div style="margin-top: 4px; color: var(--text-muted);">${escapeHtml(run.run_id || '')}</div>
                    <div style="margin-top: 4px; color: var(--text-muted);">Updated: ${escapeHtml(run.updated_at || '')}</div>
                </div>
            </div>
            <div class="goal-pilot-status-item">
                <div class="goal-pilot-status-label">Goals</div>
                <ul class="goal-pilot-list">
                    ${goals.length ? goals.map(goal => `<li>${escapeHtml(goal)}</li>`).join('') : '<li>No goals set</li>'}
                </ul>
            </div>
            <div class="goal-pilot-status-item">
                <div class="goal-pilot-status-label">Done Criteria</div>
                <div class="goal-pilot-status-value">${escapeHtml(JSON.stringify(doneCriteria))}</div>
            </div>
            <div class="goal-pilot-status-item">
                <div class="goal-pilot-status-label">Snapshot</div>
                <div class="goal-pilot-status-value">
                    ${snapshot ? escapeHtml(snapshot.created_at || '') : 'No snapshot yet'}
                </div>
            </div>
        </div>

        <div class="goal-pilot-status-grid">
            <div class="goal-pilot-status-item">
                <div class="goal-pilot-status-label">Progress</div>
                <ul class="goal-pilot-list">
                    ${summaryItems.length ? summaryItems.map(([key, value]) => `<li>${escapeHtml(key)}: ${escapeHtml(String(value))}</li>`).join('') : '<li>No progress summary</li>'}
                </ul>
            </div>
            <div class="goal-pilot-status-item">
                <div class="goal-pilot-status-label">Recent Actions</div>
                <ul class="goal-pilot-list">
                    ${recentActions.length ? recentActions.map(action => `<li>${escapeHtml(action.action_type || '')} (${escapeHtml(action.status || '')})</li>`).join('') : '<li>No actions yet</li>'}
                </ul>
                ${lastAction ? `<div style="margin-top: 8px; color: var(--text-muted); font-size: 11px;">Last: ${escapeHtml(lastAction.action_type || '')}</div>` : ''}
            </div>
            <div class="goal-pilot-status-item">
                <div class="goal-pilot-status-label">Recent Notes</div>
                <ul class="goal-pilot-list">
                    ${recentNotes.length ? recentNotes.map(note => `<li>${escapeHtml(note.note_type || '')}: ${escapeHtml(note.note_text || '')}</li>`).join('') : '<li>No notes yet</li>'}
                </ul>
                ${lastNote ? `<div style="margin-top: 8px; color: var(--text-muted); font-size: 11px;">Last: ${escapeHtml(lastNote.note_type || '')}</div>` : ''}
            </div>
        </div>
    `;
}

function renderWorktreeAudit(data) {
    const worktrees = data.worktrees || [];
    let html = `<p>${escapeHtml(data.message)}</p>`;
    if (worktrees.length > 0) {
        const candidates = worktrees.filter(wt => wt.disposition === 'cleanup_candidate');
        const managed = worktrees.filter(wt => wt.disposition === 'managed');
        const retained = worktrees.filter(wt => wt.disposition === 'retained');
        html += `<p><strong>${candidates.length}</strong> cleanup candidate(s), <strong>${managed.length}</strong> managed issue worktree(s), and <strong>${retained.length}</strong> retained worktree(s).</p>`;
        html += '<ul style="margin: 8px 0; padding-left: 20px;">';
        worktrees.forEach(wt => {
            html += `<li><code>${escapeHtml(wt.path)}</code><br><span>${escapeHtml(wt.kind)} — ${escapeHtml(wt.disposition)}: ${escapeHtml(wt.reason)}</span></li>`;
        });
        html += '</ul>';
    }
    if (typeof data.issue_cleanup_enabled === 'boolean') {
        const status = data.issue_cleanup_enabled ? 'enabled' : 'disabled';
        html += `<p><strong>Review-gated issue cleanup:</strong> ${status} in the selected config.</p>`;
    }
    if (data.note) {
        html += `<p>${escapeHtml(data.note)}</p>`;
    }
    return html;
}

async function requestWorktreeAudit(repoPath) {
    try {
        const response = await fetch('/control/tools/worktrees/cleanup', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ repo_root: repoPath })
        });
        let data;
        try {
            data = await response.json();
        } catch (_error) {
            data = {};
        }
        if (!response.ok) {
            const detail = data.error || data.detail || 'Worktree audit failed';
            return `<div class="error-message">${escapeHtml(detail)}</div>`;
        }
        return renderWorktreeAudit(data);
    } catch (error) {
        return `<div class="error-message">Failed to audit worktrees: ${escapeHtml(error.message)}</div>`;
    }
}

// ============================================
// Event Listeners
// ============================================
document.addEventListener('DOMContentLoaded', () => {
    // Apply initial theme
    applyTheme(state.theme);

    // Load repos
    loadRepos();
    loadSystemState();

    // Navigation
    document.querySelectorAll('.nav-item[data-view]').forEach(item => {
        item.addEventListener('click', () => switchView(item.dataset.view));
    });

    // Back to repos from consolidated header
    document.getElementById('consolidatedBack').addEventListener('click', () => {
        switchView('repositories');
    });

    // Theme selector buttons (in Settings view)
    document.querySelectorAll('.theme-btn').forEach(btn => {
        btn.addEventListener('click', () => applyTheme(btn.dataset.theme));
    });

    document.getElementById('rescanReposBtn').addEventListener('click', rescanRepos);
    document.getElementById('reconnectEnginesBtn').addEventListener('click', reconnectToActiveEngine);
    document.getElementById('cleanupRecoveryBtn').addEventListener('click', cleanRecoveryState);

    // Scope note (dismissible)
    if (!localStorage.getItem('cc-scope-note-dismissed')) {
        document.getElementById('scopeNote').style.display = '';
    }
    document.getElementById('dismissScopeNote').addEventListener('click', () => {
        document.getElementById('scopeNote').style.display = 'none';
        localStorage.setItem('cc-scope-note-dismissed', '1');
    });

    // Doctor (Tools view — repo scope) and System Check (Settings — CC scope)
    document.getElementById('doctorTool').addEventListener('click', runDoctor);
    document.getElementById('systemCheckBtn').addEventListener('click', runSystemCheck);
    document.getElementById('repairGuardrailsBtn').addEventListener('click', repairGuardrails);
    document.getElementById('closeDoctorModal').addEventListener('click', () => {
        document.getElementById('doctorModal').classList.remove('active');
    });
    document.getElementById('closeDoctorBtn').addEventListener('click', () => {
        document.getElementById('doctorModal').classList.remove('active');
    });

    // Shutdown
    document.getElementById('sidebarCloseCC').addEventListener('click', (event) => {
        const triggerRect = event.currentTarget?.getBoundingClientRect?.() || null;
        closeSidebarAppMenu();
        showShutdownModal(triggerRect);
    });
    document.getElementById('closeShutdownModal').addEventListener('click', () => {
        hideShutdownModal();
    });
    document.getElementById('cancelShutdown').addEventListener('click', () => {
        hideShutdownModal();
    });
    document.getElementById('confirmShutdown').addEventListener('click', confirmShutdown);
    document.getElementById('shutdownStopOrchestrators')?.addEventListener('change', updateShutdownOptionControls);
    document.getElementById('shutdownGracefulTimeout')?.addEventListener('change', updateShutdownOptionControls);
    document.getElementById('shutdownModeGraceful')?.addEventListener('change', updateShutdownOptionControls);
    document.getElementById('shutdownModeForce')?.addEventListener('change', updateShutdownOptionControls);
    updateShutdownOptionControls();
    document.getElementById('closeRepoStopModal')?.addEventListener('click', closeRepoStopModal);
    document.getElementById('cancelRepoStop')?.addEventListener('click', closeRepoStopModal);
    document.getElementById('confirmRepoStop')?.addEventListener('click', confirmRepoStop);
    document.getElementById('repoStopModeGraceful')?.addEventListener('change', updateRepoStopOptionControls);
    document.getElementById('repoStopModeForce')?.addEventListener('change', updateRepoStopOptionControls);
    document.getElementById('repoStopGracefulTimeout')?.addEventListener('change', updateRepoStopOptionControls);
    updateRepoStopOptionControls();
    document.getElementById('shutdownHudAbort')?.addEventListener('click', requestShutdownAbort);
    document.getElementById('shutdownHudChange')?.addEventListener('click', requestShutdownChangeTimeout);
    document.getElementById('shutdownHudForce')?.addEventListener('click', requestShutdownForceNow);
    document.getElementById('shutdownHudHide')?.addEventListener('click', () => {
        document.getElementById('shutdownHud')?.classList.remove('visible');
    });
    setupShutdownHudDraggable();
    refreshShutdownHud();

    // Tools - helper to get current repo (defined in outer scope, see below DOMContentLoaded)

    // Goal Pilot
    const goalPilotState = {
        runId: null,
        runs: [],
        journeys: [],
        actions: [],
        run: null
    };

    function parseGoalsInput(raw) {
        const text = (raw || '').trim();
        if (!text) return [];
        const lines = text.split('\n').map(line => line.replace(/^[-*]\s+/, '').trim()).filter(Boolean);
        if (lines.length > 1) return lines;
        return text.split(';').map(part => part.trim()).filter(Boolean);
    }

    function buildDoneCriteria() {
        return {
            journeys_mapped: document.getElementById('goalPilotDoneJourneys').checked,
            milestones_mapped: document.getElementById('goalPilotDoneMilestones').checked,
            tests_planned: document.getElementById('goalPilotDoneTests').checked,
            guardrails_planned: document.getElementById('goalPilotDoneGuardrails').checked,
            notes: document.getElementById('goalPilotDoneNotes').value.trim()
        };
    }

    async function goalPilotConfig() {
        const banner = document.getElementById('goalPilotConfigAlert');
        const details = document.getElementById('goalPilotConfigDetails');
        if (!banner || !details) return;
        try {
            const response = await fetch('/control/goal_pilot/config');
            const data = await response.json();
            if (data.error) throw new Error(data.error);
            if (data.configured) {
                banner.style.display = 'none';
                details.textContent = '';
                return;
            }
            const enabled = data.enabled ? 'enabled' : 'disabled';
            const agent = data.agent || 'none';
            const policy = data.approval_policy || 'journeys_only';
            banner.style.display = 'flex';
            details.textContent = `Status: ${enabled}. Agent: ${agent}. Policy: ${policy}.`;
        } catch (error) {
            banner.style.display = 'flex';
            details.textContent = `Status: unknown (${error.message}).`;
        }
    }

    async function goalPilotLoadRuns() {
        try {
            const response = await fetch('/control/goal_pilot/runs');
            const data = await response.json();
            if (data.error) throw new Error(data.error);
            goalPilotState.runs = Array.isArray(data.runs) ? data.runs : [];
            renderGoalPilotRuns();
        } catch (error) {
            showToast(`Goal Pilot runs failed: ${error.message}`, 'error');
        }
    }

    function renderGoalPilotRuns() {
        const list = document.getElementById('goalPilotRunList');
        if (!list) return;
        if (goalPilotState.runs.length === 0) {
            list.innerHTML = '<div class="goal-pilot-hint">No runs yet. Create one to begin.</div>';
            return;
        }
        list.innerHTML = goalPilotState.runs.map(run => {
            const isActive = goalPilotState.runId === run.run_id;
            const goalsCount = Array.isArray(run.goals) ? run.goals.length : 0;
            return `
                <div class="goal-pilot-run-item ${isActive ? 'active' : ''}" data-run="${run.run_id}">
                    <div class="goal-pilot-run-title">${escapeHtml(run.name || run.run_id)}</div>
                    <div class="goal-pilot-run-meta">Phase: ${escapeHtml(run.phase || 'n/a')} · Goals: ${goalsCount}</div>
                    <div class="goal-pilot-run-meta">Updated: ${escapeHtml(run.updated_at || '')}</div>
                </div>
            `;
        }).join('');
        list.querySelectorAll('.goal-pilot-run-item').forEach(item => {
            item.addEventListener('click', () => {
                const runId = item.dataset.run;
                if (!runId) return;
                goalPilotSelectRun(runId);
            });
        });
    }

    async function goalPilotSelectRun(runId) {
        goalPilotState.runId = runId;
        renderGoalPilotRuns();
        await goalPilotStatus();
    }

    async function goalPilotCreate() {
        try {
            const goals = parseGoalsInput(document.getElementById('goalPilotGoals').value);
            const doneCriteria = buildDoneCriteria();
            const name = document.getElementById('goalPilotRunName').value.trim() || null;
            if (!name) throw new Error('Run name required');
            const response = await fetch('/control/goal_pilot/runs', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ goals, done_criteria: doneCriteria, name })
            });
            const data = await response.json();
            if (data.error) throw new Error(data.error);
            setOutput('goalPilotCreateOutput', data);
            if (data.run_id) {
                await goalPilotLoadRuns();
                await goalPilotSelectRun(data.run_id);
            }
        } catch (error) {
            showToast(`Goal Pilot create failed: ${error.message}`, 'error');
        }
    }

    async function goalPilotStatus() {
        try {
            const runId = goalPilotState.runId;
            if (!runId) throw new Error('Select a run first');
            const statusResponse = await fetch(`/control/goal_pilot/runs/${encodeURIComponent(runId)}`);
            const statusData = await statusResponse.json();
            if (statusData.error) throw new Error(statusData.error);
            const status = statusData.status || statusData;
            goalPilotState.run = status.run || null;
            goalPilotState.journeys = status.journeys || [];
            goalPilotState.actions = status.actions || [];
            renderGoalPilotJourneys();
            renderGoalPilotPhaseHistory(status.phase_history || [], status.run);
            renderGoalPilotSuggestedChanges(goalPilotState.actions);
            renderGoalPilotRuns();
            syncGoalPilotRefineForm();
        } catch (error) {
            showToast(`Goal Pilot status failed: ${error.message}`, 'error');
        }
    }

    function syncGoalPilotRefineForm() {
        const run = goalPilotState.run;
        const activeLabel = document.getElementById('goalPilotActiveRun');
        if (activeLabel) {
            activeLabel.textContent = run ? (run.name || run.run_id || 'Selected') : 'None';
        }
        const goalsField = document.getElementById('goalPilotRefineGoals');
        if (!goalsField || !run) return;
        const goals = Array.isArray(run.goals) ? run.goals : [];
        goalsField.value = goals.join('\n');
    }

    function renderGoalPilotPhaseHistory(history, run) {
        const output = document.getElementById('goalPilotPhaseHistory');
        const selector = document.getElementById('goalPilotPhaseSelect');
        if (!output) return;
        if (selector && run && run.phase) {
            selector.value = run.phase;
        }
        if (!history.length) {
            output.innerHTML = '<div class="goal-pilot-hint">No phase changes yet.</div>';
            return;
        }
        output.innerHTML = history.map(item => `
            <div class="goal-pilot-run-meta">${escapeHtml(item.created_at || '')} — ${escapeHtml(item.from_phase)} → ${escapeHtml(item.to_phase)} · ${escapeHtml(item.reason || '')}</div>
        `).join('');
    }

    async function goalPilotPhaseChange() {
        try {
            const runId = goalPilotState.runId;
            if (!runId) throw new Error('Select a run first');
            const phase = document.getElementById('goalPilotPhaseSelect').value;
            const reason = document.getElementById('goalPilotPhaseReason').value.trim();
            if (!reason) throw new Error('Reason required');
            const response = await fetch(`/control/goal_pilot/runs/${encodeURIComponent(runId)}/phase`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ phase, reason })
            });
            const data = await response.json();
            if (data.error) throw new Error(data.error);
            await goalPilotStatus();
        } catch (error) {
            showToast(`Goal Pilot phase change failed: ${error.message}`, 'error');
        }
    }

    function renderGoalPilotJourneys() {
        const container = document.getElementById('goalPilotJourneyList');
        if (!container) return;
        if (!goalPilotState.journeys.length) {
            container.innerHTML = '<div class="goal-pilot-hint">No journeys yet. Add the first critical user journey.</div>';
            return;
        }
        container.innerHTML = goalPilotState.journeys.map((journey, index) => {
            const lookahead = journey.lookahead || {};
            const lookaheadStatus = lookahead.status || 'green';
            const lookaheadNote = lookahead.note || '';
            const under = journey.under_the_covers || {};
            const badges = [
                `<span class="goal-pilot-pill">${escapeHtml(journey.priority || 'medium')}</span>`,
                `<span class="goal-pilot-pill ${lookaheadStatus === 'green' ? 'success' : lookaheadStatus === 'yellow' ? 'warning' : 'danger'}">Future fit: ${escapeHtml(lookaheadStatus)}</span>`
            ];
            if (journey.milestone) {
                badges.push(`<span class="goal-pilot-pill">Milestone: ${escapeHtml(journey.milestone)}</span>`);
            }
            return `
                <div class="goal-pilot-journey">
                    <div class="goal-pilot-journey-header">
                        <div class="goal-pilot-journey-title">${index + 1}. ${escapeHtml(journey.title)}</div>
                        <div class="goal-pilot-journey-meta">${escapeHtml(journey.status || 'planned')}</div>
                    </div>
                    <div class="goal-pilot-journey-meta">${escapeHtml(journey.description || '')}</div>
                    <div class="goal-pilot-journey-meta">Success: ${escapeHtml(journey.success_criteria || 'Not set')}</div>
                    <div class="goal-pilot-badges">${badges.join('')}</div>
                    <div class="goal-pilot-journey-meta">Look-ahead: ${escapeHtml(lookaheadNote || 'No conflicts noted')}</div>
                    <div class="goal-pilot-journey-meta">Under the covers: ${Object.keys(under).filter(key => under[key]).map(key => key.replace('_', ' ')).join(', ') || 'None set'}</div>
                    <div class="goal-pilot-journey-actions">
                        <button class="btn btn-xs" data-move="up" data-journey="${journey.journey_id}">Move Up</button>
                        <button class="btn btn-xs" data-move="down" data-journey="${journey.journey_id}">Move Down</button>
                    </div>
                </div>
            `;
        }).join('');
        container.querySelectorAll('button[data-move]').forEach(btn => {
            btn.addEventListener('click', async () => {
                const journeyId = btn.dataset.journey;
                const direction = btn.dataset.move;
                await reorderJourney(journeyId, direction);
            });
        });
    }

    async function reorderJourney(journeyId, direction) {
        const index = goalPilotState.journeys.findIndex(j => j.journey_id === journeyId);
        if (index === -1) return;
        const swapWith = direction === 'up' ? index - 1 : index + 1;
        if (swapWith < 0 || swapWith >= goalPilotState.journeys.length) return;
        const reordered = [...goalPilotState.journeys];
        [reordered[index], reordered[swapWith]] = [reordered[swapWith], reordered[index]];
        const order = reordered.map(j => j.journey_id);
        const runId = goalPilotState.runId;
        try {
            const response = await fetch(`/control/goal_pilot/runs/${encodeURIComponent(runId)}/journeys/reorder`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ order })
            });
            const data = await response.json();
            if (data.error) throw new Error(data.error);
            goalPilotState.journeys = reordered;
            renderGoalPilotJourneys();
        } catch (error) {
            showToast(`Goal Pilot reorder failed: ${error.message}`, 'error');
        }
    }

    async function goalPilotCreateJourney() {
        try {
            const runId = goalPilotState.runId;
            if (!runId) throw new Error('Select a run first');
            const title = document.getElementById('goalPilotJourneyTitle').value.trim();
            if (!title) throw new Error('Journey title required');
            const underTheCovers = {};
            document.querySelectorAll('[data-under]').forEach(el => {
                underTheCovers[el.dataset.under] = el.checked;
            });
            const lookaheadStatus = document.getElementById('goalPilotJourneyLookahead').value;
            const lookaheadNote = document.getElementById('goalPilotJourneyLookaheadNote').value.trim();
            const payload = {
                title,
                description: document.getElementById('goalPilotJourneyDesc').value.trim(),
                success_criteria: document.getElementById('goalPilotJourneySuccess').value.trim(),
                priority: document.getElementById('goalPilotJourneyPriority').value,
                milestone: document.getElementById('goalPilotJourneyMilestone').value.trim() || null,
                under_the_covers: underTheCovers,
                lookahead: { status: lookaheadStatus, note: lookaheadNote },
                order_index: goalPilotState.journeys.length
            };
            const response = await fetch(`/control/goal_pilot/runs/${encodeURIComponent(runId)}/journeys`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await response.json();
            if (data.error) throw new Error(data.error);
            setOutput('goalPilotJourneyOutput', data.journey || data);
            await goalPilotStatus();
        } catch (error) {
            showToast(`Goal Pilot journey failed: ${error.message}`, 'error');
        }
    }

    async function goalPilotRefineGoals() {
        try {
            const runId = goalPilotState.runId;
            if (!runId) throw new Error('Select a run first');
            const goals = parseGoalsInput(document.getElementById('goalPilotRefineGoals').value);
            if (!goals.length) throw new Error('Refined goals required');
            const note = document.getElementById('goalPilotRefineNote').value.trim();
            const response = await fetch(`/control/goal_pilot/runs/${encodeURIComponent(runId)}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ goals, note: note || null })
            });
            const data = await response.json();
            if (data.error) throw new Error(data.error);
            setOutput('goalPilotRefineOutput', data);
            await goalPilotStatus();
        } catch (error) {
            showToast(`Goal Pilot refine failed: ${error.message}`, 'error');
        }
    }

    async function goalPilotAssistSave() {
        try {
            const runId = goalPilotState.runId;
            if (!runId) throw new Error('Select a run first');
            if (!goalPilotState.run) {
                await goalPilotStatus();
            }
            const note = document.getElementById('goalPilotAssistNotes').value.trim();
            if (!note) throw new Error('Assist notes required');
            const researchAllowed = document.getElementById('goalPilotAssistResearch').checked;
            const researchLine = `Research permission: ${researchAllowed ? 'allowed' : 'not allowed'}`;
            const combinedNote = `${note}\n${researchLine}`;
            const response = await fetch(`/control/goal_pilot/runs/${encodeURIComponent(runId)}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ goals: goalPilotState.run?.goals || [], note: combinedNote })
            });
            const data = await response.json();
            if (data.error) throw new Error(data.error);
            setOutput('goalPilotAssistOutput', data);
            await goalPilotStatus();
        } catch (error) {
            showToast(`Goal Pilot assist failed: ${error.message}`, 'error');
        }
    }

    function setGoalPilotStep(step) {
        document.querySelectorAll('.goal-pilot-step-panel[data-step]').forEach(panel => {
            panel.dataset.active = panel.dataset.step === step ? 'true' : 'false';
        });
    }

    document.querySelectorAll('[data-step-toggle]').forEach(toggle => {
        toggle.addEventListener('click', () => {
            const step = toggle.dataset.stepToggle;
            if (step) setGoalPilotStep(step);
        });
    });

    document.querySelectorAll('[data-step-next]').forEach(btn => {
        btn.addEventListener('click', () => {
            const step = btn.dataset.stepNext;
            if (step) setGoalPilotStep(step);
        });
    });

    function goalPilotRefineSync() {
        if (!goalPilotState.run) {
            showToast('Select a run first', 'warning');
            return;
        }
        syncGoalPilotRefineForm();
    }

    function renderGoalPilotSuggestedChanges(actions) {
        const container = document.getElementById('goalPilotSuggestedChanges');
        if (!container) return;
        if (!actions.length) {
            container.innerHTML = '<div class="goal-pilot-hint">No suggested changes yet.</div>';
            return;
        }
        container.innerHTML = actions.slice(0, 6).map(action => `
            <div class="goal-pilot-run-meta">${escapeHtml(action.created_at || '')} · ${escapeHtml(action.action_type || 'action')} · ${escapeHtml(action.status || '')}</div>
        `).join('');
    }

    async function goalPilotSkills() {
        try {
            const response = await fetch('/control/goal_pilot/skills');
            const data = await response.json();
            if (data.error) throw new Error(data.error);
            setOutput('goalPilotSkillsOutput', data.skills || data);
        } catch (error) {
            showToast(`Goal Pilot skills failed: ${error.message}`, 'error');
        }
    }

    async function goalPilotUpsertSkill() {
        try {
            const payload = parseJsonInput(document.getElementById('goalPilotSkillJson').value, null);
            if (!payload) throw new Error('Skill JSON required');
            const response = await fetch('/control/goal_pilot/skills', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await response.json();
            if (data.error) throw new Error(data.error);
            setOutput('goalPilotSkillsOutput', data.skill || data);
        } catch (error) {
            showToast(`Goal Pilot upsert failed: ${error.message}`, 'error');
        }
    }

    async function goalPilotExportSkills() {
        try {
            const response = await fetch('/control/goal_pilot/skills/export', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ status: 'active' })
            });
            const data = await response.json();
            if (data.error) throw new Error(data.error);
            setOutput('goalPilotSkillsOutput', data);
        } catch (error) {
            showToast(`Goal Pilot export failed: ${error.message}`, 'error');
        }
    }

    // Tools - Issue Input Modal state
    let issueInputCallback = null;
    let issueInputRequired = false;
    let issueInputRepoPath = null;

    function resetIssuePicker() {
        const select = document.getElementById('issueNumberSelect');
        select.innerHTML = '<option value="">Load known issues...</option>';
        select.value = '';
        document.getElementById('loadKnownIssuesBtn').disabled = false;
    }

    async function readJsonBody(response) {
        try {
            return await response.json();
        } catch (_) {
            return {};
        }
    }

    function formatIssueAuditError(response, data) {
        if (data?.error === 'Config not found for repo') {
            return 'No config found for this repo. Run Setup first.';
        }
        const message = data?.error || 'Failed to load issues';
        const statusParts = [];
        if (data?.error_code) statusParts.push(data.error_code);
        if (data?.upstream_status_code) statusParts.push(`GitHub HTTP ${data.upstream_status_code}`);
        if (!response.ok) statusParts.push(`response HTTP ${response.status}`);
        const statusText = statusParts.length > 0 ? ` (${statusParts.join(', ')})` : '';
        const detail = data?.detail && data.detail !== message ? `: ${data.detail}` : '';
        return `${message}${statusText}${detail}`;
    }

    function setIssueSelectOptions(entries) {
        const select = document.getElementById('issueNumberSelect');
        select.innerHTML = '<option value="">Select an issue...</option>';
        entries.forEach(entry => {
            const label = `#${entry.issue_number} - ${entry.title} (${entry.status || 'unknown'})`;
            const option = document.createElement('option');
            option.value = String(entry.issue_number);
            option.textContent = label;
            select.appendChild(option);
        });
    }

    async function loadKnownIssues() {
        if (!issueInputRepoPath) {
            showToast('Select a repository first', 'warning');
            return;
        }
        const button = document.getElementById('loadKnownIssuesBtn');
        button.disabled = true;
        button.textContent = 'Loading...';
        try {
            const url = `/control/tools/audit?repo_root=${encodeURIComponent(issueInputRepoPath)}`;
            const response = await fetch(url);
            const data = await readJsonBody(response);
            if (!response.ok || data.error) {
                throw new Error(formatIssueAuditError(response, data));
            }
            const entries = data.entries || [];
            if (entries.length === 0) {
                showToast('No issues found in audit', 'info');
                resetIssuePicker();
                return;
            }
            setIssueSelectOptions(entries);
        } catch (error) {
            showToast(`Failed to load issues: ${error.message}`, 'error');
        } finally {
            button.disabled = false;
            button.textContent = 'Load';
        }
    }

    function showIssueInputModal(title, description, required, repoPath, callback) {
        document.getElementById('issueInputModalTitle').textContent = title;
        document.getElementById('issueInputDesc').textContent = description;
        document.getElementById('issueNumberInput').value = '';
        issueInputRequired = required;
        issueInputCallback = callback;
        issueInputRepoPath = repoPath;
        resetIssuePicker();
        document.getElementById('issueInputModal').classList.add('active');
        document.getElementById('issueNumberInput').focus();
    }

    document.getElementById('closeIssueInputModal').addEventListener('click', () => {
        document.getElementById('issueInputModal').classList.remove('active');
    });
    document.getElementById('cancelIssueInput').addEventListener('click', () => {
        document.getElementById('issueInputModal').classList.remove('active');
    });
    document.getElementById('loadKnownIssuesBtn').addEventListener('click', loadKnownIssues);
    document.getElementById('issueNumberSelect').addEventListener('change', (e) => {
        const value = e.target.value;
        if (value) {
            document.getElementById('issueNumberInput').value = value;
        }
    });
    document.getElementById('submitIssueInput').addEventListener('click', () => {
        const value = document.getElementById('issueNumberInput').value;
        const issueNum = value ? parseInt(value, 10) : null;
        if (issueInputRequired && !issueNum) {
            showToast('Issue number is required', 'error');
            return;
        }
        document.getElementById('issueInputModal').classList.remove('active');
        if (issueInputCallback) issueInputCallback(issueNum);
    });

    // Goal Pilot buttons
    document.getElementById('goalPilotCreateBtn').addEventListener('click', goalPilotCreate);
    document.getElementById('goalPilotRefreshRunsBtn').addEventListener('click', goalPilotLoadRuns);
    document.getElementById('goalPilotPhaseBtn').addEventListener('click', goalPilotPhaseChange);
    document.getElementById('goalPilotJourneyCreateBtn').addEventListener('click', goalPilotCreateJourney);
    document.getElementById('goalPilotSkillsBtn').addEventListener('click', goalPilotSkills);
    document.getElementById('goalPilotSkillUpsertBtn').addEventListener('click', goalPilotUpsertSkill);
    document.getElementById('goalPilotSkillsExportBtn').addEventListener('click', goalPilotExportSkills);
    document.getElementById('goalPilotRefineBtn').addEventListener('click', goalPilotRefineGoals);
    document.getElementById('goalPilotRefineSyncBtn').addEventListener('click', goalPilotRefineSync);
    document.getElementById('goalPilotRefreshStatusBtn').addEventListener('click', goalPilotStatus);
    document.getElementById('goalPilotAssistSaveBtn').addEventListener('click', goalPilotAssistSave);

    // Audit Tool
    document.getElementById('auditTool').addEventListener('click', () => {
        const repoPath = getToolRepoPath({ requireConfig: true });
        if (!repoPath) {
            showToast('Select a repository with a config (or run Setup) to audit issues', 'warning');
            return;
        }
        showIssueInputModal(
            'Audit Issues',
            'Optional: enter an issue number to audit just one. Leave empty to audit all issues (uses GitHub labels + branches).',
            false,
            repoPath,
            async (issueNum) => {
                document.getElementById('auditResultsContent').innerHTML = '<div class="loading-spinner"></div> Loading...';
                document.getElementById('auditResultsModal').classList.add('active');

                try {
                    let url = `/control/tools/audit?repo_root=${encodeURIComponent(repoPath)}`;
                    if (issueNum) url += `&issue_number=${issueNum}`;
                    const response = await fetch(url);
                    const data = await readJsonBody(response);

                    if (!response.ok || data.error) {
                        document.getElementById('auditResultsContent').innerHTML =
                            `<div class="error-message">${escapeHtml(formatIssueAuditError(response, data))}</div>`;
                        return;
                    }

                    const entries = data.entries || [];
                    if (entries.length === 0) {
                        document.getElementById('auditResultsContent').innerHTML =
                            '<p>No issues found matching the criteria.</p>';
                        return;
                    }

                    // Group by status
                    const queued = entries.filter(e => e.status === 'queued');
                    const blocked = entries.filter(e => e.status !== 'queued');

                    let html = '';
                    if (queued.length > 0) {
                        html += '<h3 style="margin-top: 0; color: var(--success-color);">Queued (' + queued.length + ')</h3>';
                        html += '<ul style="margin: 0 0 16px 0; padding-left: 20px;">';
                        queued.forEach(e => {
                            html += `<li><strong>#${e.issue_number}</strong> ${escapeHtml(e.title)} <span style="color: var(--text-muted);">(${e.agent || 'no agent'})</span></li>`;
                        });
                        html += '</ul>';
                    }
                    if (blocked.length > 0) {
                        html += '<h3 style="margin-top: 0; color: var(--warning-color);">Blocked (' + blocked.length + ')</h3>';
                        html += '<ul style="margin: 0; padding-left: 20px;">';
                        blocked.forEach(e => {
                            const reason = e.reason ? ` - ${escapeHtml(e.reason)}` : '';
                            html += `<li><strong>#${e.issue_number}</strong> ${escapeHtml(e.title)}: <em>${escapeHtml(e.status)}</em>${reason}</li>`;
                        });
                        html += '</ul>';
                    }
                    document.getElementById('auditResultsContent').innerHTML = html;
                } catch (error) {
                    document.getElementById('auditResultsContent').innerHTML =
                        `<div class="error-message">Failed to load audit: ${escapeHtml(error.message)}</div>`;
                }
            }
        );
    });
    document.getElementById('closeAuditResultsModal').addEventListener('click', () => {
        document.getElementById('auditResultsModal').classList.remove('active');
    });
    document.getElementById('closeAuditResultsBtn').addEventListener('click', () => {
        document.getElementById('auditResultsModal').classList.remove('active');
    });

    // Trace Tool
    document.getElementById('traceTool').addEventListener('click', () => {
        const repoPath = getToolRepoPath({ requireConfig: false });
        if (!repoPath) {
            showToast('Select a repository first', 'warning');
            return;
        }
        showIssueInputModal(
            'Issue Trace Logs',
            'Required: enter the issue number to view recent orchestrator log entries for that issue.',
            true,
            repoPath,
            async (issueNum) => {
                document.getElementById('traceResultsContent').textContent = 'Loading...';
                document.getElementById('traceResultsModalTitle').textContent = `Issue Trace Logs - Issue #${issueNum}`;
                document.getElementById('traceResultsModal').classList.add('active');

                try {
                    const url = `/control/tools/trace?repo_root=${encodeURIComponent(repoPath)}&issue_number=${issueNum}`;
                    const response = await fetch(url);
                    const data = await response.json();

                    if (data.error) {
                        document.getElementById('traceResultsContent').textContent = 'Error: ' + data.error;
                        return;
                    }

                    if (data.message) {
                        document.getElementById('traceResultsContent').textContent = data.message;
                        return;
                    }

                    const entries = data.entries || [];
                    if (entries.length === 0) {
                        document.getElementById('traceResultsContent').textContent = 'No log entries found for this issue.';
                    } else {
                        document.getElementById('traceResultsContent').textContent = entries.join('\n');
                    }
                } catch (error) {
                    document.getElementById('traceResultsContent').textContent = 'Failed to load trace: ' + error.message;
                }
            }
        );
    });
    document.getElementById('closeTraceResultsModal').addEventListener('click', () => {
        document.getElementById('traceResultsModal').classList.remove('active');
    });
    document.getElementById('closeTraceResultsBtn').addEventListener('click', () => {
        document.getElementById('traceResultsModal').classList.remove('active');
    });

    // Refresh Labels Tool
    document.getElementById('refreshLabelsTool').addEventListener('click', async () => {
        const repoPath = getToolRepoPath({ requireConfig: true });
        if (!repoPath) {
            showToast('Select a repository with a config (or run Setup) to refresh labels', 'warning');
            return;
        }

        document.getElementById('labelsResultsContent').innerHTML = '<div class="loading-spinner"></div> Creating/updating labels on GitHub...';
        document.getElementById('labelsResultsModal').classList.add('active');

        try {
            const response = await fetch('/control/tools/labels/init', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ repo_root: repoPath })
            });
            const data = await response.json();

            if (data.error) {
                if (data.error === 'Config not found for repo') {
                    document.getElementById('labelsResultsContent').innerHTML =
                        '<div class="error-message">No config found for this repo. Run Setup first.</div>';
                    return;
                }
                document.getElementById('labelsResultsContent').innerHTML =
                    `<div class="error-message">${escapeHtml(data.error)}</div>`;
                return;
            }

            const created = data.created || [];
            const updated = data.updated || [];
            const failed = data.failed || [];

            let html = '';
            if (failed.length === 0) {
                html += '<p style="color: var(--success-color); margin-top: 0;">Labels synchronized successfully!</p>';
            } else {
                html += '<p style="color: var(--warning-color); margin-top: 0;">Labels synchronized with some failures.</p>';
            }
            if (created.length > 0) {
                html += '<p><strong>Created:</strong> ' + created.map(escapeHtml).join(', ') + '</p>';
            }
            if (updated.length > 0) {
                html += '<p><strong>Updated:</strong> ' + updated.map(escapeHtml).join(', ') + '</p>';
            }
            if (failed.length > 0) {
                html += '<p style="color: var(--error-color);"><strong>Failed:</strong> ' + failed.map(escapeHtml).join(', ') + '</p>';
            }
            if (created.length === 0 && updated.length === 0 && failed.length === 0) {
                html += '<p>All labels were already up to date.</p>';
            }
            document.getElementById('labelsResultsContent').innerHTML = html;
        } catch (error) {
            document.getElementById('labelsResultsContent').innerHTML =
                `<div class="error-message">Failed to refresh labels: ${escapeHtml(error.message)}</div>`;
        }
    });
    document.getElementById('closeLabelsResultsModal').addEventListener('click', () => {
        document.getElementById('labelsResultsModal').classList.remove('active');
    });
    document.getElementById('closeLabelsResultsBtn').addEventListener('click', () => {
        document.getElementById('labelsResultsModal').classList.remove('active');
    });

    // Action contract: POST /control/tools/worktrees/cleanup is a read-only audit.
    // It returns worktrees[] with kind/disposition/reason and never deletes paths.
    document.getElementById('cleanupWorktreesTool').addEventListener('click', async () => {
        const repoPath = getToolRepoPath({ requireConfig: false });
        if (!repoPath) {
            showToast('Select a repository to scan worktrees', 'warning');
            return;
        }

        document.getElementById('worktreesContent').innerHTML = '<div class="loading-spinner"></div> Auditing registered worktrees...';
        document.getElementById('worktreesModal').classList.add('active');

        document.getElementById('worktreesContent').innerHTML =
            await requestWorktreeAudit(repoPath);
    });
    document.getElementById('closeWorktreesModal').addEventListener('click', () => {
        document.getElementById('worktreesModal').classList.remove('active');
    });
    document.getElementById('cancelWorktrees').addEventListener('click', () => {
        document.getElementById('worktreesModal').classList.remove('active');
    });

    // Setup Wizard
    setupWizardController = createControlCenterSetupWizard({
        document,
        fetch: window.fetch.bind(window),
        escapeHtml,
        loadRepos,
        setupCommands: controlCenterSetupCommands,
    });
    setupWizardController.bind();
    // Sidebar app menu toggle
    document.getElementById('sidebarAppMenuBtn').addEventListener('click', (e) => {
        e.stopPropagation();
        const menu = document.getElementById('sidebarAppMenu');
        const btn = document.getElementById('sidebarAppMenuBtn');
        const wasVisible = menu.classList.contains('visible');
        closeConsolidatedDropdowns();
        closeSidebarAppMenu();
        if (!wasVisible) {
            menu.classList.add('visible');
            btn.classList.add('active');
            btn.setAttribute('aria-expanded', 'true');
        }
    });

    // Scope info popover toggle
    document.getElementById('scopeInfoBtn').addEventListener('click', (e) => {
        e.stopPropagation();
        const popover = document.getElementById('scopePopover');
        const btn = document.getElementById('scopeInfoBtn');
        const wasVisible = popover.classList.contains('visible');
        closeConsolidatedDropdowns();
        if (!wasVisible) {
            popover.classList.add('visible');
            btn.classList.add('active');
            const s = state.dashboardStatus;
            if (s) {
                // Build scope string: milestones + optional label filter
                let scopeStr = s.scope.filterMilestones?.length
                    ? 'milestones=' + s.scope.filterMilestones.join(',')
                    : 'milestones=all';
                if (s.scope.filterLabel) scopeStr += ', label=' + s.scope.filterLabel;
                document.getElementById('scopePopoverScope').textContent = scopeStr;

                // Exclude labels (only show row if present)
                const excludeRow = document.getElementById('scopePopoverExcludeRow');
                if (s.scope.excludeLabels?.length) {
                    document.getElementById('scopePopoverExclude').textContent = s.scope.excludeLabels.join(', ');
                    excludeRow.style.display = '';
                } else {
                    excludeRow.style.display = 'none';
                }

                document.getElementById('scopePopoverRepo').textContent = s.scope.repo || '--';
                document.getElementById('scopePopoverTotal').textContent = s.scope.inScopeTotal;
                document.getElementById('scopePopoverQueued').textContent = s.counts.queued;
                document.getElementById('scopePopoverRunning').textContent = s.counts.running;
                document.getElementById('scopePopoverMerge').textContent = s.counts.awaitingMerge;
                document.getElementById('scopePopoverBlocked').textContent = s.counts.blocked;
                document.getElementById('scopePopoverSync').textContent =
                    s.refresh.lastRefreshAt ? formatSyncAge(s.refresh.lastRefreshAt) : (s.refresh.lastRefreshLabel || '--');
            }
        }
    });

    // Action menu toggle
    document.getElementById('actionMenuBtn').addEventListener('click', (e) => {
        e.stopPropagation();
        const menu = document.getElementById('actionMenu');
        const btn = document.getElementById('actionMenuBtn');
        const wasVisible = menu.classList.contains('visible');
        closeConsolidatedDropdowns();
        if (!wasVisible) {
            menu.classList.add('visible');
            btn.classList.add('active');
            btn.setAttribute('aria-expanded', 'true');
        }
    });

    // Close consolidated dropdowns on outside click
    document.addEventListener('click', () => {
        closeConsolidatedDropdowns();
        closeSidebarAppMenu();
    });

    // Maximize toggle
    document.getElementById('maximizeBtn').addEventListener('click', toggleMaximize);
    document.getElementById('maximizeExitBtn').addEventListener('click', toggleMaximize);

    // Config selector in action menu
    document.getElementById('menuConfigSelect').addEventListener('change', handleMenuConfigChange);
    document.getElementById('menuModeSelect').addEventListener('change', handleMenuModeChange);

    // Close modals on backdrop click
    document.querySelectorAll('.modal-backdrop').forEach(backdrop => {
        backdrop.addEventListener('click', (e) => {
            if (e.target === backdrop) {
                if (backdrop.id === 'shutdownModal') {
                    hideShutdownModal();
                } else {
                    backdrop.classList.remove('active');
                }
            }
        });
    });

    // Keyboard navigation for modals
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            document.querySelectorAll('.modal-backdrop.active').forEach(modal => {
                if (modal.id === 'shutdownModal') {
                    hideShutdownModal();
                } else {
                    modal.classList.remove('active');
                }
            });
        }
    });

    // Refresh repos periodically (silent — no error toasts on background polls)
    setInterval(() => {
        loadRepos(true);
        loadSystemState();
        refreshShutdownHud();
    }, 30000);
    setInterval(refreshShutdownHud, 3000);
});
