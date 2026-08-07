let retrospectiveReviewPreviewedIssueIds = [];
let retrospectiveReviewEligibleIssueIds = [];

function parseRetrospectiveReviewIssueInput(value) {
    const matches = String(value || '').match(/\d+/g) || [];
    return uiActionContract.normalizeIssueNumbers(matches);
}

function openRetrospectiveReviewDialog() {
    closeSettingsMenu();
    retrospectiveReviewPreviewedIssueIds = [];
    retrospectiveReviewEligibleIssueIds = [];
    openModal('Review Existing Implementation', `
        <form id="retrospectiveReviewForm" class="retrospective-review-form">
            <label class="prefs-row retrospective-review-field" for="retrospectiveReviewIssues">
                Issue numbers
                <textarea id="retrospectiveReviewIssues" class="retrospective-review-input" rows="6" aria-describedby="retrospectiveReviewHelp retrospectiveReviewBoundary" placeholder="6376, 6377"></textarea>
            </label>
            <div class="retrospective-review-note" id="retrospectiveReviewHelp">
                Eligible issues are labeled with the configured retrospective review trigger. Closed issues stay closed unless the reviewer requests changes. Skipped issues are unchanged.
            </div>
            <div class="retrospective-review-warning" id="retrospectiveReviewBoundary">
                This starts with a reviewer audit of the existing implementation. It does not delete worktrees, delete branches, supersede PRs, or start a coder unless the reviewer requests changes.
            </div>
            <div class="retrospective-review-actions">
                <button type="button" class="issue-action-btn" onclick="closeModal()">Cancel</button>
                <button type="submit" class="issue-action-btn active" id="retrospectiveReviewPreviewBtn">Preview</button>
            </div>
            <div id="retrospectiveReviewResults" class="retrospective-review-results" role="status" aria-live="polite"></div>
        </form>
    `);
    const form = document.getElementById('retrospectiveReviewForm');
    const input = document.getElementById('retrospectiveReviewIssues');
    if (form) {
        form.addEventListener('submit', (event) => {
            event.preventDefault();
            previewRetrospectiveReview();
        });
    }
    if (input) {
        input.addEventListener('input', () => {
            retrospectiveReviewPreviewedIssueIds = [];
            retrospectiveReviewEligibleIssueIds = [];
            const results = document.getElementById('retrospectiveReviewResults');
            if (results) results.replaceChildren();
        });
        setTimeout(() => input.focus(), 0);
    }
}

function setRetrospectiveReviewBusy(busy) {
    const previewBtn = document.getElementById('retrospectiveReviewPreviewBtn');
    const confirmBtn = document.getElementById('retrospectiveReviewConfirmBtn');
    if (previewBtn) previewBtn.disabled = busy;
    if (confirmBtn) confirmBtn.disabled = busy || retrospectiveReviewEligibleIssueIds.length === 0;
}

function renderRetrospectiveReviewMessage(message, type = 'info') {
    const results = document.getElementById('retrospectiveReviewResults');
    if (!results) return;
    results.innerHTML = `<div class="retrospective-review-summary ${escapeAttr(type)}">${escapeHtml(message)}</div>`;
}

function retrospectiveReviewDecisionLabel(decision) {
    if (decision && decision.actionText) return String(decision.actionText);
    if (!decision || !decision.eligible) return 'Skipped';
    return 'Will queue review';
}

function renderRetrospectiveReviewDecision(decision) {
    const issue = Number(decision.issue);
    const title = decision.title ? ` ${decision.title}` : '';
    const action = retrospectiveReviewDecisionLabel(decision);
    const statusClass = action === 'Failed' ? 'failed' : (decision.eligible ? 'eligible' : 'skipped');
    const stateText = decision.state ? ` (${decision.state})` : '';
    const priorPr = decision.prior_pr_number ? ` Prior PR #${Number(decision.prior_pr_number)}.` : '';
    return `
        <li class="retrospective-review-row ${statusClass}">
            <span class="retrospective-review-issue">#${issue}${escapeHtml(title)}${escapeHtml(stateText)}</span>
            <span class="retrospective-review-action">${escapeHtml(action)}</span>
            <span class="retrospective-review-reason">${escapeHtml((decision.reason || '') + priorPr)}</span>
        </li>`;
}

function renderRetrospectiveReviewPreflight(payload) {
    const decisions = Array.isArray(payload.decisions) ? payload.decisions : [];
    const eligible = decisions.filter((decision) => decision.eligible).map((decision) => Number(decision.issue));
    const skipped = decisions.filter((decision) => !decision.eligible).map((decision) => Number(decision.issue));
    retrospectiveReviewEligibleIssueIds = eligible;
    const summaryType = skipped.length ? 'warning' : 'success';
    const summary = `${eligible.length} eligible | ${skipped.length} skipped`;
    const confirmDisabled = eligible.length === 0 ? ' disabled' : '';
    const results = document.getElementById('retrospectiveReviewResults');
    if (!results) return;
    results.innerHTML = `
        <div class="retrospective-review-summary ${summaryType}">${escapeHtml(summary)}</div>
        <ul class="retrospective-review-list">
            ${decisions.map(renderRetrospectiveReviewDecision).join('')}
        </ul>
        <div class="retrospective-review-actions">
            <button type="button" class="issue-action-btn" onclick="previewRetrospectiveReview()">Refresh Preview</button>
            <button type="button" class="issue-action-btn active" id="retrospectiveReviewConfirmBtn" onclick="executeRetrospectiveReview()"${confirmDisabled}>Queue Eligible Reviews</button>
        </div>
    `;
}

async function previewRetrospectiveReview() {
    const input = document.getElementById('retrospectiveReviewIssues');
    const issueNumbers = parseRetrospectiveReviewIssueInput(input ? input.value : '');
    if (!issueNumbers.length) {
        renderRetrospectiveReviewMessage('Enter at least one issue number.', 'warning');
        return;
    }
    retrospectiveReviewPreviewedIssueIds = issueNumbers;
    setRetrospectiveReviewBusy(true);
    renderRetrospectiveReviewMessage('Checking issues...', 'info');
    try {
        const req = uiActionContract.buildRetrospectiveReviewPreflightRequest(issueNumbers);
        const res = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            renderRetrospectiveReviewMessage(data.error || `Preview failed (${res.status})`, 'error');
            showToast(data.error || `Retrospective review preview failed (${res.status})`, 'error');
            return;
        }
        renderRetrospectiveReviewPreflight(data);
    } catch (err) {
        console.error('Retrospective review preview failed:', err);
        renderRetrospectiveReviewMessage('Preview failed: network error', 'error');
        showToast('Retrospective review preview failed: network error', 'error');
    } finally {
        setRetrospectiveReviewBusy(false);
    }
}

function renderRetrospectiveReviewExecution(payload) {
    const queued = Array.isArray(payload.queued) ? payload.queued : [];
    const skipped = Array.isArray(payload.skipped) ? payload.skipped : [];
    const failed = Array.isArray(payload.failed) ? payload.failed : [];
    const rows = [
        ...queued.map((item) => ({
            ...item,
            eligible: true,
            actionText: item.queued === false ? 'Already queued' : 'Queued review',
            reason: item.queued === false ? 'Retrospective review was already queued or running' : 'Review queued',
        })),
        ...skipped,
        ...failed.map((item) => ({
            issue: item.issue,
            eligible: false,
            actionText: 'Failed',
            title: null,
            state: null,
            reason: item.error || 'Queue failed',
        })),
    ];
    const results = document.getElementById('retrospectiveReviewResults');
    if (!results) return;
    results.innerHTML = `
        <div class="retrospective-review-summary ${failed.length ? 'error' : 'success'}">
            ${escapeHtml(`${queued.length} queued, ${skipped.length} skipped, ${failed.length} failed`)}
        </div>
        <ul class="retrospective-review-list">
            ${rows.map(renderRetrospectiveReviewDecision).join('')}
        </ul>
    `;
}

async function executeRetrospectiveReview() {
    if (!retrospectiveReviewPreviewedIssueIds.length) {
        await previewRetrospectiveReview();
    }
    if (!retrospectiveReviewEligibleIssueIds.length) {
        renderRetrospectiveReviewMessage('No eligible issues to queue.', 'warning');
        return;
    }
    const confirmMsg = `Queue retrospective review for ${retrospectiveReviewEligibleIssueIds.length} issue(s)?\n\nThis will apply the configured trigger label and start with reviewer audit. Closed issues stay closed unless the reviewer requests changes. It will not delete worktrees, delete branches, supersede PRs, or start a coder unless changes are requested.`;
    const confirmBtn = document.getElementById('retrospectiveReviewConfirmBtn');
    if (!await showConfirm(confirmMsg, confirmBtn)) return;

    setRetrospectiveReviewBusy(true);
    renderRetrospectiveReviewMessage('Queueing eligible reviews...', 'info');
    try {
        const req = uiActionContract.buildRetrospectiveReviewExecuteRequest(retrospectiveReviewPreviewedIssueIds);
        const res = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            renderRetrospectiveReviewMessage(data.error || `Queue failed (${res.status})`, 'error');
            showToast(data.error || `Retrospective review failed (${res.status})`, 'error');
            return;
        }
        renderRetrospectiveReviewExecution(data);
        const queuedCount = Array.isArray(data.queued) ? data.queued.length : 0;
        const skippedCount = Array.isArray(data.skipped) ? data.skipped.length : 0;
        const failedCount = Array.isArray(data.failed) ? data.failed.length : 0;
        const toastType = failedCount > 0 ? 'warning' : 'success';
        showToast(`Retrospective review: ${queuedCount} queued, ${skippedCount} skipped, ${failedCount} failed`, toastType);
        if (queuedCount > 0) await refreshViewModel();
    } catch (err) {
        console.error('Retrospective review failed:', err);
        renderRetrospectiveReviewMessage('Queue failed: network error', 'error');
        showToast('Retrospective review failed: network error', 'error');
    } finally {
        setRetrospectiveReviewBusy(false);
    }
}
