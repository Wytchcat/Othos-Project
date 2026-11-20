"""The Smithy's practice check: a small sample request replayed through its own checklist steps with saved model
replies, no model and no tokens (Nov 19, colibri idea; the first piece of "the doctor").

It checks what has gone wrong in real runs, in seconds:
- every lettered check (and every item) is asked for, so each can be read back;
- no step is bigger than a provider's minute takes (MAX_STEP_TOKENS);
- an item that names no file reads its changed files whole, as far as the budget allows, and the re-ask keeps them;
- the saved answers (a JSON answer and a text answer) read the same, and a DONE whose quote isn't in the files
  counts as MISSING.

Run: python smithy_practice.py   (exit 1 and the problems listed when something is wrong)
"""
from __future__ import annotations

import json
import sys

import smithy_jobs
import worker_cloud

MAX_STEP_TOKENS = 30_000             # Cerebras takes 30,000 new tokens a minute

_README = "# Sample\nThis explains the file words. Run `pip install` then open setup.ps1.\nUses JSON and find_terms.\n"
_CODE = ("def find_terms(text):\n    return [w for w in text.split() if w.isupper()]\n\n\n"
         "def readme_lesson(path):\n    return 'Not in my glossary yet: ' + ', '.join(find_terms(path))\n")
_TEST = ("import unittest\n\nfrom lesson import find_terms\n\n\nclass T(unittest.TestCase):\n"
         "    def test_upper(self):\n        assert find_terms('a B') == ['B']\n")
_ITEMS = {1: "1. Lesson.\n   - Rule: what counts as a topic.\n   - Checks: (a) upper-case words are found; "
             "(b) lower-case words are not."}
_PLAN = {"files": [{"path": "lesson.py", "change": "changed", "purpose": "the lesson"},
                   {"path": "test_lesson.py", "change": "changed", "purpose": "its test"},
                   {"path": "notes.py", "change": "same", "purpose": "unchanged"}]}


class _Replay:
    """A model that records every step it is sent and answers with the saved replies, in order."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.steps: list[list[dict]] = []

    def chat(self, msgs, max_tokens):
        self.steps.append(msgs)
        return self.replies.pop(0) if self.replies else ""


def _saved_replies() -> dict[str, str]:
    as_json = json.dumps({"checks": [
        {"key": "1", "state": "DONE", "file": "lesson.py", "quote": "def find_terms(text):", "note": ""},
        {"key": "1a", "state": "DONE", "file": "test_lesson.py", "quote": "assert find_terms('a B') == ['B']",
         "note": ""},
        {"key": "1b", "state": "MISSING", "file": "test_lesson.py", "quote": "", "note": "no test checks it"}]})
    as_text = ("1: DONE - lesson.py: `def find_terms(text):`\n1a: DONE - test_lesson.py: "
               "`assert find_terms('a B') == ['B']`\n1b: MISSING - test_lesson.py: no test checks it")
    return {"json": as_json, "text": as_text}


def practice() -> list[str]:
    """The problems found, none when The Smithy's steps pass the practice."""
    bad: list[str] = []
    written = {"lesson.py": _CODE, "test_lesson.py": _TEST, "notes.py": "X = 1\n" * 3000}
    saved = _saved_replies()
    results = {}
    for form, reply in saved.items():
        model = _Replay([reply])
        got = smithy_jobs._checklist(model, {"request": "Sample request"}, "", _PLAN, written, dict(_ITEMS), 20000)
        results[form] = got[1][0] if 1 in got else "none"
        if not model.steps:
            bad.append(f"{form}: the checklist asked nothing")
            continue
        asked = "\n".join(str(m["content"]) for m in model.steps[-1])
        for key in ("1:", "1a:", "1b:"):
            if key not in asked:
                bad.append(f"{form}: the check line {key!r} was never asked for")
        for step in model.steps:
            n = worker_cloud.prompt_tokens(step)
            if n > MAX_STEP_TOKENS:
                bad.append(f"{form}: a step was {n:,} tokens (over {MAX_STEP_TOKENS:,})")
        if "--- lesson.py ---\n" + _CODE[:60] not in asked:
            bad.append(f"{form}: the changed file lesson.py wasn't read whole")
    if results.get("json") != results.get("text"):
        bad.append(f"the JSON and the text answers read differently: {results}")
    if results.get("json") != "MISSING":
        bad.append(f"check (b) had no test, so the item should be MISSING, not {results.get('json')}")
    # a DONE whose quote isn't in the files counts as MISSING
    quote = "`assert find_terms('a B') == ['B']`"
    gone = "1: DONE - lesson.py: `a line that is not in the file`"
    fake = _Replay([gone + "\n1a: DONE - test_lesson.py: " + quote + "\n1b: DONE - test_lesson.py: " + quote])
    got = smithy_jobs._checklist(fake, {"request": "Sample request"}, "", _PLAN, written, dict(_ITEMS), 20000)
    if got.get(1, ("",))[0] != "MISSING":
        bad.append("a DONE with a made-up quote wasn't counted as MISSING")
    return bad


if __name__ == "__main__":
    problems = practice()
    print("\n".join(problems) or "The practice passed: every check asked for, every step small enough, "
          "the answers read the same.")
    sys.exit(1 if problems else 0)
