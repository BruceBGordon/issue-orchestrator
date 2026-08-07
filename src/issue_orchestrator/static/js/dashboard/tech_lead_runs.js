// Scoped tech-lead run requests from the repository dashboard (#6994).
//
// Two actions, one command surface: "Run board health review" (whole board) and
// "Investigate with tech lead" (one blocked issue). Both build their request
// through `uiActionContract`, both check `response.ok`, and both surface the
// server's typed rejection detail in a durable (sticky) toast — server-side
// admission is authoritative, and a disabled button is only an affordance.
//
// This module is also the ONE owner of how those actions render. Every surface
// that can change the run state — a view-model refresh, an SSE-driven refresh,
// an admission response, opening the drawer, opening a card menu — ends in
// `refreshTechLeadRunControls()`, so no surface can be left showing "idle" for
// a run the server has already queued (#6994 round 1 F6).

const TECH_LEAD_STATUS_LABELS = {
    idle: '',
    queued: 'Tech lead queued',
    running: 'Tech lead running',
};

const TECH_LEAD_NOT_RUNNING_REASON = 'The Repository Engine is not running. Start it to run tech-lead work.';
const TECH_LEAD_PAUSED_REASON = 'The Repository Engine is paused. Resume it to run tech-lead work.';
const TECH_LEAD_UNCONFIGURED_REASON = 'No tech lead agent is configured for this repository.';

// The projection the server publishes on every view-model refresh. Before the
// first payload arrives the actions read as "engine not running", which
// disables them rather than promising a run nothing would start — and, unlike
// claiming the agent is unconfigured, does not send the operator to Settings
// for a problem they may not have.
const TECH_LEAD_RUNS_UNKNOWN = {
    configured: true,
    running: false,
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

// Why the action cannot run right now, or '' when it can. The same reasons the
// server enforces, so the visible explanation never contradicts the rejection an
// operator would get by clicking anyway. An in-flight run is its own reason,
// read straight from the status vocabulary rather than re-branched here.
//
// Order matters: engine liveness, then configuration, then pause, then run
// state — the same order `TechLeadRunCoordinator` applies, so the reason shown
// is the reason the server would give.
function techLeadActionBlockedReason(runStatus) {
    const state = techLeadRunState();
    if (state.running === false) return TECH_LEAD_NOT_RUNNING_REASON;
    if (!state.configured) return TECH_LEAD_UNCONFIGURED_REASON;
    if (state.paused) return TECH_LEAD_PAUSED_REASON;
    return TECH_LEAD_STATUS_LABELS[runStatus] || '';
}

// True when the only thing missing is a configured tech lead agent, which is
// the one blocked state the operator fixes in Settings.
function techLeadNeedsSettings() {
    const state = techLeadRunState();
    return state.running !== false && !state.configured;
}

// A no-op status sink for surfaces that carry their state in the action's own
// label (the compact card menu). No nulls: the owner always has somewhere to
// write, so no caller needs a presence check.
const NO_STATUS_TEXT = { textContent: '' };

// Render one action's availability.
//
// The reason is VISIBLE text in the status element and is programmatically
// associated with the control via `aria-describedby` — not a `title` tooltip.
// A natively disabled button is not keyboard focusable, so a tooltip is an
// explanation no keyboard or screen-reader user can reach (#6994 round 1 F7).
// `title` is kept as well, purely as a pointer-user convenience.
function applyTechLeadDisabledState(button, blockedReason, statusEl, statusText) {
    if (!button) return;
    button.disabled = Boolean(blockedReason);
    // aria mirrors the real disabled state by construction, so the two cannot
    // drift; the reason itself becomes the visible status text.
    button.setAttribute('aria-disabled', String(button.disabled));
    button.title = blockedReason;
    // Text, never colour alone: the queued/running/blocked state stays readable
    // with no styling applied.
    const visible = blockedReason || statusText;
    if (statusEl && typeof statusEl.setAttribute === 'function') {
        if (!statusEl.id) statusEl.id = `${button.id || 'techLeadAction'}Status`;
        if (visible) {
            button.setAttribute('aria-describedby', statusEl.id);
        } else {
            button.removeAttribute('aria-describedby');
        }
    }
    if (statusEl) statusEl.textContent = visible;
    applyTechLeadSettingsLink(button, statusEl);
}

// A keyboard-reachable path to Settings, rendered beside the explanation when
// (and only when) configuration is what is missing. A disabled button cannot
// carry the operator anywhere, so the remedy has to be its own operable
// control (#6994 round 1 F7).
function applyTechLeadSettingsLink(button, statusEl) {
    if (!statusEl || typeof document === 'undefined' || !document.createElement) return;
    const host = statusEl.parentNode;
    if (!host || typeof host.insertBefore !== 'function') return;
    const linkId = `${button.id || 'techLeadAction'}SettingsLink`;
    let link = document.getElementById(linkId);
    const wanted = techLeadNeedsSettings() && !button.hidden && button.style.display !== 'none';
    if (!wanted) {
        if (link && link.parentNode) link.parentNode.removeChild(link);
        return;
    }
    if (!link) {
        link = document.createElement('button');
        link.id = linkId;
        link.type = 'button';
        link.className = 'tech-lead-settings-link';
        link.textContent = 'Open Settings';
        link.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            if (typeof showConfigDialog === 'function') showConfigDialog();
        });
        if (typeof addKeyboardSupport === 'function') addKeyboardSupport(link);
    }
    if (link.parentNode !== host) host.insertBefore(link, statusEl.nextSibling);
}

function refreshTechLeadMenuState() {
    const state = techLeadRunState();
    const globalStatus = state.globalStatus || 'idle';
    applyTechLeadDisabledState(
        document.getElementById('techLeadHealthReviewItem'),
        techLeadActionBlockedReason(globalStatus),
        document.getElementById('techLeadHealthReviewStatus'),
        state.globalStatusLabel || TECH_LEAD_STATUS_LABELS[globalStatus] || '',
    );
}

// THE refresh owner. Reapplies every tech-lead affordance that is currently on
// screen from the latest `window.dashboardData`. Called after each view-model
// replacement and after each admission response, so a targeted request made
// from the card menu also updates an already-open drawer, and a background
// refresh cannot leave the global action stale.
function refreshTechLeadRunControls() {
    refreshTechLeadMenuState();
    refreshTechLeadDrawerAction();
    refreshTechLeadCardMenuAction();
}

function refreshTechLeadDrawerAction() {
    const button = document.getElementById('issueDetailInvestigateTechLeadBtn');
    if (!button || button.style.display === 'none') return;
    const number = Number(issueDetailData && issueDetailData.issue_number);
    if (!Number.isInteger(number) || number <= 0) return;
    applyTechLeadDisabledState(
        button,
        techLeadActionBlockedReason(techLeadIssueStatus(number)),
        document.getElementById('issueDetailTechLeadStatus'),
        TECH_LEAD_STATUS_LABELS[techLeadIssueStatus(number)],
    );
}

function refreshTechLeadCardMenuAction() {
    const button = document.getElementById('menuInvestigateTechLead');
    if (!button || button.style.display === 'none') return;
    const number = Number(button.dataset && button.dataset.issue);
    if (!Number.isInteger(number) || number <= 0) return;
    applyTechLeadDisabledState(
        button,
        techLeadActionBlockedReason(techLeadIssueStatus(number)),
        NO_STATUS_TEXT,
        TECH_LEAD_STATUS_LABELS[techLeadIssueStatus(number)],
    );
    button.textContent = techLeadIssueActionLabel(number);
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
    // Every visible surface, not just the one that was clicked.
    refreshTechLeadRunControls();
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
        refreshTechLeadRunControls();
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

// Show the targeted action for eligible blocked cards, and keep the drawer's
// copy in lockstep with the menu's (compact/expanded parity).
//
// Eligibility is the ISSUE's property (is this a blocked work item?), never the
// engine's availability: an unconfigured or stopped engine leaves the action
// VISIBLE and disabled, with its reason on screen, so the capability stays
// discoverable instead of vanishing (#6994 round 1 F7).
// Returns whether the action ended up visible.
function updateTechLeadIssueAction(elements, issueNumber, isBlocked) {
    const eligible = Boolean(isBlocked);
    const { button, statusEl = NO_STATUS_TEXT } = elements;
    if (!button) return false;
    button.style.display = eligible ? '' : 'none';
    const runStatus = eligible ? techLeadIssueStatus(issueNumber) : 'idle';
    const blockedReason = eligible ? techLeadActionBlockedReason(runStatus) : '';
    button.classList.toggle('disabled', Boolean(blockedReason));
    applyTechLeadDisabledState(
        button,
        blockedReason,
        statusEl,
        eligible ? TECH_LEAD_STATUS_LABELS[runStatus] : '',
    );
    return eligible;
}

document.addEventListener('DOMContentLoaded', () => {
    refreshTechLeadRunControls();
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
