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
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

import gaming
import personal

# The shape an answer must have (a JSON schema), for the calls made inside `with answer_shape(schema):`. Ollama,
# llama-server and the cloud providers that support it hold the model to it; others answer as before, so the
# caller still reads a plain answer (Nov 16, checkers plan phase 2: weak models answered "no edits" or broke the
# edit format).
ANSWER_SCHEMA: ContextVar = ContextVar("answer_schema", default=None)


@contextmanager
def answer_shape(schema: dict | None):
    token = ANSWER_SCHEMA.set(schema)
    try:
        yield
    finally:
        ANSWER_SCHEMA.reset(token)


# The smallest model (billions of parameters, from its name: "gpt-oss-120b" = 120) that may take the calls made
# inside `with strong_models(70):`. Switching skips smaller cloud models and this PC's model for them, and says so
# (worker_cloud.NoStrongModel) instead of falling to one; a model whose name gives no size counts as strong
# (Nov 16, task 56: a whole-file rewrite fell to NVIDIA's gpt-oss-20b, which kept the wrong test). 0 = any.
MIN_MODEL_B: ContextVar = ContextVar("min_model_b", default=0)


@contextmanager
def strong_models(min_b: float):
    token = MIN_MODEL_B.set(float(min_b or 0))
    try:
        yield
    finally:
        MIN_MODEL_B.reset(token)


HERE = Path(__file__).parent
STATE_DIR = HERE / "state"
USER = personal.name() or "the user"  # personal.json's name, for the prompts
USERS = f"{USER}'s"

RULES = ("You cannot run code, send, publish, install or change anything, and you can't see any file "
         "you aren't shown: you only write text. Everything you write is saved as a draft and waits for "
         f"{USERS} review; they apply anything themselves, on screen. Don't invent facts, features or "
         "file paths; say when you are unsure.")


# ---------------------------------------------------------------- the Yes/No gate
_YES = re.compile(r"^\W*(?:(?:othos|osos)\W+)?(?:yes|yeah|yep|yup|do it|go ahead|ok(?:ay)?|sure|please do)\b", re.I)
_NO = re.compile(r"\b(?:no|nope|don't|do not|cancel|never mind|wait|stop)\b", re.I)
# "keep it" / "discard it" are a yes only to the question that asked for that action (Nov 18; the Nov 13
# review: both were a yes to any question, so "discard it" to "Keep task 12?" kept it, and to a plan built it)
_SAYS = {"kept": re.compile(r"^\W*(?:(?:othos|osos)\W+)?(?:(?:yes|yeah|yep|ok(?:ay)?)\W+)?keep (?:it|that|them)\b", re.I),
         "discarded": re.compile(r"^\W*(?:(?:othos|osos)\W+)?(?:(?:yes|yeah|yep|ok(?:ay)?)\W+)?(?:discard|delete|bin|scrap) (?:it|that|them)\b", re.I)}
_OTHER = {"kept": re.compile(r"\b(?:discard|delete|bin|scrap|throw\w* (?:it )?away|get rid)", re.I),
          "discarded": re.compile(r"\b(?:keep|kept|save)\b", re.I)}


def answer(transcript: str, verdict: str | None = None) -> str | None:
    """'yes', 'no', or None for the user's reply to a yes/no question. Any 'no' word wins.
    verdict ('kept' or 'discarded') is what a keep/discard question asked: its own words ("keep it" to a keep
    question) are a yes, and a reply naming the other action ("yes, discard it" to "Keep task 12?") is None,
    so it's asked again. Without a verdict, "keep it" and "discard it" are None."""
    text = (transcript or "").strip()
    if _NO.search(text):
        return "no"
    if verdict in _OTHER and _OTHER[verdict].search(text):
        return None
    if verdict in _SAYS and _SAYS[verdict].search(text):
        return "yes"
    return "yes" if _YES.search(text) else None


# ---------------------------------------------------------------- what may go to a model
# key-like strings: provider prefixes, long random tokens, and NAME=value lines for keys/secrets
_REDACT = [
    # WordPress wp-config.php: define( 'DB_PASSWORD', '...' ), define('AUTH_KEY', '...'), the salts. The
    # whole second argument, whatever it holds (salts have spaces and symbols); Firestarter, Nov 14.
    re.compile(r"(?i)\bdefine\s*\(\s*(['\"][A-Z0-9_]*(?:PASSWORD|PASS|SECRET|KEY|SALT|TOKEN)[A-Z0-9_]*['\"]\s*,\s*)"
               r"(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"),
    re.compile(r"\b(?:sk|pk|rk|tvly|ghp|gho|ghs|github_pat|xox[abpr]|AKIA|AIza|hf)[-_][A-Za-z0-9_\-]{12,}\b"),
    # an AWS access key ID: AKIA (or ASIA, a temporary one) and 16 capitals or digits, with no dash after it,
    # so the rule above missed every real one (Nov 13 review, item 7; fixed Nov 18)
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    # NAME = value, where the value isn't code: a call, index or attribute after it (`total_tokens =
    # sum(...)`, `passed = data.get(...)`) is left alone (Nov 14, Smithy task 39: the model saw
    # `total_tokens=[REDACTED]`, called it placeholder code, and its edits couldn't find the real lines)
    # The name, the = or : and the value stay on one line (task 40: `if tokens > max_tokens:` took the
    # colon as a separator and hid the next line).
    # passed, passing, passes aren't password names (task 41: {"passed": passed} was hidden)
    re.compile(r"(?i)\b([A-Z0-9_]*(?:API_?KEY|_KEY|KEY_|SECRET|TOKEN|PASSWORD|PASSWD|PASS(?!ED|ING|ES))[A-Z0-9_]*)['\"]?([ \t]*[:=][ \t]*)['\"]?"
               r"([^\s'\"#()\[\]{},]{6,})(?![\w(\[.])['\"]?"),
    # a WordPress application password, "abcd EFGH ijkl MNOP qrst UVWX", after a PASSWORD / PASS /
    # SECRET name (the spaces stopped the rule above; Firestarter, Nov 14). The name has to be there,
    # so a sentence of 4-letter words is left alone.
    re.compile(r"(?i)\b([A-Z0-9_]*(?:PASSWORD|PASSWD|PASS(?!ED|ING|ES)|SECRET)[A-Z0-9_]*)['\"]?[ \t]*[:=][ \t]*['\"]?"
               r"[A-Za-z0-9]{4}(?: [A-Za-z0-9]{4}){3,7}\b['\"]?"),
    # long random-looking runs; a long snake_case name (`test_disallowed_filenames_and_key_redaction`) isn't one
    re.compile(r"\b(?![a-z]+(?:_[a-z]+)+\b)[A-Za-z0-9+/_\-]{40,}={0,2}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PW_NAME = re.compile(r"(?i)PASS|SECRET")


def _hide(m: re.Match) -> str:
    if not m.re.groups:
        return "[REDACTED]"
    if m.re is _REDACT[0]:                       # define('NAME', '...'): keep the call, hide the value
        return m.group(0)[:m.end(1) - m.start()] + "'[REDACTED]'"
    if m.re.groups >= 3:
        name, sep, value = m.group(1), m.group(2), m.group(3)
        # Python code, not a secret: a lowercase name, " = " with spaces, and a bare name as the value
        # (`max_tokens = tokens`; unquoted, a real secret wouldn't be valid Python). .env (NAME=value),
        # YAML (name: value), JSON and quoted values are still hidden.
        # Never for a password or secret name: in an .ini file `password = sunshinesky` is the real thing
        # (Firestarter, Nov 14: d2f2d4c showed it). Only TOKEN / KEY names (`max_tokens = tokens`).
        if name != name.upper() and not _PW_NAME.search(name) and sep.strip() == "=" \
                and sep.startswith((" ", "\t")) \
                and sep.endswith((" ", "\t")) and _IDENT.fullmatch(value) and m.group(0).endswith(value):
            return m.group(0)
    return m.group(1) + "=[REDACTED]"


def redact(text: str) -> str:
    for rx in _REDACT:
        text = rx.sub(_hide, text)
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
