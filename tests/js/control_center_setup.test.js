const test = require('node:test');
const assert = require('node:assert');

const setupCommands = require(
    '../../src/issue_orchestrator/static/js/control_center_setup_commands.js',
);
const createSetupWizard = require(
    '../../src/issue_orchestrator/static/js/control_center_setup.js',
);

function fakeElement({
    dataset = {},
    value = '',
    checked = false,
    ownerDocument = null,
    parentElement = null,
} = {}) {
    const classes = new Set();
    const listeners = new Map();
    const attributes = new Map();
    const node = {
        dataset,
        value,
        checked,
        parentElement,
        disabled: false,
        hidden: false,
        inert: false,
        innerHTML: '',
        textContent: '',
        style: {},
        focusCount: 0,
        focusableChildren: [],
        classList: {
            add: (...names) => names.forEach((name) => classes.add(name)),
            remove: (...names) => names.forEach((name) => classes.delete(name)),
            contains: (name) => classes.has(name),
        },
        setAttribute: (name, attributeValue) => attributes.set(name, attributeValue),
        getAttribute: (name) => attributes.get(name),
        removeAttribute: (name) => attributes.delete(name),
        addEventListener: (name, listener) => listeners.set(name, listener),
        emit: async (name, event = {}) => listeners.get(name)?.(event),
        querySelectorAll() {
            return this.focusableChildren;
        },
        contains(candidate) {
            return candidate === this || this.focusableChildren.includes(candidate);
        },
        closest(selector) {
            let current = this;
            while (current) {
                if (
                    selector === '[hidden], [inert]'
                    && (current.hidden || current.inert)
                ) {
                    return current;
                }
                current = current.parentElement;
            }
            return null;
        },
        focus() {
            this.focusCount += 1;
            if (ownerDocument) ownerDocument.activeElement = this;
        },
    };
    return node;
}

function fakeDocument() {
    const listeners = new Map();
    const document = {
        activeElement: null,
        body: { children: [] },
        elements: null,
        getElementById: null,
        querySelectorAll: null,
        addEventListener: (name, listener) => listeners.set(name, listener),
        emit: async (name, event = {}) => listeners.get(name)?.(event),
    };
    const makeElement = (options = {}) => fakeElement({
        ...options,
        ownerDocument: document,
    });
    const elements = new Map([
        ['setupWizardModal', makeElement()],
        ['setupWizardBack', makeElement()],
        ['setupWizardNext', makeElement()],
        ['setupContent', makeElement()],
        ['closeSetupWizardModal', makeElement()],
        ['setupWizardCancel', makeElement()],
    ]);
    const content = elements.get('setupContent');
    const dynamicIds = [
        'setupRepoName',
        'setupAgentLabel',
        'setupWorkerModel',
        'setupWorkerEffort',
        'setupConfigureReviewer',
        'setupReviewerModel',
        'setupReviewerEffort',
        'setupConfigureInternalReviewer',
        'setupInternalReviewerFields',
        'setupInternalReviewMaxRounds',
        'setupInternalReviewInstructions',
        'setupValidationQuickCommand',
        'setupValidationPublishCommand',
        'setupWorktreeBase',
        'setupConfigureTechLead',
        'setupTechLeadModel',
        'setupTechLeadEffort',
        'setupTechLeadReviewThreshold',
        'setupGithubPersonal',
        'setupGithubApp',
        'setupGithubPersonalPanel',
        'setupGithubAppPanel',
        'setupVerifyDetectedGithub',
        'setupGithubToken',
        'setupStoreGithubToken',
        'setupGithubAppClientId',
        'setupGithubAppId',
        'setupGithubAppInstallationId',
        'setupGithubAppKeyPath',
        'setupGithubAppKeyEnv',
        'setupVerifyGithubApp',
        'setupGithubStatus',
        'setupCreateLabels',
        'setupConfirmReplace',
    ];
    let contentHtml = '';
    Object.defineProperty(content, 'innerHTML', {
        get: () => contentHtml,
        set: (html) => {
            contentHtml = html;
            dynamicIds.forEach((id) => elements.delete(id));
            if (html.includes('id="setupCreateLabels"')) {
                elements.set('setupCreateLabels', makeElement({ checked: true }));
            }
            if (html.includes('id="setupConfirmReplace"')) {
                elements.set('setupConfirmReplace', makeElement({ checked: false }));
            }
            if (html.includes('id="setupWorktreeBase"')) {
                const value = html.match(/id="setupWorktreeBase"[\s\S]*?value="([^"]*)"/)?.[1]
                    || '../worktrees/repository';
                elements.set('setupWorktreeBase', makeElement({ value }));
            }
            dynamicIds.forEach((id) => {
                if (elements.has(id) || !html.includes(`id="${id}"`)) return;
                const tag = html.match(new RegExp(`<[^>]+id="${id}"[^>]*>`))?.[0] || '';
                const select = html.match(
                    new RegExp(`<select[^>]+id="${id}"[^>]*>[\\s\\S]*?<\\/select>`),
                )?.[0] || '';
                const value = tag.match(/value="([^"]*)"/)?.[1]
                    || select.match(/<option value="([^"]*)"[^>]*selected/)?.[1]
                    || '';
                const node = makeElement({
                    value,
                    checked: /\schecked(?:\s|>)/.test(tag),
                });
                node.hidden = /\shidden(?:\s|>)/.test(tag);
                elements.set(id, node);
            });
        },
    });
    const steps = [1, 2, 3, 4].map((step) => makeElement({
        dataset: { step: String(step) },
    }));
    const modal = elements.get('setupWizardModal');
    const background = makeElement();
    modal.focusableChildren = [
        elements.get('closeSetupWizardModal'),
        elements.get('setupWizardBack'),
        elements.get('setupWizardCancel'),
        elements.get('setupWizardNext'),
    ];
    document.body.children = [background, modal];
    document.elements = elements;
    document.background = background;
    document.getElementById = (id) => elements.get(id) || null;
    document.querySelectorAll = (selector) => selector === '.setup-step' ? steps : [];
    document.makeElement = makeElement;
    return document;
}

function jsonResponse(data, ok = true) {
    return {
        ok,
        json: async () => data,
    };
}

function githubVerificationResponse() {
    return jsonResponse({
        verified: true,
        identity: 'setup-user',
        repository: 'owner/porchpin',
        auth_kind: 'personal',
        source: 'Environment variable ISSUE_ORCH_GITHUB_TOKEN',
        authorship_notice: 'Branches and pull requests will be authored as setup-user.',
        verification_note: (
            'Setup verified identity and repository access without making writes.'
        ),
        required_permissions: [
            'Contents: read and write',
            'Issues: read and write',
            'Pull requests: read and write',
            'Metadata: read',
        ],
        authorization: {
            kind: 'personal',
            token_env: 'ISSUE_ORCH_GITHUB_TOKEN',
            api_url: 'https://api.github.com',
            http_timeout_seconds: 20,
        },
    });
}

function repositoryDetection(overrides = {}) {
    return {
        repo_root: '/repos/porchpin',
        repo: 'owner/porchpin',
        existing_config: null,
        config_path: null,
        github_labels: [],
        agent_labels: [],
        prompt_candidates: [],
        worktree_base_default: '../worktrees/porchpin',
        worktree_base_resolved: '/repos/worktrees/porchpin',
        github_authorization: {
            authorization: {
                kind: 'detected',
                api_url: 'https://api.github.com',
                http_timeout_seconds: 20,
            },
            configured_kind: 'detected',
            inline_token_migration_required: false,
        },
        validation_defaults: {
            quick_command: 'make test-quick',
            publish_command: 'make validate',
            source: 'Makefile targets',
        },
        ...overrides,
    };
}

function githubPreviewSummary() {
    return {
        identity: 'setup-user',
        source: 'Environment variable ISSUE_ORCH_GITHUB_TOKEN',
    };
}

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((resolvePromise, rejectPromise) => {
        resolve = resolvePromise;
        reject = rejectPromise;
    });
    return { promise, resolve, reject };
}

test('setup action command dispatches to the repository setup controller', async () => {
    const calls = [];
    const controller = {
        open: async (path, trigger) => calls.push([path, trigger]),
    };
    const trigger = fakeElement();

    await setupCommands.runOpenSetupCommand(
        setupCommands.buildOpenSetupCommand('/repos/porchpin'),
        controller,
        trigger,
    );

    assert.deepEqual(calls, [['/repos/porchpin', trigger]]);
});

test('setup request contract defaults the complete review pipeline on', () => {
    const enabled = setupCommands.buildSetupPreviewRequest('/repos/porchpin', {
        repoName: 'owner/porchpin',
        workerAgentLabel: 'agent:dev',
        model: 'sonnet',
        validationQuickCommand: 'make test-quick',
        validationPublishCommand: 'make validate',
        worktreeBase: '../worktrees/porchpin',
    });
    const disabled = setupCommands.buildSetupSaveRequest('/repos/porchpin', {
        repoName: 'owner/porchpin',
        workerAgentLabel: 'agent:dev',
        model: 'sonnet',
        validationQuickCommand: 'make test-quick',
        validationPublishCommand: 'make validate',
        worktreeBase: '../worktrees/porchpin',
        configureReviewer: false,
        configureInternalReviewer: true,
        internalReviewMaxRounds: 3,
        internalReviewInstructions: '.io/fast-review.md',
        configureTechLead: false,
    }, {
        createLabels: false,
    });

    assert.equal(enabled.endpoint, '/control/setup/preview');
    assert.equal(enabled.body.effort, 'high');
    assert.equal(enabled.body.configure_reviewer, true);
    assert.equal(enabled.body.reviewer_model, 'sonnet');
    assert.equal(enabled.body.reviewer_effort, 'high');
    assert.equal(enabled.body.configure_internal_reviewer, false);
    assert.equal(enabled.body.internal_review_max_rounds, 5);
    assert.equal(
        enabled.body.internal_review_instructions,
        '.io/internal-review.md',
    );
    assert.equal(enabled.body.validation_quick_command, 'make test-quick');
    assert.equal(enabled.body.validation_publish_command, 'make validate');
    assert.equal(enabled.body.configure_tech_lead, true);
    assert.equal(enabled.body.tech_lead_model, 'sonnet');
    assert.equal(enabled.body.tech_lead_effort, 'high');
    assert.equal(enabled.body.tech_lead_review_threshold, 1);
    assert.equal(enabled.body.worktree_base, '../worktrees/porchpin');
    assert.equal(disabled.endpoint, '/control/setup/save');
    assert.equal(disabled.body.configure_reviewer, false);
    assert.equal(disabled.body.configure_internal_reviewer, true);
    assert.equal(disabled.body.internal_review_max_rounds, 3);
    assert.equal(disabled.body.internal_review_instructions, '.io/fast-review.md');
    assert.equal(disabled.body.configure_tech_lead, false);
    assert.equal(disabled.body.create_prompts, true);
    assert.equal(disabled.body.create_labels, false);
    assert.equal(disabled.body.replace_existing, false);
});

test('setup request contract rejects empty and reserved worker labels', () => {
    for (const workerAgentLabel of ['agent:', 'agent:reviewer', 'agent:tech-lead']) {
        assert.throws(
            () => setupCommands.buildSetupPreviewRequest('/repos/porchpin', {
                repoName: 'owner/porchpin',
                workerAgentLabel,
                model: 'sonnet',
                worktreeBase: '../worktrees/porchpin',
            }),
            /workerAgentLabel must match/,
        );
    }
});

test('setup request contract rejects invalid role and validation choices', () => {
    const valid = {
        repoName: 'owner/porchpin',
        workerAgentLabel: 'agent:dev',
        model: 'sonnet',
        validationQuickCommand: 'make test-quick',
        validationPublishCommand: 'make validate',
        worktreeBase: '../worktrees/porchpin',
    };

    assert.throws(
        () => setupCommands.buildSetupPreviewRequest('/repos/porchpin', {
            ...valid,
            reviewerEffort: 'unbounded',
        }),
        /Unsupported reviewer effort/,
    );
    assert.throws(
        () => setupCommands.buildSetupPreviewRequest('/repos/porchpin', {
            ...valid,
            techLeadReviewThreshold: 51,
        }),
        /techLeadReviewThreshold must be an integer/,
    );
    assert.throws(
        () => setupCommands.buildSetupPreviewRequest('/repos/porchpin', {
            ...valid,
            internalReviewMaxRounds: 0,
        }),
        /internalReviewMaxRounds must be an integer/,
    );
    assert.throws(
        () => setupCommands.buildSetupPreviewRequest('/repos/porchpin', {
            ...valid,
            validationQuickCommand: '   ',
        }),
        /validationQuickCommand is required/,
    );
});

test('personal token storage preserves GHES transport metadata', () => {
    const command = setupCommands.buildGithubTokenStoreRequest(
        '/repos/porchpin',
        'owner/porchpin',
        'ghp_secret',
        {
            kind: 'detected',
            api_url: 'https://github.example/api/v3',
            http_timeout_seconds: 47,
        },
    );

    assert.deepEqual(command.body, {
        repo_root: '/repos/porchpin',
        repo_name: 'owner/porchpin',
        token: 'ghp_secret',
        api_url: 'https://github.example/api/v3',
        http_timeout_seconds: 47,
    });
});

test('setup modal completes the default-on preview and save round trip', async () => {
    const document = fakeDocument();
    const fetchCalls = [];
    let loadReposCalls = 0;
    const responses = [
        jsonResponse({
            all_ok: true,
            checks: { git: { ok: true, detail: 'git version 2' } },
        }),
        jsonResponse(repositoryDetection()),
        githubVerificationResponse(),
        jsonResponse({
            yaml: 'repo:\n  name: owner/porchpin\n',
            worktree_base: '/repos/worktrees/porchpin',
            github_authorization: githubPreviewSummary(),
            files: [
                { path: '/repos/porchpin/.issue-orchestrator/config/default.yaml', action: 'create' },
                { path: '/repos/porchpin/.io/reviewer.md', action: 'create', type: 'prompt' },
                { path: '/repos/porchpin/.io/tech-lead.md', action: 'create', type: 'prompt' },
            ],
        }),
        jsonResponse({
            status: 'saved',
            config_path: '/repos/porchpin/.issue-orchestrator/config/default.yaml',
            created_files: [
                '/repos/porchpin/.issue-orchestrator/config/default.yaml',
                '/repos/porchpin/.io/reviewer.md',
                '/repos/porchpin/.io/tech-lead.md',
            ],
            created_labels: ['agent:dev', 'agent:reviewer', 'agent:tech-lead'],
        }),
    ];
    const fetch = async (...args) => {
        fetchCalls.push(args);
        return responses.shift();
    };
    const wizard = createSetupWizard({
        document,
        fetch,
        escapeHtml: (value) => String(value),
        loadRepos: async () => { loadReposCalls += 1; },
        setupCommands,
    });
    wizard.bind();
    const trigger = fakeElement();

    await setupCommands.runOpenSetupCommand(
        setupCommands.buildOpenSetupCommand('/repos/porchpin'),
        wizard,
        trigger,
    );

    const modal = document.elements.get('setupWizardModal');
    assert.equal(modal.classList.contains('active'), true);
    assert.equal(modal.getAttribute('aria-hidden'), 'false');
    assert.equal(document.background.inert, true);
    assert.equal(document.elements.get('closeSetupWizardModal').focusCount, 1);
    assert.equal(fetchCalls[0][0], '/control/setup/prereqs?repo_root=%2Frepos%2Fporchpin');

    const next = document.elements.get('setupWizardNext');
    await next.emit('click');
    const configureHtml = document.elements.get('setupContent').innerHTML;
    assert.match(configureHtml, /<fieldset class="setup-role-card">/);
    assert.match(configureHtml, /<legend>Worker<\/legend>/);
    assert.match(configureHtml, /<legend>Code reviewer<\/legend>/);
    assert.match(configureHtml, /id="setupConfigureReviewer" checked/);
    assert.match(configureHtml, /<legend>Coder-owned internal review<\/legend>/);
    assert.match(configureHtml, /id="setupConfigureInternalReviewer"/);
    assert.doesNotMatch(
        configureHtml,
        /id="setupConfigureInternalReviewer"[^>]*checked/,
    );
    assert.match(configureHtml, /aria-controls="setupInternalReviewerFields"/);
    assert.match(configureHtml, /id="setupInternalReviewMaxRounds"/);
    assert.match(configureHtml, /min="1"/);
    assert.match(configureHtml, /max="50"/);
    assert.match(configureHtml, /id="setupInternalReviewInstructions"/);
    assert.match(
        configureHtml,
        /aria-describedby="setupInternalReviewInstructionsHelp"/,
    );
    const internalReviewToggle = document.elements.get(
        'setupConfigureInternalReviewer',
    );
    const internalReviewFields = document.elements.get(
        'setupInternalReviewerFields',
    );
    assert.equal(internalReviewFields.hidden, true);
    assert.equal(internalReviewToggle.getAttribute('aria-expanded'), 'false');
    internalReviewToggle.checked = true;
    await internalReviewToggle.emit('change');
    assert.equal(internalReviewFields.hidden, false);
    assert.equal(internalReviewToggle.getAttribute('aria-expanded'), 'true');
    internalReviewToggle.checked = false;
    await internalReviewToggle.emit('change');
    assert.match(configureHtml, /id="setupConfigureTechLead" checked/);
    assert.match(configureHtml, /id="setupWorkerEffort"/);
    assert.match(configureHtml, /id="setupReviewerEffort"/);
    assert.match(configureHtml, /id="setupTechLeadEffort"/);
    assert.match(configureHtml, /id="setupTechLeadReviewThreshold"/);
    assert.match(configureHtml, /<legend>Validation gates<\/legend>/);
    assert.match(configureHtml, /id="setupValidationQuickCommand"/);
    assert.match(configureHtml, /id="setupValidationPublishCommand"/);
    assert.match(configureHtml, /Makefile targets/);
    assert.match(configureHtml, /Authoritative gate before push and PR publication/);
    assert.match(configureHtml, /1 reviews every approved PR/);
    assert.match(configureHtml, /id="setupWorktreeBase"/);
    assert.match(configureHtml, /value="\.\.\/worktrees\/porchpin"/);
    assert.match(configureHtml, /aria-describedby="setupWorktreeBaseHelp"/);

    document.elements.set(
        'setupRepoName',
        document.makeElement({ value: 'owner/porchpin' }),
    );
    document.elements.set(
        'setupAgentLabel',
        document.makeElement({ value: 'agent:dev' }),
    );
    document.elements.set('setupWorkerModel', document.makeElement({ value: 'sonnet' }));
    document.elements.set(
        'setupConfigureTechLead',
        document.makeElement({ checked: true }),
    );
    await next.emit('click');

    const authorizationHtml = document.elements.get('setupContent').innerHTML;
    assert.match(authorizationHtml, /Use my GitHub identity/);
    assert.match(authorizationHtml, /Use a GitHub App/);
    assert.match(authorizationHtml, /GitHub API identity/);
    assert.match(authorizationHtml, /personal mode keeps the repository’s configured git transport/);
    assert.match(authorizationHtml, /Setup waits here until repository access is verified/);
    assert.match(authorizationHtml, /Contents: read and write/);
    assert.match(authorizationHtml, /<fieldset/);
    assert.match(authorizationHtml, /<legend class="form-label">Authorization mode/);
    assert.match(authorizationHtml, /<details/);
    assert.match(authorizationHtml, /type="password" id="setupGithubToken"/);
    assert.match(authorizationHtml, /rel="noopener noreferrer"/);
    assert.equal(document.elements.get('setupGithubPersonalPanel').inert, false);
    assert.equal(document.elements.get('setupGithubAppPanel').inert, true);
    assert.equal(next.disabled, true);

    await document.elements.get('setupVerifyDetectedGithub').emit('click');
    const verificationRequest = fetchCalls[2];
    assert.equal(verificationRequest[0], '/control/setup/github-auth/verify');
    assert.deepEqual(JSON.parse(verificationRequest[1].body).authorization, {
        kind: 'detected',
        api_url: 'https://api.github.com',
        http_timeout_seconds: 20,
    });
    assert.equal(next.disabled, false);
    assert.match(document.elements.get('setupGithubStatus').innerHTML, /Verified as setup-user/);
    assert.match(
        document.elements.get('setupGithubStatus').innerHTML,
        /Agents never receive this credential/,
    );

    await next.emit('click');
    const previewRequest = fetchCalls[3];
    assert.equal(previewRequest[0], '/control/setup/preview');
    assert.equal(JSON.parse(previewRequest[1].body).configure_reviewer, true);
    assert.equal(
        JSON.parse(previewRequest[1].body).configure_internal_reviewer,
        false,
    );
    assert.equal(JSON.parse(previewRequest[1].body).configure_tech_lead, true);
    assert.equal(JSON.parse(previewRequest[1].body).effort, 'high');
    assert.equal(JSON.parse(previewRequest[1].body).reviewer_effort, 'high');
    assert.equal(JSON.parse(previewRequest[1].body).tech_lead_effort, 'high');
    assert.equal(JSON.parse(previewRequest[1].body).tech_lead_review_threshold, 1);
    assert.equal(
        JSON.parse(previewRequest[1].body).validation_quick_command,
        'make test-quick',
    );
    assert.equal(
        JSON.parse(previewRequest[1].body).validation_publish_command,
        'make validate',
    );
    assert.deepEqual(
        JSON.parse(previewRequest[1].body).github_authorization,
        {
            kind: 'personal',
            token_env: 'ISSUE_ORCH_GITHUB_TOKEN',
            api_url: 'https://api.github.com',
            http_timeout_seconds: 20,
        },
    );
    assert.equal(
        JSON.parse(previewRequest[1].body).worktree_base,
        '../worktrees/porchpin',
    );
    const previewHtml = document.elements.get('setupContent').innerHTML;
    assert.match(previewHtml, /\/repos\/porchpin\/\.io\/reviewer\.md/);
    assert.match(previewHtml, /\/repos\/porchpin\/\.io\/tech-lead\.md/);
    assert.match(previewHtml, /Resolved worktree location:/);
    assert.match(previewHtml, /\/repos\/worktrees\/porchpin/);
    assert.match(previewHtml, /Verified GitHub identity:<\/strong> setup-user/);
    assert.doesNotMatch(previewHtml, /\[object Object\]/);

    let prevented = 0;
    document.activeElement = next;
    await document.emit('keydown', {
        key: 'Tab',
        shiftKey: false,
        preventDefault: () => { prevented += 1; },
    });
    assert.equal(document.activeElement, document.elements.get('closeSetupWizardModal'));
    document.activeElement = document.elements.get('closeSetupWizardModal');
    await document.emit('keydown', {
        key: 'Tab',
        shiftKey: true,
        preventDefault: () => { prevented += 1; },
    });
    assert.equal(document.activeElement, next);
    assert.equal(prevented, 2);

    await next.emit('click');
    const saveRequest = fetchCalls[4];
    assert.equal(saveRequest[0], '/control/setup/save');
    assert.equal(JSON.parse(saveRequest[1].body).create_labels, true);
    assert.equal(JSON.parse(saveRequest[1].body).replace_existing, false);
    assert.match(document.elements.get('setupContent').innerHTML, /Setup Complete!/);
    assert.match(
        document.elements.get('setupContent').innerHTML,
        /Configured pipeline:<\/strong> worker → reviewer\/rework → tech lead/,
    );
    assert.match(
        document.elements.get('setupContent').innerHTML,
        /Optional capabilities to consider later/,
    );
    assert.match(document.elements.get('setupContent').innerHTML, /Specialized routing/);
    assert.match(document.elements.get('setupContent').innerHTML, /Other AI providers/);
    assert.match(document.elements.get('setupContent').innerHTML, /E2E runner/);
    assert.match(document.elements.get('setupContent').innerHTML, /Merge queue/);
    assert.match(document.elements.get('setupContent').innerHTML, /Goal pilot/);
    assert.match(
        document.elements.get('setupContent').innerHTML,
        /Tech-lead health automation/,
    );
    assert.equal(loadReposCalls, 1);

    wizard.close();
    assert.equal(modal.getAttribute('aria-hidden'), 'true');
    assert.equal(document.background.inert, false);
    assert.equal(trigger.focusCount, 1);
});

test('GHES setup keeps credential creation links on the configured host', async () => {
    const document = fakeDocument();
    const responses = [
        jsonResponse({
            all_ok: true,
            checks: { git: { ok: true, detail: 'git version 2' } },
            agent_checks: [],
        }),
        jsonResponse(repositoryDetection({
            github_authorization: {
                authorization: {
                    kind: 'detected',
                    api_url: 'https://github.example/api/v3',
                    http_timeout_seconds: 47,
                },
                configured_kind: 'detected',
                inline_token_migration_required: true,
            },
        })),
    ];
    const wizard = createSetupWizard({
        document,
        fetch: async () => responses.shift(),
        escapeHtml: (value) => String(value),
        loadRepos: async () => {},
        setupCommands,
    });
    wizard.bind();
    await wizard.open('/repos/porchpin');

    const next = document.elements.get('setupWizardNext');
    await next.emit('click');
    document.elements.set(
        'setupRepoName',
        document.makeElement({ value: 'owner/porchpin' }),
    );
    document.elements.set(
        'setupAgentLabel',
        document.makeElement({ value: 'agent:dev' }),
    );
    document.elements.set(
        'setupWorkerModel',
        document.makeElement({ value: 'sonnet' }),
    );
    await next.emit('click');

    const authorizationHtml = document.elements.get('setupContent').innerHTML;
    assert.match(
        authorizationHtml,
        /href="https:\/\/github\.example\/settings\/personal-access-tokens\/new\?/,
    );
    assert.match(
        authorizationHtml,
        /href="https:\/\/github\.example\/settings\/apps\/new"/,
    );
    assert.doesNotMatch(
        authorizationHtml,
        /https:\/\/github\.com\/settings\//,
    );
});

test('focus trap excludes controls beneath hidden or inert ancestors', async () => {
    const document = fakeDocument();
    const wizard = createSetupWizard({
        document,
        fetch: async () => jsonResponse({
            all_ok: true,
            checks: { git: { ok: true, detail: 'git version 2' } },
            agent_checks: [],
        }),
        escapeHtml: (value) => String(value),
        loadRepos: async () => {},
        setupCommands,
    });
    wizard.bind();
    await wizard.open('/repos/porchpin');

    const visibleFirst = document.makeElement();
    const visibleLast = document.makeElement();
    const hiddenPanel = document.makeElement();
    hiddenPanel.hidden = true;
    hiddenPanel.inert = true;
    const hiddenControl = document.makeElement({ parentElement: hiddenPanel });
    const modal = document.elements.get('setupWizardModal');
    modal.focusableChildren = [visibleFirst, visibleLast, hiddenControl];
    document.activeElement = visibleFirst;
    let prevented = 0;

    await document.emit('keydown', {
        key: 'Tab',
        shiftKey: true,
        preventDefault: () => { prevented += 1; },
    });

    assert.equal(document.activeElement, visibleLast);
    assert.equal(hiddenControl.focusCount, 0);
    assert.equal(prevented, 1);
});

test('undetected validation gates block forward progress until both are entered', async () => {
    const document = fakeDocument();
    const responses = [
        jsonResponse({
            all_ok: true,
            checks: { git: { ok: true, detail: 'git version 2' } },
            agent_checks: [],
        }),
        jsonResponse(repositoryDetection({
            validation_defaults: {
                quick_command: null,
                publish_command: null,
                source: 'Enter quick and publish commands before continuing.',
            },
        })),
    ];
    const wizard = createSetupWizard({
        document,
        fetch: async () => responses.shift(),
        escapeHtml: (value) => String(value),
        loadRepos: async () => {},
        setupCommands,
    });
    wizard.bind();
    await wizard.open('/repos/porchpin');

    const next = document.elements.get('setupWizardNext');
    await next.emit('click');
    const quick = document.elements.get('setupValidationQuickCommand');
    const publish = document.elements.get('setupValidationPublishCommand');
    assert.equal(next.disabled, true);

    quick.value = 'make test';
    await quick.emit('input');
    assert.equal(next.disabled, true);
    publish.value = 'make validate';
    await publish.emit('input');
    assert.equal(next.disabled, false);
});

test('partial save failure renders applied mutations and requires a new preview', async () => {
    const document = fakeDocument();
    const fetchCalls = [];
    const detectedRepo = repositoryDetection();
    const responses = [
        jsonResponse({
            all_ok: true,
            checks: { git: { ok: true, detail: 'git version 2' } },
        }),
        jsonResponse(detectedRepo),
        githubVerificationResponse(),
        jsonResponse({
            yaml: 'repo:\n  name: owner/porchpin\n',
            worktree_base: '/repos/worktrees/porchpin',
            github_authorization: githubPreviewSummary(),
            files: [{
                path: '/repos/porchpin/.issue-orchestrator/config/default.yaml',
                action: 'create',
            }],
        }),
        jsonResponse({
            error: 'repository_setup_failed',
            stage: 'labels',
            detail: 'GitHub unavailable',
            applied_files: [
                '/repos/porchpin/.issue-orchestrator/config/default.yaml',
            ],
            created_labels: ['agent:dev'],
        }, false),
        jsonResponse(detectedRepo),
    ];
    const fetch = async (...args) => {
        fetchCalls.push(args);
        return responses.shift();
    };
    const wizard = createSetupWizard({
        document,
        fetch,
        escapeHtml: (value) => String(value),
        loadRepos: async () => {},
        setupCommands,
    });
    wizard.bind();
    await wizard.open('/repos/porchpin');

    const next = document.elements.get('setupWizardNext');
    await next.emit('click');
    document.elements.set(
        'setupRepoName',
        document.makeElement({ value: 'owner/porchpin' }),
    );
    document.elements.set(
        'setupAgentLabel',
        document.makeElement({ value: 'agent:dev' }),
    );
    document.elements.set('setupWorkerModel', document.makeElement({ value: 'sonnet' }));
    document.elements.set(
        'setupConfigureTechLead',
        document.makeElement({ checked: true }),
    );
    await next.emit('click');
    await document.elements.get('setupVerifyDetectedGithub').emit('click');
    await next.emit('click');
    await next.emit('click');

    const failureHtml = document.elements.get('setupContent').innerHTML;
    assert.match(failureHtml, /Setup did not complete/);
    assert.match(failureHtml, /GitHub unavailable/);
    assert.match(failureHtml, /Failed stage:<\/strong> labels/);
    assert.match(failureHtml, /Files already written/);
    assert.match(failureHtml, /default\.yaml/);
    assert.match(failureHtml, /Labels already created/);
    assert.match(failureHtml, /agent:dev/);
    assert.doesNotMatch(failureHtml, />repository_setup_failed</);
    assert.equal(next.disabled, true);

    await next.emit('click');
    assert.equal(fetchCalls.length, 5);

    await document.elements.get('setupWizardBack').emit('click');
    assert.equal(fetchCalls.length, 5);
    assert.match(document.elements.get('setupContent').innerHTML, /GitHub Authorization/);
    assert.equal(next.disabled, true);
});

test('detect failure disables forward click and keyboard activation', async () => {
    const document = fakeDocument();
    const fetchCalls = [];
    const responses = [
        jsonResponse({
            all_ok: true,
            checks: { git: { ok: true, detail: 'git version 2' } },
        }),
        jsonResponse({ detail: 'repository unavailable' }, false),
    ];
    const wizard = createSetupWizard({
        document,
        fetch: async (...args) => {
            fetchCalls.push(args);
            return responses.shift();
        },
        escapeHtml: (value) => String(value),
        loadRepos: async () => {},
        setupCommands,
    });
    wizard.bind();
    await wizard.open('/repos/porchpin');

    const next = document.elements.get('setupWizardNext');
    await next.emit('click', { detail: 1 });

    assert.equal(next.disabled, true);
    assert.match(document.elements.get('setupContent').innerHTML, /repository unavailable/);
    assert.match(document.elements.get('setupContent').innerHTML, /Use <strong>Back<\/strong>/);

    await next.emit('click', { detail: 1 });
    await next.emit('click', { detail: 0 });
    assert.equal(fetchCalls.length, 2);
});

test('preview failure disables save click and keyboard activation', async () => {
    const document = fakeDocument();
    const fetchCalls = [];
    const responses = [
        jsonResponse({
            all_ok: true,
            checks: { git: { ok: true, detail: 'git version 2' } },
        }),
        jsonResponse(repositoryDetection()),
        githubVerificationResponse(),
        jsonResponse({ detail: 'preview unavailable' }, false),
    ];
    const wizard = createSetupWizard({
        document,
        fetch: async (...args) => {
            fetchCalls.push(args);
            return responses.shift();
        },
        escapeHtml: (value) => String(value),
        loadRepos: async () => {},
        setupCommands,
    });
    wizard.bind();
    await wizard.open('/repos/porchpin');

    const next = document.elements.get('setupWizardNext');
    await next.emit('click');
    document.elements.set(
        'setupRepoName',
        document.makeElement({ value: 'owner/porchpin' }),
    );
    document.elements.set(
        'setupAgentLabel',
        document.makeElement({ value: 'agent:dev' }),
    );
    document.elements.set('setupWorkerModel', document.makeElement({ value: 'sonnet' }));
    document.elements.set(
        'setupConfigureTechLead',
        document.makeElement({ checked: true }),
    );
    await next.emit('click');
    await document.elements.get('setupVerifyDetectedGithub').emit('click');
    await next.emit('click');

    assert.equal(next.disabled, true);
    assert.match(document.elements.get('setupContent').innerHTML, /preview unavailable/);
    assert.match(document.elements.get('setupContent').innerHTML, /Use <strong>Back<\/strong>/);

    await next.emit('click', { detail: 1 });
    await next.emit('click', { detail: 0 });
    assert.equal(fetchCalls.length, 4);
});

test('pending detect cannot overwrite prerequisites after Back', async () => {
    const document = fakeDocument();
    const pendingDetect = deferred();
    let prereqCall = 0;
    const wizard = createSetupWizard({
        document,
        fetch: async (url) => {
            if (url.startsWith('/control/setup/prereqs')) {
                prereqCall += 1;
                return jsonResponse({
                    all_ok: true,
                    checks: {
                        git: {
                            ok: true,
                            detail: prereqCall === 1 ? 'first check' : 'back check',
                        },
                    },
                });
            }
            if (url.startsWith('/control/setup/detect')) {
                return pendingDetect.promise;
            }
            throw new Error(`Unexpected request: ${url}`);
        },
        escapeHtml: (value) => String(value),
        loadRepos: async () => {},
        setupCommands,
    });
    wizard.bind();
    await wizard.open('/repos/porchpin');

    const next = document.elements.get('setupWizardNext');
    const detectClick = next.emit('click');
    await document.elements.get('setupWizardBack').emit('click');

    pendingDetect.resolve(jsonResponse(repositoryDetection()));
    await detectClick;

    const content = document.elements.get('setupContent').innerHTML;
    assert.match(content, /Prerequisites/);
    assert.match(content, /back check/);
    assert.doesNotMatch(content, /Configuration/);
    assert.equal(document.elements.get('setupWizardBack').style.display, 'none');
    assert.equal(next.textContent, 'Next');
});

test('pending preview cannot render after close and reopen for another repository', async () => {
    const document = fakeDocument();
    const pendingPreview = deferred();
    const wizard = createSetupWizard({
        document,
        fetch: async (url) => {
            if (url.startsWith('/control/setup/prereqs')) {
                const repo = new URL(`http://test${url}`).searchParams.get('repo_root');
                return jsonResponse({
                    all_ok: true,
                    checks: { git: { ok: true, detail: `prerequisites for ${repo}` } },
                });
            }
            if (url.startsWith('/control/setup/detect')) {
                return jsonResponse(repositoryDetection());
            }
            if (url === '/control/setup/github-auth/verify') {
                return githubVerificationResponse();
            }
            if (url === '/control/setup/preview') {
                return pendingPreview.promise;
            }
            throw new Error(`Unexpected request: ${url}`);
        },
        escapeHtml: (value) => String(value),
        loadRepos: async () => {},
        setupCommands,
    });
    wizard.bind();
    await wizard.open('/repos/porchpin');

    const next = document.elements.get('setupWizardNext');
    await next.emit('click');
    document.elements.set(
        'setupRepoName',
        document.makeElement({ value: 'owner/porchpin' }),
    );
    document.elements.set(
        'setupAgentLabel',
        document.makeElement({ value: 'agent:dev' }),
    );
    document.elements.set('setupWorkerModel', document.makeElement({ value: 'sonnet' }));
    document.elements.set(
        'setupConfigureTechLead',
        document.makeElement({ checked: true }),
    );
    await next.emit('click');
    await document.elements.get('setupVerifyDetectedGithub').emit('click');
    const previewClick = next.emit('click');

    wizard.close();
    await wizard.open('/repos/other');
    pendingPreview.resolve(jsonResponse({
        yaml: 'repo:\n  name: owner/porchpin\n',
        worktree_base: '/repos/worktrees/porchpin',
        github_authorization: githubPreviewSummary(),
        files: [],
    }));
    await previewClick;

    const content = document.elements.get('setupContent').innerHTML;
    assert.match(content, /Prerequisites/);
    assert.match(content, /prerequisites for \/repos\/other/);
    assert.doesNotMatch(content, /Preview Configuration/);
    assert.equal(document.elements.get('setupWizardBack').style.display, 'none');
});

test('pending save cannot complete after close and reopen for another repository', async () => {
    const document = fakeDocument();
    const pendingSave = deferred();
    let loadReposCalls = 0;
    const wizard = createSetupWizard({
        document,
        fetch: async (url) => {
            if (url.startsWith('/control/setup/prereqs')) {
                const repo = new URL(`http://test${url}`).searchParams.get('repo_root');
                return jsonResponse({
                    all_ok: true,
                    checks: { git: { ok: true, detail: `prerequisites for ${repo}` } },
                });
            }
            if (url.startsWith('/control/setup/detect')) {
                return jsonResponse(repositoryDetection());
            }
            if (url === '/control/setup/github-auth/verify') {
                return githubVerificationResponse();
            }
            if (url === '/control/setup/preview') {
                return jsonResponse({
                    yaml: 'repo:\n  name: owner/porchpin\n',
                    worktree_base: '/repos/worktrees/porchpin',
                    github_authorization: githubPreviewSummary(),
                    files: [],
                });
            }
            if (url === '/control/setup/save') {
                return pendingSave.promise;
            }
            throw new Error(`Unexpected request: ${url}`);
        },
        escapeHtml: (value) => String(value),
        loadRepos: async () => { loadReposCalls += 1; },
        setupCommands,
    });
    wizard.bind();
    await wizard.open('/repos/porchpin');

    const next = document.elements.get('setupWizardNext');
    await next.emit('click');
    document.elements.set(
        'setupRepoName',
        document.makeElement({ value: 'owner/porchpin' }),
    );
    document.elements.set(
        'setupAgentLabel',
        document.makeElement({ value: 'agent:dev' }),
    );
    document.elements.set('setupWorkerModel', document.makeElement({ value: 'sonnet' }));
    document.elements.set(
        'setupConfigureTechLead',
        document.makeElement({ checked: true }),
    );
    await next.emit('click');
    await document.elements.get('setupVerifyDetectedGithub').emit('click');
    await next.emit('click');
    const saveClick = next.emit('click');

    wizard.close();
    await wizard.open('/repos/other');
    pendingSave.resolve(jsonResponse({
        status: 'saved',
        created_files: ['/repos/porchpin/.issue-orchestrator/config/default.yaml'],
        created_labels: [],
    }));
    await saveClick;

    const content = document.elements.get('setupContent').innerHTML;
    assert.match(content, /Prerequisites/);
    assert.match(content, /prerequisites for \/repos\/other/);
    assert.doesNotMatch(content, /Setup Complete!/);
    assert.equal(document.elements.get('setupWizardBack').style.display, 'none');
    assert.equal(loadReposCalls, 0);
});

test('existing explicit tech-lead disable wins over retained agent configuration', async () => {
    const document = fakeDocument();
    const responses = [
        jsonResponse({
            all_ok: true,
            checks: { git: { ok: true, detail: 'git version 2' } },
        }),
        jsonResponse(repositoryDetection({
            existing_config: {
                agents: { 'agent:tech-lead': { model: 'sonnet' } },
                review: { tech_lead_review_agent: 'agent:tech-lead' },
                tech_lead: { enabled: false },
            },
        })),
    ];
    const wizard = createSetupWizard({
        document,
        fetch: async () => responses.shift(),
        escapeHtml: (value) => String(value),
        loadRepos: async () => {},
        setupCommands,
    });
    wizard.bind();
    await wizard.open('/repos/porchpin');

    await document.elements.get('setupWizardNext').emit('click');

    const configureHtml = document.elements.get('setupContent').innerHTML;
    assert.match(configureHtml, /id="setupConfigureTechLead"\s*>/);
    assert.doesNotMatch(configureHtml, /id="setupConfigureTechLead" checked/);
});

test('existing config preview requires explicit replacement confirmation before save', async () => {
    const document = fakeDocument();
    const fetchCalls = [];
    const responses = [
        jsonResponse({
            all_ok: true,
            checks: { git: { ok: true, detail: 'git version 2' } },
        }),
        jsonResponse(repositoryDetection({
            existing_config: {
                repo: { name: 'owner/porchpin' },
                agents: {
                    'agent:reviewer': { model: 'haiku' },
                    'agent:backend': { model: 'opus' },
                    'agent:tech-lead': { model: 'sonnet' },
                },
                review: {
                    default: 'agent:reviewer',
                    tech_lead_review_agent: 'agent:tech-lead',
                },
            },
        })),
        githubVerificationResponse(),
        jsonResponse({
            yaml: 'repo:\n  name: owner/porchpin\n',
            worktree_base: '/repos/worktrees/porchpin',
            github_authorization: githubPreviewSummary(),
            files: [{
                path: '/repos/porchpin/.issue-orchestrator/config/default.yaml',
                action: 'overwrite',
            }],
        }),
        jsonResponse({
            status: 'saved',
            config_path: '/repos/porchpin/.issue-orchestrator/config/default.yaml',
            created_files: [
                '/repos/porchpin/.issue-orchestrator/config/default.yaml',
            ],
            created_labels: [],
        }),
    ];
    const fetch = async (...args) => {
        fetchCalls.push(args);
        return responses.shift();
    };
    const wizard = createSetupWizard({
        document,
        fetch,
        escapeHtml: (value) => String(value),
        loadRepos: async () => {},
        setupCommands,
    });
    wizard.bind();
    await wizard.open('/repos/porchpin');

    const next = document.elements.get('setupWizardNext');
    await next.emit('click');
    const configureHtml = document.elements.get('setupContent').innerHTML;
    assert.match(configureHtml, /value="agent:backend"/);
    assert.match(configureHtml, /value="opus" selected/);
    assert.match(configureHtml, /id="setupConfigureTechLead" checked/);

    document.elements.set(
        'setupRepoName',
        document.makeElement({ value: 'owner/porchpin' }),
    );
    document.elements.set(
        'setupAgentLabel',
        document.makeElement({ value: 'agent:backend' }),
    );
    document.elements.set('setupWorkerModel', document.makeElement({ value: 'opus' }));
    document.elements.set(
        'setupConfigureTechLead',
        document.makeElement({ checked: true }),
    );
    await next.emit('click');
    await document.elements.get('setupVerifyDetectedGithub').emit('click');
    await next.emit('click');

    const previewHtml = document.elements.get('setupContent').innerHTML;
    assert.match(previewHtml, /Planned file changes:/);
    assert.match(previewHtml, /<strong>Replace:<\/strong>/);
    assert.match(previewHtml, /Settings and agents\s+not shown/);
    assert.equal(next.disabled, true);

    const confirmation = document.elements.get('setupConfirmReplace');
    confirmation.checked = true;
    await confirmation.emit('change');
    assert.equal(next.disabled, false);

    await next.emit('click');
    const saveBody = JSON.parse(fetchCalls[4][1].body);
    assert.equal(saveBody.replace_existing, true);
    assert.equal(saveBody.worker_agent_label, 'agent:backend');
});
