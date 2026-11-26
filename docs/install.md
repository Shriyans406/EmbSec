# Installing AEISP

AEISP has been set up and tested on Windows 11 with PowerShell. The steps below assume the project is in a
folder of your choice, called `<repo>` here.

## 1. What you need

| Tool | Version | Used for |
|---|---|---|
| Python | 3.11 or newer (3.13 is tested) | the backend |
| Raspberry Pi Pico SDK for Windows | 1.5.1 (includes ARM GCC, CMake, Ninja, picotool) | building firmware |
| Node.js | an LTS release | building the dashboard (only needed if you change it) |
| Git | any | version control |
| MinGW `gcc` | optional | the host-side profiler unit test |
| Board | Vicharak Shrike Lite (RP2040), USB VID `2E8A` | running the firmware |
| Gemini API key | free tier works (about 20 requests a day) | the AI agents (optional) |

Everything except the agents works without a Gemini key: building, flashing, tests, fuzzing, profiling and the
dashboard.

## 2. Python environment

```powershell
python -m venv $HOME\.venvs\aeisp
& $HOME\.venvs\aeisp\Scripts\Activate.ps1
cd <repo>\backend
python -m pip install -r requirements.txt
```

## 3. Configuration (`.env`)

```powershell
cd <repo>
Copy-Item .env.example .env
```

Open `.env` and set these values:

- `PICO_SDK_PATH`, `PICO_TOOLCHAIN_PATH`, `PICOTOOL_PATH`: where the Pico SDK installer put them.
- `GEMINI_API_KEY`: from Google AI Studio. Leave it empty to run without agents.
- `GEMINI_MODEL`: `gemini-2.5-flash` by default.

Then create the three API tokens:

```powershell
cd <repo>\backend
python -m app.cli make-tokens
```

This writes `AEISP_OPERATOR_TOKEN`, `AEISP_AGENT_TOKEN` and `AEISP_VIEWER_TOKEN` into `.env`.
`python -m app.cli show-tokens` prints them again when you need one for the dashboard. `.env` is in
`.gitignore`: never commit it.

## 4. Check the setup

```powershell
cd <repo>
python tools\doctor.py
```

The doctor checks Python, the Pico SDK, ARM GCC, picotool, CMake, Ninja, the virtual environment, `.env`, the
Gemini key, Git, Node and the board. Fix whatever it marks as failing.

## 5. The board

1. Plug the board in over USB. It shows up as a COM port with VID `2E8A`.
2. Before anything is flashed, save the firmware the board came with. AEISP refuses to flash without a
   recovery image. Hold **BOOT** while plugging the board in, then run:
   ```powershell
   & $env:PICOTOOL_PATH save -a <repo>\backups\shrike_stock_backup.uf2
   ```
   (`PICOTOOL_PATH` is the path from `.env`. You can also type the full path to `picotool.exe`.) Unplug the
   board and plug it in again without BOOT.
3. Then put the harness firmware on it. Build a project (the harness plus a starter app), then
   flash it. The deploy command checks the build, asks you to type a confirmation, saves a backup of the
   factory image, flashes and verifies:
   ```powershell
   cd <repo>\backend
   python -m app.build.cli new demo
   python -m app.build.cli build demo          # prints the build number, e.g. build #1
   python -m app.deploy.cli deploy 1
   ```
   If the board is too stuck for that, hold **BOOT** while plugging it in. It appears as a USB drive
   called `RPI-RP2`. Then run `python -m app.deploy.cli deploy 1` again.
4. Check that the board answers, and keep the build as the image to fall back to:
   ```powershell
   python -m app.hardware.cli ports
   python -m app.hardware.cli hello
   python -m app.deploy.cli known-good promote 1
   python -m app.deploy.cli status
   ```

Wiring notes: the on-board LED is on GPIO4. **Never drive GPIO12 or GPIO13.** They are connected to other
parts of the board, and the security scan refuses firmware that uses them.

## 6. Start the backend

```powershell
cd <repo>\backend
python -m uvicorn app.main:app --port 8000
```

- Dashboard: http://127.0.0.1:8000/ui/ (paste the viewer or operator token when asked)
- API reference: http://127.0.0.1:8000/docs
- Health: http://127.0.0.1:8000/health, and `/system/health/deep` with a token

The backend listens on `127.0.0.1` only. Do not start it with `--host 0.0.0.0` on a shared network; see
[security-review.md](security-review.md).

## 7. The dashboard (only if you change it)

The built dashboard is served from `frontend/dist`. To rebuild it:

```powershell
cd <repo>\frontend
npm install
npm run build          # writes frontend/dist; the backend serves it at /ui/
npm run dev            # or: a development server on http://localhost:5173 with live reload
```

## 8. Check the installation

```powershell
cd <repo>\backend
python -m pytest -q                     # about 300 tests, simulated board, about 7 minutes
python -m pytest -q -m hardware         # 9 tests on the real board
cd <repo>
python tools\acceptance.py              # the acceptance checklist
python tools\demo.py                    # with the backend running
```

## Troubleshooting

| Problem | What to do |
|---|---|
| `no RP2040 found on USB` | Check the cable (some carry only power). Try another port. Run `python -m app.hardware.cli ports`. |
| The board is stuck and does not answer | Run `python -m app.deploy.cli recover`. If that fails too, hold BOOT while plugging in and run the same command again. `recover --stock` puts the factory image back. |
| `board_busy` | Another job or the dashboard terminal is using the board. Wait, or cancel the job. |
| `auth_not_configured` | Run `python -m app.cli make-tokens` and restart the backend. |
| The agents say `GEMINI_API_KEY is not set` | Put the key in `.env` and restart. |
| `429` from Gemini | The free daily quota is used up. Agents fail cleanly; try again tomorrow. |
| A build fails with toolchain errors | Run `python tools\doctor.py` and check the paths in `.env`. |
