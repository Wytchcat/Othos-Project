"""Download public documentation pages into the shared library (library.py): one fetch step for every caller.

The site worker's "Download docs" (extension docs, ext_docs.py) and the Academy's "Download language docs" both use
this. A caller passes data only: the start pages, the library source folder (with a version in it where it has one,
like "python/3.12"), and optionally a page cap, an exact scope, a licence and a version. The caller's own window
shows what will be fetched and asks first; crawl() refuses to run unless the call says approved=True.

Rules:
- public pages only: no keys, no logins, no cookies; https only; the start page's own site and docs folder only
- robots.txt is followed (and its crawl delay); at most one page every 2 seconds; a page cap per call
- a site that answers "busy" or puts up a robot check (202/403/429/503) is left alone, never worked around: the
  result says to read it in the browser
- text pages only: PDFs, archives and pictures are skipped (nothing is downloaded as a file, nothing is run).
  A maker's downloadable docs archive needs download_scan.vet_download(kind="program") first (not done here yet).
- each page keeps its licence (the caller's, or the site's own copyright line), its address and the date fetched;
  the library folder is never committed or put in the public copy

Progress is printed as one JSON line per event ({"event": "progress", "done", "total", "status"}), like model_fetch.
"""
import json
import re
import urllib.robotparser
import time
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


# ---------------------------------------------------------------- the crawl
def _robots(client: httpx.Client, host: str) -> urllib.robotparser.RobotFileParser:
    rp = urllib.robotparser.RobotFileParser()
    try:
        r = client.get(f"https://{host}/robots.txt")
        rp.parse(r.text.splitlines() if r.status_code == 200 else [])
    except httpx.HTTPError:
        rp.parse([])
    return rp


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
    for start in start_urls:
        if urlsplit(start).scheme != "https":
            raise ValueError(f"not an https address: {start}")
    source = library.clean_source(source)
    max_pages = max(1, int(max_pages))
    own = client is None
    client = client or httpx.Client(headers={"User-Agent": UA}, timeout=httpx.Timeout(30, read=60),
                                    follow_redirects=True)
    saved, skipped, blocked, cut = 0, 0, "", False
    try:
        for start in start_urls:
            host = urlsplit(start).netloc.lower()
            rp = _robots(client, host)
            wait = max(delay, float(rp.crawl_delay(UA) or 0))
            queue, seen = [start], set()
            here, start_path, first = scope or "", "", True
            while queue:
                if saved >= max_pages:
                    cut = True
                    break
                url = queue.pop(0)
                if url in seen:
                    continue
                seen.add(url)
                if not rp.can_fetch(UA, url):
                    skipped += 1
                    continue
                emit(event="progress", done=saved, total=max_pages, status=f"Reading {url}")
                try:
                    r = client.get(url)
                except httpx.HTTPError as ex:
                    skipped += 1
                    emit(event="progress", done=saved, total=max_pages, status=f"Skipped {url}: {type(ex).__name__}")
                    continue
                if r.status_code in BLOCKED:
                    if first:              # the start page itself: the site doesn't let programs read it
                        blocked = (f"{host} answered {r.status_code} (busy or a robot check), so its docs weren't "
                                   f"downloaded. Read them in your browser: {start}")
                    else:
                        blocked = f"{host} answered {r.status_code} part way through; stopped there, as asked."
                    break
                final = normal(str(r.url))
                if final != url and final in seen:          # a redirect to a page already read
                    continue
                seen.add(final)
                if first:
                    first = False
                    here = here or scope_of(start, final)
                    start_path = urlsplit(final).path or "/"
                    host = urlsplit(final).netloc.lower()
                if r.status_code != 200 or "html" not in r.headers.get("content-type", "") \
                        or len(r.content) > MAX_BYTES or not in_scope(final, host, here, start_path):
                    skipped += 1
                    continue
                page = to_markdown(r.text)
                if len(page["text"]) >= MIN_TEXT:
                    slug = slug_of(final, here)
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
    finally:
        if own:
            client.close()
    return {"pages": saved, "skipped": skipped, "blocked": blocked, "source": source, "cut": cut}
