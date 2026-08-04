"""Unit tests for the tech_lead artifact pair contract (domain/tech_lead_artifacts.py)."""

from typing import cast

import pytest

from issue_orchestrator.domain.tech_lead_artifacts import (
    ACT_LEVEL_TECH_LEAD_ACTIONS,
    MAX_ACTION_BODY_CHARS,
    MAX_TECH_LEAD_ACTIONS,
    MAX_TECH_LEAD_FINDINGS,
    ProposedTechLeadAction,
    TechLeadDecision,
    TechLeadFinding,
    VALID_TECH_LEAD_ACTION_TYPES,
    validate_tech_lead_report_links,
)


def _finding(fid="T1", **overrides):
    payload = {
        "id": fid,
        "title": "Sessions hang after restart",
        "classification": "infra",
        "evidence": ["sessions/issue-12/log tail", "timeline issue 12"],
    }
    payload.update(overrides)
    return payload


def _action(aid="A1", **overrides):
    payload = {
        "id": aid,
        "action_type": "post_comment",
        "target_number": 12,
        "body": "Diagnosis: stale callback token after orchestrator restart.",
        "finding_ids": ["T1"],
    }
    payload.update(overrides)
    return payload


def _payload(**overrides):
    payload = {
        "schema_version": 1,
        "summary": "One systemic infra fault affecting three sessions.",
        "findings": [_finding()],
        "proposed_actions": [_action()],
    }
    payload.update(overrides)
    return payload


class TestTechLeadDecisionParsing:
    def test_round_trip(self):
        decision = TechLeadDecision.from_agent_payload(_payload())
        again = TechLeadDecision.from_agent_payload(decision.to_dict())
        assert again == decision
        assert again.summary.startswith("One systemic")
        assert again.findings[0].classification == "infra"
        assert again.proposed_actions[0].action_type == "post_comment"

    def test_accepts_nested_decision_key(self):
        decision = TechLeadDecision.from_agent_payload({"decision": _payload()})
        assert decision.findings[0].id == "T1"

    def test_extra_keys_preserved(self):
        decision = TechLeadDecision.from_agent_payload(
            _payload(agent_notes="something the schema does not know")
        )
        assert decision.extra["agent_notes"] == "something the schema does not know"
        assert "agent_notes" in decision.to_dict()

    @pytest.mark.parametrize("bad", [None, [], "text", 7])
    def test_rejects_non_object_payload(self, bad):
        with pytest.raises(ValueError, match="JSON object"):
            TechLeadDecision.from_agent_payload(bad)

    def test_rejects_unknown_schema_version(self):
        with pytest.raises(ValueError, match="schema_version"):
            TechLeadDecision.from_agent_payload(_payload(schema_version=2))

    def test_rejects_missing_summary(self):
        payload = _payload()
        del payload["summary"]
        with pytest.raises(ValueError, match="summary"):
            TechLeadDecision.from_agent_payload(payload)

    def test_rejects_non_list_findings(self):
        with pytest.raises(ValueError, match="findings must be a list"):
            TechLeadDecision.from_agent_payload(_payload(findings={"id": "T1"}))

    def test_rejects_too_many_findings(self):
        findings = [_finding(f"T{n}") for n in range(MAX_TECH_LEAD_FINDINGS + 1)]
        with pytest.raises(ValueError, match="max"):
            TechLeadDecision.from_agent_payload(_payload(findings=findings))

    def test_rejects_too_many_actions(self):
        actions = [_action(f"A{n}") for n in range(MAX_TECH_LEAD_ACTIONS + 1)]
        with pytest.raises(ValueError, match="max"):
            TechLeadDecision.from_agent_payload(_payload(proposed_actions=actions))

    def test_rejects_duplicate_finding_ids(self):
        with pytest.raises(ValueError, match="duplicate finding ids"):
            TechLeadDecision.from_agent_payload(
                _payload(findings=[_finding("T1"), _finding("T1")])
            )

    def test_rejects_duplicate_action_ids(self):
        with pytest.raises(ValueError, match="duplicate proposed action ids"):
            TechLeadDecision.from_agent_payload(
                _payload(proposed_actions=[_action("A1"), _action("A1")])
            )

    def test_rejects_multiple_act_level_actions_for_one_target(self):
        actions = [
            _action(
                "A1",
                action_type="reset_retry",
                target_number=17,
                body="Reset the corrupted worktree.",
            ),
            _action(
                "A2",
                action_type="reset_retry",
                target_number=17,
                body="Retry the same issue from scratch.",
            ),
        ]

        with pytest.raises(
            ValueError,
            match=r"multiple act-level proposed actions target #17: A1, A2",
        ):
            TechLeadDecision.from_agent_payload(_payload(proposed_actions=actions))

    def test_rejects_unknown_finding_reference(self):
        with pytest.raises(ValueError, match="unknown finding ids"):
            TechLeadDecision.from_agent_payload(
                _payload(proposed_actions=[_action(finding_ids=["T404"])])
            )


class TestTechLeadFindingParsing:
    def test_rejects_invalid_classification(self):
        with pytest.raises(ValueError, match="classification"):
            TechLeadDecision.from_agent_payload(
                _payload(findings=[_finding(classification="vibes")])
            )

    def test_rejects_non_object_finding(self):
        with pytest.raises(ValueError, match="must be an object"):
            TechLeadDecision.from_agent_payload(_payload(findings=["just a string"]))

    def test_rejects_missing_title(self):
        finding = _finding()
        del finding["title"]
        with pytest.raises(ValueError, match="title"):
            TechLeadDecision.from_agent_payload(_payload(findings=[finding]))


class TestProposedActionParsing:
    def test_rejects_invalid_action_type(self):
        with pytest.raises(ValueError, match="action_type"):
            TechLeadDecision.from_agent_payload(
                _payload(proposed_actions=[_action(action_type="merge_pr")])
            )

    @pytest.mark.parametrize("bad_target", [0, -3, True, "12"])
    def test_rejects_invalid_target_number(self, bad_target):
        with pytest.raises(ValueError, match="target_number"):
            TechLeadDecision.from_agent_payload(
                _payload(proposed_actions=[_action(target_number=bad_target)])
            )

    def test_rejects_oversized_body(self):
        with pytest.raises(ValueError, match="exceeds"):
            TechLeadDecision.from_agent_payload(
                _payload(
                    proposed_actions=[_action(body="x" * (MAX_ACTION_BODY_CHARS + 1))]
                )
            )

    def test_rejects_disallowed_label_characters(self):
        with pytest.raises(ValueError, match="disallowed"):
            TechLeadDecision.from_agent_payload(
                _payload(
                    proposed_actions=[
                        _action(
                            action_type="create_issue",
                            title="Fix it",
                            labels=["ok-label", "bad\nlabel"],
                        )
                    ]
                )
            )

    def test_post_comment_requires_target_and_body(self):
        action = _action()
        del action["target_number"]
        with pytest.raises(ValueError, match="requires target_number"):
            TechLeadDecision.from_agent_payload(_payload(proposed_actions=[action]))
        action = _action()
        del action["body"]
        with pytest.raises(ValueError, match="requires body"):
            TechLeadDecision.from_agent_payload(_payload(proposed_actions=[action]))

    def test_create_issue_requires_title_and_body(self):
        action = _action(action_type="create_issue")
        del action["target_number"]
        action.pop("title", None)
        with pytest.raises(ValueError, match="requires title"):
            TechLeadDecision.from_agent_payload(_payload(proposed_actions=[action]))

    def test_escalate_requires_target_and_body(self):
        action = _action(action_type="escalate_to_human")
        del action["target_number"]
        with pytest.raises(ValueError, match="requires target_number"):
            TechLeadDecision.from_agent_payload(_payload(proposed_actions=[action]))

    def test_flag_pattern_requires_body_and_signature(self):
        """#6781: flag_pattern needs body PLUS a pattern_signature (the durable
        case-file ledger key); no target."""
        action = {
            "id": "A1",
            "action_type": "flag_pattern",
            "body": "Every timeout follows a provider 429 burst.",
            "pattern_signature": "provider-429-timeout",
            "finding_ids": ["T1"],
        }
        decision = TechLeadDecision.from_agent_payload(
            _payload(proposed_actions=[action])
        )
        parsed = decision.proposed_actions[0]
        assert parsed.target_number is None
        assert parsed.pattern_signature == "provider-429-timeout"

    def test_flag_pattern_without_signature_is_contract_violation(self):
        """A flag_pattern that cannot accrue evidence is rejected (#6781)."""
        action = {
            "id": "A1",
            "action_type": "flag_pattern",
            "body": "Every timeout follows a provider 429 burst.",
            "finding_ids": ["T1"],
        }
        with pytest.raises(ValueError, match="requires pattern_signature"):
            TechLeadDecision.from_agent_payload(_payload(proposed_actions=[action]))

    def test_flag_pattern_blank_signature_is_rejected(self):
        """Direct construction bypasses from_mapping normalization; validate
        still rejects a present-but-blank signature (#6781)."""
        from issue_orchestrator.domain.tech_lead_artifacts import ProposedTechLeadAction

        action = ProposedTechLeadAction(
            id="A1", action_type="flag_pattern", body="b", pattern_signature="   "
        )
        with pytest.raises(ValueError, match="pattern_signature must be non-empty"):
            action.validate()

    def test_flag_pattern_area_must_be_label_safe(self):
        action = {
            "id": "A1",
            "action_type": "flag_pattern",
            "body": "b",
            "pattern_signature": "sig",
            "area": "bad area!",
            "finding_ids": ["T1"],
        }
        with pytest.raises(ValueError, match="area must be a non-empty label-safe"):
            TechLeadDecision.from_agent_payload(_payload(proposed_actions=[action]))

    def test_flag_pattern_round_trips_signature_and_area(self):
        action = {
            "id": "A1",
            "action_type": "flag_pattern",
            "body": "b",
            "pattern_signature": "db-pool-exhausted",
            "area": "db",
            "finding_ids": ["T1"],
        }
        decision = TechLeadDecision.from_agent_payload(
            _payload(proposed_actions=[action])
        )
        payload = decision.proposed_actions[0].to_dict()
        assert payload["pattern_signature"] == "db-pool-exhausted"
        assert payload["area"] == "db"

    def test_pattern_signature_is_bounded(self):
        from issue_orchestrator.domain.tech_lead_artifacts import (
            MAX_PATTERN_SIGNATURE_CHARS,
        )

        action = {
            "id": "A1",
            "action_type": "flag_pattern",
            "body": "b",
            "pattern_signature": "x" * (MAX_PATTERN_SIGNATURE_CHARS + 1),
            "finding_ids": ["T1"],
        }
        with pytest.raises(ValueError, match="pattern_signature"):
            TechLeadDecision.from_agent_payload(_payload(proposed_actions=[action]))

    def test_direct_signature_and_area_are_bounded(self):
        from issue_orchestrator.domain.tech_lead_artifacts import (
            MAX_AREA_CHARS,
            MAX_PATTERN_SIGNATURE_CHARS,
            ProposedTechLeadAction,
        )
        with pytest.raises(ValueError, match="pattern_signature"):
            ProposedTechLeadAction(
                id="A1", action_type="flag_pattern", body="b",
                pattern_signature="x" * (MAX_PATTERN_SIGNATURE_CHARS + 1),
            ).validate()
        with pytest.raises(ValueError, match="area"):
            ProposedTechLeadAction(
                id="A1", action_type="create_issue", title="t", body="b",
                area="x" * (MAX_AREA_CHARS + 1),
            ).validate()

    @pytest.mark.parametrize("act_type", sorted(ACT_LEVEL_TECH_LEAD_ACTIONS))
    def test_act_level_requires_target_and_rationale(self, act_type):
        action = _action(action_type=act_type)
        parsed = TechLeadDecision.from_agent_payload(_payload(proposed_actions=[action]))
        assert parsed.proposed_actions[0].is_act_level
        broken = _action(action_type=act_type)
        del broken["body"]
        with pytest.raises(ValueError, match="rationale"):
            TechLeadDecision.from_agent_payload(_payload(proposed_actions=[broken]))

    def test_act_level_registry_is_subset_of_vocabulary(self):
        assert ACT_LEVEL_TECH_LEAD_ACTIONS < VALID_TECH_LEAD_ACTION_TYPES


class TestReportLinkValidation:
    def test_passes_when_all_ids_mentioned(self):
        decision = TechLeadDecision.from_agent_payload(_payload())
        validate_tech_lead_report_links(
            decision, "# Report\n\n## T1 hang diagnosis\nProposing A1."
        )

    def test_raises_listing_missing_ids(self):
        decision = TechLeadDecision.from_agent_payload(_payload())
        with pytest.raises(ValueError, match="T1"):
            validate_tech_lead_report_links(decision, "# Report with no ids")


class TestEvidenceRequired:
    """Findings must carry >=1 strictly-typed evidence reference (#6761 F9)."""

    def test_missing_evidence_is_contract_violation(self):
        finding = _finding()
        del finding["evidence"]
        with pytest.raises(ValueError, match="non-empty evidence list"):
            TechLeadDecision.from_agent_payload(_payload(findings=[finding]))

    def test_empty_evidence_list_is_contract_violation(self):
        with pytest.raises(ValueError, match="non-empty evidence list"):
            TechLeadDecision.from_agent_payload(
                _payload(findings=[_finding(evidence=[])])
            )

    def test_empty_string_evidence_item_is_contract_violation(self):
        with pytest.raises(ValueError, match="evidence #2 must be a non-empty string"):
            TechLeadDecision.from_agent_payload(
                _payload(findings=[_finding(evidence=["log tail", "   "])])
            )

    def test_non_string_evidence_item_is_contract_violation(self):
        with pytest.raises(ValueError, match="evidence #1 must be a non-empty string"):
            TechLeadDecision.from_agent_payload(
                _payload(findings=[_finding(evidence=[{"ref": 1}])])
            )

    def test_direct_construction_cannot_bypass_evidence(self):
        decision = TechLeadDecision(
            summary="s",
            findings=(
                TechLeadFinding(id="T1", title="t", classification="infra"),
            ),
        )
        with pytest.raises(ValueError, match="evidence"):
            decision.validate()


class TestCanonicalIds:
    """Finding ids are T<n>, action ids are A<n>; one shared namespace (#6761 F8)."""

    @pytest.mark.parametrize("bad_id", ["F1", "T0", "T01", "t1", "A1", "T", "T1x"])
    def test_non_canonical_finding_id_rejected(self, bad_id):
        with pytest.raises(ValueError, match="not canonical"):
            TechLeadDecision.from_agent_payload(_payload(findings=[_finding(bad_id)]))

    @pytest.mark.parametrize("bad_id", ["T1", "A0", "A01", "a1", "A", "A2b"])
    def test_non_canonical_action_id_rejected(self, bad_id):
        with pytest.raises(ValueError, match="not canonical"):
            TechLeadDecision.from_agent_payload(
                _payload(proposed_actions=[_action(bad_id)])
            )

    def test_cross_namespace_collision_rejected_on_direct_construction(self):
        """A finding and an action sharing an id must not validate."""
        finding = TechLeadFinding(
            id="T1", title="t", classification="infra", evidence=("log",)
        )
        action = ProposedTechLeadAction(
            id="T1", action_type="flag_pattern", body="pattern"
        )
        decision = TechLeadDecision(
            summary="s", findings=(finding,), proposed_actions=(action,)
        )
        with pytest.raises(ValueError, match="share a namespace|not canonical"):
            decision.validate()

    def test_combined_namespace_duplicates_rejected(self):
        """Even if id forms drift, the combined namespace stays unique."""
        from issue_orchestrator.domain import tech_lead_artifacts

        finding = TechLeadFinding(
            id="T1", title="t", classification="infra", evidence=("log",)
        )
        action = ProposedTechLeadAction(
            id="T1", action_type="flag_pattern", body="pattern"
        )
        decision = TechLeadDecision(
            summary="s", findings=(finding,), proposed_actions=(action,)
        )
        original = tech_lead_artifacts._ACTION_ID_RE
        tech_lead_artifacts._ACTION_ID_RE = tech_lead_artifacts._FINDING_ID_RE
        try:
            with pytest.raises(ValueError, match="share a namespace"):
                decision.validate()
        finally:
            tech_lead_artifacts._ACTION_ID_RE = original

    def test_report_token_match_is_exact_not_substring(self):
        """T1 must not be satisfied by a report that only mentions T10."""
        payload = _payload(
            findings=[_finding("T1"), _finding("T10")],
            proposed_actions=[_action(finding_ids=["T1"])],
        )
        decision = TechLeadDecision.from_agent_payload(payload)
        with pytest.raises(ValueError, match="exact token: T1(,|$)"):
            validate_tech_lead_report_links(decision, "# Report\n\nT10 and A1 only.")

    def test_report_token_match_accepts_exact_tokens(self):
        payload = _payload(
            findings=[_finding("T1"), _finding("T10")],
            proposed_actions=[_action(finding_ids=["T1"])],
        )
        decision = TechLeadDecision.from_agent_payload(payload)
        validate_tech_lead_report_links(decision, "T1, T10 and A1 (see report).")


class TestDirectConstruction:
    def test_finding_from_mapping_rejects_evidence_non_list(self):
        with pytest.raises(ValueError, match="non-empty evidence list"):
            TechLeadFinding.from_mapping(_finding(evidence={"ref": 1}), index=1)

    def test_action_validate_is_idempotent_on_parsed(self):
        action = ProposedTechLeadAction.from_mapping(_action(), index=1)
        action.validate()


class TestProposedActionExpedite:
    """The create_issue-only expedite urgency signal (#6870)."""

    def _create_issue(self, **overrides):
        payload = {
            "id": "A1",
            "action_type": "create_issue",
            "title": "Fix the flaky merge race NOW",
            "body": "It corrupts state on every 3rd run.",
        }
        payload.update(overrides)
        return payload

    def test_expedite_defaults_false(self):
        action = ProposedTechLeadAction.from_mapping(self._create_issue(), index=1)
        assert action.expedite is False
        # Absent from to_dict when false (mirrors target_is_pr/area).
        assert "expedite" not in action.to_dict()

    def test_expedite_parses_and_round_trips(self):
        action = ProposedTechLeadAction.from_mapping(
            self._create_issue(expedite=True), index=1
        )
        assert action.expedite is True
        payload = action.to_dict()
        assert payload["expedite"] is True
        # Full round-trip preserves the flag.
        assert ProposedTechLeadAction.from_mapping(payload, index=1).expedite is True

    def test_expedite_false_when_explicitly_false(self):
        action = ProposedTechLeadAction.from_mapping(
            self._create_issue(expedite=False), index=1
        )
        assert action.expedite is False

    @pytest.mark.parametrize("bad", ["false", "true", 1, 0, [], {}, [True], None])
    def test_expedite_rejects_non_boolean_untrusted_input(self, bad):
        # The decision file is untrusted; a non-JSON-boolean must fail loudly
        # rather than be coerced (bool("false") is True, bool(1) is True). An
        # explicitly PRESENT null is a contract violation too — distinct from an
        # absent key (see test_expedite_absent_is_default_false_not_rejected).
        with pytest.raises(ValueError, match="must be a JSON boolean"):
            ProposedTechLeadAction.from_mapping(
                self._create_issue(expedite=bad), index=1
            )

    def test_expedite_absent_is_default_false_not_rejected(self):
        # A truly ABSENT key (not present at all) is the documented default,
        # not a type error — unlike an explicit null, which is rejected above.
        payload = self._create_issue()
        assert "expedite" not in payload
        assert ProposedTechLeadAction.from_mapping(payload, index=1).expedite is False

    @pytest.mark.parametrize(
        "action_type,extra",
        [
            ("post_comment", {"target_number": 7, "body": "b"}),
            ("escalate_to_human", {"target_number": 7, "body": "b"}),
            ("flag_pattern", {"body": "b", "pattern_signature": "sig"}),
            ("reset_retry", {"target_number": 7, "body": "b"}),
            ("kill_hung_session", {"target_number": 7, "body": "b"}),
        ],
    )
    def test_expedite_rejected_on_non_create_issue(self, action_type, extra):
        payload = {"id": "A1", "action_type": action_type, "expedite": True}
        payload.update(extra)
        with pytest.raises(ValueError, match="only valid on create_issue"):
            ProposedTechLeadAction.from_mapping(payload, index=1)

    def test_expedite_rejected_on_direct_construction_bypass(self):
        # Direct construction bypasses from_mapping; validate() re-checks.
        action = ProposedTechLeadAction(
            id="A1",
            action_type="post_comment",
            target_number=7,
            body="b",
            expedite=True,
        )
        with pytest.raises(ValueError, match="only valid on create_issue"):
            action.validate()

    def test_expedite_decision_round_trips_through_payload(self):
        payload = _payload(
            findings=[_finding("T1")],
            proposed_actions=[
                _action(
                    aid="A1",
                    action_type="create_issue",
                    title="Urgent fix",
                    body="body",
                    target_number=None,
                    expedite=True,
                )
            ],
        )
        decision = TechLeadDecision.from_agent_payload(payload)
        assert decision.proposed_actions[0].expedite is True
        again = TechLeadDecision.from_agent_payload(decision.to_dict())
        assert again.proposed_actions[0].expedite is True


class TestProposedActionDuplicateOf:
    """#6878: the create_issue-only ``duplicate_of`` dedup intent."""

    def _create_issue(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": "A1",
            "action_type": "create_issue",
            "title": "Fix the flaky merge race",
            "body": "It corrupts state on every 3rd run.",
        }
        payload.update(overrides)
        return payload

    def test_defaults_none_and_absent_from_payload(self):
        action = ProposedTechLeadAction.from_mapping(self._create_issue(), index=1)
        assert action.duplicate_of is None
        assert "duplicate_of" not in action.to_dict()

    def test_parses_and_round_trips(self):
        action = ProposedTechLeadAction.from_mapping(
            self._create_issue(duplicate_of=42), index=1
        )
        assert action.duplicate_of == 42
        payload = action.to_dict()
        assert payload["duplicate_of"] == 42
        assert ProposedTechLeadAction.from_mapping(payload, index=1).duplicate_of == 42

    @pytest.mark.parametrize("bad", [0, -1, True, False, "42", 42.0, [], {}])
    def test_rejects_non_positive_int(self, bad):
        # Untrusted decision file: a non-positive-int duplicate_of must fail loudly
        # (bool is an int subclass, so True/False are rejected too).
        with pytest.raises(ValueError, match="duplicate_of must be a positive"):
            ProposedTechLeadAction.from_mapping(
                self._create_issue(duplicate_of=bad), index=1
            )

    @pytest.mark.parametrize(
        "action_type,extra",
        [
            ("post_comment", {"target_number": 7, "body": "b"}),
            ("escalate_to_human", {"target_number": 7, "body": "b"}),
            ("flag_pattern", {"body": "b", "pattern_signature": "sig"}),
            ("reset_retry", {"target_number": 7, "body": "b"}),
            ("kill_hung_session", {"target_number": 7, "body": "b"}),
        ],
    )
    def test_rejected_on_non_create_issue(self, action_type, extra):
        payload = {"id": "A1", "action_type": action_type, "duplicate_of": 9}
        payload.update(extra)
        with pytest.raises(ValueError, match="only valid on create_issue"):
            ProposedTechLeadAction.from_mapping(payload, index=1)

    def test_rejected_on_direct_construction_bypass(self):
        # Direct construction bypasses from_mapping; validate() re-checks.
        action = ProposedTechLeadAction(
            id="A1",
            action_type="post_comment",
            target_number=7,
            body="b",
            duplicate_of=9,
        )
        with pytest.raises(ValueError, match="only valid on create_issue"):
            action.validate()

    @pytest.mark.parametrize("bad", [42.0, "42", True])
    def test_validate_rechecks_int_type_on_direct_construction(self, bad: object):
        # Direct construction bypasses from_mapping's parse check; validate() must
        # still reject a non-int (float / str / bool) as a ValueError — not pass it
        # (42.0) and not raise TypeError (a str comparison).
        action = ProposedTechLeadAction(
            id="A1",
            action_type="create_issue",
            title="t",
            body="b",
            duplicate_of=cast(int, bad),
        )
        with pytest.raises(ValueError, match="duplicate_of must be a positive"):
            action.validate()

    def test_decision_round_trips_through_payload(self):
        payload = _payload(
            findings=[_finding("T1")],
            proposed_actions=[
                _action(
                    aid="A1",
                    action_type="create_issue",
                    title="Dup fix",
                    body="body",
                    target_number=None,
                    duplicate_of=77,
                )
            ],
        )
        decision = TechLeadDecision.from_agent_payload(payload)
        assert decision.proposed_actions[0].duplicate_of == 77
        again = TechLeadDecision.from_agent_payload(decision.to_dict())
        assert again.proposed_actions[0].duplicate_of == 77


class TestFixClassPromotionField:
    """#6957: flag_pattern's fix:code / fix:human promotion classification."""

    @staticmethod
    def _flag_pattern(**overrides):
        payload = {
            "id": "A1",
            "action_type": "flag_pattern",
            "body": "Anchor close buries the needs-human escalation.",
            "pattern_signature": "anchor-close-buries-needs-human-escalation",
        }
        payload.update(overrides)
        return payload

    def test_absent_fix_class_is_unclassified(self):
        action = ProposedTechLeadAction.from_mapping(self._flag_pattern(), index=1)
        assert action.fix_class is None
        assert "fix_class" not in action.to_dict()

    @pytest.mark.parametrize("value", ("code", "human"))
    def test_fix_class_parses_and_round_trips(self, value: str):
        action = ProposedTechLeadAction.from_mapping(
            self._flag_pattern(fix_class=value), index=1
        )
        assert action.fix_class == value
        payload = action.to_dict()
        assert payload["fix_class"] == value
        assert (
            ProposedTechLeadAction.from_mapping(payload, index=1).fix_class == value
        )

    @pytest.mark.parametrize("bad", ("codefix", "CODE", "unknown", "fix:code"))
    def test_out_of_vocabulary_fix_class_is_rejected(self, bad: str):
        """A typo must fail loudly, not silently make a pattern unpromotable."""
        with pytest.raises(ValueError, match="fix_class"):
            ProposedTechLeadAction.from_mapping(
                self._flag_pattern(fix_class=bad), index=1
            )

    def test_fix_class_rejected_on_non_flag_pattern_actions(self):
        with pytest.raises(ValueError, match="only valid on flag_pattern"):
            ProposedTechLeadAction.from_mapping(
                {
                    "id": "A1",
                    "action_type": "create_issue",
                    "title": "t",
                    "body": "b",
                    "fix_class": "code",
                },
                index=1,
            )

    def test_fix_class_rejected_on_direct_construction_bypass(self):
        action = ProposedTechLeadAction(
            id="A1",
            action_type="post_comment",
            target_number=5,
            body="b",
            fix_class="code",
        )
        with pytest.raises(ValueError, match="only valid on flag_pattern"):
            action.validate()
