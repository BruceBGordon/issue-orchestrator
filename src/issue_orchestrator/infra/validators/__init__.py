"""Config validators module.

Each validator checks a specific aspect of configuration and returns
a list of error messages. This separation makes validators independently
testable and the Config.validate() method cleaner.

Usage:
    errors = []
    errors.extend(WorktreeValidator().validate(config))
    errors.extend(AgentValidator().validate(config))
    errors.extend(ReviewWorkflowValidator().validate(config))
    errors.extend(IsolationValidator().validate(config))
    errors.extend(TemplateValidator().validate(config))
    errors.extend(UnknownFieldsValidator().validate(config))
"""

from .base import ConfigValidator
from .worktree import WorktreeValidator
from .agent import AgentValidator
from .goal_pilot import GoalPilotValidator
from .milestones import MilestonesValidator
from .review import ReviewWorkflowValidator
from .isolation import IsolationValidator
from .template import TemplateValidator
from .unknown_fields import UnknownFieldsValidator

__all__ = [
    "ConfigValidator",
    "WorktreeValidator",
    "AgentValidator",
    "GoalPilotValidator",
    "MilestonesValidator",
    "ReviewWorkflowValidator",
    "IsolationValidator",
    "TemplateValidator",
    "UnknownFieldsValidator",
]
