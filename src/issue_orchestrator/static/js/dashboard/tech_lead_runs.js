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
//
// Accessibility rules this module keeps (round 2 F6):
//
// * Unavailability is `aria-disabled`, never the native `disabled` property. A
//   natively disabled button is removed from the tab order, which is exactly
//   where the explanation and the Settings remedy would become unreachable for
//   keyboard and screen-reader users. The control stays focusable, announces
//   itself as disabled, and its handler refuses the action with the reason.
// * Every surface owns a REAL status element that is a SIBLING of its button,
//   so the reason is visible text with a resolvable `aria-describedby`, and the
//   Settings remedy is inserted beside the button rather than inside it.

const TECH_LEAD_STATUS_LABELS = {
    idle: '',
    queued: 'Tech lead queued',
    running: 'Tech lead running',
};

// The projection the server publishes on every view-model refresh. Before the
// first payload arrives the actions read as "engine not running", which
// disables them rather than promising a run nothing would start — and, unlike
// claiming the agent is unconfigured, does not send the operator to Settings
// for a problem they may not have.
const TECH_LEAD_RUNS_UNKNOWN = {
    globalStatus: 'idle',
    globalStatusLabel: '',
    healthReviewStatus: 'idle',
    healthReviewStatusLabel: '',
    globalBarrierNote: '',
    queuedIssueNumbers: [],
    runningIssueNumbers: [],
    globalBarrierActive: false,
    unavailableReason: 'The Repository Engine is not running. Start it to run tech-lead work.',
    needsSettings: false,
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

// Why the action cannot run right now, or '' when it can.
//
// Both halves come from the server: the ENGINE-level sentence is published as
// `unavailableReason` (resolved by `read_tech_lead_run_actions` in the same
// order `TechLeadRunCoordinator` applies), and an in-flight run of THIS scope
// supplies its own reason from the shared status vocabulary. Nothing about
// availability is decided here — a second copy of that order in the browser is
// exactly how a disabled button ends up contradicting the server's rejection.
function techLeadActionBlockedReason(runStatus) {
    return techLeadRunState().unavailableReason
        || TECH_LEAD_STATUS_LABELS[runStatus]
        || '';
}

// The health review's OWN status. Deliberately not `globalStatus`, which is the
// any-global BARRIER: a queued batch review must not make the health action
// look already-requested, because admission keeps the two identities distinct
// and queues one behind the other (#6994 round 2 F5).
function techLeadHealthReviewStatus() {
    return techLeadRunState().healthReviewStatus || 'idle';
}

// The advisory note shown when a request would QUEUE behind a different global
// run. It is not a blocked reason: the click still does what the operator asked.
function techLeadGlobalBarrierNote() {
    return techLeadRunState().globalBarrierNote || '';
}

// Whether the operator's remedy is Settings. Published by the projection, not
// inferred here, so the UI never decides which remedy a state deserves.
function techLeadNeedsSettings() {
    return Boolean(techLeadRunState().needsSettings);
}

// Set an attribute, or remove it when the value is empty. One helper so the
// present/absent rule is written once instead of at every aria call site.
function setOrRemoveAttribute(element, name, value) {
    if (value) {
        element.setAttribute(name, value);
        return;
    }
    element.removeAttribute(name);
}

// Render one action's availability.
//
// The reason is VISIBLE text in the status element and is programmatically
// associated with the control via `aria-describedby` — not a `title` tooltip.
// `title` is kept as well, purely as a pointer-user convenience.
function applyTechLeadDisabledState(button, blockedReason, sink, runLabel, note) {
    if (!button || !sink) return;
    const blocked = Boolean(blockedReason);
    // aria-disabled rather than the native property: the control must stay
    // focusable so its explanation and its Settings remedy remain reachable
    // (round 2 F6). The click handlers refuse the action themselves.
    button.disabled = false;
    button.setAttribute('aria-disabled', String(blocked));
    button.classList.toggle('is-unavailable', blocked);
    button.title = blockedReason;
    // Text, never colour alone: the queued/running/blocked state stays readable
    // with no styling applied.
    const visible = blockedReason || runLabel || note || '';
    if (!sink.id) sink.id = `${button.id || 'techLeadAction'}Status`;
    setOrRemoveAttribute(button, 'aria-describedby', visible ? sink.id : '');
    sink.textContent = visible;
    applyTechLeadSettingsLink(button, sink);
}

// A keyboard-reachable path to Settings, rendered beside the explanation when
// (and only when) configuration is what is missing. The disabled button itself
// cannot carry the operator anywhere, so the remedy is its own operable control
// — inserted into the status element's PARENT, which every surface makes a
// container element rather than the button (round 2 F6: inserting it into the
// button produced a button nested inside a button).
function applyTechLeadSettingsLink(button, sink) {
    const host = sink.parentNode;
    if (!host || typeof document === 'undefined' || !document.createElement) return;
    const linkId = `${button.id || 'techLeadAction'}SettingsLink`;
    let link = document.getElementById(linkId);
    const wanted = techLeadNeedsSettings() && techLeadActionVisible(button);
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
    if (link.parentNode !== host) host.insertBefore(link, sink.nextSibling);
}

// Whether an action is on screen. Checked on the button AND on the container a
// surface hides it with, so a remedy is never rendered next to a hidden action.
function techLeadActionVisible(button) {
    for (let node = button; node; node = node.parentNode) {
        if (node.hidden) return false;
        if (node.style && node.style.display === 'none') return false;
        if (!node.parentNode) break;
    }
    return true;
}

function refreshTechLeadMenuState() {
    const state = techLeadRunState();
    const status = techLeadHealthReviewStatus();
    applyTechLeadDisabledState(
        document.getElementById('techLeadHealthReviewItem'),
        // The BARRIER never blocks the request; only an unavailable engine or a
        // health review that already exists does.
        state.unavailableReason || TECH_LEAD_STATUS_LABELS[status] || '',
        document.getElementById('techLeadHealthReviewStatus'),
        state.healthReviewStatusLabel || TECH_LEAD_STATUS_LABELS[status] || '',
        techLeadGlobalBarrierNote(),
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
        techLeadGlobalBarrierNote(),
    );
}

function refreshTechLeadCardMenuAction() {
    const button = document.getElementById('menuInvestigateTechLead');
    const row = document.getElementById('menuInvestigateTechLeadRow');
    if (!button || (row && row.style.display === 'none')) return;
    const number = Number(button.dataset && button.dataset.issue);
    if (!Number.isInteger(number) || number <= 0) return;
    // The label is set BEFORE the state is applied: writing `textContent`
    // afterwards would erase nothing here (the reason lives in the sibling
    // status element), but doing it in this order keeps that true by
    // construction rather than by luck (round 2 F6).
    button.textContent = techLeadIssueActionLabel(number);
    applyTechLeadDisabledState(
        button,
        techLeadActionBlockedReason(techLeadIssueStatus(number)),
        document.getElementById('menuInvestigateTechLeadStatus'),
        TECH_LEAD_STATUS_LABELS[techLeadIssueStatus(number)],
        techLeadGlobalBarrierNote(),
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
    // Every visible surface, not just the one that was clicked.
    refreshTechLeadRunControls();
    return payload;
}

async function runBoardHealthReview() {
    closeSettingsMenu();
    const state = techLeadRunState();
    const blocked = state.unavailableReason
        || TECH_LEAD_STATUS_LABELS[techLeadHealthReviewStatus()]
        || '';
    if (blocked) {
        showToast(blocked, 'warning');
        return null;
    }
    try {
        return await submitTechLeadRunRequest(
            uiActionContract.buildGlobalHealthReviewRunRequest(),
            techLeadGlobalBarrierNote()
                || 'Board health review requested.',
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
// VISIBLE and aria-disabled, with its reason on screen, so the capability stays
// discoverable instead of vanishing (#6994 round 1 F7).
// Returns whether the action ended up visible.
function updateTechLeadIssueAction(elements, issueNumber, isBlocked) {
    const eligible = Boolean(isBlocked);
    const { button, statusEl, container } = elements;
    if (!button || !statusEl) return false;
    // A surface that wraps its action in a container hides the container, so the
    // status element and the Settings remedy travel with the button instead of
    // being orphaned on screen.
    (container || button).style.display = eligible ? '' : 'none';
    const runStatus = eligible ? techLeadIssueStatus(issueNumber) : 'idle';
    const blockedReason = eligible ? techLeadActionBlockedReason(runStatus) : '';
    button.classList.toggle('disabled', Boolean(blockedReason));
    applyTechLeadDisabledState(
        button,
        blockedReason,
        statusEl,
        eligible ? TECH_LEAD_STATUS_LABELS[runStatus] : '',
        eligible ? techLeadGlobalBarrierNote() : '',
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
