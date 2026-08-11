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
        detected: null,
        githubVerified: false,
        githubVerification: null,
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
            state.step === 4 ? 'Save Configuration' : 'Next';
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
            detected: null,
            githubVerified: false,
            githubVerification: null,
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
            state.detected = data;
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
            const effort = workerEntry?.[1]?.provider_args?.effort || 'high';
            const reviewerAgentLabel = existingConfig.review?.default;
            const reviewerEntry = reviewerAgentLabel
                ? existingAgents[reviewerAgentLabel]
                : null;
            const reviewerModel = reviewerEntry?.model || 'sonnet';
            const reviewerEffort = reviewerEntry?.provider_args?.effort || 'high';
            const techLeadAgentLabel = existingConfig.review?.tech_lead_review_agent;
            const techLeadEntry = techLeadAgentLabel
                ? existingAgents[techLeadAgentLabel]
                : null;
            const techLeadModel = techLeadEntry?.model || 'sonnet';
            const techLeadEffort = techLeadEntry?.provider_args?.effort || 'high';
            const detectedValidation = data.validation_defaults || {};
            const validationQuickCommand = existingConfig.validation?.quick?.cmd
                || detectedValidation.quick_command
                || '';
            const validationPublishCommand = existingConfig.validation?.publish?.cmd
                || detectedValidation.publish_command
                || '';
            const validationSource = detectedValidation.source
                || 'No repository-native validation command was detected.';
            const worktreeBase = existingConfig.worktrees?.base
                || data.worktree_base_default
                || `../worktrees/${data.repo_root?.split('/').pop() || 'repository'}`;
            const configureReviewer = data.existing_config
                ? Boolean(existingConfig.review?.enabled && reviewerAgentLabel)
                : true;
            const configureInternalReviewer = Boolean(
                existingConfig.review?.internal?.enabled,
            );
            const internalReviewMaxRounds = Number.isInteger(
                existingConfig.review?.internal?.max_rounds,
            )
                ? existingConfig.review.internal.max_rounds
                : 5;
            const internalReviewInstructions =
                existingConfig.review?.internal?.instructions
                || '.io/internal-review.md';
            const configureTechLead = data.existing_config
                ? existingConfig.tech_lead?.enabled
                    ?? Boolean(existingConfig.review?.tech_lead_review_agent)
                : true;
            const techLeadReviewThreshold = Number.isInteger(
                existingConfig.review?.tech_lead_review_threshold,
            )
                ? existingConfig.review.tech_lead_review_threshold
                : 1;

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
                    <label class="form-label" for="setupWorktreeBase">Worktree Base Directory</label>
                    <input
                        type="text"
                        id="setupWorktreeBase"
                        class="form-input"
                        value="${escapeHtml(worktreeBase)}"
                        aria-describedby="setupWorktreeBaseHelp"
                        style="width: 100%;"
                    >
                    <div id="setupWorktreeBaseHelp" style="font-size: 12px; color: var(--text-muted); margin-top: 4px;">
                        Worktrees default to a dedicated directory outside this repo.
                        The resolved path will be shown in Preview.
                    </div>
                </div>
                <fieldset class="setup-role-card setup-validation-card">
                    <legend>Validation gates</legend>
                    <p class="setup-field-help">
                        These required commands run inside each issue worktree and must
                        exercise repository behavior. Setup detected:
                        <strong>${escapeHtml(validationSource)}</strong>
                    </p>
                    <div class="setup-role-fields">
                        <div class="form-group">
                            <label class="form-label" for="setupValidationQuickCommand">Quick review-loop command</label>
                            <input
                                type="text"
                                id="setupValidationQuickCommand"
                                class="form-input"
                                value="${escapeHtml(validationQuickCommand)}"
                                aria-describedby="setupValidationQuickCommandHelp"
                            >
                            <div id="setupValidationQuickCommandHelp" class="setup-field-help">
                                Runs before each reviewer turn.
                            </div>
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="setupValidationPublishCommand">Publish command</label>
                            <input
                                type="text"
                                id="setupValidationPublishCommand"
                                class="form-input"
                                value="${escapeHtml(validationPublishCommand)}"
                                aria-describedby="setupValidationPublishCommandHelp"
                            >
                            <div id="setupValidationPublishCommandHelp" class="setup-field-help">
                                Authoritative gate before push and PR publication.
                            </div>
                        </div>
                    </div>
                </fieldset>
                <div class="setup-role-grid" aria-label="Agent role configuration">
                    ${renderAgentRoleFields({
                        role: 'Worker',
                        idPrefix: 'setupWorker',
                        model,
                        effort,
                        description: 'Implements issues in an isolated worktree.',
                    })}
                    ${renderAgentRoleFields({
                        role: 'Code reviewer',
                        idPrefix: 'setupReviewer',
                        model: reviewerModel,
                        effort: reviewerEffort,
                        description: 'Reviews each implementation and returns required changes to the worker.',
                        toggleId: 'setupConfigureReviewer',
                        enabled: configureReviewer,
                    })}
                    <fieldset class="setup-role-card">
                        <legend>Coder-owned internal review</legend>
                        <p id="setupInternalReviewerHelp" class="setup-field-help">
                            Makes each coder iterate with one fast internal reviewer before
                            the independent code reviewer sees the work.
                        </p>
                        <label class="setup-role-toggle">
                            <input
                                type="checkbox"
                                id="setupConfigureInternalReviewer"
                                ${configureInternalReviewer ? 'checked' : ''}
                                aria-describedby="setupInternalReviewerHelp"
                                aria-controls="setupInternalReviewerFields"
                                aria-expanded="${configureInternalReviewer}"
                            >
                            <span>Enable internal reviewer loop</span>
                        </label>
                        <div id="setupInternalReviewerFields" class="setup-role-fields">
                            <div class="form-group">
                                <label class="form-label" for="setupInternalReviewMaxRounds">
                                    Maximum review rounds
                                </label>
                                <input
                                    type="number"
                                    id="setupInternalReviewMaxRounds"
                                    class="form-input"
                                    min="1"
                                    max="50"
                                    value="${escapeHtml(internalReviewMaxRounds)}"
                                >
                            </div>
                            <div class="form-group">
                                <label class="form-label" for="setupInternalReviewInstructions">
                                    Reviewer instructions file
                                </label>
                                <input
                                    type="text"
                                    id="setupInternalReviewInstructions"
                                    class="form-input"
                                    value="${escapeHtml(internalReviewInstructions)}"
                                    aria-describedby="setupInternalReviewInstructionsHelp"
                                >
                                <div id="setupInternalReviewInstructionsHelp" class="setup-field-help">
                                    Repository-relative Markdown file. Setup creates the starter
                                    file when the loop is enabled and the file is missing.
                                </div>
                            </div>
                        </div>
                    </fieldset>
                    ${renderAgentRoleFields({
                        role: 'Tech lead',
                        idPrefix: 'setupTechLead',
                        model: techLeadModel,
                        effort: techLeadEffort,
                        description: 'Reviews approved work for broader architectural concerns and investigates failures.',
                        toggleId: 'setupConfigureTechLead',
                        enabled: configureTechLead,
                        extra: `
                            <div class="form-group">
                                <label class="form-label" for="setupTechLeadReviewThreshold">Review cadence</label>
                                <input
                                    type="number"
                                    id="setupTechLeadReviewThreshold"
                                    class="form-input"
                                    min="0"
                                    max="50"
                                    value="${escapeHtml(techLeadReviewThreshold)}"
                                    aria-describedby="setupTechLeadReviewThresholdHelp"
                                >
                                <div id="setupTechLeadReviewThresholdHelp" class="setup-field-help">
                                    1 reviews every approved PR; 5 reviews in batches of five;
                                    0 limits the tech lead to manual and failure-triggered reviews.
                                </div>
                            </div>
                        `,
                    })}
                </div>
            `;
            element('setupContent').innerHTML = html;
            const updateValidationReadiness = () => {
                nextButton.disabled = (
                    !element('setupValidationQuickCommand').value.trim()
                    || !element('setupValidationPublishCommand').value.trim()
                );
            };
            element('setupValidationQuickCommand').addEventListener(
                'input',
                updateValidationReadiness,
            );
            element('setupValidationPublishCommand').addEventListener(
                'input',
                updateValidationReadiness,
            );
            const internalReviewToggle = element('setupConfigureInternalReviewer');
            const updateInternalReviewFields = () => {
                const enabled = internalReviewToggle.checked;
                internalReviewToggle.setAttribute('aria-expanded', String(enabled));
                element('setupInternalReviewerFields').hidden = !enabled;
            };
            internalReviewToggle.addEventListener('change', updateInternalReviewFields);
            updateInternalReviewFields();
            updateValidationReadiness();
        } catch (error) {
            if (!isCurrentOperation(operation)) return;
            element('setupContent').innerHTML =
                `<div class="error-message" role="alert">
                    Failed to detect repository: ${escapeHtml(error.message)}
                    <p>Use <strong>Back</strong> to return to prerequisites before retrying.</p>
                </div>`;
        }
    }

    function renderAgentRoleFields({
        role,
        idPrefix,
        model,
        effort,
        description,
        toggleId = null,
        enabled = true,
        extra = '',
    }) {
        const toggle = toggleId
            ? `
                <label class="setup-role-toggle">
                    <input type="checkbox" id="${toggleId}" ${enabled ? 'checked' : ''}>
                    <span>Configure this role</span>
                </label>
            `
            : '';
        return `
            <fieldset class="setup-role-card">
                <legend>${escapeHtml(role)}</legend>
                <p class="setup-field-help">${escapeHtml(description)}</p>
                ${toggle}
                <div class="setup-role-fields">
                    <div class="form-group">
                        <label class="form-label" for="${idPrefix}Model">Model</label>
                        <select id="${idPrefix}Model" class="form-input">
                            <option value="sonnet" ${model === 'sonnet' ? 'selected' : ''}>Sonnet (recommended)</option>
                            <option value="opus" ${model === 'opus' ? 'selected' : ''}>Opus</option>
                            <option value="haiku" ${model === 'haiku' ? 'selected' : ''}>Haiku</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label class="form-label" for="${idPrefix}Effort">Effort</label>
                        <select id="${idPrefix}Effort" class="form-input">
                            <option value="low" ${effort === 'low' ? 'selected' : ''}>Low</option>
                            <option value="medium" ${effort === 'medium' ? 'selected' : ''}>Medium</option>
                            <option value="high" ${effort === 'high' ? 'selected' : ''}>High (recommended)</option>
                            <option value="xhigh" ${effort === 'xhigh' ? 'selected' : ''}>Extra high</option>
                            <option value="max" ${effort === 'max' ? 'selected' : ''}>Maximum</option>
                        </select>
                    </div>
                    ${extra}
                </div>
            </fieldset>
        `;
    }

    function collectOptions() {
        return {
            repoName: element('setupRepoName').value,
            workerAgentLabel: element('setupAgentLabel').value,
            model: element('setupWorkerModel').value,
            effort: element('setupWorkerEffort').value,
            worktreeBase: element('setupWorktreeBase').value,
            configureReviewer: element('setupConfigureReviewer').checked,
            reviewerModel: element('setupReviewerModel').value,
            reviewerEffort: element('setupReviewerEffort').value,
            configureInternalReviewer: element('setupConfigureInternalReviewer').checked,
            internalReviewMaxRounds: element('setupInternalReviewMaxRounds').value,
            internalReviewInstructions: element('setupInternalReviewInstructions').value,
            validationQuickCommand: element('setupValidationQuickCommand').value,
            validationPublishCommand: element('setupValidationPublishCommand').value,
            configureTechLead: element('setupConfigureTechLead').checked,
            techLeadModel: element('setupTechLeadModel').value,
            techLeadEffort: element('setupTechLeadEffort').value,
            techLeadReviewThreshold: element('setupTechLeadReviewThreshold').value,
            githubAuthorization: state.options?.githubAuthorization || { kind: 'detected' },
        };
    }

    function githubPermissionList() {
        return `
            <ul style="margin: 8px 0 0; padding-left: 20px;">
                <li>Contents: read and write</li>
                <li>Issues: read and write</li>
                <li>Pull requests: read and write</li>
                <li>Metadata: read</li>
            </ul>
        `;
    }

    function authorizationTransport(authorization = {}) {
        return {
            api_url: authorization.api_url || 'https://api.github.com',
            http_timeout_seconds: authorization.http_timeout_seconds || 20,
        };
    }

    function githubWebOrigin(apiUrl) {
        const parsed = new URL(apiUrl || 'https://api.github.com');
        if (!['http:', 'https:'].includes(parsed.protocol)) {
            throw new Error('GitHub API URL must use HTTP or HTTPS');
        }
        return parsed.hostname.toLowerCase() === 'api.github.com'
            ? 'https://github.com'
            : parsed.origin;
    }

    function appAuthorizationFromForm() {
        return {
            ...authorizationTransport(
                state.detected?.github_authorization?.authorization,
            ),
            kind: 'github_app',
            app_client_id: element('setupGithubAppClientId').value,
            app_id: element('setupGithubAppId').value,
            app_installation_id: element('setupGithubAppInstallationId').value,
            app_private_key_path: element('setupGithubAppKeyPath').value,
            app_private_key_env: element('setupGithubAppKeyEnv').value,
        };
    }

    function setGithubVerificationPending() {
        state.githubVerified = false;
        state.githubVerification = null;
        element('setupWizardNext').disabled = true;
        const status = document.getElementById('setupGithubStatus');
        if (status) status.innerHTML = 'Authorization has not been verified yet.';
    }

    async function verifyGithubAuthorization(command) {
        const operation = beginOperation(3);
        const status = element('setupGithubStatus');
        const nextButton = element('setupWizardNext');
        state.githubVerified = false;
        state.githubVerification = null;
        nextButton.disabled = true;
        status.innerHTML = '<div class="loading-spinner"></div> Verifying repository access...';
        try {
            const response = await fetch(command.endpoint, {
                method: command.method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(command.body),
            });
            const data = await responseJson(response, 'GitHub authorization failed');
            if (!isCurrentOperation(operation)) return;

            state.options.githubAuthorization = data.authorization;
            state.githubVerified = true;
            state.githubVerification = data;
            let permissions = '';
            (data.required_permissions || []).forEach((permission) => {
                permissions += `<li>${escapeHtml(permission)}</li>`;
            });
            status.innerHTML = `
                <div class="prereq-item ok" role="status">
                    <span class="prereq-icon" aria-hidden="true">✓</span>
                    <div>
                        <div class="prereq-name">Verified as ${escapeHtml(data.identity)}</div>
                        <div class="prereq-detail">
                            ${escapeHtml(data.source)} · ${escapeHtml(data.repository)}
                        </div>
                    </div>
                </div>
                <p>${escapeHtml(data.authorship_notice)}</p>
                <p>${escapeHtml(data.verification_note)}</p>
                <p><strong>Required GitHub permissions:</strong></p>
                <ul style="margin: 8px 0; padding-left: 20px;">${permissions}</ul>
                <p style="font-size: 12px; color: var(--text-muted);">
                    Agents never receive this credential. GitHub operations are performed
                    by the orchestrator.
                </p>
            `;
            nextButton.disabled = false;
        } catch (error) {
            if (!isCurrentOperation(operation)) return;
            status.innerHTML = `
                <div class="error-message" role="alert">
                    GitHub verification failed: ${escapeHtml(error.message)}
                    <p>Complete the GitHub steps above, then select Verify again.</p>
                </div>
            `;
        }
    }

    async function loadStep3() {
        if (document.getElementById('setupRepoName')) {
            state.options = collectOptions();
        }
        if (!state.options) {
            throw new Error('Repository setup options are missing');
        }
        state.githubVerified = false;
        state.githubVerification = null;
        const nextButton = element('setupWizardNext');
        nextButton.disabled = true;

        const authInfo = state.detected?.github_authorization || {};
        const existingAuthorization = authInfo.authorization || { kind: 'detected' };
        const appConfigured = authInfo.configured_kind === 'github_app';
        const repoName = state.options.repoName;
        const owner = repoName.includes('/') ? repoName.split('/')[0] : '';
        const webOrigin = githubWebOrigin(existingAuthorization.api_url);
        const tokenUrl = new URL('/settings/personal-access-tokens/new', webOrigin);
        const appUrl = new URL('/settings/apps/new', webOrigin);
        tokenUrl.searchParams.set('name', 'Issue Orchestrator');
        tokenUrl.searchParams.set(
            'description',
            `Issue Orchestrator access for ${repoName}`,
        );
        if (owner) tokenUrl.searchParams.set('target_name', owner);
        tokenUrl.searchParams.set('expires_in', '90');
        tokenUrl.searchParams.set('contents', 'write');
        tokenUrl.searchParams.set('issues', 'write');
        tokenUrl.searchParams.set('pull_requests', 'write');

        let migrationWarning = '';
        if (authInfo.inline_token_migration_required) {
            migrationWarning = `
                <div class="setup-replacement-warning" role="alert">
                    The existing YAML contains an inline GitHub token. Setup will not send
                    that secret to the browser. Store a replacement in the OS keychain
                    below before continuing.
                </div>
            `;
        }
        if (authInfo.configuration_error) {
            migrationWarning += `
                <div class="error-message" role="alert">
                    Existing GitHub auth is invalid: ${escapeHtml(authInfo.configuration_error)}
                </div>
            `;
        }

        const app = appConfigured ? existingAuthorization : {};
        element('setupContent').innerHTML = `
            <h3 style="margin-top: 0;">GitHub Authorization</h3>
            <p>
                Choose the GitHub API identity that will create labels and open pull
                requests. GitHub App mode also authenticates orchestrator-owned branch
                pushes; personal mode keeps the repository’s configured git transport.
                Setup waits here until repository access is verified.
            </p>
            ${migrationWarning}
            <fieldset style="border: 0; margin: 16px 0; padding: 0;">
                <legend class="form-label">Authorization mode</legend>
                <label style="display: flex; gap: 8px; margin-top: 8px;">
                    <input type="radio" name="setupGithubMode" id="setupGithubPersonal"
                        value="personal" ${appConfigured ? '' : 'checked'}>
                    <span>
                        <strong>Use my GitHub identity</strong>
                        <span style="display: block; font-size: 12px; color: var(--text-muted);">
                            Fastest for personal use. Pull requests are authored as you,
                            so you cannot approve your own protected-branch PR.
                        </span>
                    </span>
                </label>
                <label style="display: flex; gap: 8px; margin-top: 12px;">
                    <input type="radio" name="setupGithubMode" id="setupGithubApp"
                        value="github_app" ${appConfigured ? 'checked' : ''}>
                    <span>
                        <strong>Use a GitHub App</strong>
                        <span style="display: block; font-size: 12px; color: var(--text-muted);">
                            Recommended for teams and protected branches. PRs are authored
                            by the bot, leaving you eligible to approve them.
                        </span>
                    </span>
                </label>
            </fieldset>

            <section id="setupGithubPersonalPanel" ${appConfigured ? 'hidden' : ''}>
                <h4>Use an existing credential</h4>
                <p>
                    Verify the credential already configured for this repository or
                    detected in the Control Center environment, GitHub CLI, or Keychain.
                </p>
                <button type="button" class="btn" id="setupVerifyDetectedGithub">
                    Verify detected identity
                </button>

                <details style="margin-top: 16px;">
                    <summary>Create and store a fine-grained personal token</summary>
                    <ol style="padding-left: 20px;">
                        <li>
                            <a href="${escapeHtml(tokenUrl.toString())}" target="_blank"
                               rel="noopener noreferrer">Open GitHub’s prefilled token form</a>.
                        </li>
                        <li>Select only <strong>${escapeHtml(repoName)}</strong>.</li>
                        <li>Confirm these repository permissions:${githubPermissionList()}</li>
                        <li>Generate the token, copy it once, and paste it below.</li>
                    </ol>
                    <label class="form-label" for="setupGithubToken">New personal token</label>
                    <input type="password" id="setupGithubToken" class="form-input"
                           autocomplete="new-password" style="width: 100%;">
                    <p id="setupGithubTokenHelp" style="font-size: 12px; color: var(--text-muted);">
                        The token is verified, then stored in a repo-scoped OS keychain
                        entry. It is never written to YAML.
                    </p>
                    <button type="button" class="btn" id="setupStoreGithubToken">
                        Verify and store token
                    </button>
                </details>
            </section>

            <section id="setupGithubAppPanel" ${appConfigured ? '' : 'hidden'}>
                <h4>Configure a GitHub App</h4>
                <ol style="padding-left: 20px;">
                    <li>
                        <a href="${escapeHtml(appUrl.toString())}" target="_blank"
                           rel="noopener noreferrer">Create a GitHub App</a> owned by the
                        repository owner.
                    </li>
                    <li>Disable webhooks and user authorization/device flow.</li>
                    <li>Grant Contents, Issues, and Pull requests read/write; Checks and
                        Commit statuses read; Metadata is automatic.</li>
                    <li>Install it on only <strong>${escapeHtml(repoName)}</strong>.</li>
                    <li>Generate a private key and save it outside the repository.</li>
                </ol>
                <div class="form-group">
                    <label class="form-label" for="setupGithubAppClientId">Client ID</label>
                    <input id="setupGithubAppClientId" class="form-input" type="text"
                           value="${escapeHtml(app.app_client_id || '')}" style="width: 100%;">
                </div>
                <div class="form-group">
                    <label class="form-label" for="setupGithubAppId">App ID (optional fallback)</label>
                    <input id="setupGithubAppId" class="form-input" type="text"
                           value="${escapeHtml(app.app_id || '')}" style="width: 100%;">
                </div>
                <div class="form-group">
                    <label class="form-label" for="setupGithubAppInstallationId">Installation ID</label>
                    <input id="setupGithubAppInstallationId" class="form-input" type="text"
                           value="${escapeHtml(app.app_installation_id || '')}" style="width: 100%;">
                </div>
                <div class="form-group">
                    <label class="form-label" for="setupGithubAppKeyPath">Private-key path</label>
                    <input id="setupGithubAppKeyPath" class="form-input" type="text"
                           value="${escapeHtml(app.app_private_key_path || '')}"
                           placeholder="~/.config/issue-orchestrator/github-apps/bot.pem"
                           style="width: 100%;">
                </div>
                <div class="form-group">
                    <label class="form-label" for="setupGithubAppKeyEnv">
                        Or private-key environment variable
                    </label>
                    <input id="setupGithubAppKeyEnv" class="form-input" type="text"
                           value="${escapeHtml(app.app_private_key_env || '')}"
                           placeholder="ISSUE_ORCH_GITHUB_APP_PRIVATE_KEY"
                           style="width: 100%;">
                </div>
                <p style="font-size: 12px; color: var(--text-muted);">
                    Set exactly one private-key source. YAML stores only the path or
                    environment-variable name, never the private key.
                </p>
                <button type="button" class="btn" id="setupVerifyGithubApp">
                    Verify GitHub App
                </button>
            </section>
            <div id="setupGithubStatus" aria-live="polite" style="margin-top: 16px;">
                Authorization has not been verified yet.
            </div>
        `;

        const personalPanel = element('setupGithubPersonalPanel');
        const appPanel = element('setupGithubAppPanel');
        personalPanel.inert = appConfigured;
        appPanel.inert = !appConfigured;
        element('setupGithubPersonal').addEventListener('change', () => {
            personalPanel.hidden = false;
            personalPanel.inert = false;
            appPanel.hidden = true;
            appPanel.inert = true;
            setGithubVerificationPending();
        });
        element('setupGithubApp').addEventListener('change', () => {
            personalPanel.hidden = true;
            personalPanel.inert = true;
            appPanel.hidden = false;
            appPanel.inert = false;
            setGithubVerificationPending();
        });
        element('setupVerifyDetectedGithub').addEventListener('click', async () => {
            const transport = authorizationTransport(existingAuthorization);
            const command = setupCommands.buildGithubVerifyRequest(
                state.repoPath,
                repoName,
                existingAuthorization.kind === 'github_app'
                    ? { kind: 'detected', ...transport }
                    : existingAuthorization,
            );
            await verifyGithubAuthorization(command);
        });
        element('setupStoreGithubToken').addEventListener('click', async () => {
            const tokenInput = element('setupGithubToken');
            try {
                const command = setupCommands.buildGithubTokenStoreRequest(
                    state.repoPath,
                    repoName,
                    tokenInput.value,
                    existingAuthorization,
                );
                await verifyGithubAuthorization(command);
            } finally {
                tokenInput.value = '';
            }
        });
        element('setupVerifyGithubApp').addEventListener('click', async () => {
            const command = setupCommands.buildGithubVerifyRequest(
                state.repoPath,
                repoName,
                appAuthorizationFromForm(),
            );
            await verifyGithubAuthorization(command);
        });
        [
            'setupGithubAppClientId',
            'setupGithubAppId',
            'setupGithubAppInstallationId',
            'setupGithubAppKeyPath',
            'setupGithubAppKeyEnv',
        ].forEach((id) => {
            element(id).addEventListener('input', setGithubVerificationPending);
        });
    }

    async function loadStep4() {
        const operation = beginOperation(4);
        state.previewReady = false;
        const nextButton = element('setupWizardNext');
        nextButton.disabled = true;
        try {
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
            html += `<p><strong>Resolved worktree location:</strong> <code>${escapeHtml(data.worktree_base || '')}</code></p>`;
            html += `<p><strong>Verified GitHub identity:</strong> ${escapeHtml(data.github_authorization?.identity || '')} via ${escapeHtml(data.github_authorization?.source || '')}</p>`;
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
        const operation = beginOperation(4);
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
            const configuredRoles = ['worker'];
            if (state.options?.configureReviewer) configuredRoles.push('reviewer/rework');
            if (state.options?.configureInternalReviewer) {
                configuredRoles.splice(1, 0, 'coder-owned internal review');
            }
            if (state.options?.configureTechLead) configuredRoles.push('tech lead');
            html += `<p><strong>Configured pipeline:</strong> ${configuredRoles.join(' → ')}</p>`;
            const omittedDefaultRoles = [
                state.options?.configureReviewer
                    ? ''
                    : '<li><strong>Code reviewer:</strong> enable the bounded review/rework gate.</li>',
                state.options?.configureInternalReviewer
                    ? ''
                    : '<li><strong>Internal reviewer:</strong> add a fast coder-owned review loop before external review.</li>',
                state.options?.configureTechLead
                    ? ''
                    : '<li><strong>Tech lead:</strong> enable architectural and failure review.</li>',
            ].join('');
            html += `
                <section class="setup-next-options" aria-labelledby="setupNextOptionsHeading">
                    <h4 id="setupNextOptionsHeading">Optional capabilities to consider later</h4>
                    <p>These independent capabilities can be added from repository
                        Settings when they become useful:</p>
                    <ul>
                        ${omittedDefaultRoles}
                        <li><strong>Specialized routing:</strong> multiple worker roles and role-specific reviewers.</li>
                        <li><strong>Other AI providers:</strong> Codex or custom agent commands.</li>
                        <li><strong>E2E runner:</strong> scheduled repository-level end-to-end testing.</li>
                        <li><strong>Merge queue:</strong> GitHub-managed merging after review gates pass.</li>
                        <li><strong>Goal pilot:</strong> coordinated multi-issue planning and approval.</li>
                        <li><strong>Tech-lead health automation:</strong> periodic reviews, storm detection, and stuck-session recovery.</li>
                    </ul>
                </section>
            `;

            element('setupContent').innerHTML = html;
            nextButton.textContent = 'Done';
            nextButton.disabled = false;
            element('setupWizardBack').style.display = 'none';
            state.step = 5;
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
            state.githubVerified = false;
            state.githubVerification = null;
            element('setupWizardNext').disabled = false;
            updateSteps();
            if (state.step === 1) await loadStep1();
            else if (state.step === 2) await loadStep2();
            else if (state.step === 3) await loadStep3();
        });
        element('setupWizardNext').addEventListener('click', async () => {
            if (element('setupWizardNext').disabled) return;
            if (state.step === 5) {
                close();
                return;
            }
            if (state.step === 4) {
                await save();
                return;
            }
            state.step += 1;
            updateSteps();
            if (state.step === 2) await loadStep2();
            else if (state.step === 3) await loadStep3();
            else if (state.step === 4) await loadStep4();
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
                && !candidate.closest?.('[hidden], [inert]')
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
