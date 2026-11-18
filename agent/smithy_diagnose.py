"""The Smithy's self-diagnosis, step 1 (Nov 16, 2026; the user: "Let's build some self diagnosis features into The
Smithy"). Plain code: no model, no tokens.

- A history per task (drafts\\smithy\\NNNNN-history.jsonl), one JSON line per event, kept across runs and restarts
  (the log rotates). Only file and test names, rule codes, counts and short assertion lines go in, through
  redact(): never file contents, prompts or keys.
- Detectors over it, for the result's "Self-check" section:
  stuck tests (the same test failing run after run, task 56's README tests failed 5 times), no progress (check
  rounds that leave the same things missing and the same problems), and unchecked rules (a request sentence with
  "only if", "never", "not"... that no lettered check covers: task 56's "names the version only if the README does").
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

_RULE_WORDS = re.compile(r"(?i)\b(only if|only when|never|not|no|must|exactly|at most|at least|without|unless)\b")
_STOP = set("the a an and or of to in is it its it's be for on as by with that this these those from at are was "
            "one each any all into than then when only if never not no must exactly most least without unless "
            "file files says say".split())
_CHECKS = re.compile(r"(?is)\bchecks?\s*:")


def _redact(text: str) -> str:
    try:
        from smithy_jobs import redact          # the same hiding as everything sent out
        return redact(text)
    except Exception:
        return text


def record(path: Path | None, kind: str, **fields) -> None:
    """One event to the task's history; never fails the task."""
    if path is None:
        return
    try:
        line = json.loads(_redact(json.dumps({"ts": round(time.time(), 1), "kind": kind, **fields},
                                             ensure_ascii=False)))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read(path: Path | None) -> list[dict]:
    out = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except (OSError, TypeError):
        pass
    return out


def commit(folder: Path) -> str:
    """The Smithy's own code version (git), so a run can be told apart from one on older code."""
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=folder, capture_output=True, text=True,
                           timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout.strip()
    except Exception:
        return ""


def problem_codes(problems: list[str]) -> list[str]:
    """"test_a.py line 49: F821 Undefined name `List`" -> "test_a.py F821"; others -> file and their first words."""
    out = []
    for p in problems:
        m = re.match(r"(\S+) line \d+: (?:pyright: )?([A-Z]+\d+)?", p)
        if m:
            rest = p.split(": ", 1)[-1].removeprefix("pyright: ")
            out.append(f"{m.group(1)} {m.group(2) or ' '.join(rest.split()[:5])}")
        else:
            out.append(" ".join(p.split()[:5]))
    return sorted(set(out))


def assertions(output: str) -> dict[str, str]:
    """{failing test name: its assertion line} from unittest or pytest output ("AssertionError: 2 != 3")."""
    found, current = {}, ""
    for line in (output or "").splitlines():
        m = re.match(r"^(?:FAIL|ERROR): (\w+)", line)
        if m:
            current = m.group(1)
            continue
        m = re.match(r"^\s*(\w*(?:Error|Exception)\b.*)$", line)
        if current and m and current not in found:
            found[current] = m.group(1)[:120]
    return found


# ---------------------------------------------------------------- detectors
def stuck_tests(events: list[dict], at_least: int = 2) -> list[dict]:
    """Tests that failed in the last `at_least` or more Sandbox runs in a row: [{"test", "runs", "asserts"}]."""
    runs = [e for e in events if e.get("kind") == "sandbox" and e.get("status") in ("failed", "passed", "timeout")]
    names = {t for e in runs for t in (e.get("failing") or [])}
    out = []
    for name in sorted(names):
        streak, said = 0, []
        for e in reversed(runs):
            if name not in (e.get("failing") or []):
                break
            streak += 1
            said.append((e.get("asserts") or {}).get(name, ""))
        if streak >= at_least:
            out.append({"test": name, "runs": streak, "asserts": [s for s in reversed(said) if s]})
    return out


def _signature(e: dict) -> tuple:
    return tuple(sorted(e.get("missing") or [])), tuple(e.get("problems") or [])


def no_progress(events: list[dict]) -> int:
    """How many check rounds in a row, at the end of this run, found exactly what the one before found (missing
    items and build problems), while there was something to fix. 0 = progress (or nothing left)."""
    run = []
    for e in events:
        if e.get("kind") == "run":
            run = []
        elif e.get("kind") == "check":
            run.append(e)
    n = 0
    for a, b in zip(reversed(run[:-1]), reversed(run[1:])):
        if _signature(a) != _signature(b) or b.get("files") == a.get("files") or not any(_signature(b)):
            break
        n += 1
    return n


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9_]+", text.lower()) if w not in _STOP and len(w) > 2}


def unchecked_rules(items: dict[int, str], limit: int = 8) -> list[str]:
    """Request sentences that set a rule ("only if", "never", "not", "must"...) and that no lettered check of the
    same item shares two or more words with: rules nothing tests. Items with no "Checks:" line are left out (they
    have no lettered checks to compare)."""
    out = [f"{n}: {s}" for n, text in items.items() for s in rules_of(text)]
    return out[:limit]


def rules_of(text: str, limit: int = 6) -> list[str]:
    """One item's rules that none of its lettered checks covers (at most `limit`, 160 characters each); [] when the
    item has no "Checks:" line. The checklist asks for each to be shown in the code (Nov 16, task 56's review)."""
    try:
        from smithy_jobs import sub_points
    except Exception:
        return []
    subs = sub_points(text)
    if not subs:
        return []
    body = _CHECKS.split(text)[0]
    checks = [_words(v) for v in subs.values()]
    out = []
    for s in re.split(r"(?<=[.;])\s+|\n+", body):
        s = " ".join(s.split()).strip(" -")
        if len(s) < 20 or not _RULE_WORDS.search(s):
            continue
        if not any(len(_words(s) & c) >= 2 for c in checks):
            out.append(s[:160])
    return out[:limit]


def self_check(events: list[dict], items: dict[int, str]) -> list[str]:
    """Plain lines for the result's "Self-check" section; [] when nothing was found."""
    lines = []
    for s in stuck_tests(events):
        lines.append(f"{s['test']} has failed in {s['runs']} test runs in a row"
                     + (f" ({', then '.join(s['asserts'][-4:])})" if s["asserts"] else "")
                     + ": The Smithy judges it against the request (the test or the code is wrong) and fixes that side.")
    n = no_progress(events)
    if n:
        lines.append(f"the last {n + 1} checks found the same changes missing and the same build problems, though "
                     f"the files changed: the fixes aren't reaching the cause.")
    rules = unchecked_rules(items)
    if rules:
        lines.append("these rules in the request aren't covered by any lettered check, so the checklist asks for a "
                     "test that checks each: " + "; ".join(rules))
    return lines
