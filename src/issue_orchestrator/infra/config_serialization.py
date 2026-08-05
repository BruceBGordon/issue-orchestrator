"""Per-section serializers for :meth:`Config.to_dict`.

``Config.to_dict`` reconstructs a YAML-shaped mapping field by field, emitting a
key only when it differs from the loader default so round-tripped configs stay
minimal. That is inherently branch-heavy, and keeping every section inline made
one method a C901/PLR0912 hotspot. Each self-contained section lives here as a
small function that returns its sub-dict (empty when everything is at default);
``to_dict`` assembles them. Splitting by section keeps each unit simple and lets
``config.py`` shed the bulk without changing a single serialized byte.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config


def observability_section(config: "Config") -> dict:
    section: dict = {}
    if config.session_no_output_seconds != 120:
        section["session_no_output_seconds"] = config.session_no_output_seconds
    if config.stale_escalation_ticks != 0:
        section["stale_escalation_ticks"] = config.stale_escalation_ticks
    if config.session_output_retention_runs != 7:
        section["session_output_retention_runs"] = config.session_output_retention_runs
    if config.session_output_retention_days != 7:
        section["session_output_retention_days"] = config.session_output_retention_days
    if config.session_output_retention_tier != "hot":
        section["session_output_retention_tier"] = config.session_output_retention_tier
    return section


def filtering_section(config: "Config") -> dict:
    section: dict = {}
    if config.filtering.label:
        section["label"] = config.filtering.label
    if config.filtering.milestones:
        section["milestones"] = list(config.filtering.milestones)
    elif config.filtering.milestone:
        section["milestone"] = config.filtering.milestone
    if config.filtering.exclude_labels:
        section["exclude_labels"] = list(config.filtering.exclude_labels)
    if config.filtering.exclude_label_prefixes:
        section["exclude_label_prefixes"] = list(
            config.filtering.exclude_label_prefixes
        )
    if config.filtering.fetch_limit != 100:
        section["fetch_limit"] = config.filtering.fetch_limit
    if config.filtering.max_to_start != 0:
        section["max_to_start"] = config.filtering.max_to_start
    return section


def goal_pilot_section(config: "Config") -> dict:
    section: dict = {}
    if config.goal_pilot.enabled:
        section["enabled"] = True
    if config.goal_pilot.agent:
        section["agent"] = config.goal_pilot.agent
    if config.goal_pilot.approval_policy != "journeys_only":
        section["approval_policy"] = config.goal_pilot.approval_policy
    if config.goal_pilot.approval_batch_size != 10:
        section["approval_batch_size"] = config.goal_pilot.approval_batch_size
    if config.goal_pilot.approval_batch_window_minutes != 60:
        section["approval_batch_window_minutes"] = (
            config.goal_pilot.approval_batch_window_minutes
        )
    return section


def merge_queue_section(config: "Config") -> dict:
    section: dict = {}
    if config.merge_queue.enabled:
        section["enabled"] = True
    if config.merge_queue.provider != "github":
        section["provider"] = config.merge_queue.provider
    if config.merge_queue.enqueue_after != "code-reviewed":
        section["enqueue_after"] = config.merge_queue.enqueue_after
    if config.merge_queue.failure_action != "rework":
        section["failure_action"] = config.merge_queue.failure_action
    return section


def worktrees_section(config: "Config") -> dict:
    from .config_paths import default_worktree_base, default_worktree_base_config

    section: dict = {
        "base": (
            default_worktree_base_config(config.repo_root)
            if config.worktree_base == default_worktree_base(config.repo_root)
            else str(config.worktree_base)
        )
    }
    if config.worktree_base_branch_override:
        section["base_branch_override"] = config.worktree_base_branch_override
    if config.worktree_seed_ref:
        section["seed_ref"] = config.worktree_seed_ref
    if config.setup_worktree:
        section["setup"] = list(config.setup_worktree)
    if config.worktree_branch_on_recreate != "delete":
        section["worktree_branch_on_recreate"] = config.worktree_branch_on_recreate
    return section
