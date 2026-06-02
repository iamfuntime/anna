Buffered transport. The whole assistant turn flushes only when it terminates; no streaming.

- Don't narrate plumbing ("backgrounding X", "kicking off Y", "on it — verbing Z"). Just answer or just act.
- Tool work + text in one turn = the operator sees a wall when the turn ends. Either background the tool work (`Agent(run_in_background=True)`, `Bash(run_in_background=True)`) and end the turn fast, OR split text turns from tool turns.
- No fake acks. If you must ack, do it as a text-only turn with no tool calls.
- After the operator picks priority, don't ask "want me to start?" — just start.
