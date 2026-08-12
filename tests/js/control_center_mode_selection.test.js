const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadSelectionHelpers() {
    const values = new Map();
    const localStorage = {
        getItem: key => values.has(key) ? values.get(key) : null,
        setItem: (key, value) => values.set(key, value),
    };
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/control_center.js'),
        'utf8',
    ).split("document.addEventListener('DOMContentLoaded'")[0];
    const context = {
        URL,
        console,
        localStorage,
        window: {
            addEventListener() {},
            matchMedia: () => ({ addEventListener() {}, matches: false }),
            setTimeout() {},
        },
        document: { addEventListener() {} },
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        setInterval() {},
        setTimeout() {},
        clearTimeout() {},
    };
    vm.createContext(context);
    vm.runInContext(source, context);
    context.escapeHtml = value => String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;');
    return { context, localStorage };
}

test('saved config defaults remain independent for each mode', () => {
    const { context } = loadSelectionHelpers();

    context.setDefaultConfigForRepo('/repo', 'claude.yaml', 'claude');
    context.setDefaultConfigForRepo('/repo', 'codex.yaml', 'codex');

    assert.equal(context.getDefaultConfig('/repo', 'claude'), 'claude.yaml');
    assert.equal(context.getDefaultConfig('/repo', 'codex'), 'codex.yaml');
    context.clearDefaultConfigForRepo('/repo', 'claude');
    assert.equal(context.getDefaultConfig('/repo', 'claude'), null);
    assert.equal(context.getDefaultConfig('/repo', 'codex'), 'codex.yaml');
});

test('running repository selection uses active lock identity', () => {
    const { context } = loadSelectionHelpers();
    const repo = {
        path: '/repo',
        status: { state: 'running' },
        modes: ['default', 'codex'],
        mode_configs: {
            default: ['main.yaml'],
            codex: ['main.yaml'],
        },
        selected_mode: 'default',
        selected_config: 'main.yaml',
        active_mode: 'codex',
        active_config: 'main.yaml',
    };

    assert.equal(context.getSelectedRepoMode(repo, 'default'), 'codex');
    assert.equal(context.getSelectedRepoConfig(repo, null, 'codex'), 'main.yaml');
});

test('server desired selection wins over stale browser defaults after stop', () => {
    const { context } = loadSelectionHelpers();
    context.setDefaultModeForRepo('/repo', 'default');
    context.setDefaultConfigForRepo('/repo', 'default-one.yaml', 'default');
    const repo = repository({
        selected_mode: 'codex',
        selected_config: 'codex-two.yaml',
    });

    assert.equal(context.getSelectedRepoMode(repo, 'default'), 'codex');
    assert.equal(
        context.getSelectedRepoConfig(repo, 'codex-one.yaml', 'codex'),
        'codex-two.yaml',
    );
    assert.equal(context.getRepoConfigIssue(repo, 'codex-two.yaml', 'codex'), null);
});

test('activity selection does not reuse another repository config', () => {
    const { context } = loadSelectionHelpers();
    vm.runInContext(
        "state.activeRepo = { path: '/first', mode: 'codex', config: 'only-first.yaml' }",
        context,
    );
    const second = {
        path: '/second',
        status: { state: 'stopped' },
        modes: ['default'],
        mode_configs: { default: ['second.yaml'] },
        selected_mode: 'default',
        selected_config: 'second.yaml',
    };

    const selection = context.resolveRepoActivitySelection(second);

    assert.equal(selection.mode, 'default');
    assert.equal(selection.config, 'second.yaml');
});

function repository(overrides = {}) {
    return {
        path: '/repo',
        name: 'repo',
        exists: true,
        modes: ['default', 'codex'],
        mode_configs: {
            default: ['default-one.yaml', 'default-two.yaml'],
            codex: ['codex-one.yaml', 'codex-two.yaml'],
        },
        selected_mode: 'default',
        selected_config: 'default-one.yaml',
        status: { state: 'stopped' },
        ...overrides,
    };
}

function genericElement() {
    return {
        innerHTML: '',
        textContent: '',
        disabled: false,
        dataset: {},
        style: {},
        classList: {
            add() {},
            remove() {},
            contains() { return false; },
            toggle() { return false; },
        },
        querySelectorAll() { return []; },
        addEventListener() {},
        setAttribute() {},
        focus() {},
        contentWindow: { postMessage() {} },
    };
}

test('repository card and activity menu lock mode controls while running or paused', () => {
    for (const status of [
        { state: 'running', startup_status: 'complete' },
        { state: 'running', paused: true, startup_status: 'complete' },
    ]) {
        const { context } = loadSelectionHelpers();
        const repo = repository({
            status,
            active_mode: 'codex',
            active_config: 'codex-one.yaml',
            dashboard_url: 'http://127.0.0.1:8080',
        });
        const html = context.renderRepoCard(repo);
        assert.match(html, /data-action="select-mode"[^>]*disabled/);
        assert.match(html, /data-action="select-config"[^>]*disabled/);

        const elements = new Map();
        context.document.getElementById = id => {
            if (!elements.has(id)) elements.set(id, genericElement());
            return elements.get(id);
        };
        vm.runInContext(`state.repos = [${JSON.stringify(repo)}]`, context);
        context.loadActivityView('/repo');
        assert.equal(elements.get('menuModeSelect').disabled, true);
        assert.equal(elements.get('menuConfigSelect').disabled, true);
    }
});

test('a repository configured only in a non-default mode can start', () => {
    const { context } = loadSelectionHelpers();
    const repo = repository({
        modes: ['codex'],
        mode_configs: { codex: ['main.yaml'] },
        selected_mode: 'default',
        selected_config: 'default.yaml',
    });

    const html = context.renderRepoCard(repo);

    assert.match(html, /Start engine/);
    assert.doesNotMatch(html, />Setup</);
    assert.doesNotMatch(html, /Needs Setup/);
});

test('changing the card mode persists and rerenders that mode config list', async () => {
    const { context } = loadSelectionHelpers();
    const repo = repository();
    const container = genericElement();
    let modeChange = null;
    const modeSelect = {
        addEventListener(type, callback) {
            if (type === 'change') modeChange = callback;
        },
    };
    container.querySelectorAll = selector => {
        if (selector === 'select[data-action="select-mode"]') return [modeSelect];
        return [];
    };
    context.document.getElementById = id => id === 'reposContent' ? container : genericElement();
    vm.runInContext(
        `state.repos = [${JSON.stringify(repo)}]; showToast = () => {};`,
        context,
    );
    context.renderRepos();
    assert.match(container.innerHTML, /default-one\.yaml/);
    assert.doesNotMatch(container.innerHTML, /codex-one\.yaml/);

    await modeChange({ currentTarget: { dataset: { path: '/repo' }, value: 'codex' } });

    assert.match(container.innerHTML, /codex-one\.yaml/);
    assert.doesNotMatch(container.innerHTML, /default-one\.yaml/);
});

test('activity menu mode dispatcher persists before updating active selection', async () => {
    const { context } = loadSelectionHelpers();
    const repo = repository();
    const calls = [];
    context.fetch = async (url, options) => {
        calls.push({ url, body: JSON.parse(options.body) });
        return { ok: true, json: async () => ({}) };
    };
    vm.runInContext(
        `state.repos = [${JSON.stringify(repo)}];
         state.activeRepo = {
             path: '/repo', config: 'default-one.yaml', mode: 'default', source: 'registered'
         };
         loadActivityView = () => {};
         showToast = () => {};`,
        context,
    );

    await context.handleMenuModeChange({ target: { value: 'codex' } });

    assert.deepEqual(calls, [{
        url: '/control/repos/select-config',
        body: {
            repo_root: '/repo',
            mode: 'codex',
            config_name: 'codex-one.yaml',
        },
    }]);
    const active = JSON.parse(vm.runInContext('JSON.stringify(state.activeRepo)', context));
    assert.equal(active.mode, 'codex');
    assert.equal(active.config, 'codex-one.yaml');
});

test('activity menu persist failure leaves active selection unchanged', async () => {
    const { context } = loadSelectionHelpers();
    const repo = repository();
    context.fetch = async () => ({
        ok: false,
        json: async () => ({ detail: 'engine running' }),
    });
    vm.runInContext(
        `state.repos = [${JSON.stringify(repo)}];
         state.activeRepo = {
             path: '/repo', config: 'default-one.yaml', mode: 'default', source: 'registered'
         };
         loadActivityView = () => { state._activityReloaded = true; };
         showToast = () => {};`,
        context,
    );

    const target = { value: 'codex' };
    await context.handleMenuModeChange({ target });

    const active = JSON.parse(vm.runInContext('JSON.stringify(state.activeRepo)', context));
    assert.equal(target.value, 'default');
    assert.equal(active.mode, 'default');
    assert.equal(active.config, 'default-one.yaml');
    assert.equal(vm.runInContext('state._activityReloaded', context), true);
});

test('card mode persist failure restores the prior selection', async () => {
    const { context } = loadSelectionHelpers();
    const repo = repository();
    const target = { dataset: { path: '/repo' }, value: 'codex' };
    context.fetch = async () => ({
        ok: false,
        json: async () => ({ detail: 'engine running' }),
    });
    vm.runInContext(
        `state.repos = [${JSON.stringify(repo)}];
         state.currentView = 'repositories';
         renderRepos = () => { state._rendered = true; };
         showToast = (message, severity) => { state._toast = [message, severity]; };`,
        context,
    );

    await context.handleRepoCardModeChange({ currentTarget: target });

    assert.equal(target.value, 'default');
    assert.equal(context.getDefaultMode('/repo'), null);
    assert.deepEqual(
        JSON.parse(vm.runInContext('JSON.stringify(state._toast)', context)),
        ['engine running', 'error'],
    );
    assert.equal(vm.runInContext('state._rendered', context), true);
});

test('card config persist failure restores the prior selection', async () => {
    const { context } = loadSelectionHelpers();
    const repo = repository();
    const target = {
        dataset: { path: '/repo' },
        value: 'default-two.yaml',
    };
    context.fetch = async () => ({
        ok: false,
        json: async () => ({ detail: 'engine running' }),
    });
    vm.runInContext(
        `state.repos = [${JSON.stringify(repo)}];
         state.currentView = 'repositories';
         renderRepos = () => { state._rendered = true; };
         showToast = (message, severity) => { state._toast = [message, severity]; };`,
        context,
    );

    await context.handleRepoCardConfigChange({ currentTarget: target });

    assert.equal(target.value, 'default-one.yaml');
    assert.equal(context.getDefaultConfig('/repo', 'default'), null);
    assert.deepEqual(
        JSON.parse(vm.runInContext('JSON.stringify(state._toast)', context)),
        ['engine running', 'error'],
    );
    assert.equal(vm.runInContext('state._rendered', context), true);
});

test('activity config persist failure restores the active selection', async () => {
    const { context } = loadSelectionHelpers();
    const repo = repository();
    const target = { value: 'default-two.yaml' };
    context.fetch = async () => ({
        ok: false,
        json: async () => ({ detail: 'engine running' }),
    });
    vm.runInContext(
        `state.repos = [${JSON.stringify(repo)}];
         state.activeRepo = {
             path: '/repo', config: 'default-one.yaml', mode: 'default', source: 'registered'
         };
         loadActivityView = () => { state._activityReloaded = true; };
         showToast = (message, severity) => { state._toast = [message, severity]; };`,
        context,
    );

    await context.handleMenuConfigChange({ target });

    const active = JSON.parse(vm.runInContext('JSON.stringify(state.activeRepo)', context));
    assert.equal(target.value, 'default-one.yaml');
    assert.equal(active.config, 'default-one.yaml');
    assert.equal(active.mode, 'default');
    assert.equal(vm.runInContext('state._activityReloaded', context), true);
});

test('start sends the exact selected mode command payload', async () => {
    const { context } = loadSelectionHelpers();
    const calls = [];
    const repo = repository({
        modes: ['codex'],
        mode_configs: { codex: ['codex.yaml'] },
        selected_mode: 'codex',
        selected_config: 'codex.yaml',
    });
    context.fetch = async (url, options) => {
        calls.push({ url, options });
        return { ok: true, json: async () => ({}) };
    };
    vm.runInContext(
        `state.repos = [${JSON.stringify(repo)}];
         renderRepos = () => {};
         selectRepo = () => {};
         waitForRepoToBeReady = async () => ({});
         loadRepos = async () => {};
         showToast = () => {};`,
        context,
    );

    await context.startRepo('/repo', 'codex.yaml', true, 'codex');

    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, '/control/orchestrator/start');
    assert.deepEqual(JSON.parse(calls[0].options.body), {
        repo_root: '/repo',
        config_name: 'codex.yaml',
        mode: 'codex',
        start_paused: true,
    });
});
