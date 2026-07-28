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
        if (!workerAgentLabel.startsWith('agent:')) {
            throw new Error("workerAgentLabel must start with 'agent:'");
        }
        if (workerAgentLabel === 'agent:tech-lead') {
            throw new Error('workerAgentLabel must identify a worker');
        }
        const model = requiredText(options.model, 'model');
        if (!['haiku', 'sonnet', 'opus'].includes(model)) {
            throw new Error(`Unsupported setup model: ${model}`);
        }
        return {
            repo_root: requiredText(repoRoot, 'repoRoot'),
            repo_name: requiredText(options.repoName, 'repoName'),
            worker_agent_label: workerAgentLabel,
            model,
            configure_tech_lead: options.configureTechLead !== false,
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
            },
        };
    }

    return {
        OPEN_SETUP_KIND,
        buildOpenSetupCommand,
        runOpenSetupCommand,
        buildSetupPreviewRequest,
        buildSetupSaveRequest,
    };
});
