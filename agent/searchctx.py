"""Search queries that keep the topic and the user's town (agent.py's web_search uses these).

Found in the Nov 16 call: "I'm looking for a butcher in my area. What are any good recommendations?" was searched
as "What are any good recommendations?" (TV shows came back), and the follow-up "Please look up good local options"
as "good local options" (restaurants in other cities). Fixes:
  1. a query with no topic of its own takes the topic from the user's earlier words ("a butcher");
  2. "near me" / "in my area" / "local" becomes "near <city, state>" (city and state only, from the user's own
     home setting; the same town name the weather search already sends);
  3. if nothing that came back mentions the topic, say so instead of reading it out.
Pure functions: no network, no files.
"""
from __future__ import annotations

import re

# words that say nothing about WHAT is being looked for
_GENERIC = set("""good best top any some local locally nearby near me my area around here options option recommendations
recommendation suggestions suggestion places place ones one where what are is the a an to go find look up please for can
you i in of there great nice decent closest nearest other more tell give about and or that this those thanks thank othos
it on at do does would could should can's whats what's where's some anything something somewhere help advice info
information ideas idea""".split())
_NOT_TOPIC = _GENERIC | {"help", "advice", "something", "anything", "it", "this", "that", "one", "more", "info"}

_LOCAL = re.compile(r"\b(?:in my area|near me|nearby|near here|around here|close to me|around me|in my town|"
                    r"in the area|locally|local|nearest|closest)\b", re.I)
_LOCAL_PHRASE = re.compile(r"\b(?:in my area|near me|nearby|near here|around here|close to me|around me|in my town|"
                           r"in the area)\b", re.I)
_PLACE = re.compile(r"\b(?:in|at|near|around)\s+[A-Z][\w'.-]+")   # a place already named ("near Springfield")

_END = r"(?=\s+(?:in|near|around|nearby|close|for|to|that|who|with|and|so|because|but|please|from)\b|[.,?!;]|$)"
_TOPIC_PATTERNS = [
    re.compile(r"\b(?:looking for|look for|searching for|search for|in need of|need|want|find(?: me)?|"
               r"where (?:can|do|could) (?:i|we) (?:find|get|buy)|hire|try)\s+(?:an?|the|some|any|good|best)?\s*"
               r"([a-z][a-z' -]{2,40}?)" + _END, re.I),
    re.compile(r"\b(?:good|best|top|nearest|closest)\s+([a-z][a-z' -]{2,30}?)" + _END, re.I),
]
_SYNONYMS = {"butcher": ["meat"], "baker": ["bakery"], "grocer": ["grocery"], "barber": ["hair"], "vet": ["animal"]}


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z']+", s.lower())


def is_generic(q: str) -> bool:
    """A query that says nothing about what is wanted ("What are any good recommendations?")."""
    return not [w for w in _words(q) if len(w) > 1 and w not in _GENERIC]


def wants_local(*texts: str) -> bool:
    return any(_LOCAL.search(t or "") for t in texts)


def says_near_me(text: str) -> bool:
    """"Find a butcher near me", "any coffee shops nearby": a place search whatever the opening words are."""
    return bool(_LOCAL_PHRASE.search(text or ""))


def city_state(home: str) -> str:
    """'Springfield, Greene County, MO, US' -> 'Springfield, MO' (city and state only)."""
    parts = [p.strip() for p in (home or "").split(",") if p.strip() and p.strip().upper() not in ("US", "USA")]
    if not parts:
        return ""
    state = next((p for p in reversed(parts[1:]) if re.fullmatch(r"[A-Z]{2}", p)), "")
    return f"{parts[0]}, {state}" if state else parts[0]


def topic_from(texts: list[str]) -> str:
    """What the user is looking for, from their words (the current turn first, then earlier turns, newest first)."""
    for text in texts:
        for sent in reversed([s for s in re.split(r"(?<=[.!?])\s+", text or "") if s]):
            for rx in _TOPIC_PATTERNS:
                for m in rx.finditer(sent):
                    topic = " ".join(m.group(1).split()).strip(" -'")
                    ws = _words(topic)
                    if ws and not all(w in _NOT_TOPIC for w in ws) and not (len(ws) == 1 and ws[0] in _NOT_TOPIC):
                        return topic
    return ""


def resolve(q: str, user: str, earlier: list[str], home: str) -> tuple[str, str]:
    """(query, topic). `topic` is set only when this module changed the query, so the relevance check applies.

    `user` is the whole turn, `earlier` the previous user turns (oldest first)."""
    loc = city_state(home)
    local = wants_local(user, q)
    topic = ""
    if is_generic(q):
        topic = topic_from([user, *reversed(earlier)])
        if not topic:
            return q, ""
        local = local or wants_local(*earlier[-3:])
        q = f"best {topic}"
    elif local and loc and not _PLACE.search(q):
        topic = topic_from([q, user])
    if local and loc and not _PLACE.search(q):
        if _LOCAL_PHRASE.search(q):
            q = _LOCAL_PHRASE.sub(f"near {loc}", q)
        else:
            q = re.sub(r"\b(?:local|locally)\b\s*", "", q, flags=re.I).strip() or q
            q = f"{q} near {loc}"
        q = re.sub(r"\s+", " ", q).strip()
    return q, topic


def relevant(topic: str, results: list[dict], answer: str | None = "") -> bool:
    """True if anything that came back mentions the topic (or if there's nothing to check it by)."""
    stems = []
    for w in _words(topic):
        if len(w) >= 4 and w not in _GENERIC:
            stems.append(w[:5])
            stems += [s[:5] for s in _SYNONYMS.get(w, [])]
    if not stems:
        return True
    blob = " ".join(f"{r.get('title', '')} {r.get('content', '')}" for r in results[:6]).lower() + " " + (answer or "").lower()
    return any(s in blob for s in stems)


def no_match_reply(topic: str, home: str) -> str:
    loc = city_state(home)
    where = f" near {loc}" if loc else ""
    return (f"I searched for {topic}{where}, but what came back was about other things, so I won't read it out. "
            f"Want me to try different words?")
