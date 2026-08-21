#!/usr/bin/env bash
# Wrapper: the implementation is a package resource so it ships with an
# installed issue-orchestrator and can be applied to arbitrary target repos.
# Shell callers (Make, Control Center) use this path; Python callers resolve
# the same file through VenvMutationAuthority.
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${here}/../src/issue_orchestrator/resources/venv_guard.sh" "$@"
