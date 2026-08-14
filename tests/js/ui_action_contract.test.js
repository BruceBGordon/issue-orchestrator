const test = require('node:test');
const assert = require('node:assert/strict');

const uiActionContract = require('../../src/issue_orchestrator/static/js/ui_action_contract.js');

test('buildUnblockRequest returns canonical endpoint and payload', () => {
    const req = uiActionContract.buildUnblockRequest([4057, '12', 0, 'x']);
    assert.equal(req.endpoint, '/api/unblock-retry');
    assert.equal(req.method, 'POST');
    assert.deepEqual(req.body, { issues: [4057, 12] });
});

test('normalizeIssueNumbers rejects invalid issue numbers', () => {
    assert.deepEqual(
        uiActionContract.normalizeIssueNumbers([1, -1, 0, '3', 'x', null, 4.5]),
        [1, 3],
    );
});

test('buildResetRetryRequest returns canonical endpoint and payload', () => {
    const req = uiActionContract.buildResetRetryRequest([10, '11']);
    assert.equal(req.endpoint, '/api/reset-retry');
    assert.equal(req.method, 'POST');
    assert.deepEqual(req.body, { issues: [10, 11], from_scratch: false });
});

test('retrospective review requests return canonical endpoints and payloads', () => {
    const preflight = uiActionContract.buildRetrospectiveReviewPreflightRequest([10, '11', 'x']);
    assert.equal(preflight.endpoint, '/api/retrospective-review/preflight');
    assert.equal(preflight.method, 'POST');
    assert.deepEqual(preflight.body, { issues: [10, 11] });

    const execute = uiActionContract.buildRetrospectiveReviewExecuteRequest([10, '11', 'x']);
    assert.equal(execute.endpoint, '/api/retrospective-review');
    assert.equal(execute.method, 'POST');
    assert.deepEqual(execute.body, { issues: [10, 11] });
});

test('buildBulkDeprioritizeRequest returns canonical endpoint and payload', () => {
    const req = uiActionContract.buildBulkDeprioritizeRequest([99, 'x', 100]);
    assert.equal(req.endpoint, '/api/bulk-deprioritize');
    assert.equal(req.method, 'POST');
    assert.deepEqual(req.body, { issue_numbers: [99, 100] });
});

test('buildIssueRetryRequest returns issue-specific endpoint', () => {
    const req = uiActionContract.buildIssueRetryRequest('4057');
    assert.equal(req.endpoint, '/api/issues/4057/retry');
    assert.equal(req.method, 'POST');
    assert.deepEqual(req.body, {});
});

test('buildIssueResumeRequest returns issue-specific endpoint and run contract', () => {
    const req = uiActionContract.buildIssueResumeRequest('4057', '/tmp/run');
    assert.equal(req.endpoint, '/api/issues/4057/resume');
    assert.equal(req.method, 'POST');
    assert.deepEqual(req.body, { run_dir: '/tmp/run' });
});

test('buildIssueResumeRequest rejects missing runDir', () => {
    assert.throws(
        () => uiActionContract.buildIssueResumeRequest(4057, ''),
        /runDir is required/,
    );
});

test('buildTimelineEvidencePinRequest returns exact-run typed command', () => {
    const req = uiActionContract.buildTimelineEvidencePinRequest(
        '4057', '/runs/failed-4057', true,
    );
    assert.equal(req.endpoint, '/api/issues/4057/timeline-evidence/pin');
    assert.equal(req.method, 'PUT');
    assert.deepEqual(req.body, { run_dir: '/runs/failed-4057', pinned: true });
});

test('buildTimelineEvidencePinRequest preserves Control Center repository scope', () => {
    const req = uiActionContract.buildTimelineEvidencePinRequest(
        4057, '/runs/failed-4057', true, '/repos/project one',
    );
    assert.equal(
        req.endpoint,
        '/api/issues/4057/timeline-evidence/pin?repo_root=%2Frepos%2Fproject%20one',
    );
});

test('buildTimelineEvidencePinRequest rejects missing runDir', () => {
    assert.throws(
        () => uiActionContract.buildTimelineEvidencePinRequest(4057, '', true),
        /runDir is required/,
    );
});

test('buildRetryPublishRequest returns issue-specific endpoint', () => {
    const req = uiActionContract.buildRetryPublishRequest('4057');
    assert.equal(req.endpoint, '/api/issues/4057/retry-publish');
    assert.equal(req.method, 'POST');
    assert.deepEqual(req.body, {});
});

test('buildTerminalRecordingRequest returns canonical endpoint and query params', () => {
    const req = uiActionContract.buildTerminalRecordingRequest(4057, '/tmp/run', { offset: 10, limit: 0 });
    assert.equal(
        req.endpoint,
        '/api/session/terminal-recording/4057?run_dir=%2Ftmp%2Frun&offset=10&limit=0',
    );
    assert.equal(req.method, 'GET');
});

test('buildTerminalRecordingRequest includes review phase scoping params', () => {
    const req = uiActionContract.buildTerminalRecordingRequest(4057, '/tmp/run', {
        offset: 0,
        limit: 0,
        round_index: 2,
        session_role: 'reviewer',
    });
    assert.equal(
        req.endpoint,
        '/api/session/terminal-recording/4057?run_dir=%2Ftmp%2Frun&offset=0&limit=0&round_index=2&session_role=reviewer',
    );
    assert.equal(req.method, 'GET');
});

test('buildTerminalRecordingRequest preserves Control Center repository scope', () => {
    const req = uiActionContract.buildTerminalRecordingRequest(4057, '/tmp/run', {
        offset: 0,
        limit: 0,
        repo_root: '/repos/project one',
    });
    assert.equal(
        req.endpoint,
        '/api/session/terminal-recording/4057?run_dir=%2Ftmp%2Frun&offset=0&limit=0&repo_root=%2Frepos%2Fproject+one',
    );
});

test('buildTerminalRecordingRequest passes since_hash when provided (transcript short-circuit)', () => {
    const req = uiActionContract.buildTerminalRecordingRequest(4057, '/tmp/run', {
        offset: 0,
        limit: 0,
        since_hash: 'abc123',
    });
    assert.equal(
        req.endpoint,
        '/api/session/terminal-recording/4057?run_dir=%2Ftmp%2Frun&offset=0&limit=0&since_hash=abc123',
    );
});

test('buildTerminalRecordingRequest omits since_hash when empty', () => {
    const req = uiActionContract.buildTerminalRecordingRequest(4057, '/tmp/run', {
        offset: 0,
        limit: 0,
        since_hash: '',
    });
    assert.ok(!req.endpoint.includes('since_hash'), `since_hash should not appear: ${req.endpoint}`);
});

test('buildReviewArtifactRequest returns canonical endpoint and query params', () => {
    const req = uiActionContract.buildReviewArtifactRequest(
        4057,
        '/tmp/run',
        '/tmp/run/review-exchange/turns/review-report.md',
        'review_report',
    );
    assert.equal(
        req.endpoint,
        '/api/session/review-artifact/4057?run_dir=%2Ftmp%2Frun&artifact_path=%2Ftmp%2Frun%2Freview-exchange%2Fturns%2Freview-report.md&artifact_type=review_report',
    );
    assert.equal(req.method, 'GET');
});

test('buildReviewArtifactRequest rejects unsupported artifact type', () => {
    assert.throws(
        () => uiActionContract.buildReviewArtifactRequest(4057, '/tmp/run', '/tmp/run/x.txt', 'text'),
        /Unsupported review artifact type/,
    );
});

test('buildSessionPromptRequest returns canonical endpoint and encoded run_dir', () => {
    const req = uiActionContract.buildSessionPromptRequest('454', '/runs/rework-454');
    assert.equal(req.endpoint, '/api/session/prompt/454?run_dir=%2Fruns%2Frework-454');
    assert.equal(req.method, 'GET');
});

test('buildSessionPromptRequest rejects missing runDir', () => {
    assert.throws(
        () => uiActionContract.buildSessionPromptRequest(454, ''),
        /runDir is required/,
    );
});

test('buildSessionPromptRequest rejects an invalid issue number', () => {
    assert.throws(
        () => uiActionContract.buildSessionPromptRequest('x', '/runs/rework-454'),
        /Invalid issue number/,
    );
});
