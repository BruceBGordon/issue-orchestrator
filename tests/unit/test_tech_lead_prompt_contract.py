"""Guardrails keeping the three tech_lead prompt variants on the artifact contract.

#6761 finding 10: the no-manifest ("no PRs") path used to instruct a bare
``coding-done`` completion, which guarantees a ``missing_decision`` rejection
under the mandatory-pair rule. Every prompt variant must instead show the
minimal valid empty-audit pair written BEFORE ``coding-done``, and the JSON it
shows must actually validate against the domain contract.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.tech_lead_issue_policy import (
    protected_tech_lead_label_violations,
)
from issue_orchestrator.domain.tech_lead_artifacts import (
    MAX_ACTION_BODY_CHARS,
    MAX_AREA_CHARS,
    MAX_EVIDENCE_REFS,
    MAX_LABEL_CHARS,
    MAX_LABELS_PER_ACTION,
    MAX_PATTERN_SIGNATURE_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_TECH_LEAD_ACTIONS,
    MAX_TECH_LEAD_FINDINGS,
    MAX_TITLE_CHARS,
    TechLeadDecision,
)
from issue_orchestrator.entrypoints.setup_wizard_prompts import (
    build_tech_lead_review_prompt_text,
)
from issue_orchestrator.infra.config import Config

REPO_ROOT = Path(__file__).resolve().parents[2]

PROMPT_VARIANTS = {
    "setup_wizard": build_tech_lead_review_prompt_text("tech-lead-review", "tech-lead-reviewed"),
    "examples": (REPO_ROOT / "examples" / "prompts" / "tech-lead-review.md").read_text(),
    "repo_specific": (REPO_ROOT / "repo-specific" / "prompts" / "tech-lead.md").read_text(),
}

NO_MANIFEST_MARKER = "**If the manifest is missing or lists no PRs:**"


def _no_manifest_block(text: str) -> str:
    """The no-manifest instructions up to the next section heading."""
    assert NO_MANIFEST_MARKER in text, "no-manifest path missing from prompt"
    start = text.index(NO_MANIFEST_MARKER)
    match = re.search(r"\n#{2,3} ", text[start:])
    end = start + match.start() if match else len(text)
    return text[start:end]


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_no_manifest_path_writes_pair_before_coding_done(variant: str) -> None:
    block = _no_manifest_block(PROMPT_VARIANTS[variant])

    assert "tech-lead-decision.json" in block
    assert "tech-lead-report.md" in block
    assert "before completing" in block
    # The pair-writing instructions come BEFORE the completion command.
    assert "coding-done" in block
    assert block.index("tech-lead-decision.json") < block.index("coding-done completed")


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_no_manifest_empty_audit_json_is_contract_valid(variant: str) -> None:
    block = _no_manifest_block(PROMPT_VARIANTS[variant])
    match = re.search(r"<<'JSON'\n(.*?)\nJSON\n", block, re.DOTALL)
    assert match, "empty-audit heredoc JSON missing from no-manifest path"

    decision = TechLeadDecision.from_agent_payload(json.loads(match.group(1)))

    assert decision.findings == ()
    assert decision.proposed_actions == ()
    assert decision.summary


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_compact_decision_example_is_contract_valid(variant: str) -> None:
    """The worked example must parse (canonical ids, evidence, allowed labels)."""
    text = PROMPT_VARIANTS[variant]
    blocks = [
        block
        for block in re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
        if "proposed_actions" in block
    ]
    assert blocks, "compact tech-lead-decision.json example missing"

    decision = TechLeadDecision.from_agent_payload(json.loads(blocks[0]))

    assert decision.findings, "example should demonstrate a finding"
    assert all(finding.evidence for finding in decision.findings)
    config = Config()
    labels = LabelManager(config)
    for action in decision.proposed_actions:
        assert (
            protected_tech_lead_label_violations(
                action.labels, config=config, labels=labels
            )
            == []
        ), f"{variant} example proposes protected labels"


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_machine_field_bounds_are_documented(variant: str) -> None:
    """Agent-visible limits must not remain a completion-time surprise."""
    text = " ".join(PROMPT_VARIANTS[variant].split())
    expected_phrases = (
        f"`title` values are at most **{MAX_TITLE_CHARS:,} characters**",
        f"`summary` is at most {MAX_SUMMARY_CHARS:,} characters",
        f"`body` is at most {MAX_ACTION_BODY_CHARS:,} characters",
        f"at most {MAX_EVIDENCE_REFS:,} evidence references",
        f"at most {MAX_LABELS_PER_ACTION:,} labels",
        f"label is at most {MAX_LABEL_CHARS:,} characters",
        f"`pattern_signature` is at most {MAX_PATTERN_SIGNATURE_CHARS:,} characters",
        f"`area` is at most {MAX_AREA_CHARS:,} characters",
        f"at most {MAX_TECH_LEAD_FINDINGS:,} findings",
        f"{MAX_TECH_LEAD_ACTIONS:,} proposed actions",
    )
    for phrase in expected_phrases:
        assert phrase in text, f"{variant} does not document bound: {phrase}"
    assert "full explanation" in text


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_investigation_flavor_documents_focus_comment_rule(variant: str) -> None:
    """Finding 2's rule is orchestrator-enforced; the prompts must teach it."""
    text = PROMPT_VARIANTS[variant]
    assert "focus_issue_number" in text
    marker = text.index('"flavor": "failure_investigation"')
    # Scope to the failure-investigation flow (marker -> next level-2 heading)
    # rather than a fixed char window: the flow legitimately grew when the
    # tech-lead investigation rubric + evidence-map guidance were baked in, but
    # the focus post_comment rule must still live somewhere in that flow.
    rest = text[marker:]
    next_section = re.search(r"\n## (?!#)", rest)
    bullet = rest[: next_section.start()] if next_section else rest
    assert "post_comment" in bullet
    assert "target_number" in bullet


# --- Per-flavor flow isolation (#6763 finding 1) ---------------------------
#
# A health or failure-investigation session gets no PR manifest. If the
# manifest-read step or the "Empty batch" artifact-pair fallback lives in a
# section those flavors are told to follow, the session either fails on its
# intentionally absent manifest or publishes an empty-batch result instead of
# walking the board. Each prompt source must therefore isolate every
# batch-only instruction inside the Batch Review Flow. These strings are the
# unambiguous batch-only tells: the `manifest.json` read step and the literal
# "Empty batch" summary the empty-audit fallback writes.
_BATCH_ONLY_TELLS = ("manifest.json", "Empty batch")


def _flow_section(text: str, heading: str) -> str:
    """The named ``## <heading>`` flow, up to the next level-2 heading.

    Level-3 subsections (``### 1. Read the Manifest`` etc.) belong to their
    parent flow, so the section runs until the next ``## `` that is not
    ``### ``.
    """
    marker = f"## {heading}"
    assert marker in text, f"{heading!r} section missing"
    start = text.index(marker)
    rest = text[start + len(marker) :]
    match = re.search(r"\n## (?!#)", rest)
    end = start + len(marker) + (match.start() if match else len(rest))
    return text[start:end]


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_all_three_flavor_flows_are_present(variant: str) -> None:
    """The per-flavor structure the guardrails rely on exists in every source."""
    text = PROMPT_VARIANTS[variant]
    for heading in (
        "Batch Review Flow",
        "Failure Investigation Flow",
        "Health Review Flow",
    ):
        assert f"## {heading}" in text, f"{variant} missing '## {heading}'"


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_batch_flow_still_carries_the_manifest_and_empty_batch_steps(
    variant: str,
) -> None:
    """Non-vacuity: the tells the other flows must NOT contain live in batch."""
    batch = _flow_section(PROMPT_VARIANTS[variant], "Batch Review Flow")
    for tell in _BATCH_ONLY_TELLS:
        assert tell in batch, f"{variant} batch flow lost the {tell!r} step"


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
@pytest.mark.parametrize(
    "heading", ["Health Review Flow", "Failure Investigation Flow"]
)
def test_non_batch_flows_contain_no_batch_only_instructions(
    variant: str, heading: str
) -> None:
    """A no-manifest flavor never inherits a manifest-read or empty-batch step."""
    section = _flow_section(PROMPT_VARIANTS[variant], heading)
    for tell in _BATCH_ONLY_TELLS:
        assert tell not in section, (
            f"{variant} '{heading}' contains batch-only instruction {tell!r}"
        )


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_act_level_wiring_state_is_synchronized(variant: str) -> None:
    """#6764/#6778: every variant must document the gated-proposal tier
    (propose = reviewable issue, approval = removing the gate label), the
    wired reset_retry execute authority, and the agent's prohibition on the
    gate label itself."""
    text = PROMPT_VARIANTS[variant]
    assert "`tech_lead.authority.reset_retry: execute`" in text, (
        f"{variant} does not document the wired reset_retry authority"
    )
    assert "`proposed-tech-lead`" in text, (
        f"{variant} does not document the gated-proposal label"
    )
    assert "removing that label" in text, (
        f"{variant} does not document label removal as the approval gesture"
    )
    assert "Never propose or\n  touch the `proposed-tech-lead` label" in text, (
        f"{variant} does not forbid the agent from touching the gate label"
    )
    # The pre-#6778 shadow-only claim must be gone from every variant.
    assert "recorded as would-have-done until its" not in text, (
        f"{variant} still claims kill_hung_session is shadow-only"
    )


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_step_back_mandate_synchronized(variant: str) -> None:
    """#6781 amendment: every prompt variant must teach the durable case-file
    contract (flag_pattern needs a stable signature) AND the step-back
    mandate — a recurring same-signature pattern escalates to a deeper
    root-cause design-review issue, not another observation."""
    text = PROMPT_VARIANTS[variant]
    assert "pattern_signature" in text, (
        f"{variant} does not document the required flag_pattern signature"
    )
    assert "case file" in text, (
        f"{variant} does not document the durable case file"
    )
    assert "Step back on recurrence" in text, (
        f"{variant} does not teach the step-back mandate"
    )
    assert "root-cause design review" in text, (
        f"{variant} does not name the root-cause design-review escalation"
    )
    assert "mandate to" in text, (
        f"{variant} does not frame recurrence as a root-cause mandate"
    )


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_restart_safe_shipped_fix_evidence_is_synchronized(variant: str) -> None:
    text = PROMPT_VARIANTS[variant]
    assert "recent_shipped_fixes" in text
    assert "issue/PR" in text


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_health_flow_teaches_cohort_scoped_act_level_authority(
    variant: str,
) -> None:
    """#6780: the prompt must match the runtime act-level scope rule.

    The orchestrator records a health review's act-level authority from its
    OWNED cohort and rejects `reset_retry`/`kill_hung_session` proposals for
    anything outside it. The prompt used to tell the agent that act-level
    proposals "may only target THIS tracking issue", which made a storm
    review's whole reason for existing unusable: it could be authorized for
    #41/#42/#43 and still be instructed never to propose for them.
    """
    section = _flow_section(PROMPT_VARIANTS[variant], "Health Review Flow")

    # The superseded rule must be gone, or the agent is told not to use its
    # own authority.
    assert "act-level) may\n  only target THIS tracking issue" not in section, (
        f"{variant} still forbids act-level proposals for the owned cohort"
    )
    # Anchor-scoped proposals stay anchor-scoped.
    assert "`post_comment`/`escalate_to_human` may only target THIS tracking" in (
        section
    ), f"{variant} no longer scopes post_comment/escalate_to_human to the anchor"
    # Act-level scope is the cohort surface, named exactly as the snapshot
    # field the orchestrator writes and validates against.
    assert "problem_cohort" in section, (
        f"{variant} does not name the problem_cohort act-level scope"
    )
    for act_level in ("reset_retry", "kill_hung_session"):
        assert act_level in section, (
            f"{variant} does not name {act_level} in the health flow"
        )
    # An empty cohort grants nothing (the periodic-review case).
    assert "EMPTY `problem_cohort`" in section, (
        f"{variant} does not teach that an empty cohort grants no act-level targets"
    )
    # recent_failures is context, never authority.
    assert "`recent_failures` is CONTEXT, not authority" in section, (
        f"{variant} does not warn that recent_failures is not authority"
    )


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_generic_target_scope_rule_matches_the_two_scope_runtime_split(
    variant: str,
) -> None:
    """#6780 round-2 F1: the GENERIC artifact rule must not contradict the flow.

    ``test_health_flow_teaches_cohort_scoped_act_level_authority`` only reads
    the Health Review Flow section, so the later generic rule under Required
    Output Artifacts kept telling every agent that act-level proposals "may
    only target the manifest PRs or your own tracking issue ... or the
    `focus_issue_number`. Any other target is rejected." A storm review could
    therefore be launched holding cohort authority and still be instructed
    that its authorized cohort targets were invalid.

    The rule mirrors the runtime two-scope split
    (``TechLeadLaunchAuthority.allowed_targets`` vs
    ``allowed_act_level_targets``), which is DISJOINT for a health review:
    anchor for post_comment/escalate_to_human, cohort for act-level.
    """
    section = _flow_section(
        PROMPT_VARIANTS[variant], "Required Output Artifacts (MANDATORY)"
    )

    # The superseded single-scope rule must be gone: it lumped the act-level
    # verbs in with the anchor-scoped ones and never named the cohort.
    assert "and `kill_hung_session` may only\n  target the manifest PRs" not in (
        section
    ), f"{variant} generic rule still forbids the health review's cohort targets"
    # The generic rule must name the health-review act-level authority.
    assert "`problem_cohort` (health review)" in section, (
        f"{variant} generic rule does not name problem_cohort act-level authority"
    )
    # Anchor-scoped verbs stay anchor-scoped, including in a health review.
    assert "THIS tracking issue (health review)" in section, (
        f"{variant} generic rule does not keep post_comment anchor-scoped"
    )
    # A batch review's act-level scope is empty at runtime (frozenset()), so
    # the prompt must not invite a manifest-PR/anchor reset (#6764 F1).
    assert "no act-level target at all" in section, (
        f"{variant} generic rule does not teach that batch owns no act-level target"
    )


def test_generic_rule_act_level_verbs_match_the_domain_contract() -> None:
    """The generic rule names the REAL act-level verbs, not a drifted alias.

    Pins the prompt's act-level list to ``ACT_LEVEL_TECH_LEAD_ACTIONS`` so a new
    act-level action type cannot be added to the runtime scope check while the
    generic prompt rule silently keeps teaching the old pair.
    """
    from issue_orchestrator.domain.tech_lead_artifacts import ACT_LEVEL_TECH_LEAD_ACTIONS

    for variant, text in sorted(PROMPT_VARIANTS.items()):
        section = _flow_section(text, "Required Output Artifacts (MANDATORY)")
        act_level_rule = section[section.index("- Act-level ") :]
        for action_type in sorted(ACT_LEVEL_TECH_LEAD_ACTIONS):
            assert f"`{action_type}`" in act_level_rule, (
                f"{variant} generic act-level rule does not name {action_type}"
            )


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_board_snapshot_fields_document_the_cohort_surface(variant: str) -> None:
    """The snapshot field list must distinguish context from authority."""
    text = PROMPT_VARIANTS[variant]
    assert "`problem_cohort` (the issue" in text, (
        f"{variant} does not document the problem_cohort field"
    )
    assert "`recent_failures` (context)" in text, (
        f"{variant} does not mark recent_failures as context"
    )


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_board_snapshot_fields_document_e2e_health(variant: str) -> None:
    """The snapshot field list must surface the aggregate E2E-health signal."""
    assert "`e2e_health`" in PROMPT_VARIANTS[variant], (
        f"{variant} does not document the e2e_health snapshot field"
    )


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_health_flow_teaches_e2e_suite_health_assessment(variant: str) -> None:
    """The health review must assess E2E as a SYSTEM (ADR-0031).

    E2E health is easy to neglect — it runs on a slow ungoverned cadence and
    rots unwatched — so every variant's Health Review Flow must teach reading
    `e2e_health` (cadence/streak/chronic) and routing an off-cadence or
    chronically-red suite, and untracked/stale chronic failures, to findings.
    """
    section = _flow_section(PROMPT_VARIANTS[variant], "Health Review Flow")
    for token in (
        "e2e_health",
        "nonpassing_streak",
        "chronic_failures",
        "tracking_issue",
        "e2e suite health",
        "easy to neglect",
    ):
        assert token in section, (
            f"{variant} health flow does not teach the e2e-health token {token!r}"
        )


def test_prompt_e2e_health_rule_matches_the_snapshot_contract() -> None:
    """The prompt names the REAL serialized field, not a drifted alias.

    Pins ``e2e_health`` to the field ``BoardSnapshot.to_dict`` actually writes
    so a rename cannot leave the prompt pointing at a field that never exists.
    """
    from issue_orchestrator.domain.board_snapshot import BoardSnapshot

    snapshot = BoardSnapshot(
        generated_at="2026-07-15T00:00:00",
        orchestrator_paused=False,
    )
    assert "e2e_health" in snapshot.to_dict()


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_board_snapshot_fields_document_hung_evidence(variant: str) -> None:
    """The snapshot field list must surface the per-session hung-evidence."""
    text = PROMPT_VARIANTS[variant]
    for token in ("`idle_minutes`", "`commits_ahead`"):
        assert token in text, (
            f"{variant} does not document the {token} snapshot field"
        )


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_health_flow_teaches_evidence_based_hung_judgment(variant: str) -> None:
    """The health review must judge HUNG from EVIDENCE, not age alone (#6823).

    A session hung is judged from idle + no-progress evidence corroborated by
    the run dir/recording, NOT a timer — "take a look, don't kill prematurely".
    A long-running-but-working session (fresh output or landing commits) is not
    hung. The GATED ``kill_hung_session`` follows only from that evidence.
    """
    section = _flow_section(PROMPT_VARIANTS[variant], "Health Review Flow")
    for token in (
        "idle_minutes",
        "commits_ahead",
        "age_minutes` alone",
        "WORKING, not hung",
        "kill_hung_session",
        "prematurely",
    ):
        assert token in section, (
            f"{variant} health flow does not teach the hung-evidence token {token!r}"
        )


def test_prompt_hung_evidence_rule_matches_the_snapshot_contract() -> None:
    """The prompt names the REAL serialized session fields, not drifted aliases.

    Pins ``idle_minutes``/``commits_ahead`` to what ``BoardSnapshot.to_dict``
    writes per active session, so a rename cannot leave the rubric pointing at
    evidence fields the board never carries.
    """
    from issue_orchestrator.domain.board_snapshot import (
        BoardSessionInfo,
        BoardSnapshot,
    )

    snapshot = BoardSnapshot(
        generated_at="2026-07-15T00:00:00",
        orchestrator_paused=False,
        sessions=[
            BoardSessionInfo(
                issue_number=1,
                issue_title="t",
                agent_type="",
                session_type="code",
                status="running",
                started_at="2026-07-15T00:00:00",
                age_minutes=1,
                terminal_id="issue-1",
            )
        ],
    )
    session = snapshot.to_dict()["sessions"][0]
    assert "idle_minutes" in session
    assert "commits_ahead" in session


def test_prompt_cohort_rule_matches_the_snapshot_contract() -> None:
    """The prompt names the REAL serialized field, not a drifted alias.

    A guardrail asserting on a field name the orchestrator never writes would
    pass while the agent looked for something that does not exist.
    """
    from issue_orchestrator.domain.board_snapshot import BoardSnapshot

    snapshot = BoardSnapshot(
        generated_at="2026-07-15T00:00:00",
        orchestrator_paused=False,
        problem_cohort=[41],
    )
    assert "problem_cohort" in snapshot.to_dict()
    assert snapshot.problem_issue_numbers() == frozenset({41})


def _dedup_clause(text: str) -> str:
    """The bounded create_issue-dedup paragraph, lowercased.

    Scoping the semantic assertions to this clause (not the whole multi-flow
    prompt) is the point: words like "verif"/"gated" occur in unrelated sections
    (setup-wizard "Trust but verify", act-level GATED-proposal text), so a
    whole-text check would stay green even if the dedup clause lost its meaning.
    """
    start = text.index("Do not file a duplicate")
    marker = "only valid on `create_issue`"
    end = text.index(marker, start) + len(marker)
    return text[start:end].lower()


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_all_variants_teach_the_duplicate_of_dedup_field(variant: str) -> None:
    """#6878 B4: every prompt variant must teach create_issue dedup with the final
    verify-or-gate semantics — not the old unconditional comment-routing promise
    (production withholds auto-routing until increment 2). Asserted WITHIN the
    bounded dedup clause so unrelated prose can't satisfy it, in both directions."""
    clause = _dedup_clause(PROMPT_VARIANTS[variant])
    assert "duplicate_of" in clause, f"{variant} dedup clause omits duplicate_of"
    assert "untrusted" in clause, f"{variant} does not mark duplicate_of as untrusted intent"
    assert "verif" in clause, f"{variant} dedup clause omits verification semantics"
    assert "gated" in clause, f"{variant} dedup clause omits gating semantics"
    assert "candidate" in clause and "preserv" in clause, (
        f"{variant} dedup clause omits candidate preservation"
    )
    assert "title" in clause and "body" in clause, (
        f"{variant} dedup clause drops the title/body requirement"
    )
    assert (
        "instead of filing a duplicate" not in clause
    ), f"{variant} dedup clause still promises unconditional comment routing"
