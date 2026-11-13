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

### Builds from scratch (`build`, `joomla`)

1. **Plan first:** the model lists the files and what each does (saved as
   `drafts\smithy\NNNNN-plan.json`). If the plan comes back unreadable it's asked once more.
2. **Then each file in turn**, with the plan and the files written so far as context.
3. **README.md last:** what it is, what each file does, where it goes, how to use it.
4. Output: `drafts\smithy\NNNNN-<name>\`, laid out like the real project.
5. The spoken summary says what was built and how many files; "show me Smithy task N"
   opens the folder.
6. **A follow-up on a build makes a new version** in its own folder
   (`NNNNN-<name>-v2\`): unchanged files are copied, changed ones rewritten with the old
   version as context. The earlier version is left as it was.
7. **Paused for a game or stopped?** It carries on from the next unwritten file.

**Where files may go (checked in code, `smithy_jobs.safe_rel` / `inside`):** only inside
the build's own folder. A planned path must be relative, with no `..`, no drive letter,
no Windows device names (CON, NUL...), no hidden or private names (`.env*`, keys), at
most 8 folders deep, and a text file (no programs, archives or images). It is checked
again after resolving. A path that fails is left out and listed in the result as
"Left out (not allowed)". Builds get `max_build_minutes` (180) and at most
`max_build_files` (20) files, in `smithy.json`.

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
| `worker_shared.py`, `worker_queue.py`, `worker_backend.py` | Shared with Firestarter |
| `app\WorkerApp.cs`, `build-smithy.ps1` | The app (built with `/define:SMITHY`), icon, shortcuts |
| `state\smithy.db`, `state\smithy-status.json` | Its queue and heartbeat |
| `drafts\smithy\` | Results and build folders |
| `workers\Smithy.md` | Its work log (git-ignored) |
| `logs\smithy.log` | Log (older runs in `logs\old`) |

## Models: a small PC or a big one

- A small PC (this laptop, 16 GB RAM): the stand-in `qwen2.5-coder:3b` (proves the code works; its code is weak, and it
  often copies a file instead of reviewing it).
- A bigger PC (the new build: 12-core CPU, 128 GB RAM; The Smithy still runs on the CPU only): **Qwen3-Coder 30B-A3B** (Get models...), then in `smithy.json`:
  `"num_thread": 10`, `"ollama": {"start_own": true}` (its own CPU-only Ollama on port
  11436), `"context_tokens": 32768`; then
  `powershell -ExecutionPolicy Bypass -File build-smithy.ps1`.
- Ports: the Smithy's Ollama 11436, its llama-server 11438 (Firestarter: 11435 / 11437).
