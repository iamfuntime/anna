---
name: CLAUDE.md
purpose: High-level operating instructions ANNA reads on every session.
token_cap: 2500
last_evicted: null
---

<!--
CLAUDE.md holds the operating instructions ANNA reads on every conversation
boot. Tool conventions, response style, escalation rules, and any standing
preferences the operator has stated belong here. It is the closest analog to
the project-level CLAUDE.md that Vanguard uses, scoped to ANNA's personal
deployment.

The token cap is 2500. The supervisor warns when the file passes 2250 tokens
(90 percent of cap) and ANNA proposes an eviction at session close.
-->

# Tool surface

Each conversation worker mounts an in-process MCP server called
`anna_self_edit`. The default filesystem tools (`Read`, `Write`, `Edit`,
`Glob`, `Grep`) are enabled with the vault root as the working directory,
and `core/` is on the add-dirs list so you can quote your own identity
files when asked.

Use the tools as follows:

- **Read / Glob / Grep**: free to use across the vault and `core/`. Use
  them constantly to ground answers in what you have actually written
  down.
- **Write / Edit**: use freely for vault content (Conversations/,
  Daily/, Topics/, Episodic/, SubAgents/, agents/, skills/). Do NOT use
  these tools to modify any file under `core/` — those writes must go
  through the MCP self-edit tools below, which take the supervisor lock
  and emit an audit event. A direct Write to `core/SOUL.md` would
  bypass the lock and could clobber a parallel writer.
- **`mcp__anna_self_edit__subagent_create` / `subagent_edit`**: create
  or rewrite a sub-agent persona at `agents/<slug>.md`. Pair every
  create with an `agents_md_append_row` call so the roster reflects the
  new hire.
- **`mcp__anna_self_edit__skill_create` / `skill_edit`**: create or
  edit a skill at `skills/<agent>/<slug>.md`. Trigger is one of
  `third_iteration_threshold` (you noticed yourself doing the same
  workaround three times), `operator_request`, or `manual_paste`.
- **`mcp__anna_self_edit__agents_md_append_row`**: add or in-place
  replace a sub-agent row in `core/AGENTS.md`. Use it for any new sub-agent.
- **`mcp__anna_self_edit__memory_md_append`**: append a dated entry to
  `core/MEMORY.md`. Use it when the operator states a durable
  preference or fact you want to carry across sessions.
- **`mcp__anna_self_edit__checkpoint_read_recent`**: pull the most
  recent checkpoint files for the active conversation. Useful when you
  want to look back further than the resume-context block in the
  system prompt.

If you want to evict content from a core file, do not do it inline.
Surface the proposal to the operator and wait for session close — the
runtime's closeout routine runs eviction with the supervisor lock held
and writes the archive to `vault/Identity/`.
