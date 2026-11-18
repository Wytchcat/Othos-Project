"""The Smithy's language checkers (Nov 16, phase 1 of the checkers plan): tools that read a build's files and
report exact errors, so "checks clean" means the tools agree, not only a model (task 56: a model ticked changes
DONE that weren't in the files).

Each checker reads the code and never runs it: ruff and pyright (Python), PSScriptAnalyzer (PowerShell), tsc
(TypeScript) and javac (Java; its .class files go to a temporary folder that's deleted). One that isn't installed
is skipped and said as "not checked". Problems use the build check's shape, "<file> line <n>: <message>", so the
fix rounds send each to its own file. A few per file at most, since a fix round can only take so many.

Installed for the user's user only (Nov 16): ruff in .venv; pyright and TypeScript with npm (%APPDATA%\\npm);
PSScriptAnalyzer from the PowerShell Gallery; the Temurin JDK unpacked in %LOCALAPPDATA%\\Programs\\Temurin.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_NO_WINDOW = 0x08000000                                  # CREATE_NO_WINDOW
PER_FILE = 8                                             # problems kept per file and checker
TIMEOUT = 180


def _tool(name: str, *extra: Path) -> str | None:
    """A tool by name on PATH, else in the places it was installed for this user."""
    found = shutil.which(name)
    if found:
        return found
    for p in extra:
        if p.is_file():
            return str(p)
    return None


def _npm(name: str) -> str | None:
    return _tool(name, Path(os.environ.get("APPDATA", "")) / "npm" / f"{name}.cmd")


def _javac() -> str | None:
    found = shutil.which("javac")
    if found:
        return found
    jdks = sorted((Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Temurin").glob("jdk-*/bin/javac.exe"))
    return str(jdks[-1]) if jdks else None


def _run(cmd: list[str], cwd: Path, **k) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=TIMEOUT, creationflags=_NO_WINDOW if os.name == "nt" else 0, **k)
    except (OSError, subprocess.SubprocessError):
        return None


def _rel(folder: Path, path: str, rels: list[str]) -> str:
    """A tool's path back to the build's own name for the file."""
    try:
        r = Path(path).resolve().relative_to(folder.resolve()).as_posix()
    except ValueError:
        r = Path(path).name
    return next((x for x in rels if x.replace("\\", "/") == r), r)


def _cap(found: list[tuple[str, int, str]]) -> list[str]:
    out, per = [], {}
    for rel, line, msg in found:
        per[rel] = per.get(rel, 0) + 1
        if per[rel] <= PER_FILE:
            out.append(f"{rel} line {line}: {msg}")
    return out


# ---------------------------------------------------------------- Python
def ruff_errors(folder: Path, rels: list[str]) -> list[str] | None:
    """ruff's serious checks only: syntax, undefined names, `return` outside a function and the like (E9, F63,
    F7, F82), and a name defined again before it's used (F811: Nov 16, task 56's test file held a pasted copy of
    academy_readme.py's functions, overwritten by the import below them; it also catches two tests with one name,
    where only the second runs), and an unused import or variable (F401, F841: the user's benchmarks, Nov 17: no dead
    code), not style."""
    exe = _tool("ruff", Path(sys.executable).parent / "ruff.exe")
    if not exe:
        return None
    r = _run([exe, "check", "--no-cache", "--isolated", "--output-format", "json",
              "--select", "E9,F63,F7,F82,F811,F401,F841", "--target-version", "py312", *rels], folder)
    if r is None:
        return None
    try:
        items = json.loads(r.stdout or "[]")
    except ValueError:
        return None
    return _cap([(_rel(folder, i.get("filename", ""), rels), int((i.get("location") or {}).get("row") or 1),
                  f"{i.get('code') or 'ruff'} {i.get('message', '')}") for i in items])


def drop_unused_imports(folder: Path, rels: list[str]) -> list[str]:
    """Unused imports (F401) taken out by ruff's own safe fix, with no model (the user's benchmarks, Nov 17: no dead
    code, and the best use of the tools already here). Returns the files it changed; [] when ruff isn't here."""
    exe = _tool("ruff", Path(sys.executable).parent / "ruff.exe")
    py = [r for r in rels if r.lower().endswith(".py")]
    if not exe or not py:
        return []
    before = {r: (folder / r).read_bytes() for r in py if (folder / r).exists()}
    if _run([exe, "check", "--no-cache", "--isolated", "--select", "F401", "--fix", "--target-version", "py312",
             *py], folder) is None:
        return []
    return [r for r, b in before.items() if (folder / r).read_bytes() != b]


def pyright_errors(folder: Path, rels: list[str]) -> list[str] | None:
    """pyright's errors in basic mode (wrong arguments, names that don't exist on a module or class). Imports
    of packages that aren't installed here aren't counted; the import check covers the build's own files."""
    exe = _npm("pyright")
    if not exe:
        return None
    with tempfile.TemporaryDirectory() as t:
        cfg = Path(t) / "pyrightconfig.json"
        try:                                         # pyright takes only paths relative to its config's folder
            inc = [os.path.relpath((folder / r).resolve(), t) for r in rels]
            extra = os.path.relpath(folder.resolve(), t)
        except ValueError:                           # another drive: no relative path; not checked
            return None
        # only the clear-cut errors: a name or attribute that doesn't exist, a call with the wrong arguments.
        # "Maybe None" and type nits are left out: in tests they're mostly false alarms, and a fix round would
        # chase them (Nov 16: task 56's tests got 3 of those and nothing real)
        cfg.write_text(json.dumps({"include": inc, "extraPaths": [extra],
                                   "typeCheckingMode": "off", "pythonVersion": "3.12",
                                   "reportUndefinedVariable": "error", "reportAttributeAccessIssue": "error",
                                   "reportCallIssue": "error",
                                   "reportMissingImports": "none", "reportMissingModuleSource": "none"}),
                       encoding="utf-8")
        r = _run([exe, "--outputjson", "-p", str(cfg)], folder)
    if r is None:
        return None
    try:
        diags = json.loads(r.stdout or "{}").get("generalDiagnostics", [])
    except ValueError:
        return None
    return _cap([(_rel(folder, d.get("file", ""), rels), int(((d.get("range") or {}).get("start") or {}).get("line", 0)) + 1,
                  "pyright: " + " ".join(str(d.get("message", "")).split()))
                 for d in diags if d.get("severity") == "error"])


# ---------------------------------------------------------------- PowerShell
_PSA = ("$ErrorActionPreference = 'Stop'\n"
        "try { Import-Module PSScriptAnalyzer } catch { 'NOMODULE'; exit }\n"
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        "foreach ($p in $env:SMITHY_PSA.Split('|')) {\n"
        "  Invoke-ScriptAnalyzer -Path $p -Severity Error | ForEach-Object {\n"
        "    '{0}|{1}|{2}: {3}' -f $p, $_.Line, $_.RuleName, ($_.Message -replace '\\s+', ' ') } }")


def psa_errors(folder: Path, rels: list[str]) -> list[str] | None:
    """PSScriptAnalyzer's errors (not warnings), beyond the parse check."""
    paths = {str((folder / r).resolve()): r for r in rels}
    r = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand",
              base64.b64encode(_PSA.encode("utf-16-le")).decode()], folder,
             env={**os.environ, "SMITHY_PSA": "|".join(paths)})
    if r is None or "NOMODULE" in (r.stdout or ""):
        return None
    found = []
    for line in (r.stdout or "").splitlines():
        path, _, rest = line.partition("|")
        num, _, msg = rest.partition("|")
        if num.strip().isdigit():
            found.append((paths.get(path, Path(path).name), int(num), msg.strip()))
    return _cap(found)


# ---------------------------------------------------------------- TypeScript, Java
def tsc_errors(folder: Path, rels: list[str]) -> list[str] | None:
    exe = _npm("tsc")
    if not exe:
        return None
    r = _run([exe, "--noEmit", "--pretty", "false", "--target", "es2022", "--module", "esnext",
              "--moduleResolution", "bundler", "--skipLibCheck", "--allowJs", *rels], folder)
    if r is None:
        return None
    found = []
    for line in (r.stdout or "").splitlines():       # file(12,5): error TS2304: Cannot find name 'x'.
        head, sep, msg = line.partition("): error ")
        if sep and "(" in head:
            path, _, pos = head.rpartition("(")
            num = pos.split(",")[0]
            if num.isdigit():
                found.append((_rel(folder, str(folder / path), rels), int(num), msg.strip()))
    return _cap(found)


def javac_errors(folder: Path, rels: list[str]) -> list[str] | None:
    exe = _javac()
    if not exe:
        return None
    with tempfile.TemporaryDirectory() as t:         # the .class files go here and are deleted
        r = _run([exe, "-d", t, "-encoding", "UTF-8", "-Xlint:none", "-Xmaxerrs", "40", *rels], folder)
    if r is None:
        return None
    found = []
    for line in (r.stderr or "").splitlines():       # Main.java:12: error: cannot find symbol
        head, sep, msg = line.partition(": error: ")
        if sep:
            path, _, num = head.rpartition(":")
            if num.isdigit():
                found.append((_rel(folder, str(folder / path), rels), int(num), msg.strip()))
    return _cap(found)


# ---------------------------------------------------------------- JSON fields asked for
_PARENS = re.compile(r"\([^()]*\)")
_QUOTED = re.compile(r"[\"“`]([A-Za-z_][\w\-]{0,40})[\"”`]")
_FIELDWORD = re.compile(r"\b(?:fields?|keys?|propert(?:y|ies))\b", re.I)


def asked_fields(text: str, json_names: list[str]) -> dict[str, set[str]]:
    """{json file: fields every entry must have}, from sentences of a change that name the JSON file and say
    "field(s)" / "key(s)", with the field names in quotes. What's in brackets is left out (examples like
    ("syntax-error") aren't fields). Task 56: 'Give every entry in academy_glossary.json two new fields: "id"
    (...) and "version" (...). Keep "term", "plain" and "track" exactly as they are.'"""
    out: dict[str, set[str]] = {}
    last = None                                      # "Keep ..." right after the sentence that named the file
    for sent in re.split(r"(?<=[.;])\s+|\n", _PARENS.sub("", text)):
        named = [j for j in json_names if Path(j).name.lower() in sent.lower()]
        if named:
            last = named
        elif not re.match(r"\s*(?:keep|also|and)\b", sent, re.I):
            last = None
        if not last or not (_FIELDWORD.search(sent) or re.match(r"\s*keep\b", sent, re.I)):
            continue
        fields = {f for f in _QUOTED.findall(sent) if not f.lower().endswith(".json")}
        for j in last:
            if fields:
                out.setdefault(j, set()).update(fields)
    return out


def _entries(data) -> list:
    """The entries of a JSON file: a list of objects, a {"x": [objects]} wrapper, or an object of objects."""
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    if isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list) and v and all(isinstance(e, dict) for e in v)]
        if len(lists) == 1:
            return lists[0]
        if data and all(isinstance(v, dict) for v in data.values()):
            return list(data.values())
    return []


def json_field_errors(folder: Path, asked: dict[str, set[str]]) -> list[str] | None:
    """Each entry of each JSON file checked for the fields its change asked for (jsonschema "required")."""
    try:
        import jsonschema
    except ImportError:
        return None
    problems = []
    for rel, fields in asked.items():
        try:
            data = json.loads((folder / rel).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue                                 # missing or not JSON: the parse check says so
        entries = _entries(data)
        if not entries:
            continue
        schema = {"type": "object", "required": sorted(fields)}
        missing: dict[str, int] = {}
        for e in entries:
            for err in jsonschema.Draft7Validator(schema).iter_errors(e):
                if err.validator == "required":
                    name = str(err.message).split("'")[1] if "'" in str(err.message) else str(err.message)
                    missing[name] = missing.get(name, 0) + 1
        for name, n in sorted(missing.items()):
            problems.append(f"{rel} line 1: {n} of {len(entries)} entries have no \"{name}\" field, which the "
                            "change asks every entry to have")
    return problems


# ---------------------------------------------------------------- calls asked for
_CALLS = re.compile(r"\b(?:call|calls|calling)\s+`?(?:[A-Za-z_]\w*\.)*([A-Za-z_]\w*)\(\)`?")


def asked_calls(text: str, py_files: list[str]) -> dict[str, set[str]]:
    """{python file: functions it must call}, from sentences that name the files (with or without .py) and say
    "call(s) name()". Task 56 item 4: "academy_explain and academy_readme call record() when told enabled is
    True" - both files still had no record() call when a model's check said it was done."""
    out: dict[str, set[str]] = {}
    for sent in re.split(r"(?<=[.;])\s+|\n", text):
        names = _CALLS.findall(sent)
        if not names:
            continue
        low = sent.lower()
        head = low[:low.find(" call")] if " call" in low else low     # the files before the verb do the calling
        for f in py_files:
            stem = Path(f).stem.lower()
            if re.search(r"\b" + re.escape(stem) + r"(?:\.py)?\b", head):
                out.setdefault(f, set()).update(names)
    return out


def call_errors(folder: Path, asked: dict[str, set[str]]) -> list[str]:
    """Each file checked with `ast` for a call to each function asked for (as name() or x.name())."""
    import ast
    problems = []
    for rel, names in asked.items():
        try:
            tree = ast.parse((folder / rel).read_text(encoding="utf-8-sig"))
        except (OSError, SyntaxError, ValueError):
            continue                                 # missing or not Python: other checks say so
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                called.add(fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else "")
        for name in sorted(names - called):
            problems.append(f"{rel} line 1: it never calls {name}(), which the change says it calls")
    return problems


# ---------------------------------------------------------------- all of them
CHECKERS = [  # (label, which files, checker)
    ("ruff", (".py",), ruff_errors),
    ("pyright", (".py",), pyright_errors),
    ("PSScriptAnalyzer", (".ps1", ".psm1"), psa_errors),
    ("TypeScript (tsc)", (".ts", ".tsx"), tsc_errors),
    ("Java (javac)", (".java",), javac_errors),
]


def check_all(folder: Path, rels: list[str]) -> tuple[list[str], list[str]]:
    """(problems, what was checked) from every checker that has files to read."""
    problems, checked = [], []
    for label, ends, fn in CHECKERS:
        mine = [r for r in rels if r.lower().endswith(ends)]
        if not mine:
            continue
        found = fn(folder, mine)
        if found is None:
            checked.append(f"{label} not checked (not installed on this PC)")
        else:
            problems += found
            checked.append(f"{label} ({len(mine)} file{'s' if len(mine) != 1 else ''})")
    return problems, checked
