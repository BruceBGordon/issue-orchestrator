(function (root, factory) {
    if (typeof module === 'object' && module.exports) {
        module.exports = factory;
    }
    if (root) {
        root.createControlCenterSetupWizard = factory;
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function createControlCenterSetupWizard(deps) {
    const {
        document,
        fetch,
        escapeHtml,
        loadRepos,
        setupCommands,
    } = deps;

    let state = {
        step: 1,
        repoPath: null,
        options: null,
        requiresReplacementConfirmation: false,
        previewReady: false,
    };
    let returnFocusElement = null;
    let inertSiblings = [];
    let bound = false;
    let operationGeneration = 0;

    function element(id) {
        const found = document.getElementById(id);
        if (!found) throw new Error(`Setup wizard element is missing: ${id}`);
        return found;
    }

    async function responseJson(response, fallbackMessage) {
        const data = await response.json();
        if (!response.ok || data.error) {
            const error = new Error(data.detail || data.error || fallbackMessage);
            error.setupPayload = data;
            throw error;
        }
        return data;
    }

    function renderSaveFailure(error) {
        const payload = error.setupPayload || {};
        let html = '<div class="error-message" role="alert">';
        html += '<h3 style="margin-top: 0;">Setup did not complete</h3>';
        html += `<p>${escapeHtml(payload.detail || error.message)}</p>`;
        if (payload.stage) {
            html += `<p><strong>Failed stage:</strong> ${escapeHtml(payload.stage)}</p>`;
        }
        if (payload.config_path) {
            html += `<p><strong>Existing config:</strong> <code>${escapeHtml(payload.config_path)}</code></p>`;
        }
        if (payload.applied_files?.length) {
            html += '<p><strong>Files already written:</strong></p><ul>';
            payload.applied_files.forEach((path) => {
                html += `<li><code>${escapeHtml(path)}</code></li>`;
            });
            html += '</ul>';
        }
        if (payload.created_labels?.length) {
            html += '<p><strong>Labels already created:</strong></p><ul>';
            payload.created_labels.forEach((label) => {
                html += `<li><code>${escapeHtml(label)}</code></li>`;
            });
            html += '</ul>';
        }
        html += '<p>Use <strong>Back</strong> to review the setup and generate a new preview before retrying.</p>';
        html += '</div>';
        return html;
    }

    function beginOperation(expectedStep) {
        operationGeneration += 1;
        return {
            generation: operationGeneration,
            repoPath: state.repoPath,
            step: expectedStep,
        };
    }

    function invalidateOperations() {
        operationGeneration += 1;
    }

    function isCurrentOperation(operation) {
        return operation.generation === operationGeneration
            && operation.repoPath === state.repoPath
            && operation.step === state.step
            && element('setupWizardModal').classList.contains('active');
    }

    function updateSteps() {
        document.querySelectorAll('.setup-step').forEach((stepElement) => {
            const step = parseInt(stepElement.dataset.step, 10);
            stepElement.classList.remove('active', 'done');
            stepElement.removeAttribute('aria-current');
            if (step < state.step) {
                stepElement.classList.add('done');
                stepElement.setAttribute('aria-label', `${stepElement.textContent}, complete`);
            } else if (step === state.step) {
                stepElement.classList.add('active');
                stepElement.setAttribute('aria-current', 'step');
                stepElement.setAttribute('aria-label', `${stepElement.textContent}, current`);
            } else {
                stepElement.setAttribute('aria-label', `${stepElement.textContent}, not started`);
            }
        });
        element('setupWizardBack').style.display = state.step > 1 ? 'inline-flex' : 'none';
        element('setupWizardNext').textContent =
            state.step === 3 ? 'Save Configuration' : 'Next';
    }

    function close() {
        invalidateOperations();
        const modal = element('setupWizardModal');
        modal.classList.remove('active');
        modal.setAttribute('aria-hidden', 'true');
        inertSiblings.forEach(({ sibling, wasInert }) => {
            sibling.inert = wasInert;
        });
        inertSiblings = [];
        if (returnFocusElement && typeof returnFocusElement.focus === 'function') {
            returnFocusElement.focus();
        }
        returnFocusElement = null;
    }

    async function open(repoPath, triggerElement = null) {
        invalidateOperations();
        state = {
            step: 1,
            repoPath,
            options: null,
            requiresReplacementConfirmation: false,
            previewReady: false,
        };
        returnFocusElement = triggerElement;
        const modal = element('setupWizardModal');
        modal.classList.add('active');
        modal.setAttribute('aria-hidden', 'false');
        inertSiblings = Array.from(document.body?.children || [])
            .filter((sibling) => sibling !== modal)
            .map((sibling) => {
                const wasInert = Boolean(sibling.inert);
                sibling.inert = true;
                return { sibling, wasInert };
            });
        updateSteps();
        element('closeSetupWizardModal').focus();
        await loadStep1();
    }

    async function loadStep1() {
        const operation = beginOperation(1);
        element('setupContent').innerHTML =
            '<div class="loading-spinner"></div> Checking prerequisites...';
        try {
            const response = await fetch(
                `/control/setup/prereqs?repo_root=${encodeURIComponent(state.repoPath)}`,
            );
            const data = await responseJson(response, 'Failed to check prerequisites');
            if (!isCurrentOperation(operation)) return;

            let html = '<h3 style="margin-top: 0;">Prerequisites</h3>';
            for (const [name, check] of Object.entries(data.checks || {})) {
                const isOk = check.ok;
                html += `<div class="prereq-item ${isOk ? 'ok' : 'fail'}">
                    <span class="prereq-icon" aria-hidden="true">${isOk ? '✓' : '✗'}</span>
                    <div>
                        <div class="prereq-name">${escapeHtml(name)}</div>
                        <div class="prereq-detail">${escapeHtml(check.detail || (isOk ? 'Found' : 'Not found'))}</div>
                    </div>
                </div>`;
            }

            if (!data.all_ok) {
                html += '<p style="color: var(--warning-color); margin-top: 16px;">Some prerequisites are missing. You can still continue, but the repository engine may not work correctly.</p>';
            }
            element('setupContent').innerHTML = html;
        } catch (error) {
            if (!isCurrentOperation(operation)) return;
            element('setupContent').innerHTML =
                `<div class="error-message">Failed to check prerequisites: ${escapeHtml(error.message)}</div>`;
        }
    }

    async function loadStep2() {
        const operation = beginOperation(2);
        const nextButton = element('setupWizardNext');
        nextButton.disabled = true;
        element('setupContent').innerHTML =
            '<div class="loading-spinner"></div> Detecting repository...';
        try {
            const response = await fetch(
                `/control/setup/detect?repo_root=${encodeURIComponent(state.repoPath)}`,
            );
            const data = await responseJson(response, 'Failed to detect repository');
            if (!isCurrentOperation(operation)) return;
            const existingConfig = data.existing_config || {};
            const existingAgents = existingConfig.agents || {};
            const reviewAgents = new Set([
                existingConfig.review?.default,
                existingConfig.review?.tech_lead_review_agent,
            ].filter(Boolean));
            const workerEntry = Object.entries(existingAgents).find(
                ([label]) => label !== 'agent:tech-lead' && !reviewAgents.has(label),
            );
            const detectedRepoName = typeof data.repo === 'string' ? data.repo : data.repo?.name;
            const repoName = existingConfig.repo?.name
                || detectedRepoName
                || data.repo_root?.split('/').pop()
                || 'unknown/repo';
            const workerAgentLabel = workerEntry?.[0] || 'agent:dev';
            const model = workerEntry?.[1]?.model || 'sonnet';
            const configureTechLead = data.existing_config
                ? Boolean(existingConfig.review?.tech_lead_review_agent)
                : true;

            let html = '<h3 style="margin-top: 0;">Configuration</h3>';
            html += data.existing_config
                ? '<p>Existing configuration found. Review the setup choices below.</p>'
                : '<p>No configuration found. Create a new repository setup.</p>';
            html += `
                <div class="form-group" style="margin-top: 16px;">
                    <label class="form-label" for="setupRepoName">Repository Name</label>
                    <input type="text" id="setupRepoName" class="form-input" value="${escapeHtml(repoName)}" style="width: 100%;">
                </div>
                <div class="form-group" style="margin-top: 12px;">
                    <label class="form-label" for="setupAgentLabel">Worker Agent Label</label>
                    <input type="text" id="setupAgentLabel" class="form-input" value="${escapeHtml(workerAgentLabel)}" style="width: 100%;">
                    <div style="font-size: 12px; color: var(--text-muted); margin-top: 4px;">The GitHub label that routes implementation work to this agent.</div>
                </div>
                <div class="form-group" style="margin-top: 12px;">
                    <label class="form-label" for="setupModel">Model</label>
                    <select id="setupModel" class="form-input" style="width: 100%;">
                        <option value="sonnet" ${model === 'sonnet' ? 'selected' : ''}>Sonnet (recommended)</option>
                        <option value="opus" ${model === 'opus' ? 'selected' : ''}>Opus</option>
                        <option value="haiku" ${model === 'haiku' ? 'selected' : ''}>Haiku</option>
                    </select>
                </div>
                <div class="form-group" style="margin-top: 16px;">
                    <label style="display: flex; align-items: flex-start; gap: 8px; cursor: pointer;">
                        <input type="checkbox" id="setupConfigureTechLead" ${configureTechLead ? 'checked' : ''}>
                        <span>
                            <strong>Configure a tech-lead agent?</strong>
                            <span style="display: block; font-size: 12px; color: var(--text-muted); margin-top: 3px;">Creates the tech-lead agent, prompt, review labels, and follow-up routing. Enabled by default.</span>
                        </span>
                    </label>
                </div>
            `;
            element('setupContent').innerHTML = html;
            nextButton.disabled = false;
        } catch (error) {
            if (!isCurrentOperation(operation)) return;
            element('setupContent').innerHTML =
                `<div class="error-message" role="alert">
                    Failed to detect repository: ${escapeHtml(error.message)}
                    <p>Use <strong>Back</strong> to return to prerequisites before retrying.</p>
                </div>`;
        }
    }

    function collectOptions() {
        return {
            repoName: element('setupRepoName').value,
            workerAgentLabel: element('setupAgentLabel').value,
            model: element('setupModel').value,
            configureTechLead: element('setupConfigureTechLead').checked,
        };
    }

    async function loadStep3() {
        const operation = beginOperation(3);
        state.previewReady = false;
        const nextButton = element('setupWizardNext');
        nextButton.disabled = true;
        try {
            state.options = collectOptions();
            element('setupContent').innerHTML =
                '<div class="loading-spinner"></div> Generating preview...';
            const command = setupCommands.buildSetupPreviewRequest(
                state.repoPath,
                state.options,
            );
            const response = await fetch(command.endpoint, {
                method: command.method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(command.body),
            });
            const data = await responseJson(response, 'Failed to generate preview');
            if (!isCurrentOperation(operation)) return;

            let html = '<h3 style="margin-top: 0;">Preview Configuration</h3>';
            html += '<p>The following configuration will be saved:</p>';
            html += `<pre style="background: var(--bg-tertiary); padding: 12px; border-radius: 8px; font-size: 12px; overflow-x: auto;">${escapeHtml(data.yaml || '')}</pre>`;

            if (data.files && data.files.length > 0) {
                html += '<p style="margin-top: 16px;"><strong>Planned file changes:</strong></p>';
                html += '<ul style="margin: 8px 0; padding-left: 20px;">';
                data.files.forEach((file) => {
                    const action = file.action === 'overwrite' ? 'Replace' : 'Create';
                    html += `<li><strong>${action}:</strong> <code>${escapeHtml(file.path)}</code></li>`;
                });
                html += '</ul>';
            }

            state.requiresReplacementConfirmation = Boolean(
                data.files?.some((file) => file.action === 'overwrite'),
            );
            if (state.requiresReplacementConfirmation) {
                html += `
                    <div class="setup-replacement-warning" role="alert">
                        This setup will replace an existing configuration. Settings and agents
                        not shown in the preview will be discarded.
                    </div>
                    <div class="form-group" style="margin-top: 16px;">
                        <label style="display: flex; align-items: flex-start; gap: 8px; cursor: pointer;">
                            <input type="checkbox" id="setupConfirmReplace">
                            <span>I understand that saving will replace the existing configuration.</span>
                        </label>
                    </div>
                `;
            }
            html += `
                <div class="form-group" style="margin-top: 16px;">
                    <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
                        <input type="checkbox" id="setupCreateLabels" checked>
                        Create GitHub labels for configured agents and workflows
                    </label>
                </div>
            `;
            element('setupContent').innerHTML = html;
            const replaceConfirmation = document.getElementById('setupConfirmReplace');
            nextButton.disabled = Boolean(replaceConfirmation);
            state.previewReady = true;
            replaceConfirmation?.addEventListener('change', () => {
                if (!isCurrentOperation(operation)) return;
                nextButton.disabled = !replaceConfirmation.checked;
            });
        } catch (error) {
            if (!isCurrentOperation(operation)) return;
            element('setupContent').innerHTML =
                `<div class="error-message" role="alert">
                    Failed to generate preview: ${escapeHtml(error.message)}
                    <p>Use <strong>Back</strong> to review the setup before retrying.</p>
                </div>`;
        }
    }

    async function save() {
        if (!state.previewReady) return;
        const operation = beginOperation(3);
        const nextButton = element('setupWizardNext');
        const createLabels = element('setupCreateLabels').checked;
        const replaceExisting = state.requiresReplacementConfirmation
            ? element('setupConfirmReplace').checked
            : false;
        if (state.requiresReplacementConfirmation && !replaceExisting) {
            element('setupConfirmReplace').focus();
            return;
        }
        state.previewReady = false;
        element('setupContent').innerHTML =
            '<div class="loading-spinner"></div> Saving configuration...';
        nextButton.disabled = true;

        try {
            const command = setupCommands.buildSetupSaveRequest(
                state.repoPath,
                state.options,
                {
                    createPrompts: true,
                    createLabels,
                    replaceExisting,
                },
            );
            const response = await fetch(command.endpoint, {
                method: command.method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(command.body),
            });
            const data = await responseJson(response, 'Failed to save configuration');
            if (!isCurrentOperation(operation)) return;

            let html = '<h3 style="margin-top: 0; color: var(--success-color);">Setup Complete!</h3>';
            html += '<p>Configuration has been saved successfully.</p>';
            if (data.created_files && data.created_files.length > 0) {
                html += '<p><strong>Written files:</strong></p>';
                html += '<ul style="margin: 8px 0; padding-left: 20px;">';
                data.created_files.forEach((file) => {
                    html += `<li><code>${escapeHtml(file)}</code></li>`;
                });
                html += '</ul>';
            }
            html += '<p style="margin-top: 16px;">You can now start the repository engine for this repository.</p>';

            element('setupContent').innerHTML = html;
            nextButton.textContent = 'Done';
            nextButton.disabled = false;
            element('setupWizardBack').style.display = 'none';
            state.step = 4;
            await loadRepos();
        } catch (error) {
            if (!isCurrentOperation(operation)) return;
            element('setupContent').innerHTML = renderSaveFailure(error);
            nextButton.disabled = true;
            element('setupWizardBack').style.display = 'inline-flex';
        }
    }

    function bind() {
        if (bound) return;
        bound = true;
        element('closeSetupWizardModal').addEventListener('click', close);
        element('setupWizardCancel').addEventListener('click', close);
        element('setupWizardBack').addEventListener('click', async () => {
            if (state.step <= 1) return;
            invalidateOperations();
            state.step -= 1;
            state.previewReady = false;
            element('setupWizardNext').disabled = false;
            updateSteps();
            if (state.step === 1) await loadStep1();
            else if (state.step === 2) await loadStep2();
        });
        element('setupWizardNext').addEventListener('click', async () => {
            if (element('setupWizardNext').disabled) return;
            if (state.step === 4) {
                close();
                return;
            }
            if (state.step === 3) {
                await save();
                return;
            }
            state.step += 1;
            updateSteps();
            if (state.step === 2) await loadStep2();
            else if (state.step === 3) await loadStep3();
        });
        document.addEventListener('keydown', (event) => {
            const modal = element('setupWizardModal');
            if (!modal.classList.contains('active')) return;
            if (event.key === 'Escape') {
                close();
                return;
            }
            if (event.key !== 'Tab') return;

            const focusable = Array.from(modal.querySelectorAll(
                'button, [href], input, select, textarea, [tabindex]',
            )).filter((candidate) => (
                !candidate.disabled
                && !candidate.hidden
                && candidate.style?.display !== 'none'
                && candidate.getAttribute?.('tabindex') !== '-1'
            ));
            if (focusable.length === 0) {
                event.preventDefault();
                modal.focus();
                return;
            }

            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            const activeElement = document.activeElement;
            if (event.shiftKey && (activeElement === first || !modal.contains(activeElement))) {
                event.preventDefault();
                last.focus();
            } else if (
                !event.shiftKey
                && (activeElement === last || !modal.contains(activeElement))
            ) {
                event.preventDefault();
                first.focus();
            }
        });
    }

    return { bind, open, close };
});
