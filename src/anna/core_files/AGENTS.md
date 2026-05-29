---
name: AGENTS.md
purpose: Registry of sub-agents ANNA has hired, with their purposes.
token_cap: 1500
last_evicted: null
---

<!--
AGENTS.md lists every sub-agent persona ANNA has created in the agents
directory. Each entry pairs a slug with a one-line description of what the
agent is for. This file is the index Claude reads when deciding which
specialist to delegate to.

The token cap is 1500. The supervisor warns when the file passes 1350 tokens
(90 percent of cap) and ANNA proposes an eviction at session close. Eviction
of an AGENTS.md entry never deletes the underlying agents/<slug>.md file; it
only trims the index.
-->
