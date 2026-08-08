// Provider circuit-breaker outage banner + health panel (issue #5980).
//
// Reads the typed ``providerCircuit`` payload the dashboard view model
// publishes inside ``window.dashboardData`` and reveals a page-level banner
// when any provider circuit is open (or the circuit read itself failed). A
// native <details> disclosure lists per-provider state (Unavailable /
// Recovering), cooldown remaining, attempts, and last error so operators see
// *why* work is blocked without watching the UI during the outage.
//
// Accessibility (issue #5980): the banner container is NOT the live region.
// A dedicated visually-hidden ``#providerCircuitAnnouncer`` (role="alert") is
// the only assertive region, and it is written ONLY when the semantic outage
// state changes -- so the countdown ticking down does not re-announce the
// outage on every SSE refresh. The interactive <details> node is persistent:
// updates replace only its <ul> rows, so an operator's expanded panel and
// keyboard focus survive live refreshes.
//
// The banner is hidden whenever no circuit is open and the read succeeded --
// a healthy fleet shows nothing. Live refresh: ``refreshViewModel`` re-invokes
// ``updateProviderCircuitBanner`` after each SSE-triggered fetch.

// Pure: one per-provider <li> row (escapes untrusted provider/error text).
function renderProviderCircuitEntryRow(entry) {
    const provider = escapeHtml(entry.provider || '');
    const isOpen = !!entry.is_open;
    const stateClass = isOpen ? 'open' : 'recovering';
    const statusLabel = escapeHtml(entry.status_label || (isOpen ? 'Unavailable' : 'Recovering'));

    const cells = [
        `<span class="pcircuit-provider">${provider}</span>`,
        `<span class="pcircuit-badge pcircuit-badge--${stateClass}">${statusLabel}</span>`,
    ];
    if (entry.cooldown_remaining_label) {
        cells.push(`<span class="pcircuit-cooldown">Retry in ${escapeHtml(entry.cooldown_remaining_label)}</span>`);
    }
    if (entry.next_retry_at) {
        const at = formatTimestamp(entry.next_retry_at, '');
        if (at) {
            cells.push(`<span class="pcircuit-eta">(${escapeHtml(at)})</span>`);
        }
    }
    const attempts = Number(entry.consecutive_outages) || 0;
    cells.push(`<span class="pcircuit-attempts">Attempts: ${attempts}</span>`);
    if (entry.last_error_summary) {
        cells.push(
            `<span class="pcircuit-error" title="${escapeAttr(entry.last_error_summary)}">`
            + `${escapeHtml(entry.last_error_summary)}</span>`,
        );
    }
    return `<li class="pcircuit-row pcircuit-row--${stateClass}">${cells.join('')}</li>`;
}

// Pure: true when the banner should be visible. A failed circuit read
// (status_unavailable) is NOT the same as a healthy fleet -- surface it as a
// warning so a broken read can't masquerade as "no outage" (issue #5980).
function providerCircuitIsActive(circuit) {
    if (!circuit) {
        return false;
    }
    return !!circuit.any_open || !!circuit.status_unavailable;
}

// Pure: the visible summary line, which may include the live countdown. Shown
// to sighted users and updated freely each tick -- it is NOT the alert region.
function providerCircuitSummaryText(circuit) {
    if (!providerCircuitIsActive(circuit)) {
        return '';
    }
    if (circuit.summary_text) {
        return String(circuit.summary_text);
    }
    return circuit.status_unavailable
        ? 'Provider circuit status unavailable — could not read circuit state.'
        : 'Provider outage in progress.';
}

// Pure: concise, countdown-free message for the assertive announcer.
function providerCircuitAnnouncement(circuit) {
    if (!providerCircuitIsActive(circuit)) {
        return '';
    }
    if (circuit.status_unavailable) {
        return 'Provider circuit status unavailable — could not read circuit state.';
    }
    const providers = Array.isArray(circuit.open_providers) ? circuit.open_providers : [];
    const names = providers.length ? providers.join(', ') : 'a provider';
    return `Provider outage in progress: ${names} unavailable.`;
}

// Pure: a stable signature for the announcer. It changes ONLY when the semantic
// outage state changes (which providers are open, or a read failure), NOT when
// the countdown ticks -- so a non-semantic refresh never re-announces.
function providerCircuitSignature(circuit) {
    if (!providerCircuitIsActive(circuit)) {
        return '';
    }
    if (circuit.status_unavailable) {
        return 'unavailable';
    }
    const providers = Array.isArray(circuit.open_providers)
        ? circuit.open_providers.slice().sort()
        : [];
    return `open:${providers.join(',')}`;
}

// Pure: the <ul> inner rows for the details panel, or '' when there are none.
function providerCircuitRowsHtml(circuit) {
    const entries = (circuit && Array.isArray(circuit.entries)) ? circuit.entries : [];
    return entries.map(renderProviderCircuitEntryRow).join('');
}

// Surgically update the banner DOM. This never rewrites the whole container and
// never replaces the <details> node, so (a) the assertive announcer is written
// only for semantic changes -- not on every countdown tick -- and (b) an
// operator's expanded panel and keyboard focus survive live refreshes.
function updateProviderCircuitBanner(circuit) {
    const el = document.getElementById('providerCircuitBanner');
    if (!el) {
        return;
    }
    const announcer = document.getElementById('providerCircuitAnnouncer');
    const body = document.getElementById('providerCircuitBody');
    const summary = document.getElementById('providerCircuitSummary');
    const details = document.getElementById('providerCircuitDetails');
    const list = document.getElementById('providerCircuitList');

    if (!providerCircuitIsActive(circuit)) {
        el.style.display = 'none';
        if (summary) {
            summary.textContent = '';
        }
        if (list) {
            list.innerHTML = '';
        }
        if (details) {
            details.hidden = true;
        }
        // Reset the announcement + its signature so a NEW outage re-announces.
        if (announcer && announcer.dataset.circuitSig !== '') {
            announcer.textContent = '';
            announcer.dataset.circuitSig = '';
        }
        return;
    }

    if (body && body.classList) {
        body.classList.toggle('pcircuit-body--unavailable', !!circuit.status_unavailable);
    }
    if (summary) {
        // Visible, non-live: free to change every tick (e.g. the countdown).
        summary.textContent = providerCircuitSummaryText(circuit);
    }
    if (announcer) {
        // Assertive: write ONLY when the semantic state changes, so the
        // countdown ticking down does not re-announce the outage.
        const signature = providerCircuitSignature(circuit);
        if (announcer.dataset.circuitSig !== signature) {
            announcer.textContent = providerCircuitAnnouncement(circuit);
            announcer.dataset.circuitSig = signature;
        }
    }
    if (list && details) {
        const rows = providerCircuitRowsHtml(circuit);
        if (rows) {
            // Replace only the rows -- the <details>/<summary> nodes persist, so
            // an opened panel stays open and keeps focus across refreshes.
            list.innerHTML = rows;
            details.hidden = false;
        } else {
            list.innerHTML = '';
            details.hidden = true;
        }
    }
    el.style.display = 'flex';
}

// Convenience for the load/refresh hooks: pull the payload off the shared
// dashboard data blob and render.
function renderProviderCircuitFromDashboardData() {
    const circuit = (typeof window !== 'undefined' && window.dashboardData)
        ? window.dashboardData.providerCircuit
        : null;
    updateProviderCircuitBanner(circuit);
}
