"""Sub-agent registry.

Per v3 section 6. Sub-agents are ANNA-authored persona files at
``agents/<slug>.md`` with indefinite persistence. Created on demand the first
time ANNA decides she needs a role she does not have. The supervisor lock
prevents duplicate creation when two workers decide simultaneously.
"""

from anna.agents.registry import SubAgentRegistry, SubAgentSpec

__all__ = ["SubAgentRegistry", "SubAgentSpec"]
