// Scoped tech-lead run requests from the repository dashboard (#6994).
//
// Two actions, one command surface: "Run board health review" (whole board) and
// "Investigate with tech lead" (one blocked issue). Both build their request
// through `uiActionContract`, both check `response.ok`, and both surface the
// server's typed rejection detail in a durable (sticky) toast — server-side
// admission is authoritative, and a disabled button is only an affordance.

const TECH_LEAD_STATUS_LABELS = {
    idle: '',
    queued: 'Tech lead queued',
    running: 'Tech lead running',
};

// The projection the server publishes on every view-model refresh. Before the
// first payload arrives the actions read as not-configured, which disables them
// rather than promising a run nothing would start.
const TECH_LEAD_RUNS_UNKNOWN = {
    configured: false,
    paused: false,
    globalStatus: 'idle',
    globalStatusLabel: '',
    queuedIssueNumbers: [],
    runningIssueNumbers: [],
    globalBarrierActive: false,
};

function techLeadRunState() {
    return (window.dashboardData && window.dashboardData.techLeadRuns) || TECH_LEAD_RUNS_UNKNOWN;
}

function techLeadIssueStatus(issueNumber) {
    const state = techLeadRunState();
    const number = Number(issueNumber);
    if ((state.runningIssueNumbers || []).includes(number)) return 'running';
    if ((state.queuedIssueNumbers || []).includes(number)) return 'queued';
    return 'idle';
}

// A no-op status sink for surfaces that carry their state in the action's own
// label (the compact card menu). No nulls: the owner always has somewhere to
// write, so no caller needs a presence check.
const NO_STATUS_TEXT = { textContent: '' };

// Why the action cannot run right now, or '' when it can. The same reasons the
// server enforces, so the disabled tooltip never contradicts the rejection an
// operator would get by clicking anyway. An in-flight run is its own reason,
// read straight from the status vocabulary rather than re-branched here.
function techLeadActionBlockedReason(runStatus) {
    const state = techLeadRunState();
    if (!state.configured) return 'No tech lead agent is configured. Open Settings to add one.';
    if (state.paused) return 'The Repository Engine is paused. Resume it to run tech-lead work.';
    return TECH_LEAD_STATUS_LABELS[runStatus] || '';
}

function applyTechLeadDisabledState(button, blockedReason, statusEl, statusText) {
    button.disabled = Boolean(blockedReason);
    // aria mirrors the real disabled state by construction, so the two cannot
    // drift; the reason itself becomes the tooltip.
    button.setAttribute('aria-disabled', String(button.disabled));
    button.title = blockedReason;
    // Text, never colour alone: the queued/running state stays readable with no
    // styling applied.
    statusEl.textContent = statusText;
}

function refreshTechLeadMenuState() {
    const state = techLeadRunState();
    const globalStatus = state.globalStatus || 'idle';
    const blocked = state.configured
        ? (state.paused
            ? 'The Repository Engine is paused. Resume it to run tech-lead work.'
            : (globalStatus === 'idle' ? '' : (state.globalStatusLabel || TECH_LEAD_STATUS_LABELS[globalStatus] || '')))
        : 'No tech lead agent is configured. Open Settings to add one.';
    applyTechLeadDisabledState(
        document.getElementById('techLeadHealthReviewItem'),
        blocked,
        document.getElementById('techLeadHealthReviewStatus'),
        state.globalStatusLabel || TECH_LEAD_STATUS_LABELS[globalStatus] || '',
    );
}

async function submitTechLeadRunRequest(request, pendingMessage) {
    const res = await fetch(request.endpoint, {
        method: request.method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request.body),
    });
    let payload = null;
    try {
        payload = await res.json();
    } catch (err) {
        payload = null;
    }
    if (!res.ok) {
        const detail = (payload && (payload.detail || payload.error))
            || `Tech-lead request failed (HTTP ${res.status})`;
        // Errors and warnings are sticky toasts by design: the operator must be
        // able to read (and copy) the typed reason.
        showToast(detail, res.status >= 500 ? 'error' : 'warning');
        return payload;
    }
    const detail = (payload && payload.detail) || pendingMessage;
    const admitted = Boolean(payload && payload.admitted);
    showToast(detail, admitted ? 'success' : 'info');
    await refreshViewModel();
    refreshTechLeadMenuState();
    return payload;
}

async function runBoardHealthReview() {
    closeSettingsMenu();
    const button = document.getElementById('techLeadHealthReviewItem');
    const blocked = techLeadActionBlockedReason(techLeadRunState().globalStatus);
    if (blocked) {
        showToast(blocked, 'warning');
        return null;
    }
    if (button) button.disabled = true;
    try {
        return await submitTechLeadRunRequest(
            uiActionContract.buildGlobalHealthReviewRunRequest(),
            'Board health review requested.',
        );
    } catch (err) {
        showToast(`Board health review request failed: ${err.message || err}`, 'error');
        return null;
    } finally {
        refreshTechLeadMenuState();
    }
}

async function investigateWithTechLead(issueNumber) {
    const number = Number(issueNumber);
    if (!Number.isInteger(number) || number <= 0) {
        showToast('Cannot investigate: no issue number for this card.', 'warning');
        return null;
    }
    const blocked = techLeadActionBlockedReason(techLeadIssueStatus(number));
    if (blocked) {
        showToast(blocked, 'warning');
        return null;
    }
    try {
        return await submitTechLeadRunRequest(
            uiActionContract.buildIssueInvestigationRunRequest(number),
            `Tech-lead investigation requested for #${number}.`,
        );
    } catch (err) {
        showToast(`Tech-lead request failed: ${err.message || err}`, 'error');
        return null;
    }
}

function investigateWithTechLeadFromDrawer() {
    return investigateWithTechLead(issueDetailData && issueDetailData.issue_number);
}

// The exact label the targeted action carries, including its live state — one
// owner, so the compact card menu and the drawer never disagree.
function techLeadIssueActionLabel(issueNumber) {
    const label = TECH_LEAD_STATUS_LABELS[techLeadIssueStatus(issueNumber)];
    return label ? `Investigate with tech lead — ${label}` : 'Investigate with tech lead';
}

// Hide the targeted action and clear its state text. The owner of the action's
// appearance owns its reset too, so a surface re-rendering cannot leave a stale
// "Tech lead queued" behind on an issue that no longer has a run.
function resetTechLeadIssueAction(elements) {
    return updateTechLeadIssueAction(elements, 0, false);
}

// Show the targeted action only for eligible blocked cards, and keep the
// drawer's copy in lockstep with the menu's (compact/expanded parity).
// Returns whether the action ended up visible.
function updateTechLeadIssueAction(elements, issueNumber, isBlocked) {
    const eligible = Boolean(isBlocked) && techLeadRunState().configured;
    const { button, statusEl = NO_STATUS_TEXT } = elements;
    button.style.display = eligible ? '' : 'none';
    button.classList.toggle('disabled', !eligible);
    const runStatus = eligible ? techLeadIssueStatus(issueNumber) : 'idle';
    applyTechLeadDisabledState(
        button,
        eligible ? techLeadActionBlockedReason(runStatus) : '',
        statusEl,
        eligible ? TECH_LEAD_STATUS_LABELS[runStatus] : '',
    );
    return eligible;
}

document.addEventListener('DOMContentLoaded', () => {
    refreshTechLeadMenuState();
    const menuItem = document.getElementById('menuInvestigateTechLead');
    if (menuItem) {
        menuItem.addEventListener('click', (event) => {
            event.stopPropagation();
            const contextMenu = document.getElementById('contextMenu');
            if (contextMenu) contextMenu.classList.remove('visible');
            investigateWithTechLead(menuItem.dataset.issue);
        });
        if (typeof addKeyboardSupport === 'function') addKeyboardSupport(menuItem);
    }
});
