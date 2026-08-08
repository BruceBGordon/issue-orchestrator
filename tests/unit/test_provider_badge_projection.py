"""Queue-row provider badge: producer side (issue #5980, item 2 / F3).

Both sides of the command surface are covered:
- producer: the label -> card projection here, driven through the real
  ``build_dashboard_view_model`` so every lane's cards carry the typed badge.
- payload -> rendered output: ``tests/js/provider_badge_row.test.js`` asserts
  the compact and expanded row forms render it.

Before this, an issue blocked by a provider outage was indistinguishable from
every other blocked issue: the raw ``blocked:provider-unavailable`` pill
rendered exactly like any other orchestrator label.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.domain.models import AgentConfig, Issue, OrchestratorState
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.provider_resilience import NO_PROVIDER_CIRCUIT_STATUS
from issue_orchestrator.view_models.dashboard import build_dashboard_view_model
from issue_orchestrator.view_models.dashboard_flow import (
    compact_card,
    compute_compact_card_fingerprint,
)
from issue_orchestrator.view_models.issue_card_labels import (
    provider_badge,
    provider_signal,
)

BLOCKED = 4101
HEALTHY = 4102


def _config(label_prefix: str = "") -> Config:
    config = Config(repo="test/repo")
    config.repo_root = Path("/tmp/repo")
    config.e2e.enabled = False
    config.agents["agent:web"] = AgentConfig(
        prompt_path=Path("/tmp/web.md"), provider="anthropic"
    )
    if label_prefix:
        config.label_prefix = label_prefix
    return config


class _OrchestratorStub:
    def __init__(self, config: Config, issues: list[Issue]) -> None:
        self.config = config
        self.state = OrchestratorState(
            startup_status="complete", cached_queue_issues=issues
        )
        self.shutdown_requested = False


def _cards(config: Config, issues: list[Issue]) -> dict[int, dict]:
    view_model = build_dashboard_view_model(
        _OrchestratorStub(config, issues),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="flow",
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )
    lanes = (
        view_model.queue_items
        + view_model.blocked_items
        + view_model.active_items
        + view_model.awaiting_merge_items
    )
    return {item["issue_number"]: item for item in lanes}


# --------------------------------------------------------------------------
# Label -> badge projection
# --------------------------------------------------------------------------

def test_provider_blocked_issue_projects_a_typed_badge():
    config = _config()
    lm = LabelManager(config)

    badge = provider_badge(["agent:web", lm.provider_unavailable], lm)

    assert badge is not None
    # Human-readable, prefix-aware text — not the raw label string.
    assert badge.label_text == "Provider unavailable"
    assert badge.label_text != lm.provider_unavailable
    assert badge.tone == "blocked"
    # Colour is never the only status signal; the title explains the state.
    assert "provider outage" in badge.title.lower()


def test_unaffected_issue_projects_no_badge():
    config = _config()
    lm = LabelManager(config)

    assert provider_badge(["agent:web", "blocked:pr-closed"], lm) is None
    assert provider_badge([], lm) is None
    assert provider_signal(None) == ""


def test_badge_detection_is_label_prefix_aware():
    """A repo that configures a label prefix still gets the badge."""
    prefixed = LabelManager(_config(label_prefix="orch"))
    plain = LabelManager(_config())

    assert prefixed.provider_unavailable != plain.provider_unavailable

    assert provider_badge([prefixed.provider_unavailable], prefixed) is not None
    # The unprefixed label is NOT this repo's provider label.
    assert provider_badge([plain.provider_unavailable], prefixed) is None


# --------------------------------------------------------------------------
# Every card in every lane carries the projection
# --------------------------------------------------------------------------

def test_dashboard_cards_carry_the_provider_badge_only_when_affected():
    config = _config()
    lm = LabelManager(config)
    cards = _cards(
        config,
        [
            Issue(
                number=BLOCKED,
                title="Stalled by outage",
                labels=["agent:web", lm.provider_unavailable],
            ),
            Issue(number=HEALTHY, title="Fine", labels=["agent:web"]),
        ],
    )

    blocked = cards[BLOCKED]
    assert blocked["provider_badge"] == {
        "tone": "blocked",
        "label_text": "Provider unavailable",
        "title": blocked["provider_badge"]["title"],
    }
    assert blocked["provider_signal"]

    healthy = cards[HEALTHY]
    assert healthy["provider_badge"] is None
    assert healthy["provider_signal"] == ""


def test_provider_badge_change_re_fingerprints_a_compact_card():
    """A card that gains or loses the badge must be rebuilt, not reused."""
    config = _config()
    lm = LabelManager(config)
    cards = _cards(
        config,
        [
            Issue(
                number=BLOCKED,
                title="Same title",
                labels=["agent:web", lm.provider_unavailable],
            ),
            Issue(number=HEALTHY, title="Same title", labels=["agent:web"]),
        ],
    )

    blocked = compact_card(dict(cards[BLOCKED], issue_number=1, card_id="issue-1"))
    healthy = compact_card(dict(cards[HEALTHY], issue_number=1, card_id="issue-1"))

    assert blocked["provider_signal"] != healthy["provider_signal"]
    assert compute_compact_card_fingerprint(blocked) != compute_compact_card_fingerprint(
        healthy
    )


def test_compact_card_fingerprint_matches_the_js_mirror_field_order():
    """The Python and JS fingerprints must agree on where provider_signal sits.

    They are compared as opaque strings across the wire, so a field inserted in
    one mirror and appended in the other would make every first-paint card look
    changed and flash on the first refresh.
    """
    js = Path(
        "src/issue_orchestrator/static/js/compact_card_state.js"
    ).read_text()
    assert "providerSignal" in js
    order = [js.index("labels,"), js.index("stackSignal,"), js.index("providerSignal,"), js.index("runDir,")]
    assert order == sorted(order)

    py = Path("src/issue_orchestrator/view_models/dashboard_flow.py").read_text()
    py_order = [
        py.index("labels_str,"),
        py.index('_s(card.get("stack_signal")),'),
        py.index('_s(card.get("provider_signal")),'),
        py.index('_s(card.get("run_dir")),'),
    ]
    assert py_order == sorted(py_order)


def test_expanded_row_fingerprint_includes_the_provider_signal():
    """The expanded list rebuilds when a row gains or loses the badge."""
    js = Path(
        "src/issue_orchestrator/static/js/expanded_column_state.js"
    ).read_text()
    assert "item.provider_signal" in js


def test_first_paint_template_renders_the_same_badge_as_the_client():
    """Server first paint and the JS rebuild must produce identical markup."""
    template = Path("src/issue_orchestrator/templates/dashboard.html").read_text()
    assert "card.provider_badge" in template
    assert 'class="provider-badge provider-badge--{{ card.provider_badge.tone }}"' in template
    assert "card.provider_badge.label_text" in template
    assert 'title="{{ card.provider_badge.title | e }}"' in template


def test_provider_badge_is_not_re_fingerprinted_by_the_cooldown_tick():
    """The badge carries no ETA, so a counting-down outage never flashes cards."""
    config = _config()
    lm = LabelManager(config)
    badge = provider_badge([lm.provider_unavailable], lm)
    assert badge is not None

    signal = provider_signal(badge)
    assert signal
    # No digits from a cooldown/ETA leak into the fingerprint input.
    assert not any(char.isdigit() for char in signal)


def test_badge_projection_does_not_depend_on_a_live_circuit_read():
    """The card badge is label-driven, so it stays correct with no reader wired.

    The live provider/cooldown detail belongs to the global banner and health
    panel; conflating them would couple every card to a per-second value.
    """
    config = _config()
    lm = LabelManager(config)
    cards = _cards(
        config,
        [
            Issue(
                number=BLOCKED,
                title="Stalled",
                labels=["agent:web", lm.provider_unavailable],
            )
        ],
    )
    # NO_PROVIDER_CIRCUIT_STATUS reports no open circuits, yet the affected
    # card still explains itself.
    assert cards[BLOCKED]["provider_badge"] is not None


def test_projection_survives_a_card_with_no_orchestrator_labels():
    config = _config()
    lm = LabelManager(config)
    assert provider_badge(MagicMock(spec=[]) and [], lm) is None
