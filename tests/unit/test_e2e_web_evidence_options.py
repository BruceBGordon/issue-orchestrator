"""The browser lane's artifact defaults must not override the operator.

``tests/e2e_web/conftest.py`` defaults pytest-playwright's ``--tracing`` and
``--screenshot`` to their failure-only modes so a browser flake leaves
evidence behind. Both options default to ``"off"``, so an operator who
explicitly passes ``--tracing=off`` lands on exactly the same
``config.option.tracing`` value as one who passed nothing — a value
comparison cannot tell them apart, and resolving that back into evidence
retention would run against an explicit instruction.

These tests pin all three states against a REAL pytest config parsed from
real argv by the real pytest-playwright parser, plus the supplied-ness
detection over every channel pytest reads options from.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

# `_pytest.config.get_config` is how pytest's own `pytester` builds a config
# for tests; it registers the installed plugins (pytest-playwright included)
# so `--tracing` is parsed by the same parser production runs use.
from _pytest.config import Config, get_config

from tests.e2e_web.conftest import (
    ARTIFACT_OPTIONS,
    apply_failure_only_artifact_defaults,
    captures_failure_evidence,
    effective_pytest_args,
    pytest_configure,
    registered_artifact_dests,
    supplied_option_dests,
)


@contextmanager
def parsed_config(*argv: str) -> Iterator[Config]:
    """A real pytest ``Config`` with ``argv`` parsed, nothing configured."""
    config = get_config(list(argv))
    config.parse(list(argv), addopts=False)
    yield config


def _resolved(*argv: str) -> tuple[str, str]:
    with parsed_config(*argv) as config:
        pytest_configure(config)
        return config.option.tracing, config.option.screenshot


def _captures(*argv: str) -> bool:
    with parsed_config(*argv) as config:
        pytest_configure(config)
        return captures_failure_evidence(config.option)


# ── The three states, end to end through the real parser ────────────────


def test_absent_options_get_the_lanes_failure_only_defaults() -> None:
    assert _resolved() == ("retain-on-failure", "only-on-failure")
    assert _captures()


def test_explicitly_off_is_respected_and_retains_nothing() -> None:
    """The regression: "off" is also the default, so being supplied has to be
    detected by origin, not by value."""
    assert _resolved("--tracing=off", "--screenshot=off") == ("off", "off")
    # Drives the console sidecar too: nothing is written, even on failure.
    assert not _captures("--tracing=off", "--screenshot=off")


def test_explicitly_enabled_is_respected() -> None:
    assert _resolved("--tracing=on", "--screenshot=on") == ("on", "on")
    assert _captures("--tracing=on", "--screenshot=on")


# ── Spelling, channel, and independence coverage ────────────────────────


def test_space_separated_spelling_counts_as_supplied() -> None:
    assert _resolved("--tracing", "off") == ("off", "only-on-failure")


def test_each_option_is_resolved_independently() -> None:
    """Disabling one artifact must not disable the other, or vice versa."""
    assert _resolved("--tracing=off") == ("off", "only-on-failure")
    assert _resolved("--screenshot=on") == ("retain-on-failure", "on")


def test_declared_flags_match_the_live_parser() -> None:
    """Each declared flag must still route to the dest we default."""
    for dest, spec in ARTIFACT_OPTIONS.items():
        enabling = sorted(spec.capturing)[0]
        with parsed_config(f"{spec.flag}={enabling}") as config:
            assert config.getoption(dest) == enabling
            assert dest in registered_artifact_dests(config)


def test_supplied_detection_reads_the_token_stream() -> None:
    assert supplied_option_dests(()) == frozenset()
    assert supplied_option_dests(("--tracing=off",)) == {"tracing"}
    assert supplied_option_dests(("--screenshot", "on")) == {"screenshot"}
    assert supplied_option_dests(
        ("-q", "tests/e2e_web", "--tracing=on", "--screenshot=off")
    ) == {"tracing", "screenshot"}


def test_effective_args_union_ini_env_and_argv(monkeypatch) -> None:
    """All three channels pytest reads options from feed detection."""
    monkeypatch.setenv("PYTEST_ADDOPTS", "--screenshot=on")

    class _StubConfig:
        invocation_params = argparse.Namespace(args=("tests/e2e_web", "-q"))

        def getini(self, name: str) -> Sequence[str]:
            assert name == "addopts"
            return ["--tracing=off"]

    args = effective_pytest_args(_StubConfig())  # type: ignore[arg-type]

    assert set(args) == {"--tracing=off", "--screenshot=on", "tests/e2e_web", "-q"}
    assert supplied_option_dests(args) == {"tracing", "screenshot"}


def test_unregistered_options_are_left_alone() -> None:
    """With pytest-playwright not loaded there is no option to default."""
    option = argparse.Namespace()

    apply_failure_only_artifact_defaults(
        option, supplied=frozenset(), registered=frozenset()
    )

    assert not vars(option)


def test_every_lane_default_is_itself_a_capturing_value() -> None:
    """Otherwise the sidecar gate would disagree with the artifacts
    pytest-playwright writes."""
    for spec in ARTIFACT_OPTIONS.values():
        assert spec.failure_only in spec.capturing
