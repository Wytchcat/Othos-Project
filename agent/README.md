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
| `sources.py` | free sources tried before Tavily (weather, RSS, Stack Exchange, Wikipedia...) |
| `personal.py` | reads `personal.json` (your name, town, local feeds); `personal.example.json` shows the fields |
| `gaming.py`, `gaming.json` | gaming mode |
| `smithy.py`, `smithy_jobs.py`, `smithy.json`, `SMITHY.md` | The Smithy, the coding worker |
| `worker_*.py`, `model_fetch.py`, `app\WorkerApp.cs` | the worker engine The Smithy runs on |
| `workers_voice.py` | the workers by voice ("ask the Smithy to...") |
| `app\Othos.cs`, `build-othos-app.ps1` | the Othos taskbar app |
| `start-othos.ps1/.bat`, `stop-othos.ps1/.bat`, `restart-agent.ps1`, `othos-tray.ps1` | starting and stopping |
| `setup.ps1`, `doctor.ps1` | set-up and checks |
| `requirements.txt`, `update-requirements.ps1`, `githooks\` | pinned packages and the check that keeps them pinned |
| `pronunciations.json` | how the voice says tech words |

## Running

- Start: `start-othos.bat`, or the Othos app (`build-othos-app.ps1`).
- After changing `agent.py`: `restart-agent.ps1` restarts only the agent. It refuses while
  a call is open.
- Logs: `logs\agent.log`, `server.log`, `ui.log`. Earlier runs are in `logs\old\`.

Licence: GNU AGPL v3.0 or later (the `LICENSE` file at the top of the shared copy).

Never committed: `.env`, `personal.json`, `profile.md`, `brain\` (notes), `logs\`,
`drafts\`, `state\`, `workers\`.
