"""Othos's quiet self-check, for "How are you?" asked back after a greeting.

When the user answers Othos's greeting and asks how Othos is ("I'm fine, how about you?"), code looks at
what it can see on this PC, silently and in well under a second, and words the answer itself from a few
templates. No model is used (it is slow on a CPU laptop and can invent things), nothing leaves this PC, and
nothing private is read: only this PC's own load, Ollama's local status, the workers' status files and counters
for this call.
"""
from __future__ import annotations

import logging
import random
import re
import shutil
from pathlib import Path

import httpx
import psutil

log = logging.getLogger("othos")

# ---------------------------------------------------------------- counters for this call
_replies: list[float] = []   # seconds the last few spoken answers took to start
_errors = 0                  # speech or model errors since the call opened


def reset() -> None:
    global _errors
    _replies.clear()
    _errors = 0


def note_reply(seconds: float) -> None:
    if seconds and seconds > 0:
        _replies.append(float(seconds))
        del _replies[:-5]


def note_error() -> None:
    global _errors
    _errors += 1


# ---------------------------------------------------------------- what the user said
_RETURN = re.compile(
    r"\b(?:how about you|and you|and yourself|what about you|how are you|how're you|how are you doing|"
    r"how have you been|how(?:'s| is) it going with you|how do you feel|how are you feeling|"
    r"how are you holding up|you doing (?:ok|okay|alright|well)|how(?:'s| is) your day|how(?:'s| is) it going)\b", re.I)
# the question has to END the sentence ("How are you?", "and you?"): "How are you going to fix it?" is a request
_RETURN_END = re.compile(
    r"\b(?:how about you|and you|and yourself|what about you|how are you|how're you|how have you been|"
    r"how(?:'s| is) it going(?: with you)?|how do you feel|how are you feeling|how are you holding up|"
    r"you doing (?:ok|okay|alright|well)|how(?:'s| is) your day)(?:\s+(?:doing|today|tonight|now|"
    r"this (?:morning|afternoon|evening)))*\W*$", re.I)
_GREET = re.compile(r"^\W*(?:(?:hey|hi|hello|good (?:morning|afternoon|evening)|thanks|thank you)\W*)+"
                    r"(?:(?:there|othos|osos|otis|azis|azos)\W*)*$", re.I)
_WELL = re.compile(r"\b(?:well|good|great|fine|okay|ok|alright|not bad|wonderful|excellent|doing fine|can't complain)\b", re.I)
_NOT_WELL = re.compile(r"\b(?:tired|exhausted|sleepy|worn out|sick|ill|unwell|rough|bad|awful|terrible|stressed|"
                       r"sad|down|meh|so-so|not (?:great|good|well|so good|the best)|could be better|hurts?|"
                       r"headache|sore|achy|cold|grumpy|cranky|frustrated|anxious|overwhelmed)\b", re.I)


def asks_back(text: str) -> bool:
    """True for a short turn that is only (an answer about the user, and) a question about Othos.

    "I'm fine, how are you?", "Good, and you?", "How about you?", "How are you?" -> True.
    "I'm fine. Can you check the weather?" -> False (that is a real request; the model gets it).
    """
    text = (text or "").strip()
    if not text or len(text.split()) > 16 or not _RETURN.search(text):
        return False
    for s in re.split(r"(?<=[.!?])\s+|,\s+(?=(?:and|so|but|how)\b)", text):
        s = s.strip()
        if not s or _GREET.match(s) or _RETURN_END.search(s):
            continue
        # a short statement about how the user is
        if len(s.split()) <= 8 and "?" not in s and (_WELL.search(s) or _NOT_WELL.search(s)):
            continue
        return False
    return True


def user_state(text: str) -> str:
    """'unwell', 'well' or '' (the user didn't say)."""
    if re.search(r"\bnot bad\b", text, re.I):
        return "well"                     # "Not bad" is good news (it would match "bad" below)
    if _NOT_WELL.search(text):
        return "unwell"
    if _WELL.search(re.sub(r"\bhow (?:are|about|do)\b[^?]*\?", "", text, flags=re.I)):
        return "well"
    return ""


# ---------------------------------------------------------------- the check
def gather(ollama_root: str, model: str = "", *, workers: list[dict] | None = None, gaming: bool = False,
           camera: bool = False, health: str = "", starting: bool = False) -> dict:
    """Blocking (about 1 s at most); call it with asyncio.to_thread. Every part is optional: a part that
    can't be read is left out, never guessed. `health`: the conversation model's own server to ask instead of
    Ollama (llama-server's /health, llm_server.py)."""
    facts: dict = {"errors": _errors, "last_reply": _replies[-1] if _replies else None,
                   "gaming": gaming, "camera": camera, "workers": workers or []}
    try:
        r = httpx.get(health or f"{ollama_root}/api/ps", timeout=1.5)
        if r.status_code == 503 and health:
            facts["ollama"] = "starting"   # llama-server is up and still loading its model
        else:
            r.raise_for_status()
            facts["ollama"] = True    # (a model that isn't loaded right now is normal: Ollama loads it on demand)
    except Exception:
        # its program is running but not answering yet: still starting (Nov 20, 8:20 call: a call 10 s after Othos
        # started heard "I can't reach my local model", and the model later made up a "data center" problem)
        facts["ollama"] = "starting" if starting else False
    try:
        facts["memory"] = psutil.virtual_memory().percent
        facts["cpu"] = psutil.cpu_percent(interval=0.2)
    except Exception:
        pass
    try:
        facts["disk_free_gb"] = shutil.disk_usage(Path.home().anchor or "C:\\").free / 1e9
    except Exception:
        pass
    return facts


SLOW_REPLY_SECONDS = 40   # this CPU laptop's model can take 10-60 s to start answering: only "slow" past that


def mood(f: dict) -> str:
    """'trouble', 'slow' or 'good'."""
    if f.get("ollama") is False or f.get("errors", 0) >= 3:
        return "trouble"
    slow = f.get("ollama") == "starting" or (f.get("last_reply") or 0) > SLOW_REPLY_SECONDS \
        or (f.get("memory") or 0) > 90 or (f.get("cpu") or 0) > 90 \
        or (f.get("disk_free_gb") if f.get("disk_free_gb") is not None else 99) < 5
    return "slow" if slow else "good"


# ---------------------------------------------------------------- the words
_GOOD = ["Running smoothly, thanks for asking.", "In good shape, thanks.", "All systems are humming along.",
         "Feeling sharp, honestly. I checked."]
_OK_DETAIL = ["My local model is up and I haven't hit a single error on this call.",
              "Everything I can check is in order, and this call's been error-free.",
              "My local model is answering and there haven't been any errors."]
_SLOW = ["A bit sluggish, to be honest.", "Not at my quickest right now, honestly.", "Doing alright, though a little slow."]
_TROUBLE = ["Honestly, not at my best.", "A bit off, if I'm honest."]
_SORRY = {"unwell": ["Sorry to hear that.", "Ah, that's rough."], "well": ["Glad to hear it.", "Good to hear."], "": [""]}


def _busy(f: dict) -> str:
    names = [w["name"] for w in f.get("workers", []) if w.get("running") and w.get("working")]
    if not names:
        return ""
    if len(names) == 1:
        return f"{names[0]}'s busy on a task in the background."
    return f"{' and '.join(names)} are busy in the background."


# "Could you expand on that?" right after the self-check: answered from what the check saw, never by the model, which
# filled the gap with a made-up "trouble accessing the local data center" (Nov 20, 8:20 call)
_EXPAND = re.compile(r"\b(?:expand on|elaborate|explain|what do you mean|what did you mean|tell me more|more about|"
                     r"what(?:'s| is| was) (?:wrong|going on|the (?:problem|issue|matter))|why (?:is|was|are|were|did|"
                     r"can't|couldn't)|how come|what happened)\b", re.I)
_ABOUT_CHECK = re.compile(r"\b(?:local model|your model|language model|memory|slow|sluggish|reach(?:ing)?|"
                          r"not at your best|a bit off|processor|cpu|errors?|starting up|thinking)\b", re.I)
_POINTS_BACK = re.compile(r"\b(?:that|it|this)\b", re.I)


def asks_more(text: str, right_after: bool = False) -> bool:
    """A follow-up asking what the self-check meant: "You mentioned you couldn't reach your local model. Could you
    expand on that?" any time; a bare "What do you mean?" / "Can you explain that?" only as the reply to it."""
    t = text or ""
    if not _EXPAND.search(t):
        return False
    return bool(_ABOUT_CHECK.search(t) or (right_after and (_POINTS_BACK.search(t) or len(t.split()) <= 5)))


def explain(then: dict, now: dict) -> str:
    """The self-check's findings in plain words, from what it saw then and sees now (nothing guessed)."""
    out = []
    if then.get("ollama") == "starting" or (then.get("ollama") is False and now.get("ollama") is True):
        out.append("When you called, the program that runs my language model on this laptop was still starting up. "
                   "It takes about twenty seconds after Othos starts, longer when the computer is busy.")
    elif then.get("ollama") is False:
        out.append("The program that runs my language model on this laptop wasn't answering.")
    if now.get("ollama") is True:
        out.append("It's answering now.")
    elif now.get("ollama") == "starting":
        out.append("It's still starting.")
    elif now.get("ollama") is False:
        out.append("It still isn't answering. Restarting Othos with its button usually fixes that.")
    mem, cpu = now.get("memory"), now.get("cpu")
    if mem is not None and mem > 90:
        out.append(f"This computer's memory is {int(round(mem))} percent full, which slows everything down.")
    if cpu is not None and cpu > 60:
        out.append(f"Other programs have the processor at {int(round(cpu))} percent, so I think more slowly.")
    if (then.get("errors") or 0) >= 3:
        out.append(f"I'd also hit {then['errors']} speech or model errors on this call.")
    if not out:
        out.append("Everything I can check looks fine now.")
    return " ".join(out)


def describe(f: dict, user_said: str = "", rng: random.Random | None = None) -> str:
    """A short, spoken answer. Only things the check actually saw are mentioned."""
    rng = rng or random
    m = mood(f)
    state = user_state(user_said)
    parts = [rng.choice(_SORRY[state])] if state else []
    parts = [p for p in parts if p]
    if m == "good":
        parts.append(rng.choice(_GOOD))
        parts.append(rng.choice(_OK_DETAIL))
    elif m == "slow":
        parts.append(rng.choice(_SLOW))
        why = []
        if f.get("ollama") == "starting":
            why.append("my local model is still starting up, so give me a few seconds")
        if (f.get("last_reply") or 0) > SLOW_REPLY_SECONDS:
            why.append(f"my last answer took about {int(round(f['last_reply']))} seconds")
        if (f.get("memory") or 0) > 90:
            why.append("memory is running high")
        if (f.get("cpu") or 0) > 90:
            why.append("the processor is working hard")
        if f.get("disk_free_gb") is not None and f["disk_free_gb"] < 5:
            why.append("disk space is getting low")
        if why:
            parts.append(" and ".join(why[:2]).capitalize() + ".")
    else:
        parts.append(rng.choice(_TROUBLE))
        if f.get("ollama") is False:
            parts.append("I can't reach my local model right now, so my thinking may be off.")
        elif f.get("errors", 0) >= 3:
            parts.append(f"I've hit {f['errors']} speech or model errors on this call.")
    if f.get("gaming"):
        parts.append("I'm taking it easy because a game is on.")
    busy = _busy(f)
    if busy and m != "trouble":
        parts.append(busy)
    parts.append("What can I do for you?" if state else "How about you?")
    return " ".join(parts)
