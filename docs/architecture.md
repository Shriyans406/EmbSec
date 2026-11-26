# Architecture

## The layers

```
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│ Dashboard (React 19 + Vite, served at /ui/)        CLIs (python -m app.<area>.cli)            │
│   live feed over WebSocket /ws ── typed approval dialog                                       │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ FastAPI  ── bearer tokens: viewer < agent < operator ── audit of every change and refusal     │
├───────────────┬───────────────────────────────┬──────────────────────────────────────────────┤
│ Workflows     │ Agents (LangGraph + Gemini)   │ Services                                     │
│  pipeline     │  lab, supervisor, firmware,   │  firmware sources · builds · deployments     │
│  remediation  │  build, security, performance,│  tests · fuzzing · profiling · knowledge     │
│  optimisation │  verification, debugger       │  records · observability                     │
│               │  └─ tool registry (54 tools,  │                                              │
│               │     role-checked)             │                                              │
├───────────────┴───────────────────────────────┴──────────────────────────────────────────────┤
│ JobManager: queued jobs in lanes  build · board · ai · system. Approval gates. Expiry. Logs.  │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ Pico SDK build (CMake/Ninja/ARM GCC) │ Board link (pyserial, CRC, ids)  │ SQLite + files       │
└──────────────────────────────────────┴──────────────────────────────────┴──────────────────────┘
                                          │ USB CDC serial / UF2 drive
                                   RP2040 (Shrike Lite): harness + app
```

## The firmware

The firmware has two parts. The **harness** is fixed and stays the same in every build. The **app** is the
part that changes: it is written by a person or an agent.

- **The harness** (`firmware/harness/src`) owns USB, the watchdog (3 s), the LED (GPIO4) and the line
  protocol. It provides the built-in commands (`PING`, `HELLO`, `INFO`, `STATUS`, `UPTIME`, `TEMP`, `LED`,
  `BLINK`, `ECHO`, `TIMER`, `RESETINFO`, `PROF`, and the guarded `BOOTSEL`, `HANG` and `REBOOT`). It also
  contains the profiler (`prof.c`).
- **The app** (`firmware/workspace/<project>/app/*.c`) implements `app_init()`, `app_tick()` and
  `app_command()`. It is reached with `APP <args>` and can time its own code with
  `app_prof_begin/app_prof_end`.

**Wire protocol (v1).** A request line looks like `[#<id> ]COMMAND [args][*CRC16]`. A reply is
`[#<id> ]OK <payload>[*CRC]` or `ERR <code> <NAME> [detail]`. The CRC is CRC-16/CCITT-FALSE. Ids pair replies
with requests, so a late reply is never taken for the wrong command.

## The backend (`backend/app`)

| Package | Responsibility |
|---|---|
| `api/` | The HTTP routes, one module per area, and the WebSocket live feed. |
| `security.py`, `audit.py` | Tokens and roles, the failed-token throttle, and the hash-chained audit log. |
| `jobs.py` | The JobManager: lanes, queueing, approval with confirmation text, expiry, cancellation and logs. |
| `hardware/` | The serial link: framing, CRC, retries, reconnects and the command allow-list. |
| `build/` | Projects and sources, builds, diagnostics parsing, size reports and UF2 checks. |
| `analysis/` | The security scan and size analysis that are run before a flash. |
| `deploy/` | The pre-flight gate, BOOTSEL, UF2 copy, verification, rollback, known-good and factory images. |
| `testfw/` | Test suites, regression comparison and coverage. |
| `fuzz/` | Generators (with a safety filter), the oracle, campaigns, reproduction and minimisation. |
| `profiling/` | Benchmarks, baselines and comparisons with a noise margin. |
| `ai/` | The model client (Gemini, retries, usage log), agent specs, prompts and the LangGraph graph with checkpoints. |
| `tools.py` | The tool registry: JSON schemas, role checks, argument validation and per-tool limits. |
| `pipeline/`, `remediate/`, `optimize/` | Workflows built as resumable state machines that stop at approvals. |
| `knowledge/` | Collectors, the FTS5 search, the memory given to agents, timelines and board history. |
| `records/` | Task records, verification, replay, rebuild, and reports in md, pdf and json. |
| `observability.py` | `/metrics` (Prometheus) and the deep health check. |

### Jobs and lanes

Anything slow is a job. Each job kind runs in one lane, and a lane runs one job at a time. Different lanes run
in parallel, so a build can run while the board is being tested.

| Lane | Job kinds |
|---|---|
| build | `build`, `rebuild` |
| board | `deploy`, `recover`, `test`, `fuzz`, `fuzz_repro`, `benchmark` |
| ai | `agent_run`, `agent_resume`, `pipeline`, `remediation`, `optimization` |
| system | `selftest` |

A job that changes the board starts as `AWAITING_APPROVAL`. It runs only after the operator approves it with
the exact confirmation text. If nobody approves it within 15 minutes, it becomes `EXPIRED`.

### How an agent works

The agent graph is plan → model → tools → model … → report. A step works like this:

1. The model gets the task, the relevant entries from the knowledge base, and the tools its role allows.
2. Each tool call is checked before it runs: does the tool exist for this agent, are the arguments
   valid, is the command allowed, is the filename safe, does the build pass the security gate.
3. A refused call goes back to the model as a structured error, such as `tool_not_allowed` or
   `invalid_arguments`, so it can correct itself. Nothing happens on the board.
4. The run stops at its step budget, its token budget, an operator stop, or a final answer. Checkpoints
   allow a run to resume after an approval.

The supervisor gives subtasks to specialist agents. The workflows (pipeline, remediation, optimisation) use
agents for single stages, such as writing code or finding a root cause. The workflow code decides the order of
the stages and checks every result. The model only acts inside a stage.

### Flash safety

The deploy path runs in this order:

1. **Pre-flight**: the build succeeded and the UF2 file is valid, the sources hash matches, the security scan
   is clean, the image fits, the board is known, and a recovery image exists.
2. **Approval**: an operator types the confirmation text.
3. **BOOTSEL**: the board is put into its UF2 bootloader.
4. **Flash**: the UF2 file is copied.
5. **Verify**: the board must come back and report the firmware version, build id and sources hash of the
   image that was just flashed.
6. **Rollback** if anything above fails: the known-good image goes back, or the factory image. If even that
   fails, the result is `ROLLBACK_FAILED` with steps for a person (hold BOOT).

Any unexpected exception in steps 3–5 also leads to step 6. Phase 17's failure tests check this.

## Data

There is one SQLite database, `data/aeisp.db`. The schema changes are numbered migrations, 13
so far. Its main tables:

`jobs`, `job_logs`, `audit_log`, `builds`, `test_runs`, `test_suites`, `test_cases`, `fuzz_campaigns`,
`fuzz_findings`, `benchmarks`, `benchmark_runs`, `agent_runs`, `agent_steps`, `llm_calls`, `pipelines`,
`pipeline_events`, `remediations`, `remediation_events`, `optimizations`, `optimization_events`, `knowledge`
(+ FTS5 index), `schema_migrations`.

Files are stored outside the database:

- `data/artifacts/<id>/`: the UF2 image, the ELF file and the build log.
- `data/pipelines|remediations|optimizations/<id>/`: the work folders.
- `data/agent_checkpoints.db`: the LangGraph checkpoints.
- `firmware/known_good/`: the known-good image.
- `backups/`: the factory image.
- `data/logs/backend.log`: the backend log, with tokens redacted.

**Audit chain.** Each audit row stores `hash = sha256(previous hash + row contents)`. Database triggers
reject `UPDATE` and `DELETE` on the table. `/audit/verify` recomputes the whole chain.

## Why it is built this way

- **One process with SQLite, no message broker.** This is a one-person lab tool, and keeping it simple made
  every failure easier to test.
- **Lanes instead of a pool.** There is one board and one model quota. Two jobs that both need the board must
  never run at the same time, while a build may run next to a test.
- **Workflows are code, and agents work inside stages.** The order of steps, the approvals and the rollbacks
  are fixed in the code. The model does what it is good at (writing, diagnosing) but cannot skip a gate.
- **The board decides.** A finding counts only after it is reproduced on the board, and a fix counts only
  after the retest and the regression suite pass on the board.
