# Othos

*Othos Project V1*

Othos is a voice companion that runs on your own Windows PC. You talk to it in a
browser call page, and it answers out loud. The thinking happens on your computer,
using local models through [Ollama](https://ollama.com). It can:

- chat, with a dry, friendly wit;
- remember notes you ask it to keep, and find them again later;
- look through your webcam when you ask it to;
- read the news, give the weather and search the web, trying free sources first;
- hand coding jobs to **The Smithy**, a background coding worker that drafts code and
  never applies or runs anything.

Speech can be **mixed** (cloud speech-to-text and voice, everything else local; works on
a CPU-only laptop) or **fully offline** (needs a graphics card).

Windows only for now. macOS and Linux may come later.

```
Othos\
  agent\        the voice agent (Python 3.12, LiveKit Agents, Ollama) and The Smithy
  ui\           the call page (Next.js) with the Circuit Board visualizer and code chat
  setup.ps1     one-step setup (runs agent\setup.ps1)
  doctor.ps1    checks the setup and says what's missing (changes nothing)
  start-othos.bat
```

## Quick start

1. Install these first:
   - [Python 3.12](https://www.python.org/downloads/) (3.12.x exactly: livekit-agents 1.8
     doesn't support 3.13 or later). Tick "py launcher" during setup.
   - [Node.js LTS](https://nodejs.org/).
   - [Ollama](https://ollama.com/download).
   - [Git](https://git-scm.com/download/win) (optional, for updates).
   - Prefer a window? `installer\` has the source for an optional `OthosSetup.exe`
     (see `installer\README.md`). `setup.ps1` below does the same thing.
2. Open PowerShell in this folder and run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File setup.ps1
   ```
   It asks **mixed** or **offline**, then:
   - installs pnpm if needed;
   - downloads the LiveKit server into `livekit_server\`;
   - builds `agent\.venv` from the pinned `requirements.txt` and installs the call page;
   - pulls the models;
   - opens three files for you to fill in: `agent\.env`, `agent\personal.json` and
     `agent\profile.md`.
3. Put your keys in `agent\.env` (see [Settings](#settings-agentenv)) and your name and
   town in `agent\personal.json` (see [Personal settings](#personal-settings-agentpersonaljson)).
4. Check everything:
   ```powershell
   powershell -ExecutionPolicy Bypass -File doctor.ps1
   ```
5. Start Othos with `start-othos.bat`. The call page opens at `http://127.0.0.1:3000`.
   Press **Start call** and say hello.
   - For a taskbar app instead: `agent\build-othos-app.ps1` builds `agent\app\Othos.exe` and
     adds an Othos shortcut. Click it to start Othos; close its window to stop Othos.
     The first time it opens, it asks whether it may check your PC's specs. If you say
     yes, it shows which models and settings fit your PC, local options first, and can
     apply them (backing up first). Nothing leaves your computer. **Check my PC…** in the
     window does this again any time.
   - Once a week the window asks **"Check for updated models?"** (see
     [The weekly model check](#the-weekly-model-check)). **Check models…** runs it any time.
   - To stop Othos without the app, run `agent\stop-othos.bat`.

## Setup modes

| | Mixed (default) | Fully offline |
|---|---|---|
| Speech-to-text | Deepgram (cloud) | a local OpenAI-compatible server ([Speaches](https://github.com/speaches-ai/speaches) / faster-whisper), port 8000 |
| Voice | Rime (cloud) | [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI), port 8880 |
| Conversation, notes, camera, coding | local (Ollama) | local (Ollama) |
| Web search | Tavily, after the free sources | off unless you add a Tavily key |
| Hardware | any 16 GB PC, CPU-only is fine | a graphics card (see [Hardware](#hardware)) |
| `.env` | `OTHOS_SPEECH=cloud` | `OTHOS_SPEECH=local` |

**Fully offline set-up:**
1. Run `setup.ps1 -Mode offline`. It sets `OTHOS_SPEECH=local`.
2. Install and start the two speech servers, following their own instructions. Both run in
   Docker or Python on Windows:
   - **Speaches** on `http://127.0.0.1:8000`, with a Whisper model such as
     `Systran/faster-whisper-small.en`;
   - **Kokoro-FastAPI** on `http://127.0.0.1:8880`.
3. Set `LOCAL_STT_URL`, `LOCAL_STT_MODEL`, `LOCAL_TTS_URL` and `LOCAL_TTS_VOICE` in
   `agent\.env` if yours differ from the defaults.
4. Start both servers before Othos. `doctor.ps1` checks they're listening.

> **Not tested yet:** local speech was built on a CPU-only laptop with no graphics card,
> so it has only been checked to start up, not used in a real call. Reports welcome.

## Settings (`agent\.env`)

`setup.ps1` copies `agent\.env.example` to `agent\.env`. It stays on your computer and
is never committed. Anything not listed as required can stay empty.

| Line | What it is | Where to get it | Needed? |
|---|---|---|---|
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | the local call server (`ws://127.0.0.1:7880`, `devkey`, `secret`) | already filled in | yes, leave as is |
| `LIVEKIT_TOKEN_SERVER_ID`, `AGENT_NAME` | LiveKit Cloud options | - | leave empty |
| `GREET_ON_START` | Othos says hello when the call starts | - | optional (`true`) |
| `OTHOS_SPEECH` | `cloud` or `local` (see [Setup modes](#setup-modes)) | - | yes |
| `DEEPGRAM_API_KEY` | speech-to-text (cloud) | [console.deepgram.com](https://console.deepgram.com/) (free credit on sign-up) | cloud speech |
| `DEEPGRAM_MIP_OPT_OUT` | `true` = Deepgram may not use your audio to train its models | - | keep `true` |
| `RIME_API_KEY`, `RIME_MODEL`, `RIME_SPEAKER` | the voice (cloud); default `coda` / `holloway` | [rime.ai](https://rime.ai/) dashboard | cloud speech |
| `LOCAL_STT_URL`, `LOCAL_STT_MODEL` | local speech-to-text server and model | your Speaches install | local speech |
| `LOCAL_TTS_URL`, `LOCAL_TTS_MODEL`, `LOCAL_TTS_VOICE` | local voice server, model and voice (`af_heart`) | your Kokoro-FastAPI install | local speech |
| `OLLAMA_MODEL`, `OLLAMA_BASE_URL` | the conversation model (`llama3.2:3b`) and Ollama's address | `ollama pull <model>` | yes (defaults work) |
| `TAVILY_API_KEY` | web search, used last | [app.tavily.com](https://app.tavily.com/) (1,000 free credits a month) | optional |
| `OTHOS_TAVILY_WARN`, `OTHOS_TAVILY_LIMIT` | spoken heads-up after this many credits; stop at the limit (`0` = the plan's own) | - | optional |
| `US_NEWS_SITES`, `NEWS_SITES` | outlets for news searches (comma-separated) | - | optional |
| `RAVELRY_USER`, `RAVELRY_KEY` | knitting and crochet patterns (read-only key) | [ravelry.com/pro/developer](https://www.ravelry.com/pro/developer) | optional |
| `YOUTUBE_API_KEY` | tutorial videos | [Google Cloud console](https://console.cloud.google.com/apis/library/youtube.googleapis.com) (YouTube Data API v3) | optional |
| `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` | how busy a game is on Twitch | [dev.twitch.tv/console](https://dev.twitch.tv/console) | optional |
| `GUARDIAN_API_KEY` | news from The Guardian | [open-platform.theguardian.com](https://open-platform.theguardian.com/access/) | optional |
| `NYT_API_KEY` | New York Times top stories, most read, bestsellers | [developer.nytimes.com](https://developer.nytimes.com/) | optional |
| `OTHOS_WEATHER_LOCATION`, `OTHOS_WEATHER_LATLON`, `OTHOS_HOME_LOCATION`, `OTHOS_CONTACT_URL` | older place settings; `personal.json` is used first | - | no |

The call page has its own small `ui\.env.local`, which `setup.ps1` makes. It holds the same
three LiveKit lines and `OTHOS_CODE_MODEL` (the code chat's model, `qwen2.5-coder:3b`).
Optional extras there: `OTHOS_AGENT_DIR` (if `agent\` isn't next to `ui\`),
`OTHOS_DRAFTS_DIR` and `OTHOS_EDITOR` (the editor that opens saved drafts; Notepad++ by default).

## Personal settings (`agent\personal.json`)

`setup.ps1` copies `personal.example.json` to `personal.json`. It stays on your computer
and is never committed. Every field starts empty, which means that feature is off, so
Othos works before you fill anything in. At the bottom, `_example` shows a filled-in file
for a made-up user; it's never used. Close and reopen Othos after editing.

| Field | What it does | Example |
|---|---|---|
| `name` | what Othos calls you (only in greetings, goodbyes and urgent moments) | `"Alex"` |
| `assistant` | the assistant's own name, logo and colour (see [Make it yours](#make-it-yours-name-logo-and-colour)) | `{"name": "Jarvis"}` |
| `weather_location` | your town, for weather and "where do I live?" | `"Springfield, Greene County, MO, US"` |
| `weather_latlon` | the town's map point for the US National Weather Service. Only this goes to weather.gov | `"37.2090,-93.2923"` |
| `home_location` | where you live, if different from the weather town | `""` |
| `contact_url` | your website or contact page. Wikipedia refuses programs without one, so Wikipedia answers are off until it's set | `"https://example.com"` |
| `local_news.feeds` | RSS feeds for "local news": `site`, `name` (said aloud), `url`, and optional `skip_urls` (a pattern; stories whose address matches are dropped, such as a paper's wire-service pages) | see the example file |
| `local_news.trigger_words` | words that mean local news, besides "local", "nearby" and "around here": towns, county, region | `["springfield", "ozarks"]` |
| `state_news.state` | your state; the news set is then called "<State> state news" | `"Missouri"` |
| `state_news.feeds`, `state_news.trigger_words` | state news feeds, and extra words that mean state news (the capital, say) | `["jefferson city"]` |
| `pronunciations` | how the voice should say local place names (added to `pronunciations.json`, which has the tech words) | `{"Ozarks": "Oh zarks"}` |
| `keyterms` | names speech-to-text keeps mishearing (Deepgram key terms; a hint for local Whisper) | `["Springfield"]` |
| `model_check` | the weekly model check: `"ask"` (a Yes/No once a week, the default), `"auto"` (checks without asking and shows a line in the Othos window when something is new) or `"off"` (only from **Check models…**) | `"ask"` |

`agent\profile.md` holds a few lines about you and how you like Othos to behave
("Keep answers to one or two sentences unless asked for more", "I'm vegetarian", "I live
with two cats"). It's read at the start of
each call. Othos never writes to it; only you edit it.

The weather comes from the US National Weather Service, so outside the US it falls back to
a Tavily search for the town's weather.

### Make it yours: name, logo and colour

Your assistant doesn't have to be called Othos. `setup.ps1` asks what to call it, or you
can fill in the `assistant` section of `agent\personal.json` yourself:

```json
"assistant": {"name": "Jarvis", "heard_as": ["Jervis"], "logo": "my-logo.png", "color": "#9d7bff"}
```

| Field | What it does |
|---|---|
| `name` | What the assistant calls itself, and the name you use for it ("Jarvis, end the call"). Empty means Othos. |
| `heard_as` | Words speech-to-text writes instead of the name, so they count as the name too. Look in a call's transcript to find them. |
| `logo` | An image file: a full path, or a file name in `agent\`. It's used on the welcome screen, as the browser tab's icon and as the Othos window's icon. PNG, SVG, JPG, WebP and ICO all work on the call page. For the app's taskbar and shortcut icon, use an `.ico` file, then run `agent\build-othos-app.ps1` again. |
| `color` | An accent colour for the name, like `"#9d7bff"`. Empty means the usual colours. |

The Circuit Board picture stays the same; only the name on it changes. Close and reopen
Othos after editing (refreshing is enough for the call page).

## The Smithy (coding worker)

The Smithy is a background coding helper with its own taskbar app.

### Quick start

1. Finish the Othos quick start above first: `setup.ps1` makes the Python environment The
   Smithy uses, and Ollama must be installed.
2. Build the app (it also adds a Desktop and Start-menu shortcut). `setup.ps1` doesn't do this:
   ```powershell
   powershell -ExecutionPolicy Bypass -File agent\build-smithy.ps1
   ```
3. Open **The Smithy** from the shortcut. Opening it starts the worker; closing it stops it.
4. Click **Get models...** and fetch the default coding model (`qwen2.5-coder:3b`) if setup
   didn't already pull it. Ollama must be running.
5. Try one small job from the window's command line, or by voice once Othos is running:
   "ask the Smithy to review gaming dot py". The result waits in `agent\drafts\smithy\`.
   Nothing is ever applied or run for you.
6. Optional: **Providers...** lets you add a free cloud model's key (saved encrypted) and turn it
   on. Nothing leaves your PC until you turn a provider on; `cloud.enabled` is empty by default.

More below.

- **Build the app:** `agent\build-smithy.ps1`.
- **Start and stop:** opening the app starts the worker; closing it stops it.
- **Ask by voice:** "ask the Smithy to review gaming dot py", "ask the Smithy to write a
  script that renames my photos by date", "ask the Smithy to build a small to-do web app".
- **Task types:** review, fix (a diff, never applied), script (saved, never run), Joomla
  template overrides, build (a small project from scratch, in its own drafts folder), and
  follow-ups.
- **Results:** they wait in `agent\drafts\smithy\` for you to read. Othos tells you when one
  is ready ("Smithy task 3 is ready").
- **What it may read:** files only from the folders in `agent\smithy.json`
  (`code_folders`). Private files (`.env`, keys, notes) are always blocked, and key-like
  strings are removed before anything reaches the model.
- **Models:** it runs on the CPU only, with its own model (`qwen2.5-coder:3b` by default;
  **Get models...** in its window fetches others).
- **Gaming:** it pauses while a CPU-heavy game runs.
- **More:** `agent\SMITHY.md`.

There's also a typed **code chat** at `http://127.0.0.1:3000/code` (or the `</>` button
in the call). **Save code to drafts** writes new files to `agent\drafts\` and never
changes anything else.

## Coming later: Firestarter (website work)

**Firestarter** is a second background worker, for website work. It drafts articles,
product descriptions, research write-ups and site content for Joomla or WordPress, and
can send a finished article to WordPress **as a draft** when you press Send. It will be
a separate add-on with its own repository, **published later**; it isn't available yet.
- **Othos is ready for it:** once it's installed (its `install.ps1` copies it into
  `agent\`), Othos picks it up by itself: its voice commands ("ask Firestarter to ...")
  and its chip on the Circuit Board.
- **Until then,** Othos simply says "Firestarter isn't installed".

## Voice commands

| Say | What happens |
|---|---|
| "Othos, end the call" / "hang up" | Othos says goodbye and ends the call |
| "Remember that my sister's birthday is June 3rd" | saves a note (in `agent\brain\`, on your computer) |
| "What did I tell you about my sister?" | searches your notes |
| "What's the news?" / "keep going" / "tell me more about the second one" / "where's that from?" | news roundup, more headlines, follow-ups, sources |
| "Any breaking news?" / "summarize the news" | the last few hours / a spoken summary |
| "What's the local news?" / "state news" | your feeds from `personal.json` |
| "PC hardware news" / "tech news" / "design news" | free news feeds |
| "What are people reading?" / "bestsellers" | New York Times lists (with its key) |
| "What's the weather like?" / "will it rain tomorrow?" | weather for your town |
| "Search the web for ..." | Tavily directly, skipping the free sources |
| "What's Apple's stock price?" | a quoted price with its source and time |
| "Take a look" / "what am I holding?" / "read this label" | one webcam picture, described locally (camera on) |
| "Switch to code chat" | opens the code chat after Othos finishes speaking |
| "Gaming mode on" / "gaming mode off" / "is gaming mode on?" | moves Othos out of a game's way |
| "Ask the Smithy to ..." | starts a Smithy task (while The Smithy runs) |
| "What's the Smithy working on?" / "read me Smithy task 3" / "show me Smithy task 3" | progress, results read aloud, open the result |
| "Keep Smithy task 3" / "discard Smithy task 3" | marks a result (asks yes or no first) |
| "Read me the Smithy log" / "show me the Smithy log" | the work log |
| "Othos, start a how-to: setting up OBS" | starts recording a how-to: Othos says "Recording", then stays quiet; each thing you say is a step, with a picture of the window in front |
| "Scratch that" / "note: use the second port" | while recording: removes the last step / adds a tip to it |
| "Take a picture" (or "snap") / "no picture" | while recording: another picture for the last step / removes its pictures |
| "Pause the how-to" / "resume the how-to" / "how many steps?" | while recording |
| "Finish the how-to" | saves it in `agent\drafts\howto\` (the steps, pictures and what you said); the coming Firestarter add-on will write these up as guides |

## Privacy: what goes where

- **On your computer only:** the conversation model, your notes and their search, webcam
  pictures (taken only when you ask; never streamed), code chat, The Smithy (unless you
  turn on another model provider, below), `.env`,
  `personal.json`, `profile.md` and the logs.
- **How-to screenshots** are taken only while you're recording a how-to: one per step you
  say, or when you say "take a picture". There's no timer and no background capture.
  - **What's captured:** only the window in front, never the whole screen.
  - **What's skipped:** chat apps (Signal, Discord, Telegram, WhatsApp), password
    managers, Windows sign-in and permission prompts, and windows whose title mentions a
    password, credentials, signing in or two-factor codes.
  - **Where they go:** they're saved in `agent\drafts\howto\` and go nowhere else.
- **Deepgram** (mixed mode) hears your voice during a call. **Rime** gets Othos's replies to
  speak. In offline mode neither is used.
- **Tavily** gets search questions. Personal questions are never sent to it.
- **Other model providers for The Smithy** (Anthropic, DeepSeek, Mistral and others, by API
  key) are **off until you turn one on** in The Smithy's **Providers…** window. Keys
  typed there are encrypted for your Windows account and kept outside the Othos folder
  (`%LOCALAPPDATA%\Othos\keys.dat`), so they never end up in git. `.env` works too (the
  advanced way). Even then, a task uses a provider only when you name it, or when you
  set it as the default.
  - **What's sent:** that task's request, after the same filter that removes personal
    details.
  - **Refused:** providers whose free tiers may train on what you send, unless you allow
    them.
  - **Limits:** daily token and spending limits. LM Studio on this computer works the
    same way and sends nothing out. See `agent\SMITHY.md`, "Other model providers".
- **The free sources** get only search words, a game or channel name, or your town's map
  point: the National Weather Service, Wikipedia (it also sees your `contact_url`), Stack
  Exchange, GitHub, RSS feeds, and, with your keys, Ravelry, YouTube, Twitch, The Guardian
  and the NYT.
- **The weekly model check** asks ollama.com, Hugging Face and GitHub about newer models.
  It runs only when you say yes, click **Check models…**, or set `"model_check": "auto"`.
  It sends only model family names (and, like any web request, your computer's internet
  address). There's no account and no cookies. Nothing is downloaded unless you click
  **Get** and say yes. The same check also re-reads the free limits of the model providers
  you turned on in The Smithy (only their own free account pages, with the keys you saved
  there, no test requests). With no provider on, that part does nothing.
- **Othos never sends, posts or deletes anything** on your behalf. Keep or discard goes
  through a yes/no check in code. The Smithy never runs, installs or applies anything.
- The call page and LiveKit server listen on `127.0.0.1` only, so other devices on your
  network can't reach them.

## The weekly model check

Once a week the Othos window asks **"Check for updated models?"** with **Yes, check** or
**Not now**. "Not now" asks again a week later. **Check models…** at the top of the window
checks any time. To change how it starts, set `model_check` in `personal.json` (`"ask"`,
`"auto"` or `"off"`).

**The check itself downloads nothing.** The list shows:

1. **Models tested with your version** (`agent\models-tested.json`), for each job: voice
   conversation, camera, notes search, and coding (The Smithy). Each says whether it's
   installed, whether the Ollama library has an update for it, or how to get it.
2. **Newer models in the same families**, marked "not tested yet": from the Ollama library,
   plus GGUF files from trusted Hugging Face publishers for llama-server.
3. **A newer Ollama** (and llama.cpp, if The Smithy uses llama-server), from GitHub.
4. **The model providers you turned on** in The Smithy's **Providers…** window. These come
   last, after the local models. Each check also quietly re-reads their free limits (their
   own free account pages, no test requests) so the Providers window stays in the right
   order.

Each model gets **Fits**, **Tight** or **Won't fit**, from your PC's RAM and graphics card
(the **Check my PC…** figures). Voice models too big to answer quickly on a CPU are marked
Tight at best.

**To get a model,** click **Get** (or **Update**) on its row:

1. A Yes/No shows the download size and how much space is free. If there isn't enough
   room, it tells you and doesn't start.
2. Click **Yes, download**. Progress shows at the top of the window. **Cancel** stops it,
   and Get carries on later where it left off.
3. When it's done, the row says **Installed**. Downloading doesn't change which model is
   used: switch with **Check my PC… → Apply** (voice), or The Smithy's model switcher
   (coding).

Models marked **Won't fit** get no button. **Open page** on a newer Ollama opens its
download page. The last check is saved in `agent\state\model-check.json`.

From the command line: `.venv\Scripts\python.exe model_check.py` (in `agent\`).

## Hardware

Speeds are **estimates** for writing a reply (tokens per second) with 4-bit (Q4)
models in Ollama. Your numbers will differ. "Context" is how much conversation the model
keeps in mind. Mixed mode runs on any of these. Fully offline needs about 2-3 GB of
graphics memory for speech on top of the conversation model.

### Graphics cards (current and two previous generations)

| Cards | VRAM | Conversation | Coding (The Smithy) | Local speech | Context | Est. speed |
|---|---|---|---|---|---|---|
| RTX 5090 | 32 GB | 27-32B (Qwen3 32B, Gemma 3 27B) | Qwen3-Coder 30B-A3B | Whisper large-v3-turbo + Kokoro | 32k | 50-70 t/s |
| RTX 4090, 3090 / 3090 Ti, RX 7900 XTX | 24 GB | 27-32B | Qwen3-Coder 30B-A3B | Whisper large-v3-turbo + Kokoro | 16-32k | 30-50 t/s |
| RX 7900 XT | 20 GB | 14B, or 27B with a short context | Qwen3-Coder 30B-A3B (part in RAM) | Whisper turbo + Kokoro | 16k | 25-40 t/s |
| RTX 5080, 5070 Ti, 5060 Ti 16 GB, 4080 / Super, 4070 Ti Super, 4060 Ti 16 GB, RX 9070 XT, 9070, 9060 XT 16 GB, 7900 GRE, 7800 XT, 7600 XT, 6950 XT, 6900 XT, 6800 XT, 6800, Arc A770 16 GB | 16 GB | 8-14B (Qwen3 8B / 14B, Llama 3.1 8B) | Qwen2.5-Coder 14B, or Qwen3-Coder 30B-A3B mostly in RAM | Whisper turbo + Kokoro | 16k | 25-60 t/s (8B) |
| RTX 5070, 4070 / Super / Ti, 3080 12 GB, 3060 12 GB, RX 7700 XT, 6700 XT, Arc B580 | 12 GB | 8B | Qwen2.5-Coder 7B | Whisper small/medium + Kokoro | 8-16k | 25-50 t/s |
| RTX 3080 10 GB, Arc B570 | 10 GB | 8B (short context) | Qwen2.5-Coder 7B | Whisper small + Kokoro | 8k | 25-45 t/s |
| RTX 5060, 4060, 4060 Ti 8 GB, 3070, 3060 Ti, RX 9060 XT 8 GB, 7600, 6600 / XT, Arc A750, A770 8 GB | 8 GB | 3-4B (Llama 3.2 3B, Qwen3 4B), or 8B with speech on the CPU | Qwen2.5-Coder 3B / 7B | Whisper small + Kokoro (tight) | 8k | 40-80 t/s (3B) |

### Laptops (newest first)

| Laptop | Memory for models | Conversation | Coding | Speech | Est. speed |
|---|---|---|---|---|---|
| RTX 5090 laptop GPU | 24 GB | 27-32B | Qwen3-Coder 30B-A3B | local | 30-45 t/s |
| RTX 5080 / 4090 laptop GPU | 16 GB | 8-14B | Qwen2.5-Coder 14B | local | 25-45 t/s |
| RTX 5070 Ti / 4080 laptop GPU | 12 GB | 8B | Qwen2.5-Coder 7B | local | 25-40 t/s |
| RTX 5070, 5060, 4070, 4060 laptop GPU | 8 GB | 3-4B (8B tight) | Qwen2.5-Coder 3B / 7B | local (tight) or mixed | 30-60 t/s (3B) |
| AMD Ryzen AI Max / Max+ (Strix Halo), 64-128 GB | most of the RAM is shared with the graphics | 14-32B, even 70B slowly | Qwen3-Coder 30B-A3B | local | 10-40 t/s |
| Intel Core Ultra (Arc graphics built in), 16-32 GB | system RAM | 3-8B, mostly on the CPU | Qwen2.5-Coder 3B | mixed | 6-15 t/s |
| Snapdragon X (Windows on Arm), 16-32 GB | system RAM | 3-8B on the CPU | Qwen2.5-Coder 3B | mixed | 8-15 t/s |
| CPU-only, 16 GB (for example Intel i7-1355U with Iris Xe) | system RAM | Llama 3.2 3B | Qwen2.5-Coder 3B (slow; background only) | mixed | 5-9 t/s |

The last row is the laptop Othos was built and tested on, in mixed mode. Replies take a
few seconds to start, and the visualizer can be turned off for speed (`?board=off`).

**Ollama on Windows: support differs by brand.**
- **NVIDIA:** RTX cards work out of the box.
- **AMD:** Radeon support uses ROCm and covers only some cards (mostly the RX 7000 and
  9000 series and the higher RX 6000 cards). Others fall back to the CPU, or need Ollama's
  experimental Vulkan support. Strix Halo support is newer; check Ollama's GPU page.
- **Intel Arc and Snapdragon:** Arc cards, Intel's built-in graphics and Snapdragon's GPU
  and NPU aren't used by standard Ollama, so models run on the CPU. Intel's IPEX-LLM build
  of Ollama, or llama.cpp with Vulkan, can use Arc.
- **Before you buy:** check [Ollama's GPU list](https://docs.ollama.com/gpu).

## Troubleshooting

- **Run `doctor.ps1` first.** It lists missing keys, models and programs, busy ports and
  `localhost` addresses.
- **The agent didn't join the call:**
  - Look in `agent\logs\agent.log`.
  - Usually the agent is still loading (wait a few seconds and press Start call again),
    Ollama isn't running, or `LIVEKIT_URL` differs between `agent\.env` and
    `ui\.env.local`.
  - Port 8081 busy means an old agent is still running: run `agent\stop-othos.bat`.
- **401 errors about LiveKit Cloud in the log are harmless.** The plugins try LiveKit
  Cloud's turn-detection service; Othos uses its own local server and doesn't need it.
- **Use 127.0.0.1, not localhost.** On some Windows setups `localhost` resolves to IPv6
  first and the call page or LiveKit server can't be reached. Every address in the
  settings should be `127.0.0.1`.
- **Wrong Python:** the agent needs 3.12. If `doctor.ps1` says otherwise, delete
  `agent\.venv` and run `setup.ps1` again.
- **Busy ports:**
  - What they're for: 7880 (LiveKit), 8081 (agent), 3000 (call page), 11434 (Ollama), and
    for local speech 8000 and 8880.
  - Close whatever holds one, or stop Othos with `agent\stop-othos.bat`.
- **Slow replies:**
  - Use a smaller model (`OLLAMA_MODEL`).
  - Turn the visualizer off (`http://127.0.0.1:3000/?board=off`).
  - Close other heavy programs.

## Customizing

- **Personality and rules:** `agent\profile.md` (yours to edit). The core personality is
  in `agent\agent.py` (`Assistant`).
- **Conversation model:** `OLLAMA_MODEL` in `agent\.env`. Pull it first, for example
  `ollama pull qwen3:8b`.
- **Coding model:** `OTHOS_CODE_MODEL` in `ui\.env.local` (code chat); The Smithy's
  window or `agent\smithy.json` (the worker).
- **Voice:** `RIME_SPEAKER` (cloud) or `LOCAL_TTS_VOICE` (Kokoro). Respellings go in
  `agent\pronunciations.json`, and place names in `personal.json`.
- **News outlets:** `US_NEWS_SITES` / `NEWS_SITES` in `.env`; local and state feeds in
  `personal.json`.
- **Gaming mode:** `agent\gaming.json` (your games and what each one needs);
  `agent\make-gaming-model.ps1` makes the CPU-only gaming voice model.
- **Requirements stay pinned.** After installing or upgrading a Python package, run
  `agent\update-requirements.ps1`.

## Changelog

From the version tags.

- **V1, "Othos Project V1" (the first public version):**
  - personal settings in `personal.json`;
  - your own name, logo and colour for the assistant;
  - no fixed folders;
  - the `OTHOS_SPEECH` cloud/local switch;
  - `setup.ps1` and `doctor.ps1`;
  - The Smithy (coding worker) with voice commands and work logs;
  - worker chips on the Circuit Board;
  - code chat in the call's control bar.
- **v0.10.1:** news from The Guardian and the NYT; "end the call" by voice; numbers in an
  answer must match their sources; stock and crypto prices.
- **v0.10:** free sources before Tavily (weather, RSS feeds, Stack Exchange, Ravelry,
  YouTube, Twitch, GitHub, Wikipedia); a monthly Tavily credit guard; hardware news;
  pronunciation fixes.
- **v0.9.2:** friendlier, slightly sarcastic conversation; news roundups with follow-ups,
  sources and breaking news; real dates in searches; local weather and home town;
  pronunciations; pinned requirements; logs kept.
- **v0.9.1:** gaming mode (game detection, a CPU-only voice model, voice and window
  switches); more ways to open code chat.
- **v0.9:** typed code chat with a local coding model and save-to-drafts; page server
  on 127.0.0.1 only.
- **v0.8.1:** conversational news, with headlines read with their source.
- **v0.8:** one Othos taskbar app (open to start, close to stop); a private call page
  that closes itself; instant replies to chit-chat.
- **v0.7.1:** start/stop buttons and tray icon; greeting stutter fixed; says "I don't
  know" instead of inventing reasons.
- **v0.7:** webcam look with local Moondream (one picture per look, follow-ups on it).
- **v0.6:** notes questions answered without waiting for the model.
- **v0.5:** notes search by meaning (nomic-embed-text); models kept loaded.
- **v0.4:** questions about you don't trigger web search.
- **v0.3:** faster turns, tools chosen in code, rebuilt search, name filter.
- **v0.2:** notes and web search tools.
- **v0.1:** first voice assistant (Deepgram, Ollama Llama 3.2 3B, Rime), no tools.

## Licences

- **`ui\public\othos-board\`** (the Circuit Board visualizer) is under the **GNU AGPL
  v3.0 or later**. See `ui\public\othos-board\LICENSE` and its README. Because the call page
  includes it, if you share Othos or run it as a service for other people, you must offer
  the source under the AGPL too. Private use on your own computer is fine.
- **`ui\`** started as LiveKit's Agent Starter for React, under the **MIT License** (see
  `ui\LICENSE`, copyright LiveKit, Inc.).
- **`ui\` as a whole** is under the AGPL v3.0 or later, because it includes the board.
- **`agent\`** and the top-level files are under the **GNU AGPL v3.0 or later** too (see
  `LICENSE` in this folder), so all of Othos has one set of terms.

## Credits

- Circuit Board visualizer graphics by Jared Rhodenizer, [jaredrhod.com](https://jaredrhod.com), from [ai-visualizer](https://github.com/jaredrhod/ai-visualizer) (AGPL-3.0-or-later).
- Call page based on LiveKit's [Agent Starter for React](https://github.com/livekit-examples/agent-starter-react) (MIT).
- Built with [LiveKit Agents](https://github.com/livekit/agents), [Ollama](https://ollama.com),
  [Deepgram](https://deepgram.com), [Rime](https://rime.ai), [Tavily](https://tavily.com),
  [Speaches](https://github.com/speaches-ai/speaches) and
  [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI).
