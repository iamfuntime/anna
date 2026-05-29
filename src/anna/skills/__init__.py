"""Skill registry.

Per v3 section 6. Skills are persona modifiers attached to sub-agents and
live at ``$ANNA_HOME/skills/<agent>/<slug>.md``. Auto-created after the third
invocation of the same task variant.
"""

from anna.skills.registry import SkillRegistry, SkillSpec

__all__ = ["SkillRegistry", "SkillSpec"]
