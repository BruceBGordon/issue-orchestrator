(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.controlCenterSetupCommands = api;
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    const OPEN_SETUP_KIND = 'open_repository_setup';
    const CLAUDE_MODELS = ['haiku', 'sonnet', 'opus'];
    const CLAUDE_EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'];

    function requiredText(value, name) {
        const normalized = typeof value === 'string' ? value.trim() : '';
        if (!normalized) throw new Error(`${name} is required`);
        return normalized;
    }

    function buildOpenSetupCommand(repoRoot) {
        return {
            kind: OPEN_SETUP_KIND,
            repo_root: requiredText(repoRoot, 'repoRoot'),
        };
    }

    function githubAuthorization(value = { kind: 'detected' }) {
        const kind = requiredText(value.kind, 'githubAuthorization.kind');
        if (!['detected', 'personal', 'github_app'].includes(kind)) {
            throw new Error(`Unsupported GitHub authorization: ${kind}`);
        }
        const apiUrl = typeof value.api_url === 'string' ? value.api_url.trim() : '';
        const timeout = Number(value.http_timeout_seconds ?? 20);
        if (!Number.isFinite(timeout) || timeout <= 0) {
            throw new Error('githubAuthorization.http_timeout_seconds must be positive');
        }
        const authorization = {
            kind,
            api_url: apiUrl || 'https://api.github.com',
            http_timeout_seconds: timeout,
        };
        [
            'token_env',
            'keyring_service',
            'keyring_username',
            'app_client_id',
            'app_id',
            'app_installation_id',
            'app_private_key_path',
            'app_private_key_env',
        ].forEach((key) => {
            const valueAtKey = typeof value[key] === 'string' ? value[key].trim() : '';
            if (valueAtKey) authorization[key] = valueAtKey;
        });
        return authorization;
    }

    function requiredChoice(value, name, choices) {
        const normalized = requiredText(value, name);
        if (!choices.includes(normalized)) {
            throw new Error(`Unsupported ${name}: ${normalized}`);
        }
        return normalized;
    }

    async function runOpenSetupCommand(command, controller, triggerElement = null) {
        if (!command || command.kind !== OPEN_SETUP_KIND) {
            throw new Error(`Unsupported setup command: ${command?.kind || ''}`);
        }
        if (!controller || typeof controller.open !== 'function') {
            throw new Error('Repository setup controller is not ready');
        }
        await controller.open(
            requiredText(command.repo_root, 'repo_root'),
            triggerElement,
        );
    }

    function buildSetupPayload(repoRoot, options = {}) {
        const workerAgentLabel = requiredText(
            options.workerAgentLabel,
            'workerAgentLabel',
        );
        if (!/^agent:(?!(?:reviewer|tech-lead)$).+$/u.test(workerAgentLabel)) {
            throw new Error(
                "workerAgentLabel must match 'agent:<worker>' and cannot be 'agent:reviewer' or 'agent:tech-lead'",
            );
        }
        const model = requiredChoice(options.model, 'worker model', CLAUDE_MODELS);
        const effort = requiredChoice(
            options.effort ?? 'high',
            'worker effort',
            CLAUDE_EFFORTS,
        );
        const reviewerModel = requiredChoice(
            options.reviewerModel ?? 'sonnet',
            'reviewer model',
            CLAUDE_MODELS,
        );
        const reviewerEffort = requiredChoice(
            options.reviewerEffort ?? 'high',
            'reviewer effort',
            CLAUDE_EFFORTS,
        );
        const techLeadModel = requiredChoice(
            options.techLeadModel ?? 'sonnet',
            'tech lead model',
            CLAUDE_MODELS,
        );
        const techLeadEffort = requiredChoice(
            options.techLeadEffort ?? 'high',
            'tech lead effort',
            CLAUDE_EFFORTS,
        );
        const techLeadReviewThreshold = Number(
            options.techLeadReviewThreshold ?? 1,
        );
        if (
            !Number.isInteger(techLeadReviewThreshold)
            || techLeadReviewThreshold < 0
            || techLeadReviewThreshold > 50
        ) {
            throw new Error('techLeadReviewThreshold must be an integer from 0 to 50');
        }
        const internalReviewMaxRounds = Number(
            options.internalReviewMaxRounds ?? 5,
        );
        if (
            !Number.isInteger(internalReviewMaxRounds)
            || internalReviewMaxRounds < 1
            || internalReviewMaxRounds > 50
        ) {
            throw new Error('internalReviewMaxRounds must be an integer from 1 to 50');
        }
        return {
            repo_root: requiredText(repoRoot, 'repoRoot'),
            repo_name: requiredText(options.repoName, 'repoName'),
            worker_agent_label: workerAgentLabel,
            model,
            effort,
            configure_reviewer: options.configureReviewer !== false,
            reviewer_model: reviewerModel,
            reviewer_effort: reviewerEffort,
            configure_internal_reviewer: options.configureInternalReviewer === true,
            internal_review_max_rounds: internalReviewMaxRounds,
            internal_review_instructions: requiredText(
                options.internalReviewInstructions ?? '.io/internal-review.md',
                'internalReviewInstructions',
            ),
            validation_quick_command: requiredText(
                options.validationQuickCommand,
                'validationQuickCommand',
            ),
            validation_publish_command: requiredText(
                options.validationPublishCommand,
                'validationPublishCommand',
            ),
            worktree_base: requiredText(options.worktreeBase, 'worktreeBase'),
            github_authorization: githubAuthorization(options.githubAuthorization),
            configure_tech_lead: options.configureTechLead !== false,
            tech_lead_model: techLeadModel,
            tech_lead_effort: techLeadEffort,
            tech_lead_review_threshold: techLeadReviewThreshold,
        };
    }

    function buildGithubVerifyRequest(repoRoot, repoName, authorization) {
        return {
            endpoint: '/control/setup/github-auth/verify',
            method: 'POST',
            body: {
                repo_root: requiredText(repoRoot, 'repoRoot'),
                repo_name: requiredText(repoName, 'repoName'),
                authorization: githubAuthorization(authorization),
            },
        };
    }

    function buildGithubTokenStoreRequest(
        repoRoot,
        repoName,
        token,
        transportAuthorization = { kind: 'detected' },
    ) {
        const authorization = githubAuthorization(transportAuthorization);
        return {
            endpoint: '/control/setup/github-auth/store-personal-token',
            method: 'POST',
            body: {
                repo_root: requiredText(repoRoot, 'repoRoot'),
                repo_name: requiredText(repoName, 'repoName'),
                token: requiredText(token, 'token'),
                api_url: authorization.api_url || 'https://api.github.com',
                http_timeout_seconds: authorization.http_timeout_seconds || 20,
            },
        };
    }

    function buildSetupPreviewRequest(repoRoot, options) {
        return {
            endpoint: '/control/setup/preview',
            method: 'POST',
            body: buildSetupPayload(repoRoot, options),
        };
    }

    function buildSetupSaveRequest(repoRoot, options, saveOptions = {}) {
        return {
            endpoint: '/control/setup/save',
            method: 'POST',
            body: {
                ...buildSetupPayload(repoRoot, options),
                config_name: saveOptions.configName || 'default.yaml',
                create_prompts: saveOptions.createPrompts !== false,
                create_labels: saveOptions.createLabels !== false,
                replace_existing: saveOptions.replaceExisting === true,
            },
        };
    }

    return {
        OPEN_SETUP_KIND,
        buildOpenSetupCommand,
        buildGithubTokenStoreRequest,
        buildGithubVerifyRequest,
        runOpenSetupCommand,
        buildSetupPreviewRequest,
        buildSetupSaveRequest,
    };
});
