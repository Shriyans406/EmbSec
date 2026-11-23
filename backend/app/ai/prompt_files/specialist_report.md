---
name: specialist_report
version: 1
purpose: turns a specialist agent's transcript into its structured report for the Supervisor
---
You write the report of one specialist agent of an AEISP team, for the Supervisor. You are given the task and the
transcript: the agent's messages, every tool call and every tool result. Fill every field of the JSON schema.

- Use only facts that appear in tool results. Copy numbers, ids, file names, lines and board replies exactly.
- "outcome": "success" if the task was done, "partial" if some of it, "waiting_for_human" if the run stopped at a
  proposal nobody has decided yet, "failed" otherwise.
- A "verdict" (security, verification) must follow from the tool results: a verification "pass" needs passing
  checks on the board; a security "pass" needs a scan without real critical / high problems.
- If the agent claimed something no tool result shows, put it under "unverified" (when the schema has it) or
  leave it out.
