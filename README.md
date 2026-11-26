# AEISP: Agentic Embedded Intelligence & Security Platform

AEISP is a lab assistant for microcontroller firmware. You describe what the firmware should do. AI agents
write the C code, build it, check it for security problems and measure its size. With your approval they
flash it to a real board. Then they test it, fuzz it, fix the bugs they find, profile it and make it smaller
or faster. Every step is recorded, so you can see later what was done, why, and by whom.

The board is a **Vicharak Shrike Lite (RP2040)** on USB. The agents use **Gemini** through **LangGraph**. The
backend is **FastAPI + SQLite** and the dashboard is **React**.

```
 you ──► dashboard / API ──► agents (Gemini) ──► tools ──► build (Pico SDK) ──► security scan ──► approval ──► flash
                                   ▲                                                                           │
                                   └──── test · fuzz · profile · knowledge base ◄───── the board (USB serial) ◄┘
```

## What it can do

| Area | What happens |
|---|---|
| Firmware | Projects with an `app/` folder (`app_init`, `app_tick`, `app_command`) on top of a fixed harness that speaks a checksummed line protocol. |
| Build | Reproducible Pico SDK builds with parsed compiler diagnostics, size reports and UF2 validation. |
| Security | A static scan before every flash: forbidden pins, `BOOTSEL` from app code, unbounded string functions, large stack arrays and more. |
| Deploy | Flashing happens only after an operator types a confirmation. The board is verified after every flash and rolled back automatically on failure. A known-good image is kept. |
| Agents | Eight agents (lab, supervisor, firmware, build, security, performance, verification, debugger) use 54 tools under role limits. |
| Pipelines | One requirement goes to a spec, then code, build, scan, deploy (approved), tests and a report. |
| Testing | Test suites on the real board with regression comparison and coverage. |
| Fuzzing | Seeded, safe protocol fuzzing with an oracle (crash, hang, garbled reply, out-of-range numbers…), reproduction and minimisation. |
| Remediation | Reproduce a finding, then find the root cause, fix, rebuild, re-flash (approved), retest, and roll back if the fix fails. |
| Profiling | On-board profiler, benchmarks, baselines and noise-aware comparisons. |
| Optimisation | Smaller or faster builds, accepted only when the tests still pass. |
| Knowledge | A searchable memory of past findings, fixes and decisions. Agents receive the relevant entries with each task. |
| Trust | A hash-chained audit log, verifiable task records, replay, and reports in Markdown, PDF or JSON. |
| Operations | A live dashboard, Prometheus `/metrics` and a deep health check. |

## Quick start

```powershell
cd backend
python -m pip install -r requirements.txt
python -m app.cli make-tokens          # writes the three role tokens into .env
python -m uvicorn app.main:app --port 8000
```

Then open http://127.0.0.1:8000/ui/ for the dashboard, or http://127.0.0.1:8000/docs for the API.

Full setup, including the Pico SDK, the board and the dashboard build, is in [docs/install.md](docs/install.md).

## Documentation

- [Install](docs/install.md): toolchain, Python, tokens, board, dashboard
- [Architecture](docs/architecture.md): how the pieces fit, the data model and the safety design
- [User manual](docs/user-manual.md): everyday tasks, CLI and dashboard
- [Security review](docs/security-review.md): threats to the platform itself and what was done about them
- [Acceptance checklist](docs/acceptance.md): what "done" means, checked by `tools/acceptance.py`

## Checking it

```powershell
cd backend
python -m pytest -q                    # the whole suite with a simulated board (no hardware, no model)
python -m pytest -q -m hardware        # the hardware tests, with the board plugged in
python ..\tools\acceptance.py           # the acceptance checklist
python ..\tools\demo.py                 # an end-to-end walk through a running backend
```

## Safety rules that cannot be switched off from the API

- Agents can propose a deployment but never approve one. Only the operator token approves, by typing the
  confirmation text.
- Agents cannot send `BOOTSEL`, `HANG` or `REBOOT`, and the fuzzer never generates them.
- A build that fails the security scan cannot be proposed for flashing.
- Every flash is verified. If verification fails, the previous image is put back.
- The audit log cannot be edited. `/audit/verify` checks the whole chain.
