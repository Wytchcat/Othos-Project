"""What the "Add documents" window does, without the window (so it can be tested and used from other code).

  add_local(paths, source, licence, say)   the person's own .pdf / .txt / .md files and folders on this computer
  add_s3(keys, source, licence, say)       PDFs copied from the person's S3 bucket (s3_source.py), scanned, then added

Both turn each file into a library page (library.py) and report one (name, ok, message) per file. A person's own
file is never moved or deleted. A file that fails the type check or the virus scan is not added.
"""
import json
import tempfile
from pathlib import Path

import download_scan
import library
import s3_source

HERE = Path(__file__).resolve().parent
STATE = HERE / "state" / "library_window.json"
KINDS = {".pdf", ".txt", ".md"}


def ui_state() -> dict:
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_ui_state(**kw) -> None:
    d = {**ui_state(), **kw}
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(d, indent=2), encoding="utf-8")


def expand(paths) -> list[Path]:
    """Files as given; folders become the .pdf/.txt/.md files inside (all levels)."""
    out = []
    for p in map(Path, paths):
        if p.is_dir():
            out += sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in KINDS)
        elif p.is_file():
            out.append(p)
    seen, unique = set(), []
    for f in out:
        if f.resolve() not in seen:
            seen.add(f.resolve())
            unique.append(f)
    return unique


def _check_own_file(f: Path) -> str:
    """'' if fine, else why not. Scans in place; never moves the person's file."""
    if f.suffix.lower() not in KINDS:
        return f"{f.suffix or 'that'} files aren't supported (use .pdf, .txt or .md)"
    if f.suffix.lower() == ".pdf" and not download_scan.check_pdf(f):
        return "it isn't a real PDF"
    r = download_scan.scan(f, "data")
    if r["status"] == "found":
        return r["detail"] + " It was not added (your file was left where it is)."
    return ""


def add_local(paths, source: str, licence: str = "unknown", say=lambda s: None) -> list[tuple]:
    results = []
    files = expand(paths)
    if not files:
        say("No .pdf, .txt or .md files found there.")
    for f in files:
        say(f"Adding {f.name}...")
        try:
            why = _check_own_file(f)
            if why:
                raise ValueError(why)
            library.add_file(f, source, licence)
            results.append((f.name, True, "added"))
        except Exception as e:           # one bad file doesn't stop the rest
            results.append((f.name, False, str(e)))
    return results


def add_s3(keys, source: str, licence: str = "unknown", say=lambda s: None) -> list[tuple]:
    results = []
    with tempfile.TemporaryDirectory() as d:
        for key in keys:
            name = Path(key).name or key
            say(f"Copying {name} from AWS...")
            try:
                dest = Path(d) / (library._segment(Path(key).stem) + ".pdf")
                s3_source.download(key, dest)
                say(f"Scanning {name}...")
                r = download_scan.vet_download(dest, "data")
                if not r["ok"]:
                    raise ValueError(r["detail"])
                library.add_file(dest, source, licence, title=Path(key).stem,
                                 url="s3://" + s3_source.settings()["bucket"] + "/" + key)
                results.append((name, True, "added" + ("" if r["status"] == "clean" else " (not virus-scanned: " + r["detail"].split(":")[-1].strip() + ")")))
            except Exception as e:
                results.append((name, False, str(e)))
    return results
