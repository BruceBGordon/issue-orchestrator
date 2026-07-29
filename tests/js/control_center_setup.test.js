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
} = {}) {
    const classes = new Set();
    const listeners = new Map();
    const attributes = new Map();
    const node = {
        dataset,
        value,
        checked,
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
        'setupModel',
        'setupConfigureTechLead',
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
        },
    });
    const steps = [1, 2, 3].map((step) => makeElement({
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

test('setup request contract defaults tech lead on and supports explicit opt-out', () => {
    const enabled = setupCommands.buildSetupPreviewRequest('/repos/porchpin', {
        repoName: 'owner/porchpin',
        workerAgentLabel: 'agent:dev',
        model: 'sonnet',
    });
    const disabled = setupCommands.buildSetupSaveRequest('/repos/porchpin', {
        repoName: 'owner/porchpin',
        workerAgentLabel: 'agent:dev',
        model: 'sonnet',
        configureTechLead: false,
    }, {
        createLabels: false,
    });

    assert.equal(enabled.endpoint, '/control/setup/preview');
    assert.equal(enabled.body.configure_tech_lead, true);
    assert.equal(disabled.endpoint, '/control/setup/save');
    assert.equal(disabled.body.configure_tech_lead, false);
    assert.equal(disabled.body.create_prompts, true);
    assert.equal(disabled.body.create_labels, false);
    assert.equal(disabled.body.replace_existing, false);
});

test('setup request contract rejects empty and tech-lead worker labels', () => {
    for (const workerAgentLabel of ['agent:', 'agent:tech-lead']) {
        assert.throws(
            () => setupCommands.buildSetupPreviewRequest('/repos/porchpin', {
                repoName: 'owner/porchpin',
                workerAgentLabel,
                model: 'sonnet',
            }),
            /workerAgentLabel must match/,
        );
    }
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
        jsonResponse({
            repo_root: '/repos/porchpin',
            repo: 'owner/porchpin',
            existing_config: null,
        }),
        jsonResponse({
            yaml: 'repo:\n  name: owner/porchpin\n',
            files: [
                { path: '/repos/porchpin/.issue-orchestrator/config/default.yaml', action: 'create' },
                { path: '/repos/porchpin/.io/tech-lead.md', action: 'create', type: 'prompt' },
            ],
        }),
        jsonResponse({
            status: 'saved',
            config_path: '/repos/porchpin/.issue-orchestrator/config/default.yaml',
            created_files: [
                '/repos/porchpin/.issue-orchestrator/config/default.yaml',
                '/repos/porchpin/.io/tech-lead.md',
            ],
            created_labels: ['agent:dev', 'agent:tech-lead'],
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
    assert.match(configureHtml, /id="setupConfigureTechLead" checked/);
    assert.match(configureHtml, /Configure a tech-lead agent\?/);
    assert.match(configureHtml, /Enabled by default/);

    document.elements.set(
        'setupRepoName',
        document.makeElement({ value: 'owner/porchpin' }),
    );
    document.elements.set(
        'setupAgentLabel',
        document.makeElement({ value: 'agent:dev' }),
    );
    document.elements.set('setupModel', document.makeElement({ value: 'sonnet' }));
    document.elements.set(
        'setupConfigureTechLead',
        document.makeElement({ checked: true }),
    );
    await next.emit('click');

    const previewRequest = fetchCalls[2];
    assert.equal(previewRequest[0], '/control/setup/preview');
    assert.equal(JSON.parse(previewRequest[1].body).configure_tech_lead, true);
    const previewHtml = document.elements.get('setupContent').innerHTML;
    assert.match(previewHtml, /\/repos\/porchpin\/\.io\/tech-lead\.md/);
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
    const saveRequest = fetchCalls[3];
    assert.equal(saveRequest[0], '/control/setup/save');
    assert.equal(JSON.parse(saveRequest[1].body).create_labels, true);
    assert.equal(JSON.parse(saveRequest[1].body).replace_existing, false);
    assert.match(document.elements.get('setupContent').innerHTML, /Setup Complete!/);
    assert.equal(loadReposCalls, 1);

    wizard.close();
    assert.equal(modal.getAttribute('aria-hidden'), 'true');
    assert.equal(document.background.inert, false);
    assert.equal(trigger.focusCount, 1);
});

test('partial save failure renders applied mutations and requires a new preview', async () => {
    const document = fakeDocument();
    const fetchCalls = [];
    const detectedRepo = {
        repo_root: '/repos/porchpin',
        repo: 'owner/porchpin',
        existing_config: null,
    };
    const responses = [
        jsonResponse({
            all_ok: true,
            checks: { git: { ok: true, detail: 'git version 2' } },
        }),
        jsonResponse(detectedRepo),
        jsonResponse({
            yaml: 'repo:\n  name: owner/porchpin\n',
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
    document.elements.set('setupModel', document.makeElement({ value: 'sonnet' }));
    document.elements.set(
        'setupConfigureTechLead',
        document.makeElement({ checked: true }),
    );
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
    assert.equal(fetchCalls.length, 4);

    await document.elements.get('setupWizardBack').emit('click');
    assert.equal(fetchCalls.length, 5);
    assert.match(document.elements.get('setupContent').innerHTML, /Configuration/);
    assert.equal(next.disabled, false);
});

test('existing config preview requires explicit replacement confirmation before save', async () => {
    const document = fakeDocument();
    const fetchCalls = [];
    const responses = [
        jsonResponse({
            all_ok: true,
            checks: { git: { ok: true, detail: 'git version 2' } },
        }),
        jsonResponse({
            repo_root: '/repos/porchpin',
            repo: 'owner/porchpin',
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
        }),
        jsonResponse({
            yaml: 'repo:\n  name: owner/porchpin\n',
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

    document.elements.set(
        'setupRepoName',
        document.makeElement({ value: 'owner/porchpin' }),
    );
    document.elements.set(
        'setupAgentLabel',
        document.makeElement({ value: 'agent:backend' }),
    );
    document.elements.set('setupModel', document.makeElement({ value: 'opus' }));
    document.elements.set(
        'setupConfigureTechLead',
        document.makeElement({ checked: true }),
    );
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
    const saveBody = JSON.parse(fetchCalls[3][1].body);
    assert.equal(saveBody.replace_existing, true);
    assert.equal(saveBody.worker_agent_label, 'agent:backend');
});
