"""The local library: reference pages kept on this PC and searched without the internet.

One page = one UTF-8 markdown file under library\\<source>\\<slug>.md with a short front matter
(title, url, source, licence, fetched, version). Everything is searched through ONE index
(library\\.index.sqlite, SQLite full-text search), so Othos and its workers share one copy and one search.

    import library
    library.add_page("wordpress/extensions/woocommerce", "orders", "Orders", "https://...", text,
                     licence="GPL-2.0", version="9.4")
    library.search("refund an order", source="wordpress/extensions/woocommerce")
    # -> [{"title", "url", "source", "slug", "snippet", "fetched", "licence", "version", "score"}]

A source is a folder path ("joomla/extensions/zoo"). search(source=X) matches X and everything under it,
so "joomla" searches all of Joomla and "joomla/extensions/zoo" only ZOO.

The library folder is never committed and never in the public copy (.gitignore). Third-party pages keep
their licence line, and answers should cite title, url and fetched date.

Command line (.venv\\Scripts\\python.exe library.py ...):
    search QUERY [--source S] [--limit N]    add-file PATH --source S [--licence L]    list    reindex    remove SOURCE SLUG
"""
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIBRARY = Path(os.getenv("OTHOS_LIBRARY") or HERE / "library")
MAX_PAGE_CHARS = 2_000_000
CHUNK_CHARS = 1200
_SEG = re.compile(r"[^a-z0-9._-]+")
_last_sync = 0.0


# ---------------------------------------------------------------- names and paths
def _segment(s: str) -> str:
    s = _SEG.sub("-", str(s).strip().lower()).strip("-.")
    if not s or s in (".", ".."):
        raise ValueError(f"not a usable name: {s!r}")
    return s[:80]


def clean_source(source: str) -> str:
    """'WordPress/Extensions/Woo Commerce' -> 'wordpress/extensions/woo-commerce' (no .., no drive letters)."""
    parts = [p for p in re.split(r"[\\/]+", str(source).strip()) if p]
    if not parts:
        raise ValueError("a source is needed (like wordpress/extensions/woocommerce)")
    return "/".join(_segment(p) for p in parts)


def _page_path(source: str, slug: str) -> Path:
    p = (LIBRARY / clean_source(source) / (_segment(slug) + ".md")).resolve()
    if LIBRARY.resolve() not in p.parents:
        raise ValueError("that path is outside the library")
    return p


# ---------------------------------------------------------------- pages
def _front(meta: dict) -> str:
    return "---\n" + "".join(f"{k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in meta.items()) + "---\n\n"


def parse_page(path: Path) -> tuple[dict, str]:
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    meta, body = {}, raw
    if raw.startswith("---\n"):
        head, _, rest = raw[4:].partition("\n---\n")
        for line in head.splitlines():
            k, _, v = line.partition(": ")
            if not k:
                continue
            try:
                meta[k] = json.loads(v)
            except ValueError:
                meta[k] = v
        body = rest.lstrip("\n")
    return meta, body


def add_page(source: str, slug: str, title: str, url: str, text: str, licence: str = "unknown",
             version: str = "", fetched: str = "") -> Path:
    """Save (or replace) one page and index it. Returns the file's path."""
    text = (text or "").strip()
    if not text:
        raise ValueError("the page has no text")
    path = _page_path(source, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"title": (title or slug).strip(), "url": url or "", "source": clean_source(source), "licence": licence or "unknown",
            "fetched": fetched or date.today().isoformat(), "version": version or ""}
    path.write_text(_front(meta) + text[:MAX_PAGE_CHARS] + "\n", encoding="utf-8")
    with _db() as db:
        _index_file(db, path)
    return path


def remove(source: str, slug: str) -> bool:
    path = _page_path(source, slug)
    with _db() as db:
        db.execute("DELETE FROM chunks WHERE path = ?", (str(path),))
        db.execute("DELETE FROM pages WHERE path = ?", (str(path),))
    if path.exists():
        path.unlink()
        return True
    return False


def add_file(path, source: str, licence: str = "unknown", title: str = "") -> Path:
    """Add one of the person's own files (.txt, .md, .pdf) as a page. PDFs need the pypdf package."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in (".txt", ".md"):
        text = path.read_text(encoding="utf-8", errors="replace")
    elif ext == ".pdf":
        text, pdf_title = _pdf_text(path)
        title = title or pdf_title
    else:
        raise ValueError(f"{ext or 'that'} files aren't supported yet (use .txt, .md or .pdf)")
    if len(text.strip()) < 40:
        raise ValueError(f"{path.name} has no readable text (a scanned PDF would need OCR)")
    return add_page(source, path.stem, title or path.stem, "file:" + path.name, text, licence=licence)


def _pdf_text(path: Path) -> tuple[str, str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("Reading PDFs needs the pypdf package, which isn't installed yet.") from None
    r = PdfReader(str(path))
    pages = [(pg.extract_text() or "").strip() for pg in r.pages]
    title = ""
    try:
        title = (r.metadata.title or "").strip() if r.metadata else ""
    except Exception:
        pass
    return "\n\n".join(p for p in pages if p), title


# ---------------------------------------------------------------- the index
def _db() -> sqlite3.Connection:
    LIBRARY.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(LIBRARY / ".index.sqlite"), timeout=30)
    db.execute("CREATE TABLE IF NOT EXISTS pages (path TEXT PRIMARY KEY, mtime REAL, size INTEGER)")
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5("
               "title, text, path UNINDEXED, source UNINDEXED, slug UNINDEXED, url UNINDEXED, fetched UNINDEXED, "
               "licence UNINDEXED, version UNINDEXED, tokenize='porter unicode61')")
    return db


def _chunks(body: str) -> list[str]:
    out, cur = [], ""
    for para in re.split(r"\n\s*\n", body):
        para = para.strip()
        if not para:
            continue
        if cur and len(cur) + len(para) > CHUNK_CHARS:
            out.append(cur)
            cur = ""
        while len(para) > CHUNK_CHARS * 2:          # a huge paragraph: cut it on a space
            cut = para.rfind(" ", 0, CHUNK_CHARS) or CHUNK_CHARS
            out.append((cur + "\n\n" + para[:cut]).strip() if cur else para[:cut])
            cur, para = "", para[cut:].strip()
        cur = (cur + "\n\n" + para).strip() if cur else para
    if cur:
        out.append(cur)
    return out


def _index_file(db: sqlite3.Connection, path: Path) -> None:
    path = Path(path)
    meta, body = parse_page(path)
    db.execute("DELETE FROM chunks WHERE path = ?", (str(path),))
    rel = path.parent.resolve().relative_to(LIBRARY.resolve()).as_posix()
    for part in _chunks(body):
        db.execute("INSERT INTO chunks (title, text, path, source, slug, url, fetched, licence, version) VALUES (?,?,?,?,?,?,?,?,?)",
                   (meta.get("title", path.stem), part, str(path), rel, path.stem, meta.get("url", ""), meta.get("fetched", ""),
                    meta.get("licence", "unknown"), meta.get("version", "")))
    st = path.stat()
    db.execute("INSERT OR REPLACE INTO pages (path, mtime, size) VALUES (?,?,?)", (str(path), st.st_mtime, st.st_size))


def reindex(force: bool = False) -> int:
    """Bring the index in line with the files (pages added, changed or deleted by hand). Returns pages indexed."""
    global _last_sync
    n = 0
    with _db() as db:
        known = {p: (m, s) for p, m, s in db.execute("SELECT path, mtime, size FROM pages")}
        seen = set()
        for path in LIBRARY.rglob("*.md"):
            if any(part.startswith(".") for part in path.relative_to(LIBRARY).parts):
                continue
            key = str(path)
            seen.add(key)
            st = path.stat()
            if force or known.get(key) != (st.st_mtime, st.st_size):
                _index_file(db, path)
                n += 1
        for gone in set(known) - seen:
            db.execute("DELETE FROM chunks WHERE path = ?", (gone,))
            db.execute("DELETE FROM pages WHERE path = ?", (gone,))
    _last_sync = time.monotonic()
    return n


_STOP = {"a", "an", "the", "is", "are", "to", "of", "in", "on", "and", "or", "for", "how", "do", "i", "my", "me", "it", "can", "what"}


def _match_query(query: str, any_word: bool = False) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9_]+", query.lower()) if w not in _STOP and len(w) > 1]
    return (" OR " if any_word else " ").join(f'"{w}"' for w in words)


def search(query: str, source: str = "", limit: int = 5) -> list[dict]:
    """Best matching pages for a question, one result per page. source limits to a folder and everything under it."""
    if time.monotonic() - _last_sync > 60:
        reindex()
    src = clean_source(source) if source else ""
    results: dict[str, dict] = {}
    with _db() as db:
        for any_word in (False, True):           # every word first; if nothing, any word
            match = _match_query(query, any_word)
            if not match:
                return []
            sql = ("SELECT title, source, slug, url, fetched, licence, version, path, "
                   "snippet(chunks, 1, '', '', ' ... ', 24), bm25(chunks, 4.0, 1.0) FROM chunks WHERE chunks MATCH ?")
            args: list = [match]
            if src:
                sql += " AND (source = ? OR source LIKE ?)"
                args += [src, src + "/%"]
            sql += " ORDER BY bm25(chunks, 4.0, 1.0) LIMIT ?"
            args.append(limit * 6)
            for t, s, sl, u, f, lic, v, path, snip, score in db.execute(sql, args):
                if path not in results:
                    results[path] = {"title": t, "source": s, "slug": sl, "url": u, "fetched": f, "licence": lic,
                                     "version": v, "snippet": snip.replace("\n", " ").strip(), "score": round(-score, 3)}
            if results:
                break
    return list(results.values())[:limit]


def sources() -> dict[str, int]:
    """Pages per source folder."""
    reindex()
    with _db() as db:
        return dict(db.execute("SELECT source, COUNT(DISTINCT path) FROM chunks GROUP BY source ORDER BY source"))


def cite(r: dict) -> str:
    """'Orders (https://..., fetched 2026-11-16, GPL-2.0)': what an answer should say about where it came from."""
    bits = [b for b in (r.get("url"), f"fetched {r['fetched']}" if r.get("fetched") else "", r.get("licence")) if b]
    return f"{r['title']} ({', '.join(bits)})"


def main(argv: list[str]) -> int:
    def opt(name, default=""):
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    try:
        if cmd == "search" and len(argv) > 2:
            for r in search(argv[2], opt("--source"), int(opt("--limit", "5"))):
                print(f"{cite(r)}\n   [{r['source']}] {r['snippet']}")
        elif cmd == "add-file" and len(argv) > 2:
            print(add_file(argv[2], opt("--source", "mine"), opt("--licence", "unknown")))
        elif cmd == "list":
            for s, n in sources().items():
                print(f"{n:5d}  {s}")
        elif cmd == "reindex":
            print(f"{reindex(force=True)} pages indexed")
        elif cmd == "remove" and len(argv) > 3:
            print("removed" if remove(argv[2], argv[3]) else "not found")
        else:
            print(__doc__)
            return 2
    except (ValueError, RuntimeError, OSError) as e:
        print(f"library: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
