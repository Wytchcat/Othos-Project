# Othos agent

The voice agent half of Othos: Python 3.12, LiveKit Agents 1.8, Ollama, plus The Smithy
(coding worker). The call page is the `ui` folder (`Othos-ui`) next to this one.

The full guide is the top-level `README.md` of the shared copy (kept here as
`public-root\README.md`). It covers set-up, every `.env` line, `personal.json`, the
mixed and offline modes, the voice commands, privacy, hardware and troubleshooting.

## In this folder

| File | What it is |
|---|---|
| `agent.py` | the voice agent: conversation, tools chosen in code, news, weather, search, notes, camera |
| `sources.py` | free sources tried before Tavily (weather, RSS, Stack Exchange, Wikipedia, OpenStreetMap places near your town, Brave Search, quakes, air quality and smoke, built-in gardening, recipe, book and movie feeds...) |
| `download_scan.py` | virus scan for downloads: Microsoft Defender or ClamAV scan each file before it is used; a hit is set aside; models must be real GGUF files. If Defender is off you pick your antivirus from a list (`personal.json` "antivirus") |
| `suggested-sources.json` | free web APIs for gardening, books, recipes and movies/TV that you can add yourself (suggestions only; read their terms first) |
| `personal.py` | reads `personal.json` (your name, town, local feeds); `personal.example.json` shows the fields |
| `gaming.py`, `gaming.json` | gaming mode |
| `smithy.py`, `smithy_jobs.py`, `smithy.json`, `SMITHY.md` | The Smithy, the coding worker |
| `worker_*.py`, `model_fetch.py`, `app\WorkerApp.cs` | the worker engine The Smithy runs on |
| `workers_voice.py` | the workers by voice ("ask the Smithy to...") |
| `app\Othos.cs`, `build-othos-app.ps1` | the Othos taskbar app |
| `system_specs.py` | the PC check ("Check my PC..."): this PC's specs and the settings that fit it |
| `model_check.py`, `models-tested.json` | the weekly model check ("Check models..."): new or updated models for voice Othos and The Smithy, and whether this PC can run them. Its window has a Get button (a Yes/No first) for each model this PC can run. `models-tested.json` lists the models tested with this version |
| `start-othos.ps1/.bat`, `stop-othos.ps1/.bat`, `restart-agent.ps1`, `othos-tray.ps1` | starting and stopping |
| `setup.ps1`, `doctor.ps1` | set-up and checks |
| `requirements.txt`, `update-requirements.ps1`, `githooks\` | pinned packages and the check that keeps them pinned |
| `pronunciations.json` | how the voice says tech words |

## Running

- Start: `start-othos.bat`, or the Othos app (`build-othos-app.ps1`).
- After changing `agent.py`: `restart-agent.ps1` restarts only the agent. It refuses while
  a call is open.
- Logs: `logs\agent.log`, `server.log`, `ui.log`. Earlier runs are in `logs\old\`.

## Two people on one computer

Each Windows account has its own Othos folder, settings, drafts and notes. Its keys are in
`%LOCALAPPDATA%\Othos\keys.dat`, locked to that account by Windows (DPAPI), so one account can't read
another's. Ollama and its models are per account too (`%LOCALAPPDATA%\Programs\Ollama`,
`%USERPROFILE%\.ollama\models`), and so are npm tools (`%APPDATA%\npm`) and shortcuts.

The whole computer shares:
- **Local addresses (ports):** LiveKit 7880, the agent 8081, the call page 3000 and Ollama 11434. While one
  account's Othos is running, another account's either uses what is already there (with the first account's
  models and data) or can't start its own.
- **The microphone and speakers:** two calls would hear each other.
- **Windows Sandbox:** only one can run at a time.
- **The CPU, memory and graphics card.**

**Best practice:** when you switch users, fully sign out: **Start**, your account picture, then **Sign out**.
Don't use **Switch user**, and don't just lock the screen. Signing out closes Othos, its Ollama and anything else
it started, so the next person starts clean. With Switch user, the first account's programs keep running in the
background. The Othos app's **Stop** (or closing its window) also stops everything it started.

Licence: GNU AGPL v3.0 or later (the `LICENSE` file at the top of the shared copy).

Never committed: `.env`, `personal.json`, `profile.md`, `brain\` (notes), `logs\`,
`drafts\`, `state\`, `workers\`.
