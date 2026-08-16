const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const DASHBOARD_JS_DIR = path.join(
    __dirname,
    '../../src/issue_orchestrator/static/js/dashboard',
);

function _extractFunction(source, signaturePrefix) {
    const start = source.indexOf(signaturePrefix);
    if (start < 0) throw new Error(`function not found: ${signaturePrefix}`);
    const bodyStart = source.indexOf('{', start + signaturePrefix.length);
    if (bodyStart < 0) throw new Error(`function body not found: ${signaturePrefix}`);
    let depth = 0;
    for (let index = bodyStart; index < source.length; index += 1) {
        const char = source[index];
        if (char === '{') depth += 1;
        if (char === '}') depth -= 1;
        if (depth === 0) {
            const extracted = source.slice(start, index + 1);
            assert.ok(
                extracted.length > signaturePrefix.length + 20,
                `function slice too short: ${signaturePrefix}`,
            );
            return extracted;
        }
    }
    throw new Error(`function body did not close: ${signaturePrefix}`);
}

function _escapeHtml(value) {
    return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function loadTimelineSlice(overrides = {}) {
    const calls = [];
    const source = fs.readFileSync(path.join(DASHBOARD_JS_DIR, 'timeline.js'), 'utf8');
    const slice = [
        'const timelineEventDetailsById = new Map();',
        _extractFunction(source, 'function _timelineActionShortLabel'),
        _extractFunction(source, 'function runTimelineEventAction'),
    ].join('\n');
    const context = {
        openReviewArtifact: (...args) => calls.push(['openReviewArtifact', ...args]),
        showToast: (...args) => calls.push(['showToast', ...args]),
        ...overrides,
    };
    vm.createContext(context);
    vm.runInContext(slice, context, { filename: 'timeline.js review artifact slice' });
    return { context, calls };
}

function loadReviewDialogSlice(overrides = {}) {
    const calls = [];
    const source = fs.readFileSync(path.join(DASHBOARD_JS_DIR, 'session_dialogs.js'), 'utf8');
    const slice = [
        _extractFunction(source, 'function _renderReviewMarkdownInline('),
        _extractFunction(source, 'function _renderReviewMarkdown('),
        _extractFunction(source, 'function _reviewArtifactTitleName('),
        _extractFunction(source, 'function _renderReviewArtifactContent('),
        _extractFunction(source, 'async function openReviewArtifact('),
    ].join('\n');
    const context = {
        escapeHtml: _escapeHtml,
        showToast: (...args) => calls.push(['showToast', ...args]),
        openModal: (...args) => calls.push(['openModal', ...args]),
        modalOverlay: { querySelector: () => ({ classList: { add: () => {} } }) },
        uiActionContract: {
            buildReviewArtifactRequest: (...args) => {
                calls.push(['buildReviewArtifactRequest', ...args]);
                return { endpoint: '/artifact', method: 'GET' };
            },
        },
        fetch: async () => ({
            ok: true,
            json: async () => ({
                artifact_path: '/tmp/run/review-report.md',
                content_type: 'text/markdown',
                content: '# Review\n\n- `N1` escaped <tag>',
            }),
        }),
        ...overrides,
    };
    vm.createContext(context);
    vm.runInContext(slice, context, { filename: 'session_dialogs.js review artifact slice' });
    return { context, calls };
}

function loadLifecycleCommands(overrides = {}) {
    const calls = [];
    const source = fs.readFileSync(path.join(DASHBOARD_JS_DIR, 'lifecycle_commands.js'), 'utf8');
    const context = {
        openReviewArtifact: (...args) => calls.push(['openReviewArtifact', ...args]),
        showToast: (...args) => calls.push(['showToast', ...args]),
        ...overrides,
    };
    vm.createContext(context);
    vm.runInContext(source, context, { filename: 'lifecycle_commands.js' });
    return { context, calls };
}

function loadLifecycleCommandToDialogSlice(overrides = {}) {
    const loaded = loadReviewDialogSlice(overrides);
    const { context, calls } = loaded;
    const openReviewArtifact = context.openReviewArtifact;
    let pendingArtifact = null;
    context.openReviewArtifact = (...args) => {
        calls.push(['openReviewArtifactCommand', ...args]);
        pendingArtifact = openReviewArtifact(...args);
        return pendingArtifact;
    };
    const source = fs.readFileSync(path.join(DASHBOARD_JS_DIR, 'lifecycle_commands.js'), 'utf8');
    vm.runInContext(source, context, { filename: 'lifecycle_commands.js' });
    return {
        context,
        calls,
        waitForArtifact: async () => {
            assert.ok(pendingArtifact, 'expected lifecycle command to open a review artifact');
            await pendingArtifact;
        },
    };
}

test('timeline dispatch opens review artifact actions', () => {
    const { context, calls } = loadTimelineSlice();

    context.runTimelineEventAction({
        type: 'open_review_artifact',
        issue_number: 4057,
        run_dir: '/tmp/run',
        artifact_path: '/tmp/run/review-report.md',
        artifact_type: 'review_report',
        render_mode: 'markdown',
    });

    assert.deepEqual(calls, [[
        'openReviewArtifact',
        4057,
        '/tmp/run',
        '/tmp/run/review-report.md',
        'review_report',
        'markdown',
    ]]);
});

test('typed lifecycle dispatcher opens review artifact commands', () => {
    const { context, calls } = loadLifecycleCommands();

    context.runLifecycleCommand({
        kind: 'open_review_artifact',
        issue_number: 4057,
        run_dir: '/tmp/run',
        artifact_path: '/tmp/run/review-decision.json',
        artifact_type: 'review_decision',
        render_mode: 'json',
    });

    assert.deepEqual(calls, [[
        'openReviewArtifact',
        4057,
        '/tmp/run',
        '/tmp/run/review-decision.json',
        'review_decision',
        'json',
    ]]);
});

test('timeline short labels distinguish report and decision JSON', () => {
    const { context } = loadTimelineSlice();

    assert.equal(
        context._timelineActionShortLabel({ type: 'open_review_artifact', artifact_type: 'review_report' }),
        'Review report',
    );
    assert.equal(
        context._timelineActionShortLabel({ type: 'open_review_artifact', artifact_type: 'review_decision' }),
        'Decision JSON',
    );
});

test('review artifact modal fetches through contract and renders escaped markdown', async () => {
    const { context, calls } = loadReviewDialogSlice();

    await context.openReviewArtifact(
        4057,
        '/tmp/run',
        '/tmp/run/review-report.md',
        'review_report',
        'markdown',
    );

    assert.deepEqual(calls[1], [
        'buildReviewArtifactRequest',
        4057,
        '/tmp/run',
        '/tmp/run/review-report.md',
        'review_report',
    ]);
    const finalModal = calls.filter((call) => call[0] === 'openModal').at(-1);
    assert.match(finalModal[2], /class="review-artifact-markdown"/);
    assert.match(finalModal[2], /&lt;tag&gt;/);
    assert.doesNotMatch(finalModal[2], /<tag>/);
});

test('lifecycle review report command fetches and renders report content', async () => {
    const report = '# Review Report\n\n## N1\n\nRename helper <script>';
    const { context, calls, waitForArtifact } = loadLifecycleCommandToDialogSlice({
        fetch: async () => ({
            ok: true,
            json: async () => ({
                artifact_path: '/tmp/run/review-exchange/turns/review-report.md',
                content_type: 'text/markdown',
                content: report,
            }),
        }),
    });

    context.runLifecycleCommand({
        kind: 'open_review_artifact',
        issue_number: 4057,
        run_dir: '/tmp/run',
        artifact_path: '/tmp/run/review-exchange/turns/review-report.md',
        artifact_type: 'review_report',
        render_mode: 'markdown',
    });
    await waitForArtifact();

    assert.deepEqual(calls.find((call) => call[0] === 'openReviewArtifactCommand'), [
        'openReviewArtifactCommand',
        4057,
        '/tmp/run',
        '/tmp/run/review-exchange/turns/review-report.md',
        'review_report',
        'markdown',
    ]);
    const finalModal = calls.filter((call) => call[0] === 'openModal').at(-1);
    assert.match(finalModal[1], /Review report #4057/);
    assert.match(finalModal[2], /Review Report/);
    assert.match(finalModal[2], /N1/);
    assert.match(finalModal[2], /Rename helper &lt;script&gt;/);
    assert.doesNotMatch(finalModal[2], /Rename helper <script>/);
});

test('lifecycle decision command fetches and renders JSON content', async () => {
    const decision = JSON.stringify({
        schema_version: 1,
        verdict: 'approved',
        nits: [{ id: 'N1', title: 'Rename helper' }],
    });
    const { context, calls, waitForArtifact } = loadLifecycleCommandToDialogSlice({
        fetch: async () => ({
            ok: true,
            json: async () => ({
                artifact_path: '/tmp/run/review-exchange/turns/review-decision.json',
                content_type: 'application/json',
                content: decision,
            }),
        }),
    });

    context.runLifecycleCommand({
        kind: 'open_review_artifact',
        issue_number: 4057,
        run_dir: '/tmp/run',
        artifact_path: '/tmp/run/review-exchange/turns/review-decision.json',
        artifact_type: 'review_decision',
        render_mode: 'json',
    });
    await waitForArtifact();

    const finalModal = calls.filter((call) => call[0] === 'openModal').at(-1);
    assert.match(finalModal[1], /Decision JSON #4057/);
    assert.match(finalModal[2], /class="review-artifact-json"/);
    assert.match(finalModal[2], /&quot;verdict&quot;: &quot;approved&quot;/);
    assert.match(finalModal[2], /&quot;id&quot;: &quot;N1&quot;/);
    assert.match(finalModal[2], /&quot;title&quot;: &quot;Rename helper&quot;/);
});

test('the viewer titles a tech-lead artifact with the tech lead\'s name', async () => {
    // #6858 F4: the same endpoint and the same viewer now serve the tech lead's
    // report/decision pair. Titling one "Review report" would put the wrong
    // agent's name on the artifact an operator opened.
    const { context, calls } = loadReviewDialogSlice();

    await context.openReviewArtifact(
        900,
        '/repo/.issue-orchestrator/state/tech-lead-runs/run-900__tech-lead-900',
        'tech-lead-data/tech-lead-report.md',
        'tech_lead_report',
        'markdown',
    );

    const titles = calls.filter(entry => entry[0] === 'openModal').map(entry => entry[1]);
    assert.ok(
        titles.every(title => title === 'Tech lead report #900'),
        `unexpected titles: ${JSON.stringify(titles)}`,
    );
});
