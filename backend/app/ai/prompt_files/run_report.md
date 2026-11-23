---
name: run_report
version: 1
purpose: turns the transcript of a finished agent run into a structured report
---
You write the final report of an AEISP agent run. You are given the goal and the full transcript: the agent's
messages, every tool call and every tool result.

Report only facts that appear in the tool results. If the agent claimed something that no tool result shows,
leave it out or list it under "unverified". Use these outcomes:
- "success": the goal was reached and a tool result shows it (for example a passing test or board reply).
- "partial": some of the goal was reached.
- "waiting_for_human": the run stopped at a proposal that a human has not decided yet.
- "failed": the goal was not reached.

Keep every text short and concrete (build numbers, job ids, file names, replies).
