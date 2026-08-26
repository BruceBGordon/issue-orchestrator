# Developer-only targets for issue-orchestrator development.
# These are NOT for end users of the orchestrator framework.
#
# Usage: make -f repo-specific/Makefile <target>

.PHONY: validate-profile stress-test

PYTEST ?= .venv/bin/pytest

# Repo-maintainer profiling target (not framework/user-facing).
# Usage:
#   make -f repo-specific/Makefile validate-profile
#   make -f repo-specific/Makefile validate-profile DRY_RUN=1
# The JSON report and its sibling *-artifacts directory retain the exact
# command output and bounded typed executor-event evidence.
validate-profile:
	@args=""; \
	if [ ! -x ".venv/bin/python" ]; then echo "Missing .venv/bin/python. Run: make worktree-setup"; exit 1; fi; \
	if [ "$(DRY_RUN)" = "1" ]; then args="$$args --dry-run"; fi; \
	if [ -n "$(TARGETS)" ]; then args="$$args --targets $(TARGETS)"; fi; \
	if [ -n "$(OUTPUT)" ]; then args="$$args --output $(OUTPUT)"; fi; \
	if [ -n "$(COMMAND_TIMEOUT_SECONDS)" ]; then args="$$args --command-timeout-seconds $(COMMAND_TIMEOUT_SECONDS)"; fi; \
	GMAKE="$${GMAKE:-$$(command -v gmake 2>/dev/null || command -v make)}" \
	.venv/bin/python repo-specific/scripts/validate_profile.py $$args

# Stress-test a single test under CPU saturation to flush out flaky timing bugs.
# Spawns one busy-loop per core, runs the test RUNS times, then kills the load.
# Usage: make -f repo-specific/Makefile stress-test TEST=tests/unit/test_foo.py::test_bar
#        make -f repo-specific/Makefile stress-test TEST=tests/unit/test_foo.py::test_bar RUNS=200
RUNS ?= 100
NCPU := $(shell sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4)
stress-test:
ifndef TEST
	$(error TEST is required. Usage: make -f repo-specific/Makefile stress-test TEST=tests/unit/test_foo.py::test_bar)
endif
	@echo "Stress-testing $(TEST) ($(RUNS) runs, $(NCPU) CPU burners)"
	@for i in $$(seq 1 $(NCPU)); do python3 -c "while True: pass" & done; \
	trap 'kill $$(jobs -p) 2>/dev/null' EXIT; \
	pass=0; fail=0; \
	for i in $$(seq 1 $(RUNS)); do \
		if $(PYTEST) "$(TEST)" -q --no-header --tb=line >/dev/null 2>&1; then \
			pass=$$((pass+1)); \
		else \
			fail=$$((fail+1)); \
			echo "FAIL on run $$i"; \
		fi; \
	done; \
	echo "Results: $$pass passed, $$fail failed out of $(RUNS)"; \
	[ $$fail -eq 0 ]
