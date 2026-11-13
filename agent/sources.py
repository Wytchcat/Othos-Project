"""Free sources Othos tries before Tavily (Nov 10, 2026).

the user's plan (Documents\\Othos_future\\research sources plan.md): search credits
are limited, so free APIs and feeds come first and Tavily only when they have
nothing. Brave will sit between them later (tabled for now).

Only search words, a game name, or the town's map point leave the laptop; never
anything personal. Keys come from .env (RAVELRY_USER / RAVELRY_KEY, YOUTUBE_API_KEY,
TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET). Every function returns [] / None on any
failure so the caller can fall back to Tavily.

Results use the same shape as Tavily's: {"title", "url", "content", "published_date"}.
"""
from __future__ import annotations

import asyncio
import base64
import html
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime

import httpx

import personal  # the user's town, feeds and contact link: personal.json

log = logging.getLogger("othos.sources")

# NWS asks automated clients to identify themselves; a generic name, no contact details
UA = "Othos personal voice assistant (private, non-commercial)"
_http: httpx.AsyncClient | None = None


_http_loop = None  # the event loop _http was made on
_SSL = None  # the certificate list, read once (about 0.5-2.5 s of CPU each time otherwise)


def ssl_context():
    """One shared SSL context; agent.py's model client uses it too."""
    global _SSL
    if _SSL is None:
        import ssl
        import certifi  # what httpx uses by default
        _SSL = ssl.create_default_context(cafile=certifi.where())
    return _SSL


def _client() -> httpx.AsyncClient:
    # Each call runs on its own event loop, and a client from an earlier call's (closed)
    # loop fails with "Event loop is closed" (Nov 12: second call's NWS weather fell back
    # to Tavily). So a new loop gets a new client.
    global _http, _http_loop
    loop = asyncio.get_running_loop()
    if _http is None or _http.is_closed or _http_loop is not loop:
        _http = httpx.AsyncClient(timeout=6.0, follow_redirects=True, headers={"User-Agent": UA},
                                  verify=ssl_context())
        _http_loop = loop
    return _http


async def _get(url: str, **kw):
    """GET -> response, or None (logged) on any error."""
    try:
        r = await _client().get(url, **kw)
        r.raise_for_status()
        return r
    except Exception as e:
        log.info("source %s failed: %s", url.split("?")[0], e)
        return None


def _plain(s: str, limit: int = 0) -> str:
    """HTML -> plain words for speech (code blocks dropped)."""
    s = re.sub(r"<pre[\s\S]*?</pre>|<code>[\s\S]{40,}?</code>", " ", s or "", flags=re.I)
    s = re.sub(r"<br\s*/?>|</p>|</li>", ". ", s, flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"(?:\.\s*){2,}", ". ", s)
    if limit and len(s) > limit:
        cut = s[:limit]
        s = cut[:cut.rfind(". ") + 1] if ". " in cut[limit // 2:] else cut.rsplit(" ", 1)[0]
    return s


_STOP = set("""a an the and or of to in on for with about from at by is are was were be been do does did
    how what what's whats which who whom why when where can could should would will i me my we our you your
    it its this that these those there any some get got make best way ways good need want use using tell
    know please really just also than then into out up much many often long last""".split())


def keywords(q: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9+#.'-]*", q.lower()) if w not in _STOP and len(w) > 1]


# ---------------------------------------------------------------- topics
# Which free sources fit a question. Stack Exchange site names are their API ids.
TOPICS = {
    "crafts": {
        "rx": re.compile(r"\b(?:knit\w*|crochet\w*|cross[- ]?stitch\w*|embroider\w*|needlepoint|yarn|skeins?|"
                         r"purl|amigurumi|granny squares?|cast(?:ing)? on|bind(?:ing)? off|aida|floss|dmc)\b", re.I),
        "se": ["crafts"], "where": "the Stack Exchange crafts forum",
    },
    "offgrid": {
        "rx": re.compile(r"\b(?:off[- ]?grid|solar|photovoltaic|pv (?:panel|system|array)s?|inverters?|"
                         r"charge controllers?|battery banks?|lifepo4|deep[- ]cycle|homestead\w*|rainwater|"
                         r"composting toilets?|wood ?stoves?|generators?|well pumps?|grey ?water|gray ?water)\b", re.I),
        "se": ["sustainability", "diy"], "where": "the Stack Exchange home improvement and sustainable living forums",
    },
    "joomla": {
        "rx": re.compile(r"\bjoomla\b", re.I),
        "se": ["joomla"], "where": "the Stack Exchange Joomla forum",
    },
    "streaming": {
        "rx": re.compile(r"\b(?:live ?stream\w*|streamers?|streaming (?:setup|software|on|to)|twitch|obs\b|obs studio|"
                         r"streamlabs|streamelements|go(?:ing)? live|bitrate|encoder|nvenc|x264|overlays?|"
                         r"capture card|stream deck|tiktok live|youtube live|facebook live|restream)\b", re.I),
        "se": ["video", "superuser"], "where": "the Stack Exchange video and tech forums",
    },
    "hardware": {  # the user, Nov 10
        "rx": re.compile(r"\b(?:gpus?|graphics cards?|video cards?|cpus?|processors?|motherboards?|mobo|ram|ddr[45]|"
                         r"ssds?|nvme|m\.2|hard drives?|psus?|power supply|pc builds?|build(?:ing)? a pc|gaming pc|"
                         r"rtx ?\d{3,4}\w*|gtx ?\d{3,4}|radeon|rx ?\d{4}|ryzen|threadripper|intel core|core i\d|"
                         r"cpu coolers?|aio|liquid cool\w*|case fans?|thermal paste|overclock\w*|bios|uefi|"
                         r"pcie|vram|bottleneck\w*|benchmarks?|tom'?s hardware)\b", re.I),
        "se": ["hardwarerecs", "superuser"], "where": "the Stack Exchange hardware and tech forums",
    },
    "dji": {
        "rx": re.compile(r"\b(?:dji|osmo|pocket 3|mavic|avata|ronin|drones?|gimbals?|quadcopters?)\b", re.I),
        "se": ["drones", "video"], "where": "the Stack Exchange drone and video forums",
    },
}


def topic_of(text: str) -> str | None:
    for name, t in TOPICS.items():
        if t["rx"].search(text or ""):
            return name
    return None


# ---------------------------------------------------------------- Stack Exchange
async def stack_exchange(q: str, sites: list[str], n: int = 3) -> list[dict]:
    """Questions like the user's, each with its best answer (accepted, else top-voted).
    Free, no key (300 requests a day per address)."""
    kw = keywords(q)
    if not kw:
        return []
    # most of the user's words must be in the question's title ("solar" alone matched everything);
    # word stems count ("streaming" ~ "stream", "controllers" ~ "controller")
    need = len(kw) if len(kw) <= 3 else -(-len(kw) * 3 // 5)  # all of up to 3 words, else 60%

    def covered(title: str) -> int:
        words = re.findall(r"[a-z0-9+#.'-]+", html.unescape(title).lower())
        return sum(any(t.startswith(w[:5]) or w.startswith(t[:5]) for t in words if len(t) > 2) for w in kw)

    out = []
    for site in sites[:2]:
        # "similar" ranks questions by how alike their titles are; plain search ranked a
        # typo question first for "how to cast on"
        r = await _get("https://api.stackexchange.com/2.3/similar", params={
            "order": "desc", "sort": "relevance", "title": q, "site": site, "pagesize": 10})
        if r is None:
            continue
        qs = [x for x in r.json().get("items", []) if x.get("is_answered") and x.get("score", 0) >= 0]
        # and the question mustn't be mostly about something else: "how to cast on" matched
        # "Second row of knitting pattern has less stitches than cast on", and the model then
        # made up cast-on steps (Nov 10 test call)
        def alike(title: str) -> bool:
            tkw = keywords(html.unescape(title))
            hits = sum(any(t.startswith(w[:5]) or w.startswith(t[:5]) for w in kw) for t in tkw if len(t) > 2)
            return bool(tkw) and hits / len(tkw) >= 0.4
        qs = [x for x in qs if covered(x.get("title", "")) >= need and alike(x.get("title", ""))][:n]
        if not qs:
            continue
        ids = ";".join(str(x["question_id"]) for x in qs)
        a = await _get(f"https://api.stackexchange.com/2.3/questions/{ids}/answers", params={
            "order": "desc", "sort": "votes", "site": site, "filter": "withbody", "pagesize": 30})
        if a is None:
            continue
        best = {}
        for ans in a.json().get("items", []):
            qid = ans["question_id"]
            key = (ans.get("is_accepted", False), ans.get("score", 0))
            if qid not in best or key > best[qid][0]:
                best[qid] = (key, ans)
        for x in qs:
            if x["question_id"] in best and best[x["question_id"]][1].get("score", 0) >= 1:
                ans = best[x["question_id"]][1]
                out.append({"title": html.unescape(x["title"]), "url": x["link"],
                            "content": _plain(ans.get("body", ""), 600), "site": site,
                            "votes": ans.get("score", 0)})
        if len(out) >= n:
            break
    log.info("stack exchange %s %r: %d answers", sites, " ".join(kw), len(out))
    return out[:n]


# ---------------------------------------------------------------- Wikipedia
_NOT_ENCYCLOPEDIA = re.compile(r"\b(?:current|currently|now|today|latest|this year|president|ceo|governor|mayor|"
                               r"prime minister|leader|champion|winner|price|cost|weather|score|news)\b", re.I)


async def wikipedia(subject: str) -> dict | None:
    """The opening of the Wikipedia article on a subject, only when the article is
    clearly about it (its title contains all the user's words). Free, no key."""
    # Wikimedia refuses programs that don't give a contact link (403 "respect our robot
    # policy"); without contact_url in personal.json (or OTHOS_CONTACT_URL), Wikipedia is skipped
    contact = personal.contact_url()
    subject = re.sub(r"^(?:a|an|the)\s+", "", subject.strip(" ?.!"), flags=re.I)
    if not contact or not subject or len(subject.split()) > 6 or _NOT_ENCYCLOPEDIA.search(subject):
        return None
    wk = {"User-Agent": f"Othos/0.9 (personal voice assistant; {contact})"}
    r = await _get("https://en.wikipedia.org/w/api.php", headers=wk, params={
        "action": "query", "list": "search", "srsearch": subject, "srlimit": 3, "format": "json"})
    if r is None:
        return None
    want = keywords(subject) or [subject.lower()]
    for hit in r.json().get("query", {}).get("search", []):
        title = hit["title"]
        words = set(re.findall(r"[a-z0-9'-]+", title.lower()))
        if not all(w.rstrip("s") in {t.rstrip("s") for t in words} for w in want):
            continue
        s = await _get("https://en.wikipedia.org/api/rest_v1/page/summary/" + title.replace(" ", "_"), headers=wk)
        if s is None:
            return None
        j = s.json()
        if j.get("type") == "disambiguation" or not j.get("extract"):
            return None
        log.info("wikipedia %r -> %r", subject, title)
        return {"title": title, "url": j.get("content_urls", {}).get("desktop", {}).get("page", ""),
                "content": j["extract"]}
    return None


# ---------------------------------------------------------------- Ravelry
_PATTERN_ASK = re.compile(r"\bpatterns?\b|\bsomething to (?:knit|crochet)\b|\bproject ideas?\b|"
                          r"\b(?:what|anything) (?:should|could|can) i (?:knit|crochet|make)\b", re.I)


def wants_patterns(text: str) -> bool:
    return bool(_PATTERN_ASK.search(text or "")) and bool(re.search(r"\b(?:knit\w*|crochet\w*|yarn)\b", text or "", re.I))


async def ravelry_patterns(q: str, n: int = 4) -> list[dict]:
    """Popular Ravelry patterns for a search, with craft, difficulty and whether free."""
    user = os.getenv("RAVELRY_USER", "")
    key = os.getenv("RAVELRY_KEY") or os.getenv("RAVERLY_KEY", "")  # .env spells it RAVERLY_KEY
    words = [w for w in keywords(q) if w not in ("pattern", "patterns", "ravelry", "ideas", "idea", "project",
                                                   "projects", "something", "knit", "crochet", "knitting",
                                                   "crocheting", "make")]
    craft = "crochet" if re.search(r"crochet", q, re.I) else "knitting" if re.search(r"knit", q, re.I) else None
    if not (user and key):
        return []
    auth = {"Authorization": "Basic " + base64.b64encode(f"{user}:{key}".encode()).decode()}
    params = {"query": " ".join(words), "page_size": n, "sort": "best"}
    if craft:
        params["craft"] = craft
    if re.search(r"\bfree\b", q, re.I):
        params["availability"] = "free"
    if re.search(r"\b(?:beginner|easy|simple)\b", q, re.I):
        params["diff"] = "1-3"
    r = await _get("https://api.ravelry.com/patterns/search.json", params=params, headers=auth)
    if r is None:
        return []
    found = r.json().get("patterns", [])
    if not found:
        return []
    d = await _get("https://api.ravelry.com/patterns.json",
                   params={"ids": " ".join(str(p["id"]) for p in found)}, headers=auth)
    details = d.json().get("patterns", {}) if d is not None else {}
    out = []
    for p in found:
        full = details.get(str(p["id"]), {})
        diff = full.get("difficulty_average") or 0
        level = "easy" if 0 < diff < 3 else "intermediate" if diff < 5.5 and diff else "harder" if diff else ""
        out.append({"title": p.get("name", ""), "url": f"https://www.ravelry.com/patterns/library/{p.get('permalink', '')}",
                    "content": _plain(full.get("notes_html", "") or "", 300),
                    "designer": (p.get("designer") or {}).get("name", ""), "free": p.get("free", False),
                    "level": level, "craft": (full.get("craft") or {}).get("name", ""),
                    "yarn": (full.get("yarn_weight") or {}).get("name", "")})
    log.info("ravelry %r: %d patterns", params["query"], len(out))
    return out


# ---------------------------------------------------------------- YouTube
_VIDEO_ASK = re.compile(r"\btutorials?\b|\b(?:a|any|some|good|find|recommend|show me|best)\s+(?:\w+\s+){0,2}videos?\b|"
                        r"\bvideos? (?:on|about|showing|for)\b|\bon youtube\b", re.I)


def wants_videos(text: str) -> bool:
    return bool(_VIDEO_ASK.search(text or ""))


async def youtube(q: str, n: int = 3) -> list[dict]:
    """Top YouTube videos for a search (a search costs 100 of the 10,000 free units a day)."""
    key = os.getenv("YOUTUBE_API_KEY", "")
    words = [w for w in keywords(q) if w not in ("video", "videos", "youtube", "tutorial", "tutorials", "find",
                                                   "recommend", "show")]
    if not key or not words:
        return []
    r = await _get("https://www.googleapis.com/youtube/v3/search", params={
        "part": "snippet", "type": "video", "maxResults": n, "q": " ".join(words) + " tutorial"
        if re.search(r"\btutorial", q, re.I) else " ".join(words),
        "relevanceLanguage": "en", "safeSearch": "moderate", "key": key})
    if r is None:
        return []
    out = []
    for it in r.json().get("items", []):
        sn = it.get("snippet", {})
        out.append({"title": html.unescape(sn.get("title", "")), "channel": html.unescape(sn.get("channelTitle", "")),
                    "url": f"https://www.youtube.com/watch?v={it.get('id', {}).get('videoId', '')}",
                    "content": html.unescape(sn.get("description", "")), "published_date": sn.get("publishedAt", "")})
    log.info("youtube %r: %d videos", " ".join(words), len(out))
    return out


# ---------------------------------------------------------------- Twitch
_twitch_token: tuple[str, float] = ("", 0.0)
_TWITCH_ASK = re.compile(
    r"\bhow many (?:people|streamers|channels|viewers)\b[^?]*\b(?:streaming|watching)\b|"
    r"\b(?:crowded|saturated|popular|busy)\b[^?]*\bon twitch\b|\bon twitch\b[^?]*\b(?:crowded|saturated|popular|"
    r"busy|viewers|right now)\b|\bwho(?:'s| is) streaming\b", re.I)
_GAME_END = r"(?:\s+(?:on twitch|right now|now|today|tonight|at the moment|these days|lately))*\W*$"
_TWITCH_GAME = [  # tried in order: "streaming Minecraft right now" before "is Minecraft crowded on Twitch"
    re.compile(r"\b(?:streaming|watching)\s+(?P<g>[A-Za-z0-9][\w' :&.-]*?)" + _GAME_END, re.I),
    re.compile(r"\b(?:is|are)\s+(?P<g>[A-Za-z0-9][\w' :&.-]*?)\s+(?:crowded|saturated|popular|busy)\b", re.I),
    re.compile(r"\b(?:of|for)\s+(?P<g>[A-Za-z0-9][\w' :&.-]*?)" + _GAME_END, re.I),
]


def twitch_game_asked(text: str) -> str | None:
    if not _TWITCH_ASK.search(text or ""):
        return None
    m = next((m for rx in _TWITCH_GAME if (m := rx.search(text.strip()))), None)
    g = m.group("g").strip() if m else ""
    g = re.sub(r"^(?:the game|the category|people|streamers)\s+", "", g, flags=re.I)
    return g if g and g.lower() not in ("it", "that", "this", "twitch", "people") else None


async def _twitch_auth() -> dict | None:
    global _twitch_token
    cid, secret = os.getenv("TWITCH_CLIENT_ID", ""), os.getenv("TWITCH_CLIENT_SECRET", "")
    if not (cid and secret):
        return None
    if not _twitch_token[0] or time.time() > _twitch_token[1]:
        try:
            r = await _client().post("https://id.twitch.tv/oauth2/token", data={
                "client_id": cid, "client_secret": secret, "grant_type": "client_credentials"})
            r.raise_for_status()
            j = r.json()
            _twitch_token = (j["access_token"], time.time() + j.get("expires_in", 3600) - 300)
        except Exception as e:
            log.info("twitch token failed: %s", e)
            return None
    return {"Client-Id": cid, "Authorization": f"Bearer {_twitch_token[0]}"}


async def twitch_category(game: str) -> dict | None:
    """Live numbers for a Twitch category: streams in the top 100, their viewers, the biggest."""
    h = await _twitch_auth()
    if h is None:
        return None
    c = await _get("https://api.twitch.tv/helix/search/categories", params={"query": game, "first": 1}, headers=h)
    cats = c.json().get("data", []) if c is not None else []
    if not cats:
        return None
    cat = cats[0]
    s = await _get("https://api.twitch.tv/helix/streams", params={"game_id": cat["id"], "first": 100}, headers=h)
    if s is None:
        return None
    streams = s.json().get("data", [])
    out = {"name": cat["name"], "streams": len(streams), "more": bool(s.json().get("pagination", {}).get("cursor")),
           "viewers": sum(x.get("viewer_count", 0) for x in streams),
           "top": streams[0]["user_name"] if streams else "", "top_viewers": streams[0]["viewer_count"] if streams else 0,
           "small": sum(1 for x in streams if x.get("viewer_count", 0) < 20)}
    log.info("twitch %r: %s", game, out)
    return out


# Channels by name (Nov 10 call: "How many videos does Justice Brewing have on Twitch?",
# "Search Twitch for users with the name Griffin" went to Tavily and found breweries and LinkedIn)
_CHANNEL_ASK = re.compile(
    r"\b(?:search|look|find|check|looking)\b[^.?!]*\b(?:twitch)\b[^.?!]*\b(?:users?|channels?|streamers?|accounts?|names?|"
    r"named|called)\b|"
    r"\b(?:users?|channels?|streamers?|accounts?)\b[^.?!]*\b(?:named|called|with the name|whose name is|by the name)\b|"
    r"\bhow many (?:videos|vods|clips|followers|streams)\b[^.?!]*\bon twitch\b|"
    r"\b(?:is|are)\s+[\w' ]+\s+(?:on twitch|live on twitch|streaming on twitch|live right now)\b|"
    r"\b(?:find|look up|search for)\s+[\w' ]+\s+on twitch\b", re.I)
_CHANNEL_NAME = [
    re.compile(r"\b(?:named|called|with the name|whose name is|by the name(?: of)?|name is)\s+(?P<n>[\w' .,-]+?)"
               r"(?:\s+on twitch)?\W*$", re.I),
    re.compile(r"\bhow many (?:videos|vods|clips|followers|streams) (?:does|do|has|have)\s+(?P<n>[\w' .-]+?)\s+"
               r"(?:have|got|posted|made)?\s*(?:on twitch)\b", re.I),
    re.compile(r"\b(?:is|are)\s+(?P<n>[\w' .-]+?)\s+(?:on twitch|live on twitch|streaming on twitch|live right now)\b", re.I),
    re.compile(r"\b(?:find|look up|search for|search twitch for)\s+(?P<n>[\w' .-]+?)\s*(?:on twitch)?\W*$", re.I),
]


def join_spelled(text: str) -> str:
    """'Griffin, g r I f f I n' -> 'Griffin, griffin': letters said one by one become a word."""
    return re.sub(r"\b(?:[A-Za-z][\s.,-]+){2,}[A-Za-z]\b",
                  lambda m: re.sub(r"[^A-Za-z]", "", m.group(0)).lower(), text or "")


def twitch_channel_asked(text: str, twitch_context: bool = False) -> str | None:
    """The channel name in 'How many videos does Justice Brewing have on Twitch?' etc.
    twitch_context: the conversation was just about Twitch ('a user whose name is X')."""
    text = join_spelled(text or "")
    if not (_CHANNEL_ASK.search(text) and (re.search(r"\btwitch\b", text, re.I) or twitch_context)):
        return None
    for rx in _CHANNEL_NAME:
        if m := rx.search(text):
            n = m.group("n").strip(" .,'")
            n = re.sub(r"^(?:the |a |users? |channels? |streamers? |someone |people )+", "", n, flags=re.I)
            # "Griffin, griffin" (said, then spelled): the spelled one
            parts = [p.strip() for p in n.split(",") if p.strip()]
            n = parts[-1] if parts else n
            if n and n.lower() not in ("it", "that", "them", "twitch", "users", "people"):
                return n
    return None


async def twitch_channels(name: str, n: int = 5) -> list[dict]:
    """Twitch channels matching a name, the exact match first: login, display name, live or not."""
    h = await _twitch_auth()
    if h is None:
        return []
    r = await _get("https://api.twitch.tv/helix/search/channels", params={"query": name, "first": 20}, headers=h)
    found = r.json().get("data", []) if r is not None else []
    want = re.sub(r"[^a-z0-9]", "", name.lower())
    bare = lambda c: c.get("broadcaster_login", "").replace("_", "")  # "justice_brewing" = "Justice Brewing"
    found.sort(key=lambda c: (bare(c) != want, not bare(c).startswith(want), not c.get("is_live")))
    out = [{"login": c.get("broadcaster_login", ""), "name": c.get("display_name", "").replace("_", " ").strip(),
            "id": c.get("id", ""), "exact": bare(c) == want,
            "live": bool(c.get("is_live")), "game": c.get("game_name", ""), "title": c.get("title", "")}
           for c in found[:n]]
    log.info("twitch channels %r: %s", name, [c["login"] for c in out])
    return out


async def twitch_channel_stats(ch: dict) -> dict:
    """Videos (past broadcasts, highlights, uploads; counted up to 500) and followers for one channel."""
    h = await _twitch_auth()
    stats = {"videos": None, "videos_more": False, "followers": None}
    if h is None or not ch.get("id"):
        return stats
    count, cursor = 0, None
    for _ in range(5):
        params = {"user_id": ch["id"], "first": 100, **({"after": cursor} if cursor else {})}
        r = await _get("https://api.twitch.tv/helix/videos", params=params, headers=h)
        if r is None:
            break
        j = r.json()
        count += len(j.get("data", []))
        cursor = j.get("pagination", {}).get("cursor")
        if not cursor:
            break
    stats["videos"], stats["videos_more"] = count, bool(cursor)
    f = await _get("https://api.twitch.tv/helix/channels/followers", params={"broadcaster_id": ch["id"]}, headers=h)
    if f is not None:
        stats["followers"] = f.json().get("total")
    log.info("twitch stats %s: %s", ch.get("login"), stats)
    return stats


# ---------------------------------------------------------------- The Guardian / NYT
# the user's keys (Nov 10): GUARDIAN_API_KEY (search, full text, ~500 calls/day) and
# NYT_API_KEY (Top Stories, Most Popular, Books; ~500 calls/day, a few seconds apart).
def _rfc(iso: str) -> str:
    d = _when(iso)
    return format_datetime(d) if d else ""


async def guardian(q: str = "", n: int = 10, days: int = 3, section: str = "", newest: bool = False) -> list[dict]:
    """Guardian articles: a search (most relevant first) or a section's newest."""
    key = os.getenv("GUARDIAN_API_KEY", "")
    if not key:
        return []
    params = {"page-size": n, "show-fields": "trailText,bodyText", "api-key": key,
              "from-date": (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d"),
              "order-by": "newest" if newest or not q else "relevance", "type": "article"}
    if q:
        params["q"] = q
    if section:
        params["section"] = section
    r = await _get("https://content.guardianapis.com/search", params=params)
    if r is None:
        return []
    out = []
    for a in r.json().get("response", {}).get("results", []):
        f = a.get("fields", {})
        out.append({"title": a.get("webTitle", ""), "url": a.get("webUrl", ""),
                    "content": _plain(f.get("trailText", "")) + " " + _plain(f.get("bodyText", ""), 1500),
                    "published_date": _rfc(a.get("webPublicationDate", "")), "outlet": "The Guardian"})
    log.info("guardian %r section=%r: %d", q, section, len(out))
    return out


_nyt_last = 0.0  # NYT allows about one call every 6 seconds


async def _nyt(path: str, **params):
    global _nyt_last
    key = os.getenv("NYT_API_KEY", "")
    if not key:
        return None
    wait = 6.2 - (time.monotonic() - _nyt_last)
    if wait > 0:
        await asyncio.sleep(wait)
    _nyt_last = time.monotonic()
    r = await _get(f"https://api.nytimes.com/svc/{path}", params={**params, "api-key": key})
    return r.json() if r is not None else None


def _nyt_item(x: dict) -> dict:
    return {"title": x.get("title", ""), "url": x.get("url", ""), "content": x.get("abstract", ""),
            "published_date": _rfc(x.get("published_date", "") or x.get("updated_date", "")),
            "outlet": "The New York Times"}


async def nyt_top(section: str = "home") -> list[dict]:
    """NYT Top Stories for a section (home, us, world, politics, science, technology...)."""
    j = await _nyt(f"topstories/v2/{section}.json")
    items = [x for x in (j or {}).get("results", [])
             if x.get("item_type", "Article") == "Article" and len(x.get("title", "").split()) >= 4 and x.get("url")]
    log.info("nyt top %s: %d", section, len(items))
    return [_nyt_item(x) for x in items]


async def nyt_popular() -> list[dict]:
    """NYT's most-viewed articles of the past day."""
    j = await _nyt("mostpopular/v2/viewed/1.json")
    return [_nyt_item(x) for x in (j or {}).get("results", []) if x.get("title")]


BOOK_LISTS = {  # what the user might say -> the NYT list's name
    "nonfiction": "combined-print-and-e-book-nonfiction", "non-fiction": "combined-print-and-e-book-nonfiction",
    "fiction": "combined-print-and-e-book-fiction", "young adult": "young-adult-hardcover", "ya": "young-adult-hardcover",
    "children": "childrens-middle-grade-hardcover", "kids": "childrens-middle-grade-hardcover",
    "graphic": "graphic-books-and-manga", "manga": "graphic-books-and-manga", "advice": "advice-how-to-and-miscellaneous",
    "how-to": "advice-how-to-and-miscellaneous", "how to": "advice-how-to-and-miscellaneous",
    "paperback": "trade-fiction-paperback", "audio": "audio-fiction",
}
BOOKS_ASK = re.compile(r"\b(?:best ?sellers?|bestseller lists?|best[- ]selling books?|top (?:\w+ )?books?|"
                       r"(?:new york times|nyt|times) (?:book|best)|books? (?:list|chart))\b", re.I)


async def nyt_books(text: str) -> tuple[str, list[dict]]:
    """('Combined Print & E-Book Fiction', [{'title', 'author', 'weeks'}]) for the list the user named."""
    low = (text or "").lower()
    name = next((v for k, v in BOOK_LISTS.items() if re.search(rf"\b{re.escape(k)}\b", low)),
                "combined-print-and-e-book-fiction")
    j = await _nyt(f"books/v3/lists/current/{name}.json")
    res = (j or {}).get("results", {})
    books = [{"title": b.get("title", "").title(), "author": b.get("author", ""),
              "weeks": b.get("weeks_on_list", 0), "url": b.get("amazon_product_url", "")}
             for b in res.get("books", [])]
    return res.get("display_name", "bestseller"), books


# ---------------------------------------------------------------- Joomla
async def joomla_release() -> dict | None:
    """Joomla's latest stable release from GitHub (free, no key)."""
    r = await _get("https://api.github.com/repos/joomla/joomla-cms/releases", params={"per_page": 10},
                   headers={"Accept": "application/vnd.github+json"})
    if r is None:
        return None
    for rel in r.json():
        if not rel.get("prerelease") and not rel.get("draft"):
            when = datetime.fromisoformat(rel["published_at"].replace("Z", "+00:00"))
            return {"version": rel.get("tag_name", "").lstrip("v"), "when": when, "url": rel.get("html_url", "")}
    return None


JOOMLA_SECURITY_FEED = "https://developer.joomla.org/security-centre.feed?type=rss"
JOOMLA_NEWS_FEED = "https://www.joomla.org/announcements.feed?type=rss"


# ---------------------------------------------------------------- RSS / Atom
def _when(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


async def feed(url: str, outlet: str = "", keep=None) -> list[dict]:
    """One RSS or Atom feed -> results (published_date in RFC 2822, as Tavily's news gives it)."""
    r = await _get(url, headers={"User-Agent": "Mozilla/5.0 (Othos feed reader)"})
    if r is None:
        return []
    try:
        root = ET.fromstring(r.content.lstrip())
    except ET.ParseError:
        # some feeds use a namespace prefix they never declare (seen in a local paper's feed):
        # drop the prefixes and try again
        text = r.text.lstrip()
        text = re.sub(r"<(/?)[A-Za-z][\w.-]*:([A-Za-z][\w.-]*)", r"<\1\2", text)
        text = re.sub(r"\s[A-Za-z][\w.-]*:[A-Za-z][\w.-]*=(\"[^\"]*\"|'[^']*')", "", text)
        try:
            root = ET.fromstring(text.encode("utf-8"))
        except ET.ParseError as e:
            log.info("feed %s unreadable: %s", url, e)
            return []
    out = []
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        def get(*names):  # (an Element with no children is falsy, so no "or" chains)
            for name in names:
                for c in it:
                    if c.tag.split("}")[-1] == name:
                        return c
            return None
        title_el = get("title")
        title = (title_el.text if title_el is not None else "") or ""
        link_el = get("link")
        link = ((link_el.text or "").strip() or link_el.get("href", "")) if link_el is not None else ""
        desc_el = get("description", "summary", "encoded", "content")
        desc = desc_el.text if desc_el is not None else ""
        date_el = get("pubDate", "published", "updated", "date")
        when = _when(date_el.text if date_el is not None else "")
        res = {"title": _plain(title), "url": link.strip(), "content": _plain(desc or "", 500),
               "published_date": format_datetime(when) if when else "", "outlet": outlet}
        if res["title"] and (keep is None or keep(res)):
            out.append(res)
    return out


# Local and state news feeds come from personal.json ("local_news", "state_news"). A
# feed's "skip_urls" drops stories whose address matches (a paper's wire-service pages).
LOCAL_FEEDS = personal.local_feeds()
STATE_FEEDS = personal.state_feeds()
STATE = personal.state_name()  # "Washington" -> the set is called "Washington state"
STATE_LABEL = f"{STATE} state" if STATE else "state"
# PC hardware news (checked Nov 10). Tom's Hardware has no API, but its RSS feed is free.
# Not usable: Guru3D and the Linus Tech Tips forum (both refuse readers, 403).
HARDWARE_FEEDS = {
    "tomshardware.com": ("https://www.tomshardware.com/feeds/all", "Tom's Hardware", None),
    "techpowerup.com": ("https://www.techpowerup.com/rss/news", "TechPowerUp", None),
    "kitguru.net": ("https://www.kitguru.net/feed/", "KitGuru", None),
    "gamersnexus.net": ("https://gamersnexus.net/rss.xml", "Gamers Nexus", None),
    "arstechnica.com": ("https://feeds.arstechnica.com/arstechnica/gadgets", "Ars Technica", None),
}
HARDWARE_NEWS = re.compile(r"\b(?:(?:pc |computer )?hardware|gpu|graphics card|cpu|pc gaming|nvidia|amd|intel)\s+"
                           r"(?:news|headlines)\b|\bnews (?:about|on|in) (?:pc |computer )?(?:hardware|gpus?|cpus?)\b",
                           re.I)
# Tech in general (the user, Nov 10: add WIRED); its coupon posts are dropped by _NOT_NEWS
TECH_FEEDS = {
    "wired.com": ("https://www.wired.com/feed/rss", "Wired", None),
    "arstechnica.com": ("https://feeds.arstechnica.com/arstechnica/index", "Ars Technica", None),
    "tomshardware.com": HARDWARE_FEEDS["tomshardware.com"],
}
TECH_NEWS = re.compile(r"\b(?:tech|technology|science|wired)\s+(?:news|headlines|stories)\b|"
                       r"\bnews (?:about|on|in|from) (?:tech|technology|science|wired)\b|\bwhat'?s new in tech\b", re.I)
# Digital art and design (the user, Nov 10: add Computer Arts Magazine)
DESIGN_FEEDS = {
    "computerartsmagazine.net": ("https://computerartsmagazine.net/feed/", "Computer Arts Magazine", None),
    # (Wired's culture feed was mostly TV shows, so it's left out)
}
DESIGN_NEWS = re.compile(r"\b(?:design|digital art|graphic design|creative|illustration|art|computer arts)\s+"
                         r"(?:news|headlines|stories|trends)\b|\bcomputer arts\b|\bnews (?:about|on|in) (?:design|digital art)\b",
                         re.I)
FEED_SETS = {"hardware": HARDWARE_FEEDS, "tech": TECH_FEEDS, "design": DESIGN_FEEDS}
if LOCAL_FEEDS:
    FEED_SETS["local"] = LOCAL_FEEDS
if STATE_FEEDS:
    FEED_SETS[STATE_LABEL] = STATE_FEEDS
OUTLETS = {k: v[1] for k, v in {**LOCAL_FEEDS, **STATE_FEEDS, **HARDWARE_FEEDS, **TECH_FEEDS, **DESIGN_FEEDS}.items()}
OUTLETS.update({"developer.joomla.org": "Joomla", "joomla.org": "Joomla"})

def _alts(words: list[str]) -> str:
    return "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in sorted(words, key=len, reverse=True))


# Matched even with no feeds set up: Othos then says how to add them (agent.py),
# instead of searching world news for "local news".
LOCAL_NEWS = re.compile(r"\b(?:local|nearby|around here" + ("|" + _alts(personal.local_words())
                        if personal.local_words() else "") + r")\b", re.I)
if STATE_FEEDS:
    _st = _alts([STATE]) if STATE else r"(?!x)x"
    STATE_NEWS = re.compile(rf"\b(?:{_st})(?: state|'s state| state's)\b|\bstate (?:news|headlines|politics|legislature)\b|"
                            r"\b(?:legislature|statewide" + ("|" + _alts(personal.state_words())
                                                             if personal.state_words() else "") + r")\b|"
                            rf"\b(?:{_st}) (?:legislature|governor)\b", re.I)
else:
    STATE_NEWS = re.compile(r"\bstate (?:news|headlines)\b", re.I)


_NOT_NEWS = re.compile(r"^\s*\(pr\)|^(?:on the air|obituar|letters?\b|today in history|crossword|horoscope|weather forecast)|"
                       r"\b(?:sponsored|business briefly|police blotter|calendar|giveaway|the winners|coupons?|"
                       r"promo codes?|discount codes?|referral deals?|\d+% off)\b", re.I)


async def feed_news(kind: str, hours: int = 36) -> list[dict]:
    """Recent stories from one set of feeds (local, state, hardware...), newest
    first, taking turns between outlets."""
    feeds = FEED_SETS.get(kind) or {}
    state = kind
    lists = await asyncio.gather(*(feed(u, name, keep) for u, name, keep in feeds.values()))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    oldest = datetime(1970, 1, 1, tzinfo=timezone.utc)
    per = []
    for items in lists:
        fresh = [r for r in items if (_when(r["published_date"]) or oldest) >= cutoff and len(r["title"].split()) >= 4
                 and not _NOT_NEWS.search(r["title"])]
        fresh.sort(key=lambda r: _when(r["published_date"]) or oldest, reverse=True)
        per.append(fresh)
    out = []
    while any(per):  # take turns between outlets so one paper doesn't fill the roundup
        for items in per:
            if items:
                out.append(items.pop(0))
    log.info("%s feed news: %d stories from %s", state, len(out), [len(x) for x in lists])
    return out


# ---------------------------------------------------------------- weather (NWS)
_nws_point: dict | None = None


def _latlon() -> tuple[float, float] | None:
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$", personal.weather_latlon())
    return (float(m.group(1)), float(m.group(2))) if m else None


async def _point() -> dict | None:
    global _nws_point
    if _nws_point is None and (ll := _latlon()):
        r = await _get(f"https://api.weather.gov/points/{ll[0]:.4f},{ll[1]:.4f}")
        if r is not None:
            _nws_point = r.json().get("properties")
    return _nws_point


def _f(c) -> int | None:
    return None if c is None else round(c * 9 / 5 + 32)


async def nws_now() -> dict | None:
    """Latest observation near the town: {'sky': 'Mostly Cloudy', 'temp_f': 47, 'station', 'when'}."""
    p = await _point()
    if not p:
        return None
    st = await _get(p["observationStations"])
    stations = st.json().get("features", []) if st is not None else []
    best = None
    for s in stations[:4]:  # the nearest station sometimes has no recent reading or no temperature
        sid = s["properties"]["stationIdentifier"]
        r = await _get(f"https://api.weather.gov/stations/{sid}/observations/latest")
        if r is None:
            continue
        o = r.json().get("properties", {})
        when = _when(o.get("timestamp", ""))
        if not when or datetime.now(timezone.utc) - when > timedelta(hours=3):
            continue
        temp = _f((o.get("temperature") or {}).get("value"))
        sky = (o.get("textDescription") or "").strip()
        if best is None and sky:  # the nearest station's sky
            best = {"sky": sky, "temp_f": temp, "station": s["properties"].get("name", sid), "when": when}
        if best is not None and best["temp_f"] is None and temp is not None:
            best["temp_f"] = temp  # temperature from the next station over
        if best is not None and best["temp_f"] is not None:
            break
    log.info("nws now: %s", best)
    return best


async def nws_forecast() -> list[dict]:
    """The next few forecast periods: [{'name': 'Tonight', 'short', 'detail', 'temp', 'day'}]."""
    p = await _point()
    if not p:
        return []
    r = await _get(p["forecast"])
    if r is None:
        return []
    out = []
    for x in r.json().get("properties", {}).get("periods", [])[:14]:
        out.append({"name": x.get("name", ""), "short": x.get("shortForecast", ""),
                    "detail": x.get("detailedForecast", ""), "temp": x.get("temperature"),
                    "day": x.get("isDaytime", True), "start": _when(x.get("startTime", ""))})
    return out


async def nws_alerts() -> list[str]:
    """Active weather alerts for the town's point, as headlines."""
    ll = _latlon()
    if not ll:
        return []
    r = await _get("https://api.weather.gov/alerts/active", params={"point": f"{ll[0]:.4f},{ll[1]:.4f}"})
    if r is None:
        return []
    return [f["properties"].get("headline", "") for f in r.json().get("features", []) if f.get("properties")]


# ---------------------------------------------------------------- Tavily usage
async def tavily_usage() -> dict | None:
    """{'used': 149, 'limit': 1500} from Tavily's usage check (costs no credits)."""
    key = os.getenv("TAVILY_API_KEY", "")
    if not key:
        return None
    r = await _get("https://api.tavily.com/usage", headers={"Authorization": f"Bearer {key}"})
    if r is None:
        return None
    acct = r.json().get("account", {})
    used = acct.get("plan_usage")
    if used is None:
        return None
    return {"used": int(used) + int(acct.get("paygo_usage") or 0), "limit": acct.get("plan_limit")}
