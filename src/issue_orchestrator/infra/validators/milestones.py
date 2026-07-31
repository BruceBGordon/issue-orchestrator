"""Milestone configuration validator."""

from typing import TYPE_CHECKING

from .base import ConfigValidator

if TYPE_CHECKING:
    from ..config import Config


class MilestonesValidator(ConfigValidator):
    """Validates milestone settings that runtime compares verbatim.

    Checks:
    - milestones.foundation is present and non-blank
    - milestones.foundation has no leading or trailing whitespace
    """

    def validate(self, config: "Config") -> list[str]:
        errors: list[str] = []
        errors.extend(self._validate_foundation(config))
        return errors

    def _validate_foundation(self, config: "Config") -> list[str]:
        """Reject a foundation milestone runtime could never match.

        Dependency scoping compares this against GitHub milestone titles by
        exact string equality::

            if dep_milestone != source_milestone and dep_milestone != foundation_milestone:

        so an unmatched value does not degrade gracefully — it silently
        disables the foundation exemption entirely. Every cross-milestone edge
        into the intended foundation evaluates as CROSS_MILESTONE, and each
        dependent issue is blocked by an error naming a milestone the user
        believes they configured.

        A padded or blank value is therefore a configuration error rather than
        an alternate value, and is rejected at startup. Deliberately NOT
        trimmed: normalising in any single path is what makes config, runtime,
        and doctor disagree, which is the defect this rule exists to prevent
        (#6939 B4/B7). Interior spacing is ordinary in a title
        ("M0 - Foundation") and must survive untouched.
        """
        foundation = config.foundation_milestone
        if foundation is None or not str(foundation).strip():
            return [
                "milestones.foundation must be a non-empty milestone title"
                f" (got {foundation!r}); it names the one milestone any other"
                " milestone may depend on"
            ]
        if foundation != foundation.strip():
            return [
                "milestones.foundation must not have leading or trailing"
                f" whitespace (got {foundation!r}); it is compared to GitHub"
                " milestone titles by exact equality"
            ]
        return []
