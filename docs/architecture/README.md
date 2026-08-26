# Architecture

For engineering conventions, dependency-injection rules, event vs log guidance, and package-level boundaries, see [AGENTS.md](../../AGENTS.md). Directory-specific `AGENTS.md` files under `src/` and `tests/` refine those rules for each area.

This page is about how the Issue-Orchestrator codebase is organized internally. For the distinction between the product thesis and this repo's implementation architecture, see [Issue-Orchestrator Internal Architecture](internal-architecture.md).

## System Overview

```mermaid
graph TB
    subgraph "Entry Points"
        CLI[CLI<br/>issue-orchestrator]
        WEB[Web UI<br/>localhost:8765]
    end

    subgraph "Control Plane"
        ORCH[Orchestrator]
        SCHED[Scheduler]
        PLAN[Planner]
        APPLY[ActionApplier]
        OBS[Observer]
    end

    subgraph "Domain"
        MOD[Models]
        EVT[Events/Catalog]
        DEP[Dependencies]
    end

    subgraph "Ports (Interfaces)"
        PT_REPO[RepositoryHost]
        PT_SESS[SessionRunner]
        PT_EVT[EventSink]
        PT_WC[WorkingCopy]
        PT_WT[WorktreeManager]
        PT_CMD[CommandRunner]
        PT_STORE[SessionStore]
    end

    subgraph "Adapters"
        GH[GitHubAdapter]
        TERM[Terminal Adapter]
        WT[Worktree Adapter]
        STORE[JsonSessionStore]
    end

    subgraph "Execution Support"
        SSE[SSE Plugin]
        PROV[Provider Factories]
    end

    subgraph "External Systems"
        GHAPI[GitHub API]
        TERMS[Terminal Sessions]
        BROWSER[Browser SSE]
        FS[Filesystem]
    end

    CLI --> ORCH
    WEB -->|REST API| ORCH
    WEB -->|SSE| SSE

    ORCH --> SCHED
    ORCH --> PLAN
    ORCH --> OBS
    PLAN --> APPLY

    SCHED --> DEP
    PLAN --> MOD
    OBS --> EVT

    APPLY --> PT_REPO
    APPLY --> PT_SESS
    APPLY --> PT_EVT
    OBS --> PT_WC
    ORCH --> PT_WT
    ORCH --> PT_STORE

    PT_REPO --> GH
    PT_SESS --> TERM
    PT_EVT --> SSE
    PT_WT --> WT
    PT_STORE --> STORE

    GH --> GHAPI
    TERM --> TERMS
    SSE --> BROWSER
    PROV --> GH
    PROV --> TERM
    WT --> FS
    STORE --> FS

    style WEB fill:#6366f1,color:#fff
    style CLI fill:#22c55e,color:#fff
    style ORCH fill:#f97316,color:#fff
    style GH fill:#0969da,color:#fff
```

## Further Reading

- [ADRs](ADR/README.md) — Architectural Decision Records
- [Internal Architecture](internal-architecture.md) — How this repo is built and enforced
- [Hook Enforcement](hooks.md) — Multi-layer guardrail system
- [Host Executor](host-executor.md) — Machine-wide adaptive command admission
- [Review Workflow](../development/REVIEW_WORKFLOW.md) — Code review, rework cycles, exchange mechanisms
- [Guardrails & Safety](../design/guardrails.md) — Safety model and trust boundaries
