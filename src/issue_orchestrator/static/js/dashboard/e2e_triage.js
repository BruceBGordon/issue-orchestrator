let e2eTriageData = null;

async function showE2ERunDetails(runId) {
    // Redirect to the unified run view via the typed Command pipeline
    // (issue #6322, PR #6329 reviewer Blocker 2).
    return runLifecycleCommand({
        kind: 'open_e2e_run',
        label: 'Open E2E Run',
        run_id: Number(runId),
        expand_run_details: false,
    });
}

// Legacy run-details + per-test detail paths removed in Phase C
// (PR #6319 round 3).  ``showUnifiedRunView`` (canonical viewer)
// owns the run modal end-to-end; per-test detail lives inside the
// canonical viewer's expansion and the ``io.agent-context`` plugin
// block.  See git history for the removed function bodies and
// tests/unit/test_dashboard_ui_guardrails.py for the absence
// guardrail.

async function showE2ETriage() {
    if (!e2eLastRun) {
        showToast('No E2E run data available', true);
        return;
    }

    // Show modal with loading state
    document.getElementById('e2eTriageContent').innerHTML = '<div class="loading-spinner">Loading triage data...</div>';
    document.getElementById('e2eTriageModal').classList.add('visible');

    try {
        const res = await fetch(`/control/e2e/triage/${e2eLastRun.id}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}&mode=${encodeURIComponent(CONFIG_MODE)}`);
        const data = await res.json();

        if (!res.ok) {
            closeE2ETriageModal();
            showToast(data.error || data.detail || 'Failed to load triage data', true);
            return;
        }

        e2eTriageData = data;
        renderE2ETriage(data);
    } catch (err) {
        closeE2ETriageModal();
        showToast('Failed to load triage data: ' + err.message, true);
    }
}

async function triageE2ERun(runId = null) {
    if (runId) {
        e2eLastRun = { ...(e2eLastRun || {}), id: runId };
    }
    return showE2ETriage();
}

function renderE2ETriage(data) {
    const content = document.getElementById('e2eTriageContent');
    const failures = data.failures || [];

    if (failures.length === 0) {
        content.innerHTML = '<p>No failures to triage.</p>';
        return;
    }

    // Count categories
    const newFailures = failures.filter(f => !f.existing_issue && !f.is_likely_flaky);
    const flakyFailures = failures.filter(f => f.category === 'flaky');
    const consistentFailures = failures.filter(f => f.category === 'consistently_failing');
    const existingIssues = failures.filter(f => f.existing_issue);

    let html = `
        <div class="triage-summary">
            <div class="triage-summary-stat failures">
                <div class="count">${failures.length}</div>
                <div class="label">Total Failures</div>
            </div>
            <div class="triage-summary-stat">
                <div class="count">${newFailures.length}</div>
                <div class="label">New</div>
            </div>
            <div class="triage-summary-stat flaky">
                <div class="count">${flakyFailures.length}</div>
                <div class="label">Flaky</div>
            </div>
            <div class="triage-summary-stat" style="${consistentFailures.length > 0 ? 'color: var(--danger);' : ''}">
                <div class="count">${consistentFailures.length}</div>
                <div class="label">Consistent</div>
            </div>
            <div class="triage-summary-stat existing">
                <div class="count">${existingIssues.length}</div>
                <div class="label">Has Issue</div>
            </div>
        </div>

        <div class="triage-select-all">
            <input type="checkbox" id="triageSelectAll" class="triage-checkbox" onchange="toggleAllTriageItems(this.checked)">
            <label for="triageSelectAll">Select all new failures for issue creation</label>
        </div>

        <div class="triage-list">
    `;

    for (const failure of failures) {
        const hasIssue = !!failure.existing_issue;
        const isFlaky = failure.is_likely_flaky;
        const itemClass = hasIssue ? 'has-issue' : (isFlaky ? 'is-flaky' : '');
        const shortNodeid = failure.nodeid.split('::').pop() || failure.nodeid;

        html += `
            <div class="triage-item ${itemClass}">
                <input type="checkbox"
                       class="triage-checkbox triage-item-checkbox"
                       data-nodeid="${escapeAttr(failure.nodeid)}"
                       ${hasIssue ? 'disabled' : ''}
                       ${!hasIssue && !isFlaky ? 'checked' : ''}
                       onchange="updateTriageSelection()">
                <div class="triage-details clickable" data-action="open-test-detail" data-nodeid="${escapeAttr(failure.nodeid)}" title="Click to view full details">
                    <div class="triage-nodeid" title="${escapeHtml(failure.nodeid)}">${escapeHtml(shortNodeid)}</div>
                    <div class="triage-badges">
                        ${hasIssue ? `<span class="triage-badge existing">Issue #${failure.existing_issue.github_issue_number}</span>` : '<span class="triage-badge new">New</span>'}
                        ${failure.category === 'flaky' ? `<span class="triage-badge flaky">Flaky (${failure.flip_rate_percent}%)</span>` : ''}
                        ${failure.category === 'consistently_failing' ? '<span class="triage-badge" style="background: var(--danger); color: var(--tab-badge-text);">Consistent</span>' : ''}
                        ${failure.category === 'new_failure' ? '<span class="triage-badge" style="background: var(--accent); color: var(--tab-badge-text);">New failure</span>' : ''}
                        ${failure.duration_seconds ? `<span class="triage-badge">${failure.duration_seconds.toFixed(1)}s</span>` : ''}
                    </div>
                    ${failure.longrepr ? `<div class="triage-longrepr">${escapeHtml(failure.longrepr.substring(0, 500))}${failure.longrepr.length > 500 ? '...' : ''}</div>` : ''}
                    <span class="triage-detail-hint">→</span>
                </div>
            </div>
        `;
    }

    html += '</div>';

    if (data.has_parent_issue) {
        html += renderIssueStatusSection(data);
    }

    content.innerHTML = html;
    updateTriageSelection();
}

function renderIssueStatusSection(data) {
    const parentUrl = data.parent_issue_url || '#';
    const isClosed = data.parent_issue_closed;
    const subIssues = data.sub_issues || [];
    const summary = data.sub_issues_summary || { total: 0, resolved: 0 };

    let html = `
        <div class="issue-status-section">
            <div class="issue-status-header">
                <span class="issue-status-title">Issue Status</span>
            </div>
            <div style="margin-bottom: 8px;">
                <a href="${parentUrl}" target="_blank" class="issue-link">
                    #${data.parent_issue_number}
                </a>
                <span class="issue-status-badge ${isClosed ? 'closed' : 'open'}">
                    ${isClosed ? 'CLOSED' : 'OPEN'}
                </span>
            </div>
    `;

    if (subIssues.length > 0) {
        html += `
            <div class="sub-issues-summary">
                Sub-issues: <strong>${summary.resolved}</strong> of <strong>${summary.total}</strong> resolved
            </div>
            <div id="subIssuesToggle" class="sub-issues-toggle" onclick="toggleSubIssuesList()">
                <span class="arrow">▶</span>
                <span class="toggle-text">Show sub-issues</span>
            </div>
            <div id="subIssuesList" class="sub-issues-list">
        `;

        for (const issue of subIssues) {
            const shortNodeid = issue.nodeid.split('::').pop() || issue.nodeid;
            const statusClass = issue.resolved ? 'resolved' : 'open';
            const statusText = issue.resolved ? (issue.resolution || 'resolved') : 'open';
            const issueUrl = issue.url || '#';

            html += `
                <div class="sub-issue-item">
                    <a href="${issueUrl}" target="_blank" class="issue-link">#${issue.issue_number}</a>
                    <span class="sub-issue-nodeid" title="${escapeHtml(issue.nodeid)}">${escapeHtml(shortNodeid)}</span>
                    <span class="issue-status-badge ${statusClass}">${statusText.toUpperCase()}</span>
                </div>
            `;
        }

        html += '</div>';
    }

    html += '</div>';
    return html;
}

function toggleSubIssuesList() {
    const list = document.getElementById('subIssuesList');
    const toggle = document.getElementById('subIssuesToggle');
    const isExpanded = list.classList.contains('expanded');

    if (isExpanded) {
        list.classList.remove('expanded');
        toggle.classList.remove('expanded');
        toggle.querySelector('.toggle-text').textContent = 'Show sub-issues';
    } else {
        list.classList.add('expanded');
        toggle.classList.add('expanded');
        toggle.querySelector('.toggle-text').textContent = 'Hide sub-issues';
    }
}

// Toggle E2E sub-issues expansion in issue list
function toggleE2ESubIssues(issueId, event) {
    event.stopPropagation();
    const container = document.getElementById(`e2e-sub-${issueId}`);
    const button = event.currentTarget;

    if (!container) return;

    const isExpanded = container.style.display !== 'none';
    container.style.display = isExpanded ? 'none' : 'block';
    button.setAttribute('aria-expanded', !isExpanded);
}

function toggleAllTriageItems(checked) {
    const checkboxes = document.querySelectorAll('.triage-item-checkbox:not(:disabled)');
    checkboxes.forEach(cb => cb.checked = checked);
    updateTriageSelection();
}

function updateTriageSelection() {
    const checkboxes = document.querySelectorAll('.triage-item-checkbox:checked:not(:disabled)');
    const createBtn = document.getElementById('e2eCreateIssuesBtn');
    createBtn.disabled = checkboxes.length === 0;
    createBtn.textContent = checkboxes.length > 0 ? `Create ${checkboxes.length} Issue${checkboxes.length > 1 ? 's' : ''}` : 'Create Issues';
}

function closeE2ETriageModal() {
    document.getElementById('e2eTriageModal').classList.remove('visible');
}

async function createE2EIssues() {
    const agentSelect = document.getElementById('e2eTriageAgent');
    const agent = agentSelect.value;
    if (!agent) {
        showToast('Please select an agent to work on these issues', true);
        agentSelect.focus();
        return;
    }

    const checkboxes = document.querySelectorAll('.triage-item-checkbox:checked:not(:disabled)');
    const selectedNodeids = Array.from(checkboxes).map(cb => cb.dataset.nodeid);

    if (selectedNodeids.length === 0) {
        showToast('No failures selected', true);
        return;
    }

    const btn = document.getElementById('e2eCreateIssuesBtn');
    btn.disabled = true;
    btn.textContent = 'Creating...';

    try {
        const res = await fetch(`/control/e2e/create-issues/${e2eLastRun.id}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}&mode=${encodeURIComponent(CONFIG_MODE)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ nodeids: selectedNodeids, agent: agent }),
        });
        const data = await res.json();

        if (!res.ok) {
            showToast(data.error || data.detail || 'Failed to create issues', true);
            return;
        }

        showToast(`Created parent issue #${data.parent_issue.number} with ${data.sub_issues.length} sub-issue(s)`);
        closeE2ETriageModal();

        // Open parent issue in new tab
        if (data.parent_issue.url) {
            window.open(data.parent_issue.url, '_blank');
        }
    } catch (err) {
        showToast('Failed to create issues: ' + err.message, true);
    } finally {
        btn.disabled = false;
        updateTriageSelection();
    }
}

async function syncE2EIssues() {
    if (!e2eLastRun) {
        showToast('No E2E run to sync', true);
        return;
    }

    const btn = document.getElementById('e2eSyncBtn');
    btn.disabled = true;
    btn.textContent = 'Syncing...';

    try {
        const res = await fetch(`/control/e2e/sync-issues/${e2eLastRun.id}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}&mode=${encodeURIComponent(CONFIG_MODE)}`, {
            method: 'POST',
        });
        const data = await res.json();

        if (!res.ok) {
            showToast(data.error || data.detail || 'Failed to sync issues', true);
            return;
        }

        const closedCount = data.closed_issues.length;
        const parentCount = data.closed_parent_issues.length;

        if (closedCount === 0 && parentCount === 0) {
            showToast('No issues to sync - all tests still failing or no open issues');
        } else {
            showToast(`Synced: closed ${closedCount} sub-issue(s), ${parentCount} parent issue(s)`);
        }

        // Refresh triage data to show updated state
        if (e2eLastRun.id) {
            triageE2ERun(e2eLastRun.id);
        }
    } catch (err) {
        showToast('Failed to sync issues: ' + err.message, true);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Sync Issues';
    }
}

// Quarantine viewer state
let quarantineData = { current: [], flaky: [], toAdd: new Set(), toRemove: new Set() };

async function showQuarantineViewer() {
    const modal = document.getElementById('e2eQuarantineModal');
    const loading = document.getElementById('quarantineLoading');
    const content = document.getElementById('quarantineContent');

    modal.classList.add('visible');
    loading.style.display = 'block';
    content.style.display = 'none';

    quarantineData = { current: [], flaky: [], toAdd: new Set(), toRemove: new Set() };

    try {
        // Fetch current quarantine list and flaky tests in parallel
        const [quarantineRes, flakyRes] = await Promise.all([
            fetch(`/control/e2e/quarantine?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}&mode=${encodeURIComponent(CONFIG_MODE)}`),
            fetch(`/control/e2e/flaky-tests?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}&mode=${encodeURIComponent(CONFIG_MODE)}&threshold=3&window=10`)
        ]);

        const quarantine = await quarantineRes.json();
        const flaky = await flakyRes.json();

        if (!quarantineRes.ok) {
            showToast(quarantine.error || 'Failed to load quarantine list', true);
            closeQuarantineModal();
            return;
        }

        quarantineData.current = quarantine.tests || [];
        quarantineData.flaky = (flaky.flaky_tests || []).filter(t => !t.is_quarantined);

        document.getElementById('quarantineFile').textContent = `File: ${quarantine.quarantine_file}`;
        renderQuarantineViewer();

        loading.style.display = 'none';
        content.style.display = 'block';
    } catch (err) {
        showToast('Failed to load quarantine data: ' + err.message, true);
        closeQuarantineModal();
    }
}

function renderQuarantineViewer() {
    const currentList = document.getElementById('quarantineCurrentList');
    const flakyList = document.getElementById('quarantineFlakyList');
    const flakySection = document.getElementById('quarantineFlakySection');

    // Render flaky suggestions
    if (quarantineData.flaky.length > 0) {
        flakySection.style.display = 'block';
        flakyList.innerHTML = quarantineData.flaky.map(test => `
            <div class="triage-item">
                <label class="triage-checkbox-label">
                    <input type="checkbox" class="quarantine-add-checkbox"
                           data-nodeid="${escapeAttr(test.nodeid)}"
                           data-action="quarantine-add"
                           ${quarantineData.toAdd.has(test.nodeid) ? 'checked' : ''}>
                    <span class="test-nodeid">${escapeHtml(test.nodeid)}</span>
                    <span class="flake-badge">${test.flip_rate_percent}% flip rate</span>
                </label>
            </div>
        `).join('');
    } else {
        flakySection.style.display = 'none';
    }

    // Render current quarantine list
    if (quarantineData.current.length > 0) {
        currentList.innerHTML = quarantineData.current.map(nodeid => `
            <div class="triage-item ${quarantineData.toRemove.has(nodeid) ? 'to-remove' : ''}">
                <label class="triage-checkbox-label">
                    <input type="checkbox" class="quarantine-remove-checkbox"
                           data-nodeid="${escapeAttr(nodeid)}"
                           data-action="quarantine-remove"
                           ${quarantineData.toRemove.has(nodeid) ? 'checked' : ''}>
                    <span class="test-nodeid">${escapeHtml(nodeid)}</span>
                    ${quarantineData.toRemove.has(nodeid) ? '<span class="remove-badge">will remove</span>' : ''}
                </label>
            </div>
        `).join('');
    } else {
        currentList.innerHTML = '<div class="triage-empty">No tests currently quarantined</div>';
    }

    // Update count and save button state
    const effectiveCount = quarantineData.current.length + quarantineData.toAdd.size - quarantineData.toRemove.size;
    document.getElementById('quarantineCount').textContent = `${effectiveCount} test${effectiveCount !== 1 ? 's' : ''} quarantined`;

    const saveBtn = document.getElementById('quarantineSaveBtn');
    saveBtn.disabled = quarantineData.toAdd.size === 0 && quarantineData.toRemove.size === 0;
}

function toggleQuarantineAdd(nodeid) {
    if (quarantineData.toAdd.has(nodeid)) {
        quarantineData.toAdd.delete(nodeid);
    } else {
        quarantineData.toAdd.add(nodeid);
    }
    renderQuarantineViewer();
}

function toggleQuarantineRemove(nodeid) {
    if (quarantineData.toRemove.has(nodeid)) {
        quarantineData.toRemove.delete(nodeid);
    } else {
        quarantineData.toRemove.add(nodeid);
    }
    renderQuarantineViewer();
}

async function saveQuarantineChanges() {
    const btn = document.getElementById('quarantineSaveBtn');
    btn.disabled = true;
    btn.textContent = 'Saving...';

    try {
        // Process additions
        if (quarantineData.toAdd.size > 0) {
            const addRes = await fetch(`/control/e2e/quarantine?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}&mode=${encodeURIComponent(CONFIG_MODE)}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'add', nodeids: Array.from(quarantineData.toAdd) })
            });
            if (!addRes.ok) {
                const data = await addRes.json();
                throw new Error(data.error || 'Failed to add to quarantine');
            }
        }

        // Process removals
        if (quarantineData.toRemove.size > 0) {
            const removeRes = await fetch(`/control/e2e/quarantine?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}&mode=${encodeURIComponent(CONFIG_MODE)}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'remove', nodeids: Array.from(quarantineData.toRemove) })
            });
            if (!removeRes.ok) {
                const data = await removeRes.json();
                throw new Error(data.error || 'Failed to remove from quarantine');
            }
        }

        showToast(`Quarantine updated: +${quarantineData.toAdd.size} added, -${quarantineData.toRemove.size} removed`);
        closeQuarantineModal();
    } catch (err) {
        showToast('Failed to save quarantine changes: ' + err.message, true);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Save Changes';
    }
}

function closeQuarantineModal() {
    document.getElementById('e2eQuarantineModal').classList.remove('visible');
}

// ============================================================================
// Unified Run View - Replaces separate Triage and Details modals
// ============================================================================
