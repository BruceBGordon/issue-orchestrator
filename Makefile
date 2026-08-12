.PHONY: help venv venv-fast semgrep-venv worktree-create worktree-setup install upgrade-deps deps-batch release release-pr prepare-release preview-readme typecheck lint-arch lint-complexity quality-guardrails quality-guardrails-stale sync-deps test test-unit test-unit-cov test-unit-cov-html test-integration test-integration-core test-integration-core-local test-integration-core-live-codex test-integration-agent test-simulated test-simulated-core test-simulated-agent test-e2e test-e2e-heavy test-e2e-onboarding-live test-e2e-one test-e2e-live test-real-claude-dev test-real-claude-review test-real-gh-labels test-real-gh test-real-gh-plus-e2e test-real-gh-plus-e2e-subprocess test-web test-web-headed playwright-install validate validate-raw validate-pr validate-pr-raw validate-quick validate-full verify-hooks-all _validate-impl _validate-static-impl _validate-core-tests-impl _validate-pr-impl _validate-agent-impl _validate-full-impl clean demo issues-validate issues-fix issues-fix-dry-run issues-create

# GNU make detection - required for parallel validation with grouped output
# On macOS: brew install make (provides gmake)
# On Linux: GNU make is the default
GMAKE := $(shell command -v gmake 2>/dev/null || command -v make)
GMAKE_VERSION := $(shell $(GMAKE) --version 2>/dev/null | head -1)

# Default target
help:
	@echo "Available targets:"
	@echo "  venv                Create/recreate .venv with Python 3.14+ and install all deps"
	@echo "  venv-fast           Reuse .venv when possible; install/sync deps (reliable + fast)"
	@echo "  semgrep-venv        Sync locked Semgrep tool environment"
	@echo "  worktree-create     Create and fully set up a worktree (use BRANCH=my-branch)"
	@echo "  worktree-setup      Full worktree setup: venv + vscode extensions + playwright"
	@echo "  install             Install dev dependencies (assumes venv exists)"
	@echo "  upgrade-deps        Update uv.lock after changing pyproject.toml"
	@echo "  deps-batch          Batch-upgrade all manifests + verify locally (MAJOR=1 for npm majors)"
	@echo "  release             Run full release flow (use VERSION=v1.0.0 ARGS=--dry-run)"
	@echo "  release-pr          Create release metadata PR (use VERSION=v1.0.0)"
	@echo "  prepare-release     Bump release files only (use VERSION=v1.0.0)"
	@echo "  typecheck           Run pyright type checking"
	@echo "  lint-arch           Run import-linter + AST guardrails"
	@echo "  lint-complexity     Check cyclomatic complexity (C901) and branch count (PLR0912)"
	@echo "  quality-guardrails  Run ratcheted control-quality guardrails"
	@echo "  quality-guardrails-stale  Check for stale ratchet-baseline entries"
	@echo "  test-unit           Run unit tests"
	@echo "  test-simulated      Run all simulated scenario tests"
	@echo "  test-simulated-core Run fast simulated scenario slice used by local validate"
	@echo "  test-simulated-agent Run real agent-backed simulated scenario slice"
	@echo "  test-unit-cov       Run unit tests with coverage report"
	@echo "  test-unit-cov-html  Run unit tests with HTML coverage (open htmlcov/index.html)"
	@echo "  test-integration    Run integration tests"
	@echo "  test-integration-core   Run fast integration slice used by local validate"
	@echo "  test-integration-agent  Run real agent-backed integration slice"
	@echo "  test-e2e            Run e2e tests (stops on first failure, use NOFAST=1 to run all)"
	@echo "  test-e2e-heavy      Run expensive journey-level onboarding/orchestration tests"
	@echo "  test-e2e-onboarding-live  Run opt-in live agent-guided onboarding acceptance"
	@echo "  test-e2e-one        Run single e2e test (TEST=test_name)"
	@echo "  test-e2e-live       Run e2e tests with REAL PR creation (no dry run!)"
	@echo "  test-real-claude-dev    Test dev agent: Claude execution -> PR created"
	@echo "  test-real-claude-review Test full pipeline: dev agent -> review agent -> approved"
	@echo "  test-real-gh-labels     Verify label write paths against real GitHub"
	@echo "  test-real-gh            Run full real-GitHub suite (dev + review + labels)"
	@echo "  test-real-gh-plus-e2e   Run real-GitHub suite plus full e2e tests"
	@echo "  test-real-gh-plus-e2e-subprocess   Same as above but using subprocess backend"
	@echo "  test-web            Run Flow-first Playwright web UI smoke tests (headless)"
	@echo "  test-web-headed     Run Flow-first Playwright web UI smoke tests (headed)"
	@echo "  test-vscode         Run VS Code extension tests (local only, skipped in CI)"
	@echo "  install-vscode-extensions      Install VS Code extension dev dependencies"
	@echo "  playwright-install  Install Playwright browser binaries"
	@echo "  preview-readme      Render README through GitHub Markdown API to .preview/README.html"
	@echo "  test                Run all tests"
	@echo "  validate            Fast local validation: typecheck + lint + unit + simulated-core + integration-core + web-ui smoke"
	@echo "  validate-pr         Cache-aware required PR gate; seeds/reuses pre-push validation"
	@echo "  validate-pr-raw     Force required PR suite without cache lookup"
	@echo "  validate-quick      Quick validation (typecheck + unit tests only)"
	@echo "  validate-full       Full validation: validate-pr + e2e tests"
	@echo "  verify-hooks-all    Install + live-verify hooks for all supported CLIs"
	@echo "  demo                Run demo showing orchestrator features"
	@echo "  issues-validate     Check issue naming conventions"
	@echo "  issues-fix          Apply issue name fixes"
	@echo "  issues-fix-dry-run  Preview issue name fixes (no changes)"
	@echo "  issues-create       Create issue (use ARGS='--agent x --milestone n --title y')"
	@echo "  clean               Remove build artifacts"
	@echo ""
	@echo "Using: $(GMAKE_VERSION)"

# System Python for venv creation - prefer 3.14, fall back to 3.13, 3.12, 3.11
SYSTEM_PYTHON := $(shell command -v python3.14 2>/dev/null || command -v python3.13 2>/dev/null || command -v python3.12 2>/dev/null || command -v python3.11 2>/dev/null || echo python3)

# Timing log for worktree setup analysis (central location for cumulative stats)
SETUP_LOG ?= $(HOME)/.issue-orchestrator/worktree-setup.log

# Shared playwright browser cache - avoids 250MB re-downloads across worktrees
export PLAYWRIGHT_BROWSERS_PATH ?= $(HOME)/.cache/ms-playwright
# Shared VS Code test cache - avoids VS Code binary re-downloads across worktrees
export IO_VSCODE_TEST_CACHE_PATH ?= $(HOME)/.cache/issue-orchestrator/vscode-test

# uv command - prefer PATH, fall back to default install location
UV := $(shell command -v uv 2>/dev/null || echo $(HOME)/.local/bin/uv)

SEMGREP_PROJECT ?= tools/semgrep
SEMGREP_VENV ?= .venv-semgrep
SEMGREP_DEPS_MARKER ?= $(SEMGREP_VENV)/.deps-synced

# Auto-install uv if not present (one-time per machine)
ensure-uv:
	@if [ ! -x "$(UV)" ]; then \
		echo "Installing uv for fast package management..."; \
		curl -LsSf https://astral.sh/uv/install.sh | sh; \
	fi

venv: ensure-uv
	@mkdir -p $$(dirname $(SETUP_LOG))
	@if [ -d .venv ]; then \
		echo "Removing existing .venv..."; \
		rm -rf .venv; \
	fi
	@echo "Creating venv with $(SYSTEM_PYTHON) and installing dependencies..."
	@t0=$$(date +%s); \
	$(UV) venv .venv --python $(SYSTEM_PYTHON); \
	t1=$$(date +%s); \
	$(UV) sync --frozen --all-extras; \
	t2=$$(date +%s); \
	touch .venv/.deps-synced; \
	echo "venv pid=$$$$ ts=$$(date -Iseconds) pwd=$$(pwd) uv_venv=$$((t1-t0))s uv_sync=$$((t2-t1))s total=$$((t2-t0))s" >> $(SETUP_LOG)
	@$(GMAKE) --no-print-directory semgrep-venv
	@echo ""
	@echo "Done! Activate with: source .venv/bin/activate"

# Fast, reliable venv setup: reuse if present, otherwise create
venv-fast: ensure-uv
	@mkdir -p $$(dirname $(SETUP_LOG))
	@if [ ! -d .venv ]; then \
		echo "Creating venv with $(SYSTEM_PYTHON) and installing dependencies..."; \
		t0=$$(date +%s); \
		$(UV) venv .venv --python $(SYSTEM_PYTHON); \
		t1=$$(date +%s); \
	else \
		echo "Reusing existing .venv; syncing dependencies..."; \
		t0=$$(date +%s); \
		t1=$$(date +%s); \
	fi; \
	$(UV) sync --frozen --all-extras; \
	t2=$$(date +%s); \
	touch .venv/.deps-synced; \
	echo "venv-fast pid=$$$$ ts=$$(date -Iseconds) pwd=$$(pwd) uv_venv=$$((t1-t0))s uv_sync=$$((t2-t1))s total=$$((t2-t0))s" >> $(SETUP_LOG)
	@$(GMAKE) --no-print-directory semgrep-venv
	@echo ""
	@echo "Done! Activate with: source .venv/bin/activate"

semgrep-venv: ensure-uv
	@if [ ! -f $(SEMGREP_DEPS_MARKER) ] || \
		[ ! -x $(SEMGREP_VENV)/bin/semgrep ] || \
		[ $(SEMGREP_PROJECT)/pyproject.toml -nt $(SEMGREP_DEPS_MARKER) ] || \
		[ $(SEMGREP_PROJECT)/uv.lock -nt $(SEMGREP_DEPS_MARKER) ]; then \
		echo "Syncing locked Semgrep tool environment..."; \
		UV_PROJECT_ENVIRONMENT="$(CURDIR)/$(SEMGREP_VENV)" $(UV) sync --project $(SEMGREP_PROJECT) --frozen --no-install-project && \
		touch $(SEMGREP_DEPS_MARKER); \
	fi

# Legacy pip-based venv for systems without uv
venv-pip:
	@mkdir -p $$(dirname $(SETUP_LOG))
	@if [ -d .venv ]; then \
		echo "Removing existing .venv..."; \
		rm -rf .venv; \
	fi
	@echo "Creating venv with $(SYSTEM_PYTHON) (pip fallback)..."
	@t0=$$(date +%s); \
	$(SYSTEM_PYTHON) -m venv .venv; \
	t1=$$(date +%s); \
	echo "Installing agent-runner package first..."; \
	.venv/bin/pip install -e "packages/agent_runner"; \
	t2=$$(date +%s); \
	echo "Installing main package with dev dependencies..."; \
	.venv/bin/pip install -e ".[dev]"; \
	t3=$$(date +%s); \
	touch .venv/.deps-synced; \
	echo "venv-pip pid=$$$$ ts=$$(date -Iseconds) pwd=$$(pwd) venv_create=$$((t1-t0))s pip_agent_runner=$$((t2-t1))s pip_dev_deps=$$((t3-t2))s total=$$((t3-t0))s" >> $(SETUP_LOG)
	@echo ""
	@echo "Done! Activate with: source .venv/bin/activate"

# Keep user-provided values out of Make's exported-variable expansion path.
unexport BRANCH BASE_REF WORKTREE_PATH
worktree-create: export IO_WORKTREE_CREATE_BRANCH := $(value BRANCH)
worktree-create: export IO_WORKTREE_CREATE_BASE_REF := $(value BASE_REF)
worktree-create: export IO_WORKTREE_CREATE_PATH := $(value WORKTREE_PATH)

# Full worktree setup - use this when setting up a new git worktree
worktree-create:
	@$(SYSTEM_PYTHON) scripts/create_dev_worktree.py \
		--repo-root . \
		--make "$(GMAKE)"

worktree-setup: venv-fast
	@echo ""
	@t0=$$(date +%s); \
	echo "Installing VS Code extension dependencies..."; \
	(cd packages/vscode && npm ci --silent); \
	t1=$$(date +%s); \
	echo "Installing Playwright browsers..."; \
	.venv/bin/playwright install chromium --with-deps 2>/dev/null || .venv/bin/playwright install chromium; \
	t2=$$(date +%s); \
	echo "worktree-setup pid=$$$$ ts=$$(date -Iseconds) pwd=$$(pwd) npm_vscode=$$((t1-t0))s playwright=$$((t2-t1))s total=$$((t2-t0))s" >> $(SETUP_LOG)
	@# Generate .mcp.json with worktree-isolated Playwright user-data-dir
	@scripts/generate-mcp-json.sh
	@echo ""
	@echo "Worktree setup complete! Activate with: source .venv/bin/activate"

# Install/reinstall dependencies
install: ensure-uv
	$(UV) sync --frozen --all-extras
	@$(GMAKE) --no-print-directory semgrep-venv
	@touch .venv/.deps-synced

preview-readme:
	$(SYSTEM_PYTHON) scripts/preview_markdown.py README.md --output .preview/README.html

# Update dependencies after changing pyproject.toml
# Usage: make upgrade-deps           - re-resolve after pyproject.toml changes
#        make upgrade-deps UPGRADE=1 - upgrade all deps to latest versions
upgrade-deps: ensure-uv
ifdef UPGRADE
	@echo "Upgrading all dependencies to latest versions..."
	$(UV) lock --upgrade
else
	@echo "Updating uv.lock..."
	$(UV) lock
endif
	@echo "Syncing dependencies..."
	$(UV) sync --frozen --all-extras
	@$(GMAKE) --no-print-directory semgrep-venv
	@touch .venv/.deps-synced
	@echo ""
	@echo "Done! Commit uv.lock with your changes."

# Batched dependency upgrade across every manifest, verified locally.
#
# Why this exists: local `make validate` is a strict superset of CI. It runs
# test-vscode (the real VS Code extension harness), which self-skips on GitHub
# Actions, so packages/vscode has no CI coverage at all. This target is the only
# place the npm bucket is actually exercised before it lands.
#
# Python majors arrive automatically -- pyproject pins with `>=`, so
# `uv lock --upgrade` already crosses major boundaries. npm ranges are `^`, so
# npm majors need MAJOR=1, which rewrites package.json ranges via
# npm-check-updates.
#
# MAJOR=1 is not a rubber stamp: npm-check-updates targets absolute latest, which
# can overshoot what Dependabot proposes (and can bump runtime deps Dependabot
# left alone). Read the package.json diff before committing it, and keep
# @types/vscode at or below the `engines.vscode` floor -- raising it above the
# declared minimum lets the extension compile against APIs that are not present
# in the oldest VS Code we claim to support.
#
# Usage: make deps-batch          - upgrade Python fully; npm within ^ ranges
#        make deps-batch MAJOR=1  - also bump npm package.json ranges to latest
deps-batch: ensure-uv
	@# Validate MAJOR before ANY mutation. ifdef tests presence (so MAJOR=0 would
	@# wrongly take the major branch); ifeq alone rejects bad values but only
	@# after the lockfile upgrades below have already run. This guard is the very
	@# first recipe line so a typo (MAJOR=yes) cannot alter a single lockfile.
	@if [ -n "$(MAJOR)" ] && [ "$(MAJOR)" != "1" ]; then \
		echo "deps-batch: MAJOR must be unset or 1, got '$(MAJOR)'." >&2; \
		exit 2; \
	fi
	@echo "==> Upgrading Python dependencies (root)..."
	$(UV) lock --upgrade
	@echo "==> Upgrading Python dependencies (tools/semgrep)..."
	cd tools/semgrep && $(UV) lock --upgrade
ifeq ($(MAJOR),1)
	@echo "==> Upgrading VS Code extension dependencies (including majors)..."
	cd packages/vscode && npx --yes npm-check-updates -u && npm install
else
	@echo "==> Upgrading VS Code extension dependencies (within ranges)..."
	cd packages/vscode && npm update
endif
	@echo "==> Syncing Python environment..."
	$(UV) sync --frozen --all-extras
	@$(GMAKE) --no-print-directory semgrep-venv
	@touch .venv/.deps-synced
	@echo ""
	@echo "==> Verifying with the full required suite (agent lane + test-vscode)..."
	@# validate-pr-raw, not validate: `validate` stops at _validate-impl and omits
	@# _validate-agent-impl, so it would skip the live claude/codex lane -- exactly
	@# the lane CI already cannot run. That lane is the whole reason this batch is
	@# verified locally, so skipping it here would leave pexpect-class dependencies
	@# (agent spawning) covered by nothing at all. -raw avoids seeding the
	@# SHA-keyed pre-push cache from an uncommitted tree.
	@$(GMAKE) --no-print-directory validate-pr-raw
	@echo ""
	@echo "==> Upgraded manifests:"
	@git diff --stat -- uv.lock tools/semgrep/uv.lock packages/vscode/package.json packages/vscode/package-lock.json
	@echo ""
	@echo "Batch verified. Commit the manifests above; Dependabot closes its own"
	@echo "PRs once the versions it proposed are on main."

release:
	@if [ -z "$(VERSION)" ]; then \
		echo "Usage: make release VERSION=v1.0.0"; \
		exit 2; \
	fi
	@$(SYSTEM_PYTHON) scripts/prepare_release.py "$(VERSION)" $(ARGS)

release-pr:
	@if [ -z "$(VERSION)" ]; then \
		echo "Usage: make release-pr VERSION=v1.0.0"; \
		exit 2; \
	fi
	@$(SYSTEM_PYTHON) scripts/prepare_release.py "$(VERSION)" --prepare-pr $(ARGS)

prepare-release:
	@if [ -z "$(VERSION)" ]; then \
		echo "Usage: make prepare-release VERSION=v1.0.0"; \
		exit 2; \
	fi
	@$(SYSTEM_PYTHON) scripts/prepare_release.py "$(VERSION)" --prepare-only $(ARGS)

PYRIGHT ?= .venv/bin/pyright --pythonpath .venv/bin/python
PYTEST ?= .venv/bin/pytest
PYTEST_DURATIONS ?= 10
PYTEST_DURATIONS_MIN ?= 1.0
PYTEST_TIMINGS ?= --durations=$(PYTEST_DURATIONS) --durations-min=$(PYTEST_DURATIONS_MIN)

define TIMED_RUN
	@target="$(1)"; \
	start=$$(date +%s); \
	start_hr=$$(date '+%Y-%m-%dT%H:%M:%S%z'); \
	echo "[validate-timing] START target=$$target at=$$start_hr"; \
	set +e; \
	{ $(2); }; \
	status=$$?; \
	end=$$(date +%s); \
	end_hr=$$(date '+%Y-%m-%dT%H:%M:%S%z'); \
	elapsed=$$((end-start)); \
	echo "[validate-timing] END target=$$target status=$$status elapsed=$${elapsed}s at=$$end_hr"; \
	exit $$status
endef

# Two-pass typecheck: strict for core (domain/ports/control), standard for rest
# --warnings ensures 0 warnings required (exit code 1 if warnings reported)
typecheck:
	$(call TIMED_RUN,typecheck,\
		echo "Running pyright (standard mode, excluding core)..." && \
		$(PYRIGHT) --project pyrightconfig.json --warnings && \
		echo "Running pyright (strict mode, core only)..." && \
		$(PYRIGHT) --project pyrightconfig.strict.json --warnings)

LINT_IMPORTS ?= .venv/bin/lint-imports
RUFF ?= .venv/bin/ruff

lint-arch: semgrep-venv
	$(call TIMED_RUN,lint-arch,\
		$(LINT_IMPORTS) && \
		$(PYTHON) tools/check_arch_guardrails.py src && \
		$(PYTHON) tools/quality_guardrails.py --fail-on-new && \
		scripts/check_agents_md.sh && \
		$(PYTHON) scripts/check_docs_md.py)

quality-guardrails: semgrep-venv
	$(call TIMED_RUN,quality-guardrails,\
		$(PYTHON) tools/quality_guardrails.py --fail-on-new)

quality-guardrails-stale: semgrep-venv
	$(call TIMED_RUN,quality-guardrails-stale,\
		$(PYTHON) tools/quality_guardrails.py --check-stale)

# Ruff guardrails - blocks on violations (C901 complexity, PLR0912 branches, SLF001 private access)
lint-complexity:
	$(call TIMED_RUN,lint-complexity,\
		echo "Checking code complexity (C901) and branch count (PLR0912)..." && \
		$(RUFF) check src packages/agent_runner/src --output-format=concise)

# Parallel test execution with pytest-xdist (-n auto uses all CPU cores)
# Use PARALLEL=0 to disable: make test-unit PARALLEL=0
PARALLEL ?= auto
UNIT_PARALLEL ?= $(PARALLEL)
SIMULATED_PARALLEL ?= $(PARALLEL)
INTEGRATION_PARALLEL ?= $(PARALLEL)
# Live provider-backed integration tests share authenticated local CLIs and
# provider account state. Run them serially unless explicitly overridden.
INTEGRATION_AGENT_PARALLEL ?= 0
INTEGRATION_AGENT_FILES := tests/integration/test_claude_execution.py tests/integration/test_codex_execution.py tests/integration/test_live_agent_chain.py
# Keep this list in sync with the -k exclusion in test-simulated-core.
# New agent-backed tests added to test_foreign_repo_lifecycle.py must be listed here
# so they move to test-simulated-agent instead of staying in the fast local slice.
SIMULATED_AGENT_FILES := tests/simulated_scenarios/test_foreign_repo_lifecycle.py::test_foreign_repo_claude_code_agent_done tests/simulated_scenarios/test_foreign_repo_lifecycle.py::test_foreign_repo_codex_agent_done

# Python interpreter for dependency checks
PYTHON ?= .venv/bin/python

# Marker file for tracking when deps were last synced
DEPS_MARKER ?= .venv/.deps-synced

# Auto-sync dependencies if pyproject.toml or uv.lock is newer than last sync
# This prevents cryptic errors like "unrecognized arguments: -n" when pytest-xdist is missing
sync-deps:
	@if [ ! -f $(DEPS_MARKER) ] || [ pyproject.toml -nt $(DEPS_MARKER) ] || [ uv.lock -nt $(DEPS_MARKER) ]; then \
		echo ""; \
		echo "================================================================"; \
		echo "[sync-deps] Dependencies changed since last install"; \
		echo "[sync-deps] Auto-syncing dependencies on your behalf..."; \
		echo "================================================================"; \
		if [ ! -x "$(UV)" ]; then \
			echo "ERROR: uv not found. Run: curl -LsSf https://astral.sh/uv/install.sh | sh"; \
			exit 1; \
		fi; \
		$(UV) sync --frozen --all-extras && touch $(DEPS_MARKER) && \
		echo "[sync-deps] Done. Continuing with original command..."; \
		echo ""; \
	fi

test-unit: sync-deps
ifeq ($(UNIT_PARALLEL),0)
	$(call TIMED_RUN,test-unit,\
		$(PYTEST) tests/unit packages/agent_runner/tests -x -q --tb=short $(PYTEST_TIMINGS))
else
	$(call TIMED_RUN,test-unit,\
		$(PYTEST) tests/unit packages/agent_runner/tests -x -q --tb=short -n $(UNIT_PARALLEL) --dist=loadgroup $(PYTEST_TIMINGS))
endif

test-simulated: sync-deps
ifeq ($(SIMULATED_PARALLEL),0)
	$(PYTEST) tests/simulated_scenarios -x -q --tb=short $(PYTEST_TIMINGS)
else
	$(PYTEST) tests/simulated_scenarios -x -q --tb=short -n $(SIMULATED_PARALLEL) --dist=loadgroup $(PYTEST_TIMINGS)
endif

test-simulated-core: sync-deps
ifeq ($(SIMULATED_PARALLEL),0)
	$(call TIMED_RUN,test-simulated-core,\
		$(PYTEST) tests/simulated_scenarios -x -q --tb=short \
			--ignore=tests/simulated_scenarios/test_foreign_repo_lifecycle.py \
			$(PYTEST_TIMINGS) && \
		$(PYTEST) tests/simulated_scenarios/test_foreign_repo_lifecycle.py -x -q --tb=short \
			-k "not test_foreign_repo_claude_code_agent_done and not test_foreign_repo_codex_agent_done" \
			$(PYTEST_TIMINGS))
else
	$(call TIMED_RUN,test-simulated-core,\
		$(PYTEST) tests/simulated_scenarios -x -q --tb=short -n $(SIMULATED_PARALLEL) --dist=loadgroup \
			--ignore=tests/simulated_scenarios/test_foreign_repo_lifecycle.py \
			$(PYTEST_TIMINGS) && \
		$(PYTEST) tests/simulated_scenarios/test_foreign_repo_lifecycle.py -x -q --tb=short -n $(SIMULATED_PARALLEL) --dist=loadgroup \
			-k "not test_foreign_repo_claude_code_agent_done and not test_foreign_repo_codex_agent_done" \
			$(PYTEST_TIMINGS))
endif

test-simulated-agent: sync-deps
ifeq ($(SIMULATED_PARALLEL),0)
	$(call TIMED_RUN,test-simulated-agent,\
		$(PYTEST) $(SIMULATED_AGENT_FILES) -x -q --tb=short $(PYTEST_TIMINGS))
else
	$(call TIMED_RUN,test-simulated-agent,\
		$(PYTEST) $(SIMULATED_AGENT_FILES) -x -q --tb=short -n $(SIMULATED_PARALLEL) --dist=loadgroup $(PYTEST_TIMINGS))
endif

test-unit-cov:
	$(PYTEST) tests/unit packages/agent_runner/tests --cov=src/issue_orchestrator --cov=packages/agent_runner/src --cov-report=term-missing -x -q --tb=short $(PYTEST_TIMINGS)

test-unit-cov-html:
	$(PYTEST) tests/unit packages/agent_runner/tests --cov=src/issue_orchestrator --cov=packages/agent_runner/src --cov-report=html -x -q --tb=short $(PYTEST_TIMINGS)
	@echo "Coverage report: open htmlcov/index.html"

test-integration: sync-deps
	$(PYTEST) tests/integration -x -q --tb=short $(PYTEST_TIMINGS)

# Integration tests excluding those that require external infrastructure (GitHub token, etc.)
# Used in pre-push validation where full infra may not be available
test-integration-core: test-integration-core-local test-integration-core-live-codex

test-integration-core-local: sync-deps
ifeq ($(INTEGRATION_PARALLEL),0)
	$(call TIMED_RUN,test-integration-core,\
		$(PYTEST) tests/integration -x -q --tb=short -m "not requires_infra and not live_codex" \
			--ignore=tests/integration/test_claude_execution.py \
			--ignore=tests/integration/test_codex_execution.py \
			--ignore=tests/integration/test_live_agent_chain.py \
			$(PYTEST_TIMINGS))
else
	$(call TIMED_RUN,test-integration-core,\
		$(PYTEST) tests/integration -x -q --tb=short -m "not requires_infra and not live_codex" -n $(INTEGRATION_PARALLEL) --dist=loadgroup \
			--ignore=tests/integration/test_claude_execution.py \
			--ignore=tests/integration/test_codex_execution.py \
			--ignore=tests/integration/test_live_agent_chain.py \
			$(PYTEST_TIMINGS))
endif

test-integration-core-live-codex: sync-deps
	$(call TIMED_RUN,test-integration-core-live-codex,\
		$(PYTEST) tests/integration -x -q --tb=short -m "live_codex and not requires_infra" \
			--ignore=tests/integration/test_claude_execution.py \
			--ignore=tests/integration/test_codex_execution.py \
			--ignore=tests/integration/test_live_agent_chain.py \
			$(PYTEST_TIMINGS))

# Backward-compatible alias for existing callers.
test-integration-no-infra: test-integration-core

test-integration-agent: sync-deps
ifeq ($(INTEGRATION_AGENT_PARALLEL),0)
	$(call TIMED_RUN,test-integration-agent,\
		$(PYTEST) $(INTEGRATION_AGENT_FILES) -x -q --tb=short $(PYTEST_TIMINGS))
else
	$(call TIMED_RUN,test-integration-agent,\
		$(PYTEST) $(INTEGRATION_AGENT_FILES) -x -q --tb=short -n $(INTEGRATION_AGENT_PARALLEL) --dist=loadgroup $(PYTEST_TIMINGS))
endif

# Full integration tests including infrastructure-dependent ones (run in CI)
test-integration-full: sync-deps
ifeq ($(PARALLEL),0)
	$(PYTEST) tests/integration -x -q --tb=short $(PYTEST_TIMINGS)
else
	$(PYTEST) tests/integration -x -q --tb=short -n $(PARALLEL) --dist=loadgroup $(PYTEST_TIMINGS)
endif

# E2E tests stop on first failure by default. Use NOFAST=1 to run all tests.
# Usage: make test-e2e        (stops on first failure)
#        make test-e2e NOFAST=1  (runs all tests even if some fail)
test-e2e:
ifdef NOFAST
	$(PYTEST) tests/e2e -v -s --tb=short $(PYTEST_TIMINGS)
else
	$(PYTEST) tests/e2e -v -s --tb=short -x $(PYTEST_TIMINGS)
endif

test-e2e-heavy:
	$(PYTEST) tests/integration tests/e2e -m heavy_e2e -v -s --tb=short -x $(PYTEST_TIMINGS)

test-e2e-onboarding-live:
	E2E_AGENT_GUIDED_ONBOARDING=1 $(PYTEST) tests/e2e/test_agent_guided_onboarding.py -v -s --tb=short -x $(PYTEST_TIMINGS)

# Real Claude tests - layered for incremental debugging
# test-real-claude-dev: dev agent only (faster, good for basic sanity)
# test-real-claude-review: dev + review agent (full happy path)

test-real-claude-dev:
	@echo "Testing agent-done invocation from Claude..."
	$(PYTEST) tests/integration/test_claude_execution.py::TestAgentDoneInvocation -v -s --tb=short -x $(PYTEST_TIMINGS)
	@echo "Testing real Claude execution in tmux mode..."
	E2E_DRY_RUN_PUSH=false $(PYTEST) tests/e2e/test_terminal_adapter.py::TestTerminalAdapterExecution -v -s --tb=short -x $(PYTEST_TIMINGS)
	@echo "✓ Dev agent tests passed!"

test-real-claude-review:
	@echo "Testing full pipeline: dev agent -> review agent..."
	@echo "Note: This test creates REAL PRs (not dry-run)"
	E2E_DRY_RUN_PUSH=false $(PYTEST) tests/e2e/test_review_agent.py::TestReviewAgentExecution -v -s --tb=short -x $(PYTEST_TIMINGS)
	@echo "✓ Review agent tests passed!"

test-real-gh-labels:
	@echo "Testing label write verification against real GitHub..."
	E2E_DRY_RUN_PUSH=false $(PYTEST) tests/e2e/test_label_write_verification.py::TestLabelWriteVerification -v -s --tb=short -x $(PYTEST_TIMINGS)
	@echo "✓ Label write verification passed!"

test-real-gh: test-real-claude-dev test-real-claude-review test-real-gh-labels
	@echo "✓ Real GitHub suite passed!"

test-real-gh-plus-e2e: test-real-gh test-e2e
	@echo "✓ Real GitHub + e2e suite passed!"

test-real-gh-plus-e2e-subprocess:
	@echo "✓ Running real GitHub + e2e suite with subprocess backend"
	E2E_TERMINAL_ADAPTER=subprocess $(GMAKE) test-real-gh
	E2E_TERMINAL_ADAPTER=subprocess $(GMAKE) test-e2e
	@echo "✓ Real GitHub + e2e subprocess suite passed!"

# Run a single e2e test by name. Usage: make test-e2e-one TEST=test_code_review_produces_review_comment
# E2E tests stop on first failure by default
test-e2e-one:
ifdef NOFAST
	$(PYTEST) tests/e2e -v -s --tb=short -k "$(TEST)" $(PYTEST_TIMINGS)
else
	$(PYTEST) tests/e2e -v -s --tb=short -x -k "$(TEST)" $(PYTEST_TIMINGS)
endif

# Run e2e tests with REAL PR creation on GitHub (no dry run)
# WARNING: This creates actual PRs and branches on the target repo!
# Use TEST= to run a specific test, e.g.: make test-e2e-live TEST=test_code_review
test-e2e-live:
	@echo "⚠️  Running e2e tests with REAL PR creation (no dry run)!"
	@echo "   This will create actual PRs and branches on GitHub."
	@echo ""
ifdef TEST
	E2E_DRY_RUN_PUSH= $(PYTEST) tests/e2e -v -s --tb=short -x -k "$(TEST)" $(PYTEST_TIMINGS)
else
	E2E_DRY_RUN_PUSH= $(PYTEST) tests/e2e -v -s --tb=short -x $(PYTEST_TIMINGS)
endif

test:
	$(PYTEST) tests/ -x -q --tb=short $(PYTEST_TIMINGS)

# Playwright browser smoke tests for Flow-first web UI
test-web:
	$(call TIMED_RUN,test-web,\
		$(PYTEST) tests/e2e_web -v --tb=short $(PYTEST_TIMINGS))

test-web-headed:
	$(PYTEST) tests/e2e_web -v --tb=short --headed $(PYTEST_TIMINGS)

# VS Code extension tests (local only). Skipped in GitHub Actions.
test-vscode:
ifneq ($(GITHUB_ACTIONS),)
	$(call TIMED_RUN,test-vscode,\
		echo "Skipping test-vscode in GitHub Actions")
else
	$(call TIMED_RUN,test-vscode,\
		if [ ! -d "packages/vscode/node_modules" ]; then \
			echo "Missing packages/vscode/node_modules. Run: make install-vscode-extensions"; \
			exit 1; \
		fi && \
		cd packages/vscode && npm test)
endif

install-vscode-extensions:
	cd packages/vscode && npm install

playwright-install:
	playwright install chromium

# Quick validation for agent_gate (~45s)
validate-quick: typecheck test-unit

# Standard validation - runs through Python wrapper for output capture
# Output is saved to ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR or .issue-orchestrator/diagnostics/
# On failure, prints path to output file so agents can find failure details
validate:
	@$(PYTHON) -m issue_orchestrator.entrypoints.cli_tools.validate_runner --command "$(GMAKE) validate-raw"

# Required PR validation - cache-aware wrapper around the publish gate.
# This seeds the same HEAD+command record the pre-push hook reuses.
validate-pr:
	@./scripts/verify-pr.sh

# Raw validation - direct execution without output capture wrapper
# Use this as a fallback if the Python wrapper fails
VALIDATE_JOBS ?= $(shell sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 5)
VALIDATE_STATIC_JOBS ?= $(VALIDATE_JOBS)
VALIDATE_TEST_JOBS ?= 1
VALIDATE_WEB_JOBS ?= 1
# Run the browser smoke lane beside the single live-Codex core check after
# local xdist-heavy core tests pass.
VALIDATE_LIVE_WEB_JOBS ?= 2
VALIDATE_AGENT_JOBS ?= 1
VALIDATE_E2E_JOBS ?= 1

define VALIDATE_CONFIG
	@echo "[validate-timing] CONFIG validate_jobs=$(VALIDATE_JOBS) unit_parallel=$(UNIT_PARALLEL) simulated_parallel=$(SIMULATED_PARALLEL) integration_parallel=$(INTEGRATION_PARALLEL) integration_agent_parallel=$(INTEGRATION_AGENT_PARALLEL) static_jobs=$(VALIDATE_STATIC_JOBS) test_jobs=$(VALIDATE_TEST_JOBS) web_jobs=$(VALIDATE_WEB_JOBS) live_web_jobs=$(VALIDATE_LIVE_WEB_JOBS) agent_jobs=$(VALIDATE_AGENT_JOBS) e2e_jobs=$(VALIDATE_E2E_JOBS)"
endef

validate-raw:
	$(VALIDATE_CONFIG)
	@$(GMAKE) --output-sync=target _validate-impl
	@$(GMAKE) --output-sync=target test-vscode
	@echo "✓ All validations passed!"

# Raw required PR gate - runs the full PR suite without the cache-aware wrapper.
# validation.publish.cmd points here so pre-push validation (scripts/verify-pr.sh)
# does not re-enter itself. Prefer `make validate-pr` for day-to-day use; it is
# cache-aware and seeds the pre-push record. Use validate-pr-raw only when you
# intentionally need to force the full uncached suite at the current HEAD.
validate-pr-raw:
	$(VALIDATE_CONFIG)
	@$(GMAKE) --output-sync=target _validate-pr-impl
	@$(GMAKE) --output-sync=target test-vscode
	@echo "✓ Required PR validations passed!"

# Internal phased validation targets. Invoke through validate-raw,
# validate-pr-raw, or validate-full so timing metadata is emitted.
# Keep pytest suite fan-out low by default:
# each suite may already use xdist internally, so running many suites together
# can oversubscribe local CPUs and starve browser/subprocess tests.
_validate-impl:
	$(call TIMED_RUN,validate-static-phase,\
		$(GMAKE) -j$(VALIDATE_STATIC_JOBS) --output-sync=target _validate-static-impl)
	$(call TIMED_RUN,validate-core-tests-phase,\
		$(GMAKE) -j$(VALIDATE_TEST_JOBS) --output-sync=target _validate-core-tests-impl)
	$(call TIMED_RUN,validate-live-web-phase,\
		$(GMAKE) -j$(VALIDATE_LIVE_WEB_JOBS) --output-sync=target test-integration-core-live-codex test-web)

_validate-static-impl: typecheck lint-arch lint-complexity

_validate-core-tests-impl: test-unit test-simulated-core test-integration-core-local

_validate-pr-impl:
	$(call TIMED_RUN,validate-main-phase,\
		$(GMAKE) --output-sync=target _validate-impl)
	$(call TIMED_RUN,validate-agent-phase,\
		$(GMAKE) -j$(VALIDATE_AGENT_JOBS) --output-sync=target _validate-agent-impl)

_validate-agent-impl: test-simulated-agent test-integration-agent

# Full validation including e2e tests
validate-full:
	$(VALIDATE_CONFIG)
	@$(GMAKE) --output-sync=target _validate-full-impl
	@$(GMAKE) --output-sync=target test-vscode
	@echo "✓ All validations passed (including e2e)!"

_validate-full-impl:
	@$(GMAKE) --output-sync=target _validate-pr-impl
	@$(GMAKE) -j$(VALIDATE_E2E_JOBS) --output-sync=target test-e2e

verify-hooks-all:
	@.venv/bin/issue-orchestrator setup-hooks --config .issue-orchestrator/config/maintenance/hooks-validate.yaml

# Demo - show orchestrator features with mock data
demo:
	.venv/bin/issue-orchestrator demo

# Issue management
PYTHON ?= .venv/bin/python

issues-validate:
	$(PYTHON) scripts/issues.py validate $(ARGS)

issues-fix:
	$(PYTHON) scripts/issues.py fix --apply $(ARGS)

issues-fix-dry-run:
	$(PYTHON) scripts/issues.py fix $(ARGS)

issues-create:
	$(PYTHON) scripts/issues.py create $(ARGS)
