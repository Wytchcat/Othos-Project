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
- **Progress** (next to Work log): a small window, in The Smithy's steel blue, that opens in the
  bottom-right corner and stays on top (untick **Keep on top** to let it go behind). It shows the
  running task with its progress bar and time, and below that **each step as it happens**, with
  the time: "Started task 20 (followup). Using switching: Cerebras first, then ...", "Planning the new
  version.", "Plan ready: 3 files: ...", "Editing OthosSetup.cs: asking for the changes as edits.",
  "OthosSetup.cs: 10 edits in; every other line kept.", "Cerebras asked to slow down: waiting 60
  seconds.", "Now on Groq (...)", "Task 20 is done, in 2 minutes 12 seconds." Waits and problems
  are gold; the start and the end are blue. Opened part way, it shows the last 40 steps first.
  The steps are in `state\smithy-status-steps.jsonl` (the last 400 lines are kept), written by
  the worker (`worker_engine.StepFile`); the window only reads them. Firestarter's app has the
  same window, in its own colours.
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
- **Task Mode** (the user's idea): Othos reads each new step of a task aloud as it happens,
  between turns, from the same steps file as the Progress window: "The Smithy: Planning the new
  version. Plan ready: 3 files ..."
  - **Turning it on:** "open / go into / assume / switch to / start / enter task mode", "task mode
    on", "put yourself in task mode". With no task named, Othos asks "Do you want a particular
    task or the current one?":
    - "the current one" (or "yes"): the task running now; with none running, the next one to start.
    - a number ("21", "Smithy task 21"), or "a particular one" and then the number when asked.
    - "never mind" / "no": it stays off. Anything else: the question lapses.
  - Or name it at once: "switch to task mode for Smithy task 21". A task still queued is read
    when it starts; one already finished is offered to read instead.
  - It follows only that task, and turns itself off when the task ends ("That's the end of Smithy
    task 21, so task mode is off."); a failure that will be tried again keeps it on.
  - A burst of more than four steps is said as the count and the latest three. "Task mode off"
    stops it; the "is ready" announcement carries on either way. It lasts for the call.
  - Steps say what the engine is doing (task type, step, file name, provider, wait), never a
    task's text or the model's output, and each line is redacted before it's written
    (Firestarter's briefs and ZOO rows can hold product details).
  - While a how-to is recording, steps aren't read over the narration: they wait until it's
    paused or finished.

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
- **cloudflare:** Workers AI. It needs an account API token (`CLOUDFLARE_API_TOKEN`) and the
  account ID (`CLOUDFLARE_ACCOUNT_ID`, 32 letters and digits) from the dashboard; the address is
  always `api.cloudflare.com`. Default model `@cf/openai/gpt-oss-120b`.
- **vercel:** Vercel's AI Gateway (`ai-gateway.vercel.sh`), one key for many providers' models
  (`AI_GATEWAY_API_KEY`, or `VERCEL_API_KEY`). Free tier: $5 every 30 days on its free-tier models,
  but only once a card is on file. Counted as "may train" (the providers behind it decide).
  Default model `openai/gpt-oss-120b`.

xAI isn't offered.

****Most free first (Nov 13).** The Providers window lists the cloud providers by how much they
give away, most to least, then the paid-only ones (`worker_cloud.py` FREE_TIERS: each figure
from the provider's own page, with the date it was checked). Each row has a **Free:** line
(click it for that provider's limits page):
- **Live figures win** when there are some: the limits Cerebras, Groq and Mistral report on every
  reply (kept in `state\free-limits.json`), OpenRouter's free requests used today, and whether
  Hugging Face is PRO. A figure older than a month with nothing live says "worth re-checking".
- **Re-checked** each time you open **Get models**, and by **Refresh**: only the providers' free
  account pages are read (OpenRouter, Hugging Face, Cloudflare), only for providers that are On;
  no test request is sent
  (`smithy.py free-limits`).
- **Keys:** "key ends <date>" next to the name for keys that report one (Cloudflare; OpenRouter
  and Hugging Face when the key has one), gold within 30 days. "refused" next to a key the
  provider turned down (for example OpenRouter's "User not found": make a new key).
- **Sort by free allowance** (bottom row) fills the switch order the same way: providers that
  are On, ready, give something free and whose key wasn't refused (`smithy.py switch-sort`).
  Change any number by hand afterwards.
- Checked on Nov 13: Mistral about 1 billion tokens a month (may train); Cerebras 1M tokens and
  2,400 requests a day (seen live); Cloudflare 10,000 neurons a day (about 300K tokens in);
  Vercel $5 every 30 days (card on file needed); Groq about 1,000 requests and 200K tokens a day; NVIDIA 40 a minute (testing only); Gemini a
  small number per project; OpenRouter 50 requests a day; Hugging Face nothing on a free
  account (PRO: $2 a month). DeepSeek, Together, Fireworks, OpenAI and Claude are paid only.

To turn one on: the Providers… window** (button in The Smithy's window; Firestarter
has the same one). Local first ("On this PC"), then "Cloud". Each provider's row has:
- **On:** a tick box. A free tier that may train on what's sent has a yellow note and
  asks Yes/No first.
- **Key:** a hidden box, then **Save key** (or **Remove**). Only the last 4 characters are
  ever shown.
- **Get a key:** opens the provider's own key page.
- **Model:** the box left of Test (it shows "model name" when empty). Filled in where there's
  a default; type any model the provider serves, then press Enter (or leave the box).
  Defaults, checked against each provider's live model list on Nov 13 2026: Hugging Face and
  Groq `openai/gpt-oss-120b`, Cerebras `gpt-oss-120b`, Gemini `gemini-flash-latest`, OpenRouter
  `cohere/north-mini-code:free` (free models come and go; type another if it's retired).
- **Not ready:** a provider that's On but can't be used yet says why, in yellow, top right
  (no key, no model, data-use not allowed). Only ready ones appear in the Provider list.
- **Test:** asks first, then sends one tiny "Reply with OK" request, nothing of yours.

The daily limits (tokens, dollars) are at the bottom, with **Refresh**: it reads `.env` and the
saved keys again, so a key you've just added to `.env` shows up without closing anything (each
task reads the keys fresh anyway; the worker never needs a restart for a new key).

**Where keys are kept:** encrypted by Windows for your account (DPAPI), in
`%LOCALAPPDATA%\Othos\keys.dat`, outside the Othos folder (`worker_keys.py`). They never go
into git or a shared copy.

**The advanced way:**
- A key in `.env` (for example `DEEPSEEK_API_KEY=...`) also works. Common other names are
  read too: `HUGGINGFACE_API_KEY`, `HUGGINGFACEHUB_API_TOKEN` or `HF_API_KEY` for
  Hugging Face (`HF_TOKEN`), `GOOGLE_API_KEY` for Gemini (`GEMINI_API_KEY`), and
  `CEREBRAS_AI_KEY` or `CEREBRAS_KEY` for Cerebras (`CEREBRAS_API_KEY`). The window
  shows such a key as "(.env)". A saved key wins.
- Settings by hand: `cloud.enabled`, `cloud.models` and `cloud.accept_data_use` in
  `smithy.json`.
- Command line: `smithy.py providers | provider-set NAME on|off model=M accept=yes|no |
  set-key NAME` (key on stdin) `| del-key NAME | test-provider NAME | set-limits TOKENS USD`.

**Switching as free tokens run out (Nov 13).** Several providers can serve the same model
(Groq and Hugging Face both run `openai/gpt-oss-120b`). The Smithy uses them one after another:
- **Set the order:** in Providers…, each cloud row has **Switch order** (1, 2, 3…; "-" leaves
  it out). Saved as `cloud.switch_order`.
- **Use it:** pick **Switching: …, then this PC** in the Smithy's Provider list (`cloud.default`
  "auto"), or say "ask the Smithy, using the free providers, to …". A task for a provider that's
  in the order also switches onward from it.
- **What happens:** each step of a job goes to the first provider that still has free tokens.
  When one says it's out (no credit, or over its limit), can't be reached, or can't take the
  request, the same step goes to the next one. The job carries on from where it was, since
  every step carries the whole job so far.
- **When every provider is out: it pauses and asks you** (chosen Nov 13). The task waits where it
  is, nothing lost, and the window says "Task N is waiting for free tokens (carries on about
  3:20 PM)".
  - **The Smithy's window** asks once, Yes/No: **Yes** finishes it on this PC's model now (slower;
    nothing more is sent out); **No** waits.
  - **Othos** asks too, between turns: "Should I finish it on this PC now? Or say wait…".
    Answer yes / "finish it on this PC", or no / "wait". "Finish task 3 on this PC" works at
    any time while it waits.
  - **No answer, or wait:** it carries on by itself 5 minutes after the first provider's free
    tokens are back, going round the order again.
  - Waiting doesn't count against the job's time limit, and the task gives up its turn on the
    CPU meanwhile, so Firestarter isn't held up. Cancelling the task still stops it.
  - By voice, only a clear yes or your own "finish it on this PC" (with no "no" in it) means
    this PC; anything else, or no answer, leaves it waiting.
  - `smithy.py out-choice TASK local|wait` answers from the command line. To skip the
    question and always finish on this PC, set `cloud.when_all_out` to `"local"` (default `"ask"`).
  - "Slow down" with a wait of a minute or less: it waits, then tries the same provider again.
  - A provider that ran out is skipped for a while (an hour, or as long as it asks; a day for
    "no credit"); the window shows "Out of free tokens for now".
  - A provider reaching your daily token limit is skipped until midnight, like one that ran out.
  Your dollar limit (all providers together) or the per-task limit going over sends the job
  straight to this PC's model.
- **Recorded:** the result's first line says which providers did the work ("providers used:
  Groq (openai/gpt-oss-120b), then Hugging Face (…)"); `logs\smithy.log` has each switch.
- **Each key in one place:** a free allowance belongs to the account, not the program, so a
  key used here shouldn't also sit in another program that draws on it. That's why FreeLLMAPI
  (which did the same switching outside the Smithy) was removed on Nov 13.
- **Safeguards still apply to every provider in turn** (On, key, data-use Yes/No, limits,
  redaction). A provider in the order that's Off is skipped; with none ready, a job just uses
  this PC's model and nothing is sent.

**New versions edit long files instead of rewriting them.** In a new version of a build, an
earlier file of `edit_min_chars` (4,000) characters or more is shown to the model whole, and the
model sends back only find/replace pairs (`<<<<<<< FIND` ... `=======` ... `>>>>>>> REPLACE`).
The Smithy applies them itself:
- Each FIND has to be in the file exactly once (exactly, or line by line ignoring spaces at the
  ends of lines). One that's missing or in the file more than once is skipped, never guessed;
  the model is asked once more for just those, with the file as it is now. When a FIND is in
  the file twice, the reason names each line and the line above it, so the next try can pick the
  right one. A FIND with fewer than 3 letters or digits (a bare ``` or `}`) is refused as too
  vague.
- In brace languages (C#, JavaScript, PHP, PowerShell, CSS...), REPLACE lines that come back at
  the left margin get the indentation of the line they replace. `#if` lines stay at the margin.
- Every other line stays exactly as it was. The result lists how many edits went in and any
  that didn't ("Edited, not rewritten").
- The README too: an earlier README that long is edited to describe the new version (the model
  sees this version's files, up to the build's budget). If no usable edits come back, it's
  written whole as before.
- Shorter files and new files are still written whole. `"edit_min_chars": 0` turns edits off.
- Why: installer versions 7, 8 and 9 (about 1,000 lines each) came back rewritten, losing
  earlier fixes; version 9 was given 11 exact edits and still dropped whole methods (Nov 13).

**Builds on the cloud read more.** A build on a cloud provider or the switch order is shown up to
`cloud_build_chars` (64,000) characters of the files so far and of the earlier version, instead of
the local model's `context_tokens` x 1.5. If the switch order ends on this PC, the local model
reads only what fits in its own context. **A step too long for this PC's model** (more than
`context_tokens` less 512) never goes to it, because Ollama would drop the start of it without a word.
When every provider is out, the task keeps waiting for free tokens, even if "this PC" is picked, and
the step line says why. A task on this PC's model alone fails with that reason. (Nov 14.)

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
  - `max_tokens_per_task` (200,000) counts **the whole task**: every step, fix round and provider
    (until Nov 14 it counted each call on its own). Each result's header line says how many cloud
    tokens the task used. Over it, the task goes to this PC's model if the step fits, else it stops
    and says why.
  - `max_tokens_per_day` (1,000,000). The day limit counts
    **each provider on its own**: when one reaches it, switching moves on to the next, and that
    one is skipped until midnight. (Until Nov 13 it was one total for all providers, which
    stopped switching while Cerebras still had tokens left.)
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
