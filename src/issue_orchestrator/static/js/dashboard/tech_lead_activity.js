// Tech-lead activity panel — ADR-0033's local visibility surface (#6858).
//
// ``tech_lead_runs.js`` owns what the operator can DO; this owns what the tech
// lead has already DONE. It renders the typed ``techLeadActivity`` payload the
// dashboard view model publishes: one row per recorded run, with its scope, its
// phase, what it produced, and the subject it REFERENCED (never owned).
//
// Nothing is decided here. Phase words, tones, flavor names and the empty-state
// sentence all arrive from the server projection, so the browser cannot invent
// a second vocabulary for run state — the same rule the run-action affordances
// follow.
//
// Accessibility:
//
// * The panel is a native <details> disclosure with a real <summary>, so it is
//   keyboard reachable, has an accessible name, and reports expanded state
//   without any ARIA of our own.
// * Runs are a real <ul>/<li> list, so a screen reader announces how many there
//   are and where it is in them.
// * Phase is ALWAYS rendered as text (``phaseLabel``) next to its tone class —
//   colour is never the only signal.
// * Updates replace only the list's rows, so an operator's open panel and
//   keyboard focus survive live SSE-driven refreshes.

function techLeadActivityState() {
    return (window.dashboardData && window.dashboardData.techLeadActivity) || null;
}

// Pure: the runs to render, newest first (the server already ordered them).
function techLeadActivityEntries(activity) {
    return (activity && Array.isArray(activity.entries)) ? activity.entries : [];
}

// Pure: "#123 Some title", or '' when the run referenced no GitHub object.
// A whole-board review's subject is the board, and claiming an issue number it
// does not have would point the operator at the wrong thing.
function techLeadActivitySubjectText(entry) {
    const number = Number(entry.subjectIssueNumber) || 0;
    if (!number) {
        return '';
    }
    const title = entry.subjectTitle ? ` ${entry.subjectTitle}` : '';
    return `#${number}${title}`;
}

// Pure: "2 findings · 1 proposal", or '' when the run produced neither.
// Singular/plural is decided here rather than server-side because it is pure
// presentation of two numbers the projection already publishes.
function techLeadActivityProducedText(entry) {
    const parts = [];
    const findings = Number(entry.findings) || 0;
    const proposals = Number(entry.proposals) || 0;
    if (findings) {
        parts.push(`${findings} finding${findings === 1 ? '' : 's'}`);
    }
    if (proposals) {
        parts.push(`${proposals} proposal${proposals === 1 ? '' : 's'}`);
    }
    return parts.join(' · ');
}

// Pure: one <li> for a recorded run. Every interpolated value is escaped: the
// subject title and the decision summary are agent- and GitHub-authored text.
function renderTechLeadActivityRow(entry) {
    const cells = [
        `<span class="tla-flavor">${escapeHtml(entry.flavorLabel || '')}</span>`,
        `<span class="tla-phase tla-phase--${escapeAttr(entry.tone || 'muted')}">`
        + `${escapeHtml(entry.phaseLabel || '')}</span>`,
    ];
    const subject = techLeadActivitySubjectText(entry);
    if (subject) {
        cells.push(`<span class="tla-subject">${escapeHtml(subject)}</span>`);
    }
    const started = formatTimestamp(entry.startedAt, '');
    if (started) {
        cells.push(`<span class="tla-started">${escapeHtml(started)}</span>`);
    }
    const produced = techLeadActivityProducedText(entry);
    if (produced) {
        cells.push(`<span class="tla-produced">${escapeHtml(produced)}</span>`);
    }
    if (entry.detail) {
        cells.push(
            `<span class="tla-detail" title="${escapeAttr(entry.detail)}">`
            + `${escapeHtml(entry.detail)}</span>`,
        );
    }
    return `<li class="tla-row">${cells.join('')}</li>`;
}

// Pure: the whole list body — rows, or the server's empty-state sentence.
function techLeadActivityRowsHtml(activity) {
    const entries = techLeadActivityEntries(activity);
    if (!entries.length) {
        const message = (activity && activity.emptyMessage) || '';
        return `<li class="tla-row tla-row--empty">${escapeHtml(message)}</li>`;
    }
    return entries.map(renderTechLeadActivityRow).join('');
}

// Pure: the count shown beside the panel's name.
function techLeadActivityCountText(activity) {
    const count = techLeadActivityEntries(activity).length;
    return count ? String(count) : '';
}

// Surgically update the panel. The <details>/<summary> nodes are never
// replaced, so an operator's expanded panel and keyboard focus survive every
// live refresh; only the <ul> rows and the count change.
function updateTechLeadActivityPanel(activity) {
    const panel = document.getElementById('techLeadActivityPanel');
    // No payload yet is NOT an empty history: before the first view-model
    // arrives we have observed nothing, so the panel is left as rendered rather
    // than told there are no runs. Only a real projection writes rows.
    if (!panel || !activity) {
        return;
    }
    const list = document.getElementById('techLeadActivityList');
    if (list) {
        list.innerHTML = techLeadActivityRowsHtml(activity);
    }
    const count = document.getElementById('techLeadActivityCount');
    if (count) {
        count.textContent = techLeadActivityCountText(activity);
    }
}

// Convenience for the load/refresh hooks.
function renderTechLeadActivityFromDashboardData() {
    updateTechLeadActivityPanel(techLeadActivityState());
}
