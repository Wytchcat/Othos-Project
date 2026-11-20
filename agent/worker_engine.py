"""The engine Firestarter (worker.py) and the Smithy (smithy.py) both run on. CPU only.

Each worker is a Profile: its queue (worker_queue.py), its settings file, its task
types and its log. The engine does the rest the same way for both:

- takes the next task, but only once it has the CPU (worker_shared.CpuLock): the
  two workers take turns when both have work, and a worker alone gets all its
  threads;
- games (gaming.json): cpu-heavy -> pause (the task goes back to the front of the
  queue); gpu-heavy / light -> held back (fewer threads, low priority);
- writes a heartbeat status file every few seconds (the apps, voice Othos and the
  call page's board read it);
- saves each result as a draft that waits for the user's review: nothing is ever
  applied, run, sent or published.

A task type is one model call (build -> messages), or several (run -> the Smithy's
"build from scratch": a plan, then each file).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import gaming
import worker_cloud
import worker_shared
from worker_backend import Stopped, available_models, make_backend
from worker_queue import Queue

HERE = Path(__file__).parent
LOG_DIR = HERE / "logs"

log = logging.getLogger("worker")
# One plain sentence per step of a task, as it happens ("Planning the build.", "Cerebras asked to
# wait 60 seconds."): also in the worker's log, and written to state\<worker>-status-steps.jsonl for
# the app's Progress window and Othos's Task Mode (Nov 13: the user wants to watch a task step by step).
steps = logging.getLogger("worker.step")
steps.setLevel(logging.INFO)          # its own level: a quieter "worker" log mustn't drop the steps


def steps_path(status_path: Path) -> Path:
    return status_path.with_name(status_path.stem + "-steps.jsonl")


class StepFile(logging.Handler):
    """Writes each worker.step line, with the time and the running task, to the steps file.
    Kept short: past about 300 KB only the last 400 lines are kept. Step lines say what the engine
    is doing (task type, step, file name, provider, wait), never the task's text or the model's
    output, and each is redacted (worker_shared.redact) before it's written."""

    def __init__(self, path: Path, worker: str):
        super().__init__(logging.INFO)
        self.path, self.worker, self.task = path, worker, None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = json.dumps({"ts": round(record.created, 1), "worker": self.worker, "task": self.task,
                               "text": worker_shared.redact(record.getMessage())}, ensure_ascii=False)
            self.path.parent.mkdir(exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            if self.path.stat().st_size > 300_000:
                keep = self.path.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
                self.path.write_text("\n".join(keep) + "\n", encoding="utf-8")
        except Exception:
            pass                                       # the steps are for watching; never stop a task


def _seconds(s: float) -> str:
    s = round(s)
    return f"{s} seconds" if s < 120 else f"{s // 60} minutes {s % 60} seconds"


FORMAT = (
    "Answer in exactly two parts, with these two headings on their own lines:\n"
    "SPOKEN SUMMARY:\n"
    "Two to four plain sentences that will be read aloud: no code, no symbols, no markdown, "
    "no lists. Say what you found or made and the most important point.\n"
    "FULL RESULT:\n"
    "The complete answer. Put any code in fenced code blocks with a language tag.")


@dataclass
class JobType:
    name: str
    description: str
    tier: str                                  # a key of "models" in the worker's settings
    build: Callable[[dict], list[dict]] | None # params -> chat messages (one model call)
    check: Callable[[dict], dict]              # validates/cleans params, raises ValueError
    title: Callable[[dict], str]               # how Othos names it: "your gaming.py review"
    max_tokens: int = 1024
    artifact: str = ""                         # "diff" or "script": also saved as its own file
    run: Callable | None = None                # (ctx, params) -> (summary, full, artifact path): several calls
    minutes_key: str = "max_job_minutes"       # which time limit applies
    coding: bool = False                       # for the board's chip
    # (job id, minutes running, the model it's on now) -> the "See me" check-in's text on a long job, or ""
    see_me: Callable[[int, float, str], str] | None = None


@dataclass
class Profile:
    queue: Queue
    config_path: Path
    registry: dict[str, JobType]
    log_name: str                              # logs\<log_name> (written by the app)
    script: str                                # worker.py / smithy.py
    tiers: tuple[str, ...]                     # the model switcher's rows
    add_usage: str = ""
    add_parse: Callable[[str, list[str]], dict] | None = None   # command-line "add KIND ARGS" -> raw params
    # called about once a minute while the worker has nothing queued, with its settings (The Smithy: a
    # plan nobody answered is built on its assumptions after build_ask_wait_hours, when that's set)
    on_idle: Callable[[dict], None] | None = None


# ---------------------------------------------------------------- results
_FENCE = re.compile(r"```[^\n]*\n[\s\S]*?(?:```|$)")
# a heading is matched on its own line only: never eat the next line's { or #
_SUMMARY_HEAD = re.compile(r"(?im)^[^\w\n]*spoken\s+summary[^\w\n]*$|^[^\w\n]*spoken\s+summary\s*:[ \t]*")
_FULL_HEAD = re.compile(r"(?im)^[^\w\n]*full\s+result[^\w\n]*$|^[^\w\n]*full\s+result\s*:[ \t]*")


def plain_summary(text: str, sentences: int = 3) -> str:
    """Fallback spoken summary: first sentences, with code and symbols taken out."""
    t = _FENCE.sub(" ", text)
    # inline code: keep short things (paths, names) as words; long snippets point to the drafts
    t = re.sub(r"`([^`]{1,80})`", r"\1", t)
    t = re.sub(r"`[^`]*`", " (see the drafts) ", t)
    t = re.sub(r"[#*_>|\[\]{}()<>=\\/~^$]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    parts = re.split(r"(?<=[.!?])\s+", t)
    return " ".join(parts[:sentences]).strip() or "The result is ready on screen."


def split_result(text: str) -> tuple[str, str]:
    """(spoken summary, full result) from the model's two-part answer."""
    s = _SUMMARY_HEAD.search(text)
    f = _FULL_HEAD.search(text)
    if s and f and f.start() > s.end():
        summary, full = text[s.end():f.start()].strip(), text[f.end():].strip()
    elif f:
        summary, full = text[:f.start()].strip(), text[f.end():].strip()
    else:
        summary, full = "", text.strip()
    # a list ("- It does X") becomes plain sentences, for reading aloud
    summary = re.sub(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+(.*?)[.;:]?\s*$", r"\1.", summary).strip()
    summary = re.sub(r"\s*\n\s*", " ", summary)
    if not summary or _FENCE.search(summary) or len(summary.split()) > 90:
        summary = plain_summary(summary or full)
    return plain_summary(summary, 4) if re.search(r"[`#*{}<>]", summary) else summary, full or text.strip()


def copied_back(path: str | None, full: str) -> bool:
    """True when the answer's code is mostly the input file pasted back."""
    if not path:
        return False
    import difflib
    try:
        src = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    for block in re.findall(r"```[^\n]*\n([\s\S]*?)```", full):
        if len(block) > 0.6 * len(src) and difflib.SequenceMatcher(None, block[:6000], src[:6000]).quick_ratio() > 0.9:
            return True
    return False


EXT = {"powershell": "ps1", "ps1": "ps1", "pwsh": "ps1", "python": "py", "py": "py", "bash": "sh", "sh": "sh",
       "batch": "bat", "bat": "bat", "cmd": "bat", "javascript": "js", "js": "js", "typescript": "ts", "ts": "ts",
       "csharp": "cs", "cs": "cs", "html": "html", "css": "css", "php": "php", "sql": "sql", "json": "json"}


def save_artifact(results_dir: Path, job_id: int, kind: str, full: str, newline: str | None = None) -> Path | None:
    """A fix's diff or a script as its own draft file (never applied or run).
    newline: the line ends to write ("\\n" or "\\r\\n"); None = Windows' default. A diff gets its file's own
    (Nov 14: a diff saved with \\r\\n didn't apply to the \\n file it was for)."""
    blocks = re.findall(r"```([\w#+.-]*)[^\n]*\n([\s\S]*?)```", full)
    if not kind or not blocks:
        return None
    if kind == "diff":
        lang, code = next(((l, c) for l, c in blocks if l.lower() in ("diff", "patch")), blocks[0])
        path = results_dir / f"{job_id:05d}-fix.diff"
    else:
        lang, code = blocks[0]
        path = results_dir / f"{job_id:05d}-script.{EXT.get(lang.lower(), 'txt')}"
    path.write_text(code, encoding="utf-8", newline=newline)
    return path


FREE_TOKENS_GRACE = 300   # carry on 5 minutes after free tokens are back (the user's resume rule)
# A result that starts with this didn't check clean (The Smithy's build checks): never reported as done
NOT_COMPLETE = "NOT COMPLETE, doesn't check clean"


class CheckInTimer:
    """Calls `fire` whenever `due()` says so, checked every `every_s` seconds on its own thread, until stop(): a
    long job's "See me" check-in keeps its own time, so a quiet step can't hold it back (Nov 18, Smithy task 60: due
    at 30 minutes, it came at 48, while one step on this PC's model said nothing for 40 minutes)."""

    def __init__(self, due, fire, every_s: float = 15, name: str = "check-in"):
        self.due, self.fire, self.every_s, self.name = due, fire, every_s, name
        self._stop = threading.Event()

    def _loop(self) -> None:
        while not self._stop.wait(self.every_s):
            try:
                if self.due():
                    self.fire()
            except Exception as e:                       # a check-in never stops the task
                log.info("%s: check-in skipped (%s)", self.name, e)

    def start(self) -> None:
        threading.Thread(target=self._loop, name=self.name, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


class TaskContext:
    """What a several-step task (run=...) gets: the model, and a chat() that keeps
    pausing, stopping and the progress figure working across all its steps."""

    def __init__(self, worker: "Worker", row, model: str, threads: int, should_stop, backend=None):
        self.worker, self.row, self.model, self.threads = worker, row, model, threads
        self.backend = backend or worker.backend          # a cloud provider for this task (worker_cloud.py)
        self.cfg = worker.cfg
        self.queue = worker.profile.queue
        self._should_stop = should_stop
        self.lo, self.hi = 0.0, 0.95

    def step(self, lo: float, hi: float) -> None:
        self.lo, self.hi = lo, hi
        self._should_stop(0, frac=lo)

    def chat(self, messages: list[dict], max_tokens: int) -> str:
        def stop(chars: int = 0):
            part = min(1.0, chars / (max_tokens * 3.5))
            self._should_stop(chars, frac=self.lo + (self.hi - self.lo) * part)
        if self.backend is self.worker.backend:        # this PC's model: never a step it can't hold
            worker_cloud.check_fits(messages, max_tokens, self.cfg)
        return self.backend.chat(self.model, messages, self.threads, max_tokens, stop)


class Worker:
    def __init__(self, profile: Profile):
        self.profile = profile
        self.q = profile.queue
        self.cfg = self.load_cfg()
        self.backend = make_backend(self.cfg, LOG_DIR, Path(profile.log_name).stem)
        self.gcfg = gaming.load_config()
        self.cpu = worker_shared.CpuLock(self.q.key)
        self.mode, self.mode_settings, self.mode_why = "normal", {}, ""
        self._last_game_check = 0.0
        self.current: dict | None = None
        self.paused_task: dict | None = None   # task sent back to the queue for a game
        self.step_file = StepFile(steps_path(self.q.status_path), self.q.key)
        steps.addHandler(self.step_file)

    def load_cfg(self) -> dict:
        # utf-8-sig: Notepad++ (and PowerShell) can save with a BOM at the start
        return worker_shared.load_json(self.profile.config_path)

    # -- heartbeat for the apps, voice Othos and the board
    def write_status(self, state: str, waiting_for: str = "") -> None:
        cur = self.current or {}
        try:
            c = self.q.counts()
            self.q.status_path.parent.mkdir(exist_ok=True)
            tmp = self.q.status_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "worker": self.q.key, "name": self.q.name,
                "pid": os.getpid(), "state": state, "updated_ts": time.time(),
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "backend": self.backend.name, "game": self.mode_why,
                "waiting_for_cpu": waiting_for or None,
                "task_id": cur.get("id"), "task_type": cur.get("type"), "task_title": cur.get("title"),
                # for the call page's chips
                "task_requested_by": cur.get("requested_by"),
                "task_is_coding": cur.get("coding") if cur else None,
                "results_to_report": self.q.results_to_report(),
                "model": cur.get("model"), "threads": cur.get("threads"),
                "started_ts": cur.get("started"),
                "elapsed": round(time.time() - cur["started"]) if cur.get("started") else None,
                "progress": cur.get("progress"),
                "see_me": cur.get("see_me"),           # the last "See me" check-in on a long job: {ts, text}
                "paused_task": self.paused_task if state == "paused" else None,
                # every provider in the switch order is out (worker_cloud.Chain): when they're back, and her answer
                "free_tokens_back_ts": cur.get("free_tokens_back") if state == "waiting for free tokens" else None,
                "out_choice": cur.get("out_choice", "") if state == "waiting for free tokens" else "",
                "queued": c.get("queued", 0), "review": c.get("review", 0),
                "models": self.cfg.get("models", {}),
            }, indent=2), encoding="utf-8")
            # Windows refuses the swap while another program is reading the file (the
            # app, the call page's /api/worker): retry, then write in place
            for attempt in range(5):
                try:
                    tmp.replace(self.q.status_path)
                    break
                except PermissionError:
                    time.sleep(0.05 * (attempt + 1))
            else:
                self.q.status_path.write_text(tmp.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception as e:
            log.warning("couldn't write status: %s", e)

    def check_games(self, force: bool = False) -> None:
        if force or time.monotonic() - self._last_game_check >= float(self.cfg.get("game_check_seconds", 10)):
            self._last_game_check = time.monotonic()
            mode, s, why = worker_shared.game_mode(self.cfg, self.gcfg)
            if (mode, why) != (self.mode, self.mode_why):
                log.info("games: %s%s", mode, f" - {why}" if why else "")
            self.mode, self.mode_settings, self.mode_why = mode, s, why

    # -- one task
    def run_task(self, row) -> None:
        q = self.q
        jt = self.profile.registry.get(row["type"])
        if jt is None:
            q.fail(row["id"], f"{q.name} doesn't do '{row['type']}' tasks", retry=False)
            return
        try:
            params = jt.check(json.loads(row["params"] or "{}"))
        except ValueError as e:
            q.fail(row["id"], str(e), retry=False)
            return
        try:
            self.cfg = self.load_cfg()   # the model switcher may have changed it
        except Exception as e:           # a typo in the settings mustn't stop the worker
            log.warning("%s couldn't be read (%s); keeping the last good settings", self.profile.config_path.name, e)
        held = self.mode == "throttle"
        models = self.cfg.get("models", {})
        tier = self.mode_settings.get("model", jt.tier) if held else jt.tier
        model = models.get(tier) or models.get(jt.tier) or next(iter(models.values()))
        threads = int(self.mode_settings.get("num_thread", 2) if held else self.cfg.get("num_thread", 4))
        self.backend.set_low_priority(held and bool(self.mode_settings.get("low_priority")))
        # a provider named for this task (off unless turned on in the settings; worker_cloud.py)
        task_key = f"{q.key}:{row['id']}"
        worker_cloud.begin_task(task_key)              # max_tokens_per_task counts this whole task
        try:
            cloud = worker_cloud.pick(self.cfg, json.loads(row["params"] or "{}"), row["type"],
                                      local=(self.backend, model))   # this PC's model: the last stop when switching
        except ValueError as e:
            q.fail(row["id"], str(e), retry=False)
            log.info("task %d not started: %s", row["id"], e)
            steps.info("Task %d couldn't start: %s.", row["id"], str(e).rstrip("."))
            return
        backend = cloud[0] if cloud else self.backend
        if cloud:
            model = cloud[1] or "the loaded model"
            log.info("task %d uses %s", row["id"], backend.name)
        start_mode = (self.mode, self.mode_why)
        title = row["title"] or jt.title(params)
        if isinstance(backend, worker_cloud.Chain):
            names = [l[1] for l in backend.links] + (["this PC"] if backend.local else [])
            where = "switching: " + names[0] + " first" + (", then " + ", ".join(names[1:]) if len(names) > 1 else "")
        elif cloud:
            where = f"{getattr(getattr(backend, 'p', None), 'spoken', 'the cloud')} ({model})"
        else:
            where = f"this PC's model ({model})"
        # the task type, not its title: a title can hold a brief's details, and Task Mode reads
        # these aloud (Firestarter, Nov 13). The Progress window shows the title from the status.
        steps.info("Started task %d (%s). Using %s%s.", row["id"], row["type"], where,
                   f"; held back for {self.mode_why}" if held else "")
        self.current = {"id": row["id"], "type": row["type"], "title": title, "model": model,
                        "threads": threads, "started": time.time(), "progress": 0.0,
                        "requested_by": row["requested_by"], "coding": jt.coding}
        log.info("task %d (%s): %s, %d threads%s", row["id"], row["type"], model, threads,
                 f", held back for {self.mode_why}" if held else "")
        self.write_status("held back" if held else "working")
        deadline = [time.monotonic() + 60 * float(self.cfg.get(jt.minutes_key, self.cfg.get("max_job_minutes", 30)))]
        last_check = [0.0]
        # a "See me" check-in every see_me_minutes on a long job (Nov 17, the user: task 58 peaked at 6:41 pm and slid
        # backwards for an hour with nothing said); 0 turns it off
        every = 60 * float(self.cfg.get("see_me_minutes", 30) or 0)
        next_see_me = [time.monotonic() + every]
        see_me_lock = threading.Lock()                 # the step and the timer never both say it

        def see_me() -> None:
            with see_me_lock:
                if time.monotonic() < next_see_me[0]:
                    return
                next_see_me[0] = time.monotonic() + every
            mins = (time.time() - self.current["started"]) / 60
            on = backend.used[-1] if isinstance(backend, worker_cloud.Chain) and backend.used else model
            try:
                text = jt.see_me(row["id"], mins, on) if jt.see_me else ""
            except Exception as e:                    # a check-in never stops the task
                log.info("task %d: no See me text (%s)", row["id"], e)
                text = ""
            text = text or f"Task {row['id']} has been running {mins:.0f} minutes, on {on}."
            self.current["see_me"] = {"ts": time.time(), "text": text}
            log.info("see me: task %d: %s", row["id"], text)
            steps.info("See me: %s", text)

        def should_stop(chars: int = 0, frac: float | None = None):
            # rough progress: answer length against the most it may write (~3.5 characters per token)
            self.current["progress"] = round(min(0.95, frac if frac is not None else chars / (jt.max_tokens * 3.5)), 2)
            if time.monotonic() - last_check[0] < 1.0:
                return
            last_check[0] = time.monotonic()
            if q.status_of(row["id"]) != "running":
                raise Stopped("cancelled")
            self.check_games()
            if (self.mode, self.mode_why) != start_mode:
                raise Stopped("games")       # paused, or held-back settings changed
            if time.monotonic() > deadline[0]:
                raise Stopped("timeout")
            if every > 0 and time.monotonic() >= next_see_me[0]:
                see_me()
            self.write_status("held back" if held else "working")

        if isinstance(backend, worker_cloud.Chain) and \
                str((self.cfg.get("cloud") or {}).get("when_all_out", "ask")).lower() != "local":
            backend.ask = lambda back: self._wait_for_tokens(row, back, deadline)
        # the check-in keeps its own time, so a quiet step can't hold it back (Nov 18, task 60: due at 30 minutes, it
        # came at 48, while this PC's model worked 40 minutes on one step and said nothing)
        ticker = CheckInTimer(lambda: every > 0 and time.monotonic() >= next_see_me[0] and self.current is not None,
                              see_me, name=f"see-me-{row['id']}")
        if every > 0:
            ticker.start()
        t0 = time.monotonic()
        artifact = None
        try:
            if jt.run:
                summary, full, artifact = jt.run(TaskContext(self, row, model, threads, should_stop, backend), params)
            else:
                steps.info("Writing the answer.")
                if backend is self.backend:           # one-call types too (Firestarter, Nov 14: a followup
                    # on a long earlier result is about 8,900 tokens)
                    worker_cloud.check_fits(jt.build(params), jt.max_tokens, self.cfg)
                summary, full = split_result(backend.chat(model, jt.build(params), threads, jt.max_tokens, should_stop))
        except Stopped as s:
            if s.reason == "cancelled":
                log.info("task %d stopped (cancelled)", row["id"])
                steps.info("Task %d was cancelled.", row["id"])
            elif s.reason == "games":
                q.requeue(row["id"], paused_for=self.mode_why or "")
                self.paused_task = {"id": row["id"], "title": title, "game": self.mode_why}
                log.info("task %d back to the front of the queue (%s)", row["id"], self.mode_why or "game closed")
                steps.info("Paused for %s: task %d goes back to the front of the queue.", self.mode_why or "a game",
                           row["id"])
            else:
                q.fail(row["id"], f"took longer than {jt.minutes_key}", retry=False)
                steps.info("Task %d stopped: it took longer than its time limit.", row["id"])
            return
        except worker_cloud.TooLongForLocal as e:      # the same next time: no retry (Firestarter, Nov 14)
            q.fail(row["id"], str(e), retry=False)
            log.warning("task %d failed (%s), not retried", row["id"], e)
            steps.info("Task %d failed: %s.", row["id"], str(e).rstrip("."))
            return
        except Exception as e:
            retry = row["attempts"] < int(self.cfg.get("max_attempts", 2))
            q.fail(row["id"], str(e), retry=retry)
            log.warning("task %d failed (%s)%s", row["id"], e, ", will retry" if retry else "")
            steps.info("Task %d failed: %s.%s", row["id"], str(e).rstrip("."),
                       " It will try once more." if retry else "")
            time.sleep(5)
            return
        finally:
            ticker.stop()
            self.current = None
        if copied_back(params.get("path"), full):
            # small models sometimes describe the file and paste it back instead of reviewing it
            summary = ("The model mostly copied the file instead of reviewing it; a bigger coding "
                       "model should do better. " + summary)
            log.info("task %d: the result mostly repeats the file", row["id"])
        if getattr(backend, "used", None):            # switching: who did the steps (worker_cloud.Chain)
            model = "providers used: " + ", then ".join(backend.used)
        spent = worker_cloud.task_tokens(task_key)
        spent_note = f", {spent:,} cloud tokens" if spent else ""   # the whole task (max_tokens_per_task)
        q.results_dir.mkdir(parents=True, exist_ok=True)
        path = q.results_dir / f"{row['id']:05d}-{row['type']}.md"
        if artifact is None:
            artifact = save_artifact(q.results_dir, row["id"], jt.artifact, full)
        where = ""
        if artifact:
            where = (f"Built in its own folder (not installed or run): `{Path(artifact)}`\n\n" if Path(artifact).is_dir()
                     else f"Saved separately (not applied): `{Path(artifact).name}`\n\n")
        path.write_text(
            f"<!-- {q.name} task {row['id']} ({row['type']}): {title}. {model}{spent_note}, {threads} threads, "
            f"{time.monotonic() - t0:.0f} s, {time.strftime('%Y-%m-%d %H:%M')}. Waiting for review; "
            f"nothing has been applied. -->\n\n# {q.spoken.capitalize()} {row['id']}: {title}\n\n"
            f"## Summary\n\n{summary}\n\n{where}## Full result\n\n{full}\n", encoding="utf-8")
        q.finish(row["id"], str(path), model, summary, str(artifact) if artifact else "")
        self.paused_task = None if (self.paused_task or {}).get("id") == row["id"] else self.paused_task
        if full.startswith(f"**{NOT_COMPLETE}"):
            log.info("task %d stopped NOT complete in %.0f s -> %s (doesn't check clean; waiting for review)",
                     row["id"], time.monotonic() - t0, path.name)
            steps.info("Task %d is NOT complete: it doesn't check clean. Its draft is waiting for your review.",
                       row["id"])
            return
        log.info("task %d done in %.0f s -> %s (waiting for review)", row["id"], time.monotonic() - t0, path.name)
        steps.info("Task %d is done, in %s. It's waiting for your review.", row["id"], _seconds(time.monotonic() - t0))

    def _wait_for_tokens(self, row, back: float, deadline: list) -> str:
        """Every provider in the switch order is out of free tokens: wait here (nothing lost) and
        ask. "local" = finish on this PC's model now; "retry" = 5 minutes after the earliest free
        tokens are back (also when no one answers). Waiting doesn't count against the job's time,
        and it gives up the CPU turn meanwhile, so the other worker isn't held up."""
        q, start = self.q, time.monotonic()
        self.cpu.release()                             # nothing runs here while it waits
        resume = max(back, time.time()) + FREE_TOKENS_GRACE
        worker_cloud.set_out_choice(q.status_path, row["id"], "")
        self.current.update({"free_tokens_back": resume, "out_choice": ""})
        log.info("task %d: every provider in the switch order is out; waiting (asking: this PC, or wait until %s)",
                 row["id"], time.strftime("%H:%M", time.localtime(resume)))
        steps.info("Every provider is out of free tokens. Waiting: it carries on by itself at %s, or finish it "
                   "on this PC.", time.strftime("%I:%M %p", time.localtime(resume)).lstrip("0"))
        try:
            while True:
                if q.status_of(row["id"]) != "running":
                    raise Stopped("cancelled")
                choice = worker_cloud.out_choice(q.status_path, row["id"])
                if choice == "local":
                    log.info("task %d: finishing on this PC's model (her choice)", row["id"])
                    steps.info("Finishing on this PC's model, as you asked.")
                    result = "local"
                    break
                if choice == "wait" and self.current.get("out_choice") != "wait":
                    self.current["out_choice"] = "wait"
                    log.info("task %d: waiting for free tokens (her choice)", row["id"])
                    steps.info("Waiting for free tokens, as you asked.")
                if time.time() >= resume:
                    log.info("task %d: free tokens should be back; carrying on", row["id"])
                    steps.info("Free tokens should be back; carrying on.")
                    result = "retry"
                    break
                self.write_status("waiting for free tokens")
                time.sleep(2)
            while not self.cpu.try_acquire():          # back in turn for the CPU before going on
                if q.status_of(row["id"]) != "running":
                    raise Stopped("cancelled")
                other = self.cpu.holder()
                self.write_status("waiting for the CPU", {"firestarter": "Firestarter", "smithy": "The Smithy"}
                                  .get(other or "", other or "the other worker"))
                time.sleep(2)
            return result
        finally:
            deadline[0] += time.monotonic() - start
            self.current.pop("free_tokens_back", None)
            self.current.pop("out_choice", None)
            self.write_status("working")

    def run(self) -> None:
        n = self.q.recover_running()
        if n:
            log.info("%d task(s) left running last time: back in the queue", n)
        self.q.write_log()
        log.info("%s started: %s, models %s, CPU only", self.q.name, self.backend.name, self.cfg.get("models"))
        poll = float(self.cfg.get("poll_seconds", 5))
        idle_check = 0.0
        try:
            while True:
                self.check_games()
                if self.mode == "pause":
                    self.cpu.stop_waiting()
                    self.write_status("paused")
                    time.sleep(poll)
                    continue
                if not self.q.has_queued():
                    self.cpu.stop_waiting()
                    self.write_status("idle")
                    if self.profile.on_idle and time.monotonic() - idle_check >= 60:
                        idle_check = time.monotonic()
                        try:
                            self.profile.on_idle(self.load_cfg())    # the settings as they are now
                        except Exception as e:             # never stops the worker
                            log.warning("idle check failed: %s", e)
                    time.sleep(poll)
                    continue
                if not self.cpu.try_acquire():
                    # the other worker has a task on the CPU: wait for it to finish
                    other = self.cpu.holder()
                    self.write_status("waiting for the CPU", {"firestarter": "Firestarter", "smithy": "The Smithy"}
                                      .get(other or "", other or "the other worker"))
                    time.sleep(2)
                    continue
                try:
                    row = self.q.claim_next()
                    if row is not None:
                        self.step_file.task = row["id"]
                        try:
                            self.run_task(row)
                        finally:
                            self.step_file.task = None
                finally:
                    self.cpu.release()
        finally:
            self.cpu.release()
            self.backend.close()
            self.write_status("stopped")


# ---------------------------------------------------------------- command line
def main(profile: Profile, argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    q, reg = profile.queue, profile.registry
    if worker_cloud.command(argv, profile.config_path):   # the Providers window's commands
        return
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "out-choice":                             # a task waiting for free tokens: local | wait
        if len(argv) < 4 or argv[3] not in ("local", "wait") or not argv[2].isdigit():
            raise SystemExit("usage: out-choice TASK local|wait")
        worker_cloud.set_out_choice(q.status_path, int(argv[2]), argv[3])
        print("saved")
        return
    if cmd == "run":
        Worker(profile).run()
    elif cmd == "add":
        if len(argv) < 4 or argv[2] not in reg or profile.add_parse is None:
            raise SystemExit(profile.add_usage)
        kind, rest, extra = argv[2], list(argv[3:]), {}
        for opt in ("--provider", "--model"):           # a cloud provider for this task (worker_cloud.py)
            if opt in rest:
                i = rest.index(opt)
                extra[opt[2:]], rest = rest[i + 1].lower() if opt == "--provider" else rest[i + 1], rest[:i] + rest[i + 2:]
        try:
            raw = profile.add_parse(kind, rest)
            params = reg[kind].check(raw)
            if extra:
                worker_cloud.pick(worker_shared.load_json(profile.config_path), extra, kind)   # say no now, not later
                params.update(extra)
        except (ValueError, IndexError) as e:
            raise SystemExit(f"can't queue that: {e or profile.add_usage}")
        pid = params.get("parent_id")
        print(f"queued task {q.add(kind, params, requested_by='cli', title=reg[kind].title(params), parent_id=pid)}")
    elif cmd == "list":
        for r in q.list_jobs():
            print(f"{r['id']:>5}  {r['type']:<8} {r['status']:<10} {r['model'] or '':<18} "
                  f"{r['title'] or ''}  {r['error'] or r['artifact'] or r['result_path'] or ''}")
    elif cmd == "cancel":
        row = q.cancel(int(argv[2]) if len(argv) > 2 else None)
        print(f"stopped task {row['id']} ({row['type']})" if row else "no running or queued task")
    elif cmd == "types":
        for jt in reg.values():
            print(f"{jt.name:<10} {jt.tier:<9} {jt.description}")
    elif cmd == "models":
        cfg = worker_shared.load_json(profile.config_path)
        print(json.dumps({"backend": cfg.get("backend", "ollama"), "available": available_models(cfg),
                          "tiers": list(profile.tiers), **cfg.get("models", {})}))
    elif cmd == "set-model":
        if len(argv) < 4 or argv[2] not in profile.tiers:
            raise SystemExit("usage: set-model " + "|".join(profile.tiers) + " NAME")
        cfg = worker_shared.load_json(profile.config_path)
        cfg.setdefault("models", {})[argv[2]] = argv[3]
        profile.config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        print(f"{argv[2]} model set to {argv[3]} (used from the next task)")
    elif cmd == "log":
        q.write_log()
        print(q.log_path)
    else:  # status
        print(json.dumps({"running": q.running(), **q.read_status(), "counts": q.counts()}, indent=2))
