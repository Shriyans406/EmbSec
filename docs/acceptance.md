# Acceptance checklist

This is what "AEISP works" means. Each item is checked automatically:

```powershell
cd <repo>
python tools\acceptance.py              # everything (runs the test suite once, a few minutes)
python tools\acceptance.py --quick      # only the installation checks (seconds)
python tools\acceptance.py --hardware   # also the tests on the real board (stop the backend server first)
```

Run it with the project's virtual environment. The checker never uses the model. It compares secrets with
the repository but never prints them. Each result is **PASS**, **WARN** (works, but something optional is
missing), **SKIP** (not checked in this mode) or **FAIL**.

| # | Item | Accepted when | Checked by |
|---|---|---|---|
| A1 | Installation | Python is 3.11 or newer and every backend package imports. | installation check |
| A2 | Configuration | `.env` has three different role tokens of at least 24 characters. `.env` is ignored by git and not tracked. Without a Gemini key the result is WARN. | check + `test_api_core` |
| A3 | No committed secrets | No tracked file contains a Google API key pattern, a token value from `.env` or the Gemini key. | `git grep` |
| A4 | Toolchain | The Pico SDK, ARM GCC and picotool are where `.env` says (WARN if not). | installation check |
| A5 | Board link | The protocol is parsed and the CRC is correct. Ids pair requests and replies. Reconnects and board discovery work. The harness profiler passes its host test. | `test_protocol`, `test_link_fake`, `test_discovery`, `test_harness_host` |
| A6 | Build and scan | Builds are recorded with diagnostics and sizes. The security scan flags forbidden pins, BOOTSEL and unsafe calls. | `test_analysis`, `test_api_services` |
| A7 | Safe flashing | Nothing is flashed without an operator typing the confirmation. Every flash is verified. Any failure, including a pulled cable or a driver error, is rolled back, or ends in `ROLLBACK_FAILED` with recovery instructions. | `test_api_services`, `test_failures` |
| A8 | Agents within limits | Unknown tools, wrong arguments, forbidden commands, path tricks and a failing gate are all refused. Agents cannot approve. Step and token budgets hold. A model outage ends in a report, not a hang. | `test_agents`, `test_team`, `test_failures` |
| A9 | Pipeline | A requirement becomes firmware through the spec, code, build, scan, approval, deploy, test and report stages, with fix rounds. | `test_pipeline` |
| A10 | Test framework | Suites, runs, regression comparison and coverage work. | `test_testfw` |
| A11 | Fuzzing | Campaigns are repeatable from their seed. Unsafe inputs are never sent. The oracle classifies results and findings are reproduced and minimised. | `test_fuzz` |
| A12 | Remediation and optimisation | A fix is accepted only after retest, regression and fuzz checks pass. Otherwise it is rolled back. An optimisation is kept only if it meets its gain and correctness gates. | `test_remediate`, `test_optimize` |
| A13 | Profiling | Benchmarks run. Baselines must be repeatable, and comparisons respect the noise margin. | `test_profiling` |
| A14 | Knowledge and records | Search, memory for agents, timelines, task records with verify, replay and rebuild, md/pdf/json reports, and the audit chain all work. | `test_knowledge`, `test_records` |
| A15 | Dashboard and operations | The dashboard is built and served. The live feed works with role-based hiding. Deep health and `/metrics` work. | check + `test_live`, `test_health` |
| A16 | Platform security | Tokens are kept out of logs. Security headers are sent. Token guessing is throttled. No route lets a viewer change anything. | `test_hardening`, `test_failures` |
| A17 | Documentation and release | Every document exists and its links resolve. The demo script works end to end. The version is 1.0.0 (phase 17). | check + `test_demo`, `test_acceptance` |
| H1 | On the real board (`--hardware`) | The hardware tests pass against the connected RP2040. | `pytest -m hardware` |

## Checked by hand

These need a person and are not automated:

1. **Approve one real deployment** from the dashboard's Approval centre. Watch the board reboot into the new
   firmware, and check that the job reports the build id and sources hash it verified.
2. **Pull the USB cable during a flash** (on a build you do not mind losing). The deployment must end in
   `ROLLED_BACK` or `ROLLBACK_FAILED`. Then `python -m app.deploy.cli recover` must bring the board back.
3. **Run one AI pipeline** with a Gemini key (`python -m app.pipeline.cli run "..."`). Read its report.
4. **Read a remediation diff** before approving it. The fix must touch only what the finding needs.

## Last result

Release 1.0.0 on the development PC, with the board connected and the backend running separately:
A1–A17 pass. A2 warns only if no Gemini key is set. H1 passed (9/9) in the last board session.
