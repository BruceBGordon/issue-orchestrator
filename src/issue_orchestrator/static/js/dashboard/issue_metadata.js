let dependencyProblems = {};  // issue_number -> problem info

function updateDependencyWarning(issueNumber, problem) {
    const warningIcon = document.getElementById('dep-warning-' + issueNumber);
    if (warningIcon) {
        if (problem) {
            warningIcon.style.display = 'inline';
            warningIcon.title = problem.summary || 'Dependency problem';
            // Store for context menu
            warningIcon.dataset.problemSummary = problem.summary;
        } else {
            warningIcon.style.display = 'none';
            warningIcon.title = '';
        }
    }
}

function loadDependencyProblems() {
    fetch('/api/dependency-problems')
        .then(response => response.json())
        .then(data => {
            if (data.problems) {
                dependencyProblems = data.problems;
                console.log('[deps] Loaded', Object.keys(dependencyProblems).length, 'dependency problems');
                // Update warning icons for all problems
                for (const [issueNum, problem] of Object.entries(dependencyProblems)) {
                    updateDependencyWarning(issueNum, problem);
                }
            }
        })
        .catch(err => console.error('[deps] Failed to load dependency problems:', err));
}

// Stale in-progress tracking
let staleIssues = {};  // issue_number -> stale info

function updateStaleWarning(issueNumber, staleInfo) {
    const warningIcon = document.getElementById('stale-warning-' + issueNumber);
    if (warningIcon) {
        if (staleInfo) {
            warningIcon.style.display = 'inline';
            const ticks = staleInfo.consecutive_ticks || 1;
            const persistent = staleInfo.persistent;
            warningIcon.title = persistent
                ? `Persistent stale: no session for ${ticks} cycles (needs investigation)`
                : `Stale in-progress: no session running (${ticks} cycle${ticks > 1 ? 's' : ''})`;
            // Add/remove persistent class for red color
            if (persistent) {
                warningIcon.classList.add('persistent');
            } else {
                warningIcon.classList.remove('persistent');
            }
        } else {
            warningIcon.style.display = 'none';
            warningIcon.title = '';
            warningIcon.classList.remove('persistent');
        }
    }
}

function loadStaleIssues() {
    fetch('/api/stale-issues')
        .then(response => response.json())
        .then(data => {
            if (data.stale) {
                staleIssues = data.stale;
                console.log('[stale] Loaded', Object.keys(staleIssues).length, 'stale issues');
                // Update warning icons for all stale issues
                for (const [issueNum, staleInfo] of Object.entries(staleIssues)) {
                    updateStaleWarning(issueNum, staleInfo);
                }
            }
        })
        .catch(err => console.error('[stale] Failed to load stale issues:', err));
}

let excludedLoaded = false;

function renderFlowStepper(steps, activeKey, blockedSummary) {
    if (!steps || steps.length === 0) return '';
    const stepHtml = steps.map(step => {
        const active = step.key === activeKey ? 'active' : '';
        return `<span class="flow-step ${active}" tabindex="0">${escapeHtml(step.label)}</span>`;
    }).join('');
    const blockedBadge = blockedSummary
        ? `<span class="blocked-badge" title="${escapeHtml(blockedSummary)}">Blocked</span>`
        : '';
    const blockedClass = blockedSummary ? 'blocked' : '';
    return `<span class="flow-stepper ${blockedClass}">${stepHtml}${blockedBadge}</span>`;
}

function renderExcludedList(items) {
    const list = document.getElementById('excludedList');
    if (!items || items.length === 0) {
        list.innerHTML = '<div class="empty-state">No excluded issues found</div>';
        return;
    }
    list.innerHTML = items.map(item => `
        <div class="excluded-row">
            <div class="excluded-meta">
                <a href="${item.issue_url}" target="_blank">#${item.issue_number}</a>
                <span class="excluded-reason">${escapeHtml(item.excluded_reason || 'not eligible')}</span>
            </div>
            <div class="issue-title">${escapeHtml(item.title)}</div>
            ${renderFlowStepper(item.flow_steps, item.flow_stage, item.blocked_summary)}
        </div>
    `).join('');
}

async function toggleExcluded() {
    const panel = document.getElementById('excludedPanel');
    const toggle = document.getElementById('excludedToggle');
    const opening = panel.style.display === 'none';
    panel.style.display = opening ? 'block' : 'none';
    toggle.classList.toggle('active', opening);

    if (!opening) return;
    if (!excludedLoaded) {
        try {
            const res = await fetch('/api/excluded-issues');
            const data = await res.json();
            const items = data.excluded || [];
            renderExcludedList(items);
            toggle.textContent = `Excluded (${items.length})`;
            excludedLoaded = true;
        } catch (err) {
            console.error('Failed to fetch excluded issues:', err);
            document.getElementById('excludedList').innerHTML =
                '<div class="empty-state">Failed to load excluded issues</div>';
        }
    }
}

// Server-Sent Events for real-time updates
// Always connect - even during startup - so we can receive startup_complete
// IMPORTANT: Connect first, then fetch initial state on open to avoid race conditions
(function() {
    const startupComplete = window.dashboardData.startupComplete;
    let evtSource = null;
    let reconnectAttempts = 0;
    let reconnectTimer = null;
    let healthPollTimer = null;
    const restartBanner = document.getElementById('engineRestartBanner');
    const HEALTH_POLL_MS = 5000;

    function setRestartBanner(message) {
        if (!restartBanner) return;
        restartBanner.textContent = message;
        restartBanner.style.display = '';
    }

    function clearRestartBanner() {
        if (!restartBanner) return;
        restartBanner.style.display = 'none';
        restartBanner.textContent = '';
    }

    async function checkEngineHealth() {
        try {
            const response = await fetch('/api/info', { cache: 'no-store' });
            if (response.ok) {
                if (evtSource === null) {
                    setRestartBanner('Engine reachable. Reconnecting event stream...');
                } else {
                    clearRestartBanner();
                }
                return true;
            }
        } catch (_) {
            // handled below
        }
        setRestartBanner('Engine restarting... waiting for service to recover.');
        return false;
    }

    function closeEventStream() {
        if (evtSource) {
            evtSource.close();
            evtSource = null;
        }
    }

    function scheduleReconnect() {
        if (reconnectTimer !== null) return;
        const capped = Math.min(reconnectAttempts, 6);
        const backoffMs = Math.min(30000, 1000 * (2 ** capped));
        const jitterMs = Math.floor(Math.random() * 300);
        const waitMs = backoffMs + jitterMs;
        const seconds = Math.max(1, Math.round(waitMs / 1000));
        reconnectAttempts += 1;
        setRestartBanner(`Event stream disconnected... reconnecting in ${seconds}s.`);
        reconnectTimer = window.setTimeout(() => {
            reconnectTimer = null;
            connectEventStream();
        }, waitMs);
    }

    function wireEventListeners(source) {
        source.onopen = function() {
            console.log('[SSE] Connected to event stream (startup_complete=' + startupComplete + ')');
            reconnectAttempts = 0;
            clearRestartBanner();
            loadDependencyProblems();
            loadStaleIssues();
            refreshViewModel({ reloadOnListChange: false });
        };

        const refreshEvents = [
            'session.started',
            'session.completed',
            'history.reconciled',
            'issue.unblocked',
            'orchestrator.paused',
            'orchestrator.resumed',
            'startup_complete',
            // Provider circuit-breaker outages (issue #5980): refresh the view
            // model so the outage banner + health panel appear/clear live.
            'provider.outage_entered',
            'provider.outage_exited',
        ];
        refreshEvents.forEach(eventType => {
            source.addEventListener(eventType, function(e) {
                console.log('[SSE] Received event:', eventType, e.data);
                if (eventType === 'startup_complete') {
                    document.querySelectorAll('.skeleton-card').forEach(el => el.remove());
                }
                setTimeout(() => refreshViewModel({ reloadOnListChange: true }), 200);
            });
        });

        source.addEventListener('tick.completed', function() {
            refreshViewModel({ reloadOnListChange: false });
        });

        source.addEventListener('shutdown_requested', function(e) {
            console.log('[SSE] Shutdown requested:', e.data);
            const badge = document.querySelector('.status-badge');
            if (badge) {
                badge.textContent = 'Stopping...';
                badge.classList.remove('status-running', 'status-starting');
                badge.classList.add('status-paused');
            }
            setTimeout(() => {
                document.body.innerHTML = '<div style="display:flex;justify-content:center;align-items:center;height:100vh;flex-direction:column;gap:16px;color:var(--text-muted);"><div style="font-size:48px;">👋</div><h2 style="color:var(--text);">Orchestrator Stopped</h2><p>You can close this tab or wait for it to restart.</p></div>';
            }, 500);
        });

        source.addEventListener('queue.changed', function(e) {
            try {
                const data = JSON.parse(e.data);
                console.log('[SSE] Queue changed:', data.added.length, 'added,', data.removed.length, 'removed');
                setTimeout(() => refreshViewModel({ reloadOnListChange: true }), 200);
            } catch (err) {
                console.error('[SSE] Failed to parse queue.changed:', err);
            }
        });

        source.addEventListener('dependency.blocked', function(e) {
            try {
                const data = JSON.parse(e.data);
                console.log('[SSE] Dependency blocked:', data);
                dependencyProblems[data.issue_number] = data;
                updateDependencyWarning(data.issue_number, data);
            } catch (err) {
                console.error('[SSE] Failed to parse dependency.blocked:', err);
            }
        });

        source.addEventListener('dependency.unblocked', function(e) {
            try {
                const data = JSON.parse(e.data);
                console.log('[SSE] Dependency unblocked:', data);
                delete dependencyProblems[data.issue_number];
                updateDependencyWarning(data.issue_number, null);
            } catch (err) {
                console.error('[SSE] Failed to parse dependency.unblocked:', err);
            }
        });

        source.addEventListener('stale.in_progress_detected', function(e) {
            try {
                const data = JSON.parse(e.data);
                console.log('[SSE] Stale in-progress detected:', data);
                staleIssues[data.issue_number] = {
                    issue_number: data.issue_number,
                    consecutive_ticks: 1,
                    persistent: false,
                };
                updateStaleWarning(data.issue_number, staleIssues[data.issue_number]);
            } catch (err) {
                console.error('[SSE] Failed to parse stale.in_progress_detected:', err);
            }
        });

        source.addEventListener('stale.in_progress_cleared', function(e) {
            try {
                const data = JSON.parse(e.data);
                console.log('[SSE] Stale in-progress cleared:', data);
                delete staleIssues[data.issue_number];
                updateStaleWarning(data.issue_number, null);
            } catch (err) {
                console.error('[SSE] Failed to parse stale.in_progress_cleared:', err);
            }
        });

        source.addEventListener('stale.persistent_detected', function(e) {
            try {
                const data = JSON.parse(e.data);
                console.log('[SSE] Persistent stale detected:', data);
                staleIssues[data.issue_number] = {
                    issue_number: data.issue_number,
                    consecutive_ticks: data.consecutive_ticks,
                    persistent: true,
                    threshold: data.threshold,
                };
                updateStaleWarning(data.issue_number, staleIssues[data.issue_number]);
            } catch (err) {
                console.error('[SSE] Failed to parse stale.persistent_detected:', err);
            }
        });

        // E2E lifecycle events — trigger immediate status refresh instead of
        // waiting for the next poll cycle.
        source.addEventListener('e2e.completed', function(event) {
            console.log('[SSE] E2E run completed');
            updateE2EProgress();
        });
        source.addEventListener('e2e.failed', function(event) {
            console.log('[SSE] E2E run failed');
            updateE2EProgress();
        });
        source.addEventListener('e2e.started', function(event) {
            console.log('[SSE] E2E run started');
            updateE2EProgress();
        });
        source.addEventListener('e2e.stopped', function(event) {
            console.log('[SSE] E2E run stopped');
            updateE2EProgress();
        });

        source.onerror = function() {
            console.log('[SSE] Connection error, scheduling reconnect');
            closeEventStream();
            scheduleReconnect();
        };
    }

    async function connectEventStream() {
        closeEventStream();
        try {
            // Control API requires an authenticated query-string token
            // on /api/events (security #6017). Fail fast if the shared
            // helper is not loaded; a raw EventSource would produce an
            // endless unauthenticated reconnect loop.
            if (typeof window.openAuthenticatedSseStream !== 'function') {
                throw new Error('authenticated SSE helper is not loaded');
            }
            evtSource = await window.openAuthenticatedSseStream('/api/events');
            wireEventListeners(evtSource);
        } catch (err) {
            console.error('[SSE] Failed to create EventSource:', err);
            closeEventStream();
            scheduleReconnect();
        }
    }

    connectEventStream();
    healthPollTimer = window.setInterval(() => {
        checkEngineHealth();
    }, HEALTH_POLL_MS);
    // No eager checkEngineHealth() here: at init time evtSource is still
    // null because connectEventStream() is async, so an eager call paints
    // the "Engine reachable. Reconnecting event stream..." banner that
    // SSE's onopen clears ~10ms later — a visible whole-screen flicker on
    // every dashboard load. Real disconnects are caught by the periodic
    // health poll above and by scheduleReconnect() on SSE failure.

    window.addEventListener('beforeunload', () => {
        if (healthPollTimer !== null) {
            window.clearInterval(healthPollTimer);
        }
        if (reconnectTimer !== null) {
            window.clearTimeout(reconnectTimer);
        }
        closeEventStream();
    });
})();

// Helper to add keyboard support to menu items
function addKeyboardSupport(element) {
    if (!element) return;
    element.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            element.click();
        }
    });
}

function clampPagePoint(left, top, width, height, margin = 8) {
    const minLeft = window.scrollX + margin;
    const minTop = window.scrollY + margin;
    const maxLeft = Math.max(minLeft, window.scrollX + window.innerWidth - width - margin);
    const maxTop = Math.max(minTop, window.scrollY + window.innerHeight - height - margin);
    return {
        left: Math.max(minLeft, Math.min(left, maxLeft)),
        top: Math.max(minTop, Math.min(top, maxTop)),
    };
}

function clampClientPoint(left, top, width, height, margin = 8) {
    const minLeft = margin;
    const minTop = margin;
    const maxLeft = Math.max(minLeft, window.innerWidth - width - margin);
    const maxTop = Math.max(minTop, window.innerHeight - height - margin);
    return {
        left: Math.max(minLeft, Math.min(left, maxLeft)),
        top: Math.max(minTop, Math.min(top, maxTop)),
    };
}

function normalizeToClientPoint(point) {
    if (!point) return null;
    if (Number.isFinite(point.clientX) && Number.isFinite(point.clientY)) {
        return { x: Number(point.clientX), y: Number(point.clientY) };
    }
    if (Number.isFinite(point.pageX) && Number.isFinite(point.pageY)) {
        return {
            x: Number(point.pageX) - window.scrollX,
            y: Number(point.pageY) - window.scrollY,
        };
    }
    if (Number.isFinite(point.x) && Number.isFinite(point.y)) {
        return {
            x: Number(point.x) - window.scrollX,
            y: Number(point.y) - window.scrollY,
        };
    }
    return null;
}

// Context menu
