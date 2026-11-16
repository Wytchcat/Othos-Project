"""Download public documentation pages into the shared library (library.py): one fetch step for every caller.

The site worker's "Download docs" (extension docs, ext_docs.py) and the Academy's "Download language docs" both use
this. A caller passes data only: the start pages, the library source folder (with a version in it where it has one,
like "python/3.12"), and optionally a page cap, an exact scope, a licence and a version. The caller's own window
shows what will be fetched and asks first; crawl() refuses to run unless the call says approved=True.

Rules:
- public pages only: no keys, no logins, no cookies (none kept or sent back); https only; the start page's own
  site and docs folder only; never this PC or the home network (localhost, private addresses)
- redirects are followed one hop at a time, each checked like a new page (https, same site, robots.txt)
- robots.txt is followed (and its crawl delay, up to 30 s); a robots.txt that answers 401/403/5xx means "not
  allowed" and stops it; at most one page every 2 seconds; a page cap per call; pages over 3 MB aren't read
- a site that answers "busy" or puts up a robot check (202/403/429/503) is left alone, never worked around: the
  result says to read it in the browser
- text pages only: PDFs, archives and pictures are skipped (nothing is downloaded as a file, nothing is run).
  A maker's downloadable docs archive needs download_scan.vet_download(kind="program") first (not done here yet).
- each page keeps its licence (the caller's, or the site's own copyright line), its address and the date fetched;
  the library folder is never committed or put in the public copy

Progress is printed as one JSON line per event ({"event": "progress", "done", "total", "status"}), like model_fetch.
"""
import hashlib
import ipaddress
import json
import re
import socket
import time
import urllib.robotparser
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

import library

UA = "Othos-docs-fetch/1.0 (downloads public documentation for one person's own use)"
MAX_PAGES = 80                 # the default cap per call; a caller with a bigger docs set passes its own
MAX_BYTES = 3_000_000          # a single page bigger than this is skipped
DELAY = 2.0                    # seconds between pages (more if robots.txt asks)
MIN_TEXT = 200                 # a page with less text than this (an empty script-built page) is skipped
BLOCKED = {202, 403, 429, 503}
REDIRECT = {301, 302, 303, 307, 308}
MAX_HOPS = 5                   # redirects followed per page, each one checked
MAX_WAIT = 30.0                # a robots.txt asking for longer than this between pages: stop and say so
SKIP_EXT = re.compile(r"\.(pdf|zip|gz|tgz|rar|7z|exe|msi|dmg|png|jpe?g|gif|webp|svg|ico|mp4|mp3|webm|css|js|json|xml|txt)$", re.I)


def emit(**ev) -> None:
    print(json.dumps(ev, ensure_ascii=False), flush=True)


# ---------------------------------------------------------------- where a docs site starts and ends
def scope_of(start_url: str, final_url: str) -> str:
    """The folder a crawl stays in. A start page that redirects to a first chapter (yootheme.com/docs/ ->
    /docs/joomla/introduction) means that chapter's folder; "guide.html" means guide.html and guide/...;
    anything else means the path as a folder."""
    path = urlsplit(final_url).path or "/"
    if urlsplit(start_url).path.rstrip("/") != path.rstrip("/"):
        return path.rsplit("/", 1)[0] + "/"
    if re.search(r"\.html?$", path, re.I):
        return re.sub(r"\.html?$", "", path, flags=re.I) + "/"
    return path if path.endswith("/") else path + "/"


def in_scope(url: str, host: str, scope: str, start_path: str) -> bool:
    u = urlsplit(url)
    if u.scheme != "https" or u.netloc.lower() != host or SKIP_EXT.search(u.path):
        return False
    p = u.path or "/"
    return p == start_path or p.startswith(scope) or p.rstrip("/") + "/" == scope


def normal(url: str) -> str:
    """No fragment, no query (pagination and tracking only make duplicates), no trailing index.html."""
    u = urlsplit(url)
    path = re.sub(r"/index\.html?$", "/", u.path, flags=re.I) or "/"
    return urlunsplit((u.scheme.lower(), u.netloc.lower(), path, "", ""))


def slug_of(url: str, scope: str) -> str:
    path = urlsplit(url).path
    rest = path[len(scope):] if path.startswith(scope) else path.rsplit("/", 1)[-1]
    rest = re.sub(r"\.html?$", "", rest.strip("/"), flags=re.I)
    return re.sub(r"[^a-z0-9._-]+", "-", rest.lower()).strip("-.")[:80] or "index"


# ---------------------------------------------------------------- HTML to plain markdown
class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "button", "iframe",
            "template", "select", "dialog"}
    BLOCK = {"p", "div", "section", "article", "main", "table", "tr", "ul", "ol", "dl", "blockquote", "figure"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.main = 0
        self.saw_main = False
        self.pre = 0
        self.parts: list[tuple[bool, str]] = []        # (inside main/article, text)
        self.links: list[str] = []
        self.title = ""
        self.h1 = ""
        self._in_title = self._in_h1 = False
        self.foot: list[str] = []
        self._in_foot = 0

    def _add(self, s: str) -> None:
        self.parts.append((self.main > 0, s))

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])                # menus too: a docs site lists its chapters there
        if tag == "footer":
            self._in_foot += 1
        if tag in self.SKIP or a.get("aria-hidden") == "true" or a.get("role") in ("navigation", "banner"):
            if tag not in ("br", "img", "input", "hr"):
                self.skip += 1
            return
        if self.skip:
            return
        if tag in ("main", "article") or a.get("role") == "main":
            self.main += 1
            self.saw_main = True
        if tag == "title":
            self._in_title = True
        elif re.fullmatch(r"h[1-6]", tag):
            if tag == "h1" and not self.h1:
                self._in_h1 = True
            self._add("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self._add("\n- ")
        elif tag == "br":
            self._add("\n")
        elif tag == "pre":
            self.pre += 1
            self._add("\n\n```\n")
        elif tag in ("td", "th"):
            self._add(" | ")
        elif tag in self.BLOCK:
            self._add("\n\n")

    def handle_endtag(self, tag):
        if tag == "footer" and self._in_foot:
            self._in_foot -= 1
        if tag in self.SKIP:
            if self.skip:
                self.skip -= 1
            return
        if self.skip:
            return
        if tag in ("main", "article") and self.main:
            self.main -= 1
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False
        elif tag == "pre" and self.pre:
            self.pre -= 1
            self._add("\n```\n\n")
        elif re.fullmatch(r"h[1-6]", tag) or tag in self.BLOCK:
            self._add("\n\n")

    def handle_data(self, data):
        if self._in_foot:
            self.foot.append(data)
        if self._in_title:
            self.title += data
            return
        if self.skip:
            return
        if self._in_h1:
            self.h1 += data
        self._add(data if self.pre else re.sub(r"\s+", " ", data))


def to_markdown(html: str) -> dict:
    """{"title", "text", "links", "licence"} from one page. Only the page's main part when it marks one."""
    t = _Text()
    t.feed(html)
    t.close()
    parts = [s for inside, s in t.parts if inside] if t.saw_main else [s for _, s in t.parts]
    text = "".join(parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    foot = " ".join(" ".join(t.foot).split())
    m = re.search(r"((?:©|&copy;|copyright)[^|•\n]{0,120})", foot or " ".join(html.split())[-4000:], re.I)
    licence = " ".join(m.group(1).split())[:140] if m else "unknown"
    title = " ".join((t.h1 or t.title).split())[:200]
    return {"title": title, "text": text, "links": t.links, "licence": licence}


# ---------------------------------------------------------------- one page, safely
class _Stop(Exception):
    """The whole crawl stops here; the message is for the person."""


def _bad_ip(ip) -> bool:
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified


def private_host(host: str, resolve: bool = True) -> bool:
    """This PC, the home network or another internal address: never fetched, whatever a page links or redirects to."""
    h = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    h = h.strip("[]").lower().rstrip(".")
    if h == "localhost" or h.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        return True
    try:
        return _bad_ip(ipaddress.ip_address(h))
    except ValueError:
        pass
    if not resolve:
        return False
    try:
        infos = socket.getaddrinfo(h, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False                    # unknown name: the request fails on its own
    return any(_bad_ip(ipaddress.ip_address(i[4][0].split("%")[0])) for i in infos)


class _Fetcher:
    """GETs with no cookies kept, redirects followed by hand (each hop https, an allowed host, not internal, and
    allowed by that host's robots.txt), and a page read only up to MAX_BYTES."""

    def __init__(self, client: httpx.Client, resolve: bool):
        self.client, self.resolve, self.robots = client, resolve, {}

    def _open(self, url: str):
        self.client.cookies.clear()                  # no cookies, ever: nothing a site sets is sent back
        return self.client.stream("GET", url, follow_redirects=False)   # each hop is checked here, by hand

    def robots_for(self, host: str) -> urllib.robotparser.RobotFileParser:
        if host in self.robots:
            return self.robots[host]
        url, why = f"https://{host}/robots.txt", ""
        rp = urllib.robotparser.RobotFileParser()
        for _ in range(MAX_HOPS + 1):
            u = urlsplit(url)
            if u.scheme != "https" or private_host(u.netloc, self.resolve):
                why = "sends its robots.txt somewhere unsafe"
                break
            try:
                with self._open(url) as r:
                    loc = r.headers.get("location")
                    if r.status_code in REDIRECT and loc:
                        url = urljoin(url, loc)
                        continue
                    if r.status_code == 200:
                        body = b""
                        for chunk in r.iter_bytes():
                            body += chunk
                            if len(body) > 500_000:
                                break
                        rp.parse(body.decode("utf-8", errors="replace").splitlines())
                    elif r.status_code in (404, 410):
                        rp.parse([])                 # no robots file: everything allowed
                    else:                            # 401/403: not allowed; 5xx and the rest: not now
                        why = f"answered {r.status_code} for its robots.txt"
            except httpx.HTTPError as ex:
                why = f"couldn't be asked for its robots.txt ({type(ex).__name__})"
            break
        else:
            why = "redirected its robots.txt too many times"
        self.client.cookies.clear()
        if why:
            raise _Stop(f"{host} {why}, so its docs weren't downloaded. Read them in your browser.")
        self.robots[host] = rp
        return rp

    def get(self, url: str, hosts: set, new_host_once: bool = False) -> dict:
        """{"url", "status", "type", "text", "skip"}: skip says why a page wasn't read (text is None then)."""
        for _ in range(MAX_HOPS + 1):
            u = urlsplit(url)
            host = u.netloc.lower()
            if u.scheme != "https":
                return {"url": url, "skip": "not https"}
            if host not in hosts:
                if not new_host_once:
                    return {"url": url, "skip": "another site"}
                new_host_once = False                # the start page may move to its docs site, once
                hosts.add(host)
            if private_host(host, self.resolve):
                return {"url": url, "skip": "an internal address"}
            if not self.robots_for(host).can_fetch(UA, url):
                return {"url": url, "skip": "robots.txt"}
            try:
                with self._open(url) as r:
                    loc = r.headers.get("location")
                    if r.status_code in REDIRECT and loc:
                        url = urljoin(url, loc)
                        continue
                    out = {"url": url, "status": r.status_code, "type": r.headers.get("content-type", ""),
                           "text": None, "skip": ""}
                    if r.status_code != 200:
                        return out
                    if int(r.headers.get("content-length") or 0) > MAX_BYTES:
                        return {**out, "skip": "too big"}
                    body = bytearray()
                    for chunk in r.iter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            return {**out, "skip": "too big"}
                    out["text"] = bytes(body).decode(r.encoding or "utf-8", errors="replace")
                    return out
            finally:
                self.client.cookies.clear()
        return {"url": url, "skip": "too many redirects"}


def _free_slug(source: str, slug: str, url: str, used: dict) -> str:
    """A page name not already taken by another page ("a/b" and "a-b" both make "a-b")."""
    def taken(s):
        if used.get(s, url) != url:
            return True
        path = library.LIBRARY / source / (s + ".md")
        if path.exists():
            meta, _ = library.parse_page(path)
            return meta.get("url", url) not in ("", url)
        return False
    if taken(slug):
        slug = slug[:73].rstrip("-.") + "-" + hashlib.sha1(url.encode()).hexdigest()[:6]
    used[slug] = url
    return slug


# ---------------------------------------------------------------- the crawl
def crawl(start_urls: list[str], source: str, *, approved: bool, max_pages: int = MAX_PAGES, scope: str | None = None,
          licence: str | None = None, version: str = "", delay: float = DELAY,
          client: httpx.Client | None = None) -> dict:
    """Read the docs pages under each start page into library source `source`.
    approved: the caller's window asked and the person said yes (nothing is fetched without it).
    scope: an exact path prefix to stay in ("/3.12/"); None works it out from the start page.
    licence: the docs' licence ("PSF-2.0"); None keeps each page's own copyright line.
    Returns {"pages", "skipped", "blocked", "cut", "source"}."""
    if approved is not True:
        raise PermissionError("not approved: the caller's window must ask first")
    if not start_urls:
        raise ValueError("no start page")
    own = client is None
    for start in start_urls:
        if urlsplit(start).scheme != "https":
            raise ValueError(f"not an https address: {start}")
        if private_host(urlsplit(start).netloc, resolve=own):
            raise ValueError(f"an internal address, never fetched: {start}")
    source = library.clean_source(source)
    max_pages = max(1, int(max_pages))
    client = client or httpx.Client(headers={"User-Agent": UA}, timeout=httpx.Timeout(30, read=60),
                                    follow_redirects=False)
    fetcher = _Fetcher(client, resolve=own)
    saved, skipped, blocked, cut, used = 0, 0, "", False, {}
    try:
        for start in start_urls:
            hosts = {urlsplit(start).netloc.lower()}
            queue, seen = [start], set()
            here, start_path, first, wait, host = scope or "", "", True, delay, ""
            while queue:
                if saved >= max_pages:
                    cut = True
                    break
                url = queue.pop(0)
                if url in seen:
                    continue
                seen.add(url)
                emit(event="progress", done=saved, total=max_pages, status=f"Reading {url}")
                try:
                    r = fetcher.get(url, hosts, new_host_once=first)
                except httpx.HTTPError as ex:
                    skipped += 1
                    emit(event="progress", done=saved, total=max_pages, status=f"Skipped {url}: {type(ex).__name__}")
                    continue
                if r.get("status") in BLOCKED:
                    where = urlsplit(r["url"]).netloc
                    if first:              # the start page itself: the site doesn't let programs read it
                        blocked = (f"{where} answered {r['status']} (busy or a robot check), so its docs weren't "
                                   f"downloaded. Read them in your browser: {start}")
                    else:
                        blocked = f"{where} answered {r['status']} part way through; stopped there, as asked."
                    break
                final = normal(r["url"])
                if final != url and final in seen:          # a redirect to a page already read
                    continue
                seen.add(final)
                if first:
                    if r["skip"] or r.get("status") != 200:
                        skipped += 1
                        emit(event="progress", done=saved, total=max_pages,
                             status=f"The start page couldn't be read ({r['skip'] or r.get('status')}): {start}")
                        break
                    first = False
                    here = here or scope_of(start, final)
                    start_path = urlsplit(final).path or "/"
                    host = urlsplit(final).netloc.lower()
                    asked = float(fetcher.robots_for(host).crawl_delay(UA) or 0)
                    if asked > MAX_WAIT:
                        blocked = (f"{host} asks programs to wait {asked:g} seconds between pages, too slow to "
                                   f"download its docs. Read them in your browser: {start}")
                        break
                    wait = max(delay, asked)
                if r["skip"] or r.get("status") != 200 or "html" not in r.get("type", "") \
                        or not in_scope(final, host, here, start_path):
                    skipped += 1
                    continue
                page = to_markdown(r["text"])
                if len(page["text"]) >= MIN_TEXT:
                    slug = _free_slug(source, slug_of(final, here), final, used)
                    library.add_page(source, slug, page["title"] or slug, final, page["text"],
                                     licence=licence or page["licence"], version=version)
                    saved += 1
                else:
                    skipped += 1
                for href in page["links"]:
                    nxt = normal(urljoin(final, href))
                    if nxt not in seen and nxt not in queue and in_scope(nxt, host, here, start_path):
                        queue.append(nxt)
                time.sleep(wait)
            if blocked:
                break
    except _Stop as stop:
        blocked = str(stop)
    finally:
        if own:
            client.close()
    return {"pages": saved, "skipped": skipped, "blocked": blocked, "source": source, "cut": cut}
