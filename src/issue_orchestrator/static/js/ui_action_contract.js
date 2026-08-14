(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.uiActionContract = api;
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    const ENDPOINTS = {
        UNBLOCK_RETRY: '/api/unblock-retry',
        RESET_RETRY: '/api/reset-retry',
        RETROSPECTIVE_REVIEW_PREFLIGHT: '/api/retrospective-review/preflight',
        RETROSPECTIVE_REVIEW: '/api/retrospective-review',
        BULK_RETRY: '/api/bulk-retry',
        BULK_DEPRIORITIZE: '/api/bulk-deprioritize',
        BULK_CANCEL_QUEUED: '/api/bulk-cancel-queued',
        HOST_OPEN_PATH: '/api/host/open-path',
        REVEAL_WORKTREE: (issueNumber) => `/api/host/reveal-worktree/${issueNumber}`,
        REVIEW_ARTIFACT: (issueNumber) => `/api/session/review-artifact/${issueNumber}`,
        SESSION_PROMPT: (issueNumber) => `/api/session/prompt/${issueNumber}`,
        TERMINAL_RECORDING: (issueNumber) => `/api/session/terminal-recording/${issueNumber}`,
        TECH_LEAD_RUNS: '/api/tech-lead/runs',
        RETRY_PUBLISH: (issueNumber) => `/api/issues/${issueNumber}/retry-publish`,
        CLOSE_ISSUE: (issueNumber) => `/api/issues/${issueNumber}/close`,
        ISSUE_RESUME: (issueNumber) => `/api/issues/${issueNumber}/resume`,
        TIMELINE_EVIDENCE_PIN: (issueNumber) => `/api/issues/${issueNumber}/timeline-evidence/pin`,
    };

    function normalizeIssueNumbers(issueNumbers) {
        if (!Array.isArray(issueNumbers)) return [];
        return issueNumbers
            .map((value) => Number(value))
            .filter((value) => Number.isInteger(value) && value > 0);
    }

    function buildUnblockRequest(issueNumbers) {
        const issues = normalizeIssueNumbers(issueNumbers);
        return {
            endpoint: ENDPOINTS.UNBLOCK_RETRY,
            method: 'POST',
            body: { issues },
        };
    }

    function buildResetRetryRequest(issueNumbers, options = {}) {
        const issues = normalizeIssueNumbers(issueNumbers);
        const fromScratch = Boolean(options.fromScratch);
        return {
            endpoint: ENDPOINTS.RESET_RETRY,
            method: 'POST',
            body: { issues, from_scratch: fromScratch },
        };
    }

    function buildRetrospectiveReviewPreflightRequest(issueNumbers) {
        const issues = normalizeIssueNumbers(issueNumbers);
        return {
            endpoint: ENDPOINTS.RETROSPECTIVE_REVIEW_PREFLIGHT,
            method: 'POST',
            body: { issues },
        };
    }

    function buildRetrospectiveReviewExecuteRequest(issueNumbers) {
        const issues = normalizeIssueNumbers(issueNumbers);
        return {
            endpoint: ENDPOINTS.RETROSPECTIVE_REVIEW,
            method: 'POST',
            body: { issues },
        };
    }

    function buildBulkDeprioritizeRequest(issueNumbers) {
        const issue_numbers = normalizeIssueNumbers(issueNumbers);
        return {
            endpoint: ENDPOINTS.BULK_DEPRIORITIZE,
            method: 'POST',
            body: { issue_numbers },
        };
    }

    function buildBulkCancelQueuedRequest(issueNumbers) {
        const issue_numbers = normalizeIssueNumbers(issueNumbers);
        return {
            endpoint: ENDPOINTS.BULK_CANCEL_QUEUED,
            method: 'POST',
            body: { issue_numbers },
        };
    }

    function buildBulkRetryRequest(issueNumbers) {
        const issue_numbers = normalizeIssueNumbers(issueNumbers);
        return {
            endpoint: ENDPOINTS.BULK_RETRY,
            method: 'POST',
            body: { issue_numbers },
        };
    }

    function buildIssueRetryRequest(issueNumber) {
        const normalized = normalizeIssueNumbers([issueNumber]);
        return {
            endpoint: `/api/issues/${normalized[0] || 0}/retry`,
            method: 'POST',
            body: {},
        };
    }

    function buildIssueResumeRequest(issueNumber, runDir) {
        const normalized = normalizeIssueNumbers([issueNumber]);
        if (normalized.length !== 1) {
            throw new Error(`Invalid issue number for resume action: ${issueNumber}`);
        }
        if (!runDir) {
            throw new Error('runDir is required for resume action');
        }
        return {
            endpoint: ENDPOINTS.ISSUE_RESUME(normalized[0]),
            method: 'POST',
            body: { run_dir: String(runDir) },
        };
    }

    function buildTimelineEvidencePinRequest(issueNumber, runDir, pinned, repoRoot = null) {
        const normalized = normalizeIssueNumbers([issueNumber]);
        if (normalized.length !== 1) {
            throw new Error(`Invalid issue number for Timeline evidence pin: ${issueNumber}`);
        }
        if (!runDir) {
            throw new Error('runDir is required for Timeline evidence pin');
        }
        const endpoint = repoRoot
            ? `${ENDPOINTS.TIMELINE_EVIDENCE_PIN(normalized[0])}?repo_root=${encodeURIComponent(String(repoRoot))}`
            : ENDPOINTS.TIMELINE_EVIDENCE_PIN(normalized[0]);
        return {
            endpoint,
            method: 'PUT',
            body: { run_dir: String(runDir), pinned: Boolean(pinned) },
        };
    }

    function buildHostOpenPathRequest(path) {
        return {
            endpoint: ENDPOINTS.HOST_OPEN_PATH,
            method: 'POST',
            body: { path: String(path || '') },
        };
    }

    function buildRetryPublishRequest(issueNumber) {
        const normalized = normalizeIssueNumbers([issueNumber]);
        if (normalized.length !== 1) {
            throw new Error(`Invalid issue number for retry-publish action: ${issueNumber}`);
        }
        return {
            endpoint: ENDPOINTS.RETRY_PUBLISH(normalized[0]),
            method: 'POST',
            body: {},
        };
    }

    function buildCloseIssueRequest(issueNumber) {
        const normalized = normalizeIssueNumbers([issueNumber]);
        if (normalized.length !== 1) {
            throw new Error(`Invalid issue number for close-issue action: ${issueNumber}`);
        }
        return {
            endpoint: ENDPOINTS.CLOSE_ISSUE(normalized[0]),
            method: 'POST',
            body: {},
        };
    }

    // Tech-lead run requests (#6994). One command surface, one discriminated
    // scope: the endpoint never learns "which button" was clicked, only which
    // scope was requested, so the server-side admission owner stays the single
    // place that decides whether the run may start.
    function buildGlobalHealthReviewRunRequest() {
        return {
            endpoint: ENDPOINTS.TECH_LEAD_RUNS,
            method: 'POST',
            body: { scope: { kind: 'global_health_review' } },
        };
    }

    function buildIssueInvestigationRunRequest(issueNumber) {
        const normalized = normalizeIssueNumbers([issueNumber]);
        if (normalized.length !== 1) {
            throw new Error(`Invalid issue number for tech-lead investigation: ${issueNumber}`);
        }
        return {
            endpoint: ENDPOINTS.TECH_LEAD_RUNS,
            method: 'POST',
            body: { scope: { kind: 'issue', issue_number: normalized[0] } },
        };
    }

    function buildRevealWorktreeRequest(issueNumber) {
        const normalized = normalizeIssueNumbers([issueNumber]);
        if (normalized.length !== 1) {
            throw new Error(`Invalid issue number for reveal-worktree action: ${issueNumber}`);
        }
        return {
            endpoint: ENDPOINTS.REVEAL_WORKTREE(normalized[0]),
            method: 'POST',
            body: {},
        };
    }

    function buildTerminalRecordingRequest(issueNumber, runDir, options = {}) {
        const normalized = normalizeIssueNumbers([issueNumber]);
        if (normalized.length !== 1) {
            throw new Error(`Invalid issue number for terminal recording action: ${issueNumber}`);
        }
        if (!runDir) {
            throw new Error('runDir is required for terminal recording action');
        }
        const params = new URLSearchParams();
        params.set('run_dir', String(runDir));
        if (options.offset !== undefined) {
            params.set('offset', String(options.offset));
        }
        if (options.limit !== undefined) {
            params.set('limit', String(options.limit));
        }
        const roundIndex = Number(options.round_index);
        if (Number.isInteger(roundIndex) && roundIndex > 0) {
            params.set('round_index', String(roundIndex));
        }
        const sessionRole = typeof options.session_role === 'string'
            ? options.session_role.trim()
            : '';
        if (sessionRole) {
            params.set('session_role', sessionRole);
        }
        const sinceHash = typeof options.since_hash === 'string' ? options.since_hash : '';
        if (sinceHash) {
            params.set('since_hash', sinceHash);
        }
        const repoRoot = typeof options.repo_root === 'string' ? options.repo_root.trim() : '';
        if (repoRoot) {
            params.set('repo_root', repoRoot);
        }
        return {
            endpoint: `${ENDPOINTS.TERMINAL_RECORDING(normalized[0])}?${params.toString()}`,
            method: 'GET',
        };
    }

    function buildSessionPromptRequest(issueNumber, runDir) {
        const normalized = normalizeIssueNumbers([issueNumber]);
        if (normalized.length !== 1) {
            throw new Error(`Invalid issue number for session prompt action: ${issueNumber}`);
        }
        if (!runDir) {
            throw new Error('runDir is required for session prompt action');
        }
        const params = new URLSearchParams();
        params.set('run_dir', String(runDir));
        return {
            endpoint: `${ENDPOINTS.SESSION_PROMPT(normalized[0])}?${params.toString()}`,
            method: 'GET',
        };
    }

    function buildReviewArtifactRequest(issueNumber, runDir, artifactPath, artifactType) {
        const normalized = normalizeIssueNumbers([issueNumber]);
        if (normalized.length !== 1) {
            throw new Error(`Invalid issue number for review artifact action: ${issueNumber}`);
        }
        if (!runDir) {
            throw new Error('runDir is required for review artifact action');
        }
        if (!artifactPath) {
            throw new Error('artifactPath is required for review artifact action');
        }
        if (artifactType !== 'review_report' && artifactType !== 'review_decision') {
            throw new Error(`Unsupported review artifact type: ${artifactType}`);
        }
        const params = new URLSearchParams();
        params.set('run_dir', String(runDir));
        params.set('artifact_path', String(artifactPath));
        params.set('artifact_type', String(artifactType));
        return {
            endpoint: `${ENDPOINTS.REVIEW_ARTIFACT(normalized[0])}?${params.toString()}`,
            method: 'GET',
        };
    }

    return {
        ENDPOINTS,
        normalizeIssueNumbers,
        buildUnblockRequest,
        buildResetRetryRequest,
        buildRetrospectiveReviewPreflightRequest,
        buildRetrospectiveReviewExecuteRequest,
        buildBulkRetryRequest,
        buildBulkDeprioritizeRequest,
        buildBulkCancelQueuedRequest,
        buildIssueRetryRequest,
        buildIssueResumeRequest,
        buildTimelineEvidencePinRequest,
        buildRetryPublishRequest,
        buildCloseIssueRequest,
        buildGlobalHealthReviewRunRequest,
        buildIssueInvestigationRunRequest,
        buildHostOpenPathRequest,
        buildRevealWorktreeRequest,
        buildTerminalRecordingRequest,
        buildReviewArtifactRequest,
        buildSessionPromptRequest,
    };
});
