const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadAuditHelpers(fetchImpl) {
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/control_center.js'),
        'utf8',
    ).split("document.addEventListener('DOMContentLoaded'")[0];
    const context = {
        URL,
        console,
        localStorage: { getItem: () => null, setItem() {} },
        window: {
            addEventListener() {},
            matchMedia: () => ({ addEventListener() {}, matches: false }),
            setTimeout() {},
        },
        document: { addEventListener() {} },
        fetch: fetchImpl,
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
    return context;
}

function auditPayload(overrides = {}) {
    return {
        worktrees: [],
        cleanup_candidates: [],
        stale_worktrees: [],
        message: 'Read-only audit complete.',
        issue_cleanup_enabled: true,
        activity_evidence: 'known',
        audit_unavailable: false,
        scope: 'configured',
        note: null,
        ...overrides,
    };
}

test('worktree audit fetches the selected repo and renders classified results', async () => {
    const calls = [];
    const worktrees = [
        {
            path: '/repos/project-41-review-20260812T010203123456Z',
            name: 'project-41-review-20260812T010203123456Z',
            kind: 'reviewer',
            disposition: 'cleanup_candidate',
            reason: 'owned detached reviewer worktree is inactive',
        },
        {
            path: '/repos/project-42',
            name: 'project-42',
            kind: 'issue',
            disposition: 'managed',
            reason: 'managed issue worktree; removal waits for the configured review gate',
        },
    ];
    const context = loadAuditHelpers(async (url, options) => {
        calls.push({ url, body: JSON.parse(options.body) });
        return { ok: true, json: async () => auditPayload({ worktrees }) };
    });

    const html = await context.requestWorktreeAudit('/repos/project');

    assert.deepEqual(calls, [{
        url: '/control/tools/worktrees/cleanup',
        body: { repo_root: '/repos/project' },
    }]);
    assert.match(html, /1<\/strong> cleanup candidate/);
    assert.match(html, /1<\/strong> managed issue worktree/);
    assert.match(html, /reviewer — cleanup_candidate/);
    assert.match(html, /Review-gated issue cleanup:<\/strong> enabled/);
});

test('worktree audit renders the typed empty state', async () => {
    const context = loadAuditHelpers(async () => ({
        ok: true,
        json: async () => auditPayload({
            message: 'No registered secondary worktrees.',
            issue_cleanup_enabled: false,
        }),
    }));

    const html = await context.requestWorktreeAudit('/repos/project');

    assert.match(html, /No registered secondary worktrees/);
    assert.match(html, /Review-gated issue cleanup:<\/strong> disabled/);
    assert.doesNotMatch(html, /<ul/);
});

test('worktree audit renders an escaped HTTP error', async () => {
    const context = loadAuditHelpers(async () => ({
        ok: false,
        json: async () => ({ detail: '<engine unavailable>' }),
    }));

    const html = await context.requestWorktreeAudit('/repos/project');

    assert.match(html, /class="error-message"/);
    assert.match(html, /&lt;engine unavailable&gt;/);
    assert.doesNotMatch(html, /<engine unavailable>/);
});
