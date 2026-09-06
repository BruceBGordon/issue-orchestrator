"""Tech Lead board projection: orchestrator-authored rung-1 visibility (#6781).

A frozen view over the orchestrator-owned tech_lead ledgers and the tick's
observed facts — the operator-approval backlog, open pattern case files
(ranked by comment cadence), per-area case-file counts, and the last
health-review time — plus a deterministic markdown renderer. Everything here
is orchestrator-authored: no agent prose ever reaches the board.

The approval backlog is the union of two truths (#7014). The op ledger says
what an approval will EXECUTE (op type, target); the observed gate LABEL says
what is waiting. Neither alone is the backlog: promoted findings and plain
follow-up proposals carry the gate with no ledger row, so a ledger-only
projection printed "Open proposals: None." while twenty gated issues waited on
GitHub. The union renders every gated issue, ledger-backed or not.

Build inputs come from data the tick already holds (the authority store's rows
and the issues it already fetched); building the view makes zero GitHub calls.
The renderer is pure and deterministic: the same view always produces the same
markdown, so the publisher can throttle writes by content comparison and tests
can golden-match the output exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Sequence

from ..domain.tech_lead_session import PROPOSED_TECH_LEAD_LABEL

if TYPE_CHECKING:
    from ..domain.tech_lead_session import (
        GatedTechLeadProposal,
        StoredTechLeadOp,
        TechLeadCaseFileSummary,
    )

# Rendered in a table cell the projection has no fact for: a gated issue with
# no ledger op has no operation/target, and a ledger op the tick did not
# observe on GitHub has no title.
_UNKNOWN_CELL = "—"


@dataclass(frozen=True)
class TechLeadBoardProposalOp:
    """The act-level operation approving a proposal will execute (#6778)."""

    op_type: str
    target_issue_number: int


@dataclass(frozen=True)
class TechLeadBoardProposal:
    """One gated proposal awaiting operator approval, as shown on the board.

    ``op`` is None for a gated issue with no act-level op ledger row — a
    promoted finding or a proposed follow-up issue, where approval un-gates
    the issue for scheduling rather than executing a stored operation.
    """

    proposal_issue_number: int
    age_hours: int
    title: str = ""
    op: TechLeadBoardProposalOp | None = None


@dataclass(frozen=True)
class TechLeadBoardCaseFile:
    """One open pattern case file, as shown on the board."""

    issue_number: int
    title: str
    comment_count: int
    updated_at: str
    area: str


@dataclass(frozen=True)
class TechLeadBoardView:
    """Frozen board projection; input to :func:`render_tech_lead_board_md`."""

    open_proposals: tuple[TechLeadBoardProposal, ...]
    case_files: tuple[TechLeadBoardCaseFile, ...]
    area_counts: tuple[tuple[str, int], ...]
    last_health_review: str  # ISO timestamp; "" when never


def _proposal_age_hours(created_at: str, now: datetime) -> int:
    """Whole hours since the op was recorded; 0 for unparseable timestamps.

    Coarse on purpose: hour granularity keeps the rendered board stable
    within an hour, so the publisher's content-comparison throttle holds.
    """
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return 0
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0, int((now - created).total_seconds() // 3600))


def _merge_open_proposals(
    ops: Sequence[tuple[int, "StoredTechLeadOp"]],
    gated_proposals: Sequence["GatedTechLeadProposal"],
    now: datetime,
) -> tuple[TechLeadBoardProposal, ...]:
    """Union ledger truth and gate-label truth into one backlog (#7014).

    Keyed by proposal issue number, so a ledger-backed proposal the tick also
    observed on GitHub is ONE row carrying both halves: its operation from the
    ledger and its title from the observation. Age comes from whichever half
    knows when the proposal was filed, preferring the ledger's own record.
    """
    ops_by_issue = dict(ops)
    observed = {item.issue_number: item for item in gated_proposals}
    rows: list[TechLeadBoardProposal] = []
    for number in sorted({*ops_by_issue, *observed}):
        op = ops_by_issue.get(number)
        seen = observed.get(number)
        # One of the two halves always exists (the number came from their
        # union), so an absent ledger row means the observation is present.
        created_at = op.created_at if op is not None else observed[number].created_at
        rows.append(
            TechLeadBoardProposal(
                proposal_issue_number=number,
                age_hours=_proposal_age_hours(created_at, now),
                title=seen.title if seen is not None else "",
                op=(
                    TechLeadBoardProposalOp(
                        op_type=op.op_type, target_issue_number=op.target_issue_number
                    )
                    if op is not None
                    else None
                ),
            )
        )
    return tuple(rows)


def build_tech_lead_board_view(
    *,
    ops: Sequence[tuple[int, "StoredTechLeadOp"]],
    gated_proposals: Sequence["GatedTechLeadProposal"],
    case_files: Sequence["TechLeadCaseFileSummary"],
    area_counts: Sequence[tuple[str, int]],
    last_health_review_at: float,
    now: datetime,
) -> TechLeadBoardView:
    """Project the ledgers + observed facts onto the board.

    ``gated_proposals`` is required, not defaulted: the approval backlog is the
    board's whole point, and a caller that forgets to observe the gate label
    must fail loudly rather than silently render an empty one (#7014).

    Proposals sort by issue number (stable audit order); case files rank by
    comment count (the severity signal), then most recently updated, then
    issue number — the same priority order a health review should read
    them in.
    """
    proposals = _merge_open_proposals(ops, gated_proposals, now)
    ranked = sorted(case_files, key=lambda item: (item.updated_at, item.issue_number), reverse=True)
    ranked.sort(key=lambda item: item.comment_count, reverse=True)
    return TechLeadBoardView(
        open_proposals=proposals,
        case_files=tuple(
            TechLeadBoardCaseFile(
                issue_number=item.issue_number,
                title=item.title,
                comment_count=item.comment_count,
                updated_at=item.updated_at,
                area=item.area,
            )
            for item in ranked
        ),
        area_counts=tuple(area_counts),
        last_health_review=(
            datetime.fromtimestamp(last_health_review_at, tz=timezone.utc).isoformat()
            if last_health_review_at > 0
            else ""
        ),
    )


def render_tech_lead_board_md(view: TechLeadBoardView) -> str:
    """Render the board to markdown. Deterministic; orchestrator-authored."""
    lines: list[str] = [
        "# Tech Lead Board",
        "",
        "Orchestrator-authored projection of the tech_lead ledgers and the"
        " observed approval gates (ADR-0031 / #6781, #7014).",
        "",
        f"Last health review: {view.last_health_review or 'never'}",
        "",
        "## Open proposals",
        "",
    ]
    if view.open_proposals:
        lines.extend(
            [
                f"{len(view.open_proposals)} awaiting operator approval — remove"
                f" the `{PROPOSED_TECH_LEAD_LABEL}` label from a proposal to"
                " approve it.",
                "",
                "| Proposal | Operation | Target | Age | Title |",
                "|---|---|---|---|---|",
                *(_proposal_row(item) for item in view.open_proposals),
            ]
        )
        lines.extend(_ledgerless_proposal_note(view.open_proposals))
    else:
        lines.append("None.")
    lines.extend(["", "## Open pattern case files", ""])
    if view.case_files:
        lines.extend(
            [
                "| Case file | Title | Comments | Updated | Area |",
                "|---|---|---|---|---|",
                *(
                    f"| #{item.issue_number} | {_markdown_cell(item.title)}"
                    f" | {item.comment_count} | {item.updated_at or 'unknown'}"
                    f" | {_markdown_cell(item.area or 'unclassified')} |"
                    for item in view.case_files
                ),
            ]
        )
    else:
        lines.append("None.")
    lines.extend(["", "## Case files by area", ""])
    if view.area_counts:
        lines.extend(
            f"- {_markdown_cell(area)}: {count}" for area, count in view.area_counts
        )
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def _proposal_row(item: TechLeadBoardProposal) -> str:
    """One approval-backlog row; unknown halves render as the em dash."""
    op = item.op
    operation = f"`{op.op_type}`" if op is not None else _UNKNOWN_CELL
    target = f"#{op.target_issue_number}" if op is not None else _UNKNOWN_CELL
    return (
        f"| #{item.proposal_issue_number} | {operation} | {target}"
        f" | {_format_age(item.age_hours)}"
        f" | {_markdown_cell(item.title) or _UNKNOWN_CELL} |"
    )


def _ledgerless_proposal_note(
    proposals: Sequence[TechLeadBoardProposal],
) -> tuple[str, ...]:
    """Explain the em-dashed rows, only when the board actually has some.

    A gated issue without an op ledger row is not a defect — promoted findings
    and proposed follow-up issues are gated exactly that way — but the operator
    has to know that approving one schedules work rather than executing a
    recorded operation (#7014).
    """
    if all(item.op is not None for item in proposals):
        return ()
    return (
        "",
        f"`{_UNKNOWN_CELL}` operation: gated issue with no act-level operation"
        " recorded (a proposed follow-up or promoted finding); approving it"
        " releases the issue for scheduling.",
    )


def _format_age(hours: int) -> str:
    """Coarse, stable wait time: hours under two days, whole days beyond.

    Backlogs are measured in weeks once they go unnoticed, and "456h" hides
    that. Both granularities stay stable within an hour, so the publisher's
    content-comparison throttle still holds.
    """
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _markdown_cell(value: str) -> str:
    """Keep issue/label text from breaking the deterministic table shape."""
    return value.replace("\r", " ").replace("\n", " ").replace("|", r"\|")
