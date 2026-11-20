"""Things the user asked Othos to hold on to, kept for every later call: state\\held.json (stays on this PC).

1. A PERMANENT disability. "I'm disabled" can mean a bad week, so it is never assumed to be permanent;
   "I'm permanently disabled" / "I have a permanent disability" is caught by code (no model), kept, and from then on
   the chat instructions say to keep it in mind quietly and never bring it up. "Forget that I have a permanent
   disability" clears it.
2. Preferred pronouns, one set or several ("she/her", "she/they", "xe/xem", "fae/faer"). Setup asks right after the
   name; if it is skipped Othos never asks by voice, but the person can say "my pronouns are ..." or "I'm a she" at
   any time, and "forget my pronouns" clears them. "I'm nonbinary" with no pronouns known: Othos asks once which to
   use (the person brought it up). Without them Othos says "they" and never guesses from a name.
3. Names, and no deadnaming: "my name is now Jo", "don't call me Joe", "Michael is my deadname". The current name is
   used; old names go on a never-say list that is NEVER put into the model's instructions (that would only remind
   it). scrub() swaps them out of everything Othos says and shows, whatever the source.
4. The people the user talks about: "my friend Sam said she...", "Sam's pronouns are they/them", "Alex is now
   Jordan". Only a name, its pronouns and its never-say names are kept. What the user said outranks what code
   noticed from how they talk. "Forget Sam" clears that person.
"""
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state" / "held.json"
DEFAULTS = {"permanent_disability": False, "pronouns": [], "asked_pronouns": False, "name": "", "never_say": [],
            "people": {}}

_I = re.I
_PERMANENT = re.compile(
    r"\bi(?:'m| am)\s+permanently\s+disabled\b|\bi(?:'ve| have) been\s+permanently\s+disabled\b|"
    r"\bi(?:'m| am)\s+disabled\s+permanently\b|"
    r"\bi\s+(?:have|got|live with|am living with)\s+(?:a\s+)?permanent\s+disabilit(?:y|ies)\b|"
    r"\bmy\s+disabilit(?:y|ies)\s+(?:is|are)\s+permanent\b", _I)
_NEGATED = re.compile(r"\b(?:not|never|no longer)\b|n't", _I)
_FORGET = re.compile(r"\bforget\b[^.!?]{0,40}\bpermanent(?:ly\s+disabled|\s+disabilit(?:y|ies))\b", _I)
_FORGET_PRONOUNS = re.compile(r"\bforget\b[^.!?]{0,30}\bpronouns?\b", _I)

# "my pronouns are she and her" (strong: anything of the form a/b is accepted) or "I use she/they",
# "I go by xe/xem", "call me they/them" (weak: only the known sets)
_STRONG = re.compile(r"\bmy\s+pronouns?\s+(?:are|is)\s+([^.!?]*)", _I)
_WEAK = re.compile(r"\b(?:i\s+(?:use|go by|prefer)|(?:please\s+)?(?:call|refer to)\s+me)\s+([^.!?]*)", _I)
_SELF = re.compile(r"\bi(?:'m| am) an? (she|he|they)\b|\buse (she|he|they)(?:/[a-z]+)? for me\b", _I)
_IDENTITY = re.compile(r"\bi(?:'m| am) (?:an? )?(?:non-?binary|enby|genderfluid|genderqueer|agender|"
                       r"trans(?:gender)?(?: (?:woman|man|person|guy|girl))?)\b", _I)
_SETS = {"she": "she/her", "he": "he/him", "they": "they/them", "it": "it/its", "xe": "xe/xem", "ze": "ze/hir"}
_OBJECTS = {"her", "him", "them", "its", "xem", "hir"}
_GLUE = {"and", "or", "also", "both", "&"}

# a name: one capitalised word ("Jo", "Mary-Ann", "O'Neil"), never one of these
_N = r"[A-Z][a-z]+(?:[-'][A-Z]?[a-z]+)?"
_NOT_NAMES = {
    "I", "The", "A", "An", "My", "Her", "His", "Their", "She", "He", "They", "It", "We", "You", "This", "That", "Please",
    "Actually", "Hi", "Hello", "Hey", "Good", "Okay", "Ok", "Yes", "No", "And", "But", "So", "Well", "Othos", "Now",
    "Today", "Tomorrow", "Tonight", "Yesterday", "Later", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
    "Saturday", "Sunday", "January", "February", "March", "April", "May", "June", "July", "August", "September",
    "October", "November", "December", "Also", "Then", "Just", "Maybe", "Oh", "Mr", "Mrs", "Ms", "Dr", "Mom", "Dad",
    "Thanks", "Thank", "Sorry", "Again", "Not", "Never", "Don't", "Do", "Is", "Was", "Are", "Were", "What", "Why",
    "How", "When", "Where", "Who", "Which", "If", "Or", "Because", "Since", "After", "Before", "Sure", "Bus", "Car"}
_REL = (r"(?:best )?(?:friend|sister|brother|sibling|partner|wife|husband|spouse|mom|mum|mother|dad|father|parent|son|"
        r"daughter|kid|child|cousin|aunt|uncle|niece|nephew|boss|coworker|co-worker|neighbou?r|roommate|grandma|"
        r"grandpa|grandmother|grandfather|girlfriend|boyfriend|grandkid|grandson|granddaughter|stepson|stepdaughter)")
_MY_NEW = re.compile(rf"\b(?i:my name is now|my name's now|my new name is|my name is|i go by|i'm going by|"
                     rf"please call me|call me)\s+(?P<n>{_N})\b(?!/)")
_MY_OLD = [re.compile(rf"\b(?i:don'?t call me|do not call me|please don'?t use|i used to be called|i used to be named|"
                      rf"my old name (?:was|is)|my dead ?name (?:was|is)|my birth name (?:was|is))\s+(?P<n>{_N})\b"),
           re.compile(rf"\b(?P<n>{_N})\s+(?i:is|was)\s+my\s+(?i:old name|dead ?name|birth name)\b")]
_P_PRON_STRONG = re.compile(rf"\b(?P<n>{_N})(?:'s| has)\s+(?i:pronouns are)\s+(?P<rest>[^.!?]*)")
_P_PRON_WEAK = re.compile(rf"\b(?P<n>{_N})\s+(?i:uses|goes by|is an?)\s+(?P<rest>[^.!?]*)")
_P_RENAME = [re.compile(rf"\b(?P<old>{_N})(?:'s name)?\s+(?i:is now|goes by|is going by)\s+(?P<new>{_N})\b(?!/)"),
             re.compile(rf"\b(?P<new>{_N})\s+(?i:used to be called|used to be named|used to be)\s+(?P<old>{_N})\b")]
_REL_OLD = re.compile(rf"\b(?i:my)\s+(?i:{_REL})\s+(?i:used to be called|used to be named|used to be)\s+(?P<old>{_N})\b")
_THEY_NEW = re.compile(rf"\b(?i:she|he|they)\s+(?i:goes|go|is going|are going)\s+by\s+(?P<new>{_N})\b|"
                       rf"\b(?i:her|his|their)\s+(?i:name is now|new name is)\s+(?P<new2>{_N})\b")
_REL_NAME = re.compile(rf"\b(?i:my)\s+(?i:{_REL})\s+(?P<n>{_N})\b")
_SUBJECT = re.compile(r"\b(she|he|they)\b", _I)
_FORGET_PERSON = re.compile(rf"\b(?i:forget)\s+(?i:about\s+)?(?P<n>{_N})\b")


def _name_ok(n: str | None) -> bool:
    return bool(n) and n not in _NOT_NAMES


# for agent.py's guest mode (the same idea of a name and a relation as here)
NAME_RX = _N
RELATION_RX = _REL
name_ok = _name_ok


def _pronoun_sets(rest: str, strong: bool) -> list[str]:
    """['she/her', 'they/them'] from "she and they", "she/they", "xe/xem"; [] when the words aren't just pronouns."""
    out: list[str] = []
    words = re.findall(r"[a-z]+(?:/[a-z]+)?|&", rest.lower())
    if not words:
        return []
    for w in words:
        if w in _GLUE or w in _OBJECTS:
            continue
        if "/" in w:
            a, b = w.split("/")
            if b in _SETS:                              # she/they, he/they
                out += [_SETS.get(a, f"{a}/{a}"), _SETS[b]] if a in _SETS else []
            elif a in _SETS and b in _OBJECTS:          # she/her, he/him, they/them
                out.append(_SETS[a])
            elif strong and a != b:                     # fae/faer: typed by the person
                out.append(f"{a}/{b}")
            else:
                return []
        elif w in _SETS:
            out.append(_SETS[w])
        else:
            return []                                   # "it for work", "Linux", "Becky": not pronouns
    return list(dict.fromkeys(out))


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def _people_said(text: str, state: dict) -> list[str]:
    """What the user said about other people: renames, pronouns said, pronouns noticed, forgetting."""
    people = state.get("people", {})
    kinds: list[str] = []
    m = _FORGET_PERSON.search(text)
    if m and m.group("n").lower() in people:
        return [f"forget_person:{m.group('n')}"]
    # "My sister used to be called Mark. She goes by Marie now."
    m, n = _REL_OLD.search(text), _THEY_NEW.search(text)
    if m and n and _name_ok(new := n.group("new") or n.group("new2")) and _name_ok(m.group("old")):
        kinds.append(f"person_name:{new}|{m.group('old')}")
    for rx in _P_RENAME:
        for m in rx.finditer(text):
            if _name_ok(m.group("old")) and _name_ok(m.group("new")) and m.group("old") != m.group("new"):
                kinds.append(f"person_name:{m.group('new')}|{m.group('old')}")
    for m in _P_PRON_STRONG.finditer(text):
        if _name_ok(m.group("n")) and (sets := _pronoun_sets(m.group("rest"), True)):
            kinds.append(f"person_pron:{m.group('n')}|{','.join(sets)}|said")
    for m in _P_PRON_WEAK.finditer(text):
        if _name_ok(m.group("n")) and (sets := _pronoun_sets(m.group("rest"), False)):
            kinds.append(f"person_pron:{m.group('n')}|{','.join(sets)}|said")
    if any(k.startswith("person_pron:") for k in kinds):
        return kinds
    # noticed from how the user talks: "My friend Sam said she would come" (one person in the sentence only)
    for s in _sentences(text):
        intro = {m.group("n") for m in _REL_NAME.finditer(s) if _name_ok(m.group("n"))}
        words = re.findall(_N, s)
        first = s.split()[0] if s.split() else ""
        names = {w for w in words if _name_ok(w) and (w in intro or w.lower() in people or w != first.strip(",.!?"))}
        if len(names) != 1:
            continue
        who = names.pop()
        if not (who in intro or who.lower() in people):
            continue
        after = s[s.index(who) + len(who):]
        if p := _SUBJECT.search(after):
            kinds.append(f"person_pron:{who}|{_SETS[p.group(1).lower()]}|used")
    return kinds


def catch(text: str, state: dict | None = None) -> list[str]:
    """What the user just asked to have held, in order. Kinds: 'permanent_disability', 'forget_permanent_disability',
    'pronouns:she/her,they/them', 'forget_pronouns', 'ask_pronouns', 'name:Jo', 'never:Joe',
    'person_name:New|Old', 'person_pron:Name|sets|said or used', 'forget_person:Name'."""
    state = _norm(state or {})
    kinds: list[str] = []
    if _FORGET.search(text):
        kinds.append("forget_permanent_disability")
    else:
        for m in _PERMANENT.finditer(text):
            sentence = re.split(r"(?<=[.!?])\s", text[:m.end()])[-1]
            if not _NEGATED.search(sentence):
                kinds.append("permanent_disability")
                break
    if _FORGET_PRONOUNS.search(text):
        kinds.append("forget_pronouns")
    else:
        found = ""
        for rx, strong in ((_STRONG, True), (_WEAK, False)):
            m = rx.search(text)
            if m and (sets := _pronoun_sets(m.group(1), strong)):
                found = "pronouns:" + ",".join(sets)
                break
        if not found and (m := _SELF.search(text)):
            found = "pronouns:" + _SETS[(m.group(1) or m.group(2)).lower()]
        if found:
            kinds.append(found)
        elif _IDENTITY.search(text) and not state["pronouns"] and not state["asked_pronouns"]:
            kinds.append("ask_pronouns")
    for rx in _MY_OLD:
        for m in rx.finditer(text):
            if _name_ok(m.group("n")):
                kinds.append(f"never:{m.group('n')}")
    for m in _MY_NEW.finditer(text):
        before = text[:m.start()].lower()
        if _name_ok(m.group("n")) and not re.search(r"(?:don'?t|do not|never)\s*$", before):
            kinds.append(f"name:{m.group('n')}")
    if not any(k.startswith(("name:", "never:")) for k in kinds):
        kinds += _people_said(text, state)
    return kinds


def _norm(d: dict) -> dict:
    """A clean copy with every key, whatever shape the file or caller gave."""
    def names(v):
        return [x for x in v if isinstance(x, str) and x] if isinstance(v, list) else []
    people = {}
    for k, p in (d.get("people") or {}).items() if isinstance(d.get("people"), dict) else []:
        if isinstance(p, dict) and isinstance(p.get("name"), str) and p["name"]:
            people[str(k).lower()] = {"name": p["name"], "pronouns": names(p.get("pronouns")),
                                      "never_say": names(p.get("never_say")), "said": p.get("said") is True}
    return {"permanent_disability": d.get("permanent_disability") is True, "pronouns": names(d.get("pronouns")),
            "asked_pronouns": d.get("asked_pronouns") is True,
            "name": d["name"] if isinstance(d.get("name"), str) else "", "never_say": names(d.get("never_say")),
            "people": people}


def load(path: Path | None = None) -> dict:
    try:
        d = json.loads((path or STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    return _norm(d if isinstance(d, dict) else {})


def save(state: dict, path: Path | None = None) -> None:
    p = path or STATE
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _say(sets: list[str]) -> str:
    return sets[0] if len(sets) == 1 else ", ".join(sets[:-1]) + " and " + sets[-1]


def apply(state: dict, kinds: list[str]) -> tuple[dict, str]:
    """(new state, the short line Othos says back). The line is '' when nothing changed."""
    s = _norm(state)
    said = []
    for kind in kinds:
        head, _, arg = kind.partition(":")
        if head == "forget_permanent_disability" and s["permanent_disability"]:
            s["permanent_disability"] = False
            said.append("Okay, that's forgotten.")
        elif head == "permanent_disability" and not s["permanent_disability"]:
            s["permanent_disability"] = True
            said.append("Understood. I'll keep that in mind and won't keep bringing it up.")
        elif head == "forget_pronouns" and s["pronouns"]:
            s["pronouns"] = []
            said.append("Okay, my pronouns note is forgotten.")
        elif head == "pronouns":
            sets = arg.split(",")
            if sets != s["pronouns"]:
                s["pronouns"] = sets
                said.append(f"Got it. I'll use {_say(sets)}.")
        elif head == "ask_pronouns" and not s["pronouns"] and not s["asked_pronouns"]:
            s["asked_pronouns"] = True
            said.append("Thanks for telling me. Which pronouns would you like me to use for you?")
        elif head == "name" and arg != s["name"]:
            s["name"] = arg
            s["never_say"] = [x for x in s["never_say"] if x != arg]
            said.append(f"Got it, {arg}.")
        elif head == "never" and arg not in s["never_say"] and arg != s["name"]:
            s["never_say"].append(arg)
            said.append("Understood. I won't use that name.")
        elif head == "person_name":
            new, old = arg.split("|")
            p = s["people"].pop(old.lower(), None) or s["people"].get(new.lower()) or \
                {"name": new, "pronouns": [], "never_say": [], "said": False}
            p["name"] = new
            p["never_say"] = [x for x in dict.fromkeys([*p["never_say"], old]) if x != new]
            if new.lower() not in s["people"]:
                said.append(f"Got it, {new}.")
            s["people"][new.lower()] = p
        elif head == "person_pron":
            who, sets_s, how = arg.split("|")
            sets = sets_s.split(",")
            p = s["people"].get(who.lower()) or {"name": who, "pronouns": [], "never_say": [], "said": False}
            if how == "said" or not p["said"]:
                if p["pronouns"] != sets and how == "said":
                    said.append(f"Got it. {who} uses {_say(sets)}.")
                p["pronouns"] = sets
                p["said"] = p["said"] or how == "said"
            s["people"][who.lower()] = p
        elif head == "forget_person" and arg.lower() in s["people"]:
            del s["people"][arg.lower()]
            said.append(f"Okay, {arg} is forgotten.")
    return s, " ".join(said)


def current_name(state: dict) -> str:
    """The name the user asked to be called ('' when they haven't said)."""
    return _norm(state)["name"]


def prompt_line(state: dict) -> str:
    """The sentences added to the chat instructions for what is held ('' when nothing is). Old names never appear."""
    s = _norm(state)
    out = ""
    if s["permanent_disability"]:
        out += (" The user has told you they have a permanent disability. Keep it in mind quietly: never bring it up, "
                "never ask about it, never suggest it will get better or that they rest until it does, and never "
                "suggest going out or moving; speak of it only if they do.")
    if s["name"]:
        out += f" The user's name is {s['name']}."
    if s["pronouns"]:
        out += (f" The user's pronouns are {_say(s['pronouns'])}. Use them (either, naturally) when you speak "
                "about the user, and never guess anyone's pronouns from a name.")
    people = [f"{p['name']} ({_say(p['pronouns'])})" if p["pronouns"] else p["name"] for p in s["people"].values()]
    if people:
        out += (f" People the user has told you about: {', '.join(people)}. Use those names and pronouns for them, "
                "and never guess anyone's pronouns from a name.")
    return out


# ---------------------------------------------------------------- the never-say filter (speech and transcript)
def _swaps(state: dict) -> list[tuple[re.Pattern, str]]:
    s = _norm(state)
    out = []
    for old in s["never_say"]:
        if s["name"]:
            out.append((re.compile(rf"(?<![\w]){re.escape(old)}(?![\w])"), s["name"]))
        else:   # no new name known: drop the old one ("Good afternoon, Joe." -> "Good afternoon.")
            out.append((re.compile(rf"(?:,\s*|\s+)?(?<![\w]){re.escape(old)}(?![\w])(?:,\s*)?"), ""))
    for p in s["people"].values():
        for old in p["never_say"]:
            out.append((re.compile(rf"(?<![\w]){re.escape(old)}(?![\w])"), p["name"]))
    return out


def _old_names(state: dict) -> list[str]:
    s = _norm(state)
    return s["never_say"] + [old for p in s["people"].values() for old in p["never_say"]]


def _swap(text: str, swaps: list[tuple[re.Pattern, str]]) -> str:
    for rx, new in swaps:
        text = rx.sub(new, text)
    return text


def scrub_text(text: str, state: dict) -> str:
    """One whole text with never-say names swapped out (the chat instructions, built from profile.md and
    personal.json, which may still hold an old name)."""
    return _swap(text, _swaps(state))


async def scrub(stream, state: dict):
    """Othos's text stream with never-say names swapped out. Pieces are held back a word or so, so a name split
    across pieces ("Mich" + "ael") is still caught. Word-timed pieces (TimedString) are swapped one by one and keep
    their timing. With nothing on the list the stream passes through untouched."""
    swaps = _swaps(state)
    if not swaps:
        async for piece in stream:
            yield piece
        return
    keep = 1 + max(len(old.split()) for old in _old_names(state))   # whole words held back behind the partial one
    buf = ""
    async for piece in stream:
        if hasattr(piece, "start_time"):           # a TimedString: one word with its timing
            if buf:
                yield _swap(buf, swaps)
                buf = ""
            yield _retime(piece, _swap(str(piece), swaps))
            continue
        buf += piece
        spaces = [m.start() for m in re.finditer(r"\s", buf)]
        if len(spaces) > keep:
            cut = spaces[-keep - 1] + 1
            if m := re.search(r"[,\s]+$", buf[:cut]):   # the comma before a dropped name goes with it
                cut = m.start()
            if cut <= 0:
                continue
            yield _swap(buf[:cut], swaps)
            buf = buf[cut:]
    if buf:
        yield _swap(buf, swaps)


def _retime(piece, text: str):
    """A TimedString with new text and the same timing."""
    return type(piece)(text, start_time=piece.start_time, end_time=piece.end_time, confidence=piece.confidence,
                       start_time_offset=piece.start_time_offset, speaker_id=piece.speaker_id)
