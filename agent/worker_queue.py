"""The workers' task queues (SQLite), the review gate, and their work logs.

One Queue per worker, each with its own files:
  Firestarter (site work): state\\worker.db, state\\worker-status.json, drafts\\worker\\, workers\\Firestarter.md
  The Smithy (coding):     state\\smithy.db, state\\smithy-status.json, drafts\\smithy\\, workers\\Smithy.md

Task states: queued -> running -> review (result waiting for the user) ->
kept / discarded; or failed / cancelled. A task that was running when the worker
stopped, or was paused for a game, goes back to queued.

Voice Othos may read results aloud and mark them kept or discarded through the
Yes/No gate (decide, using worker_shared.answer). Marks only: applying anything to
their files is something the user does on screen, never by voice.

The work log (workers\\<Name>.md, newest first) is rewritten from the queue after
every change, by whichever program made it (the worker, voice Othos or the command
line), so it's right even when Othos isn't running.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import worker_shared

HERE = Path(__file__).parent
STATE_DIR = HERE / "state"
LOGS_DIR = HERE / "workers"            # the work logs (git-ignored: personal)
STALE_SECONDS = 20                     # no heartbeat for this long = not running
LOG_ENTRIES = 200                      # newest tasks kept in the work log

_STATUS_WORD = {"queued": "queued", "running": "working", "review": "ready", "kept": "kept",
                "discarded": "discarded", "cancelled": "stopped", "failed": "failed"}


def _when(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts))) if ts else ""


class Queue:
    def __init__(self, key: str, name: str, spoken: str, db: str, status: str, results: str, log_name: str,
                 app: str):
        self.key = key                       # "firestarter" / "smithy"
        self.name = name                     # "Firestarter" / "The Smithy"
        self.spoken = spoken                 # how a task is named aloud: "Firestarter task 3", "Smithy task 3"
        self.db_path = STATE_DIR / db
        self.status_path = STATE_DIR / status
        self.results_dir = HERE / "drafts" / results
        self.log_path = LOGS_DIR / log_name
        self.app = app                       # the app that starts it ("Firestarter", "The Smithy")

    def __repr__(self):
        return f"Queue({self.key})"

    def connect(self) -> sqlite3.Connection:
        STATE_DIR.mkdir(exist_ok=True)
        con = sqlite3.connect(self.db_path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            params TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'queued',
            priority INTEGER NOT NULL DEFAULT 0,
            requested_by TEXT NOT NULL DEFAULT '',
            created REAL NOT NULL,
            started REAL, finished REAL,
            attempts INTEGER NOT NULL DEFAULT 0,
            model TEXT, result_path TEXT, error TEXT,
            decided REAL, decision_text TEXT)""")
        # added later; kept when an older queue is opened
        have = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
        for col, kind in (("title", "TEXT"), ("summary", "TEXT"), ("announced", "REAL"), ("parent_id", "INTEGER"),
                          ("artifact", "TEXT"), ("paused_for", "TEXT")):
            if col not in have:
                con.execute(f"ALTER TABLE jobs ADD COLUMN {col} {kind}")
        return con

    def _one(self, sql: str, args=()) -> sqlite3.Row | None:
        con = self.connect()
        try:
            return con.execute(sql, args).fetchone()
        finally:
            con.close()

    def _all(self, sql: str, args=()) -> list[sqlite3.Row]:
        con = self.connect()
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def _change(self, sql: str, args=()) -> int:
        con = self.connect()
        try:
            with con:
                n = con.execute(sql, args).rowcount
        finally:
            con.close()
        if n:
            self.write_log()
        return n

    # -- adding and reading
    def add(self, job_type: str, params: dict, requested_by: str = "cli", priority: int = 0,
            title: str = "", parent_id: int | None = None) -> int:
        con = self.connect()
        try:
            with con:
                cur = con.execute(
                    "INSERT INTO jobs (type, params, requested_by, priority, created, title, parent_id) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (job_type, json.dumps(params), requested_by, priority, time.time(), title, parent_id))
                job_id = cur.lastrowid
        finally:
            con.close()
        self.write_log()
        return job_id

    def get(self, job_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def by_status(self, *statuses: str, limit: int = 10) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        return self._all(f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY id DESC LIMIT ?", (*statuses, limit))

    def unannounced(self) -> list[sqlite3.Row]:
        """Finished results voice Othos hasn't told the user about yet (oldest first)."""
        return self._all("SELECT * FROM jobs WHERE status='review' AND announced IS NULL ORDER BY id")

    def results_to_report(self) -> int:
        """Finished voice-requested tasks voice Othos hasn't announced yet (the board's 'ready' light)."""
        return self._one("SELECT COUNT(*) FROM jobs WHERE status='review' AND announced IS NULL "
                         "AND requested_by='voice'")[0]

    def mark_announced(self, job_id: int) -> None:
        self._change("UPDATE jobs SET announced=? WHERE id=?", (time.time(), job_id))

    def list_jobs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))

    def counts(self) -> dict:
        return {r["status"]: r["n"] for r in self._all("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}

    def status_of(self, job_id: int) -> str | None:
        row = self._one("SELECT status FROM jobs WHERE id=?", (job_id,))
        return row["status"] if row else None

    def has_queued(self) -> bool:
        return self._one("SELECT 1 FROM jobs WHERE status='queued' LIMIT 1") is not None

    # -- the worker's side
    def claim_next(self) -> sqlite3.Row | None:
        """Take the next queued task (highest priority, then oldest) and mark it running."""
        con = self.connect()
        try:
            with con:
                row = con.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY priority DESC, id LIMIT 1").fetchone()
                if row is None:
                    return None
                n = con.execute("UPDATE jobs SET status='running', started=?, attempts=attempts+1, paused_for=NULL "
                                "WHERE id=? AND status='queued'", (time.time(), row["id"])).rowcount
                row = con.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone() if n else None
        finally:
            con.close()
        if row is not None:
            self.write_log()
        return row

    def requeue(self, job_id: int, front: bool = True, undo_attempt: bool = True, paused_for: str = "") -> None:
        """Back in the queue (paused for a game, or the worker stopped mid-task)."""
        self._change("UPDATE jobs SET status='queued', started=NULL, priority=priority+?, "
                     "attempts=MAX(attempts-?,0), paused_for=? WHERE id=? AND status='running'",
                     (1 if front else 0, 1 if undo_attempt else 0, paused_for or None, job_id))

    def finish(self, job_id: int, result_path: str, model: str, summary: str = "", artifact: str = "") -> None:
        self._change("UPDATE jobs SET status='review', finished=?, result_path=?, model=?, summary=?, artifact=?, error=NULL "
                     "WHERE id=? AND status='running'",
                     (time.time(), result_path, model, summary, artifact or None, job_id))

    def fail(self, job_id: int, error: str, retry: bool) -> None:
        self._change("UPDATE jobs SET status=?, error=?, finished=? WHERE id=? AND status='running'",
                     ("queued" if retry else "failed", error[:500], None if retry else time.time(), job_id))

    def cancel(self, job_id: int | None = None) -> sqlite3.Row | None:
        """Stop a task: the given one, else the running one, else the oldest queued one."""
        con = self.connect()
        try:
            with con:
                if job_id is None:
                    row = con.execute("SELECT * FROM jobs WHERE status IN ('running','queued') "
                                      "ORDER BY status='running' DESC, priority DESC, id LIMIT 1").fetchone()
                else:
                    row = con.execute("SELECT * FROM jobs WHERE id=? AND status IN ('running','queued')",
                                      (job_id,)).fetchone()
                if row is not None:
                    con.execute("UPDATE jobs SET status='cancelled', finished=? WHERE id=?", (time.time(), row["id"]))
        finally:
            con.close()
        if row is not None:
            self.write_log()
        return row

    def recover_running(self) -> int:
        """At worker start: tasks left 'running' by a stopped worker go back in the queue."""
        return self._change("UPDATE jobs SET status='queued', started=NULL, priority=priority+1 WHERE status='running'")

    # -- the review gate (the user's own words, checked in code)
    def decide(self, job_id: int, transcript: str, verdict: str) -> str | None:
        """Apply verdict ('kept' or 'discarded') only if the user's reply is a clear yes."""
        if verdict not in ("kept", "discarded") or worker_shared.answer(transcript) != "yes":
            return None
        n = self._change("UPDATE jobs SET status=?, decided=?, decision_text=? WHERE id=? AND status='review'",
                         (verdict, time.time(), (transcript or "")[:300], job_id))
        return verdict if n else None

    # -- is the worker running?
    def read_status(self) -> dict:
        try:
            return json.loads(self.status_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def running(self) -> bool:
        """True only while the worker program is alive and writing its heartbeat."""
        s = self.read_status()
        if not s or time.time() - float(s.get("updated_ts", 0)) > STALE_SECONDS:
            return False
        try:
            import psutil
            return psutil.pid_exists(int(s.get("pid", 0)))
        except Exception:
            return True

    # -- the work log
    def entry(self, r: sqlite3.Row) -> dict:
        """One task as the log shows it (also what voice Othos reads)."""
        status = _STATUS_WORD.get(r["status"], r["status"])
        if r["status"] == "queued" and r["paused_for"]:
            status = f"paused ({r['paused_for']})"
        where = r["artifact"] or r["result_path"] or ""
        return {"id": r["id"], "title": r["title"] or r["type"], "type": r["type"], "status": status,
                "asked": _when(r["created"]), "finished": _when(r["finished"]),
                "by": {"voice": "by voice", "cli": "from the command line"}.get(r["requested_by"], r["requested_by"]),
                "summary": (r["summary"] or "").strip(), "error": r["error"] or "",
                "where": str(Path(where).relative_to(HERE)) if where and Path(where).is_relative_to(HERE) else where}

    def write_log(self) -> None:
        try:
            rows = self._all("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (LOG_ENTRIES,))
            out = [f"# {self.name}: work log\n",
                   f"Newest first. Updated automatically by {self.name}, voice Othos and the command line; "
                   "don't edit (it's rewritten from the queue). Results wait for your review; nothing is "
                   "applied or run.\n"]
            for r in rows:
                e = self.entry(r)
                out.append(f"## Task {e['id']}: {e['title']}\n")
                out.append(f"- **Status:** {e['status']}")
                out.append(f"- **Asked:** {e['asked']} ({e['by']})" if e["by"] else f"- **Asked:** {e['asked']}")
                if e["finished"]:
                    out.append(f"- **Finished:** {e['finished']}")
                if e["where"]:
                    out.append(f"- **Result:** `{e['where']}`")
                if e["status"] == "failed" and e["error"]:
                    out.append(f"- **Why it failed:** {e['error']}")
                if e["summary"]:
                    out.append(f"\n{e['summary']}")
                out.append("")
            LOGS_DIR.mkdir(exist_ok=True)
            text = "\n".join(out).rstrip() + "\n"
            tmp = self.log_path.with_suffix(f".{time.time_ns()}.tmp")
            tmp.write_text(text, encoding="utf-8")
            for attempt in range(5):
                try:
                    tmp.replace(self.log_path)
                    return
                except PermissionError:      # open for reading somewhere: retry, then write in place
                    time.sleep(0.05 * (attempt + 1))
            self.log_path.write_text(text, encoding="utf-8")
            tmp.unlink(missing_ok=True)
        except Exception:
            pass                             # the log must never stop a task


FIRESTARTER = Queue("firestarter", "Firestarter", "Firestarter task", "worker.db", "worker-status.json",
                    "worker", "Firestarter.md", "Firestarter")
SMITHY = Queue("smithy", "The Smithy", "Smithy task", "smithy.db", "smithy-status.json",
               "smithy", "Smithy.md", "The Smithy")
QUEUES = {"firestarter": FIRESTARTER, "smithy": SMITHY}
