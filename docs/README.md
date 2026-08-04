# Documentation

## For Users

Getting started with Issue Orchestrator:

- [A Software Engineering Control Plane for Agentic Development](journeys/software-engineering-control-plane.md) - Public thesis and visual walkthrough
- [Feature List](user/features.md) - Product capabilities grouped by user problem
- [Installation](user/installation.md)
- [Quickstart](user/quickstart.md)
- [Tutorial](user/tutorial.md) - Hands-on walkthrough
- [Configuration](user/configuration.md) - Getting started with config
- [Configuration Reference](user/configuration_reference.md) - Every config field
- [GitHub Permissions](user/github-permissions.md) - Token setup and scopes
- [Client Test Integrations](user/test-integrations.md) - Expose your repo's tests: validation gates, JUnit XML, artifacts, framework recipes
- [E2E Test Runner](user/e2e.md) - Async test execution
- [Goal Pilot](user/goal_pilot.md) *(planned)* - Autonomous goal-driven orchestration
- [VS Code Integration](user/vscode.md) - IDE integration via MCP
- [FAQ](user/faq.md)

## For Developers

Contributing to or extending Issue Orchestrator:

- [Testing Guide](development/TESTING.md)
- [Debugging Events](development/debugging.md)
- [Troubleshooting](development/TROUBLESHOOTING.md)
- [Review Workflow](development/REVIEW_WORKFLOW.md)
- [GitHub Token Setup](development/GITHUB_TOKEN_SETUP.md) - Token resolution internals
- [Caching & ETags](development/CACHING_ETAGS.md)
- [Worktree Hook Setup](development/WORKTREE_HOOK_SETUP.md) - Dev environment hook enforcement
- [Creating Guardrails](development/CREATE_GUARDRAILS.md) - Guide for setting up guardrails on any codebase
- [Control Center Lifecycle Checklist](development/control_center_lifecycle_checklist.md)

## For AI Agents

If you're an AI agent working on this codebase:

- Review [`AGENT_PROTOCOL.md`](../AGENT_PROTOCOL.md) for completion contracts
- Check `AGENTS.md` files in `src/` and `tests/` for context-specific guidance

## Architecture

System design and decision records:

- [Architecture Overview](architecture/README.md) - Core architecture and boundaries
- [Issue-Orchestrator Internal Architecture](architecture/internal-architecture.md) - How this repo is built and enforced
- [Architecture Decision Records](architecture/ADR/README.md) - Why decisions were made
- [Guardrails & Safety Model](design/guardrails.md) - Enforcement layers and trust boundaries
- [Hooks](architecture/hooks.md) - Multi-layer hook enforcement
- [Validation System](architecture/validation.md) - Publish gate design
- [Control Center Lifecycle](architecture/control_center_lifecycle_model.md) - UI/engine lifecycle model

## Design

- [Goal Pilot Design](design/goal-pilot.md) - Goal Pilot architecture and design
- [Blocked Issues UX Ideas](design/blocked-issues-ux-ideas.md) - UX improvement brainstorm

## Archive

- [Archive Index](archive/README.md) - Historical planning notes and incident playbooks
