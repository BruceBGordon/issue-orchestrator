"""Canonical human-facing name for the tech lead agent role.

Presentational only. This is the canonical human word ("Tech Lead"), wired
into the highest-traffic operator surfaces where re-labelling matters most:
the settings form (field titles/sections) and orchestrator-generated
batch-review issue titles. Incidental literals elsewhere (the setup wizard,
doctor check names, log lines, label descriptions) are intentionally left
inline rather than centralised behind a constant nobody would think to look up.

Deliberately narrow: internal identifiers (modules, classes, config keys,
GitHub labels, event names) are NOT routed through this constant. They stay
aligned with the concept and are read/written directly, so the code remains
greppable and honest. Indirection is reserved for the presentational layer;
see the tech-lead rename discussion for the reasoning.
"""
from __future__ import annotations

#: Human-facing display name of the tech lead agent.
TECH_LEAD_DISPLAY_NAME = "Tech Lead"
