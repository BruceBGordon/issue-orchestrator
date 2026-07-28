let blockedIssuesData = [];
const blockedModal = document.getElementById('blockedModal');
const blockedList = document.getElementById('blockedList');
const blockedSelectAll = document.getElementById('blockedSelectAll');
const blockedSelectAllLabel = document.getElementById('blockedSelectAllLabel');
const blockedWarning = document.getElementById('blockedWarning');
const blockedWarningText = document.getElementById('blockedWarningText');
const blockedUnblockBtn = document.getElementById('blockedUnblockBtn');
const blockedResetBtn = document.getElementById('blockedResetBtn');

async function openBlockedModal() {
    // Fetch blocked issues
    try {
        const res = await fetch('/api/dialog/blocked-issues');
        const data = await res.json();
        blockedIssuesData = data.blocked_issues || [];
    } catch (err) {
        console.error('Failed to fetch blocked issues:', err);
        blockedIssuesData = [];
    }

    renderBlockedList();
    blockedModal.classList.add('visible');
}

function closeBlockedModal(e) {
    if (!e || e.target === blockedModal) {
        blockedModal.classList.remove('visible');
    }
}

// Phase Info Modal
const phaseModal = document.getElementById('phaseModal');
let currentPhaseData = null;
let currentPhaseIssue = null;

async function openPhaseModal(issueNumber, flowStepKey) {
    currentPhaseIssue = issueNumber;
    try {
        const res = await fetch(`/api/dialog/phase/${issueNumber}?phase=${encodeURIComponent(flowStepKey)}`);
        const data = await res.json();

        if (data.error) {
            console.error('Failed to fetch phases:', data.error);
            return;
        }

        const phase = data.phase;

        if (!phase) {
            // No phases yet, show a simple message
            document.getElementById('phaseModalTitle').textContent = flowStepKey.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
            document.getElementById('phaseStatusIcon').textContent = '○';
            document.getElementById('phaseStatusIcon').className = 'phase-status-icon';
            document.getElementById('phaseStatusLabel').textContent = 'Not started';
            document.getElementById('phaseDuration').textContent = '-';
            document.getElementById('phaseAgent').textContent = '-';
            document.getElementById('phaseValidationRow').style.display = 'none';
            document.getElementById('phaseDetailsBtn').style.display = 'none';
            phaseModal.classList.add('visible');
            return;
        }

        currentPhaseData = phase;

        // Update modal content
        document.getElementById('phaseModalTitle').textContent = phase.display_name;

        const iconEl = document.getElementById('phaseStatusIcon');
        const labelEl = document.getElementById('phaseStatusLabel');

        iconEl.textContent = phase.status_icon;
        iconEl.className = 'phase-status-icon ' + getStatusClass(phase.status);
        labelEl.textContent = formatStatus(phase.status);

        // Duration
        const duration = calculateDuration(phase.started_at, phase.ended_at);
        document.getElementById('phaseDuration').textContent = duration || '-';

        // Agent
        document.getElementById('phaseAgent').textContent = phase.agent_label || '-';

        // Validation
        const validationRow = document.getElementById('phaseValidationRow');
        if (phase.validation_passed !== null && phase.validation_passed !== undefined) {
            validationRow.style.display = 'flex';
            document.getElementById('phaseValidation').textContent =
                phase.validation_passed ? 'Passed' : 'Failed';
            document.getElementById('phaseValidation').style.color =
                phase.validation_passed ? 'var(--ok)' : 'var(--danger)';
        } else {
            validationRow.style.display = 'none';
        }

        // Show Details button
        document.getElementById('phaseDetailsBtn').style.display = 'block';

        phaseModal.classList.add('visible');
    } catch (err) {
        console.error('Error fetching phase data:', err);
    }
}

function closePhaseModal(e) {
    if (!e || e.target === phaseModal) {
        phaseModal.classList.remove('visible');
        currentPhaseData = null;
    }
}

// The legacy ``#timelineModal`` teleport was retired with the uncontracted
// ``GET /api/timeline/{issue_number}`` route (#6421).  The issue-detail drawer
// is the single issue-timeline surface; it renders the same events/phase_toc/
// cycles from the contracted ``/api/issue-detail/{issue_number}`` payload.
const issueDetailDrawer = document.getElementById('issueDetailDrawer');
let issueDetailData = null;
let lastIssueDetailTrigger = null;
let journeyFilter = 'latest-run'; // 'latest-run' or 'all'
let timelineView = 'user'; // one of TIMELINE_VIEWS

// Mirrors the generated ``TimelineView`` wire enum (ui-contracts.d.ts), whose
// runtime owner on the server side is ``view_models/timeline_view.py``.
const TIMELINE_VIEWS = ['user', 'ops', 'debug', 'raw'];

// The broad semantic lens: every semantically retained event, including the
// Ops-only ones (``validation.completed``) and Debug-only ones
// (``issue.labels_changed``) that the default Story view hides.  Diagnostic
// entrypoints open the drawer with this lens so they keep showing what the
// retired ``GET /api/timeline/{issue_number}`` route — which applied no view
// filter at all — used to show (#6421).
const DIAGNOSTIC_TIMELINE_VIEW = 'debug';

// Single owner of the shared ``timelineView`` state.  Every writer (the view
// toggle and any view-scoped drawer open) goes through here, so the lens the
// drawer requests from the server and the lens its toggle reports as active
// cannot drift.  Unrecognised values are ignored, matching the server-side
// ``normalize_timeline_view`` coercion.
function applyTimelineView(view) {
    if (TIMELINE_VIEWS.includes(view)) {
        timelineView = view;
    }
    return timelineView;
}

function isIssueDetailDrawerOpen() {
    return Boolean(issueDetailDrawer) && issueDetailDrawer.classList.contains('visible');
}

// Single owner of the drawer's focus-return target.
//
// ``openIssueDetail`` is re-entrant: retiring the ``#timelineModal`` teleport
// (#6421) means the Diagnose affordance now reloads the *already-visible*
// drawer under the debug lens instead of opening a separate modal.  Capturing
// ``document.activeElement`` on every entry would therefore overwrite the real
// opener with whatever happened to be focused mid-session — for Diagnose the
// popover link is already detached by ``closeArtifactPopover`` when the handler
// runs, and for a plain reload it is the drawer's own close button.  Closing
// would then strand focus on a removed node or inside the hidden drawer
// instead of returning it to the card/timeline control the user came from.
//
// Rule: an explicit trigger always wins; the implicit ``document.activeElement``
// capture happens only on the closed -> open transition.  Call this BEFORE the
// drawer is marked visible.
function captureIssueDetailReturnFocus(triggerEl) {
    if (triggerEl) {
        lastIssueDetailTrigger = triggerEl;
    } else if (!isIssueDetailDrawerOpen()) {
        lastIssueDetailTrigger = document.activeElement;
    }
    return lastIssueDetailTrigger;
}

function restoreIssueDetailReturnFocus() {
    if (lastIssueDetailTrigger && typeof lastIssueDetailTrigger.focus === 'function') {
        lastIssueDetailTrigger.focus();
    }
    return lastIssueDetailTrigger;
}

