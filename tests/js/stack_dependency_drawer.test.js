// JS-vm tests for the issue-detail drawer stack dependency section.
//
// Slices only the stack render functions out of issue_detail_drawer.js (the
// full file registers document listeners at load) and drives them with a
// minimal fake document, asserting the disclosure section shows producer-
// provided gate/edge state with text-first, non-colour-only status.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const DRAWER_JS = path.join(
    __dirname,
    '../../src/issue_orchestrator/static/js/dashboard/issue_detail_drawer.js',
);

function _extractFunction(source, signaturePrefix) {
    const start = source.indexOf(signaturePrefix);
    if (start < 0) throw new Error(`function not found: ${signaturePrefix}`);
    const after = source.indexOf('\n}\n', start);
    if (after < 0) throw new Error(`function close not found: ${signaturePrefix}`);
    return source.slice(start, after + 3);
}

function _escapeHtml(value) {
    return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function fakeNode() {
    return { style: { display: '' }, innerHTML: '', textContent: '' };
}

function loadStackRenderer() {
    const source = fs.readFileSync(DRAWER_JS, 'utf8');
    const slice = [
        _extractFunction(source, 'function _stackGateItemHtml'),
        _extractFunction(source, 'function _stackEdgeItemHtml'),
        _extractFunction(source, 'function renderIssueDetailStack'),
    ].join('\n');
    const nodes = {
        issueDetailStack: fakeNode(),
        issueDetailStackBody: fakeNode(),
        issueDetailStackSummary: fakeNode(),
    };
    const context = {
        // Cross-chunk owner: ``tech_lead_runs.js`` owns the targeted tech-lead
        // action's visibility/state; the drawer only names its elements (#6994).
        resetTechLeadIssueAction: () => {},
        updateTechLeadIssueAction: () => false,
        escapeHtml: _escapeHtml,
        document: { getElementById: (id) => nodes[id] || null },
    };
    vm.createContext(context);
    vm.runInContext(slice, context, { filename: 'issue_detail_drawer.js (stack slice)' });
    return { context, nodes };
}

function stackDependency(overrides = {}) {
    return {
        issue_number: 20,
        mode: 'stack',
        has_stack_edges: true,
        gates: [
            { gate: 'work', open: true, reason_codes: [], reasons: [] },
            { gate: 'publish', open: false, reason_codes: ['predecessor_not_merged'], reasons: ['the predecessor has not merged yet'] },
        ],
        predecessors: [{ ref: '#10', mode: 'stack', state: 'unsatisfied', problem: null }],
        successors: [{ issue_number: 30, ref: '#30', mode: 'stack' }],
        blocked_gates: ['publish', 'merge'],
        blocked_reason_codes: ['predecessor_not_merged'],
        stale: false,
        stale_reason_codes: [],
        stack_base_branch: 'feat/base',
        approval_freshness: 'fresh',
        ...overrides,
    };
}

test('section is hidden when there is no stack dependency data', () => {
    const { context, nodes } = loadStackRenderer();
    context.renderIssueDetailStack({ stack_dependency: null });
    assert.strictEqual(nodes.issueDetailStack.style.display, 'none');
    assert.strictEqual(nodes.issueDetailStackBody.innerHTML, '');
});

test('section renders gates with text status, predecessors and successors', () => {
    const { context, nodes } = loadStackRenderer();
    context.renderIssueDetailStack({ stack_dependency: stackDependency() });
    assert.strictEqual(nodes.issueDetailStack.style.display, '');
    const html = nodes.issueDetailStackBody.innerHTML;
    // Gate state is text ("open"/"blocked"), not colour-only.
    assert.match(html, /stack-gate--open/);
    assert.match(html, /stack-gate--blocked/);
    assert.match(html, /<span class="stack-gate-state">blocked<\/span>/);
    // Human reason phrase for the blocked gate.
    assert.match(html, /the predecessor has not merged yet/);
    // Chain context: depends-on and stacked-behind.
    assert.match(html, /Depends on/);
    assert.match(html, /#10/);
    assert.match(html, /Stacked behind this/);
    assert.match(html, /#30/);
    // Stack base branch is shown.
    assert.match(html, /feat\/base/);
    // Decorative gate icons are aria-hidden.
    assert.match(html, /<span class="stack-gate-icon" aria-hidden="true">/);
});

test('normal dependents render under "Dependent issues", not "Stacked behind this"', () => {
    const { context, nodes } = loadStackRenderer();
    context.renderIssueDetailStack({
        stack_dependency: stackDependency({
            mode: 'none',
            has_stack_edges: false,
            predecessors: [],
            gates: [],
            stack_base_branch: null,
            // #2 declared a plain `Depends-on: #this`, so it is a normal dependent
            // of this issue — not stacked behind it.
            successors: [{ issue_number: 2, ref: '#2', mode: 'normal' }],
        }),
    });
    const html = nodes.issueDetailStackBody.innerHTML;
    assert.match(html, /Dependent issues/);
    assert.doesNotMatch(html, /Stacked behind this/);
    assert.match(html, /#2/);
});

test('mixed successors split by mode into stack and dependent headings', () => {
    const { context, nodes } = loadStackRenderer();
    context.renderIssueDetailStack({
        stack_dependency: stackDependency({
            successors: [
                { issue_number: 30, ref: '#30', mode: 'stack' },
                { issue_number: 40, ref: '#40', mode: 'normal' },
            ],
        }),
    });
    const html = nodes.issueDetailStackBody.innerHTML;
    // Both headings present; stack heading precedes the dependent heading.
    assert.match(html, /Stacked behind this/);
    assert.match(html, /Dependent issues/);
    assert.ok(html.indexOf('Stacked behind this') < html.indexOf('Dependent issues'));
    assert.match(html, /#30/);
    assert.match(html, /#40/);
});

test('summary label switches to plain dependencies when not a stack', () => {
    const { context, nodes } = loadStackRenderer();
    context.renderIssueDetailStack({
        stack_dependency: stackDependency({
            mode: 'normal',
            has_stack_edges: false,
            successors: [],
            stack_base_branch: null,
        }),
    });
    assert.strictEqual(nodes.issueDetailStackSummary.textContent, 'Dependencies & gates');
});

test('unverified approval freshness on an open merge gate shows an explicit note', () => {
    const { context, nodes } = loadStackRenderer();
    context.renderIssueDetailStack({
        stack_dependency: stackDependency({
            approval_freshness: 'unknown',
            gates: [{ gate: 'merge', open: true, reason_codes: [], reasons: [] }],
            blocked_gates: [],
        }),
    });
    const html = nodes.issueDetailStackBody.innerHTML;
    // The merge gate is not silently rendered verified-fresh: an explicit note
    // says approval freshness is not verified.
    assert.match(html, /stack-approval-unverified/);
    assert.match(html, /does not include an approval-freshness check/);
    assert.match(html, /aria-hidden="true">ⓘ/);
});

test('no approval note when the merge gate is already blocked (not the deciding factor)', () => {
    const { context, nodes } = loadStackRenderer();
    context.renderIssueDetailStack({
        stack_dependency: stackDependency({
            approval_freshness: 'unknown',
            gates: [{ gate: 'merge', open: false, reason_codes: ['predecessor_not_merged'], reasons: ['the predecessor has not merged yet'] }],
        }),
    });
    const html = nodes.issueDetailStackBody.innerHTML;
    assert.doesNotMatch(html, /stack-approval-unverified/);
});

test('no approval note when freshness is verified fresh', () => {
    const { context, nodes } = loadStackRenderer();
    context.renderIssueDetailStack({
        stack_dependency: stackDependency({
            approval_freshness: 'fresh',
            gates: [{ gate: 'merge', open: true, reason_codes: [], reasons: [] }],
            blocked_gates: [],
        }),
    });
    assert.doesNotMatch(nodes.issueDetailStackBody.innerHTML, /stack-approval-unverified/);
});

test('stale slice shows a text stale note with reason codes', () => {
    const { context, nodes } = loadStackRenderer();
    context.renderIssueDetailStack({
        stack_dependency: stackDependency({ stale: true, stale_reason_codes: ['predecessor_branch_advanced'] }),
    });
    const html = nodes.issueDetailStackBody.innerHTML;
    assert.match(html, /stack-stale/);
    assert.match(html, /needs rebuilding/);
    assert.match(html, /predecessor_branch_advanced/);
    // The stale icon is decorative; the word "Stale" carries the meaning.
    assert.match(html, /aria-hidden="true">🔄/);
    assert.match(html, /Stale/);
});
