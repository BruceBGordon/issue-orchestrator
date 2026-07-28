const test = require('node:test');
const assert = require('node:assert');

const setupCommands = require(
    '../../src/issue_orchestrator/static/js/control_center_setup_commands.js',
);
const createSetupWizard = require(
    '../../src/issue_orchestrator/static/js/control_center_setup.js',
);

function fakeElement({ dataset = {}, value = '', checked = false } = {}) {
    const classes = new Set();
    const listeners = new Map();
    const attributes = new Map();
    return {
        dataset,
        value,
        checked,
        disabled: false,
        innerHTML: '',
        textContent: '',
        style: {},
        focusCount: 0,
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
        focus() {
            this.focusCount += 1;
        },
    };
}

function fakeDocument() {
    const elements = new Map([
        ['setupWizardModal', fakeElement()],
        ['setupWizardBack', fakeElement()],
        ['setupWizardNext', fakeElement()],
        ['setupContent', fakeElement()],
        ['closeSetupWizardModal', fakeElement()],
        ['setupWizardCancel', fakeElement()],
    ]);
    const steps = [1, 2, 3].map((step) => fakeElement({ dataset: { step: String(step) } }));
    const listeners = new Map();
    return {
        elements,
        getElementById: (id) => elements.get(id) || null,
        querySelectorAll: (selector) => selector === '.setup-step' ? steps : [],
        addEventListener: (name, listener) => listeners.set(name, listener),
        emit: async (name, event = {}) => listeners.get(name)?.(event),
    };
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
});

test('setup modal opens from the command, asks the default-on question, and previews it', async () => {
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
            existing_config: null,
        }),
        jsonResponse({
            yaml: 'repo:\n  name: owner/porchpin\n',
            files: [
                { path: '/repos/porchpin/.issue-orchestrator/config/default.yaml', action: 'create' },
                { path: '/repos/porchpin/.io/tech-lead.md', action: 'create', type: 'prompt' },
            ],
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
    const trigger = fakeElement();

    await setupCommands.runOpenSetupCommand(
        setupCommands.buildOpenSetupCommand('/repos/porchpin'),
        wizard,
        trigger,
    );

    const modal = document.elements.get('setupWizardModal');
    assert.equal(modal.classList.contains('active'), true);
    assert.equal(modal.getAttribute('aria-hidden'), 'false');
    assert.equal(document.elements.get('closeSetupWizardModal').focusCount, 1);
    assert.equal(fetchCalls[0][0], '/control/setup/prereqs?repo_root=%2Frepos%2Fporchpin');

    const next = document.elements.get('setupWizardNext');
    await next.emit('click');
    const configureHtml = document.elements.get('setupContent').innerHTML;
    assert.match(configureHtml, /id="setupConfigureTechLead" checked/);
    assert.match(configureHtml, /Configure a tech-lead agent\?/);
    assert.match(configureHtml, /Enabled by default/);

    document.elements.set('setupRepoName', fakeElement({ value: 'owner/porchpin' }));
    document.elements.set('setupAgentLabel', fakeElement({ value: 'agent:dev' }));
    document.elements.set('setupModel', fakeElement({ value: 'sonnet' }));
    document.elements.set('setupConfigureTechLead', fakeElement({ checked: true }));
    await next.emit('click');

    const previewRequest = fetchCalls[2];
    assert.equal(previewRequest[0], '/control/setup/preview');
    assert.equal(JSON.parse(previewRequest[1].body).configure_tech_lead, true);
    const previewHtml = document.elements.get('setupContent').innerHTML;
    assert.match(previewHtml, /\/repos\/porchpin\/\.io\/tech-lead\.md/);
    assert.doesNotMatch(previewHtml, /\[object Object\]/);

    wizard.close();
    assert.equal(modal.getAttribute('aria-hidden'), 'true');
    assert.equal(trigger.focusCount, 1);
});
