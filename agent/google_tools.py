"""Google for Othos, stage 1: connecting a Google account, and reading its calendar.

Sign-in: Google's "desktop app" method. Your browser opens Google's own sign-in page; after you say yes, Google sends
a one-time code back to this PC only (http://127.0.0.1:<random port>), which Othos swaps for a sign-in it can renew.
PKCE and a random "state" check guard that hand-over. Settings: GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env
(your own Google Cloud project's Desktop client).

The saved sign-in lives in %LOCALAPPDATA%\\Othos\\google.dat, encrypted with Windows' own protection (DPAPI) for
this Windows account only. It is never printed, logged, committed or sent anywhere except to Google.
Disconnect cancels it at Google and deletes the saved copy.

Permissions asked for now: your email address (to show which account is connected) and READ-ONLY calendar.
Later stages add Gmail, Sheets and Docs one at a time (plan: Othos_future\\code\\Google tools plan 2026-11-16.md).

    google_tools.py status | connect | disconnect      (the Connect Google window uses these)
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import hashlib
import http.server
import json
import os
import re
import secrets
import socketserver
import sys
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

HERE = Path(__file__).resolve().parent
STORE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Othos" / "google.dat"
_ENTROPY = b"Othos Google sign-in v1"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CAL_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
SCOPES = ["openid", "email", "https://www.googleapis.com/auth/calendar.readonly"]
CONNECT_TIMEOUT = 300          # seconds to finish signing in
OPERA = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Opera" / "opera.exe"


class NeedsConnect(RuntimeError):
    """No usable sign-in: the person has to press Connect (again)."""


class GoogleError(RuntimeError):
    pass


# ---------------------------------------------------------------- settings and the saved sign-in
def load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(HERE / ".env", override=False)
    except ImportError:
        pass


def client() -> tuple[str, str]:
    return (os.getenv("GOOGLE_CLIENT_ID") or "").strip(), (os.getenv("GOOGLE_CLIENT_SECRET") or "").strip()


def configured() -> bool:
    cid, sec = client()
    return bool(cid and sec)


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(protect: bool, data: bytes) -> bytes:
    def blob(b: bytes) -> _Blob:
        buf = ctypes.create_string_buffer(b, len(b))
        return _Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out, ent, src = _Blob(), blob(_ENTROPY), blob(data)
    c = ctypes.windll.crypt32
    ok = (c.CryptProtectData(ctypes.byref(src), "Othos Google", ctypes.byref(ent), None, None, 0x01, ctypes.byref(out)) if protect
          else c.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x01, ctypes.byref(out)))
    if not ok:
        raise OSError("Windows couldn't protect or read the saved Google sign-in")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def saved() -> dict:
    """{"refresh_token", "email", "scopes", "connected_on"} or {} (never shown)."""
    try:
        return json.loads(_dpapi(False, base64.b64decode(STORE.read_text(encoding="ascii"))).decode("utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(base64.b64encode(_dpapi(True, json.dumps(d).encode("utf-8"))).decode("ascii"), encoding="ascii")
    tmp.replace(STORE)


def status() -> dict:
    """What the window shows: {"configured", "connected", "email", "connected_on"} (no secrets)."""
    s = saved()
    return {"configured": configured(), "connected": bool(s.get("refresh_token")), "email": s.get("email", ""),
            "connected_on": s.get("connected_on", ""), "calendar": "https://www.googleapis.com/auth/calendar.readonly" in s.get("scopes", [])}


# ---------------------------------------------------------------- connect (desktop sign-in with PKCE)
def _open_browser(url: str) -> None:
    """Opera when it's installed (the Othos rule), else the default browser."""
    try:
        if OPERA.exists():
            import subprocess
            subprocess.Popen([str(OPERA), url])
            return
    except OSError:
        pass
    webbrowser.open(url)


_DONE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Othos</title><style>
body{{background:#020705;color:#c8e6d2;font:17px/1.6 "Segoe UI",sans-serif;display:grid;place-items:center;height:100vh;margin:0}}
div{{max-width:520px;padding:24px;border:1px solid #403569;border-radius:10px;background:#07110c}}h1{{color:#e7c368;font-size:22px;margin:0 0 8px}}
</style></head><body><div><h1>{title}</h1><p>{text}</p></div></body></html>"""


def connect(open_browser=_open_browser, timeout: int = CONNECT_TIMEOUT, http_client: httpx.Client | None = None) -> dict:
    """Run the sign-in. Returns status(). Raises GoogleError with a plain message if it doesn't finish."""
    load_env()
    cid, sec = client()
    if not (cid and sec):
        raise GoogleError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET aren't in .env yet.")
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    got: dict = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):            # never log the code
            pass

        def do_GET(self):
            q = parse_qs(urlsplit(self.path).query)
            ok_state = q.get("state", [""])[0] == state
            if not ok_state:
                self.send_response(400)
                self.end_headers()
                return                          # someone else's request: ignored, keep waiting
            if "code" in q:
                got["code"] = q["code"][0]
                title, text = "Connected", "Google is connected to Othos. You can close this tab."
            else:
                got["error"] = q.get("error", ["cancelled"])[0]
                title, text = "Not connected", "Nothing was connected. You can close this tab and try again from Othos."
            body = _DONE_PAGE.format(title=title, text=text).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    redirect = f"http://127.0.0.1:{port}"
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        url = AUTH_URL + "?" + urlencode({
            "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": " ".join(SCOPES),
            "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
            "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"})
        open_browser(url)
        if not done.wait(timeout):
            raise GoogleError("The Google sign-in wasn't finished within 5 minutes. Nothing was connected.")
    finally:
        srv.shutdown()
        srv.server_close()
    if "code" not in got:
        raise GoogleError("Google sign-in was cancelled. Nothing was connected." if got.get("error") == "access_denied"
                          else f"Google said no ({got.get('error', 'unknown')}). Nothing was connected.")
    own = http_client is None
    c = http_client or httpx.Client(timeout=30)
    try:
        r = c.post(TOKEN_URL, data={"code": got["code"], "client_id": cid, "client_secret": sec, "code_verifier": verifier,
                                    "grant_type": "authorization_code", "redirect_uri": redirect})
    except httpx.HTTPError as e:
        raise GoogleError(f"Couldn't reach Google: {type(e).__name__}") from None
    finally:
        if own:
            c.close()
    if r.status_code != 200:
        raise GoogleError(_google_said(r))
    tok = r.json()
    if not tok.get("refresh_token"):
        raise GoogleError("Google didn't give a sign-in Othos can renew. Press Connect again.")
    granted = (tok.get("scope") or "").split()
    _save({"refresh_token": tok["refresh_token"], "email": _email_from(tok.get("id_token", "")), "scopes": granted,
           "connected_on": date.today().isoformat()})
    _cache.update(token=tok.get("access_token", ""), until=time.time() + int(tok.get("expires_in", 0)) - 60)
    return status()


def _email_from(id_token: str) -> str:
    """The account's email from Google's ID token (shown in the window only; not checked as proof of anything)."""
    try:
        part = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("email", "")
    except Exception:
        return ""


def _google_said(r: httpx.Response) -> str:
    try:
        j = r.json()
        err = j.get("error")
        if isinstance(err, dict):
            return f"Google said no ({r.status_code}): {str(err.get('message', ''))[:160]}"
        return f"Google said no ({r.status_code} {err}): {str(j.get('error_description', ''))[:160]}"
    except ValueError:
        return f"Google said no ({r.status_code})."


# ---------------------------------------------------------------- the access token (memory only) and disconnect
_cache = {"token": "", "until": 0.0}


def access_token(http_client: httpx.Client | None = None) -> str:
    if _cache["token"] and time.time() < _cache["until"]:
        return _cache["token"]
    load_env()
    s = saved()
    cid, sec = client()
    if not s.get("refresh_token") or not (cid and sec):
        raise NeedsConnect("Google isn't connected.")
    own = http_client is None
    c = http_client or httpx.Client(timeout=20)
    try:
        r = c.post(TOKEN_URL, data={"client_id": cid, "client_secret": sec, "refresh_token": s["refresh_token"],
                                    "grant_type": "refresh_token"})
    except httpx.HTTPError as e:
        raise GoogleError(f"Couldn't reach Google: {type(e).__name__}") from None
    finally:
        if own:
            c.close()
    if r.status_code == 400 and "invalid_grant" in r.text:
        # expired (Testing mode: every 7 days), cancelled at Google, or the password changed
        raise NeedsConnect("Google needs you to connect again.")
    if r.status_code != 200:
        raise GoogleError(_google_said(r))
    tok = r.json()
    if tok.get("refresh_token") and tok["refresh_token"] != s["refresh_token"]:
        s["refresh_token"] = tok["refresh_token"]
        _save(s)
    _cache.update(token=tok["access_token"], until=time.time() + int(tok.get("expires_in", 3600)) - 60)
    return _cache["token"]


def disconnect(http_client: httpx.Client | None = None) -> str:
    """Cancel the sign-in at Google, then delete the saved copy (deleted even if Google can't be reached)."""
    s = saved()
    msg = "Google is disconnected."
    if s.get("refresh_token"):
        own = http_client is None
        c = http_client or httpx.Client(timeout=20)
        try:
            r = c.post(REVOKE_URL, data={"token": s["refresh_token"]})
            if r.status_code not in (200, 400):        # 400: already cancelled at Google
                msg = ("Google is disconnected on this PC. Google didn't confirm the cancel; "
                       "remove Othos at myaccount.google.com/permissions to be sure.")
        except httpx.HTTPError:
            msg = ("Google is disconnected on this PC, but Google couldn't be reached to cancel it. "
                   "Remove Othos at myaccount.google.com/permissions to be sure.")
        finally:
            if own:
                c.close()
    STORE.unlink(missing_ok=True)
    _cache.update(token="", until=0.0)
    return msg


# ---------------------------------------------------------------- calendar (read-only)
def events(start: datetime, end: datetime, http_client: httpx.Client | None = None, limit: int = 50) -> list[dict]:
    """Events on the primary calendar between start and end (aware datetimes), in order. Cancelled ones left out."""
    tok = access_token(http_client)
    own = http_client is None
    c = http_client or httpx.Client(timeout=20)
    try:
        r = c.get(CAL_URL, headers={"Authorization": f"Bearer {tok}"},
                  params={"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                          "orderBy": "startTime", "maxResults": str(limit)})
    except httpx.HTTPError as e:
        raise GoogleError(f"Couldn't reach Google: {type(e).__name__}") from None
    finally:
        if own:
            c.close()
    if r.status_code == 401:
        _cache.update(token="", until=0.0)
        raise NeedsConnect("Google needs you to connect again.")
    if r.status_code == 403:
        raise GoogleError("Google won't let Othos read the calendar (permission not given). Press Connect again and tick the calendar.")
    if r.status_code != 200:
        raise GoogleError(_google_said(r))
    out = []
    for e in r.json().get("items", []):
        if e.get("status") == "cancelled":
            continue
        s, en = e.get("start", {}), e.get("end", {})
        if "date" in s:                                    # all-day
            out.append({"title": _clean_title(e.get("summary")), "all_day": True,
                        "start": datetime.fromisoformat(s["date"]).astimezone(), "end": datetime.fromisoformat(en.get("date", s["date"])).astimezone()})
        elif "dateTime" in s:
            out.append({"title": _clean_title(e.get("summary")), "all_day": False,
                        "start": datetime.fromisoformat(s["dateTime"]).astimezone(),
                        "end": datetime.fromisoformat(en.get("dateTime", s["dateTime"])).astimezone()})
    return out


def _clean_title(t) -> str:
    """An event title as plain words (never a link to follow; long titles shortened)."""
    t = " ".join(str(t or "untitled event").split())
    t = re.sub(r"https?://\S+", "a link", t)
    return t[:80]


# ---------------------------------------------------------------- calendar questions, answered by code
_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_PARTS = {"morning": (5, 12), "afternoon": (12, 17), "evening": (17, 22), "tonight": (17, 24), "night": (17, 24)}
ASKS_CALENDAR = re.compile(
    r"\b(?:my (?:calendar|schedule|agenda|appointments?|meetings?|day|week)|"
    r"am i (?:free|busy|booked)|(?:do|have) i (?:have|got) (?:anything|any (?:appointments?|meetings?|plans))|"
    r"what(?:'s| is) (?:on|next) (?:on )?my|(?:next|first) (?:appointment|meeting|event)|"
    r"on (?:the|my) calendar|what do i have (?:on|going on|planned)?)", re.I)
_NOT_CAL = re.compile(r"\b(?:tv|television|bus|train|ferry|game|sports?|flight|movie|smithy|garbage|trash)\b", re.I)


def asks_calendar(text: str) -> bool:
    return bool(ASKS_CALENDAR.search(text or "")) and not _NOT_CAL.search(text or "")


def when_from(text: str, now: datetime) -> tuple[datetime, datetime, str]:
    """(start, end, spoken label) for the time the question is about. Default: the rest of today."""
    t = (text or "").lower()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    label, start, end = "today", now, day0 + timedelta(days=1)
    if "tomorrow" in t:
        start, end, label = day0 + timedelta(days=1), day0 + timedelta(days=2), "tomorrow"
    elif re.search(r"\bnext week\b", t):
        mon = day0 + timedelta(days=7 - day0.weekday())
        start, end, label = mon, mon + timedelta(days=7), "next week"
    elif re.search(r"\b(?:this week|my week|the week)\b", t):
        start, end, label = now, day0 + timedelta(days=7 - day0.weekday()), "the rest of this week"
    else:
        for i, d in enumerate(_DAYS):
            if re.search(rf"\b{d}\b", t):
                ahead = (i - day0.weekday()) % 7
                if ahead == 0 and re.search(rf"\bnext {d}\b", t):
                    ahead = 7
                start = day0 + timedelta(days=ahead)
                end = start + timedelta(days=1)
                label = "today" if ahead == 0 else d.capitalize()
                if ahead == 0:
                    start = now
                break
    for part, (h0, h1) in _PARTS.items():
        if re.search(rf"\b{part}\b", t):
            base = start.replace(hour=0, minute=0, second=0, microsecond=0)
            ps, pe = base + timedelta(hours=h0), base + timedelta(hours=h1)
            start, end = max(ps, now) if base.date() == now.date() else ps, pe
            label = ("this " + part if part != "tonight" else "tonight") if label == "today" else f"{label} {part}"
            break
    return start, end, label


def _clock(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    return f"{h} {'AM' if dt.hour < 12 else 'PM'}" if dt.minute == 0 else f"{h}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def _day_word(dt: datetime, now: datetime) -> str:
    d = (dt.date() - now.date()).days
    return "today" if d == 0 else "tomorrow" if d == 1 else dt.strftime("%A")


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + ", and " + items[-1]


def describe(evs: list[dict], label: str, kind: str, now: datetime, multi_day: bool = False) -> str:
    """The spoken answer. kind: "list", "free" or "next". Says where it came from."""
    if kind == "next":
        nxt = next((e for e in evs if e["all_day"] or e["start"] >= now), None)
        if not nxt:
            return "There's nothing coming up on your calendar in the next week."
        when = "all day " + _day_word(nxt["start"], now) if nxt["all_day"] else f"{_day_word(nxt['start'], now)} at {_clock(nxt['start'])}"
        return f"Next on your calendar: {nxt['title']}, {when}."
    if not evs:
        return f"You're free {label}. Nothing's on your calendar." if kind == "free" else f"Nothing on your calendar {label}."
    parts = []
    for e in evs[:8]:
        day = f"{_day_word(e['start'], now)} " if multi_day else ""
        parts.append(f"{e['title']} {day}all day".replace("  ", " ") if e["all_day"] else f"{e['title']} {day}at {_clock(e['start'])}")
    more = f" and {len(evs) - 8} more" if len(evs) > 8 else ""
    n = len(evs)
    head = (f"{label[0].upper() + label[1:]} you're not free. You have " if kind == "free"
            else f"{label[0].upper() + label[1:]} you have ")
    return f"{head}{n} thing{'s' if n != 1 else ''} on your calendar: {_join(parts)}{more}."


def kind_of(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"\b(?:next|first) (?:appointment|meeting|event)\b|what(?:'s| is) next\b", t):
        return "next"
    if re.search(r"\bam i (?:free|busy|booked)\b|\bfree\b", t):
        return "free"
    return "list"


def calendar_answer(text: str, now: datetime | None = None, http_client: httpx.Client | None = None) -> str:
    """A calendar question answered by code (no model). Plain messages when Google isn't connected or reachable."""
    load_env()
    now = now or datetime.now().astimezone()
    if not configured() or not saved().get("refresh_token"):
        return ("Your Google calendar isn't connected yet. Open Connect Google (google-connect.ps1) and press Connect, "
                "then ask me again.")
    if not status()["calendar"]:
        return "Google is connected, but not for the calendar. Press Connect again in Connect Google and tick the calendar."
    kind = kind_of(text)
    if kind == "next":
        start, end, label = now, now + timedelta(days=7), "this week"
    else:
        start, end, label = when_from(text, now)
        if start >= end:
            return f"{label[0].upper() + label[1:]} is already over."
    try:
        evs = events(start, end, http_client)
    except NeedsConnect:
        return "Google needs you to connect again before I can read your calendar. Open Connect Google and press Connect."
    except GoogleError as e:
        return f"I couldn't read your calendar just now. {e}"
    return describe(evs, label, kind, now, multi_day=(end - start) > timedelta(days=1, hours=1))


# ---------------------------------------------------------------- command line (for the window)
def main(argv: list[str]) -> int:
    load_env()
    cmd = argv[1] if len(argv) > 1 else "status"
    try:
        if cmd == "status":
            print(json.dumps(status()))
        elif cmd == "connect":
            print(json.dumps(connect()))
        elif cmd == "disconnect":
            print(json.dumps({"message": disconnect()}))
        else:
            print(__doc__)
            return 2
    except (GoogleError, NeedsConnect) as e:
        print(json.dumps({"error": str(e)}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
