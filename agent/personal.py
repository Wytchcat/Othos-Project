"""The user's personal settings: personal.json (git-ignored, next to this file).

Copy personal.example.json to personal.json and fill it in. Anything missing falls
back to the old .env settings (OTHOS_WEATHER_LOCATION, OTHOS_WEATHER_LATLON,
OTHOS_HOME_LOCATION, OTHOS_CONTACT_URL) or to nothing (the feature is then off):
no local feeds means no local news roundup, no state means no state news.

Read once at import; close and reopen Othos after editing.
"""
import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("othos.personal")
HERE = Path(__file__).resolve().parent
PATH = HERE / "personal.json"


def _load() -> dict:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("personal.json couldn't be read: %s", e)
        return {}


DATA = _load()


def _text(key: str, env: str = "") -> str:
    v = DATA.get(key)
    if isinstance(v, str) and v.strip():
        return v.strip()
    return os.getenv(env, "").strip() if env else ""


def _words(v) -> list[str]:
    return [w.strip() for w in (v or []) if isinstance(w, str) and w.strip()]


def _assistant() -> dict:
    a = DATA.get("assistant")
    return a if isinstance(a, dict) else {}


def assistant_name() -> str:
    """What the assistant is called: "Othos" unless personal.json's assistant.name says otherwise."""
    n = _assistant().get("name")
    return n.strip() if isinstance(n, str) and n.strip() else "Othos"


def assistant_heard_as() -> list[str]:
    """What speech-to-text may hear instead of the name ("Jervis" for "Jarvis")."""
    return _words(_assistant().get("heard_as"))


# (the assistant's "logo" and "color" are read straight from personal.json by the app, Othos.cs, and the call page)


def name() -> str:
    """The user's first name ('' if not set; agent.py then looks in profile.md)."""
    return _text("name")


def weather_location() -> str:
    return _text("weather_location", "OTHOS_WEATHER_LOCATION")


def weather_latlon() -> str:
    """'lat,lon' for the National Weather Service."""
    return _text("weather_latlon", "OTHOS_WEATHER_LATLON")


def home_location() -> str:
    return _text("home_location", "OTHOS_HOME_LOCATION") or weather_location()


def contact_url() -> str:
    """The user's website or contact page (Wikipedia asks for one)."""
    return _text("contact_url", "OTHOS_CONTACT_URL")


def _feeds(section: dict) -> dict:
    """[{"site", "url", "name", "skip_urls"}] -> {site: (url, name, keep)} as sources.py uses."""
    out = {}
    for f in (section or {}).get("feeds") or []:
        if not isinstance(f, dict) or not f.get("url"):
            continue
        site = (f.get("site") or re.sub(r"^https?://(?:www\.)?([^/]+).*$", r"\1", f["url"])).lower()
        keep = None
        if f.get("skip_urls"):
            try:
                rx = re.compile(f["skip_urls"], re.I)
                keep = (lambda r, rx=rx: not rx.search(r["url"]))
            except re.error as e:
                log.warning("personal.json: bad skip_urls for %s: %s", site, e)
        out[site] = (f["url"], f.get("name") or site, keep)
    return out


def local_feeds() -> dict:
    return _feeds(DATA.get("local_news") or {})


def local_words() -> list[str]:
    """Extra words that mean 'local news' (towns, counties, regions)."""
    return _words((DATA.get("local_news") or {}).get("trigger_words"))


def state_name() -> str:
    """'Washington' -> the state news set is called 'Washington state'."""
    s = (DATA.get("state_news") or {}).get("state")
    return s.strip() if isinstance(s, str) else ""


def state_feeds() -> dict:
    return _feeds(DATA.get("state_news") or {})


def state_words() -> list[str]:
    """Extra words that mean 'state news' (the capital, for example)."""
    return _words((DATA.get("state_news") or {}).get("trigger_words"))


def pronunciations() -> dict:
    """Place names respelled for the voice (added to pronunciations.json's)."""
    v = DATA.get("pronunciations") or {}
    return {k: s for k, s in v.items() if isinstance(k, str) and isinstance(s, str) and s.strip()}


def keyterms() -> list[str]:
    """Words speech-to-text should listen for (place names it mishears)."""
    return _words(DATA.get("keyterms"))


def topic_feeds(topic: str) -> dict:
    """Extra feeds a user adds for gardening / recipes / books / movies (personal.json "topic_feeds"), same shape as
    local_news: {"gardening": {"feeds": [{"url": ..., "name": ...}], "drop": ["gardenista.com"]}}."""
    return _feeds(((DATA.get("topic_feeds") or {}).get(topic)) or {})


def topic_feed_drops(topic: str) -> list[str]:
    """Built-in feeds (by their site key) the user doesn't want for a topic."""
    drops = (((DATA.get("topic_feeds") or {}).get(topic)) or {}).get("drop") or []
    return [str(d).lower() for d in drops if d]
