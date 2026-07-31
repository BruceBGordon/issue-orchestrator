"""Milestone configuration validator."""

from typing import TYPE_CHECKING

from .base import ConfigValidator

if TYPE_CHECKING:
    from ..config import Config


class MilestonesValidator(ConfigValidator):
    """Validates milestone settings that runtime compares verbatim.

    Checks:
    - milestones.foundation is a string (YAML scalars arrive unconverted)
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
        # Unset is a missing value, not a wrong type — report it as such.
        if foundation is None:
            return [
                "milestones.foundation must be a non-empty milestone title"
                f" (got {foundation!r}); it names the one milestone any other"
                " milestone may depend on"
            ]
        # Type before any string operation. Config.load keeps the raw YAML
        # scalar, so `foundation: 123` arrives as an int and .strip() would
        # raise AttributeError out of Config.validate() — a validator crashing
        # on exactly the input it exists to reject, taking every other check in
        # the same pass down with it (#6939 B8). A wrong type is a configuration
        # error to report, not an exception to propagate.
        if not isinstance(foundation, str):
            return [
                "milestones.foundation must be a string milestone title (got"
                f" {type(foundation).__name__}: {foundation!r}); YAML scalars"
                " are kept unconverted, so quote a title that looks like a"
                " number or boolean"
            ]
        if not foundation.strip():
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
