const VIEWED_ISSUES_KEY = 'issue-orchestrator.blocked-viewed.v1';

function getViewedIssues() {
    try {
        return new Set(JSON.parse(localStorage.getItem(VIEWED_ISSUES_KEY) || '[]'));
    } catch { return new Set(); }
}

function setViewedIssues(issueNumbers) {
    localStorage.setItem(VIEWED_ISSUES_KEY, JSON.stringify([...issueNumbers]));
}

function markIssuesViewed(numbers) {
    const viewed = getViewedIssues();
    numbers.forEach(n => viewed.add(n));
    setViewedIssues(viewed);
}

function clearIssuesViewed(numbers) {
    const viewed = getViewedIssues();
    numbers.forEach(n => viewed.delete(n));
    setViewedIssues(viewed);
}

const OPTIMISTIC_REQUEUE_HIDE_MS = 30_000;
const optimisticRequeueSuppressions = new Map();

function _optimisticSuppressionKey(issueNumber, columnId) {
    return `${columnId}:${issueNumber}`;
}

function pruneOptimisticSuppressions() {
    const now = Date.now();
    for (const [key, expiresAt] of optimisticRequeueSuppressions.entries()) {
        if (expiresAt <= now) optimisticRequeueSuppressions.delete(key);
    }
}

function suppressIssueInColumn(issueNumber, columnId) {
    optimisticRequeueSuppressions.set(
        _optimisticSuppressionKey(issueNumber, columnId),
        Date.now() + OPTIMISTIC_REQUEUE_HIDE_MS,
    );
}

function isIssueSuppressedInColumn(issueNumber, columnId) {
    pruneOptimisticSuppressions();
    const expiresAt = optimisticRequeueSuppressions.get(_optimisticSuppressionKey(issueNumber, columnId));
    return Boolean(expiresAt && expiresAt > Date.now());
}

function filterSuppressedItems(items, columnId) {
    return (items || []).filter((item) => !isIssueSuppressedInColumn(Number(item.issue_number), columnId));
}

function hasPrClosedBlock(item) {
    const labels = Array.isArray(item?.orchestrator_labels) ? item.orchestrator_labels : [];
    return labels.some((label) => {
        const value = String(label || '');
        return value === 'blocked:pr-closed' || value.endsWith(':blocked:pr-closed');
    });
}

function ensureCompactEmptyState(cardsEl) {
    if (!cardsEl) return;
    const hasCards = cardsEl.querySelector('.issue-card');
    const emptyNode = cardsEl.querySelector('.column-empty');
    if (!hasCards && !emptyNode) {
        cardsEl.innerHTML = '<div class="column-empty">No items</div>';
    } else if (hasCards && emptyNode) {
        emptyNode.remove();
    }
}

function applyOptimisticRequeue(issueNumbers, sourceColumns) {
    const normalizedIssues = (issueNumbers || [])
        .map((n) => Number(n))
        .filter((n) => Number.isFinite(n));
    const normalizedColumns = (sourceColumns || [])
        .map((c) => String(c || '').trim())
        .filter(Boolean);
    if (!normalizedIssues.length || !normalizedColumns.length) return;

    for (const columnId of normalizedColumns) {
        const col = document.querySelector(`[data-column="${cssEscape(columnId)}"]`);
        if (!col) continue;

        let removedCount = 0;
        for (const issueNumber of normalizedIssues) {
            suppressIssueInColumn(issueNumber, columnId);

            const compactCard = col.querySelector(`.issue-card[data-issue="${issueNumber}"]`);
            if (compactCard) {
                compactCard.remove();
                removedCount += 1;
            }

            const expandedCard = col.querySelector(`.expanded-card[data-issue="${issueNumber}"]`);
            if (expandedCard) expandedCard.remove();
        }

        ensureCompactEmptyState(col.querySelector('.column-cards'));
        const countEl = col.querySelector('h2 .count');
        if (countEl && removedCount > 0) {
            const current = Number.parseInt(countEl.textContent || '0', 10);
            countEl.textContent = String(Math.max(0, (Number.isNaN(current) ? 0 : current) - removedCount));
        }

        if (columnId === 'blocked') {
            const items = getAllBlockedItems(col);
            updateBlockedNewCount(col, items, getViewedIssues());
            applyBlockedFilter(col);
        }
        updateBulkBar(columnId);
    }
}

// ── Kanban column expand/collapse ──

function buildCompactGithubLink(card) {
    const prUrl = String(card.pr_url || '');
    const githubUrl = String(card.github_url || card.issue_url || '');
    if (!githubUrl) return '';

    const isPrLink = Boolean(prUrl && githubUrl === prUrl);
    const label = String(card.github_label || (isPrLink ? 'PR ↗' : '↗'));
    const title = String(card.github_title || (isPrLink ? 'Open PR on GitHub' : 'Open issue on GitHub'));
    const ariaLabel = String(
        card.github_aria_label
        || (isPrLink
            ? `Open PR for issue #${card.issue_number} on GitHub`
            : `Open issue #${card.issue_number} on GitHub`),
    );
    const extraClass = isPrLink ? ' card-pr-link' : '';
    return `<a class="card-gh${extraClass}" href="${escapeAttr(githubUrl)}" target="_blank" rel="noopener noreferrer" title="${escapeAttr(title)}" aria-label="${escapeAttr(ariaLabel)}">${escapeHtml(label)}</a>`;
}

// Single source of truth for the phase-age portion of a compact card.
// Timestamps participate in the dashboard-wide [data-dashboard-timestamp]
// localizer (the same mechanism issue_row.html uses), so UTC→local
// conversion lives in exactly one place (formatDashboardTimestampElement).
// Relative labels ("5 min") have no timezone and render as-is.
function compactCardPhaseAgeInnerHtml(card) {
    if (!card.phase_age) return '';
    const raw = String(card.phase_age);
    if (card.time_is_timestamp) {
        return '<span aria-hidden="true"> &middot; </span>'
            + `<span data-dashboard-timestamp="${escapeAttr(raw)}" data-dashboard-timestamp-fallback="-">${escapeHtml(raw)}</span>`;
    }
    return ` &middot; ${escapeHtml(raw)}`;
}

// Compact stack-gate chip. Rendered from the server-precomputed card.stack_chip
// so the first-paint DOM (Jinja) and this client rebuild are byte-identical —
// the mode/tone/status/title logic lives once in the projection, not here. Status
// is conveyed by text ("blocked"/"stale"/"ready"), not colour alone; the icon is
// decorative (aria-hidden) and the visible text is the accessible name.
function renderStackChipHtml(card) {
    const chip = card && card.stack_chip;
    if (!chip) return '';
    return `<div class="card-line card-stack">`
        + `<span class="stack-chip stack-chip--${chip.tone}" title="${escapeAttr(String(chip.title || ''))}">`
        + `<span class="stack-chip-icon" aria-hidden="true">⛓</span>`
        + `<span class="stack-chip-mode">${escapeHtml(String(chip.mode_label || ''))}</span>`
        + `<span class="stack-chip-status">${escapeHtml(String(chip.status_text || ''))}</span>`
        + `</span></div>`;
}

// Provider-outage badge. Rendered from the server-precomputed
// card.provider_badge (view_models/issue_card_labels.py::provider_badge) so the
// first-paint DOM (Jinja), the compact card, and the expanded row all render
// the same markup from the same projection — the label semantics live once, in
// Python, and are prefix/config aware. Status is text ("Provider unavailable"),
// never colour alone; the icon is decorative and the visible text is the
// accessible name.
function renderProviderBadgeHtml(card) {
    const badge = card && card.provider_badge;
    if (!badge) return '';
    return `<div class="card-line card-provider">`
        + `<span class="provider-badge provider-badge--${badge.tone}" title="${escapeAttr(String(badge.title || ''))}">`
        + `<span class="provider-badge-icon" aria-hidden="true">⚡</span>`
        + `<span class="provider-badge-text">${escapeHtml(String(badge.label_text || ''))}</span>`
        + `</span></div>`;
}

function renderCompactCardHtml(card) {
    const n = card.issue_number;
    const cardId = String(card.card_id || `issue-${n}`);
    const staleAttr = card.is_stale ? 'true' : 'false';
    const showStaleBadge = Boolean(card.show_stale_badge);
    const showStaleBadgeAttr = showStaleBadge ? 'true' : 'false';
    const staleDot = showStaleBadge
        ? `<span class="stale-dot" title="${card.stale_reason || 'Issue may be stale'}" aria-label="Issue data may be stale"></span>`
        : '';
    const staleBadge = showStaleBadge
        ? '<span class="badge badge-stale" title="Data may be stale">stale</span>'
        : '';
    const ghLink = buildCompactGithubLink(card);
    const hasTerminal = card.state_label === 'running' ? 'true' : 'false';
    const action = card.state_label === 'running' ? 'focus' : 'open';
    const menuButton = `<button class="card-menu-btn"
        data-issue="${n}"
        data-title="${escapeAttr(String(card.title || ''))}"
        data-issue-url="${escapeAttr(String(card.issue_url || ''))}"
        data-pr-url="${escapeAttr(String(card.pr_url || ''))}"
        data-status="${escapeAttr(String(card.state_label || ''))}"
        data-row-action="${escapeAttr(String(action || ''))}"
        data-agent="${escapeAttr(String(card.agent_type || ''))}"
        data-run-dir="${escapeAttr(String(card.run_dir || ''))}"
        data-has-terminal="${hasTerminal}"
        data-orchestrator-labels="${escapeAttr(JSON.stringify(card.orchestrator_labels || []))}"
        onclick="openCompactCardActionsMenu(event, this)"
        title="More actions for issue #${n}"
        aria-label="More actions for issue #${n}">&#x22EE;</button>`;
    const phaseLine = card.phase || card.state_label || '';
    const phaseLineHtml = `<span class="card-phase-text">${escapeHtml(String(phaseLine))}</span><span class="card-phase-age">${compactCardPhaseAgeInnerHtml(card)}</span>`;
    const queueWaitLine = card.queue_wait_reason
        ? `<div class="card-line card-wait">${escapeHtml(String(card.queue_wait_reason))}</div>`
        : '';
    let detailLine = '';
    if (card.summary && !card.queue_wait_reason) {
        detailLine = `<div class="card-line card-muted">${escapeHtml(String(card.summary))}</div>`;
    }
    const orchLabels = card.orchestrator_labels || [];
    const orchPills = orchLabels.map(l => `<span class="badge badge-orch">${l}</span>`).join('');
    const allBadges = orchPills + staleBadge;
    const badgesDiv = allBadges
        ? `<div class="card-badges">${allBadges}</div>`
        : '';
    const issueLabel = card.issue_label || `#${n}`;
    const issueLabelHtml = escapeHtml(String(issueLabel));
    const issueLabelAttr = escapeAttr(String(issueLabel));
    return `<div class="issue-card" data-card-id="${cardId}" data-issue="${n}" data-stale="${staleAttr}" data-show-stale-badge="${showStaleBadgeAttr}" data-last-refresh-age-seconds="${card.last_refreshed_age_seconds || 0}">
        <div class="card-top">
            <button class="card-focus" onclick="openIssueDetail(${n}, this);event.stopPropagation();" title="Focus issue ${issueLabelAttr}">
                ${issueLabelHtml} ${escapeHtml(String(card.title || ''))}
            </button>
            <div class="card-head-actions">
                ${staleDot}
                <button class="card-refresh-btn" onclick="refreshIssueCard(${n}, this);event.stopPropagation();" title="Refresh issue #${n} from GitHub" aria-label="Refresh issue #${n}">&#x27F3;</button>
                ${ghLink}
                ${menuButton}
                <button class="card-timeline-btn" onclick="openIssueTimeline(${n}, this);event.stopPropagation();" title="Open timeline for issue #${n}" aria-label="Open timeline for issue #${n}">&#x1F9ED;</button>
            </div>
        </div>
        <div class="card-line">${phaseLineHtml}</div>
        ${queueWaitLine}
        ${detailLine}
        ${renderProviderBadgeHtml(card)}
        ${renderStackChipHtml(card)}
        ${badgesDiv}
    </div>`;
}

function syncCompactCardPhaseAge(node, card) {
    // phase_age is excluded from the fingerprint so that the relative-time
    // string ticking ("2s ago" → "5s ago") doesn't force the whole node to
    // be replaced on every refresh. Instead, sync it in place.
    const ageEl = node.querySelector('.card-phase-age');
    if (!ageEl) return;
    if (card.time_is_timestamp) {
        // Timestamps are owned by the shared [data-dashboard-timestamp]
        // localizer. Only touch the DOM when the underlying timestamp
        // actually changed — re-running the localizer on an unchanged value
        // would rewrite textContent and trip the MutationObserver flash the
        // fingerprint design exists to avoid.
        const marked = ageEl.querySelector('[data-dashboard-timestamp]');
        const raw = card.phase_age ? String(card.phase_age) : '';
        if (marked && marked.dataset.dashboardTimestamp === raw) return;
        ageEl.innerHTML = compactCardPhaseAgeInnerHtml(card);
        formatDashboardTimestamps(ageEl);
        return;
    }
    const desired = card.phase_age ? ' · ' + String(card.phase_age) : '';
    if (ageEl.textContent !== desired) {
        ageEl.textContent = desired;
    }
}

function renderCompactCards(container, items) {
    if (!items.length) {
        container.innerHTML = '<div class="column-empty">No items</div>';
        return;
    }

    // Remove "No items" placeholder and skeleton cards when real items exist
    container.querySelectorAll('.column-empty, .skeleton-card').forEach(el => el.remove());

    const nextIds = new Set(items.map((card) => String(card.card_id || `issue-${card.issue_number}`)));
    const existingCards = Array.from(container.querySelectorAll('.issue-card[data-card-id], .issue-card[data-issue]'));
    const existingById = new Map(existingCards.map((card) => [String(card.dataset.cardId || `issue-${card.dataset.issue || ''}`), card]));
    existingCards.forEach((card) => {
        const existingId = String(card.dataset.cardId || `issue-${card.dataset.issue || ''}`);
        if (!nextIds.has(existingId)) {
            card.remove();
        }
    });

    let insertAfter = null;
    for (const card of items) {
        const id = String(card.card_id || `issue-${card.issue_number}`);
        const existing = existingById.get(id) || null;
        const nextFingerprint = compactCardState.computeCompactCardFingerprint(card);
        let node = existing;

        if (!existing || existing.dataset.cardFingerprint !== nextFingerprint) {
            const wrapper = document.createElement('div');
            wrapper.innerHTML = renderCompactCardHtml(card).trim();
            const newNode = wrapper.firstElementChild;
            if (!newNode) continue;
            newNode.dataset.cardFingerprint = nextFingerprint;
            // Localize any timestamp phase-age on freshly built nodes. Reused
            // SSR/prior nodes are handled by syncCompactCardPhaseAge (and the
            // DOMContentLoaded document scan) so this never re-touches them.
            formatDashboardTimestamps(newNode);
            if (existing) {
                existing.replaceWith(newNode);
            } else if (insertAfter) {
                insertAfter.after(newNode);
            } else {
                container.prepend(newNode);
            }
            node = newNode;
        } else {
            // Fingerprint already matches (line 232 just checked) — do NOT
            // re-write the attribute. Even a same-value write fires a
            // MutationObserver and forces an attribute-selector style
            // invalidation, which on initial dashboard boot stacks 8 cards
            // × 3 view-model fetch cycles into a visible flash. Only the
            // phase-age string (excluded from the fingerprint by design)
            // needs an in-place sync.
            syncCompactCardPhaseAge(existing, card);
        }

        if (!node) continue;
        if (insertAfter) {
            if (node.previousElementSibling !== insertAfter) {
                insertAfter.after(node);
            }
        } else if (node.parentElement !== container || node !== container.firstElementChild) {
            container.prepend(node);
        }
        insertAfter = node;
    }
}

function syncColumnOverflowFooter(columnElement, column) {
    if (!columnElement) return;
    const footer = columnElement.querySelector('.column-overflow-footer');
    if (!footer) return;

    const hiddenCount = Number(column?.hidden_count);
    if (!Number.isInteger(hiddenCount) || hiddenCount < 0) {
        throw new Error(`Invalid hidden_count for dashboard column ${column?.id || 'unknown'}`);
    }

    footer.querySelector('.column-overflow-label').textContent = `${hiddenCount} more`;
    if (hiddenCount === 0 && document.activeElement === footer) {
        columnElement.querySelector('.column-expand-btn').focus();
    }
    footer.hidden = hiddenCount === 0;

    const title = String(column?.title || column?.id || 'column');
    const itemNoun = hiddenCount === 1 ? 'item' : 'items';
    footer.setAttribute(
        'aria-label',
        `Show full ${title} list, including ${hiddenCount} more ${itemNoun}`,
    );
}

const expandedColumnFingerprints = new Map();

function getSelectedIssueSet(columnId) {
    return new Set(getSelectedIssueNumbers(columnId));
}

function reapplyExpandedSelections(columnId, selectedIssues) {
    if (!selectedIssues || selectedIssues.size === 0) return;
    const col = document.querySelector(`[data-column="${columnId}"]`);
    if (!col) return;
    col.querySelectorAll('.expanded-card').forEach(card => {
        const issueNumber = Number(card.dataset.issue);
        const checkbox = card.querySelector('.card-checkbox');
        if (!checkbox || isNaN(issueNumber)) return;
        checkbox.checked = selectedIssues.has(issueNumber);
    });
}

function syncColumnExpansionAccessibility(column, isExpanded) {
    const title = String(column.dataset.columnTitle || column.dataset.column || 'column');
    const expandButton = column.querySelector('.column-expand-btn');
    if (expandButton) {
        expandButton.setAttribute('aria-expanded', String(isExpanded));
        expandButton.setAttribute('aria-label', `${isExpanded ? 'Collapse' : 'Expand'} ${title}`);
    }
    const overflowFooter = column.querySelector('.column-overflow-footer');
    if (overflowFooter) {
        overflowFooter.setAttribute('aria-expanded', String(isExpanded));
    }
    const expandedRegion = column.querySelector('.column-expanded');
    if (expandedRegion) {
        expandedRegion.setAttribute('aria-hidden', String(!isExpanded));
    }
}

function expandColumnFromOverflow(button) {
    const column = button?.closest('.kanban-column');
    const columnId = column?.dataset?.column;
    if (!columnId) return;
    toggleColumnExpand(columnId);
    column.querySelector('.column-expand-btn')?.focus();
}

function toggleColumnExpand(columnId) {
    const col = document.querySelector(`[data-column="${columnId}"]`);
    if (!col) return;
    const isExpanded = col.dataset.expanded === 'true';

    // Collapse all first — also clear stale checkbox/bulk state
    document.querySelectorAll('.kanban-column').forEach(c => {
        c.classList.remove('expanded', 'collapsed-peer');
        c.dataset.expanded = 'false';
        syncColumnExpansionAccessibility(c, false);
        const expanded = c.querySelector('.column-expanded');
        const cards = c.querySelector('.column-cards');
        if (expanded) expanded.style.display = 'none';
        if (cards) cards.style.display = '';
        // Reset checkboxes and bulk bar so stale state doesn't flash on re-expand
        c.querySelectorAll('.card-checkbox:checked').forEach(cb => { cb.checked = false; });
        const bar = c.querySelector('.bulk-action-bar');
        if (bar) {
            bar.style.display = 'none';
            const countEl = bar.querySelector('.selected-count');
            if (countEl) countEl.textContent = '0 selected';
            if (c.dataset.column === 'blocked') {
                bar.querySelectorAll('.issue-action-btn').forEach((btn) => {
                    btn.disabled = true;
                });
            }
        }
    });

    if (!isExpanded) {
        col.classList.add('expanded');
        col.dataset.expanded = 'true';
        syncColumnExpansionAccessibility(col, true);
        const expanded = col.querySelector('.column-expanded');
        const cards = col.querySelector('.column-cards');
        if (expanded) expanded.style.display = '';
        if (cards) cards.style.display = 'none';
        // Collapse peers
        document.querySelectorAll('.kanban-column:not(.expanded)').forEach(c => {
            c.classList.add('collapsed-peer');
        });
        updateBulkBar(columnId);
        loadExpandedColumn(columnId, { forceRebuild: true });
    }

    document.body.classList.toggle('column-focus-mode', !isExpanded);
    updateEmbeddedBackButtonVisibility();
}

// One expanded (list-view) row. Extracted from loadExpandedColumn so the
// compact and expanded row forms can both be asserted directly, and so the
// provider badge cannot be added to one form and forgotten in the other.
function renderExpandedCardHtml(item, columnId, isViewed) {
    const n = item.issue_number;
    const isPrClosedBlock = columnId === 'blocked' && hasPrClosedBlock(item);
    const orchLabels = item.orchestrator_labels || [];
    const orchPills = orchLabels.map((label) => `<span class="badge badge-orch">${label}</span>`).join('');
    const badgesDiv = orchPills
        ? `<div class="card-badges">${orchPills}</div>`
        : '';
    const queueWaitReason = item.queue_wait_reason || '';
    const detailText = queueWaitReason || item.detail_label || item.status || '';
    const detailClass = queueWaitReason ? 'card-line card-wait' : 'card-line card-muted';
    const detailDiv = detailText
        ? `<div class="${detailClass}">${escapeHtml(String(detailText))}</div>`
        : '';
    const issueLink = item.issue_url
        ? `<a class="card-gh card-issue-link" href="${escapeAttr(String(item.issue_url))}" target="_blank" rel="noopener noreferrer" title="Open issue #${n} on GitHub" aria-label="Open issue #${n} on GitHub">↗</a>`
        : '';
    const prLink = item.pr_url
        ? `<a class="card-action-btn card-pr-link" href="${escapeAttr(String(item.pr_url))}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation();" title="Open PR for issue #${n} on GitHub" aria-label="Open PR for issue #${n} on GitHub">PR ↗</a>`
        : '';
    const itemLabel = item.issue_label || `#${n}`;
    const itemLabelHtml = escapeHtml(String(itemLabel));
    const itemLabelAttr = escapeAttr(String(itemLabel));
    return `
                <div class="expanded-card${isViewed ? ' viewed' : ''}" data-issue="${n}" data-viewed="${isViewed}">
                    <input type="checkbox" class="card-checkbox" onchange="updateBulkBar('${columnId}')">
                    <div class="card-content">
                        <button class="card-focus" onclick="openIssueDetail(${n}, this);event.stopPropagation();"
                                title="Focus issue ${itemLabelAttr}">
                            ${itemLabelHtml} ${escapeHtml(String(item.title || ''))}
                        </button>
                        ${detailDiv}
                        ${renderProviderBadgeHtml(item)}
                        ${badgesDiv}
                    </div>
                    <div class="card-actions">
                        ${columnId === 'blocked' && isPrClosedBlock ? `<button class="card-action-btn card-action-unblock" onclick="retryPrClosedSingle(${n}, this);event.stopPropagation();" title="Remove stale PR labels and requeue issue #${n}">Retry</button>` : ''}
                        ${columnId === 'blocked' && isPrClosedBlock ? `<button class="card-action-btn card-action-reset" onclick="closePrClosedIssue(${n}, this);event.stopPropagation();" title="Close issue #${n}">Close Issue</button>` : ''}
                        ${columnId === 'blocked' && !isPrClosedBlock ? `<button class="card-action-btn card-action-unblock" onclick="unblockSingle(${n}, this);event.stopPropagation();" title="Unblock issue #${n}">Unblock</button>` : ''}
                        ${columnId === 'blocked' && !isPrClosedBlock ? `<button class="card-action-btn card-action-reset" onclick="resetRetrySingle(${n}, this);event.stopPropagation();" title="Full reset and requeue issue #${n}">Reset & Retry</button>` : ''}
                        ${columnId === 'blocked' && !isPrClosedBlock ? `<button class="card-action-btn card-action-reset" onclick="resetRetrySingleFromScratch(${n}, this);event.stopPropagation();" title="Full reset and requeue issue #${n} from a fresh branch based on main">Reset & Retry From Scratch</button>` : ''}
                        ${columnId === 'running' ? `<button class="card-action-btn card-action-reset" onclick="killExpandedSingle(${n}, this);event.stopPropagation();" title="Terminate issue #${n} and place on hold">Cancel</button>` : ''}
                        ${columnId === 'queued' ? `<button class="card-action-btn card-action-reset" onclick="cancelQueuedSingle(${n}, this);event.stopPropagation();" title="Place queued issue #${n} on hold">Cancel</button>` : ''}
                        ${columnId === 'awaiting-merge' ? `<button class="card-action-btn card-action-unblock" onclick="retryExpandedSingle(${n}, 'awaiting-merge', this);event.stopPropagation();" title="Remove pr-pending and requeue issue #${n}">Retry</button>` : ''}
                        ${columnId === 'awaiting-merge' ? `<button class="card-action-btn card-action-reset" onclick="resetRetrySingle(${n}, this);event.stopPropagation();" title="Full reset and requeue issue #${n}">Reset & Retry</button>` : ''}
                        ${columnId === 'awaiting-merge' ? `<button class="card-action-btn card-action-reset" onclick="resetRetrySingleFromScratch(${n}, this);event.stopPropagation();" title="Full reset and requeue issue #${n} from a fresh branch based on main">Reset & Retry From Scratch</button>` : ''}
                        ${columnId === 'completed' ? `<button class="card-action-btn card-action-unblock" onclick="retryExpandedSingle(${n}, 'completed', this);event.stopPropagation();" title="Requeue issue #${n} for another run">Retry</button>` : ''}
                        ${issueLink}
                        ${prLink}
                        <button class="card-timeline-btn" onclick="openIssueTimeline(${n}, this);event.stopPropagation();" title="Open timeline for issue #${n}" aria-label="Open timeline for issue #${n}">&#x1F9ED;</button>
                    </div>
                </div>`;
}

async function loadExpandedColumn(columnId, options = {}) {
    const forceRebuild = Boolean(options.forceRebuild);
    let vm = options.viewModel || null;
    const col = document.querySelector(`[data-column="${columnId}"]`);
    if (!col) return;
    const expandedList = col.querySelector('.expanded-cards-list');
    if (!expandedList) return;
    const previousSelection = getSelectedIssueSet(columnId);

    try {
        if (!vm) {
            const resp = await fetch(`/api/view-model?tab=${columnId}`);
            if (!resp.ok) return;
            vm = await resp.json();
        }
        const items = filterSuppressedItems(expandedColumnState.getExpandedItemsFromViewModel(vm, columnId), columnId);
        const nextFingerprint = expandedColumnState.computeExpandedItemsFingerprint(items, {
            columnId,
            viewedIssueNumbers: columnId === 'blocked' ? [...getViewedIssues()] : [],
        });
        const prevFingerprint = expandedColumnFingerprints.get(columnId);
        const shouldRebuild = forceRebuild || prevFingerprint !== nextFingerprint;

        if (shouldRebuild) {
            const viewed = columnId === 'blocked' ? getViewedIssues() : new Set();
            expandedList.innerHTML = items
                .map(item => renderExpandedCardHtml(item, columnId, viewed.has(item.issue_number)))
                .join('');
            expandedColumnFingerprints.set(columnId, nextFingerprint);
            const reconciledSelection = new Set(
                expandedColumnState.reconcileSelectedIssues([...previousSelection], items),
            );
            reapplyExpandedSelections(columnId, reconciledSelection);
        }

        // Update blocked-only derived UI even when list body is unchanged.
        if (columnId === 'blocked') {
            updateBlockedNewCount(col, items, getViewedIssues());
            applyBlockedFilter(col);
        }
        updateBulkBar(columnId);
    } catch (e) {
        console.error('Failed to load expanded column:', e);
        expandedList.innerHTML = '<div class="column-empty">Failed to load items</div>';
        expandedColumnFingerprints.delete(columnId);
    }
}

function updateBlockedNewCount(col, items, viewed) {
    const newCount = items.filter(item => !viewed.has(item.issue_number)).length;
    let badge = col.querySelector('.new-count-badge');
    if (newCount > 0) {
        if (!badge) {
            badge = document.createElement('span');
            badge.className = 'new-count-badge';
            const h2 = col.querySelector('h2');
            if (h2) h2.appendChild(badge);
        }
        badge.textContent = `${newCount} new`;
        badge.title = `${newCount} blocked issue${newCount === 1 ? '' : 's'} not yet viewed`;
    } else if (badge) {
        badge.remove();
    }
}

function applyBlockedFilter(col) {
    if (!col) col = document.querySelector('[data-column="blocked"]');
    if (!col) return;
    const activeBtn = col.querySelector('.filter-btn.active');
    const filter = activeBtn ? activeBtn.dataset.filter : 'all';
    const cards = col.querySelectorAll('.expanded-card');
    cards.forEach(card => {
        const isViewed = card.dataset.viewed === 'true';
        if (filter === 'all') card.style.display = '';
        else if (filter === 'new') card.style.display = isViewed ? 'none' : '';
        else if (filter === 'viewed') card.style.display = isViewed ? '' : 'none';
    });
}

function filterBlockedColumn(filter, btn) {
    const col = document.querySelector('[data-column="blocked"]');
    if (!col) return;
    col.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    applyBlockedFilter(col);
}

function updateBulkBar(columnId) {
    const col = document.querySelector(`[data-column="${columnId}"]`);
    if (!col) return;
    const checked = col.querySelectorAll('.card-checkbox:checked');
    const bar = col.querySelector('.bulk-action-bar');
    if (!bar) return;
    const alwaysVisibleColumns = new Set(['blocked', 'awaiting-merge', 'completed', 'running']);
    const alwaysVisible = alwaysVisibleColumns.has(columnId);
    bar.style.display = alwaysVisible || checked.length > 0 ? 'flex' : 'none';
    const countEl = bar.querySelector('.selected-count');
    if (countEl) {
        countEl.textContent = checked.length > 0 ? `${checked.length} selected` : 'No issues selected';
    }
    const actionButtons = bar.querySelectorAll('.issue-action-btn');
    actionButtons.forEach((btn) => {
        const requiresSelection = btn.dataset.requiresSelection !== 'false';
        btn.disabled = requiresSelection && checked.length === 0;
    });
}

async function killExpandedSingle(issueNumber, btn) {
    const confirmMsg = `Cancel running issue #${issueNumber}?\n\nThis will terminate the active session and place the issue on hold.\nIt will not run again until you explicitly retry/unblock it.`;
    if (!await showConfirm(confirmMsg, btn)) return;
    if (btn) btn.disabled = true;
    try {
        const res = await fetch(`/api/kill/${issueNumber}`, { method: 'POST' });
        const data = await res.json().catch(() => ({}));
        if (res.ok && data.status === 'terminated') {
            showToast(`Cancelled #${issueNumber} (on hold)`);
            await refreshViewModel();
        } else {
            showToast(data.error || `Cancel failed (${res.status})`, true);
            if (btn) btn.disabled = false;
        }
    } catch (e) {
        console.error('Cancel failed:', e);
        showToast('Cancel failed: network error', true);
        if (btn) btn.disabled = false;
    }
}

async function bulkKillRunning() {
    const numbers = getSelectedIssueNumbers('running');
    if (!numbers.length) return;
    const confirmMsg = `Cancel ${numbers.length} running issue(s)?\n\nThis will terminate active sessions and place issues on hold.\nThey will not run again until explicitly retried/unblocked.`;
    if (!await showConfirm(confirmMsg)) return;
    try {
        const res = await fetch('/api/bulk-kill', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ issue_numbers: numbers }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok) {
            const terminated = Array.isArray(data.terminated) ? data.terminated.length : 0;
            const failed = Array.isArray(data.failed) ? data.failed.length : 0;
            if (terminated > 0) showToast(`Cancelled ${terminated} running issue(s)`);
            if (failed > 0) showToast(`Failed to cancel ${failed} issue(s)`, true);
            await refreshViewModel();
            return;
        }
        showToast(data.error || `Bulk cancel failed (${res.status})`, true);
    } catch (e) {
        console.error('Bulk cancel failed:', e);
        showToast('Bulk cancel failed: network error', true);
    }
}

async function bulkKillAllRunning() {
    const col = document.querySelector('[data-column="running"]');
    if (!col) return;
    const allNumbers = Array.from(col.querySelectorAll('.expanded-card'))
        .map((card) => Number(card.dataset.issue))
        .filter((n) => Number.isInteger(n));
    if (!allNumbers.length) {
        showToast('No running issues to cancel');
        return;
    }
    const confirmMsg = `Cancel ALL ${allNumbers.length} running issue(s)?\n\nThis will terminate active sessions and place issues on hold.\nThey will not run again until explicitly retried/unblocked.`;
    if (!await showConfirm(confirmMsg)) return;
    try {
        const res = await fetch('/api/bulk-kill', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ issue_numbers: allNumbers }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok) {
            const terminated = Array.isArray(data.terminated) ? data.terminated.length : 0;
            const failed = Array.isArray(data.failed) ? data.failed.length : 0;
            if (terminated > 0) showToast(`Cancelled ${terminated} running issue(s)`);
            if (failed > 0) showToast(`Failed to cancel ${failed} issue(s)`, true);
            await refreshViewModel();
            return;
        }
        showToast(data.error || `Cancel all failed (${res.status})`, true);
    } catch (e) {
        console.error('Cancel all failed:', e);
        showToast('Cancel all failed: network error', true);
    }
}

async function cancelQueuedSingle(issueNumber, btn) {
    const confirmMsg = `Cancel queued issue #${issueNumber}?\n\nThis will place the issue on hold before it launches.\nIt will not run again until you explicitly retry/unblock it.`;
    if (!await showConfirm(confirmMsg, btn)) return;
    if (btn) btn.disabled = true;
    try {
        const req = uiActionContract.buildBulkCancelQueuedRequest([issueNumber]);
        const res = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok && Array.isArray(data.cancelled) && data.cancelled.includes(issueNumber)) {
            showToast(`Cancelled queued issue #${issueNumber}`);
            await refreshViewModel();
            return;
        }
        showToast(data.error || `Queue cancel failed (${res.status})`, true);
        if (btn) btn.disabled = false;
    } catch (e) {
        console.error('Queue cancel failed:', e);
        showToast('Queue cancel failed: network error', true);
        if (btn) btn.disabled = false;
    }
}

async function bulkRefreshRunning() {
    const numbers = getSelectedIssueNumbers('running');
    if (!numbers.length) return;
    const confirmMsg = `Refresh ${numbers.length} running issue(s) from GitHub now?`;
    if (!await showConfirm(confirmMsg)) return;
    const failures = [];
    for (const issueNumber of numbers) {
        try {
            const res = await fetch(`/api/issues/${issueNumber}/refresh`, { method: 'POST' });
            if (!res.ok) failures.push(issueNumber);
        } catch (_) {
            failures.push(issueNumber);
        }
    }
    if (failures.length === 0) {
        showToast(`Refreshed ${numbers.length} running issue(s)`);
    } else {
        showToast(`Refresh failed for ${failures.length} issue(s)`, true);
    }
    await refreshViewModel();
}

function getSelectedIssueNumbers(columnId) {
    const col = document.querySelector(`[data-column="${columnId}"]`);
    if (!col) return [];
    return Array.from(col.querySelectorAll('.expanded-card'))
        .filter(card => card.querySelector('.card-checkbox:checked'))
        .map(card => Number(card.dataset.issue))
        .filter(n => !isNaN(n));
}

async function unblockSingle(issueNumber, btn) {
    const confirmMsg = `Requeue issue #${issueNumber}?\n\nThis will REMOVE retry-gating labels (including blocking labels and pr-pending).\n\nIt will not delete the local worktree or remote branch.`;
    if (!await showConfirm(confirmMsg, btn || lastContextMenuPoint)) return;
    if (btn) btn.disabled = true;
    try {
        const req = uiActionContract.buildUnblockRequest([issueNumber]);
        const resp = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        if (resp.ok) {
            applyOptimisticRequeue([issueNumber], ['blocked']);
            showToast(`Unblocked #${issueNumber} → Queued`);
            await refreshViewModel();
        } else {
            const data = await resp.json().catch(() => ({}));
            showToast(data.error || `Unblock failed (${resp.status})`, true);
            if (btn) btn.disabled = false;
        }
    } catch (e) {
        console.error('Unblock failed:', e);
        showToast('Unblock failed: network error', true);
        if (btn) btn.disabled = false;
    }
}

async function retryPrClosedSingle(issueNumber, btn) {
    const confirmMsg = `Retry issue #${issueNumber}?\n\nThis will REMOVE stale PR labels and requeue the issue.\n\nIt will not delete the local worktree or remote branch.`;
    if (!await showConfirm(confirmMsg, btn || lastContextMenuPoint)) return;
    if (btn) btn.disabled = true;
    try {
        const req = uiActionContract.buildUnblockRequest([issueNumber]);
        const resp = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        if (resp.ok) {
            applyOptimisticRequeue([issueNumber], ['blocked']);
            showToast(`Retrying #${issueNumber}`);
            await refreshViewModel();
        } else {
            const data = await resp.json().catch(() => ({}));
            showToast(data.error || `Retry failed (${resp.status})`, true);
            if (btn) btn.disabled = false;
        }
    } catch (e) {
        console.error('Retry failed:', e);
        showToast('Retry failed: network error', true);
        if (btn) btn.disabled = false;
    }
}

async function closePrClosedIssue(issueNumber, btn) {
    const confirmMsg = `Close issue #${issueNumber}?\n\nUse this when the linked PR was closed or is gone and no more work is needed.`;
    if (!await showConfirm(confirmMsg, btn || lastContextMenuPoint)) return;
    if (btn) btn.disabled = true;
    try {
        const req = uiActionContract.buildCloseIssueRequest(issueNumber);
        const resp = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.success) {
            applyOptimisticRequeue([issueNumber], ['blocked']);
            showToast(`Closed issue #${issueNumber}`);
            await refreshViewModel();
        } else {
            showToast(data.error || `Close failed (${resp.status})`, true);
            if (btn) btn.disabled = false;
        }
    } catch (e) {
        console.error('Close issue failed:', e);
        showToast('Close failed: network error', true);
        if (btn) btn.disabled = false;
    }
}

async function bulkUnblock() {
    const numbers = getSelectedIssueNumbers('blocked');
    if (!numbers.length) return;
    const confirmMsg = `Requeue ${numbers.length} issue(s)?\n\nThis will REMOVE retry-gating labels (including blocking labels and pr-pending).\n\nIt will not delete local worktrees or remote branches.`;
    if (!await showConfirm(confirmMsg)) return;
    try {
        const req = uiActionContract.buildUnblockRequest(numbers);
        const resp = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        if (resp.ok) {
            applyOptimisticRequeue(numbers, ['blocked']);
            showToast(`Unblocking ${numbers.length} issue(s) → Queued`);
            await refreshViewModel();
        } else {
            const data = await resp.json().catch(() => ({}));
            showToast(data.error || `Bulk unblock failed (${resp.status})`, true);
        }
    } catch (e) {
        console.error('Bulk unblock failed:', e);
        showToast('Bulk unblock failed: network error', true);
    }
}

async function bulkResetRetry() {
    const numbers = getSelectedIssueNumbers('blocked').concat(getSelectedIssueNumbers('awaiting-merge'));
    if (!numbers.length) return;
    const confirmMsg = `Full reset and requeue ${numbers.length} issue(s)?\n\nThis will DELETE:\n• Local worktrees\n• Remote branches\n• Orchestrator labels\n\nAfter reset, the issues will be requeued for a fresh retry.`;
    if (!await showConfirm(confirmMsg)) return;
    try {
        const req = uiActionContract.buildResetRetryRequest(numbers, { fromScratch: false });
        const res = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok && data.reset && data.reset.length > 0) {
            applyOptimisticRequeue(data.reset, ['blocked', 'awaiting-merge']);
            showToast(`Reset ${data.reset.length} issue(s) → Queued`);
            await refreshViewModel();
        } else if (res.ok && data.failed && data.failed.length > 0) {
            showToast(`Failed to reset some issues: ${data.failed.map((f) => f.error).join(', ')}`, true);
        } else {
            showToast(data.error || `Reset failed (${res.status})`, true);
        }
    } catch (e) {
        console.error('Bulk reset failed:', e);
        showToast('Bulk reset failed: network error', true);
    }
}

async function bulkResetRetryFromScratch() {
    const numbers = getSelectedIssueNumbers('blocked').concat(getSelectedIssueNumbers('awaiting-merge'));
    if (!numbers.length) return;
    const confirmMsg = `Full reset and requeue ${numbers.length} issue(s) from scratch?\n\nThis will DELETE:\n• Local worktrees\n• Remote branches\n• Orchestrator labels\n\nThis will also supersede open orchestrator PRs by commenting and closing them.\n\nPrior review approvals and validation artifacts will not be reused. Next launch will force NEW branches from base (main), not prior issue branch history.`;
    if (!await showConfirm(confirmMsg)) return;
    try {
        const req = uiActionContract.buildResetRetryRequest(numbers, { fromScratch: true });
        const res = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok && data.reset && data.reset.length > 0) {
            applyOptimisticRequeue(data.reset, ['blocked', 'awaiting-merge']);
            showToast(`Reset ${data.reset.length} issue(s) from scratch → Queued`);
            await refreshViewModel();
        } else if (res.ok && data.failed && data.failed.length > 0) {
            showToast(`Failed to reset some issues: ${data.failed.map((f) => f.error).join(', ')}`, true);
        } else {
            showToast(data.error || `Reset failed (${res.status})`, true);
        }
    } catch (e) {
        console.error('Bulk reset from scratch failed:', e);
        showToast('Bulk reset from scratch failed: network error', true);
    }
}

async function resetRetrySingle(issueNumber, btn) {
    const confirmMsg = `Full reset and requeue issue #${issueNumber}?\n\nThis will DELETE:\n• Local worktree\n• Remote branch\n• Orchestrator labels\n\nAfter reset, the issue will be requeued for a fresh retry.`;
    if (!await showConfirm(confirmMsg, btn || lastContextMenuPoint)) return;
    await performResetRetry(issueNumber, btn, { fromScratch: false });
}

async function resetRetrySingleFromScratch(issueNumber, btn) {
    const confirmMsg = `Full reset and requeue issue #${issueNumber} from scratch?\n\nThis will DELETE:\n• Local worktree\n• Remote branch\n• Orchestrator labels\n\nThis will also supersede open orchestrator PRs by commenting and closing them.\n\nPrior review approvals and validation artifacts will not be reused. Next launch will force a NEW branch from base (main), not prior issue branch history.`;
    if (!await showConfirm(confirmMsg, btn || lastContextMenuPoint)) return;
    await performResetRetry(issueNumber, btn, { fromScratch: true });
}

async function performResetRetry(issueNumber, btn, options = {}) {
    if (btn) btn.disabled = true;
    try {
        const fromScratch = Boolean(options.fromScratch);
        const req = uiActionContract.buildResetRetryRequest([issueNumber], { fromScratch });
        const res = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok && data.reset && data.reset.length > 0) {
            applyOptimisticRequeue(data.reset, ['blocked']);
            showToast(fromScratch ? `Reset #${issueNumber} from scratch → Queued` : `Reset #${issueNumber} → Queued`);
            await refreshViewModel();
        } else if (res.ok && data.failed && data.failed.length > 0) {
            showToast(`Reset failed: ${data.failed.map((f) => f.error).join(', ')}`, true);
            if (btn) btn.disabled = false;
        } else {
            showToast(data.error || `Reset failed (${res.status})`, true);
            if (btn) btn.disabled = false;
        }
    } catch (e) {
        console.error('Single reset failed:', e);
        showToast('Reset failed: network error', true);
        if (btn) btn.disabled = false;
    }
}

function bulkMarkViewed() {
    const numbers = getSelectedIssueNumbers('blocked');
    if (!numbers.length) return;
    markIssuesViewed(numbers);
    // Update card visuals immediately
    const col = document.querySelector('[data-column="blocked"]');
    if (col) {
        numbers.forEach(n => {
            const card = col.querySelector(`.expanded-card[data-issue="${n}"]`);
            if (card) { card.classList.add('viewed'); card.dataset.viewed = 'true'; }
        });
        updateBlockedNewCount(col, getAllBlockedItems(col), getViewedIssues());
        applyBlockedFilter(col);
    }
    // Deselect checkboxes
    uncheckAll('blocked');
    showToast(`Marked ${numbers.length} issue(s) as viewed`);
}

function bulkClearViewed() {
    const numbers = getSelectedIssueNumbers('blocked');
    if (!numbers.length) return;
    clearIssuesViewed(numbers);
    const col = document.querySelector('[data-column="blocked"]');
    if (col) {
        numbers.forEach(n => {
            const card = col.querySelector(`.expanded-card[data-issue="${n}"]`);
            if (card) { card.classList.remove('viewed'); card.dataset.viewed = 'false'; }
        });
        updateBlockedNewCount(col, getAllBlockedItems(col), getViewedIssues());
        applyBlockedFilter(col);
    }
    uncheckAll('blocked');
    showToast(`Cleared viewed status for ${numbers.length} issue(s)`);
}

function getAllBlockedItems(col) {
    return Array.from(col.querySelectorAll('.expanded-card'))
        .map(card => ({ issue_number: Number(card.dataset.issue) }));
}

function uncheckAll(columnId) {
    const col = document.querySelector(`[data-column="${columnId}"]`);
    if (!col) return;
    col.querySelectorAll('.card-checkbox:checked').forEach(cb => { cb.checked = false; });
    updateBulkBar(columnId);
}

function bulkOpenPRs() {
    const col = document.querySelector('[data-column="awaiting-merge"]');
    if (!col) return;
    const cards = Array.from(col.querySelectorAll('.expanded-card'))
        .filter(card => card.querySelector('.card-checkbox:checked'));
    cards.forEach(card => {
        const link = card.querySelector('.card-pr-link');
        if (link && link.href) window.open(link.href, '_blank');
    });
}

async function retryExpandedSingle(issueNumber, columnId, btn) {
    if (columnId === 'awaiting-merge') {
        const confirmMsg = `Retry issue #${issueNumber} from Awaiting Merge?\n\nThis will REMOVE pr-pending and requeue the issue for another run.\n\nUse this when you want new work despite an existing PR state.`;
        if (!await showConfirm(confirmMsg, btn)) return;
        if (btn) btn.disabled = true;
        try {
            const req = uiActionContract.buildUnblockRequest([issueNumber]);
            const resp = await fetch(req.endpoint, {
                method: req.method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(req.body),
            });
            if (resp.ok) {
                applyOptimisticRequeue([issueNumber], ['awaiting-merge']);
                showToast(`Retrying #${issueNumber} from Awaiting Merge`);
                await refreshViewModel();
            } else {
                const data = await resp.json().catch(() => ({}));
                showToast(data.error || `Retry failed (${resp.status})`, true);
                if (btn) btn.disabled = false;
            }
        } catch (e) {
            console.error('Retry failed:', e);
            showToast('Retry failed: network error', true);
            if (btn) btn.disabled = false;
        }
        return;
    }

    const confirmMsg = `Retry completed issue #${issueNumber}?\n\nThis will requeue the issue for another run.\nUse this when you want the agent to re-run with newer context.`;
    if (!await showConfirm(confirmMsg, btn)) return;
    if (btn) btn.disabled = true;
    try {
        const req = uiActionContract.buildBulkRetryRequest([issueNumber]);
        const resp = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.retried && data.retried.length > 0) {
            applyOptimisticRequeue(data.retried, ['completed']);
            showToast(`Retrying completed issue #${issueNumber}`);
            await refreshViewModel();
        } else {
            showToast(data.error || `Retry failed (${resp.status})`, true);
            if (btn) btn.disabled = false;
        }
    } catch (e) {
        console.error('Retry failed:', e);
        showToast('Retry failed: network error', true);
        if (btn) btn.disabled = false;
    }
}

async function bulkRetryAwaitingMerge() {
    const numbers = getSelectedIssueNumbers('awaiting-merge');
    if (!numbers.length) return;
    const confirmMsg = `Retry ${numbers.length} Awaiting Merge issue(s)?\n\nThis will REMOVE pr-pending and requeue selected issues for another run.\n\nUse this when you intentionally want a new run before merge.`;
    if (!await showConfirm(confirmMsg)) return;
    try {
        const req = uiActionContract.buildUnblockRequest(numbers);
        const resp = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        if (resp.ok) {
            applyOptimisticRequeue(numbers, ['awaiting-merge']);
            showToast(`Retrying ${numbers.length} Awaiting Merge issue(s)`);
            await refreshViewModel();
        } else {
            const data = await resp.json().catch(() => ({}));
            showToast(data.error || `Retry failed (${resp.status})`, true);
        }
    } catch (e) {
        console.error('Bulk retry failed:', e);
        showToast('Bulk retry failed: network error', true);
    }
}

async function bulkRetryCompleted() {
    const numbers = getSelectedIssueNumbers('completed');
    if (!numbers.length) return;
    const confirmMsg = `Retry ${numbers.length} completed issue(s)?\n\nThis will requeue selected issues for another run.\nUse this when you want re-execution with newer context or codebase changes.`;
    if (!await showConfirm(confirmMsg)) return;
    try {
        const req = uiActionContract.buildBulkRetryRequest(numbers);
        const resp = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.retried && data.retried.length > 0) {
            applyOptimisticRequeue(data.retried, ['completed']);
            showToast(`Retrying ${data.retried.length} completed issue(s)`);
            await refreshViewModel();
        } else {
            showToast(data.error || `Retry failed (${resp.status})`, true);
        }
    } catch (e) {
        console.error('Bulk retry failed:', e);
        showToast('Bulk retry failed: network error', true);
    }
}

async function bulkDeprioritize() {
    const numbers = getSelectedIssueNumbers('queued');
    if (!numbers.length) return;
    try {
        const req = uiActionContract.buildBulkDeprioritizeRequest(numbers);
        const resp = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        if (resp.ok) {
            showToast(`Deprioritized ${numbers.length} issue(s)`);
            await refreshViewModel();
        }
    } catch (e) {
        console.error('Bulk deprioritize failed:', e);
    }
}

async function bulkCancelQueued() {
    const numbers = getSelectedIssueNumbers('queued');
    if (!numbers.length) return;
    const confirmMsg = `Cancel ${numbers.length} queued issue(s)?\n\nThis will place them on hold before launch.\nThey will not run again until explicitly retried/unblocked.`;
    if (!await showConfirm(confirmMsg)) return;
    try {
        const req = uiActionContract.buildBulkCancelQueuedRequest(numbers);
        const res = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok) {
            const cancelled = Array.isArray(data.cancelled) ? data.cancelled.length : 0;
            const failed = Array.isArray(data.failed) ? data.failed.length : 0;
            if (cancelled > 0) showToast(`Cancelled ${cancelled} queued issue(s)`);
            if (failed > 0) showToast(`Failed to cancel ${failed} queued issue(s)`, true);
            await refreshViewModel();
            return;
        }
        showToast(data.error || `Queued cancel failed (${res.status})`, true);
    } catch (e) {
        console.error('Queued cancel failed:', e);
        showToast('Queued cancel failed: network error', true);
    }
}

// Pagination (preserves current tab)
function goToPage(page) {
    const url = new URL(window.location.href);
    url.searchParams.set('page', page);
    window.location.href = url.toString();
}

// E2E pagination (preserves current tab)
function goToE2EPage(page) {
    const url = new URL(window.location.href);
    url.searchParams.set('e2e_page', page);
    window.location.href = url.toString();
}

// Auto-refresh (preserves page param) - uses queue_refresh_seconds from config
const queueRefreshSeconds = window.dashboardData.queueRefreshSeconds;
if (queueRefreshSeconds > 0) {
    setTimeout(() => {
        window.location.reload();
    }, queueRefreshSeconds * 1000);
}

// Manual refresh function
function refreshPage() {
    window.location.reload();
}

// Dependency problems tracking
