"""Unit tests for the scheduler module."""

from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch
from issue_orchestrator.control.scheduler import (
    Scheduler, SchedulerResult, DueDateStrategy, MilestoneNumberStrategy,
    PatternStrategy, NameStrategy, get_milestone_strategy, load_strategy_class,
    BUILTIN_STRATEGIES
)
from issue_orchestrator.domain.models import Issue, AgentConfig
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.repository_host import DependencyIssueSnapshot
from issue_orchestrator.entrypoints.bootstrap_session_launcher import (
    build_session_launcher_factory,
)
from tests.callback_endpoint_helpers import ready_callback_endpoint


def create_mock_issue(number, priority=None, milestone=None, state="open", milestone_number=None, milestone_due_on=None, title=None):
    """Helper to create mock GitHub issue objects."""
    mock_issue = MagicMock()
    mock_issue.number = number
    # Title must be a string for regex matching
    mock_issue.title = title or f"Issue #{number}"
    # Milestone must be a string or None, not MagicMock
    mock_issue.milestone = milestone
    mock_issue.state = state
    mock_issue.milestone_number = milestone_number
    mock_issue.milestone_due_on = milestone_due_on

    # Labels are plain strings, not objects with .name
    if priority:
        mock_issue.labels = [f"priority:{priority}"]
    else:
        mock_issue.labels = []

    return mock_issue


class TestScheduler:
    """Test the Scheduler class."""

    def test_scheduler_creation(self, sample_config):
        """Test basic scheduler creation."""
        scheduler = Scheduler(config=sample_config)
        assert scheduler.config == sample_config

    def test_sort_by_priority_high_to_low(self, sample_config):
        """Test sorting issues by priority tier P0-P3."""
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(number=1, title="[M1-001][P2-001] Low priority", labels=[]),
            Issue(number=2, title="[M1-002][P0-001] Highest priority", labels=[]),
            Issue(number=3, title="[M1-003][P1-001] Medium priority", labels=[]),
            Issue(number=4, title="Old style no priority", labels=[]),
        ]

        sorted_issues = scheduler.sort_by_priority(issues)

        # Priority order: P0 (0), P1 (1), none (default P1, sequence=inf), P2 (2)
        assert sorted_issues[0].number == 2  # P0
        assert sorted_issues[1].number == 3  # P1
        assert sorted_issues[2].number == 4  # No priority (defaults to P1)
        assert sorted_issues[3].number == 1  # P2

    def test_sort_by_priority_same_priority_by_sequence(self, sample_config):
        """Test that issues with same priority are sorted by sequence then number."""
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(number=10, title="[M1-001][P0-020] Third", labels=[]),
            Issue(number=5, title="[M1-002][P0-005] First", labels=[]),
            Issue(number=15, title="[M1-003][P0-010] Second", labels=[]),
        ]

        sorted_issues = scheduler.sort_by_priority(issues)

        # Same priority P0, sorted by sequence: 005, 010, 020
        assert [i.number for i in sorted_issues] == [5, 15, 10]

    def test_sort_by_priority_with_milestones(self, sample_config):
        """Test sorting with milestone priority using pattern strategy."""
        sample_config.milestone_sort = "pattern"
        sample_config.milestone_sort_config = {"pattern": r"M(\d+)"}
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(number=1, title="[M6-001][P0-001] M6 issue", labels=[], milestone="M6"),
            Issue(number=2, title="[M7-001][P0-001] M7 issue", labels=[], milestone="M7"),
            Issue(number=3, title="[P0-001] No milestone", labels=[], milestone=None),
        ]

        sorted_issues = scheduler.sort_by_priority(issues)

        # M6 should come before M7, both before no milestone
        assert sorted_issues[0].number == 1  # M6
        assert sorted_issues[1].number == 2  # M7
        assert sorted_issues[2].number == 3  # No milestone

    def test_sort_by_priority_with_milestone_order(self, sample_config):
        """Explicit milestone order should override sort strategy for listed milestones."""
        sample_config.milestone_order = ["A", "B", "D"]
        sample_config.milestone_sort = "name"
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(number=1, title="Issue C", labels=[], milestone="C"),
            Issue(number=2, title="Issue A", labels=[], milestone="A"),
            Issue(number=3, title="Issue D", labels=[], milestone="D"),
            Issue(number=4, title="Issue B", labels=[], milestone="B"),
            Issue(number=5, title="Issue E", labels=[], milestone="E"),
        ]

        sorted_issues = scheduler.sort_by_priority(issues)

        assert [i.milestone for i in sorted_issues] == ["A", "B", "D", "C", "E"]

    def test_sort_by_priority_with_title_format(self, sample_config):
        """Test sorting with new [Mx-nnn][Px-nnn] title format."""
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(number=1, title="[M1-003][P1-020] Third", labels=[]),
            Issue(number=2, title="[M1-001][P0-001] First", labels=[]),
            Issue(number=3, title="[M1-002][P0-010] Second", labels=[]),
            Issue(number=4, title="[M1-004][P2-001] Fourth", labels=[]),
        ]

        sorted_issues = scheduler.sort_by_priority(issues)

        # Order: P0-001, P0-010, P1-020, P2-001
        assert sorted_issues[0].number == 2  # P0-001
        assert sorted_issues[1].number == 3  # P0-010
        assert sorted_issues[2].number == 1  # P1-020
        assert sorted_issues[3].number == 4  # P2-001

    def test_pick_next_batch_respects_max_sessions(self, sample_config):
        """Test that pick_next_batch respects max_sessions limit."""
        sample_config.max_concurrent_sessions = 2
        scheduler = Scheduler(config=sample_config)

        available = [create_mock_issue(i, priority="high") for i in range(1, 6)]

        # Current count is 0, so we can pick 2
        batch = scheduler.pick_next_batch(available, current_count=0)

        assert len(batch) == 2

    def test_pick_next_batch_with_current_sessions(self, sample_config):
        """Test picking batch when some sessions are already active."""
        sample_config.max_concurrent_sessions = 3
        scheduler = Scheduler(config=sample_config)

        available = [create_mock_issue(i, priority="high") for i in range(1, 6)]

        # Current count is 2, so we can pick 1 more
        batch = scheduler.pick_next_batch(available, current_count=2)

        assert len(batch) == 1

    def test_pick_next_batch_no_slots_available(self, sample_config):
        """Test picking batch when no slots are available."""
        sample_config.max_concurrent_sessions = 2
        scheduler = Scheduler(config=sample_config)

        available = [create_mock_issue(i, priority="high") for i in range(1, 4)]

        # Current count equals max sessions
        batch = scheduler.pick_next_batch(available, current_count=2)

        assert len(batch) == 0

    def test_pick_next_batch_with_priority_overrides(self, sample_config):
        """Test that priority overrides are picked first."""
        sample_config.max_concurrent_sessions = 3
        scheduler = Scheduler(config=sample_config)

        available = [
            create_mock_issue(1, priority="low"),
            create_mock_issue(2, priority="low"),
            create_mock_issue(3, priority="high"),
        ]

        # Override to prioritize issue 1
        batch = scheduler.pick_next_batch(
            available, current_count=0, priority_overrides=[1]
        )

        # Issue 1 should be first in the batch
        assert batch[0].number == 1

    def test_pick_next_batch_override_respects_max_sessions(self, sample_config):
        """Test that overrides don't exceed max sessions."""
        sample_config.max_concurrent_sessions = 2
        scheduler = Scheduler(config=sample_config)

        available = [create_mock_issue(i, priority="low") for i in range(1, 6)]

        batch = scheduler.pick_next_batch(
            available, current_count=0, priority_overrides=[3, 4, 5]
        )

        # Should only pick 2 even though we have 3 overrides
        assert len(batch) == 2
        # Overrides should be picked first
        assert batch[0].number == 3
        assert batch[1].number == 4

    def test_pick_next_batch_override_missing_issue(self, sample_config):
        """Test that missing override issues are skipped gracefully."""
        sample_config.max_concurrent_sessions = 3
        scheduler = Scheduler(config=sample_config)

        available = [create_mock_issue(i) for i in [1, 2]]

        # Override includes issue 99 which doesn't exist
        batch = scheduler.pick_next_batch(available, current_count=0, priority_overrides=[99, 1])

        assert len(batch) == 2
        assert batch[0].number == 1
        assert batch[1].number == 2

    def test_analyze_dependencies_single_blocked_by(self, sample_config):
        """Test analyzing single 'blocked by' dependency."""
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(
                number=1,
                title="Base task",
                labels=[],
                body="Do this first",
            ),
            Issue(
                number=2,
                title="Dependent task",
                labels=[],
                body="blocked by #1",
            ),
        ]

        deps = scheduler.analyze_dependencies(issues)

        assert deps[2] == [1]
        assert 1 not in deps

    def test_analyze_dependencies_multiple_patterns(self, sample_config):
        """Test analyzing various dependency patterns."""
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(
                number=1,
                title="First",
                labels=[],
                body="",
            ),
            Issue(
                number=2,
                title="Second",
                labels=[],
                body="blocked by #1",
            ),
            Issue(
                number=3,
                title="Third",
                labels=[],
                body="depends on #1",
            ),
            Issue(
                number=4,
                title="Fourth",
                labels=[],
                body="after #1",
            ),
            Issue(
                number=5,
                title="Fifth",
                labels=[],
                body="waiting for #1",
            ),
            Issue(
                number=6,
                title="Sixth",
                labels=[],
                body="requires #1",
            ),
        ]

        deps = scheduler.analyze_dependencies(issues)

        # All issues 2-6 should depend on 1
        for issue_num in [2, 3, 4, 5, 6]:
            assert deps[issue_num] == [1]

    def test_analyze_dependencies_multiple_blockers(self, sample_config):
        """Test analyzing issue with multiple blockers using different patterns."""
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(number=1, title="First", labels=[], body=""),
            Issue(number=2, title="Second", labels=[], body=""),
            Issue(
                number=3,
                title="Third",
                labels=[],
                body="blocked by #1\ndepends on #2",
            ),
        ]

        deps = scheduler.analyze_dependencies(issues)

        assert set(deps[3]) == {1, 2}

    def test_analyze_dependencies_case_insensitive(self, sample_config):
        """Test that dependency analysis is case-insensitive."""
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(number=1, title="First", labels=[], body=""),
            Issue(
                number=2,
                title="Second",
                labels=[],
                body="BLOCKED BY #1 and Blocked By #1",
            ),
        ]

        deps = scheduler.analyze_dependencies(issues)

        # Should find both despite case difference
        assert deps[2] == [1, 1] or deps[2] == [1]

    def test_analyze_dependencies_no_mentions(self, sample_config):
        """Test analyzing issue with no dependency mentions."""
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(number=1, title="Task", labels=[], body="Just a regular task"),
        ]

        deps = scheduler.analyze_dependencies(issues)

        assert 1 not in deps

    def test_analyze_dependencies_no_body(self, sample_config):
        """Test analyzing issue with no body text."""
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(number=1, title="Task", labels=[], body=None),
        ]

        deps = scheduler.analyze_dependencies(issues)

        assert 1 not in deps

    def test_analyze_dependencies_sorted_output(self, sample_config):
        """Test that dependencies are sorted in output."""
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(
                number=1,
                title="Complex",
                labels=[],
                body="blocked by #5\ndepends on #2\nwaiting for #10",
            ),
        ]

        deps = scheduler.analyze_dependencies(issues)

        assert deps[1] == [2, 5, 10]


class TestSchedulerResult:
    """Test the SchedulerResult data class."""

    def test_scheduler_result_creation(self, sample_issues):
        """Test basic SchedulerResult creation."""
        result = SchedulerResult(
            issues_to_launch=[sample_issues[0]],
            blocked_issues=[(sample_issues[1], "Blocked by #1")],
        )

        assert len(result.issues_to_launch) == 1
        assert result.issues_to_launch[0].number == 1
        assert len(result.blocked_issues) == 1
        assert result.blocked_issues[0][0].number == 2
        assert result.blocked_issues[0][1] == "Blocked by #1"

    def test_scheduler_result_empty(self):
        """Test SchedulerResult with empty lists."""
        result = SchedulerResult(
            issues_to_launch=[],
            blocked_issues=[],
        )

        assert result.issues_to_launch == []
        assert result.blocked_issues == []


class TestMilestoneSortStrategies:
    """Test milestone sorting strategies."""

    def test_due_date_strategy_with_dates(self):
        """Test DueDateStrategy sorts by due date."""
        strategy = DueDateStrategy()

        issue1 = Issue(
            number=1, title="First", labels=[],
            milestone="M1", milestone_due_on="2025-01-15T00:00:00Z"
        )
        issue2 = Issue(
            number=2, title="Second", labels=[],
            milestone="M2", milestone_due_on="2025-01-10T00:00:00Z"
        )
        issue3 = Issue(
            number=3, title="Third", labels=[],
            milestone="M3", milestone_due_on="2025-01-20T00:00:00Z"
        )

        key1 = strategy.get_sort_key(issue1)
        key2 = strategy.get_sort_key(issue2)
        key3 = strategy.get_sort_key(issue3)

        # Earlier dates should sort first
        assert key2 < key1 < key3

    def test_due_date_strategy_nulls_last(self):
        """Test DueDateStrategy places nulls at the end."""
        strategy = DueDateStrategy()

        issue_with_date = Issue(
            number=1, title="Has date", labels=[],
            milestone="M1", milestone_due_on="2025-01-15T00:00:00Z"
        )
        issue_no_date = Issue(
            number=2, title="No date", labels=[],
            milestone="M2", milestone_due_on=None
        )

        key_with_date = strategy.get_sort_key(issue_with_date)
        key_no_date = strategy.get_sort_key(issue_no_date)

        assert key_with_date < key_no_date
        assert key_no_date == (float("inf"),)

    def test_due_date_strategy_invalid_date(self):
        """Test DueDateStrategy handles invalid dates gracefully."""
        strategy = DueDateStrategy()

        issue = Issue(
            number=1, title="Bad date", labels=[],
            milestone="M1", milestone_due_on="invalid-date"
        )

        key = strategy.get_sort_key(issue)
        assert key == (float("inf"),)

    def test_milestone_number_strategy_extracts_numbers(self):
        """Test MilestoneNumberStrategy extracts first number from name."""
        strategy = MilestoneNumberStrategy()

        issue1 = Issue(number=1, title="First", labels=[], milestone="M1")
        issue2 = Issue(number=2, title="Second", labels=[], milestone="M10")
        issue3 = Issue(number=3, title="Third", labels=[], milestone="M2")

        key1 = strategy.get_sort_key(issue1)
        key2 = strategy.get_sort_key(issue2)
        key3 = strategy.get_sort_key(issue3)

        # Numeric sort: M1 < M2 < M10
        assert key1 == (1,)
        assert key2 == (10,)
        assert key3 == (2,)
        assert key1 < key3 < key2

    def test_milestone_number_strategy_various_formats(self):
        """Test MilestoneNumberStrategy handles various milestone formats."""
        strategy = MilestoneNumberStrategy()

        issue_m = Issue(number=1, title="", labels=[], milestone="M5")
        issue_sprint = Issue(number=2, title="", labels=[], milestone="Sprint 3")
        issue_v = Issue(number=3, title="", labels=[], milestone="v2.0")
        issue_no_num = Issue(number=4, title="", labels=[], milestone="Alpha")
        issue_none = Issue(number=5, title="", labels=[], milestone=None)

        assert strategy.get_sort_key(issue_m) == (5,)
        assert strategy.get_sort_key(issue_sprint) == (3,)
        assert strategy.get_sort_key(issue_v) == (2,)
        assert strategy.get_sort_key(issue_no_num) == (float("inf"),)
        assert strategy.get_sort_key(issue_none) == (float("inf"),)

    def test_pattern_strategy_default_pattern(self):
        """Test PatternStrategy with default M(\\d+) pattern."""
        strategy = PatternStrategy(r"M(\d+)")

        issue1 = Issue(number=1, title="First", labels=[], milestone="M6")
        issue2 = Issue(number=2, title="Second", labels=[], milestone="M10")
        issue3 = Issue(number=3, title="Third", labels=[], milestone="M7")

        key1 = strategy.get_sort_key(issue1)
        key2 = strategy.get_sort_key(issue2)
        key3 = strategy.get_sort_key(issue3)

        # Extract numbers and sort
        assert key1 == (6,)
        assert key2 == (10,)
        assert key3 == (7,)
        assert key1 < key3 < key2

    def test_pattern_strategy_custom_pattern(self):
        """Test PatternStrategy with custom pattern."""
        strategy = PatternStrategy(r"Sprint (\d+)")

        issue1 = Issue(number=1, title="First", labels=[], milestone="Sprint 15")
        issue2 = Issue(number=2, title="Second", labels=[], milestone="Sprint 12")
        issue3 = Issue(number=3, title="Third", labels=[], milestone="Sprint 20")

        key1 = strategy.get_sort_key(issue1)
        key2 = strategy.get_sort_key(issue2)
        key3 = strategy.get_sort_key(issue3)

        assert key1 == (15,)
        assert key2 == (12,)
        assert key3 == (20,)
        assert key2 < key1 < key3

    def test_pattern_strategy_no_match(self):
        """Test PatternStrategy when pattern doesn't match."""
        strategy = PatternStrategy(r"M(\d+)")

        issue_no_match = Issue(
            number=1, title="No match", labels=[],
            milestone="Random Milestone"
        )
        issue_no_milestone = Issue(
            number=2, title="No milestone", labels=[],
            milestone=None
        )

        key_no_match = strategy.get_sort_key(issue_no_match)
        key_no_milestone = strategy.get_sort_key(issue_no_milestone)

        # Both should be infinity
        assert key_no_match == (float("inf"),)
        assert key_no_milestone == (float("inf"),)

    def test_name_strategy_alphabetical(self):
        """Test NameStrategy sorts alphabetically."""
        strategy = NameStrategy()

        issue1 = Issue(number=1, title="First", labels=[], milestone="Beta")
        issue2 = Issue(number=2, title="Second", labels=[], milestone="Alpha")
        issue3 = Issue(number=3, title="Third", labels=[], milestone="Gamma")

        key1 = strategy.get_sort_key(issue1)
        key2 = strategy.get_sort_key(issue2)
        key3 = strategy.get_sort_key(issue3)

        assert key1 == ("Beta",)
        assert key2 == ("Alpha",)
        assert key3 == ("Gamma",)
        assert key2 < key1 < key3

    def test_name_strategy_nulls_last(self):
        """Test NameStrategy places nulls at the end."""
        strategy = NameStrategy()

        issue_with_name = Issue(
            number=1, title="Has name", labels=[],
            milestone="Milestone A"
        )
        issue_no_name = Issue(
            number=2, title="No name", labels=[],
            milestone=None
        )

        key_with_name = strategy.get_sort_key(issue_with_name)
        key_no_name = strategy.get_sort_key(issue_no_name)

        assert key_with_name < key_no_name
        assert key_no_name == ("\uffff",)

    def test_get_milestone_strategy_due_date(self):
        """Test factory creates DueDateStrategy."""
        config = Config()
        config.milestone_sort = "due_date"

        strategy = get_milestone_strategy(config)
        assert isinstance(strategy, DueDateStrategy)

    def test_get_milestone_strategy_milestone_number(self):
        """Test factory creates MilestoneNumberStrategy."""
        config = Config()
        config.milestone_sort = "milestone_number"

        strategy = get_milestone_strategy(config)
        assert isinstance(strategy, MilestoneNumberStrategy)

    def test_get_milestone_strategy_pattern(self):
        """Test factory creates PatternStrategy."""
        config = Config()
        config.milestone_sort = "pattern"
        config.milestone_sort_config = {"pattern": r"Sprint (\d+)"}

        strategy = get_milestone_strategy(config)
        assert isinstance(strategy, PatternStrategy)
        assert strategy.pattern.pattern == r"Sprint (\d+)"

    def test_get_milestone_strategy_name(self):
        """Test factory creates NameStrategy."""
        config = Config()
        config.milestone_sort = "name"

        strategy = get_milestone_strategy(config)
        assert isinstance(strategy, NameStrategy)

    def test_get_milestone_strategy_invalid(self):
        """Test factory raises error for invalid strategy."""
        config = Config()
        config.milestone_sort = "invalid_strategy"

        with pytest.raises(ValueError, match="Cannot load strategy class"):
            get_milestone_strategy(config)

    def test_get_milestone_strategy_case_insensitive(self):
        """Test factory is case-insensitive."""
        config = Config()
        config.milestone_sort = "DUE_DATE"

        strategy = get_milestone_strategy(config)
        assert isinstance(strategy, DueDateStrategy)

    def test_load_strategy_class_builtin(self):
        """Test load_strategy_class can load built-in strategies."""
        cls = load_strategy_class("issue_orchestrator.control.scheduler.DueDateStrategy")
        assert cls is DueDateStrategy

    def test_load_strategy_class_invalid_module(self):
        """Test load_strategy_class raises for invalid module."""
        with pytest.raises(ValueError, match="Cannot load strategy class"):
            load_strategy_class("nonexistent.module.Strategy")

    def test_load_strategy_class_invalid_class(self):
        """Test load_strategy_class raises for invalid class name."""
        with pytest.raises(ValueError, match="Cannot load strategy class"):
            load_strategy_class("issue_orchestrator.control.scheduler.NonexistentStrategy")

    def test_get_milestone_strategy_full_module_path(self):
        """Test factory accepts full module path (same mechanism as user plugins)."""
        config = Config()
        # Use full module path instead of alias - proves dynamic import works
        config.milestone_sort = "issue_orchestrator.control.scheduler.MilestoneNumberStrategy"

        strategy = get_milestone_strategy(config)
        assert isinstance(strategy, MilestoneNumberStrategy)

    def test_builtin_strategies_registry(self):
        """Test all builtin aliases map to valid module paths."""
        for alias, module_path in BUILTIN_STRATEGIES.items():
            cls = load_strategy_class(module_path)
            assert cls is not None, f"Failed to load {alias} -> {module_path}"

    def test_strict_kwargs_rejects_unknown_args(self):
        """Test that unknown config arguments raise TypeError (strict mode)."""
        config = Config()
        config.milestone_sort = "due_date"
        config.milestone_sort_config = {"unknown_param": "value"}

        # DueDateStrategy doesn't accept any kwargs, so this should fail
        # Error message varies by Python version: "takes no arguments" or "unexpected keyword argument"
        with pytest.raises(TypeError):
            get_milestone_strategy(config)

    def test_strategy_kwargs_uniform_with_plugins(self):
        """Test that built-in and plugin strategies use the same kwargs mechanism."""
        # This test proves a user-defined plugin would work the same way
        config = Config()

        # Using full module path (as a plugin would be specified)
        config.milestone_sort = "issue_orchestrator.control.scheduler.PatternStrategy"
        config.milestone_sort_config = {"pattern": r"v(\d+)"}

        strategy = get_milestone_strategy(config)
        assert isinstance(strategy, PatternStrategy)
        assert strategy.pattern.pattern == r"v(\d+)"


class TestSchedulerWithStrategies:
    """Test Scheduler integration with milestone strategies."""

    def test_scheduler_default_strategy_is_milestone_number(self):
        """A fresh Config should resolve to MilestoneNumberStrategy."""
        scheduler = Scheduler(config=Config())

        assert isinstance(scheduler.milestone_strategy, MilestoneNumberStrategy)

    def test_scheduler_honors_explicit_due_date_strategy(self, sample_config):
        """Setting milestone_sort='due_date' opts into DueDateStrategy."""
        sample_config.milestone_sort = "due_date"
        scheduler = Scheduler(config=sample_config)

        assert isinstance(scheduler.milestone_strategy, DueDateStrategy)

    def test_scheduler_with_custom_strategy(self, sample_config):
        """Test Scheduler can accept custom strategy."""
        custom_strategy = MilestoneNumberStrategy()
        scheduler = Scheduler(config=sample_config, milestone_strategy=custom_strategy)

        assert scheduler.milestone_strategy is custom_strategy

    def test_sort_by_priority_with_due_date_strategy(self, sample_config):
        """Test sorting with DueDateStrategy."""
        sample_config.milestone_sort = "due_date"
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(
                number=1, title="Later", labels=["priority:high"],
                milestone="M2", milestone_due_on="2025-01-20T00:00:00Z"
            ),
            Issue(
                number=2, title="Earlier", labels=["priority:high"],
                milestone="M1", milestone_due_on="2025-01-10T00:00:00Z"
            ),
            Issue(
                number=3, title="No date", labels=["priority:high"],
                milestone="M3", milestone_due_on=None
            ),
        ]

        sorted_issues = scheduler.sort_by_priority(issues)

        # Should sort by due date: earlier first, no date last
        assert sorted_issues[0].number == 2
        assert sorted_issues[1].number == 1
        assert sorted_issues[2].number == 3

    def test_sort_by_priority_with_milestone_number_strategy(self, sample_config):
        """Test sorting with MilestoneNumberStrategy."""
        sample_config.milestone_sort = "milestone_number"
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(
                number=1, title="M7", labels=["priority:high"],
                milestone="M7"
            ),
            Issue(
                number=2, title="M5", labels=["priority:high"],
                milestone="M5"
            ),
            Issue(
                number=3, title="M10", labels=["priority:high"],
                milestone="M10"
            ),
        ]

        sorted_issues = scheduler.sort_by_priority(issues)

        # Should sort by extracted milestone number: M5 < M7 < M10
        assert sorted_issues[0].number == 2  # M5
        assert sorted_issues[1].number == 1  # M7
        assert sorted_issues[2].number == 3  # M10

    def test_sort_by_priority_with_pattern_strategy(self, sample_config):
        """Test sorting with PatternStrategy."""
        sample_config.milestone_sort = "pattern"
        sample_config.milestone_sort_config = {"pattern": r"M(\d+)"}
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(number=1, title="M7", labels=["priority:high"], milestone="M7"),
            Issue(number=2, title="M5", labels=["priority:high"], milestone="M5"),
            Issue(number=3, title="No match", labels=["priority:high"], milestone="Random"),
        ]

        sorted_issues = scheduler.sort_by_priority(issues)

        # Should sort by extracted number, no match goes last
        assert sorted_issues[0].number == 2  # M5
        assert sorted_issues[1].number == 1  # M7
        assert sorted_issues[2].number == 3  # Random

    def test_sort_by_priority_milestone_then_priority(self, sample_config):
        """Test sorting respects milestone first, then priority."""
        sample_config.milestone_sort = "milestone_number"
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(
                number=1, title="M7 low", labels=["priority:low"],
                milestone="M7"
            ),
            Issue(
                number=2, title="M5 high", labels=["priority:high"],
                milestone="M5"
            ),
            Issue(
                number=3, title="M5 low", labels=["priority:low"],
                milestone="M5"
            ),
        ]

        sorted_issues = scheduler.sort_by_priority(issues)

        # M5 issues come first, then within M5 priority matters
        assert sorted_issues[0].number == 2  # M5 high
        assert sorted_issues[1].number == 3  # M5 low
        assert sorted_issues[2].number == 1  # M7 low


class MockIssueChecker:
    """Mock issue checker for testing dependency gating."""

    def __init__(self, default_milestone: str | None = "M1"):
        self.issues: dict[int, str] = {}  # issue_number -> state
        self._default_milestone = default_milestone

    def get_dependency_issue_snapshot(
        self,
        issue_number: int,
        repo: str | None = None,
    ) -> DependencyIssueSnapshot | None:
        state = self.issues.get(issue_number)
        if state is None:
            return None
        return DependencyIssueSnapshot(state=state, milestone=self._default_milestone)

    def get_issue_state(self, issue_number: int, repo: str | None = None) -> str | None:
        return self.issues.get(issue_number)

    def get_issue_milestone(self, issue_number: int, repo: str | None = None) -> str | None:
        return self._default_milestone


class CollectingEventSink:
    """Event sink that collects events for testing."""

    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


class TestSchedulerDependencyGating:
    """Tests for scheduler's dependency gating integration.

    Key invariant: Issues with unsatisfied dependencies are not available.
    """

    @pytest.fixture
    def checker(self):
        return MockIssueChecker()

    @pytest.fixture
    def events(self):
        return CollectingEventSink()

    @pytest.fixture
    def sample_config(self):
        return Config(
            repo="test/repo",
            repo_root=Path("/tmp/test"),
            worktree_base=Path("/tmp"),  # Top-level worktree_base
            agents={
                "claude": AgentConfig(
                    prompt_path=Path("/tmp/prompt.txt"),
                ),
            },
            max_concurrent_sessions=3,
        )

    def test_get_available_filters_unsatisfied_dependencies(
        self, sample_config, checker, events
    ):
        """Issues with unsatisfied dependencies are filtered out."""
        from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator

        checker.issues[100] = "open"  # Dependency is open (unsatisfied)

        evaluator = DependencyEvaluator(issue_checker=checker, events=events)
        scheduler = Scheduler(config=sample_config, dependency_evaluator=evaluator)

        issues = [
            Issue(
                number=1,
                title="Independent issue",
                labels=[],
                body="No dependencies",
                milestone="M1",
            ),
            Issue(
                number=2,
                title="Blocked issue",
                labels=[],
                body="Depends-on: #100",  # Depends on open issue
                milestone="M1",
            ),
        ]

        available, dep_blocked = scheduler.get_available_issues(issues)

        # Only issue 1 should be available
        assert len(available) == 1
        assert available[0].number == 1

        # Issue 2 should be in dependency_blocked
        assert len(dep_blocked) == 1
        blocked_issue, reason = dep_blocked[0]
        assert blocked_issue.number == 2
        assert "waiting on: #100" in reason

    def test_blocking_label_skip_detail_names_labels(self, sample_config):
        scheduler = Scheduler(config=sample_config)
        issue = Issue(
            number=2,
            title="Blocked issue",
            labels=["blocked-cross-milestone", "publish-failed", "agent:backend"],
            body="",
        )

        decision = scheduler.evaluate_issues([issue])[0]

        assert decision.available is False
        assert decision.reason == "blocked_label"
        assert decision.detail == (
            "blocking labels: blocked-cross-milestone (Cross-milestone dep), "
            "publish-failed (Publishing failed)"
        )

    def test_gated_tech_lead_proposal_issue_is_never_picked_up(self, sample_config):
        """proposed-tech-lead joins the blocking classification (#6778): a gated
        issue is excluded with a reason detail until an operator removes the
        gate label (per-instance approval)."""
        scheduler = Scheduler(config=sample_config)
        issue = Issue(
            number=3,
            title="Fix flaky CI runner",
            labels=["proposed-tech-lead", "agent:backend"],
            body="",
        )

        decision = scheduler.evaluate_issues([issue])[0]

        assert decision.available is False
        assert decision.reason == "blocked_label"
        assert decision.detail == (
            "blocking labels: proposed-tech-lead"
            " (Tech Lead proposal awaiting operator approval)"
        )

        # Approval = removing the gate label: the issue flows into normal
        # scheduling with nothing else changed.
        approved = Issue(
            number=3,
            title="Fix flaky CI runner",
            labels=["agent:backend"],
            body="",
        )
        assert scheduler.evaluate_issues([approved])[0].available is True

    def test_get_available_allows_satisfied_dependencies(
        self, sample_config, checker, events
    ):
        """Issues with satisfied (closed) dependencies are available."""
        from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator

        checker.issues[100] = "closed"  # Dependency is satisfied

        evaluator = DependencyEvaluator(issue_checker=checker, events=events)
        scheduler = Scheduler(config=sample_config, dependency_evaluator=evaluator)

        issues = [
            Issue(
                number=1,
                title="Issue with satisfied dep",
                labels=[],
                body="Depends-on: #100",
                milestone="M1",
            ),
        ]

        available, dep_blocked = scheduler.get_available_issues(issues)

        # Issue should be available since dependency is satisfied
        assert len(available) == 1
        assert available[0].number == 1
        assert len(dep_blocked) == 0

    def test_get_available_no_dependency_check_when_disabled(
        self, sample_config, checker, events
    ):
        """When check_dependencies=False, dependencies are not checked."""
        from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator

        checker.issues[100] = "open"  # Would block if checked

        evaluator = DependencyEvaluator(issue_checker=checker, events=events)
        scheduler = Scheduler(config=sample_config, dependency_evaluator=evaluator)

        issues = [
            Issue(
                number=1,
                title="Would be blocked",
                labels=[],
                body="Depends-on: #100",
                milestone="M1",
            ),
        ]

        available, dep_blocked = scheduler.get_available_issues(
            issues, check_dependencies=False
        )

        # Issue should be available since dependency check is disabled
        assert len(available) == 1
        assert len(dep_blocked) == 0

    def test_get_available_no_evaluator_skips_dependency_check(self, sample_config):
        """When no evaluator is provided, dependencies are not checked."""
        scheduler = Scheduler(config=sample_config)  # No dependency_evaluator

        issues = [
            Issue(
                number=1,
                title="Has dependency but no evaluator",
                labels=[],
                body="Depends-on: #100",
                milestone="M1",
            ),
        ]

        available, dep_blocked = scheduler.get_available_issues(issues)

        # Issue should be available since no evaluator
        assert len(available) == 1
        assert len(dep_blocked) == 0

    def test_dependency_evaluator_emits_event(self, sample_config, checker, events):
        """Dependency evaluator emits events for observability."""
        from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator

        checker.issues[100] = "open"

        evaluator = DependencyEvaluator(issue_checker=checker, events=events)
        scheduler = Scheduler(config=sample_config, dependency_evaluator=evaluator)

        issues = [
            Issue(
                number=1,
                title="Blocked",
                labels=[],
                body="Depends-on: #100",
                milestone="M1",
            ),
        ]

        scheduler.get_available_issues(issues)

        # Evaluator should have emitted an event
        assert len(events.events) == 1
        event = events.events[0]
        assert event.name == "dependencies.evaluated"
        assert event.data["issue_number"] == 1
        assert event.data["runnable"] is False

    def test_multiple_dependencies_all_must_be_closed(
        self, sample_config, checker, events
    ):
        """Issue with multiple dependencies only runs when ALL are closed."""
        from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator

        # Initially both dependencies are open
        checker.issues[100] = "open"
        checker.issues[200] = "open"

        evaluator = DependencyEvaluator(issue_checker=checker, events=events)
        scheduler = Scheduler(config=sample_config, dependency_evaluator=evaluator)

        issues = [
            Issue(
                number=1,
                title="Multi-dep issue",
                labels=[],
                body="Depends-on: #100\nDepends-on: #200",
                milestone="M1",
            ),
        ]

        # Both open -> blocked
        available, dep_blocked = scheduler.get_available_issues(issues)
        assert len(available) == 0
        assert len(dep_blocked) == 1
        assert "waiting on" in dep_blocked[0][1]

        # Close one, still blocked
        checker.issues[100] = "closed"
        events.events.clear()

        available, dep_blocked = scheduler.get_available_issues(issues)
        assert len(available) == 0, "Should still be blocked - only one dep closed"
        assert len(dep_blocked) == 1
        assert "#200" in dep_blocked[0][1], "Should show #200 as blocking"

        # Close both, now runnable
        checker.issues[200] = "closed"
        events.events.clear()

        available, dep_blocked = scheduler.get_available_issues(issues)
        assert len(available) == 1, "Should be available when all deps closed"
        assert len(dep_blocked) == 0

    def test_dependency_closes_issue_becomes_available(
        self, sample_config, checker, events
    ):
        """When a dependency closes, previously blocked issue becomes available."""
        from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator

        # Dependency starts open
        checker.issues[100] = "open"

        evaluator = DependencyEvaluator(issue_checker=checker, events=events)
        scheduler = Scheduler(config=sample_config, dependency_evaluator=evaluator)

        issues = [
            Issue(
                number=1,
                title="Waiting on #100",
                labels=[],
                body="Depends-on: #100",
                milestone="M1",
            ),
        ]

        # First check: blocked
        available, dep_blocked = scheduler.get_available_issues(issues)
        assert len(available) == 0
        assert len(dep_blocked) == 1
        assert dep_blocked[0][0].number == 1

        # Simulate dependency being closed
        checker.issues[100] = "closed"

        # Second check: now available
        available, dep_blocked = scheduler.get_available_issues(issues)
        assert len(available) == 1
        assert available[0].number == 1
        assert len(dep_blocked) == 0

    def test_mixed_dependencies_satisfied_unsatisfied_missing(
        self, sample_config, checker, events
    ):
        """Issue with mixed dependency states is blocked."""
        from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator

        checker.issues[100] = "closed"  # satisfied
        checker.issues[200] = "open"    # unsatisfied
        # 300 not in checker -> missing

        evaluator = DependencyEvaluator(issue_checker=checker, events=events)
        scheduler = Scheduler(config=sample_config, dependency_evaluator=evaluator)

        issues = [
            Issue(
                number=1,
                title="Complex deps",
                labels=[],
                body="Depends-on: #100\nDepends-on: #200\nDepends-on: #300",
                milestone="M1",
            ),
        ]

        available, dep_blocked = scheduler.get_available_issues(issues)

        assert len(available) == 0, "Should be blocked with any unsatisfied deps"
        assert len(dep_blocked) == 1
        reason = dep_blocked[0][1]
        # Should mention both blocking deps
        assert "#200" in reason or "#300" in reason


def _make_test_session(issue_number: int) -> "Session":
    """Helper to create a test session for scheduler tests."""
    from issue_orchestrator.domain.models import Session, SessionStatus
    from issue_orchestrator.domain.issue_key import FakeIssueKey
    from issue_orchestrator.domain.session_key import SessionKey, TaskKind
    from datetime import datetime
    from tests.unit.session_run_helpers import make_session_run_assets

    mock_issue = MagicMock()
    mock_issue.number = issue_number
    mock_issue.title = f"Test issue #{issue_number}"

    issue_key = FakeIssueKey(name=str(issue_number))
    session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)
    agent_config = AgentConfig(prompt_path=Path("/tmp/prompt.txt"))

    return Session(
        key=session_key,
        issue=mock_issue,
        agent_config=agent_config,
        terminal_id=f"test-session-{issue_number}",
        worktree_path=Path(f"/tmp/wt{issue_number}"),
        branch_name=f"issue-{issue_number}",
        run_assets=make_session_run_assets(
            Path(f"/tmp/wt{issue_number}"),
            session_name=f"test-session-{issue_number}",
        ),
        started_at=datetime.now(),
        status=SessionStatus.RUNNING,
    )


class TestSchedulerRuntimeAwareGating:
    """Tests for scheduler's runtime-aware gating for stale in-progress labels.

    Key invariant: Issues with in-progress label but no active session should
    NOT be blocked - the label is stale and will be cleaned up by the planner.
    """

    @pytest.fixture
    def sample_config(self):
        return Config(
            repo="test/repo",
            repo_root=Path("/tmp/test"),
            worktree_base=Path("/tmp"),
            agents={
                "agent:developer": AgentConfig(
                    prompt_path=Path("/tmp/prompt.txt"),
                ),
            },
            max_concurrent_sessions=3,
        )

    def test_in_progress_with_active_session_is_blocked(self, sample_config):
        """Issue with in-progress label AND active session is correctly blocked."""
        scheduler = Scheduler(config=sample_config)

        # Create an issue with in-progress label
        issues = [
            Issue(
                number=1,
                title="Active work",
                labels=["in-progress"],
                body="",
            ),
        ]

        # Create a session for this issue
        active_sessions = [_make_test_session(1)]

        available, dep_blocked = scheduler.get_available_issues(
            issues, active_sessions=active_sessions
        )

        # Issue should NOT be available (has active session)
        assert len(available) == 0
        assert len(dep_blocked) == 0

    def test_in_progress_without_active_session_is_available(self, sample_config):
        """Issue with in-progress label but NO active session is available (stale label)."""
        scheduler = Scheduler(config=sample_config)

        # Create an issue with in-progress label
        issues = [
            Issue(
                number=1,
                title="Stale in-progress",
                labels=["in-progress"],
                body="",
            ),
        ]

        # No active sessions
        active_sessions = []

        available, dep_blocked = scheduler.get_available_issues(
            issues, active_sessions=active_sessions
        )

        # Issue SHOULD be available (no session running, label is stale)
        assert len(available) == 1
        assert available[0].number == 1
        assert len(dep_blocked) == 0

    def test_in_progress_with_different_session_is_available(self, sample_config):
        """Issue with in-progress label is available if session is for a DIFFERENT issue."""
        scheduler = Scheduler(config=sample_config)

        # Issue with in-progress label
        issues = [
            Issue(
                number=1,
                title="Stale in-progress",
                labels=["in-progress"],
                body="",
            ),
        ]

        # Session is for a different issue (issue #2, not #1)
        active_sessions = [_make_test_session(2)]

        available, dep_blocked = scheduler.get_available_issues(
            issues, active_sessions=active_sessions
        )

        # Issue #1 should be available (session is for #2, so #1's label is stale)
        assert len(available) == 1
        assert available[0].number == 1

    def test_no_in_progress_label_is_available(self, sample_config):
        """Issue without in-progress label is available regardless of sessions."""
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(
                number=1,
                title="Ready issue",
                labels=[],  # No in-progress
                body="",
            ),
        ]

        available, dep_blocked = scheduler.get_available_issues(
            issues, active_sessions=[]
        )

        assert len(available) == 1
        assert available[0].number == 1

    def test_backward_compatibility_no_active_sessions_param(self, sample_config):
        """Backward compatibility: if active_sessions not provided, old behavior applies."""
        scheduler = Scheduler(config=sample_config)

        # Issue with in-progress label
        issues = [
            Issue(
                number=1,
                title="In progress",
                labels=["in-progress"],
                body="",
            ),
        ]

        # Call without active_sessions parameter (backward compat)
        available, dep_blocked = scheduler.get_available_issues(issues)

        # Old behavior: in-progress would always block
        # New behavior with None: treat as no sessions, so label is stale
        assert len(available) == 1  # Available because no sessions to check against

    def test_multiple_issues_mixed_states(self, sample_config):
        """Multiple issues with mixed in-progress and session states."""
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(number=1, title="Active", labels=["in-progress"], body=""),
            Issue(number=2, title="Stale", labels=["in-progress"], body=""),
            Issue(number=3, title="Ready", labels=[], body=""),
        ]

        # Session only for issue #1
        active_sessions = [_make_test_session(1)]

        available, dep_blocked = scheduler.get_available_issues(
            issues, active_sessions=active_sessions
        )

        # Issue #1 blocked (has session), #2 available (stale), #3 available (no label)
        available_numbers = {i.number for i in available}
        assert available_numbers == {2, 3}


class TestLaunchSessionDependencyCAS:
    """Tests for CAS (Compare-And-Swap) dependency check at launch time.

    These tests verify that launch_session re-checks dependencies to handle
    the race condition where an issue's dependencies may have changed
    between scheduling and launching.
    """

    @pytest.fixture
    def checker(self):
        return MockIssueChecker()

    @pytest.fixture
    def events(self):
        return CollectingEventSink()

    def test_launch_skips_if_dependencies_added(self, checker, events):
        """launch_session skips if issue gained new unsatisfied dependencies."""
        from issue_orchestrator.infra.orchestrator import Orchestrator
        from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator
        from issue_orchestrator.infra.config import Config
        from unittest.mock import patch, MagicMock

        # Dependency is open
        checker.issues[100] = "open"

        evaluator = DependencyEvaluator(issue_checker=checker, events=events)

        # Create orchestrator with mocked dependencies
        config = MagicMock(spec=Config)
        config.repo = "test/repo"
        config.repo_root = "/tmp"
        config.worktree_base = "/tmp"  # Top-level worktree_base
        config.agents = {"agent:backend": MagicMock()}  # No per-agent worktree_base/repo_root
        config.setup_worktree = None

        # Create a mock repository host
        mock_repository_host = MagicMock()
        mock_repository_host.add_label = MagicMock()

        with patch.object(Orchestrator, '__init__', lambda self, *args, **kwargs: None):
            orch = Orchestrator.__new__(Orchestrator)
            orch.config = config
            orch.state = MagicMock()
            orch.state.active_sessions = []
            orch.scheduler = MagicMock()
            orch.scheduler.dependency_evaluator = evaluator
            # Create mock deps with all required attributes
            orch.deps = MagicMock()
            orch.deps.events = events
            orch.deps.runner = MagicMock()
            orch.deps.runner.session_exists.return_value = False
            orch.deps.repository_host = mock_repository_host
            orch.deps.session_manager = MagicMock()
            orch.deps.worktree_manager = MagicMock()
            orch.deps.working_copy = MagicMock()
            orch.deps.command_runner = MagicMock()
            orch.deps.session_restorer.restore_known_terminal.return_value = []
            # Build a REAL launcher from these mocks. Assembly moved to the
            # composition root, so a MagicMock factory would return a mock
            # launcher and the dependency CAS check under test would never
            # run (#6924 A3-R2).
            orch.deps.session_launcher_factory = build_session_launcher_factory(
                config=config,
                events=events,
                repository_host=mock_repository_host,
                action_applier=orch.deps.action_applier,
                session_manager=orch.deps.session_manager,
                worktree_manager=orch.deps.worktree_manager,
                working_copy=orch.deps.working_copy,
                command_runner=orch.deps.command_runner,
                session_output=orch.deps.session_output,
                manifest_downloader=orch.deps.manifest_downloader,
                tech_lead_authority=orch.deps.tech_lead_authority,
                claim_manager=orch.deps.claim_manager,
                provider_resilience=orch.deps.provider_resilience,
                state_machine_manager=orch.deps.state_machine_manager,
                label_manager=orch.deps.label_manager,
                agent_callback_endpoint=ready_callback_endpoint(),
            )

        # Original issue had no dependencies
        issue = Issue(
            number=1,
            title="Test",
            labels=["agent:backend"],
            body="No deps originally",
        )

        # But when refreshed, it now has a dependency
        fresh_issue = Issue(
            number=1,
            title="Test",
            labels=["agent:backend"],
            body="Depends-on: #100",  # New dependency added!
        )

        with patch.object(orch, '_refresh_issue', return_value=fresh_issue):
            with patch.object(orch, '_session_exists', return_value=False):
                result = orch.launch_session(issue)

        # Should have skipped due to new unsatisfied dependency
        assert result is None

    def test_launch_does_not_block_if_dependencies_satisfied(self, checker, events):
        """launch_session does not emit block event if dependencies are satisfied."""
        from issue_orchestrator.infra.orchestrator import Orchestrator
        from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator
        from issue_orchestrator.infra.config import Config
        from unittest.mock import patch, MagicMock

        # Dependency is closed (satisfied)
        checker.issues[100] = "closed"

        evaluator = DependencyEvaluator(issue_checker=checker, events=events)

        config = MagicMock(spec=Config)
        config.repo = "test/repo"
        config.repo_root = "/tmp/repo"
        config.worktree_base = "/tmp"  # Top-level worktree_base
        config.agents = {"agent:backend": MagicMock()}  # No per-agent worktree_base/repo_root
        config.setup_worktree = None

        # Create a mock repository host
        mock_repository_host = MagicMock()
        mock_repository_host.add_label = MagicMock()

        # Create mock worktree manager
        mock_worktree_manager = MagicMock()
        mock_worktree_manager.create.side_effect = Exception("Stop here - deps check passed")

        with patch.object(Orchestrator, '__init__', lambda self, *args, **kwargs: None):
            orch = Orchestrator.__new__(Orchestrator)
            orch.config = config
            orch.state = MagicMock()
            orch.state.active_sessions = []
            orch.scheduler = MagicMock()
            orch.scheduler.dependency_evaluator = evaluator
            # Create mock deps with all required attributes
            orch.deps = MagicMock()
            orch.deps.events = events
            orch.deps.runner = MagicMock()
            orch.deps.runner.session_exists.return_value = False
            orch.deps.repository_host = mock_repository_host
            orch.deps.session_manager = MagicMock()
            orch.deps.worktree_manager = mock_worktree_manager
            orch.deps.working_copy = MagicMock()
            orch.deps.command_runner = MagicMock()
            orch.deps.session_restorer.restore_known_terminal.return_value = []
            # Build a REAL launcher from these mocks. Assembly moved to the
            # composition root, so a MagicMock factory would return a mock
            # launcher and the dependency CAS check under test would never
            # run (#6924 A3-R2).
            orch.deps.session_launcher_factory = build_session_launcher_factory(
                config=config,
                events=events,
                repository_host=mock_repository_host,
                action_applier=orch.deps.action_applier,
                session_manager=orch.deps.session_manager,
                worktree_manager=orch.deps.worktree_manager,
                working_copy=orch.deps.working_copy,
                command_runner=orch.deps.command_runner,
                session_output=orch.deps.session_output,
                manifest_downloader=orch.deps.manifest_downloader,
                tech_lead_authority=orch.deps.tech_lead_authority,
                claim_manager=orch.deps.claim_manager,
                provider_resilience=orch.deps.provider_resilience,
                state_machine_manager=orch.deps.state_machine_manager,
                label_manager=orch.deps.label_manager,
                agent_callback_endpoint=ready_callback_endpoint(),
            )

        issue = Issue(
            number=1,
            title="Test",
            labels=["agent:backend"],
            body="Depends-on: #100",
        )

        # Fresh issue still has same dependency (which is satisfied)
        fresh_issue = Issue(
            number=1,
            title="Test",
            labels=["agent:backend"],
            body="Depends-on: #100",
        )

        # We only test up to the dependency check - if it passes, launch continues
        # The rest of the launch will fail due to incomplete mocking, but that's OK
        with patch.object(orch, '_refresh_issue', return_value=fresh_issue):
            with patch.object(orch, '_session_exists', return_value=False):
                # If we get to create_worktree, the dependency check passed
                try:
                    orch.launch_session(issue)
                except Exception as e:
                    if "Stop here" not in str(e):
                        raise  # Re-raise unexpected errors

        # The key assertion: no dependency_blocked event was emitted
        dep_blocked_events = [e for e in events.events if e.name == "issue.dependency_blocked"]
        assert len(dep_blocked_events) == 0, "Should not have blocked - deps are satisfied"


class _StackFactsProvider:
    """Test stack-predecessor facts provider with canned facts per target."""

    def __init__(self, facts):
        from issue_orchestrator.domain.dependency_gates import PredecessorFacts
        self._facts = facts
        self._default = PredecessorFacts()
        self.calls = []

    def gather_facts(self, targets):
        self.calls.append(list(targets))
        return {t: self._facts.get(t, self._default) for t in targets}


class TestSchedulerStackGating:
    """Scheduler consumes the dependency WORK gate for stack predecessors.

    A stack successor stays blocked for work until its predecessor exposes a
    usable, validated, agent-reviewed branch — without waiting for merge — while
    normal Depends-on: issues keep waiting for a closed dependency exactly as
    before.
    """

    @pytest.fixture
    def checker(self):
        return MockIssueChecker()

    @pytest.fixture
    def events(self):
        return CollectingEventSink()

    @pytest.fixture
    def sample_config(self):
        return Config(
            repo="test/repo",
            repo_root=Path("/tmp/test"),
            worktree_base=Path("/tmp"),
            agents={"claude": AgentConfig(prompt_path=Path("/tmp/prompt.txt"))},
            max_concurrent_sessions=3,
        )

    def _evaluator(self, checker, events, provider=None):
        from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator
        return DependencyEvaluator(
            issue_checker=checker, events=events, predecessor_facts_provider=provider
        )

    def test_stack_successor_blocked_when_predecessor_not_ready(self, sample_config, checker, events):
        from issue_orchestrator.domain.dependencies import DependencyTarget
        from issue_orchestrator.domain.dependency_gates import PredecessorFacts

        checker.issues[20] = "open"  # predecessor open (unsatisfied)
        provider = _StackFactsProvider(
            {DependencyTarget(20): PredecessorFacts(branch_usable=True, validation_passed=True, agent_reviewed=False)}
        )
        scheduler = Scheduler(
            config=sample_config,
            dependency_evaluator=self._evaluator(checker, events, provider),
        )

        issues = [Issue(number=2, title="Successor", labels=[], body="Stack-after: #20", milestone="M1")]
        available, dep_blocked = scheduler.get_available_issues(issues)

        assert available == []
        assert len(dep_blocked) == 1
        assert dep_blocked[0][0].number == 2

    def test_stack_successor_available_when_predecessor_ready(self, sample_config, checker, events):
        from issue_orchestrator.domain.dependencies import DependencyTarget
        from issue_orchestrator.domain.dependency_gates import PredecessorFacts

        checker.issues[20] = "open"  # predecessor still open, but branch ready
        provider = _StackFactsProvider(
            {DependencyTarget(20): PredecessorFacts(
                branch_usable=True, validation_passed=True, agent_reviewed=True, branch_name="20-base"
            )}
        )
        scheduler = Scheduler(
            config=sample_config,
            dependency_evaluator=self._evaluator(checker, events, provider),
        )

        issues = [Issue(number=2, title="Successor", labels=[], body="Stack-after: #20", milestone="M1")]
        available, dep_blocked = scheduler.get_available_issues(issues)

        assert [i.number for i in available] == [2]
        assert dep_blocked == []

    def test_stack_successor_blocked_when_predecessor_state_missing(self, sample_config, checker, events):
        # Predecessor open, but no facts gathered (no provider) -> conservatively blocked.
        checker.issues[20] = "open"
        scheduler = Scheduler(
            config=sample_config,
            dependency_evaluator=self._evaluator(checker, events, provider=None),
        )

        issues = [Issue(number=2, title="Successor", labels=[], body="Stack-after: #20", milestone="M1")]
        available, dep_blocked = scheduler.get_available_issues(issues)

        assert available == []
        assert len(dep_blocked) == 1

    def test_normal_dependency_unaffected_by_provider(self, sample_config, checker, events):
        from issue_orchestrator.domain.dependencies import DependencyTarget
        from issue_orchestrator.domain.dependency_gates import PredecessorFacts

        # Even a "ready" provider must not unblock a normal (non-stack) open dep.
        checker.issues[100] = "open"
        provider = _StackFactsProvider(
            {DependencyTarget(100): PredecessorFacts(branch_usable=True, validation_passed=True, agent_reviewed=True)}
        )
        scheduler = Scheduler(
            config=sample_config,
            dependency_evaluator=self._evaluator(checker, events, provider),
        )

        issues = [Issue(number=2, title="Normal", labels=[], body="Depends-on: #100", milestone="M1")]
        available, dep_blocked = scheduler.get_available_issues(issues)

        assert available == []
        assert "waiting on: #100" in dep_blocked[0][1]
        # Provider never consulted for a normal edge.
        assert provider.calls == []

    def test_blocked_event_identifies_mode_gate_predecessor_reason(self, sample_config, checker, events):
        from issue_orchestrator.domain.dependencies import DependencyTarget
        from issue_orchestrator.domain.dependency_gates import PredecessorFacts

        checker.issues[20] = "open"
        provider = _StackFactsProvider(
            {DependencyTarget(20): PredecessorFacts(branch_usable=False, validation_passed=False, agent_reviewed=False)}
        )
        scheduler = Scheduler(
            config=sample_config,
            dependency_evaluator=self._evaluator(checker, events, provider),
        )

        issues = [Issue(number=2, title="Successor", labels=[], body="Stack-after: #20", milestone="M1")]
        scheduler.get_available_issues(issues)

        evaluated = [e for e in events.events if e.name == "dependencies.evaluated"]
        assert len(evaluated) == 1
        data = evaluated[0].data
        assert data["issue_number"] == 2
        assert data["gate"] == "work"
        assert data["runnable"] is False
        reasons = data["blocked_reasons"]
        assert any(
            r["mode"] == "stack" and r["gate"] == "work" and r["predecessor"] == "#20"
            and r["reason"] == "predecessor_branch_unusable"
            for r in reasons
        )
