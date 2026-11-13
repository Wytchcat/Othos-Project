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


def save_artifact(results_dir: Path, job_id: int, kind: str, full: str) -> Path | None:
    """A fix's diff or a script as its own draft file (never applied or run)."""
    blocks = re.findall(r"```([\w#+.-]*)[^\n]*\n([\s\S]*?)```", full)
    if not kind or not blocks:
        return None
    if kind == "diff":
        lang, code = next(((l, c) for l, c in blocks if l.lower() in ("diff", "patch")), blocks[0])
        path = results_dir / f"{job_id:05d}-fix.diff"
    else:
        lang, code = blocks[0]
        path = results_dir / f"{job_id:05d}-script.{EXT.get(lang.lower(), 'txt')}"
    path.write_text(code, encoding="utf-8")
    return path


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
                "paused_task": self.paused_task if state == "paused" else None,
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
        try:
            cloud = worker_cloud.pick(self.cfg, json.loads(row["params"] or "{}"), row["type"])
        except ValueError as e:
            q.fail(row["id"], str(e), retry=False)
            log.info("task %d not started: %s", row["id"], e)
            return
        backend = cloud[0] if cloud else self.backend
        if cloud:
            model = cloud[1] or "the loaded model"
            log.info("task %d uses %s", row["id"], backend.name)
        start_mode = (self.mode, self.mode_why)
        title = row["title"] or jt.title(params)
        self.current = {"id": row["id"], "type": row["type"], "title": title, "model": model,
                        "threads": threads, "started": time.time(), "progress": 0.0,
                        "requested_by": row["requested_by"], "coding": jt.coding}
        log.info("task %d (%s): %s, %d threads%s", row["id"], row["type"], model, threads,
                 f", held back for {self.mode_why}" if held else "")
        self.write_status("held back" if held else "working")
        deadline = time.monotonic() + 60 * float(self.cfg.get(jt.minutes_key, self.cfg.get("max_job_minutes", 30)))
        last_check = [0.0]

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
            if time.monotonic() > deadline:
                raise Stopped("timeout")
            self.write_status("held back" if held else "working")

        t0 = time.monotonic()
        artifact = None
        try:
            if jt.run:
                summary, full, artifact = jt.run(TaskContext(self, row, model, threads, should_stop, backend), params)
            else:
                summary, full = split_result(backend.chat(model, jt.build(params), threads, jt.max_tokens, should_stop))
        except Stopped as s:
            if s.reason == "cancelled":
                log.info("task %d stopped (cancelled)", row["id"])
            elif s.reason == "games":
                q.requeue(row["id"], paused_for=self.mode_why or "")
                self.paused_task = {"id": row["id"], "title": title, "game": self.mode_why}
                log.info("task %d back to the front of the queue (%s)", row["id"], self.mode_why or "game closed")
            else:
                q.fail(row["id"], f"took longer than {jt.minutes_key}", retry=False)
            return
        except Exception as e:
            retry = row["attempts"] < int(self.cfg.get("max_attempts", 2))
            q.fail(row["id"], str(e), retry=retry)
            log.warning("task %d failed (%s)%s", row["id"], e, ", will retry" if retry else "")
            time.sleep(5)
            return
        finally:
            self.current = None
        if copied_back(params.get("path"), full):
            # small models sometimes describe the file and paste it back instead of reviewing it
            summary = ("The model mostly copied the file instead of reviewing it; a bigger coding "
                       "model should do better. " + summary)
            log.info("task %d: the result mostly repeats the file", row["id"])
        q.results_dir.mkdir(parents=True, exist_ok=True)
        path = q.results_dir / f"{row['id']:05d}-{row['type']}.md"
        if artifact is None:
            artifact = save_artifact(q.results_dir, row["id"], jt.artifact, full)
        where = ""
        if artifact:
            where = (f"Built in its own folder (not installed or run): `{Path(artifact)}`\n\n" if Path(artifact).is_dir()
                     else f"Saved separately (not applied): `{Path(artifact).name}`\n\n")
        path.write_text(
            f"<!-- {q.name} task {row['id']} ({row['type']}): {title}. {model}, {threads} threads, "
            f"{time.monotonic() - t0:.0f} s, {time.strftime('%Y-%m-%d %H:%M')}. Waiting for review; "
            f"nothing has been applied. -->\n\n# {q.spoken.capitalize()} {row['id']}: {title}\n\n"
            f"## Summary\n\n{summary}\n\n{where}## Full result\n\n{full}\n", encoding="utf-8")
        q.finish(row["id"], str(path), model, summary, str(artifact) if artifact else "")
        self.paused_task = None if (self.paused_task or {}).get("id") == row["id"] else self.paused_task
        log.info("task %d done in %.0f s -> %s (waiting for review)", row["id"], time.monotonic() - t0, path.name)

    def run(self) -> None:
        n = self.q.recover_running()
        if n:
            log.info("%d task(s) left running last time: back in the queue", n)
        self.q.write_log()
        log.info("%s started: %s, models %s, CPU only", self.q.name, self.backend.name, self.cfg.get("models"))
        poll = float(self.cfg.get("poll_seconds", 5))
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
                        self.run_task(row)
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
