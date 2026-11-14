"""What voice Othos may do with Firestarter (site work) and the Smithy (coding), recognised in code.

Allowed (the user, Nov 11-12): start and stop tasks (only while that worker runs);
list them; read results aloud (summary, then the rest in short pieces, code
described rather than spelled out); say how a task is coming; open a result
(Notepad++) or a build's folder (Explorer); queue a follow-up; mark a result kept
or discarded through the Yes/No gate; read or open each worker's work log. Marks
only: applying anything to their files is something the user does on screen, never by voice.

Routing: coding requests (review, fix, script, build, Joomla) go to the Smithy,
site requests to Firestarter. Task numbers are per worker: "read me Smithy task 3",
"keep Firestarter task 5"; a plain "task 3" works when only one worker has a task 3.

The "it's ready" announcer: between turns (never mid-reply) Othos says "Smithy task 3,
your gaming.py review, is ready" and the summary; results that finished while the user
was away are mentioned at the start of the next call.

Task Mode (the user's idea, Nov 13): "task mode on" and Othos reads each step of the
workers' tasks aloud as it happens ("Planning the build.", "Cerebras asked to slow down:
waiting 60 seconds.", "OthosSetup.cs: 10 edits in."), between turns, from the same steps file
the app's Progress window shows (worker_engine.StepFile). "Task mode off" stops it.

The how-to recorder (howto_recorder.py) is checked first: "start a how-to", then
every turn is a step with a picture and Othos says nothing, until "finish the
how-to", which queues Firestarter's write-up (when Firestarter is installed and open).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
from pathlib import Path

import howto_recorder
import smithy_jobs
try:                                             # the Smithy's check task: left out of the shared copy
    import smithy_rea
except ImportError:
    smithy_rea = None
import worker_cloud
import worker_engine
import worker_files
import worker_shared
from worker_queue import FIRESTARTER, QUEUES, SMITHY, Queue

# Firestarter (site work) is optional: a copy of Othos without worker.py /
# worker_jobs.py has only the Smithy, and Firestarter requests say so.
try:
    import worker_jobs
    HAS_FIRESTARTER = (Path(__file__).parent / "worker.py").exists()
except ImportError:
    worker_jobs = None
    HAS_FIRESTARTER = False
INSTALLED = [SMITHY, FIRESTARTER] if HAS_FIRESTARTER else [SMITHY]
NO_FIRESTARTER = "Firestarter isn't installed in this copy of Othos; only the Smithy is."

log = logging.getLogger("othos")
NOTEPAD_PP = r"C:\Program Files\Notepad++\notepad++.exe"
REGISTRIES = {"smithy": smithy_jobs.REGISTRY}
if HAS_FIRESTARTER:
    REGISTRIES["firestarter"] = worker_jobs.REGISTRY
CODING = {"review", "fix", "script", "build", "joomla", "check"}   # the Smithy's
SITE = {"test", "article", "zoo"}                            # Firestarter's

_NUMWORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split())}
_N = r"(\d{1,5}|" + "|".join(_NUMWORDS) + r")"


def _num(s: str) -> int:
    return int(s) if s.isdigit() else _NUMWORDS[s.lower()]


# ---------------------------------------------------------------- which worker the user means
# Speech-to-text spellings of "Smithy" ("Smithee" heard on the Nov 12 live call).
_SMITHY_W = r"smith(?:y|ie|ey|ee)|smitt(?:y|ie|ee)"
_FIRE_W = r"fire\s*starter"
_WORKER_WORD = re.compile(rf"\b(?:the\s+)?(?:(?P<s>{_SMITHY_W})|(?P<f>{_FIRE_W}))(?:'s|s)?\b", re.I)


def _who(text: str) -> tuple[str | None, str]:
    """('smithy' | 'firestarter' | None, the text with the worker's name taken out)."""
    m = _WORKER_WORD.search(text)
    if not m:
        return None, text
    return ("smithy" if m.group("s") else "firestarter"), _WORKER_WORD.sub(" ", text)


# ---------------------------------------------------------------- phrases (worker names already taken out)
# "Othos, ask the Smithy to..." is often heard as "Othos asked the Smithy to...", so
# "asked" counts too, but only at the start of the turn or straight after "Othos":
# "I asked the Smithy to..." in ordinary talk doesn't start a task.
# "to" may be left out when a job word follows: "have the Smithy write a script...".
_JOB_WORD = r"(?:review|fix|write|build|make|draft|create)\b"
_DO = re.compile(rf"(?:\b(?:ask|have|get|tell)|(?:^\W*|\bothos\W+)asked)\s+(?:the\s+)?(?:{_SMITHY_W}|{_FIRE_W})"
                 rf"\s+(?:to\s+|(?={_JOB_WORD}))(?P<rest>.+)", re.I | re.S)
_TASK_START = re.compile(
    r"\b(?:start|run|begin|queue|add)\s+(?:(?:a|the|new|another)\s+){0,2}(?:worker\s+|coding\s+)?task\b"
    r"(?:\s*[:,-]?\s*(?P<rest>.*))?", re.I | re.S)
_TASK_STOP = re.compile(
    r"\b(?:stop|cancel|end|halt|kill)\s+(?:(?:the|that|this|current|my)\s+){0,2}(?:worker\s+)?task\b"
    rf"(?:\s+(?:number\s+)?{_N})?", re.I)
_LIST_DONE = re.compile(
    r"\bwhat\s+(?:tasks?|results?|jobs?)\s+(?:are|is|have)\s+(?:been\s+)?(?:done|finished|ready|completed)|"
    r"\bwhat'?s\s+(?:done|finished|ready)\b|\bwhich\s+tasks?\s+(?:are|is)\s+(?:done|finished|ready)|"
    r"\bany\s+(?:tasks?|results?)\s+(?:done|ready|finished)", re.I)
_QUEUE = re.compile(
    r"\bwhat'?s\s+in\s+the\s+queue|\bwhat\s+is\s+in\s+the\s+queue|\bwhat'?s\s+queued|"
    r"\bwhat\s+tasks?\s+(?:are|is)\s+(?:queued|waiting|running)|\bwhat'?s\s+(?:the\s+worker\s+)?doing|"
    r"\bwhat\s+(?:are|is)\s+(?:the\s+workers?\s+)?(?:doing|working\s+on)", re.I)
_PROGRESS = re.compile(
    rf"\bhow'?s\s+task\s+(?:number\s+)?{_N}\s+(?:coming|going|doing)|\bhow\s+is\s+task\s+(?:number\s+)?{_N}|"
    rf"\bstatus\s+of\s+task\s+(?:number\s+)?{_N}|\bis\s+task\s+(?:number\s+)?{_N}\s+(?:done|finished|ready)", re.I)
# "give me an update", "what's the status?", "how's it going?", "anything ready?",
# "the Smithy has a review ready"
_UPDATE = re.compile(
    r"\b(?:updates?|status|progress|how(?:'?s|\s+is|\s+are)\s+(?:it|that|things|everything|the\s+\w+)\s+(?:going|coming(?:\s+along)?)|"
    r"anything\s+(?:ready|done|finished|new)|"
    r"(?:has|have|got)\s+(?:a\s+|an\s+|any\s+|the\s+)?(?:\w+\s+)?(?:reviews?|results?|tasks?|fix(?:es)?|scripts?|builds?)"
    r"\s+(?:ready|done|finished|waiting))\b", re.I)
# The worker itself, by name: "the Smithy", "Smithy's", "Firestarter". Not plurals
# ("smithies") and not "a fire starter" (an ordinary thing), so casual talk isn't
# answered with a status.
_NAMED = re.compile(r"(?<!\ba )(?<!\ban )\b(?:smith(?:y|ey|ee)|smitt(?:y|ie|ee)|fire\s*starter)\b(?!s\b)", re.I)
# ...and the sentence asks something
_ASKS = re.compile(r"\?|\b(?:update|status|progress|doing|working|ready|done|finished|how|what|any|news|tell\s+me)\b", re.I)
_PAUSED = re.compile(r"\bis\s+any(?:thing|\s+task)\s+paused|\bis\s+(?:the\s+worker\s+)?paused|\bare\s+(?:the\s+)?workers\s+paused", re.I)
# Which result the user means: "task 3", "result 3", and the natural ways round it, "the
# results of task 3", "the review for task 3", "task 3's results" (Nov 12 call: "read
# the results of task two aloud" matched nothing and went to web search).
_OBJ = (r"(?:(?:the\s+)?(?:results?|answer|review|script|fix|build|draft|summary|output)\s+(?:of|for|from|on)\s+)?"
        rf"(?:the\s+)?(?:task|result)\s+(?:number\s+)?{_N}")
_READ = re.compile(rf"\bread\s+(?:me\s+|out\s+|aloud\s+)?{_OBJ}", re.I)
_REST = re.compile(r"^\W*(?:(?:okay|ok|yes|please|and)\W+)?(?:read\s+(?:me\s+)?the\s+rest|go\s+on|keep\s+going|continue|"
                   r"carry\s+on|more|next(?:\s+part)?|what\s+else)\b", re.I)
_SHOW = re.compile(rf"\b(?:show|open)\s+(?:me\s+|up\s+)?{_OBJ}", re.I)
_ASK = re.compile(rf"\bask\s+(?:about|on)\s+task\s+(?:number\s+)?{_N}\W*(?P<q>.+)", re.I | re.S)
_MARK = re.compile(rf"\b(?P<verb>keep|discard|throw\s+away|bin)\s+{_OBJ}", re.I)
# A turn about an existing "task N" that matched nothing else gets a hint from code,
# never web search or the model. Only with the worker named or a result-ish word, so
# "I finished task two of my homework" is left alone.
_TASK_N = re.compile(rf"\btask\s+(?:number\s+)?{_N}\b", re.I)
_TASK_WORDS = re.compile(r"\b(?:read|show|open|keep|discard|results?|review|script|aloud|out\s+loud|say|tell\s+me)\b", re.I)
_LOG_READ = re.compile(r"\bread\s+(?:me\s+)?(?:the\s+)?(?:work\s+)?log\b|\bwhat'?s\s+in\s+(?:the\s+)?(?:work\s+)?log\b|"
                       r"\bwhat\s+is\s+in\s+(?:the\s+)?(?:work\s+)?log\b", re.I)
_LOG_SHOW = re.compile(r"\b(?:show|open)\s+(?:me\s+)?(?:the\s+)?(?:work\s+)?log\b", re.I)


def _first(m) -> int:
    return _num(next(g for g in m.groups() if g and (g.isdigit() or g.lower() in _NUMWORDS)))


# ---------------------------------------------------------------- reading aloud
_LANG = {"py": "Python", "python": "Python", "ps1": "PowerShell", "powershell": "PowerShell", "pwsh": "PowerShell",
         "diff": "diff", "patch": "diff", "js": "JavaScript", "javascript": "JavaScript", "ts": "TypeScript",
         "typescript": "TypeScript", "tsx": "TypeScript", "cs": "C sharp", "csharp": "C sharp", "json": "JSON",
         "bash": "shell", "sh": "shell", "bat": "batch", "html": "HTML", "css": "CSS", "php": "PHP", "sql": "SQL"}


def _describe_code(m: re.Match) -> str:
    lang = (m.group(1) or "").lower()
    lines = len([l for l in m.group(2).splitlines() if l.strip()])
    name = _LANG.get(lang, lang.upper() if lang else "")
    if lang in ("diff", "patch"):
        what = f"a {lines}-line diff"
    elif lang in ("ps1", "powershell", "pwsh", "bash", "sh", "bat"):
        what = f"a {lines}-line {name} script"
    else:
        what = f"{lines} lines of {name + ' ' if name else ''}code"
    return f" (Here there's {what}; it's in the drafts on screen.) "


def speakable(full: str) -> str:
    t = re.sub(r"```([\w#+.-]*)[^\n]*\n([\s\S]*?)(?:```|$)", _describe_code, full)
    t = re.sub(r"`?\b[A-Za-z]:\\[^\s`]*`?", "the folder on screen", t)   # full Windows paths aren't read out
    t = re.sub(r"`([^`]{1,80})`", r"\1", t)          # short inline code (names) read as words
    t = re.sub(r"`[^`]*`", " (see the drafts) ", t)  # long snippets aren't spelled out
    t = re.sub(r"^\s*#+\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", t, flags=re.M)
    t = re.sub(r"[*_>|\[\]{}<>~^\\]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def pieces(full: str, words: int = 60) -> list[str]:
    out, cur = [], []
    for sentence in re.split(r"(?<=[.!?)])\s+", speakable(full)):
        cur.append(sentence)
        if len(" ".join(cur).split()) >= words:
            out.append(" ".join(cur)); cur = []
    if cur:
        out.append(" ".join(cur))
    return out


def _full_result(job) -> str:
    try:
        text = Path(job["result_path"]).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return text.split("## Full result", 1)[1] if "## Full result" in text else text


def _summary(job) -> str:
    """The stored spoken summary, made safe to read aloud (lists and symbols cleaned)."""
    s = job["summary"] or ""
    s = re.sub(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+(.*?)[.;:]?\s*$", r"\1.", s)
    return speakable(s)


def _title(q: Queue, job) -> str:
    if job["title"]:
        return job["title"]
    jt = REGISTRIES[q.key].get(job["type"])
    try:
        return jt.title(json.loads(job["params"] or "{}")) if jt else job["type"]
    except Exception:
        return job["type"]


def _name(q: Queue, job_id: int) -> str:
    return f"{q.spoken} {job_id}"                     # "Smithy task 3", "Firestarter task 5"


def _not_running(q: Queue) -> str:
    return f"{q.name} isn't running. Open {q.app} first."


_KIND_WORDS = [
    # Firestarter's first, so "write an article for my Joomla site" isn't a Joomla override
    # ("page" only as "a page about ...": "make a small web page" / "create a login page" are Smithy builds)
    ("article", re.compile(r"^(?:write|draft|make|create|do)\s+(?:me\s+)?(?!.*\bscript\b)(?:[\w'-]+\s+){0,3}?"
                           r"(?:articles?\b|blog\s+posts?\b|posts?\b(?![-\w])(?!\s+requests?\b)|"
                           r"pages?\s+(?:about|on|for|describing|explaining)\b)", re.I)),
    ("zoo", re.compile(r"^(?:add|make|create|draft|write|prepare|do)\s+(?:me\s+)?(?:[\w'-]+\s+){0,3}?zoo\s+items?\b", re.I)),
    ("joomla", re.compile(r"\b(?:joomla|yoo\s*theme|template\s+override|layout\s+override|override)\b", re.I)),
    ("script", re.compile(r"^(?:write|make|create)\s+(?:me\s+)?(?:a|an)\s+(?:\w+\s+){0,2}script\b|^script\b", re.I)),
    # a program (.exe / .dll) to study for a clean-room rebuild; "check gaming dot py" stays a review
    ("check", re.compile(r"^(?:check|inspect|study|analy[sz]e|take\s+apart|look\s+inside)\s+(?:out\s+)?(?:the\s+|my\s+)?"
                         r"(?:program\s+)?(?:[\w'-]+\s+){0,5}?(?:\w+(?:\.|\s+dot\s+)(?:exe|dll)\b|program\b)", re.I)),
    ("review", re.compile(r"^(?:review|check|look\s+over|go\s+over)\b", re.I)),
    ("fix", re.compile(r"^fix(?:es)?\b", re.I)),
    ("build", re.compile(r"^(?:build|make|create|write)\b", re.I)),
    ("test", re.compile(r"^(?:test|summari[sz]e)\b", re.I)),
]


def _kind_of(rest: str, who: str | None = None) -> tuple[str | None, str]:
    """('review' | 'fix' | 'script' | 'build' | 'joomla' | 'test' | 'article' | 'zoo' | None, what's after
    the verb). With the Smithy named, Firestarter's kinds are skipped ("ask the Smithy to make ...")."""
    r = rest.strip(" \t\n.,:;!?")
    for kind, rx in _KIND_WORDS:
        if who == "smithy" and kind in ("article", "zoo"):
            continue
        if rx.search(r):
            if kind == "joomla" and not re.match(r"^(?:build|make|create|write|draft|do)\b", r, re.I):
                continue                                 # "review my Joomla override" is a review
            after = re.sub(r"^(?:review|check(?:\s+out)?|inspect|study|analy[sz]e|take\s+apart|look\s+inside|"
                           r"look\s+over|go\s+over|fix(?:es)?|test|summari[sz]e|build|make|"
                           r"create|write|draft|do)\s+(?:me\s+)?", "", r, flags=re.I)
            return kind, after.strip()
    return None, r


_PROVIDER_WORDS = worker_cloud.spoken_names()
# "using / via / through" only: "a page on Claude Monet" isn't a provider
_PROVIDER = re.compile(r"[,\s]*\b(?:using|via|through)\s+(?:the\s+)?(" +
                       "|".join(sorted((re.escape(k).replace(r"\ ", r"\s+") for k in _PROVIDER_WORDS), key=len, reverse=True))
                       + r")\b[,\s]*", re.I)


def _take_provider(text: str) -> tuple[str, str | None]:
    """'ask the Smithy, using DeepSeek, to review x' -> ('ask the Smithy to review x', 'deepseek')."""
    m = _PROVIDER.search(text)
    if not m:
        return text, None
    name = _PROVIDER_WORDS.get(re.sub(r"\s+", " ", m.group(1).lower()))
    return (text[:m.start()] + " " + text[m.end():]).strip(), name


# ---------------------------------------------------------------- the voice side
class WorkersVoice:
    def __init__(self, session=None):
        self.session = session
        self.reading: tuple[str, list[str], int] | None = None       # (what it's from, pieces, next piece)
        self.pending: tuple[Queue, int, str] | None = None          # (queue, task, "kept"/"discarded")
        self.pending_out: tuple[Queue, int] | None = None           # a task waiting for free tokens: asked
        self._asked_out: set[tuple[str, int]] = set()                # asked once per task
        self._announcer: asyncio.Task | None = None
        self.howto = howto_recorder.Recorder()                      # picks up a recording left running
        self.task_mode = False                                      # read each step aloud as it happens
        self.tm_target = None                                       # (worker, task) | "next" | None = every task
        self.tm_asking = False                                      # asked "a particular task or the current one?"
        self._step_pos: dict[str, int] = {}                         # how far each steps file has been read
        # "While you were away, N tasks finished. Want to hear about them?" (the user, Nov 14: not read
        # out unasked). The tasks asked about, and when; they're marked heard on her answer.
        self.away: list | None = None
        self._away_ts = 0.0
        self._away_skip: set[tuple[str, int]] = set()               # not read this call (she moved on)

    # -- one turn: a reply from code ('' = say nothing), or None for "not about the workers"
    def handle(self, text: str) -> str | None:
        if (said := self._howto(text)) is not None:
            return said
        if self.away is not None and (said := self._away_answer(text)) is not None:
            return said
        asking, self.tm_asking = self.tm_asking, False   # the question is for the very next turn only
        if _TASK_MODE.search(text):
            if _TASK_MODE_OFF.search(text):
                return self._task_mode_off()
            if _TASK_MODE_ON.search(text):
                return self._task_mode_request(text)
            return ("Task mode reads each step of a task aloud as it happens. "
                    "Say task mode on to start it, and task mode off to stop.")
        if asking and (said := self._task_mode_answer(text)) is not None:
            return said
        reading, self.reading = self.reading, None   # "read the rest" only right after a read
        if (said := self._out_answer(text)) is not None:
            return said
        pending, self.pending = self.pending, None
        if pending:
            q, job_id, verdict = pending
            ans = worker_shared.answer(text)
            if ans == "yes":
                done = q.decide(job_id, text, verdict)
                return (f"Okay, {_name(q, job_id)} is marked {verdict}." if done else
                        f"{_name(q, job_id)} isn't waiting for review any more, so I left it.")
            if ans == "no":
                return f"Okay, I've left {_name(q, job_id)} as it is."
        if reading and _REST.search(text):
            return self._next_piece(*reading)
        text, self._provider = _take_provider(text)        # "..., using DeepSeek, ..." (worker_cloud.py)
        if m := _DO.search(text):                          # "ask the Smithy to review gaming dot py"
            who, _ = _who(m.group(0))
            if who == "firestarter" and not HAS_FIRESTARTER:
                return NO_FIRESTARTER
            return self._start(m.group("rest"), who)
        who, t = _who(text)
        if who == "firestarter" and not HAS_FIRESTARTER:
            return NO_FIRESTARTER
        if m := _MARK.search(t):
            return self._ask_mark(who, _first(m), "kept" if m.group("verb").lower() == "keep" else "discarded")
        if m := _ASK.search(t):
            return self._followup(who, _first(m), m.group("q"))
        if m := _READ.search(t):
            return self._read(who, _first(m))
        if m := _SHOW.search(t):
            return self._show(who, _first(m))
        if _LOG_SHOW.search(t):
            return self._log_show(who)
        if _LOG_READ.search(t):
            return self._log_read(who)
        if m := _PROGRESS.search(t):
            return self._progress(who, _first(m))
        if _PAUSED.search(t):
            return self._paused(who)
        if _LIST_DONE.search(t):
            return self._list_done(who)
        if _QUEUE.search(t) and (who or re.search(r"\b(?:queue|tasks?|workers?)\b", t, re.I)):
            return self._queue(who)
        if m := _TASK_STOP.search(t):
            return self._stop(who, _first(m) if any(m.groups()) else None)
        if m := _TASK_START.search(t):
            return self._start((m.group("rest") or "").strip(" \t\n.,:;!?"), who)
        named = bool(who and _NAMED.search(text))
        if _UPDATE.search(t) and (named or re.search(r"\b(?:tasks?|workers?|reviews?|results?)\b", t, re.I)):
            return self._status(who if named else None)
        if (m := _TASK_N.search(t)) and (named or _TASK_WORDS.search(t)):
            got = self._pick(who, _first(m))
            if not isinstance(got, str):
                n = _name(got[0], _first(m))
                return f"For {n}, you can say read me, show me, keep, or discard {n}."
            if "?" in got:                                       # "Smithy task 2 or Firestarter task 2?"
                return got
        if named and _ASKS.search(text):
            # the worker was named in a question and nothing above matched: answer from
            # the queue, never the model (Nov 12 call: "the Smithy has a review ready, can
            # you give me an update?" went to the model, which made up a woodcarving shop)
            return self._status(who)
        return None

    # -- the how-to recorder
    def _howto(self, text: str) -> str | None:
        rec = self.howto
        if rec.recording:
            if howto_recorder.FINISH.search(text):
                return self._howto_finish()
            return rec.handle(text)
        if howto_recorder.WRITE_UP.search(text):
            return self._howto_write_up(None)
        if m := howto_recorder.START.search(text):
            return rec.start(m.group("title"))
        return None

    def _howto_finish(self) -> str:
        rec = self.howto
        steps = len(rec.steps)
        pics = sum(len(st["pictures"]) for st in rec.steps)
        folder = rec.finish()
        if not steps:
            return "Stopped. That how-to had no steps, so there's nothing to write up."
        said = (f"Done: {steps} step{'s' if steps != 1 else ''} and {pics} picture{'s' if pics != 1 else ''}, "
                "saved in drafts, how-to.")
        if not HAS_FIRESTARTER:
            return said + " Firestarter isn't installed, so it won't be written up; what you said is in raw dot M D in that folder."
        return said + " " + self._howto_write_up(folder)

    def _howto_write_up(self, folder) -> str:
        if not HAS_FIRESTARTER:
            return NO_FIRESTARTER
        folder = folder or howto_recorder.latest_unwritten()
        if folder is None:
            return "There's no recorded how-to waiting for a write-up."
        q = FIRESTARTER
        if not q.running():
            return "When Firestarter is open, say: Firestarter, write up the how-to."
        jt = worker_jobs.REGISTRY["howto"]
        try:
            params = jt.check({"folder": str(folder)})
        except ValueError as e:
            return f"I can't write that up: {e}."
        job_id = q.add("howto", params, requested_by="voice", title=f"the how-to guide: {self.howto_title(folder)}")
        return f"{_name(q, job_id)} is writing it up as a guide."

    @staticmethod
    def howto_title(folder) -> str:
        try:
            return json.loads((Path(folder) / "steps.json").read_text(encoding="utf-8")).get("title") or "how-to"
        except Exception:
            return "how-to"

    # -- which task the user means
    def _pick(self, who: str | None, job_id: int):
        """(queue, row) or a reply asking which / saying there's none."""
        if who:
            q = QUEUES[who]
            row = q.get(job_id)
            return (q, row) if row else f"There's no {_name(q, job_id)}."
        hits = [(q, r) for q in INSTALLED if (r := q.get(job_id)) is not None]
        if not hits:
            return f"There's no task {job_id}."
        if len(hits) > 1:
            return f"Smithy task {job_id} or Firestarter task {job_id}? Say which."
        return hits[0]

    def _queues(self, who: str | None) -> list[Queue]:
        return [QUEUES[who]] if who else list(INSTALLED)

    # -- start / stop (only while that worker runs)
    def _stop(self, who: str | None, job_id: int | None) -> str:
        if job_id is not None:
            got = self._pick(who, job_id)
            if isinstance(got, str):
                return got
            q = got[0]
        else:
            busy = [q for q in self._queues(who) if q.by_status("running", "queued", limit=1)]
            if not busy:
                return "There's no task running or waiting."
            if len(busy) > 1:
                return "Stop the Smithy's task or Firestarter's? Say stop the Smithy task, or stop the Firestarter task."
            q = busy[0]
        if not q.running():
            return _not_running(q)
        row = q.cancel(job_id)
        return f"Stopped {_name(q, row['id'])}." if row else "There's no task running or waiting."

    def _start(self, rest: str, who: str | None) -> str:
        kind, arg = _kind_of(rest, who)
        if kind is None:
            return ("Which task? The Smithy can review, fix, write a script, build something" +
                    (", make a Joomla override, or check a program." if smithy_rea else ", or make a Joomla override.") +
                    (" Firestarter can write an article or a page, add ZOO items, or run a test." if HAS_FIRESTARTER else ""))
        if kind == "check" and (smithy_rea is None or "check" not in REGISTRIES["smithy"]):
            return "Checking programs isn't part of this copy of Othos."
        target = "smithy" if kind in CODING else "firestarter"
        if target == "firestarter" and not HAS_FIRESTARTER:
            return NO_FIRESTARTER
        if who and who != target:
            return ("That's coding, which is the Smithy's job." if target == "smithy"
                    else "That's site work, which is Firestarter's job.")
        q = QUEUES[target]
        if not q.running():
            return _not_running(q)
        if kind in ("review", "fix"):
            path, request, problem = self._file_and_request(arg, need_request=(kind == "fix"))
            if problem:
                return problem
            raw = {"path": path, "request": request}
        elif kind == "check":
            path, problem = self._program(arg)
            if problem:
                return problem
            raw = {"path": path}
        elif kind in ("script", "build", "joomla"):
            raw = {"request": arg}          # "a PowerShell script to ...": the language stays in
        elif kind == "article":
            raw = {"brief": arg, "kind": "page" if re.search(r"\bpage\b", rest, re.I) else "post"}
        elif kind == "zoo":
            raw = {"brief": arg}            # the app: the only one added, or the one named
        else:
            raw = {"text": arg}
        jt = REGISTRIES[target][kind]
        provider, self._provider = getattr(self, "_provider", None), None
        try:
            params = jt.check(raw)
            if provider:                                   # checked now, so she hears why straight away
                cfg = worker_shared.load_json(Path(__file__).parent / ("smithy.json" if target == "smithy" else "worker.json"))
                worker_cloud.pick(cfg, {"provider": provider}, kind)
                params["provider"] = provider
        except ValueError as e:
            return f"I can't start that: {e}."
        job_id = q.add(kind, params, requested_by="voice", title=jt.title(params))
        via = f", using {worker_cloud.PROVIDERS[provider].spoken}" if provider else ""
        return f"{_name(q, job_id)}, {jt.title(params)}, is queued for {q.name}{via}."

    def _program(self, arg: str) -> tuple[str, str]:
        """'llama server dot exe in my downloads' -> (path, ''), or ('', what to say)."""
        cfg = worker_shared.load_json(Path(__file__).parent / "smithy.json")
        words = re.sub(r"^(?:the\s+|my\s+)?(?:program\s+)?(?:called\s+)?", "", arg, flags=re.I)
        words = re.sub(r"\s+(?:in|from)\s+(?:my\s+|the\s+)?downloads?\b.*$", "", words, flags=re.I).split()
        for n in range(min(len(words), 6), 0, -1):
            found, name = smithy_rea.find_program(" ".join(words[:n]), cfg)
            if len(found) == 1:
                return str(found[0]), ""
            if len(found) > 1:
                return "", ("I found more than one: " + "; ".join(f"{p.name} in {p.parent.name}" for p in found[:3])
                            + ". Say the name with its folder.")
        return "", (f"I couldn't find a program called {worker_files.spoken_to_name(' '.join(words[:4]))} in the "
                    "folders the Smithy may check. They're listed in smithy dot json, under inspect folders.")

    def _file_and_request(self, arg: str, need_request: bool) -> tuple[str, str, str]:
        """'gaming dot py to stop crashing on startup' -> (path, 'stop crashing on startup', '')."""
        words = re.sub(r"^(?:my|the)\s+", "", arg, flags=re.I).split()
        for n in range(min(len(words), 8), 0, -1):
            found, name = worker_files.find(" ".join(words[:n]))
            if len(found) == 1:
                rest = re.sub(r"^\W*(?:so\s+that|so|to|that|and|because|which|:)\W*", "", " ".join(words[n:]), flags=re.I)
                if need_request and len(rest.split()) < 2:
                    return "", "", f"What should the fix for {found[0].name} do? Say, fix {name} to, and what it should do."
                return str(found[0]), rest, ""
            if len(found) > 1:
                where = "; ".join(f"{p.name} in {p.parent.name}" for p in found[:3])
                return "", "", f"I found more than one: {where}. Say the name with its folder, like Othos U I's {found[0].name}."
        name = worker_files.spoken_to_name(" ".join(words[:4]))
        return "", "", f"I couldn't find a file called {name} in the Othos folders the Smithy may read."

    # -- listing and progress
    def _list_done(self, who: str | None) -> str:
        jobs = [(q, j) for q in self._queues(who) for j in q.by_status("review", limit=6)]
        if not jobs:
            return "Nothing is waiting for review."
        names = "; ".join(f"{_name(q, j['id'])}, {_title(q, j)}" for q, j in jobs[:6])
        return f"{len(jobs)} ready: {names}. Say read me, and the task."

    def _status(self, who: str | None) -> str:
        """What each worker is doing, what's ready and what's waiting, in a sentence or two."""
        parts = []
        for q in self._queues(who):
            running = q.by_status("running", limit=1)
            ready = q.by_status("review", limit=6)
            queued = q.by_status("queued", limit=6)
            bits = []
            if running:
                job = running[0]
                s = q.read_status()
                pct = ""
                if q.running() and s.get("task_id") == job["id"] and s.get("progress"):
                    pct = f", about {int(float(s['progress']) * 100)} percent done"
                still = "is still running" if q.running() else f"is waiting for you to open {q.app}"
                bits.append(f"{_name(q, job['id'])}, {_title(q, job)}, {still}{pct}")
            if ready:
                names = "; ".join(f"{_name(q, j['id'])}, {_title(q, j)}" for j in ready[:3])
                what = "result" if len(ready) == 1 else "results"
                bits.append(f"{q.name} has {len(ready)} {what} ready: {names}. "
                            f"Say read me {_name(q, ready[0]['id'])} to hear it")
            if queued:
                bits.append(f"{len(queued)} more waiting in the queue")
            if not bits:
                parts.append(f"{q.name} has nothing running or ready" + ("" if q.running() else ", and isn't running"))
            else:
                parts.append(". ".join(bits[:1] + [b[0].upper() + b[1:] for b in bits[1:]]))
        return ". ".join(parts) + "."

    def _queue(self, who: str | None) -> str:
        parts = []
        for q in self._queues(who):
            running = q.by_status("running", limit=1)
            queued = q.by_status("queued", limit=6)
            if not running and not queued:
                parts.append(f"{q.name} has nothing queued" + ("" if q.running() else ", and isn't running"))
                continue
            bits = []
            if running:
                bits.append(f"is working on {_name(q, running[0]['id'])}, {_title(q, running[0])}")
            if queued:
                bits.append(f"has {len(queued)} waiting: " +
                            "; ".join(f"task {j['id']}, {_title(q, j)}" for j in reversed(queued[:4])))
            s = q.read_status()
            if q.running() and s.get("state") == "waiting for the CPU":
                bits.append(f"is waiting for {s.get('waiting_for_cpu') or 'the other worker'} to finish")
            if not q.running():
                bits.append(f"isn't running, so nothing will start until you open {q.app}")
            parts.append(f"{q.name} " + ", and ".join(bits))
        return ". ".join(parts) + "."

    def _progress(self, who: str | None, job_id: int) -> str:
        got = self._pick(who, job_id)
        if isinstance(got, str):
            return got
        q, job = got
        name = _name(q, job_id)
        st = job["status"]
        s = q.read_status()
        if st == "running":
            if s.get("task_id") == job_id and q.running():
                pct = int(float(s.get("progress") or 0) * 100)
                mins, secs = divmod(int(s.get("elapsed") or 0), 60)
                took = f"{mins} minute{'s' if mins != 1 else ''} {secs} seconds" if mins else f"{secs} seconds"
                held = f", held back for {s['game']}" if s.get("state") == "held back" and s.get("game") else ""
                return f"{name} is about {pct} percent done after {took}{held}."
            return f"{name} was running, but {q.name} isn't. It'll carry on when you open {q.app}."
        if st == "queued":
            if job["paused_for"]:
                return f"{name} is paused while {job['paused_for']} is running. It'll continue after."
            if s.get("state") == "waiting for the CPU" and q.running():
                return f"{name} is waiting for {s.get('waiting_for_cpu') or 'the other worker'} to finish its task."
            return f"{name} is waiting in the queue."
        return {"review": f"{name} is done. Say read me {name}.",
                "kept": f"{name} is done, and you kept it.",
                "discarded": f"{name} is done, and you discarded it.",
                "cancelled": f"{name} was stopped.",
                "failed": f"{name} failed: {job['error'] or 'no reason given'}."}.get(st, f"{name} is {st}.")

    def _paused(self, who: str | None) -> str:
        parts = []
        for q in self._queues(who):
            s = q.read_status()
            if not q.running():
                parts.append(f"{q.name} isn't running")
            elif s.get("state") == "paused":
                p = s.get("paused_task") or {}
                what = f", with {_name(q, p['id'])} waiting" if p.get("id") else ""
                parts.append(f"{q.name} is paused for {s.get('game') or 'a game'}{what}")
            elif s.get("state") == "held back":
                parts.append(f"{q.name} isn't paused, but it's held back for {s.get('game') or 'a game'}, so it's slower")
            else:
                parts.append(f"{q.name} isn't paused")
        return ". ".join(parts) + "."

    # -- reading results
    def _read(self, who: str | None, job_id: int) -> str:
        got = self._pick(who, job_id)
        if isinstance(got, str):
            return got
        q, job = got
        name = _name(q, job_id)
        if not job["result_path"]:
            return f"{name} doesn't have a result yet."
        summary = _summary(job) or "Here's the result."
        rest = pieces(_full_result(job))
        if not rest:
            return f"{name}, {_title(q, job)}. {summary}"
        self.reading = (name, rest, 0)
        return f"{name}, {_title(q, job)}. {summary} Say read the rest for the full result, or show me {name}."

    def _next_piece(self, what: str, rest: list[str], i: int) -> str:
        if i >= len(rest):
            return f"That's the end of {what}."
        more = i + 1 < len(rest)
        if more:
            self.reading = (what, rest, i + 1)
        return rest[i] + (" Shall I go on?" if more else f" That's the end of {what}.")

    def _show(self, who: str | None, job_id: int) -> str:
        got = self._pick(who, job_id)
        if isinstance(got, str):
            return got
        q, job = got
        name = _name(q, job_id)
        folder = Path(job["artifact"]) if job["artifact"] else None
        if folder is not None and folder.is_dir():
            subprocess.Popen(["explorer.exe", str(folder)], creationflags=subprocess.DETACHED_PROCESS)
            return f"Opening the folder for {name}."
        if not job["result_path"] or not Path(job["result_path"]).exists():
            return f"{name} doesn't have a result to show yet."
        if not Path(NOTEPAD_PP).exists():
            return "I can't find Notepad plus plus."
        subprocess.Popen([NOTEPAD_PP, job["result_path"]], creationflags=subprocess.DETACHED_PROCESS)
        return f"Opening {name} in Notepad plus plus."

    # -- the work logs
    def _log_read(self, who: str | None) -> str:
        if not who and not HAS_FIRESTARTER:
            who = "smithy"
        if not who:
            return "The Smithy's log or Firestarter's? Say read me the Smithy log, or the Firestarter log."
        q = QUEUES[who]
        rows = q.list_jobs(limit=30)
        if not rows:
            return f"{q.name}'s log is empty: no tasks yet."
        entries = []
        for r in rows:
            e = q.entry(r)
            bit = f"{_name(q, e['id'])}, {e['title']}: {e['status']}"
            if e["finished"]:
                bit += f", finished {_spoken_time(r['finished'])}"
            else:
                bit += f", asked {_spoken_time(r['created'])}"
            if e["summary"] and r["status"] in ("review", "kept"):
                bit += ". " + re.split(r"(?<=[.!?])\s+", speakable(e["summary"]))[0]   # first sentence only
            elif e["status"] == "failed" and e["error"]:
                bit += ". " + e["error"][:150]
            entries.append(bit.rstrip(".") + ".")
        groups = [" ".join(entries[i:i + 3]) for i in range(0, len(entries), 3)]
        what = f"{q.name}'s log"
        if len(groups) > 1:
            self.reading = (what, groups, 1)
            return f"The latest from {what}. {groups[0]} Shall I go on?"
        return f"The latest from {what}. {groups[0]} That's everything in it."

    def _log_show(self, who: str | None) -> str:
        if not who and not HAS_FIRESTARTER:
            who = "smithy"
        if not who:
            return "The Smithy's log or Firestarter's? Say show me the Smithy log, or the Firestarter log."
        q = QUEUES[who]
        q.write_log()
        if not Path(NOTEPAD_PP).exists():
            return "I can't find Notepad plus plus."
        subprocess.Popen([NOTEPAD_PP, str(q.log_path)], creationflags=subprocess.DETACHED_PROCESS)
        return f"Opening {q.name}'s log in Notepad plus plus."

    # -- follow-ups and marks
    def _followup(self, who: str | None, job_id: int, question: str) -> str:
        got = self._pick(who, job_id)
        if isinstance(got, str):
            return got
        q, _ = got
        if not q.running():
            return _not_running(q)
        jt = REGISTRIES[q.key]["followup"]
        try:
            params = jt.check({"parent_id": job_id, "question": question.strip()})
        except ValueError as e:
            return f"I can't ask that: {e}."
        new_id = q.add("followup", params, requested_by="voice", title=jt.title(params), parent_id=job_id)
        return f"{_name(q, new_id)} is queued: {jt.title(params)}."

    def _ask_mark(self, who: str | None, job_id: int, verdict: str) -> str:
        got = self._pick(who, job_id)
        if isinstance(got, str):
            return got
        q, job = got
        if job["status"] != "review":
            return f"{_name(q, job_id)} isn't waiting for review."
        self.pending = (q, job_id, verdict)
        word = "Keep" if verdict == "kept" else "Discard"
        return f"{word} {_name(q, job_id)}, {_title(q, job)}? Say yes or no."

    # -- "While you were away...": asked first, read only if she wants it. Conversational, not a
    # code-checked gate (it only reads a list; nothing is sent or applied). Her next reply answers it,
    # or it's dropped: something else is just carried on with, and it isn't asked again this call.
    def _away_reply_text(self, jobs) -> str:
        if len(jobs) == 1:
            q, j = jobs[0]
            return f"{_name(q, j['id'])}, {_title(q, j)}, finished. {_summary(j)}"
        names = "; ".join(f"{_name(q, j['id'])}, {_title(q, j)}" for q, j in jobs[:5])
        more = f", and {len(jobs) - 5} more" if len(jobs) > 5 else ""
        return f"{len(jobs)} tasks finished: {names}{more}. Say read me, and the task."

    def _away_drop(self) -> None:
        """The question lapses: those tasks aren't read or marked; not asked again this call."""
        self._away_skip.update((q.key, int(j["id"])) for q, j in (self.away or []))
        self.away = None

    def _away_answer(self, text: str) -> str | None:
        jobs = self.away or []
        if _AWAY_NO.search(text):
            ans = "no"
        elif _AWAY_YES.search(text) or worker_shared.answer(text) == "yes":
            ans = "yes"
        else:
            self._away_drop()                          # something else: carry on with what she said
            return None
        self.away = None
        for q, j in jobs:                              # heard (or skipped): still there by "list" / "read me task N"
            q.mark_announced(j["id"])
        log.info("workers: away list %s for %s", "read" if ans == "yes" else "skipped",
                 [_name(q, j["id"]) for q, j in jobs])
        if ans == "no" and len(re.findall(r"\w+", text)) > 4:
            return None                                # "no, what's the time?": skipped, and her question answered
        return self._away_reply_text(jobs).strip() if ans == "yes" else "Okay."

    # -- "it's ready" announcements, between turns only
    # -- a task waiting for free tokens (worker_cloud.Chain): "finish on this PC, or wait?"
    def _waiting_out(self) -> list[tuple[Queue, dict]]:
        out = []
        for q in INSTALLED:
            st = q.read_status()
            if st.get("state") == "waiting for free tokens" and st.get("task_id") and q.running():
                out.append((q, st))
        return out

    def _out_answer(self, text: str) -> str | None:
        """Her answer to the free-tokens question, or "finish task 3 on this PC" at any time.
        This PC only on a clear yes (worker_shared.answer) or her own "finish it on this PC" with
        no "no" word in it; anything else, or nothing, leaves it waiting. The question is used up
        by the first reply, so a later "yes" to something else can't count."""
        m = _OUT_LOCAL.search(text)
        if m and worker_shared.answer(text) == "no":    # "don't finish it on this PC", "no, wait"
            m = None
        target = None
        if m and m.group("n"):
            n = _num(m.group("n"))
            target = next(((q, st) for q, st in self._waiting_out() if int(st["task_id"]) == n), None)
        elif self.pending_out:
            q, n = self.pending_out
            target = next(((q2, st) for q2, st in self._waiting_out() if q2 is q and int(st["task_id"]) == n), None)
        if target is None:
            if self.pending_out and not m:
                self.pending_out = None                # that task isn't waiting any more
            return None
        q, st = target
        n = int(st["task_id"])
        asked, self.pending_out = self.pending_out, None
        if m or (asked and worker_shared.answer(text) == "yes"):
            choice = "local"
        elif asked and (_OUT_WAIT.search(text) or worker_shared.answer(text) == "no"):
            choice = "wait"
        else:
            return None                                # not an answer: it keeps waiting
        worker_cloud.set_out_choice(q.status_path, n, choice)
        if choice == "local":
            return f"Okay, {_name(q, n)} carries on with this PC's model. It's slower, but nothing more is sent out."
        return f"Okay, {_name(q, n)} waits, and carries on by itself {_spoken_time(st.get('free_tokens_back_ts') or 0)}."

    # -- Task Mode: each step read aloud as it happens
    # "task mode on" without a task: "Do you want a particular task or the current one?" (the user,
    # Nov 13). A particular task: only its steps, and task mode ends when it does. The current one:
    # the task running now (or, with none running, the next one to start), the same way.
    def _task_mode_request(self, text: str) -> str:
        who, t = _who(text)
        if m := _TM_TASK.search(t):
            return self._tm_follow(who, _num(m.group(1)))
        if _TM_CURRENT.search(t):
            return self._tm_current()
        self.tm_asking = True
        return "Do you want a particular task or the current one?"

    def _task_mode_answer(self, text: str) -> str | None:
        """Her answer to "a particular task or the current one?" (None: not an answer; the question lapses)."""
        if _TM_NEVER.search(text) or worker_shared.answer(text) == "no":
            return "Okay, task mode stays off."
        who, t = _who(text)
        # "the current one" / "a particular one": that "one" isn't task 1
        bare = re.sub(r"\b(?:the|a|this|that|current|particular|specific|certain|another|different)\s+one\b", " ", t,
                      flags=re.I)
        if m := _TM_NUMBER.search(bare):
            return self._tm_follow(who, _num(m.group(1)))
        if _TM_CURRENT.search(t) or worker_shared.answer(text) == "yes":
            return self._tm_current()
        if _TM_PARTICULAR.search(t):
            self.tm_asking = True
            return "Which task? Say its number, like Smithy task 21."
        return None

    def _tm_on(self, target) -> None:
        """target: (worker key, task number), "next" (the next task to start), or None (every task)."""
        self.task_mode, self.tm_target = True, target
        for q in INSTALLED:                            # from now on: only steps that happen after this
            try:
                self._step_pos[q.key] = worker_engine.steps_path(q.status_path).stat().st_size
            except OSError:
                self._step_pos[q.key] = 0

    def _tm_follow(self, who: str | None, job_id: int) -> str:
        got = self._pick(who, job_id)
        if isinstance(got, str):
            return got
        q, row = got
        name = _name(q, job_id)
        if row["status"] in ("review", "kept", "discarded"):
            return f"{name} has already finished. Say read me {name} to hear it."
        if row["status"] in ("failed", "cancelled"):
            return f"{name} has already {'failed' if row['status'] == 'failed' else 'been stopped'}, so there are no steps to read."
        self._tm_on((q.key, job_id))
        st = q.read_status()
        if q.running() and st.get("task_id") == job_id:
            last = _last_step(worker_engine.steps_path(q.status_path), job_id)
            now = f" Right now: {last}" if last else ""
            return f"Task mode is on for {name}. I'll read each step as it happens. Say task mode off to stop.{now}"
        where = "" if q.running() else f" {q.name} isn't open, so it won't start until you open {q.app}."
        return f"Task mode is on for {name}. It's waiting in the queue; I'll read its steps when it starts.{where}"

    def _tm_current(self) -> str:
        running = [(q, int(st["task_id"])) for q in INSTALLED
                   if q.running() and (st := q.read_status()).get("task_id")]
        if len(running) == 1:
            return self._tm_follow(running[0][0].key, running[0][1])
        if len(running) > 1:                           # both workers busy: both
            self._tm_on(None)
            names = " and ".join(_name(q, n) for q, n in running)
            return f"Task mode is on for {names}. I'll read each step as it happens. Say task mode off to stop."
        self._tm_on("next")
        return "Nothing is running right now. Task mode is on for the next task that starts."

    def _task_mode_off(self) -> str:
        self.task_mode, self.tm_target, self.tm_asking = False, None, False
        return "Task mode is off. I'll still tell you when a task is ready."

    def _new_steps(self) -> list[tuple[Queue, int | None, str]]:
        """(worker, task, text) for the step lines written since the last look that Task Mode
        follows, oldest first."""
        out = []
        for q in INSTALLED:
            path = worker_engine.steps_path(q.status_path)
            try:
                size = path.stat().st_size
                pos = self._step_pos.get(q.key, size)
                if size < pos:                         # the file was trimmed: start again from its end
                    pos = size
                if size > pos:
                    with path.open("rb") as f:
                        f.seek(pos)
                        chunk = f.read(size - pos)
                    keep = chunk.rfind(b"\n") + 1     # whole lines only; a half-written one waits
                    self._step_pos[q.key] = pos + keep
                    for line in chunk[:keep].decode("utf-8", errors="replace").splitlines():
                        try:
                            d = json.loads(line)
                        except ValueError:
                            continue
                        task, text = d.get("task"), str(d.get("text", ""))
                        if self.tm_target == "next" and task is not None:
                            self.tm_target = (q.key, task)          # the next task: this one
                        if isinstance(self.tm_target, tuple) and self.tm_target != (q.key, task):
                            continue
                        if text:
                            out.append((q, task, text))
                else:
                    self._step_pos[q.key] = size
            except OSError:
                continue
        return out

    def _say_steps(self) -> bool:
        if self.howto.recording and not self.howto.paused:
            return False                               # never over her how-to narration: held until it pauses or ends
        new = self._new_steps()
        if not new:
            return False
        parts, ended = [], None
        for q in [q for q in INSTALLED if any(x[0] is q for x in new)]:
            mine = [(task, t) for q2, task, t in new if q2 is q]
            if isinstance(self.tm_target, tuple) and any(_END_STEP.match(t) for _, t in mine):
                ended = (q, mine[-1][0])
            texts = [t for _, t in mine if not _DONE_STEP.match(t)]   # "is ready" is said by the announcer
            if len(texts) > 4:                          # a burst: the count, then the latest few
                parts.append(f"{q.name}, {len(texts)} steps. The latest: " + " ".join(texts[-3:]))
            elif texts:
                parts.append(f"{q.name}: " + " ".join(texts))
        if ended:                                       # the task it followed is over: task mode ends with it
            q, task = ended
            self.task_mode, self.tm_target = False, None
            parts.append(f"That's the end of {_name(q, task)}, so task mode is off.")
        if not parts:
            return False
        self.session.say(speakable(" ".join(parts)), allow_interruptions=True)
        return True

    def start_announcer(self) -> None:
        if self.session is not None and self._announcer is None:
            self._announcer = asyncio.create_task(self._announce_loop())

    def _between_turns(self) -> bool:
        s = self.session
        return (getattr(s, "agent_state", "") == "listening" and getattr(s, "user_state", "") != "speaking"
                and (s.current_speech is None or s.current_speech.done()))

    async def _announce_loop(self) -> None:
        first = True
        await asyncio.sleep(2)
        while True:
            try:
                if self.away is not None and time.time() - self._away_ts > AWAY_ANSWER_SECONDS:
                    log.info("workers: no answer to the away question; dropped for this call")
                    self._away_drop()
                if self.away is not None:
                    pass                                # waiting for her answer: nothing else is announced
                elif self._between_turns():
                    for q, st in await asyncio.to_thread(self._waiting_out):
                        key = (q.key, int(st["task_id"]))
                        if key in self._asked_out or st.get("out_choice"):
                            continue
                        self._asked_out.add(key)
                        self.pending_out = (q, key[1])
                        when = _spoken_time(st.get("free_tokens_back_ts") or 0)
                        self.session.say(f"{_name(q, key[1])}, {st.get('task_title') or 'its task'}, has used up the free "
                                         f"tokens on every provider in the switch order. Should I finish it on this PC "
                                         f"now? Or say wait, and it carries on by itself {when}.", allow_interruptions=True)
                        log.info("workers: asked about free tokens for %s", _name(q, key[1]))
                        break
                    # Task Mode: the steps first, so "is ready" comes after the task's last step
                    if self.task_mode and await asyncio.to_thread(self._say_steps):
                        await asyncio.sleep(5)
                        continue
                    jobs = [(q, j) for q in INSTALLED for j in await asyncio.to_thread(q.unannounced)
                            if (q.key, int(j["id"])) not in self._away_skip]
                    if jobs:
                        if first:   # finished while the user was away: ask first (the user, Nov 14)
                            first = False
                            self.away, self._away_ts = jobs, time.time()
                            text = ("While you were away, a task finished. Want to hear about it?" if len(jobs) == 1
                                    else f"While you were away, {len(jobs)} tasks finished. Want to hear about them?")
                            log.info("workers: asked about the away list %s", [_name(q, j["id"]) for q, j in jobs])
                            self.session.say(text, allow_interruptions=True)
                            await asyncio.sleep(5)
                            continue
                        else:
                            q, j = jobs[0]
                            text, done = f"{_name(q, j['id'])}, {_title(q, j)}, is ready. {_summary(j)}", [jobs[0]]
                        for q, j in done:
                            await asyncio.to_thread(q.mark_announced, j["id"])
                        log.info("workers: announced %s", [_name(q, j["id"]) for q, j in done])
                        self.session.say(text.strip(), allow_interruptions=True)
                    first = False
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("worker announcement failed: %s", e)
            await asyncio.sleep(5)


# "finish it on this PC", "finish task 3 on the computer", "do it locally"
_OUT_LOCAL = re.compile(r"\b(?:finish|carry\s+on|continue|do)\b(?:\s+(?:task\s+)?(?P<n>" + _N[1:-1] + r"))?"
                        r"[\w\s,]{0,30}?\b(?:on|with)\s+(?:this|the|my)\s+(?:pc|computer|laptop|machine|local\s+model)\b"
                        r"|\blocally\b", re.I)
_OUT_WAIT = re.compile(r"\b(?:wait|later|hold\s+on|leave\s+it)\b", re.I)

# Her answer to "While you were away, N tasks finished. Want to hear about them?" A reply that's
# neither (another question, a new topic) is carried on with, and the question is dropped.
AWAY_ANSWER_SECONDS = 60                                 # no reply this long: dropped, nothing read
_AWAY_YES = re.compile(r"^\W*(?:(?:othos|osos|um+|uh+|oh|well)\W+)*(?:yes|yeah|yep|yup|sure|ok(?:ay)?|go\s+ahead|"
                       r"please|tell\s+me(?=\W*$|\s+about\s+(?:them|it|those)|\s+more)|read\s+(?:them|it)|"
                       r"let'?s\s+hear|i'?d\s+like\s+that|go\s+on|"
                       r"absolutely|of\s+course|definitely|what\s+(?:are|were)\s+they|what\s+(?:is|was)\s+it|"
                       r"which\s+ones?|what\s+finished)\b", re.I)
_AWAY_NO = re.compile(r"^\W*(?:(?:othos|osos|um+|uh+|oh|well)\W+)*(?:no|nope|nah|not\s+(?:now|yet|right\s+now)|"
                      r"later|maybe\s+later|skip|never\s*mind|don'?t\s+(?:bother|worry)|i'?m\s+good|pass)\b"
                      r"|\b(?:not\s+now|later|no\s+thanks?)\W*$", re.I)

# "task mode on", "turn on task mode", "go into task mode"; off: "task mode off", "stop task mode", "leave task mode"
_TASK_MODE = re.compile(r"\btask\s*-?\s*mode\b", re.I)
_TASK_MODE_OFF = re.compile(r"\b(?:off|stop|end|leave|exit|quit|out\s+of|disable|cancel|no\s+more)\b", re.I)
# on: "task mode on", "open / start / enter / go into / assume / switch to / turn on / use task mode",
# "put yourself in task mode"
_TASK_MODE_ON = re.compile(r"\b(?:on|open|start|enter|into|assume|begin|use|activate|switch\s+to|go\s+to|"
                           r"put\s+(?:\w+\s+)?in)\b", re.I)
_TM_TASK = re.compile(r"\btask\s+(?:number\s+)?" + _N + r"\b", re.I)          # "task mode for Smithy task 21"
_TM_NUMBER = re.compile(r"(?:\btask\s+)?(?:\bnumber\s+)?\b" + _N + r"\b", re.I)   # the answer: "21", "task 21"
_TM_CURRENT = re.compile(r"\b(?:current|this\s+one|the\s+one\s+(?:that'?s\s+)?running|running|"
                         r"whatever'?s\s+running|that\s+one|the\s+one\s+going)\b", re.I)
_TM_PARTICULAR = re.compile(r"\b(?:particular|specific|a\s+certain|another|different)\b", re.I)
_TM_NEVER = re.compile(r"\b(?:never\s*mind|cancel|forget\s+it|neither|none)\b", re.I)
# the end of a task, as the engine writes it (worker_engine.run_task)
_END_STEP = re.compile(r"^Task \d+ (?:is done\b|was cancelled\b|couldn't start\b|stopped:|failed:(?!.*once more))")
_DONE_STEP = re.compile(r"^Task \d+ is done\b")


def _last_step(path: Path, task_id: int) -> str:
    """The latest step line of that task in a steps file ('' if none)."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("task") == task_id:
            return str(d.get("text", ""))
    return ""


def _spoken_time(ts) -> str:
    import time
    t = time.localtime(float(ts))
    now = time.localtime()
    clock = time.strftime("%I:%M %p", t).lstrip("0")
    if t.tm_yday == now.tm_yday and t.tm_year == now.tm_year:
        return f"at {clock}"
    if (now.tm_yday - t.tm_yday == 1 and t.tm_year == now.tm_year):
        return f"yesterday at {clock}"
    return time.strftime("%B %d", t).replace(" 0", " ") + f" at {clock}"
