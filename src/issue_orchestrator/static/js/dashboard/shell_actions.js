async function showInfo() {
    closeSettingsMenu();
    const res = await fetch('/api/dialog/info');
    const data = await res.json();
    const html = (data.rows || []).map(row => `
        <div class="info-row"><span class="info-label">${escapeHtml(row.label)}</span><span class="info-value">${escapeHtml(String(row.value))}</span></div>
    `).join('');
    openModal(data.title || 'About Issue Orchestrator', html);
}

async function showConfig() {
    closeSettingsMenu();
    const res = await fetch('/api/dialog/config');
    const data = await res.json();
    openModal(data.title || 'Configuration', `<pre>${escapeHtml(data.config_text || '')}</pre>`);
}

async function showDebug() {
    closeSettingsMenu();
    const res = await fetch('/api/dialog/debug');
    const data = await res.json();
    let html = '';
    for (const section of data.sections || []) {
        html += `<h3 style="margin: 0 0 8px; font-size: 14px;">${escapeHtml(section.title)}</h3>`;
        html += (section.rows || []).map(row => `
            <div class="info-row"><span class="info-label">${escapeHtml(row.label)}</span><span class="info-value">${escapeHtml(String(row.value))}</span></div>
        `).join('');
        html += '<div style="height: 12px;"></div>';
    }
    openModal(data.title || 'Debug Info', html);
}

async function showDoctor() {
    closeSettingsMenu();
    const res = await fetch('/api/dialog/doctor');
    const data = await res.json();

    const statusIcon = {
        'ok': '✅',
        'warning': '⚠️',
        'error': '❌'
    };

    const html = `
        <div style="margin-bottom: 12px;">
            <span style="font-size: 18px;">${statusIcon[data.overall]}</span>
            <span style="font-weight: 600; margin-left: 8px;">Overall: ${String(data.overall || '').toUpperCase()}</span>
        </div>
        ${(data.checks || []).map(c => `
            <div class="info-row">
                <span class="info-label">${statusIcon[c.status]} ${escapeHtml(c.name || '')}</span>
                <span class="info-value" style="font-size: 12px;">${escapeHtml(c.detail || '')}</span>
            </div>
        `).join('')}
    `;
    openModal(data.title || 'Doctor', html);
}

function openRepo() {
    closeSettingsMenu();
    window.open(`https://github.com/${window.dashboardData.repo}`, '_blank');
}

function formatRelativeMillis(deltaMs) {
    if (deltaMs <= 0) return 'now';
    const totalMinutes = Math.round(deltaMs / 60000);
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    if (hours > 0 && minutes > 0) return `${hours}h ${minutes}m`;
    if (hours > 0) return `${hours}h`;
    return `${minutes}m`;
}

function formatNextRun(nextInfo) {
    if (!nextInfo) return '';
    const reason = nextInfo.next_run_reason;
    if (reason === 'interval' && nextInfo.next_run_at) {
        const when = new Date(nextInfo.next_run_at);
        const deltaMs = when.getTime() - Date.now();
        const timeStr = formatTimestamp(when);
        return `Next: in ${formatRelativeMillis(deltaMs)} (at ${timeStr})`;
    }
    if (reason === 'main_unchanged') return 'Next: waiting for main change';
    if (reason === 'ready') return 'Next: pending';
    if (reason === 'auto_disabled') return 'Auto: off';
    if (reason === 'running') return '';
    return '';
}

async function clearHistory() {
    closeSettingsMenu();
    if (!confirm('Clear all session history (completed, failed, etc.)?')) return;
    const res = await fetch('/api/history/clear', { method: 'POST' });
    const data = await res.json();
    if (data.cleared !== undefined) {
        openModal('History Cleared', `<p>Cleared ${data.cleared} history entries.</p>`);
        setTimeout(() => location.reload(), 1500);
    } else {
        alert(data.error || 'Failed to clear history');
    }
}

// Toast notification
let toastTimer = null;

function hideToast(toast) {
    toast.classList.remove('visible');
}

function normalizeToastType(type) {
    if (type === true) return 'error';
    if (type === false || type === null || type === undefined) return 'info';
    if (['error', 'warning', 'success', 'info'].includes(type)) return type;
    return 'info';
}

function showToast(message, type = false) {
    const toast = document.getElementById('toast');
    if (!toast) return;
    if (!toast.dataset.dismissBound) {
        toast.addEventListener('click', (e) => {
            // Sticky toasts (errors/warnings) only dismiss via the explicit
            // close button — clicking the message must not lose diagnostic
            // detail the user might be trying to read or copy.
            if (toast.classList.contains('sticky')) return;
            // Don't dismiss when the user is selecting text to copy.
            const sel = window.getSelection && window.getSelection();
            if (sel && sel.toString && sel.toString().length > 0) return;
            // Ignore clicks on the explicit close button — its own handler runs.
            if (e.target && e.target.classList && e.target.classList.contains('toast-close')) {
                return;
            }
            clearTimeout(toastTimer);
            hideToast(toast);
        });
        toast.dataset.dismissBound = 'true';
    }
    const toastType = normalizeToastType(type);
    // Errors and warnings stay until dismissed so the user can read or copy
    // the diagnostic detail (e.g. GitHub API reason text). Info/success
    // confirmations auto-dismiss.
    const sticky = toastType === 'error' || toastType === 'warning';
    toast.classList.remove('info', 'success', 'warning', 'error', 'visible', 'sticky');
    toast.replaceChildren();
    const messageEl = document.createElement('span');
    messageEl.className = 'toast-message';
    messageEl.textContent = message;
    toast.appendChild(messageEl);
    if (sticky) {
        const closeBtn = document.createElement('button');
        closeBtn.type = 'button';
        closeBtn.className = 'toast-close';
        closeBtn.setAttribute('aria-label', 'Dismiss');
        closeBtn.textContent = '×';
        closeBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            clearTimeout(toastTimer);
            hideToast(toast);
        });
        toast.appendChild(closeBtn);
        toast.classList.add('sticky');
    }
    toast.classList.add(toastType);
    toast.classList.add('visible');
    clearTimeout(toastTimer);
    if (!sticky) {
        toastTimer = setTimeout(() => hideToast(toast), 3000);
    }
}

/**
 * Show a positioned confirmation popover near an anchor element.
 * Returns a Promise that resolves to true (confirm) or false (cancel).
 */
function showConfirm(message, anchorEl) {
    return new Promise(resolve => {
        // Remove any existing confirm popover
        const existing = document.getElementById('confirmPopover');
        if (existing) { existing.remove(); resolve(false); return; }

        const overlay = document.createElement('div');
        overlay.id = 'confirmPopover';
        overlay.className = 'confirm-overlay';

        const box = document.createElement('div');
        box.className = 'confirm-box';
        box.innerHTML = `
            <div class="confirm-message">${escapeHtml(message)}</div>
            <div class="confirm-actions">
                <button class="confirm-btn confirm-btn-cancel">Cancel</button>
                <button class="confirm-btn confirm-btn-ok">Confirm</button>
            </div>`;

        overlay.appendChild(box);
        document.body.appendChild(overlay);
        box.style.position = 'fixed';
        box.style.transform = 'none';
        box.style.maxWidth = `min(320px, ${Math.max(220, window.innerWidth - 16)}px)`;

        // Position near anchor element or pointer location.
        if (anchorEl) {
            const rect = box.getBoundingClientRect();
            const boxW = rect.width || 280;
            const boxH = rect.height || 120;
            let top = 8;
            let left = 8;

            if (typeof anchorEl.getBoundingClientRect === 'function') {
                const anchorRect = anchorEl.getBoundingClientRect();
                top = anchorRect.bottom + 6;
                left = anchorRect.left + anchorRect.width / 2 - boxW / 2;
                if (top + boxH > window.innerHeight - 8) top = anchorRect.top - boxH - 6;
            } else {
                const point = normalizeToClientPoint(anchorEl);
                if (point) {
                    top = point.y + 6;
                    left = point.x - (boxW / 2);
                    if (top + boxH > window.innerHeight - 8) top = point.y - boxH - 6;
                }
            }

            const clamped = clampClientPoint(left, top, boxW, boxH, 8);
            box.style.top = `${clamped.top}px`;
            box.style.left = `${clamped.left}px`;
        }

        function cleanup(result) {
            overlay.remove();
            resolve(result);
        }

        overlay.addEventListener('click', e => { if (e.target === overlay) cleanup(false); });
        box.querySelector('.confirm-btn-cancel').addEventListener('click', () => cleanup(false));
        box.querySelector('.confirm-btn-ok').addEventListener('click', () => cleanup(true));
        box.querySelector('.confirm-btn-ok').focus();
    });
}

// Create Issue Modal Functions
async function loadMilestones() {
    const select = document.getElementById('issueMilestone');
    if (!select) return;

    try {
        const res = await fetch('/api/milestones');
        const data = await res.json();

        if (data.error) {
            console.error('[milestones] Error:', data.error);
            select.innerHTML = '<option value="">No milestone</option>';
            return;
        }

        // Clear and add default option
        select.innerHTML = '<option value="">No milestone</option>';

        // Separate included and excluded milestones
        const included = (data.milestones || []).filter(m => m.included);
        const excluded = (data.milestones || []).filter(m => !m.included);

        // Add included milestones first
        for (const m of included) {
            const option = document.createElement('option');
            option.value = m.number;
            option.textContent = m.title;
            select.appendChild(option);
        }

        // Add excluded milestones with clear warning
        if (excluded.length > 0 && data.filter_active) {
            const separator = document.createElement('option');
            separator.disabled = true;
            separator.textContent = '── Won\'t be picked up by this orchestrator ──';
            select.appendChild(separator);

            for (const m of excluded) {
                const option = document.createElement('option');
                option.value = m.number;
                option.textContent = `${m.title}`;
                option.style.color = 'var(--text-muted)';
                select.appendChild(option);
            }
        }

        // Update hint
        const hint = document.getElementById('milestoneHint');
        if (hint) {
            if (data.filter_active) {
                hint.textContent = `Orchestrator filters to: ${data.filter_milestones.join(', ')}`;
            } else {
                hint.textContent = '';
            }
        }
    } catch (err) {
        console.error('[milestones] Failed to load:', err);
        select.innerHTML = '<option value="">Failed to load milestones</option>';
    }
}

async function createIssue() {
    const title = document.getElementById('issueTitle').value.trim();
    const body = document.getElementById('issueBody').value.trim();
    const agent = document.getElementById('issueAgent').value;
    const priority = document.getElementById('issuePriority').value;
    const milestone = document.getElementById('issueMilestone').value;
    const refreshAfter = document.getElementById('refreshAfterCreate').checked;

    if (!title) {
        showToast('Title is required', true);
        return;
    }

    if (!agent) {
        showToast('Please select an agent', true);
        return;
    }

    showToast('Creating issue...');

    try {
        const res = await fetch('/api/issues', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                title: title,
                body: body,
                agent: agent,
                priority: priority || undefined,
                milestone: milestone ? parseInt(milestone) : undefined,
            }),
        });
        const data = await res.json();

        if (data.error) {
            showToast(data.error, true);
            return;
        }

        showToast(`Issue #${data.issue_number} created!`);

        // Clear the form
        document.getElementById('issueTitle').value = '';
        document.getElementById('issueBody').value = '';
        document.getElementById('issueAgent').selectedIndex = 0;
        document.getElementById('issuePriority').selectedIndex = 0;
        document.getElementById('issueMilestone').selectedIndex = 0;

        // Trigger refresh if requested
        if (refreshAfter) {
            try {
                await fetch('/api/refresh', { method: 'POST' });
                showToast('Queue refreshed');
            } catch (err) {
                // Ignore refresh errors
            }
        }

        // Open the issue in a new tab
        if (data.url) {
            window.open(data.url, '_blank');
        }

    } catch (err) {
        showToast('Failed to create issue', true);
    }
}

// Create Issue Modal Functions
function openCreateIssueModal() {
    document.getElementById('createIssueModal').classList.add('visible');
    loadMilestones();
}

function closeCreateIssueModal() {
    document.getElementById('createIssueModal').classList.remove('visible');
}

// E2E Test Functions
