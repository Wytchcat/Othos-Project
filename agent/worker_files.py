"""Which files the Smithy's coding tasks may read, and what may be sent to a model.

Used by voice Othos (to turn "gaming dot py" into a file) and by the worker (which
checks again before reading). Enforced in code, for local models too:

- Only files inside the allowlisted folders (smithy.json "code_folders"), with a
  code/text extension, and not over max size.
- Never: .env* files, brain\\ (the user's notes), profile.md, logs\\, state\\, .git, .venv,
  node_modules, .next, databases, and anything that looks like a key file.
- Before any text goes to a model it passes redact() (worker_shared.py, shared with
  Firestarter): key-like strings are replaced with [REDACTED].
"""
from __future__ import annotations

import difflib
import json
import os
import re
from pathlib import Path

import worker_shared

HERE = Path(__file__).parent
DEFAULT_FOLDERS = [str(HERE), str(HERE.parent / "Othos-ui"), str(HERE.parent / "ui")]  # ui: shared copy
CODE_EXT = {".py", ".ps1", ".psm1", ".bat", ".cmd", ".cs", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".json",
            ".md", ".css", ".scss", ".html", ".php", ".yml", ".yaml", ".toml", ".ini", ".txt", ".sql", ".xml", ".sh"}
BLOCKED_DIRS = {"brain", "logs", "state", ".git", ".venv", "venv", "node_modules", ".next", "__pycache__",
                "drafts", "old", "icons", ".claude", "workers"}
BLOCKED_NAMES = re.compile(r"^(\.env.*|profile\.md|.*\.(db|db-wal|db-shm|sqlite|pem|key|pfx|p12|crt)|"
                           r"id_rsa.*|.*secret.*|.*credential.*|.*token.*\.json|pids\.txt)$", re.I)
MAX_BYTES = 120_000

redact = worker_shared.redact   # the one filter, shared with Firestarter


def folders() -> list[Path]:
    try:
        cfg = worker_shared.load_json(HERE / "smithy.json")
        dirs = cfg.get("code_folders") or DEFAULT_FOLDERS
    except Exception:
        dirs = DEFAULT_FOLDERS
    paths = [Path(d) if Path(d).is_absolute() else HERE / d for d in dirs]  # relative: from this folder
    return [p.resolve() for p in paths if p.exists()]


def allowed(path: str | Path) -> tuple[bool, str]:
    """(ok, why not). The single check every file read goes through."""
    try:
        p = Path(path).resolve(strict=True)
    except Exception:
        return False, "that file doesn't exist"
    root = next((f for f in folders() if p.is_relative_to(f)), None)
    if root is None:
        return False, "that file isn't in a folder I'm allowed to read"
    rel = p.relative_to(root).parts
    if any(part.lower() in BLOCKED_DIRS for part in rel[:-1]):
        return False, "that folder is private (notes, logs, settings or build files)"
    if BLOCKED_NAMES.match(p.name):
        return False, "that file is private (keys, settings or personal notes)"
    if p.suffix.lower() not in CODE_EXT:
        return False, "that isn't a code or text file"
    if not p.is_file() or p.stat().st_size > MAX_BYTES:
        return False, "that file is too big for a review"
    return True, ""


def read_for_model(path: str | Path) -> str:
    ok, why = allowed(path)
    if not ok:
        raise ValueError(why)
    return redact(Path(path).read_text(encoding="utf-8", errors="replace"))


def candidates() -> list[Path]:
    """Readable code files. Blocked folders are skipped without walking into them
    (Othos-ui's node_modules alone has hundreds of thousands of files)."""
    out = []
    for root in folders():
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in BLOCKED_DIRS and not d.startswith(".")]
            for name in filenames:
                if Path(name).suffix.lower() in CODE_EXT and not BLOCKED_NAMES.match(name):
                    out.append(Path(dirpath) / name)
    return out


_SPOKEN = [(r"\s+dot\s+", "."), (r"\s+(?:dash|hyphen)\s+", "-"), (r"\s+underscore\s+", "_"),
           (r"\bpie\b", "py"), (r"\bpython\b", "py"), (r"\bpowershell\b", "ps1")]


def spoken_to_name(words: str) -> str:
    """'gaming dot py' -> 'gaming.py'; 'start dash othos dot ps1' -> 'start-othos.ps1'."""
    s = " " + words.lower().strip(" .,!?") + " "
    for rx, rep in _SPOKEN:
        s = re.sub(rx, rep if rep in ".-_" else f" {rep} ", s)
    s = re.sub(r"\s*\.\s*", ".", s)
    s = re.sub(r"\s*-\s*", "-", s)
    s = re.sub(r"\s*_\s*", "_", s)
    return re.sub(r"\s+", "", s) if "." in s else re.sub(r"\s+", " ", s).strip()


def find(words: str) -> tuple[list[Path], str]:
    """Files matching what the user said, only inside the allowlisted folders.
    Returns (matches, the name looked for): one match = found; several = ask which."""
    name = spoken_to_name(words)
    files = candidates()
    exact = [p for p in files if p.name.lower() == name]
    if exact:
        return exact, name
    stem = [p for p in files if p.stem.lower() == name.split(".")[0] and "." not in name]
    if stem:
        return stem, name
    close = difflib.get_close_matches(name, [p.name.lower() for p in files], n=3, cutoff=0.8)
    return [p for p in files if p.name.lower() in close], name
