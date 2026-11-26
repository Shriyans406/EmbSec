# User manual

Run every command below from the `backend` folder, with the virtual environment active. There are three ways
to use AEISP:

- **The dashboard** at http://127.0.0.1:8000/ui/, with the backend running. Best for watching and approving.
- **The API** at http://127.0.0.1:8000/docs. Every action is there. Send `Authorization: Bearer <token>`.
- **The command-line tools** (`python -m app.<area>.cli`). These work without the server. **Stop the server
  first** when the command needs the board, because only one program can open the COM port. When a CLI asks
  for a confirmation text, you are the approver.

## Roles

| Token | Can do |
|---|---|
| viewer | Read everything: the dashboard, records and reports. |
| agent | Also edit firmware sources, build, run tests, fuzz, benchmark, start agents and propose deployments. |
| operator | Also approve or reject proposals, recover the board, set baselines and promote known-good. |

The agents always use the agent token, so a model can never approve its own deployment.

## The dashboard

| Page | What it is for |
|---|---|
| Overview | Board state, running jobs, recent findings and health checks. |
| Device | The board's status, plus a terminal for safe protocol commands. |
| Agents | Agent runs step by step, team trees and token use. |
| Firmware | Projects, the source editor, builds, diagnostics and sizes. |
| Pipelines | Requirement-to-firmware runs, stage by stage. |
| Approval centre | Proposals waiting for you. Each shows its gate checks and needs the confirmation text typed in. |
| Tests | Test suites, runs, regressions and coverage. |
| Fuzzing | Campaigns and findings (verdict, CWE, minimised input), with reproduce. |
| Remediation | Bug fixes from findings, with the diff, retest and rollback. |
| Profiling | Benchmarks, runs, baselines and comparisons. |
| Optimisation | Speed, flash or RAM improvements with A/B measurements. |
| Knowledge | Search the lab memory, project timelines and board history. |
| Reports | Task records: verify, replay, download as md, pdf or json. |
| Jobs | Every job, its live log, and the cancel button. |
| Audit log | The audit log and the result of the chain check (operator only). |

## Everyday tasks

### Is everything all right?

```powershell
python ..\tools\doctor.py                  # the tools and the board
python -m app.deploy.cli status            # what is on the board, and what can be restored
python -m app.hardware.cli hello           # the board's identity and firmware version
```

With the server running, `GET /system/health/deep` shows the same checks.

### Write and build firmware yourself

```powershell
python -m app.build.cli new thermo                  # a project with a starter app in firmware\workspace\thermo\app
# edit firmware\workspace\thermo\app\app.c  (app_init, app_tick, app_command)
python -m app.build.cli build thermo
python -m app.build.cli show 15 --log               # diagnostics and size of build #15
python -m app.deploy.cli deploy 15 --dry-run        # the pre-flight gate only
python -m app.deploy.cli deploy 15                  # flash it: type the confirmation text
```

### Let the AI do it from a requirement

```powershell
python -m app.pipeline.cli run "APP ADD a b replies with the sum of two 32-bit integers"
```

The pipeline goes through these stages: spec, code, build, security scan, and fixes if needed (up to 3
rounds). Then it stops and asks you to approve the flash. After that it tests the firmware on the board and
writes a report. `--no-deploy` stops before the board. Each pipeline uses several Gemini requests. On the free
tier, run about two a day.

### Ask an agent something

```powershell
python -m app.ai.cli run "Read the board's temperature three times and tell me the average"
python -m app.ai.cli team "add a TEMP_F command that replies in Fahrenheit"   # the supervisor uses specialists
python -m app.ai.cli show 3
```

### Test

From the dashboard's Tests page, or:

```powershell
python -m pytest -q -m hardware            # the platform's own hardware tests
```

With the server running, `POST /tests/run {"suite": "smoke"}` runs a suite. A project suite such as
`spec-calc2` is created by its pipeline. `/tests/suites/<name>/regression` compares the latest build with the
one before.

### Fuzz

On the Fuzzing page, start a campaign: choose the families, the number of inputs and a seed. The same seed gives
the same inputs. Through the API this is `POST /fuzz/campaigns`. Each finding has a verdict, such as crash,
hang, garbled reply or out-of-range. It also has a severity, a CWE and a minimised input. **Reproduce** runs
the input again to confirm the finding.

### Fix a finding

```powershell
python -m app.remediate.cli findings
python -m app.remediate.cli fix 7
```

The remediation reproduces the bug and measures the current firmware. The debugger agent finds the root cause
and writes a fix, which is then built and scanned. Then it asks you to approve the flash. After that it
retests, runs the regression suite and a short fuzz run. If the fix made things worse, it asks you to approve a
rollback. Before you approve, read the diff in the approval dialog. A fix should change only what the finding
needs.

### Measure and optimise

```powershell
python -m app.optimize.cli run calc2 --goal speed     # or flash / ram
```

The benchmarks run on the board. `POST /benchmarks/<name>/baseline` (operator) saves a baseline. It is
accepted only when repeated runs agree. Comparisons take run-to-run noise into account. An optimisation is
kept only if it reaches the minimum gain and all the tests still pass.

### Look things up

```powershell
python -m app.knowledge.cli collect                    # take in the latest records
python -m app.knowledge.cli search "watchdog reset"
python -m app.knowledge.cli timeline calc2
```

Agents are given the matching entries automatically. You can add decisions and notes on the Knowledge page.

### Reports and proof

On the Reports page, pick a task (a pipeline, remediation, optimisation, fuzz campaign or agent run):

- **Verify** checks that the record still matches the database and the files.
- **Replay** shows the steps again without touching the board or the model.
- **Report** downloads it as Markdown, PDF or JSON.

`GET /audit/verify` (operator) checks the audit chain.

## When something goes wrong

| Situation | What to do |
|---|---|
| A flash failed | Usually nothing: it was rolled back. The job result says `ROLLED_BACK` and what failed. |
| `ROLLBACK_FAILED` | Unplug the board. Hold BOOT while plugging it in. Run `python -m app.deploy.cli recover`. |
| The board does not answer | `python -m app.hardware.cli ping`. Then `python -m app.deploy.cli recover`. The watchdog resets a hung app within 3 s. |
| An approval disappeared | Proposals expire after 15 minutes. Start the task again or resume the workflow (`/…/resume`). |
| An agent ran out of steps or tokens | Read its report (`ai.cli show <id>`). Start it again with a narrower goal or `--max-steps`. |
| `429` from Gemini | The daily free quota is used up. Runs end with a "model failed" report. Try again the next day. |
| `429 too_many_failures` from AEISP | A wrong token was used too often. Wait a minute and check the token. |

## Rules to remember

- Never approve a proposal you have not read. The confirmation text must be typed in on purpose.
- Never drive GPIO12 or GPIO13 from app code. The scan blocks it.
- Keep `.env` private. If a token leaks, delete its line from `.env`, run `make-tokens` and restart.
