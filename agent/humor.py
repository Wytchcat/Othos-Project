"""Humor hints: what the person says about Othos's jokes, caught by code and kept for next time.

"Go easy on the puns", "I like the humor, keep it", "tone it down", "more jokes", "it's okay" (not a complaint).
The words are matched here, not by the model, so a hint always works the same way: it changes a small setting
(state\\humor.json: level 0-3, puns normal/fewer/none, keep), Othos says one short line back, and the personality
prompt reads the setting at the start of every call.

Soft hints ("that's not funny", "ha") are only counted for now; they never change the setting.
Only labels are kept (never the person's words). With OTHOS_HUMOR_LOG=1 each caught hint is also appended, as labels,
to state\\humor-events.jsonl: examples for later fine-tuning (see training\\README.md). Off by default.
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state" / "humor.json"
EVENTS = HERE / "state" / "humor-events.jsonl"
LEVEL_MIN, LEVEL_MAX, LEVEL_DEFAULT = 0, 3, 2
DEFAULTS = {"level": LEVEL_DEFAULT, "puns": "normal", "keep": False, "soft_less": 0, "soft_more": 0}
DIRECT = ("reset", "puns_none", "puns_fewer", "less", "more", "keep")

_HUM = r"(?:humou?r|jokes?|joking|sarcasm|sarcastic|wit|teasing)"
_I = re.I
_PATTERNS = [
    ("reset", re.compile(rf"\b(?:reset|go back to normal|back to normal|back to your usual)\b.{{0,24}}\b{_HUM}\b|"
                         rf"\b{_HUM}\b.{{0,18}}\b(?:back to normal|reset)\b", _I)),
    ("puns_none", re.compile(r"\bno (?:more )?puns\b|\bstop (?:with )?(?:the |all )?puns\b|\bnever (?:make|do|use) puns\b", _I)),
    ("puns_fewer", re.compile(r"\b(?:(?:go |be )?easy on|ease up on|lay off|cut back on|fewer|less|too many)\s+(?:the\s+|of\s+)?puns?\b", _I)),
    ("less", re.compile(rf"\btone (?:it|them|that|the {_HUM}) down\b|\bless (?:sarcas\w+|joking|jokes|teasing|humou?r)\b|"
                        rf"\bbe (?:a bit |a little |much )?(?:more )?serious\b|\bstop (?:joking|teasing|with the (?:jokes|sarcasm))\b|"
                        rf"\b(?:too|so) (?:sarcastic|silly)\b|\bnot so (?:sarcastic|jokey)\b|"
                        rf"\bi (?:don'?t|do not) (?:really )?(?:like|love|enjoy) (?:the |your )?{_HUM}\b", _I)),
    ("more", re.compile(rf"\bmore (?:jokes|humou?r|sarcasm|teasing)\b|\bbe (?:a bit |a little )?(?:funnier|more sarcastic|more playful)\b|"
                        r"\b(?:crack|make|tell) more jokes\b", _I)),
    ("keep", re.compile(rf"\b(?:i )?(?:really |do |still )?(?:like|love|enjoy) (?:the |your )?{_HUM}\b|"
                        rf"\bkeep (?:the|your) {_HUM}\b(?! to (?:yourself|myself))|"
                        rf"\bdon'?t (?:stop|lose) (?:the |your )?{_HUM}\b", _I)),
]
_KEEP_IT = re.compile(r"\b(?:please )?keep it(?: up| coming)?\b|\bkeep them coming\b", _I)   # only after Othos offered to stop joking
_PREV_HUMOR = re.compile(rf"\b{_HUM}\b", _I)
_SOFT_LESS = re.compile(r"\bnot funny\b|\bgroan\b|\bthat was (?:a )?(?:bad|terrible|awful) (?:one|joke|pun)\b|\bbad joke\b", _I)
_SOFT_MORE = re.compile(r"\bthat(?:'s| is| was) (?:really |pretty |so )?funny\b|\bgood one\b|\bha ?ha+\b|\blol\b", _I)
_REASSURE = re.compile(r"\b(?:it'?s|that'?s) (?:okay|ok|fine|alright)\b|\bno worries\b|\bno need to apologi[sz]e\b|"
                       r"\bthat'?s what you'?re supposed to do\b|\byou'?re fine\b", _I)


def catch(text: str, prev_assistant: str = "") -> list[str]:
    """The hint kinds in what the person just said (empty = none). prev_assistant: Othos's last line."""
    t = (text or "").strip()
    if not t:
        return []
    kinds = []
    for kind, rx in _PATTERNS:
        if rx.search(t) and not (kind == "keep" and "less" in kinds):
            kinds.append(kind)
    if "keep" not in kinds and "less" not in kinds and _KEEP_IT.search(t) and _PREV_HUMOR.search(prev_assistant or ""):
        kinds.append("keep")                       # "Please keep it" right after Othos offered to drop the humor
    if not kinds:
        if _SOFT_LESS.search(t):
            kinds.append("soft_less")
        elif _SOFT_MORE.search(t):
            kinds.append("soft_more")
    if _REASSURE.search(t):
        kinds.append("reassure")
    return kinds


def whole_hint(text: str, kinds: list[str]) -> bool:
    """True when the whole turn was the hint (short, not a question): Othos answers it by code."""
    words = re.findall(r"[\w']+", text or "")
    return bool(set(kinds) & set(DIRECT)) and len(words) <= 14 and "?" not in (text or "")


# ---------------------------------------------------------------- the setting
def load(path: Path | None = None) -> dict:
    try:
        d = json.loads((path or STATE).read_text(encoding="utf-8"))
        d = d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        d = {}
    out = {**DEFAULTS, **{k: d[k] for k in DEFAULTS if k in d}}
    out["level"] = max(LEVEL_MIN, min(LEVEL_MAX, int(out["level"]) if str(out["level"]).lstrip("-").isdigit() else LEVEL_DEFAULT))
    if out["puns"] not in ("normal", "fewer", "none"):
        out["puns"] = "normal"
    return out


def save(state: dict, path: Path | None = None) -> None:
    p = path or STATE
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


def apply(state: dict, kinds: list[str]) -> tuple[dict, str]:
    """(new state, the short line Othos says back). The line is '' when nothing needs saying."""
    s = dict(state)
    said = []
    if "reset" in kinds:
        s = dict(DEFAULTS)
        return s, "Back to my usual self."
    if "less" in kinds:
        if s["level"] <= LEVEL_MIN:
            said.append("I'm already about as plain as I get.")
        else:
            s["level"] -= 1
            said.append("Okay, dialling it back.")
    elif "more" in kinds:
        if s["level"] >= LEVEL_MAX:
            said.append("That's about as dry as I go.")
        else:
            s["level"] += 1
            said.append("Okay, a bit more of that.")
    if "puns_none" in kinds:
        s["puns"] = "none"
        said.append("No more puns, then.")
    elif "puns_fewer" in kinds:
        s["puns"] = "fewer"
        said.append("Fewer puns, got it.")
    if "keep" in kinds:
        s["keep"] = True
        said.append("Good, I'll keep it dry." if not said else "I'll keep the rest.")
        if len(said) == 2 and said[0].startswith("Fewer puns"):
            said = ["Good, I'll keep it dry, with fewer puns."]
    if "soft_less" in kinds:
        s["soft_less"] += 1
    if "soft_more" in kinds:
        s["soft_more"] += 1
    return s, " ".join(said)


def prompt_line(state: dict) -> str:
    """The sentence(s) added to the personality prompt for this setting ('' at the defaults)."""
    parts = []
    lvl = state["level"]
    if lvl == 0:
        parts.append("Be plain and warm: no jokes or sarcasm unless the user jokes first.")
    elif lvl == 1:
        parts.append("Keep the humor rare and very light.")
    elif lvl == 3:
        parts.append("Lean into the dry, teasing humor a little more.")
    if state["puns"] == "none":
        parts.append("Never make puns or wordplay.")
    elif state["puns"] == "fewer":
        parts.append("Avoid puns; use one only when it is really good.")
    if state["keep"]:
        parts.append("The user likes your humor: never offer to stop joking or apologise for a joke unless they ask.")
    return (" " + " ".join(parts)) if parts else ""


def log_event(kinds: list[str], state: dict) -> None:
    """Labels only, and only when OTHOS_HUMOR_LOG=1."""
    if os.getenv("OTHOS_HUMOR_LOG", "") != "1" or not kinds:
        return
    EVENTS.parent.mkdir(exist_ok=True)
    rec = {"at": datetime.now().strftime("%Y-%m-%d %H:%M"), "hints": kinds, "level": state["level"], "puns": state["puns"]}
    with open(EVENTS, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
