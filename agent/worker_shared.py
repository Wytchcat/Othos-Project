"""What Firestarter and the Smithy share, in one place, so a safety fix reaches both.

- RULES: what every worker model is told (it only writes text; nothing is sent,
  published, run or applied; results wait for the user's review).
- answer(): the Yes/No gate for keep/discard, checked in code against the user's own
  words from the real transcript (never decided by a model or a prompt).
- redact(): what may be sent to a model (key-like strings removed).
- load_json(): settings files, read with or without a BOM (Notepad++ / PowerShell).
- game_mode(): pause or hold back while a game runs (gaming.json, same as voice Othos).
- CpuLock: the two workers take turns on the CPU when both have work. Alone, a
  worker gets the full thread count; they never run tasks side by side.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import gaming
import personal

HERE = Path(__file__).parent
STATE_DIR = HERE / "state"
USER = personal.name() or "the user"  # personal.json's name, for the prompts
USERS = f"{USER}'s"

RULES = ("You cannot run code, send, publish, install or change anything, and you can't see any file "
         "you aren't shown: you only write text. Everything you write is saved as a draft and waits for "
         f"{USERS} review; they apply anything themselves, on screen. Don't invent facts, features or "
         "file paths; say when you are unsure.")


# ---------------------------------------------------------------- the Yes/No gate
_YES = re.compile(r"^\W*(?:(?:othos|osos)\W+)?(?:yes|yeah|yep|yup|keep it|discard it|do it|go ahead|ok(?:ay)?|sure|please do)\b", re.I)
_NO = re.compile(r"\b(?:no|nope|don't|do not|cancel|never mind|wait|stop)\b", re.I)


def answer(transcript: str) -> str | None:
    """'yes', 'no', or None for the user's reply to a keep/discard question. Any 'no' word wins."""
    text = (transcript or "").strip()
    if _NO.search(text):
        return "no"
    return "yes" if _YES.search(text) else None


# ---------------------------------------------------------------- what may go to a model
# key-like strings: provider prefixes, long random tokens, and NAME=value lines for keys/secrets
_REDACT = [
    re.compile(r"\b(?:sk|pk|rk|tvly|ghp|gho|ghs|github_pat|xox[abpr]|AKIA|AIza|hf)[-_][A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"(?i)\b([A-Z0-9_]*(?:API_?KEY|_KEY|KEY_|SECRET|TOKEN|PASSWORD|PASSWD|PASS)[A-Z0-9_]*)\s*[:=]\s*['\"]?[^\s'\"#]{6,}['\"]?"),
    re.compile(r"\b[A-Za-z0-9+/_\-]{40,}={0,2}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]


def redact(text: str) -> str:
    for rx in _REDACT:
        text = rx.sub(lambda m: (m.group(1) + "=[REDACTED]") if m.re.groups else "[REDACTED]", text)
    return text


# ---------------------------------------------------------------- settings
def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


# ---------------------------------------------------------------- games
def game_mode(cfg: dict, gcfg: gaming.Config) -> tuple[str, dict, str]:
    """('normal'|'pause'|'throttle', settings, why)."""
    override = gaming.read_override()
    game = None if override == "off" else gaming.detect(gcfg)
    if game:
        profile, why = game.profile, game.name
    elif override == "on":
        profile, why = gcfg.override_profile, "gaming mode turned on by hand"
    else:
        return "normal", {}, ""
    s = cfg.get("gaming", {}).get(profile, {"action": "pause"})
    return s.get("action", "pause"), s, f"{why} ({profile})"


# ---------------------------------------------------------------- taking turns on the CPU
def _alive(pid) -> bool:
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except Exception:
        return True


class CpuLock:
    """One task at a time across both workers (state\\cpu-lock.json).

    A worker holds it for one task. When the other worker is waiting, the one that
    just finished lets it go first, so neither can starve the other. A lock left by
    a worker that has stopped (its process is gone) is ignored.
    """
    PATH = STATE_DIR / "cpu-lock.json"
    WAITING_FRESH = 15          # a "waiting" note older than this is ignored

    def __init__(self, me: str):
        self.me = me
        self.held = False

    def _read(self) -> dict:
        try:
            return json.loads(self.PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write(self, d: dict) -> None:
        STATE_DIR.mkdir(exist_ok=True)
        tmp = self.PATH.with_suffix(f".{self.me}.tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        for attempt in range(10):
            try:
                tmp.replace(self.PATH)
                return
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
        self.PATH.write_text(json.dumps(d), encoding="utf-8")

    def _owner_alive(self, d: dict) -> bool:
        return bool(d.get("owner")) and _alive(d.get("pid", 0))

    def holder(self) -> str | None:
        """Which worker has the CPU now (None = free)."""
        d = self._read()
        return d.get("owner") if self._owner_alive(d) else None

    def try_acquire(self) -> bool:
        """Take the CPU if it's free and it's our turn. Call again until True."""
        # a small file lock around the check-and-set, so both can't take it at once
        guard = STATE_DIR / "cpu-lock.guard"
        STATE_DIR.mkdir(exist_ok=True)
        try:
            fd = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:   # a guard left by a crash
                if time.time() - guard.stat().st_mtime > 10:
                    guard.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        try:
            os.close(fd)
            d = self._read()
            waiting = d.get("waiting", {})
            others_waiting = [w for w, ts in waiting.items()
                              if w != self.me and time.time() - float(ts) < self.WAITING_FRESH]
            if self._owner_alive(d) and d.get("owner") != self.me:
                waiting[self.me] = time.time()
                self._write({**d, "waiting": waiting})
                return False
            if others_waiting and d.get("last_owner") == self.me:
                # we just had a turn and the other is waiting: let it go first
                waiting[self.me] = time.time()
                self._write({**d, "owner": None, "pid": None, "waiting": waiting})
                return False
            waiting.pop(self.me, None)
            self._write({"owner": self.me, "pid": os.getpid(), "since": time.time(),
                         "last_owner": d.get("last_owner"), "waiting": waiting})
            self.held = True
            return True
        finally:
            guard.unlink(missing_ok=True)

    def release(self) -> None:
        if not self.held:
            return
        self.held = False
        d = self._read()
        if d.get("owner") == self.me:
            self._write({**d, "owner": None, "pid": None, "last_owner": self.me})

    def stop_waiting(self) -> None:
        """No task queued any more: take our 'waiting' note away."""
        d = self._read()
        if self.me in d.get("waiting", {}):
            d["waiting"].pop(self.me, None)
            self._write(d)
