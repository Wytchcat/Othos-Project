import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import re
import ssl
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

load_dotenv()  # must run before anything reads environment variables

from livekit import agents, rtc
from livekit.agents import inference, llm
from livekit.agents import (
    Agent,
    APIConnectOptions,
    AgentServer,
    AgentSession,
    AutoSubscribe,
    JobContext,
    JobProcess,
    RunContext,
    SessionConnectOptions,
    TurnHandlingOptions,
    function_tool,
    get_job_context,
)
from livekit.agents.utils.images import EncodeOptions, ResizeOptions, encode as encode_image
from livekit.agents.types import FlushSentinel
from livekit.plugins import deepgram, openai, rime, silero
from tavily import AsyncTavilyClient

import gaming  # gaming mode: gaming.py + gaming.json
from workers_voice import WorkersVoice  # Firestarter (site work) and the Smithy (coding) by voice
import workers_voice  # (its INSTALLED list, for the self-check)
import selfcheck  # the quiet self-check behind "How are you?"
import voices  # who is speaking, when speech-to-text labels speakers (feeds guest mode)
import llm_server  # the conversation on llama-server when voice-llm.json says so (a prompt top that never changes)
import searchctx  # searches keep the topic and the user's town: searchctx.py
import library  # the user's own saved reference pages: library.py
import google_tools  # Google Calendar (read-only) once connected: google_tools.py, google-connect.ps1
import held as holdon  # what the user asked Othos to hold on to (held.py: permanent disability, pronouns, names): caught by code, kept in state\held.json
import humor  # hints about the jokes ("go easy on the puns"): caught by code, kept in state\humor.json
import sources  # free sources tried before Tavily: sources.py
import personal  # the user's name, town, feeds and respellings: personal.json

# The assistant's own name: "Othos" unless personal.json's assistant.name says otherwise.
# The code that recognises "Othos, ..." in what the user says (addressing, stop words,
# end the call, the workers) is written for "Othos", so the chosen name and what
# speech-to-text hears for it are turned into "Othos" for those checks only: the model
# and the transcript keep the user's real words.
ASSISTANT = personal.assistant_name()
_called_as = [w for w in [ASSISTANT, *personal.assistant_heard_as()] if w.lower() != "othos"]
_CALLED = (re.compile(r"\b(?:" + "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in
                                         sorted(_called_as, key=len, reverse=True)) + r")\b", re.I)
           if _called_as else None)


def _as_othos(text: str) -> str:
    """'Jarvis, stop' -> 'Othos, stop', for code checks (personal.json assistant.name)."""
    return _CALLED.sub("Othos", text) if _CALLED and text else text

# ---------------------------------------------------------------- paths/settings
BASE_DIR = Path(__file__).parent
PROFILE_PATH = BASE_DIR / "profile.md"
BRAIN_DIR = Path(os.getenv("BRAIN_DIR") or BASE_DIR / "brain")
NOTES_DIR = BRAIN_DIR / "notes"
NOTES_DIR.mkdir(parents=True, exist_ok=True)

_tavily = None  # AsyncTavilyClient, made on first search
# Tavily credits (the user, Nov 10): free sources come first (sources.py); Othos says so
# at call start once the month's use reaches OTHOS_TAVILY_WARN, and stops using Tavily at
# the limit (the plan's, or OTHOS_TAVILY_LIMIT) instead of running into paid overage.
TAVILY_WARN = int(os.getenv("OTHOS_TAVILY_WARN", "1400") or 1400)
TAVILY_LIMIT = int(os.getenv("OTHOS_TAVILY_LIMIT", "0") or 0)  # 0 = the plan's own limit
_tavily_used: int | None = None   # Tavily's count at call start, plus this call's searches
_tavily_limit: int | None = None


def _tavily_out() -> bool:
    return bool(_tavily_used is not None and _tavily_limit and _tavily_used >= _tavily_limit)


async def _tavily_search(**kw) -> dict:
    """Every Tavily search goes through here, so the month's credits are counted."""
    global _tavily, _tavily_used
    if _tavily_out():
        raise RuntimeError("this month's Tavily credits are used up")
    if _tavily is None:
        _tavily = AsyncTavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
    result = await _tavily.search(**kw)
    if _tavily_used is not None:
        _tavily_used += 2 if kw.get("search_depth") == "advanced" else 1
    return result


FETCH_N = 12   # results pulled per search
MAX_PAGE = 8   # most results read aloud at once
# When the user asked a question (not for a list), the model answers from the
# results with this instruction. Lists of results are read out by code instead:
# the 3B model read one result per turn, took ~25 s, and mislabelled sites.
_ANSWER_FROM_RESULTS = (
    "\nAnswer the user's question from these results only, in one or two sentences, "
    "the way you'd tell a friend. Don't mention 'the results' or number them. "
    "If they don't answer it, say so."
)
# Pages from the user's own library (reference copies saved on this PC; they can be out of date)
_ANSWER_FROM_LIBRARY = (
    "\nThese are pages the user saved in their own library. Answer the user's question from them only, in one to three "
    "sentences, and say it comes from their library (name the page if that helps). They may be out of date: if they "
    "don't answer the question, say so."
)
# Answers from Stack Exchange (free) are people's advice, not checked facts: said that way
_ANSWER_FROM_QA = (
    "\nThese are questions people asked on {where}, each with the answer others voted best. "
    "If one fits the user's question, tell the user in one to three sentences what people there suggest, "
    "the way you'd tell a friend (like 'People who've done it say...'). Use only what these answers "
    "actually say; never add steps or facts of your own. Don't number them or mention votes. "
    "If none is about the user's question, say you couldn't find a good answer there and ask if they want you "
    "to search the web."
)
# "Search the web for it" / "look it up online": skip the free sources
_SEARCH_ONLINE = re.compile(r"\b(?:search|look|check)\b[^.?!]*\b(?:the web|online|the internet|tavily)\b", re.I)
# "What is permaculture?", "Who was Nikola Tesla?": the Wikipedia article's opening
_DEFINITION = re.compile(r"^\W*(?:what(?:'s| is| are| was| were)\s+(?:an?\s+)?|who(?:'s| is| was| were)\s+|"
                         r"tell me (?:a little bit |a little |a bit |something |more |all )?about\s+)"
                         r"(?P<x>[^?.!]{2,60}?)\W*$", re.I)


def _say_title(t: str) -> str:
    """A video or pattern title as it can be spoken: no brackets, hashtags or emoji."""
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)|#\w+", " ", t or "")
    t = re.sub(r"[^\w\s,.'&:!?+-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -:|,")
    return " ".join(t.split()[:14])


def _twitch_line(t: dict) -> str:
    name, n, viewers = t["name"], t["streams"], t["viewers"]
    many = t.get("more") and n >= 90
    lead = (f"Right now there are more than 100 live {name} streams on Twitch. The top hundred have"
            if many else f"Right now there {'is' if n == 1 else 'are'} {n} live {name} stream{'' if n == 1 else 's'} on Twitch, with")
    if not n:
        return f"Nobody's streaming {name} on Twitch right now."
    text = f"{lead} about {viewers:,} viewers between them"
    if t.get("top"):
        text += f", and the biggest, {t['top']}, has {t['top_viewers']:,}"
    text += "."
    if t.get("small"):
        text += f" {t['small']} of those have fewer than 20 viewers."
    return text


def _videos_line(vids: list) -> str:
    parts = [f"\"{_say_title(v['title'])}\" from {_say_title(v['channel'])}" for v in vids]
    listed = parts[0] if len(parts) == 1 else "; ".join(parts[:-1]) + f"; and {parts[-1]}"
    return f"On YouTube, a few that look good: {listed}."


def _patterns_line(pats: list) -> str:
    lines = []
    for p in pats[:4]:
        bits = [b for b in (p.get("level"), "free" if p.get("free") else "") if b]
        desc = f" ({', '.join(bits)})" if bits else ""
        by = f" by {p['designer']}" if p.get("designer") else ""
        lines.append(f"{_say_title(p['title'])}{by}{desc}")
    listed = lines[0] if len(lines) == 1 else "; ".join(lines[:-1]) + f"; and {lines[-1]}"
    return f"From Ravelry, some popular ones: {listed}. Want more on any of them?"


# Said while Tavily searches (2-3 s of silence otherwise), like "Let me look." for the camera.
# Fits what the user asked and never repeats back to back ("Good question, let me look." every
# other turn sounded canned).
_LOOKUP_LEADS = {
    "news": ["Let me pull up the headlines.", "Okay, checking the news.", "One sec, grabbing the latest."],
    "weather": ["Let me check the forecast.", "Checking the weather.", "One sec, looking at the forecast."],
    "sports": ["Let me check how that went.", "One sec, checking the score.", "Let me look that game up."],
    "general": ["One sec.", "Let me find out.", "Hmm, let me check.", "Let me look into that.",
                "Give me a second.", "Let me see.", "Checking now."],
}
_LEAD_KIND = [
    ("news", re.compile(r"\b(?:news|headlines?|current events|what'?s happening|elections?|vote|votes|"
                        r"ballot|congress|senate|president|governor|mayor|court|war|politic\w*)\b", re.I)),
    ("weather", re.compile(r"\b(?:weather|forecast|rain|snow|temperature|sunny|windy|degrees)\b", re.I)),
    ("sports", re.compile(r"\b(?:game|score|match|won|lost|beat|playoffs?|quarterback|season|team|race)\b", re.I)),
]
_last_lead = ""


def _lookup_lead(text: str = "") -> str:
    global _last_lead
    import random
    kind = next((k for k, rx in _LEAD_KIND if rx.search(text)), "general")
    choices = [c for c in _LOOKUP_LEADS[kind] if c != _last_lead] or _LOOKUP_LEADS[kind]
    _last_lead = random.choice(choices)
    return _last_lead


# Result lists read like someone telling you, in order (so "the second one" still works)
_ORDINALS = ["First", "Next", "Then", "Also", "And", "Plus", "And", "Lastly"]


# ---------------------------------------------------------------- helpers
def load_profile() -> str:
    try:
        return PROFILE_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].rstrip("-") or "note"


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
# "tomorrow", "next Sunday", ... unless a date already follows it
_RELATIVE_DAY = re.compile(
    r"\b(day after tomorrow|tomorrow|today|tonight|(?:this |next )?(" + "|".join(_WEEKDAYS) + r"))\b"
    r"(?!\s*\()(?!,?\s+(?:the\s+)?\d)"
    r"(?!,?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec))",
    re.IGNORECASE,
)


def _resolve_dates(text: str, base: datetime) -> str:
    """Pin relative days to real dates, relative to when the note was saved:
    'appointment tomorrow morning' -> 'appointment tomorrow (Sunday, November 8) morning'.
    Safe to run twice: words already followed by '(' are skipped."""
    def sub(m: re.Match) -> str:
        phrase = m.group(1).lower()
        if phrase == "day after tomorrow":
            offset = 2
        elif phrase == "tomorrow":
            offset = 1
        elif phrase in ("today", "tonight"):
            offset = 0
        else:
            offset = (_WEEKDAYS.index(m.group(2).lower()) - base.weekday()) % 7
            if offset == 0 and not phrase.startswith("this"):
                offset = 7  # "Saturday" said on a Saturday means next week
            day = base + timedelta(days=offset)
            return f"{m.group(0)} ({day:%B} {day.day})"
        day = base + timedelta(days=offset)
        return f"{m.group(0)} ({day:%A}, {day:%B} {day.day})"
    return _RELATIVE_DAY.sub(sub, text)


def _clean_tags(tags) -> str:
    tags = str(tags or "").replace("\n", " ").strip()
    return "" if tags.lower() in ("null", "none", "n/a") else tags


def _read_note(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
        meta, body = {}, raw
        if raw.startswith("---"):
            _, head, body = raw.split("---", 2)
            for line in head.strip().splitlines():
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
        saved = datetime.strptime(meta.get("date", "")[:16], "%Y-%m-%d %H:%M")
        return {
            "saved": saved,
            "tags": _clean_tags(meta.get("tags")),
            # also fixes notes saved before dates were resolved
            "text": _resolve_dates(body.strip(), saved),
        }
    except Exception:
        return None


def _when_saved(saved: datetime) -> str:
    return f"saved {saved:%A}, {saved:%B} {saved.day} at {saved:%I:%M %p}".replace(" 0", " ")


# words that say nothing about which note is wanted
_FILLER = set(
    "the and for you your my mine me what whats when where who which have has had do did does "
    "any anything something going got about tell please can could would will with that this "
    "there are is was were been know remember told note notes saved "
    "say said saying mention mentioned anyone ask asked hey othos okay".split()
)
# asking about plans should find appointments and the like
_SYNONYMS = {
    w: ["appointment", "meeting", "plan", "event"]
    for w in ("schedule", "plans", "plan", "calendar", "events", "busy", "agenda", "happening")
}


# ---------------------------------------------------------------- note meanings (embeddings)
# Each note also gets a vector from a small local embedding model in Ollama, so
# "what did I say about the generator?" finds "the Honda needs an oil change"
# even with no word in common. Keyword search still runs and is blended in; if
# Ollama or the model is unavailable, notes search is keyword-only as before.
# Vectors live in brain/note_vectors.json (one entry per note file).
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
# How long Ollama keeps a model loaded after a request. Negative = until Ollama
# stops, so neither model reloads mid-conversation (default is 5 minutes).
KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "-1m")
_OLLAMA_ROOT = re.sub(r"/v1/?$", "", os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1"))
# Camera "look": a small local vision model in Ollama. Images go only from the
# browser to the local LiveKit server to this agent to Ollama, all on this laptop.
# The vision model loads when asked to look and unloads after VISION_KEEP_ALIVE.
VISION_MODEL = os.getenv("VISION_MODEL", "moondream:1.8b")
VISION_KEEP_ALIVE = os.getenv("VISION_KEEP_ALIVE", "5m")
VECTORS_PATH = BRAIN_DIR / "note_vectors.json"
SEM_MIN = float(os.getenv("NOTES_SEM_MIN", "0.60"))  # at or above: clearly about it
# Short questions score lower ("what did I say about the backup power?" vs the
# generator note: 0.53, but "car insurance" scores the same). When nothing is
# clearly about it, a best note of at least SEM_FLOOR that stands ahead of the
# next one is offered to the model to judge, instead of the recent notes.
SEM_FLOOR = float(os.getenv("NOTES_SEM_FLOOR", "0.45"))
SEM_MARGIN = float(os.getenv("NOTES_SEM_MARGIN", "0.07"))
_vectors_lock = threading.Lock()


def _note_doc(n: dict) -> str:
    """The text embedded for a note (nomic needs the 'search_document:' prefix)."""
    return f"search_document: {n['text']} {n['tags']}".strip()


def _doc_key(doc: str) -> str:
    return hashlib.sha1(f"{EMBED_MODEL}\n{doc}".encode("utf-8")).hexdigest()[:16]


def _load_vectors() -> dict:
    try:
        return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _store_vectors(updates: dict, keep: set | None = None) -> None:
    """Merge {note file name: {key, v}} into the vectors file. keep = names of
    notes that still exist (others are dropped). Written whole, then swapped in."""
    with _vectors_lock:
        data = _load_vectors()
        data.update(updates)
        if keep is not None:
            data = {k: v for k, v in data.items() if k in keep}
        tmp = VECTORS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, VECTORS_PATH)


def _embed_body(texts: list[str]) -> dict:
    return {"model": EMBED_MODEL, "input": texts, "keep_alive": KEEP_ALIVE}


def _embed_sync(texts: list[str], timeout: float = 30) -> list | None:
    try:
        r = httpx.post(f"{_OLLAMA_ROOT}/api/embed", json=_embed_body(texts), timeout=timeout)
        r.raise_for_status()
        return r.json()["embeddings"]
    except Exception as e:
        log.info("embedding unavailable (%s): notes search is keyword-only", e)
        return None


_embed_client = None  # made once per process: setting one up costs ~250 ms here


async def _embed_async(texts: list[str], timeout: float = 3) -> list | None:
    global _embed_client
    try:
        if _embed_client is None:
            _embed_client = httpx.AsyncClient()
        r = await _embed_client.post(f"{_OLLAMA_ROOT}/api/embed", json=_embed_body(texts),
                                     timeout=httpx.Timeout(timeout, connect=0.5))  # Ollama is local
        r.raise_for_status()
        return r.json()["embeddings"]
    except Exception as e:
        log.info("embedding unavailable (%s): notes search is keyword-only", e)
        return None


def _cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _missing_vectors(notes: list, vectors: dict) -> list:
    """(name, doc, key) for notes with no vector, or one made from older text."""
    out = []
    for name, n in notes:
        doc = _note_doc(n)
        key = _doc_key(doc)
        if vectors.get(name, {}).get("key") != key:
            out.append((name, doc, key))
    return out


def backfill_note_vectors() -> int:
    """Embed every note that has no vector yet; drop vectors of deleted notes.
    Runs in the background when the worker starts. Returns notes embedded."""
    notes = [(p.name, n) for p in NOTES_DIR.glob("*.md") if (n := _read_note(p))]
    missing = _missing_vectors(notes, _load_vectors())
    done = {}
    for i in range(0, len(missing), 32):
        batch = missing[i:i + 32]
        vecs = _embed_sync([doc for _, doc, _ in batch])
        if vecs is None:
            break
        done.update({name: {"key": key, "v": v} for (name, _, key), v in zip(batch, vecs)})
    _store_vectors(done, keep={name for name, _ in notes})
    if done:
        log.info("embedded %d note(s) for meaning search", len(done))
    return len(done)


def _warm_models() -> None:
    """Load both models in Ollama and keep them loaded (runs in a background
    thread at worker start, so it never holds up a call)."""
    try:
        if llm_server.enabled():
            # the conversation runs on llama-server (llm_server.py): start it; Ollama keeps notes search and the camera
            if not llm_server.start(os.getenv("OLLAMA_MODEL", "llama3.2:3b")):
                log.warning("llama-server isn't running: calls can't get replies until it does (voice-llm.json)")
            else:
                # Ollama's own copy of the same model would stay loaded (keep_alive -1m) and double the memory
                httpx.post(f"{_OLLAMA_ROOT}/api/generate", timeout=30,
                           json={"model": os.getenv("OLLAMA_MODEL", "llama3.2:3b"), "keep_alive": 0})
        else:
            httpx.post(f"{_OLLAMA_ROOT}/api/generate", timeout=60,
                       json={"model": os.getenv("OLLAMA_MODEL", "llama3.2:3b"), "keep_alive": KEEP_ALIVE})
        if not backfill_note_vectors():
            _embed_sync(["search_query: warm up"])
    except Exception as e:
        log.info("model warm-up skipped: %s", e)


# ---------------------------------------------------------------- tool-call leak guard
# Small local models sometimes write a tool call as plain text, e.g.
#   "name": "search_notes", "parameters": {"query": "..."}}
# instead of making a real call. Unchecked, the voice reads that aloud.
log = logging.getLogger("othos")
# A tool name said as a word at the start of a reply, on a turn with no tools:
# "Save_note Yam would be funnier." (live call 04:23) -> "Yam would be funnier."
# (only the underscore names: "look" is an ordinary word, as in "Look at that!")
_TOOL_WORD = re.compile(r"^\s*(?:save_note|search_notes|web_search|more_results)\b[\s:(\"']*", re.I)
_LEAK_START = re.compile(r'^\s*[\[{]?\s*"?(name|function|tool_call)"?\s*[:=]', re.IGNORECASE)
_LEAK_CHECK_CHARS = 12  # characters held back before deciding it is normal speech
# Replies that say Othos is searching right now. On a turn with no search, a reply is held back only while it could
# still turn out to be one of these, so "I'm glad..." or "Let me think" is spoken at once.
_CLAIMS = ("let me check", "let me look", "let me search", "let me see if i can find", "let me find", "let me google",
           "let me do a search", "let me do a web search", "let me do a quick search", "let me do a quick web search",
           "i'll search", "i'll check", "i'll look that up", "i'll look it up", "i'll do a search", "i'll do a web search",
           "i'll do a quick search", "i'll do a quick web search", "i'll run a search", "i'll run a web search",
           "i'll run a quick search", "i will search", "i will check", "i will do a web search", "i'm searching",
           "i'm checking", "i'm looking that up", "i'm looking it up", "searching now", "searching for", "checking now",
           # said as done, with nothing searched (Nov 19, 13:34: "I've looked up some options for you" after a yes)
           "i've looked up", "i have looked up", "i've searched", "i have searched", "i've done a search",
           "i've done a web search", "i've run a search")
_CLAIM_LEAD = re.compile(r"^(?:ok(?:ay)?|sure|alright|all right)\b\W*")


def _claim_state(held: str) -> str:
    """'yes' (the reply claims a lookup), 'maybe' (it still could) or 'no'."""
    t = re.sub(r"\s+", " ", held.replace("’", "'").lower()).lstrip(" \t\n\"'.,!?-")
    if t and any(w.startswith(t) for w in ("okay", "ok", "sure", "alright", "all right")):
        return "maybe"
    t = _CLAIM_LEAD.sub("", t)
    for c in _CLAIMS:
        if t.startswith(c) and (len(t) == len(c) or not t[len(c)].isalnum()):
            return "yes"
    return "maybe" if any(c.startswith(t.rstrip()) for c in _CLAIMS) and not t.endswith((".", "!", "?")) else "no"
# llama3.2 sometimes starts a reply with its own role label: "assistant\n\nHello..."
_ROLE_PREFIX = re.compile(r"^\s*assistant\s*:?\s*\n+", re.IGNORECASE)
_ROLE_ONLY = re.compile(r"^\s*assistant\s*[:.!]?\s*$", re.IGNORECASE)  # checked once the reply has ended


# The model said "Good morning" at 12:19 AM even with the time in its
# instructions (by the clock it is morning), so code picks the phrase.
_TIME_GREETING = re.compile(r"\bgood (morning|afternoon|evening|day)\b", re.I)


def _greeting_for(hour: int) -> str:
    if 5 <= hour < 12:
        return "Good morning"
    if 12 <= hour < 17:
        return "Good afternoon"
    if 17 <= hour < 22:
        return "Good evening"
    return "Hello"  # late at night


_OPENING_ASKS = ("How's it going?", "How's your day going so far?", "What's new?", "How are things?")


def _opening_line(hour: int, name: str | None, pick: int | None = None) -> str:
    """The call's first words, by code: 'Good afternoon, the user. How's it going?'"""
    import random
    asks = _OPENING_ASKS if 5 <= hour < 22 else ("How's it going?", "What's new?", "How are things?")   # no "so far" late at night
    ask = asks[pick % len(asks)] if pick is not None else random.choice(asks)
    return f"{_greeting_for(hour)}{', ' + name if name else ''}. {ask}"


def _fix_greeting(text: str, hour: int | None = None) -> str:
    phrase = _greeting_for(datetime.now().hour if hour is None else hour)
    return _TIME_GREETING.sub(lambda m: phrase if m.group(0)[0].isupper() else phrase.lower(), text)


# A plain thank-you or goodbye, with nothing else asked. Anything longer, or
# with a question or request in it, still goes to the model.
_THANKS = re.compile(r"\b(thank you|thanks|thank u|cheers|much appreciated|appreciate it)\b", re.I)
_ASKS_SOMETHING = re.compile(
    r"\?|\b(what|which|who|where|when|why|how|can|could|would|will|please|but|tell|search|look|find|"
    r"remember|remind|save|note|more|next)\b", re.I)
_THANKS_REPLIES = ["You're welcome!", "Happy to help!", "Anytime!"]
_quick_turn = 0


_WHATS_UP_REPLIES = ["Not much on my end, just here and ready to help. What's on your mind?",
                     "All quiet here. What can I do for you?",
                     "Not much, just ready when you are. What's up with you?"]
_HOW_ARE_YOU_REPLIES = ["I'm doing well, thanks for asking! How about you?",
                        "All good here, thanks! How are you doing?",
                        "Doing great, thanks! How's your day going?"]


# "Othos, stop." (heard as "Oh, this stop." on the 8:48 AM call), "never mind", "that's enough"
_STOP = re.compile(
    r"^\W*(?:(?:oh|ok|okay|hey|um|uh|othos|otis|osos|azus|azis|this|please|just|alright|all right)\W+)*"
    r"(?:stop(?: talking| it| that| there)?|never ?mind|that'?s (?:enough|fine)|enough|cancel(?: that)?|"
    r"forget it|quiet|shh+|hush|hold on|wait)(?:\W+(?:please|othos|thanks|thank you))*\W*$", re.I)


# After Othos asks how the user is: "Well, thank you." got "Happy to help!" (9:05 AM call)
_ASKED_HOW = re.compile(r"\bhow(?:'s| is| are| was)\b[^?]*\b(?:you|your|day|morning|afternoon|evening|"
                        r"going|doing|shaping|starting)\b[^?]*\?\s*$", re.I)
_DOING_WELL = re.compile(
    r"^\W*(?:(?:oh|yeah|yes|it'?s|i'?m|we'?re|all|so far)\W+)*(?:(?:going|doing|been|pretty|very|really|quite|so)\W+)*"
    r"(?:well|good|great|fine|okay|ok|alright|not bad|so good|wonderful|excellent)\b", re.I)
_GLAD_REPLIES = ["Glad to hear it! What's on your mind?", "Good to hear. What can I do for you?",
                 "Nice. What are you up to?"]
# "Okay. I'm a little tired." got "Good to hear." (12:07 PM call)
_NOT_WELL = re.compile(r"\b(?:tired|exhausted|sleepy|worn out|sick|ill|unwell|rough|bad|awful|terrible|"
                       r"stressed|sad|down|meh|so-so|not (?:great|good|well|so good|the best)|could be better|"
                       r"hurts?|headache|sore|achy|cold|grumpy|cranky|frustrated|anxious|overwhelmed)\b", re.I)
_SORRY_REPLIES = ["Sorry to hear that. Anything I can do to make it easier?",
                  "Ah, that's rough. Want to take it easy, or is there something I can help with?",
                  "Sorry you're feeling that way. I'm here if you need anything."]
# "That's everything you found?" after "That's everything I found." got "I don't know what else to say."
_ALL_FOUND_Q = re.compile(r"^\W*(?:is\s+)?(?:that'?s|that is|is that)\s+(?:everything|all|it)\b", re.I)
# "No." to "Want to hear more?" / "Want the headlines?" went to the model (9:05 AM call)
_ASKED_MORE = re.compile(r"\b(?:want to hear more|want the headlines|want me to keep going|"
                         r"want (?:me to read )?(?:more|the rest))\?\s*$", re.I)
_NO_THANKS = re.compile(r"^\W*(?:no|nope|nah|no thanks|no thank you|not now|that'?s all|i'?m good|"
                        r"that'?s (?:it|enough))\W*(?:othos|thanks|thank you)?\W*$", re.I)
# a turn that's only a filler word ("Also,") while the user gathers their thoughts
_FILLER_ONLY = re.compile(r"^\W*(?:also|and|so|um+|uh+|hmm+|er+|well|but|oh|like|okay so|ok so|"
                          r"othos|otis|orthos|othus)\W*$", re.I)


# A turn cut off mid-question: it ends on a word that can't end a sentence and has no closing mark ("What are its",
# Nov 19 call: the rest, "last two task results?", came as its own turn and neither half was understood)
_CUT_OFF = re.compile(r"\b(?:its|the|a|an|my|your|our|their|of|with|and|or|but|from|some|any|if|because|than|into|"
                      r"to|about|for|whether|which|whose|what|why|how|when|where|who)\s*,?\s*$"
                      r"|\b(?:can|could|shall|should|will|would|do|does|did|may|might)\s+(?:we|you|i|they)\s*,?\s*$"
                      r"|\b(?:what|who|where|when|why|how|which)\s+(?:is|was|are|were|do|does|did|can|could|would|should|will|has|have|had)"
                      r"\s*,?\s*$",
                      re.I)   # the second half: "Okay. Can we" (Nov 19, 13:34: answered with "Sorry, I didn't catch...")


def _dangling(text: str) -> bool:
    """True for a turn the user hasn't finished ("What are its", "Can you tell me about the")."""
    t = (text or "").strip()
    return bool(t) and not re.search(r"[.?!]\W*$", t) and bool(_CUT_OFF.search(t))


def _filler_turn(text: str, pending: list[str]) -> str | None:
    """A turn that's only "Oh," / "Also,": 'silent' (the user is still talking), or 'answer_pending' when the turn
    before it is still unanswered (Nov 18: the user's "Oh," dropped the reply to Tina's "Hello, Othos. How are you?").
    None when it isn't a filler-only turn."""
    if not _FILLER_ONLY.match(text.strip()):
        return None
    real = [p for p in pending if not _FILLER_ONLY.match(p.strip())]   # "Also," then "um": still waiting
    return "answer_pending" if real else "silent"


def _earlier_question(chat_ctx) -> str | None:
    """The search query for the user's last real question before this turn (skipping "Please do." and "Go ahead."),
    or None. For a yes to Othos's own "Can I look that up?" (Nov 19 call: "Please do" never searched).
    A short fragment between ("Plant nurseries.", said to sharpen the question) is skipped and added to the query
    (Nov 19, 13:33: the walk stopped at the fragment, found nothing, and no search ran)."""
    fragments: list[str] = []
    for i in reversed(chat_ctx.items[:-1]):
        if not (isinstance(i, llm.ChatMessage) and i.role == "user"):
            continue
        said = (i.text_content or "").strip()
        if not said or _YES_ONLY.match(said) or _FILLER_ONLY.match(said):
            continue
        q = _searchable(said, chat_ctx)
        if q:
            extra = " ".join(re.sub(r"[.?!,]+$", "", f).strip() for f in reversed(fragments))
            return f"{extra} {q}".strip() if extra else q
        if _ABOUT_OTHOS.search(said) and _QUESTION.search(said):
            continue   # "What resource did you pull that from?": about Othos, not the topic (Nov 20, 7:52 call)
        if len(said.split()) <= 5:
            fragments.append(said)
            continue
        return None
    return None


def _searchable(said: str, chat_ctx) -> str | None:
    """The search query for a turn that asks something ("Any new businesses in X?"), or None for small talk, so a
    promised lookup never searches "It's a sunny morning"."""
    said = _as_othos(said)
    if not (_QUESTION.search(said) or _SEARCH_WORDS.search(said) or _LOCAL_NEW.search(said) or _DEFINITION.search(said)
            or _ASKS_SEARCH.search(said)):
        return None
    return _query_from(said, chat_ctx)


def _offered_lookup(chat_ctx) -> bool:
    """Othos's last reply offered to look something up, or said it would."""
    said = next((i.text_content or "" for i in reversed(chat_ctx.items[:-1])
                 if isinstance(i, llm.ChatMessage) and i.role == "assistant"), "")
    return bool(_OFFERS_LOOKUP.search(said))


# what Othos's own offer said it would look up: "Would you like me to search for more specific information on plants
# for dappled shade?" -> "plants for dappled shade" (Nov 20, 7:52 call: her "Yes." searched nothing)
_OFFER_TOPIC = re.compile(
    r"\b(?:search(?:\s+the\s+web)?\s+for|look\s+up|look\s+into|find(?:\s+out)?(?:\s+more)?\s+about|check\s+on|"
    r"(?:information|info|details|recommendations|suggestions)\s+(?:on|about|for))\s+"
    r"(?:(?:some|more|specific|detailed|the\s+latest|current)\s+)*(?:(?:information|info|details)\s+(?:on|about)\s+)?"
    r"(?P<t>[^.?!]{3,90})", re.I)
_OFFER_TAIL = re.compile(r"\s+(?:for you|if you(?:'d| would) like|if you want|online|on the web|right now|now)\b.*$",
                         re.I)


def _last_said(chat_ctx) -> str:
    """Othos's last reply before this turn."""
    return next((i.text_content or "" for i in reversed(chat_ctx.items[:-1])
                 if isinstance(i, llm.ChatMessage) and i.role == "assistant"), "")


def _last_reply_searched(chat_ctx) -> bool:
    """Did Othos's last reply come from a search (a tool result between her question and that reply)?"""
    seen_reply = False
    for i in reversed(chat_ctx.items[:-1]):
        if isinstance(i, llm.ChatMessage) and i.role == "assistant":
            seen_reply = True
        elif isinstance(i, llm.FunctionCallOutput) and seen_reply:
            return True
        elif isinstance(i, llm.ChatMessage) and i.role == "user" and seen_reply:
            return False
    return False


# Othos asked what to search ("I haven't looked anything up yet. What would you like me to search for?")
_ASKED_WHAT_TO_SEARCH = re.compile(r"\bwhat (?:would you like|do you want|should|shall) (?:me to |i )?(?:search|look up|"
                                   r"look for|find)\b", re.I)
_NOTHING_TO_SEARCH = re.compile(r"^\W*(?:no|nope|nothing|never ?mind|forget (?:it|about it)|not now|that's (?:ok|okay|"
                                r"fine|all)|it's (?:ok|okay|fine)|skip it)\b", re.I)
# "Can you cite your source on that?", "Where did you get that?" (Nov 20, 8:47 call)
_ASKS_WHERE_FROM = re.compile(r"\b(?:cite (?:your|the|a|any) sources?|(?:what(?:'s| is| was| are)|which (?:is|was)) (?:your|the) "
                          r"sources?|where did you (?:get|find|hear|learn|read|pull|see) (?:that|this|it)|what (?:resource|"
                          r"source|website|site) did you|sources? (?:on|for) that|how do you know (?:that|this))\b", re.I)


def _bare_topic(text: str) -> str | None:
    """A turn that is only a topic ("Broadforks."), as a query; None for anything longer than a few words."""
    t = re.sub(r"^\W*(?:(?:um|uh|well|okay|ok|so)\W+)*(?:othos\W+)?", "", _as_othos(text or ""), flags=re.I)
    t = re.sub(r"[\s.!?,]+$", "", t).strip()
    words = re.findall(r"[\w'-]+", t)
    if not words or len(words) > 6 or _PERSONAL.search(t) or _REFERS_BACK.fullmatch(t):
        return None
    return t


def _offer_topic(chat_ctx) -> str | None:
    """The topic Othos named in its last reply's offer to look something up, or None."""
    said = next((i.text_content or "" for i in reversed(chat_ctx.items[:-1])
                 if isinstance(i, llm.ChatMessage) and i.role == "assistant"), "")
    if not _OFFERS_LOOKUP.search(said):
        return None
    found = [m.group("t") for m in _OFFER_TOPIC.finditer(said)]
    for t in reversed(found):                      # the offer itself usually comes last
        t = _OFFER_TAIL.sub("", t).strip(" ,;:")
        if len(re.findall(r"\w+", t)) >= 2 and not re.fullmatch(r"(?i)(?:it|that|this|anything|something|more)\b.*", t):
            return t
    return None


SELF_CHECK_MEMORY_S = 600  # "Could you expand on that?" is about the self-check for this long after it
SEARCH_SILENCE_S = 3.0     # a turn that may search says "One moment." after this long with nothing from the model
_SILENT = object()


def _has_words(chunk) -> bool:
    if isinstance(chunk, str):
        return bool(chunk.strip())
    if isinstance(chunk, llm.ChatChunk) and chunk.delta:
        return bool((chunk.delta.content or "").strip() or chunk.delta.tool_calls)
    return False


async def _say_if_silent(stream, wait: float):
    """The model's stream, with _SILENT put in once if nothing with words (or a tool call) came within `wait` s."""
    it = stream.__aiter__()
    deadline = time.monotonic() + wait
    waiting = True                  # no words yet, and the filler not said yet
    nxt = None
    try:
        while True:
            nxt = asyncio.ensure_future(it.__anext__())
            if waiting:
                done, _ = await asyncio.wait({nxt}, timeout=max(0.0, deadline - time.monotonic()))
                if not done:
                    waiting = False
                    yield _SILENT
            try:
                chunk = await nxt
            except StopAsyncIteration:
                return
            nxt = None
            if waiting and _has_words(chunk):
                waiting = False
            yield chunk
    finally:
        if nxt is not None and not nxt.done():
            nxt.cancel()


_ASKS_OPENER = re.compile(r"^\W*(?:(?:hey|hi|ok|okay|so|and|but|also|well)\W+)*(?:othos\W+)?(?:(?:can|could|would|will|"
                          r"do|does|did|is|are|should|tell|show|find|give)\b)", re.I)


def _whole_question(text: str) -> bool:
    """A turn that asks something complete by itself ("Othos, what's the best soil mix for raised beds?"), so a cut-off
    start just before it was abandoned, not continued ("What are its" + "last two task results?" stays joined)."""
    t = _as_othos(text or "").strip()
    if len(re.findall(r"\w+", t)) < 4 or _dangling(t):
        return False
    return bool(_QUESTION_OPENER.match(t) or _ASKS_OPENER.match(t))


def _unanswered(chat_ctx) -> list[str]:
    """What the user side said since Othos last spoke, oldest first, without this turn."""
    out: list[str] = []
    for i in reversed(chat_ctx.items[:-1]):
        if isinstance(i, llm.ChatMessage):
            if i.role == "assistant":
                break
            if i.role == "user":
                out.insert(0, i.text_content or "")
    return out


# ---------------------------------------------------------------- a guest in the room (Nov 18: Tina on speakerphone)
# One microphone can't tell voices apart, so a guest is known from the introduction. While a guest is there, Othos
# answers only when someone says its name or right after it asked something: the two of them talking to each other
# ("He's still thinking.", "Do I say hello?") cut off every reply and got none of their own.
_G = holdon.NAME_RX
_GUEST_IN = re.compile(
    rf"\b(?i:my)\s+(?i:{holdon.RELATION_RX})\s+(?P<a>{_G})(?:\s+(?i:is)|'s)\s+(?i:here|with me|on the phone|on speaker|"
    rf"joining us|meeting you|listening)\b|"
    rf"\b(?P<b>{_G})(?:\s+(?i:is)|'s)\s+(?i:here with me|on the phone with me|on speaker|joining us|meeting you)\b|"
    rf"\b(?i:this is my)\s+(?i:{holdon.RELATION_RX})\s+(?P<c>{_G})\b|"
    rf"\b(?i:say hi to|say hello to)\s+(?:(?i:my)\s+(?i:{holdon.RELATION_RX})\s+)?(?P<d>{_G})\b|"
    rf"^\W*(?:(?i:othos)\W+)?(?i:meet)\s+(?:(?i:my)\s+(?i:{holdon.RELATION_RX})\s+)?(?P<e>{_G})\b")
_ADDRESSED = re.compile(r"\b(?:othos|osos|otis|orthos|othus)\b", re.I)


def _guest_intro(text: str) -> str | None:
    """The guest's name when this turn introduces someone ("my friend Tina is meeting you"), else None."""
    for m in _GUEST_IN.finditer(text):
        name = next((g for g in m.groups() if g), None)
        if name and holdon.name_ok(name):
            return name
    return None


def _guest_left(text: str, guest: str) -> bool:
    """'Tina had to go', 'Tina's gone', 'Bye, Tina!'."""
    g = re.escape(guest)
    return bool(re.search(rf"\b{g}(?:'s|\s+(?:has|is))?\s+(?:gone|left|off the phone|hung up)\b|"
                          rf"\b{g}\s+(?:had to go|has to go|had to leave|has to leave|hung up)\b|"
                          rf"\b(?:bye|goodbye|see you),?\s+{g}\b", text, re.I))


def _addressed(text: str) -> bool:
    """Someone said Othos's name (speech-to-text's usual mishearings included)."""
    return bool(_ADDRESSED.search(_as_othos(text)))


def _guest_turn(text: str, guest: str | None, pending: list[str], last_reply: str,
                speaker: str | None = None, asked: str | None = None) -> str:
    """What to do with this turn: 'reply' as usual, 'silent' (the people are talking to each other), or
    'answer_pending' (a side remark came right after a question to Othos: answer that question).
    With speaker labels (voices.py), an answer to Othos's question counts only from the person it was asking."""
    if not guest or _addressed(text):
        return "reply"
    if not pending and last_reply.rstrip().endswith("?") and (speaker is None or asked is None or speaker == asked):
        return "reply"   # Othos just asked something: this is the answer
    if any(_addressed(p) for p in pending):
        return "answer_pending"
    return "silent"


# "Oh, so stop." (Othos, stop) went to the model and took 61 s (12:25 PM call): any short
# turn with a stop word is a stop, however speech-to-text garbles "Othos"
_SHORT_STOP = re.compile(r"\b(?:stop|enough|never ?mind|quiet|hush|shh+|cancel)\b", re.I)
_NOT_STOP = re.compile(r"\bstop (?:by|at|in|the|a|my|your|for|sign|light)\b|\b(?:bus|next|last|first) stop\b|"
                       r"\benough (?:of|for|to|time|money|food|room)\b", re.I)


def _quick_reply(text: str, name: str | None, asked: str = "") -> str | None:
    """`asked` is Othos's previous reply (for "Well, thank you" after "How's your day?")."""
    global _quick_turn
    if _STOP.match(text.strip()) or (len(re.findall(r"[\w']+", text)) <= 4 and _SHORT_STOP.search(text)
                                     and not _NOT_STOP.search(text) and "?" not in text):
        return "Okay."
    if asked and _ASKED_MORE.search(asked) and _NO_THANKS.match(text.strip()):
        return "Okay."
    if asked and "everything i found" in asked.lower() and _ALL_FOUND_Q.match(text.strip()):
        return "That's all I found for now. Want me to look into a particular story?"
    if asked and _ASKED_HOW.search(asked) and len(text.split()) <= 12 and "?" not in text:
        if _NOT_WELL.search(text):  # "Okay. I'm a little tired." is not "good to hear"
            _quick_turn += 1
            return _SORRY_REPLIES[_quick_turn % len(_SORRY_REPLIES)]
        if _DOING_WELL.match(text.strip()) and len(text.split()) <= 8:
            _quick_turn += 1
            return _GLAD_REPLIES[_quick_turn % len(_GLAD_REPLIES)]
    # Pure chit-chat ("Hello. What's going on?"): answered by code. The model drifted
    # into the time of day ("It's Sunday morning, still dark outside...") or claimed
    # to see things.
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
    chat = [s for s in sentences if _SMALL_TALK.match(s)]
    if chat and all(_SMALL_TALK.match(s) or _GREETING_ONLY.match(s) for s in sentences):
        _quick_turn += 1
        pool = _HOW_ARE_YOU_REPLIES if re.search(r"\bhow\b|\byou (?:ok|okay|alright)", chat[-1], re.I) \
            else _WHATS_UP_REPLIES
        return pool[_quick_turn % len(pool)]
    if len(text.split()) > 10 or _ASKS_SOMETHING.search(text):
        return None
    if _GOODBYE.search(text):
        return f"Goodbye, {name}! Talk soon." if name else "Goodbye! Talk soon."
    if _THANKS.search(text):
        _quick_turn += 1
        return _THANKS_REPLIES[_quick_turn % len(_THANKS_REPLIES)]
    return None


def _parse_leaked_call(text: str, tool_names: set[str]) -> tuple[str, str] | None:
    """Turn leaked tool-call text into (name, json arguments), if it names a real tool."""
    s = text.strip().lstrip("[")
    if not s.startswith("{"):
        s = "{" + s
    for candidate in (s, s + "}", s + "}}"):
        try:
            obj, _ = json.JSONDecoder().raw_decode(candidate)
            break
        except ValueError:
            continue
    else:
        return None
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("function"), dict):  # {"function": {"name": ..., "arguments": ...}}
        obj = obj["function"]
    name = obj.get("name") or obj.get("function")
    args = obj.get("parameters", obj.get("arguments", {}))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    if name not in tool_names or not isinstance(args, dict):
        return None
    return name, json.dumps(args)


# ---------------------------------------------------------------- tool gating
# Checked in this order. Personal wins over search: "when is my appointment"
# goes to the notes, not the web.
_ASKS_NOTES = re.compile(
    r"\b(remember|note|notes|save|saved|remind|appointments?|schedule|calendar|plans?|planned|"
    r"meetings?(?!\s+(?:you|him|her|them|us|othos)\b)|birthday|anniversary|told you|did i (say|tell|mention)|do i have(?! to\b)|"  # not "how old do I have to be"

    r"am i (busy|free|doing)|don'?t (let me )?forget)\b",
    re.IGNORECASE,
)
_ASKS_SEARCH = re.compile(
    r"\b(search (for|the web|online|on|up|about)|^\s*(please\s+)?search|look (it |that |this )?up|"
    r"google (it|that|this|for)|find out|news|headlines?|latest|current|price|prices|cost|"
    r"how much|weather|forecast|what (is|are|was|were|does|do)|what's (a|an|the)|"
    r"who (is|was|are|were|won|wins|made|makes|invented|owns|plays)|who's|winner|score|results of|where (is|are|was|can|do)|when (is|was|did|does|do|will)|"
    r"how (do|does|did|to|can|many|long|far|old)|why (is|are|do|does|did)|"
    # "Well, tell me a little bit about hairless cats." didn't search (Nov 20, 8:47 call)
    r"tell me (?:a little bit |a little |a bit |something |more |all )?about|explain)\b",
    re.IGNORECASE,
)
_ASKS_WEATHER = re.compile(
    r"\b(?:is|will) it (?:going to |gonna )?(?:rain|snow|storm|be (?:hot|cold|warm|sunny|windy))\b|"
    r"\btemperature\b|\bsun(?:rise|set)\b|\bhow (?:hot|cold|warm) (?:is|will)\b", re.I)
# Natural ways of asking about the world ("I wonder how tall Everest is", "Do you
# know anything about the Webb telescope?", "Is it true that honey never spoils?").
# They contain "I" or "you", which the gate otherwise reads as personal or about
# Othos, so only the topic after the lead-in is judged (and searched for).
_CURIOUS = re.compile(
    r"\b(?:i\s+(?:wonder|wondered)(?:\s+(?:if|whether))?"
    r"|i'?m\s+(?:curious|wondering)(?:\s+(?:about|if|whether))?"
    r"|(?:do|did)\s+you\s+(?:happen\s+to\s+)?know(?:\s+anything)?(?:\s+about)?"
    r"|(?:any|got\s+any)\s+idea(?:\s+(?:if|whether))?"
    r"|(?:have|did)\s+you\s+(?:hear|heard|read|see|seen)(?:\s+anything)?\s+about"
    r"|i\s+(?:heard|read|saw)\s+(?:that|about|somewhere(?:\s+that)?|on\s+the\s+news(?:\s+that)?|online(?:\s+that)?)"
    r"|i\s+heard(?=\s+the\s)"
    r"|i\s+was\s+(?:reading|learning|hearing|watching\s+something)\s+about"
    r"|tell\s+me\s+(?:something|a\s+bit|a\s+little|more)\s+about"
    r"|is\s+it\s+true(?:\s+that)?"
    r"|(?:can|could)\s+you\s+(?:check|see|find\s+out)(?:\s+(?:if|whether))?)"
    r"\s+(?P<topic>\S.*)$", re.I)


def _curious_topic(text: str) -> str | None:
    """'I wonder how tall Everest is.' -> 'how tall Everest is.'; None if no lead-in."""
    for sentence in reversed(re.split(r"(?<=[.!?])\s+", text.strip())):
        m = _CURIOUS.search(sentence)
        if not m:
            continue
        words = re.findall(r"[\w']+", m.group("topic"))
        # one word is fine ("octopuses"), but "tell me more about that" refers back
        if words and not (len(words) == 1 and _REFERS_BACK.fullmatch(words[0])):
            return m.group("topic").strip()
    return None


_ASKS_REPEAT = re.compile(r"\b(again|you (just )?(said|mentioned|told me)|repeat (that|it))\b", re.IGNORECASE)
_ASKS_MORE = re.compile(r"\b(more|next|another|other results|keep going|go on)\b", re.IGNORECASE)


# ---------------------------------------------------------------- search helpers
# Othos searches with Tavily only. Naming another engine ("using DuckDuckGo")
# used to end up in the query and pull in pages about that engine.
_ENGINES = {"duckduckgo": "DuckDuckGo", "duck duck go": "DuckDuckGo", "google": "Google",
            "bing": "Bing", "yahoo": "Yahoo", "brave": "Brave", "perplexity": "Perplexity"}
_ENGINE_RX = "|".join(e.replace(" ", r"\s*") for e in _ENGINES)
_ENGINE_ASKED = re.compile(rf"\b(?:using|use|on|with|via|through|in)\s+({_ENGINE_RX})\b", re.I)
_NUMBERS = {w: i for i, w in enumerate("one two three four five six seven eight nine ten".split(), 1)}
_NUM = r"\d{1,2}|" + "|".join(_NUMBERS)
# "top five", "three Chinese restaurants", "5 pet websites"
_COUNT_IN_TEXT = re.compile(rf"\btop\s+({_NUM})\b|\b({_NUM})\s+(?:[\w'-]+\s+){{0,2}}[\w'-]+s\b", re.I)
# Asking for a list of results (read out by code) rather than a question (model answers)
_LIST_WORDS = re.compile(r"\b(results?|websites?|sites|links|list|top)\b", re.I)
_SEARCH_WORDS = re.compile(r"\b(search|look (it |that |this )?up|google (it|that|this|for))\b", re.I)
# "Please research the game...", "Please verify that", "double-check it": these didn't
# search, and the model then claimed "After researching, I found..." (8:19 AM call).
_RESEARCH = re.compile(
    r"\b(?:research|verify|fact[- ]?check|double[- ]?check|re-?check|confirm|look into|dig into|"
    r"check (?:on|into|again)|look at the (?:game|match|score|news|results?|numbers|stats|standings|story))\b", re.I)
_RESEARCH_ONLY = re.compile(  # no topic of its own: re-check the last search
    r"^\W*(?:(?:ok(?:ay)?|so|but|and|now|please|othos|can you|could you|would you)\W+)*"
    r"(?:research|verify|fact[- ]?check|double[- ]?check|re-?check|confirm|check(?: again)?)"
    r"(?:\s+(?:that|this|it|those|again|the|information|info|facts?|answer|result|claim|for sure))*"
    r"(?:\s+(?:again|please|for me))*\W*$", re.I)
_QUESTION = re.compile(r"\b(what|what's|who|who's|which|where|when|why|how)\b|\?", re.I)


def _wants_list(text: str) -> bool:
    """Read results out as a list, or answer? 'Search for X' / 'top 5 websites'
    get a list; a question ('what are the main islands... look it up') gets an answer."""
    if _LIST_WORDS.search(text):
        return True
    return bool(_SEARCH_WORDS.search(text)) and not _QUESTION.search(text)
# "Yes." after "Want to hear more?"
_YES = re.compile(r"^\W*(yes|yeah|yep|yup|sure|ok|okay|please|go ahead|of course|definitely)\b", re.I)
# A whole turn that only says yes ("Please do.", "Go ahead.", "Yes, please"): not "Please tell me about X"
_YES_ONLY = re.compile(r"^\W*(?:(?:yes|yeah|yep|yup|sure|ok(?:ay)?|please|go ahead|of course|definitely|do it|"
                       r"go for it|do|that'?d be (?:great|nice)|thanks|thank you|othos)\W*){1,4}$", re.I)
# Othos offered, or said it would, look something up ("Can I look that up for you?", "I'll do a web search.")
_OFFERS_LOOKUP = re.compile(
    r"\b(?:(?:can|shall|should) i (?:look (?:that|it|this) up|search|check|find out)|"
    r"(?:want|would you like) me to (?:look|search|check|find|google)|i can (?:look|search|check)|"
    r"i'?ll (?:do a |run a )?(?:quick )?(?:web )?search|i'?ll look (?:that|it|this) up)\b", re.I)
# "Any new businesses in Fairview?", "new restaurants around Riverton": local news a search answers
# (Nov 19 call: no search was offered, so Othos only said it didn't know)
_LOCAL_NEW = re.compile(
    r"(?i:\b(?:any|new|newest|recent|recently opened|just opened)\b[^.?!]{0,30}?\b(?:business(?:es)?|shops?|stores?|"
    r"restaurants?|caf[eé]s?|bakery|bakeries|bars?|breweries|brewery|places?|openings?|clinics?|markets?)\b"
    r"[^.?!]{0,20}?\b(?:in|near|around|by)\s+)(?:[A-Z][\w'.-]+)")
_MORE_WORDS = re.compile(r"\b(more|next|keep going|continue|go on|other results)\b", re.I)


def _to_int(value, default: int) -> int:
    """The 3B model sometimes sends count as "3" or "three"."""
    s = str(value).strip().lower()
    return int(s) if s.isdigit() else _NUMBERS.get(s, default)


def _count_from(text: str) -> int | None:
    m = _COUNT_IN_TEXT.search(text)
    return _to_int(m.group(1) or m.group(2), 0) or None if m else None


def _engine_named(text: str) -> str | None:
    m = _ENGINE_ASKED.search(text)
    return _ENGINES.get(re.sub(r"\s+", " ", m.group(1).lower())) if m else None


# ---------------------------------------------------------------- recent events
# "Who started at quarterback in yesterday's Seahawks game?" went to Tavily as is:
# a search engine doesn't know which day "yesterday" is, so an older game won.
# Relative days become real dates, and recent-event questions use Tavily's news
# search limited to those dates.
_WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_WD = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
_RECENT = re.compile(
    rf"\b(?:yesterday|today|tonight|last night|this (?:morning|afternoon|evening|week|weekend|season)|"
    rf"last (?:week|weekend|{_WD})|this past {_WD}|{_WD}'s|on {_WD}|latest|recent(?:ly)?|"
    r"just (?:happened|announced|released)|breaking|scores?|game|match|race|fight|"
    r"won|lost|beat|playoffs?|election|results?|standings|announced|released|"
    r"died|passed away|starting (?:quarterback|lineup)|started (?:at|as))\b", re.I)


def _fmt_day(d) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def _dated_query(q: str, now: datetime | None = None) -> tuple[str, list]:
    """Relative days in a search query -> real dates. Returns (query, dates used)."""
    now = now or datetime.now()
    used = []

    def day(offset):
        d = (now + timedelta(days=offset)).date()
        used.append(d)
        return _fmt_day(d)

    def past_weekday(name):
        back = (now.weekday() - _WEEKDAY_NAMES.index(name.lower())) % 7
        return day(-back)

    def next_weekday(name):
        ahead = (_WEEKDAY_NAMES.index(name.lower()) - now.weekday()) % 7 or 7
        return day(ahead)

    q = re.sub(r"\b(?:last night|yesterday)(?:'s)?\b", lambda m: day(-1), q, flags=re.I)
    q = re.sub(r"\b(?:tonight|today|this (?:morning|afternoon|evening))(?:'s)?\b", lambda m: day(0), q, flags=re.I)
    q = re.sub(r"\btomorrow(?:'s)?\b", lambda m: day(1), q, flags=re.I)
    q = re.sub(rf"\bnext ({_WD})\b", lambda m: next_weekday(m.group(1)), q, flags=re.I)
    q = re.sub(rf"\b(?:last|this past|on) ({_WD})\b|\b({_WD})'s\b",
               lambda m: past_weekday(m.group(1) or m.group(2)), q, flags=re.I)
    return re.sub(r"\s+", " ", q).strip(), used


def _clean_query(query: str, user_text: str) -> str:
    q = re.sub(rf"\b(?:(?:using|use|on|with|via|through|in)\s+)({_ENGINE_RX})\b", " ", query, flags=re.I)
    if _engine_named(user_text):  # the user named it, so a bare mention is noise too
        q = re.sub(rf"\b({_ENGINE_RX})\b", " ", q, flags=re.I)
    q = re.sub(r"\b(othos|osos)\b", " ", q, flags=re.I)
    q = re.sub(r"\s+", " ", q).strip(" ,.?!")
    return q or query


def _clean_text(s: str) -> str:
    """Web snippets arrive as markdown with menus and timestamps; keep plain words."""
    s = re.sub(r"[​-‏⁠﻿]", "", s or "")  # invisible characters in some headlines
    s = re.sub(r"\[[^\]]*\]|https?://\S+|\btop of page\b", " ", s, flags=re.I)
    s = re.sub(r"[#*_|>`]+|\.\.\.$|…$", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"\b(.{3,60}?) \1\b", r"\1", s)  # "Buddy Template Buddy Template"


def _site(url: str) -> str:
    return re.sub(r"^www\.", "", urlparse(url or "").netloc)


def _headline(r: dict) -> str:
    """One short line saying what a result is: its title, minus the site name."""
    site = _site(r.get("url", "")).split(".")[0].lower()
    parts = [c for p in re.split(r"\s+[|\-–—]\s+", r.get("title", "")) if (c := _clean_text(p))]
    good = [p for p in parts if p.lower() not in ("home", "welcome", site) and len(p.split()) >= 2]
    line = (good or parts or [""])[0]
    if len(line.split()) < 2:  # no usable title: first sentence of the snippet
        line = re.split(r"(?<=[.!?])\s", _clean_text(r.get("content", "")))[0]
        line = " ".join(line.split()[:20])
    return line.rstrip(" .!?:;,")


def _spoken_headline(r: dict, words: int = 10) -> str:
    """_headline cut to what's worth saying aloud: a store's product title ("Bully Tools 5-Tine Broadfork, Fiberglass
    Handles, 56" 10-Gauge Steel...") ran 35 s over four results (Nov 20: "that was quite a lot"). The first clause, at
    most `words` words, never ending on a small word."""
    line = re.sub(r"^[\w.-]+\.(?:com|net|org)\s*:\s*", "", _headline(r))     # "Amazon.com : Bully Tools..."
    first = re.split(r",\s+", line)[0]
    if len(first.split()) >= 3:
        line = first
    parts = line.split()
    if len(parts) > words:
        parts = parts[:words]
        while len(parts) > 3 and parts[-1].lower().strip(",.") in _READOUT_SMALL:
            parts.pop()
    return " ".join(parts).rstrip(" .!?:;,-")


_READOUT_SMALL = {"a", "an", "the", "and", "or", "of", "in", "on", "to", "for", "with", "by", "at", "as", "from", "-", "&"}
# a search answer that retells one page instead of answering ("The speaker says...", "In this video...")
_ABOUT_A_PAGE = re.compile(r"\b(?:the|this|that)\s+(?:speaker|narrator|video|author|writer|host|presenter|creator|"
                           r"youtuber|vlogger|article|post|blog post|episode|podcast|transcript|clip|channel|thread|poster)"
                           r"\b|\bin this (?:video|article|episode|post)\b", re.I)
READOUT_COUNT = 3  # results read per page (was 4): about 20 s of speech, and less for the model to re-read each turn

# "Where can I get a broadfork?": the user wants places that sell it, near home (the user, Nov 20), not pages about
# the product, forum threads, or stores in another state
_WHERE_TO_BUY = re.compile(
    r"\bwhere\s+(?:can|could|do|should|would)\s+(?:i|we)\s+(?:get|buy|find|purchase|pick\s+up|order)\b|"
    r"\bwhere\s+to\s+(?:get|buy|find|purchase|pick\s+up|order)\b|"
    r"\b(?:buy|purchase|get|find)\b[^.?!]{0,30}\b(?:near|nearby|locally|around|in\s+stock|for\s+sale)\b|"
    r"\b(?:near|nearby|local)\b[^.?!]{0,20}\b(?:stores?|shops?|retailers?|dealers?|sellers?)\b|\bfor\s+sale\b", re.I)
_BIG_BOX = {"homedepot.com", "lowes.com", "walmart.com", "target.com", "costco.com", "tractorsupply.com", "acehardware.com",
            "menards.com", "harborfreight.com", "bestbuy.com", "staples.com", "officedepot.com", "samsclub.com", "kroger.com",
            "safeway.com", "fredmeyer.com", "albertsons.com", "walgreens.com", "cvs.com", "petsmart.com", "petco.com",
            "michaels.com", "joann.com", "hobbylobby.com", "autozone.com", "oreillyauto.com", "napaonline.com",
            "cabelas.com", "basspro.com", "sportsmans.com", "rei.com", "dickssportinggoods.com", "northerntool.com",
            "grainger.com", "fastenal.com", "ikea.com", "lowes.ca", "homedepot.ca", "canadiantire.ca"}
_LOCAL_LISTINGS = {"yelp.com", "yellowpages.com", "mapquest.com", "bbb.org", "manta.com", "maps.google.com"}
_ONLINE_ONLY = {"amazon.com", "ebay.com", "etsy.com", "wayfair.com", "aliexpress.com", "temu.com", "alibaba.com"}
_FORUMS = {"reddit.com", "quora.com", "facebook.com", "pinterest.com", "youtube.com", "tiktok.com", "instagram.com",
           "x.com", "twitter.com", "stackexchange.com", "stackoverflow.com", "wikipedia.org", "fandom.com", "medium.com",
           "tumblr.com", "nextdoor.com", "answers.com", "wikihow.com"}


def _domain_in(site: str, names: set) -> bool:
    return any(site == n or site.endswith("." + n) for n in names)


def _rank_for_buying(results: list, home: str) -> list:
    """Re-order results for a "where can I buy..." question: big-box stores and local sellers first, then other sellers,
    then pages about the product, and forum or social posts last (a subreddit is rarely local). Stable: results with the
    same score keep the search engine's order."""
    place = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z.'-]{2,}", home or "") if w.lower() not in {"county", "usa"}}

    def score(r: dict) -> float:
        site = _site(r.get("url", "")).lower()
        path = urlparse(r.get("url", "") or "").path.lower()
        title = (r.get("title") or "").strip()
        text = f"{title} {r.get('content', '')} {path}".lower()
        v = 0.0
        if _domain_in(site, _BIG_BOX):
            v += 3
        elif _domain_in(site, _LOCAL_LISTINGS):
            v += 2
        elif _domain_in(site, _ONLINE_ONLY):
            v += 0.5
        elif _domain_in(site, _FORUMS):
            v -= 3
        if place and any(re.search(rf"\b{re.escape(w)}\b", text) for w in place):
            v += 2
        if re.search(r"/(?:stores?|locations?|store-?locator|find-a-store|shop|product|products|p|dp|ip)\b", path):
            v += 1.5
        if re.search(r"\b(?:in stock|add to cart|buy now|\$\s?\d)", text):
            v += 1
        if re.match(r"(?:how|what|why|which|best|top|where|guide|review)\b", title, re.I):
            v -= 1.5        # a page about the product or a question, not a seller
        return v

    order = sorted(range(len(results)), key=lambda i: (-score(results[i]), i))
    return [results[i] for i in order]


_ADDRESS = re.compile(r"^\W*(?:(?:hey|hi|ok|okay)\W+)?(?:othos|osos|otis)\b\W*", re.I)
_REQUEST_FILLER = re.compile(
    r"\b(?:(?:can|could|would|will) you(?: please)?|(?:do|did) you know|please|for me|as well|thanks|thank you|"
    r"i(?: want|'d like| would like) to know|tell me|find out|"
    r"research|verify|fact[- ]?check|double[- ]?check|look into|dig into|look at|"
    r"do a (?:quick )?search(?: for me)?(?: of| for| on| about)?|"
    r"search(?: again)?(?: the web| online)?(?: for| on| about| up)?|look (?:it |that |this )?up|"
    r"google(?: it| that| this| for)?|(?:using|use|on|with|via) (?:" + _ENGINE_RX + r"))\b",
    re.I,
)
# Left in a query, these mean "personal" (stay private) or "refers back" (needs
# the conversation to make sense): the model writes those queries instead.
_PERSONAL = re.compile(r"\b(i|i'm|i've|i'd|my|me|mine|we|our|(?-i:us))\b", re.I)  # "US" is a country
# "Look that up", "search for it", "google this": the thing to search was said before
_LOOK_BACK = re.compile(r"\b(?:look|search|google|research|verify|check)(?: (?:for|up|out))? (?:it|that|this|them|those)\b", re.I)
_REFERS_BACK = re.compile(r"\b(it|its|that|those|they|them|these|he|she|his|her|hers|one|ones)\b", re.I)


_DIGIT_WORD = {w: i for i, w in enumerate(["zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
                                            "nine"])}
_DIGIT_WORD.update({t: i for i, t in enumerate(["", "ten", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
                                                 "eighty", "ninety"]) if t})
_TENS_WORD = {t: i * 10 for i, t in enumerate(["", "ten", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
                                                "eighty", "ninety"]) if t}
_SPOKEN_MODEL = re.compile(
    r"\b(?P<pre>(?:rtx|gtx|rx|radeon|geforce)\s+)?(?P<a>one|two|three|four|five|six|seven|eight|nine|ten|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety)[\s-]+(?P<b>ten|twenty|thirty|forty|fifty|sixty|seventy|eighty|"
    r"ninety)\b(?P<post>\s+(?:ti|super|xtx|xt)\b)?", re.I)


def _spoken_models(text: str) -> str:
    """Speech-to-text writes graphics cards as words: 'RTX four sixty Ti', 'forty sixty TI'
    -> 'RTX 4060 Ti'. Only next to a brand or a Ti/Super suffix."""
    def repl(m):
        if not (m.group("pre") or m.group("post")):
            return m.group(0)
        n = _DIGIT_WORD[m.group("a").lower()] * 1000 + _TENS_WORD[m.group("b").lower()]
        pre = (m.group("pre") or "RTX ").strip().upper() + " "
        post = " " + m.group("post").strip().capitalize() if m.group("post") else ""
        return f"{pre}{n}{post}".replace(" Xtx", " XTX").replace(" Xt", " XT")
    return _SPOKEN_MODEL.sub(repl, text)


_REQUEST_PART = re.compile(
    r"(?:^|[,;:.!?]\s+)((?:please\s+)?(?:tell me|what|what's|who|who's|when|where|how|why|which|"
    r"can you|could you|search|look up|research|find out)\b[^,;:]*)$", re.I)


# "Where could I get a broadfork near me?" -> "where to buy a broadfork near me" (searchctx puts the town in): the buying
# words stay, or the search finds farms named Broadfork (Nov 20, 7:52 call), and code writes it so the model doesn't
# spend ~15 s reading the search tool's instructions and writing the call
_BUY_ITEM = re.compile(
    r"\bwhere\s+(?:(?:can|could|do|should|would|might)\s+(?:i|we)\s+|to\s+)(?:go\s+to\s+)?"
    r"(?:get|buy|find|purchase|pick\s+up|order)\s+(?P<item>[^?.!,]+?)"
    r"(?:\s+(?:near\s+me|near\s+here|nearby|around\s+here|around\s+me|locally|in\s+my\s+area|in\s+town|close\s+by|"
    r"close\s+to\s+me|from\s+here))?\s*[?.!]*$", re.I)
# "Can you suggest any in my area?": "any" is the breeders from two turns before, about hairless cats (Nov 20)
_IN_MY_AREA = re.compile(r"\b(?:in\s+my\s+area|near\s+me|nearby|around\s+here|around\s+me|close\s+by|locally|"
                         r"close\s+to\s+me|in\s+town)\b", re.I)
# what "any" means in "Can you suggest any in my area?": places and people, not "health" or "food" (Nov 20)
_PLACE_NOUN = re.compile(r"\b(?:breeders?|vets?|veterinarians?|veterinary clinics?|shelters?|rescues?|stores?|shops?|"
                         r"nurser(?:y|ies)|trainers?|groomers?|classes|clubs?|groups?|kennels?|cattery|catteries|farms?|"
                         r"dealers?|sellers?|suppliers?|markets?|clinics?|doctors?|dentists?|mechanics?|restaurants?)\b",
                         re.I)
_ASKS_FOR_PLACES = re.compile(r"\b(?:suggest|recommend|find|know\s+(?:of|any)|are\s+there|is\s+there|any|some|"
                              r"good\s+ones?|options)\b", re.I)


_POINTS_BACK = re.compile(r"(?i)(?:(?:a|an|the|some|one\s+of)\s+)?(?:it|that|this|them|those|these|one|ones|some|any)"
                          r"(?:\s+(?:of\s+)?(?:them|those|these))?")
# what a search was about, without what it asked of it: "broadfork uses" -> "broadfork", "Sphynx cat prices" -> "Sphynx cat"
_SUBJECT_TAIL = re.compile(r"(?i)(?:\s+(?:uses?|benefits?|prices?|costs?|reviews?|history|facts|meaning|definition|"
                           r"info(?:rmation)?|guide|tips|explained|for sale|near\s+.*|in\s+[A-Z].*|"
                           r"(?:how|what|where|why|when)\b.*))+$")


def _subject_core(subject: str) -> str:
    s = re.sub(r"(?i)^(?:where to (?:buy|get|find)|what (?:is|are)|tell me about|about)\s+", "", (subject or "").strip())
    s = re.sub(r"(?i)^(?:an?|the|some)\s+", "", s)
    return _SUBJECT_TAIL.sub("", s).strip(" ,.?!")


def _local_query(text: str, recent: list[str], subject: str) -> str | None:
    """A where-to-buy or in-my-area question as a search query, or None (then _query_from or the model writes it)."""
    t = re.sub(r"^\W*(?:(?:hey|hi|ok|okay|so|and|but|also|well)\W+)*(?:othos\W+)?", "", _as_othos(text).strip(),
               flags=re.I)
    t = re.sub(r"^(?:othos\W+)", "", t, flags=re.I)
    m = _BUY_ITEM.search(t)
    if m:
        item = re.sub(r"\s+", " ", m.group("item")).strip()
        if _POINTS_BACK.fullmatch(item):
            # "Where could I buy one locally?" after a search about broadforks (Nov 20, 8:20 call: "Don't know")
            item = _subject_core(subject)
        if item and len(re.findall(r"\w+", item)) <= 6 and not _PERSONAL.search(item):
            return f"where to buy {item} near me"     # stores near home first (the user, Nov 20)
    if _IN_MY_AREA.search(t) and _ASKS_FOR_PLACES.search(t) and subject:
        own = _PLACE_NOUN.search(t)
        noun = own.group(0) if own else next(
            (mm.group(0) for r in reversed(recent) for mm in [_PLACE_NOUN.search(r)] if mm), "")
        if noun:
            return f"{subject} {noun} near me"
    return None


def _query_from(text: str, chat_ctx) -> str | None:
    """The user's request cut down to a search query, or None when the model should
    write it. 'Othos, what are the main islands in the US Virgin Islands? Please
    look it up as well.' -> 'what are the main islands in the US Virgin Islands?'"""
    asked_about = bool(_DEFINITION.search(text))   # "Tell me more about broadforks.": one word is a whole topic
    text = _without_small_talk(text) or text  # "Good morning, Othos. What's..." -> "What's..."
    # "Nicely done. Let's see. Othos can you tell me about..." was searched whole (Nov 10
    # call): with several sentences, search with the last one that asks something
    sents = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
    if len(sents) > 1:
        asks = [s for s in sents if _QUESTION.search(s) or re.search(
            r"\b(?:tell me|search|look up|look into|can you|could you|would you|find out|explain)\b", s, re.I)]
        if asks:
            text = asks[-1]
    said_before = any(isinstance(i, llm.ChatMessage) and i.role == "user" for i in chat_ctx.items[:-1])
    if said_before and _LOOK_BACK.search(text):
        return None  # "You should look that up for me": the filler words below would drop the "that"; the model knows what it was
    text = _spoken_models(text)  # "RTX four sixty Ti" -> "RTX 4060 Ti"
    # "Griffin, g r I f f I n": the spelled "I" made it personal, so the model wrote the
    # query (63 s, Nov 10 call); letters said one by one are one word
    text = sources.join_spelled(text)
    # "my RTX 4060 Ti" says nothing private once searched as "the RTX 4060 Ti"
    text = re.sub(r"\bmy\s+(?=(?:(?:new|old|current)\s+)?(?:rtx|gtx|rx|radeon|geforce|nvidia|amd|intel|ryzen|"
                  r"dji|osmo|obs|joomla|\d{4}\b))", "the ", text, flags=re.I)
    text = _curious_topic(text) or text  # "I wonder how tall Everest is" -> "how tall Everest is"
    # "How do I cast on?" is a how-to, not personal: searched as "how to cast on?"
    text = re.sub(r"\bhow (?:do|can|should|would|could) (?:i|we)\b", "how to", text, flags=re.I)
    # "What bitrate should I use?" -> "What bitrate to use?", "Where can I buy..." -> "Where to buy..."
    text = re.sub(r"\b((?:what|which|where|when)\b[^,.?!]{0,30}?)\s+(?:should|could|can|do) (?:i|we)\b",
                  r"\1 to", text, flags=re.I)
    if _PERSONAL.search(text):
        # "Before I go into the build details, tell me about yesterday's Seahawks game":
        # the "I" is in a side clause; search with the request part if that isn't personal
        m = _REQUEST_PART.search(text)
        if m and not _PERSONAL.search(_REQUEST_FILLER.sub(" ", m.group(1))):  # "tell me" isn't personal
            text = m.group(1)
    q = _REQUEST_FILLER.sub(" ", _ADDRESS.sub("", text))
    q = re.sub(r"\b(othos|osos|orthos|othus|otis)\b", " ", q, flags=re.I)
    q = re.sub(r"\s+([,.?!])", r"\1", re.sub(r"\s+", " ", q))
    q = re.sub(r"^[\s,.!?]+|[\s,.!]+$|([.?!])[\s,.!?]*$", r"\1", q).strip()
    # "But what's the forecast?", "So can you tell me of any new..." -> "any new..."
    q = re.sub(r"^(?:(?:but|and|so|also|okay|ok|well|then|now|about|of|actually)\b[\s,]*)+", "", q, flags=re.I)
    # what "tell me" and "look up" leave behind (Nov 20, 8:47 call): "a little bit about hairless cats" -> "hairless
    # cats", "Why don't you Broad Forks then?" -> "Broad Forks"
    q = re.sub(r"^(?:(?:a little bit|a little|a bit|something|more|all|everything)\s+)?about\s+", "", q, flags=re.I)
    q = re.sub(r"^(?:why (?:don't|do not) you|how about you|maybe you (?:could|can|should)|you (?:could|should|can))"
               r"\b[\s,]*", "", q, flags=re.I)
    if _SEARCH_WORDS.search(text) or re.search(r"\bwhy (?:don't|do not) you\b", text, re.I):
        q = re.sub(r"[\s,]+(?:then|instead|now|already|real quick)\s*([?.!]?)$", r"\1", q, flags=re.I).strip()
    name = _user_name()
    one_ok = asked_about and len(re.findall(r"\w+", q)) == 1 and not _REFERS_BACK.fullmatch(q.strip(" .?!,"))
    if (len(re.findall(r"\w+", q)) < 2 and not one_ok) or _PERSONAL.search(q) or (
            name and re.search(rf"\b{re.escape(name)}\b", q, re.I)):
        return None
    # weather's "it" ("Will it rain tomorrow?") doesn't refer back to anything
    check = re.sub(r"\b(?:is|was|will|does|did|isn't|wasn't|won't)\s+it\b", "", q, flags=re.I) \
        if _WEATHER_TOPIC.search(q) else q
    if said_before and _REFERS_BACK.search(check):
        return None
    return q


# "What islands make up...", "Which planet is...": any sentence opening with a
# question word, unless it is about Othos, the user, or the time.
_QUESTION_OPENER = re.compile(
    r"^\W*(?:(?:hey|hi|ok|okay|so|and|but|also)\W+)*(?:(?:othos|osos)\W+)?(what|which|who|whose|where|when|why|how)\b",
    re.I)
_NOT_A_FACT = re.compile(
    r"\b(you|your|yours|yourself|i|i'm|my|me|we|us|our|time is it|what time|what day|today's date)\b",
    re.I)  # plain chit-chat ("What's new?", "How's it going?") is dropped earlier by _SMALL_TALK


# "What do you think?", "What time is it?": for Othos, not the web
# (checked after "can you" / "do you know" are taken out)
_ABOUT_OTHOS = re.compile(r"\b(you|your|yours|yourself|time is it|what time|what day|today's date)\b", re.I)


# A whole sentence that is just friendly chit-chat ("What's going on?", "How are
# you?"). "What's going on?" was searched and answered with the Marvin Gaye song;
# "What's going on with the election?" still searches (anchored at the end).
_SMALL_TALK = re.compile(
    r"^\W*(?:(?:hey|hi|hello|ok|okay|so|and|but|well)\W+)*(?:(?:othos|osos|otis|azis)\W+)?"
    r"(?:what(?:'s| is| has been)\s+(?:going on|happening|new|up|good)|"
    r"how(?:'s| is)\s+(?:it going|everything|things|life|your (?:day|morning|night|evening))|"
    r"how are (?:you|things)(?: doing)?|how(?:'ve| have) you been|you (?:ok|okay|alright|there))"
    r"(?:\s+(?:today|tonight|now|this (?:morning|afternoon|evening)|with you))?\W*$",
    re.I)
# "Good morning, Azos." (Othos misheard) ended up in a news search, so a greeting
# followed by any one word counts as a greeting too.
_GREETING_ONLY = re.compile(r"^\W*(?:(?:hey|hi|hello|good (?:morning|afternoon|evening))?"
                            r"(?:\W+(?:there|othos|osos|otis|azis|azos))*"
                            r"|(?:hey|hi|hello|good (?:morning|afternoon|evening))\W+[\w'-]+)\W*$", re.I)


def _without_small_talk(text: str) -> str:
    """The user's words minus greetings and chit-chat sentences; '' if that's all it was."""
    keep = [s for s in re.split(r"(?<=[.!?])\s+", text)
            if not (_SMALL_TALK.match(s) or _GREETING_ONLY.match(s))]
    return " ".join(keep)


def _is_fact_question(text: str) -> bool:
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if _QUESTION_OPENER.search(sentence) and not _NOT_A_FACT.search(sentence):
            return True
    return False


_ABBREV = re.compile(r"\b(St|Mt|Dr|Mr|Mrs|Ms|Jr|Sr|U\.S|U\.K|e\.g|i\.e|approx|vs|No|Ft)\.$", re.I)
_AS_SOURCES_SAY = re.compile(r"^\s*(according to|based on) (the |these |several |multiple )?"
                             r"(sources|search results|results)[^,]*,\s*", re.I)


def _backed_by(answer: str, results: list) -> bool:
    """Every number in the answer must appear in the pages it came from. Tavily's
    answers matched their sources in testing; this keeps it that way."""
    def norm(s: str) -> str:  # "4‑1" / "4–1" -> "4-1", "14,410" -> "14410"
        return re.sub(r"\s*[‐‑–—]\s*", "-", s).replace(",", "")
    hay = norm(" ".join(f"{r.get('title', '')} {r.get('content', '')}" for r in results))
    nums = re.findall(r"\d(?:[\d.]*\d)?(?:-\d+)?", norm(answer))  # scores like 4-1 stay whole
    # a range ("90-95%") not written as one in the pages counts if both ends are there
    missing = [n for n in nums if n not in hay and not ("-" in n and all(p in hay for p in n.split("-")))]
    if missing:
        log.warning("Tavily answer not backed by its sources (%s): %s", missing, answer)
        return False
    return _in_context(answer, results)


# A number must also sit next to what it's said to be: "$3.36" was on the WSJ page, but as
# an earnings estimate, and was spoken as NVIDIA's share price (10:36 AM call, Nov 10).
_ESTIMATE = re.compile(r"\b(?:estimates?|estimated|forecasts?|projected|projections?|targets?|eps|earnings per share|"
                       r"consensus|q[1-4]|fy\s?\d{2,4}|guidance|expected|outlook)\b", re.I)
_PRICE_WORDS = re.compile(r"\b(?:price|prices|priced|trading|traded|closed|close|shares?|stock|quote|"
                          r"opened|at \$)\b", re.I)


def _in_context(answer: str, results: list, window: int = 160) -> bool:
    """Each figure in the answer (2+ digits, or with $ or %) must appear in a source next to
    at least two of the words its own sentence uses about it (one, if that's all it has)."""
    def norm(s: str) -> str:
        return re.sub(r"\s*[‐‑–—]\s*", "-", s or "").replace(",", "")
    hay = norm(" ".join(f"{r.get('title', '')}. {r.get('content', '')}" for r in results)).lower()
    for sentence in re.split(r"(?<=[.!?])\s+", norm(answer)):
        # dates aren't figures to check ("on 2026-11-10", "November 10, 2026")
        plain = re.sub(r"\b\d{4}-\d{2}-\d{2}\b|\b(?:19|20)\d{2}\b|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
                       r"[a-z]*\.? \d{1,2}\b", " ", sentence, flags=re.I)
        figures = re.findall(r"\$?\d+(?:\.\d+)?%?", plain)
        figures = [f for f in figures if len(re.sub(r"\D", "", f)) >= 2 or f.startswith("$") or f.endswith("%")]
        if not figures:
            continue
        words = [w for w in sources.keywords(re.sub(r"\$?\d+(?:\.\d+)?%?", " ", sentence))
                 if not re.fullmatch(r"[\d.%$-]+", w) and w not in ("according", "sources", "source", "says", "said",
                                                                    "reported", "reports", "about", "around")]
        need = 1 if len(words) <= 1 else 2
        # names alone don't place a figure ("The RTX 4060 Ti weighs 2535 grams" matched on
        # "RTX" and "Ti"): one of the plain words saying what it is (cores, closed, beat) must be near
        said_of = [w for w in words if re.search(rf"\b{re.escape(w)}", sentence) and not re.search(
            rf"\b{re.escape(w[:1].upper() + w[1:])}\b|\b{re.escape(w.upper())}\b", sentence)]
        price_claim = bool(_PRICE_WORDS.search(sentence)) and not _ESTIMATE.search(sentence)
        for f in figures:
            bare = f.lstrip("$").rstrip("%").lower()
            ok = False
            for m in re.finditer(re.escape(bare) + r"(?!\d)", hay):
                if m.start() > 0 and hay[m.start() - 1].isdigit():
                    continue  # "3.36" inside "13.36"
                near = hay[max(0, m.start() - window): m.end() + window]
                near_words = re.findall(r"[a-z0-9'-]+", near)
                hits = sum(any(t.startswith(w[:5]) for t in near_words) for w in words)
                plain_hit = not said_of or any(any(t.startswith(w[:5]) for t in near_words) for w in said_of)
                # a price that sits among estimate / forecast words isn't the price
                if hits >= need and plain_hit and not (
                        price_claim and _ESTIMATE.search(hay[max(0, m.start() - 60): m.end() + 60])):
                    ok = True
                    break
            if not ok:
                log.warning("figure %r not in context in the sources: %s", f, sentence)
                return False
    return True


# "What's in the news?" / "Any headlines?" / "What's going on in the world?":
# Tavily's news index, last day only, read as headlines with their source.
# A general search with a written answer gave June's World Cup in November, and
# one 1,156-character sentence that Rime refused ("text is too long").
_NEWS = re.compile(r"\b(news|headlines?|current events)\b|"
                   r"\b(going on|happening)\s+(in|around)\s+the\s+world\b", re.I)
_NEWS_BEFORE = re.compile(r"((?:[\w'-]+\s+){0,2})(?:news|headlines?)\b", re.I)
_NEWS_AFTER = re.compile(
    r"\b(?:news|headlines?)\s+(?:about|on|from|in|for|regarding|out of)\s+(?:the\s+)?([\w' -]+?)"
    r"(?=\s+(?:today|tonight|this \w+|right now|now|lately|recently|so far)\b|[?.!,]|$)", re.I)
_NOT_TOPIC = set("""the a an any some latest top main big biggest today's todays tonight's
    this that morning's morning evening's breaking good bad in on of what's whats what is
    much new recent current all your me us read give tell and or local immediate important
    major quick brief update""".split())
# "What's the immediate US news?" lost "US" (it's also the pronoun "us") and searched
# "immediate news" from any site (12:09 PM call). Written as US / U.S. / USA it's the country.
_US_WORD = re.compile(r"^(?:US|U\.S\.?|USA|U\.S\.A\.?|America|American)$")


# World / top headlines come from these outlets (any site gave obituaries and
# "Today in History"); a named topic ("news from Ukraine") searches every site.
# Change the list with NEWS_SITES in .env (comma-separated).
NEWS_SITES = [s.strip() for s in os.getenv(
    "NEWS_SITES", "apnews.com,reuters.com,bbc.com,cbc.ca,aljazeera.com,theguardian.com,npr.org,"
                  "france24.com,dw.com").split(",") if s.strip()]
# National / political headlines from established US outlets (any site gave a NOAA
# climate page and small local papers on the 9:05 AM call). US_NEWS_SITES in .env changes it.
US_NEWS_SITES = [s.strip() for s in os.getenv(
    "US_NEWS_SITES", "apnews.com,reuters.com,bbc.com,theguardian.com,npr.org,pbs.org,nbcnews.com,abcnews.go.com,cbsnews.com,"
                     "usatoday.com,axios.com,politico.com,thehill.com,cnn.com,foxnews.com,nytimes.com,"
                     "washingtonpost.com,wsj.com").split(",") if s.strip()]
_LABEL_SAID = {"political": "politics", "national political": "national politics", "us political": "US politics"}
_US_LABELS = {"national", "political", "politics", "us", "u.s.", "american", "domestic", "election",
              "congress", "washington", "white house", "supreme court", "us political", "national political"}
# ---- news roundups and follow-ups (12:21 PM call) ----
# "Is there any breaking news?" got the same headlines as three minutes earlier: breaking /
# latest / just in now mean the last few hours, newest first, nothing already told.
_BREAKING = re.compile(r"\b(?:breaking|just in|latest|right now|this hour|developing|any new|anything new)\b", re.I)
BREAKING_HOURS = int(os.getenv("OTHOS_BREAKING_HOURS", "6"))
NEWS_COUNT = 6  # stories in a roundup
_OUTLET_NAMES = {
    "reuters.com": "Reuters", "bbc.com": "the BBC", "bbc.co.uk": "the BBC", "theguardian.com": "The Guardian",
    "apnews.com": "the AP", "npr.org": "NPR", "pbs.org": "PBS", "nbcnews.com": "NBC News", "cbsnews.com": "CBS News",
    "abcnews.go.com": "ABC News", "cnn.com": "CNN", "foxnews.com": "Fox News", "nytimes.com": "The New York Times",
    "washingtonpost.com": "The Washington Post", "wsj.com": "The Wall Street Journal", "usatoday.com": "USA Today",
    "axios.com": "Axios", "politico.com": "Politico", "thehill.com": "The Hill", "aljazeera.com": "Al Jazeera",
    "cbc.ca": "CBC", "france24.com": "France 24", "dw.com": "DW", "cnbc.com": "CNBC", "bloomberg.com": "Bloomberg",
    "the-independent.com": "The Independent", "independent.co.uk": "The Independent", "espn.com": "ESPN",
    **sources.OUTLETS,  # the local and state feeds from personal.json, hardware, tech...
}
_ROUNDUP_JOIN = ["", "Meanwhile, ", "Also, ", "And ", "Plus, ", "On another note, ", "Lastly, "]
# "Where's that from?", "Who reported that?", "What's the source?"
_ASKS_SOURCE = re.compile(
    r"\b(?:sources?|where(?:'s| is| was| did)? (?:that|this|it|those|these|they|the \w+ (?:story|one))(?: one)? "
    r"(?:from|come from|came from)|where did (?:that|this|it|those|these) come from|who (?:reported|wrote|said|published) "
    r"(?:that|this|it|those)|which (?:site|outlet|paper|network)|what (?:site|outlet|paper|network))\b", re.I)
# "Tell me more about the fridge hacking", "more on the Ethiopia story", "what about Nvidia?"
_MORE_ON = re.compile(r"\b(?:(?:tell me )?more (?:on|about)|tell me about|details? (?:on|about)|what about|"
                      r"go on about|explain|what happened (?:with|in|to))\s+(?:the\s+)?(?P<topic>.+)$", re.I)
_MORE_NEWS = re.compile(r"\b(?:more|further|other|additional|any other|different|new)\s+(?:news|headlines|stories)\b|"
                        r"\bwhat else is (?:happening|going on|in the news)\b", re.I)
_STORY_STOP = set("the a an and or of to in on for with about from that this those these story one news what "
                  "tell me more please othos is was are were it its their there here".split())


_AUTHOR_BIO = re.compile(r"\b(?:is|was) (?:an?|the) (?:\w+ ){0,3}(?:editor|reporter|writer|correspondent|producer|"
                         r"journalist|columnist|anchor|contributor)\b|\b(?:also )?covers (?:breaking|politics|crime|news)\b|"
                         r"\bfollow (?:him|her|them) on\b|\bsign up for\b|\bsubscribe\b", re.I)


_SOURCE_NAMES = {"weather.gov": "the National Weather Service", "en.wikipedia.org": "Wikipedia",
                 "twitch.tv": "Twitch", "github.com": "Joomla's GitHub page", "ravelry.com": "Ravelry",
                 "youtube.com": "YouTube", "developer.joomla.org": "Joomla's security center",
                 "finance.yahoo.com": "Yahoo Finance", "google.com": "Google Finance", "marketwatch.com": "MarketWatch",
                 "nasdaq.com": "Nasdaq", "investing.com": "Investing.com", "stockanalysis.com": "Stock Analysis"}


def _source_name(url: str) -> str:
    """A result's site as it should be said: 'the National Weather Service', 'Stack Exchange'."""
    site = _site(url)
    if site.endswith(("stackexchange.com", "superuser.com")):
        return "Stack Exchange"
    return _SOURCE_NAMES.get(site) or _OUTLET_NAMES.get(site) or site


def _outlet(r: dict) -> str:
    site = _site(r.get("url", ""))
    return _OUTLET_NAMES.get(site, site)


def _published(r: dict):
    """A news result's publish time (aware datetime) or None."""
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(r.get("published_date") or "")
    except Exception:
        return None


def _ago(r: dict) -> str:
    """'an hour ago', '3 hours ago', '20 minutes ago', or ''."""
    from datetime import timezone
    t = _published(r)
    if not t:
        return ""
    mins = (datetime.now(timezone.utc) - t).total_seconds() / 60
    if mins < 0 or mins > 24 * 60:
        return ""
    if mins < 50:
        return f"{max(5, int(round(mins / 5)) * 5)} minutes ago"
    hrs = round(mins / 60)
    return "an hour ago" if hrs == 1 else f"{hrs} hours ago"


def _story_score(text: str, r: dict) -> int:
    """How many of the user's words are in a story (a name like 'McConnell' counts double)."""
    raw = re.findall(r"[A-Za-z0-9']+", text)
    hay_text = f"{r.get('title', '')} {r.get('content', '')}"
    hay = set(re.findall(r"[a-z0-9']+", hay_text.lower()))
    score = 0
    for w in raw:
        lw = w.lower()
        if len(lw) <= 2 or lw in _STORY_STOP:
            continue
        # word stems: "hacking" ~ "hacked", "fridges" ~ "fridge"
        if lw in hay or (len(lw) >= 5 and any(h.startswith(lw[:5]) for h in hay)):
            score += 2 if w[0].isupper() and w.lower() not in _STORY_COMMON else 1
    return score


_STORY_COMMON = set("did does do was is are were what who why how when where which can could would will "
                    "should has have had or and but yes no okay so then".split())


def _match_story(text: str, results: list, min_score: int = 1):
    """The result a follow-up is about, by shared words ('the fridge hacking' -> the BBC story)."""
    best = max(results, key=lambda r: _story_score(text, r), default=None)
    return best if best is not None and _story_score(text, best) >= min_score else None


# "Please summarize today's political headlines": a spoken summary, not a list
_SUMMARIZE = re.compile(r"\b(?:summari[sz]e|sum (?:it |them )?up|summary|brief me|briefing|rundown|recap|"
                        r"the gist|overview|catch me up)\b", re.I)
# Not a story: show pages, bulletins, podcasts, and bare photo captions ("Poland Shooting")
_NOT_A_STORY = re.compile(
    r"\b(podcast|newshour|bulletin|live coverage|breaking news,|today in history|top \d+ news|"
    r"news today|school assembly|newspaper headlines|top stories in|headlines \(|"
    r"news brief|morning edition|up first|newscast|news for (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)|"
    r"news (?:roundup|digest|wrap)|awards \d{4}|\d{4} awards)", re.I)
_DANGLING = re.compile(r"(?:\s+(?:on|to|of|the|a|an|and|or|for|in|with|at|by|from|as|its|his|her|their|'|\W))+\s*$", re.I)


def _news_line(r: dict) -> str:
    """A headline as it should be spoken: Tavily cuts long titles mid-phrase
    ("... await Trump's next move on..."), so a cut title gives way to the
    article's first sentence, or loses its dangling words."""
    raw = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", r.get("title", ""))
    title = re.sub(r"\s*[\u2013\u2014]\s*", " - ", _headline(r).replace("\u2019", "'"))
    title = re.sub(r"^\s*(?:BREAKING|UPDATE|LIVE|WATCH|DEVELOPING|JUST IN)\s*[:\-]\s*", "", title, flags=re.I)
    title = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", title)
    title = re.sub(r"[^\w\s.,;:!?'\"$%&()/-]", "", title)  # emoji etc.
    title = re.sub(rf"\s*:\s*{re.escape(_site(r.get('url', '')).split('.')[0])}\s*$", "", title, flags=re.I)
    # a headline with the article's first sentence run on ("...'ending fairly soon' Soaring
    # oil and gas prices following the war...", 10:34 AM call): cut where the sentence starts
    if len(title.split()) > 14 and (m := re.search(r"^(.{20,}?['\"\u2019?!])\s+(?=[A-Z][a-z]+\s+[a-z])", title)):
        return m.group(1).strip(" .:;,")
    if re.search(r"(\.\.\.|\u2026)\s*$", raw):
        lede = re.split(r"(?<=[.!?])\s", _clean_text(r.get("content", "")))[0]
        if 6 <= len(lede.split()) <= 30 and not lede.endswith(("...", "\u2026")):
            return lede.rstrip(" .")
        title = _DANGLING.sub("", title)
    return re.sub(r"\s+", " ", title).strip(" .:;,")


def _is_story(r: dict) -> bool:
    """A single story, not a section front ("theguardian.com/world/middleeast"):
    story links carry a date, an id with digits, or a hyphenated slug."""
    title, path = _headline(r), urlparse(r.get("url", "")).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1]
    is_article = bool(re.search(r"\d", path)) or slug.count("-") >= 2
    return is_article and len(title.split()) >= 4 and not _NOT_A_STORY.search(r.get("title", "") + " " + title)


def _news_query(text: str) -> tuple[str, str] | None:
    """(query, spoken label) when the user is asking for the news, else None.
    'What's going on in World News this morning?' -> ('world news', 'world')"""
    text = _without_small_talk(text) or text
    text = re.sub(r"\bU\.S\.(?:A\.)?|\bU\.S\b", "US", text)  # "U.S. news": the dots split the word
    if re.search(r"\b(?:what'?s|anything|any)\b[^.?!]*\b(?:new|latest)\b[^.?!]*\b(?:computer arts|wired)\b|"
                 r"\b(?:computer arts|wired)\b[^.?!]*\b(?:new|latest)\b", text, re.I):
        # "What's new in Computer Arts?" / "Anything new on Wired?": that magazine's feed
        return ("design news", "design") if re.search(r"computer arts", text, re.I) else ("tech news", "tech")
    for label, rx in sources.TOPIC_ASKS.items():   # gardening / recipes / books / movies and TV: the built-in feeds
        if rx.search(text):
            return sources.TOPIC_QUERY[label], label
    if re.search(r"\b(?:most (?:popular|read|viewed|shared)|what(?:'s| are| is)? (?:people|everyone) reading|"
                 r"trending (?:stories|articles|news))\b", text, re.I):
        return "most popular", "popular"  # NYT Most Popular
    if re.search(r"\b(?:anything|what'?s|whats)\s+(?:new|happening|going on)\b[^.?!]*\b(?:pc|graphics?|gpus?|"
                 r"graphics cards?|cpus?|hardware)\b", text, re.I):
        # "Tell me about anything new in the PC graphic world?" (Nov 10 call): the hardware roundup
        return "PC hardware news", "hardware"
    if not _NEWS.search(text):
        return None
    # State / local news come from free feeds (personal.json, sources.py), not Tavily.
    # "Washington news" alone stays national (DC); "Washington state news" is the state.
    if sources.STATE_NEWS.search(text):
        return f"{sources.STATE_LABEL} news", sources.STATE_LABEL
    if sources.LOCAL_NEWS.search(text):
        return "local news", "local"
    if sources.HARDWARE_NEWS.search(text):  # Tom's Hardware, TechPowerUp... (free feeds)
        return "PC hardware news", "hardware"
    if sources.DESIGN_NEWS.search(text):  # Computer Arts Magazine, Wired culture
        return "design news", "design"
    if sources.TECH_NEWS.search(text):  # Wired, Ars Technica, Tom's Hardware
        return "tech news", "tech"
    if re.search(r"\b(going on|happening)\s+(in|around)\s+the\s+world\b", text, re.I):
        topic = "world"
    elif m := _NEWS_AFTER.search(text):
        topic = m.group(1)
    else:
        m = _NEWS_BEFORE.search(text)
        topic = " ".join("US" if _US_WORD.match(w) else w for w in (m.group(1).split() if m else [])
                         if _US_WORD.match(w) or w.lower() not in _NOT_TOPIC)
    topic = re.sub(r"\s+", " ", topic).strip(" ,.?!")
    if _US_WORD.match(topic):
        topic = "US"
    if _PERSONAL.search(topic) or _user_name() and re.search(re.escape(_user_name()), topic, re.I):
        topic = ""  # stays private: general headlines instead
    if not topic:
        return "world news", ""  # "top news" alone was mostly sports scores
    return f"{topic} news", topic.lower() if topic.lower() == "world" else topic


def _fit(sentence: str, max_words: int) -> str:
    """One overlong sentence cut at a clause end. Tavily strings news together
    with semicolons; Rime refuses anything over about 1,000 characters."""
    if len(sentence.split()) <= max_words:
        return sentence
    clauses = re.split(r";\s+", sentence)
    out = clauses[0]
    for c in clauses[1:]:
        if len((out + "; " + c).split()) > max_words:
            break
        out += "; " + c
    if len(out.split()) > max_words:  # one huge clause: stop at the last comma that fits
        cut = " ".join(out.split()[:max_words])
        out = cut[:cut.rfind(",")] if "," in cut else cut
    return out.rstrip(" ,;:.") + "."


def _short_answer(answer, max_words: int = 65, max_sentences: int = 3) -> str:
    """Tavily's written answer cut to what suits speech: whole sentences, up to
    three and about 65 words, so "sources disagree: 65 vs 120 miles" stays whole."""
    text = _AS_SOURCES_SAY.sub("", _clean_text(answer or ""))
    if not text:
        return ""
    parts, sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", text), []
    for p in parts:  # re-join splits after "St." / "U.S." etc.
        if sentences and _ABBREV.search(sentences[-1]):
            sentences[-1] += " " + p
        else:
            sentences.append(p)
    out = _fit(sentences[0], max_words)
    for s in sentences[1:max_sentences]:
        if len((out + " " + s).split()) > max_words:
            break
        out += " " + s
    return out[0].upper() + out[1:]


# "How old am I?", "Who am I?", "What's my name?": about the user themselves. The web
# can't answer these and they must never reach Tavily. "How do I fix a tap?" is
# still a real question (the model writes that query, since it says "I").
_ABOUT_USER = re.compile(
    r"\b(am i|i am)\b\s*[?.!]?\s*$|\b(who|what|how old|where|when) (am i|i am)\b|"
    r"\bwhat(?:'s| is| was| are) my\b|\bdo you know (my|who i am|how old)\b|"
    r"\bwhen (is|was) my\b|\bhow old is my\b|"
    r"\bwhere (?:do|did) (?:i|we) live\b|\bwhat (?:town|city|county|state) (?:do|am|are) (?:i|we)\b", re.I)
# "Othos, where do I live?" searched the web for "my current location" (8:48 AM call).
# Answered by code from the user's home town (OTHOS_HOME_LOCATION, or the weather town).
_WHERE_LIVE = re.compile(
    r"\b(?:where (?:do|did) (?:i|we) live|where(?:'s| is) (?:my|our) (?:home|house)|where am i|where are we|"
    r"what (?:town|city|county|state) (?:do|am|are) (?:i|we)(?: live)?(?: in)?|"
    r"what(?:'s| is) my (?:town|city|address|location|home ?town)|where(?:'s| is) home)\b", re.I)


# Asking to save something: the model must word the note, so it makes that call.
_SAVE_REQUEST = re.compile(
    r"\b(remember (that|to|this|it)|remind me (to|that|on|at|tomorrow|tonight|next)|"
    r"(make|take|add|save) (a |this |that )?note|note (that|down|this)|save (that|this|it)|"
    r"write (that |this |it )?down|don'?t (let me )?forget)\b", re.I)
# Asking about notes: code runs search_notes at once (the model took 12.5 s to write that call)
_NOTES_QUESTION = re.compile(
    r"\?|^\W*(?:(?:hey|hi|ok|okay|so|and|but|also|um|uh)\W+)*(?:(?:othos|osos)\W+)?"
    r"(what|when|where|who|which|how|do|did|does|is|are|am|have|has|any|tell me|read|"
    r"remind me (what|when|where|who|about|of)|can you (tell|remind|read|find|check|look))\b", re.I)


def _notes_question(text: str) -> bool:
    """A notes turn that only asks (no saving): code can search straight away."""
    return bool(_NOTES_QUESTION.search(text)) and not _SAVE_REQUEST.search(text)


# ---------------------------------------------------------------- camera look
# Asking Othos to look ("look up X" / "look for X" stay web search). Only acted
# on while the user's camera is on; with it off, code says how to turn it on.
_ASKS_LOOK = re.compile(
    r"\b(?:(?:take|have) a look|look(?:ing)? (?:at|here|now)|"
    r"can you see|do you see|what (?:do|can) you see|"
    r"what am i (?:holding|wearing|showing you|doing|pointing at)|what(?:'s| is) in my hand|"
    r"am i holding (?:anything|something)|"
    r"read (?:this|that|it|the label)|what colou?r is (?:this|that|it)|how many fingers|look again|"
    r"what colou?rs? (?:is|are) my (?:shirt|top|t-?shirt|jumper|sweater|hoodie|jacket|coat|hat|glasses|mug|cup|nails)|"
    r"what does (?:it|this|that|the (?:bag|label|sign|box|can|bottle|package|packet|paper|card|note|"
    r"screen|book|jar|tin|carton|letter|envelope|cover|shirt|mug)) say|what(?:'s| is) written)\b|"
    r"\bwhat(?:'s| is) (?:this|that)(?: thing)?\W*$",  # the whole question, not "what is this song"
    re.I)
# Moondream sees "the person", not "I": "What am I holding?" -> "What is the person holding?"
_FIRST_PERSON = [
    (re.compile(r"\bam i\b", re.I), "is the person"),
    (re.compile(r"\bi'?m\b|\bi am\b", re.I), "the person is"),
    (re.compile(r"\bmy\b", re.I), "the person's"),
    (re.compile(r"\bme\b", re.I), "the person"),
    (re.compile(r"\bi\b", re.I), "the person"),
]
_LOOK_FILLER = re.compile(
    r"^\W*(?:(?:hey|hi|ok|okay|so|and|but|also)\W+)*(?:(?:othos|osos|otis)\W+)?|"
    r"\b(?:can|could|would|will) you(?: please)?\s+|\bplease\b|"
    r"\b(?:take|have) a look(?: at this| at that| at it)?(?: and tell me)?\W*|\blook(?: here| now)\W*|"
    r"\btell me\b\W*", re.I)
# Moondream's own phrasing; "Describe what you see in one or two short sentences"
# made it start mid-word ("urn of blue paper...")
_GENERIC_LOOK = "Describe this image."


def _vision_prompt(text: str) -> str:
    """The user's words as a question for the vision model; generic asks get a description."""
    # only the sentence(s) asking to look: "My pleasure. Othos, can you see me?" -> "can you see me?"
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if _ASKS_LOOK.search(s)]
    text = " ".join(sentences) or text
    if re.search(r"\bread\b|\bsay\b|\bwritten\b", text, re.I):  # "Can you read this?", "What does the bag say?"
        return "What does the text in this image say?"
    if re.search(r"\b(can|do) you see\b|\bwhat (do|can) you see\b|\banything else\b", text, re.I) \
            and not re.search(r"\bholding\b|\bin my hands?\b", text, re.I):
        return _GENERIC_LOOK
    if re.search(r"\b(am i holding|holding anything|in my hands?)\b", text, re.I):
        return _HELD_PROMPT
    q = re.sub(r"\b(othos|osos|otis)\b", " ", _LOOK_FILLER.sub(" ", text), flags=re.I)
    q = re.sub(r"\blook again\b", " ", q, flags=re.I)
    q = re.sub(r"\s+", " ", q).strip(" ,.!")
    if len(q.split()) < 3:
        return _GENERIC_LOOK
    for rx, rep in _FIRST_PERSON:
        q = rx.sub(rep, q)
    return q[0].upper() + q[1:] + ("" if q.endswith("?") else "?")


def _camera_publication():
    """The user's camera track, if they have turned it on (and not paused it)."""
    try:
        room = get_job_context().room
    except RuntimeError:
        return None
    for p in room.remote_participants.values():
        for pub in p.track_publications.values():
            if pub.source == rtc.TrackSource.SOURCE_CAMERA and not pub.muted:
                return pub
    return None


# "Othos, switch to code chat (and help me write a regex)": the call page opens the
# typed code chat (Othos-ui /code) and asks whatever came after "and".
CODE_CHAT_RPC = "othos.openCodeChat"
# Phrases from the user's calls: "switch to coding" (worked), "hand off to chat code" and
# "hand off to coding model" (2026-11-09: weren't recognized, now are).
_CODE_TARGET = (r"(?:the\s+)?(?:othos\s+)?(?:chat\s+code|code|coding)"
                r"(?:\s+(?:chat|mode|page|window|model|assistant|helper|side))?\b(?!\s+of\b)")
_CODE_CHAT = re.compile(
    r"\b(?:switch|go|move|change|jump|take me|send me|"
    r"hand(?:\s+(?:it|this|me|us))?\s+(?:off|over)|pass(?:\s+(?:it|this|me|us))?(?:\s+(?:off|over|along))?|"
    r"transfer(?:\s+(?:it|this|me|us))?|let'?s (?:go|switch|move|hand off))"
    r"\s+(?:over\s+)?(?:to|into)\s+" + _CODE_TARGET + r"|"
    r"\bopen\s+(?:up\s+)?(?:the\s+)?(?:othos\s+)?(?:chat\s+code|(?:code|coding)\s+(?:chat|mode|page|window))\b|"
    r"\b(?:code|coding)\s+chat,?\s+(?:please|now)\b|\bcoding mode\b",
    re.I)
_CODE_CHAT_LEAD = re.compile(r"^[\s,.;:!-]*(?:(?:and|then|so|to)\b[\s,]*)?(?:please\b[\s,]*)?", re.I)


def _code_chat_request(text: str) -> str | None:
    """None if the user isn't asking for the code chat; else what to ask there ('' if nothing)."""
    m = _CODE_CHAT.search(text)
    if not m:
        return None
    rest = _CODE_CHAT_LEAD.sub("", text[m.end():]).strip()
    return rest if len(rest.split()) >= 2 else ""


# "Gaming mode on/off", "turn off gaming mode", "is gaming mode on?" (gaming.py)
_GAME_MODE = r"gam(?:ing|e|er)\s+mode"
_GAMING_CMD = re.compile(
    rf"\b(?:(?P<on1>start|enable|enter|begin|use|switch\s+(?:on|to)|turn\s+on|put\s+on|go\s+(?:in)?to)\s+(?:the\s+)?{_GAME_MODE}"
    rf"|(?P<off1>stop|end|exit|leave|disable|quit|turn\s+off|switch\s+off|cancel)\s+(?:the\s+)?{_GAME_MODE}"
    rf"|{_GAME_MODE}\s*,?\s*(?P<on2>on|please)\b|{_GAME_MODE}\s*,?\s*(?P<off2>off)\b"
    rf"|(?:turn|switch|put)\s+(?:the\s+)?{_GAME_MODE}\s+(?P<on3>on)|(?:turn|switch|put)\s+(?:the\s+)?{_GAME_MODE}\s+(?P<off3>off))",
    re.I)
_GAMING_ASK = re.compile(rf"\b(?:is|are you in|what about)\s+(?:the\s+)?{_GAME_MODE}\b|\b{_GAME_MODE}\s+(?:status|still on)\b", re.I)


# Firestarter (site work) and the Smithy (coding), the CPU workers: what voice Othos may
# do with them lives in workers_voice.py (start/stop/list/read/progress/show/ask/keep/
# discard/work logs, all recognised in code from the user's words; marks only, never applies anything).


def _gaming_command(text: str) -> str | None:
    """'on', 'off', 'status', or None. A question ("Is gaming mode on?") is a status check."""
    if _GAMING_ASK.search(text):
        return "status"
    m = _GAMING_CMD.search(text)
    if m:
        return "off" if (m.group("off1") or m.group("off2") or m.group("off3")) else "on"
    return None


async def _open_code_chat(request: str, session=None, max_wait: float = 10.0) -> None:
    """Tell the user's call page to switch to the code chat, once "Opening code chat" has
    been said in full. (A fixed 2.5 s cut it off at "See you" on the 07:02 call.)"""
    await asyncio.sleep(0.8)  # let the reply start
    deadline = time.monotonic() + max_wait
    while session is not None and time.monotonic() < deadline:
        speech = session.current_speech
        if speech is None or speech.done():
            break
        await asyncio.sleep(0.2)
    await asyncio.sleep(0.4)  # the last word's audio is still on its way
    try:
        room = get_job_context().room
    except RuntimeError:
        return
    for identity in list(room.remote_participants):
        try:
            await room.local_participant.perform_rpc(
                destination_identity=identity, method=CODE_CHAT_RPC,
                payload=json.dumps({"q": request}), response_timeout=5.0)
            log.info("code chat opened on the call page (request: %r)", request)
        except Exception as e:
            log.warning("couldn't open the code chat on the call page: %s", e)


# Live market prices: no quote service (Finnhub set aside, no new keys: the user, Nov 10).
# Tavily's "NVIDIA stock price" answer read "$3.36" off an earnings-estimate table, so price
# questions get _price_answer: a $ figure only if it sits next to price words in the sources.
_PRICE_ASK = re.compile(
    r"\b(?:stock|share)s? (?:price|value|quote)\b|\bstock (?:is|at) (?:at|trading)\b|\btrading at\b|\bmarket cap\b|"
    r"\bprice of (?:a |one )?(?:share|stock)\b|\bhow much is (?:a share of |one share of )?[\w.&'-]+ stock\b|"
    r"\b(?:bitcoin|btc|ethereum|crypto\w*|dogecoin)\b[^.?!]*\b(?:price|worth|trading|at)\b|"
    r"\bhow (?:is|are|did) (?:the )?(?:stock market|dow|nasdaq|s&p|[\w.&'-]+ stock) (?:doing|today|trading)\b", re.I)


# "Othos, end the call" / "hang up" / "Othos, end" (Nov 10 call: "Othos, end" did nothing).
# The whole turn must be the command, so "the end of the story" never hangs up.
_END_CALL = re.compile(
    r"^\W*(?:(?:okay|ok|alright|all right|thanks|thank you|bye|goodbye|good night)\W+)*(?:othos\W+|osos\W+|otis\W+)?"
    r"(?:(?:please|you can|go ahead and|let's)\W+)?(?:end|hang up|disconnect)(?:\W+(?:the|this|our))?"
    r"(?:\W+(?:call|chat|conversation))?(?:\W+(?:now|please|for now|othos))*\W*$|"
    r"^\W*(?:othos\W+)?(?:that'?s all|i'?m done|we'?re done)\W+(?:end|hang up)(?:\W+(?:the|this))?\W+call\W*$", re.I)


async def _end_call(session=None, max_wait: float = 8.0) -> None:
    """Hang up once the goodbye has been said: closing the room ends the call page's call."""
    await asyncio.sleep(0.8)
    deadline = time.monotonic() + max_wait
    while session is not None and time.monotonic() < deadline:
        speech = session.current_speech
        if speech is None or speech.done():
            break
        await asyncio.sleep(0.2)
    await asyncio.sleep(0.5)  # the last word's audio is still on its way
    try:
        ctx = get_job_context()
        log.info("ending the call by voice")
        await ctx.delete_room()
    except Exception as e:
        log.warning("couldn't end the call: %s", e)


async def _grab_frame(pub, timeout: float = 5.0):
    """Subscribe to the user's camera just long enough to get one picture, then let go,
    so video is only received and decoded while Othos is looking."""
    pub.set_subscribed(True)
    try:
        t0 = time.monotonic()
        while pub.track is None:
            if time.monotonic() - t0 > timeout:
                return None
            await asyncio.sleep(0.05)
        stream = rtc.VideoStream(pub.track)
        try:
            async def settled():
                first = None
                async for ev in stream:  # skip ~0.3 s so the picture isn't a half-built first frame
                    first = first or time.monotonic()
                    if time.monotonic() - first >= 0.3:
                        return ev.frame
            return await asyncio.wait_for(settled(), timeout - (time.monotonic() - t0))
        except asyncio.TimeoutError:
            return None
        finally:
            await stream.aclose()
    finally:
        pub.set_subscribed(False)


# the user, after "an older woman ... her face is contorted": "In the future, just
# say a woman. You can describe me as having gray hair. It's not so much about
# accuracy as it is about kindness." And: "Descriptions are fine as long as it
# leaves out age or weight." Moondream doesn't take that reliably as an
# instruction, so code applies it: age words go, and sentences about age or weight.
_AGE_WORDS = re.compile(r"\b(?:an? )?(?:older|elderly|old|aged|middle-aged|senior|young(?:er)?)"
                        r"(?= (?:woman|man|person|lady|gentleman|people|women|men)\b)\s*", re.I)
# sentences still about age or weight after the adjectives are gone are dropped;
# body words only count in a sentence about a person ("a thin red wire" stays)
_UNKIND = re.compile(r"\b(wrinkl\w*|aging|ageing|in (?:her|his|their) (?:\w+-)?\w+ies|\d+s?\b years old|"
                     r"weight|overweight|underweight|obese|large build)\b|"
                     r"\b(?:she|he|they|person|woman|man|lady|gentleman)\b[^.!?]*\b(?:heavy|heavy-set|heavyset|"
                     r"chubby|plump|fat|skinny|thin|slender|slim|stocky|portly|petite)\b", re.I)


_WEIGHT_WORDS = re.compile(r"\b(?:heavy-set|heavyset|heavy|chubby|plump|fat|obese|overweight|skinny|thin|"
                           r"slender|slim|stocky|portly|petite)(?:,? and)?\s+(?=(?:\w+ )?(?:woman|man|person|lady|"
                           r"gentleman|people|women|men)\b)", re.I)
# "a man in his sixties" -> "a man" (right after the person only; "She looks to be in her
# fifties." is dropped whole by _UNKIND instead of leaving "She looks to be.")
_AGE_PHRASE = re.compile(r"\b(woman|man|person|lady|gentleman)\s+(?:who (?:is|appears to be|looks) )?"
                         r"in (?:her|his|their) (?:\w+-)?\w+ies\b", re.I)


def _kind(text: str) -> str:
    text = _AGE_WORDS.sub(lambda m: "a " if m.group(0).lower().startswith(("a ", "an ")) else "", text)
    text = re.sub(r"\ban a\b|\ba an\b", "a", text, flags=re.I)
    text = re.sub(r"^(?:In|From) (?:the|this) (?:image|picture|photo),?\s*", "", text, flags=re.I)
    text = re.sub(r"^(?:The|This) (?:image|picture|photo) (?:shows|features|depicts)\s+", "", text, flags=re.I)
    # Moondream sometimes adds things that aren't there (a red cup the user wasn't
    # holding), so it is worded as its impression, not a fact
    return "It looks like " + text[0].lower() + text[1:] if text else text


# Questioning what Othos just said it saw: "There's no red cup. Where did you see
# that?" The language model denied having said it, so code answers honestly.
_LOOK_CHALLENGE = re.compile(
    r"\b(where did you see|where(?:'d| did) (?:that|it|you get)|there(?:'s| is) no|there are no|"
    r"i'?m not (?:holding|wearing)|i don'?t have|that'?s (?:wrong|not right|not true|not there)|"
    r"you'?re wrong|made (?:that|it) up|what are you talking about|i'?m holding (?:only|just) one)\b", re.I)
LOOK_MEMORY_S = 180  # a challenge or follow-up this soon after a look is about that look
# Follow-ups about the last picture ("Am I holding anything?", "What colour is the
# cup?") are answered from that same picture: Moondream has already read it, so
# it takes a few seconds instead of ~30. The picture stays in memory only (never
# on disk) and is dropped after LOOK_MEMORY_S. "Look again" takes a new one.
_LOOK_FOLLOWUP = re.compile(
    r"\b(am i holding|holding anything|in my hands?|do you (?:notice|see) anything|anything else|"
    r"what colou?r|what kind of|what brand|how many (?:fingers|things|items|cups)|behind me|on (?:the|my) (?:table|wall|desk|shirt|lap))\b",
    re.I)
_LOOK_AGAIN = re.compile(r"\b(look again|another look|new picture|look now|now can you see|can you see me)\b", re.I)
_HELD_PROMPT = "What is the person holding in their hands? If nothing, say nothing."
_DETAIL_PROMPT = "Describe this image in detail."


# What the user is holding is where Moondream invents things (the red cup), so Othos
# asks instead of telling, as the user suggested: "Are you holding A?"
_HAND = re.compile(r"\s+in (?:her|his|their|the person's) (?:right |left |both )?hands?\b", re.I)
_HOLDING_SENTENCE = re.compile(
    r"^(?:she|he|they|the person|the woman|the man) (?:is|are|appears to be|seems to be) holding (.+?)[.!]?$", re.I)
_HOLDING_CLAUSE = re.compile(r",?\s*(?:and )?(?:is )?holding ([^,.]+?)(?=[,.]|$)", re.I)


def _holding_as_question(text: str) -> tuple[str, str]:
    """'... She is holding an orange cup in her right hand and a red cup in her left hand.'
    -> ('...', 'Are you holding an orange cup and a red cup?')"""
    held, kept = [], []
    for s in re.split(r"(?<=[.!?])\s+", text):
        if m := _HOLDING_SENTENCE.match(s):
            held.append(m.group(1))
            continue
        if m := _HOLDING_CLAUSE.search(s):
            held.append(m.group(1))
            s = (s[:m.start()] + s[m.end():]).strip()
            s = re.sub(r"\s+([,.])", r"\1", s)
        kept.append(s)
    if not held:
        return text, ""
    # one thing only: Moondream named the same cup twice ("a red cup and a red mug")
    things = re.split(r"\s+and\s+(?=an?\b|the\b|some\b)", _HAND.sub("", held[0]).strip(), maxsplit=1)[0]
    things = _HAND.sub("", things).strip()
    things = re.sub(r"\b(?:her|his|their)\b", "your", things, flags=re.I)
    kept = [k if re.search(r"[.!?]$", k) else k + "." for k in kept if k and not re.fullmatch(r"\W*", k)]
    return " ".join(kept), f"Are you holding {things}?"


def _held_from(answer: str) -> str:
    """Moondream's answer to _HELD_PROMPT as a sentence, or '' when nothing is held."""
    a = re.sub(r"\s+", " ", answer or "").strip().rstrip(".")
    if not a or re.search(r"\b(nothing|not holding|no(?:thing)? in|empty)\b|^no\b", a, re.I):
        return ""
    m = re.search(r"\bholding (.+)$", a, re.I)
    return f"The person is holding {m.group(1) if m else a[0].lower() + a[1:]}."


def _spoken_vision(text: str, held: str = "") -> str:
    """Moondream's answer, made kind and cut to two sentences for speech. `held`
    is its separate answer about what the user is holding, asked as a question."""
    text = re.sub(r"\s+", " ", text or "").strip()
    # take age and weight words out where they're just adjectives, so the rest of
    # the sentence stays ("A heavy-set man in his sixties holds a cup" -> "A man holds a cup")
    text = _AGE_PHRASE.sub(r"\1", _WEIGHT_WORDS.sub("", text))
    parts = [p for p in re.split(r"(?<=[.!?])\s+", text) if p and not _UNKIND.search(p)]
    # find what the user is holding in the whole answer first, then keep two sentences
    desc, question = _holding_as_question(" ".join(parts).strip())
    desc = " ".join(re.split(r"(?<=[.!?])\s+", desc)[:2])
    if held and not question:
        question = _holding_as_question(held)[1]
    # "The image shows a person. The bag is red" -> no bare "It looks like a person."
    desc = re.sub(r"^(?:The image shows |In the image, )?(?:an?|the) (?:person|woman|man)\.\s*", "", desc, flags=re.I)
    out = _kind(desc)
    out = out[0].upper() + out[1:] if out else ""
    return f"{out} {question}".strip()


# "Please look at the game against the Cardinals" was taken as a camera request
_NOT_CAMERA_LOOK = re.compile(
    r"\blook(?:ing)?\s+(?:into|for|up|over|"
    r"at\s+(?:the\s+|that\s+|this\s+)?(?:game|match|score|scores|news|results?|numbers|stats|standings|"
    r"forecast|weather|schedule|article|story|website|page|link|history|data|research|report))\b", re.I)


def _asks_look(text: str) -> bool:
    """Asking Othos to look with the camera (not "look at the game", "look into it")."""
    return bool(_ASKS_LOOK.search(text)) and not _NOT_CAMERA_LOOK.search(text)


def _tool_names_for(text: str, has_results: bool) -> set[str]:
    """Which tools fit what the user just said. Empty set = just talk."""
    if _asks_look(text) and _camera_publication():
        return {"look"}  # camera on and asked to look: nothing else this turn
    text = sources.join_spelled(_spoken_models(_without_small_talk(text)))  # "my forty sixty TI" is a graphics card
    if not text:
        return set()  # "Hello. What's going on?": just chat, no tools
    if google_tools.asks_calendar(text) and not _SEARCH_WORDS.search(text):
        return set()  # "Am I free Thursday?": the calendar, answered by code in llm_node (never a web search)
    if _ASKS_NOTES.search(text):
        return {"save_note", "search_notes"}
    names = set()
    if has_results and _ASKS_REPEAT.search(text) and not _SEARCH_WORDS.search(text):
        return names  # "what were those names again?": answer from what was said
    plain = _REQUEST_FILLER.sub(" ", text)  # "Can you tell me what X is?" -> "what X is?"
    if _ABOUT_USER.search(text) and not _SEARCH_WORDS.search(text):
        return names  # "How old am I?": answer from what Othos knows, no search
    if _RESEARCH.search(text) and (has_results or not _RESEARCH_ONLY.search(text)):
        names.add("web_search")  # "Please research the game...", "verify that"
    topic = _curious_topic(text)
    if topic and not _PERSONAL.search(topic) and not _ABOUT_OTHOS.search(topic):
        names.add("web_search")  # "I wonder how tall Everest is": judged by its topic
    elif _ASKS_WEATHER.search(text) and not _ABOUT_OTHOS.search(plain):
        names.add("web_search")  # "Is it going to rain tomorrow in Denver?"
    if _SEARCH_WORDS.search(text) or (
            (_ASKS_SEARCH.search(text) or _is_fact_question(plain)) and not _ABOUT_OTHOS.search(plain)):
        names.add("web_search")
    if (sources.topic_of(text) and (_QUESTION.search(text) or _YES_NO_ASK.match(text))) \
            or sources.wants_videos(text) or sources.wants_patterns(text) or sources.twitch_game_asked(text) \
            or sources.BOOKS_ASK.search(text) \
            or (_news_query(text) and not _ASKS_NOTES.search(text)):  # "Anything new on Wired?"
        # "Any free crochet hat patterns?", "Are there any good OBS tutorials?", "Have there
        # been any Joomla security updates?": the user's topics, answered from the free sources first
        names.add("web_search")
    if searchctx.says_near_me(text) and not _ABOUT_OTHOS.search(plain):
        names.add("web_search")  # "Where's a butcher near me?", "Find me a coffee shop nearby": found by the contraction
    if _LOCAL_NEW.search(text):
        names.add("web_search")  # "Any new businesses in Fairview?"
    if has_results and _ASKS_MORE.search(text):
        names |= {"more_results", "web_search"}
    return names


_YES_NO_ASK = re.compile(r"^\W*(?:any|are there|is there|have there|has there|should|can|could|do|does|did|is|are|"
                         r"will|would|which|got any)\b", re.I)


# ---------------------------------------------------------------- name filter
# the user's request: Othos should not say the user's name in every reply, only when
# greeting, saying goodbye, or for urgent emphasis. The 3B model ignores that
# instruction often, so ordinary replies have the name taken out in code.
_GOODBYE = re.compile(
    r"\b(bye|goodbye|good night|goodnight|see you|talk (to you )?later|that's all|"
    r"end (the|this) call|hang(ing)? up)\b",
    re.IGNORECASE,
)
_URGENT = re.compile(
    r"\b(urgent|urgently|important|careful|warning|immediately|right away|danger|"
    r"emergency|asap|don't forget)\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.!?](\s+)")


def _timing_line(item) -> str:
    """Where a turn's wait went, from LiveKit's metrics on each message (never the words). Nov 19: replies took
    12-20 s and the log couldn't say whether the model or the voice was slow."""
    m = getattr(item, "metrics", None) or {}
    if getattr(item, "role", "") == "assistant":
        parts = [f"{label} {m[key]:.1f} s" for key, label in (("llm_node_ttft", "model's first words"),
                                                              ("tts_node_ttfb", "voice's first audio")) if key in m]
        if "e2e_latency" in m:
            return f"reply timing: {m['e2e_latency']:.1f} s until Othos spoke" + (f" ({', '.join(parts)})" if parts else "")
        return f"reply timing: {', '.join(parts)}" if parts else ""
    if getattr(item, "role", "") == "user":
        parts = [f"{label} {m[key]:.1f} s" for key, label in (("transcription_delay", "transcript"),
                                                              ("end_of_turn_delay", "end of turn")) if key in m]
        return f"heard timing: {', '.join(parts)}" if parts else ""
    return ""


def _note_timing(item) -> None:
    """Log a message's timing line, and give the self-check ("How are you?") the time Othos took to start answering."""
    if line := _timing_line(item):
        log.info(line)
    m = getattr(item, "metrics", None) or {}
    if getattr(item, "role", "") == "assistant" and m.get("llm_node_ttft", -1) >= 0:
        selfcheck.note_reply(m["llm_node_ttft"])


def _held_whole_turn(text: str) -> bool:
    """A short turn that was just the thing to hold on to ("I'm nonbinary", "My name is now Jo"): Othos says its
    one line back. A longer turn gets the model's normal reply instead (the held thing is saved either way)."""
    return len(re.findall(r"\w+", text)) <= 8


def _user_name() -> str | None:
    """The name the user asked to be called (held.py), else personal.json's name, else profile.md's "Address the
    user as ...". What the user said in a call wins, so an old name in a settings file is never used."""
    if said := holdon.current_name(holdon.load()):
        return said
    if personal.name():
        return personal.name()
    m = re.search(r"(?:address the user as|user's name is|my name is|name is)\s+([A-Z][\w'-]*)",
                  load_profile(), re.IGNORECASE)
    return m.group(1) if m else None


class _NameFilter:
    """Takes the user's name out of a streamed reply, one sentence at a time.
    A sentence that sounds urgent keeps the name."""

    def __init__(self, name: str):
        n = re.escape(name)
        self._subs = [
            # "Yes, I can hear you, the user." -> "Yes, I can hear you."
            (re.compile(rf"\s*,\s*{n}\b(?=\s*[.!?,;:]|\s*$)", re.I), ""),
            # "Hello the user!" / "Sure, the user" -> "Hello!" / "Sure"
            (re.compile(rf"\b(hi|hello|hey|oh|well|sure|yes|okay|ok|great|thanks|thank you|"
                        rf"good (?:morning|afternoon|evening))[\s,]+{n}\b", re.I), r"\1"),
            # "the user, the weather..." -> "The weather..."
            (re.compile(rf"(^\s*|[.!?]\s+){n}\s*[,!:]\s*", re.I), r"\1"),
            # "thanks for asking the user." -> "thanks for asking."
            (re.compile(rf"\s+{n}\b(?=\s*[.!?])", re.I), ""),
        ]
        self._buf = ""

    def _clean(self, s: str) -> str:
        if _URGENT.search(s):
            return s
        for rx, rep in self._subs:
            s = rx.sub(rep, s)
        return re.sub(r"(^\s*|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), s)

    def feed(self, text: str) -> str:
        self._buf += text
        ends = list(_SENTENCE_END.finditer(self._buf))
        if not ends:
            return ""
        cut = ends[-1].end()
        out, self._buf = self._buf[:cut], self._buf[cut:]
        return self._clean(out)

    def flush(self) -> str:
        out, self._buf = self._buf, ""
        return self._clean(out) if out else ""


class _ClaimFilter:
    """On a turn with no search, a reply that says it found something ("Hairless cats are a breed... I found that they
    often require grooming") is reworded to what it is: "From what I know, they often require grooming." (Nov 20,
    8:47 call). One sentence at a time, like _NameFilter; the opening "Let me check..." is _claim_state's job."""

    _RX = re.compile(
        r"\b(?:i(?:'ve| have)? (?:found|discovered|learned|read|saw)(?: out)?(?: online)? that|"
        r"i (?:looked (?:it|that|this) up|did (?:some|a little|a bit of) research|did a (?:quick )?(?:web )?search|"
        r"searched (?:online|the web)?) and (?:found|learned|saw|read)(?: that)?|"
        r"(?:according to|based on) (?:my|a|the|some) (?:quick )?(?:search|research|sources?|look-?up|results?)|"
        r"my (?:search|research|look-?up) (?:shows|showed|found|says|said)(?: that)?|"
        r"(?:the )?(?:search )?results (?:show|showed|say|said)(?: that)?)\s*,?\s*", re.I)

    def __init__(self):
        self._buf = ""

    @classmethod
    def _clean(cls, s: str) -> str:
        def rep(m):
            before = s[:m.start()].rstrip()
            start = not before or before[-1] in ".!?"
            return "From what I know, " if start else "from what I know, "
        out = cls._RX.sub(rep, s)
        return out

    def feed(self, text: str) -> str:
        self._buf += text
        ends = list(_SENTENCE_END.finditer(self._buf))
        if not ends:
            return ""
        cut = ends[-1].end()
        out, self._buf = self._buf[:cut], self._buf[cut:]
        return self._clean(out)

    def flush(self) -> str:
        out, self._buf = self._buf, ""
        return self._clean(out) if out else ""


# ---------------------------------------------------------------- pronunciation
# Words the voice says wrong, respelled just before the text goes to Rime
# (place names, acronyms). Settings: pronunciations.json, plus the place names in
# personal.json's "pronunciations". The transcript on screen keeps the real spelling.
def _load_pronunciations() -> list[tuple[re.Pattern, str]]:
    try:
        raw = json.loads((Path(__file__).parent / "pronunciations.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except Exception as e:
        log.warning("pronunciations.json couldn't be read: %s", e)
        raw = {}
    raw = {**raw, **personal.pronunciations()}
    pairs = [(k, v) for k, v in raw.items() if not k.startswith("_") and isinstance(v, str) and v.strip()]
    pairs.sort(key=lambda kv: -len(kv[0]))  # "Grand Rapids" before "Grand"
    return [(re.compile(rf"(?<![\w-]){re.escape(k)}(?![\w-])", re.I), v) for k, v in pairs]


_PRONOUNCE = _load_pronunciations()


_TENS = ["", "ten", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]


def _two_digits(n: int) -> str:
    if n < 10:
        return _ONES[n] or "oh"
    if n < 20:
        return ["ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
                "eighteen", "nineteen"][n - 10]
    return _TENS[n // 10] + ("-" + _ONES[n % 10] if n % 10 else "")


# "RTX 4060 Ti" was read "four thousand sixty tie" (Nov 10 call): model numbers are said in
# pairs ("forty sixty", "fifty ninety", "seventy-nine hundred") next to a brand or suffix
_MODEL_NUM = re.compile(r"\b(?P<pre>(?:RTX|GTX|RX|Radeon|GeForce|Arc)[ -])(?P<a>\d{4})\b|"
                        r"\b(?P<b>\d{4})(?=\s*(?:Ti|Super|XTX|XT|GRE|X3D)\b)", re.I)
# "16 384 CUDA cores" (a thin space as thousands separator) and odd hyphens
_SPACED_THOUSANDS = re.compile(r"(?<=\d)[    ](?=\d{3}\b)")


def _say_number_pairs(m: re.Match) -> str:
    n = m.group("a") or m.group("b")
    hi, lo = int(n[:2]), int(n[2:])
    said = f"{_two_digits(hi)} hundred" if lo == 0 else f"{_two_digits(hi)} {_two_digits(lo)}"
    return (m.group("pre") or "") + said


# File names: the voice read the dot in "worker.py" as a full stop and paused (the user,
# Nov 12). Said as "worker dot pie", with the extension as letters or a word.
_EXT_SAID = {
    "py": "pie", "js": "J S", "jsx": "J S X", "ts": "T S", "tsx": "T S X", "json": "jason",
    "md": "M D", "ps1": "P S one", "bat": "bat", "cs": "C S", "html": "H T M L", "css": "C S S",
    "txt": "text", "php": "P H P", "xml": "X M L", "yml": "yaml", "yaml": "yaml", "ini": "I N I",
    "cfg": "config", "exe": "E X E", "log": "log", "db": "D B", "sh": "S H", "toml": "toml",
    "csv": "C S V", "gguf": "G G U F", "ico": "icon", "lnk": "link", "zip": "zip", "pdf": "P D F",
}
_FILE_NAME = re.compile(r"(?<![\w.])([A-Za-z_][\w-]*)\.(" + "|".join(sorted(_EXT_SAID, key=len, reverse=True)) +
                        r")(?!\w)(?!\.\w)", re.I)  # a full stop after it is fine
_DOT_FILE = re.compile(r"(?<![\w.])\.(env|gitignore|venv)\b", re.I)  # ".env" -> "dot env"


def _say_file_names(text: str) -> str:
    text = _FILE_NAME.sub(lambda m: f"{m.group(1).replace('_', ' ')} dot {_EXT_SAID[m.group(2).lower()]}", text)
    return _DOT_FILE.sub(lambda m: f"dot {m.group(1).lower().replace('gitignore', 'git ignore')}", text)


def _say_as(text: str) -> str:
    text = re.sub(r"[‐‑‒]", "-", text)  # non-breaking hyphens ("Blackwell‑based")
    text = _say_file_names(text)
    text = _SPACED_THOUSANDS.sub(",", text)
    text = _MODEL_NUM.sub(_say_number_pairs, text)
    for rx, said in _PRONOUNCE:
        text = rx.sub(said, text)
    return text


async def _respelled(text):
    """The voice's text stream with respellings applied; an unfinished last word is
    held until it's complete, so a name split across pieces is still caught."""
    buf = ""
    async for chunk in text:
        buf += chunk
        # keep the last whole word too: "4060" + "Ti" arriving apart still reads "forty sixty tee eye"
        last = max(buf.rfind(" "), buf.rfind("\n"))
        cut = max(buf.rfind(" ", 0, last), buf.rfind("\n", 0, last)) if last > 0 else -1
        if cut >= 0:
            yield _say_as(buf[:cut + 1])
            buf = buf[cut + 1:]
    if buf:
        yield _say_as(buf)


# ---------------------------------------------------------------- weather
# A quick Tavily search for the user's town's current weather when each call starts, so
# "this lovely sunny morning" matches the real sky. Off unless the user sets
# OTHOS_WEATHER_LOCATION in .env; the town name is all Tavily gets.
WEATHER_LOCATION = personal.weather_location()  # personal.json, or OTHOS_WEATHER_LOCATION
# Where the user lives: OTHOS_HOME_LOCATION, or else the weather town. Kept on the laptop:
# it goes into Othos's instructions (local model) and the weather search only.
HOME_LOCATION = personal.home_location()
BRAVE_ALL = os.getenv("OTHOS_BRAVE", "").strip().lower() == "all"   # every web search tries Brave first
_STATE_NAMES = {"WA": "Washington", "MO": "Missouri", "OR": "Oregon", "ID": "Idaho", "CA": "California", "MT": "Montana",
                "AK": "Alaska", "NV": "Nevada", "AZ": "Arizona", "UT": "Utah", "CO": "Colorado", "TX": "Texas",
                "NY": "New York", "FL": "Florida", "BC": "British Columbia"}


def _home_spoken() -> str:
    """'Springfield, Greene County, MO, US' -> 'Springfield, in Greene County, Missouri'."""
    parts = [p.strip() for p in HOME_LOCATION.split(",")
             if p.strip() and p.strip().upper() not in ("US", "USA", "UNITED STATES")]
    parts = [_STATE_NAMES.get(p.upper(), p) for p in parts]
    if len(parts) >= 3:
        return f"{parts[0]}, in {', '.join(parts[1:])}"
    return ", ".join(parts)
_weather_now: dict | None = None  # {"summary": "...", "adj": "cloudy", "words": {...}} for this call
_WEATHER_ADJ = {  # what the sky words in a forecast mean, and the adjective for it
    "thunderstorm": "stormy", "storm": "stormy", "snow": "snowy", "sleet": "icy", "hail": "stormy",
    "freezing rain": "icy", "rain": "rainy", "showers": "rainy", "drizzle": "drizzly", "fog": "foggy",
    "mist": "misty", "haze": "hazy", "overcast": "overcast", "mostly cloudy": "cloudy", "cloudy": "cloudy",
    "partly cloudy": "partly cloudy", "partly sunny": "partly sunny", "mostly sunny": "sunny",
    "sunny": "sunny", "clear": "clear", "windy": "windy",
}
# adjectives the model might use, and which real conditions they fit
_SKY_WORDS = {
    "sunny": {"sunny", "clear", "partly sunny"}, "bright": {"sunny", "clear", "partly sunny"},
    "clear": {"sunny", "clear"}, "cloudy": {"cloudy", "overcast", "partly cloudy", "partly sunny"},
    "overcast": {"cloudy", "overcast"}, "gray": {"cloudy", "overcast", "rainy", "drizzly", "foggy", "misty"},
    "grey": {"cloudy", "overcast", "rainy", "drizzly", "foggy", "misty"},
    "rainy": {"rainy", "drizzly", "stormy"}, "drizzly": {"rainy", "drizzly"}, "wet": {"rainy", "drizzly", "stormy"},
    "snowy": {"snowy"}, "stormy": {"stormy"}, "foggy": {"foggy", "misty"}, "misty": {"foggy", "misty"},
    "windy": {"windy", "stormy"},
}
_SKY_RX = re.compile(r"\b(" + "|".join(_SKY_WORDS) + r")\b(?=\s+(?:\w+\s+)?(?:day|morning|afternoon|evening|"
                     r"night|weather|skies|sky|outside|start|Monday|Tuesday|Wednesday|Thursday|Friday|"
                     r"Saturday|Sunday))", re.I)


def _weather_from(answer: str) -> dict | None:
    """Tavily's written answer -> {'summary', 'adj'} or None."""
    text = re.sub(r"\s+", " ", _clean_text(answer or "")).strip()
    if not text:
        return None
    summary = re.split(r"(?<=[.!?])\s+", text)[0]
    # "6.1°C (43.0°F)" -> "43°F": US units first (asking Tavily for Fahrenheit gave a forecast instead)
    summary = re.sub(r"-?\d+(?:\.\d+)?\s*°\s*C\s*\(\s*(-?\d+)(?:\.\d+)?\s*°\s*F\s*\)", r"\1°F", summary)
    summary = re.sub(r"\(\s*-?\d+(?:\.\d+)?\s*°\s*C\s*\)", "", summary)
    summary = " ".join(summary.split()[:30])
    # the sky word only from the reading itself: from the whole answer it once came from
    # another sentence and turned the user's "gray skies" into "sunny skies" (12:43 PM call)
    low = summary.lower()
    adj = next((a for k, a in sorted(_WEATHER_ADJ.items(), key=lambda kv: -len(kv[0])) if k in low), None)
    return {"summary": summary, "adj": adj}


# What the user says the weather is beats the reading for the rest of the call
# ("No. Currently, it's actually gray outside.")
_HER_SKY = re.compile(
    r"\b(?:it'?s|it is|kind of|pretty|really|very|so|actually|bit|little|quite)\s+(?:(?:a|kind of|pretty|really|"
    r"very|so|actually|little|bit|quite)\s+)*(?P<w>gray|grey|cloudy|overcast|sunny|clear|rainy|raining|drizzly|"
    r"drizzling|snowy|snowing|foggy|misty|windy|stormy|pouring|dreary|gloomy)\b", re.I)
_HER_SKY_ADJ = {"gray": "gray", "grey": "gray", "dreary": "gray", "gloomy": "gray", "raining": "rainy",
                "pouring": "rainy", "drizzling": "drizzly", "snowing": "snowy"}


def _note_her_sky(text: str) -> None:
    global _weather_now
    m = _HER_SKY.search(text or "")
    if not m:
        return
    word = m.group("w").lower()
    adj = _HER_SKY_ADJ.get(word, word)
    _weather_now = dict(_weather_now or {"summary": ""}, adj=adj, said=True)
    log.info("weather: the user says it's %s; using that", adj)


def _nws_weather(now: dict) -> dict:
    """NWS reading -> {'summary', 'adj'} ('Cloudy and 52°F (reading at the town airport).')"""
    sky, temp = now.get("sky", ""), now.get("temp_f")
    summary = (f"{sky} and {temp}°F" if sky and temp is not None else sky or f"{temp}°F")
    summary += f" (reading at {now.get('station', 'the nearest station')})."
    low = sky.lower()
    adj = next((a for k, a in sorted(_WEATHER_ADJ.items(), key=lambda kv: -len(kv[0])) if k in low), None)
    return {"summary": summary, "adj": adj}


async def _fetch_weather() -> dict | None:
    if not WEATHER_LOCATION:
        return None
    # the National Weather Service first: free, and Tavily's reading was sometimes stale
    try:
        t = time.monotonic()
        now = await asyncio.wait_for(sources.nws_now(), timeout=7)
        if now:
            w = _nws_weather(now)
            log.info("weather (NWS) for %s in %.1f s: %s", WEATHER_LOCATION, time.monotonic() - t, w)
            return w
    except Exception as e:
        log.info("NWS weather check failed: %s", e)
    if _tavily_out():
        return None
    try:
        t = time.monotonic()
        r = await asyncio.wait_for(_tavily_search(
            query=f"current weather in {WEATHER_LOCATION} right now", include_answer="basic",
            max_results=3), timeout=8)
        w = _weather_from(r.get("answer"))
        log.info("weather for %s in %.1f s: %s", WEATHER_LOCATION, time.monotonic() - t, w)
        return w
    except Exception as e:
        log.info("weather check skipped: %s", e)
        return None


# "What's the weather like outside?" searched with no place and got someone else's
# sunny 79°F (9 AM test). Now-weather with no place named is answered from the
# call-start check; other weather questions with no place get the user's town added.
_WEATHER_TOPIC = re.compile(r"\b(?:weather|temperature|forecast|rain(?:ing|y)?|snow(?:ing|y)?|sunny|cloudy|"
                            r"windy|cold|warm|hot|freezing|humid|degrees)\b", re.I)
_WEATHER_NOW = re.compile(
    r"\b(?:what(?:'s| is) the (?:weather|temperature)|how(?:'s| is) the weather|weather (?:like|outside|out there|now)|"
    r"is it (?:raining|snowing|sunny|cloudy|cold|warm|hot|windy|nice|freezing)|how (?:cold|warm|hot) is it|"
    r"what(?:'s| is) it like (?:out|outside))\b", re.I)
_WEATHER_LATER = re.compile(r"\b(?:tomorrow|tonight|later|this (?:weekend|week|afternoon|evening)|next|"
                            r"supposed to|expected|going to be|gonna be|should (?:it|we)|today|high|low|"
                            r"forecast|will it|going to|gonna|monday|tuesday|wednesday|thursday|friday|"
                            r"saturday|sunday)\b", re.I)
_PLACE_NAMED = re.compile(r"\b(?:in|at|for|near)\s+(?:[A-Z][\w'.-]+)", re.U)


# ---------------------------------------------------------------- earthquakes, air quality, smoke (by code)
_ASKS_QUAKE = re.compile(r"\b(?:earthquakes?|quakes?|tremors?|aftershocks?|seismic)\b", re.I)
_QUAKE_NOW = re.compile(r"\b(?:just|now|felt|was that|right now|minutes? ago|moment ago|shaking)\b", re.I)
_ASKS_AIR = re.compile(
    r"\b(?:air quality|aqi|air pollution|wildfire smoke|(?:any|is there|is there any) (?:wildfire )?smoke|"
    r"smoke (?:outside|today|in the air|out there|nearby|near me)|smoky (?:out|outside|today)|"
    r"how(?:'s| is) the air|is the air|(?:the )?air (?:outside|today)|wildfires? (?:smoke|nearby|near me))\b", re.I)


def _units(km: float) -> str:
    us = bool(re.search(r"\b(?:US|USA)\s*$", HOME_LOCATION or "")) or not HOME_LOCATION
    d = km * 0.621371 if us else km
    unit = "mile" if us else "kilometer"
    d = round(d) if d >= 10 else round(d, 1)
    return f"{d:g} {unit}{'' if d == 1 else 's'}"


def _hours_ago(hours: float) -> str:
    if hours < 1:
        m = max(1, round(hours * 60))
        return f"{m} minute{'' if m == 1 else 's'} ago"
    h = round(hours)
    return f"about {h} hour{'' if h == 1 else 's'} ago"


async def _quake_reply(user: str) -> str | None:
    """'Any earthquakes?', 'Was that an earthquake?': USGS's feed, the distances worked out here (nothing sent)."""
    ll = sources._latlon()
    if not ll:
        return None
    just = bool(_QUAKE_NOW.search(user))
    week = bool(re.search(r"\b(?:this|past|last) week\b", user, re.I))
    hours, feed, radius = (2, "all_day", 161) if just else (168, "2.5_week", 300) if week else (24, "all_day", 161)
    qs = await sources.usgs_quakes(ll[0], ll[1], radius, hours, feed)
    span = "in the last two hours" if just else "in the past week (magnitude 2.5 and up)" if week else "in the last day"
    near = _units(radius)
    if qs is None:
        return "I couldn't reach the U.S. Geological Survey's earthquake feed just now."
    if not qs:
        extra = " Small or very recent ones can take a few minutes to show up." if just else ""
        return f"The U.S. Geological Survey shows no earthquakes within {near} {span}.{extra}"

    def one(q: dict) -> str:
        return (f"a magnitude {q['mag']:.1f}, about {_units(q['km'])} to the {q['dir']}, near {q['town']}, "
                f"{_hours_ago(q['age_h'])}")
    big, close = max(qs, key=lambda q: q["mag"]), min(qs, key=lambda q: q["km"])
    n = len(qs)
    if just:
        text = f"Yes. The U.S. Geological Survey lists {one(qs[0])}."
        if n > 1:
            text += f" That's {n} within {near} {span}."
    else:
        text = (f"The U.S. Geological Survey lists {n} earthquake{'' if n == 1 else 's'} within {near} {span}. "
                f"The biggest was {one(big)}.")
        if close is not big:
            text += f" The closest was {one(close)}."
    if any(q["tsunami"] for q in qs):
        text += " One of them carries a tsunami flag, so check the tsunami warning center."
    return text


async def _air_reply(user: str) -> str | None:
    """'How's the air?', 'Any wildfire smoke?': AirNow's reading, NOAA's satellite smoke map (checked here) and any
    smoke or air-quality alert from the NWS."""
    ll = sources._latlon()
    if not ll:
        return None
    aq, smoke, alerts = await asyncio.gather(
        sources.airnow_current(ll[0], ll[1]), sources.smoke_map(ll[0], ll[1]), sources.nws_alerts(),
        return_exceptions=True)
    aq = None if isinstance(aq, Exception) else aq
    smoke = None if isinstance(smoke, Exception) else smoke
    alerts = [] if isinstance(alerts, Exception) else alerts
    parts = []
    if aq:
        best = max(aq, key=lambda o: o.get("AQI") or 0)
        what = {"PM2.5": "fine particles, which is what smoke is made of", "PM10": "coarse dust", "O3": "ozone"}.get(
            best.get("ParameterName"), best.get("ParameterName") or "")
        cat_name = (best.get("Category") or {}).get("Name", "unrated").lower()
        mainly = "" if cat_name == "good" else f", mainly {what}"   # nothing to explain when the air is good
        where = ", ".join(x for x in (best.get("ReportingArea"), best.get("StateCode")) if x)
        hr = best.get("HourObserved")
        when = f" at {hr % 12 or 12} {'AM' if hr < 12 else 'PM'}" if isinstance(hr, int) else ""
        parts.append(f"The air quality index is {best.get('AQI')}, which AirNow calls {cat_name}{mainly}, "
                     f"reported for {where}{when}. It's preliminary data.")
    elif aq == []:
        parts.append("AirNow has no reporting area within 50 miles of you right now.")
    elif aq is None:
        parts.append("I couldn't reach AirNow just now.")
    if smoke:
        stamp = ""
        if smoke["date"] and smoke["date"] != datetime.now(timezone.utc).strftime("%Y%m%d"):
            d = datetime.strptime(smoke["date"], "%Y%m%d")
            stamp = f" (the latest map is from {d.strftime('%B')} {d.day})"
        if smoke["overhead"]:
            parts.append(f"NOAA's satellite smoke map shows {smoke['overhead'].lower()} smoke overhead{stamp}.")
        elif smoke["nearest_km"] is not None and smoke["nearest_km"] < 322:
            parts.append(f"NOAA's satellite smoke map shows no smoke overhead; the closest is about "
                         f"{_units(smoke['nearest_km'])} away{stamp}.")
        else:
            parts.append(f"NOAA's satellite smoke map shows no smoke within {_units(322)}{stamp}.")
    bad = [a for a in alerts if re.search(r"smoke|air quality", a, re.I)]
    if bad:
        parts.append("The National Weather Service has an alert: " + bad[0].split(" issued ")[0] + ".")
    return " ".join(parts) if parts else "I couldn't reach the air quality sources just now."


async def _forecast_reply(user: str) -> str | None:
    """'Will it rain tomorrow?' -> the NWS forecast for the user's town, worded by code:
    'Tuesday: Rain. Cloudy, with a high near 57.' Plus any active alert."""
    periods = await sources.nws_forecast()
    if not periods:
        return None
    low = (user or "").lower()
    now = datetime.now()
    # by each period's date, not its name: NWS names holidays ("Veterans Day", not "Wednesday")
    weekday = lambda p: (p["start"].astimezone().strftime("%A").lower() if p.get("start") else "")
    on = lambda d: [p for p in periods if p.get("start") and p["start"].astimezone().date() == d]
    named = lambda *names: [p for p in periods if p["name"].lower() in names]
    # just after midnight, "tomorrow" means the coming day (it's Tuesday 1 AM: Tuesday)
    tomorrow = (now + timedelta(days=0 if now.hour < 4 else 1)).date()
    if re.search(r"\btomorrow (?:night|evening)\b", low):
        pick = [p for p in on(tomorrow) if not p["day"]][:1]
    elif "tomorrow" in low:
        pick = [p for p in on(tomorrow) if p["day"]][:1]
    elif re.search(r"\b(?:tonight|overnight|this evening)\b", low):
        pick = named("tonight", "overnight", "this evening")[:1]
    elif "weekend" in low:
        pick = [p for p in periods if p["day"] and weekday(p) in ("saturday", "sunday")][:2]
    elif "week" in low:
        pick = [p for p in periods if p["day"]][:5]
    elif m := re.search(rf"\b({_WD})\b", low):
        pick = [p for p in periods if weekday(p) == m.group(1) and p["day"] != ("night" in low)][:1]
    else:
        pick = periods[:2]
    pick = pick or periods[:2]
    for p in pick:  # "Veterans Day" -> "Wednesday, Veterans Day"
        wd = weekday(p).capitalize()
        if wd and not re.match(rf"(?:{wd}|tonight|overnight|today|this )", p["name"], re.I):
            p["name"] = f"{wd}, {p['name']}"
    town = HOME_LOCATION.split(",")[0].strip() if HOME_LOCATION else "your area"
    if len(pick) >= 4:  # the week: short form for each day
        parts = [f"{p['name']}, {p['short'].lower()}" + (f", high {p['temp']}" if p.get("temp") is not None else "")
                 for p in pick]
        text = f"For {town} this week: " + "; ".join(parts) + "."
    else:
        text = " ".join(f"{p['name']}: {_short_answer(p['detail'], 40, 2)}" for p in pick)
    try:
        alerts = await asyncio.wait_for(sources.nws_alerts(), timeout=4)
    except Exception:
        alerts = []
    for h in alerts[:1]:  # "Wind Advisory issued November 9 at 6:37PM PST until November 11 at 7:00PM PST by NWS..."
        event = h.split(" issued")[0].strip()
        until = re.search(r"\buntil (.+?)(?: [A-Z]{3})? by\b", h)
        text += f" There's also a {event} out" + (f" until {until.group(1)}." if until else ".")
    log.info("forecast (NWS): %s", text)
    return text


_CELSIUS_BOTH = re.compile(r"-?\d+(?:\.\d+)?\s*°\s*C\s*\(\s*(?:about\s+|around\s+|~)?(-?\d+)(?:\.\d+)?\s*°\s*F\s*\)", re.I)
_CELSIUS_PAREN = re.compile(r"\s*\(\s*(?:about\s+|around\s+|~)?-?\d+(?:\.\d+)?\s*°\s*C\s*\)", re.I)


def _fahrenheit_only(s: str) -> str:
    """'a high near 57 °F (about 13 °C)' -> 'a high near 57 °F'. The model read '13 degrees'
    off a Celsius figure on the 9:03 AM call; the user is in the US."""
    return _CELSIUS_PAREN.sub("", _CELSIUS_BOTH.sub(r"\1°F", s or ""))


def _weather_reply() -> str | None:
    """'Right now in Springfield it's overcast and 43 degrees.' from the call-start check."""
    if not _weather_now or not HOME_LOCATION:
        return None
    town = HOME_LOCATION.split(",")[0].strip()
    t = re.search(r"(-?\d+)(?:\.\d+)?\s*°\s*F", _weather_now.get("summary", ""))
    adj = _weather_now.get("adj")
    if adj and t:
        return f"Right now in {town} it's {adj} and {t.group(1)} degrees."
    if adj:
        return f"Right now in {town} it's {adj}."
    return re.sub(r"^As of [^,]+,\s*", "", _weather_now.get("summary", "")) or None


_WEATHER_CHAT = re.compile(
    r"\b(?:beautiful|lovely|nice|gorgeous|great|perfect|glorious|gloomy|dreary|gray|grey|sunny|rainy|fine)\s+"
    r"(?:day|morning|afternoon|evening|weather|skies)\b[^.!?]*\?|"
    r"\b(?:weather|outside|out there)\b[^.!?]*\b(?:isn'?t it|right|huh|eh)\s*\?", re.I)


def _fix_sky(s: str) -> str:
    """'this lovely sunny morning' -> the real sky word, or no sky word if unknown."""
    real = (_weather_now or {}).get("adj")

    def repl(m):
        word = m.group(1)
        w = word.lower()
        if real and (real == w or real in _SKY_WORDS[w] or w in _SKY_WORDS.get(real, ())
                     or (real == "gray" and w in ("cloudy", "overcast", "grey"))):
            return word
        if real:
            return real.capitalize() if word[0].isupper() else real
        return ""
    out = _SKY_RX.sub(repl, s)
    if out != s:
        log.info("weather words: %r -> %r", s.strip(), out.strip())
    return re.sub(r"\s{2,}", " ", out)


# ---------------------------------------------------------------- time filter
# The model brings up the day or time unasked ("a sense of completion on a Monday
# morning", "this Sunday morning" at 3 AM, "It's 3:31 AM and the sun isn't up yet")
# despite "Mention the date or time only when asked." Only plain chat turns are
# filtered: answers from notes or search ("your appointment is on Sunday") aren't.
_DAYS = r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
_PARTS = r"(?:morning|afternoon|evening|night)"
_MOOD = (r"(?:(?:lovely|beautiful|fine|nice|quiet|peaceful|busy|great|wonderful|sunny|rainy|cozy|early|late|"
         r"lazy|bright|cloudy|gray|grey|snowy|stormy|foggy|misty|clear|overcast|windy|crisp|chilly|cold|warm|"
         r"hot|drizzly|wet|gorgeous)\s+){0,2}")  # "this lovely sunny morning": up to two
_ASKS_TIME = re.compile(rf"\b(?:time|day|days|date|today|tonight|tomorrow|yesterday|week|weekend|"
                        rf"{_DAYS}|{_PARTS}|clock|hour|o'?clock|am|pm|sun(?:rise|set)?|dark)\b", re.I)
_TIME_PHRASE = re.compile(rf"\s*,?\s*\b(?:on this|on a|on such a|for this|for a|on|for|this)\s+{_MOOD}"
                          rf"(?:{_DAYS}(?:\s+{_PARTS})?|{_PARTS})\b", re.I)
# "to match the sunny morning" -> "to match the day" (also drops invented weather)
_MOOD_PART = re.compile(rf"\bthe\s+(?:lovely|beautiful|fine|nice|quiet|peaceful|sunny|rainy|cozy|"
                        rf"early|bright|crisp|gorgeous)\s+{_PARTS}\b", re.I)
_TIME_ONLY_SENTENCE = re.compile(
    rf"^\W*(?:it'?s|it is|what a)\s+(?:a\s+|an\s+)?{_MOOD}(?:{_DAYS}(?:\s+{_PARTS})?|{_PARTS})\W*$|"
    rf"\b\d{{1,2}}:\d{{2}}\s*(?:[ap]\.?\s?m\.?)?|\bstill dark\b|"
    rf"\bthe sun (?:is|has|hasn't|isn't|is not|will|won't)\b", re.I)


# Weather talk in a greeting ("It's a cloudy Tuesday in Springfield", "Looks rainy out")
_GREETING_WEATHER = re.compile(r"\b(?:weather|cloudy|overcast|sunny|rainy|raining|drizzl\w*|snow\w*|foggy|misty|"
                               r"windy|stormy|gray|grey|chilly|cold|warm|degrees|skies|sky)\b", re.I)
# A sentence emptied by the filters: "It's in Springfield.", "It's a.", "On this."
_FRAGMENT = re.compile(r"^\W*(?:(?:it'?s|it is|what a|such a|on|for|this is)\s+(?:a\s+|an\s+|the\s+)?"
                       r"(?:(?:in|at|on|for|here|out|there)\b[^.!?]{0,40})?)?\W*$", re.I)


def _part_of_day(hour: int) -> str | None:
    return ("morning" if 5 <= hour < 12 else "afternoon" if hour < 17 and hour >= 12
            else "evening" if 17 <= hour < 22 else None)


class _TimeFilter:
    """Sentence-buffered like _NameFilter. greeting=True keeps the day but fixes the
    part of the day; otherwise unasked day/time talk is taken out."""

    def __init__(self, greeting: bool, hour: int | None = None):
        self.greeting = greeting
        self.part = _part_of_day(datetime.now().hour if hour is None else hour)
        self._buf = ""
        self._said = False   # something has been spoken
        self._dropped = ""   # first dropped sentence, spoken at the end if nothing else was

    def _fix_part(self, s: str) -> str:
        # "this Sunday morning" at 3 AM -> "this Sunday"; "good morning" is _fix_greeting's job
        def repl(m):
            word = m.group(0)
            if self.part is None:  # late night (10 PM-5 AM): "night" itself is right
                # (dropping it left "How's your going?", 1:23 AM call); "morning" at 3 AM goes
                return word if word.lower() == "night" else ""
            return self.part.capitalize() if word[0].isupper() else self.part
        s = re.sub(rf"(?<!good )(?<=\s){_PARTS}\b", repl, s, flags=re.I)
        return re.sub(r"\s+([,.!?])", r"\1", re.sub(r"\s{2,}", " ", s))

    def _clean(self, s: str) -> str:
        if not s.strip():
            return s
        if self.greeting:
            # no weather small talk in greetings ("Beautiful day in Springfield, isn't it?"
            # while it was gray, 12:43 PM call); kept only if it's the whole greeting
            # any sentence about the user's town or its weather goes: fixing its words left
            # "Hello, Alex. It's in Springfield." (1:23 AM call, Nov 10)
            town = HOME_LOCATION.split(",")[0].strip() if HOME_LOCATION else ""
            if _WEATHER_CHAT.search(s) or _GREETING_WEATHER.search(s) or (
                    town and re.search(re.escape(town).replace(r"\-", "[- ]"), s, re.I)):
                log.info("greeting: dropped weather small talk %r", s.strip())
                self._dropped = self._dropped or s
                return ""
            # weather words match the real weather (or go, if it's unknown); no clock
            # times ("it's 09:01 AM on a pretty overcast Monday" -> "it's a pretty overcast Monday")
            s = re.sub(r"\b\d{1,2}:\d{2}\s*(?:[ap]\.?\s?m\.?)?\s*(?:on\s+(?=a\b|an\b|this\b|the\b))?", "", s, flags=re.I)
            out = self._fix_part(_fix_sky(s))
            if _FRAGMENT.match(out):  # what's left after the fixes doesn't say anything
                log.info("greeting: dropped fragment %r (was %r)", out.strip(), s.strip())
                return ""
            if out.strip():
                self._said = True
            return out
        if _TIME_ONLY_SENTENCE.search(s):
            log.info("time filter: dropped %r", s.strip())
            self._dropped = self._dropped or self._fix_part(s)
            return ""
        out = _MOOD_PART.sub("the day", _TIME_PHRASE.sub("", s))
        if out != s:
            log.info("time filter: %r -> %r", s.strip(), out.strip())
        out = _fix_sky(out)  # what's left about the weather must match the real weather
        out = re.sub(r"\s+([,.!?])", r"\1", out)
        if out != s and _FRAGMENT.match(out):
            log.info("time filter: dropped fragment %r (was %r)", out.strip(), s.strip())
            return ""
        if out.strip():
            self._said = True
        return out

    def feed(self, text: str) -> str:
        self._buf += text
        ends = list(_SENTENCE_END.finditer(self._buf))
        if not ends:
            return ""
        cut = ends[-1].end()
        out, self._buf = self._buf[:cut], self._buf[cut:]
        return "".join(self._clean(p) for p in re.split(r"(?<=[.!?])(?=\s)", out))

    def flush(self) -> str:
        out, self._buf = self._buf, ""
        rest = self._clean(out) if out else ""
        if not self._said and not rest.strip() and self._dropped:
            return self._dropped.strip()  # the whole reply was about the time: don't go silent
        return rest


def _name_allowed(chat_ctx) -> bool:
    """Greetings (Othos's first reply in a call) and goodbyes may use the name."""
    msgs = [i for i in chat_ctx.items if isinstance(i, llm.ChatMessage)]
    if not any(m.role == "assistant" for m in msgs):
        return True
    last_user = next((m for m in reversed(msgs) if m.role == "user"), None)
    return bool(last_user and _GOODBYE.search(_as_othos(last_user.text_content or "")))


# ---------------------------------------------------------------- the agent
class Assistant(Agent):
    def __init__(self):
        profile = load_profile()
        # Kept short on purpose: on a CPU-only laptop every token here costs
        # time on every turn (see agent.prev.py for the longer version).
        instructions = (
            f"You are {ASSISTANT}, a friendly voice companion with a dry, slightly sarcastic wit, like "
            "a best friend who teases: deadpan one-liners, gentle ribbing and understatement, "
            "never mean, and kind when the user is having a hard time. React to what the user "
            "says, keep replies to one or two sentences, and "
            "now and then ask one short follow-up question. No lists. You have tastes and "
            "opinions like a friend does; never say you have no preferences. "
            "For greetings and small talk, just answer; do not use tools. "
            "Use web_search for what something is or does, news, or prices. "
            "Use save_note when asked to remember something, and search_notes "
            "for things the user told you before. "
            "You hear the user clearly. Only mention the camera if the user does: you see only "
            "a picture you've just taken with it, nothing else. "
            "Never say you'll remember something unless you saved it with save_note. "
            "Never say you searched, researched or checked sources unless a web_search result "
            "is right in front of you; otherwise say you don't know. "
            "Never give specs, prices, figures or facts about places from memory; offer to look them up. "
            "Never say a shop, restaurant, event or local happening exists, is new or is open unless you were told; "
            "in small talk ask about the user's day instead, and if the user asks about something you "
            "don't know, offer to look it up yourself, never ask the user to look it up. "
            "When the user is tired, hurting or disabled, acknowledge it once in their own words, "
            "don't ask what causes it, don't try to fix it, never suggest going out or moving, and ask at most "
            "one gentle question; after a no, stop asking and just keep them company. Don't assume their tastes "
            "or condition beyond what they said, and never guess what they meant: say you don't know or ask. "
            "\"Disabled\" alone doesn't mean permanent: don't call it permanent or chronic unless they said so. "
            "You can't see your own audio, settings, or logs: if asked why you did something "
            "or how you work, just say you don't know; never make up a reason. "
            "Don't address the user by name in normal replies; use their name only "
            "when greeting them, saying goodbye, or for urgent emphasis. "
            "Mention the date or time only when asked."
        )
        if profile:
            instructions += "\n\nAbout the user and how they like you to behave:\n" + profile
        if personal.name() and not re.search(r"address the user as", profile, re.I):
            instructions += f"\n- Address the user as {personal.name()}."
        if HOME_LOCATION:  # from .env; stays on the laptop
            instructions += f"\nThe user lives in {_home_spoken()}."
        # what the user said about the jokes (humor.py) is read at the start of every call
        self._humor = humor.load()
        # what the user asked Othos to hold on to (held.py): a permanent disability, pronouns, names. A name they
        # no longer use never reaches the model, even from profile.md or personal.json: it is swapped out here.
        self._held = holdon.load()
        self._instr_base = instructions = holdon.scrub_text(instructions, self._held)
        instructions += self._prompt_extras()
        # The time goes last: it changes every minute, and anything after it can't
        # reuse Ollama's cache from the previous call. Without the time it greeted
        # with "Good morning" just after midnight.
        instructions += f"\n\nIt is {datetime.now():%A, %B %d, %Y, %I:%M %p}."
        super().__init__(instructions=instructions)
        self._humor_seen: set[str] = set()   # user messages whose hints were already applied
        self._held_seen: set[str] = set()
        self._guest: str | None = None   # someone introduced as in the room this call ("my friend Tina is here")
        self._voices = voices.Speakers()   # who is speaking, when speech-to-text labels speakers (voices.py)
        self._asked_speaker: str | None = None
        self._handoff = None  # the "open code chat" task (kept so it isn't garbage-collected)
        self._gaming: gaming.GamingMonitor | None = None  # set in entrypoint
        self._workers = WorkersVoice()  # session set in entrypoint
        self._last_query = ""  # last web search, for "verify that" / "double-check it"
        self._results = []
        self._cursor = 0
        self._news = False       # the last search was for news: read as a roundup
        self._read_urls: set[str] = set()  # stories already told this call (never repeated)
        self._breaking = False   # the last news search was for breaking news (say how recent)
        self._news_q = ("", "")  # last news (query, label), for "any other news?"
        self._story = None       # the story last talked about, for "where's that from?"
        self._followup_query = ""  # a question about one story, to search
        self._user_text = ""     # what the user said this turn (tools can't see it)
        self._recent_user: list[str] = []  # their last few earlier turns (a search borrows the topic from them)
        self._last_subject = ""            # what the last search was about ("hairless cats"): a follow-up borrows it
        self._verbatim = False   # the last tool output is ready to speak as-is
        self._say_first = ""     # spoken before the model's answer to a search
        self._force_web = False  # next web_search skips the free sources ("yes, search the web")
        self._offered_web = ""   # the query a Q and A answer offered to search the web for
        self._wait_rest = False  # this turn stopped mid-question ("What are its"): no reply until the rest comes
        self._brave_next = False  # the next web search goes to Brave (reviews, after an OpenStreetMap list)
        self._twitch_turns = 0   # Twitch was mentioned this recently (turns left)
        self._allowed = set()    # tools the gate allowed this turn
        self._looking = False    # a look is in progress (one at a time)
        self._last_look = ("", 0.0)  # what the last look said, and when
        self._last_pic = ("", 0.0)   # the last picture (base64 JPEG, memory only), and when

    def _wants_more(self, chat_ctx) -> bool:
        """'Yes' to 'Want to hear more?', or a bare 'more' / 'next'."""
        if not self._results or self._cursor >= len(self._results):
            return False
        last = chat_ctx.items[-1] if chat_ctx.items else None
        if not (isinstance(last, llm.ChatMessage) and last.role == "user"):
            return False
        text = (last.text_content or "").strip()
        if len(text.split()) > 6 or re.search(r"\babout\b", text, re.I):
            return False  # "tell me more about X" is a new question
        if _MORE_WORDS.search(text):
            return True
        msgs = [i for i in chat_ctx.items if isinstance(i, llm.ChatMessage) and i.role == "assistant"]
        last_said = (msgs[-1].text_content or "").lower() if msgs else ""
        asked = "more" in last_said or "want the headlines" in last_said  # after a news summary
        return bool(asked and _YES.search(text))

    # ---- decide in code which tools the model may use this turn ------------
    def _source_reply(self, chat_ctx) -> str:
        """For an answer that didn't come from a search: "my own general knowledge", with an offer to look the topic
        up, worded so a yes searches it (_offer_topic). A searched answer's sites are named further on."""
        topic = _earlier_question(chat_ctx)
        topic = re.sub(r"[?.!]+$", "", topic or "").strip()
        if topic:
            return (f"That came from my own general knowledge, not a search, so there's no source I can point to. "
                    f"Want me to look up {topic}?")
        return ("That came from my own general knowledge, not a search, so there's no source I can point to. "
                "Want me to look it up?")

    def _earlier_local(self, chat_ctx) -> str | None:
        """The where-to-buy / in-my-area search for her last real question before a yes ("Where could I buy one
        locally?" -> "Don't know. Would you like me to look it up?" -> "Yes, please": Nov 20, 8:20 call)."""
        for i in reversed(chat_ctx.items[:-1]):
            if isinstance(i, llm.ChatMessage) and i.role == "user":
                said = (i.text_content or "").strip()
                if said and not _YES_ONLY.match(said) and not _FILLER_ONLY.match(said):
                    return _local_query(said, self._recent_user, getattr(self, "_last_subject", ""))
        return None

    def _recent_pic(self) -> str:
        """The last picture if it's still fresh enough for follow-ups; else drop it."""
        pic, when = self._last_pic
        if pic and time.monotonic() - when < LOOK_MEMORY_S:
            return pic
        self._last_pic = ("", 0.0)
        return ""

    def _tools_for_turn(self, chat_ctx, tools):
        """The 3B model calls a tool whenever one is offered ("can you hear me?"
        became a web search for "assistant"), whatever the instructions say.
        So offer tools only when the user's words call for them. Fewer tools in
        the request also makes small-talk replies faster."""
        last = chat_ctx.items[-1] if chat_ctx.items else None
        self._wait_rest = False
        if not (isinstance(last, llm.ChatMessage) and last.role == "user"):
            # answering a tool result (or greeting): no tools. Offering tools changes
            # the start of Ollama's prompt, so it re-reads the whole call (up to 28 s)
            self._allowed = set()
            return []
        if self._user_text:
            self._recent_user = (self._recent_user + [self._user_text])[-4:]
        self._user_text = _as_othos(last.text_content or "")  # the chosen name reads as "Othos" here
        before = _unanswered(chat_ctx)
        if before and _dangling(before[-1]):
            if _whole_question(self._user_text):
                # "What was what was" + "Othos, what's the best soil mix for raised beds?": she started over, so the
                # cut-off start is dropped (Nov 20, 7:52 call: it was glued onto the search)
                log.info("cut-off start dropped; the next turn is a whole question: %r", before[-1])
            else:
                # "What are its" + "last two task results?": answered as one question
                self._user_text = _as_othos(before[-1]).strip().rstrip(",") + " " + self._user_text
        if _dangling(self._user_text):
            self._wait_rest = True   # still talking: no tools, no reply (llm_node)
            self._allowed = set()
            return []
        allowed = _tool_names_for(self._user_text, has_results=bool(self._results))
        if "web_search" not in allowed and _local_query(self._user_text, self._recent_user,
                                                         getattr(self, "_last_subject", "")):
            allowed = allowed | {"web_search"}   # "Where could I buy one locally?" after broadforks (Nov 20, 8:20 call)
        if self._recent_pic() and _LOOK_FOLLOWUP.search(self._user_text) \
                and not _ASKS_NOTES.search(self._user_text):
            allowed = {"look"}  # "Am I holding anything?" just after a look: ask that picture
        if self._wants_more(chat_ctx):
            allowed.add("more_results")
        self._allowed = allowed
        by_name = llm.ToolContext(tools).function_tools
        picked = [t for name, t in by_name.items() if name in allowed]
        log.debug("tools offered this turn: %s", sorted(allowed & set(by_name)) or "none")
        return picked

    async def tts_node(self, text, model_settings):
        """The voice, with pronunciations.json / personal.json respellings ("VRAM" -> "vee ram")."""
        # never-say names (held.py) swapped out first, whatever wrote the text: model, code, or session.say
        async for frame in Agent.default.tts_node(self, _respelled(holdon.scrub(text, self._held)), model_settings):
            yield frame

    async def transcription_node(self, text, model_settings):
        """What the call page shows: never-say names swapped out, the same as the voice (held.py)."""
        async for piece in holdon.scrub(text, self._held):
            yield piece

    async def _model_stream(self, chat_ctx, tools, model_settings):
        """The language model for this turn: the normal one, or in gaming mode
        (GPU-heavy game) the CPU-only gaming model. Chosen per turn, so a switch
        never lands in the middle of a reply."""
        g = self._gaming
        if g is None or not g.use_gaming_model or g.cpu_llm is None:
            if llm_server.enabled() and not await llm_server.wait_ready(wait=60):
                log.warning("llama-server isn't answering: this turn can't get a reply")
            async for chunk in Agent.default.llm_node(self, chat_ctx, tools, model_settings):
                yield chunk
            return
        log.info("gaming mode: this turn answered by %s", g.cfg.gaming_model)
        tool_choice = getattr(model_settings, "tool_choice", None)
        kwargs = {"tool_choice": tool_choice} if isinstance(tool_choice, (str, dict)) else {}
        async with g.cpu_llm.chat(chat_ctx=chat_ctx, tools=tools,
                                  conn_options=self.session.conn_options.llm_conn_options, **kwargs) as stream:
            async for chunk in stream:
                yield chunk

    def _prompt_extras(self) -> str:
        """What the user said about the jokes and what they asked Othos to hold on to, as instruction sentences."""
        return humor.prompt_line(self._humor) + holdon.prompt_line(self._held)

    async def _held_hints(self, msg_id: str, user_text: str) -> str:
        """"I'm permanently disabled", "my pronouns are she/they", "my name is now Jo", "my friend Sam said she...":
        kept for every later call (held.py). Returns the short line to say back when the whole turn was that
        statement, else ''. Caught by code, never by the model."""
        if msg_id in self._held_seen:
            return ""
        self._held_seen.add(msg_id)
        kinds = holdon.catch(user_text, self._held)
        if not _held_whole_turn(user_text):   # the line won't be said, so the one-time pronoun question isn't used up
            kinds = [k for k in kinds if k != "ask_pronouns"]
        if not kinds:
            return ""
        new, line = holdon.apply(self._held, kinds)
        if new != self._held:
            self._held = new
            holdon.save(new)
            self._instr_base = holdon.scrub_text(self._instr_base, new)   # a name just retired leaves the instructions
            try:
                await self.update_instructions(self._instr_base + self._prompt_extras()
                                               + f"\n\nIt is {datetime.now():%A, %B %d, %Y, %I:%M %p}.")
            except Exception as e:
                log.info("held: instructions not updated mid-call: %s", e)
            log.info("held: %s", kinds)   # the kind only, never the words
        return line

    async def _humor_hints(self, msg_id: str, user_text: str, prev_assistant: str) -> str:
        """Apply what the user said about the humor (once per message). Returns the short line to say back
        when the whole turn was the hint, else ''. The setting changes either way."""
        if msg_id in self._humor_seen:
            return ""
        self._humor_seen.add(msg_id)
        kinds = humor.catch(user_text, prev_assistant)
        if not kinds:
            return ""
        new, line = humor.apply(self._humor, kinds)
        if new != self._humor:
            self._humor = new
            humor.save(new)
            try:
                await self.update_instructions(self._instr_base + humor.prompt_line(new) + holdon.prompt_line(self._held)
                                               + f"\n\nIt is {datetime.now():%A, %B %d, %Y, %I:%M %p}.")
            except Exception as e:
                log.info("humor: instructions not updated mid-call: %s", e)
        humor.log_event(kinds, self._humor)
        log.info("humor hint %s -> level %d, puns %s, keep %s", kinds, self._humor["level"], self._humor["puns"], self._humor["keep"])
        return line if line and humor.whole_hint(user_text, kinds) else ""

    async def _self_check_reply(self, user_text: str) -> str:
        """The quiet self-check behind "How are you?": looks at this PC and the call, nothing spoken
        until the answer is ready, and words it from templates (selfcheck.py)."""
        workers = []
        for q in workers_voice.INSTALLED:
            try:
                workers.append({"name": q.name, "running": q.running(),
                                "working": q.read_status().get("state") == "working"})
            except Exception:
                pass
        facts = await self._check_facts(workers)
        reply = selfcheck.describe(facts, user_text)
        self._last_check = (facts, time.monotonic(), reply)
        log.info("self-check: %s -> %r", {k: v for k, v in facts.items() if k != "workers"}, reply)
        return reply

    async def _check_facts(self, workers: list | None = None) -> dict:
        g = self._gaming

        def read():
            return selfcheck.gather(
                _OLLAMA_ROOT, os.getenv("OLLAMA_MODEL", "llama3.2:3b"),
                health=llm_server.root() + "/health" if llm_server.enabled() else "",
                workers=workers or [], gaming=bool(g and g.mode.on), camera=bool(_camera_publication()),
                starting=llm_server.starting())
        return await asyncio.to_thread(read)

    def _asks_check_more(self, chat_ctx) -> bool:
        check = getattr(self, "_last_check", None)
        if not check or time.monotonic() - check[1] > SELF_CHECK_MEMORY_S:
            return False
        said = next((i.text_content or "" for i in reversed(chat_ctx.items[:-1])
                     if isinstance(i, llm.ChatMessage) and i.role == "assistant"), "")
        return selfcheck.asks_more(self._user_text, right_after=said.strip() == check[2].strip())

    async def _self_check_more(self) -> str:
        """ "Could you expand on that?" after the self-check: what it saw then and sees now, in plain words."""
        then = self._last_check[0]
        now = await self._check_facts()
        reply = selfcheck.explain(then, now)
        log.info("self-check explained: then %s, now %s -> %r", then.get("ollama"), now.get("ollama"), reply)
        return reply

    # ---- keep leaked tool-call text out of speech --------------------------
    async def llm_node(self, chat_ctx, tools, model_settings):
        """Hold back the first few characters of each reply. If they look like a
        tool call written as text, never speak them: run the tool for real when
        it names one of ours, otherwise drop the text."""
        last = chat_ctx.items[-1] if chat_ctx.items else None
        if isinstance(last, llm.FunctionCallOutput) and self._verbatim:
            # search results already worded by code: speak them, skip the model
            self._verbatim = False
            yield last.output
            return
        self._verbatim = False
        if isinstance(last, llm.FunctionCallOutput) and self._say_first:
            yield self._say_first
        self._say_first = ""
        tools = self._tools_for_turn(chat_ctx, tools)  # also reads this turn's words into _user_text
        if isinstance(last, llm.ChatMessage) and last.role == "user" and self._wait_rest:
            # "What are its": cut off mid-question; say nothing and answer it with the rest (_tools_for_turn joins them)
            log.info("unfinished turn: waiting for the rest")
            return
        if isinstance(last, llm.ChatMessage) and last.role == "user":
            _note_her_sky(self._user_text)  # "it's actually gray outside" beats the reading
        if isinstance(last, llm.ChatMessage) and last.role == "user" and _END_CALL.match(self._user_text.strip()):
            # "Othos, end the call": say goodbye, then hang up (code does it)
            log.info("end call asked for by voice: %r", self._user_text)
            self._handoff = asyncio.create_task(_end_call(self.session))
            yield "Okay, ending the call. Bye!"
            return
        if isinstance(last, llm.ChatMessage) and last.role == "user":
            # a guest in the room: known from the introduction, or from a second voice when speech-to-text labels
            # speakers (voices.py; none today, so this part waits for Deepgram's labels or a local voiceprint)
            spk = self._voices.take()
            if spk and self._voices.is_new_voice(spk):
                self._voices.note_guest_voice(spk)
                if not self._guest:
                    self._guest = "a guest"   # a name comes with an introduction
                    log.info("guest mode on (a second voice)")
            if guest := _guest_intro(self._user_text):
                self._guest = guest
                self._voices.expect_guest(guest)
                await self._held_hints(last.id, self._user_text)   # "my friend Tina ... She's" kept (held.py)
                log.info("guest mode on")   # never the guest's name
                yield f"Hi {guest}, nice to meet you. I'm {ASSISTANT}."
                return
            if self._guest and _guest_left(self._user_text, self._guest):
                log.info("guest mode off")
                self._guest = None
            pending = _unanswered(chat_ctx)
            last_reply = next((i.text_content or "" for i in reversed(chat_ctx.items[:-1])
                               if isinstance(i, llm.ChatMessage) and i.role == "assistant"), "")
            what = _guest_turn(self._user_text, self._guest, pending, last_reply, speaker=spk, asked=self._asked_speaker)
            if what != "silent":
                self._asked_speaker = spk   # whom Othos is answering: their reply to a question is the one that counts
                if who := self._voices.label_for_model(spk):
                    # the model is told who spoke ("(said by Tina) Othos, what's your name?"); the call keeps the words
                    chat_ctx = chat_ctx.copy()
                    chat_ctx.items[-1] = llm.ChatMessage(role="user", content=[f"(said by {who}) {self._user_text}"])
            if what == "silent":
                await self._held_hints(last.id, self._user_text)   # "Sam's pronouns are..." still kept
                log.info("guest mode: not spoken to, no reply")
                return
            if what == "answer_pending":
                # a side remark right after a question to Othos: answer the question, with the model and no tools
                log.info("guest mode: side remark after a question; answering the question")
                self._user_text = _as_othos(next(p for p in reversed(pending) if _addressed(p)))
                tools, self._allowed = [], set()
        if (isinstance(last, llm.ChatMessage) and last.role == "user"
                and (request := _code_chat_request(self._user_text)) is not None):
            # "Switch to code chat": code handles it; the page ends the call and opens /code
            log.info("code chat asked for by voice: %r", self._user_text)
            self._handoff = asyncio.create_task(_open_code_chat(request, self.session))
            yield "Opening code chat. See you there." if not request else "Opening code chat with your question."
            return
        if (isinstance(last, llm.ChatMessage) and last.role == "user"
                and (fs_reply := await asyncio.to_thread(self._workers.handle, self._user_text)) is not None):
            # Firestarter / the Smithy (the CPU workers), handled in code: workers_voice.py
            # ('' = say nothing: a step of a how-to being recorded, howto_recorder.py)
            log.info("workers by voice: %r", self._user_text)
            if fs_reply:
                yield fs_reply
            return
        if (isinstance(last, llm.ChatMessage) and last.role == "user" and self._gaming is not None
                and (cmd := _gaming_command(self._user_text))):
            # "Gaming mode on/off" or "is gaming mode on?": code does it and says so
            if cmd == "status":
                yield self._gaming.mode.describe()
                return
            log.info("gaming mode %s by voice", cmd)
            mode = await self._gaming.set_override(cmd)
            if cmd == "off":
                yield "Gaming mode off. I'm back on the normal model."
            elif self._gaming.failed_gaming_model:
                yield "Gaming mode on, but the gaming model didn't load, so I'm staying on the normal one."
            elif mode.voice_on_cpu:
                yield "Gaming mode on. I'll use the lighter model while you play."
            else:
                yield f"Gaming mode on. {mode.game or 'This game'} doesn't need the graphics card freed, so I'm keeping my normal model."
            return
        if (isinstance(last, llm.ChatMessage) and last.role == "user"
                and _WEATHER_NOW.search(self._user_text) and not _WEATHER_LATER.search(self._user_text)
                and not _PLACE_NAMED.search(self._user_text) and (wx := _weather_reply())):
            # "What's the weather like outside?": the user's town's weather from the call-start check
            # (before the code-written search below, which would search with no place)
            log.info("weather answered from the call-start check")
            yield wx
            return
        if isinstance(last, llm.ChatMessage) and last.role == "user" and self._news and self._results:
            # follow-ups on a news roundup, answered from the stories already found
            if (reply := await self._news_followup(self._user_text)) is not None:
                yield reply
                return
            if self._followup_query:  # a question about one story: search it, keep the roundup
                q, self._followup_query = self._followup_query, ""
                yield _lookup_lead(self._user_text) + " "
                yield await self._story_answer(q, self._user_text)
                return
        if (isinstance(last, llm.ChatMessage) and last.role == "user" and self._offered_web
                and _YES.match(self._user_text)):
            # "Yes" to "Want me to search the web?" after a Q and A answer: Tavily this time
            asked_web = next((i.text_content or "" for i in reversed(chat_ctx.items[:-1])
                              if isinstance(i, llm.ChatMessage) and i.role == "assistant"), "")
            if re.search(r"\b(?:search|look)\b[^.?!]*\b(?:web|online|internet)\b", asked_web, re.I):
                q, self._offered_web, self._force_web = self._offered_web, "", True
                log.info("web search after the Q and A answer: %r", q)
                yield _lookup_lead(q)
                yield llm.ChatChunk(
                    id="othos-search",
                    delta=llm.ChoiceDelta(role="assistant", tool_calls=[llm.FunctionToolCall(
                        name="web_search", arguments=json.dumps({"query": q, "count": 4}),
                        call_id=f"search_{uuid.uuid4().hex[:12]}",
                    )]),
                )
                return
        if (isinstance(last, llm.ChatMessage) and last.role == "user" and _YES_ONLY.match(self._user_text.strip())
                and _offered_lookup(chat_ctx)
                and (q := _offer_topic(chat_ctx) or self._earlier_local(chat_ctx) or _earlier_question(chat_ctx))):
            # "Please do." to the model's own "Can I look that up for you?": search the question it offered to look
            # up (Nov 19 call: two yeses never searched, then the model said "Let me check..." without searching)
            log.info("search after a yes to Othos's offer: %r", q)
            yield _lookup_lead(q)
            yield llm.ChatChunk(
                id="othos-search",
                delta=llm.ChoiceDelta(role="assistant", tool_calls=[llm.FunctionToolCall(
                    name="web_search", arguments=json.dumps({"query": q, "count": 4}),
                    call_id=f"search_{uuid.uuid4().hex[:12]}",
                )]),
            )
            return
        if (isinstance(last, llm.ChatMessage) and last.role == "user"
                and _ASKED_WHAT_TO_SEARCH.search(_last_said(chat_ctx))
                and not _YES_ONLY.match(self._user_text.strip()) and not _NOTHING_TO_SEARCH.match(self._user_text)
                and (q := _local_query(self._user_text, self._recent_user, getattr(self, "_last_subject", ""))
                     or _query_from(self._user_text, chat_ctx) or _bare_topic(self._user_text))):
            # "What would you like me to search for?" -> "Broadforks.": search it (Nov 20, 8:47 call: "I don't know
            # what broadforks are")
            log.info("search for what she named after Othos asked: %r", q)
            yield _lookup_lead(q)
            yield llm.ChatChunk(
                id="othos-search",
                delta=llm.ChoiceDelta(role="assistant", tool_calls=[llm.FunctionToolCall(
                    name="web_search", arguments=json.dumps({"query": q, "count": 4}),
                    call_id=f"search_{uuid.uuid4().hex[:12]}",
                )]),
            )
            return
        if (isinstance(last, llm.ChatMessage) and last.role == "user" and not _last_reply_searched(chat_ctx)
                and (_ASKS_SOURCE.search(self._user_text) or _ASKS_WHERE_FROM.search(self._user_text))):
            # "Can you cite your source on that?": said by code, honestly (Nov 20, 8:47 call: "I don't know. Want me to
            # look it up?", and the yes then searched nothing)
            reply = self._source_reply(chat_ctx)
            log.info("source asked: %r", reply)
            yield reply
            return
        if isinstance(last, llm.ChatMessage) and last.role == "user":
            self._offered_web = "" if not _YES.match(self._user_text) else self._offered_web
        if (isinstance(last, llm.ChatMessage) and last.role == "user" and self._results and not self._news
                and (_ASKS_SOURCE.search(self._user_text) or _ASKS_WHERE_FROM.search(self._user_text))
                and not _SEARCH_WORDS.search(self._user_text)):
            # "Can you reference the source?" after a search answer: the real sites, said by
            # code (the model made up "The Spruce, Gardeners.com" on the Nov 10 call)
            sites = list(dict.fromkeys(_source_name(r.get("url", "")) for r in self._results[:4] if r.get("url")))
            if sites:
                listed = sites[0] if len(sites) == 1 else ", ".join(sites[:-1]) + f" and {sites[-1]}"
                log.info("sources of the last answer: %s", listed)
                yield f"That came from {listed}."
                return
        if isinstance(last, llm.ChatMessage) and last.role == "user" and self._wants_more(chat_ctx):
            # "Yes." to "Want to hear more?": the model didn't call more_results
            # for that, so call it directly
            n = _count_from(self._user_text) or 4
            yield llm.ChatChunk(
                id="othos-more",
                delta=llm.ChoiceDelta(role="assistant", tool_calls=[llm.FunctionToolCall(
                    name="more_results", arguments=json.dumps({"count": n}),
                    call_id=f"more_{uuid.uuid4().hex[:12]}",
                )]),
            )
            return
        if isinstance(last, llm.ChatMessage) and last.role == "user" and self._allowed == {"look"}:
            # "What am I holding?" with the camera on: code starts the look (the
            # language model can't see, so it has nothing to add)
            log.info("look started by code: %r", self._user_text)
            yield "Let me look."  # a new picture takes ~20 s on this laptop
            yield llm.ChatChunk(
                id="othos-look",
                delta=llm.ChoiceDelta(role="assistant", tool_calls=[llm.FunctionToolCall(
                    name="look", arguments=json.dumps({"question": self._user_text}),
                    call_id=f"look_{uuid.uuid4().hex[:12]}",
                )]),
            )
            return
        if (isinstance(last, llm.ChatMessage) and last.role == "user" and "look" not in self._allowed
                and _LOOK_CHALLENGE.search(self._user_text)
                and time.monotonic() - self._last_look[1] < LOOK_MEMORY_S):
            # "There's no red cup. Where did you see that?" right after a look: own
            # the mistake instead of letting the model deny saying it
            log.info("look questioned; told the user it was the vision model's mistake")
            yield ("Sorry, that's my mistake. It came from my camera picture: the vision model "
                   "sometimes describes things that aren't there. Ask me to look again if you like.")
            return
        if (isinstance(last, llm.ChatMessage) and last.role == "user" and "look" not in self._allowed
                and _asks_look(self._user_text) and not _ASKS_NOTES.search(self._user_text)
                and not _SEARCH_WORDS.search(self._user_text)):
            # asked to look with the camera off
            log.info("look asked with the camera off")
            yield "I can't see anything right now. Turn on the camera with the camera button, then ask me again."
            return
        if isinstance(last, llm.ChatMessage) and last.role == "user":
            # Twitch stays the subject for a few turns ("a user whose name is X" after a Twitch question)
            self._twitch_turns = 3 if re.search(r"\btwitch\b", self._user_text, re.I) else max(0, self._twitch_turns - 1)
        if (isinstance(last, llm.ChatMessage) and last.role == "user"
                and (channel := sources.twitch_channel_asked(self._user_text, self._twitch_turns > 0))):
            # "How many videos does Justice Brewing have on Twitch?", "Search Twitch for users
            # named Griffin": the Twitch API (free), worded by code (Nov 10 call went to Tavily)
            log.info("twitch channel lookup by code: %r", channel)
            yield "Let me check Twitch. "
            reply = await self._twitch_channel_reply(channel, self._user_text)
            yield reply[:1].upper() + reply[1:]  # "shroud isn't live" starts a sentence
            return
        if isinstance(last, llm.ChatMessage) and last.role == "user" and self._asks_check_more(chat_ctx):
            # "You mentioned you couldn't reach your local model. Could you expand on that?": from the check's readings,
            # before any search (Nov 20, 8:20 call: the model made up "trouble accessing the local data center")
            yield await self._self_check_more()
            return
        if (isinstance(last, llm.ChatMessage) and last.role == "user" and self._allowed == {"web_search"}
                and (q := (_news_query(self._user_text) or [None])[0]
                     # "Please verify that" / "double-check it": search the last topic again, deeper
                     or (self._last_query if _RESEARCH_ONLY.search(self._user_text) else None)
                     or _local_query(self._user_text, self._recent_user, getattr(self, "_last_subject", ""))
                     or _query_from(self._user_text, chat_ctx))):
            # Search with the user's own words, so the model doesn't spend 5-11 s
            # writing the call. Falls back to the model for follow-ups and anything personal.
            log.info("search query written by code: %r", q)
            n = _count_from(self._user_text) or 4
            yield _lookup_lead(self._user_text)  # spoken while Tavily searches, instead of silence
            yield llm.ChatChunk(
                id="othos-search",
                delta=llm.ChoiceDelta(role="assistant", tool_calls=[llm.FunctionToolCall(
                    name="web_search", arguments=json.dumps({"query": q, "count": n}),
                    call_id=f"search_{uuid.uuid4().hex[:12]}",
                )]),
            )
            return
        if (isinstance(last, llm.ChatMessage) and last.role == "user" and "search_notes" in self._allowed
                and _notes_question(self._user_text)):
            # "What did I say about X?": search notes with the user's own words.
            # The model spent ~12.5 s writing this call; search_notes drops the
            # filler words itself. Saving ("remember that...") stays with the model.
            q = _ADDRESS.sub("", self._user_text).strip() or self._user_text
            log.info("notes search started by code: %r", q)
            yield llm.ChatChunk(
                id="othos-notes",
                delta=llm.ChoiceDelta(role="assistant", tool_calls=[llm.FunctionToolCall(
                    name="search_notes", arguments=json.dumps({"query": q, "limit": 5}),
                    call_id=f"notes_{uuid.uuid4().hex[:12]}",
                )]),
            )
            return
        if (isinstance(last, llm.ChatMessage) and last.role == "user" and google_tools.asks_calendar(self._user_text)
                and not _SEARCH_WORDS.search(self._user_text)):
            # "What's on my calendar tomorrow?" / "Am I free Thursday afternoon?": Google Calendar, read-only, worded by code
            try:
                reply = await asyncio.wait_for(asyncio.to_thread(google_tools.calendar_answer, self._user_text), timeout=12)
            except Exception as e:
                log.info("calendar failed: %s", type(e).__name__)
                reply = "I couldn't reach your Google calendar just now. Try again in a minute."
            log.info("calendar answered by code (%d chars)", len(reply))   # never the events themselves
            self._verbatim = True
            yield reply
            return
        if (isinstance(last, llm.ChatMessage) and last.role == "user" and not _PLACE_NAMED.search(self._user_text)
                and not _SEARCH_WORDS.search(self._user_text)
                and (_ASKS_QUAKE.search(self._user_text) or _ASKS_AIR.search(self._user_text))):
            # "Any earthquakes?" / "How's the air? Any wildfire smoke?": USGS, AirNow and NOAA, worded by code
            replies = []
            if _ASKS_QUAKE.search(self._user_text) and (r := await _quake_reply(self._user_text)):
                replies.append(r)
            if _ASKS_AIR.search(self._user_text) and (r := await _air_reply(self._user_text)):
                replies.append(r)
            if replies:
                log.info("quake/air answered by code: %r", " ".join(replies)[:300])
                yield " ".join(replies)
                return
        if (isinstance(last, llm.ChatMessage) and last.role == "user"
                and _WHERE_LIVE.search(self._user_text) and not _SEARCH_WORDS.search(self._user_text)):
            # "Where do I live?" / "Where am I?": from the user's home town, never the web
            if not HOME_LOCATION:
                yield f"I don't know where you live yet. You can set your town in {ASSISTANT}'s settings."
            elif re.search(r"\bwhere (?:am i|are we)\b", self._user_text, re.I):
                yield f"As far as I know, you're home in {_home_spoken()}."
            else:
                yield f"You live in {_home_spoken()}."
            log.info("home location answered by code")
            return
        if (isinstance(last, llm.ChatMessage) and last.role == "user"
                and (filler := _filler_turn(self._user_text, pending := _unanswered(chat_ctx)))):
            if filler == "silent":
                # "Also," on its own: the user is still talking; say nothing and wait for the rest
                log.info("filler-only turn %r: no reply", self._user_text)
                return
            # "Oh," right after an unanswered turn cut that reply off: answer that turn instead (Nov 18, Tina)
            log.info("filler-only turn %r after an unanswered turn: answering that one", self._user_text)
            self._user_text = _as_othos(next(p for p in reversed(pending) if not _FILLER_ONLY.match(p.strip())))
            tools, self._allowed = [], set()
        if isinstance(last, llm.ChatMessage) and last.role == "user":
            # "Go easy on the puns", "I like the humor, keep it": caught by code, kept for next time (humor.py)
            prev = next((i.text_content or "" for i in reversed(chat_ctx.items[:-1])
                         if isinstance(i, llm.ChatMessage) and i.role == "assistant"), "")
            hint_reply = await self._humor_hints(last.id, self._user_text, prev)
            held_reply = await self._held_hints(last.id, self._user_text)   # said only when that was the whole turn
            if held_reply and not self._allowed and _held_whole_turn(self._user_text):
                yield held_reply
                return
            if hint_reply and not self._allowed:
                yield hint_reply
                return
        if (isinstance(last, llm.ChatMessage) and last.role == "user" and not self._allowed
                and selfcheck.asks_back(self._user_text)):
            # "I'm fine, how are you?": a quiet self-check, answered by code (no model)
            yield await self._self_check_reply(self._user_text)
            return
        asked = next((i.text_content or "" for i in reversed(chat_ctx.items[:-1])
                      if isinstance(i, llm.ChatMessage) and i.role == "assistant"), "")
        if isinstance(last, llm.ChatMessage) and last.role == "user" and not self._allowed and (
                reply := _quick_reply(self._user_text, _user_name(), asked)):
            # plain "thank you" / "bye": no need to wait ~10 s for the model
            log.info("quick reply: %r", reply)
            yield reply
            return
        name = _user_name()
        namef = _NameFilter(name) if name and not _name_allowed(chat_ctx) else None
        greeting =not any(isinstance(i, llm.ChatMessage) and i.role in ("user", "assistant")
                           for i in chat_ctx.items)
        # hold a greeting's opening words until "Good afternoon" etc. has fully arrived
        check_chars = 40 if greeting else _LEAK_CHECK_CHARS

        # day/time talk: fixed in greetings; taken out of plain chat the user didn't ask that of
        # (not from answers built on notes, search or camera results)
        timef = None
        if greeting:
            timef = _TimeFilter(greeting=True)
        elif (isinstance(last, llm.ChatMessage) and last.role == "user"
              and not _ASKS_TIME.search(self._user_text)):
            timef = _TimeFilter(greeting=False)

        # a reply to her own words, not to a search result: "I found that..." can't be true (no search has run)
        claimf = _ClaimFilter() if isinstance(last, llm.ChatMessage) and last.role == "user" else None

        def speak(text: str) -> str:
            if greeting:
                text = _fix_greeting(text)
            text = namef.feed(text) if namef else text
            text = claimf.feed(text) if claimf and text else text
            return timef.feed(text) if timef and text else text

        def speak_rest() -> str:
            rest = namef.flush() if namef else ""
            if claimf:
                rest = (claimf.feed(rest) if rest else "") + claimf.flush()
            rest = timef.feed(rest) if timef and rest else rest
            return rest + (timef.flush() if timef else "")

        held = ""
        mode = None  # None = undecided, "speech" = normal reply, "leak" = tool-call text
        chunk_id = "othos"
        called = False  # the model made a real tool call this turn
        claimed = False  # the model began "Let me check..." on a turn with no search
        stream = self._model_stream(chat_ctx, tools, model_settings)
        if isinstance(last, llm.ChatMessage) and last.role == "user" and "web_search" in self._allowed:
            # a turn that may search: the model reads the search tool's instructions first (~10 s on this laptop), so
            # say something if it's still silent after a few seconds (Nov 20, 7:52 call: 15-19 s of silence)
            stream = _say_if_silent(stream, SEARCH_SILENCE_S)
        async for chunk in stream:
            if chunk is _SILENT:
                yield "One moment. "
                # the voice holds a last sentence until the next one starts: send it now, or it waits for the whole
                # reply (Nov 20, 8:20 call: said at 3 s, heard at 47 s)
                yield FlushSentinel()
                continue
            if isinstance(chunk, llm.ChatChunk):
                chunk_id = chunk.id
                text = chunk.delta.content if chunk.delta else None
            else:
                text = chunk if isinstance(chunk, str) else None

            if not text:
                if isinstance(chunk, llm.ChatChunk) and chunk.delta and chunk.delta.tool_calls:
                    called = True
                yield chunk  # real tool calls, usage, markers
                continue
            if mode == "speech":
                if out := speak(text):
                    yield out
                continue

            held += text
            if mode is None:
                held = _ROLE_PREFIX.sub("", held)
                if len(held.lstrip()) >= check_chars and _TOOL_WORD.match(held) and not _LEAK_START.match(held):
                    log.warning("dropped a tool name the model said as a word: %r", held[:60])
                    held = _TOOL_WORD.sub("", held)
                if not self._allowed and not called and (claim := _claim_state(held)) != "no":
                    # "Let me check..." with no search to run: hold until it's clear, then search for real
                    if claim == "yes":
                        claimed = True
                        break
                    continue
                if len(held.lstrip()) >= check_chars:
                    mode = "leak" if _LEAK_START.match(held) else "speech"
                    if mode == "speech":
                        if out := speak(held):
                            yield out
                        held = ""

        if mode is None and not claimed and not self._allowed and not called and _claim_state(held) == "yes":
            claimed = True   # a short reply that was only "Let me check."
        if claimed:
            # the model said it was searching, but no search was offered or run (Nov 19 call: "Let me check... Ah, it
            # looks like I don't have the latest information"): run the search it promised, or say none was run
            q = _searchable(self._user_text, chat_ctx) if not _YES_ONLY.match(self._user_text.strip()) else None
            q = q or (_offer_topic(chat_ctx) or self._earlier_local(chat_ctx)
                      if _YES_ONLY.match(self._user_text.strip()) else None) \
                or _earlier_question(chat_ctx)
            if not q:
                log.info("model claimed a lookup with nothing to search; said so instead")
                yield "I haven't looked anything up yet. What would you like me to search for?"
                return
            log.info("model claimed a lookup; searching for real: %r", q)
            yield _lookup_lead(q)
            yield llm.ChatChunk(
                id="othos-search",
                delta=llm.ChoiceDelta(role="assistant", tool_calls=[llm.FunctionToolCall(
                    name="web_search", arguments=json.dumps({"query": q, "count": 4}),
                    call_id=f"search_{uuid.uuid4().hex[:12]}",
                )]),
            )
            return
        if mode is None:  # very short reply: decide now
            held = _ROLE_PREFIX.sub("", held)
            if _TOOL_WORD.match(held) and not _LEAK_START.match(held):
                log.warning("dropped a tool name the model said as a word: %r", held[:60])
                held = _TOOL_WORD.sub("", held)
            if _ROLE_ONLY.match(held):  # the model said only "assistant" and stopped
                log.warning("model replied with only its role label")
                held = ""
            if not held.strip():
                if not called and not any(
                        isinstance(i, llm.ChatMessage) and i.role == "assistant" for i in chat_ctx.items):
                    yield "Hi! How can I help?"  # greeting came out empty: don't start in silence
                return
            mode = "leak" if _LEAK_START.match(held) else "speech"
            if mode == "speech":
                if out := speak(held):
                    yield out
        if mode != "leak":
            if rest := speak_rest():
                yield rest
            return

        names = set(llm.ToolContext(tools).function_tools)
        call = _parse_leaked_call(held, names)
        if call is None:
            log.warning("dropped tool-call text the model tried to speak: %r", held[:200])
            yield "Sorry, I got tangled up there. Could you say that again?"
            return
        log.warning("model wrote a %s call as text; running it as a real tool call", call[0])
        yield llm.ChatChunk(
            id=chunk_id,
            delta=llm.ChoiceDelta(
                role="assistant",
                tool_calls=[llm.FunctionToolCall(
                    name=call[0], arguments=call[1], call_id=f"leak_{uuid.uuid4().hex[:12]}",
                )],
            ),
        )

    # ---- web search -------------------------------------------------------
    def _read_page(self, start: int, count: int, intro: str) -> str:
        """Results worded for speech by code: 'N. site: what it is.'"""
        items = self._results[start:start + (count if self._news else min(count, READOUT_COUNT))]
        self._cursor = start + len(items)
        if self._news:
            # a roundup, like someone catching you up; sources are on request ("where's that from?")
            self._read_urls |= {r.get("url", "") for r in items}
            lines = []
            for i, r in enumerate(items):
                line = _news_line(r)
                ago = _ago(r) if self._breaking else ""
                join = _ROUNDUP_JOIN[min(i, len(_ROUNDUP_JOIN) - 1)] if start == 0 else _ROUNDUP_JOIN[1 + i % 4]
                if ago:  # breaking news: "An hour ago: Ohio State is investigating..."
                    text = f"{join}{ago if join else ago[0].upper() + ago[1:]}: {line}"
                else:    # headlines keep their own capitals ("Meanwhile, Trump to meet...")
                    text = f"{join}{line}"
                lines.append(text.rstrip(".") + ".")
            more = self._cursor < len(self._results)
            tail = ("Want more on any of these, or where they came from? I can also keep going."
                    if more else "Want more on any of these, or where they came from?")
            text = "\n".join([intro, *lines, tail])
            self._verbatim = True
            log.info("news roundup: %s", text.replace("\n", " "))
            return text
        else:
            words = _ORDINALS if start == 0 else _ORDINALS[1:]  # a "more" page starts at "Next"
            lines = [f"{words[min(i, len(words) - 1)]}, from {_site(r.get('url', ''))}: {_spoken_headline(r)}."
                     for i, r in enumerate(items)]
        more = self._cursor < len(self._results)
        tail = "Want to hear more?" if more else "That's everything I found."
        text = "\n".join([intro, *lines, tail])
        self._verbatim = True
        log.info("search readout: %s", text.replace("\n", " "))
        return text

    def _answer_page(self, count: int) -> str:
        """Cleaned results for the model to answer a question from. Kept short: the
        model re-reads them on later turns (5 x 300 chars cost ~14 s, 3 x 200 ~7 s)."""
        items = self._results[:count]
        self._cursor = len(items)
        return "\n".join(
            # site last: put first, the model took "emeraldpalate.com" for a restaurant
            f"{i}. {_headline(r)}: {_clean_text(r.get('content', ''))[:200]} (from {_site(r.get('url', ''))})"
            for i, r in enumerate(items, 1)
        )

    @function_tool()
    async def web_search(self, context: RunContext, query: str, count: int = 4) -> str:
        """Search the web. Use for what something is or does, news, prices.
        Never for the user's own plans, appointments, or chit-chat.

        Args:
            query: Short search query, no personal details.
            count: Results to read, default 4.
        """
        user = self._user_text
        asked_n = _count_from(user)
        count = max(1, min(asked_n or _to_int(count, 4), MAX_PAGE))
        clean, dates = _dated_query(_clean_query(query, user))  # "yesterday's game" -> "November 8, 2026 game"
        topic = ""
        local_ask = bool(user) and searchctx.wants_local(user, clean)   # "near me", "in my area", "local"
        if user and not _WEATHER_TOPIC.search(clean):
            # "What are any good recommendations?" after "I'm looking for a butcher in my area": keep the topic,
            # and "near me" / "local" becomes the user's city and state (searchctx.py)
            clean, topic = searchctx.resolve(clean, user, self._recent_user, HOME_LOCATION,
                                              getattr(self, "_last_subject", ""))
        if (HOME_LOCATION and _WEATHER_TOPIC.search(clean) and not _PLACE_NAMED.search(user or "")
                and not _PLACE_NAMED.search(clean) and not sources.topic_of(f"{user} {clean}")):
            # (not "keep the solar battery warm": that's off-grid, not weather)
            clean = f"{clean} in {HOME_LOCATION}"  # "will it rain tomorrow?" -> in the user's town
        log.info("web_search %r (model wrote %r), count %d", clean, query, count)
        news = _news_query(user) if user else None
        if news:
            return await self._news_search(*news, count=min(count, 4) if not asked_n else count,
                                           summarize=bool(_SUMMARIZE.search(user)))
        self._news = False
        as_list = not user or _wants_list(user)
        self._last_query = clean  # for "verify that" / "double-check it"
        # recent events (games, scores, elections, "yesterday"...): Tavily's news search,
        # limited to those dates, searched more deeply; "verify" also searches deeply
        recent = bool(dates or _RECENT.search(user or "") or _RECENT.search(clean))
        deep = recent or bool(_RESEARCH.search(user or ""))
        # free sources first (the user, Nov 10: save Tavily credits); "search the web" skips them
        force_web, self._force_web = self._force_web or bool(_SEARCH_ONLINE.search(user or "")), False
        if not force_web:
            try:
                free = await asyncio.wait_for(self._free_answer(clean, user or clean, as_list, count, recent),
                                              timeout=12)
            except Exception as e:
                log.info("free sources failed: %s", e)
                free = None
            if free is not None:
                return free
        # the user's own library (library.py): reference pages kept on this PC, found only when every keyword matches
        if (not force_web and not local_ask and not recent and not _WEATHER_TOPIC.search(clean)
                and not _PRICE_ASK.search(user or clean) and library.available()):
            lib = await self._library_answer(clean, user or clean)
            if lib is not None:
                return lib
        # OpenStreetMap (sources.py): "Where's a butcher near me?" lists the nearest named places around the user's town,
        # by code. It has no reviews, so it offers a web search for those (Brave).
        brave_next, self._brave_next = self._brave_next, False
        if (local_ask or topic) and not force_web and not _PLACE_NAMED.search(user or ""):
            if kind := sources.osm_kind(topic or clean):
                try:
                    places = await asyncio.wait_for(sources.osm_places(kind), timeout=25)
                except Exception as e:
                    log.info("OpenStreetMap failed: %s", e)
                    places = []
                if places:
                    return self._osm_reply(kind, places)
        # Brave (sources.py) first for lists and local searches (and when Tavily is out or has failed); Tavily writes the
        # answers to questions. OTHOS_BRAVE=all sends every web search to Brave first.
        brave = []
        engine_used = "Tavily"
        if (sources.brave_ready() and not _PRICE_ASK.search(user or clean)
                and (as_list or local_ask or brave_next or BRAVE_ALL or _tavily_out())):
            try:
                brave = await asyncio.wait_for(sources.brave_search(clean, FETCH_N, "pw" if recent else ""), timeout=8)
            except Exception as e:
                log.info("Brave search failed: %s", e)
        if brave:
            result, engine_used = {"results": brave, "answer": None}, "Brave"
            as_list = as_list or local_ask   # "butchers near me": a list of places, read out by code
            log.info("search with Brave: %r (%d results)", clean, len(brave))
        else:
            if _tavily_out():
                self._verbatim = True
                log.info("Tavily credits used up; not searching %r", clean)
                return ("I've used up this month's web searches, and my free sources didn't have that. "
                        "Ask me something else, or try again next month.")
            if _PRICE_ASK.search(user or clean):
                return await self._price_answer(clean)
            try:
                global _tavily
                if _tavily is None:  # creating it blocks for ~0.7 s, so only once per process
                    _tavily = AsyncTavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
                # for a question, Tavily also writes an answer from the pages it found
                # (~1 s), which saves the model 10-15 s of reading the results.
                # "advanced" names its sources and says when they disagree.
                extra_opts = {} if as_list else {"include_answer": "advanced"}
                if deep:
                    extra_opts["search_depth"] = "advanced"
                if recent:
                    news_opts = {"topic": "news"}
                    if dates:
                        news_opts["start_date"] = (min(dates) - timedelta(days=1)).isoformat()
                        news_opts["end_date"] = (max(dates) + timedelta(days=1)).isoformat()
                    else:
                        news_opts["time_range"] = "week"
                    log.info("news-style search: %s", news_opts)
                    result = await asyncio.wait_for(
                        _tavily_search(query=clean, max_results=FETCH_N, **extra_opts, **news_opts), timeout=10)
                    if not result.get("results"):  # nothing in the news index: try the whole web
                        log.info("news search found nothing; searching the whole web")
                        result = await asyncio.wait_for(
                            _tavily_search(query=clean, max_results=FETCH_N, **extra_opts), timeout=10)
                else:
                    result = await asyncio.wait_for(
                        _tavily_search(query=clean, max_results=FETCH_N, **extra_opts), timeout=8
                    )
            except Exception as e:
                rs = []
                if sources.brave_ready():   # Tavily failed: Brave, if it's set up
                    try:
                        rs = await asyncio.wait_for(sources.brave_search(clean, FETCH_N, "pw" if recent else ""), timeout=8)
                    except Exception:
                        rs = []
                if not rs:
                    return f"Search failed: {e}"
                result, engine_used = {"results": rs, "answer": None}, "Brave"
        self._results = result.get("results", [])
        self._cursor = 0
        if self._results and _WHERE_TO_BUY.search(clean):
            self._results = _rank_for_buying(self._results, HOME_LOCATION)
            log.info("where-to-buy order: %s", [_site(r.get("url", "")) for r in self._results[:4]])
        if not self._results:
            return "No results found."
        if len(clean.split()) <= 6 and not searchctx.is_generic(clean) and not _WEATHER_TOPIC.search(clean):
            self._last_subject = topic or clean     # "hairless cats": the next "options for breeders" is about them
        if topic and not searchctx.relevant(topic, self._results, result.get("answer")):
            # nothing that came back mentions what was asked for (TV shows for a butcher): don't read it out
            log.info("search for %r came back off-topic (%d results); not read out", topic, len(self._results))
            self._results, self._cursor, self._verbatim = [], 0, True
            return searchctx.no_match_reply(topic, HOME_LOCATION)
        weather = bool(_WEATHER_TOPIC.search(clean))
        if weather:  # Fahrenheit only: the model said "13 degrees" from "(about 13 °C)"
            for r in self._results:
                r["content"] = _fahrenheit_only(r.get("content", ""))
        engine = _engine_named(user)
        note = f"I search with {engine_used}, not {engine}. " if engine else ""
        if as_list:
            return self._read_page(0, count, note + "Here's what I found.")
        spoken = _short_answer(_fahrenheit_only(result.get("answer")) if weather else result.get("answer"))
        if spoken and topic and not searchctx.answer_fits(topic, spoken):
            log.info("search answer doesn't mention %r; not spoken: %s", topic, spoken[:120])
            spoken = ""      # (Nov 20: hairless cat breeders, answered with the Kennel Club's advice for dogs)
        if spoken and _ABOUT_A_PAGE.search(spoken):
            # "The speaker says they don't use a broadfork...": a summary of one video, not an answer (Nov 20, 8:47 call)
            log.info("search answer describes a page or video; not spoken: %s", spoken[:120])
            spoken = ""
        if spoken and _backed_by(spoken, self._results):
            self._cursor = len(self._results)  # nothing left to page through
            self._verbatim = True
            log.info("search answer (Tavily): %s", spoken)
            return note + spoken
        if engine:
            self._say_first = note
        extra = f" Name up to {asked_n}." if asked_n else ""
        if weather:
            extra += " Give temperatures in Fahrenheit."
        return self._answer_page(max(3, min(asked_n or 3, 5))) + _ANSWER_FROM_RESULTS + extra

    async def _library_answer(self, clean: str, user: str) -> str | None:
        """Passages from the user's own library (library\\), for the model to answer from; None if nothing fits.
        Needs two or more keywords and every one of them in the page, so a stray word doesn't pull in a page."""
        if len(library.keywords(clean)) < 2:
            return None
        try:
            hits = await asyncio.wait_for(asyncio.to_thread(library.search, clean, "", 3, True), timeout=4)
        except Exception as e:
            log.info("library search failed: %s", e)
            return None
        if not hits:
            return None
        log.info("library answer for %r from %s", clean, [h["path"].rsplit("library", 1)[-1] for h in hits])
        self._results = [{"title": h["title"], "url": h["url"] if h["url"].startswith("http") else "",
                          "content": h["text"], "outlet": "your library"} for h in hits]
        self._cursor = len(self._results)
        lines = []
        for i, h in enumerate(hits, 1):
            when = f", saved {h['fetched']}" if h.get("fetched") else ""
            lines.append(f"{i}. {h['title']}: {_clean_text(h['text'])[:320]} (from your library, {h['source'].split('/')[0]}{when})")
        return "\n".join(lines) + _ANSWER_FROM_LIBRARY

    def _osm_reply(self, kind, places: list[dict]) -> str:
        """The places OpenStreetMap lists near the user's town, worded by code. It has no reviews: says so."""
        one, many, _tags = kind
        us = bool(re.search(r"\b(?:US|USA)\s*$", HOME_LOCATION or "")) or not HOME_LOCATION
        place = searchctx.city_state(HOME_LOCATION).split(",")[0] or "you"

        def far(km: float) -> str:
            d = km * 0.621371 if us else km
            unit = "mile" if us else "kilometer"
            if d < 0.15:
                return "right nearby"
            d = round(d, 1)
            return f"about {d:g} {unit}{'' if d == 1 else 's'} away"

        def one_line(p: dict) -> str:
            return f"{p['name']}, {far(p['km'])}" + (f", at {p['street']}" if p.get("street") else "")

        top = places[:4]
        parts = [f"OpenStreetMap lists {len(places)} {many if len(places) != 1 else one} near {place}."]
        parts.append(f"The closest is {one_line(top[0])}.")
        if len(top) > 1:
            rest = [one_line(p) for p in top[1:]]
            parts.append("Next: " + "; ".join(rest) + ".")
        parts.append("It doesn't have reviews, so I can't say which is best. Want me to search the web for reviews?")
        self._results = [{"title": p["name"], "url": p["website"] or f"https://www.openstreetmap.org/{p['id']}",
                          "content": ", ".join(x for x in (p["street"], p["city"], p["phone"], p["hours"]) if x)}
                         for p in places]
        self._cursor, self._news, self._verbatim = len(top), False, True
        loc = searchctx.city_state(HOME_LOCATION)
        self._offered_web = f"best {one} reviews" + (f" near {loc}" if loc else "")
        self._brave_next = True
        log.info("OpenStreetMap: %d %s near the town, nearest %s", len(places), many, places[0]["name"])
        return " ".join(parts)

    async def _free_answer(self, q: str, user: str, as_list: bool, count: int, recent: bool) -> str | None:
        """An answer from the free sources (sources.py), or None to search with Tavily.
        Code words the reply for weather, Twitch, videos, patterns and Joomla releases;
        Q and A answers go to the model, framed as what people there suggest."""
        text = f"{user} {q}"
        # weather words only when it's really about the weather ("keep the battery warm" isn't)
        if (HOME_LOCATION and sources.topic_of(text) is None and not _PLACE_NAMED.search(user)
                and (_ASKS_WEATHER.search(user) or re.search(r"\b(?:weather|forecast|rain\w*|snow\w*|"
                                                             r"temperatures?|wind\w*)\b", user, re.I))
                and not _PLACE_NAMED.search(re.sub(r"\s+in " + re.escape(HOME_LOCATION) + r"$", "", q))):
            if said := await _forecast_reply(user):  # "Will it rain tomorrow?": NWS for the user's town
                self._results, self._cursor, self._verbatim = [{"url": "https://www.weather.gov/"}], 1, True
                return said
            return None
        if sources.BOOKS_ASK.search(user):  # "What's on the bestseller list?": NYT Books
            try:
                name, books = await asyncio.wait_for(sources.nyt_books(user), timeout=12)
            except Exception:
                name, books = "", []
            if books:
                self._results, self._cursor, self._news, self._verbatim = (
                    [{"url": "https://www.nytimes.com/books/best-sellers/"}], 1, False, True)
                parts = [f"{b['title']} by {b['author']}" + (", its first week" if b["weeks"] == 1 else
                                                              f", {b['weeks']} weeks on the list" if b["weeks"] > 8 else "")
                         for b in books[:5]]
                listed = "; ".join(parts[:-1]) + f"; and {parts[-1]}" if len(parts) > 1 else parts[0]
                return f"Top of the New York Times {name} list this week: {listed}."
        if game := sources.twitch_game_asked(user):  # "How many people are streaming Minecraft?"
            if t := await sources.twitch_category(game):
                self._results, self._cursor, self._news, self._verbatim = [{"url": "https://www.twitch.tv/"}], 1, False, True
                return _twitch_line(t)
        if sources.wants_videos(user):  # "Any good OBS tutorials?"
            vids = await sources.youtube(q, n=6)
            if vids:
                picked, seen = [], set()
                for v in vids:  # one per channel
                    if v["channel"] not in seen:
                        picked.append(v)
                        seen.add(v["channel"])
                self._results, self._cursor, self._news, self._verbatim = picked, 0, False, True
                return _videos_line(picked[:3])
        topic = sources.topic_of(text)
        if topic == "crafts" and sources.wants_patterns(user):  # "Any free beginner crochet hat patterns?"
            craft = re.search(r"\b(knit|crochet)", user, re.I)
            pats = await sources.ravelry_patterns(q if re.search(r"knit|crochet", q, re.I)
                                                  else f"{q} {craft.group(1) if craft else ''}")
            if pats:
                self._results, self._cursor, self._news, self._verbatim = pats, 0, False, True
                return _patterns_line(pats)
        if topic == "joomla" and re.search(r"\b(?:version|release[ds]?|update[ds]?|came out|upgrade)\b", user, re.I) \
                and not re.search(r"\bhow\b|\b(?:security|vulnerab\w*|exploits?|patch(?:es)?|cve)\b", user, re.I):
            if rel := await sources.joomla_release():
                self._results, self._cursor, self._news, self._verbatim = [{"url": rel["url"]}], 1, False, True
                return f"Joomla's latest release is {rel['version']}, from {_fmt_day(rel['when'].astimezone())}."
        if topic == "joomla" and re.search(r"\b(?:security|vulnerab\w*|exploits?|patch(?:es)?|cve)\b", user, re.I):
            notes = await sources.feed(sources.JOOMLA_SECURITY_FEED, "Joomla")
            if notes:
                first = sources._when(notes[0]["published_date"])
                names = [re.sub(r"^\[\d+\]\s*-\s*(?:Core\s*-\s*)?", "", n["title"]) for n in notes[:3]]
                self._results, self._cursor, self._news, self._verbatim = notes[:3], 3, False, True
                return (f"Joomla's latest security notices, from {_fmt_day(first) if first else 'recently'}: "
                        + "; ".join(names) + ". Keeping Joomla up to date covers those.")
        if topic and not recent and not as_list:
            found = await sources.stack_exchange(q, sources.TOPICS[topic]["se"])
            if found:
                self._results, self._cursor, self._news = found, len(found), False
                self._offered_web = q  # "Yes" to "want me to search the web?" searches Tavily
                lines = [f"{i}. Question: {r['title']}. Answer voted best ({r['votes']} votes): {r['content'][:380]}"
                         for i, r in enumerate(found, 1)]
                return "\n".join(lines) + _ANSWER_FROM_QA.format(where=sources.TOPICS[topic]["where"])
        if not recent and not as_list and (m := _DEFINITION.match(q)) \
                and not re.search(r"\b(?:of|in|on|for|between|does|do|did|to|from|with)\b", m.group("x"), re.I):
            if w := await sources.wikipedia(m.group("x")):  # "What is permaculture?"
                self._results, self._cursor, self._news, self._verbatim = [w], 1, False, True
                return _short_answer(w["content"], 55, 2)
        return None

    async def _price_answer(self, q: str) -> str:
        """A stock or crypto price from web search (no quote service, the user's choice):
        spoken only if a $ figure sits next to price words, not estimate words, in the
        sources; said with its source and how current it is. Never the model's guess."""
        self._verbatim, self._news = True, False
        try:  # Tavily's finance search gave a timed market price (Nov 10 test); news as a fallback
            result = await asyncio.wait_for(_tavily_search(
                query=f"{q} today", topic="finance", include_answer="advanced", max_results=8), timeout=12)
            if not result.get("results"):
                raise RuntimeError("no finance results")
        except Exception as e:
            log.info("finance search: %s; trying news", e)
            try:
                result = await asyncio.wait_for(_tavily_search(
                    query=f"{q} today", topic="news", time_range="day", search_depth="advanced",
                    include_answer="advanced", max_results=8), timeout=12)
            except Exception as e2:
                log.info("price search failed: %s", e2)
                return "Sorry, I couldn't reach the web just now."
        self._results, self._cursor = result.get("results", []), len(result.get("results", []))
        answer = _clean_text(result.get("answer") or "")
        site = _source_name(self._results[0].get("url", "")) if self._results else "the web"
        # "a regular-market price of 168.25 (as of 2026-11-10 18:35 UTC)": said by code, in the user's time
        m = re.search(r"(?:regular[\s‑-]?market |current |last |latest |trading )?price (?:of|is|at|was)\s*\$?"
                      r"(\d[\d,]*(?:\.\d+)?)[^.;]*?\bas of\s+(\d{4})\D?(\d{2})\D?(\d{2})\s+(\d{1,2}:\d{2})\s*UTC", answer, re.I)
        if m and _in_context(f"The stock price is ${m.group(1)}.", self._results):
            from datetime import timezone
            when = datetime.strptime(f"{m.group(2)}-{m.group(3)}-{m.group(4)} {m.group(5)}", "%Y-%m-%d %H:%M") \
                .replace(tzinfo=timezone.utc).astimezone()
            day = "" if when.date() == datetime.now().date() else f" on {when:%A}"
            name = re.sub(r"\b(?:what(?:'s| is)|the|current(?:ly)?|stock|share|price|today|right now|now|of|how much is|"
                          r"othos)\b|[?.,!]", " ", q, flags=re.I)
            name = " ".join(name.split()) or "It"
            log.info("price answer (Tavily finance): %s at %s %s", name, m.group(1), when)
            return (f"{name} was at ${m.group(1)} as of {when:%I:%M %p}{day}, going by {site}. "
                    "Prices move fast, so treat that as a rough figure.").replace(" 0", " ", 1)
        # no time given: "a regular-market price of 168.25" with the day it's for, if any
        m = re.search(r"(?:regular[\s‑-]?market |current |latest |trading )price (?:of|is|at|was)\s*\$?"
                      r"(\d[\d,]*(?:\.\d+)?)", answer, re.I)
        if m and _in_context(f"The stock price is ${m.group(1)}.", self._results):
            d = re.search(r"\b(\d{4})\D?(\d{2})\D?(\d{2})\b", answer)
            today = bool(d) and f"{d.group(1)}-{d.group(2)}-{d.group(3)}" == datetime.now().strftime("%Y-%m-%d")
            name = re.sub(r"\b(?:what(?:'s| is)|the|current(?:ly)?|stock|share|price|today|right now|now|of|how much is|"
                          r"othos)\b|[?.,!]", " ", q, flags=re.I)
            name = " ".join(name.split()) or "It"
            log.info("price answer (Tavily finance, no time): %s at %s", name, m.group(1))
            return (f"{name} was at ${m.group(1)} {'today' if today else 'recently'}, going by {site}, "
                    "though I couldn't tell the exact time. Prices move fast, so treat that as a rough figure.")
        spoken = _short_answer(answer, 45, 2)
        has_price = bool(re.search(r"\$\s?\d", spoken or ""))
        if not (spoken and has_price and _PRICE_WORDS.search(spoken) and not _ESTIMATE.search(spoken)
                and _backed_by(spoken, self._results)):
            log.info("price: no clear figure in context (%r)", spoken)
            return ("I found pages about it, but no clear current price I can trust: the numbers there "
                    "are mixed up with estimates and older figures.")
        when = re.search(r"\b(?:as of|at (?:the )?close|closed|today|this morning|this afternoon|yesterday|"
                         r"on (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                         r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec))\b", spoken, re.I)
        site = _source_name(self._results[0].get("url", "")) if self._results else "the web"
        log.info("price answer (Tavily, in context): %s", spoken)
        return (f"{spoken} That's going by {site}"
                + ("." if when else ", and I couldn't tell how current it is."))

    async def _twitch_channel_reply(self, name: str, user: str) -> str:
        """Twitch channels for a name, or one channel's videos and followers."""
        try:
            found = await asyncio.wait_for(sources.twitch_channels(name), timeout=8)
        except Exception as e:
            log.info("twitch channel search failed: %s", e)
            found = []
        self._results, self._cursor, self._news = [{"url": "https://www.twitch.tv/"}], 1, False
        if not found:
            return f"I couldn't find a Twitch channel called {name}."
        best = found[0]
        exact = best.get("exact", False)
        if re.search(r"\bhow many\b|\bvideos?\b|\bvods?\b|\bfollowers?\b", user, re.I):
            try:
                st = await asyncio.wait_for(sources.twitch_channel_stats(best), timeout=10)
            except Exception:
                st = {}
            lead = "" if exact else f"The closest channel I found is {best['name']}. "
            parts = []
            if st.get("videos") is not None:
                n = st["videos"]
                parts.append(f"more than {n} videos" if st.get("videos_more") else
                             f"{n} video{'' if n == 1 else 's'}" if n else "no saved videos")
            if st.get("followers") is not None:
                parts.append(f"{st['followers']:,} follower{'' if st['followers'] == 1 else 's'}")
            if not parts:
                return lead + f"I found {best['name']} on Twitch, but couldn't get their numbers."
            live = f" They're live right now{', playing ' + best['game'] if best['game'] else ''}." if best["live"] else ""
            return lead + f"{best['name']} has " + " and ".join(parts) + " on Twitch." + live
        if re.search(r"\b(?:live|on air|streaming (?:right )?now)\b", user, re.I):  # "Is Shroud live?"
            lead = "" if exact else f"The closest channel I found is {best['name']}. "
            if best["live"]:
                return lead + f"Yes, {best['name']} is live right now" + (f", playing {best['game']}." if best["game"] else ".")
            return lead + f"{best['name']} isn't live right now" + (f"; they usually stream {best['game']}." if best["game"] else ".")
        def say(c):
            if c["live"]:
                return f"{c['name']}, live now" + (f" playing {c['game']}" if c["game"] else "")
            return c["name"] + (f", who streams {c['game']}" if c["game"] else "")
        names = [say(c) for c in found[:5]]
        listed = names[0] if len(names) == 1 else "; ".join(names[:-1]) + f"; and {names[-1]}"
        lead = f"There's a channel called exactly {best['name']}. " if exact and len(found) > 1 else ""
        return lead + f"On Twitch I found: {listed}."

    def _fresh_stories(self, lists: list[list]) -> list[dict]:
        """Stories from several outlets: untold ones only, taking turns between outlets, the
        same story from two outlets once."""
        per = [[r for r in items if r.get("url") not in self._read_urls and _is_story(r)] for items in lists]
        mixed = []
        while any(per):
            for items in per:
                if items:
                    mixed.append(items.pop(0))
        kept, seen = [], []
        for r in mixed:
            words = set(sources.keywords(r["title"]))
            if any(len(words & s) >= max(3, min(len(words), len(s)) // 3) for s in seen):
                continue
            seen.append(words)
            kept.append(r)
        return kept

    async def _free_news(self, query: str, label: str, count: int, wider: bool = False) -> str | None:
        """A roundup from NYT Top Stories and The Guardian, or None (then Tavily)."""
        general, us = label in ("", "world"), label.lower() in _US_LABELS
        days = 7 if wider else 2
        try:
            if general:
                lists = await asyncio.wait_for(asyncio.gather(
                    sources.nyt_top("world" if label == "world" else "home"),
                    sources.guardian(section="world", days=days, n=12)), timeout=15)
            elif us:
                political = label.lower() in ("political", "politics", "election", "congress", "us political",
                                              "national political", "white house", "supreme court")
                lists = await asyncio.wait_for(asyncio.gather(
                    sources.nyt_top("politics" if political else "us"),
                    sources.guardian(section="us-news", days=days, n=12)), timeout=15)
            else:  # "news about salmon farming": The Guardian's search, stories that are really about it
                topic = re.sub(r"\s*news$", "", query, flags=re.I).strip()
                words = sources.keywords(topic)
                found = await asyncio.wait_for(sources.guardian(topic, n=15, days=10 if wider else 4), timeout=10)
                lists = [[r for r in found if words and sum(w in (r["title"] + " " + r["content"][:400]).lower()
                                                            for w in words) >= max(1, len(words) // 2)]]
        except Exception as e:
            log.info("free news failed: %s", e)
            return None
        kept = self._fresh_stories(lists)
        log.info("free news %r (%s): %d new stories from %s", query, label or "general", len(kept),
                 [len(x) for x in lists])
        if len(kept) < 3:
            return None
        self._breaking, self._news_q = False, (query, label)
        self._results, self._cursor, self._news = kept, 0, True
        topic = "" if general else f" on {'the US' if us and label.lower() in ('us', 'u.s.') else _LABEL_SAID.get(label.lower(), label)}"
        return self._read_page(0, max(count, NEWS_COUNT), f"Here's what's in the news{topic}.")

    async def _popular_news(self, count: int) -> str:
        try:
            found = await asyncio.wait_for(sources.nyt_popular(), timeout=12)
        except Exception:
            found = []
        kept = self._fresh_stories([found])
        if not kept:
            self._verbatim = True
            return ("That's everything on the most-read list." if found
                    else "I couldn't get the most-read list just now.")
        self._breaking, self._news_q = False, ("most popular", "popular")
        self._results, self._cursor, self._news = kept, 0, True
        return self._read_page(0, max(count, NEWS_COUNT), "Here's what people are reading most at The New York Times.")

    async def _wa_news(self, label: str, count: int, wider: bool = False) -> str:
        """Local, state or PC hardware headlines from free feeds (sources.py), as a roundup."""
        state = label == sources.STATE_LABEL
        where = sources.STATE or "the state"
        self._breaking = bool(_BREAKING.search(self._user_text or ""))
        self._news_q = ({"local": "local news", "hardware": "PC hardware news", "tech": "tech news",
                         "design": "design news", **sources.TOPIC_QUERY}.get(label, f"{sources.STATE_LABEL} news"), label)
        # magazines and blogs post less often than newspapers: look back further
        slow = label in ("design", *sources.TOPIC_FEED_SETS)
        hours = 72 if wider else BREAKING_HOURS if self._breaking else 36
        if slow:
            days = sources.TOPIC_DAYS.get(label, 30)
            hours = 24 * (days * 3 if wider else days)
        found = [r for r in await sources.feed_news(label, hours=hours) if r["url"] not in self._read_urls]
        kept, seen = [], []
        for r in found:  # the same story from three stations -> once
            words = set(sources.keywords(r["title"]))
            if any(len(words & s) >= max(3, min(len(words), len(s)) // 3) for s in seen):
                continue
            seen.append(words)
            kept.append(r)
        log.info("%s news: %d new stories", label, len(kept))
        if not kept:
            return f"That's all the {label} news I have right now; nothing new since I last checked."
        self._results, self._cursor, self._news = kept, 0, True
        if label in ("hardware", "tech", "design", *sources.TOPIC_FEED_SETS):
            intro = {"hardware": "Here's the latest in PC hardware.", "tech": "Here's the latest in tech.",
                     "design": "Here's what's new in digital art and design.",
                     "gardening": "Here's what's new in gardening.", "recipes": "Here are some fresh recipes.",
                     "books": "Here's what's new in books.", "movies": "Here's what's new in movies and TV."}[label]
        elif self._breaking:
            intro = f"Here's the latest from around {where}." if state else "Here's the latest around here."
        else:
            intro = f"Here's what's in the news around {where}." if state else "Here's what's happening around here."
        return self._read_page(0, max(count, NEWS_COUNT), intro)

    async def _news_search(self, query: str, label: str, count: int, summarize: bool = False,
                           wider: bool = False) -> str:
        """Today's headlines, read by code with their source (the sources' own words).
        Asked to summarize, Tavily's summary of them is spoken instead (only if its
        numbers are in the pages), then "Want the headlines?"."""
        if label in sources.FEED_SETS:  # local / state / hardware: free feeds, no Tavily credits
            return await self._wa_news(label, count, wider)
        if label in ("local", sources.STATE_LABEL):  # asked, but no feeds in personal.json
            self._verbatim = True
            return (f"I don't have any {'local' if label == 'local' else 'state'} news feeds set up yet. "
                    "You can add them in personal.json.")
        if label == "popular":  # "What are people reading?": NYT Most Popular
            return await self._popular_news(count)
        if not summarize and not _BREAKING.search(self._user_text or ""):
            # NYT Top Stories and The Guardian first (the user's keys, Nov 10); Tavily only if
            # they have fewer than 3 new stories (roundups were the biggest credit spender)
            if (free := await self._free_news(query, label, count, wider)) is not None:
                return free
        if _tavily_out():
            self._verbatim = True
            return ("I've used up this month's web searches, so world news will have to wait. "
                    + ("Local and " + sources.STATE + " news still work: just ask." if sources.STATE and "local" in sources.FEED_SETS
                       else "Local news still works: just ask." if "local" in sources.FEED_SETS else "Sorry."))
        global _tavily
        if _tavily is None:
            _tavily = AsyncTavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
        results, span, answer = [], "day", None
        general = label in ("", "world")
        us = label.lower() in _US_LABELS
        breaking = bool(_BREAKING.search(self._user_text or ""))
        self._breaking, self._news_q = breaking, (query, label)
        # major outlets first (preferred, not required): world -> NEWS_SITES, US/politics ->
        # US_NEWS_SITES, any other topic -> both, like the top of a news search
        sites = (NEWS_SITES + US_NEWS_SITES if breaking or not (general or us)  # breaking: both lists
                 else NEWS_SITES if general else US_NEWS_SITES)
        spans = ([("hours", BREAKING_HOURS)] if breaking else []) + [("day", None), ("week", None)]
        if wider:
            spans = [("week", None)]
        extra = {"include_answer": "advanced", "search_depth": "advanced"} if summarize else {}
        if breaking and general:
            query = "breaking news"  # "world news" over 6 hours brought small sites and digests
        extra_task = None
        if breaking:  # NYT and Guardian stories from the last few hours join Tavily's (free)
            extra_task = asyncio.create_task(asyncio.gather(
                sources.nyt_top("us" if us else "home"),
                sources.guardian(section="us-news" if us else "world" if general else "", days=1, n=12,
                                 q="" if (general or us) else re.sub(r"\s*news$", "", query), newest=True),
                return_exceptions=True))
        try:
            best = ([], "day", None)
            for span, hours in spans:  # a quiet topic may have nothing recent
                when_opts = {"max_age_hours": hours} if span == "hours" else {"time_range": span}
                # major outlets only first; if they have under 3 stories, prefer them over the rest
                for mode in ("restrict", "prefer"):
                    result = await asyncio.wait_for(_tavily_search(
                        query=query, topic="news", max_results=FETCH_N, include_domains=sites,
                        include_domains_mode=mode, **when_opts, **extra), timeout=10)
                    found = [r for r in result.get("results", []) if _is_story(r)
                             and r.get("url", "") not in self._read_urls]  # nothing already told
                    if len(found) > len(best[0]):  # keep the best try, not just the last
                        best = (found, span, result.get("answer"))
                    if len(found) >= 3:
                        break
                if len(best[0]) >= 3:
                    break
            results, span, answer = best
        except Exception as e:
            if extra_task:
                extra_task.cancel()
            return f"Search failed: {e}"
        if extra_task:
            from datetime import timezone
            cutoff = datetime.now(timezone.utc) - timedelta(hours=BREAKING_HOURS)
            try:
                extra = await asyncio.wait_for(extra_task, timeout=10)
            except Exception:
                extra = []
            have = {r.get("url") for r in results}
            for items in extra:
                if isinstance(items, list):
                    results += [r for r in items if r.get("url") not in have and r.get("url") not in self._read_urls
                                and _is_story(r) and (_published(r) or cutoff) > cutoff]
            log.info("breaking: %d stories with NYT/Guardian added", len(results))
        log.info("news search %r (%s): %d new results", query, span, len(results))
        if not results:
            return "That's all the news I have right now; nothing new since I last checked."
        # newest first for breaking news; then one story per site first, so the first
        # few come from different outlets; same headline twice -> once
        if breaking:
            from datetime import timezone
            oldest = datetime(1970, 1, 1, tzinfo=timezone.utc)
            results.sort(key=lambda r: _published(r) or oldest, reverse=True)
        seen, titles, first, rest = set(), set(), [], []
        for r in results:
            key = re.sub(r"\W+", " ", _news_line(r).lower())[:60]
            if key in titles:
                continue
            titles.add(key)
            site = _site(r.get("url", ""))
            (rest if site in seen else first).append(r)
            seen.add(site)
        self._results, self._cursor, self._news = first + rest, 0, True
        if summarize:
            spoken = _short_answer(answer)
            if spoken and _backed_by(spoken, results):
                self._verbatim = True  # "Yes" next reads the headlines from the start
                log.info("news summary (Tavily): %s", spoken)
                return f"{spoken} Want the headlines?"
            log.info("news summary not usable; reading headlines instead")
        topic = "" if general else f" on {'the US' if us and label.lower() in ('us', 'u.s.') else _LABEL_SAID.get(label.lower(), label)}"
        intro = f"Here's the latest{topic}." if breaking else f"Here's what's in the news{topic}."
        return self._read_page(0, max(count, NEWS_COUNT), intro)

    async def _guardian_answer(self, q: str, ask: str) -> str | None:
        """A story question answered from The Guardian's own reporting (free): the sentence
        that says most about what the user asked, if it really does. None -> Tavily."""
        story_words = set(sources.keywords(re.sub(re.escape(ask), " ", q, flags=re.I)))
        ask_words = [w for w in sources.keywords(ask) if w not in story_words and len(w) > 3 and w not in
                     ("othos", "actually", "really", "story", "news", "statement", "said", "says", "tell", "think",
                      "happen", "happened", "about", "there", "they", "them", "this", "that", "does", "were")]
        if not ask_words:
            return None
        # The Guardian's search wants every word: the story's key words only (names first)
        headline = re.sub(re.escape(ask), " ", q, flags=re.I)
        names = [w.lower() for w in re.findall(r"\b[A-Z][a-z]{2,}\b", headline) if w.lower() in story_words]
        key = list(dict.fromkeys(names + [w for w in sources.keywords(headline) if len(w) > 4]))[:3]
        try:
            arts = await asyncio.wait_for(sources.guardian(" ".join(key), n=6, days=10), timeout=10)
        except Exception:
            return None
        best, best_score = None, 0
        for a in arts:
            head = f"{a.get('title', '')} {a.get('content', '')[:300]}".lower()
            if sum(w in head for w in story_words) < min(2, len(story_words)):
                continue  # this article isn't about the story

            sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z\"'])", a.get("content", ""))
            for i, s in enumerate(sentences):
                low = s.lower()
                ask_hits = sum(any(t.startswith(w[:5]) for t in re.findall(r"[a-z0-9'-]+", low)) for w in ask_words)
                topic_hits = sum(w in low for w in story_words)
                score = ask_hits * 2 + topic_hits
                if ask_hits >= min(2, len(ask_words)) and ask_words and score > best_score:
                    best, best_score = (a, " ".join(sentences[i:i + 2])), score
        if not best:
            log.info("story answer: nothing in The Guardian for %r", ask)
            return None
        a, text = best
        spoken = _short_answer(text, 55, 2)
        log.info("story answer (The Guardian): %s", spoken)
        return f"{spoken} That's going by The Guardian."

    async def _story_answer(self, q: str, ask: str = "") -> str:
        """A question about one story: The Guardian's reporting first (free), then Tavily news
        search (deeper, past week), its written answer spoken only if its numbers are in the
        pages. The roundup stays as it was."""
        if ask and (said := await self._guardian_answer(q, ask)):
            return said
        global _tavily
        if _tavily is None:
            _tavily = AsyncTavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
        try:
            r = await asyncio.wait_for(_tavily_search(
                query=q, topic="news", time_range="week", search_depth="advanced", include_answer="advanced",
                max_results=8, include_domains=NEWS_SITES + US_NEWS_SITES + list(sources.OUTLETS)[:10],
                include_domains_mode="prefer"), timeout=12)
        except Exception as e:
            log.warning("story search failed: %s", e)
            return "Sorry, I couldn't reach the news just now."
        results = r.get("results", [])
        spoken = _short_answer(r.get("answer"))
        if spoken and results and _backed_by(spoken, results):
            names = list(dict.fromkeys(_outlet(x) for x in results[:3]))
            log.info("story answer (Tavily): %s", spoken)
            return f"{spoken} That's going by {names[0]}" + (f" and {names[1]}." if len(names) > 1 else ".")
        story = self._story or {}
        log.info("story answer: no sourced answer for %r", q)
        return ("I couldn't find that detail in the reporting. "
                f"What {_outlet(story) if story else 'they'} said was: {_news_line(story)}." if story
                else "I couldn't find that detail in the reporting.")

    async def _news_followup(self, text: str) -> str | None:
        """'Tell me more about the fridge hacking', 'Where's that from?', 'Any other news?'
        after a roundup: answered from the stories found, without a new search. None =
        not a follow-up (or no matching story): the turn goes on as usual."""
        told = [r for r in self._results if r.get("url", "") in self._read_urls] or self._results
        if _ASKS_SOURCE.search(text):
            rest = _ASKS_SOURCE.sub(" ", text)
            story = _match_story(rest, told) or self._story
            if story is not None:
                self._story = story
                log.info("news follow-up: source of %r", _news_line(story))
                return f"That one's from {_outlet(story)}."
            names = list(dict.fromkeys(_outlet(r) for r in told))
            listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
            log.info("news follow-up: all sources")
            return f"Those came from {listed}."
        if m := _MORE_ON.search(text):
            story = _match_story(m.group("topic"), told)
            if story is not None:
                self._story = story
                # drop writer bios ("Alex Sundby is a senior editor at CBSNews.com...")
                body = " ".join(s for s in re.split(r"(?<=[.!?])\s+", _clean_text(story.get("content", "")))
                                if not _AUTHOR_BIO.search(s))
                detail = _short_answer(body) or _news_line(story)
                log.info("news follow-up: more on %r", _news_line(story))
                return f"{detail} That's from {_outlet(story)}. Want me to look for more on it?"
            return None
        # A specific question about one story ("Did McConnell give that statement himself, or
        # was it written?" got "I don't know" twice, 12:45 PM call): search that story plus
        # the user's question; Tavily's sourced answer is spoken (code starts the search)
        # ...but only when the user is asking about a story: it points back ("that story", "he") or
        # shares words with a headline. "What's the NVIDIA stock price?" after the roundup was
        # searched as the Iran story plus the user's question (10:36 AM call, Nov 10)
        headlines = [{"title": r.get("title", ""), "url": r.get("url", "")} for r in told]
        points_back = bool(re.search(r"\b(?:that|this|the) (?:story|one|article|report|news)\b|"
                                     r"\b(?:he|she|they|his|her|their|them|it)\b", text, re.I))
        by_title = _match_story(text, headlines, min_score=2)
        if (_QUESTION.search(text) or re.match(r"^\W*(?:did|does|do|is|was|were|are|has|have|will|can)\b", text, re.I)) \
                and not _PRICE_ASK.search(text) \
                and (by_title is not None or points_back) \
                and (story := (next((r for r in told if r.get("url") == by_title["url"]), None) if by_title
                               else _match_story(text, told, min_score=2))) is not None:
            self._story = story
            topic = " ".join(_news_line(story).split()[:12])
            ask = re.sub(r"\b(?:othos|actually|really|please|can you tell me|do you know)\b", " ", text, flags=re.I)
            q = re.sub(r"\s+", " ", f"{topic} {ask}").strip(" ?.,!")
            log.info("news follow-up: question about %r -> search %r", _news_line(story), q)
            self._followup_query = q
            return None  # llm_node says a lead-in, then _story_answer searches
        if _MORE_NEWS.search(text) and self._news_q[0]:
            if self._cursor < len(self._results):
                out = self._read_page(self._cursor, NEWS_COUNT, "Here's more.")
            else:  # everything found was told: search again over a wider window, skipping those
                out = await self._news_search(*self._news_q, NEWS_COUNT, wider=True)
            self._verbatim = False  # spoken here, not as a tool result
            log.info("news follow-up: more news")
            return out
        return None

    @function_tool()
    async def more_results(self, context: RunContext, count: int = 4) -> str:
        """Read the next results from the last search.

        Args:
            count: Results to read, default 4.
        """
        if not self._results:
            return "There is no search to continue. Ask what to search for."
        if self._cursor >= len(self._results):
            return "That was every result I have. Offer to search again with different words."
        count = NEWS_COUNT if self._news else max(1, min(_to_int(count, 4), MAX_PAGE))
        if self._news and self._cursor > 0 and len(self._results) - self._cursor < 3 and self._news_q[0]:
            # "Let's keep going" read just one more story (10:35 AM call): when fewer than 3
            # are left, look further back for more (stories already told are skipped)
            log.info("news: only %d left; searching wider", len(self._results) - self._cursor)
            before, left = self._results, self._results[self._cursor:]
            out = await self._news_search(*self._news_q, NEWS_COUNT, wider=True)
            if self._news and self._results and self._results is not before:  # found new stories
                urls = {r.get("url") for r in self._results}
                self._results = [r for r in left if r.get("url") not in urls] + self._results
                return self._read_page(0, NEWS_COUNT, "Here's more.")
            if left:  # nothing new further back: read what's left
                return self._read_page(self._cursor, count, "Here's the rest.")
            return out
        intro = "Here are the headlines." if self._cursor == 0 else "Here's more."  # after a news summary
        return self._read_page(self._cursor, count, intro)

    # ---- camera -----------------------------------------------------------
    @function_tool()
    async def look(self, context: RunContext, question: str = "") -> str:
        """Look through the user's camera and say what is there.

        Args:
            question: What the user asked about what they're showing.
        """
        if self._looking:
            return "I'm still looking at the last one."
        text = question or self._user_text
        # a follow-up just after a look is answered from the same picture
        pic = self._recent_pic() if (_LOOK_FOLLOWUP.search(text) and not _LOOK_AGAIN.search(text)) else ""
        pub = None if pic else _camera_publication()
        if not pic and pub is None:
            return "The camera is off. Ask the user to turn it on with the camera button."
        self._looking = True
        try:
            t0 = time.monotonic()
            if not pic:
                frame = await _grab_frame(pub)
                if frame is None:
                    log.warning("look: no picture from the camera within 5 s")
                    return "I couldn't get a picture from the camera. Check that it's on and try again."
                # kept in memory only, for follow-ups for LOOK_MEMORY_S (never written to disk)
                pic = base64.b64encode(encode_image(frame, EncodeOptions(
                    format="JPEG", quality=85,
                    resize_options=ResizeOptions(width=768, height=768, strategy="scale_aspect_fit")))).decode()
                del frame
                self._last_pic = (pic, time.monotonic())
                reused = False
            else:
                reused = True
            t1 = time.monotonic()
            prompt = _vision_prompt(text)
            if reused and prompt == _GENERIC_LOOK:
                prompt = _DETAIL_PROMPT  # "Do you notice anything else?"

            async def ask(p: str) -> str:
                async with httpx.AsyncClient() as client:
                    r = await client.post(f"{_OLLAMA_ROOT}/api/generate", timeout=90, json={
                        "model": VISION_MODEL, "prompt": p, "stream": False, "images": [pic],
                        "keep_alive": VISION_KEEP_ALIVE,
                        "options": {"num_predict": 80, "temperature": 0},
                    })
                    r.raise_for_status()
                    return r.json().get("response", "")

            try:
                raw = await ask(prompt)
                held = ""
                if prompt == _HELD_PROMPT:  # "Am I holding anything?"
                    held, raw = _held_from(raw), ""
                elif prompt in (_GENERIC_LOOK, _DETAIL_PROMPT) and not re.search(r"\bholding\b", raw, re.I):
                    # the short description left out what the user held up; Moondream has
                    # read the picture already, so this costs a few seconds
                    held = _held_from(await ask(_HELD_PROMPT))
                answer = _spoken_vision(raw, held)
                if prompt == _HELD_PROMPT and not answer:
                    answer = "It doesn't look like you're holding anything."
            except Exception as e:
                log.warning("look: vision model failed: %s", e)
                return "My eyes aren't working right now; the vision model didn't answer."
            t3 = time.monotonic()
            log.info("look took %.1f s (%s, vision model %.1f s) for %r: %s",
                     t3 - t0, "same picture" if reused else f"new picture {t1 - t0:.1f} s",
                     t3 - t1, prompt, answer)
            # spoken as-is either way: given an empty result, the language model
            # told the user it can't see at all
            self._verbatim = True
            if answer:
                self._last_look = (answer, time.monotonic())
            return answer or "I looked, but I couldn't make out anything clearly. Try asking again."
        finally:
            self._looking = False

    # ---- notes ------------------------------------------------------------
    @function_tool()
    async def save_note(self, context: RunContext, text: str, tags: str = "") -> str:
        """Save something the user asked you to remember. Never passwords or card numbers.

        Args:
            text: The note, in the user's words.
            tags: Optional keywords.
        """
        now = datetime.now()
        text = _resolve_dates(text.strip(), now)
        path = NOTES_DIR / f"{now:%Y-%m-%d-%H%M%S}-{_slug(text)}.md"
        path.write_text(
            f"---\ndate: {now:%Y-%m-%d %H:%M} ({now:%A})\ntags: {_clean_tags(tags)}\n---\n{text}\n",
            encoding="utf-8",
        )
        # embed now, before the reply is spoken (~40 ms); if it fails, the
        # backfill at the next worker start or the next notes search catches it
        if n := _read_note(path):
            doc = _note_doc(n)
            t = time.perf_counter()
            if vecs := await _embed_async([doc]):
                _store_vectors({path.name: {"key": _doc_key(doc), "v": vecs[0]}})
                log.info("note embedded in %.0f ms", (time.perf_counter() - t) * 1000)
        return f"Saved. Read it back to the user: {text}"

    @function_tool()
    async def search_notes(self, context: RunContext, query: str, limit: int = 5) -> str:
        """Find things the user told you before: their appointments, plans, people.

        Args:
            query: A few keywords.
            limit: Max notes, default 5.
        """
        limit = max(1, min(int(limit or 5), 8))
        user_name = {w.lower() for w in re.findall(r"address the user as (\w+)", load_profile(), re.I)}
        if _user_name():
            user_name.add(_user_name().lower())
        words = set()
        for w in re.findall(r"[a-z0-9]+(?:'[a-z]+)?", query.lower()):
            w = re.sub(r"'s$", "", w)
            if len(w) > 2 and w not in _FILLER and w not in user_name:
                words.add(w)
                words.update(_SYNONYMS.get(w, []))

        notes = [(p.name, n) for p in NOTES_DIR.glob("*.md") if (n := _read_note(p))]
        if not notes:
            return "No notes saved yet."
        keyword = {}
        for name, n in notes:
            hay = f"{n['text']} {n['tags']}".lower()
            score = sum(len(re.findall(rf"\b{re.escape(w)}", hay)) for w in words)
            if score:
                keyword[name] = score
        meaning = await self._note_meanings(query, notes)
        # Blend: a note counts if its words match or its meaning is close enough.
        # Keyword hits are scaled 0-1 against the best one, then averaged with
        # how close the meaning is.
        top_kw = max(keyword.values(), default=0)
        close = {name for name, s in meaning.items() if s >= SEM_MIN}
        ranked = sorted(meaning.values(), reverse=True) + [0.0, 0.0]
        scored = []
        for name, n in notes:
            kw = keyword.get(name, 0) / top_kw if top_kw else 0.0
            sem = meaning.get(name, 0.0)
            if kw or name in close:
                scored.append((0.5 * kw + 0.5 * sem, name, n))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        log.info("notes search: %d keyword hit(s), %d by meaning (best %.2f, next %.2f), %d returned",
                 len(keyword), len(close), ranked[0], ranked[1], min(len(scored), limit))

        def fmt(n):
            return f"[{_when_saved(n['saved'])}] {n['text'][:300]}"

        if scored:
            return "\n".join(fmt(n) for _, _, n in scored[:limit])
        # Borderline: one note stands out by meaning but not strongly. Scores
        # can't tell "backup power" -> generator (0.52) from "car insurance" ->
        # generator (0.52), and the 3B model judged it wrongly half the time (or
        # made details up), so code reads the note out and the user decides.
        if ranked[0] >= SEM_FLOOR and ranked[0] - ranked[1] >= SEM_MARGIN:
            best = next(n for name, n in notes if meaning.get(name) == ranked[0])
            log.info("notes search: closest note read out (%.2f)", ranked[0])
            self._verbatim = True
            text = best["text"].strip().rstrip(".") + "."
            return (f"I don't have a note that says that exactly. The closest one, "
                    f"{_when_saved(best['saved'])}, says: {text}")
        # Nothing matched the words: hand back the latest notes so the model
        # can still answer questions like "what's on Sunday?"
        recent = sorted(notes, key=lambda t: t[0], reverse=True)[:limit]
        return ("No note uses those words. The most recent notes are below; "
                "answer from them if they fit, otherwise say nothing was saved.\n"
                + "\n".join(fmt(n) for _, n in recent))

    async def _note_meanings(self, query: str, notes: list) -> dict:
        """{note file name: closeness 0-1} between the question and each note, or
        {} if the embedding model can't be reached. Uses the user's own words
        (fuller than the model's keywords), with "tomorrow" etc. pinned to dates
        the same way notes are, so "my schedule tomorrow" meets the right day."""
        question = _resolve_dates(self._user_text or query, datetime.now())
        vectors = _load_vectors()
        missing = _missing_vectors(notes, vectors)[:16]  # normally none
        t = time.perf_counter()
        vecs = await _embed_async([f"search_query: {question}"] + [doc for _, doc, _ in missing])
        if not vecs:
            return {}
        if missing:
            new = {name: {"key": key, "v": v} for (name, _, key), v in zip(missing, vecs[1:])}
            _store_vectors(new)
            vectors.update(new)
        qv = vecs[0]
        out = {}
        for name, n in notes:
            rec = vectors.get(name)
            if rec and rec.get("key") == _doc_key(_note_doc(n)):
                out[name] = _cosine(qv, rec["v"])
        log.info("notes meaning search took %.0f ms (%d notes)", (time.perf_counter() - t) * 1000, len(out))
        return out


# ---------------------------------------------------------------- server setup
# Fixed health-check port (dev mode otherwise picks a random one) so the
# Claude app's run panel can tell when the agent is up: http://127.0.0.1:8081
# num_idle_processes=1 keeps one agent process loaded and ready, so a new call
# does not have to start one from scratch (dev mode otherwise keeps none).
server = AgentServer(
    port=int(os.getenv("AGENT_HTTP_PORT", "8081")),
    num_idle_processes=1,
    # One person, one call: the worker's "too busy" check (it turns a call away when the processor is 70%+ busy)
    # only ever sent the user's call away (Nov 20, 7:42: three tries, "no servers available", PC at 65-84% right after
    # The Smithy loaded its model). The load is always reported as 0, so a call is never declined for it.
    load_fnc=lambda: 0.0,
    # starting a call's process took 8.2 s of the default 10 s on a busy PC, and failed at 7:18 ("error initializing
    # process"): give it a minute
    initialize_process_timeout=60.0,
)


def prewarm(proc: JobProcess):
    # Runs once when the worker starts, before any session
    proc.userdata["vad"] = silero.VAD.load()
    # Each call runs in its own process; make the search client here, before the
    # call, so the first search doesn't spend ~0.7 s setting it up
    global _tavily, _embed_client
    try:
        _tavily = AsyncTavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
    except Exception as e:
        log.warning("search client not ready at start: %s", e)
    _embed_client = httpx.AsyncClient()  # for notes meaning search, same reason
    _ssl_context()  # the certificate list, read once (see _llm_client)
    # load llama + the embedding model and embed any new notes, in the
    # background so a call can start straight away
    threading.Thread(target=_warm_models, name="warm-models", daemon=True).start()


server.setup_fnc = prewarm


# ---------------------------------------------------------------- the model connection
# Building a web client reads the whole certificate list, about 2.5 s of CPU on this
# laptop, and it blocked every call's start (Nov 12 call: the loop froze 3.2 s, speech
# detection fell 4 s behind during the greeting). The list is read once in prewarm and
# shared; each call still gets its own client, on its own event loop.
def _ssl_context() -> ssl.SSLContext:
    return sources.ssl_context()  # one shared list for the model and the free sources


def _llm_client(ollama: bool = False):
    """The conversation's client: llama-server when voice-llm.json says so, else Ollama. ollama=True: always Ollama
    (gaming mode's model stays there)."""
    import openai as openai_sdk
    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
    # the same settings LiveKit's plugin uses for its own client
    return openai_sdk.AsyncClient(
        api_key="ollama",
        base_url=ollama_url if ollama or not llm_server.enabled() else llm_server.root() + "/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(
            verify=_ssl_context(),
            timeout=httpx.Timeout(connect=15.0, read=5.0, write=5.0, pool=5.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=50, keepalive_expiry=120),
        ),
    )


# ---------------------------------------------------------------- speech
# OTHOS_SPEECH in .env: "cloud" (default) = Deepgram hears, Rime speaks (as always);
# "local" = an OpenAI-compatible speech-to-text server on this computer (Speaches /
# faster-whisper) and Kokoro-FastAPI for the voice, so no audio leaves the computer.
# Local speech needs a graphics card to be quick; it hasn't been tested on a CPU laptop.
SPEECH = os.getenv("OTHOS_SPEECH", "cloud").strip().lower()


def _make_stt():
    if SPEECH == "local":
        return openai.STT(
            model=os.getenv("LOCAL_STT_MODEL", "Systran/faster-whisper-small.en"),
            base_url=os.getenv("LOCAL_STT_URL", "http://127.0.0.1:8000/v1"),
            api_key=os.getenv("LOCAL_STT_KEY", "local"),
            language="en",
            # the place names it should listen for (Whisper takes them as a hint)
            prompt=", ".join([ASSISTANT, *personal.keyterms()]),
            use_realtime=False,
        )
    return deepgram.STT(
        mip_opt_out=os.getenv("DEEPGRAM_MIP_OPT_OUT", "true").lower() == "true",
        # names it kept mishearing ("Otis" for Othos, the town); Deepgram already
        # hears them in the user's voice, so listing them shares nothing new
        keyterm=[ASSISTANT, *personal.keyterms()],  # its name, and place names from personal.json
    )


def _make_tts():
    if SPEECH == "local":
        return openai.TTS(
            model=os.getenv("LOCAL_TTS_MODEL", "kokoro"),
            voice=os.getenv("LOCAL_TTS_VOICE", "af_heart"),
            base_url=os.getenv("LOCAL_TTS_URL", "http://127.0.0.1:8880/v1"),
            api_key=os.getenv("LOCAL_TTS_KEY", "local"),
        )
    return rime.TTS(
        model=os.getenv("RIME_MODEL", "coda"),
        speaker=os.getenv("RIME_SPEAKER", "holloway"),
    )


@server.rtc_session()
async def entrypoint(ctx: JobContext):
    # Audio only: the user's camera video is fetched only for the moment Othos looks
    # (see _grab_frame), never received or decoded in the background
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    llm_client = _llm_client()
    ctx.add_shutdown_callback(llm_client.close)  # ours, so LiveKit won't close it
    session = AgentSession(
        stt=_make_stt(),
        # (openai.LLM rather than with_ollama, which can't pass keep_alive: it
        # keeps llama loaded between calls instead of unloading after 5 minutes)
        llm=openai.LLM(
            model=os.getenv("OLLAMA_MODEL", "llama3.2:3b"),
            client=llm_client,  # made with the shared certificate list (_llm_client)
            # llama-server: the call's own slot, keeping what's been read; Ollama: stay loaded between calls
            extra_body=llm_server.request_extras() if llm_server.enabled() else {"keep_alive": KEEP_ALIVE},
        ),
        tts=_make_tts(),
        vad=ctx.proc.userdata["vad"],
        # LiveKit gives the LLM 10s by default, then cancels and retries up to
        # 3 times. A CPU-only laptop needs longer, and retries only add load.
        conn_options=SessionConnectOptions(
            llm_conn_options=APIConnectOptions(
                timeout=float(os.getenv("LLM_TIMEOUT", "45")),
                max_retry=1,
            ),
        ),
        # Preemptive generation starts the LLM before the user has finished
        # talking and throws the work away if they keep going. On a CPU-only
        # laptop that wasted run delays the real reply, so it is off.
        # Interruptions need 2 words from speech-to-text, not just sound: with
        # speakers, the mic hears Othos's own voice, and that echo alone paused
        # it, then it resumed 2 s later (the "stutter").
        turn_handling=TurnHandlingOptions(
            # Local turn detector only. The default is the cloud one in "dev" mode
            # (sends audio to LiveKit's gateway); "v1-mini" runs on this PC.
            turn_detection=inference.TurnDetector(version="v1-mini"),
            preemptive_generation={"enabled": False},
            # "vad": interruptions are judged on this PC. The default ("adaptive") connects to LiveKit's
            # cloud gateway (agent-gateway.livekit.cloud) with the call's audio, which isn't an approved service.
            interruption={"mode": "vad", "min_words": 2},
        ),
    )

    # End the job as soon as the call ends. Otherwise it lingers ~20 s until the
    # server closes the empty room, and calling straight back could fail to get
    # an agent.
    session.on("close", lambda ev: ctx.shutdown(reason=f"session closed: {ev.reason}"))

    # counters for the quiet self-check ("How are you?"): how long answers take, errors this call
    selfcheck.reset()

    # each message's timing (where a slow reply's wait went) and the self-check's reply times; this replaces the
    # deprecated metrics_collected event
    session.on("conversation_item_added", lambda ev: _note_timing(ev.item))
    # (only errors that weren't retried away: a recoverable one worked on the next try)
    session.on("error", lambda ev: None if getattr(ev.error, "recoverable", False) else selfcheck.note_error())

    assistant = Assistant()
    # who said each final piece of transcript, when speech-to-text labels speakers (voices.py; none yet)
    # and when she was last heard at all, so worker announcements never start mid-sentence (workers_voice.py)
    def _on_transcribed(ev) -> None:
        assistant._workers.heard()
        if ev.is_final:
            assistant._voices.heard(ev.speaker_id, ev.transcript)
    session.on("user_input_transcribed", _on_transcribed)
    # gaming mode: watches for listed games and switches the voice model between turns
    monitor = gaming.GamingMonitor(session, _OLLAMA_ROOT, KEEP_ALIVE)
    gaming_client = llm_client
    if llm_server.enabled():
        # the conversation is on llama-server; gaming mode's model stays in Ollama, and Ollama's copy of the
        # normal model is never loaded (it would double the memory)
        gaming_client = _llm_client(ollama=True)
        ctx.add_shutdown_callback(gaming_client.close)
        monitor.normal_on_ollama = False
        asyncio.create_task(llm_server.warm(str(assistant.instructions)))   # instructions read while greeting
    monitor.cpu_llm = openai.LLM(
        model=monitor.cfg.gaming_model,
        client=gaming_client,  # an Ollama client (the model name goes with each request)
        extra_body={"keep_alive": KEEP_ALIVE},
    )
    assistant._gaming = monitor
    weather_task = asyncio.create_task(_fetch_weather())  # runs while the call connects
    usage_task = asyncio.create_task(sources.tavily_usage())  # this month's Tavily credits (free check)
    await session.start(room=ctx.room, agent=assistant)
    monitor.start()

    async def use_weather(wait: float) -> None:
        """Give Othos the real weather (and the filter its sky word). If Tavily is slow,
        the greeting doesn't wait: the weather is added when it arrives."""
        global _weather_now
        try:
            w = await asyncio.wait_for(asyncio.shield(weather_task), timeout=wait)
        except asyncio.TimeoutError:
            weather_task.add_done_callback(lambda t: asyncio.ensure_future(use_weather(0.1)))
            return
        except Exception:
            return
        if not w or _weather_now is not None:
            return
        _weather_now = w
        base = assistant.instructions if isinstance(assistant.instructions, str) else str(assistant.instructions)
        await assistant.update_instructions(
            base + f" Weather now in {WEATHER_LOCATION}: {w['summary']} "
                   "Don't comment on the weather unless the user brings it up (the reading can be "
                   "off: 'mostly sunny' while the user saw gray); then use only this, never guess it.")

    if WEATHER_LOCATION:
        await use_weather(3.0)

    if os.getenv("GREET_ON_START", "true").lower() == "true":
        # not interruptible: the call is just starting, and echo protection only
        # covers the first 3 s, so the greeting's own echo used to pause it.
        # Said by code (no model): the model took 10-15 s to write two short sentences.
        # OTHOS_GREETING=model brings back the model-written greeting.
        if os.getenv("OTHOS_GREETING", "").lower() == "model":
            await session.generate_reply(instructions="Greet the user briefly.", allow_interruptions=False)
        else:
            line = _opening_line(datetime.now().hour, _user_name())
            log.info("greeting by code: %r", line)
            await session.say(line, allow_interruptions=False)

    # Firestarter and the Smithy: "Smithy task 3 is ready" between turns; results finished while the user was away
    assistant._workers.session = session
    assistant._workers.start_announcer()

    # Tavily credits: counted from Tavily's own figure; a spoken heads-up once past the
    # warning point (the user: 1,400), and no Tavily at all at the limit
    global _tavily_used, _tavily_limit
    try:
        usage = await asyncio.wait_for(usage_task, timeout=5)
    except Exception:
        usage = None
    if usage:
        _tavily_used, _tavily_limit = usage["used"], TAVILY_LIMIT or usage.get("limit")
        log.info("Tavily credits this month: %s of %s (warn at %s)", _tavily_used, _tavily_limit, TAVILY_WARN)
        if _tavily_out():
            session.say(f"Heads up: I've used all {_tavily_limit:,} of this month's Tavily searches, so I'll stick "
                        "to my free sources until it resets.")
        elif _tavily_used >= TAVILY_WARN:
            left = f", {_tavily_limit - _tavily_used:,} left" if _tavily_limit else ""
            session.say(f"Heads up: Tavily's at {_tavily_used:,} of its "
                        f"{_tavily_limit:,} searches this month{left}." if _tavily_limit else
                        f"Heads up: Tavily's at {_tavily_used:,} searches this month.")
    else:
        log.info("Tavily usage check failed; credits not counted this call")
    # Brave Search: its own count of this month's searches (state\brave-usage.json); a spoken heads-up near the limit
    if os.getenv("BRAVE_API_KEY"):
        used = sources.brave_used()
        log.info("Brave searches this month: %s of %s (warn at %s)", used, sources.BRAVE_LIMIT, sources.BRAVE_WARN)
        if used >= sources.BRAVE_LIMIT:
            session.say(f"Heads up: I've used all {sources.BRAVE_LIMIT:,} of this month's Brave searches, so I'll use "
                        "my free sources and Tavily until it resets.")
        elif used >= sources.BRAVE_WARN:
            session.say(f"Heads up: Brave's at {used:,} of its {sources.BRAVE_LIMIT:,} searches this month.")


if __name__ == "__main__":
    agents.cli.run_app(server)