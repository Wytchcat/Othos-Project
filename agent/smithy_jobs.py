"""The Smithy: task types. Coding only, CPU only; everything it makes is a draft.

- review    a code file: bugs, risks, small improvements
- fix       a code file: a diff (never applied)
- script    a script (saved, never run)
- joomla    a Joomla / YOOtheme override or CSS, built like a small project
- build     a project from scratch: a plan first, then each file in turn, laid
            out like the real project in its own folder, with a README
- followup  a question about an earlier result; on a build or Joomla task it
            makes a new version of the build, in its own folder
- check     study a Windows program with REA and Ghidra and write FEATURES.md,
            SPECS.md and AGENT.md for a clean-room rebuild (smithy_check.py)

Rules, enforced here in code, not in the prompt:
- Files are only read through worker_files (allowlisted folders, private files
  blocked, keys redacted before anything reaches the model).
- A build only writes inside its own folder, drafts\\smithy\\NNNNN-<name>\\: every
  path the model plans is checked (relative, no "..", no drive, no hidden or key
  files, no programs) and checked again after resolving. Nothing is run or
  installed; the user copies what they want into place themselves.
- A build is checked before its README: C# files are compiled together (Windows' own csc.exe,
  into a temporary folder that's deleted), PowerShell, Python and JSON files are parsed, and
  Python imports between the files are checked (with `ast`). The language checkers in
  smithy_checkers.py read them too (ruff, pyright, PSScriptAnalyzer, tsc, javac), and the JSON fields
  a change asks for are checked on every entry. The result is never run, so tests
  are reported as "not run", and a request that wants them passing stays NOT COMPLETE. Each
  numbered item asked for (a first build's request, or a new version's change) is checked off.
"""
from __future__ import annotations

import ast
import base64
import collections
import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path, PurePosixPath

try:                                             # the check task: left out of the shared copy of Othos
    import smithy_check
except ImportError:
    smithy_check = None
import smithy_checkers
import smithy_diagnose
import smithy_sandbox
import worker_cloud
import worker_files
import worker_shared
from worker_cloud import BudgetOut, TooLongForLocal
from worker_engine import FORMAT, NOT_COMPLETE, JobType, plain_summary, save_artifact, split_result
from worker_queue import SMITHY
from worker_shared import RULES, USER, USERS, redact

log = logging.getLogger("worker")
steps = logging.getLogger("worker.step")     # plain step lines (Progress window, Othos's Task Mode)

SYSTEM = (f"You are the Smithy, a careful coding assistant for Othos on {USERS} Windows 11 PC. "
          "You review code, draft fixes, write scripts, and build small projects from scratch. " + RULES
          # task 42: a test's made-up key reached the model as [REDACTED], and it reported that the test
          # planted the word "[REDACTED]"
          + " Key-like values are hidden from you before you see any text: [REDACTED] stands for a value that "
          "is really there in the file (often a made-up test key). Treat it as that value, not as the word "
          "[REDACTED], and never report it as a bug."
          # the user's benchmarks for every line of code (Nov 17): the same ones Claude follows on Othos
          + "\n\nHow every line of code you write is judged, in this order. 1. Safety first, pass or fail: no keys, "
          "passwords or personal data in code; no network use unless the request asks for it; never delete or "
          "write outside the project's own folder or a temporary folder. 2. Quality on the first pass: it does "
          "exactly what the request says; it handles bad input and missing files without crashing; it fits this "
          "PC (Windows 11, Windows PowerShell 5.1, C# 5 with .NET Framework 4); it's plain and small; windows and "
          "pop-ups follow the app's own colours and can be resized. 3. Use what exists: the standard library and "
          "the project's own code before anything new. 4. Speed never buys errors: no dead code, no unused imports "
          "or variables, no leftover comments."
          # Nov 19, task 59: the request's state\google-prompt.json was written under the install folder, while the
          # same file reached the program's files as Path.Combine(targetFolder, "agent", ...), where Othos reads it
          + " A path the request names for the program's own files (state\\..., a settings file) is where the "
          "existing code already keeps that program's files: build it the way the existing code builds its paths, "
          "from the same base folder and subfolders, never from a new base. Say plainly what's done and what isn't, and settle what you can "
          "yourself instead of asking the person using it.")
SYSTEM_TWO_PART = SYSTEM + "\n\n" + FORMAT

JOOMLA = ("This is for a Joomla 5 site using the YOOtheme Pro template (and ZOO). Never change Joomla core "
          "or extension files: use overrides. With YOOtheme Pro, put everything in a child theme so updates "
          f"don't overwrite it: call it yootheme_child unless {USER} names theirs. Layout overrides go in its html "
          "folder, for example templates/yootheme_child/html/com_content/article/default.php for the article "
          "layout, and custom CSS in templates/yootheme_child/css/custom.css. Use real folder names in paths, "
          "never placeholders in angle brackets. "
          "Escape all output (htmlspecialchars or Joomla's escape helpers), keep defined('_JEXEC') or die; "
          "at the top of PHP files, and don't add database queries without prepared statements. If you aren't "
          "sure of an exact file name or path for the installed version, say so in the README.")


# A backslash lost on the way in: a request saved through an escaping step (a JSON or Python string) turns
# "state\academy-glossary.json" into "state" + a bell character + "cademy-glossary.json" (Nov 18, the Academy's
# task 58 follow-up). These four control characters are never typed in a request, so the backslash is put back.
_LOST_BACKSLASH = {"\x07": "\\a", "\x08": "\\b", "\x0b": "\\v", "\x0c": "\\f"}
_LOST_RX = re.compile("[\x07\x08\x0b\x0c]")


def lost_backslashes(t: str) -> tuple[str, int]:
    """(the text with each lost backslash put back, how many were)."""
    return _LOST_RX.sub(lambda m: _LOST_BACKSLASH[m.group(0)], t), len(_LOST_RX.findall(t))


def _repaired(p: dict, key: str) -> dict:
    """{"repaired": N} when the request had lost backslashes (kept when the params are checked again)."""
    n = lost_backslashes(str(p.get(key, "")))[1] or int(p.get("repaired") or 0)
    return {"repaired": n} if n else {}


def _text(p: dict, key: str, what: str, min_words: int = 2) -> str:
    t = lost_backslashes(str(p.get(key, "")))[0].strip()
    if len(t.split()) < min_words:
        raise ValueError(f"it needs {what}")
    return t[:20_000]


def _file(p: dict, key: str = "path") -> str:
    path = str(p.get(key, ""))
    ok, why = worker_files.allowed(path)
    if not ok:
        raise ValueError(why)
    return str(Path(path).resolve())


def _code_block(path: str) -> str:
    lang = Path(path).suffix.lstrip(".") or "text"
    return f"File: {Path(path).name}\n```{lang}\n{worker_files.read_for_model(path)}\n```"


# ---------------------------------------------------------------- one-call tasks
REVIEW_ASK = ("Review this code file: real bugs first, then risks (security, crashes, data loss), "
              "then small improvements. Point to the function or line for each. Don't describe what the "
              "file does, and don't copy or rewrite the file.")


def _review(p):
    # the request is repeated after the code: small models follow the last instruction best
    return [{"role": "system", "content": SYSTEM_TWO_PART + "\n" + REVIEW_ASK},
            {"role": "user", "content": _code_block(p["path"]) + "\n\n" + REVIEW_ASK +
             " Use the two headings SPOKEN SUMMARY: and FULL RESULT:."}]


def _fix(p):
    return [{"role": "system", "content": SYSTEM_TWO_PART + (
                "\nDraft a fix for what's asked, as a unified diff (```diff block, paths relative to the file) "
                "that changes as little as possible. Explain the change after the diff. It will NOT be applied "
                f"automatically: {USER} reviews it first.")},
            {"role": "user", "content": f"What to fix: {redact(p['request'])}\n\n{_code_block(p['path'])}\n\n"
                                        f"Now draft the fix for: {redact(p['request'])}. As a ```diff block, "
                                        "with the two headings SPOKEN SUMMARY: and FULL RESULT:."}]


def _script(p):
    return [{"role": "system", "content": SYSTEM_TWO_PART + (
                "\nWrite the script that's asked for, in one fenced code block (PowerShell unless another "
                "language is asked for), with short comments. Then say how to run it. It will be saved as a "
                "draft; it will not be run.")},
            {"role": "user", "content": redact(p["request"])}]


_BLOCK = re.compile(r"```([\w#+.-]*)[^\n]*\n([\s\S]*?)```")
_LANG_EXT = {"powershell": ".ps1", "ps1": ".ps1", "pwsh": ".ps1", "python": ".py", "py": ".py", "json": ".json",
             "csharp": ".cs", "cs": ".cs", "c#": ".cs"}


def _script_problems(full: str) -> list[str]:
    """Parse problems in a script answer's first code block (PowerShell, Python, JSON, C#). Never run."""
    m = _BLOCK.search(full)
    if not m:
        return ["the answer has no code block"]
    ext = _LANG_EXT.get(m.group(1).lower(), ".ps1" if not m.group(1) else "")
    if not ext:
        return []                                             # a language that can't be checked here
    with tempfile.TemporaryDirectory(prefix="smithy-check-") as tmp:
        (Path(tmp) / f"script{ext}").write_text(m.group(2), encoding="utf-8")
        problems, _ = build_problems(Path(tmp), [f"script{ext}"])
    return problems


def _diff_problems(path: str, full: str) -> list[str]:
    """Does the fix's diff apply cleanly to a copy of the file? (`git apply --check`; nothing is changed.)"""
    blocks = [c for l, c in _BLOCK.findall(full) if l.lower() in ("diff", "patch")] or [c for _, c in _BLOCK.findall(full)]
    if not blocks:
        return ["the answer has no diff block"]
    if shutil.which("git") is None:
        return []                                             # can't check here: say nothing rather than guess
    with tempfile.TemporaryDirectory(prefix="smithy-check-") as tmp:
        shutil.copyfile(path, Path(tmp) / Path(path).name)
        # the diff's line ends as the file's (Nov 14: written with Windows' \r\n, a diff for a \n file never applied)
        crlf = b"\r\n" in Path(path).read_bytes()
        (Path(tmp) / "fix.diff").write_text(blocks[0], encoding="utf-8", newline="\r\n" if crlf else "\n")
        last = ""
        for strip in ("-p1", "-p0"):
            r = subprocess.run(["git", "apply", "--check", "--recount", "--unsafe-paths", strip, "fix.diff"], cwd=tmp,
                               capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW)
            if r.returncode == 0:
                return []
            last = (r.stderr or r.stdout).strip().replace(tmp, "")
    return [f"the diff doesn't apply to {Path(path).name}: {last[:400]}"]


_BARE_HUNK = re.compile(r"(?m)^@@[ \t]*(?:@@)?[ \t]*$")


def repair_diff(path: str, full: str) -> str:
    """A diff whose hunks have no line numbers (a bare "@@"; Nov 14, task 27: right changes, three rounds, still
    no numbers) rebuilt by The Smithy itself: each hunk's old lines (context and "-") are found exactly once
    in the file, the new lines ("+" and context) put in their place, and a real diff is written from the
    result. Anything it can't place exactly leaves the answer as it was, for the check to report."""
    m = next((b for b in _BLOCK.finditer(full) if b.group(1).lower() in ("diff", "patch")), None) or _BLOCK.search(full)
    if not m or not _BARE_HUNK.search(m.group(2)):
        return full
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return full
    new = list(lines)
    for hunk in re.split(r"(?m)^@@[^\n]*\n", m.group(2))[1:]:
        old_part, new_part = [], []
        for l in hunk.split("\n"):
            if l.startswith("-"):
                old_part.append(l[1:])
            elif l.startswith("+"):
                new_part.append(l[1:])
            elif l.startswith(" ") or l == "":
                if l == "" and not (old_part or new_part):
                    continue
                old_part.append(l[1:]), new_part.append(l[1:])
        while old_part and new_part and old_part[-1] == "" and new_part[-1] == "":   # the block's trailing newline
            old_part.pop(), new_part.pop()
        if not old_part:
            return full                                  # nothing to find it by
        at = [i for i in range(len(new) - len(old_part) + 1) if new[i:i + len(old_part)] == old_part]
        if len(at) != 1:
            return full
        new[at[0]:at[0] + len(old_part)] = new_part
    name = Path(path).name
    diff = "\n".join(difflib.unified_diff(lines, new, f"a/{name}", f"b/{name}", lineterm="")) + "\n"
    log.info("fix: the diff had no line numbers; rebuilt from its lines")
    steps.info("The diff had no line numbers; The Smithy rebuilt it from its lines.")
    return full[:m.start(2)] + diff + full[m.end(2):]


def _checked_answer(ctx, msgs: list[dict], max_tokens: int, problems_of, what: str,
                    repair=None) -> tuple[str, str, list[str]]:
    """One answer, checked; asked again with the problems (up to build_check_rounds) until it checks clean.
    `repair(full) -> full`: a fix The Smithy can make itself first (a diff with no line numbers).
    Returns (summary, full, problems left)."""
    rounds = max(1, int(ctx.cfg.get("build_check_rounds", 3)))
    text = ctx.chat(msgs, max_tokens)
    summary, full = split_result(text)
    if repair:
        full = repair(full)
    problems = problems_of(full)
    for rnd in range(1, rounds + 1):
        if not problems:
            steps.info("The %s checks clean.", what)
            break
        steps.info("Check round %d of %d: the %s has %d problem%s; asking for a fixed one.", rnd, rounds, what,
                   len(problems), "" if len(problems) == 1 else "s")
        text = ctx.chat(msgs + [{"role": "assistant", "content": text},
                                {"role": "user", "content": f"That {what} doesn't check clean:\n- " + "\n- ".join(problems[:20])
                                 + f"\n\nSend the whole {what} again, fixed, with the same two headings."}], max_tokens)
        summary, full = split_result(text)
        if repair:
            full = repair(full)
        problems = problems_of(full)
    return summary, full, problems


def _not_complete(what: str, summary: str, full: str, problems: list[str]) -> tuple[str, str]:
    if not problems:
        return summary, full
    return (f"The {what} is NOT complete: it still doesn't check clean ({len(problems)} problem"
            f"{'s' if len(problems) != 1 else ''}). It's saved as a draft for your review; nothing was run.",
            f"**{NOT_COMPLETE}:**\n- " + "\n- ".join(problems[:20]) + "\n\n" + full)


def _run_script(ctx, p):
    steps.info("Writing the script.")
    summary, full, problems = _checked_answer(ctx, _script(p), 1500, _script_problems, "script")
    summary, full = _not_complete("script", summary, full, problems)
    return summary, full, save_artifact(SMITHY.results_dir, ctx.row["id"], "script", full)


def _run_fix(ctx, p):
    steps.info("Writing the fix.")
    summary, full, problems = _checked_answer(ctx, _fix(p), 1500, lambda f: _diff_problems(p["path"], f), "fix",
                                              repair=lambda f: repair_diff(p["path"], f))
    summary, full = _not_complete("fix", summary, full, problems)
    # saved with the file's own line ends, so `git apply` takes it as it is (as the check above did)
    try:
        crlf = b"\r\n" in Path(p["path"]).read_bytes()
    except OSError:
        crlf = False
    return summary, full, save_artifact(SMITHY.results_dir, ctx.row["id"], "diff", full,
                                        newline="\r\n" if crlf else "\n")


def _followup_messages(p):
    parent = SMITHY.get(int(p["parent_id"]))
    earlier = redact(Path(parent["result_path"]).read_text(encoding="utf-8", errors="replace"))
    msgs = [{"role": "system", "content": SYSTEM_TWO_PART + "\nAnswer the follow-up question about the earlier result."}]
    path = json.loads(parent["params"] or "{}").get("path")
    if path and worker_files.allowed(path)[0]:
        msgs.append({"role": "user", "content": "The file it was about:\n" + _code_block(path)})
    msgs.append({"role": "user", "content": f"Earlier result (task {parent['id']}):\n{earlier[:30_000]}"})
    msgs.append({"role": "user", "content": f"Follow-up question: {redact(p['question'])}"})
    return msgs


def _check_followup(p):
    try:
        pid = int(p.get("parent_id"))
    except (TypeError, ValueError):
        raise ValueError("it needs the number of the earlier task")
    parent = SMITHY.get(pid)
    if parent is None or not parent["result_path"]:
        raise ValueError(f"Smithy task {pid} has no result to ask about")
    return {"parent_id": pid, "question": _text(p, "question", "a question")} | _repaired(p, "question")


# ---------------------------------------------------------------- builds: where files may go
_PART = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9 ._()+-]{0,79}$")
_NO_BUILD_EXT = {".exe", ".dll", ".so", ".dylib", ".bin", ".msi", ".com", ".scr", ".sys", ".lnk", ".zip", ".7z",
                 ".rar", ".tar", ".gz", ".jar", ".class", ".pyc", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp",
                 ".pdf", ".db", ".sqlite",
                 # pictures, sound, video and fonts (a model can only write text; Nov 13: a .bmp came back
                 # as the model's apology)
                 ".bmp", ".tif", ".tiff", ".icns", ".cur", ".ani", ".psd", ".xcf", ".avif", ".heic",
                 ".wav", ".mp3", ".ogg", ".flac", ".m4a", ".mp4", ".mov", ".avi", ".mkv", ".webm",
                 ".ttf", ".otf", ".woff", ".woff2", ".eot"}
_KEEP_DOTFILES = {".htaccess", ".gitignore", ".editorconfig"}
_RESERVED = re.compile(r"^(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?$", re.I)   # device names


def safe_rel(path: str) -> str:
    """A planned file path, made safe: relative, inside the build, text only. Raises ValueError."""
    p = str(path or "").strip().strip("`'\"").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if not p or p.startswith("/") or re.match(r"^[A-Za-z]:", p) or "//" in p or len(p) > 180:
        raise ValueError(f"not a relative path: {path!r}")
    parts = PurePosixPath(p).parts
    if len(parts) > 8:
        raise ValueError(f"too deep: {path!r}")
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if part in (".", ".."):
            raise ValueError(f"'..' isn't allowed: {path!r}")
        if last and part.lower() in _KEEP_DOTFILES:
            continue
        if not _PART.match(part) or part.endswith((".", " ")):
            raise ValueError(f"not an allowed name: {part!r}")
        if _RESERVED.match(part):
            raise ValueError(f"Windows doesn't allow that name: {part!r}")
    name = parts[-1]
    if worker_files.BLOCKED_NAMES.match(name) or name.lower().startswith(".env"):
        raise ValueError(f"private file names aren't allowed: {name!r}")
    if PurePosixPath(name).suffix.lower() in _NO_BUILD_EXT:
        raise ValueError(f"only text files can be built: {name!r}")
    return "/".join(parts)


def inside(folder: Path, rel: str) -> Path:
    """The file's real path, checked again after resolving: it must stay in the folder."""
    root = folder.resolve()
    target = (root / safe_rel(rel)).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError(f"outside the build folder: {rel!r}")
    return target


def slug(text: str, fallback: str = "build") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return s[:40].strip("-") or fallback


# ---------------------------------------------------------------- builds: the steps
_JSON_BLOCK = re.compile(r"```(?:json)?\s*\n([\s\S]*?)```", re.I)


def _plan_messages(p: dict, max_files: int, parent: dict | None) -> list[dict]:
    flavour = ("\n\n" + JOOMLA) if p.get("joomla") else ""
    ask = (f"Plan this project before writing any code.\n\nWhat to build: {redact(p['request'])}\n\n"
           "Reply in exactly two parts:\nSPOKEN SUMMARY:\nTwo or three plain sentences for reading aloud: "
           "what you'll build and how it fits together. No code or symbols.\nFULL RESULT:\nA short description "
           "of the design, then ONE ```json block like this:\n"
           '{"name": "short-folder-name", "files": [{"path": "src/main.py", "purpose": "what this file does"}]}\n'
           f"At most {max_files} files. Paths are relative, with forward slashes, laid out like the real "
           "project. Text files only. Don't list a README.md: it's written last, automatically.")
    msgs = [{"role": "system", "content": SYSTEM + flavour}]
    if parent:
        files = "\n".join(f"- {f['path']}: {f.get('purpose', '')}" for f in parent["plan"]["files"])
        msgs.append({"role": "user", "content":
                     f"This is a new version of an earlier build, \"{parent['plan'].get('name')}\". Its files:\n{files}\n\n"
                     f"Its README:\n{parent['readme'][:6000]}\n\nThe change wanted: {redact(p['change'])}\n\n"
                     "Plan the new version. For each file also add \"change\": \"same\" (copy it unchanged), "
                     "\"changed\" or \"new\". Keep the same name. Keep the same files: don't add a new file or "
                     "move code into one unless the change asks for that (Nov 14: a new file copied classes that "
                     "stayed in the old one, so it didn't compile, and the build script never used it)."})
    msgs.append({"role": "user", "content": ask})
    return msgs


# "only" limits the files only when it's about files or changing them (Nov 19, task 64: "Only coding or Othos terms
# are topics" froze every file the request didn't name, so the confusion detector and the tests couldn't change)
_ONLY = re.compile(r"\b(?:only (?:change|fix|edit|touch|update|modify)|(?:change|fix|edit|touch|update|modify) only|"
                   r"only (?:in |to )?(?:the |these |this )?files?|[\w/\\-]+\.\w{1,5} only|"
                   r"no other files?|nothing else|change no other|leave (?:the )?(?:other|rest)|"
                   r"leave (?:all )?(?:the )?(?:existing |other )?files? alone|"
                   r"don'?t (?:change|touch) (?:any )?(?:other|anything else))\b", re.I)
_FILES_LINE = re.compile(r"(?im)^[ \t]*(?:[-*][ \t]*)?files?\b[^:\n]{0,20}:(.*)$")
_SENT = r"(?:[^.\n]|\.(?=\w))*"                       # within one sentence: a dot in "a.py" doesn't end it
_LEAVE = re.compile(r"(?i)" + _SENT + r"\bleave (?:them|it|these|those)?[ \t]*(?:as (?:they|it) (?:are|is)|alone|"
                    r"unchanged)" + _SENT)
_SELF_CHECK = re.compile(r"(?i)\b(?:self[- ]?check|check (?:itself|your(?:self| own))|clean[- ]?up|tidy|bloat|"
                         r"bad code|dead code|code smells?|lint)\b")


def earlier_files(parent: dict) -> list[dict]:
    """Every file of the earlier version, as plan entries: its plan's files that are there, then any other file in
    its folder (not the README, which each version writes): the request's tests (test_request.py) are written
    outside the plan, and the first self check left them out (Nov 19, task 62)."""
    folder = parent.get("folder")
    if folder is None:
        return [{"path": f["path"], "purpose": f.get("purpose", "")} for f in parent["plan"]["files"]]
    out = [{"path": f["path"], "purpose": f.get("purpose", "")} for f in parent["plan"]["files"]
           if (folder / f["path"]).is_file()]
    have = {f["path"] for f in out}
    for x in sorted(folder.rglob("*")):
        rel = x.relative_to(folder).as_posix()
        hidden = any(part.startswith(".") or part == "__pycache__" for part in rel.split("/"))   # caches, tools' folders
        if x.is_file() and rel not in have and rel.lower() != "readme.md" and not hidden:
            out.append({"path": rel, "purpose": "kept from the earlier version"})
    return out


def is_self_check(change: str) -> bool:
    """A change that asks The Smithy to check and clean the code it has, with no numbered changes of its own."""
    return bool(_SELF_CHECK.search(change)) and not numbered_items(change)


_REMOVE = re.compile(r"(?i)" + _SENT + r"\b(?:remove|delete|drop|get rid of|take out|retire|merge)\b" + _SENT)


def _names(text: str, files: list[dict], stems: bool = False) -> set[str]:
    """The files the text names. stems: also by the name without its extension, for a name long enough not to be
    an ordinary word, and not as a module before a dot ("academy_privacy.safe" isn't asking for that file)."""
    out = set()
    for f in files:
        p = PurePosixPath(f["path"])
        if re.search(r"(?<![\w.])" + re.escape(p.name) + r"(?![\w])", text, re.I) or (
                stems and len(p.stem) >= 8 and "_" in p.stem
                and re.search(r"(?<![\w.])" + re.escape(p.stem) + r"(?![\w.])", text, re.I)):
            out.add(f["path"])
    return out


def _failing_files(out: str, test: str, code: list[str], same: set[str]) -> list[str]:
    """The build files a failed test run points at, in order: the ones its tracebacks name (`File "...\\x.py"`),
    then the ones whose name matches a failing test (FAIL: test_complex_confusion_detected ... TestConfusion ->
    academy_confusion.py), the test file last if it's named. Files the plan keeps the same are left out (task 55:
    the fixes went to academy_glossary.py and academy_sources.py, the most imported, while the errors were in
    academy_readme.py, academy_query.py and academy_confusion.py). Never another test file: a test's failure is in
    the code it tests or in itself (Nov 17, task 58: "FAIL: ... TestAcademyFeatures" named test_academy.py, so the
    code fixes for test_request.py were sent there and all 6 refused)."""
    pick = [r for r in code if r not in same and (r == test or not _is_test(r))]
    found: list[str] = []
    for m in re.finditer(r'File "([^"]+)"', out):
        name = re.split(r"[\\/]", m.group(1))[-1].lower()
        found += [r for r in pick if Path(r).name.lower() == name and r not in found]
    heads = " ".join(re.findall(r"(?m)^(?:FAIL|ERROR): (.*)$", out))
    words = set(re.findall(r"[a-z]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", heads).lower().replace("_", " ")))
    for r in pick:
        parts = [w for w in Path(r).stem.lower().split("_") if len(w) >= 4 and w != "test"]
        if r not in found and parts and parts[-1] in words:
            found.append(r)
    return [r for r in found if r != test] + ([test] if test in found else [])


def outline(path: str, body: str) -> str:
    """A short view of a file for this PC's model: a Python file's imports, constants, and each function and class
    with its signature and the first line of its docstring; another file's first lines."""
    if path.lower().endswith(".py"):
        try:
            tree = ast.parse(body)
        except SyntaxError:
            tree = None
        if tree is not None:
            out = []

            def doc1(n):
                d = ast.get_docstring(n) or ""
                return f'    """{d.splitlines()[0][:120]}"""' if d.strip() else ""

            def sig(n, pad=""):
                line = (ast.get_source_segment(body, n) or "").split("\n")[0].rstrip()
                return pad + line + (" ..." if not line.endswith(":") else "")

            for n in tree.body:
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    out.append(ast.get_source_segment(body, n) or "")
                elif isinstance(n, (ast.Assign, ast.AnnAssign)):
                    seg = (ast.get_source_segment(body, n) or "").split("\n")[0]
                    out.append(seg[:120] + (" ..." if len(seg) > 120 or "\n" in (ast.get_source_segment(body, n) or "") else ""))
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out += [sig(n)] + ([doc1(n)] if doc1(n) else [])
                elif isinstance(n, ast.ClassDef):
                    out += [sig(n)] + ([doc1(n)] if doc1(n) else [])
                    for m in n.body:
                        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            out.append(sig(m, "    "))
            return "\n".join(x for x in out if x)
    lines = body.splitlines()
    return "\n".join(lines[:30]) + (f"\n(... {len(lines) - 30} more lines)" if len(lines) > 30 else "")


_SECTION = re.compile(r"(?ms)^--- (?P<path>[^\n]+?) ---(?P<rest>[^\n]*)\n(?P<body>.*?)(?=^--- [^\n]+? ---|\Z)")


def shrink_for_local(messages: list[dict], tokens: int) -> list[dict]:
    """The same step, smaller, for this PC's model (Nov 15, the user: "If it chews makes progress until free resets
    that's the goal"; task 55's steps were about 30,000 tokens against its 20,480). The files the step's last
    message names stay whole; every other file shown as `--- path ---` becomes its outline, the largest first, until
    the step fits. Nothing is changed when it already fits."""
    if worker_cloud.prompt_tokens(messages) <= tokens:
        return messages
    last = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    out = [dict(m) for m in messages]
    found = []                                         # (size, message index, path)
    for i, m in enumerate(out):
        for s in _SECTION.finditer(str(m.get("content", ""))):
            name = PurePosixPath(s.group("path").strip()).name
            if name and name not in last and len(s.group("body")) > 400:
                found.append((len(s.group("body")), i, s.group("path")))
    # still too big: the files the last message names become outlines too, all but the one being edited (Nov 15,
    # task 56: a fix's item and check notes named 6 files, so a 30,899-token step only came down to 29,238)
    # the file being edited is shown whole in its own message, so its copy among the files is an outline first
    editing = {PurePosixPath(m.group(1)).name for m in (_EDITING.search(str(x.get("content", ""))) for x in out) if m}
    twice, named = [], []
    if editing:
        for i, m in enumerate(out):
            for s in _SECTION.finditer(str(m.get("content", ""))):
                name = PurePosixPath(s.group("path").strip()).name
                if name and name in last and len(s.group("body")) > 400:
                    (twice if name in editing else named).append((len(s.group("body")), i, s.group("path")))
    for _, i, path in twice + sorted(found, reverse=True) + sorted(named, reverse=True):
        def swap(s, path=path):
            if s.group("path") != path:
                return s.group(0)
            return (f"--- {path} --- (outline only: its names and signatures; the whole file is in the build)\n"
                    + outline(path, s.group("body")) + "\n\n")
        out[i]["content"] = _SECTION.sub(swap, str(out[i]["content"]))
        if worker_cloud.prompt_tokens(out) <= tokens:
            break
    return out


_EDITING = re.compile(r"^The current (\S+) \(the whole file\)", re.M)


worker_cloud.LOCAL_SHRINK = shrink_for_local


def _failing_test_code(test_path: Path, out: str, limit: int = 6000) -> str:
    """The source of each test function the output says FAIL or ERROR, so a fix sees what was asked (task 55: the
    fixes saw only "AssertionError: False is not true" and said the code needed no change, three times)."""
    names = re.findall(r"(?m)^(?:FAIL|ERROR): (\w+)", out)
    try:
        src = test_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return ""
    parts = [ast.get_source_segment(src, n) or "" for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    text = "\n\n".join(p for p in parts if p)
    return text[:limit]


def _test_funcs(test_path: Path, names: list[str]) -> dict[str, str]:
    """{test function name: its source} for the named tests ('' when the file doesn't parse)."""
    try:
        src = test_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return {}
    return {n.name: ast.get_source_segment(src, n) or "" for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names}


def _backed_by_request(src: str, request: str) -> bool:
    """A test that uses the request's own words: a string in it of 20 or more characters that is in the request
    word for word (Nov 16, task 57: test_confusion_understanding is check 2(b)'s own sentence, so its failure is the
    code's fault, never "the test may disagree")."""
    req = _norm(request).lower()
    found = [a or b for a, b in re.findall(r"\"([^\"\n]{20,})\"|'([^'\n]{20,})'", src)]
    return any(_norm(s).lower() in req for s in found)


def _assert_count(text: str) -> int:
    """How many checks a test file makes: assert statements and self.assert...() calls (SyntaxError if it doesn't
    parse)."""
    return sum(1 for n in ast.walk(ast.parse(text))
               if isinstance(n, ast.Assert) or (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                                                and n.func.attr.startswith("assert")))


def _changed_tests(old: str, new: str, names: set[str]) -> list[str]:
    """Which of these test functions an edit changed (or removed)."""
    if not names:
        return []

    def funcs(text: str) -> dict[str, str]:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return {}
        return {n.name: _norm(ast.get_source_segment(text, n) or "") for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names}
    a, b = funcs(old), funcs(new)
    return sorted(n for n in a if a[n] != b.get(n))


# -- a test correction that checks less than before (Nov 18, task 60: test_personal_glossary_sentence_not_kept, judged
# wrong, went from checking json.dumps(entry), the whole saved entry, to entry.get("lesson") alone, and from 5 words
# to 3; it kept its one assertion and the user's sentence, so no guard saw it)
_ASSERT_TARGET = {"assertIn": 1, "assertNotIn": 1, "assertRegex": 0, "assertNotRegex": 0, "assertCountEqual": 0}


def _func_nodes(text: str) -> dict[str, ast.AST]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {}
    return {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith("test")}


def _scopes(fn: ast.AST) -> dict[str, int]:
    """{name: the fewest keys or fields taken from it} over what the test's assertions look inside: assertIn's
    container, `assert x in Y`'s Y. `json.dumps(entry)` is entry itself (0); `entry.get("lesson")` and
    `entry["lesson"]` take one key (1). A name set in the test (`entry = data[0]`) is followed to its root."""
    assigns: dict[str, ast.AST] = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            assigns[n.targets[0].id] = n.value

    def walk(e: ast.AST, depth: int, follow: frozenset) -> list[tuple[str, int]]:
        if isinstance(e, ast.Name):
            if e.id in assigns and e.id not in follow:
                return walk(assigns[e.id], depth, follow | {e.id}) or [(e.id, depth)]
            return [(e.id, depth)]
        if isinstance(e, ast.Subscript):
            return walk(e.value, depth + 1, follow)
        if isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute):
            if e.func.attr == "get":
                return walk(e.func.value, depth + 1, follow)
            if e.func.attr in ("lower", "upper", "strip", "casefold", "read_text", "read_bytes"):
                return walk(e.func.value, depth, follow)
            if e.func.attr in ("dumps", "join") and e.args:        # json.dumps(x), " ".join(x): all of x
                return walk(e.args[0], depth, follow)
        if isinstance(e, ast.Call) and isinstance(e.func, ast.Name) and e.func.id in ("str", "repr") and e.args:
            return walk(e.args[0], depth, follow)
        if isinstance(e, ast.Attribute):
            return walk(e.value, depth + 1, follow)
        return []
    found: dict[str, int] = {}
    for n in ast.walk(fn):
        target = None
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _ASSERT_TARGET:
            i = _ASSERT_TARGET[n.func.attr]
            target = n.args[i] if len(n.args) > i else None
        elif isinstance(n, ast.Assert) and isinstance(n.test, ast.Compare) and \
                isinstance(n.test.ops[0], (ast.In, ast.NotIn)):
            target = n.test.comparators[0]
        for name, d in walk(target, 0, frozenset()) if target is not None else []:
            found[name] = min(d, found.get(name, d))
    return found


def _loop_values(fn: ast.AST) -> list[set]:
    """The literal values each `for x in [...]` loop with an assertion in it really checks (lower case; a value the
    loop skips with `if x in [...]: continue` doesn't count)."""
    def consts(e: ast.AST) -> set[str]:
        return {str(c.value).lower() for c in getattr(e, "elts", []) if isinstance(c, ast.Constant)}
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.For) and isinstance(n.iter, (ast.List, ast.Tuple, ast.Set)) and any(
                isinstance(c, ast.Assert) or (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                                              and c.func.attr.startswith("assert")) for c in ast.walk(n)):
            skipped = set()
            for s in n.body:
                if isinstance(s, ast.If) and len(s.body) == 1 and isinstance(s.body[0], ast.Continue) \
                        and isinstance(s.test, ast.Compare) and isinstance(s.test.ops[0], ast.In):
                    skipped |= consts(s.test.comparators[0])
            vals = consts(n.iter) - skipped
            if vals:
                out.append(vals)
    return out


def _words_of(e: ast.AST) -> bool:
    """`e` gives separate words: text.split(), re.findall(...), set(...)/list(...) of those."""
    if isinstance(e, ast.Call):
        f = e.func
        if isinstance(f, ast.Attribute) and f.attr in ("split", "findall", "finditer"):
            return True
        if isinstance(f, ast.Name) and f.id in ("set", "list", "frozenset", "sorted") and e.args:
            return _words_of(e.args[0])
    return isinstance(e, (ast.List, ast.Tuple, ast.Set))


def copied_tests(old: str, new: str) -> list[str]:
    """Why an edit is refused for adding a test that copies one already there (its body, docstring aside, at least
    90% the same): the copy repeats the same check under a new name, and any fix later has to find the same lines
    twice (Nov 18, task 60: test_rule_a_never_keeps_sentence copied test_personal_glossary_sentence_not_kept, and
    every edit was refused as "in the file 2 times")."""
    def body(fn: ast.AST) -> str:
        stmts = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        return "\n".join(ast.unparse(s) for s in stmts)
    a, b = _func_nodes(old), _func_nodes(new)
    out = []
    for name in sorted(set(b) - set(a)):
        mine = body(b[name])
        twin = next((o for o in sorted(a) if difflib.SequenceMatcher(None, mine, body(a[o])).ratio() >= 0.9), "")
        if twin and mine:
            out.append(f"it adds {name}, a copy of {twin} (change {twin} instead of adding a copy)")
    return out


def added_fields(old: str, new: str, request_words: set[str], broken: set[str] | frozenset = frozenset()) -> list[str]:
    """Dict keys (fields) an edit adds to a test file that the request never names (task 57: "voice": True, a field
    only the code had made up). A test in `broken` (it can't run: a build problem on its own lines) may gain the
    fields the code needs to be called at all (Nov 18, task 60: record() takes entries with "term" and "id").
    SyntaxError if either doesn't parse."""
    def keys(text: str) -> set[str]:
        return {k.value for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Dict)
                for k in n.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    if broken:
        new = restore_tests(old, new, set(broken))       # the edit with its broken tests' changes left out
    return sorted(k for k in keys(new) - keys(old) if k.lower() not in request_words)


_SILENCER = re.compile(r"#\s*(?:type:\s*ignore|noqa|pyright:\s*ignore)\b", re.IGNORECASE)


def added_silencers(old: str, new: str) -> str:
    """Why an edit is refused for adding comments that silence the checkers ('' when it adds none): a problem is
    fixed, never hidden (Nov 18, task 60: `# type: ignore[arg-type]` on two wrong calls of record())."""
    more = len(_SILENCER.findall(new)) - len(_SILENCER.findall(old))
    return (f"it adds {more} comment{'s' if more != 1 else ''} that silence the checkers (# type: ignore, # noqa): fix "
            "the problem itself instead") if more > 0 else ""


_SKIP = re.compile(r"\b(?:SkipTest|skipTest|skipIf|skipUnless|unittest\.skip|pytest\.skip|mark\.skip)\b")


def added_skips(old: str, new: str) -> str:
    """Why an edit is refused for adding ways to skip tests ('' when it adds none): a skipped test checks nothing
    (Nov 19, task 61: each missing module's import was caught and its tests made to raise SkipTest, so 37 of 47
    tests were skipped and the run read as passed)."""
    more = len(_SKIP.findall(new)) - len(_SKIP.findall(old))
    return (f"it adds {more} way{'s' if more != 1 else ''} to skip tests (SkipTest, skipIf...): a skipped test checks "
            "nothing, so fix the code or the test instead") if more > 0 else ""


def skips_as_failures(sandbox: dict, earlier: Path | None, asked: str) -> None:
    """A test file whose run skipped tests counts as failed, unless the request mentions skipping or the version
    before already had ways to skip in that file (task 61: "passed" with 37 of 47 skipped)."""
    if re.search(r"(?i)\bskip", asked):
        return
    for t, n in (sandbox.get("skipped") or {}).items():
        before = earlier / t if earlier else None
        if before is not None and before.is_file() and _SKIP.search(before.read_text(encoding="utf-8", errors="replace")):
            continue
        sandbox["tests"][t] = "failed"
        note = (f"\n{n} test{'s were' if n != 1 else ' was'} skipped in {t}: a skipped test checks nothing, so this "
                "counts as failed. Make the skipped tests run (the code they need must be there).")
        sandbox["output"][t] = sandbox["output"].get(t, "") + note
        if "full" in sandbox:
            sandbox["full"][t] = sandbox["full"].get(t, "") + note
        sandbox["status"] = "failed" if sandbox["status"] == "passed" else sandbox["status"]
        steps.info("%s skipped %d test%s; a skipped test checks nothing, so it counts as failed.", t, n,
                   "" if n == 1 else "s")


def restore_tests(old: str, new: str, names: set[str]) -> str:
    """`new` with each named test function put back as it is in `old` (a test not in both is left as it is)."""
    a, b = _func_nodes(old), _func_nodes(new)
    old_lines, new_lines = old.splitlines(keepends=True), new.splitlines(keepends=True)
    spans = sorted(((b[n].lineno, b[n].end_lineno or b[n].lineno, n) for n in names if n in a and n in b),
                   reverse=True)
    for start, end, n in spans:                                  # from the bottom, so line numbers stay right
        new_lines[start - 1:end] = old_lines[a[n].lineno - 1:a[n].end_lineno or a[n].lineno]
    return "".join(new_lines)


def never_fail(text: str) -> list[str]:
    """Tests that can't fail: every assertion in them compares only fixed values (`assertTrue(True)`, `assert 1`),
    or they hold nothing but a docstring, `pass` or a skip (Nov 18, task 60: test_3z_self_check_missing_tests was
    `self.assertTrue(True, ...)`)."""
    out = []
    for name, fn in _func_nodes(text).items():
        asserts = [n for n in ast.walk(fn) if isinstance(n, ast.Assert) or (isinstance(n, ast.Call) and isinstance(
            n.func, ast.Attribute) and n.func.attr.startswith("assert"))]
        if asserts:
            fixed = all(all(isinstance(x, ast.Constant) for x in ([a.test] if isinstance(a, ast.Assert)
                                                                  else a.args[:2] if a.args else [ast.Name()]))
                        for a in asserts)
            if fixed:
                out.append(name)
            continue
        body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        if all(isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
                                           and isinstance(s.value.func, ast.Attribute)
                                           and s.value.func.attr == "skipTest") for s in body):
            out.append(name)
    return out


def drop_tests(text: str, names: list[str]) -> str:
    """`text` without the named test functions."""
    nodes = _func_nodes(text)
    lines = text.splitlines(keepends=True)
    for n in sorted((nodes[x] for x in names if x in nodes), key=lambda n: -n.lineno):
        start = n.lineno - 1 - len(n.decorator_list)
        while start > 0 and lines[start - 1].strip().startswith("#"):     # its comment line(s) go with it
            start -= 1
        del lines[start:n.end_lineno or n.lineno]
    return "".join(lines)


def broken_tests(text: str, rel: str, problems: list[str]) -> set[str]:
    """The functions in `rel` (tests, setUp and helpers) with a build problem on their own lines ("rel line N: ..."):
    they can't run as written."""
    lines = {int(m.group(1)) for x in problems for m in [re.match(rf"{re.escape(rel)} line (\d+)", x)] if m}
    try:
        funcs = [n for n in ast.walk(ast.parse(text)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    except SyntaxError:
        return set()
    return {n.name for n in funcs if any(n.lineno <= ln <= (n.end_lineno or n.lineno) for ln in lines)}


def _is_var(e: ast.AST, name: str) -> bool:
    """`e` is the name itself, or name.lower() / .strip() / .casefold()."""
    while isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute) and e.func.attr in ("lower", "strip", "casefold"):
        e = e.func.value
    return isinstance(e, ast.Name) and e.id == name


def part_word_checks(text: str) -> dict[str, str]:
    """{test: why} for tests that check single words are NOT inside a whole text (assertNotIn), so a short word
    matches inside another ("i" in "id", "t" in "term") and a correct program fails. Words from a loop over a list
    with a word of 1 to 3 letters (symbols such as "$" or "?." are fine), or over words split from a sentence (Nov 18,
    task 60: "I don't get syntax errors" checked against the saved file, which has "id" in it)."""
    out = {}
    for name, fn in _func_nodes(text).items():
        assigns = {n.targets[0].id: n.value for n in ast.walk(fn) if isinstance(n, ast.Assign)
                   and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)}
        for loop in [n for n in ast.walk(fn) if isinstance(n, ast.For) and isinstance(n.target, ast.Name)]:
            it = assigns.get(loop.iter.id, loop.iter) if isinstance(loop.iter, ast.Name) else loop.iter
            consts = [c.value for c in getattr(it, "elts", []) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            words = (isinstance(it, (ast.List, ast.Tuple, ast.Set))
                     and any(c.strip().isalpha() and len(c.strip()) <= 3 for c in consts)) \
                or (isinstance(it, ast.Call) and _words_of(it))
            if not words:
                continue
            for c in ast.walk(loop):
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) \
                        and c.func.attr == "assertNotIn" and len(c.args) >= 2 \
                        and _is_var(c.args[0], loop.target.id):
                    hay = c.args[1]
                    hay = assigns.get(hay.id, hay) if isinstance(hay, ast.Name) else hay
                    if not _words_of(hay):
                        out[name] = (f"{name} looks for single words inside the whole text, so a short word matches "
                                     "inside another (\"i\" in \"id\"): split the text into words first (for example "
                                     "re.findall(r\"[\\w']+\", text.lower())) and check against those")
    return out


def _signatures(text: str) -> dict[str, tuple[set[str], bool]] | None:
    """Module-level functions (and classes, by their __init__) -> (parameter names, takes **kwargs); None when the
    text doesn't parse."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    out: dict[str, tuple[set[str], bool]] = {}
    for n in tree.body:
        f = n
        if isinstance(n, ast.ClassDef):
            f = next((b for b in n.body if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and b.name == "__init__"), None)
            if f is None:
                out[n.name] = (set(), True)                 # no __init__ of its own: nothing to check
                continue
        elif not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        a = f.args
        out[n.name] = ({x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}, a.kwarg is not None)
    return out


def _calls_into(text: str, stem: str) -> list[tuple[str, set[str], int]]:
    """Calls in `text` to functions of the module `stem` (from stem import f / import stem; stem.f(...)):
    (function name, keyword names passed, line)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    names: dict[str, str] = {}                              # local name -> the function's name in stem
    mods: set[str] = set()                                  # local names of the module itself
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == stem and not n.level:
            for al in n.names:
                names[al.asname or al.name] = al.name
        elif isinstance(n, ast.Import):
            for al in n.names:
                if al.name == stem:
                    mods.add(al.asname or al.name)
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        fn = None
        if isinstance(n.func, ast.Name) and n.func.id in names:
            fn = names[n.func.id]
        elif isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id in mods:
            fn = n.func.attr
        if fn:
            out.append((fn, {k.arg for k in n.keywords if k.arg}, n.lineno))
    return out


def dropped_uses(rel: str, old: str, new: str, others: dict[str, str]) -> list[str]:
    """What an edit to the Python file `rel` takes away that another file still uses: a function it imports that's
    gone, or a parameter it passes by name that the function no longer takes (none: []). Only what changed counts:
    a call that was already wrong before the edit isn't blamed on it. (Nov 20, task 69: a build edit took
    `enabled` out of readme_lesson(); test_request.py, which a fix may not change, still passed enabled=True, and
    three fix rounds went to the test files instead of putting the parameter back.)"""
    if new == old or not rel.lower().endswith(".py"):
        return []
    before, after = _signatures(old), _signatures(new)
    if before is None or after is None:
        return []                                           # the parse check speaks for a file that doesn't parse
    stem = PurePosixPath(rel).with_suffix("").as_posix().replace("/", ".")
    found: dict[str, list[int]] = {}                       # what's taken away, in which file -> its lines
    for other, text in sorted(others.items()):
        if other == rel or not other.lower().endswith(".py"):
            continue
        for fn, kws, line in _calls_into(text, stem):
            if fn not in before:
                continue
            if fn not in after:
                found.setdefault(f"{fn}() is gone, but {other} calls it", []).append(line)
                continue
            had, had_kw = before[fn]
            has, has_kw = after[fn]
            lost = sorted(k for k in kws if (k in had or had_kw) and k not in has and not has_kw)
            if lost:
                found.setdefault(f"{fn}() no longer takes {', '.join(lost)}, but {other} passes "
                                 + ("it" if len(lost) == 1 else "them"), []).append(line)
    return [f"{what} (line{'s' if len(ls) > 1 else ''} {', '.join(map(str, sorted(set(ls))[:6]))}"
            + (", ..." if len(set(ls)) > 6 else "") + ")" for what, ls in found.items()]


_CALL_PROBLEM = re.compile(r'^(\S+) line (\d+): pyright: (?:No parameter named|Argument missing for parameter)s? "(\w+)"')


def aimed_call_problems(problems: list[str], written: dict[str, str], locked: set[str]) -> list[str]:
    """A call problem in a file a fix may not change (the request's tests) said again for the file that defines the
    function, so that file's fix puts the parameter back (Nov 20, task 69: every fix round went to test_request.py,
    whose tests a fix doesn't change, and the problem stayed for three rounds)."""
    out = []
    for x in problems:
        m = _CALL_PROBLEM.match(x)
        if not m or m.group(1) not in locked or m.group(1) not in written:
            continue
        rel, line, param = m.group(1), int(m.group(2)), m.group(3)
        for other in written:
            if other == rel or not other.lower().endswith(".py"):
                continue
            stem = PurePosixPath(other).with_suffix("").as_posix().replace("/", ".")
            for fn, _, at in _calls_into(written[rel], stem):
                if at == line and fn in (_signatures(written[other]) or {}):
                    out.append(f"{other} {fn}() is called with {param} by {rel} line {line}, whose tests a fix "
                               f"doesn't change: give {fn}() the parameter {param} (with a default), as the call "
                               "expects")
    return list(dict.fromkeys(out))


_RETEST_WHO = re.compile(r"request'?s?\W+(?:own\W+)?tests?\b|test_request\.py|tests?\W+(?:written\W+)?from\W+the\W+"
                         r"request|\b\d+\W+tests\W+that\W+failed", re.I)
_RETEST_DO = re.compile(r"\b(?:chang\w*|rewrit\w*|correct\w*|updat\w*|check\w*\W+only|sets?\W+up)\b", re.I)


def request_retests(request: str) -> set[str]:
    """The request's own tests (test_request.py) the request itself asks to be changed: the test names a sentence
    asking that names, or {"*"} (all of them) when such a sentence names none; set() when the request doesn't ask.
    A fix may then change those tests, under every other guard (no assertion taken out, nothing the request names
    removed, no weaker check). (Nov 20, task 69: fix (c) asked for the request's own tests to look only at the
    "Not in my glossary yet:" line, and "test_request.py holds the tests written from the request and a fix doesn't
    change them" refused it, so the miswritten tests could never pass; the user: "fix the smithy, not the request".)"""
    out: set[str] = set()
    for sentence in retest_sentences(request):
        out |= set(re.findall(r"\b(test_\w+)\b", sentence)) - {"test_request"} or {"*"}
    return out


def retest_sentences(request: str) -> list[str]:
    """The request's sentences that ask for its own tests to change (request_retests), as written: told to the judge
    and to the step that corrects those tests, so both work from the request's own words (Nov 21, task 70)."""
    out: list[str] = []
    for sentence in re.split(r"\n\s*\n|(?<=[.;])\s+(?=[A-Z(])|\n(?=\s*(?:[-*]|\(?[a-z0-9]\)|\d+\.)\s)", request or ""):
        who = _RETEST_WHO.search(sentence)              # the tests named first, then what's done to them,
        if who and (_RETEST_DO.search(sentence, who.end())   # or an order right before them ("Correct test_request.py")
                    or re.search(r"\b(?:change|rewrite|correct|update)\W+(?:\w+\W+){0,2}$", sentence[:who.start()], re.I)):
            out.append(" ".join(sentence.split()))
    return out


def weakened_tests(old: str, new: str) -> list[str]:
    """Why each test an edit changed now checks less than it did (none: []): an assertion looks inside a part of
    what it looked at whole (the saved entry -> one field of it), or a loop of values to check lost some."""
    a, b = _func_nodes(old), _func_nodes(new)
    why = []
    for name in sorted(n for n in a if n in b and _norm(ast.unparse(a[n])) != _norm(ast.unparse(b[n]))):
        before, after = _scopes(a[name]), _scopes(b[name])
        narrowed = [k for k, d in before.items() if k in after and after[k] > d]
        if narrowed:
            why.append(f"{name} now checks only a part of what it checked before (of "
                       + ", ".join(f"`{k}`" for k in narrowed) + "): keep checking all of it, as the request says")
        old_loops = _loop_values(a[name])
        new_loops = _loop_values(b[name])
        lost = [v for v in old_loops if not any(v <= w for w in new_loops)
                and any(w < v for w in new_loops)]
        if lost:
            gone = sorted({str(x) for v in lost for x in v} - {str(x) for w in new_loops for x in w})
            why.append(f"{name} checks fewer values than before (dropped: {', '.join(gone[:6])})")
    return why


def _test_fix_missed(test: str, said: str, before: str, after: str) -> bool:
    """A Sandbox fix of a failing test that was tried (edits were sent) but left its failing tests as they were.
    "No edits sent" means the model judged the test right, so the code is fixed as usual; so is a test whose code
    couldn't be found (before is '')."""
    if not before or not said or "no edits sent" in said:
        return False
    return _norm(before) == _norm(after)


def _stage_files(staged: list[str], written: dict, change: str) -> list[str]:
    """The files one stage edits: the long earlier-version files being edited, plus any other file of this build
    that the stage's own changes name (Nov 15, task 55: item 4 said academy_explain.py and academy_readme.py call
    record(); both were new files, so only the old ones were offered, and the model sent their edits to
    academy_glossary.json, where none could go in)."""
    named = sorted(_names(change, [{"path": r} for r in written if r not in staged and not r.lower().endswith(".md")],
                          stems=True))         # task 56: "academy_explain and academy_readme call record()", no .py
    return list(staged) + named


def _keep_unasked(plan: dict, parent: dict, change: str) -> None:
    """A new version's plan, checked in code (Nov 15, task 52: "Fix README.md only... Change no other file", and
    the plan marked all 8 files "new", so the short ones were written again):
    - a file the earlier version already has is never "new": at most "changed"
    - when the change limits itself to the files it names ("only", "no other file"), every other file is "same".
    Which files it names (Nov 15, task 54: "academy_sources.py and academy_fit.py ... leave them as they are" counted
    as asking for them, and "Use only the standard library" as "only"): its "Files:" lines when it has them;
    otherwise every name in it, less those in a "leave them as they are / alone" sentence."""
    before = {f["path"] for f in parent["plan"]["files"]}
    lines = _FILES_LINE.findall(change)
    named = _names("\n".join(lines), plan["files"]) if lines else _names(change, plan["files"])
    left = _names(" ".join(_LEAVE.findall(change)), plan["files"]) - (named if lines else set())
    named -= left
    limits = _STDLIB_ONLY.sub(" ", change)
    only = bool(_ONLY.search(limits)) and (named or re.search(r"\breadme\b", change, re.I))
    for f in plan["files"]:
        if f["path"] in before and f.get("change") == "new":
            f["change"] = "changed"
        if f["path"] in before and (f["path"] in left or only and f["path"] not in named):
            f["change"] = "same"
    # every file of the earlier version stays unless the change asks for it to go (Nov 19, task 61: "run your own
    # self check for bloat" got a plan of 9 of the Academy's 14 files, and the other 5 modules were simply gone)
    planned = {f["path"] for f in plan["files"]}
    earlier = earlier_files(parent)
    gone = _names(" ".join(_REMOVE.findall(change)), earlier, stems=True)
    for f in earlier:
        if f["path"] not in planned and f["path"] not in gone:
            plan["files"].append({"path": f["path"], "purpose": f.get("purpose", ""), "change": "same"})
            plan.setdefault("kept_unplanned", []).append(f["path"])


def parse_plan(text: str, max_files: int) -> tuple[str, str, dict]:
    """(spoken summary, description, plan) from the planning answer. Raises ValueError."""
    summary, full = split_result(text)
    m = None
    for m in _JSON_BLOCK.finditer(full):
        pass                                   # the last json block is the plan
    raw = m.group(1) if m else full[full.find("{"):full.rfind("}") + 1]
    try:
        plan = json.loads(raw)
    except Exception:
        raise ValueError("the plan didn't come back in a form I could read")
    if not isinstance(plan, dict) or not isinstance(plan.get("files"), list):
        raise ValueError("the plan didn't list any files")
    files, seen, refused = [], set(), []
    for f in plan["files"][:max_files]:
        if not isinstance(f, dict):
            continue
        try:
            rel = safe_rel(f.get("path", ""))
        except ValueError as e:              # left out, and said so in the result
            refused.append(str(e))
            continue
        if rel.lower() == "readme.md" or rel.lower() in seen:
            continue
        seen.add(rel.lower())
        files.append({"path": rel, "purpose": str(f.get("purpose", ""))[:300],
                      "change": str(f.get("change", "new")).lower()})
    if not files:
        raise ValueError("the plan has no files I'm allowed to write" + (f" ({refused[0]})" if refused else ""))
    desc = (_JSON_BLOCK.sub("", full) if m else full.replace(raw, "")).strip()
    if refused:
        desc += "\n\n**Left out (not allowed):** " + "; ".join(refused)
    return summary, desc, {"name": slug(plan.get("name") or ""), "files": files}


_RUNTIME_DIRS = {"state", "drafts", "logs", "brain"}      # Othos's own folders a program writes while it runs
_FOLDER_WORDS = re.compile(r"(?i)\b(?:folder|directory|dir)\b")


def not_build_files(files: list[dict]) -> tuple[list[dict], list[str]]:
    """(the plan's files that are files of the build, why each other one was left out). Left out (Nov 16, task 57:
    the plan listed state/academy-glossary.json, drafts/academy/queries and drafts/academy, and the build failed
    "Permission denied" writing a folder as a file):
    - a folder: a path another planned path is inside, or one with no file ending whose purpose calls it a folder;
    - a place the program writes to while it runs: under state/, drafts/, logs/ or brain/ (made at run time,
      never shipped in the build)."""
    paths = [f["path"].replace("\\", "/") for f in files]
    kept, left = [], []
    for f, p in zip(files, paths):
        if any(o != p and o.lower().startswith(p.lower().rstrip("/") + "/") for o in paths) \
                or (not PurePosixPath(p).suffix and _FOLDER_WORDS.search(f.get("purpose", ""))):
            left.append(f"{p} (a folder, not a file)")
        elif p.split("/")[0].lower() in _RUNTIME_DIRS:
            left.append(f"{p} (written by the program when it runs, under {p.split('/')[0]}/, not part of the build)")
        else:
            kept.append(f)
    return kept, left


_REFUSAL = re.compile(r"^\W*(?:I[â€™']?m sorry|I am sorry|Sorry[,!]?\s+(?:but|I)\b|I can[â€™']?t|I cannot|I'm unable|I am unable|"
                      r"Unfortunately,? I|As an AI)", re.I)


def _refusal(body: str) -> bool:
    """The model saying it can't write the file, instead of the file (short, starts like an apology)."""
    return len(body) < 1500 and bool(_REFUSAL.match(body.strip()))


def _join_continued(text: str, more: str) -> str:
    """A cut-off reply plus the model's "continue". When the continuation starts the file over
    (its first code line is the file's first code line), keep the fresh copy instead of both
    (Nov 13: run-setup.ps1 came out twice, joined mid-word). When it repeats the last few lines,
    drop the repeat."""
    if more.lstrip().startswith("```"):                 # "```csharp" opening a new block: drop that whole line
        more = more.lstrip().split("\n", 1)[1] if "\n" in more.lstrip() else ""
    head, sep, code = text.partition("```")
    code_lines = [l for l in code.split("\n")[1:] if l.strip()] if sep else []
    more_lines = [l for l in more.split("\n") if l.strip()]
    if code_lines and more_lines and more_lines[0].strip() == code_lines[0].strip():
        fence_line = code.split("\n", 1)[0]
        fresh = head + "```" + fence_line + "\n" + more.lstrip("\n")
        # keep whichever got further: a fresh copy cut off at the same place again is no progress
        # (Nov 13, installer v4: restarts in a row left the file cut off)
        return fresh if len(fresh) > len(text) else text
    for k in range(min(len(text), len(more), 400), 19, -1):   # the overlap: the end of text = the start of more
        if text.endswith(more[:k]):
            return text + more[k:]
    return text + more


def _strip_outer_fence(text: str, markdown: bool) -> str:
    t = text.strip()
    m = re.match(r"^```[^\n]*\n([\s\S]*)\n```\s*$", t)          # the whole answer is one block
    if m:
        return m.group(1) + "\n"
    if not markdown:
        first = re.search(r"```[^\n]*\n", t)                       # prose around the block: take the block
        if first:
            last = t.rfind("\n```")
            if last > first.end():
                return t[first.end():last] + "\n"
    return t + "\n"


# Windows' own C# compiler (.NET Framework csc.exe, used by the Smithy's and Firestarter's apps) only
# knows C# 5 (Nov 13: the installer's $"..." strings gave 49 errors).
CSHARP5 = (" It's compiled with the C# compiler that comes with Windows (.NET Framework csc.exe), which only "
           "knows C# 5: no $\"...\" strings (use string.Format or +), no ?. or ?? =, no => bodies on methods or "
           "properties, no nameof, no tuples, no out var, no using static.")
_WINDOWS_CSC = re.compile(r"csc(?:\.exe)?\b|\.NET Framework|Framework64|compiler that comes with Windows", re.I)


def stable_start(paths, plan: dict) -> list[str]:
    """`paths` with the files this version leaves as they are first, then the files it changes, each group in the
    order given (the order they were written, which only grows). A step's start is then the same from one step to the
    next up to the first file that changes, which a provider's prompt cache and this PC's server can reuse (Nov 19,
    colibri idea: Cerebras's cached tokens, llama-server's cache reuse)."""
    changed = {f.get("path") for f in plan.get("files", []) if f.get("change") not in ("same", None)}
    return sorted(paths, key=lambda r: r in changed)


def _file_messages(p: dict, desc: str, plan: dict, rel: str, written: dict[str, str], budget: int,
                   old: str | None) -> list[dict]:
    flavour = ("\n\n" + JOOMLA) if p.get("joomla") else ""
    listing = "\n".join(f"- {f['path']}: {f['purpose']}" for f in plan["files"])
    purpose = next((f["purpose"] for f in plan["files"] if f["path"] == rel), "")
    md = rel.lower().endswith(".md")
    context, used = {}, 0
    for path, body in reversed(list(written.items())):            # the most recent files first
        piece = f"--- {path} ---\n{body}"
        if used + len(piece) > budget:
            context[path] = f"--- {path} --- (written; too long to show here)"
            continue
        context[path] = piece
        used += len(piece)
    msgs = [{"role": "system", "content": SYSTEM + flavour},
            {"role": "user", "content": f"The project: {redact(p['request'])}\n\nThe design:\n{desc[:4000]}\n\n"
                                        f"All its files:\n{listing}"}]
    if context:
        msgs.append({"role": "user", "content": "Files already written:\n" + "\n\n".join(
            context[r] for r in stable_start(reversed(list(context)), plan))})
    if old is not None:
        msgs.append({"role": "user", "content": f"The earlier version of {rel}:\n{old[:budget // 2]}\n\n"
                                                f"The change wanted: {redact(p.get('change', ''))}"})
    msgs.append({"role": "user", "content":
                 f"Now write the complete file {rel} ({purpose})."
                 + (CSHARP5 if rel.lower().endswith(".cs") and _WINDOWS_CSC.search(
                     # the request, this version's change, the design, the earlier version of this file
                     # and the files written so far (a build script naming csc.exe): any of them
                     " ".join([str(p.get("request", "")), str(p.get("change", "")), desc, old or ""]
                              + list(written.values()))) else "") + " " +
                 ("Reply with only the file's text, no outer code fence." if md else
                  "Reply with only one fenced code block that holds the whole file, nothing before or after it.")})
    return msgs


# ---------------------------------------------------------------- builds: editing a long file
# A long file in a new version is changed by find/replace pairs that the Smithy applies itself, so
# lines the model wasn't asked to change can't change (Nov 13: installer v7, v8 and v9, about 1,000
# lines each, came back rewritten; v9 was given 11 exact edits and still dropped whole methods).
EDIT_FORMAT = ("Reply with only the edits, each one in exactly this form:\n"
               "<<<<<<< FIND\n(lines copied exactly from the current file, enough to be found only once)\n"
               "=======\n(the lines that replace them; nothing here deletes them)\n>>>>>>> REPLACE\n"
               "Copy the FIND lines character for character, with their indentation. Keep each edit small. "
               "To add new lines, FIND the line just before where they go and REPLACE it with that same line "
               "followed by the new ones. Don't send the whole file. If your answer has to be JSON, send "
               "{\"edits\": [{\"find\": ..., \"replace\": ...}]} with the same FIND and REPLACE text.")
_EDIT = re.compile(r"<<<<<<< FIND[ \t]*\n([\s\S]*?)\n=======[ \t]*\n([\s\S]*?)\n?>>>>>>> REPLACE")


# the edits as an answer shape (worker_shared.answer_shape): where the model is held to it, every answer is a
# list of find/replace pairs, never prose or a broken block (Nov 16, checkers plan phase 2)
EDITS_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["edits"],
                "properties": {"edits": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["find", "replace"],
                    "properties": {"find": {"type": "string"}, "replace": {"type": "string"}}}}}}


def parse_edits(text: str) -> list[tuple[str, str]]:
    """The (find, replace) pairs in a reply, in order: {"edits": [...]} JSON, or FIND/REPLACE blocks."""
    t = text.strip()
    if t.startswith("{"):
        try:
            data = json.loads(t)
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("edits"), list):
            return [(str(e["find"]).replace("\r\n", "\n"), str(e["replace"]).replace("\r\n", "\n"))
                    for e in data["edits"] if isinstance(e, dict) and "find" in e and "replace" in e
                    and str(e["find"]).strip()]
    return [(m.group(1), m.group(2)) for m in _EDIT.finditer(text.replace("\r\n", "\n"))]


def _unfence(part: str) -> str:
    """A ``` fence line wrapped around an edit's FIND or REPLACE, taken off (task 40)."""
    lines = part.split("\n")
    if lines and re.fullmatch(r"[ \t]*```[\w#+.-]*[ \t]*", lines[0]):
        lines = lines[1:]
        if lines and re.fullmatch(r"[ \t]*```[ \t]*", lines[-1]):
            lines = lines[:-1]
    return "\n".join(lines)


def _find_once(body: str, find: str) -> tuple[int, int] | str:
    """Where `find` is in `body`, as (start, end); or why it can't be used. Exact first; then line by
    line ignoring spaces at the ends of lines (models often get indentation or trailing spaces wrong).
    It has to be found exactly once."""
    if not find.strip():
        return "the FIND part is empty"
    if len(re.sub(r"[^A-Za-z0-9]", "", find)) < 3:              # Nov 13, v13: a bare ``` fence as the anchor
        return "its FIND text is too short to be sure where it goes (include the line above it)"
    lines = body.split("\n")

    def twice(line_nums: list[int]) -> str:
        # say where each copy is, with the line above it, so the next try can pick the right one (Nov 14,
        # installer v14: the 3 edits that missed were the window-size ones, "this.MaximizeBox = false;" twice)
        where = "; ".join(f"line {i + 1}, below \"{next((l.strip() for l in reversed(lines[:i]) if l.strip()), '')[:70]}\""
                          for i in line_nums[:4])
        return (f"its FIND text is in the file {len(line_nums)} times ({where}): start the FIND with the line "
                "above the one you mean")

    n = body.count(find)
    if n == 1:
        i = body.index(find)
        return i, i + len(find)
    if n > 1:
        return twice([body.count("\n", 0, m.start()) for m in re.finditer(re.escape(find), body)])
    want = [l.strip() for l in find.strip("\n").split("\n")]
    starts = [0]
    for l in lines:
        starts.append(starts[-1] + len(l) + 1)
    hits = [i for i in range(len(lines) - len(want) + 1)
            if all(lines[i + k].strip() == want[k] for k in range(len(want)))]
    if len(hits) == 1:
        i = hits[0]
        return starts[i], starts[i + len(want)] - 1                 # whole lines, without the last newline
    if len(hits) > 1:
        return twice(hits)
    if "[REDACTED]" in find:              # the model saw the file with a value hidden: match it as shown
        hits = [i for i in range(len(lines) - len(want) + 1)
                if all(redact(lines[i + k]).strip() == want[k] for k in range(len(want)))]
        if len(hits) == 1:
            i = hits[0]
            return starts[i], starts[i + len(want)] - 1
        if len(hits) > 1:
            return twice(hits)
    return "its FIND text isn't in the file"


def _reindent(repl: str, indent: str, first_has_it: bool) -> str:
    """REPLACE lines that came back at the left margin get the indentation of the line they replace
    (brace languages only; Nov 14, installer v14: every inserted line lost its indentation).
    C# / C preprocessor lines (#if, #region) stay at the margin."""
    out = []
    for i, l in enumerate(repl.split("\n")):
        if l.strip() and not l[:1].isspace() and not l.startswith("#") and not (i == 0 and first_has_it):
            l = indent + l
        out.append(l)
    return "\n".join(out)


_ELSEWHERE = "its FIND text is from "
NEAR_LINES = 15


def _occurrences(body: str, find: str) -> list[tuple[int, int]]:
    """Every place `find` is in `body`, as (start, end): exact copies, else line by line ignoring spaces at the
    ends of lines (as _find_once matches)."""
    if body.count(find) > 1:
        return [(m.start(), m.end()) for m in re.finditer(re.escape(find), body)]
    lines = body.split("\n")
    want = [l.strip() for l in find.strip("\n").split("\n")]
    starts = [0]
    for l in lines:
        starts.append(starts[-1] + len(l) + 1)
    return [(starts[i], starts[i + len(want)] - 1) for i in range(len(lines) - len(want) + 1)
            if all(lines[i + k].strip() == want[k] for k in range(len(want)))]


_QUOTED = re.compile(r'"((?:[^"\\\n]|\\.){8,})"')
_QUOTE_WORD = re.compile(r"[\w\\/-]+(?:\.[\w\\/-]+)*")     # file names keep their dots, a sentence's end doesn't


def keep_quotes(find: str, repl: str, known: str) -> tuple[str, list[tuple[str, str]]]:
    """A quoted text the edit retyped with one word misspelt gets its original back: (REPLACE, [(new, old)]).
    Nov 19, task 59: adding a line above the "Setup is done" message, the model retyped that message and wrote
    start-outhos.bat for start-othos.bat, and "every other line kept" was still said. Only one word changed, a
    small change to that word, and the new word in nothing the model was given (`known`: its messages), so a
    change the request asks for ("say start-othos.bat") is never undone."""
    if not known:
        return repl, []
    olds = _QUOTED.findall(find)
    put_back = []
    for new in dict.fromkeys(_QUOTED.findall(repl)):
        if new in olds:
            continue
        for old in olds:
            words = None if f'"{old}"' in repl else _misspelt(old, new, known)
            if words and repl.count(words[0]) == new.count(words[0]):     # used elsewhere in the edit: a rename
                repl = repl.replace(f'"{new}"', f'"{old}"')
                put_back.append(words)
                break
    return repl, put_back


def _misspelt(old: str, new: str, known: str) -> tuple[str, str] | None:
    """(new word, old word) when `new` is `old` with one word slightly changed into a word `known` doesn't have."""
    a, b = _QUOTE_WORD.findall(old), _QUOTE_WORD.findall(new)
    ops = [o for o in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if o[0] != "equal"]
    if len(ops) != 1 or ops[0][0] != "replace" or ops[0][2] - ops[0][1] != 1 or ops[0][4] - ops[0][3] != 1:
        return None
    was, now = a[ops[0][1]], b[ops[0][3]]
    if now.lower() == was.lower() or "$" in new:      # a capital, or a PowerShell variable renamed inside the quotes
        return None
    # a slip changes a letter or two; a word grown or cut at an end (sensitive -> insensitive, a folder put in
    # front) or only its punctuation changed (unknownterm -> unknown-term) is meant
    if (was in now or now in was or abs(len(now) - len(was)) > 2
            or re.sub(r"[\W_]", "", now).lower() == re.sub(r"[\W_]", "", was).lower()):
        return None
    if difflib.SequenceMatcher(None, was, now).ratio() >= 0.75 and now not in known and new.replace(now, was) == old:
        return now, was
    return None


def misspelt_since(folder: Path, before: Path, rels: list[str], asked: str) -> list[str]:
    """Quoted texts that, since the version this one was built from (`before`), have one word slightly changed
    into a word neither the request nor that version has (Nov 19, task 59: "start-outhos.bat" for start-othos.bat,
    made in an earlier run, so the edit-time guard never saw it and a continue left it)."""
    problems = []
    for rel in rels:
        old_path, new_path = before / rel, folder / rel
        if not old_path.is_file() or not new_path.is_file():
            continue
        old_text = old_path.read_text(encoding="utf-8-sig", errors="replace")
        new_text = new_path.read_text(encoding="utf-8-sig", errors="replace")
        olds = set(_QUOTED.findall(old_text))
        news = set(_QUOTED.findall(new_text))
        gone = [o for o in olds if o not in news]
        for m in _QUOTED.finditer(new_text):
            if m.group(1) in olds:
                continue
            words = next((w for o in gone if (w := _misspelt(o, m.group(1), asked + "\n" + old_text))), None)
            # a word the new code uses outside this text too is a rename, not a slip
            if words and new_text.count(words[0]) == m.group(1).count(words[0]):
                line = new_text.count("\n", 0, m.start()) + 1
                problems.append(f"{rel} line {line}: this quoted text says {words[0]}, which was {words[1]} in the "
                                f"version before, and nothing asks for {words[0]}: put {words[1]} back")
    return problems


_PATH_FROM = re.compile(r'\b(?:Path\.Combine|os\.path\.join|Join-Path)\(?\s*(\$?\w+)\s*,\s*["\']([^"\'\\/]+)["\']')


def paths_off_base(folder: Path, before: Path, rels: list[str], asked: str) -> list[str]:
    """A path the request names built from a folder that the version before only ever took through one subfolder:
    v23 used targetFolder twice, both as Path.Combine(targetFolder, "agent", ...); v24 saved the request's
    state\\google-prompt.json as Path.Combine(targetFolder, "state"), outside agent\\, where Othos reads it (Nov 19,
    task 59). Only for a first folder the request names in a path ("state\\..."), and never for one the version
    before used too."""
    used: dict[str, set[str]] = {}
    counts: dict[str, int] = {}
    for p in before.rglob("*"):
        if p.is_file() and p.suffix.lower() in (".cs", ".py", ".ps1", ".psm1"):
            for base, seg in _PATH_FROM.findall(p.read_text(encoding="utf-8-sig", errors="replace")):
                used.setdefault(base, set()).add(seg)
                counts[base] = counts.get(base, 0) + 1
    problems = []
    for rel in rels:
        path = folder / rel
        if not path.is_file() or path.suffix.lower() not in (".cs", ".py", ".ps1", ".psm1"):
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        for m in _PATH_FROM.finditer(text):
            base, seg = m.group(1), m.group(2)
            only = used.get(base, set())
            if (len(only) != 1 or counts[base] < 2 or seg in only
                    or not re.search(rf"(?<![\w-]){re.escape(seg)}[\\/]", asked)):
                continue
            sub = next(iter(only))
            line = text.count("\n", 0, m.start()) + 1
            problems.append(f"{rel} line {line}: {seg} is built straight from {base}, but the version before only "
                            f"ever took {base} through \"{sub}\" ({counts[base]} times), where the program's own files "
                            f"are: build the request's {seg}\\ path inside {base}\\{sub} the same way")
    return problems


def apply_edits(body: str, edits: list[tuple[str, str]], brace: bool = False,
                others: dict[str, str] | None = None,
                spread: list[tuple[int, int]] | None = None,
                near: list[int] | tuple = (), known: str = "") -> tuple[str, int, list[str]]:
    """`known`: the text the model was given; a quoted text an edit retyped with a word misspelt gets its
    original back (keep_quotes).
    `near`: the lines the problems being fixed name; a FIND that's in the file several times goes to the one
    copy within NEAR_LINES of such a line, when exactly one copy is (Nov 18, task 59: a fix for unreachable code at
    line 1279 sent FIND "return;", in the file 12 times; it missed twice and a whole fix round was lost).
    Apply the pairs in order. Returns (new body, how many applied, problems for the ones that weren't).
    A pair that can't be found exactly once is skipped, never guessed. brace: a brace language (C#, JS,
    PHP, PowerShell, CSS...), where lines that lost their indentation get it back. others: the build's other
    files; a FIND found in one of them (not here) is said to be from that file (Nov 16, task 57: asked for
    test_academy.py, the model sent edits for academy_query.py's write_query, and the retry only said "isn't in
    the file")."""
    done, problems = 0, []
    # only a file with something hidden from the model can lose a value to [REDACTED]; a file with none may
    # use the text itself (task 51, Nov 15: a build whose own job is removing [REDACTED] couldn't be edited)
    hidden = redact(body) != body
    for k, (find, repl) in enumerate(edits, 1):
        if hidden and "[REDACTED]" in repl:   # it would write [REDACTED] over a value the model wasn't shown
            first = next((l.strip() for l in find.split("\n") if l.strip()), "")[:80]
            problems.append(f"edit {k} (FIND starting \"{first}\"): its REPLACE has [REDACTED] in it, which would "
                            "overwrite a hidden value: leave the lines with [REDACTED] out of the edit")
            continue
        where = _find_once(body, find)
        if isinstance(where, str) and near and " times (" in where:
            close = [o for o in _occurrences(body, find)
                     if any(abs(body.count("\n", 0, o[0]) + 1 - n) <= NEAR_LINES for n in near)]
            if len(close) == 1:
                where = close[0]
        if isinstance(where, str) and spread and find.strip() and body.count(find) > 1:
            # the same lines in several failing tests that the fix is for: changed in each (Nov 18, task 60: a
            # copied test held the same part-of-word check, and every edit was refused as "in the file 2 times")
            at = [body.count("\n", 0, m.start()) + 1 for m in re.finditer(re.escape(find), body)]
            if all(any(a <= ln <= b for a, b in spread) for ln in at):
                body = body.replace(find, repl)
                done += 1
                continue
        if isinstance(where, str) and _unfence(find) != find and _unfence(find).strip():
            # the model wrapped its FIND in a ``` fence (task 40): try it without, and its REPLACE too
            unf = _find_once(body, _unfence(find))
            if not isinstance(unf, str):
                where, find, repl = unf, _unfence(find), _unfence(repl)
        if isinstance(where, str) and where.endswith("isn't in the file") and others:
            src = next((name for name, text in others.items()
                        if not isinstance(_find_once(text.replace("\r\n", "\n"), find), str)), "")
            if src:
                where = f"{_ELSEWHERE}{src}, not this file, so it isn't an edit for this file"
        if isinstance(where, str):
            first = next((l.strip() for l in find.split("\n") if l.strip()), "")[:80]
            problems.append(f"edit {k} (FIND starting \"{first}\"): {where}")
            continue
        a, b = where
        repl, put_back = keep_quotes(find, repl, known)
        for now, was in put_back:
            steps.info("An edit retyped a quoted text with %s for %s; the original is kept.", now, was)
        line_start = body.rfind("\n", 0, a) + 1
        if brace and repl.strip():
            line_end = body.find("\n", line_start)
            here = body[line_start:line_end if line_end >= 0 else len(body)]
            indent = here[:len(here) - len(here.lstrip())]
            if indent and not body[line_start:a].strip():
                # the first REPLACE line already sits after the file's own indentation when FIND began after it
                repl = _reindent(repl, indent, first_has_it=a > line_start)
        if not body[line_start:a].strip() and repl[:1] in (" ", "\t"):
            a = line_start              # FIND left out the indentation but REPLACE has it: don't double it
        if not repl and a == line_start and body[b:b + 1] == "\n":
            b += 1                      # deleting whole lines: take their line break too
        body = body[:a] + repl + body[b:]
        done += 1
    return body, done, problems


def _edit_messages(p: dict, desc: str, plan: dict, rel: str, written: dict[str, str], old: str,
                   what: str = "", cap: int = 8000) -> list[dict]:
    """`cap`: characters of each file already written to show (a README needs to see the code)."""
    flavour = ("\n\n" + JOOMLA) if p.get("joomla") else ""
    listing = "\n".join(f"- {f['path']}: {f['purpose']}" for f in plan["files"])
    others = "\n\n".join(f"--- {path} ---\n{written[path][:cap]}" + ("\n(... cut here)" if len(written[path]) > cap
                                                                    else "")
                         for path in stable_start(written, plan))
    csharp = CSHARP5 if rel.lower().endswith(".cs") and _WINDOWS_CSC.search(
        " ".join([str(p.get("request", "")), str(p.get("change", "")), desc, old] + list(written.values()))) else ""
    msgs = [{"role": "system", "content": SYSTEM + flavour},
            {"role": "user", "content": f"The project: {redact(p['request'])}\n\nThe design:\n{desc[:4000]}\n\n"
                                        f"All its files:\n{listing}"}]
    if others:
        msgs.append({"role": "user", "content": "Files already written for this version:\n" + others})
    msgs.append({"role": "user", "content": f"The current {rel} (the whole file):\n```\n{old}\n```"})
    msgs.append({"role": "user", "content": f"The change wanted: {redact(p.get('change', ''))}\n\n"
                                            + (what or f"Change {rel} only where the change needs it")
                                            + f"; every other line stays exactly as it is.{csharp}\n\n"
                                            f"Send edits for {rel} only: every FIND is copied from {rel} as shown "
                                            "above, never from another file (a change another file needs is asked "
                                            f"of that file separately).\n\n{EDIT_FORMAT}"})
    return msgs


def _sort_out_elsewhere(edits: list[tuple[str, str]], old: str, others: dict[str, str], movable: set[str],
                        elsewhere: dict[str, list[tuple[str, str]]]) -> list[tuple[str, str]]:
    """The edits for this file; each one whose FIND isn't in it but is in exactly one other file the build may
    change goes to `elsewhere[that file]` instead of being thrown away (Nov 17, task 58: 6 good code edits were
    sent for test_academy.py and all refused)."""
    mine = []
    body = old.replace("\r\n", "\n")
    for find, repl in edits:
        here = _find_once(body, find) if find.strip() else ""
        if isinstance(here, str) and here.endswith("isn't in the file"):
            homes = [r for r in sorted(movable) if r in others
                     and not isinstance(_find_once(others[r].replace("\r\n", "\n"), find), str)]
            if len(homes) == 1:
                elsewhere.setdefault(homes[0], []).append((find, repl))
                continue
        mine.append((find, repl))
    return mine


EXTRA_FIX_ROUNDS = 2     # more check rounds, one at a time, while each round puts something right
RETRY_WAIT_S = 65        # seconds a second try at missed edits waits for a strong model (a minute's limit clears)


def _edit_file_once(ctx, job_id: int, msgs: list[dict], rel: str, old: str,
               none_ok: bool = True, others: dict[str, str] | None = None,
               movable: set[str] | None = None,
               elsewhere: dict[str, list[tuple[str, str]]] | None = None,
               spread: list[tuple[int, int]] | None = None,
               near: list[int] | tuple = ()) -> tuple[str, str]:
    """A long file changed by find/replace pairs. Returns (new body, a note for the result).
    No edits at all: the file is kept as it was (the plan can call a file "changed" that the change
    doesn't touch; Nov 13, task 18 failed on that), or with none_ok=False, ValueError.
    `others`: the build's other files, so an edit whose FIND is from one of them is named as such.
    `movable` + `elsewhere`: an edit clearly for one of the `movable` files is put in `elsewhere` for the caller."""
    others = {k: v for k, v in (others or {}).items() if k != rel}
    steps.info("Editing %s: asking for the changes as edits.", rel)
    with worker_shared.answer_shape(EDITS_SCHEMA):
        reply = ctx.chat(msgs, 6000)
    edits = parse_edits(reply)
    if edits and movable and elsewhere is not None:
        edits = _sort_out_elsewhere(edits, old, others, movable - {rel}, elsewhere)
        if not edits:
            return old, f"`{rel}`: its edits were all for other files, so they went there"
    if not edits:
        if none_ok:
            log.info("build %d: %s, no edits sent; kept as it was", job_id, rel)
            steps.info("%s needs no change; kept exactly as it was.", rel)
            return old, f"`{rel}`: no edits sent, so kept exactly as it was"
        raise ValueError(f"the changes to {rel} didn't come back as edits I could apply")
    brace = Path(rel).suffix.lower() in _BRACES
    known = "\n".join(str(m.get("content", "")) for m in msgs)
    body, done, problems = apply_edits(old, edits, brace, others, spread, near, known)
    if problems:                                          # one more try for the ones that missed
        log.info("build %d: %s, %d of %d edits applied; asking again for %d", job_id, rel, done, len(edits),
                 len(problems))
        steps.info("%s: %d of %d edits went in; %d couldn't be found, asking again for %s.", rel, done, len(edits),
                   len(problems), "it" if len(problems) == 1 else "them")
        retry_msgs = msgs + [{"role": "assistant", "content": reply},
                             {"role": "user", "content":
                              "These edits couldn't be applied, so they weren't:\n- " + "\n- ".join(problems)
                              + f"\n\nThe others are already in. Here is {rel} now:\n```\n{body}\n```\n\n"
                              + (f"Edits whose FIND is from another file aren't for {rel}: drop them. "
                                 if any(_ELSEWHERE in x for x in problems) else "")
                              + f"Send only the missed edits for {rel} again, with the FIND lines copied "
                              "exactly from it. " + EDIT_FORMAT}]
        # the second try waits for a strong cloud model, never this PC's: a per-minute limit clears in about a minute
        # (Nov 18, task 60: every provider was busy for one minute, so this went to this PC's model, which worked 40
        # minutes and sent 0 usable edits). Still none after the wait: the missed edits stay missed this round.
        again = ""
        min_b = float((getattr(ctx, "cfg", None) or {}).get("rewrite_min_model_b", 70) or 0)
        stop = getattr(ctx, "_should_stop", None)
        for wait in (0, RETRY_WAIT_S):
            if wait:
                steps.info("No strong model has room for the missed edits; waiting %d seconds for one.", wait)
                end = time.monotonic() + wait
                while time.monotonic() < end:
                    if stop:
                        stop(0)
                    time.sleep(1)
            try:
                with worker_shared.answer_shape(EDITS_SCHEMA), worker_shared.strong_models(min_b):
                    again = ctx.chat(retry_msgs, 6000)
                break
            except worker_cloud.NoStrongModel as e:
                log.info("build %d: %s, missed edits: %s", job_id, rel, e)
        if not again:
            steps.info("%s: no strong model was free for the missed edits; they're left for the next round.", rel)
        more = parse_edits(again)
        if more and movable and elsewhere is not None:
            more = _sort_out_elsewhere(more, body, others, movable - {rel}, elsewhere)
        if more:                                       # no edits in the reply: the missed ones stay missed
            body, done2, problems = apply_edits(body, more, brace, others, spread, near, known)
            done += done2
    note = f"`{rel}`: changed by {done} edit{'s' if done != 1 else ''}; every other line kept as it was"
    if problems:
        note += ". **Not applied (couldn't be found exactly once):** " + "; ".join(problems)
    log.info("build %d: %s, %d edits applied, %d not", job_id, rel, done, len(problems))
    steps.info("%s: %d edit%s in%s; every other line kept.", rel, done, "" if done == 1 else "s",
               f", {len(problems)} left out" if problems else "")
    return body, note


def _edit_file(ctx, job_id: int, msgs: list[dict], rel: str, old: str, none_ok: bool = True,
               others: dict[str, str] | None = None, callers: dict[str, str] | None = None,
               **kw) -> tuple[str, str]:
    """_edit_file_once, with one guard after it: edits that take away a function or a parameter another file still
    uses (dropped_uses) aren't kept; asked once more with what they took away, and if that's still so, the file
    stays as it was (Nov 20, task 69). `callers`: the files to look in (default: `others`); at build time the earlier
    version's files too, since the tests are written after the code."""
    body, note = _edit_file_once(ctx, job_id, msgs, rel, old, none_ok=none_ok, others=others, **kw)
    look = callers if callers is not None else (others or {})
    lost = dropped_uses(rel, old, body, look)
    if not lost:
        return body, note
    log.info("build %d: %s, edits take away what other files use: %s", job_id, rel, "; ".join(lost))
    steps.info("%s: the edits weren't kept, they take away what other files use (%s); asking again.", rel,
               "; ".join(lost[:3]))
    again = msgs + [{"role": "user", "content":
                     f"Your edits to {rel} were NOT kept: they take away what other files still use:\n- "
                     + "\n- ".join(lost) + "\n\nSome of those files can't be changed by a fix (the request's tests). "
                     "Send the edits again, keeping every function and parameter other files use; a new parameter "
                     "may be added, with a default. " + EDIT_FORMAT}]
    try:
        body2, note2 = _edit_file_once(ctx, job_id, again, rel, old, none_ok=True, others=others, **kw)
    except ValueError:
        body2, note2 = old, ""
    lost2 = dropped_uses(rel, old, body2, look)
    if not lost2:
        return body2, note2 + f" (the first edits took away what other files use: {'; '.join(lost)}; asked again)"
    log.info("build %d: %s, edits not kept twice: %s", job_id, rel, "; ".join(lost2))
    steps.info("%s: the edits weren't kept again (%s); the file stays as it was.", rel, "; ".join(lost2[:3]))
    return old, (f"`{rel}`: its edits were NOT kept: twice they took away what other files use "
                 f"({'; '.join(lost2)}), so it stays as it was")


# ---------------------------------------------------------------- builds: checking before the README
# Nov 14, installer v14: 11 numbered changes asked, 4 made, it didn't compile, and the README said
# all of it was done. So a build is now compiled / parsed (never run), the numbered changes are
# checked off, one round of edits goes to whatever failed, and the README is told the truth.
_CSC = [Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Microsoft.NET" / fw / "v4.0.30319" / "csc.exe"
        for fw in ("Framework64", "Framework")]
_CSC_REFS = ["System.dll", "System.Core.dll", "System.Data.dll", "System.Drawing.dll",
             "System.Windows.Forms.dll", "System.Xml.dll"]
_CSC_ERR = re.compile(r"^(.*?)\((\d+),\d+\): error (CS\d+): (.*)$")
# the compiler warnings that mean code never runs, read as problems (Nov 18, task 59: "return;" right after making the
# Google page left 80 lines that could never run, CS0162, and the build checked clean because only errors were read)
_CSC_WARN_BAD = re.compile(r"^(.*?)\((\d+),\d+\): warning (CS0162): (.*)$")
_NO_WINDOW = 0x08000000                                     # CREATE_NO_WINDOW
_BRACES = {".cs", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".php", ".ps1", ".psm1", ".css", ".scss", ".java", ".c", ".h"}


_SCRIPT_EXT = (".ps1", ".bat", ".cmd", ".sh")
_CS_NAME = re.compile(r"[\w.\\/*-]*\.cs\b", re.I)
_CS_TARGET = re.compile(r"[/-](?:target|t):(winexe|exe|library)\b", re.I)
_CS_REF = re.compile(r"[/-](?:r|reference):[\"'`]?([\w.-]+\.dll)", re.I)


def csc_script(folder: Path, rels: list[str]) -> dict | None:
    """The build's own script that runs csc (a .ps1 / .bat / .cmd that names csc), and what it compiles:
    {"script": rel, "files": [the .cs rels it names], "missing": [.cs names it names that aren't in the
    build], "target": "winexe" | "exe" | "library", "refs": [dlls]}. None when there's no such script.
    Nov 14: a new file the script never compiled still passed the check, which compiled every .cs."""
    cs = [r for r in rels if r.lower().endswith(".cs")]
    for rel in rels:
        if not rel.lower().endswith(_SCRIPT_EXT):
            continue
        text = (folder / rel).read_text(encoding="utf-8-sig", errors="replace")
        if not re.search(r"\bcsc(?:\.exe)?\b", text, re.I):
            continue
        named = {Path(n.replace("\\", "/")).name.lower() for n in _CS_NAME.findall(text)}
        if any("*" in n for n in named):                # "*.cs": everything
            files = cs
        else:
            files = [r for r in cs if Path(r).name.lower() in named]
        have = {Path(r).name.lower() for r in cs}
        missing = sorted(n for n in named if "*" not in n and n not in have)
        t = _CS_TARGET.search(text)
        return {"script": rel, "files": files, "missing": missing, "target": t.group(1).lower() if t else "library",
                "refs": sorted({m.lower() for m in _CS_REF.findall(text)})}
    return None


def _cs_errors(folder: Path, rels: list[str], target: str = "library", refs=()) -> list[str] | None:
    """Errors from compiling the build's C# files together, or None when there's no compiler."""
    csc = next((c for c in _CSC if c.exists()), None)
    if csc is None:
        return None
    with tempfile.TemporaryDirectory(prefix="smithy-check-") as tmp:
        out_name = "check.dll" if target == "library" else "check.exe"
        all_refs = list(dict.fromkeys([r.lower() for r in _CSC_REFS] + [r.lower() for r in refs]))
        args = [str(csc), "/nologo", "/target:" + target, "/out:" + str(Path(tmp) / out_name)] + \
               ["/r:" + r for r in all_refs] + [str(folder / r) for r in rels]
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=120, cwd=tmp,
                                 creationflags=_NO_WINDOW).stdout
        except Exception as e:
            return [f"the C# compiler couldn't be run ({e})"]
    errors = []
    for line in out.splitlines():
        m = _CSC_ERR.match(line.strip()) or _CSC_WARN_BAD.match(line.strip())
        if m:
            try:
                where = Path(m.group(1)).resolve().relative_to(folder.resolve()).as_posix()
            except ValueError:
                where = Path(m.group(1)).name
            errors.append(f"{where} line {m.group(2)}: {m.group(3)}: {m.group(4)}")
    return errors


_CS_NOISE = re.compile(r'//[^\n]*|/\*.*?\*/|@"(?:[^"]|"")*"|"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)+\'', re.DOTALL)
_CS_TYPE = re.compile(r"^[ \t]*(?:(?:public|internal|private|protected|sealed|abstract|static|partial)\s+)*"
                      r"(class|struct|enum|interface)\s+(\w+)", re.MULTILINE)
_CS_MAIN = re.compile(r"\bstatic\s+(?:void|int|async\s+\w+)\s+Main\s*\(")


def unused_cs_types(folder: Path, rels: list[str]) -> list[str]:
    """Types a program (/target:winexe or exe) never uses: a class, struct, enum or interface whose name appears
    nowhere in the compiled files but its own declaration and constructors (comments and strings don't count).
    Nov 18, task 59: GooglePrompt.cs's class compiled clean and nothing used it, because OthosSetup.cs had its own
    copy of the same page; two copies that drift apart, and nothing checked. The problem is named in the file with
    Main (the program's start), where the fix is to use the class, in place of any copy of it there."""
    texts = {r: _CS_NOISE.sub(lambda m: "\n" * m.group(0).count("\n"),
                              (folder / r).read_text(encoding="utf-8-sig", errors="replace")) for r in rels}
    starts = {r: s.start() for r, t in texts.items() if (s := _CS_MAIN.search(t))}
    main = next(iter(starts), None)
    problems = []
    for rel, text in texts.items():
        for m in _CS_TYPE.finditer(text):
            kind, name = m.group(1), m.group(2)
            if "partial" in m.group(0).split() or (rel == main and _owns_main(text, m.end())):
                continue
            word = re.compile(rf"\b{re.escape(name)}\b")
            ctor = re.compile(rf"(?:public|internal|private|protected|static)\s+{re.escape(name)}\s*\(")
            uses = sum(len(word.findall(t)) for t in texts.values()) - 1 - len(ctor.findall(text))
            if uses > 0:
                continue
            line = text.count("\n", 0, m.start()) + 1
            if main and main != rel:
                at = texts[main].count("\n", 0, starts[main]) + 1
                problems.append(f"{main} line {at}: the program never uses {kind} {name} from {rel} (line {line}): "
                                f"nothing creates or calls it, so its code never runs. Where it belongs, create it AND "
                                f"call what it does (e.g. its Show()), so it really runs; if {main} has its own copy of "
                                f"what {name} does, remove that copy completely, so there's one copy")
            else:
                problems.append(f"{rel} line {line}: the program never uses {kind} {name}: nothing creates or calls "
                                "it, so its code never runs. Where it belongs, create it AND call what it does, or take "
                                "it out if other code already does the same job")
    return problems


_CS_NEW_LOCAL = re.compile(r"\b(?:var|[A-Z]\w*(?:<[^<>;=]*>)?)\s+(\w+)\s*=\s*new\s+([A-Z]\w*)")


def unused_cs_objects(folder: Path, rels: list[str]) -> list[str]:
    """A local made with `new` and never used again in its block: `var prompt = new GooglePrompt(...);` and nothing
    calls prompt.Show(), so the page never shows (Nov 18, task 59, after the unused-class check: the class was
    "used" by being made, and nothing it does ever ran). `using (var x = new ...)` is left alone."""
    problems = []
    for rel in rels:
        text = _CS_NOISE.sub(lambda m: "\n" * m.group(0).count("\n"),
                             (folder / rel).read_text(encoding="utf-8-sig", errors="replace"))
        for m in _CS_NEW_LOCAL.finditer(text):
            name, made = m.group(1), m.group(2)
            if text[:m.start()].rstrip().endswith(("(", ".")):  # using (var x = ...), or a dotted type's tail
                continue
            depth, end = 0, len(text)
            for i in range(m.end(), len(text)):          # the rest of the block it's declared in
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth < 0:
                        end = i
                        break
            if re.search(rf"\b{re.escape(name)}\b", text[m.end():end]):
                continue
            line = text.count("\n", 0, m.start(1)) + 1
            problems.append(f"{rel} line {line}: {name} is made (new {made}) and never used after that, so nothing it "
                            f"does happens: call what it's for (e.g. {name}.Show()), or take it out")
    return problems


_CS_PRIVATE_METHOD = re.compile(r"\bprivate\s+(?:(?:static|async|unsafe|extern)\s+)*[\w<>\[\],.?]+\s+(\w+)\s*\(")


def unused_cs_methods(folder: Path, rels: list[str]) -> list[str]:
    """A private method whose name is nowhere in the build's files but its own declaration: nothing calls it, and
    the compiler says nothing (Nov 19, task 59: OthosSetup.cs kept its own WriteGooglePromptJson after the page
    moved to GooglePrompt.cs, which has the one that runs; two copies that drift apart)."""
    texts = {r: _CS_NOISE.sub(lambda m: "\n" * m.group(0).count("\n"),
                              (folder / r).read_text(encoding="utf-8-sig", errors="replace")) for r in rels}
    problems = []
    for rel, text in texts.items():
        for m in _CS_PRIVATE_METHOD.finditer(text):
            name = m.group(1)
            if name == "Main":
                continue
            word = re.compile(rf"\b{re.escape(name)}\b")
            # private: only its own class can call it, so its own file (all files when a class there is partial)
            where = texts.values() if re.search(r"\bpartial\s+class\b", text) else [text]
            if sum(len(word.findall(t)) for t in where) > 1:
                continue
            line = text.count("\n", 0, m.start(1)) + 1
            decl = re.compile(rf"[\w>\]?]\s+{re.escape(name)}\s*\(")
            twin = next((r for r, t in texts.items() if r != rel and decl.search(t)), "")
            problems.append(f"{rel} line {line}: the method {name} is never called, so its code never runs"
                            + (f"; {twin} has its own {name}, the one that runs, so remove this copy completely"
                               if twin else ": call it where it's needed, or remove it"))
    return problems


_CS1061 = re.compile(r"CS1061: '(?:[\w.]+\.)?(\w+)' does not contain a definition for '(\w+)'")
_CS_MEMBER = re.compile(r"\b(?:public|internal)\s+(?:(?:static|override|virtual|abstract|new|readonly)\s+)*"
                        r"[\w<>\[\],.?]+\s+(\w+)\s*(\(|\{|=|;)")


def name_cs_members(folder: Path, rels: list[str], errs: list[str]) -> list[str]:
    """A "does not contain a definition for X" error (CS1061) on a type from the build's own files says what it does
    have: "GooglePrompt has: Show()" (Nov 18, task 59: the fix called prompt.ShowDialog(), a Form's method, on a
    class whose method is Show(), and the rounds ran out before it was put right)."""
    if not any(_CS1061.search(e) for e in errs):
        return errs
    texts = {r: _CS_NOISE.sub(lambda m: "\n" * m.group(0).count("\n"),
                              (folder / r).read_text(encoding="utf-8-sig", errors="replace")) for r in rels}
    out = []
    for e in errs:
        m = _CS1061.search(e)
        has: list[str] = []
        if m:
            for text in texts.values():
                for t in _CS_TYPE.finditer(text):
                    if t.group(2) != m.group(1):
                        continue
                    body = _block_after(text, t.end())
                    has += [n + ("()" if k == "(" else "") for n, k in _CS_MEMBER.findall(body) if n != m.group(1)]
        out.append(e + (f". {m.group(1)} has: {', '.join(dict.fromkeys(has))}; use one of those" if m and has else ""))
    return out


def _block_after(text: str, end: int) -> str:
    """The text inside the first { ... } after `end` (comments and strings already blanked)."""
    open_at = text.find("{", end)
    if open_at < 0:
        return ""
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_at + 1:i]
    return text[open_at + 1:]


def _owns_main(text: str, end: int) -> bool:
    """Whether the type declared just before `end` holds Main within its braces (comments and strings already
    blanked, so the braces are the code's own)."""
    open_at = text.find("{", end)
    if open_at < 0:
        return False
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return bool(_CS_MAIN.search(text, open_at, i))
    return bool(_CS_MAIN.search(text, open_at))


_PS_PARSE = ("$ErrorActionPreference = 'SilentlyContinue'\n"
             "foreach ($p in $env:SMITHY_PARSE.Split('|')) { $e = $null\n"
             "  [void][System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$null, [ref]$e)\n"
             "  foreach ($x in $e) { '{0}|{1}|{2}' -f $p, $x.Extent.StartLineNumber, $x.Message } }")


def _ps_errors(folder: Path, rels: list[str]) -> list[str] | None:
    """Parse errors in the build's PowerShell files (Windows PowerShell 5.1's own parser; nothing is run)."""
    paths = {str((folder / r).resolve()): r for r in rels}
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand",
             base64.b64encode(_PS_PARSE.encode("utf-16-le")).decode()],
            capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW,
            env={**os.environ, "SMITHY_PARSE": "|".join(paths)}).stdout
    except Exception:
        return None
    errors = []
    for line in out.splitlines():
        path, _, rest = line.partition("|")
        num, _, msg = rest.partition("|")
        if num.strip().isdigit():
            errors.append(f"{paths.get(path, Path(path).name)} line {num}: {msg.strip()}")
    return errors


def build_problems(folder: Path, rels: list[str], stdlib_only: bool = False,
                   net_ok: bool = True) -> tuple[list[str], list[str]]:
    """(problems, what was checked). C# compiled together, PowerShell / Python / JSON parsed, Python
    imports between the files checked. Nothing runs."""
    problems, checked = [], []
    cs = [r for r in rels if r.lower().endswith(".cs")]
    script = csc_script(folder, rels) if cs else None
    if script:                                          # exactly what the build's own script compiles
        for r in cs:
            if r not in script["files"]:
                problems.append(f"{script['script']} line 1: it doesn't compile {r}, so none of that file's code "
                                "is in the program: add it to the files this script compiles")
        for n in script["missing"]:
            problems.append(f"{script['script']} line 1: it compiles {n}, which isn't in the build")
        # the files it leaves out are compiled with it all the same, so their own errors are named in their own
        # file and fixed there (Nov 17, task 59: GooglePrompt.cs's error showed only once the script compiled it,
        # was blamed on the script's edit, refused, and never sent to GooglePrompt.cs)
        cs = list(script["files"]) + [r for r in cs if r not in script["files"]]
    if cs:
        errs = _cs_errors(folder, cs, script["target"] if script else "library", script["refs"] if script else ())
        if errs is None:
            checked.append("C# not checked (no compiler on this PC)")
        else:
            problems += name_cs_members(folder, cs, errs)
            if not errs:
                problems += unused_cs_objects(folder, cs) + unused_cs_methods(folder, cs)
                if script and script["target"] in ("winexe", "exe"):
                    problems += unused_cs_types(folder, cs)    # a program, not a library: nothing may sit unused
            checked.append(f"C# compiled together ({len(cs)} file{'s' if len(cs) != 1 else ''}"
                           + (f", as {script['script']} compiles it: /target:{script['target']}" if script else "")
                           + ")")
    ps = [r for r in rels if r.lower().endswith((".ps1", ".psm1"))]
    if ps:
        errs = _ps_errors(folder, ps)
        if errs is None:
            checked.append("PowerShell not checked")
        else:
            problems += errs
            checked.append(f"PowerShell parsed ({len(ps)})")
    for r in rels:
        low = r.lower()
        if not low.endswith((".py", ".json")):
            continue
        text = (folder / r).read_text(encoding="utf-8-sig", errors="replace")
        try:
            if low.endswith(".py"):
                compile(text, r, "exec")                    # parsed only, never run
            else:
                json.loads(text)
        except SyntaxError as e:
            problems.append(f"{r} line {e.lineno}: {e.msg}")
        except json.JSONDecodeError as e:
            problems.append(f"{r} line {e.lineno}: {e.msg}")
    if any(r.lower().endswith((".py", ".json")) for r in rels):
        checked.append("Python / JSON parsed")
    py = [r for r in rels if r.lower().endswith(".py")]
    if py:
        found = py_import_problems(folder, py, stdlib_only)
        if found is not None:
            problems += found
            checked.append("Python imports between the files checked"
                           + (" (standard library only)" if stdlib_only else ""))
        tests = [r for r in py if _is_test(r)]
        for r in tests:
            problems += test_main_problems(r, (folder / r).read_text(encoding="utf-8-sig", errors="replace"))
            problems += chdir_problems(r, (folder / r).read_text(encoding="utf-8-sig", errors="replace"))
        if tests:
            checked.append("unittest.main() placed so the tests run")
        for r in py:
            text = (folder / r).read_text(encoding="utf-8-sig", errors="replace")
            problems += stdlib_patch_problems(r, text) + delete_problems(r, text) + doubled_escape_problems(r, text) \
                + ([] if net_ok else network_problems(r, text))
        checked.append("no change to Python's own modules, no deleting outside the build"
                       + ("" if net_ok else ", no network use (the request doesn't ask for it)"))
        unused = unused_py_code(folder, py)
        if unused is not None:
            problems += unused
            checked.append("no unused copies or private functions")
    more, said = smithy_checkers.check_all(folder, rels)      # ruff, pyright, PSScriptAnalyzer, tsc, javac
    return problems + more, checked + said


_PY_COMMENT = re.compile(r"#[^\n]*")
_WORD = re.compile(r"\b[A-Za-z_]\w*\b")
_FROM_IMPORT = re.compile(r"\bfrom\s+(\w+)\s+import\s+(?:\(([^)]*)\)|([^\n]*))")
_DOTTED = re.compile(r"\b(\w+)\.(\w+)\b")


def unused_py_code(folder: Path, rels: list[str]) -> list[str] | None:
    """Top-level functions and classes nothing in the build uses: a private one (_name), and a copy of one another
    file defines too, where the other copy is the one in use. Uses inside unused code don't count, so a dead copy's
    helpers go with it. Public names alone are left: other programs may call them (Nov 19, task 61's review:
    academy_sources.py held a whole unused copy of build_lesson and 8 helpers; academy_lesson.py has the one that
    runs, and no check saw it). None when a file doesn't parse (the parse error is named elsewhere)."""
    texts = {r: (folder / r).read_text(encoding="utf-8-sig", errors="replace") for r in rels}
    defs: dict[str, list[tuple[str, int, int]]] = {}       # rel -> [(name, first line, last line)]
    try:
        for r, t in texts.items():
            defs[r] = [(n.name, n.lineno, n.end_lineno or n.lineno) for n in ast.parse(t).body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    except SyntaxError:
        return None
    where: dict[str, set[str]] = {}
    for r, ds in defs.items():
        for name, _, _ in ds:
            where.setdefault(name, set()).add(r)
    stems = {Path(r).stem: r for r in rels}
    dead: set[tuple[str, str]] = set()
    while True:                                            # until nothing new: a dead copy's helpers die with it
        words: dict[str, collections.Counter] = {}
        used_from: dict[str, set[str]] = {r: set() for r in rels}     # rel -> names other files take from it
        for r, t in texts.items():
            lines = t.splitlines()
            for name, a, b in defs[r]:
                if (r, name) in dead:
                    lines[a - 1:b] = [""] * (b - a + 1)
            live = _PY_COMMENT.sub("", "\n".join(lines))
            words[r] = collections.Counter(_WORD.findall(live))
            for m in _FROM_IMPORT.finditer(live):
                if m.group(1) in stems and stems[m.group(1)] != r:
                    used_from[stems[m.group(1)]] |= set(_WORD.findall(m.group(2) or m.group(3)))
            for m in _DOTTED.finditer(live):
                if m.group(1) in stems and stems[m.group(1)] != r:
                    used_from[stems[m.group(1)]].add(m.group(2))
        found = set()
        for r, ds in defs.items():
            for name, _, _ in ds:
                if (r, name) in dead or name.startswith(("test", "__")) or name == "main":
                    continue
                if words[r][name] <= 1 and name not in used_from[r] and (name.startswith("_") or len(where[name]) > 1):
                    found.add((r, name))
        if not found:
            break
        dead |= found
    problems = []
    for r, ds in defs.items():
        for name, a, _ in ds:
            if (r, name) not in dead:
                continue
            twins = sorted(o for o in where[name] if o != r and (o, name) not in dead)
            problems.append(f"{r} line {a}: {name} is never used, so its code never runs"
                            + (f"; {', '.join(twins)} has the {name} that's used, so remove this copy completely"
                               if twins else ": remove it, or call it where it's needed"))
    return problems


def _is_main_guard(node: ast.AST) -> bool:
    """`if __name__ == "__main__":`"""
    t = getattr(node, "test", None)
    return isinstance(node, ast.If) and isinstance(t, ast.Compare) and isinstance(t.left, ast.Name) \
        and t.left.id == "__name__" and any(isinstance(c, ast.Constant) and c.value == "__main__"
                                             for c in t.comparators)


def chdir_problems(rel: str, text: str) -> list[str]:
    """A test file that moves into a folder (os.chdir) and never moves back to where it was: on Windows a folder in
    use can't be removed, so every test's temporary folder fails to clean up (Nov 18, task 60: setUp did
    os.chdir(self.cwd) into its TemporaryDirectory, and all 9 request tests errored in cleanup). Moving back means an
    os.chdir to a value saved from os.getcwd() / Path.cwd(), or addCleanup(os.chdir, that value)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    def is_chdir(f: ast.AST) -> bool:
        return isinstance(f, ast.Attribute) and f.attr == "chdir"

    def cwd_call(e: ast.AST) -> bool:
        return isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute) and e.func.attr in ("getcwd", "cwd")

    saved = {ast.unparse(t) for n in ast.walk(tree) if isinstance(n, ast.Assign) and cwd_call(n.value)
             for t in n.targets}
    moves, back = [], False
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        if is_chdir(n.func) and n.args:
            if cwd_call(n.args[0]) or ast.unparse(n.args[0]) in saved:
                back = True
            else:
                moves.append(n)
        elif isinstance(n.func, ast.Attribute) and n.func.attr == "addCleanup" and n.args and is_chdir(n.args[0]):
            back = True
    if back or not moves:
        return []
    return [f"{rel} line {moves[0].lineno}: it moves into a folder with os.chdir and never moves back, so on Windows "
            "the temporary folder can't be removed (save os.getcwd() first and add self.addCleanup(os.chdir, it), "
            "or pass full paths and don't change folder)"]


def test_main_problems(rel: str, text: str) -> list[str]:
    """unittest.main() anywhere but one top-level `if __name__ == "__main__":` after the tests: it runs before the
    test classes exist, so "Ran 0 tests" and a failed run (Nov 16, task 57: the block was indented inside the
    test class, with 3 more calls, and an 11-minute Sandbox run tested nothing)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []                                       # the parse check reports it
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "main" and isinstance(n.func.value, ast.Name) and n.func.value.id == "unittest"]
    if not calls:
        return []
    ok = {id(c) for node in tree.body if _is_main_guard(node) for s in node.body for c in ast.walk(s)}
    last_test = max((n.end_lineno or n.lineno for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef))
                     and (n.name.startswith("test") or isinstance(n, ast.ClassDef))), default=0)
    out = []
    for c in calls:
        if id(c) not in ok:
            where = next((f"inside {n.name}" for n in ast.walk(tree) if isinstance(n, (ast.ClassDef, ast.FunctionDef))
                          and n.lineno <= c.lineno <= (n.end_lineno or n.lineno)), "outside the main guard")
            out.append(f"{rel} line {c.lineno}: unittest.main() is {where}, so it runs before the tests exist "
                       "(\"Ran 0 tests\"). Keep exactly one, in a top-level `if __name__ == \"__main__\":` at the "
                       "end of the file, and remove the others")
        elif c.lineno < last_test:
            out.append(f"{rel} line {c.lineno}: unittest.main() comes before the last test (line {last_test}), so "
                       "the tests after it never run. Move the `if __name__ == \"__main__\":` block to the end")
    if len(calls) > 1 and not out:
        out.append(f"{rel} line {calls[1].lineno}: unittest.main() is called {len(calls)} times; keep exactly one")
    return out


# what a program may set on Python's own modules: where its output goes, its arguments, its import path
_STDLIB_SETTABLE = {("sys", "stdout"), ("sys", "stderr"), ("sys", "stdin"), ("sys", "argv"), ("sys", "path")}


def stdlib_patch_problems(rel: str, text: str) -> list[str]:
    """An assignment to an attribute of something from Python's standard library (`Path.__str__ = ...`,
    `os.sep = ...`, `setattr(Path, ...)`) changes it for the whole program (Nov 17, task 58: to fit a test,
    `Path.__str__` was rewired to call itself, and 26 tests crashed). Only where output goes, the arguments and the
    import path may be set (sys.stdout, sys.stderr, sys.stdin, sys.argv, sys.path)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    std = set(getattr(sys, "stdlib_module_names", ()))
    names: dict[str, str] = {}                         # local name -> the standard-library module it came from
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                if top in std:
                    names[a.asname or top] = top
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            top = node.module.split(".")[0]
            if top in std:
                for a in node.names:
                    names[a.asname or a.name] = top

    def base(n: ast.AST) -> str | None:
        while isinstance(n, ast.Attribute):
            n = n.value
        return n.id if isinstance(n, ast.Name) else None

    out = []
    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        elif isinstance(node, ast.Delete):
            targets = list(node.targets)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("setattr", "delattr") \
                and node.args:
            targets = [ast.Attribute(value=node.args[0], attr="?", lineno=node.lineno)]
        for tg in targets:
            if not isinstance(tg, ast.Attribute):
                continue
            b = base(tg)
            if b is None or b not in names:
                continue
            if isinstance(tg.value, ast.Name) and (names[b], tg.attr) in _STDLIB_SETTABLE:
                continue
            what = ast.unparse(tg) if tg.attr != "?" else f"{b} (setattr)"
            out.append(f"{rel} line {node.lineno}: it changes {what}, part of Python's own {names[b]}, for the whole "
                       "program. Never change Python's own modules or classes; write the code that needs a "
                       "different result so it makes that result itself (for a path with / in it: p.as_posix())")
    return out


# redefined (the later one wins), an unused import or variable: Python runs it all the same
# (a test that never moves back out of its folder still runs; only its cleanup fails, Nov 18, task 60)
_RUNS_ANYWAY = re.compile(r"^\S.* line \d+: (?:F(811|401|841) |it moves into a folder with os\.chdir)")
_DEAD_CODE = re.compile(r"^\S.* line \d+: F(401|841) ")       # unused import or variable

# -- the safety gate (the user's benchmarks, Nov 17): no network unless asked, no deleting outside the build
_NET_MODULES = {"urllib", "http", "socket", "requests", "httpx", "aiohttp", "ftplib", "smtplib", "poplib", "imaplib",
                "telnetlib", "xmlrpc", "websocket", "websockets", "ssl"}
_NET_ASKED = re.compile(r"(?i)\b(internet|online|download|upload|http|https|url|web\s?site|website|web page|api|"
                        r"server|network|fetch|e-?mail|smtp|socket)\b")


def net_ok_for(request: str) -> bool:
    """The request asks for network use (a download, a website, an API, email...)."""
    return bool(_NET_ASKED.search(request or ""))


def network_problems(rel: str, text: str) -> list[str]:
    """An import of a network module when the request never asks for network use."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        mods = [a.name for a in node.names] if isinstance(node, ast.Import) else \
            [node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level else []
        for mod in mods:
            if mod.split(".")[0] in _NET_MODULES:
                out.append(f"{rel} line {node.lineno}: it imports {mod}, for network use, which the request never "
                           "asks for. Take it out: the program works on this PC without the network")
    return out


_DOUBLED = re.compile(r"\\\\[bBwWdDsS]")


def doubled_escape_problems(rel: str, text: str) -> list[str]:
    """A raw string with a doubled backslash before a regex letter (r"\\\\b", r"\\\\w", r"\\\\d", r"\\\\s"): in a raw
    string that means a literal backslash, so the pattern never matches what it was written for. Python accepts it
    and ruff and pyright don't flag it (Nov 21, task 70: academy_mine.py's topic pattern had r"\\\\b..." throughout,
    so ALL-CAPS words like JSON never matched and the personal glossary dropped them, check (j), in four runs)."""
    import io
    import tokenize
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return []
    for tok in toks:
        if tok.type != tokenize.STRING:
            continue
        prefix = re.match(r"[A-Za-z]*", tok.string).group(0).lower()
        if "r" in prefix and (m := _DOUBLED.search(tok.string)):
            out.append(f"{rel} line {tok.start[0]}: the raw string has {m.group(0)}, a doubled backslash: in a raw "
                       f"string that's a literal backslash, so the pattern never matches. Write \\{m.group(0)[-1]} "
                       "(one backslash) in a raw string")
    return out


_DELETERS = {("shutil", "rmtree"), ("os", "remove"), ("os", "unlink"), ("os", "rmdir"), ("os", "removedirs")}
_OUTSIDE = re.compile(r"(?i)^([a-z]:[\\/]|[\\/]|~)")


def delete_problems(rel: str, text: str) -> list[str]:
    """A delete aimed outside the build: a fixed absolute path, the home folder, or a path from the environment.
    Deleting inside a temporary folder or the build's own folder (relative paths) is fine."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        is_del = (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and (f.value.id, f.attr) in _DELETERS) \
            or (isinstance(f, ast.Attribute) and f.attr in ("unlink", "rmdir", "rmtree"))
        if not is_del:
            continue
        target = node.args[0] if node.args else f.value if isinstance(f, ast.Attribute) else None
        if target is None:
            continue
        seen = ast.unparse(target)
        lits = [n.value for n in ast.walk(target) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        if any(_OUTSIDE.match(s) for s in lits) or re.search(r"\b(home|expanduser|environ|getenv|USERPROFILE|"
                                                               r"APPDATA|LOCALAPPDATA)\b", seen):
            out.append(f"{rel} line {node.lineno}: it deletes {seen[:80]}, outside the build's own folder. Only "
                       "delete inside the build's folder or a temporary folder")
    return out


def blocks_tests(problems: list[str]) -> list[str]:
    """The problems that keep the Sandbox tests from running: all but the ones Python runs with all the same."""
    return [x for x in problems if not _RUNS_ANYWAY.match(x)]


def drop_repeated_imports(text: str) -> tuple[str, int]:
    """An import inside a function that repeats one at the top of the file (the same module, the same names) is
    taken out, with no model: ruff calls it F811 and it blocked the tests (Nov 17, task 58: three fix rounds failed
    to remove `from academy_readme import ...` repeated inside a test). Returns (text, how many were taken out)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text, 0
    top: set[tuple] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            top |= {("from", node.module, node.level, a.name, a.asname) for a in node.names}
        elif isinstance(node, ast.Import):
            top |= {("import", a.name, a.asname) for a in node.names}

    def key(node: ast.AST) -> set[tuple] | None:
        if isinstance(node, ast.ImportFrom):
            return {("from", node.module, node.level, a.name, a.asname) for a in node.names}
        if isinstance(node, ast.Import):
            return {("import", a.name, a.asname) for a in node.names}
        return None

    lines = text.splitlines(keepends=True)
    cut: list[tuple[int, int]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        mine = []
        for st in fn.body:
            k = key(st)
            if not k or not k <= top:
                continue
            first, last = st.lineno - 1, (st.end_lineno or st.lineno) - 1
            before = lines[first][:st.col_offset].strip()
            after = lines[last][st.end_col_offset or 0:].strip()
            if before or (after and not after.startswith("#")):   # only a statement on lines of its own
                continue
            mine.append((first, last))
        if len(mine) < len(fn.body):                 # never leaves a function with no body
            cut += mine
    if not cut:
        return text, 0
    for first, last in sorted(cut, reverse=True):
        del lines[first:last + 1]
    return "".join(lines), len(cut)


def _py_module(rel: str) -> str:
    """academy/lesson.py -> academy.lesson; pkg/__init__.py -> pkg."""
    parts = list(PurePosixPath(rel.replace("\\", "/")).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _top_names(tree: ast.Module) -> set[str] | None:
    """The names a module defines at its top level (None: it has a `from x import *`, so can't tell)."""
    names: set[str] = set()

    def walk(body):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            names.add(n.id)
            elif isinstance(node, ast.Import):
                names.update((a.asname or a.name).split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if any(a.name == "*" for a in node.names):
                    raise LookupError
                names.update(a.asname or a.name for a in node.names)
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for field in ("body", "orelse", "finalbody"):
                    walk(getattr(node, field, []) or [])
                for h in getattr(node, "handlers", []) or []:
                    walk(h.body)
    try:
        walk(tree.body)
    except LookupError:
        return None
    return names


def _imported_by(folder: Path, test: str, rels: list[str]) -> list[str]:
    """The build's own Python files a test imports, the most used first (read with ast; nothing is run)."""
    try:
        src = (folder / test).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
    except (OSError, SyntaxError, ValueError):
        return []
    mods = {PurePosixPath(r.replace("\\", "/")).with_suffix("").as_posix().replace("/", "."): r
            for r in rels if r.lower().endswith(".py") and r != test}
    found = []
    for node in ast.walk(tree):
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        for n in names:
            for mod, rel in mods.items():
                if (n == mod or n.endswith("." + mod) or mod.endswith("." + n)) and rel not in found:
                    found.append(rel)
    return sorted(found, key=lambda r: -src.count(PurePosixPath(r.replace("\\", "/")).stem))


def _called_files(folder: Path, test: str, failing: list[str], rels: list[str]) -> list[str]:
    """The build's own files whose functions the failing tests call, in the order they're first called (read with
    ast; nothing is run). `from academy_mine import clear` + `clear(p)` in a failing test -> academy_mine.py.
    Nov 17, task 58: the traceback named only test_request.py, the fix went to the first 4 files it imports, and
    academy_mine.py (5th), where both bugs were, was never sent a fix in 10 runs."""
    try:
        tree = ast.parse((folder / test).read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return []
    mods = {PurePosixPath(r.replace("\\", "/")).with_suffix("").as_posix().replace("/", "."): r
            for r in rels if r.lower().endswith(".py") and r != test and not _is_test(r)}

    def rel_of(module: str) -> str:
        return next((r for m, r in mods.items() if module == m or module.endswith("." + m)), "")

    names: dict[str, str] = {}                     # a name in the test -> the build file it comes from
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and rel_of(node.module):
            for a in node.names:
                names[a.asname or a.name] = rel_of(node.module)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if rel_of(a.name):
                    names[a.asname or a.name.split(".")[0]] = rel_of(a.name)
    found: list[str] = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name in failing]:
        calls = sorted((c for c in ast.walk(fn) if isinstance(c, ast.Call)), key=lambda c: (c.lineno, c.col_offset))
        for c in calls:
            f = c.func
            base = f.id if isinstance(f, ast.Name) else \
                f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
            rel = names.get(base, "")
            if rel and rel not in found:
                found.append(rel)
    return found


def _none_ran(out: str) -> bool:
    return bool(re.search(r"\bRan 0 tests?\b|NO TESTS RAN", out))


def fix_files(folder: Path, test: str, out: str, code: list[str], same: set[str]) -> list[str]:
    """The files a failed test run's fix goes to, the test file first if it's one of them: the files the traceback
    names, then the ones whose functions the failing tests call, else the files the test imports. No test ran at
    all: the test file only (Nov 16, task 57: unittest.main() inside the test class; the fix went to
    academy_confusion.py and began rewriting it whole). `smithy.py replay` uses this too."""
    if _none_ran(out):
        return [test]
    traced = _failing_files(out, test, code, same)
    called = [r for r in _called_files(folder, test, re.findall(r"(?m)^(?:FAIL|ERROR): (\w+)", out), code)
              if r not in same and r not in traced]
    named = ([r for r in traced if r != test] + called + [r for r in traced if r == test]) or \
        [r for r in _imported_by(folder, test, code) if r not in same] or [test]
    return ([test] if test in named else []) + [r for r in named if r != test]


def fix_files_judged(folder: Path, test: str, named: list[str], code: list[str], same: set[str],
                     wrong_here: bool, code_wrong: list[str] | tuple = (), others: bool = False) -> list[str]:
    """`fix_files` after the judging: a test judged wrong puts the test file first; the tests written from the
    request otherwise send the fix to code files only (never a test file).
    `code_wrong`: the failing tests judged "the code is wrong" (or "both"): their fix goes first to the code files
    they call, even one the plan keeps the same, and never to their test file alone; the test file stays on the list
    only when it's being corrected too (`wrong_here`) or another failing test in it isn't judged so (`others`).
    Nov 18, task 60: test_personal_glossary_sentence_not_kept was judged "code", its fix went only to the locked test
    file (academy_mine.py, which it calls, was "kept the same"), both edits were refused and the run ended."""
    calls = []
    if code_wrong:
        calls = [r for r in _called_files(folder, test, list(code_wrong), code) if not _is_test(r)] or \
            [r for r in _imported_by(folder, test, code) if not _is_test(r)][:4]
    if wrong_here:
        return [test] + calls + [r for r in named if r != test and r not in calls]
    if code_wrong:
        rest = [r for r in named if r not in calls
                and (not _is_test(r) or (others and r == test and test != REQUEST_TESTS))]
        return calls + rest if calls or rest else named
    if test == REQUEST_TESTS:
        return [r for r in named if not _is_test(r)] or \
            [r for r in _imported_by(folder, test, code) if r not in same and not _is_test(r)][:4]
    return named


def py_import_problems(folder: Path, rels: list[str], stdlib_only: bool = False) -> list[str] | None:
    """Imports between a build's Python files, read with `ast` (nothing is run; review step 2, Nov 14).
    - `from X import a` where X is a file in the build: X must define a.
    - `from .X import a` when the files aren't a package (no __init__.py): it fails when run as a script.
    - with stdlib_only: any import that's neither in the build nor in Python's standard library.
    Imports inside a try with an except are optional, so only their names are checked."""
    import sys
    trees, mods = {}, {}
    for r in rels:
        try:
            trees[r] = ast.parse((folder / r).read_text(encoding="utf-8-sig", errors="replace"), r)
        except SyntaxError:
            continue                                     # already reported by the parse check
        mods[_py_module(r)] = r
    defined = {m: _top_names(trees[r]) for m, r in mods.items()}
    stdlib = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
    problems: list[str] = []

    for r, tree in trees.items():
        # an import of one of the build's own files, commented out: a fix round's way round a missing name
        # (task 49: "# from academy_lesson import build_lesson"), which hides the problem instead of fixing it
        for no, line in enumerate((folder / r).read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
            m = re.match(r"\s*#\s*(?:from\s+([\w.]+)\s+import\s+\w|import\s+([\w.]+)\s*(?:#|$))", line)
            if m and (m.group(1) or m.group(2) or "").lstrip(".") in mods:
                problems.append(f"{r} line {no}: an import of {(m.group(1) or m.group(2)).lstrip('.')} is commented "
                                "out. Make what it imports exist and work, then put the import back")
        here = _py_module(r).split(".")
        is_pkg = r.replace("\\", "/").endswith("__init__.py")
        pkg = here if is_pkg else here[:-1]                # the package this file is in
        has_init = any(x.replace("\\", "/") == "/".join(pkg + ["__init__.py"]) for x in rels) if pkg else False
        optional: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Try) and node.handlers:
                for inner in node.body:
                    optional.update(id(n) for n in ast.walk(inner))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    if not has_init and id(node) not in optional:
                        problems.append(f"{r} line {node.lineno}: `from {'.' * node.level}{node.module or ''} import "
                                        f"...` is a package import, but these files aren't a package (no "
                                        "__init__.py), so it fails when the files are run or imported as they are. "
                                        f"Use `from {node.module or ''} import ...`")
                        continue
                    base = pkg[:len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                    target = ".".join(base + ([node.module] if node.module else []))
                else:
                    target = node.module or ""
                if target in mods:
                    names = defined.get(target)
                    if names is None:
                        continue
                    for a in node.names:
                        if a.name != "*" and a.name not in names and f"{target}.{a.name}" not in mods:
                            problems.append(f"{r} line {node.lineno}: imports {a.name} from {mods[target]}, "
                                            f"which doesn't define {a.name}")
                elif not node.level and stdlib_only and id(node) not in optional:
                    top = target.split(".")[0]
                    if top and top not in stdlib and not any(m.split(".")[0] == top for m in mods):
                        problems.append(f"{r} line {node.lineno}: imports {top}, which isn't in Python's standard "
                                        "library (the request asks for the standard library only)")
            elif isinstance(node, ast.Import) and id(node) not in optional and any(
                    (a.name in mods and not a.asname) for a in node.names):
                # `import academy_lesson` then `academy_lesson.build_lesson(...)` (task 49: the module was empty
                # and the check didn't see it): each name used must be defined there
                for a in node.names:
                    if a.name not in mods or a.asname or defined.get(a.name) is None:
                        continue
                    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                            and isinstance(n.value, ast.Name) and n.value.id == a.name}
                    for name in sorted(used - defined[a.name]):
                        problems.append(f"{r} line {node.lineno}: uses {a.name}.{name}, but {mods[a.name]} doesn't "
                                        f"define {name}")
            if isinstance(node, ast.Import) and stdlib_only and id(node) not in optional:
                for a in node.names:
                    top = a.name.split(".")[0]
                    if top not in stdlib and not any(m.split(".")[0] == top for m in mods):
                        problems.append(f"{r} line {node.lineno}: imports {top}, which isn't in Python's standard "
                                        "library (the request asks for the standard library only)")
    return problems


_STDLIB_ONLY = re.compile(r"(?i)\bstandard[ -]library\b[^.\n]{0,20}\bonly\b|\bonly\b[^.\n]{0,30}\bstandard[ -]library\b"
                          r"|\bno (?:third[- ]party|external) (?:packages|libraries|modules)\b")
_WANTS_TESTS = re.compile(r"(?i)\b(?:tests?|test_\w+\.py|\w+_test\.py)\b[^.\n]{0,40}\bpass(?:es|ing)?\b")
_ITEM = re.compile(r"(?m)^\s*(\d{1,2})[.)]\s+(\S.*)$")
# "## 1. README: ..." (Nov 18, task 60: the Academy numbered its 4 changes as Markdown headings, none was read as
# a change, so none was checked off, and the README changes that weren't made were never reported missing)
_HEAD_ITEM = re.compile(r"(?m)^[ \t]*(#{1,6})[ \t]*(\d{1,2})[.)][ \t]+(\S.*)$")
_HEADING = re.compile(r"(?m)^[ \t]*(#{1,6})[ \t]+\S")
_README_ITEM = re.compile(r"(?i)^\s*(?:#{1,6}\s*)?\d{1,2}[.)]\s+[`*]*README(?:\.md)?\b")


def numbered_items(change: str) -> dict[int, str]:
    """The numbered changes in a request ("1. ...", "2) ..."), each with its following lines. A request that numbers
    its changes as Markdown headings ("## 1. ...") is read by those headings alone (numbered lines inside one stay
    part of it); each ends at the next heading of the same level or higher."""
    change = change or ""
    heads = list(_HEAD_ITEM.finditer(change))
    if heads:
        items = {}
        for m in heads:
            level = len(m.group(1))
            end = next((h.start() for h in _HEADING.finditer(change, m.end()) if len(h.group(1)) <= level),
                       len(change))
            items.setdefault(int(m.group(2)), change[m.start():end].strip())
        return items
    items, marks = {}, list(_ITEM.finditer(change))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(change)
        items[int(m.group(1))] = change[m.start():end].strip()
    return items


_SUB = re.compile(r"\(([a-z])\)\s*(.+?)(?=;?\s*\([a-z]\)\s|\s*$)", re.S)
_CHECKS_LINE = re.compile(r"(?mi)^\s*[-*]?\s*Checks?:\s*(.+(?:\n(?!\s*[-*]\s|\s*\d{1,2}[.)]\s).+)*)")
# "2f: DONE - test_a.py: `assert x`", "2 (f): DONE ...", "**2.f** — DONE ..."
_PROOF = re.compile(r"(?i)^[\s*|>\-]*(?:item\s*)?(\d{1,2})\s*[.\s]?\(?([a-z])?\)?[\s*]*[:.)\-–—|]+[\s*]*"
                    r"(DONE|MISSING)\b\**[\s:\-–—|]*(.*)$")
_PROOF_FILE = re.compile(r"[\w./\\-]+\.(?:py|json|ps1|psm1|cs|ts|js|java|xml|txt|ini|toml|cfg)\b")
_SPLIT_CHECKLIST = 12_000      # tokens: a bigger checklist is asked one item at a time, so its files stay whole


def sub_points(item: str) -> dict[str, str]:
    """The lettered checks in an item's "Checks:" line: {"a": "...", "b": "..."} (task 56: "Checks: (a) ...; (b)
    ...", seven in item 2; one tick for the whole item let (f) and (g) pass unwritten)."""
    m = _CHECKS_LINE.search(item or "")
    if not m:
        return _letter_lines(item or "")
    return {k: re.sub(r"\s+", " ", v).strip(" ;.") for k, v in _SUB.findall(" ".join(m.group(1).split()))}


_LETTER_START = re.compile(r"^[ \t]*(?:[-*][ \t]*)?\(([a-z])\)[ \t]*(\S.*)$")


def _split_letters(item: str) -> list[str]:
    """The item's lines, a line holding several lettered checks split before each next letter that follows a "."
    or ";" (Nov 19, task 65: "(d) More than 8 candidates: exactly the first 8 are listed. (e) The same candidate
    twice appears once." on one line: (e) ran into (d), and (f) and (g) were never read as checks)."""
    lines: list[str] = []
    for line in item.splitlines():
        m = _LETTER_START.match(line)
        while m:
            nxt = chr(ord(m.group(1)) + 1)
            cut = re.search(r"(?<=[.;])[ \t]+(?=\(" + nxt + r"\)[ \t]*\S)", line)
            if not cut:
                break
            lines.append(line[:cut.start()])
            line = line[cut.end():]
            m = _LETTER_START.match(line)
        lines.append(line)
    return lines


def _letter_lines(item: str) -> dict[str, str]:
    """Lettered checks that each start their own line, "(a) ...", "(b) ...", in an item with no "Checks:" line;
    from (a) on, in order, at least two (Nov 18, task 60: change 3 listed (a) to (g) that way, the checker answered
    3a to 3g, and as no check was known the whole change read as "not answered"). A line that runs on (not ended by
    ";" or ".") takes the next lines until a blank line, a heading or a list mark."""
    out: dict[str, str] = {}
    key, open_ = "", False
    for line in _split_letters(item):
        m = _LETTER_START.match(line)
        if m and m.group(1) == chr(ord("a") + len(out)):
            key, open_ = m.group(1), True
            out[key] = m.group(2)
        elif key and open_ and line.strip() and not re.match(r"[ \t]*(?:#|[-*][ \t]|\d{1,2}[.)]\s|\([a-z]\))",
                                                             line):
            out[key] += " " + line.strip()
        else:
            open_ = False
            continue
        open_ = not out[key].rstrip().endswith((";", "."))
    if len(out) < 2:
        return {}
    return {k: re.sub(r"\s+", " ", v).strip(" ;.") for k, v in out.items()}


def _is_test(rel: str) -> bool:
    name = PurePosixPath(rel).name.lower()
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def quote_shown(written: dict[str, str], where: str, quote: str, test: bool = False) -> str:
    """Where a checker's quote really is: the file it's in, or '' (Nov 16, the user: "cite it or it's missing").
    The quote must be at least 12 characters, found exactly (spaces aside) in a build file that isn't Markdown, not
    only in comments; for a lettered check, inside a test_ function of a test file. The named file first, then
    any (a model names the wrong file more often than it invents a line)."""
    q = _norm(quote)
    if len(q) < 12:
        return ""
    files = [r for r in written if not r.lower().endswith(".md")]
    named = [r for r in files if where and (r == where or PurePosixPath(r).name == PurePosixPath(where).name)]
    for rel in named + [r for r in files if r not in named]:
        if test and not _is_test(rel):
            continue
        body = written[rel]
        if q not in _norm(body):
            continue
        lines = body.splitlines()
        first = _norm(quote.strip().splitlines()[0]) if quote.strip() else q
        at = [i for i, l in enumerate(lines) if first[:60] in _norm(l) or _norm(l) and _norm(l) in first]
        if at and all(lines[i].lstrip().startswith("#") for i in at):
            continue                                   # only in a comment
        if test:
            try:
                tree = ast.parse(body)
                spans = [(n.lineno, n.end_lineno or n.lineno) for n in ast.walk(tree)
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")]
                inside_test = any(a <= i + 1 <= b for i in at for a, b in spans)
            except SyntaxError:
                # a test file that doesn't parse is a build problem of its own; its tests are still found by
                # indentation (Nov 16, task 56: one broken line made all 18 checks read as missing)
                inside_test = any(_in_test_by_indent(lines, i) for i in at)
            if not inside_test:
                continue
        return rel
    return ""


def _breaks(rel: str, old: str, new: str) -> str:
    """Why `new` no longer parses when `old` did ('' when it's fine, or old was broken too): Python and JSON."""
    low = rel.lower()
    if new == old or not (low.endswith(".py") or low.endswith(".json")):
        return ""

    def err(text: str) -> str:
        try:
            if low.endswith(".py"):
                ast.parse(text)
            else:
                json.loads(text)
            return ""
        except SyntaxError as e:
            return f"line {e.lineno}: {e.msg}"
        except ValueError as e:
            return str(e)[:120]
    return "" if err(old) else err(new)


def _in_test_by_indent(lines: list[str], i: int) -> bool:
    """Line i sits inside a `def test...` (by indentation): for a test file that doesn't parse."""
    ind = len(lines[i]) - len(lines[i].lstrip())
    for j in range(i - 1, -1, -1):
        s = lines[j].strip()
        if not s:
            continue
        lead = len(lines[j]) - len(lines[j].lstrip())
        if lead < ind:
            if re.match(r"(?:async\s+)?def\s+test", s):
                return True
            if s.startswith(("def ", "async def ", "class ")) or lead == 0:
                return False
            ind = lead                                     # an if/with/for inside the function: keep going up
    return False


# the checklist's and the judge's answers as answer shapes (worker_shared.answer_shape): where the model is held to
# them, every answer has one entry per check and a verdict from the three, so there are no "no ticks in the reply;
# asking again" rounds. Where it isn't, the text lines are read as before (Nov 19, colibri idea: fixed-form answers)
CHECKLIST_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["checks"],
                    "properties": {"checks": {"type": "array", "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["key", "state", "file", "quote", "note"],
                        "properties": {"key": {"type": "string"}, "state": {"type": "string", "enum": ["DONE", "MISSING"]},
                                       "file": {"type": "string"}, "quote": {"type": "string"},
                                       "note": {"type": "string"}}}}}}
JUDGE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["verdict", "because"],
                "properties": {"verdict": {"type": "string", "enum": ["TEST", "CODE", "BOTH"]},
                               "because": {"type": "string"}}}
CHECKLIST_JSON_HINT = ('If your answer has to be JSON, send {"checks": [{"key": "2f", "state": "DONE" or "MISSING", '
                       '"file": ..., "quote": the exact line (empty when MISSING), "note": ...}]} with one entry for '
                       'each line asked for.')
JUDGE_JSON_HINT = 'If your answer has to be JSON, send {"verdict": "TEST" or "CODE" or "BOTH", "because": ...}.'


def _json_reply(reply: str) -> dict | None:
    """The JSON object a reply holds (bare or inside a code fence), or None."""
    t = reply or ""
    i = t.find("{")
    if i < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(t[i:])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


_PROOF_KEY = re.compile(r"(?i)^\W*(?:item\s*)?(\d{1,2})\s*[.\s]?\(?([a-z])?\)?\W*$")


def _json_proofs(reply: str) -> dict[str, tuple[str, str, str, str]]:
    """The checklist read from {"checks": [...]}: the same tuples _parse_proofs gives for the same lines."""
    data = _json_reply(reply)
    got: dict[str, tuple[str, str, str, str]] = {}
    for c in (data or {}).get("checks") or []:
        if not isinstance(c, dict):
            continue
        m = _PROOF_KEY.match(str(c.get("key", "")))
        state = str(c.get("state", "")).upper()
        if not m or state not in ("DONE", "MISSING"):
            continue
        quote = str(c.get("quote") or "")
        f = _PROOF_FILE.search(str(c.get("file") or ""))
        note = re.sub(r"[`*]+", "", str(c.get("note") or quote))[:200]
        got.setdefault(m.group(1) + (m.group(2) or "").lower(),
                       (state, f.group(0).replace("\\", "/") if f else "", quote, note))
    return got


def _parse_proofs(reply: str) -> dict[str, tuple[str, str, str, str]]:
    """{"2" or "2f": (DONE|MISSING, file, quote, note)} from a checklist reply: its JSON when it has the answer
    shape, else its lines."""
    got = _json_proofs(reply)
    if got:
        return got
    for line in (reply or "").splitlines():
        m = _PROOF.match(line)
        if not m:
            continue
        rest = m.group(4).strip()
        quote = ""
        tick = re.search(r"`([^`]+)`", rest)
        if tick:
            quote = tick.group(1)
        elif rest.count('"') >= 2:
            quote = rest[rest.index('"') + 1:rest.rindex('"')]
        f = _PROOF_FILE.search(rest.replace(quote, "") if quote else rest)
        key = m.group(1) + (m.group(2) or "").lower()
        got.setdefault(key, (m.group(3).upper(), f.group(0).replace("\\", "/") if f else "", quote,
                             re.sub(r"[`*]+", "", rest)[:200]))
    return got


# an item that names no file: its changed files read whole only up to about this many tokens for the whole step,
# the rest as outlines (Nov 19, task 67: 8 changed files and both test files whole made a 36,752-token step that
# couldn't be made smaller, more than any provider's minute; Cerebras takes 30,000)
_CHECK_WHOLE = 20_000


def whole_within(paths: list[str], written: dict[str, str], text: str, tokens: float) -> set[str]:
    """Which of `paths` to read whole within about `tokens`: those sharing the most of `text`'s words first, a word
    counting more the fewer of the files hold it (one only in the confusion detector says more than one in all);
    at least one, so the item is never checked against outlines only."""
    words = {w.lower() for w in _WORD.findall(text) if len(w) > 3}
    has = {rel: words & {w.lower() for w in _WORD.findall(written.get(rel, ""))} for rel in paths}
    held = collections.Counter(w for ws in has.values() for w in ws)

    def shared(rel: str) -> float:
        return sum(1 / held[w] for w in has[rel])

    out: set[str] = set()
    for rel in sorted(paths, key=lambda r: (-shared(r), len(written.get(r, "")))):
        size = len(written.get(rel, "")) / 3.5
        if out and size > tokens:
            continue
        out.add(rel)
        tokens -= size
    return out


def _checklist(ctx, p: dict, desc: str, plan: dict, written: dict[str, str], items: dict[int, str],
               cap: int, log_path: Path | None = None, keep_path: Path | None = None) -> dict[int, tuple[str, str]]:
    """{item number: ("DONE" | "MISSING" | "NOT SAID", note)} for the numbered changes, from the files as they are.
    Cite it or it's missing (Nov 16): each item and each of its lettered checks is its own line, and a DONE must
    quote the line that does it; the quote is looked for in the files (quote_shown), and a DONE whose quote isn't
    there counts as MISSING. An item is DONE only when it and all its checks are. A big checklist is asked one
    item at a time, naming that item's files and the test files, so the step's shrink keeps them whole (task 56:
    the test file was never named, so the checker saw only its outline and ticked checks that weren't written).
    `keep_path`: an item that was DONE keeps its quotes there; next time, if the item reads the same and every quote
    is still in the files (looked up, no model), it isn't asked again (Nov 16: asking each of task 56's 4 items
    every round cost about 80,000 tokens a round)."""
    code = stable_start([r for r in written if not r.lower().endswith(".md")], plan)
    files = "\n\n".join(f"--- {path} ---\n{written[path][:cap]}" + ("\n(... cut here)" if len(written[path]) > cap
                                                                    else "") for path in code)
    tests = [r for r in code if _is_test(r)]
    subs = {n: sub_points(t) for n, t in items.items()}
    # the item's rules that no lettered check covers ("only if the README names one"), each its own line, lettered
    # from z down, and shown like a lettered check: by a line inside a test (Nov 16, task 56: an invented "Python
    # 3.12" and a missing "not correct" line passed, as no check named them; then, cited in the code, the model
    # quoted the very line that invents "C# 5", which only proved the line exists)
    rules = {n: dict(zip([c for c in "zyxwvutsrq" if c not in subs[n]], smithy_diagnose.rules_of(t)))
             for n, t in items.items()}

    def keys(n: int) -> list[str]:
        return [str(n)] + [f"{n}{x}" for x in subs[n]] + [f"{n}{x}" for x in rules[n]]

    def in_test(key: str) -> bool:                      # lettered checks and rules are shown in a test
        return key[-1:].isalpha()

    def ask(part: dict[int, str]) -> tuple[dict[str, tuple[str, str, str, str]], str]:
        lines = []
        for n in part:
            lines.append(f"{n}: DONE - file: `the exact line from that file that does it`   (or {n}: MISSING - "
                         "the file and what's missing)")
            lines += [f"{n}{k}: DONE - test file: `the exact line, inside a test_ function, that checks ({k})`"
                      for k in subs[n]]
            lines += [f"{n}{k}: DONE - test file: `the exact line, inside a test_ function, that checks this rule: "
                      f"{r}`   (or {n}{k}: MISSING - no test checks it)" for k, r in rules[n].items()]
        named = {r for n in part for r in _names(part[n], [{"path": x} for x in code], stems=True)}
        test_read = set(tests) if any(subs[n] for n in part) else set()
        if not named:          # an item that names no file is looked for in the files this version changes, read whole
            # (Nov 19, task 64: "Rule: what counts as a topic" named none, academy_readme.py reached the checker as an
            # outline, and it reported "the file contains only an outline", so every round chased a miss that wasn't)
            changed = [f["path"] for f in plan.get("files", []) if f.get("change") not in ("same", None)
                       and f["path"] in code and not _is_test(f["path"])]
            named = whole_within(changed, written, "\n".join(part.values()),
                                 _CHECK_WHOLE - sum(len(written[t]) for t in test_read) // 3.5
                                 - len(p["request"]) // 3.5)
        read = sorted(named | test_read)
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"The project: {redact(p['request'])}\n\nIts files now:\n\n{files}"},
                {"role": "user", "content":
                 "These changes were asked for:\n\n" + redact("\n\n".join(part.values())) +
                 "\n\nCheck the files above, not the request" + (f" (read {', '.join(read)} in full)" if read else "")
                 + ". Reply with one line for each of these, and no other text:\n" + "\n".join(lines) +
                 "\n\nA DONE must quote one exact line copied from the file, in backticks; the quote is looked up "
                 "in the file, and a DONE whose quote isn't there counts as missing. A lettered check is DONE only "
                 "when a test really checks it. If you can't quote it, it's MISSING. " + CHECKLIST_JSON_HINT}]
        # room for models that think first (Nov 14, task 38: 1,500 left no ticks for 8 items, so all 8 read as
        # missing); one more ask when none can be read
        with worker_shared.answer_shape(CHECKLIST_SCHEMA):
            reply = ctx.chat(msgs, 4000)
        got = _parse_proofs(reply)
        if not got:
            log.info("checklist: no ticks in the reply (%d characters); asking again", len(reply))
            more = ctx.chat(msgs + [{"role": "assistant", "content": reply},
                                    {"role": "user", "content": "I couldn't read that. Reply with only the lines "
                                     "asked for, like \"1: DONE - a.py: `the line`\" or \"2: MISSING - file: what's "
                                     "missing\"." + (f" (Read {', '.join(read)} in full.)" if read else "")}],
                            8000 if not reply.strip() else 4000)   # an empty reply: it thought too long (task 42)
            got = _parse_proofs(more)
            reply += "\n\n--- asked again ---\n\n" + more
        return got, reply

    try:
        kept = json.loads(keep_path.read_text(encoding="utf-8")).get("done", {}) if keep_path else {}
    except (OSError, ValueError, AttributeError):
        kept = {}

    def same(n: int) -> dict:
        """The quotes an earlier check gave for item n, when the item reads the same and all are still shown."""
        k = kept.get(str(n)) or {}
        pr = k.get("proofs") or {}
        if k.get("text") != hashlib.sha256(items[n].encode("utf-8")).hexdigest() \
                or set(pr) != set(keys(n)):
            return {}
        return pr if all(quote_shown(written, w, q, in_test(key)) for key, (w, q) in pr.items()) else {}

    reused = {n: pr for n, pr in ((n, same(n)) for n in items) if pr}
    asking = {n: t for n, t in items.items() if n not in reused}
    whole = [{"role": "user", "content": files + "\n".join(asking.values())}]
    parts = ([{n: t} for n, t in asking.items()] if len(asking) > 1 and worker_cloud.prompt_tokens(whole)
             > _SPLIT_CHECKLIST else [asking] if asking else [])
    got, replies = {}, []
    for n, pr in reused.items():
        got.update({key: ("DONE", w, q, "") for key, (w, q) in pr.items()})
        replies.append(f"{n}: DONE (not asked again: the same item, and its {len(pr)} quoted line"
                       f"{'s are' if len(pr) != 1 else ' is'} still in the files)")
    for part in parts:
        g, r = ask(part)
        got.update({k: v for k, v in g.items() if k.rstrip("abcdefghijklmnopqrstuvwxyz").isdigit()
                    and int(k.rstrip("abcdefghijklmnopqrstuvwxyz")) in part})
        replies.append(r)

    def proof(key: str, test: bool) -> tuple[str, str]:
        """("DONE", "") when said and shown; ("MISSING", why) else; ("NOT SAID", "") when not answered."""
        if key not in got:
            return "NOT SAID", ""
        state, where, quote, note = got[key]
        if state != "DONE":
            return "MISSING", note
        if not quote:
            return "MISSING", "said done, but quoted no line"
        if quote_shown(written, where, quote, test):
            return "DONE", ""
        return "MISSING", (f"said done, but its quote isn't in {where or 'the files'}"
                           + (" inside a test" if test else "") + f": `{quote[:80]}`")

    out, shown = {}, []
    for n in items:
        state, note = proof(str(n), False)
        if state == "NOT SAID" and not any(f"{n}{k}" in got for k in subs[n]):
            out[n] = ("NOT SAID", "")
            continue
        why = [note] if state != "DONE" and note else ["the change itself: not answered"] if state == "NOT SAID" \
            else []
        lacking = []
        for k, text in subs[n].items():
            s, w = proof(f"{n}{k}", True)
            if s != "DONE":
                lacking.append(f"({k}) {text[:90]}" + (f" [{w}]" if w else ""))
        if lacking:
            where = f" in {', '.join(tests)}" if tests else ""
            why.append(f"{len(lacking)} of {len(subs[n])} checks not shown in a test{where}: " + "; ".join(lacking))
        broken = []
        for k, r in rules[n].items():
            s, w = proof(f"{n}{k}", True)
            if s != "DONE":
                broken.append(f"({k}) {r[:120]}" + (f" [{w}]" if w else ""))
        if broken:
            where = f" in {', '.join(tests)}" if tests else ""
            why.append(f"{len(broken)} of {len(rules[n])} rules in the request not checked by a test{where} (add a "
                       "test that checks each, as the request says): " + "; ".join(broken))
            lacking += broken
        out[n] = ("DONE", "") if state == "DONE" and not lacking else ("MISSING", "; ".join(why)[:1400])
        shown.append(f"{n}: {out[n][0]}" + (f" - {out[n][1]}" if out[n][1] else ""))
    if keep_path is not None:                           # what's DONE, with its quotes, for the next check
        done = {str(n): {"text": hashlib.sha256(items[n].encode("utf-8")).hexdigest(),
                         "proofs": {key: [got[key][1], got[key][2]] for key in keys(n)}}
                for n in items if out[n][0] == "DONE"}
        try:
            keep_path.write_text(json.dumps({"done": done}, indent=1), encoding="utf-8")
        except OSError:
            pass
    if log_path is not None:                            # kept for review: what the checklist said, each round
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"=== checklist ({len(got)} lines read, {len(parts)} ask{'s' if len(parts) != 1 else ''}) "
                     "===\n" + "\n\n--- next item ---\n\n".join(replies)
                     + "\n\n--- after looking up the quotes ---\n" + "\n".join(shown) + "\n\n")
    return out




def _item_files(items: dict[int, str], written: dict[str, str]) -> dict[int, list[str]]:
    """The files each item names; an item with lettered checks is shown in the test files, so they count too
    (Nov 16, task 56: test_academy.py was fixed, but items 3 and 4 only named their modules, so "missing" stuck)."""
    code = [{"path": r} for r in written if not r.lower().endswith(".md")]
    tests = {r for r in written if _is_test(r)}
    return {n: sorted(set(_names(t, code, stems=True)) | (tests if sub_points(t) else set())) for n, t in items.items()}


def _no_change_no_done(ticks: dict[int, tuple[str, str]], written: dict[str, str], path: Path | None,
                       files_of: dict[int, list[str]] | None = None) -> dict[int, tuple[str, str]]:
    """A check that says DONE for an item an earlier check found MISSING, with none of that item's files changed
    since, is wrong (Nov 15, task 56: this PC's 3B model ticked all 4 DONE; the glossary still had no ids), so the
    item stays MISSING. `files_of`: the files each item names (default: all; README.md never counts). What's
    MISSING, with a fingerprint of its files, is kept in `path` across continue runs."""
    if path is None:
        return ticks
    every = [k for k in written if not k.lower().endswith(".md")]

    def mark(n: int) -> str:
        names = sorted(r for r in ((files_of or {}).get(n) or every) if r in written) or sorted(every)
        return hashlib.sha256("\0".join(f"{k}\0{written[k]}" for k in names).encode("utf-8")).hexdigest()
    try:
        last = json.loads(path.read_text(encoding="utf-8")).get("items", {})
    except (OSError, ValueError, AttributeError):
        last = {}
    same = {k: v for k, v in last.items() if k.isdigit() and v.get("files") == mark(int(k))}
    ticks = {n: ("MISSING", same[str(n)]["note"] + " (a later check said done, but none of its files changed since)")
             if s == "DONE" and str(n) in same else (s, note) for n, (s, note) in ticks.items()}
    keep = dict(same)                                  # still unchanged: what was missing stays known
    keep.update({str(n): {"note": note, "files": mark(n)} for n, (s, note) in ticks.items() if s == "MISSING"})
    try:
        path.write_text(json.dumps({"items": keep}, indent=1), encoding="utf-8")
    except OSError:
        pass
    return ticks


# -- the request's README changes, checked once the README is written (Nov 18, task 60: two README edits were refused
# for a too-short FIND, and the report said nothing was missing: README changes were never checked at all)
_SAYS_PHRASE = re.compile(r"(?i)\b(?:contains?|names?|has|says|includes?|shows?)\b(?:\s+the\s+heading)?\s+"
                          r"((?:\"[^\"\n]{2,80}\"(?:\s*(?:,|\band\b|\bor\b)\s*)?)+)")
_NEGATED = re.compile(r"(?i)\b(?:no|not|never|none|nothing|unchanged|without)\b")


def readme_items(p: dict) -> dict[int, str]:
    """The request's numbered changes that are for its README."""
    items = numbered_items(p.get("change", "")) if p.get("parent_id") else numbered_items(p.get("request", ""))
    return {n: t for n, t in items.items() if _README_ITEM.match(t)}


def readme_phrases_missing(item: str, readme: str) -> list[str]:
    """The quoted words a README change's own checks say it contains ("contains the heading \"X\"", "names \"A\" and
    \"B\"") that the README doesn't; looked up, no model. A check that says what's absent is left to the checker."""
    low, gone = readme.lower(), []
    for text in sub_points(item).values():
        if _NEGATED.search(text):
            continue
        for m in _SAYS_PHRASE.finditer(text):
            gone += [x for x in re.findall(r'"([^"\n]{2,80})"', m.group(1)) if x.lower() not in low and x not in gone]
    return gone


def readme_check(ctx, items: dict[int, str], readme: str) -> dict[int, tuple[str, str]]:
    """{item: ("DONE" | "MISSING", why)} for the README changes, against the README as written. A DONE must quote a
    line of README.md that's really there, and every phrase its checks name must be in it."""
    lines = [f"{n}: DONE - README.md: `the exact line from README.md that makes this change`   (or {n}: MISSING - "
             "what's missing)" for n in items]
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"README.md as written:\n```\n{readme}\n```"},
            {"role": "user", "content": "These README changes were asked for:\n\n" + redact("\n\n".join(items.values()))
             + "\n\nCheck README.md above, not the request. Reply with one line for each, and no other text:\n"
             + "\n".join(lines) + "\n\nA DONE must quote one exact line copied from README.md, in backticks; the quote "
             "is looked up, and a DONE whose quote isn't there counts as missing. If you can't quote it, it's MISSING."}]
    got = _parse_proofs(ctx.chat(msgs, 4000))
    out = {}
    for n, t in items.items():
        why = []
        state, _, quote, note = got.get(str(n), ("", "", "", ""))
        if not state:
            why.append("the check gave no answer for it")
        elif state != "DONE":
            why.append(note or "said missing")
        elif len(_norm(quote)) < 12 or _norm(quote) not in _norm(readme):
            why.append(f"said done, but its quote isn't in README.md: `{quote[:80]}`")
        gone = readme_phrases_missing(t, readme)
        if gone:
            why.append("README.md doesn't contain " + ", ".join(f'"{x}"' for x in gone))
        out[n] = ("MISSING", "; ".join(why)[:600]) if why else ("DONE", "")
    return out


# -- a person's word on a locked test (Nov 17, the user: "Yes, build both"): `smithy.py decide ID TEST test|code|both`
def decided_path(job_id: int) -> Path:
    return SMITHY.results_dir / f"{job_id:05d}-decided.json"


def decisions(job_id: int) -> dict[str, dict]:
    """{test name: {"side": "test"|"code"|"both", "by": "smithy"|"person", "why": ..., "times": n, "at": history index}}.
    An older file's plain "test"/"code" values read as a person's word."""
    try:
        got = json.loads(decided_path(job_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for k, v in (got.items() if isinstance(got, dict) else []):
        d = {"side": v, "by": "person"} if isinstance(v, str) else v if isinstance(v, dict) else {}
        if d.get("side") in SIDES:
            out[str(k)] = d
    return out


def decided(job_id: int) -> dict[str, str]:
    """{test name: "test" (the test is wrong: correct it) | "code" (the test is right: fix the code) | "both"
    (correct the test to the request, and fix the code to it)}."""
    return {k: d["side"] for k, d in decisions(job_id).items()}


def record_decision(job_id: int, test: str, side: str, by: str, why: str = "", at: int = 0,
                    before: dict | None = None) -> dict:
    """Kept in results\\NNNNN-decided.json (not written for job 0, a test run); returns the new entry."""
    now = decisions(job_id) if job_id else {}
    prev = (before or {}).get(test) or now.get(test, {})
    entry = {"side": side, "by": by, "why": why[:400], "times": int(prev.get("times", 0)) + 1 if by == "smithy" else 0,
             "at": at}
    if job_id:
        now[test] = entry
        decided_path(job_id).write_text(json.dumps(now, indent=1), encoding="utf-8")
    return entry


def decide(job_id: int, test: str, side: str) -> str:
    """An override, never asked of anyone: The Smithy judges a locked test itself (Nov 17, the user: "it's always
    going to be the smithy's problem to solve, not the user's")."""
    if side not in SIDES:
        raise ValueError('say "test" (the test is wrong), "code" (the code is wrong) or "both"')
    if not re.fullmatch(r"test\w*", test or ""):
        raise ValueError(f"{test!r} isn't a test name (they start with test)")
    if SMITHY.get(job_id) is None:
        raise ValueError(f"there's no Smithy task {job_id}")
    record_decision(job_id, test, side, "person")
    return (f"Task {job_id}: {test} is wrong, so the next run corrects the test to match the request (the code isn't "
            "changed to fit it)." if side == "test" else
            f"Task {job_id}: {test} and the code are both wrong, so the next run corrects the test to the request "
            "and fixes the code to do what the request asks." if side == "both" else
            f"Task {job_id}: {test} is right, so the next run fixes the code for it (the test stays as it is).") \
        + f" Carry on with: smithy.py continue {job_id}"


SIDES = ("test", "code", "both")
_VERDICT = re.compile(r"(?im)^\W*(TEST|CODE|BOTH)\b[^\n]*(?:\n+[*_ ]*because[*_ ]*:?[*_ ]*\s*(.+))?")


def _judge_messages(request: str, test: str, test_src: str, failure: str, code: dict[str, str]) -> list[dict]:
    shown = "\n\n".join(f"--- {r} ---\n{t[:6000]}" for r, t in code.items())
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content":
             "The request (it decides who's right):\n\n" + redact(request) + "\n\nThis test has failed in several "
             f"test runs in a row:\n```python\n{test_src}\n```\n\nHow it fails (on Windows 11):\n```\n{failure[-2500:]}"
             f"\n```\n\nThe code it tests:\n\n{shown}"},
            {"role": "user", "content":
             "Which one is wrong, judged only against the request's own words? Answer TEST when the test expects "
             "something the request doesn't ask for, or can't be true here (for example a path written with / where "
             "Windows gives \\, a value the request never names, a setup that doesn't match the request's example). "
             "A check's conditions are part of its setup (\"with none of these in the glossary\", \"with X in "
             "Y\", \"when ...\", \"if ...\"): a test that doesn't set one up, or sets up its opposite, "
             "expects something the request doesn't ask for, so answer TEST. "
             "Answer CODE when the code doesn't do what the request asks. Answer BOTH only when each is wrong in its "
             "own way: the test checks something the request doesn't ask for (or can't be true), AND the code doesn't "
             "do what the request asks. Reply with exactly two lines:\n"
             "TEST, CODE or BOTH\nBecause: one plain sentence that quotes the request's words it rests on. "
             + JUDGE_JSON_HINT}]


_STRENGTH = re.compile(r"(?im)^\W*(SAME|WEAKER)\b[^\n]*(?:\n+[*_ ]*(?:because|missing)[*_ ]*:?[*_ ]*\s*(.+))?")
# what a correction of a test judged wrong is told (Nov 18, task 60: the correction checked one field instead of the
# saved file, and kept matching "i" inside other words)
CORRECT_RIGHT = ("Correct only the part found wrong. Keep what each check looks at as wide as the request says (\"the "
                 "saved file\" means the whole file, not one field of it), and keep every value it checks. Compare "
                 "words as whole words (split the text into words), never as parts of other words.")


def review_test(ctx, request: str, test: str, src: str, old_src: str = "", min_b: float = 70) -> tuple[str, str] | None:
    """("same" | "weaker", what it no longer checks) for a test judged wrong and corrected, by a strong model against
    the request: with `old_src`, the corrected test against the one before; without, the test against the request
    alone. None when no strong model is free or the answer can't be read (Nov 18, task 60)."""
    shown = (f"The test before it was corrected:\n```python\n{old_src}\n```\n\nThe corrected test:\n```python\n{src}\n```"
             if old_src else f"The test:\n```python\n{src}\n```")
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "The request (it decides):\n\n" + redact(request) + f"\n\n{test} was judged "
             "wrong against the request (it checked something the request doesn't ask for) and corrected.\n\n" + shown},
            {"role": "user", "content":
             "Apart from the part that was wrong, does " + ("the corrected test still check everything the old one "
                                                             "checked that the request asks for" if old_src else
                                                             "this test check everything the request asks of it")
             + ", as strictly and over as much (the whole file, every word or value the request names)? Answer SAME "
             "if it does, WEAKER if it checks less. Reply with exactly two lines:\nSAME or WEAKER\nBecause: what it "
             "no longer checks, quoting the request's words (or \"nothing\")"}]
    try:
        with worker_shared.strong_models(min_b):
            text = ctx.chat(msgs, 2000)
    except (BudgetOut, TooLongForLocal):
        raise
    except (worker_cloud.NoStrongModel, RuntimeError, ValueError):
        return None
    m = _STRENGTH.search(text or "")
    if not m:
        return None
    return m.group(1).lower(), " ".join((m.group(2) or "").split())[:400]


# every object closed (additionalProperties false): Cerebras's strict mode refuses an answer shape without it (Nov 20,
# task 68: both Cerebras keys turned down every batched judging step, and the stronger-models round ran out of models)
JUDGE_MANY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["verdicts"],
                     "properties": {"verdicts": {"type": "array", "items": {
                         "type": "object", "additionalProperties": False, "required": ["test", "verdict", "because"],
                         "properties": {"test": {"type": "string"},
                                        "verdict": {"type": "string", "enum": ["TEST", "CODE", "BOTH"]},
                                        "because": {"type": "string"}}}}}}


def judge_tests(ctx, request: str, tests: dict[str, str], failure: str, code: dict[str, str],
                min_b: float = 70, batch: int = 8) -> dict[str, tuple[str, str]]:
    """{test: (side, why)} for several tests that keep failing, judged together: one strong-model call for up to
    `batch` tests, which share the request and the code (Nov 20, task 68: 19 tests judged one call each, every call
    reading every file, used most of the task's 890,835 cloud tokens in 37 minutes). One test: judge_test as before.
    A test missing from the answer isn't judged this run."""
    names = list(tests)
    if len(names) == 1:
        one = judge_test(ctx, request, names[0], tests[names[0]], failure, code, min_b)
        return {names[0]: one} if one else {}
    out: dict[str, tuple[str, str]] = {}
    for k in range(0, len(names), batch):
        part = names[k:k + batch]
        msgs = _judge_messages(request, ", ".join(part), "\n\n".join(tests[n] for n in part), failure, code)
        msgs[-1] = {"role": "user", "content": msgs[-1]["content"].split(" Reply with exactly two lines:")[0]
                    + " Judge each of these tests on its own: " + ", ".join(part) + ". Answer as JSON: "
                    '{"verdicts": [{"test": name, "verdict": "TEST" or "CODE" or "BOTH", "because": one plain '
                    "sentence that quotes the request's words it rests on}]}, one entry for each test."}
        try:
            with worker_shared.strong_models(min_b), worker_shared.answer_shape(JUDGE_MANY_SCHEMA):
                data = _json_reply(ctx.chat(msgs, 4000)) or {}
        except (BudgetOut, TooLongForLocal):
            raise
        except (worker_cloud.NoStrongModel, RuntimeError, ValueError):
            continue
        for v in data.get("verdicts") or []:
            n, side = str(v.get("test", "")).strip(), str(v.get("verdict", "")).lower()
            if n in part and side in SIDES:
                out[n] = (side, " ".join(str(v.get("because") or "").split())[:400])
    return out


def judge_test(ctx, request: str, test: str, test_src: str, failure: str, code: dict[str, str],
               min_b: float = 70) -> tuple[str, str] | None:
    """("test" | "code" | "both", why) for a test that keeps failing, by a strong model against the request; None when no
    strong model is free or the answer can't be read. The Smithy judges it itself: a person using Othos can't be
    expected to (Nov 17, the user; task 58's test_query_writes_inside_queries wanted / in a Windows path)."""
    try:
        # room to think first: gpt-oss models reason before answering, and 400 tokens gave empty replies from
        # every provider (Nov 17, task 58's judgments of test_request.py's tests)
        with worker_shared.strong_models(min_b), worker_shared.answer_shape(JUDGE_SCHEMA):
            text = ctx.chat(_judge_messages(request, test, test_src, failure, code), 2000)
    except (BudgetOut, TooLongForLocal):
        raise
    except (worker_cloud.NoStrongModel, RuntimeError, ValueError):
        return None
    data = _json_reply(text)
    if data and str(data.get("verdict", "")).lower() in SIDES:
        return str(data["verdict"]).lower(), " ".join(str(data.get("because") or "").split())[:400]
    m = _VERDICT.search(text or "")
    if not m:
        return None
    return m.group(1).lower(), " ".join((m.group(2) or "").split())[:400]


CONFLICTS_SCHEMA = {"type": "object", "additionalProperties": False,
                    "properties": {"conflicts": {"type": "array", "items": {"type": "string"}}}, "required": ["conflicts"]}


def request_conflicts(ctx, job_id: int, request: str) -> list[str]:
    """Things in a request that can't both be true, found once per task, before tests are written from it (Nov 20,
    task 67: tests lock in one side of a contradiction, and every fix round fights the other). A plan already lists
    these; a follow-up has no plan, so it's asked here. [] when there are none or no model can say."""
    saved = SMITHY.results_dir / f"{job_id:05d}-conflicts.json"
    try:
        return list(json.loads(saved.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    msgs = [{"role": "system", "content": RULES + " You check a request for a program before it is built."},
            {"role": "user", "content": "List the things in this request that can't both be true, or that contradict "
                                        "an example the request itself gives (a rule saying one thing and a sample "
                                        "showing another). Quote both sides briefly. Only real contradictions, not "
                                        "missing details. An empty list if there are none. Answer as JSON: "
                                        '{"conflicts": ["..."]}\n\nThe request:\n' + request[:12000]}]
    try:
        with worker_shared.answer_shape(CONFLICTS_SCHEMA):
            data = _json_reply(ctx.chat(msgs, 1500)) or {}
    except (BudgetOut, TooLongForLocal):
        raise
    except Exception:
        return []
    found = [" ".join(str(c).split())[:300] for c in data.get("conflicts") or [] if str(c).strip()][:6]
    try:
        saved.write_text(json.dumps(found), encoding="utf-8")
    except OSError:
        pass
    return found


# -- tests written from the request itself, which the fix rounds don't change (Nov 17, task 58: every test passed
# while 7 of the request's rules had no test, so "syntax error" and "Python" were "not in my glossary" and nobody knew)
REQUEST_TESTS = "test_request.py"


def _request_checks(items: dict[int, str]) -> list[str]:
    """Each lettered check and each rule no check covers, as "2a: ..." lines."""
    out = []
    for n, t in items.items():
        subs = sub_points(t)
        out += [f"{n}{k}: {v}" for k, v in subs.items()]
        rules = dict(zip([c for c in "zyxwvutsrq" if c not in subs], smithy_diagnose.rules_of(t)))
        out += [f"{n}{k}: {v}" for k, v in rules.items()]
    return out


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def uncovered_checks(checks: list[str], tests_src: str) -> list[str]:
    """The "2a: text" lines whose text no test in `tests_src` quotes (each request test starts with a comment that
    quotes its check word for word). A follow-up inherits the earlier version's test_request.py, which holds tests
    for the earlier request's checks; without this its new checks had no request test at all (Nov 19, task 67:
    "request tests 9 of 9 passed" were task 63's, and (a) with JSON in the glossary was never tested)."""
    have = _plain(tests_src)
    return [c for c in checks if _plain(c.split(": ", 1)[-1])[:60] not in have]


def corrected_funcs(reply: str, names: set[str]) -> tuple[dict[str, str], list[str]]:
    """From a model's reply: ({test name: its source, dedented}, the top-level import lines), for the named tests only;
    ({}, []) when the reply doesn't parse."""
    body = _strip_outer_fence(reply or "", False)
    try:
        tree = ast.parse(body)
    except SyntaxError:
        return {}, []
    lines = body.splitlines()
    funcs = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names and n.name not in funcs:
            start = min([d.lineno for d in n.decorator_list] + [n.lineno])
            funcs[n.name] = textwrap.dedent("\n".join(lines[start - 1:n.end_lineno]))
    imports = [ast.get_source_segment(body, n) or "" for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    return funcs, [i for i in imports if i]


def replace_funcs(text: str, funcs: dict[str, str], imports: list[str] = ()) -> str:
    """The test file with each named function replaced by the given source (at the old one's indentation), and any of
    `imports` it lacks added after its last top-level import."""
    tree = ast.parse(text)
    lines = text.splitlines()
    spots = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in funcs:
            start = min([d.lineno for d in n.decorator_list] + [n.lineno])
            spots.append((start, n.end_lineno, n.col_offset, n.name))
    for start, end, col, name in sorted(spots, reverse=True):
        new = textwrap.indent(funcs[name].strip("\n"), " " * col).splitlines()
        lines[start - 1:end] = new
    have = {" ".join(x.split()) for x in lines}
    missing = [i for i in imports if " ".join(i.split()) not in have]
    if missing:
        last = max([n.end_lineno for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))] or [0])
        lines[last:last] = missing
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _correct_tests_messages(request: str, said: list[str], tests_src: str, written: dict[str, str]) -> list[dict]:
    code = "\n\n".join(f"--- {r} (outline) ---\n{outline(r, written[r])}" for r in sorted(written)
                       if r.endswith(".py") and not _is_test(r))
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "The request (it decides):\n\n" + redact(request[:12000])
             + "\n\nThe request itself says its own tests are wrong and must change:\n- " + "\n- ".join(said)
             + "\n\nThe program's Python files (outlines):\n\n" + code},
            {"role": "user", "content":
             f"These tests in {REQUEST_TESTS} were written for an earlier request and fail. Correct each one so it does "
             "what the request says, for example: look only at the line the request names, or set up its own small "
             "stand-in data (in a temporary folder) instead of reading the project's real files, when the request says "
             "so. Keep each test's name. Keep every check the request asks for, as strictly as before; never skip a "
             "test and never take out an assertion. Call the code exactly as the outlines show.\n\n```python\n"
             + tests_src + "\n```\n\nReply with one Python code block: any imports these tests need, then "
             "`class Corrected(unittest.TestCase):` holding only these test methods, corrected."}]


def add_request_tests(tests_src: str, new_src: str) -> str:
    """`tests_src` with the tests of `new_src` (its own imports and class) put before the `if __name__` guard."""
    guard = re.compile(r'(?m)^if __name__ == [\'"]__main__[\'"]:.*(?:\n[ \t]+.*)*\s*$')
    have = {ln.strip() for ln in tests_src.splitlines()}
    # an import the file already has isn't repeated (ruff's F811 stopped task 67's tests running)
    add = "\n".join(ln for ln in guard.sub("", new_src).splitlines()
                    if not (re.match(r"(?:import|from)\s", ln) and ln.strip() in have)).strip("\n") + "\n"
    m = guard.search(tests_src)
    if not m:
        return tests_src.rstrip() + "\n\n\n" + add + '\n\nif __name__ == "__main__":\n    unittest.main()\n'
    return tests_src[:m.start()].rstrip() + "\n\n\n" + add + "\n\n" + tests_src[m.start():].lstrip("\n")


def remove_added_tests(tests_src: str, job_id: int) -> str:
    """`tests_src` without the class add_request_tests put in for this task (RequestTestsT<job>) and the import
    lines just above it: for tests added earlier that have build problems, so they're written again."""
    lines = tests_src.split("\n")
    start = next((i for i, ln in enumerate(lines) if re.match(rf"class RequestTestsT{job_id}\b", ln)), None)
    if start is None:
        return tests_src
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"(?:class |if __name__)", lines[i])), len(lines))
    while start > 0 and (not lines[start - 1].strip() or re.match(r"(?:import|from)\s", lines[start - 1])):
        start -= 1
    return "\n".join(lines[:start] + ["", ""] + lines[end:])


def unrequested_changes(request: str, plan: dict) -> list[str]:
    """The files the plan changes that the request never names (a test file excepted): a follow-up's review says
    so, so each is looked at (Nov 19, task 67: academy_privacy.py, academy_fit.py and academy_run.py were changed
    by a request that names none of them)."""
    files = [f for f in plan.get("files", []) if f.get("change") not in ("same", None)
             and not _is_test(f["path"]) and not f["path"].lower().endswith(".md")]
    named = _names(request, files, stems=True)
    return sorted(f["path"] for f in files if f["path"] not in named)


def _request_tests_messages(items: dict[int, str], written: dict[str, str], checks: list[str],
                            job_id: int = 0) -> list[dict]:
    code = "\n\n".join(f"--- {r} (outline) ---\n{outline(r, written[r])}" for r in sorted(written)
                       if r.lower().endswith(".py") and not smithy_sandbox.is_test(r))
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content":
             "The request:\n\n" + redact("\n\n".join(items.values())) + "\n\nThe program's Python files (outlines: "
             "imports, functions, classes):\n\n" + code},
            {"role": "user", "content":
             f"Write {REQUEST_TESTS}: a Python 3.12 unittest file with ONE test for each of these checks and rules "
             "from the request, and no other tests:\n\n" + "\n".join(f"- {c}" for c in checks) + "\n\n"
             + (f"Each test is a method named test_t{job_id}_<key>_<a few words> (for example test_t{job_id}_2a_yes_no) "
                f"of one unittest.TestCase class named RequestTestsT{job_id}, "
                if job_id else "Each test is a method named test_<key>_<a few words> (for example "
                "test_2a_yes_no_go_ahead) of one unittest.TestCase class, ") +
             "and its first line is a comment that quotes the check word for word. "
             "Use the request's own example inputs and expected answers wherever it gives them. Test what the "
             "program does (call its functions, read what they write), never how it's written. Only the standard "
             "library and the program's own modules; files only in a tempfile.TemporaryDirectory(); compare paths "
             "as pathlib.Path objects or with os.path, never as text with / or \\ in it; never os.chdir (on Windows a "
             "temporary folder in use can't be removed: pass full paths instead); no network. End with one "
             "top-level `if __name__ == \"__main__\":` and unittest.main(). Reply with the file only."}]


# -- the user's benchmarks, measured (Nov 17): one line in each result, and one row per run in a running log, so
# whether The Smithy gets better over time can be seen
BENCHMARKS_LOG = "smithy-benchmarks.jsonl"
_SAFETY = ("for network use", "outside the build's own folder", "part of Python's own")


def _test_counts(output: str) -> tuple[int, int] | None:
    """(tests run, tests failed or in error) from unittest's last lines; None when it didn't say."""
    ran = re.findall(r"\bRan (\d+) tests?\b", output or "")
    if not ran:
        return None
    # each failing test once, by name: a test that fails and then errors in its cleanup is one test (Nov 18, task
    # 60: "failures=2, errors=9" of 9 tests read as "-2 of 9 passed")
    names = set(re.findall(r"(?m)^(?:FAIL|ERROR): (\w+)", output))
    bad = len(names) if names else \
        sum(int(n) for n in re.findall(r"\b(?:failures|errors)=(\d+)", output.split(f"Ran {ran[-1]}")[-1]))
    return int(ran[-1]), min(bad, int(ran[-1]))


def _benchmarks(job_id: int, hist: Path | None, problems: list[str], sandbox: dict, left: list[str],
                seconds: float) -> str:
    events = smithy_diagnose.read(hist)
    checks = [e for e in events if e.get("kind") == "check"]
    first_clean = bool(checks) and not checks[0].get("missing") and not checks[0].get("problems")
    runs = [e for e in events if e.get("kind") == "sandbox"]
    first_tests = runs[0].get("status") if runs else ""
    this_run = len([e for e in events[max((i for i, e in enumerate(events) if e.get("kind") == "run"), default=0):]
                    if e.get("kind") == "check"])
    unsafe = [x for x in problems if any(s in x for s in _SAFETY)]
    req = _test_counts(sandbox.get("full", {}).get(REQUEST_TESTS) or sandbox.get("output", {}).get(REQUEST_TESTS, ""))
    tokens = worker_cloud.task_tokens()
    row = {"ts": round(time.time()), "task": job_id, "clean": not left, "safety": "passed" if not unsafe else "failed",
           "first_check_clean": first_clean, "first_tests": first_tests, "checks_this_run": this_run,
           "request_tests": list(req) if req else None, "task_tokens": tokens, "check_seconds": round(seconds)}
    if job_id:
        try:
            with (SMITHY.results_dir / BENCHMARKS_LOG).open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except OSError:
            pass
    return ("**Benchmarks:** safety " + ("passed" if not unsafe else f"FAILED ({len(unsafe)} problem"
                                         f"{'s' if len(unsafe) != 1 else ''})")
            + "; first check " + ("clean" if first_clean else "not clean")
            + (f"; first test run {first_tests}" if first_tests else "")
            + f"; {this_run} check{'s' if this_run != 1 else ''} this run"
            + (f"; request tests {req[0] - req[1]} of {req[0]} passed" if req else "")
            + f"; {tokens:,} cloud tokens for the task so far; {round(seconds / 60, 1)} minutes of checks"
            + ("; checks clean." if not left else "; NOT clean yet."))


_TOKENS_SO_FAR = re.compile(r"; [\d,]+ cloud tokens for the task so far;")


def _final_tokens(job_id: int, notes: list[str]) -> list[str]:
    """The Benchmarks line's and log row's cloud tokens, counted again once the README is written: the checks
    end before it, so task 58's line said 36,169 while the result's header said 53,749 (Nov 18)."""
    tokens = worker_cloud.task_tokens()
    out = [_TOKENS_SO_FAR.sub(f"; {tokens:,} cloud tokens for the task so far;", n)
           if n.startswith("**Benchmarks:**") else n for n in notes]
    log_path = SMITHY.results_dir / BENCHMARKS_LOG
    if job_id and any(n.startswith("**Benchmarks:**") for n in notes):
        try:
            rows = log_path.read_text(encoding="utf-8").splitlines()
            for i in range(len(rows) - 1, -1, -1):
                row = json.loads(rows[i])
                if row.get("task") == job_id:
                    if row.get("task_tokens") != tokens:
                        row["task_tokens"] = tokens
                        rows[i] = json.dumps(row)
                        log_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
                    break
        except (OSError, ValueError):
            pass
    return out


def _verify_build(ctx, job_id: int, p: dict, desc: str, plan: dict, folder: Path, written: dict[str, str],
                  cap: int, only: dict[int, str] | None = None,
                  rounds: int | None = None) -> tuple[list[str], str, list[str]]:
    """Check the build before its README, and keep fixing until it checks clean (the user, Nov 14: nothing
    passes as complete until it does). Each round: the numbered changes checked off, then compile / parse;
    edits for whatever failed; up to `build_check_rounds` rounds (default 3).
    Returns (notes for the result, the truth for the README, what's still wrong: empty = checks clean)."""
    notes = []
    started = time.monotonic()
    code = [r for r in written if not r.lower().endswith(".md")]
    rounds = max(1, int(rounds or ctx.cfg.get("build_check_rounds", 3)))
    # `only`: one stage's changes (a staged build checks each stage before the next)
    # a first build's numbered list is checked off too (review step 1, Nov 14: task 37's 9 items weren't)
    items = (only if only is not None else numbered_items(p.get("change", "")) if p.get("parent_id")
             else numbered_items(p.get("request", "")))
    # the README is written after this check (and told what's true), so its item isn't ticked here
    items = {n: t for n, t in items.items() if not _README_ITEM.match(t)}
    use_list = len(items) >= (1 if only is not None else 2)
    stdlib_only = bool(_STDLIB_ONLY.search(p.get("request", "")))
    net_ok = net_ok_for(p.get("request", "") + "\n" + p.get("change", ""))    # the safety gate: network only if asked
    wants_tests = only is None and bool(_WANTS_TESTS.search(p.get("request", "") + "\n" + p.get("change", "")))
    # the version this one was built from, to see what changed since (Nov 19, task 59)
    earlier = (_parent_build(int(p["parent_id"])) or {}).get("folder") if p.get("parent_id") else None

    def _own_problems(rel: str) -> list[str]:
        """The build problems in this one file as it is on disk (unused code aside)."""
        try:
            found = build_problems(folder, code, stdlib_only, net_ok)[0]
        except Exception:                                # a checker that can't run says nothing here
            return []
        return [x for x in found if x.startswith(f"{rel} line") and not _DEAD_CODE.match(x)]

    def _adds_problems(rel: str, old: str, new: str) -> str:
        """The build problems an edit adds to this file ('' when none: the same number or fewer), from the build
        check on this file alone (Nov 16, task 56: a Sandbox fix pasted academy_readme.py's code into the top of
        test_academy.py with none of its imports, so 12 undefined names in a file that was clean, and the run ended
        there)."""
        if new == old or not rel.lower().endswith((".py", ".cs", ".ps1", ".psm1", ".json")):
            return ""
        path = inside(folder, rel)
        # checked with the whole build, so the build's own modules are known (Nov 16, task 56: alone, a new
        # `import academy_explain` in the test read as "not in Python's standard library", and a good fix was dropped)
        try:
            before = build_problems(folder, code, stdlib_only, net_ok)[0]
            path.write_text(new, encoding="utf-8")
            after = build_problems(folder, code, stdlib_only, net_ok)[0]
        except Exception:
            return ""
        finally:
            path.write_text(old, encoding="utf-8")
        # an unused import or variable alone never refuses an edit (one edit may add an import that the next uses);
        # the fix rounds still clean it up, as a build problem (Nov 17)
        before = [x for x in before if not _DEAD_CODE.match(x)]
        after = [x for x in after if not _DEAD_CODE.match(x)]
        if len(after) <= len(before):
            return ""
        old_kinds = {re.sub(r"line \d+", "", x) for x in before}
        added = [x for x in after if re.sub(r"line \d+", "", x) not in old_kinds] or after[len(before):]
        return f"{len(after) - len(before)} new build problem{'s' if len(after) - len(before) != 1 else ''}: " \
               + "; ".join(added[:6])

    request_text = "\n".join(items.values()) or p.get("change") or p.get("request", "")
    locked: set[str] = set()          # tests that failed in several Sandbox runs in a row: test fixes leave them be
    # the request's own tests the request itself asks to change: a fix may change them (other guards still apply)
    retest = request_retests(p.get("change") or p.get("request", ""))

    def _retest_ok(rel: str, name: str) -> bool:
        return rel == REQUEST_TESTS and ("*" in retest or name in retest)
    asked_tokens = {a or b for a, b in re.findall(r"\"([^\"\n]{2,80})\"|`([^`\n]{2,80})`", request_text)}
    request_words = set(re.findall(r"[a-z_][a-z0-9_]*", request_text.lower()))

    def _bends_test(rel: str, old: str, new: str) -> str:
        """Why an edit to a test file bends the test toward the code ('' when it doesn't): it removes something the
        request names in quotes or backticks, or it adds a dict key (a field) the request never names (Nov 16,
        task 57: a fix turned the test's planted ".env" into "environment file" and added "voice": True, a field
        only the code had made up)."""
        if new == old or not _is_test(rel):
            return ""
        # fewer mentions than before (task 57: the planted ".env" went, while assertNotIn(".env", ...) stayed)
        gone = sorted(t for t in asked_tokens if len(t.strip()) >= 3 and old.count(t) > new.count(t))

        try:
            added = added_fields(old, new, request_words)
            if added:                                   # only then is the build check worth running
                added = added_fields(old, new, request_words, broken_tests(old, rel, _own_problems(rel)))
        except SyntaxError:
            added = []
        why = []
        try:
            fewer = _assert_count(old) - _assert_count(new)
        except SyntaxError:
            fewer = 0
        if fewer > 0:
            # task 57: `self.assertGreaterEqual(len(result["topics"]), 1)` commented out, and the test passed
            why.append(f"it removes {fewer} assertion{'s' if fewer != 1 else ''} (a check is corrected, never "
                       "taken out or commented out)")
        changed = [n for n in _changed_tests(old, new, locked) if not _retest_ok(rel, n)]
        if changed:
            # task 57: test_write_query_... failed 4 runs in a row, then its folder was narrowed so it passed
            why.append(f"it changes {', '.join(changed)}, which failed in several test runs in a row and is locked: "
                       "the code is fixed for it, or The Smithy judges which side is wrong")
        if gone:
            why.append("it removes what the request names: " + ", ".join(f'"{t}"' for t in gone[:4])
                       + " (never take out a check the request asks for)")
        if added:
            why.append("it adds field(s) the request never names: " + ", ".join(f'"{k}"' for k in added[:4])
                       + " (a test checks the request, not what the code happens to want)")
        why += weakened_tests(old, new)                 # Nov 18, task 60: one field instead of the saved entry
        why += copied_tests(old, new)                   # Nov 18, task 60: test_rule_a copied an existing test
        # a test the edit changes may not look for words inside a whole text (task 60: "i" matched "id")
        try:
            touched = set(_changed_tests(old, new, {n.name for n in ast.walk(ast.parse(old))
                                                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}))
        except SyntaxError:
            touched = set()
        why += [w for n, w in part_word_checks(new).items() if n in touched]
        return "; ".join(why)

    def _weaker_correction(rel: str, old: str, new: str) -> str:
        """A test judged wrong whose correction a strong model finds weaker than before, against the request ('' when
        it isn't, or no strong model is free). Asked only once the guards without a model have passed."""
        if new == old or not _is_test(rel):
            return ""
        why = []
        for n in _changed_tests(old, new, set(wrong_tests)):
            before, after = (_func_nodes(x).get(n) for x in (old, new))
            if before is None or after is None:
                continue
            got = review_test(ctx, request_text, n, ast.unparse(after), ast.unparse(before),
                              float(ctx.cfg.get("rewrite_min_model_b", 70) or 0))
            if got and got[0] == "weaker":
                why.append(f"its correction of {n} checks less than the request asks"
                           + (f" ({got[1]})" if got[1] else "") + ". " + CORRECT_RIGHT)
        return "; ".join(why)

    def _request_tests_kept(rel: str, old: str, new: str) -> str:
        """The tests written from the request aren't changed by a fix, except one judged wrong (judge_test)."""
        if rel != REQUEST_TESTS or new == old:
            return ""
        try:
            names = {n.name for n in ast.walk(ast.parse(old)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        except SyntaxError:
            return ""                                  # it didn't parse before: the parse check says so
        changed = [n for n in _changed_tests(old, new, names) if n not in wrong_tests   # changed or removed,
                   and not _retest_ok(rel, n)]                     # unless the request asks for it to change
        if changed:
            # a test that can't run (a build problem on its own lines, such as a call missing arguments) may be
            # fixed there; the guards against weakening a test still apply (Nov 18, task 60)
            broken = broken_tests(old, REQUEST_TESTS, _own_problems(REQUEST_TESTS))
            changed = [n for n in changed if n not in broken]
        if changed:
            return (f"{REQUEST_TESTS} holds the tests written from the request and a fix doesn't change them "
                    f"({', '.join(changed)}); the code is fixed, or The Smithy judges which side is wrong")
        return ""

    def refused(rel: str, old: str, new: str) -> str:
        return _breaks(rel, old, new) or added_silencers(old, new) or added_skips(old, new) \
            or _adds_problems(rel, old, new) \
            or _request_tests_kept(rel, old, new) or _bends_test(rel, old, new) or _weaker_correction(rel, old, new)

    def keep_allowed(rel: str, old: str, new: str) -> tuple[str, list[str]]:
        """A refused test edit with its changes to locked tests put back, keeping the rest (a test that's broken or
        judged wrong may change); ('', []) when nothing is left to keep (Nov 18, task 60: an edit fixing test_3a and
        test_3g was refused whole because test_3g looked locked)."""
        if not _is_test(rel) or new == old:
            return "", []
        try:
            names = {n.name for n in ast.walk(ast.parse(old)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        except SyntaxError:
            return "", []
        free = set(wrong_tests) | broken_tests(old, rel, _own_problems(rel))
        held = {n for n in _changed_tests(old, new, names) if n not in free
                and (n in locked or rel == REQUEST_TESTS) and not _retest_ok(rel, n)}
        if not held:
            return "", []
        part = restore_tests(old, new, held)
        return ("", []) if part == old or refused(rel, old, part) else (part, sorted(held))

    def fix(rel: str, what: str, must: bool = False, failing: list[str] | tuple = ()) -> None:
        """`must`: the check named this file for a change it found missing, so "no edits" isn't an answer: the
        file is then written whole (Nov 15, task 56: models answered no edits while the glossary had no ids)."""
        # what the guards would refuse, said up front, so the first edit can get it right (Nov 18, task 60: the
        # part-of-word problem in test_3a was only learned from refusals, and the retry still missed it)
        pw = part_word_checks(written[rel]) if _is_test(rel) else {}
        if pw:
            what += ("\n\nIf you change any of these tests, fix this in it too, or the edit is refused:\n- "
                     + "\n- ".join(pw.values()))
        if len(written[rel].strip()) < 200 and not rel.lower().endswith(".md"):
            # (nearly) empty: there's nothing for an edit to find (task 49), so it's written whole
            steps.info("%s is (nearly) empty, so it's written whole, not edited.", rel)
            text = ctx.chat(_file_messages(p, desc, plan, rel, written, cap, None)
                            + [{"role": "user", "content": "Also make sure of this:\n\n" + what}], 6000)
            body = _strip_outer_fence(text, False)
            if len(body.strip()) >= 40 and not _refusal(body):
                inside(folder, rel).write_text(body, encoding="utf-8")
                written[rel] = body
                notes.append(f"`{rel}`: written whole again (it was empty)")
            else:
                notes.append(f"`{rel}`: still empty after asking again")
            return
        elsewhere: dict[str, list[tuple[str, str]]] = {}
        movable = {r for r in code if r not in same and r != REQUEST_TESTS and r in written}
        # the failing tests the fix is for: lines the same in several of them may be changed in each
        spread = [(n.lineno, n.end_lineno or n.lineno) for name, n in _func_nodes(written[rel]).items()
                  if name in failing] if failing and _is_test(rel) else None
        try:
            body, note = _edit_file(ctx, job_id, _edit_messages(p, desc, plan, rel, written, written[rel], what, cap),
                                    rel, written[rel], none_ok=not must, others=written,
                                    movable=movable, elsewhere=elsewhere, spread=spread,
                                    near=[int(n) for n in re.findall(rf"{re.escape(rel)} line (\d+)", what)])
        except ValueError:
            steps.info("%s: the check found a change missing here, but no edits came back.", rel)
            whole = _rewrite_whole(rel, what)
            notes.append(f"`{rel}`: no edits came back for a change the check found missing here"
                         + (f"; the file was then written whole ({whole})" if whole else "; kept as it was"))
            return
        broke = refused(rel, written[rel], body)
        if broke:
            part, held = keep_allowed(rel, written[rel], body)
            if part:                                    # the good part kept, locked tests put back
                steps.info("%s: its changes to locked tests (%s) were put back; the rest kept.", rel, ", ".join(held))
                note += f" (its changes to the locked test(s) {', '.join(held)} were put back; the rest kept)"
                body, broke = part, ""
        if broke:
            # edits that turn a file that parsed into one that doesn't aren't kept (Nov 16, task 56: the last
            # round's edit replaced unittest.main() with new tests, leaving `if __name__ == ...:` empty, and the
            # run ended on a syntax error); asked once more with the error, on the file as it was
            steps.info("%s: the edits weren't kept (%s); asking again.", rel, broke)
            try:
                body, note = _edit_file(ctx, job_id, _edit_messages(
                    p, desc, plan, rel, written, written[rel],
                    what + f"\n\nYour last edits for this were NOT kept ({broke}). Send edits that keep the file "
                    "valid and keep every check the request asks for; never replace a line other code needs (such "
                    "as unittest.main()).", cap),
                    rel, written[rel], none_ok=True, others=written)
            except ValueError:
                body = written[rel]
            again = refused(rel, written[rel], body)
            if again and (kept := keep_allowed(rel, written[rel], body))[0]:
                body, again = kept[0], ""
                note += f" (its changes to the locked test(s) {', '.join(kept[1])} were put back; the rest kept)"
            if again:
                steps.info("%s: the edits weren't kept again (%s); not kept.", rel, again[:200])
                notes.append(f"`{rel}`: its edits were refused twice ({again}), so they weren't kept")
                smithy_diagnose.record(hist, "fix", file=rel, kept=False, why=again[:160])
                moved_in(rel, elsewhere)
                return
            note += f" (the first edits broke the file: {broke}; asked again)"
        if body != written[rel] and rel == REQUEST_TESTS and retest:
            # each request test changed because the request asks it is named, with the reason (task 69, check (l))
            try:
                own = [n for n in _changed_tests(written[rel], body, set(_func_nodes(written[rel])))
                       if _retest_ok(rel, n)]
            except SyntaxError:
                own = []
            if own:
                notes.append(f"`{rel}`: changed {', '.join(own)} because the request asks for its own tests to change")
                smithy_diagnose.record(hist, "request_tests_changed", tests=own)
        if body != written[rel]:
            inside(folder, rel).write_text(body, encoding="utf-8")
            written[rel] = body
        if "**Not applied" in note and (whole := _rewrite_whole(rel, what)):
            note += f"; the missed changes were then made by writing the file whole ({whole})"
        notes.append(note)
        smithy_diagnose.record(hist, "fix", file=rel, kept=True, broke_first=bool(broke),
                               missed="**Not applied" in note)
        moved_in(rel, elsewhere)

    def moved_in(src: str, elsewhere: dict[str, list[tuple[str, str]]]) -> None:
        """Edits sent for `src` whose FIND is clearly in another file: applied there, with the same guards as any
        edit of that file (never the request's tests, never a file the plan keeps the same)."""
        for other, eds in elsewhere.items():
            old = written[other]
            new, done, _ = apply_edits(old, eds, Path(other).suffix.lower() in _BRACES)
            why = refused(other, old, new) if new != old else ""
            if done and not why:
                inside(folder, other).write_text(new, encoding="utf-8")
                written[other] = new
                steps.info("%s: %d edit%s sent for %s belonged here; applied.", other, done,
                           "" if done == 1 else "s", src)
                notes.append(f"`{other}`: {done} edit{'s' if done != 1 else ''} sent for `{src}` belonged here, "
                             "so they were applied here")
            elif why:
                steps.info("%s: edits sent for %s belonged here but weren't kept (%s).", other, src, why[:160])
                notes.append(f"`{other}`: edits sent for `{src}` belonged here but weren't kept ({why})")

    def _rewrite_whole(rel: str, what: str) -> str:
        """Edits that couldn't be found (task 51: 0 of 2 went in, twice): the file is written whole instead,
        with the current text to keep. Only kept when it's no shorter than half the old one, isn't a refusal
        and (Python, JSON) still parses; else the old file stays. Returns how it went, or '' (not tried)."""
        old = written[rel]
        if len(old) > 24_000 or rel.lower().endswith(".md") or rel == REQUEST_TESTS:
            return ""                                   # too long to rewrite safely: the edits stay the only way
        if redact(old) != old:                          # something in it was hidden from the model: a rewrite
            return "not tried: the file has a hidden value a rewrite could lose"   # could write [REDACTED] over it
        before = py_import_problems(folder, list(written), stdlib_only) or [] if rel.endswith(".py") else []
        steps.info("%s: the missed changes are made by writing the file whole, keeping what works.", rel)
        msgs = (_file_messages(p, desc, plan, rel, {k: v for k, v in written.items() if k != rel}, cap, None)
                + [{"role": "user", "content": f"The current {rel}, to keep except where it must change:\n```\n"
                                               f"{old}\n```\n\nWhat must change:\n{what}"}])
        try:
            # a whole file is only rewritten by a strong model (rewrite_min_model_b, default 70: billions of
            # parameters); when none is free, it's not tried this round, so the next round asks again (Nov 16,
            # task 56: NVIDIA's gpt-oss-20b rewrote the test file and kept the wrong test)
            with worker_shared.strong_models(float(ctx.cfg.get("rewrite_min_model_b", 70) or 0)):
                body = _strip_outer_fence(ctx.chat(msgs, 8000), False)
        except (BudgetOut, TooLongForLocal):
            raise
        except worker_cloud.NoStrongModel as e:
            steps.info("%s: not written whole this round: %s.", rel, e)
            return f"not tried this round: {e}"
        except Exception as e:
            return f"not kept: {e}"
        ok = len(body.strip()) >= len(old.strip()) // 2 and not _refusal(body)
        if ok and rel.lower().endswith(".py"):
            try:
                ast.parse(body)
            except SyntaxError:
                ok = False
        if ok and rel.lower().endswith(".json"):
            try:
                json.loads(body)
            except ValueError:
                ok = False
        if not ok:
            steps.info("%s: the whole rewrite came back short or broken, so the file was left as it was.", rel)
            return "not kept: it came back short or broken"
        patched = stdlib_patch_problems(rel, body) if rel.endswith(".py") else []
        why = _bends_test(rel, old, body) or (patched[0] if len(patched) > len(stdlib_patch_problems(rel, old)) else "")
        if why:                                         # a whole rewrite gets the same guards as an edit (Nov 17)
            steps.info("%s: the whole rewrite wasn't kept (%s).", rel, why[:200])
            return "not kept: " + why
        inside(folder, rel).write_text(body, encoding="utf-8")
        written[rel] = body
        if rel.endswith(".py"):                         # it mustn't break what the other files import (task 51:
            new = [x for x in (py_import_problems(folder, list(written), stdlib_only) or [])   # it dropped
                   if x not in before]                                                          # collect())
            if new:
                inside(folder, rel).write_text(old, encoding="utf-8")
                written[rel] = old
                steps.info("%s: the whole rewrite broke an import (%s), so the file was put back.", rel, new[0])
                return "not kept: it broke an import: " + new[0]
        return "kept"

    check_log = SMITHY.results_dir / f"{job_id:05d}-checklist.txt" if job_id else None

    found_path = SMITHY.results_dir / f"{job_id:05d}-missing.json" if job_id else None
    done_path = SMITHY.results_dir / f"{job_id:05d}-done.json" if job_id else None
    # self-diagnosis (Nov 16): what each round found, kept across runs; names and counts only
    hist = SMITHY.results_dir / f"{job_id:05d}-history.jsonl" if job_id and only is None else None
    smithy_diagnose.record(hist, "run", code=smithy_diagnose.commit(Path(__file__).parent),
                           backend=str(getattr(getattr(ctx, "backend", None), "name", "")))
    # a test that keeps failing is judged by The Smithy itself against the request (judge_test): "test" = the test
    # is wrong and is corrected to the request, "code" = the test is right and the code is fixed for it (the test
    # stays locked against edits). Never left to the person using it (Nov 17, the user). A judgment holds for the
    # task; when the test still fails twice after it, it's judged once more, and that one is final.
    verdicts = decisions(job_id) if job_id else {}
    word: dict[str, str] = {}
    wrong_tests: set[str] = set()
    to_judge: set[str] = set()                         # locked, and not (or no longer) verdicts

    def relock() -> None:
        events = smithy_diagnose.read(hist)
        stuck = {s["test"] for s in smithy_diagnose.stuck_tests(events)}
        word.clear()
        word.update({t: d["side"] for t, d in verdicts.items()})
        again = set()
        for t, d in verdicts.items():
            if d.get("by") == "smithy" and int(d.get("times", 1)) < 2 and t in stuck:
                since = [e for e in events[int(d.get("at", 0)):] if e.get("kind") == "sandbox"]
                if len(since) >= 2 and all(t in (e.get("failing") or []) for e in since[-2:]):
                    again.add(t)
        wrong_tests.clear()
        wrong_tests.update(t for t, s in word.items() if s in ("test", "both") and t not in again)
        locked.clear()
        locked.update(stuck - wrong_tests)
        to_judge.clear()
        to_judge.update((stuck - set(word)) | again)
    relock()                                           # from earlier runs
    for t, d in sorted(verdicts.items()):
        who = "You decided" if d.get("by") == "person" else "The Smithy verdicts"
        notes.append(f"**{who} `{t}`:** " + ("the test is wrong, so it's corrected to match the request and the "
                                              "code isn't changed to fit it" if d["side"] == "test" else
                                              "the test and the code are both wrong, so the test is corrected to the "
                                              "request and the code is fixed to it" if d["side"] == "both" else
                                              "the test is right, so the code is fixed for it")
                     + (f" ({d['why']})" if d.get("why") else "") + ".")

    # things in the change that can't both be true: said at the start, before tests lock one side in (Nov 20)
    if only is None and use_list and job_id and ctx.cfg.get("conflict_check"):
        clash = request_conflicts(ctx, job_id, "\n".join(items.values()))
        if clash:
            steps.info("The request has %d thing(s) that don't fit together; they're noted in the result.", len(clash))
            notes.append("**Things in the request that don't fit together** (a fix can't satisfy both sides; say "
                         "which one is meant in a follow-up):\n- " + "\n- ".join(clash))
            smithy_diagnose.record(hist, "conflicts", items=clash)
    # the tests written from the request (REQUEST_TESTS): once per build, by a strong model, before the checks;
    # the fix rounds never change them, except a test judged wrong (judge_test)
    if only is None and use_list and wants_tests and ctx.cfg.get("sandbox_tests") \
            and ctx.cfg.get("request_tests", True):
        target = inside(folder, REQUEST_TESTS)
        # an earlier version or run wrote it (a follow-up's folder holds the earlier version's files, and a
        # continued run reads them all into `written`): kept as it is, with tests added for new checks. The first
        # fix of task 67 never ran this branch: `REQUEST_TESTS not in written` skipped it (Nov 19)
        if REQUEST_TESTS in written or target.exists():
            written[REQUEST_TESTS] = written.get(REQUEST_TESTS) or target.read_text(encoding="utf-8-sig",
                                                                                   errors="replace")
            if REQUEST_TESTS not in code:
                code.append(REQUEST_TESTS)
            fake = never_fail(written[REQUEST_TESTS])  # less any test that could never fail (Nov 18, task 60)
            if fake:
                written[REQUEST_TESTS] = drop_tests(written[REQUEST_TESTS], fake)
                target.write_text(written[REQUEST_TESTS], encoding="utf-8")
                notes.append(f"`{REQUEST_TESTS}`: {len(fake)} test{'s' if len(fake) != 1 else ''} that could never "
                             f"fail left out ({', '.join(fake)}).")
            # a follow-up's file holds the earlier request's tests: tests are written for the checks it doesn't quote
            if re.search(rf"(?m)^class RequestTestsT{job_id}\b", written[REQUEST_TESTS]) and _own_problems(REQUEST_TESTS):
                # tests added earlier that can't run (a call missing arguments, a repeated import): written again
                written[REQUEST_TESTS] = remove_added_tests(written[REQUEST_TESTS], job_id)
                target.write_text(written[REQUEST_TESTS], encoding="utf-8")
                steps.info("The request tests added earlier had build problems; they're written again.")
            new_checks = uncovered_checks(_request_checks(items), written[REQUEST_TESTS])
            if new_checks and not any(f"test_t{job_id}_" in n for n in re.findall(r"def (test_\w+)", written[REQUEST_TESTS])):
                steps.info("Writing tests in %s for the %d checks no test there quotes.", REQUEST_TESTS, len(new_checks))
                try:
                    with worker_shared.strong_models(float(ctx.cfg.get("rewrite_min_model_b", 70) or 0)):
                        more = _strip_outer_fence(ctx.chat(_request_tests_messages(items, written, new_checks, job_id),
                                                           8000), False)
                    ast.parse(more)
                    if "unittest" not in more or not re.search(r"(?m)^\s+def test_\w+", more):
                        raise ValueError("it came back with no unittest tests")
                    merged = add_request_tests(written[REQUEST_TESTS], more)
                    ast.parse(merged)
                    before = written[REQUEST_TESTS]
                    target.write_text(merged, encoding="utf-8")
                    bad = _own_problems(REQUEST_TESTS)
                    if bad:                        # called wrongly: written again once, with the problems named
                        steps.info("The added tests call the code wrongly (%d problem(s)); writing them again once.",
                                   len(bad))
                        try:
                            with worker_shared.strong_models(float(ctx.cfg.get("rewrite_min_model_b", 70) or 0)):
                                again = _strip_outer_fence(ctx.chat(
                                    _request_tests_messages(items, written, new_checks, job_id)
                                    + [{"role": "assistant", "content": more},
                                       {"role": "user", "content": "The build check found these problems in the whole "
                                        "file:\n- " + "\n- ".join(bad[:15]) + "\n\nWrite your part again with them "
                                        "fixed: call each function with the arguments its outline shows. Reply with "
                                        "the file only."}], 8000), False)
                            ast.parse(again)
                            retry = add_request_tests(before, again)
                            ast.parse(retry)
                            target.write_text(retry, encoding="utf-8")
                            if len(_own_problems(REQUEST_TESTS)) < len(bad):
                                more, merged = again, retry
                        except (BudgetOut, TooLongForLocal):
                            raise
                        except (worker_cloud.NoStrongModel, SyntaxError, ValueError, RuntimeError):
                            pass
                    target.write_text(before, encoding="utf-8")
                except (BudgetOut, TooLongForLocal):
                    raise
                except (worker_cloud.NoStrongModel, SyntaxError, ValueError, RuntimeError) as e:
                    steps.info("The new request tests weren't written: %s.", e)
                    notes.append(f"**`{REQUEST_TESTS}` has no tests for {len(new_checks)} of this request's checks** "
                                 f"({e}); they're asked for again on the next run.")
                else:
                    target.write_text(merged, encoding="utf-8")
                    written[REQUEST_TESTS] = merged
                    notes.append(f"`{REQUEST_TESTS}`: {len(re.findall(r'(?m)^\s+def test_t' + str(job_id) + '_', more))} "
                                 f"tests added for the {len(new_checks)} checks of this request that no test there "
                                 "quoted; the earlier tests stay.")
                    smithy_diagnose.record(hist, "request_tests_added", checks=len(new_checks))
            # the request says its own tests are wrong: those that failed in the earlier version (or the ones it names)
            # are corrected ONCE here, by a strong model with the request in hand, before any fix round (Nov 21, task
            # 70: 92c9e58 let fixes change them, but the checklist called (c) done, the judge blamed the code, and an
            # older guard refused a correction, so 17 of task 68's 18 failing tests still failed)
            said = retest_sentences(p.get("change") or p.get("request", ""))
            done_before = any(r.get("kind") == "request_tests_corrected" for r in smithy_diagnose.read(hist))
            if said and not done_before:
                own = set(_func_nodes(written[REQUEST_TESTS])) - {
                    n for n in _func_nodes(written[REQUEST_TESTS]) if n.startswith(f"test_t{job_id}_")}
                wanted = (retest - {"*"}) if retest - {"*"} else (
                    parent_failing(SMITHY.results_dir, int(p["parent_id"])) or set() if p.get("parent_id") else set())
                todo = sorted(n for n in wanted & own if n.startswith("test"))
                if todo:
                    steps.info("The request says its own tests are wrong: correcting %d of them once, before the "
                               "fix rounds (%s).", len(todo), ", ".join(todo[:6]) + (", ..." if len(todo) > 6 else ""))
                    before = written[REQUEST_TESTS]
                    nodes = _func_nodes(before)
                    src = "\n\n".join(textwrap.dedent(ast.get_source_segment(before, nodes[n]) or "") for n in todo)
                    try:
                        with worker_shared.strong_models(float(ctx.cfg.get("rewrite_min_model_b", 70) or 0)):
                            reply = ctx.chat(_correct_tests_messages(p.get("change") or p.get("request", ""), said,
                                                                     src, written), 8000)
                    except (BudgetOut, TooLongForLocal):
                        raise
                    except (worker_cloud.NoStrongModel, RuntimeError, ValueError) as e:
                        reply = ""
                        steps.info("The request's own tests weren't corrected: %s.", e)
                    funcs, imports = corrected_funcs(reply, set(todo))
                    kept_c, refused_c = {}, []
                    for n, fsrc in funcs.items():            # each one under the guards that never weaken a test
                        old_f = textwrap.dedent(ast.get_source_segment(before, nodes[n]) or "")
                        try:
                            fewer = _assert_count(old_f) > _assert_count(fsrc)
                        except SyntaxError:
                            fewer = True
                        why = "it takes out an assertion" if fewer else added_skips(old_f, fsrc)
                        if why:
                            refused_c.append(f"{n} ({why})")
                        else:
                            kept_c[n] = fsrc
                    after = before
                    if kept_c:
                        try:
                            after = replace_funcs(before, kept_c, imports)
                            ast.parse(after)
                            weaker = {w.split(":")[0].split()[0] for w in weakened_tests(before, after)}
                            if weaker & set(kept_c):             # checks less than before: that one stays as it was
                                refused_c += [f"{n} (it checks less than before)" for n in sorted(weaker & set(kept_c))]
                                kept_c = {n: s for n, s in kept_c.items() if n not in weaker}
                                after = replace_funcs(before, kept_c, imports) if kept_c else before
                            problems_before = len(_own_problems(REQUEST_TESTS))
                            target.write_text(after, encoding="utf-8")
                            if len(_own_problems(REQUEST_TESTS)) > problems_before:   # calls the code wrongly
                                refused_c.append("all of them (they add build problems)")
                                kept_c, after = {}, before
                                target.write_text(before, encoding="utf-8")
                        except SyntaxError:
                            refused_c.append("all of them (the file didn't parse)")
                            kept_c, after = {}, before
                            target.write_text(before, encoding="utf-8")
                    written[REQUEST_TESTS] = after
                    if kept_c:
                        notes.append(f"`{REQUEST_TESTS}`: corrected {', '.join(sorted(kept_c))} once, before the fix "
                                     "rounds, because the request says its own tests are wrong: "
                                     + "; ".join(f'"{s[:160]}"' for s in said[:2]))
                    if refused_c or len(funcs) < len(todo):
                        notes.append(f"`{REQUEST_TESTS}`: not corrected: " + ", ".join(
                            refused_c + [f"{n} (no correction came back)" for n in todo if n not in funcs]))
                    smithy_diagnose.record(hist, "request_tests_corrected", tests=sorted(kept_c),
                                           refused=refused_c[:20], asked=todo)
        elif (checks_asked := _request_checks(items)):
            steps.info("Writing %s: one test for each of the request's %d checks and rules.", REQUEST_TESTS,
                       len(checks_asked))
            try:
                with worker_shared.strong_models(float(ctx.cfg.get("rewrite_min_model_b", 70) or 0)):
                    body = _strip_outer_fence(ctx.chat(_request_tests_messages(items, written, checks_asked), 8000),
                                              False)
                ast.parse(body)
                if "unittest" not in body or not re.search(r"(?m)^\s+def test_\w+", body):
                    raise ValueError("it came back with no unittest tests")
            except (BudgetOut, TooLongForLocal):
                raise
            except (worker_cloud.NoStrongModel, SyntaxError, ValueError, RuntimeError) as e:
                steps.info("%s wasn't written: %s.", REQUEST_TESTS, e)
                notes.append(f"**`{REQUEST_TESTS}` wasn't written** ({e}), so only the build's own tests ran.")
            else:
                target.write_text(body, encoding="utf-8")
                written[REQUEST_TESTS] = body
                code.append(REQUEST_TESTS)
                # checked before it's locked: one that calls the code wrongly is written again once (Nov 18, task
                # 60: test_3a called record(sentence) where record takes four arguments, no fix was allowed, and
                # the build problem kept every test from running)
                bad = _own_problems(REQUEST_TESTS)
                if bad:
                    steps.info("%s calls the code wrongly (%d problem(s)); writing it again once.", REQUEST_TESTS,
                               len(bad))
                    try:
                        with worker_shared.strong_models(float(ctx.cfg.get("rewrite_min_model_b", 70) or 0)):
                            again = _strip_outer_fence(ctx.chat(
                                _request_tests_messages(items, written, checks_asked)
                                + [{"role": "assistant", "content": body},
                                   {"role": "user", "content": "The build check found these problems in it:\n- "
                                    + "\n- ".join(bad[:15]) + "\n\nWrite the whole file again with them fixed: "
                                    "call each function with the arguments its outline shows. Reply with the file "
                                    "only."}], 8000), False)
                        ast.parse(again)
                        target.write_text(again, encoding="utf-8")
                        if len(_own_problems(REQUEST_TESTS)) < len(bad):
                            body = written[REQUEST_TESTS] = again
                        else:
                            target.write_text(body, encoding="utf-8")
                    except (BudgetOut, TooLongForLocal):
                        raise
                    except (worker_cloud.NoStrongModel, SyntaxError, ValueError, RuntimeError):
                        target.write_text(body, encoding="utf-8")
                fake = never_fail(body)
                if fake:                                 # a test that checks nothing isn't a test (task 60's 3z)
                    body = written[REQUEST_TESTS] = drop_tests(body, fake)
                    target.write_text(body, encoding="utf-8")
                    notes.append(f"`{REQUEST_TESTS}`: {len(fake)} test{'s' if len(fake) != 1 else ''} that could "
                                 f"never fail left out ({', '.join(fake)}).")
                n_tests = len(re.findall(r"(?m)^\s+def test_\w+", body))
                notes.append(f"`{REQUEST_TESTS}`: {n_tests} tests written from the request's {len(checks_asked)} "
                             "checks and rules; the fix rounds don't change them.")
                smithy_diagnose.record(hist, "request_tests", tests=n_tests, checks=len(checks_asked))

    def check() -> tuple[dict[int, tuple[str, str]], list[str], list[str]]:
        ticks = _checklist(ctx, p, desc, plan, written, items, cap, check_log, done_path) if use_list else {}
        ticks = _no_change_no_done(ticks, written, found_path, _item_files(items, written))
        problems, checked = all_problems()
        smithy_diagnose.record(hist, "check", missing=[n for n, (st, _) in ticks.items() if st == "MISSING"],
                               not_said=[n for n, (st, _) in ticks.items() if st == "NOT SAID"],
                               problems=smithy_diagnose.problem_codes(problems),
                               files=hashlib.sha256("\0".join(f"{k}\0{written[k]}" for k in sorted(written))
                                                    .encode("utf-8")).hexdigest()[:16])
        return ticks, problems, checked

    def all_problems() -> tuple[list[str], list[str]]:
        """Every check that needs no model: the build check, then the JSON fields and calls the change asks for.
        The fix rounds use this too (Nov 16, task 56: they re-ran only build_problems, so the two missing record()
        calls were never sent to a file and 3 rounds passed with no fix asked for)."""
        for rel in code:                       # a repeated import inside a function: taken out, no model needed
            if rel.lower().endswith(".py") and rel in written:
                new, n = drop_repeated_imports(written[rel])
                if n:
                    inside(folder, rel).write_text(new, encoding="utf-8")
                    written[rel] = new
                    steps.info("%s: %d import%s inside a function repeated the file's own imports; taken out.",
                               rel, n, "" if n == 1 else "s")
        # unused imports: taken out by ruff's own safe fix, no model; never in a file the plan keeps the same
        for rel in smithy_checkers.drop_unused_imports(folder, [r for r in code if r not in same and r in written]):
            written[rel] = inside(folder, rel).read_text(encoding="utf-8")
            steps.info("%s: unused imports taken out (no model needed).", rel)
        problems, checked = build_problems(folder, code, stdlib_only, net_ok)
        if earlier:                            # what changed since the version this one was built from
            wanted = p.get("request", "") + "\n" + p.get("change", "")
            problems += misspelt_since(folder, earlier, code, wanted) + paths_off_base(folder, earlier, code, wanted)
        # an unused variable alone never holds a build back or starts a fix round (Nov 17, task 58: one F841 took 3
        # rounds and kept it from checking clean); it's fixed along with a file's other problems, else listed
        dead[:] = [x for x in problems if _DEAD_CODE.match(x) and x.split(" line ")[0] not in same]
        problems = [x for x in problems if not _DEAD_CODE.match(x)]
        # the JSON fields a change asks for, checked on every entry (task 56: the glossary had no ids for hours
        # while models said it did)
        jsons = [r for r in code if r.lower().endswith(".json")]
        asked: dict[str, set[str]] = {}
        for t in items.values():
            for j, fields in smithy_checkers.asked_fields(t, jsons).items():
                asked.setdefault(j, set()).update(fields)
        if asked:
            found = smithy_checkers.json_field_errors(folder, asked)
            if found is None:
                checked.append("JSON fields not checked (jsonschema isn't installed)")
            else:
                problems = problems + found
                checked.append("JSON fields the change asks for checked ("
                               + ", ".join(f"{j}: {', '.join(sorted(f))}" for j, f in asked.items()) + ")")
        # the calls a change says a file makes ("academy_explain and academy_readme call record()"), with ast
        # (task 56: a model's check said done with no record() call in either file)
        pys = [r for r in code if r.lower().endswith(".py")]
        calls: dict[str, set[str]] = {}
        for t in items.values():
            for f, names in smithy_checkers.asked_calls(t, pys).items():
                calls.setdefault(f, set()).update(names)
        if calls:
            problems = problems + smithy_checkers.call_errors(folder, calls)
            checked.append("calls the change asks for checked ("
                           + ", ".join(f"{f}: {', '.join(n + '()' for n in sorted(c))}" for f, c in calls.items())
                           + ")")
        return problems, checked

    same = {f["path"] for f in (plan or {}).get("files", []) if f.get("change") == "same"}
    dead: list[str] = []                       # unused imports/variables: never a build problem on their own

    def files_for(n: int) -> list[str]:
        """The files to fix for item n, at most 3: the ones the check's note names, else the ones the whole item
        names (its "Files:" lines, names without .py), in the order they're named; else the longest. Never a file
        the plan keeps the same (Nov 15, task 56: the notes named no file, the item's first line neither, so all
        three items went to academy_sources.py, the longest, which the request said to leave as it is). Never the
        tests written from the request, which no fix changes (Nov 17, task 58: a missing change went there, refused),
        unless the request asks for its own tests to change (request_retests; Nov 20, task 69)."""
        shut = set() if retest else {REQUEST_TESTS}
        pick = [r for r in code if r not in same and r not in shut] or [r for r in code if r not in shut]
        for text in (ticks[n][1], items[n]):
            found = _names(text, [{"path": r} for r in pick], stems=True)
            if found:
                low = text.lower()

                def at(r):
                    p = PurePosixPath(r)
                    i = low.find(p.name.lower())
                    return i if i >= 0 else low.find(p.stem.lower())
                return sorted(found, key=at)[:3]
        return [max(pick, key=lambda r: len(written[r]))] if pick else []

    if use_list:
        steps.info("Checking the %d changes asked for%s against the files.", len(items), smithy_sandbox.about())
    # a limit reached part way (the task's cloud tokens, a step too long for this PC) ends the checks,
    # not the task: what's true so far is reported, NOT COMPLETE (Nov 14, task 38 failed instead)
    stopped = ""
    ticks: dict[int, tuple[str, str]] = {}
    try:
        ticks, problems, checked = check()
        rnd, limit = 0, rounds
        while rnd < limit:
            rnd += 1
            missing = [n for n, (state, _) in ticks.items() if state == "MISSING"]   # NOT SAID isn't fixed blind
            if not missing and not problems:
                break
            steps.info("Check round %d of %d%s: %s; asking for fixes.", rnd, limit, smithy_sandbox.about(), ", ".join(filter(None, [
                f"{len(missing)} change{'s' if len(missing) != 1 else ''} not in the files yet" if missing else "",
                f"{len(problems)} build problem{'s' if len(problems) != 1 else ''}" if problems else ""])))
            if missing:                                      # each to its own file, a few at a time
                by_file: dict[str, list[int]] = {}
                first: set[str] = set()      # each item's first file must change: no edits there isn't an answer
                for n in missing:            # (Nov 16, task 56: notes naming no file let every file say no edits)
                    for i, rel in enumerate(files_for(n)):
                        by_file.setdefault(rel, []).append(n)
                        if i == 0:
                            first.add(rel)
                before = dict(written)
                for rel, nums in by_file.items():
                    for k in range(0, len(nums), 3):
                        part = nums[k:k + 3]
                        found = "\n".join(f"- {ticks[n][1]}" for n in part if ticks[n][1])
                        here = bool(_names(found, [{"path": rel}], stems=True)) or rel in first   # named, or first
                        fix(rel, "Make only these changes, which aren't in the file yet:\n\n"
                            + "\n\n".join(items[n] for n in part)
                            + (f"\n\nWhat the check of the files found missing:\n{found}" if found else "")
                            + (f"\n\nThe check found this missing in {rel}, so send the edits it needs; "
                               if here else "\n\n")
                            + "a part that belongs only in another file is made there, not here", must=here)
                if written == before and not problems:
                    # nothing went in, so checking again would give the same answer (task 40 asked again and
                    # got an empty reply that wiped out the first one's 6 DONE)
                    steps.info("No fix went in this round, so the last check stands.")
                    break
            if problems:
                problems, checked = all_problems()   # again: the edits may have changed them
                problems = problems + aimed_call_problems(problems, written, {REQUEST_TESTS})   # Nov 20, task 69
                for rel in code:
                    mine = [x for x in problems if x.startswith(rel + " ")]
                    if mine:
                        mine += [x for x in dead if x.startswith(rel + " ")]     # tidied along with the rest
                        # the change's own words for this file, so "never calls record()" says what record() is
                        # and when it's called (Nov 16, task 56)
                        asked = [items[n] for n in items
                                 if _names(items[n], [{"path": rel}], stems=True)] if use_list else []
                        fix(rel, "Fix only these problems the checks found (the build check, and the fields and "
                                 "calls the change asks for); every other line stays exactly as it is. If a class is "
                                 "defined twice across files, keep one. Fix the code itself: never by deleting or "
                                 "commenting out the import, call or test that found the problem (task 49 commented "
                                 "out an import of a missing function):\n- " + "\n- ".join(mine[:30])
                            + ("\n\nWhat the change asks for, where it names this file:\n\n" + "\n\n".join(asked[:3])
                               if asked else ""), must=True)
            old_ticks = ticks
            ticks, problems, checked = check()
            if (late := over_time(hist, ctx.cfg)):
                stopped = late
                break
            stuck = smithy_diagnose.no_progress(smithy_diagnose.read(hist))
            if stuck and stuck >= int(ctx.cfg.get("diagnose_stop_after", 2)) > 0:
                # the same things missing and the same problems after fixes that changed the files: more rounds
                # spend tokens on the same answer (Nov 16, self-diagnosis)
                steps.info("The last %d checks found the same things, though the fixes changed the files; "
                           "stopping the fix rounds here.", stuck + 1)
                notes.append(f"**Fix rounds stopped early:** the last {stuck + 1} checks found the same things "
                             "missing and the same problems, though the files changed.")
                break
            if rnd == limit < rounds + EXTRA_FIX_ROUNDS and smithy_diagnose.progressed(smithy_diagnose.read(hist)):
                # the last round put right what it was given and something else is left: one more round, up to
                # EXTRA_FIX_ROUNDS (Nov 18, task 59: the third round's fix brought a one-word error, prompt.ShowDialog()
                # for Show(), and the build ended NOT clean with no round left to put it right)
                limit += 1
                steps.info("The last round put right what it was given and something else is left; "
                           "one more round (at most %d).", rounds + EXTRA_FIX_ROUNDS)
            # a check that gave no answer for an item keeps the round before's answer, said as such
            again = "(from the check before the last fixes; the last check gave no answer)"
            ticks = {n: (old_ticks[n][0], old_ticks[n][1] if again in old_ticks[n][1]
                         else (old_ticks[n][1] + " " if old_ticks[n][1] else "") + again)
                     if s == "NOT SAID" and old_ticks.get(n, ("NOT SAID",))[0] != "NOT SAID" else (s, note)
                     for n, (s, note) in ticks.items()}
    except (BudgetOut, TooLongForLocal) as e:
        stopped = str(e)
        log.warning("build %d: checks stopped early: %s", job_id, stopped)
        steps.info("The checks stopped early: %s.", stopped)
        problems, checked = all_problems()    # no model needed for these
        # the last check's answers stand (a fix may have been tried since, unchecked); none yet: not checked
        ticks = {n: (s, note + " (a fix was tried, not checked again)" if s == "MISSING" else note)
                 for n, (s, note) in ticks.items()} if ticks else {n: ("NOT SAID", "") for n in items} if use_list else {}

    # a test judged wrong and corrected (in this run or an earlier one) is reviewed against the request by a strong
    # model, once per version of it: one that now checks less is put right before the tests run (Nov 18, task 60:
    # the corrected test checked one field of the saved entry, passed, and the run would have ended there)
    if only is None and not stopped and any(smithy_sandbox.is_test(r) for r in code):
        try:
            for n, d in sorted(verdicts.items()):
                if d.get("side") not in ("test", "both"):
                    continue
                rel = next((r for r in code if _is_test(r) and n in _func_nodes(written.get(r, ""))), "")
                if not rel:
                    continue
                src = ast.unparse(_func_nodes(written[rel])[n])
                mark = hashlib.sha256(_norm(src).encode("utf-8")).hexdigest()[:16]
                if d.get("reviewed") == mark:
                    continue
                got = review_test(ctx, request_text, n, src, "", float(ctx.cfg.get("rewrite_min_model_b", 70) or 0))
                if got is None:
                    continue                             # no strong model free: reviewed on the next run
                d["reviewed"] = mark
                if job_id:
                    keep = decisions(job_id)
                    keep.setdefault(n, d)["reviewed"] = mark
                    decided_path(job_id).write_text(json.dumps(keep, indent=1), encoding="utf-8")
                if got[0] != "weaker":
                    continue
                steps.info("%s: its correction checks less than the request asks (%s); putting it right.", n,
                           got[1][:160])
                notes.append(f"**`{n}` was corrected earlier but checked less than the request asks** ({got[1]}); "
                             "it's put right before the tests run.")
                fix(rel, f"{n} was judged wrong against the request and corrected, but it now checks less than the "
                         f"request asks: {got[1]}\n\nPut that back: make {n} check everything the request asks of it. "
                         + CORRECT_RIGHT + "\n\nWhat the request asks:\n\n" + request_text[:6000])
            problems, checked = all_problems()
        except (BudgetOut, TooLongForLocal) as e:
            stopped = str(e)

    # the tests, in a throwaway Windows Sandbox (smithy_sandbox; off unless sandbox_tests is on). Only for the
    # whole build, once it builds clean; a failure gets one fix round (sandbox_fix_rounds) and runs again.
    sandbox = {"status": "off", "tests": {}, "output": {}, "why": ""}
    # a problem the code runs with all the same (a name defined twice) doesn't keep the tests from running; it
    # still leaves the build NOT COMPLETE (Nov 17, task 58: one repeated import meant its tests never ran)
    if only is None and not stopped and not blocks_tests(problems) and any(smithy_sandbox.is_test(r) for r in code):
        stop = getattr(ctx, "_should_stop", None)
        escalated, strong = False, None
        try:
            for rnd in range(int(ctx.cfg.get("sandbox_fix_rounds", 1)) + 1):
                if rnd and (late := over_time(hist, ctx.cfg)):
                    stopped = late
                    break
                if (late := stalled(hist, ctx.cfg)):
                    # the last round's fixes weren't tested: the best round's files are what's kept
                    best = load_best(SMITHY.results_dir, job_id) if job_id else None
                    for r in (restore_best(best[1], folder, code) if best else []):
                        written[r] = (folder / r).read_text(encoding="utf-8-sig", errors="replace")
                    smithy_diagnose.record(hist, "stalled", why=late[:300])
                    stopped = late
                    break
                sandbox = smithy_sandbox.run_tests(job_id, folder, code, ctx.cfg, SMITHY.results_dir, stop, rnd + 1)
                skips_as_failures(sandbox, earlier, p.get("request", "") + "\n" + p.get("change", ""))
                fails = failing_names(sandbox)
                smithy_diagnose.record(hist, "sandbox", status=sandbox["status"], failing=sorted(fails),
                                       asserts={k: v for t in sandbox["tests"] for k, v in smithy_diagnose.assertions(
                                           sandbox.get("full", {}).get(t, sandbox["output"].get(t, ""))).items()})
                # a test that has failed in several runs in a row is locked: a test fix may not change it (Nov 16,
                # task 57: one that failed 4 times was narrowed until it passed)
                relock()
                # the round with the fewest failing tests is kept; a worse one goes back to it and the fixing stops
                # for now (Nov 20, task 67)
                if job_id and sandbox["status"] in ("passed", "failed"):
                    best = load_best(SMITHY.results_dir, job_id)
                    if best is None or len(fails) < best[0]:
                        save_best(SMITHY.results_dir, job_id, folder, code, len(fails))
                    elif len(fails) > best[0]:
                        back = restore_best(best[1], folder, code)
                        for r in back:
                            written[r] = (folder / r).read_text(encoding="utf-8-sig", errors="replace")
                        if back:
                            steps.info("The tests went backwards (%d failing, the best was %d); the best round's "
                                       "files are put back.", len(fails), best[0])
                            notes.append(f"**The tests went backwards ({len(fails)} failing; the best round had "
                                         f"{best[0]}), so the best round's files were put back** "
                                         f"({', '.join(f'`{r}`' for r in back[:8])}). Run it again to continue from there.")
                            smithy_diagnose.record(hist, "best_restored", failing=len(fails), best=best[0], files=back)
                            if not escalated and rnd < int(ctx.cfg.get("sandbox_fix_rounds", 1)):
                                # once per run: the next round, from the best files, goes to the stronger models
                                # only (Nov 20, task 67: a weak round broke more than it fixed, and the run ended)
                                escalated = True
                                least = float(ctx.cfg.get("escalate_min_b", 70))
                                strong = worker_shared.MIN_MODEL_B.set(least)
                                steps.info("Trying once more from the best round, on stronger models only.")
                                notes.append("**The next round, from the best round's files, goes to stronger models "
                                             "only.**")
                                smithy_diagnose.record(hist, "escalated", min_b=least)
                                continue
                            break
                # tests of the earlier version that passed there and fail now: the files this version changed that the
                # request doesn't name go back to the earlier version's
                if sandbox["status"] == "failed" and p.get("parent_id") and earlier is not None:
                    keep = protected_tests(earlier, parent_failing(SMITHY.results_dir, int(p["parent_id"])))
                    broke = sorted(fails & keep)
                    # needed by the request: files it names, and files its own tests import (the request names
                    # the confusion detector in words, and its tests import academy_confusion.py; Nov 20, task 67)
                    named_req = _names(p.get("request", "") + "\n" + p.get("change", ""),
                                       [{"path": r} for r in code], stems=True)
                    if REQUEST_TESTS in code:
                        named_req |= set(_imported_by(folder, REQUEST_TESTS, code))
                    named_req |= named_in_words(p.get("request", "") + "\n" + p.get("change", ""),
                                                [r for r in code if not smithy_sandbox.is_test(r)])
                    back = regression_files(folder, Path(earlier), code, named_req) if broke else []
                    if back:
                        for r in back:
                            shutil.copyfile(Path(earlier) / r, folder / r)
                            written[r] = (folder / r).read_text(encoding="utf-8-sig", errors="replace")
                        steps.info("%d test(s) of the earlier version broke; %d file(s) the request doesn't name "
                                   "are put back as they were.", len(broke), len(back))
                        notes.append(f"**{len(broke)} test{'s' if len(broke) != 1 else ''} of the earlier version "
                                     f"passed there and failed now ({', '.join(broke[:6])}), so "
                                     f"{', '.join(f'`{r}`' for r in back)} (not named by the request) "
                                     "went back to the earlier version's.**")
                        smithy_diagnose.record(hist, "unrequested_reverted", tests=broke, files=back)
                        # the result must be of the files as they are now: run the tests again (Nov 20, task 67: the
                        # run ended after the put-back, so the draft's state was never tested)
                        sandbox = smithy_sandbox.run_tests(job_id, folder, code, ctx.cfg, SMITHY.results_dir, stop,
                                                           rnd + 1)
                        skips_as_failures(sandbox, earlier, p.get("request", "") + "\n" + p.get("change", ""))
                        fails = failing_names(sandbox)
                        smithy_diagnose.record(hist, "sandbox", status=sandbox["status"], failing=sorted(fails),
                                               after="put back")
                        relock()
                        if job_id and sandbox["status"] in ("passed", "failed"):
                            again_best = load_best(SMITHY.results_dir, job_id)
                            if again_best is None or len(fails) < again_best[0]:
                                save_best(SMITHY.results_dir, job_id, folder, code, len(fails))
                if sandbox["status"] != "failed" or rnd >= int(ctx.cfg.get("sandbox_fix_rounds", 1)):
                    break
                before = {r: (folder / r).read_bytes() for r in code if (folder / r).exists()}
                for t, state in sandbox["tests"].items():
                    if state == "passed":
                        continue
                    out = sandbox["output"].get(t, "")
                    same = {f["path"] for f in plan.get("files", []) if f.get("change") == "same"}
                    # the files the traceback names, then the ones whose functions the failing tests call, then
                    # (neither found) the files the test imports
                    named = fix_files(folder, t, sandbox.get("full", {}).get(t, out), code, same)
                    shown = _failing_test_code(folder / t, sandbox.get("full", {}).get(t, out))
                    # what the change asks for, where it names these files or has lettered checks, so a test that
                    # disagrees with the request can be put right instead of the code being bent to it (Nov 16,
                    # task 56: the test's made-up README had 2 glossary terms where check (a) says three, and the
                    # fix made the lesson give 10 entries); the test file goes first, then the code
                    asked = [items[n] for n in items if sub_points(items[n])
                             or _names(items[n], [{"path": r} for r in named], stems=True)][:3]
                    wanted = ("\n\nWhat the change asks for (the request; it decides who's right):\n\n"
                              + "\n\n".join(asked)) if asked else ""
                    full = sandbox.get("full", {}).get(t, out)
                    if _none_ran(full):
                        wanted += ("\n\nNo test ran at all (\"Ran 0 tests\"). The cause is in this test file: most "
                                   "often unittest.main() inside a class or before the tests, or tests that aren't "
                                   "methods named test_... of a unittest.TestCase class. Fix only that.")
                    said, held, judged = "", [], False
                    failing = re.findall(r"(?m)^(?:FAIL|ERROR): (\w+)", full)
                    # a test that keeps failing is judged here, against the request, by a strong model: never left
                    # to the person using The Smithy (Nov 17, the user; task 58: a test wrong on Windows failed 7
                    # runs in a row, and the code fix rewired Python's own Path.__str__ to fit it)
                    srcs = _test_funcs(folder / t, [n for n in failing if n in to_judge])
                    pending = [n for n in failing if n in to_judge]
                    code_seen = {r: written[r] for r in named if r != t and r in written}
                    # the request's own words when it says its own tests are wrong, spelled out for the judge (Nov 21,
                    # task 70: 6 of 7 such tests were judged CODE, and the fixes went to code that can't satisfy them)
                    said_wrong = retest_sentences(p.get("change") or p.get("request", "")) \
                        if t == REQUEST_TESTS and retest else []
                    judge_request = request_text + (
                        "\n\nThe request itself says its own tests (in " + REQUEST_TESTS + ") are wrong and must change:\n- "
                        + "\n- ".join(said_wrong) + "\nA test here that does what those words rule out is wrong (TEST), "
                        "even when the code doesn't satisfy it either; judge CODE only for what the request asks of "
                        "the code." if said_wrong else "")
                    found = judge_tests(ctx, judge_request, {n: srcs.get(n, "") for n in pending}, full, code_seen,
                                        float(ctx.cfg.get("rewrite_min_model_b", 70) or 0)) if pending else {}
                    for n in pending:
                        verdict = found.get(n)
                        if verdict is None:
                            continue
                        side, why = verdict
                        verdicts[n] = record_decision(job_id, n, side, "smithy", why, len(smithy_diagnose.read(hist)),
                                                      verdicts)
                        smithy_diagnose.record(hist, "judged", test=n, side=side)
                        steps.info("%s: judged against the request: %s wrong (%s).", n,
                                   "the test and the code are both" if side == "both" else f"the {side} is", why[:160])
                        notes.append(f"**The Smithy judged `{n}`** (it failed in several runs in a row): the "
                                     + ("test is wrong, so it's corrected to match the request" if side == "test"
                                        else "test and the code are both wrong, so the test is corrected to the "
                                             "request and the code is fixed to it" if side == "both"
                                        else "code is wrong, so the code is fixed for it")
                                     + (f". {why}" if why else "."))
                    relock()
                    stuck_here = [n for n in failing if n in to_judge]       # not judged: no strong model free
                    wrong_here = [n for n in failing if n in wrong_tests]
                    # judged "code" or "both": the fix goes to the code files they call (fix_files_judged)
                    code_wrong = [n for n in failing if word.get(n) in ("code", "both") and n not in to_judge]
                    named = fix_files_judged(folder, t, named, code, same, bool(wrong_here), code_wrong,
                                             any(n not in code_wrong for n in failing))
                    both_here = [n for n in wrong_here if word.get(n) == "both"]
                    if wrong_here:                     # judged wrong: the test is corrected to the request
                        wanted += ("\n\nThese tests were checked against the request and found wrong: "
                                   + ", ".join(wrong_here) + ". Correct each one so it checks what the request asks "
                                   "for (the request decides), keeping its checks; the code isn't changed to fit them. "
                                   + CORRECT_RIGHT
                                   + "".join(f"\nWhy {n} is wrong: {verdicts[n].get('why')}" for n in wrong_here
                                             if verdicts.get(n, {}).get("why")))
                    if both_here:                      # the code is wrong too: fixed to the request, not the test
                        wanted += ("\n\nThe code is also wrong for " + ", ".join(both_here) + ": in a code file, fix "
                                   "it so it does what the request asks (not what the test as written expects).")
                    if stuck_here:
                        # not judged yet (no strong model was free): neither the test nor the code is changed for
                        # it this round, and it's judged on the next
                        smithy_diagnose.record(hist, "not_judged", test=t, tests=stuck_here)
                        wanted += ("\n\nThese tests have failed in several test runs in a row and aren't judged yet: "
                                   "don't change them, and don't change the code to fit them: "
                                   + ", ".join(stuck_here) + ".")
                        notes.append(f"**`{t}`: {', '.join(stuck_here)} failed in several test runs in a row.** No "
                                     "strong model was free to judge whether the test or the code is wrong, so "
                                     "neither was changed for them; they're judged on the next run.")
                        if not [n for n in failing if n not in to_judge]:
                            steps.info("%s: only tests not judged yet fail (%s); judged on the next run.",
                                       t, ", ".join(stuck_here))
                            continue
                    funcs_before = _test_funcs(folder / t, failing)
                    request_text = "\n".join(items.values()) or p.get("change") or p.get("request", "")
                    for rel in named[:4]:
                        if rel != t and named[0] == t and not judged:
                            judged = True
                            # each failing test on its own (Nov 16, task 57: one missed test fix held back all 5,
                            # with 2 real code bugs among them): one whose fix was tried (edits were sent) but came
                            # out the same is held back, so the code isn't bent to it (task 56: the test edits
                            # didn't match, a smaller model's rewrite kept the wrong test, then the code fix made the
                            # README lesson fit it); never one that uses the request's own words
                            funcs_after = _test_funcs(folder / t, failing)
                            held = [n for n in failing if n in funcs_before
                                    and _test_fix_missed(t, said, funcs_before[n], funcs_after.get(n, ""))
                                    and not _backed_by_request(funcs_before[n], request_text)
                                    and n not in locked and n not in wrong_tests]   # those have their own way
                            held_or_locked = held + [n for n in failing if (n in to_judge or n in wrong_tests)
                                                     and n not in held and word.get(n) != "both"]
                            if held:
                                smithy_diagnose.record(hist, "test_disagrees", test=t, tests=held)
                                notes.append(f"**`{t}`: {', '.join(held)} may disagree with the request.** A fix "
                                             "for the test was tried, but they came out the same, so the code wasn't "
                                             "changed to fit them this round. Check them against the request.")
                            left = [n for n in failing if n not in held_or_locked]
                            if failing and not left:
                                steps.info("%s: its fix was tried but the failing tests didn't change; the code isn't "
                                           "changed to fit them this round.", t)
                                break
                            if held:
                                steps.info("%s: %d failing test(s) held back (may disagree with the request); the "
                                           "code is fixed for the other %d.", t, len(held), len(left))
                            shown = "\n\n".join(funcs_after.get(n) or funcs_before.get(n, "") for n in left) or shown
                            if held:
                                wanted += ("\n\nDon't change the code to fit these tests, which may disagree with the "
                                           "request: " + ", ".join(held) + ".")
                        start = len(notes)
                        how = ("If a failing test's setup or the value it expects doesn't match what the change asks "
                               "for, correct the test to match the change (never to match the code, and never by "
                               "removing a check the change asks for). If the test is right, change nothing here."
                               if rel == t else
                               "Fix the cause in this file, so it does what the change asks for. Don't weaken or "
                               "remove the test, and don't bend the code to a test that disagrees with the change.")
                        asked = (f"{t} failed when it was run (in a throwaway Sandbox). Its last lines:\n{out}\n\n"
                                 + (f"The failing tests, as written in {t}:\n```python\n{shown}\n```\n\n" if shown else "")
                                 + how + wanted)
                        try:
                            fix(rel, asked, failing=failing if rel == t else ())
                        except worker_cloud.NoStrongModel as e:
                            if strong is None:
                                raise
                            # the stronger-models round found none free: the round goes on with the usual models
                            # instead of failing the task (Nov 20, task 68 failed here at 8:10 PM)
                            worker_shared.MIN_MODEL_B.reset(strong)
                            strong = None
                            steps.info("No stronger model is free right now (%s); this round goes on with the usual "
                                       "models.", e)
                            smithy_diagnose.record(hist, "escalation_dropped", why=str(e)[:300])
                            fix(rel, asked, failing=failing if rel == t else ())
                        if rel == t:
                            said = " ".join(notes[start:])
                if all((folder / r).exists() and (folder / r).read_bytes() == b for r, b in before.items()):
                    steps.info("No fix changed a file, so the last test run stands (not run again).")
                    break                    # task 55: a 10-minute Sandbox run again on the same files
                problems, checked = all_problems()
                if blocks_tests(problems):
                    break
        except (BudgetOut, TooLongForLocal) as e:
            stopped = str(e)
        except worker_cloud.NoStrongModel as e:          # any other step: the checks stop, the draft is kept
            stopped = str(e)
        finally:
            if strong is not None:
                worker_shared.MIN_MODEL_B.reset(strong)

    # what's true now, for the result and the README
    left, truth = [], []
    if stopped:
        left.append("the checks stopped early (" + stopped.split(";")[0] + ")")
        notes.append(f"**The checks stopped early:** {stopped}.")
    not_done = [n for n, (s, _) in ticks.items() if s != "DONE"]
    if problems:
        left.append(f"{len(problems)} build problem{'s' if len(problems) != 1 else ''}")
        notes.append(f"**Build check ({'; '.join(checked)}): {len(problems)} problem"
                     f"{'s' if len(problems) != 1 else ''} left:** " + "; ".join(problems[:15]))
        truth.append("It does NOT build yet. The build check found:\n- " + "\n- ".join(problems[:15]))
    elif checked:
        notes.append(f"**Build check passed:** {'; '.join(checked)}. Nothing was run.")
    if dead:
        notes.append(f"**Unused code left ({len(dead)}; it doesn't stop the build):** " + "; ".join(dead[:10]))
        truth.append("Build check passed (" + "; ".join(checked) + "). Nothing has been run.")
    else:
        notes.append("**Build check:** none of these file types can be checked automatically here.")
    # say exactly what wasn't checked (review step 4): The Smithy runs nothing, so tests the request
    # wants passing haven't been shown to pass, and the build isn't complete
    tests = [r for r in code if re.match(r"(?i)^(?:test_\w*|\w*_test)\.py$", Path(r).name)]
    ran = sandbox["status"] in ("passed", "failed", "timeout")
    if ran:
        said = ", ".join(f"`{t}` {s}" for t, s in sandbox["tests"].items())
        if sandbox["status"] == "passed":
            notes.append(f"**Tests passed in a throwaway Windows Sandbox** (networking off): {said}.")
            truth.append("Its tests were run in a throwaway Windows Sandbox and passed: " + said + ".")
        else:
            left.append(f"its tests {'failed' if sandbox['status'] == 'failed' else 'ran out of time'} in the Sandbox")
            notes.append(f"**Tests in a throwaway Windows Sandbox:** {said}. The last lines:\n\n"
                         + "\n\n".join(f"`{t}`:\n```\n{o}\n```" for t, o in sandbox["output"].items()
                                       if sandbox["tests"].get(t) != "passed"))
            truth.append("Its tests were run in a Sandbox and did NOT all pass: " + said + ". Say so plainly.")
    elif sandbox["status"] in ("busy", "unavailable") and tests:
        notes.append(f"**Tests not run in the Sandbox:** {sandbox['why']}.")
    # why the Sandbox didn't run them, when it's on (Nov 18, task 60: "The Smithy never runs code" was said when a
    # build problem in test_request.py had stopped them)
    held = blocks_tests(problems) if ctx.cfg.get("sandbox_tests") and only is None else []
    why_not = (f"{len(held)} build problem{'s' if len(held) != 1 else ''} stop them running ("
               + "; ".join(held[:3]) + ")" if held else
               "the checks stopped early" if stopped and ctx.cfg.get("sandbox_tests") else "")
    if ran:
        pass
    elif wants_tests:
        left.append(f"its tests weren't run ({why_not})" if why_not else
                    "its tests weren't run (The Smithy runs nothing; the request asks for them to pass)")
        notes.append("**Tests not run:** the request asks for "
                     + (", ".join(f"`{t}`" for t in tests) if tests else "its tests") + " to pass, and "
                     + (f"{why_not}, so the Sandbox didn't run them." if why_not else
                        "The Smithy never runs code, so that isn't shown."))
        truth.append("Its tests have NOT been run, so don't say they pass; say \"Tests not run yet\".")
    elif tests:
        notes.append("**Tests not run:** " + ", ".join(f"`{t}`" for t in tests)
                     + (f" ({why_not})." if why_not else " (The Smithy never runs code)."))
        truth.append("Its tests have NOT been run, so don't say they pass.")
    if ticks:
        done = [n for n in ticks if n not in not_done]
        unread = [n for n in not_done if ticks[n][0] == "NOT SAID"]
        if unread:
            left.append(f"{len(unread)} of {len(ticks)} changes couldn't be checked ({', '.join(map(str, unread))})")
        if len(unread) < len(not_done):
            left.append(f"{len(not_done) - len(unread)} of {len(ticks)} changes not done "
                        f"({', '.join(str(n) for n in not_done if n not in unread)})")
        notes.append(f"**Changes asked for:** {len(done)} of {len(ticks)} done"
                     + (". **Not done:** " + "; ".join(f"{n} ({ticks[n][1] or ('not checked: the checklist reply had no answer for it' if ticks[n][0] == 'NOT SAID' else ticks[n][0].lower())})" for n in not_done)
                        if not_done else "."))
        extra = unrequested_changes(str(p.get("request", "")) + "\n" + str(p.get("change", "")), plan)
        if extra:
            notes.append("**Changed, but the request doesn't name them:** " + ", ".join(f"`{x}`" for x in extra)
                         + ". Look at each: it should change only what a named file needs.")
        truth.append("Of the numbered changes asked for, these are DONE: " + (", ".join(map(str, done)) or "none")
                     + (". These are NOT done, so don't describe them as done; list them under \"Not done yet\": "
                        + "; ".join(items[n].split("\n")[0][:160] for n in not_done) if not_done else "."))
    if hist is not None:
        smithy_diagnose.record(hist, "end", left=left, stopped=stopped[:160])
        found = smithy_diagnose.self_check(smithy_diagnose.read(hist), items)
        notes.append("**Self-check:** " + ("nothing found." if not found else "\n- " + "\n- ".join(found)))
        if found:
            steps.info("Self-check: %s", " | ".join(x[:140] for x in found))
    if only is None:
        notes.append(_benchmarks(job_id, hist, problems, sandbox, left, time.monotonic() - started))
    steps.info("Checks clean." if not left else "Still not clean after %d round%s: %s.", *(
        () if not left else (rounds, "" if rounds == 1 else "s", "; ".join(left))))
    return notes, "\n".join(truth), left


def _readme_messages(p: dict, desc: str, plan: dict, written: dict[str, str], truth: str = "") -> list[dict]:
    flavour = ("\n\n" + JOOMLA) if p.get("joomla") else ""
    listing = "\n".join(f"- {path} ({len(body.splitlines())} lines)" for path, body in written.items())
    return [{"role": "system", "content": SYSTEM + flavour},
            {"role": "user", "content": f"The project: {redact(p['request'])}\n\nThe design:\n{desc[:4000]}\n\n"
                                        f"Its files:\n{listing}\n\n"
                                        + (f"What's checked to be true now:\n{truth}\n\n" if truth else "")
                                        + "Write its README.md: what it is, what each file "
                                        "does, where each file goes in the real project, how to install or use "
                                        "it, and anything to check first. Describe only what the files really do. "
                                        "Always include a \"How to run it\" section and, if there are tests, a "
                                        "\"How to test it\" section: numbered steps a beginner can follow (which "
                                        "folder to open, the exact command to type, what they should see when it "
                                        "works, and what a failure looks like). If a question was asked while "
                                        "planning (\"how do I run the tests?\"), make sure the README answers it. "
                                        "Write it in the tone of the request and any answers given while planning "
                                        "(plain and friendly for someone new, short and technical for a "
                                        "programmer), without leaving out anything that's checked to be true. "
                                        "Never guess a user name or a user folder (Nov 15: a README said "
                                        "C:\\Users\\the user, which isn't anyone's real folder): write paths from "
                                        "the user's own folder as %USERPROFILE%\\... (cmd) or $env:USERPROFILE\\... "
                                        "(PowerShell), or say \"the folder you copied it to\". Commands for "
                                        "Windows go in ```powershell blocks, never ```bash. "
                                        "Plain markdown, no outer code fence."}]


def _parent_build(parent_id: int) -> dict | None:
    """An earlier build's plan, files and README (for a new version)."""
    row = SMITHY.get(parent_id)
    if row is None or not row["artifact"] or not Path(row["artifact"]).is_dir():
        return None
    folder = Path(row["artifact"])
    plan_path = SMITHY.results_dir / f"{row['id']:05d}-plan.json"
    try:
        saved = json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    readme = folder / "README.md"
    return {"row": row, "folder": folder, "plan": saved["plan"], "desc": saved.get("desc", ""),
            "params": json.loads(row["params"] or "{}"),
            "readme": readme.read_text(encoding="utf-8", errors="replace") if readme.exists() else ""}


def first_params(parent_id: int) -> dict:
    """The first version's params (its request, joomla, path), found by walking back through the
    versions (a version's own params hold only its change: from the third version on, "The project:"
    lost what it was; Nov 14, review item 3)."""
    seen, pid = set(), parent_id
    while pid and pid not in seen:
        seen.add(pid)
        row = SMITHY.get(int(pid))
        if row is None:
            break
        pp = json.loads(row["params"] or "{}")
        if pp.get("request"):
            return pp
        pid = pp.get("parent_id")
    return {}


def _root_of(job_id: int) -> int:
    """The first task of a project's chain of versions (a follow-up's params hold its parent)."""
    seen, cur = set(), int(job_id)
    while cur not in seen:
        seen.add(cur)
        row = SMITHY.get(cur)
        pid = json.loads(row["params"] or "{}").get("parent_id") if row is not None else None
        if not pid:
            break
        cur = int(pid)
    return cur


def _project_key(job_id: int) -> str:
    """One key for a project: its first task's request text, so separate builds of the same request (Nov 14:
    tasks 37 and 39 to 42, all the Academy tool) count as one project, not five."""
    root = SMITHY.get(_root_of(job_id))
    request = json.loads(root["params"] or "{}").get("request", "") if root is not None else ""
    return " ".join(request.split()).lower()[:2000] or f"task {_root_of(job_id)}"


def open_work() -> dict:
    """What is waiting and what has gone stale, for a person who doesn't know to look (Nov 20 tidy): versions
    waiting for review that a later kept version of the same project replaced ("superseded"), and tasks that kept
    many fixes and never got better ("stuck"). Nothing is changed here."""
    rows = [r for r in SMITHY.by_status("review", "kept", limit=500) if r["type"] in ("build", "joomla", "followup")]
    kept_by_root: dict[str, int] = {}
    for r in rows:
        if r["status"] == "kept":
            root = _project_key(r["id"])
            kept_by_root[root] = max(kept_by_root.get(root, 0), r["id"])
    out: dict = {"waiting": [], "superseded": [], "stuck": []}
    for r in rows:
        if r["status"] != "review":
            continue
        later = kept_by_root.get(_project_key(r["id"]), 0)
        item = {"id": r["id"], "title": r["title"]}
        if later > r["id"]:
            out["superseded"].append({**item, "replaced_by": later})
            continue
        out["waiting"].append(item)
        stale = smithy_diagnose.kept_without_gain(smithy_diagnose.read(SMITHY.results_dir / f"{r['id']:05d}-history.jsonl"))
        if stale >= smithy_diagnose.NO_GAIN_LIMIT:
            out["stuck"].append({**item, "kept_fixes": stale})
    return out


def tidy_text(work: dict) -> str:
    lines = []
    for w in work["waiting"]:
        lines.append(f"Waiting for your review: task {w['id']}, {w['title']}")
    for s in work["superseded"]:
        lines.append(f"Replaced: task {s['id']} ({s['title']}) was replaced by kept task {s['replaced_by']}; "
                     "safe to discard")
    for s in work["stuck"]:
        lines.append(f"Stuck: task {s['id']} kept {s['kept_fixes']} fixes without getting better; a smaller "
                     "follow-up with just the failing parts will do better than another run")
    lines[:0] = finished_lately()
    if (again := restart_needed()):
        lines.insert(0, again)
    return chr(10).join(lines) or "Nothing is waiting."


def discard_superseded() -> list[int]:
    """Discards the replaced versions (their drafts too). Only on the user's say-so: smithy.py tidy --discard."""
    import shutil
    gone = []
    for s in open_work()["superseded"]:
        _mark(s["id"], "discarded", f"replaced by kept task {s['replaced_by']}")
        for p in SMITHY.results_dir.glob(f"{s['id']:05d}-*"):
            shutil.rmtree(p) if p.is_dir() else p.unlink(missing_ok=True)
        gone.append(s["id"])
    return gone


# ---------------------------------------------------------------- builds: read, ask, propose (Nov 14)
# the user: "Build for The Smithy to read a prompt, ask for clarification where needed, create a plan
# offered with a yes/no prompt and make any adaptations for further user input." A new build is a
# proposal first (build_ask_first, on): its questions, what doesn't fit together, what it will assume, and
# the plan, then it waits. The user's reply (smithy.py reply ID TEXT, or the app's Plan window): yes builds it
# with that plan, no drops it, anything else is answers or changes and gets a revised proposal.
def ask_first(cfg: dict, p: dict) -> bool:
    """Whether this build is proposed first (not a new version, not one the user already said yes to)."""
    return bool(cfg.get("build_ask_first", True)) and not p.get("no_ask") and not p.get("plan_task") \
        and not p.get("parent_id")


def proposal_path(job_id: int) -> Path:
    return SMITHY.results_dir / f"{int(job_id):05d}-proposal.json"


def load_proposal(job_id: int) -> dict | None:
    try:
        return json.loads(proposal_path(job_id).read_text(encoding="utf-8"))
    except Exception:
        return None


def _proposal_messages(p: dict, max_files: int, earlier: dict | None, facts: str) -> list[dict]:
    flavour = ("\n\n" + JOOMLA) if p.get("joomla") else ""
    ask = (f"Read this request carefully before anything is built.\n\nThe request: {redact(p['request'])}\n\n"
           + (f"What the files the request names really look like (read from this PC, values hidden; trust "
              f"these over any guess in the request):\n{facts}\n\n" if facts else "")
           + (f"Your earlier proposal for it:\n{earlier['text'][:8000]}\n\nThe answers and changes so far "
              "(the newest last; they override the request where they differ):\n- "
              + "\n- ".join(redact(a) for a in earlier["answers"]) + "\n\nRevise the proposal to fit them.\n\n"
              if earlier else "")
           + "Then reply in exactly two parts:\nSPOKEN SUMMARY:\nTwo or three plain sentences for reading aloud: "
           "what you'd build, and how many questions you have. No code or symbols.\nFULL RESULT:\nA short "
           "description of the design, then ONE ```json block like this:\n"
           '{"name": "short-folder-name", "files": [{"path": "src/main.py", "purpose": "what this file does"}], '
           '"questions": [{"q": "the question", "default": "what you will do if not told"}], '
           '"conflicts": ["..."], "assumptions": ["..."], "replies": ["..."]}\n'
           "- The person may be new to coding. Write everything they read in plain words: no function, module "
           "or file names in a question unless they can't be avoided, and say what a term means if you use one.\n"
           "- Match their tone (the user, Nov 14): answer the way they wrote. Casual or worried gets a friendly, "
           "reassuring reply; short and technical gets a short, technical one. The tone changes, never the "
           "facts: keep every limit, conflict and cost in, however you say it.\n"
           "- questions: only what you truly can't decide yourself and that changes what gets built (at most 5; "
           "none is fine). One plain question each, with the answer you'll use if you're not told. If the "
           "person says they don't understand, or to decide for them, don't ask it again: decide, and put it "
           "under assumptions.\n"
           "- replies: if their answers ask you something (\"does that mean it won't work?\"), answer it here, "
           "plainly and honestly, one entry per thing they asked. Empty if they asked nothing. Nothing has been "
           "built or run yet, so never promise that tests will pass or that the code will work (task 48 did): "
           "say what you'll do, and what they'd need to do to find out.\n"
           "- conflicts: places where the request contradicts itself, or asks for something that can't work "
           "as written (a test that can't pass with the code it describes, a rule the code would break), "
           "including where it disagrees with the real files above.\n"
           "- assumptions: what else you'll decide yourself, said plainly (for example \"the files it reads "
           "have these fields\"), so a wrong guess can be corrected before building.\n"
           f"At most {max_files} files. Paths are relative, with forward slashes, laid out like the real "
           "project. Text files only. Don't list a README.md: it's written last, automatically.")
    return [{"role": "system", "content": SYSTEM + flavour}, {"role": "user", "content": ask}]


def _list_of(raw: dict, key: str, most: int = 8) -> list[str]:
    v = raw.get(key) if isinstance(raw, dict) else None
    return [str(x).strip()[:400] for x in v if str(x).strip()][:most] if isinstance(v, list) else []


def _q(x) -> dict:
    """A question as {"q", "default"} (plans written before Nov 14 kept them as plain text)."""
    return x if isinstance(x, dict) else {"q": str(x), "default": ""}


def _questions_of(raw: dict) -> list[dict]:
    """[{"q", "default"}]: each question with the answer it will use if not told (a plain string: no default)."""
    out = []
    for x in (raw.get("questions") if isinstance(raw, dict) else None) or []:
        if isinstance(x, dict):
            q, d = str(x.get("q") or x.get("question") or "").strip(), str(x.get("default") or "").strip()
        else:
            q, d = str(x).strip(), ""
        if q:
            out.append({"q": q[:400], "default": d[:400]})
    return out[:5]


# -- what the files a request names really look like (suggestion 5, Nov 14: Academy's prompt guessed
# The Smithy's -check.json fields wrong). Only allowlisted files (worker_files, redacted), and from The
# Smithy's own results folder only the layout of its JSON files (keys and types, never values).
_NAMED_FILE = re.compile(r"[\w*.\-/\\]*\*?[\w.\-]*\.(?:json|md|txt|py|ps1|cs|csv|ini|yml|yaml|toml|xml|html|php|css|js)\b",
                         re.I)


def _layout(value, depth: int = 0) -> str:
    """A JSON value's shape: {"notes": list of text, "left": list, ...}, never its contents."""
    if isinstance(value, dict):
        if depth >= 2:
            return "an object"
        return "{" + ", ".join(f'"{k}": {_layout(v, depth + 1)}' for k, v in list(value.items())[:25]) + "}"
    if isinstance(value, list):
        if not value:
            return "a list (empty here)"
        inner = _layout(value[0], depth + 1)
        return "a list of objects" if inner == "an object" else f"a list of {inner}"
    return {bool: "true/false", int: "a number", float: "a number", str: "text"}.get(type(value), "null")


KNOWN_KEYS: dict[str, set[str]] = {}          # the last request_facts: each JSON file name -> every key it has


def _all_keys(value, depth: int = 0) -> set[str]:
    keys: set[str] = set()
    if depth > 3:
        return keys
    if isinstance(value, dict):
        for k, v in value.items():
            keys.add(str(k))
            keys |= _all_keys(v, depth + 1)
    elif isinstance(value, list) and value:
        keys |= _all_keys(value[0], depth + 1)
    return keys


_FIELD = re.compile(r"[`'\"](\w{2,40})[`'\"]|\b(?:field|key)s?\s+(?:called\s+|named\s+)?[`'\"]?(\w{2,40})|"
                    r"(?<![-\w])(\w{2,40})\s+(?:field|key)\b")


_HAS_LIST = re.compile(r"\b(?:has|have|contains?|with)\s+((?:[a-z_]\w*(?:\s*,\s*|\s+and\s+|\s+or\s+)?){1,6})")
_NOT_FIELDS = {"a", "an", "the", "list", "lists", "string", "strings", "key", "keys", "field", "fields", "value",
               "values", "top", "level", "each", "entry", "entries", "file", "files", "path", "paths", "one", "two",
               "some", "any", "all", "no", "only", "its", "their", "text", "number", "object", "objects", "array",
               "dict", "true", "false", "null", "data", "info", "information", "items", "item", "at", "least", "most"}


def layout_conflicts(prop: dict) -> list[str]:
    """A default answer or assumption that names a field a real file doesn't have (task 45 assumed `passed` in
    -check.json, though the plan showed notes, truth and left). Worked out in code, from request_facts."""
    out = []
    texts = [(f"Question {i}'s default", q["default"]) for i, q in enumerate(map(_q, prop.get("questions", [])), 1)
             if q.get("default")] + [("An assumption", a) for a in prop.get("assumptions", [])]
    for name, keys in KNOWN_KEYS.items():
        stem = name.lstrip("*-").lower()                     # "-check.json" -> "check.json"
        for who, text in texts:
            if stem not in text.lower() and stem.split(".")[0] not in text.lower():
                continue
            named = {g for m in _FIELD.finditer(text) for g in m.groups() if g}
            for m in _HAS_LIST.finditer(text):                 # "-check.json has notes and passed"
                named |= {w for w in re.split(r"\s*,\s*|\s+and\s+|\s+or\s+|\s+", m.group(1)) if re.fullmatch(r"[a-z_]\w+", w)}
            named -= _NOT_FIELDS
            named -= {stem, stem.split(".")[0], "json", "true", "false", "null", "new", "edit", "the", "a", "an", "each",
                  "one", "this", "that", "top", "level", "same", "its", "their", "string", "list", "text", "boolean"}
            wrong = sorted(k for k in named if k not in keys and not k.endswith("json"))
            if wrong:
                out.append(f"{who} uses {', '.join(f'`{k}`' for k in wrong)} in `{name}`, but the real file has: "
                           + ", ".join(f"`{k}`" for k in sorted(keys)[:12]) + ".")
    return out


def request_facts(request: str, limit_chars: int = 3500) -> str:
    """For each file name or pattern the request names that exists: where, and what it looks like."""
    KNOWN_KEYS.clear()
    import fnmatch
    names = []
    for m in _NAMED_FILE.finditer(request or ""):
        n = Path(m.group(0).replace("\\", "/")).name
        if n and n.lower() not in (x.lower() for x in names) and not n.lower().endswith("readme.md"):
            names.append(n)
    if not names:
        return ""
    own = [x for x in SMITHY.results_dir.glob("*") if x.is_file()] if SMITHY.results_dir.exists() else []
    allowed_files = None
    out = []
    for n in names[:6]:
        pat = n if "*" in n else n
        mine = sorted((x for x in own if fnmatch.fnmatch(x.name.lower(), pat.lower())
                       or (("*" not in n) and x.name.lower().endswith(n.lower()))),
                      key=lambda x: x.stat().st_mtime, reverse=True)
        if mine and mine[0].suffix.lower() == ".json":           # The Smithy's own: the layout only
            try:
                data = json.loads(mine[0].read_text(encoding="utf-8"))
                KNOWN_KEYS[n] = _all_keys(data)
                shape = _layout(data)
                out.append(f"- `{n}` (The Smithy's own task files, e.g. `{mine[0].name}`): {shape}")
            except Exception:
                pass
            continue
        if allowed_files is None:
            allowed_files = worker_files.candidates()
        hits = sorted((x for x in allowed_files if fnmatch.fnmatch(x.name.lower(), pat.lower())),
                      key=lambda x: x.stat().st_mtime, reverse=True)
        if not hits:
            continue
        ok, why = worker_files.allowed(hits[0])                   # allowlist, private names and folders
        if not ok and "too big" not in why:
            continue
        # a file too big for a review (BUGFIXES.md) still shows how it starts: its first 20 KB, keys hidden
        text = worker_files.redact(hits[0].read_text(encoding="utf-8", errors="replace")[:20_000])
        if hits[0].suffix.lower() == ".json":
            try:
                data = json.loads(text)
                KNOWN_KEYS[n] = _all_keys(data)
                out.append(f"- `{n}` (`{hits[0].name}`): {_layout(data)}")
            except Exception:
                pass
        else:
            heads = [l for l in text.splitlines() if l.startswith("#")][:6]
            first = "\n".join(text.splitlines()[:12])
            out.append(f"- `{n}` (`{hits[0].name}`): its headings look like: " + " | ".join(h[:90] for h in heads)
                       + "\n  It starts:\n  " + first[:900].replace("\n", "\n  "))
    return "\n".join(out)[:limit_chars]


# -- what The Smithy can't do here, said before the user says yes (suggestion 6)
_UNCHECKED_EXT = {".php", ".css", ".scss", ".js", ".jsx", ".ts", ".tsx", ".html", ".sql", ".sh", ".yml", ".yaml"}


def build_limits(plan: dict, request: str, cfg: dict) -> list[str]:
    out = []
    if _WANTS_TESTS.search(request or ""):
        out.append("The request asks for tests to pass. The Smithy never runs code, so the build will say "
                   "\"tests not run\" and finish NOT COMPLETE, however good the code is. That doesn't mean it's "
                   "broken: it means nobody has tried it yet. The tests are written for you, and running them "
                   "(one command, in the README) is how you find out whether it works.")
    unchecked = sorted({Path(f["path"]).suffix.lower() for f in plan.get("files", [])} & _UNCHECKED_EXT)
    if unchecked:
        out.append("These kinds of file can only be written, not checked here: " + ", ".join(unchecked) + ".")
    if len(plan.get("files", [])) >= int(cfg.get("max_build_files", 20)):
        out.append(f"The plan is at the most files a build may have ({cfg.get('max_build_files', 20)}), so some "
                   "may have been left out.")
    return out


# -- cost and time, before the user says yes (suggestion 2): its own token limit, and what earlier builds of
# about this size really used (each result's first line: "N cloud tokens, ... S s")
_SPENT = re.compile(r"([\d,]+) cloud tokens.*?(\d+) s,")


def _past_builds(n_files: int) -> list[tuple[int, int]]:
    out = []
    for r in SMITHY.by_status("review", "kept", "discarded", limit=200):
        if r["type"] not in ("build", "joomla", "followup") or not r["result_path"] or not r["artifact"]:
            continue
        try:
            plan = json.loads((SMITHY.results_dir / f"{r['id']:05d}-plan.json").read_text(encoding="utf-8"))
            first = Path(r["result_path"]).read_text(encoding="utf-8", errors="replace").split("\n", 1)[0]
        except Exception:
            continue
        m = _SPENT.search(first)
        if m and abs(len(plan["plan"]["files"]) - n_files) <= 3:
            out.append((int(m.group(1).replace(",", "")), int(m.group(2))))
    return out


BIG_FILES, BIG_RULES = 8, 6         # a plan with this many files, or a request with this many numbered rules, is big


def size_advice(plan: dict, request: str) -> str:
    """A plain warning when one request is more than a model of this size holds at once (Nov 20, task 67: seven
    rules over six modules; every fix broke another rule, 76 fixes over 28 hours). '' when it's a normal size."""
    files = len([f for f in plan.get("files", []) if not str(f.get("path", "")).startswith("test_")])
    rules = len(numbered_items(request))
    if files < BIG_FILES and rules < BIG_RULES:
        return ""
    return (f"This is a big one ({files} files, {rules} numbered rule{"" if rules == 1 else "s"}). A small model does better with about three "
            "files at a time, and each fix on a big build tends to break another part. Build the core first, then "
            "add the rest as follow-ups of it.")


def cost_line(plan: dict, p: dict, cfg: dict, budget: int) -> str:
    n = len(plan.get("files", []))
    cc = cfg.get("cloud") or {}
    est, _ = estimate_build_tokens(plan, p.get("request", ""), cfg, budget)
    least = int(cc.get("max_tokens_per_task", 200_000))
    limit = min(max(est, least), max(least, int(cc.get("auto_task_tokens_max", 600_000)))) \
        if cc.get("auto_task_tokens") else least
    past = sorted(_past_builds(n))
    line = f"Its cloud-token limit would be {limit:,}."
    if past:
        toks, secs = sorted(t for t, _ in past), sorted(s for _, s in past)
        mid = toks[len(toks) // 2]
        line += (f" {len(past)} earlier build{'s' if len(past) != 1 else ''} of about this size used "
                 f"{toks[0]:,} to {toks[-1]:,} (about {mid:,} in the middle) and took "
                 f"{max(1, round(secs[0] / 60))} to {max(1, round(secs[-1] / 60))} minutes. "
                 "Slower when the faster providers are used up for the day.")
    else:
        line += " There's no earlier build of about this size to compare with yet"
    line = line.rstrip(".") + "."
    if (big := size_advice(plan, p.get("request", ""))):
        line += " " + big
    if (now := cloud_note(cfg)):
        line += " " + now
    return line


def _clock(ts: float) -> str:
    t = time.localtime(ts)
    when = time.strftime("%I:%M %p", t).lstrip("0")
    if time.strftime("%Y-%m-%d", t) == time.strftime("%Y-%m-%d"):
        return when
    return "midnight" if when == "12:00 AM" else "tomorrow " + when


def cloud_note(cfg: dict) -> str:
    """Which providers in the switch order are out of free tokens now, in a sentence ('' = none, or the
    cloud isn't used). Said before a build starts (Nov 14, suggestion 4: a big build queued while every
    provider was out just waited)."""
    cc = cfg.get("cloud") or {}
    if str(cc.get("default", "")).lower() != "auto":
        return ""
    st = worker_cloud.switch_out(cfg)
    if not st["out"]:
        return ""
    names = [n for n, _ in st["out"]]
    who = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    back = _clock(st["back_ts"])
    if st["all_out"]:
        return (f"Right now every provider in the switch order ({who}) is out of free tokens until about {back}, "
                "so the build would mostly wait, or run much more slowly on this PC.")
    return f"Right now {who} {'is' if len(names) == 1 else 'are'} out of free tokens until about {back}, so it may be slower."


def plan_changes(before: dict, after: dict) -> list[str]:
    """What a revised plan changed, in plain words (so the user can see their answers took effect)."""
    out = []
    files_b = {f["path"] for f in (before.get("plan") or {}).get("files", [])}
    files_a = {f["path"] for f in after["plan"].get("files", [])}
    if files_a - files_b:
        out.append("New files: " + ", ".join(f"`{f}`" for f in sorted(files_a - files_b)) + ".")
    if files_b - files_a:
        out.append("No longer planned: " + ", ".join(f"`{f}`" for f in sorted(files_b - files_a)) + ".")
    qb = {_q(q)["q"] for q in before.get("questions", [])}
    qa = {_q(q)["q"] for q in after.get("questions", [])}
    if qb - qa:
        out.append(f"{len(qb - qa)} question{'s' if len(qb - qa) != 1 else ''} answered or dropped.")
    if qa - qb:
        out.append(f"{len(qa - qb)} new question{'s' if len(qa - qb) != 1 else ''}.")
    ab, aa = set(before.get("assumptions", [])), set(after.get("assumptions", []))
    for a in sorted(aa - ab)[:5]:
        out.append(f"Now assuming: {a}")
    cb, ca = set(before.get("conflicts", [])), set(after.get("conflicts", []))
    if cb - ca:
        out.append(f"{len(cb - ca)} thing{'s' if len(cb - ca) != 1 else ''} that didn't fit together "
                   f"{'are' if len(cb - ca) != 1 else 'is'} settled.")
    return out or ["Nothing in the plan changed; your answers are kept for the build."]


def proposal_text(prop: dict) -> str:
    """The proposal as the user reads it (the result, voice, and the app's Plan window)."""
    out = []
    if prop.get("changes"):
        out.append("## What changed since the last plan\n\n" + "\n".join(f"- {c}" for c in prop["changes"]))
    if prop.get("replies"):
        out.append("## Your questions answered\n\n" + "\n".join(f"- {r}" for r in prop["replies"]))
    if prop.get("limits"):
        out.append("## What The Smithy can't do here\n\n" + "\n".join(f"- {c}" for c in prop["limits"]))
    if prop["conflicts"]:
        out.append("## Things in the request that don't fit together\n\n" + "\n".join(f"- {c}" for c in prop["conflicts"]))
    if prop["questions"]:
        out.append("## Questions\n\n" + "\n".join(
            f"{i}. {q['q']}" + (f"\n   If you don't say: {q['default']}" if q.get("default") else "")
            for i, q in enumerate(map(_q, prop["questions"]), 1)))
    if prop["assumptions"]:
        out.append("## What I'll assume unless you say otherwise\n\n" + "\n".join(f"- {a}" for a in prop["assumptions"]))
    out.append(f"## The plan: {prop['plan'].get('name') or 'the build'}\n\n{prop['desc']}\n\n"
               + "\n".join(f"- `{f['path']}`: {f['purpose']}" for f in prop["plan"]["files"]) + "\n- `README.md`")
    if prop.get("cost"):
        out.append("## Cost and time\n\n" + prop["cost"])
    if prop.get("facts"):
        out.append("## What I read in the files the request names\n\n" + prop["facts"])
    if prop["answers"]:
        out.append("## Your answers and changes so far\n\n" + "\n".join(f"- {a}" for a in prop["answers"]))
    out.append("## Your reply\n\n**Yes** builds it with this plan. **No** drops it. Anything else is taken as "
               "answers or changes, and you get a revised plan.")
    return "\n\n".join(out)


def _build_budget(ctx) -> int:
    """Characters of earlier files a call shows (the same as run_build)."""
    budget = int(ctx.cfg.get("context_tokens", 8192)) * 3 // 2
    name = str(getattr(getattr(ctx, "backend", None), "name", ""))
    if "cloud" in name or name.startswith("switching"):
        budget = max(budget, int(ctx.cfg.get("cloud_build_chars", 64000)))
    return budget


def run_proposal(ctx, p: dict):
    """Read the request, ask what's unclear, propose a plan; nothing is built. Waits for the user's reply."""
    job_id = ctx.row["id"]
    max_files = int(ctx.cfg.get("max_build_files", 20))
    earlier = None
    if p.get("proposal_of"):                            # a revision: the proposal before, and every answer
        before = load_proposal(p["proposal_of"]) or {}
        earlier = {"text": before.get("text", ""), "answers": list(before.get("answers", [])) + [p.get("answer", "")]}
    request = p.get("request_given", p["request"])
    steps.info("Reading the request%s.", " again, with your answers" if earlier else "")
    facts = request_facts(request)
    if facts:
        steps.info("Read what the files the request names look like.")
    msgs = _proposal_messages(p, max_files, earlier, facts)
    answer = ctx.chat(msgs, 3000)
    try:
        summary, desc, plan = parse_plan(answer, max_files)
    except ValueError as problem:                       # one more try, saying what was wrong (as builds do)
        steps.info("The plan couldn't be read; asking again.")
        again = ctx.chat(msgs + [{"role": "assistant", "content": answer},
                                 {"role": "user", "content": f"I can't use that: {problem}. Reply with only the "
                                  "corrected ```json block (name, files, questions, conflicts, assumptions)."}], 3000)
        answer = answer + "\n\n" + again
        summary, desc, plan = parse_plan(answer, max_files)
    raw = {}
    for m in _JSON_BLOCK.finditer(answer):              # the last readable json block holds the lists
        try:
            raw = json.loads(m.group(1))
        except Exception:
            pass
    prop = {"request": request, "joomla": bool(p.get("joomla")), "path": p.get("path", ""),
            "questions": _questions_of(raw), "conflicts": _list_of(raw, "conflicts"),
            "assumptions": _list_of(raw, "assumptions"), "plan": plan, "desc": desc,
            "limits": build_limits(plan, request, ctx.cfg), "facts": facts, "replies": _list_of(raw, "replies", 5),
            "answers": earlier["answers"] if earlier else [], "revises": p.get("proposal_of"),
            "asked": time.time()}
    prop["conflicts"] += layout_conflicts(prop)        # a default that contradicts the real files (in code)
    if p.get("proposal_of"):                            # what the user's answers changed (suggestion 8)
        prop["changes"] = plan_changes(load_proposal(p["proposal_of"]) or {}, prop)
    try:
        prop["cost"] = cost_line(plan, {"request": request}, ctx.cfg, _build_budget(ctx))
    except Exception as e:                              # never stops the plan
        log.info("task %d: no cost line (%s)", job_id, e)
        prop["cost"] = ""
    prop["text"] = proposal_text(prop)
    proposal_path(job_id).write_text(json.dumps(prop, indent=2), encoding="utf-8")
    (SMITHY.results_dir / f"{job_id:05d}-plan.json").write_text(
        json.dumps({"summary": summary, "desc": desc, "plan": plan}, indent=2), encoding="utf-8")
    nq, nc = len(prop["questions"]), len(prop["conflicts"]) + len(prop["limits"])

    def things(k: int) -> str:
        return f"{k} thing{'s' if k != 1 else ''} to know first"
    steps.info("Plan ready for your OK: %d file%s, %d question%s%s.", len(plan["files"]),
               "" if len(plan["files"]) == 1 else "s", nq, "" if nq == 1 else "s", f", {things(nc)}" if nc else "")
    bits = [f"{nq} question{'s' if nq != 1 else ''}" if nq else "", things(nc) if nc else ""]
    spoken = (f"I've read the request{' again' if earlier else ''} and planned "
              f"{len(plan['files']) + 1} files" + (" with " + " and ".join(b for b in bits if b) if any(bits) else "")
              + ". It's waiting for your answer before anything is built.")
    full = (f"**Plan for your OK (Smithy task {job_id}): nothing has been built yet.**\n\n" + prop["text"]
            + f"\n\nReply with `smithy.py reply {job_id} yes`, `no`, or your answers, or use the Plan window "
              "in The Smithy app.")
    return spoken, full, None


def _queue_from_plan(prop: dict, params: dict) -> int:
    kind = "joomla" if prop.get("joomla") else "build"
    if prop.get("joomla"):
        params["joomla"] = True
    title = REGISTRY[kind].title(params) if kind in REGISTRY else ""
    return SMITHY.add(kind, params, requested_by="plan reply", title=title)


_GO_AHEAD = re.compile(r"\b(?:go\s+ahead|build\s+it|just\s+(?:build|do|make)\s+it|do\s+it|make\s+it\s+now|"
                       r"start\s+(?:it|building)|sounds?\s+good|looks?\s+good|that'?s\s+fine|fine\s+by\s+me|"
                       r"you\s+can\s+build|please\s+build|let'?s\s+go)\b", re.I)


# a "no" aimed at the building itself ("don't build it", "not yet"), not any "don't" ("I don't get the questions")
_GO_NOT = re.compile(r"\b(?:don'?t|do\s+not|not|never|no\s+need\s+to)\s+(?:\w+\s+){0,3}?(?:go\s+ahead|build|do\s+it|start|make\s+it)"
                     r"|\bnot\s+yet\b|^\W*no\b|\bwait\b|\bhold\s+on\b|\bstop\b", re.I)


def _mark(job_id: int, status: str, text: str) -> None:
    SMITHY._change("UPDATE jobs SET status=?, decided=?, decision_text=? WHERE id=? AND status='review'",
                   (status, time.time(), text[:300], job_id))


def reply_to_proposal(job_id: int, text: str, build_now: bool = False) -> str:
    """The user's reply to a proposal: yes -> the build is queued with that plan; no -> dropped; anything else ->
    a revised proposal is queued. build_now (the app's "Build with these answers"): the answers are added
    and it's built straight away, without a revised plan. Returns what was done, in plain words."""
    prop = load_proposal(job_id)
    row = SMITHY.get(job_id)
    if prop is None or row is None:
        raise ValueError(f"Smithy task {job_id} isn't a plan waiting for an answer")
    if row["status"] != "review":
        raise ValueError(f"Smithy task {job_id}'s plan was already answered ({row['status']})")
    text = (text or "").strip()
    if not text:
        raise ValueError("the reply is empty")
    verdict = None if build_now else worker_shared.answer(text) if len(text.split()) <= 4 else None
    if verdict is None and not build_now and _GO_AHEAD.search(text) and not _GO_NOT.search(text):
        # a longer reply that clearly says to go ahead (Nov 14 test as a new user: "ok I still don't get the
        # questions but go ahead and build it" got a third plan): built now, its words kept as answers
        build_now = True
    if verdict == "no":
        _mark(job_id, "discarded", text)
        return f"Dropped: Smithy task {job_id}'s plan won't be built."
    if verdict == "yes" or build_now:
        answers = list(prop["answers"]) + ([text] if build_now else [])
        defaults = [f"{q['q']} {q['default']}" for q in map(_q, prop["questions"]) if q.get("default")]
        agreed = ("\n\nAgreed while planning (these override the request where they differ):\n- "
                  + "\n- ".join(answers) if answers else "")
        assumed = ("\n\nAssumed in the plan you said yes to:\n- " + "\n- ".join(prop["assumptions"] + defaults)
                   if prop["assumptions"] or defaults else "")
        params = {"request": prop["request"] + agreed + assumed, "plan_task": job_id, "path": prop.get("path", "")}
        new = _queue_from_plan(prop, params)
        _mark(job_id, "kept", text)
        return (f"Building it with your answers: Smithy task {new}, with the plan from task {job_id}." if build_now
                else f"Building it: Smithy task {new}, with the plan from task {job_id}.")
    params = {"request": prop["request"], "proposal_of": job_id, "answer": text[:4000], "path": prop.get("path", "")}
    new = _queue_from_plan(prop, params)
    _mark(job_id, "kept", text)
    return f"Revising the plan with your answers: Smithy task {new}."


def carry_on(job_id: int, force: bool = False) -> str:
    """A stopped or failed build back in the queue, as the same task: it keeps its plan, every file already
    written (an empty one is written again) and its cloud-token count, and carries on from there (suggestion 2,
    Nov 14: task 51 was stopped when the day's free tokens ran out)."""
    row = SMITHY.get(job_id)
    if row is None:
        raise ValueError(f"there's no Smithy task {job_id}")
    if row["type"] not in ("build", "joomla", "followup"):
        raise ValueError(f"Smithy task {job_id} isn't a build")
    # a finished build that didn't check clean (NOT COMPLETE, waiting for review) may carry on too: its checks
    # and fix rounds run again on the files it has, and its README is written again (Nov 15, task 51)
    # so may one that says done but is still waiting for review, not kept yet (Nov 15, task 56: a wrong check
    # from this PC's model passed it; the user hadn't kept it, and continue refused it)
    unfinished = row["status"] == "review"
    if row["status"] not in ("cancelled", "failed") and not unfinished:
        raise ValueError(f"Smithy task {job_id} is {row['status']}, not stopped or waiting for review")
    if not (SMITHY.results_dir / f"{job_id:05d}-plan.json").exists():
        raise ValueError(f"Smithy task {job_id} stopped before it had a plan, so there's nothing to carry on")
    # a task that kept many fixes and never got better is stopped here, in plain words, not run for another day
    # (Nov 20, task 67); "continue ID --force" overrides
    hist = SMITHY.results_dir / f"{job_id:05d}-history.jsonl"
    stale = smithy_diagnose.kept_without_gain(smithy_diagnose.read(hist))
    if stale >= smithy_diagnose.NO_GAIN_LIMIT and not force:
        raise ValueError(f"Smithy task {job_id} has kept {stale} fixes since it last got better, so more of the same "
                         "won't help. Start a smaller follow-up with just the parts still failing, or add --force "
                         "to run it again anyway")
    # the same for test runs that stopped getting better (Nov 20, task 68)
    try:
        cfg = worker_shared.load_json(Path(__file__).with_name("smithy.json"))
    except Exception:
        cfg = {}
    why = stalled(hist, cfg)
    if why and not force:
        raise ValueError(f"Smithy task {job_id} stopped getting better: {why}")
    if force:
        smithy_diagnose.record(hist, "forced")         # the stall count starts again from here
    # its old result and check are made again at the end; the files and the plan are kept
    for old in (f"{job_id:05d}-check.json",):
        (SMITHY.results_dir / old).unlink(missing_ok=True)
    if unfinished and row["artifact"] and Path(row["artifact"]).is_dir():
        (Path(row["artifact"]) / "README.md").unlink(missing_ok=True)      # told the new truth when it's done
    if unfinished:                                     # a new effort: its cloud-token count starts again
        worker_cloud.reset_task_tokens(f"smithy:{job_id}")
    SMITHY._change("UPDATE jobs SET status='queued', error=NULL, finished=NULL, started=NULL, decided=NULL, "
                   "announced=NULL, priority=priority+1 WHERE id=? AND status IN ('cancelled','failed','review')",
                   (job_id,))
    return (f"Smithy task {job_id} is back in the queue; "
            + ("its checks and fixes run again on the files it has." if unfinished
               else "it carries on from the files it had already written."))


_CODE_DIR = Path(__file__).resolve().parent


def stalled(hist: Path | None, cfg: dict) -> str:
    """'' while the Sandbox runs still get better, else why the run stops, in plain words: stall_rounds (default
    smithy_diagnose.STALL_ROUNDS; 0 = off) runs in a row haven't beaten the best one, so the fixes aren't reaching
    the cause and more rounds only spend tokens. What keeps failing, and what the judges said, goes in the result."""
    limit = int(cfg.get("stall_rounds", smithy_diagnose.STALL_ROUNDS) or 0)
    if limit <= 0:
        return ""
    events = smithy_diagnose.read(hist)
    n = smithy_diagnose.rounds_without_gain(events)
    if n < limit:
        return ""
    runs = [e for e in events if e.get("kind") == "sandbox" and e.get("status") in ("passed", "failed")]
    counts = ", ".join(str(len(e.get("failing") or [])) for e in runs[-(n + 1):])
    return (f"{n} test runs in a row didn't beat the best one (failing tests: {counts}), so the fixes aren't "
            "reaching the cause; the best round's files are kept. A smaller follow-up aimed at the tests still "
            "failing will do better than more of the same (stall_rounds; continue --force runs it again anyway)")


def over_time(hist: Path | None, cfg: dict, now: float | None = None) -> str:
    """'' while this run of the task is within run_hours_max (default 4; 0 = off), else why it stops, in plain
    words. The run starts at the history's last "run" line (Nov 20, task 67: one local-model round took about 80
    minutes, and runs went on for hours with nothing to show for it)."""
    hours = float(cfg.get("run_hours_max", 4) or 0)
    if hours <= 0:
        return ""
    starts = [e.get("ts", 0) for e in smithy_diagnose.read(hist) if e.get("kind") == "run"]
    if not starts:
        return ""
    spent = ((now or time.time()) - starts[-1]) / 3600
    if spent < hours:
        return ""
    return (f"this run went over its time limit of {hours:g} hours (run_hours_max); the files so far are kept, "
            "and a smaller follow-up will do better than more of the same")


def _code_changed_at() -> float:
    """The newest change among the worker's own code files (what a restart would load)."""
    stamps = [p.stat().st_mtime for pat in ("smithy*.py", "worker_*.py") for p in _CODE_DIR.glob(pat)]
    return max(stamps, default=0.0)


_LOADED_AT = _code_changed_at()          # the code this worker started with


def restart_needed() -> str:
    """'' while the worker runs the code on disk; else a plain sentence, so nobody has to remember to restart
    after a fix (Nov 20: every fix needed a restart that had to be asked for, and each wait added hours)."""
    now = _code_changed_at()
    if now <= _LOADED_AT + 1:
        return ""
    when = time.strftime("%I:%M %p", time.localtime(now)).lstrip("0")
    return f"The Smithy's code changed at {when}; restart it to use the new version."


def note_restart(cfg: dict) -> None:
    """While idle: write (or remove) smithy-restart-needed.txt in the state folder, which `smithy.py tidy` and the app can show."""
    flag = _CODE_DIR / "state" / "smithy-restart-needed.txt"
    text = restart_needed()
    try:
        if text:
            flag.write_text(text + chr(10), encoding="utf-8")
        else:
            flag.unlink(missing_ok=True)
    except OSError:
        pass


def finished_lately(hours: float = 24, now: float | None = None) -> list[str]:
    """A plain line for each task that finished in the last `hours` and waits for review, newest first: the run
    ended, and how it stands (Nov 20: nobody was told when task 67 stopped; a watcher had to be written)."""
    since = (now or time.time()) - hours * 3600
    out = []
    for r in SMITHY.by_status("review", limit=50):
        if not r["finished"] or r["finished"] < since:
            continue
        when = time.strftime("%I:%M %p", time.localtime(r["finished"])).lstrip("0")
        head = " ".join((r["summary"] or "").split())[:160]
        out.append(f"Task {r['id']} ({r['title']}) finished at {when}: {head}")
    return out


def note_finished(cfg: dict) -> None:
    """While idle: smithy-finished.txt in the state folder says which runs ended lately, for the app to show."""
    flag = _CODE_DIR / "state" / "smithy-finished.txt"
    lines = finished_lately()
    try:
        if lines:
            flag.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
        else:
            flag.unlink(missing_ok=True)
    except OSError:
        pass


def on_idle(cfg: dict) -> None:
    """Everything The Smithy does while it has nothing queued."""
    note_restart(cfg)
    note_finished(cfg)
    answer_waiting_plans(cfg)


def answer_waiting_plans(cfg: dict) -> None:
    """build_ask_wait_hours (0 = off, the default): a plan nobody answered in that long is built on its own
    assumptions and default answers (suggestion 7: requests from other sessions, with nobody to answer).
    Called by the worker while it's idle (worker_engine.Profile.on_idle)."""
    hours = float(cfg.get("build_ask_wait_hours", 0) or 0)
    if hours <= 0:
        return
    for r in SMITHY.by_status("review", limit=50):
        prop = load_proposal(r["id"])
        if prop is None:
            continue
        asked = float(prop.get("asked") or r["finished"] or time.time())
        if time.time() - asked >= hours * 3600:
            said = reply_to_proposal(r["id"], "yes")
            log.info("task %d: no answer in %g hours, built on its assumptions: %s", r["id"], hours, said)
            steps.info("Smithy task %d's plan had no answer in %g hours, so it's being built on its own "
                       "assumptions: %s", r["id"], hours, said)


def estimate_build_tokens(plan: dict, request: str, cfg: dict, budget_chars: int,
                          parent_chars: int = 0, items: int = 0) -> tuple[int, str]:
    """About how many cloud tokens a build needs, from its plan (tokens in + out, every call), and how
    that was worked out. Rough by design: the files' sizes aren't known before they're written, so each
    is taken as about 4,500 characters (tasks 37 to 39: 4,700 to 5,000), or the earlier version's size.
    Each call reads the request, the plan and the files so far, up to budget_chars."""
    n = max(1, len(plan.get("files", [])))
    proj = parent_chars or n * 4500
    req = len(request) // 4 + 1500                            # the request, the plan, the instructions
    ctx_all = min(proj, budget_chars) // 4 + req               # a call that reads all of the files
    rounds = max(1, int(cfg.get("build_check_rounds", 3)))
    fixes = min(n, 4)                                          # edit calls a round (a few files need fixing)
    write = sum(min(proj * i // n, budget_chars) // 4 + req + 3000 for i in range(n))   # each file in turn
    checks = (rounds + 1) * (ctx_all + 4000)                   # the checklist, before and after each round
    edits = rounds * fixes * 1.5 * (ctx_all + 2000)            # fix edits, half asked again
    stage_size = max(1, int(cfg.get("build_stage_items", 3)))
    stages = -(-items // stage_size) if parent_chars and items > stage_size else 0
    staged = stages * (2 * (ctx_all + 2000) + int(cfg.get("build_stage_rounds", 2)) * (ctx_all + 4000 + 2 * (ctx_all + 2000)))
    total = int((3000 + write + checks + edits + staged + 4000) * 1.2)   # + the plan, the README, 20% spare
    why = (f"the plan: {n} file{'s' if n != 1 else ''}, {rounds} check round{'s' if rounds != 1 else ''}"
           + (f", {stages} stages" if stages else "") + ", about "
           + f"{ctx_all:,} tokens a call that reads every file")
    return total, why


def _set_own_limit(ctx, job_id: int, plan: dict, p: dict, budget: int, parent: dict | None) -> None:
    """cloud.auto_task_tokens: the build looks at its own plan and sets its own token limit, between
    max_tokens_per_task (the least) and auto_task_tokens_max (the most; the user's ceiling, default 600,000).
    Off, or no cloud: max_tokens_per_task as before."""
    cc = ctx.cfg.get("cloud") or {}
    if not cc.get("auto_task_tokens"):
        return
    import worker_cloud
    parent_chars = 0
    if parent:
        parent_chars = sum(len(x.read_text(encoding="utf-8", errors="replace"))
                           for x in parent["folder"].rglob("*") if x.is_file() and x.suffix.lower() != ".md")
    est, why = estimate_build_tokens(plan, p.get("request", ""), ctx.cfg, budget, parent_chars,
                                     len(numbered_items(p.get("change", ""))) if parent else 0)
    least = int(cc.get("max_tokens_per_task", 200_000))
    # each second key that's On adds another ceiling's worth (Nov 16, the user: a task stopped at 600,000 the
    # minute the user's second Cerebras key came in, which "defeats the purpose of the second key completely")
    extra = worker_cloud.second_keys_on(cc)
    most = max(least, int(cc.get("auto_task_tokens_max", 600_000))) * (1 + len(extra))
    limit = min(max(est, least), most)
    if extra:
        why += f"; the ceiling times {1 + len(extra)} for the second key{'s' if len(extra) != 1 else ''} " \
               f"({', '.join(worker_cloud.PROVIDERS[n].spoken for n in extra)})"
    note = ("" if est <= most else f"; the estimate was {est:,}, over the ceiling (auto_task_tokens_max)")
    worker_cloud.set_task_limit(limit, why)
    log.info("build %d: its own token limit: %d (estimated %d from %s)", job_id, limit, est, why)
    steps.info("This task's own token limit: %s cloud tokens (from %s%s).", f"{limit:,}", why, note)


def run_build(ctx, p: dict):
    """Plan, then each file, then the README. Picks up where it left off after a pause."""
    job_id = ctx.row["id"]
    max_files = int(ctx.cfg.get("max_build_files", 20))
    budget = int(ctx.cfg.get("context_tokens", 8192)) * 3 // 2      # characters of earlier files to show
    # a cloud model (or the switch order) reads far more than the laptop's local one: show it
    # enough of the earlier version to keep what it's told to keep (Nov 13, installer v4 saw 6,000
    # of v3's 27,000 characters and rewrote the rest). 64,000 characters is about 16,000 tokens,
    # under Cerebras's 30,000 tokens a minute with the answer.
    backend_name = str(getattr(getattr(ctx, "backend", None), "name", ""))
    if "cloud" in backend_name or backend_name.startswith("switching"):
        budget = max(budget, int(ctx.cfg.get("cloud_build_chars", 64000)))
    results = SMITHY.results_dir
    results.mkdir(parents=True, exist_ok=True)
    plan_path = results / f"{job_id:05d}-plan.json"
    parent = _parent_build(p["parent_id"]) if p.get("parent_id") else None
    if p.get("parent_id") and parent is None:
        raise ValueError(f"Smithy task {p['parent_id']} isn't a build I can make a new version of")
    if parent:
        first = {k: v for k, v in first_params(p["parent_id"]).items() if k in ("request", "joomla")}
        p = {**first, **parent["params"], **p, "request": (first.get("request") or
                                                          "(the first version's request wasn't found)")
             + f"\n\nNew version {p.get('version', '')}: {p['change']}"}

    # 0. a new build is a proposal first: questions and a plan, then it waits for the user's yes (Nov 14)
    if p.get("proposal_of") or (not parent and ask_first(ctx.cfg, p)):
        return run_proposal(ctx, p)
    if p.get("plan_task") and not plan_path.exists():     # the plan the user said yes to
        agreed = results / f"{int(p['plan_task']):05d}-plan.json"
        if agreed.exists():
            shutil.copyfile(agreed, plan_path)
            steps.info("Building with the plan from task %d, which you said yes to.", int(p["plan_task"]))

    # 1. the plan (kept, so a paused build carries on instead of starting again)
    if plan_path.exists():
        saved = json.loads(plan_path.read_text(encoding="utf-8"))
        summary, desc, plan = saved["summary"], saved["desc"], saved["plan"]
    elif parent and is_self_check(p.get("change", "")):
        # a self check needs no plan from a model: every file as it is, and The Smithy's own checks (no model) say
        # what to fix (Nov 19, task 61: "run your own self check for any bad code, bloat" was planned as a new
        # project, lost 5 of its 14 modules and kept the dead copy it was asked to find)
        files = [{**f, "change": "check"} for f in earlier_files(parent)]
        plan = {"name": parent["plan"].get("name", ""), "files": files}
        summary = (f"A self check of all {len(files)} files: every file is kept as it is, and only what The Smithy's "
                   "own checks find is fixed.")
        desc = ("A self check: The Smithy's own checks, with no model, look through every file (unused code and "
                "copies, lint, types, the tests); only what they find is fixed, and behaviour and the file list stay "
                "as they are.")
        plan_path.write_text(json.dumps({"summary": summary, "desc": desc, "plan": plan}, indent=2), encoding="utf-8")
        steps.info("A self check: all %d files kept as they are; The Smithy's own checks say what to fix.", len(files))
    else:
        ctx.step(0.0, 0.10)
        steps.info("Planning the %s.", "new version" if parent else "build")
        msgs = _plan_messages(p, max_files, parent)
        answer = ctx.chat(msgs, 1200)
        try:
            summary, desc, plan = parse_plan(answer, max_files)
        except ValueError as problem:
            # one more try, saying what was wrong (small models sometimes drift)
            log.info("task %d: plan not usable (%s); asking again", job_id, problem)
            steps.info("The plan couldn't be read; asking again.")
            again = ctx.chat(msgs + [{"role": "assistant", "content": answer},
                                     {"role": "user", "content": f"I can't use that plan: {problem}. Reply with only "
                                      "the corrected ```json block (name and files), with real relative paths, "
                                      "nothing else."}], 3000)   # room for models that think first
            try:
                try:
                    summary, desc, plan = parse_plan(answer + "\n\n" + again, max_files)
                except ValueError:
                    # the first answer's broken plan block comes first and wins: keep its words,
                    # drop its code blocks, and read the corrected plan (Nov 13: tasks 3 and 5)
                    words = re.sub(r"```[\s\S]*?(?:```|$)", "", answer)
                    summary, desc, plan = parse_plan(words + "\n\n" + again, max_files)
            except ValueError:
                # kept so it can be looked at; the task fails with the reason
                (results / f"{job_id:05d}-plan-unreadable.txt").write_text(
                    answer + "\n\n---\n\n" + again, encoding="utf-8")
                raise
        if parent:
            plan["name"] = parent["plan"].get("name") or plan["name"]
            _keep_unasked(plan, parent, p.get("change", ""))
            if plan.get("kept_unplanned"):
                steps.info("The plan left out %s; nothing asks for %s to go, so %s kept as %s.",
                           ", ".join(plan["kept_unplanned"]), "them" if len(plan["kept_unplanned"]) > 1 else "it",
                           "they're" if len(plan["kept_unplanned"]) > 1 else "it's",
                           "they were" if len(plan["kept_unplanned"]) > 1 else "it was")
        plan_path.write_text(json.dumps({"summary": summary, "desc": desc, "plan": plan}, indent=2), encoding="utf-8")
        steps.info("Plan ready: %d file%s: %s, and the README.", len(plan["files"]), "" if len(plan["files"]) == 1 else "s",
                   ", ".join(f["path"] for f in plan["files"]))
    # folders and run-time files out of the plan, a saved one too (a task carried on keeps its plan)
    plan["files"], left_out = not_build_files(plan["files"])
    if left_out:
        steps.info("Left out of the plan (not files of the build): %s.", "; ".join(left_out))
        if "**Left out (not files of the build):**" not in desc:
            desc += "\n\n**Left out (not files of the build):** " + "; ".join(left_out)
        plan_path.write_text(json.dumps({"summary": summary, "desc": desc, "plan": plan}, indent=2), encoding="utf-8")
    if not plan["files"]:
        raise ValueError("the plan has no files of the build left (" + "; ".join(left_out) + ")")
    smithy_sandbox.ABOUT.set(project_words(plan.get("name") or p.get("project") or ""))
    _set_own_limit(ctx, job_id, plan, p, budget, parent)     # set again after a pause: begin_task clears it
    version = int(p.get("version", 1))
    name = plan["name"] or slug(p["request"])
    folder = results / (f"{job_id:05d}-{name}" + (f"-v{version}" if version > 1 else ""))
    folder.mkdir(parents=True, exist_ok=True)

    # 2. each file in turn, with the plan and the files so far as context
    written: dict[str, str] = {}
    refused_files: list[str] = []
    cut_files: list[str] = []
    edit_notes: list[str] = []
    # an earlier file this long is changed by edits, not written again (0 turns it off)
    edit_min = int(ctx.cfg.get("edit_min_chars", 4000)) or 10 ** 9
    # A. a version with many numbered changes is built a few at a time (Nov 14: v14 got all 11 at once
    # and made 4). The long files that are edited start as the earlier version; the stages edit them.
    stage_size = max(1, int(ctx.cfg.get("build_stage_items", 3)))
    all_items = numbered_items(p.get("change", "")) if parent else {}
    staged = [f["path"] for f in plan["files"]
              if len(all_items) > stage_size and f.get("change") != "same" and (parent["folder"] / f["path"]).exists()
              and len(inside(parent["folder"], f["path"]).read_text(encoding="utf-8", errors="replace")) >= edit_min]
    n = len(plan["files"])
    # the earlier version's Python files: what a build edit may not take away is looked for there too, since the
    # tests are written after the code (Nov 20, task 69)
    prev_py = {f["path"]: inside(parent["folder"], f["path"]).read_text(encoding="utf-8", errors="replace")
               for f in plan["files"] if parent and f["path"].lower().endswith(".py")
               and (parent["folder"] / f["path"]).is_file()}
    for i, f in enumerate(plan["files"]):
        rel = f["path"]
        target = inside(folder, rel)
        old_path = inside(parent["folder"], rel) if parent and (parent["folder"] / rel).exists() else None
        if target.is_dir():                                           # never a folder written as a file
            refused_files.append(f"{rel} (a folder)")
            continue
        if target.exists():                                           # done before a pause
            kept = target.read_text(encoding="utf-8", errors="replace")
            if len(kept.strip()) >= 40 or rel.lower().endswith(".md"):
                written[rel] = kept
                continue
            target.unlink()                                           # (nearly) empty: written again
        target.parent.mkdir(parents=True, exist_ok=True)
        if f.get("change") in ("same", "check") and old_path is not None:
            steps.info("%s %s: copied.", rel, "stays the same" if f.get("change") == "same" else "is checked as it is")
            shutil.copyfile(old_path, target)
            written[rel] = target.read_text(encoding="utf-8", errors="replace")
            continue
        if rel in staged:                                             # the stages below change it
            shutil.copyfile(old_path, target)
            written[rel] = target.read_text(encoding="utf-8", errors="replace")
            continue
        span = 0.40 if staged else 0.80
        ctx.step(0.10 + span * i / n, 0.10 + span * (i + 1) / n)
        old = old_path.read_text(encoding="utf-8", errors="replace") if old_path else None
        if old is not None and len(old) >= edit_min:
            body, note = _edit_file(ctx, job_id, _edit_messages(p, desc, plan, rel, written, old), rel, old,
                                    others=written, callers={**prev_py, **written})
            edit_notes.append(note)
            target.write_text(body, encoding="utf-8")
            written[rel] = body
            continue
        msgs = _file_messages(p, desc, plan, rel, written, budget, old)
        steps.info("Writing %s (file %d of %d).", rel, i + 1, n)
        text = ctx.chat(msgs, 3000)
        # a reply cut off part way (a code block opened but never closed: the model or the provider
        # hit its length limit): ask it to carry on where it stopped, a few times
        for _ in range(4):
            if rel.lower().endswith(".md") or text.count("```") % 2 == 0:
                break
            # cut back to the last whole line first: models resume at the next line, so a line cut
            # in the middle lost its end (Nov 13, installer v2 and v3: "rtbOutput.ScrollTo" + the next
            # method). Then name that line, so it carries on from just after it.
            if "\n" in text and not text.endswith("\n"):
                text = text[:text.rindex("\n") + 1]
            last = next((l for l in reversed(text.split("\n")) if l.strip()), "")
            steps.info("%s was cut off part way; asking it to carry on.", rel)
            more = ctx.chat(msgs + [{"role": "assistant", "content": text},
                                    {"role": "user", "content": "That was cut off. The last complete line was:\n"
                                     + last + "\nContinue with the line right after it. Don't start the file over: "
                                     "no repeating, no explanation, and close the code block when the file is "
                                     "done."}], 6000)                   # room for models that think first
            text = _join_continued(text, more)
        if not rel.lower().endswith(".md") and text.count("```") % 2:   # still cut off: say so, don't hide it
            log.warning("build %d: %s is still cut off after 4 tries to continue", job_id, rel)
            steps.info("%s is still cut off after 4 tries; it's marked as needing finishing.", rel)
            cut_files.append(rel)
        body = _strip_outer_fence(text, rel.lower().endswith(".md"))
        if len(body.strip()) < 40:                      # an empty reply (task 49: academy_lesson.py, 2 bytes,
            steps.info("%s came back empty; asking again.", rel)   # saved as "written: 1 line")
            text = ctx.chat(msgs + [{"role": "assistant", "content": text or "(nothing)"},
                                    {"role": "user", "content": f"That reply had no code in it. Write the complete "
                                     f"file {rel} now, in one code block."}], 6000)
            body = _strip_outer_fence(text, rel.lower().endswith(".md"))
            if len(body.strip()) < 40:
                log.warning("build %d: %s left out (empty twice)", job_id, rel)
                steps.info("%s left out: it came back empty twice.", rel)
                refused_files.append(rel)
                continue
        if _refusal(body):                              # the model couldn't write it: leave it out, say so
            log.warning("build %d: %s left out (the model said it can't write it)", job_id, rel)
            steps.info("%s left out: the model said it can't write it.", rel)
            refused_files.append(rel)
            continue
        target.write_text(body, encoding="utf-8")
        written[rel] = body
        nl = len(body.splitlines())
        steps.info("%s written: %d line%s.", rel, nl, "" if nl == 1 else "s")

    # 2b. the stages (A): a few numbered changes at a time, each stage checked (and fixed) before the
    # next starts. Kept after each stage, so a paused build carries on at the next one.
    stage_notes: list[str] = []
    if staged:
        stage_path = results / f"{job_id:05d}-stages.json"
        nums = sorted(all_items)
        chunks = [nums[k:k + stage_size] for k in range(0, len(nums), stage_size)]
        saved_stages = json.loads(stage_path.read_text(encoding="utf-8")) if stage_path.exists() else {}
        done_stages, stage_notes = int(saved_stages.get("done", 0)), list(saved_stages.get("notes", []))
        for s in range(done_stages, len(chunks)):
            ctx.step(0.50 + 0.36 * s / len(chunks), 0.50 + 0.36 * (s + 1) / len(chunks))
            part = {k: all_items[k] for k in chunks[s]}
            steps.info("Stage %d of %d: changes %s.", s + 1, len(chunks), ", ".join(map(str, chunks[s])))
            ps = {**p, "change": "\n\n".join(part.values())}  # only this stage's changes
            for rel in _stage_files(staged, written, ps["change"]):
                body, note = _edit_file(ctx, job_id, _edit_messages(
                    ps, desc, plan, rel, written, written[rel],
                    f"This is stage {s + 1} of {len(chunks)}: make only the changes above now (the others come in "
                    f"their own stages). If none of them belong in {rel}, send no edits", budget), rel, written[rel],
                    others=written, callers={**prev_py, **written})
                if body != written[rel]:
                    inside(folder, rel).write_text(body, encoding="utf-8")
                    written[rel] = body
                stage_notes.append(f"stage {s + 1}: {note}")
            notes_s, _, left_s = _verify_build(ctx, job_id, ps, desc, plan, folder, written, budget, only=part,
                                               rounds=int(ctx.cfg.get("build_stage_rounds", 2)))
            stage_notes.append(f"stage {s + 1} (changes {', '.join(map(str, chunks[s]))}): "
                               + ("checks clean" if not left_s else "not clean yet: " + "; ".join(left_s)))
            stage_path.write_text(json.dumps({"done": s + 1, "notes": stage_notes}, indent=2), encoding="utf-8")
        steps.info("All %d stages done; checking the whole version.", len(chunks))

    # 3. the checks: keep fixing until it checks clean (kept, so a paused build doesn't check again)
    check_path = results / f"{job_id:05d}-check.json"
    readme = inside(folder, "README.md")
    if check_path.exists() and readme.exists():
        saved_check = json.loads(check_path.read_text(encoding="utf-8"))
        check_notes, truth, left = saved_check["notes"], saved_check["truth"], saved_check["left"]
    else:
        ctx.step(0.86, 0.90)
        # out of cloud tokens during the checks and fixes: stop with the files kept, not this PC's model
        # (stop_at_cloud_limit, default on; Nov 20, task 68)
        with worker_shared.no_local_after_limit(bool(ctx.cfg.get("stop_at_cloud_limit", True))):
            check_notes, truth, left = _verify_build(ctx, job_id, p, desc, plan, folder, written, budget)
        check_path.write_text(json.dumps({"notes": check_notes, "truth": truth, "left": left}, indent=2),
                              encoding="utf-8")

    # 4. the README, last, told what's true
    if not readme.exists():
        ctx.step(0.90, 0.95)
        old_readme = parent["readme"] if parent else ""
        text = None
        if len(old_readme) >= edit_min:
            # a new version: the earlier README edited, not written again (Nov 13, installer v10: the
            # code came back exactly right by edits, the rewritten README described another program)
            try:
                text, note = _edit_file(ctx, job_id, _edit_messages(
                    p, desc, plan, "README.md", written, old_readme,
                    "Change README.md so it describes this version: what the change wanted alters, and anything "
                    "in it that the files written for this version show is wrong. Describe only what the files "
                    "really do" + (f".\n\nWhat's checked to be true now:\n{truth}\n\nThe README must agree with "
                                   "this" if truth else ""), cap=budget), "README.md", old_readme, none_ok=False)
                edit_notes.append(note)
            except ValueError as e:                     # no usable edits: write it whole, as before
                log.info("build %d: README edits not usable (%s); writing it whole", job_id, e)
                steps.info("No usable README edits came back; writing it whole instead.")
        if text is None:
            steps.info("Writing the README.")
            try:
                text = _strip_outer_fence(ctx.chat(_readme_messages(p, desc, plan, written, truth), 1500), True)
            except (BudgetOut, TooLongForLocal) as e:     # a limit: a plain README from what's known, no model
                left = left + ["the README is a plain list (" + str(e).split(";")[0] + ")"]
                text = (f"# {name.replace('-', ' ')}\n\n{summary}\n\n## Files\n\n"
                        + "\n".join(f"- `{f['path']}`: {f.get('purpose', '')}" for f in plan["files"])
                        + f"\n\n## What's checked\n\n{truth or 'Nothing could be checked.'}\n\n"
                          "Written without a model, because a limit was reached: " + str(e) + ".\n")
        readme.write_text(text, encoding="utf-8")
    # 5. the request's README changes, checked against the README as written; what's missing gets one round of
    # edits, then is checked again, and anything still missing keeps the build NOT COMPLETE (Nov 18, task 60)
    r_items = readme_items(p)
    if r_items:
        now = readme.read_text(encoding="utf-8", errors="replace")
        try:
            r_ticks = readme_check(ctx, r_items, now)
            missing = {n: why for n, (s, why) in r_ticks.items() if s != "DONE"}
            if missing:
                steps.info("README changes not made yet: %s; one round of edits for them.",
                           ", ".join(map(str, missing)))
                body, note = _edit_file(ctx, job_id, _edit_messages(
                    p, desc, plan, "README.md", written, now,
                    "Make these README changes the request asks for, which aren't in README.md yet:\n"
                    + "\n".join(f"- change {n}: {why}" for n, why in missing.items())
                    + "\n\nThe changes, as asked:\n\n" + "\n\n".join(r_items[n] for n in missing)
                    + "\n\nStart each FIND with a whole line of README.md's own text (a heading or a sentence), never "
                      "a ``` fence, a table border or a one-word line: a FIND that short is refused. To add a new "
                      "section, FIND the heading it goes after (or the last line of that section) and REPLACE it "
                      "with that same text followed by the new section", cap=budget), "README.md", now)
                edit_notes.append(note)
                if body != now:
                    readme.write_text(body, encoding="utf-8")
                    r_ticks = readme_check(ctx, r_items, body)
        except (BudgetOut, TooLongForLocal) as e:
            r_ticks = {}
            left = left + ["the README changes weren't checked (" + str(e).split(";")[0] + ")"]
        if r_ticks:
            bad = [n for n, (s, _) in r_ticks.items() if s != "DONE"]
            check_notes = check_notes + [
                "**README changes asked:** " + "; ".join(f"{n}: {s}" + (f" - {why}" if why else "")
                                                       for n, (s, why) in r_ticks.items())]
            if bad:
                left = left + [f"README change{'s' if len(bad) != 1 else ''} {', '.join(map(str, bad))} not done"]
    check_notes = _final_tokens(job_id, check_notes)    # the README and its check were the last model calls

    files = sorted(written) + ["README.md"]
    if cut_files or refused_files:                    # incomplete files count against "checks clean" too
        left = left + [f"{len(cut_files) + len(refused_files)} file(s) cut off or left out"]
    if p.get("repaired"):
        n = int(p["repaired"])
        desc += (f"\n\n**Request repaired:** {n} lost backslash{'es' if n != 1 else ''} put back (a control "
                 "character where a path had \\a, \\b, \\f or \\v); the rest of the request was used as written.")
    if edit_notes:
        desc += "\n\n**Edited, not rewritten:**\n" + "\n".join(f"- {n}" for n in edit_notes)
    if stage_notes:
        desc += "\n\n**Built in stages:**\n" + "\n".join(f"- {n}" for n in stage_notes)
    if check_notes:
        desc += "\n\n**Checks:**\n" + "\n".join(f"- {n}" for n in check_notes)
    if cut_files:
        desc += "\n\n**Cut off (incomplete, needs finishing):** " + ", ".join(f"`{r}`" for r in cut_files)
    if refused_files:
        desc += "\n\n**Left out (the model said it couldn't write them):** " + ", ".join(f"`{r}`" for r in refused_files)
    what = "Joomla override" if p.get("joomla") else "project"
    called = f"{'version ' + str(version) + ' of ' if version > 1 else ''}the {what} called {name.replace('-', ' ')}"
    if left:                                          # never passed as complete until it checks clean
        spoken = (f"{called[:1].upper()}{called[1:]} is NOT complete: it doesn't check clean yet ("
                  f"{'; '.join(left)}). The draft is in its own folder in The Smithy's drafts. "
                  "Nothing has been installed or run.")
    else:
        spoken = (f"I built {called}: {len(files)} files, in its own folder in The Smithy's drafts, and it checks "
                  "clean. Nothing has been installed or run. " + plain_summary(summary, 2))
    written["README.md"] = readme.read_text(encoding="utf-8", errors="replace")
    listing = "\n".join(f"- `{rel}` ({len(written[rel].splitlines())} lines)" for rel in files)
    full = ((f"**{NOT_COMPLETE}:** {'; '.join(left)}. Details under Checks.\n\n" if left else "")
            + f"**Folder:** `{folder}`\n\n{desc}\n\n### Files\n\n{listing}\n\n"
            "Copy what you want into place yourself; nothing here has been installed, run or applied.")
    return spoken, full, folder


# ---------------------------------------------------------------- params for builds
def _check_build(p, joomla: bool = False):
    out = {"request": _text(p, "request", "what to build", 3)} | _repaired(p, "request")
    if joomla:
        out["joomla"] = True
    if p.get("path"):                                        # a file to base it on (optional)
        out["path"] = _file(p)
    if p.get("no_ask"):                                      # build straight away, no plan to answer first
        out["no_ask"] = True
    # a plan's links, kept: the worker checks every task's params again before it runs (Nov 14, task 46 lost
    # plan_task and planned again; tasks 44 and 45 lost proposal_of and the user's answers)
    for key in ("plan_task", "proposal_of"):
        if p.get(key):
            out[key] = int(p[key])
    if p.get("answer"):
        out["answer"] = str(p["answer"])[:4000]
    return out


# -- the best round, and the earlier version's passing tests (Nov 20, task 67: a run on this PC's model went from 5
# failing tests to a worse draft, and broke 7 of the earlier version's own tests in files the request doesn't name)
def failing_names(sandbox: dict) -> set[str]:
    """The tests that failed in a Sandbox run, by name (the test file's name when no name could be read)."""
    return {name for t, st in sandbox.get("tests", {}).items() if st != "passed"
            for name in (smithy_diagnose.assertions(sandbox.get("full", {}).get(t, sandbox.get("output", {}).get(t, "")))
                         or {t: ""})}


def save_best(base: Path, job_id: int, folder: Path, rels: list[str], failing: int) -> None:
    """The files of the round with the fewest failing tests so far, kept in `<id>-best` beside the task's results."""
    best = base / f"{job_id:05d}-best"
    shutil.rmtree(best, ignore_errors=True)
    for rel in rels:
        src = folder / rel
        if src.is_file():
            (best / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, best / rel)
    best.mkdir(parents=True, exist_ok=True)
    (best / "best.json").write_text(json.dumps({"failing": failing, "at": time.time()}), encoding="utf-8")


def load_best(base: Path, job_id: int) -> tuple[int, Path] | None:
    best = base / f"{job_id:05d}-best"
    try:
        return int(json.loads((best / "best.json").read_text(encoding="utf-8"))["failing"]), best
    except (OSError, ValueError, KeyError, TypeError):
        return None


def restore_best(best: Path, folder: Path, rels: list[str]) -> list[str]:
    """Put the best round's files back (only those that differ now); the names put back."""
    back = []
    for rel in rels:
        src = best / rel
        if src.is_file() and (not (folder / rel).is_file() or (folder / rel).read_bytes() != src.read_bytes()):
            shutil.copyfile(src, folder / rel)
            back.append(rel)
    return back


def protected_tests(parent_folder: Path | None, failing_then: set[str] | None) -> set[str]:
    """The test functions of the earlier version that passed at its last Sandbox run: its test files' test_ names
    less those that failed then. Empty when that run isn't known (nothing is protected on a guess)."""
    if parent_folder is None or failing_then is None or not Path(parent_folder).is_dir():
        return set()
    names: set[str] = set()
    for f in Path(parent_folder).rglob("*.py"):
        if smithy_sandbox.is_test(f.name):
            try:
                names |= {n.name for n in ast.walk(ast.parse(f.read_text(encoding="utf-8", errors="replace")))
                          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")}
            except SyntaxError:
                continue
    return names - failing_then


def parent_failing(base: Path, parent_id: int) -> set[str] | None:
    """The tests that failed at the earlier task's last Sandbox run (None if it had none)."""
    runs = [e for e in smithy_diagnose.read(base / f"{parent_id:05d}-history.jsonl") if e.get("kind") == "sandbox"]
    return set(runs[-1].get("failing") or []) if runs else None


def named_in_words(text: str, rels: list[str], least: int = 5) -> set[str]:
    """The files whose own distinctive name-word is in the text: "the confusion detector" names
    academy_confusion.py. Words every file shares ("academy") and short ones don't count (Nov 20, task 68: the
    put-back sent the confusion detector back to the earlier version twice, though the request asks for it)."""
    parts = {r: set(re.findall(r"[a-z]+", Path(r).stem.lower())) for r in rels}
    shared = set.intersection(*parts.values()) if len(parts) > 1 else set()
    words = set(re.findall(r"[a-z]+", text.lower()))
    return {r for r, ws in parts.items() if any(len(w) >= least and w not in shared and w in words for w in ws)}


def regression_files(folder: Path, earlier: Path, rels: list[str], named: set[str]) -> list[str]:
    """The code files this version changed that the request doesn't name, to go back to the earlier version's."""
    out = []
    for rel in rels:
        old, new = Path(earlier) / rel, folder / rel
        if rel not in named and not smithy_sandbox.is_test(rel) and old.is_file() and new.is_file() \
                and old.read_bytes() != new.read_bytes():
            out.append(rel)
    return out


def project_words(name: str) -> str:
    """A plan's name in plain words: "academy-lessons" is "academy lessons"."""
    return " ".join(re.sub(r"[^A-Za-z0-9]+", " ", str(name or "")).split()).lower()


def _plan_name(job_id: int) -> str:
    try:
        return str(json.loads((SMITHY.results_dir / f"{job_id:05d}-plan.json").read_text(encoding="utf-8"))
                   ["plan"].get("name") or "")
    except (OSError, ValueError, KeyError, AttributeError):
        return ""


def _subject_of(parent) -> str:
    """What a task is, for the titles of its new versions: the root build's own title ("the build: academy error
    test runner"), kept from version to version (Nov 20, Othos main: "version 6 of Smithy task 63" meant nothing
    a day later)."""
    pp = json.loads(parent["params"] or "{}")
    if pp.get("subject"):
        return str(pp["subject"])
    jt = REGISTRY.get(parent["type"])
    try:
        title = str(parent["title"] or (jt.title(pp) if jt else ""))
    except (KeyError, TypeError, ValueError, AttributeError):
        return ""
    return re.sub(r"^the (?:revised )?plan for ", "", title)       # it's the build that went ahead, not the plan


def _check_followup_any(p):
    """A follow-up question, or on a build, a new version of it."""
    out = _check_followup(p)
    parent = SMITHY.get(out["parent_id"])
    if load_proposal(out["parent_id"]) is not None and not parent["artifact"]:   # a plan, not a build yet
        raise ValueError(f"task {out['parent_id']} is a plan waiting for your answer: reply to it instead "
                         f"(smithy.py reply {out['parent_id']} yes, no, or your answers)")
    if parent["type"] in ("build", "joomla") or "change" in json.loads(parent["params"] or "{}"):
        pp = json.loads(parent["params"] or "{}")
        subject = _subject_of(parent)
        project = project_words(pp.get("project") or _plan_name(out["parent_id"]))
        out = {"parent_id": out["parent_id"], "change": out["question"], "version": int(pp.get("version", 1)) + 1,
               "question": out["question"]} | {k: out[k] for k in ("repaired",) if k in out}
        if subject and not subject.lower().startswith("version "):
            out["subject"] = subject
        if project:
            out["project"] = project
    return out


def _run_followup(ctx, p):
    if "change" in p:                                        # a new version of a build
        return run_build(ctx, p)
    steps.info("Writing the answer.")
    summary, full = split_result(ctx.chat(_followup_messages(p), 1000))
    return summary, full, None


def _with_base_file(build):
    """A build that also gets a file to base it on (an existing layout to override)."""
    def run(ctx, p):
        if p.get("path"):
            p = {**p, "request_given": p["request"], "request": p["request"] + "\n\nBase it on this existing file (don't change the original):\n"
                 + _code_block(p["path"])}
        return build(ctx, p)
    return run


def _proposes(p) -> bool:
    try:
        cfg = worker_shared.load_json(Path(__file__).with_name("smithy.json"))
    except Exception:
        cfg = {}
    return ask_first(cfg, p)


def _title_build(p):
    every = re.sub(r"^(?:me\s+)?(?:a|an|the)\s+", "", p["request"].strip(), flags=re.I).split()
    words = every[:7]
    # "...tool called" is no title: the name after it comes too (Nov 20: task 51 was stored as "Build a small Python
    # 3.12 tool called", and every later version of it, read out by voice Othos, stopped there)
    if len(every) > len(words) and words[-1].lower() in ("called", "named", "titled"):
        words.append(every[len(words)].strip("`'\"“”‘’,.;:"))
    while len(words) > 2 and words[-1].lower().strip(",.") in _SMALL_WORDS:   # don't end on "the"
        words.pop()
    what = ("the Joomla task: " if p.get("joomla") else "the build: ") + " ".join(words).rstrip(",.;:")
    if p.get("proposal_of"):
        return "the revised plan for " + what
    return ("the plan for " + what) if _proposes(p) else what


_SMALL_WORDS = {"a", "an", "the", "and", "or", "of", "in", "on", "to", "for", "that", "which", "with", "my", "your",
                "it", "its", "is", "are", "from", "by", "at", "as"}


_WEAK = re.compile(r"(?i)this PC|20b|flash|8b|3b|mini|small")


def see_me_text(job_id: int, minutes: float, on: str) -> str:
    """The "See me" check-in on a long build, in plain words: how long, where the tests stand against the best so
    far this run, the model it's on, and whether it's getting better, holding or going backwards (Nov 17, the user:
    task 58 passed all 25 request tests at 6:41 pm, then slid back for an hour on weak models, and nothing said so)."""
    events = smithy_diagnose.read(SMITHY.results_dir / f"{job_id:05d}-history.jsonl")
    start = max((i for i, e in enumerate(events) if e.get("kind") == "run"), default=-1)
    runs = [e for e in events[start + 1:] if e.get("kind") == "sandbox"]
    head = f"Task {job_id} has been running {minutes:.0f} minutes, now on {on}."
    if not runs:
        return head + " No test run yet."
    sets = [set(e.get("failing") or []) if e.get("status") != "passed" else set() for e in runs]
    fails = [len(s) for s in sets]
    best = min(fails)
    b = fails.index(best)
    best_at = time.strftime("%I:%M %p", time.localtime(runs[b].get("ts", 0))).lstrip("0")
    now = fails[-1]
    broke = sorted(sets[-1] - sets[b])               # failing now, though they passed in the best run
    where = "all tests pass" if now == 0 else f"{now} failing test{'s' if now != 1 else ''}"
    if now > best or broke:
        trend = (f"Going backwards: the best this run was {best or 'none'} failing, at {best_at}"
                 + (f", and {', '.join(broke[:3])} passed then" if broke else "") + ". "
                 + ("It's on a weaker model now; stopping it until the stronger ones are back may be best."
                    if _WEAK.search(on) else "Worth a look."))
    elif len(fails) > 1 and now < fails[-2]:
        trend = "Getting better."
    else:
        trend = "Holding steady."
    return f"{head} Last test run: {where}. {trend}"


REGISTRY: dict[str, JobType] = {
    "review": JobType("review", "Review a code file for bugs and risks.", "code",
                      _review, lambda p: {"path": _file(p)},
                      lambda p: f"your {Path(p['path']).name} review", max_tokens=1500, coding=True),
    "fix": JobType("fix", "Draft a fix for a code file, as a diff (never applied).", "code",
                   _fix, lambda p: {"path": _file(p), "request": _text(p, "request", "what to fix")},
                   lambda p: f"the fix for {Path(p['path']).name}", max_tokens=1500, artifact="diff", coding=True,
                   run=_run_fix),                     # the diff is checked to apply (git apply --check), never applied
    "script": JobType("script", "Write a script (saved as a draft, never run).", "code",
                      _script, lambda p: {"request": _text(p, "request", "what the script should do", 3)},
                      lambda p: "the script you asked for", max_tokens=1500, artifact="script", coding=True,
                      run=_run_script),               # parsed until it checks clean, never run
    "joomla": JobType("joomla", "A Joomla / YOOtheme override or CSS, in its own folder.", "code",
                      None, lambda p: _check_build(p, joomla=True), _title_build,
                      max_tokens=3000, run=_with_base_file(run_build), minutes_key="max_build_minutes", coding=True,
                      see_me=lambda j, m, on: see_me_text(j, m, on)),
    "build": JobType("build", "Build a project from scratch: plan, files, README (never run).", "code",
                     None, _check_build, _title_build,
                     max_tokens=3000, run=_with_base_file(run_build), minutes_key="max_build_minutes", coding=True,
                     see_me=lambda j, m, on: see_me_text(j, m, on)),
    "followup": JobType("followup", "A follow-up question; on a build, a new version of it.", "code",
                        None, _check_followup_any,
                        lambda p: (f"version {p['version']} of {p.get('subject') or 'Smithy task ' + str(p['parent_id'])}" if "change" in p
                                   else f"the answer about Smithy task {p['parent_id']}"),
                        max_tokens=1000, run=_run_followup, minutes_key="max_build_minutes", coding=True,
                        see_me=lambda j, m, on: see_me_text(j, m, on)),
}
if smithy_check is not None:                     # not in the shared copy of Othos
    REGISTRY["check"] = JobType(
        "check", "Study a program with REA/Ghidra: FEATURES, SPECS and AGENT for a clean-room rebuild.",
        "code", None, smithy_check.check_params, lambda p: f"the check of {Path(p['path']).name}",
        max_tokens=1500, run=smithy_check.run_check, minutes_key="max_check_minutes", coding=True)
