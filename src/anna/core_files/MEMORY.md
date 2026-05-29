---
name: MEMORY.md
purpose: Long-term facts and preferences ANNA carries across conversations.
token_cap: 3000
last_evicted: null
---

<!--
MEMORY.md is the working memory ANNA reads on every conversation boot. Facts
about the operator's environment, decisions they have made, preferences they
have stated, and ongoing topics that span sessions all live here. This is
the file most likely to bump the token cap; eviction targets the least
relevant entries first.

The token cap is 3000. The supervisor warns when the file passes 2700 tokens
(90 percent of cap) and ANNA proposes an eviction at session close.
-->
