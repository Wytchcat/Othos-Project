# The Smithy: the Othos coding worker

The Smithy does Othos's **coding** in the background, on the **CPU only**: reviews,
fixes, scripts, Joomla/YOOtheme overrides, Othos's own code, and **builds from
scratch**. Fully separate from Firestarter (site work, `WORKER.md`): its own app,
queue, drafts, settings and work log. They share one safety module and take turns on
the CPU when both have work. Built Nov 12, 2026.

**Everything it makes is a draft.** Nothing is applied, run or installed; you look at
it and copy what you want into place yourself, on screen.

## Using it

- **Start:** the **The Smithy** shortcut (Desktop / Start menu; pin it; hammer on an
  anvil, steel blue). Opening it starts the worker; its window shows what it's doing,
  the **model switcher** (Coding model), **Get models...**, **Work log** and **Code chat**
  (Othos's typed code chat, `http://127.0.0.1:3000/code`, in a private browser window; it
  needs Othos running, so until then the button says "Code chat (start Othos first)").
- **Stop:** right-click its taskbar button -> **Close window** (or the X). A running
  task goes back in the queue; a build carries on from the file it had reached.
- **Independent** of Othos and of Firestarter.
- **Command line** (from this folder): `.venv\Scripts\python.exe smithy.py`
  `list | status | types | log | cancel [ID] | models | set-model code NAME`
  `| add review PATH | add fix PATH WHAT | add script WHAT | add build WHAT`
  `| add joomla WHAT [--file PATH] | add followup ID QUESTION`

## Task types

| Type | What | Result |
|---|---|---|
| `review` | Review a code file: bugs, then risks, then small improvements | `NNNNN-review.md` |
| `fix` | Draft a fix for a file, as a diff (never applied) | `.md` + `NNNNN-fix.diff` |
| `script` | Write a script (never run) | `.md` + `NNNNN-script.ps1` (etc.) |
| `joomla` | A Joomla 5 / YOOtheme Pro override or CSS, built like a small project (optionally based on an existing layout file, `--file`) | its own folder |
| `build` | A project from scratch | its own folder |
| `followup` | A question about an earlier result; **on a build, a new version of it** | `.md`, or a new folder |

## What voice Othos may do (`workers_voice.py`, recognised in code)

Coding requests go to the Smithy automatically. Task numbers are the Smithy's own
("Smithy task 3"); plain "task 3" works when only one worker has a task 3.

- **Start** (only while the Smithy is running): "ask the Smithy to review gaming dot py",
  "start a task: fix worker underscore queue dot py to close the connection",
  "have the Smithy write a PowerShell script to back up my drafts",
  "ask the Smithy to build a small to-do list web page",
  "ask the Smithy to make a Joomla override for the article layout that shows the author photo".
- **Stop:** "stop the Smithy task" / "stop Smithy task 3".
- **Progress and lists:** "how's Smithy task 3 coming?", "what's in the queue?", "what's done?".
- **Read:** "read me Smithy task 3", then "read the rest" / "go on" (code described, not spelled out).
- **Show:** "show me Smithy task 3" (a build opens its folder; otherwise Notepad++).
- **Follow-up / new version:** "ask about Smithy task 3: also count characters".
- **Keep / discard:** "keep Smithy task 3" -> "Say yes or no." Only a clear yes marks it.
- **Work log:** "read me the Smithy log" (latest three, "go on" for more), "show me the Smithy log".
- **"It's ready":** "Smithy task 3, the build: small to-do list web page, is ready." between turns.

## Which files it may read (`worker_files.py`, enforced in code)

- Only inside `code_folders` in `smithy.json` (Othos-agent and Othos-ui), code or text
  files up to 120 KB.
- Never: `.env*`, `brain\`, `profile.md`, `logs\`, `state\`, `drafts\`, `workers\`,
  `.git`, `.venv`, `node_modules`, `.next`, databases, key/certificate files.
- Everything sent to the model passes the shared redaction filter first.

## Taking turns with Firestarter

When both have a task, one waits until the other's task is done (a build counts as one
task), then the one that just had a turn lets the other go first. Never side by side.
Alone, the Smithy gets all its threads (`num_thread`). Games: paused for cpu-heavy, held
back (fewer threads, low priority) for gpu-heavy / light, as Firestarter.

## Files

| File | What |
|---|---|
| `smithy.json` | Settings: back end, model, threads, games, build limits, code folders |
| `smithy.py` | The Smithy (its command line, on the shared engine `worker_engine.py`) |
| `smithy_jobs.py` | Its task types, prompts and the build steps; where build files may go |
| `worker_files.py` | Which files it may read |
| `worker_cloud.py` | Other model providers by API key: off until turned on, with the safeguards (shared) |
| `worker_shared.py`, `worker_queue.py`, `worker_backend.py` | Shared with Firestarter |
| `app\WorkerApp.cs`, `build-smithy.ps1` | The app (built with `/define:SMITHY`), icon, shortcuts |
| `state\smithy.db`, `state\smithy-status.json` | Its queue and heartbeat |
| `drafts\smithy\` | Results and build folders |
| `workers\Smithy.md` | Its work log (git-ignored) |
| `logs\smithy.log` | Log (older runs in `logs\old`) |

## Other model providers, by API key (`worker_cloud.py`, shared with Firestarter)

**Off until you turn one on.** This is an exception to Othos's "only Deepgram, Rime and
Tavily get data" rule, approved Nov 12 with these safeguards, all checked in code.

**Local first.**
- **lmstudio:** LM Studio's server on this PC. Nothing leaves the PC; the address must
  be 127.0.0.1.
- **Ollama and llama.cpp:** the Smithy's own back ends (above).

**Cloud:**
- **anthropic:** Claude (an API key, billed separately from a Claude subscription).
- **openrouter:** one key for many models.
- **deepseek, mistral (Codestral), nvidia, gemini, groq, cerebras, together, fireworks,
  openai, huggingface.**
- **freellmapi:** [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi), a program you
  run yourself (default `127.0.0.1:3001`) that pools many providers' free tiers behind one
  address.
  - **Treated as cloud:** it runs on this PC but passes everything on to free cloud tiers,
    which may train on what's sent. So it needs `cloud.accept_data_use`.
  - **Setting it up:** its key comes from its own dashboard (`FREELLMAPI_KEY` in `.env`), and
    the model goes in `cloud.models.freellmapi`.
  - Its authors call it for personal experimentation.

xAI isn't offered.

**To turn one on:**
1. Put its key in `.env`, for example `DEEPSEEK_API_KEY=...`. Keys never go in
   `smithy.json`. The key names are in `_cloud` in `smithy.json`.
2. Add it to `cloud.enabled` in `smithy.json`, for example `"enabled": ["deepseek"]`.
3. Set its model under `cloud.models` if it has no default. The defaults are Claude Opus
   5.5, deepseek-chat and codestral-latest.

**Using one:**
- **In the window:** the **Provider** row under Coding model. It offers "Local (this PC)"
  and only the providers already turned on. Picking one makes it the default for the next
  tasks (`cloud.default`; `smithy.py set-provider NAME|local`). The line below shows "Cloud
  today: N tokens, $X" (`smithy.py cloud`), and turns gold while a cloud provider is selected.
- **By voice:** "ask the Smithy, using DeepSeek, to review gaming dot py". It also works with
  "via" or "through".
- **On the command line:** `smithy.py add review PATH --provider deepseek [--model NAME]`.
- **Every task:** set `cloud.default`. Otherwise every task uses the local model.

**Safeguards:**
- **Keys:** read from `.env` only, sent only to that provider's fixed address (written in
  the code), and never logged. Errors have the key blanked out.
- **What's sent:** everything passes the redaction filter again, on top of each task's own.
  It's only what the task would send to the local model (files the Smithy may read).
- **Free tiers that may train on what you send:** Gemini, and OpenRouter's `:free` models.
  They're refused unless listed in `cloud.accept_data_use`.
- **Limits:**
  - `max_tokens_per_task` (200,000) and `max_tokens_per_day` (1,000,000).
  - With `prices` set (dollars per million tokens, `[input, output]` per provider),
    `max_usd_per_day` ($2) applies.
  - Today's use is in `state\cloud-usage.json`.
- **Claude** uses Anthropic's own SDK. If Claude declines a request, its server-side fallback
  model takes over (fallbacks "default").

Firestarter can use the same providers, with its own `cloud` section in `worker.json`.
It has none, so it's off.

## Models: a small PC or a big one

- A small PC (this laptop, 16 GB RAM): the stand-in `qwen2.5-coder:3b` (proves the code works; its code is weak, and it
  often copies a file instead of reviewing it).
- A bigger PC (the new build: 12-core CPU, 128 GB RAM; The Smithy still runs on the CPU only): **Qwen3-Coder 30B-A3B** (Get models...), then in `smithy.json`:
  `"num_thread": 10`, `"ollama": {"start_own": true}` (its own CPU-only Ollama on port
  11436), `"context_tokens": 32768`; then
  `powershell -ExecutionPolicy Bypass -File build-smithy.ps1`.
- Ports: the Smithy's Ollama 11436, its llama-server 11438 (Firestarter: 11435 / 11437).
