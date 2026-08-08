const contextMenu = document.getElementById('contextMenu');
const menuFocus = document.getElementById('menuFocus');
const menuRevealWorktree = document.getElementById('menuRevealWorktree');
const menuLog = document.getElementById('menuLog');
const menuAgentLog = document.getElementById('menuAgentLog');
const menuPrompt = document.getElementById('menuPrompt');
const menuKill = document.getElementById('menuKill');
const menuPR = document.getElementById('menuPR');
const menuIssue = document.getElementById('menuIssue');
const menuUnblock = document.getElementById('menuUnblock');
const menuResetRetry = document.getElementById('menuResetRetry');
const menuResetRetryScratch = document.getElementById('menuResetRetryScratch');
const menuRetry = document.getElementById('menuRetry');
const menuCloseIssue = document.getElementById('menuCloseIssue');
const menuInvestigateTechLead = document.getElementById('menuInvestigateTechLead');
// The compact menu's action carries REAL sibling elements — a status span and
// the row that hides them together — so its blocked reason and Settings remedy
// have somewhere valid to render (#6994 round 2 F6).
const menuInvestigateTechLeadStatus = document.getElementById('menuInvestigateTechLeadStatus');
const menuInvestigateTechLeadRow = document.getElementById('menuInvestigateTechLeadRow');
const menuHistoryDivider = document.getElementById('menuHistoryDivider');
const menuDepsDivider = document.getElementById('menuDepsDivider');
const menuDepsLabel = document.getElementById('menuDepsLabel');
const menuDepsContainer = document.getElementById('menuDepsContainer');
let currentRow = null;
let lastContextMenuPoint = null;
const contextMenuEnabled = Boolean(contextMenu);

// Add keyboard support to all context menu items
if (contextMenuEnabled) {
    [menuFocus, menuRevealWorktree, menuLog, menuAgentLog, menuPrompt, menuKill, menuPR, menuIssue, menuUnblock, menuResetRetry, menuResetRetryScratch, menuRetry, menuCloseIssue, menuInvestigateTechLead]
        .filter(Boolean)
        .forEach(addKeyboardSupport);
}

function parseOrchestratorLabels(rawLabels) {
    try {
        const parsed = JSON.parse(rawLabels || '[]');
        return Array.isArray(parsed) ? parsed : [];
    } catch {
        return [];
    }
}

function showContextMenu(e, row) {
    if (!contextMenuEnabled) return;
    e.preventDefault();
    currentRow = row;

    const issueNumber = row.dataset.issue;
    const action = row.dataset.action;
    const prUrl = row.dataset.prUrl;
    const status = row.dataset.status;
    const columnId = String(row.dataset.columnId || '').toLowerCase();
    const hasDeps = row.dataset.hasDependencies === 'true';
    const isCompactCardMenu = row.dataset.compactMenu === 'true';
    const isPrClosedBlock = hasPrClosedBlock({
        orchestrator_labels: parseOrchestratorLabels(row.dataset.orchestratorLabels),
    });
    const statusLower = String(status || '').toLowerCase();
    const normalizedStatus = statusLower.replace(/_/g, '-');
    const effectiveHistoryStatus = (isCompactCardMenu && columnId) ? columnId : normalizedStatus;
    const isBlockedHistory = effectiveHistoryStatus === 'blocked' || effectiveHistoryStatus === 'needs-human';
    lastContextMenuPoint = {
        pageX: e.pageX,
        pageY: e.pageY,
        clientX: e.clientX,
        clientY: e.clientY,
    };

    const setMenuVisible = (el, visible) => {
        if (!el) return;
        el.style.display = visible ? '' : 'none';
        el.classList.toggle('disabled', !visible);
    };

    // Show only applicable actions; hide the rest.
    const canFocusSession = action === 'focus' && clientCapabilities.focus_session;
    const canRevealWorktree = action === 'focus' && clientCapabilities.reveal_worktree;
    setMenuVisible(menuFocus, canFocusSession);
    setMenuVisible(menuRevealWorktree, canRevealWorktree);

    setMenuVisible(menuPR, Boolean(prUrl || row.dataset.issueUrl));
    if (menuPR) {
        menuPR.textContent = prUrl ? 'Open PR ↗' : 'Open Issue ↗';
    }
    setMenuVisible(menuIssue, Boolean(prUrl && row.dataset.issueUrl));

    const agentType = row.dataset.agent;
    setMenuVisible(menuPrompt, Boolean(agentType));

    const hasTerminal = row.dataset.hasTerminal === 'true';
    setMenuVisible(menuKill, hasTerminal);
    // Compact-card menus avoid ambiguous log/session entries.
    setMenuVisible(menuLog, !isCompactCardMenu && !isBlockedHistory);
    setMenuVisible(menuAgentLog, !isCompactCardMenu && !isBlockedHistory);

    // Show dependencies section if this issue has dependencies
    if (menuDepsContainer) menuDepsContainer.innerHTML = '';  // Clear previous
    if (hasDeps && menuDepsContainer && menuDepsDivider && menuDepsLabel) {
        try {
            const deps = JSON.parse(row.dataset.dependencies || '[]');
            if (deps.length > 0) {
                menuDepsDivider.style.display = 'block';
                menuDepsLabel.style.display = 'flex';
                deps.forEach(dep => {
                    const item = document.createElement('div');
                    item.className = 'context-menu-item dep-item';
                    item.innerHTML = `<span class="dep-num">#${dep.number}</span><span class="dep-title" title="${dep.title}">${dep.title}</span>`;
                    item.onclick = (e) => {
                        e.stopPropagation();
                        contextMenu.classList.remove('visible');
                        window.open(`https://github.com/${window.dashboardData.repo}/issues/${dep.number}`, '_blank');
                    };
                    menuDepsContainer.appendChild(item);
                });
            } else {
                menuDepsDivider.style.display = 'none';
                menuDepsLabel.style.display = 'none';
            }
        } catch (err) {
            console.error('Failed to parse dependencies:', err);
            menuDepsDivider.style.display = 'none';
            menuDepsLabel.style.display = 'none';
        }
    } else if (menuDepsDivider && menuDepsLabel) {
        menuDepsDivider.style.display = 'none';
        menuDepsLabel.style.display = 'none';
    }

    const hasPrimaryActionsAboveHistory = [
        menuFocus,
        menuRevealWorktree,
        menuLog,
        menuAgentLog,
        menuPrompt,
        menuPR,
        menuIssue,
        menuKill,
    ].some((el) => el && el.style.display !== 'none');

    // History actions by status:
    // blocked/awaiting-merge => Retry + Reset & Retry + Reset & Retry From Scratch
    // blocked also gets Unblock; PR-closed blocks keep Close Issue; others => Retry only
    const resetRetryStatuses = new Set(['blocked', 'awaiting-merge']);
    const otherRetryStatuses = new Set(['failed', 'completed', 'timed-out']);
    if (menuHistoryDivider && menuRetry && menuUnblock && menuResetRetry && menuResetRetryScratch) {
        if (resetRetryStatuses.has(effectiveHistoryStatus) || isBlockedHistory) {
            menuHistoryDivider.style.display = hasPrimaryActionsAboveHistory ? 'block' : 'none';
            menuUnblock.style.display = isBlockedHistory && !isPrClosedBlock ? '' : 'none';
            menuResetRetry.style.display = '';
            menuResetRetryScratch.style.display = '';
            menuRetry.style.display = '';
            if (menuCloseIssue) menuCloseIssue.style.display = isPrClosedBlock ? '' : 'none';
        } else if (otherRetryStatuses.has(effectiveHistoryStatus)) {
            menuHistoryDivider.style.display = hasPrimaryActionsAboveHistory ? 'block' : 'none';
            menuUnblock.style.display = 'none';
            menuResetRetry.style.display = 'none';
            menuResetRetryScratch.style.display = 'none';
            menuRetry.style.display = '';
            if (menuCloseIssue) menuCloseIssue.style.display = 'none';
        } else {
            menuHistoryDivider.style.display = 'none';
            menuUnblock.style.display = 'none';
            menuResetRetry.style.display = 'none';
            menuResetRetryScratch.style.display = 'none';
            menuRetry.style.display = 'none';
            if (menuCloseIssue) menuCloseIssue.style.display = 'none';
        }
    }

    // Targeted tech-lead action (#6994): eligible on blocked items only, and
    // labelled with its live queued/running state. The owner in
    // ``tech_lead_runs.js`` decides visibility/disabled state so the compact
    // card menu and the issue detail drawer cannot drift apart.
    if (menuInvestigateTechLead) {
        menuInvestigateTechLead.dataset.issue = String(issueNumber || '');
        // The label goes on FIRST, so the run owner's status/aria work is the
        // last write and cannot be clobbered by a later label update (F6).
        menuInvestigateTechLead.textContent = techLeadIssueActionLabel(Number(issueNumber));
        updateTechLeadIssueAction(
            {
                button: menuInvestigateTechLead,
                statusEl: menuInvestigateTechLeadStatus,
                container: menuInvestigateTechLeadRow,
            },
            Number(issueNumber),
            isBlockedHistory,
        );
    }

    // Position menu (clamped to viewport so right-edge triggers still show)
    contextMenu.classList.add('visible');
    const menuRect = contextMenu.getBoundingClientRect();
    const clamped = clampPagePoint(e.pageX, e.pageY, menuRect.width, menuRect.height, 8);
    contextMenu.style.left = `${clamped.left}px`;
    contextMenu.style.top = `${clamped.top}px`;
}

function openRowActionsMenu(event, button) {
    event.preventDefault();
    event.stopPropagation();
    if (typeof event.stopImmediatePropagation === 'function') {
        event.stopImmediatePropagation();
    }
    const row = button?.closest('.issue-row');
    if (!row) return;
    const rect = button.getBoundingClientRect();
    const syntheticEvent = {
        preventDefault: () => {},
        pageX: rect.right + window.scrollX,
        pageY: rect.bottom + window.scrollY,
    };
    showContextMenu(syntheticEvent, row);
}

function openCompactCardActionsMenu(event, button) {
    const issueNumber = Number(button?.dataset?.issue || 0);
    const title = String(button?.dataset?.title || '');
    const issueUrl = String(button?.dataset?.issueUrl || '');
    const prUrl = String(button?.dataset?.prUrl || '');
    const status = String(button?.dataset?.status || '');
    const action = String(button?.dataset?.rowAction || '');
    const agentType = String(button?.dataset?.agent || '');
    const runDir = String(button?.dataset?.runDir || '');
    const hasTerminal = button?.dataset?.hasTerminal === 'true';
    const orchestratorLabels = String(button?.dataset?.orchestratorLabels || '[]');
    const columnId = String(button?.closest('.kanban-column')?.dataset?.column || '').toLowerCase();
    if (!contextMenuEnabled) {
        showToast('Actions menu unavailable: context menu not initialized', 'error');
        return;
    }
    event.preventDefault();
    event.stopPropagation();
    if (typeof event.stopImmediatePropagation === 'function') {
        event.stopImmediatePropagation();
    }
    if (!button) {
        showToast('Actions menu unavailable: missing trigger button', 'error');
        return;
    }
    const rect = button.getBoundingClientRect();
    const syntheticEvent = {
        preventDefault: () => {},
        pageX: rect.right + window.scrollX,
        pageY: rect.bottom + window.scrollY,
    };
    const fauxRow = {
        dataset: {
            issue: String(issueNumber),
            title: String(title || ''),
            issueUrl: String(issueUrl || ''),
            prUrl: String(prUrl || ''),
            status: String(status || ''),
            columnId: String(columnId || ''),
            action: String(action || ''),
            agent: String(agentType || ''),
            runDir: String(runDir || ''),
            hasTerminal: hasTerminal ? 'true' : 'false',
            orchestratorLabels,
            hasDependencies: 'false',
            dependencies: '[]',
            compactMenu: 'true',
        }
    };
    showContextMenu(syntheticEvent, fauxRow);
}

// Hide context menu on click elsewhere
if (contextMenuEnabled) {
    document.addEventListener('click', (event) => {
        const target = event.target;
        if (target instanceof Element) {
            if (target.closest('.context-menu')) return;
            if (target.closest('.issue-row-menu-btn')) return;
            if (target.closest('.card-menu-btn')) return;
        }
        contextMenu?.classList.remove('visible');
    });

    // Menu actions - must stopPropagation to prevent document click handler from interfering
    menuFocus?.addEventListener('click', async (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow && !menuFocus.classList.contains('disabled')) {
            await fetch(`/api/focus/${currentRow.dataset.issue}`, { method: 'POST' });
        }
    });

    menuRevealWorktree?.addEventListener('click', async (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow && !menuRevealWorktree.classList.contains('disabled')) {
            const req = uiActionContract.buildRevealWorktreeRequest(currentRow.dataset.issue);
            const res = await fetch(req.endpoint, { method: req.method });
            const data = await res.json();
            handleHostActionResponse(data, 'worktree');
        }
    });

    menuLog?.addEventListener('click', async (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow) {
            const issueNumber = currentRow.dataset.issue;
            await openFilteredOrchestratorLog(Number(issueNumber));
        }
    });

    menuAgentLog?.addEventListener('click', async (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow) {
            const issueNumber = currentRow.dataset.issue;
            await openSessionManifest(issueNumber);
        }
    });

    menuPrompt?.addEventListener('click', async (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow && !menuPrompt.classList.contains('disabled')) {
            // Active sessions carry a run_dir — prefer the run-scoped launch
            // prompt (the actual prompt the agent was launched with, e.g. a
            // rework session's merge-conflict instructions) over the static
            // agent template.
            const runDir = currentRow.dataset.runDir;
            const issueNumber = currentRow.dataset.issue;
            if (runDir && issueNumber) {
                await openLaunchPromptDialog(Number(issueNumber), runDir);
                return;
            }
            const agentType = currentRow.dataset.agent;
            const res = await fetch(`/api/prompt/${encodeURIComponent(agentType)}`, { method: 'POST' });
            const data = await res.json();
            console.log('Prompt response:', data);
        }
    });

    menuKill?.addEventListener('click', async (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow) {
            const issueNumber = currentRow.dataset.issue;
            const title = currentRow.dataset.title;
            if (confirm(`Terminate session #${issueNumber}: ${title}?\n\nThis will stop the active agent and place the issue on hold.\nIt will not run again until you explicitly retry/unblock it.`)) {
                const res = await fetch(`/api/kill/${issueNumber}`, { method: 'POST' });
                const data = await res.json();
                if (data.status === 'terminated') {
                    location.reload();
                } else {
                    alert(data.error || 'Failed to terminate session');
                }
            }
        }
    });

    menuPR?.addEventListener('click', (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow) {
            const targetUrl = currentRow.dataset.prUrl || currentRow.dataset.issueUrl;
            if (targetUrl) {
                window.open(targetUrl, '_blank');
            }
        }
    });

    menuIssue?.addEventListener('click', (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow?.dataset?.issueUrl) {
            window.open(currentRow.dataset.issueUrl, '_blank');
        }
    });

    menuRetry?.addEventListener('click', async (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow) {
            const issueNumber = Number(currentRow.dataset.issue);
            const status = String(currentRow.dataset.status || '').toLowerCase();
            if (Number.isNaN(issueNumber)) return;
            if (confirm(`Retry issue #${issueNumber}? It will be picked up on the next cycle.`)) {
                const req = uiActionContract.buildIssueRetryRequest(issueNumber);
                const res = await fetch(req.endpoint, {
                    method: req.method,
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(req.body),
                });
                const data = await res.json().catch(() => ({}));
                if (res.ok && (data.retrying || data.success || (data.retried && data.retried.length > 0))) {
                    if (status) applyOptimisticRequeue([issueNumber], [status]);
                    await refreshViewModel();
                } else {
                    alert(data.error || 'Failed to retry issue');
                }
            }
        }
    });

    menuUnblock?.addEventListener('click', async (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow) {
            const issueNumber = Number(currentRow.dataset.issue);
            if (!Number.isNaN(issueNumber)) {
                await unblockSingle(issueNumber, lastContextMenuPoint);
            }
        }
    });

    menuCloseIssue?.addEventListener('click', async (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow) {
            const issueNumber = Number(currentRow.dataset.issue);
            if (!Number.isNaN(issueNumber)) {
                await closePrClosedIssue(issueNumber, lastContextMenuPoint);
            }
        }
    });

    menuResetRetry?.addEventListener('click', async (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow) {
            const issueNumber = Number(currentRow.dataset.issue);
            if (!Number.isNaN(issueNumber)) {
                await resetRetrySingle(issueNumber, lastContextMenuPoint);
            }
        }
    });

    menuResetRetryScratch?.addEventListener('click', async (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow) {
            const issueNumber = Number(currentRow.dataset.issue);
            if (!Number.isNaN(issueNumber)) {
                await resetRetrySingleFromScratch(issueNumber, lastContextMenuPoint);
            }
        }
    });

}

// Settings menu
const settingsMenu = document.getElementById('settingsMenu');

// Single owner for the dashboard actions menu's open/closed state. Every
// surface that opens or dismisses the menu goes through here, so the `visible`
// class and the trigger's `aria-expanded` can never disagree — a menu that
// looks closed to the eye while still announcing itself as expanded is broken
// for screen-reader users only, which is exactly the kind of drift a second
// writer introduces silently.
function setSettingsMenuVisible(visible) {
    settingsMenu.classList.toggle('visible', visible);
    const trigger = document.getElementById('settingsMenuBtn');
    if (trigger) trigger.setAttribute('aria-expanded', String(visible));
}

function closeSettingsMenu() {
    setSettingsMenuVisible(false);
}

function toggleSettingsMenu(e) {
    e.stopPropagation();
    setSettingsMenuVisible(!settingsMenu.classList.contains('visible'));
}

document.addEventListener('click', () => {
    closeSettingsMenu();
});

// Modal
const modalOverlay = document.getElementById('modalOverlay');
const modalTitle = document.getElementById('modalTitle');
const modalBody = document.getElementById('modalBody');

function openModal(title, content) {
    modalTitle.textContent = title;
    modalBody.innerHTML = content;
    modalOverlay.classList.add('visible');
}

function closeModal(e) {
    if (!e || e.target === modalOverlay) {
        modalOverlay.classList.remove('visible');
        modalOverlay.classList.remove('timeline-event-detail-overlay');
        // Reset modal classes for viewers
        const modalEl = modalOverlay.querySelector('.modal');
        modalEl.classList.remove('diagnostics-modal', 'log-viewer-modal', 'live-log-modal', 'review-artifact-modal');
        if (logPoller) {
            clearInterval(logPoller);
            logPoller = null;
        }
        // Stop live log poller if running
        stopLiveLogPoller();
        window.removeEventListener('resize', fitSessionReplayTerminal);
        destroySessionReplay();
    }
}

// Blocked Issues Modal Functions
