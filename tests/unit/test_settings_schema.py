"""Tests for the data-driven settings schema."""

from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from issue_orchestrator.infra.config import Config, E2EConfig, FilteringConfig
from issue_orchestrator.domain.tech_lead_findings import (
    VALID_FINDING_PROMOTION_MODES,
)
from issue_orchestrator.infra.config_models import MERGE_QUEUE_PROVIDERS
from issue_orchestrator.infra.settings_schema import (
    CONFIG_VALUE_TYPE_PATH,
    DOCTOR_CHECK_FIRST_ARG_PATH_EXISTS,
    DOCTOR_CHECK_PATH_EXISTS,
    DOCTOR_CHECK_PATH_IS_FILE,
    DOCTOR_CHECK_REFERENCES_AGENT,
    FORM_CONTROL_DICT_ENUM,
    FORM_CONTROL_ENUM,
    FORM_CONTROL_KINDS,
    TAB_DEFINITIONS,
    UnsupportedSettingsFieldError,
    classify_form_control,
    AdvancedSettings,
    ConcurrencySettings,
    E2ESettings,
    FilteringSettings,
    MilestonesSettings,
    GoalPilotSettings,
    MergeQueueSettings,
    ReviewSettings,
    ValidationSettings,
    apply_to,
    from_config,
    generate_config_reference,
    get_doctor_check_fields,
    get_field_meta,
    get_restart_fields,
    get_settings_json_schema,
    get_setup_fields,
    get_summary_fields,
)
from issue_orchestrator.infra.settings_schema_support import (
    FORM_CONTROL_BOOLEAN,
    FORM_CONTROL_INTEGER,
    FORM_CONTROL_OPTIONAL_INTEGER,
)


# ---------------------------------------------------------------------------
# Model construction
# ---------------------------------------------------------------------------

class TestModelDefaults:
    """Each model should construct with its declared defaults."""

    def test_concurrency_defaults(self):
        m = ConcurrencySettings()
        assert m.max_concurrent_sessions == 3
        assert m.session_timeout_minutes == 45
        assert m.queue_refresh_seconds == 600
        assert m.fetch_layer_enabled is True
        assert m.fetch_layer_network_sync_seconds == 60
        assert m.fetch_layer_full_scan_interval_seconds == 1800
        assert m.fetch_layer_discovery_limit == 25
        assert m.fetch_layer_max_hot_issues_per_cycle == 40
        assert m.fetch_layer_pr_scan_every_n_refreshes == 2
        assert m.fetch_layer_dependency_scan_every_n_refreshes == 1
        assert m.fetch_layer_visibility_aware_enabled is False
        assert m.fetch_layer_selective_sync_planner_enabled is False
        assert m.default_priority_tier == 1

    def test_e2e_defaults(self):
        m = E2ESettings()
        assert m.enabled is False
        assert m.role == "auto"
        assert m.pytest_args == "tests/e2e -v"
        assert m.allow_retry_once is True
        assert m.stop_on_first_failure is False

    def test_validation_defaults(self):
        m = ValidationSettings()
        assert m.quick_cmd is None
        assert m.quick_timeout_seconds == 300
        assert m.publish_cmd is None
        assert m.publish_timeout_seconds == 1800
        assert m.publish_dirty_check == "tracked"

    def test_filtering_defaults(self):
        m = FilteringSettings()
        assert m.label is None
        assert m.milestones == ""
        assert m.exclude_labels == ""
        assert m.exclude_label_prefixes == ""
        assert m.fetch_limit == 100
        assert m.max_to_start == 0

    def test_review_defaults(self):
        m = ReviewSettings()
        assert m.enabled is False
        assert m.tech_lead_enabled is False
        assert m.default_reviewer is None
        assert m.max_rework_cycles == 5
        assert m.internal_enabled is False
        assert m.internal_max_rounds == 5
        assert m.internal_instructions == ".io/internal-review.md"
        # Reserved tech_lead concurrency defaults to unset (share worker budget).
        assert m.tech_lead_max_concurrent is None

    def test_tech_lead_max_concurrent_accepts_none_and_positive_int(self):
        assert ReviewSettings(tech_lead_max_concurrent=None).tech_lead_max_concurrent is None
        assert ReviewSettings(tech_lead_max_concurrent=1).tech_lead_max_concurrent == 1
        assert ReviewSettings(tech_lead_max_concurrent=3).tech_lead_max_concurrent == 3

    def test_tech_lead_max_concurrent_rejects_zero_and_negative(self):
        with pytest.raises(ValidationError):
            ReviewSettings(tech_lead_max_concurrent=0)
        with pytest.raises(ValidationError):
            ReviewSettings(tech_lead_max_concurrent=-1)

    def test_tech_lead_max_concurrent_classifies_to_optional_integer(self):
        prop = get_settings_json_schema()["review"]["properties"]["tech_lead_max_concurrent"]
        assert prop["x_control"]["kind"] == FORM_CONTROL_OPTIONAL_INTEGER

    def test_tech_lead_max_expedited_default(self):
        # Expedite lane cap (#6870) defaults to a small positive value.
        assert ReviewSettings().tech_lead_max_expedited == 3

    def test_tech_lead_max_expedited_accepts_zero_and_positive(self):
        # 0 disables the lane; positive values bound outstanding expedites.
        assert ReviewSettings(tech_lead_max_expedited=0).tech_lead_max_expedited == 0
        assert ReviewSettings(tech_lead_max_expedited=5).tech_lead_max_expedited == 5

    def test_tech_lead_max_expedited_accepts_upper_limit(self):
        assert ReviewSettings(tech_lead_max_expedited=20).tech_lead_max_expedited == 20

    def test_tech_lead_max_expedited_rejects_negative_and_over_cap(self):
        with pytest.raises(ValidationError):
            ReviewSettings(tech_lead_max_expedited=-1)
        with pytest.raises(ValidationError):
            ReviewSettings(tech_lead_max_expedited=21)

    def test_tech_lead_max_expedited_classifies_to_integer(self):
        prop = get_settings_json_schema()["review"]["properties"]["tech_lead_max_expedited"]
        assert prop["x_control"]["kind"] == FORM_CONTROL_INTEGER

    def test_tech_lead_dedup_defaults_and_bounds(self):
        settings = ReviewSettings()
        assert settings.tech_lead_dedup_enabled is True
        assert settings.tech_lead_dedup_similarity_threshold == 0.72
        assert (
            ReviewSettings(
                tech_lead_dedup_similarity_threshold=1.0
            ).tech_lead_dedup_similarity_threshold
            == 1.0
        )
        with pytest.raises(ValidationError):
            ReviewSettings(tech_lead_dedup_similarity_threshold=0.0)
        with pytest.raises(ValidationError):
            ReviewSettings(tech_lead_dedup_similarity_threshold=1.01)

    def test_tech_lead_dedup_fields_have_owned_yaml_paths(self):
        properties = get_settings_json_schema()["review"]["properties"]
        assert (
            properties["tech_lead_dedup_enabled"]["yaml_path"]
            == "tech_lead.dedup.enabled"
        )
        assert (
            properties["tech_lead_dedup_similarity_threshold"]["yaml_path"]
            == "tech_lead.dedup.similarity_threshold"
        )

    def test_goal_pilot_defaults(self):
        m = GoalPilotSettings()
        assert m.enabled is False
        assert m.agent is None
        assert m.approval_policy == "journeys_only"

    def test_merge_queue_defaults(self):
        m = MergeQueueSettings()
        assert m.enabled is False
        assert m.provider == "github"
        assert m.enqueue_after == "code-reviewed"
        assert m.failure_action == "rework"

    def test_advanced_defaults(self):
        m = AdvancedSettings()
        assert m.session_interactions_enabled is False
        assert m.web_port == 0
        assert m.control_api_port == 0
        assert m.worktree_seed_ref is None
        assert m.worktree_branch_on_recreate == "delete"
        assert m.session_output_retention_days == 7
        assert m.session_output_retention_tier == "hot"


# ---------------------------------------------------------------------------
# Pydantic validation
# ---------------------------------------------------------------------------

class TestValidation:
    """Pydantic should reject out-of-range values."""

    def test_max_concurrent_sessions_min(self):
        with pytest.raises(ValidationError):
            ConcurrencySettings(max_concurrent_sessions=0)

    def test_max_concurrent_sessions_max(self):
        with pytest.raises(ValidationError):
            ConcurrencySettings(max_concurrent_sessions=21)

    def test_session_timeout_min(self):
        with pytest.raises(ValidationError):
            ConcurrencySettings(session_timeout_minutes=4)

    def test_session_timeout_max(self):
        with pytest.raises(ValidationError):
            ConcurrencySettings(session_timeout_minutes=181)

    def test_e2e_role_enum(self):
        with pytest.raises(ValidationError):
            E2ESettings(role="invalid")

    def test_e2e_auto_run_interval_max(self):
        with pytest.raises(ValidationError):
            E2ESettings(auto_run_interval_minutes=1441)

    def test_validation_dirty_check_enum(self):
        with pytest.raises(ValidationError):
            ValidationSettings(publish_dirty_check="sometimes")

    def test_filtering_fetch_limit_min(self):
        with pytest.raises(ValidationError):
            FilteringSettings(fetch_limit=0)

    def test_default_priority_tier_max(self):
        with pytest.raises(ValidationError):
            ConcurrencySettings(default_priority_tier=10)

    def test_merge_queue_failure_action_enum(self):
        with pytest.raises(ValidationError):
            MergeQueueSettings(failure_action="explode")

    def test_enabled_tech_lead_requires_agent(self):
        with pytest.raises(ValidationError, match="tech_lead.enabled is true"):
            ReviewSettings(tech_lead_enabled=True)

    def test_disabled_tech_lead_preserves_dormant_subordinate_settings(self):
        settings = ReviewSettings(
            tech_lead_enabled=False,
            tech_lead_health_review_interval_minutes=240,
            tech_lead_stuck_sweep_enabled=True,
        )

        assert settings.tech_lead_health_review_interval_minutes == 240
        assert settings.tech_lead_stuck_sweep_enabled is True

    def test_merge_queue_enqueue_after_enum(self):
        with pytest.raises(ValidationError):
            MergeQueueSettings(enqueue_after="whenever")

    def test_merge_queue_provider_rejects_unsupported(self):
        # Mirrors YAML-load validation: the settings POST path must not accept a
        # provider outside MERGE_QUEUE_PROVIDERS (e.g. "gitlab").
        with pytest.raises(ValidationError):
            MergeQueueSettings(provider="gitlab")

    def test_merge_queue_provider_accepts_supported(self):
        assert MergeQueueSettings(provider="github").provider == "github"

    def test_review_max_rework_max(self):
        with pytest.raises(ValidationError):
            ReviewSettings(max_rework_cycles=11)

    @pytest.mark.parametrize("rounds", [0, 51])
    def test_internal_review_max_rounds_bounds(self, rounds):
        with pytest.raises(ValidationError):
            ReviewSettings(internal_max_rounds=rounds)

    @pytest.mark.parametrize(
        "instructions",
        ["", "   ", "../outside.md", "/tmp/outside.md"],
    )
    def test_internal_review_instructions_must_stay_inside_repository(
        self,
        instructions,
    ):
        with pytest.raises(ValidationError):
            ReviewSettings(internal_instructions=instructions)

    def test_finding_promotion_mode_rejects_unsupported(self):
        """#6957 R3 F9: `enum` in json_schema_extra shapes the select, it does
        NOT validate. Without a validator a tampered POST body reached live
        config and was only rejected later by the doctor pass — after the write,
        and as a whole-config error the form cannot attach to a field."""
        with pytest.raises(ValidationError) as excinfo:
            ReviewSettings(tech_lead_findings_promote="sometimes")

        # Field-scoped, so the settings form can point at the offending input.
        assert excinfo.value.errors()[0]["loc"] == ("tech_lead_findings_promote",)

    @pytest.mark.parametrize("mode", VALID_FINDING_PROMOTION_MODES)
    def test_finding_promotion_mode_accepts_every_supported_value(self, mode):
        assert (
            ReviewSettings(tech_lead_findings_promote=mode).tech_lead_findings_promote
            == mode
        )

    def test_every_enum_declaring_field_actually_rejects_outsiders(self):
        """The CLASS behind F9, pinned so it cannot reopen on the next field.

        A field that advertises a closed set in ``json_schema_extra["enum"]``
        must also enforce it; Pydantic does not do that for a plain ``str``.
        This walks every tab model and proves each such field rejects a value
        outside its own declared set.
        """
        unenforced: list[str] = []
        for tab in TAB_DEFINITIONS:
            model = tab["model"]
            for name, field in model.model_fields.items():
                extra = field.json_schema_extra
                allowed = extra.get("enum") if isinstance(extra, dict) else None
                if not allowed:
                    continue
                outsider = "__not_a_supported_value__"
                assert outsider not in allowed
                try:
                    model.model_validate({name: outsider})
                except ValidationError:
                    continue
                unenforced.append(f"{model.__name__}.{name}")

        assert not unenforced, (
            "these settings fields advertise a closed enum but accept anything: "
            f"{unenforced}"
        )

    def test_health_review_interval_rejects_negative(self):
        """ge=0: the dashboard and YAML must reject -5, not treat it as disabled
        (the documented disable value is exactly 0, #6763 finding 8)."""
        with pytest.raises(ValidationError):
            ReviewSettings(tech_lead_health_review_interval_minutes=-5)

    def test_health_review_interval_accepts_zero_and_positive(self):
        """0 disables; a positive value is accepted when a tech lead agent is set
        (a positive value alone is a cross-field error — see below)."""
        assert (
            ReviewSettings(
                tech_lead_health_review_interval_minutes=0
            ).tech_lead_health_review_interval_minutes
            == 0
        )
        assert (
            ReviewSettings(
                tech_lead_enabled=True,
                tech_lead_health_review_interval_minutes=240,
                tech_lead_agent="agent:tech-lead",
            ).tech_lead_health_review_interval_minutes
            == 240
        )

    def test_positive_health_review_interval_without_agent_rejected(self):
        """Cross-field invariant (#6776): a positive interval with no tech_lead
        agent must be rejected at the settings/POST boundary, never silently
        disabled at runtime."""
        with pytest.raises(ValidationError, match="no tech lead agent is configured"):
            ReviewSettings(
                tech_lead_enabled=True,
                tech_lead_health_review_interval_minutes=60,
            )
        with pytest.raises(ValidationError, match="no tech lead agent is configured"):
            ReviewSettings(
                tech_lead_enabled=True,
                tech_lead_health_review_interval_minutes=60, tech_lead_agent=""
            )

    def test_zero_interval_without_agent_is_valid(self):
        """0 + no agent is the documented disabled state and must validate."""
        assert (
            ReviewSettings(
                tech_lead_health_review_interval_minutes=0
            ).tech_lead_health_review_interval_minutes
            == 0
        )

    def test_health_review_storm_settings_enforce_bounds(self):
        assert ReviewSettings().tech_lead_health_review_storm_threshold == 3
        assert ReviewSettings().tech_lead_health_review_storm_window_minutes == 5
        with pytest.raises(ValidationError):
            ReviewSettings(tech_lead_health_review_storm_threshold=-1)
        with pytest.raises(ValidationError):
            ReviewSettings(tech_lead_health_review_storm_window_minutes=0)

    def test_web_port_min(self):
        with pytest.raises(ValidationError):
            AdvancedSettings(web_port=-1)

    def test_web_port_max(self):
        with pytest.raises(ValidationError):
            AdvancedSettings(web_port=65536)

    def test_worktree_branch_on_recreate_enum(self):
        with pytest.raises(ValidationError):
            AdvancedSettings(worktree_branch_on_recreate="invalid")

    def test_worktree_base_rejects_empty_string(self):
        with pytest.raises(ValidationError):
            AdvancedSettings(worktree_base="")

    def test_valid_values_accepted(self):
        """Boundary values should be accepted."""
        m = ConcurrencySettings(max_concurrent_sessions=1, session_timeout_minutes=5)
        assert m.max_concurrent_sessions == 1
        assert m.session_timeout_minutes == 5

        m2 = AdvancedSettings(web_port=1024, control_api_port=0)
        assert m2.web_port == 1024
        assert m2.control_api_port == 0


# ---------------------------------------------------------------------------
# from_config round-trip
# ---------------------------------------------------------------------------

class TestFromConfig:
    """from_config should read all fields from a Config object."""

    def _make_config(self) -> Config:
        cfg = Config()
        cfg.max_concurrent_sessions = 5
        cfg.session_timeout_minutes = 60
        cfg.queue_refresh_seconds = 300
        cfg.fetch_layer_enabled = False
        cfg.fetch_layer_network_sync_seconds = 45
        cfg.fetch_layer_full_scan_interval_seconds = 1200
        cfg.fetch_layer_discovery_limit = 15
        cfg.fetch_layer_max_hot_issues_per_cycle = 30
        cfg.fetch_layer_pr_scan_every_n_refreshes = 3
        cfg.fetch_layer_dependency_scan_every_n_refreshes = 2
        cfg.fetch_layer_visibility_aware_enabled = True
        cfg.fetch_layer_selective_sync_planner_enabled = True
        cfg.e2e = E2EConfig(
            enabled=True,
            role="executor",
            auto_run_interval_minutes=15,
            pytest_args=["tests/e2e", "-v", "--timeout=30"],
            allow_retry_once=False,
            stop_on_first_failure=True,
            quarantine_file="quarantine.txt",
        )
        cfg.filtering = FilteringConfig(
            label="bot-ready",
            milestones=["M1", "M2"],
            exclude_labels=["test-data", "skip"],
            exclude_label_prefixes=["io:e2e:"],
            fetch_limit=50,
            max_to_start=10,
        )
        cfg.milestone_order = ["M1", "M2"]
        cfg.review_enabled = True
        cfg.code_review_agent = "agent:reviewer"
        cfg.max_rework_cycles = 3
        cfg.internal_review_enabled = True
        cfg.internal_review_max_rounds = 4
        cfg.internal_review_instructions = ".io/custom-internal-review.md"
        cfg.tech_lead_review_agent = "agent:tech-lead"
        cfg.tech_lead_review_threshold = 5
        cfg.tech_lead.max_concurrent = 2
        cfg.validation.quick.cmd = "make validate-quick"
        cfg.validation.quick.timeout_seconds = 90
        cfg.validation.publish.cmd = "make validate-pr"
        cfg.validation.publish.timeout_seconds = 1200
        cfg.validation.publish.dirty_check = "all"
        cfg.validation.junit_xml_paths = ("test-results.xml", "reports/*.xml")
        cfg.session_interactions.enabled = True
        cfg.session_no_output_seconds = 60
        cfg.stale_escalation_ticks = 3
        cfg.session_output_retention_days = 21
        cfg.session_output_retention_tier = "cold"
        cfg.web_port = 9090
        cfg.control_api_port = 19090
        cfg.worktree_base = Path("/tmp/worktrees")
        cfg.worktree_seed_ref = "HEAD"
        cfg.worktree_branch_on_recreate = "create_new_branch"
        return cfg

    def test_concurrency_tab(self):
        tabs = from_config(self._make_config())
        conc = tabs["concurrency"]
        assert isinstance(conc, ConcurrencySettings)
        assert conc.max_concurrent_sessions == 5
        assert conc.session_timeout_minutes == 60
        assert conc.queue_refresh_seconds == 300
        assert conc.fetch_layer_enabled is False
        assert conc.fetch_layer_network_sync_seconds == 45
        assert conc.fetch_layer_full_scan_interval_seconds == 1200
        assert conc.fetch_layer_discovery_limit == 15
        assert conc.fetch_layer_max_hot_issues_per_cycle == 30
        assert conc.fetch_layer_pr_scan_every_n_refreshes == 3
        assert conc.fetch_layer_dependency_scan_every_n_refreshes == 2
        assert conc.fetch_layer_visibility_aware_enabled is True
        assert conc.fetch_layer_selective_sync_planner_enabled is True

    def test_e2e_tab(self):
        tabs = from_config(self._make_config())
        e2e = tabs["e2e"]
        assert isinstance(e2e, E2ESettings)
        assert e2e.enabled is True
        assert e2e.role == "executor"
        assert e2e.auto_run_interval_minutes == 15
        # pytest_args list should be joined with spaces
        assert e2e.pytest_args == "tests/e2e -v --timeout=30"
        assert e2e.allow_retry_once is False
        assert e2e.stop_on_first_failure is True
        assert e2e.quarantine_file == "quarantine.txt"

    def test_validation_tab(self):
        tabs = from_config(self._make_config())
        validation = tabs["validation"]
        assert isinstance(validation, ValidationSettings)
        assert validation.quick_cmd == "make validate-quick"
        assert validation.quick_timeout_seconds == 90
        assert validation.publish_cmd == "make validate-pr"
        assert validation.publish_timeout_seconds == 1200
        assert validation.publish_dirty_check == "all"
        assert validation.junit_xml_paths == "test-results.xml\nreports/*.xml"

    def test_filtering_tab(self):
        tabs = from_config(self._make_config())
        filt = tabs["filtering"]
        assert isinstance(filt, FilteringSettings)
        assert filt.label == "bot-ready"
        assert filt.milestones == "M1, M2"
        assert filt.exclude_labels == "test-data, skip"
        assert filt.exclude_label_prefixes == "io:e2e:"
        assert filt.fetch_limit == 50
        assert filt.max_to_start == 10

    def test_milestones_tab(self):
        tabs = from_config(self._make_config())
        milestones = tabs["milestones"]
        assert isinstance(milestones, MilestonesSettings)
        assert milestones.order == "M1, M2"

    def test_review_tab(self):
        tabs = from_config(self._make_config())
        rev = tabs["review"]
        assert isinstance(rev, ReviewSettings)
        assert rev.enabled is True
        assert rev.tech_lead_enabled is True
        assert rev.default_reviewer == "agent:reviewer"
        assert rev.max_rework_cycles == 3
        assert rev.internal_enabled is True
        assert rev.internal_max_rounds == 4
        assert rev.internal_instructions == ".io/custom-internal-review.md"
        assert rev.tech_lead_agent == "agent:tech-lead"
        assert rev.tech_lead_threshold == 5
        assert rev.tech_lead_max_concurrent == 2

    def test_tech_lead_max_concurrent_round_trips_through_apply(self):
        # None (share worker budget) and a reserved int must both survive the
        # from_config -> apply_to round-trip against the live Config attribute.
        cfg = Config()
        assert cfg.tech_lead.max_concurrent is None
        cfg.tech_lead.max_concurrent = 2
        tabs = from_config(cfg)
        assert tabs["review"].tech_lead_max_concurrent == 2

        back = Config()
        apply_to(tabs, back)
        assert back.tech_lead.max_concurrent == 2

        # Unset round-trips back to None (not 0), preserving the shared-budget
        # semantics the planner keys off (``is None``).
        tabs["review"].tech_lead_max_concurrent = None
        apply_to(tabs, back)
        assert back.tech_lead.max_concurrent is None

    def test_tech_lead_master_switch_round_trips_and_requires_restart(self):
        cfg = Config()
        cfg.tech_lead_review_agent = "agent:tech-lead"
        tabs = from_config(cfg)
        assert tabs["review"].tech_lead_enabled is True

        tabs["review"] = tabs["review"].model_copy(update={"tech_lead_enabled": False})
        restart = apply_to(tabs, cfg)

        assert restart is True
        assert cfg.tech_lead.enabled is False
        assert cfg.tech_lead_enabled is False

    def test_advanced_tab(self):
        tabs = from_config(self._make_config())
        adv = tabs["advanced"]
        assert isinstance(adv, AdvancedSettings)
        assert adv.session_interactions_enabled is True
        assert adv.session_no_output_seconds == 60
        assert adv.stale_escalation_ticks == 3
        assert adv.session_output_retention_days == 21
        assert adv.session_output_retention_tier == "cold"
        assert adv.web_port == 9090
        assert adv.control_api_port == 19090
        assert adv.worktree_base == "/tmp/worktrees"
        assert adv.worktree_seed_ref == "HEAD"
        assert adv.worktree_branch_on_recreate == "create_new_branch"

    def test_default_config_round_trips(self):
        """Default Config() should produce valid schema models."""
        cfg = Config()
        tabs = from_config(cfg)
        assert len(tabs) == 10
        assert "validation" in tabs
        assert "merge_queue" in tabs
        for key, model in tabs.items():
            assert model is not None


# ---------------------------------------------------------------------------
# Form-control classification (the form projection guardrail)
# ---------------------------------------------------------------------------

class TestFormControlClassification:
    """Every registry field must project to a supported form control.

    The settings form renders and collects by dispatching on x_control
    produced by classify_form_control(). A field shape outside the closed
    kind set must fail HERE (and at page render), never silently degrade
    to a text input whose posted string strict POST validation rejects.
    Regression for the "nits_by_agent: Input should be a valid dictionary"
    save failure: the dict-typed field fell through the template's
    catch-all text-input branch.
    """

    def test_every_field_classifies_to_supported_kind(self):
        schemas = get_settings_json_schema()
        for tab_key, schema in schemas.items():
            for field_name, prop in schema["properties"].items():
                control = prop.get("x_control")
                assert control is not None, f"{tab_key}.{field_name} missing x_control"
                assert control["kind"] in FORM_CONTROL_KINDS, (
                    f"{tab_key}.{field_name} classified to unknown kind "
                    f"{control['kind']!r}"
                )

    def test_merge_queue_provider_is_enum_with_allowed_providers(self):
        # The provider must render as a select whose options match the single
        # source of truth (MERGE_QUEUE_PROVIDERS), so the form cannot offer or
        # save an unsupported provider. Guards against the enum drifting from
        # the allowed set.
        schemas = get_settings_json_schema()
        control = schemas["merge_queue"]["properties"]["provider"]["x_control"]
        assert control["kind"] == FORM_CONTROL_ENUM
        assert control["options"] == list(MERGE_QUEUE_PROVIDERS)

    def test_tech_lead_master_switch_is_an_accessible_native_boolean(self):
        prop = get_settings_json_schema()["review"]["properties"]["tech_lead_enabled"]

        assert prop["x_control"]["kind"] == FORM_CONTROL_BOOLEAN
        assert prop["title"] == "Enable Tech Lead"
        assert prop["yaml_path"] == "tech_lead.enabled"
        assert prop["restart_required"] is True

    def test_nits_by_agent_is_dict_enum_with_policy_options(self):
        schemas = get_settings_json_schema()
        control = schemas["review"]["properties"]["nits_by_agent"]["x_control"]
        assert control["kind"] == FORM_CONTROL_DICT_ENUM
        assert control["value_options"] == ["ignore", "surface", "address"]

    def test_object_without_enum_values_raises(self):
        prop = {"type": "object", "additionalProperties": {"type": "string"}}
        with pytest.raises(UnsupportedSettingsFieldError, match="review.free_form"):
            classify_form_control("review.free_form", prop)

    def test_unknown_shape_raises(self):
        with pytest.raises(UnsupportedSettingsFieldError, match="review.items"):
            classify_form_control("review.items", {"type": "array"})

    def test_optional_int_classifies_to_optional_integer(self):
        # Optional[int] (tech_lead.max_concurrent) projects to a nullable integer
        # input: empty means unset (None), a value is a bounded integer. The
        # registry grew this projection deliberately - the old fail-loud guard
        # for [integer, null] is now a supported control.
        prop = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
        control = classify_form_control("review.maybe_count", prop)
        assert control == {"kind": FORM_CONTROL_OPTIONAL_INTEGER}

    def test_optional_number_raises(self):
        # Optional[float] is still outside the closed set; a new optional
        # scalar shape must fail loudly until its projection is added, so it
        # never silently degrades to a text input.
        prop = {"anyOf": [{"type": "number"}, {"type": "null"}]}
        with pytest.raises(UnsupportedSettingsFieldError):
            classify_form_control("review.maybe_ratio", prop)

    def test_optional_enum_raises(self):
        # Optional[Literal[...]] emits anyOf with an enum entry; classifying
        # it as optional_string would silently drop the value constraint
        # into a free-text input - the exact degradation this PR forbids.
        prop = {
            "anyOf": [{"enum": ["a", "b"], "type": "string"}, {"type": "null"}],
        }
        with pytest.raises(UnsupportedSettingsFieldError, match="optional enum/const"):
            classify_form_control("review.maybe_mode", prop)

    def test_single_value_literal_const_raises(self):
        # Literal["only"] emits const (not enum); classifying it as a plain
        # string would silently drop the value constraint.
        prop = {"const": "only", "type": "string"}
        with pytest.raises(UnsupportedSettingsFieldError, match="const schema"):
            classify_form_control("review.fixed_mode", prop)

    def test_nits_by_agent_accepts_dict_rejects_string(self):
        # The exact bug shape: the old form posted the dict's Python repr
        # as a string. The schema must keep rejecting strings while the
        # form now posts a real object.
        model = ReviewSettings.model_validate(
            {"nits_by_agent": {"agent:frontend": "address"}}
        )
        assert model.nits_by_agent == {"agent:frontend": "address"}
        with pytest.raises(ValidationError, match="nits_by_agent"):
            ReviewSettings.model_validate({"nits_by_agent": "{}"})
        with pytest.raises(ValidationError, match="nits_by_agent"):
            ReviewSettings.model_validate({"nits_by_agent": {"agent:frontend": "bogus"}})

    def test_health_review_interval_projects_native_number_with_min_zero(self):
        """The schema-driven control is a native number input with min=0.

        ``ge=0`` becomes ``"minimum": 0`` in the field's JSON schema, which the
        settings form renders as the HTML ``min`` attribute on a native number
        input - so the dashboard cannot even offer -5 (#6763 finding 8). The
        native ``<input type="number">`` keeps its label and focus treatment,
        satisfying the accessibility requirement without custom ARIA.
        """
        schemas = get_settings_json_schema()
        prop = schemas["review"]["properties"][
            "tech_lead_health_review_interval_minutes"
        ]
        assert prop["minimum"] == 0
        control = classify_form_control(
            "review.tech_lead_health_review_interval_minutes", prop
        )
        assert control["kind"] == FORM_CONTROL_INTEGER

    @pytest.mark.parametrize(
        ("name", "minimum"),
        [
            ("tech_lead_health_review_storm_threshold", 0),
            ("tech_lead_health_review_storm_window_minutes", 1),
        ],
    )
    def test_health_review_storm_settings_project_accessible_number_controls(
        self, name: str, minimum: int
    ) -> None:
        prop = get_settings_json_schema()["review"]["properties"][name]
        assert prop["minimum"] == minimum
        control = classify_form_control(f"review.{name}", prop)
        assert control["kind"] == FORM_CONTROL_INTEGER

    def test_rejected_negative_interval_surfaces_field_scoped_save_error(self):
        """A rejected value must surface an accessible, field-scoped save error.

        The settings POST route projects each ValidationError to
        ``{"name": loc[-1], "detail": msg}`` (web_settings_routes), so the UI
        can attach the message to the specific field. The error therefore MUST
        carry the field name in its location.
        """
        with pytest.raises(ValidationError) as exc_info:
            ReviewSettings.model_validate(
                {"tech_lead_health_review_interval_minutes": -5}
            )
        errors = exc_info.value.errors()
        assert any(
            err["loc"][-1] == "tech_lead_health_review_interval_minutes"
            for err in errors
        ), errors


# ---------------------------------------------------------------------------
# apply_to round-trip
# ---------------------------------------------------------------------------

class TestApplyTo:
    """apply_to should write all fields back to a Config object."""

    def test_round_trip(self):
        """from_config -> apply_to should preserve values."""
        cfg = Config()
        cfg.max_concurrent_sessions = 7
        cfg.session_timeout_minutes = 90
        cfg.e2e = E2EConfig(enabled=True, pytest_args=["tests/e2e", "-v"])
        cfg.filtering = FilteringConfig(
            milestones=["M1"],
            exclude_labels=["skip"],
            exclude_label_prefixes=["io:e2e:"],
        )
        cfg.milestone_order = ["M2"]
        cfg.review_enabled = True
        cfg.code_review_agent = "agent:rev"
        cfg.session_interactions.enabled = True

        tabs = from_config(cfg)
        cfg2 = Config()
        apply_to(tabs, cfg2)

        assert cfg2.max_concurrent_sessions == 7
        assert cfg2.session_timeout_minutes == 90
        assert cfg2.e2e.enabled is True
        assert cfg2.e2e.pytest_args == ["tests/e2e", "-v"]
        assert cfg2.filtering.milestones == ["M1"]
        assert cfg2.filtering.exclude_labels == ["skip"]
        assert cfg2.filtering.exclude_label_prefixes == ["io:e2e:"]
        assert cfg2.milestone_order == ["M2"]
        assert cfg2.review_enabled is True
        assert cfg2.code_review_agent == "agent:rev"
        assert cfg2.session_interactions.enabled is True

    def test_round_trip_preserves_path_typed_values(self, tmp_path):
        """String settings forms should not erase Path-typed config values."""
        cfg = Config()
        cfg.repo_root = (tmp_path / "repo").resolve()
        cfg.worktree_base = (tmp_path / "worktrees").resolve()

        tabs = from_config(cfg)
        tabs["concurrency"] = tabs["concurrency"].model_copy(update={"max_concurrent_sessions": 2})
        apply_to(tabs, cfg)

        assert cfg.max_concurrent_sessions == 2
        assert cfg.worktree_base == (tmp_path / "worktrees").resolve()
        assert isinstance(cfg.worktree_base, Path)

    def test_path_typed_values_resolve_relative_to_repo_root(self, tmp_path):
        """Path-like settings should follow Config.load relative path semantics."""
        cfg = Config()
        cfg.repo_root = (tmp_path / "repo").resolve()
        cfg.worktree_base = (tmp_path / "worktrees").resolve()

        tabs = from_config(cfg)
        tabs["advanced"] = tabs["advanced"].model_copy(update={"worktree_base": "../new-worktrees"})
        apply_to(tabs, cfg)

        assert cfg.worktree_base == (tmp_path / "new-worktrees").resolve()
        assert isinstance(cfg.worktree_base, Path)

    def test_restart_not_required_for_effectively_unchanged_path(self, tmp_path):
        """Restart checks should compare coerced config values, not raw UI text."""
        cfg = Config()
        cfg.repo_root = (tmp_path / "repo").resolve()
        cfg.worktree_base = (tmp_path / "worktrees").resolve()

        tabs = from_config(cfg)
        tabs["advanced"] = tabs["advanced"].model_copy(update={"worktree_base": "../worktrees"})
        restart = apply_to(tabs, cfg)

        assert restart is False
        assert cfg.worktree_base == (tmp_path / "worktrees").resolve()
        assert isinstance(cfg.worktree_base, Path)

    def test_restart_required_detection(self):
        """apply_to should return True when restart-required fields change."""
        cfg = Config()
        cfg.web_port = 8080

        tabs = from_config(cfg)
        # Change the web port
        tabs["advanced"] = AdvancedSettings(
            web_port=9090,
            control_api_port=cfg.control_api_port,
            worktree_base=str(cfg.worktree_base),
            worktree_seed_ref=cfg.worktree_seed_ref,
            worktree_branch_on_recreate=cfg.worktree_branch_on_recreate,
        )
        restart = apply_to(tabs, cfg)
        assert restart is True
        assert cfg.web_port == 9090

    def test_concurrency_change_applies_without_repository_engine_restart(self):
        """Concurrency is read live by the Repository Engine's health gate."""
        cfg = Config()
        cfg.max_concurrent_sessions = 2

        tabs = from_config(cfg)
        tabs["concurrency"] = tabs["concurrency"].model_copy(
            update={"max_concurrent_sessions": 3}
        )

        restart = apply_to(tabs, cfg)

        assert restart is False
        assert cfg.max_concurrent_sessions == 3

    def test_deprecated_browser_session_max_is_not_restart_required(self):
        """The ignored compatibility field must not promise a restart effect."""
        cfg = Config()
        tabs = from_config(cfg)
        tabs["advanced"] = tabs["advanced"].model_copy(
            update={"browser_session_max": 2048}
        )

        restart = apply_to(tabs, cfg)

        field = AdvancedSettings.model_fields["browser_session_max"]
        assert isinstance(field.json_schema_extra, dict)
        assert field.json_schema_extra.get("restart_required") is not True
        assert restart is False
        assert cfg.browser_session_max == 2048

    def test_no_restart_when_unchanged(self):
        """apply_to should return False when no restart-required fields change."""
        cfg = Config()
        tabs = from_config(cfg)
        restart = apply_to(tabs, cfg)
        assert restart is False

    def test_comma_separated_list_transform(self):
        """Comma-separated string should be split into list."""
        cfg = Config()
        tabs = from_config(cfg)
        tabs["filtering"] = FilteringSettings(
            milestones="M1, M2, M3",
            exclude_labels="skip, test",
            exclude_label_prefixes="io:e2e:, tmp:",
        )
        tabs["milestones"] = MilestonesSettings(order="M2, M3")
        apply_to(tabs, cfg)
        assert cfg.filtering.milestones == ["M1", "M2", "M3"]
        assert cfg.filtering.exclude_labels == ["skip", "test"]
        assert cfg.filtering.exclude_label_prefixes == ["io:e2e:", "tmp:"]
        assert cfg.milestone_order == ["M2", "M3"]

    def test_space_separated_list_transform(self):
        """Space-separated string should be split into list."""
        cfg = Config()
        tabs = from_config(cfg)
        tabs["e2e"] = E2ESettings(pytest_args="tests/e2e -v --timeout=30")
        apply_to(tabs, cfg)
        assert cfg.e2e.pytest_args == ["tests/e2e", "-v", "--timeout=30"]

    def test_empty_list_transforms(self):
        """Empty strings should produce empty lists."""
        cfg = Config()
        tabs = from_config(cfg)
        tabs["filtering"] = FilteringSettings(milestones="", exclude_labels="", exclude_label_prefixes="")
        tabs["milestones"] = MilestonesSettings(order="")
        tabs["e2e"] = E2ESettings(pytest_args="")
        apply_to(tabs, cfg)
        assert cfg.filtering.milestones == []
        assert cfg.filtering.exclude_labels == []
        assert cfg.filtering.exclude_label_prefixes == []
        assert cfg.milestone_order == []
        assert cfg.e2e.pytest_args == []

    def test_optional_string_none(self):
        """None optional strings should be written as None."""
        cfg = Config()
        tabs = from_config(cfg)
        tabs["filtering"] = FilteringSettings(label=None)
        tabs["review"] = ReviewSettings(default_reviewer=None, tech_lead_agent=None)
        apply_to(tabs, cfg)
        assert cfg.filtering.label is None
        assert cfg.code_review_agent is None
        assert cfg.tech_lead_review_agent is None


# ---------------------------------------------------------------------------
# JSON Schema generation
# ---------------------------------------------------------------------------

class TestJsonSchema:
    """get_settings_json_schema should produce valid per-tab schemas."""

    def test_all_tabs_present(self):
        schemas = get_settings_json_schema()
        expected_keys = {tab["key"] for tab in TAB_DEFINITIONS}
        assert set(schemas.keys()) == expected_keys

    def test_schema_has_properties(self):
        schemas = get_settings_json_schema()
        for key, schema in schemas.items():
            assert "properties" in schema, f"Tab {key} schema missing properties"
            assert len(schema["properties"]) > 0, f"Tab {key} has no properties"

    def test_x_extra_populated(self):
        """Each property should have x_extra with at least config_attr."""
        schemas = get_settings_json_schema()
        for key, schema in schemas.items():
            for prop_name, prop in schema["properties"].items():
                assert "x_extra" in prop, f"{key}.{prop_name} missing x_extra"
                assert "config_attr" in prop["x_extra"], f"{key}.{prop_name} missing config_attr"

    def test_schema_titles(self):
        """Properties should have human-readable titles."""
        schemas = get_settings_json_schema()
        for key, schema in schemas.items():
            for prop_name, prop in schema["properties"].items():
                assert "title" in prop, f"{key}.{prop_name} missing title"

    def test_internal_review_fields_use_native_supported_controls(self):
        properties = get_settings_json_schema()["review"]["properties"]

        assert properties["internal_enabled"]["x_control"]["kind"] == "boolean"
        assert properties["internal_max_rounds"]["x_control"]["kind"] == "integer"
        assert properties["internal_instructions"]["x_control"]["kind"] == "string"

    def test_schema_is_cached(self):
        """Calling get_settings_json_schema twice should return the same object."""
        s1 = get_settings_json_schema()
        s2 = get_settings_json_schema()
        assert s1 is s2


# ---------------------------------------------------------------------------
# Metadata accessor
# ---------------------------------------------------------------------------

class TestGetFieldMeta:

    def test_known_field(self):
        meta = get_field_meta("concurrency", "max_concurrent_sessions")
        assert meta["title"] == "Max Concurrent Sessions"
        assert meta["default"] == 3
        assert meta["yaml_path"] == "execution.concurrency.max_concurrent_sessions"

    def test_worktree_base_declares_path_config_value_type(self):
        meta = get_field_meta("advanced", "worktree_base")
        assert meta["config_value_type"] == CONFIG_VALUE_TYPE_PATH

    def test_unknown_tab_raises(self):
        with pytest.raises(KeyError):
            get_field_meta("nonexistent", "max_concurrent_sessions")

    def test_unknown_field_raises(self):
        with pytest.raises(KeyError):
            get_field_meta("concurrency", "nonexistent_field")


# ---------------------------------------------------------------------------
# Restart fields
# ---------------------------------------------------------------------------

class TestRestartFields:

    def test_restart_fields_present(self):
        fields = get_restart_fields()
        assert "session_interactions_enabled" in fields
        assert "web_port" in fields
        assert "control_api_port" in fields
        assert "worktree_base" in fields
        assert "worktree_seed_ref" in fields
        assert "worktree_branch_on_recreate" in fields

    def test_non_restart_fields_absent(self):
        fields = get_restart_fields()
        assert "max_concurrent_sessions" not in fields
        assert "session_timeout_minutes" not in fields
        assert "enabled" not in fields


# ---------------------------------------------------------------------------
# Documentation generation
# ---------------------------------------------------------------------------

class TestDocGeneration:

    def test_generates_markdown(self):
        md = generate_config_reference()
        assert "# Settings Reference" in md
        assert "## Concurrency" in md
        assert "## E2E Runner" in md
        assert "## Filtering" in md
        assert "## Review" in md
        assert "## Advanced" in md

    def test_contains_yaml_paths(self):
        md = generate_config_reference()
        assert "execution.concurrency.max_concurrent_sessions" in md
        assert "e2e.enabled" in md
        assert "ui.web_port" in md

    def test_table_format(self):
        md = generate_config_reference()
        assert "| Field | Type | Default | Description | Examples | Notes |" in md
        assert "|-------|------|---------|-------------|----------|-------|" in md


# ---------------------------------------------------------------------------
# TAB_DEFINITIONS consistency
# ---------------------------------------------------------------------------

class TestTabDefinitions:

    def test_all_tabs_have_keys(self):
        for tab in TAB_DEFINITIONS:
            assert "key" in tab
            assert "label" in tab
            assert "model" in tab

    def test_all_models_are_basemodel(self):
        for tab in TAB_DEFINITIONS:
            assert issubclass(tab["model"], BaseModel)

    def test_all_fields_have_config_attr(self):
        """Every field in every model must have config_attr in json_schema_extra."""
        for tab in TAB_DEFINITIONS:
            model_cls = tab["model"]
            for field_name, field_info in model_cls.model_fields.items():
                extra = field_info.json_schema_extra
                assert isinstance(extra, dict), (
                    f"{tab['key']}.{field_name} missing json_schema_extra"
                )
                assert "config_attr" in extra, (
                    f"{tab['key']}.{field_name} missing config_attr"
                )

    def test_all_fields_have_yaml_path(self):
        """Every field must have yaml_path for documentation."""
        for tab in TAB_DEFINITIONS:
            model_cls = tab["model"]
            for field_name, field_info in model_cls.model_fields.items():
                extra = field_info.json_schema_extra
                assert isinstance(extra, dict)
                assert "yaml_path" in extra, (
                    f"{tab['key']}.{field_name} missing yaml_path"
                )

    def test_all_fields_have_doc_examples(self):
        """Every field must include doc_examples for richer docs."""
        for tab in TAB_DEFINITIONS:
            model_cls = tab["model"]
            for field_name, field_info in model_cls.model_fields.items():
                extra = field_info.json_schema_extra
                assert isinstance(extra, dict)
                assert "doc_examples" in extra, (
                    f"{tab['key']}.{field_name} missing doc_examples"
                )
                assert isinstance(extra["doc_examples"], list), (
                    f"{tab['key']}.{field_name} doc_examples must be a list"
                )
                assert extra["doc_examples"], (
                    f"{tab['key']}.{field_name} doc_examples must not be empty"
                )

    def test_all_fields_have_descriptions(self):
        """Every field should have a description for docs."""
        for tab in TAB_DEFINITIONS:
            model_cls = tab["model"]
            for field_name, field_info in model_cls.model_fields.items():
                assert field_info.description, (
                    f"{tab['key']}.{field_name} missing description"
                )

    def test_doctor_check_types_are_valid(self):
        """doctor_check values must be known check type constants."""
        valid_types = {
            DOCTOR_CHECK_PATH_EXISTS,
            DOCTOR_CHECK_PATH_IS_FILE,
            DOCTOR_CHECK_FIRST_ARG_PATH_EXISTS,
            DOCTOR_CHECK_REFERENCES_AGENT,
        }
        for tab in TAB_DEFINITIONS:
            model_cls = tab["model"]
            for field_name, field_info in model_cls.model_fields.items():
                extra = field_info.json_schema_extra or {}
                check = extra.get("doctor_check")
                if check is not None:
                    assert check in valid_types, (
                        f"{tab['key']}.{field_name} has unknown doctor_check: {check}"
                    )

    def test_doctor_check_fields_have_config_attr(self):
        """Fields with doctor_check must also have config_attr."""
        for tab in TAB_DEFINITIONS:
            model_cls = tab["model"]
            for field_name, field_info in model_cls.model_fields.items():
                extra = field_info.json_schema_extra or {}
                if extra.get("doctor_check"):
                    assert "config_attr" in extra, (
                        f"{tab['key']}.{field_name} has doctor_check but no config_attr"
                    )

    def test_doctor_check_severity_is_valid(self):
        """doctor_severity must be 'error' or 'warning'."""
        for tab in TAB_DEFINITIONS:
            model_cls = tab["model"]
            for field_name, field_info in model_cls.model_fields.items():
                extra = field_info.json_schema_extra or {}
                sev = extra.get("doctor_severity")
                if sev is not None:
                    assert sev in ("error", "warning"), (
                        f"{tab['key']}.{field_name} has invalid doctor_severity: {sev}"
                    )

    def test_setup_fields_have_required_keys(self):
        """Fields with setup.enabled must have section and order."""
        for tab in TAB_DEFINITIONS:
            model_cls = tab["model"]
            for field_name, field_info in model_cls.model_fields.items():
                extra = field_info.json_schema_extra or {}
                setup = extra.get("setup")
                if setup and setup.get("enabled"):
                    assert "section" in setup, (
                        f"{tab['key']}.{field_name} setup missing section"
                    )
                    assert "order" in setup, (
                        f"{tab['key']}.{field_name} setup missing order"
                    )

    def test_summary_fields_have_format(self):
        """Fields with summary annotation must have section and format."""
        for tab in TAB_DEFINITIONS:
            model_cls = tab["model"]
            for field_name, field_info in model_cls.model_fields.items():
                extra = field_info.json_schema_extra or {}
                summary = extra.get("summary")
                if summary:
                    assert "section" in summary, (
                        f"{tab['key']}.{field_name} summary missing section"
                    )
                    assert "format" in summary, (
                        f"{tab['key']}.{field_name} summary missing format"
                    )


# ---------------------------------------------------------------------------
# Doctor check field extraction
# ---------------------------------------------------------------------------

class TestDoctorCheckFields:

    def test_returns_annotated_fields(self):
        fields = get_doctor_check_fields()
        names = {f["name"] for f in fields}
        assert "quarantine_file" in names
        assert "default_reviewer" in names
        assert "tech_lead_agent" in names

    def test_field_has_required_keys(self):
        for field in get_doctor_check_fields():
            assert "name" in field
            assert "doctor_check" in field
            assert "config_attr" in field
            assert "doctor_severity" in field
            assert "title" in field

    def test_path_check_fields(self):
        path_fields = [f for f in get_doctor_check_fields()
                       if f["doctor_check"] == DOCTOR_CHECK_PATH_EXISTS]
        assert any(f["name"] == "quarantine_file" for f in path_fields)
        file_fields = [f for f in get_doctor_check_fields()
                       if f["doctor_check"] == DOCTOR_CHECK_PATH_IS_FILE]
        assert any(f["name"] == "internal_instructions" for f in file_fields)

    def test_agent_ref_fields(self):
        ref_fields = [f for f in get_doctor_check_fields()
                      if f["doctor_check"] == DOCTOR_CHECK_REFERENCES_AGENT]
        assert any(f["name"] == "default_reviewer" for f in ref_fields)
        assert any(f["name"] == "tech_lead_agent" for f in ref_fields)


# ---------------------------------------------------------------------------
# Setup field extraction
# ---------------------------------------------------------------------------

class TestSetupFields:

    def test_concurrency_section(self):
        fields = get_setup_fields("concurrency")
        assert len(fields) >= 1
        names = [f["name"] for f in fields]
        assert "max_concurrent_sessions" in names

    def test_ui_section(self):
        fields = get_setup_fields("ui")
        assert len(fields) >= 1
        names = [f["name"] for f in fields]
        assert "web_port" in names

    def test_worktrees_section(self):
        fields = get_setup_fields("worktrees")
        assert len(fields) >= 1
        names = [f["name"] for f in fields]
        assert "worktree_base" in names

    def test_fields_sorted_by_order(self):
        for section in ("concurrency", "ui", "worktrees"):
            fields = get_setup_fields(section)
            if len(fields) > 1:
                orders = [f["order"] for f in fields]
                assert orders == sorted(orders), f"{section} fields not sorted by order"

    def test_field_has_required_keys(self):
        all_fields = (
            get_setup_fields("concurrency") +
            get_setup_fields("ui") +
            get_setup_fields("worktrees")
        )
        for field in all_fields:
            assert "name" in field
            assert "title" in field
            assert "default" in field
            assert "prompt" in field
            assert "tab_key" in field

    def test_empty_section_returns_empty(self):
        assert get_setup_fields("nonexistent") == []


# ---------------------------------------------------------------------------
# Summary field extraction
# ---------------------------------------------------------------------------

class TestSummaryFields:

    def test_e2e_summary_fields(self):
        fields = get_summary_fields("e2e")
        names = {f["name"] for f in fields}
        assert "enabled" in names
        assert "auto_run_interval_minutes" in names
        assert "allow_retry_once" in names

    def test_review_summary_fields(self):
        fields = get_summary_fields("review")
        names = {f["name"] for f in fields}
        assert "enabled" in names
        assert "default_reviewer" in names

    def test_empty_section_returns_empty(self):
        assert get_summary_fields("nonexistent") == []


# ---------------------------------------------------------------------------
# Drift detection — CI guardrails
# ---------------------------------------------------------------------------

class TestDriftDetection:
    """Automated guardrails that fail CI if schema-driven code drifts.

    These tests ensure the schema remains the single source of truth.
    If someone hardcodes checks or defaults that should come from schema,
    these tests catch it.
    """

    def test_allowed_top_level_fields_is_derived(self):
        """ALLOWED_TOP_LEVEL_FIELDS must be derived from _TOP_LEVEL_SECTION_KEYS.

        If someone replaces the derivation with a hardcoded set, this fails.
        """
        from issue_orchestrator.infra.config import (
            ALLOWED_TOP_LEVEL_FIELDS,
            _TOP_LEVEL_SECTION_KEYS,
        )
        expected = frozenset(_TOP_LEVEL_SECTION_KEYS) | {"repo", "default_agent"}
        assert ALLOWED_TOP_LEVEL_FIELDS == expected, (
            "ALLOWED_TOP_LEVEL_FIELDS has drifted from _TOP_LEVEL_SECTION_KEYS. "
            "It must be derived, not hardcoded."
        )

    def test_doctor_e2e_no_hardcoded_path_checks(self):
        """doctor/checks/e2e.py must not emit Check() with 'not found' detail.

        Path validation is schema-driven via doctor_check annotations.
        e2e.py may call path.exists() for runtime inspection (line counting),
        but must not emit Check() for missing-path conditions — that's the schema's job.
        """
        import ast
        import inspect
        from issue_orchestrator.infra.doctor.checks import e2e

        source = inspect.getsource(e2e)
        tree = ast.parse(source)
        # Look for Check(..., detail="...not found...") in e2e.py
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "Check":
                    for kw in node.keywords:
                        if kw.arg == "detail" and isinstance(kw.value, ast.Constant):
                            if "not found" in str(kw.value.value):
                                raise AssertionError(
                                    "e2e.py emits Check() with 'not found' detail. "
                                    "Path checks are schema-driven — add doctor_check "
                                    "annotation in settings_schema.py instead."
                                )

    def test_doctor_review_no_hardcoded_agent_ref_checks(self):
        """doctor/checks/review.py must not hardcode basic agent-reference checks.

        Basic 'is this agent in config.agents?' validation is schema-driven.
        Per-agent cross-validation (iterating config.agents) stays as code.
        """
        import ast
        import inspect
        from issue_orchestrator.infra.doctor.checks import review

        source = inspect.getsource(review)
        tree = ast.parse(source)
        # Look for code patterns like: config.code_review_agent not in config.agents
        # This would mean someone hardcoded a basic agent-reference check.
        # We check by looking for `Compare` nodes with `NotIn` op on `config.agents`.
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                for op in node.ops:
                    if isinstance(op, ast.NotIn):
                        # Check if comparator is config.agents
                        for comp in node.comparators:
                            if (isinstance(comp, ast.Attribute)
                                    and comp.attr == "agents"
                                    and isinstance(comp.value, ast.Name)
                                    and comp.value.id == "config"):
                                # Check if the left side is config.code_review_agent
                                # or config.tech_lead_review_agent
                                if (isinstance(node.left, ast.Attribute)
                                        and node.left.attr in ("code_review_agent", "tech_lead_review_agent")):
                                    raise AssertionError(
                                        f"review.py hardcodes '{node.left.attr} not in config.agents'. "
                                        "Use doctor_check='references_agent' in settings_schema.py."
                                    )

    def test_wizard_uses_schema_for_setup_fields(self):
        """setup_wizard.py must use get_setup_fields() for schema-driven sections.

        It should not call get_field_meta() anymore (superseded by get_setup_fields).
        """
        import inspect
        from issue_orchestrator.entrypoints.cli_tools import setup_wizard

        source = inspect.getsource(setup_wizard)
        assert "get_field_meta" not in source, (
            "setup_wizard.py still uses get_field_meta(). "
            "Use get_setup_fields(section) for schema-driven wizard prompts."
        )

    def test_all_doctor_check_types_have_handlers(self):
        """Every doctor_check type used in schema must have a handler in schema.py."""
        from issue_orchestrator.infra.doctor.checks.schema import _CHECK_HANDLERS

        check_types_in_schema = {
            f["doctor_check"] for f in get_doctor_check_fields()
        }
        handler_types = set(_CHECK_HANDLERS.keys())
        missing = check_types_in_schema - handler_types
        assert not missing, (
            f"Schema uses doctor_check types without handlers: {missing}. "
            f"Add handlers to _CHECK_HANDLERS in doctor/checks/schema.py."
        )

    def test_config_reference_not_stale(self):
        """configuration_reference.md auto-generated section must match generate_config_reference().

        If this fails, someone edited the generated section directly instead of
        updating settings_schema.py. Regenerate by running:
            python -c "from issue_orchestrator.infra.settings_schema import generate_config_reference; print(generate_config_reference())"
        and paste the output between the AUTO-GENERATED markers in configuration_reference.md.
        """
        import re

        docs_path = Path(__file__).parent.parent.parent / "docs" / "user" / "configuration_reference.md"
        content = docs_path.read_text()

        begin = "<!-- BEGIN AUTO-GENERATED CONFIG REFERENCE"
        end = "<!-- END AUTO-GENERATED CONFIG REFERENCE -->"
        match = re.search(
            rf"{re.escape(begin)}.*?-->\n(.*?)\n{re.escape(end)}",
            content,
            re.DOTALL,
        )
        assert match, (
            f"configuration_reference.md missing AUTO-GENERATED markers. "
            f"Expected '{begin}' and '{end}' markers."
        )

        on_disk = match.group(1).strip()
        generated = generate_config_reference().strip()

        assert on_disk == generated, (
            "configuration_reference.md config reference has drifted from settings_schema.py.\n"
            "Do NOT edit the auto-generated section directly.\n"
            "Update settings_schema.py, then regenerate the reference and paste it "
            "between the AUTO-GENERATED markers in docs/user/configuration_reference.md."
        )
