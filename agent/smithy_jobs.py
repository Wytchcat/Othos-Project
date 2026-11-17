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
import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
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
          "[REDACTED], and never report it as a bug.")
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


def _text(p: dict, key: str, what: str, min_words: int = 2) -> str:
    t = str(p.get(key, "")).strip()
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
    return {"parent_id": pid, "question": _text(p, "question", "a question")}


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


_ONLY = re.compile(r"\b(?:only|no other files?|nothing else|change no other|leave (?:the )?(?:other|rest)|"
                   r"leave (?:all )?(?:the )?(?:existing |other )?files? alone|"
                   r"don'?t (?:change|touch) (?:any )?(?:other|anything else))\b", re.I)
_FILES_LINE = re.compile(r"(?im)^[ \t]*(?:[-*][ \t]*)?files?\b[^:\n]{0,20}:(.*)$")
_SENT = r"(?:[^.\n]|\.(?=\w))*"                       # within one sentence: a dot in "a.py" doesn't end it
_LEAVE = re.compile(r"(?i)" + _SENT + r"\bleave (?:them|it|these|those)?[ \t]*(?:as (?:they|it) (?:are|is)|alone|"
                    r"unchanged)" + _SENT)


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
    academy_readme.py, academy_query.py and academy_confusion.py)."""
    pick = [r for r in code if r not in same]
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


def _file_messages(p: dict, desc: str, plan: dict, rel: str, written: dict[str, str], budget: int,
                   old: str | None) -> list[dict]:
    flavour = ("\n\n" + JOOMLA) if p.get("joomla") else ""
    listing = "\n".join(f"- {f['path']}: {f['purpose']}" for f in plan["files"])
    purpose = next((f["purpose"] for f in plan["files"] if f["path"] == rel), "")
    md = rel.lower().endswith(".md")
    context, used = [], 0
    for path, body in reversed(list(written.items())):            # the most recent files first
        piece = f"--- {path} ---\n{body}"
        if used + len(piece) > budget:
            context.append(f"--- {path} --- (written; too long to show here)")
            continue
        context.append(piece)
        used += len(piece)
    msgs = [{"role": "system", "content": SYSTEM + flavour},
            {"role": "user", "content": f"The project: {redact(p['request'])}\n\nThe design:\n{desc[:4000]}\n\n"
                                        f"All its files:\n{listing}"}]
    if context:
        msgs.append({"role": "user", "content": "Files already written:\n" + "\n\n".join(reversed(context))})
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


def apply_edits(body: str, edits: list[tuple[str, str]], brace: bool = False,
                others: dict[str, str] | None = None) -> tuple[str, int, list[str]]:
    """Apply the pairs in order. Returns (new body, how many applied, problems for the ones that weren't).
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
    others = "\n\n".join(f"--- {path} ---\n{body[:cap]}" + ("\n(... cut here)" if len(body) > cap else "")
                         for path, body in written.items())
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


def _edit_file(ctx, job_id: int, msgs: list[dict], rel: str, old: str,
               none_ok: bool = True, others: dict[str, str] | None = None) -> tuple[str, str]:
    """A long file changed by find/replace pairs. Returns (new body, a note for the result).
    No edits at all: the file is kept as it was (the plan can call a file "changed" that the change
    doesn't touch; Nov 13, task 18 failed on that), or with none_ok=False, ValueError.
    `others`: the build's other files, so an edit whose FIND is from one of them is named as such."""
    others = {k: v for k, v in (others or {}).items() if k != rel}
    steps.info("Editing %s: asking for the changes as edits.", rel)
    with worker_shared.answer_shape(EDITS_SCHEMA):
        reply = ctx.chat(msgs, 6000)
    edits = parse_edits(reply)
    if not edits:
        if none_ok:
            log.info("build %d: %s, no edits sent; kept as it was", job_id, rel)
            steps.info("%s needs no change; kept exactly as it was.", rel)
            return old, f"`{rel}`: no edits sent, so kept exactly as it was"
        raise ValueError(f"the changes to {rel} didn't come back as edits I could apply")
    brace = Path(rel).suffix.lower() in _BRACES
    body, done, problems = apply_edits(old, edits, brace, others)
    if problems:                                          # one more try for the ones that missed
        log.info("build %d: %s, %d of %d edits applied; asking again for %d", job_id, rel, done, len(edits),
                 len(problems))
        steps.info("%s: %d of %d edits went in; %d couldn't be found, asking again for %s.", rel, done, len(edits),
                   len(problems), "it" if len(problems) == 1 else "them")
        with worker_shared.answer_shape(EDITS_SCHEMA):
            again = ctx.chat(msgs + [{"role": "assistant", "content": reply},
                                     {"role": "user", "content":
                                      "These edits couldn't be applied, so they weren't:\n- " + "\n- ".join(problems)
                                      + f"\n\nThe others are already in. Here is {rel} now:\n```\n{body}\n```\n\n"
                                      + (f"Edits whose FIND is from another file aren't for {rel}: drop them. "
                                         if any(_ELSEWHERE in x for x in problems) else "")
                                      + f"Send only the missed edits for {rel} again, with the FIND lines copied "
                                      "exactly from it. " + EDIT_FORMAT}], 6000)
        more = parse_edits(again)
        if more:                                       # no edits in the reply: the missed ones stay missed
            body, done2, problems = apply_edits(body, more, brace, others)
            done += done2
    note = f"`{rel}`: changed by {done} edit{'s' if done != 1 else ''}; every other line kept as it was"
    if problems:
        note += ". **Not applied (couldn't be found exactly once):** " + "; ".join(problems)
    log.info("build %d: %s, %d edits applied, %d not", job_id, rel, done, len(problems))
    steps.info("%s: %d edit%s in%s; every other line kept.", rel, done, "" if done == 1 else "s",
               f", {len(problems)} left out" if problems else "")
    return body, note


# ---------------------------------------------------------------- builds: checking before the README
# Nov 14, installer v14: 11 numbered changes asked, 4 made, it didn't compile, and the README said
# all of it was done. So a build is now compiled / parsed (never run), the numbered changes are
# checked off, one round of edits goes to whatever failed, and the README is told the truth.
_CSC = [Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Microsoft.NET" / fw / "v4.0.30319" / "csc.exe"
        for fw in ("Framework64", "Framework")]
_CSC_REFS = ["System.dll", "System.Core.dll", "System.Data.dll", "System.Drawing.dll",
             "System.Windows.Forms.dll", "System.Xml.dll"]
_CSC_ERR = re.compile(r"^(.*?)\((\d+),\d+\): error (CS\d+): (.*)$")
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
        m = _CSC_ERR.match(line.strip())
        if m:
            try:
                where = Path(m.group(1)).resolve().relative_to(folder.resolve()).as_posix()
            except ValueError:
                where = Path(m.group(1)).name
            errors.append(f"{where} line {m.group(2)}: {m.group(3)}: {m.group(4)}")
    return errors


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


def build_problems(folder: Path, rels: list[str], stdlib_only: bool = False) -> tuple[list[str], list[str]]:
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
            problems += errs
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
        if tests:
            checked.append("unittest.main() placed so the tests run")
    more, said = smithy_checkers.check_all(folder, rels)      # ruff, pyright, PSScriptAnalyzer, tsc, javac
    return problems + more, checked + said


def _is_main_guard(node: ast.AST) -> bool:
    """`if __name__ == "__main__":`"""
    t = getattr(node, "test", None)
    return isinstance(node, ast.If) and isinstance(t, ast.Compare) and isinstance(t.left, ast.Name) \
        and t.left.id == "__name__" and any(isinstance(c, ast.Constant) and c.value == "__main__"
                                             for c in t.comparators)


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


_RUNS_ANYWAY = re.compile(r"^\S.* line \d+: F811 ")      # redefined: Python runs it, the later one wins


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
_README_ITEM = re.compile(r"(?i)^\s*\d{1,2}[.)]\s+[`*]*README(?:\.md)?\b")


def numbered_items(change: str) -> dict[int, str]:
    """The numbered changes in a request ("1. ...", "2) ..."), each with its following lines."""
    items, marks = {}, list(_ITEM.finditer(change or ""))
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
        return {}
    return {k: re.sub(r"\s+", " ", v).strip(" ;.") for k, v in _SUB.findall(" ".join(m.group(1).split()))}


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


def _parse_proofs(reply: str) -> dict[str, tuple[str, str, str, str]]:
    """{"2" or "2f": (DONE|MISSING, file, quote, note)} from a checklist reply."""
    got = {}
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
    code = [r for r in written if not r.lower().endswith(".md")]
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
        read = sorted({r for n in part for r in _names(part[n], [{"path": x} for x in code], stems=True)}
                      | (set(tests) if any(subs[n] for n in part) else set()))
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"The project: {redact(p['request'])}\n\nIts files now:\n\n{files}"},
                {"role": "user", "content":
                 "These changes were asked for:\n\n" + redact("\n\n".join(part.values())) +
                 "\n\nCheck the files above, not the request" + (f" (read {', '.join(read)} in full)" if read else "")
                 + ". Reply with one line for each of these, and no other text:\n" + "\n".join(lines) +
                 "\n\nA DONE must quote one exact line copied from the file, in backticks; the quote is looked up "
                 "in the file, and a DONE whose quote isn't there counts as missing. A lettered check is DONE only "
                 "when a test really checks it. If you can't quote it, it's MISSING."}]
        # room for models that think first (Nov 14, task 38: 1,500 left no ticks for 8 items, so all 8 read as
        # missing); one more ask when none can be read
        reply = ctx.chat(msgs, 4000)
        got = _parse_proofs(reply)
        if not got:
            log.info("checklist: no ticks in the reply (%d characters); asking again", len(reply))
            more = ctx.chat(msgs + [{"role": "assistant", "content": reply},
                                    {"role": "user", "content": "I couldn't read that. Reply with only the lines "
                                     "asked for, like \"1: DONE - a.py: `the line`\" or \"2: MISSING - file: what's "
                                     "missing\"."}],
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


def _verify_build(ctx, job_id: int, p: dict, desc: str, plan: dict, folder: Path, written: dict[str, str],
                  cap: int, only: dict[int, str] | None = None,
                  rounds: int | None = None) -> tuple[list[str], str, list[str]]:
    """Check the build before its README, and keep fixing until it checks clean (the user, Nov 14: nothing
    passes as complete until it does). Each round: the numbered changes checked off, then compile / parse;
    edits for whatever failed; up to `build_check_rounds` rounds (default 3).
    Returns (notes for the result, the truth for the README, what's still wrong: empty = checks clean)."""
    notes = []
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
    wants_tests = only is None and bool(_WANTS_TESTS.search(p.get("request", "")))

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
            before = build_problems(folder, code, stdlib_only)[0]
            path.write_text(new, encoding="utf-8")
            after = build_problems(folder, code, stdlib_only)[0]
        except Exception:
            return ""
        finally:
            path.write_text(old, encoding="utf-8")
        if len(after) <= len(before):
            return ""
        old_kinds = {re.sub(r"line \d+", "", x) for x in before}
        added = [x for x in after if re.sub(r"line \d+", "", x) not in old_kinds] or after[len(before):]
        return f"{len(after) - len(before)} new build problem{'s' if len(after) - len(before) != 1 else ''}: " \
               + "; ".join(added[:6])

    request_text = "\n".join(items.values()) or p.get("change") or p.get("request", "")
    locked: set[str] = set()          # tests that failed in several Sandbox runs in a row: test fixes leave them be
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

        def keys(text: str) -> set[str]:
            return {k.value for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Dict)
                    for k in n.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        try:
            added = sorted(k for k in keys(new) - keys(old) if k.lower() not in request_words)
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
        changed = _changed_tests(old, new, locked)
        if changed:
            # task 57: test_write_query_... failed 4 runs in a row, then its folder was narrowed so it passed
            why.append(f"it changes {', '.join(changed)}, which failed in several test runs in a row and is locked: "
                       "the code is fixed for it, or a person decides")
        if gone:
            why.append("it removes what the request names: " + ", ".join(f'"{t}"' for t in gone[:4])
                       + " (never take out a check the request asks for)")
        if added:
            why.append("it adds field(s) the request never names: " + ", ".join(f'"{k}"' for k in added[:4])
                       + " (a test checks the request, not what the code happens to want)")
        return "; ".join(why)

    def refused(rel: str, old: str, new: str) -> str:
        return _breaks(rel, old, new) or _adds_problems(rel, old, new) or _bends_test(rel, old, new)

    def fix(rel: str, what: str, must: bool = False) -> None:
        """`must`: the check named this file for a change it found missing, so "no edits" isn't an answer: the
        file is then written whole (Nov 15, task 56: models answered no edits while the glossary had no ids)."""
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
        try:
            body, note = _edit_file(ctx, job_id, _edit_messages(p, desc, plan, rel, written, written[rel], what, cap),
                                    rel, written[rel], none_ok=not must, others=written)
        except ValueError:
            steps.info("%s: the check found a change missing here, but no edits came back.", rel)
            whole = _rewrite_whole(rel, what)
            notes.append(f"`{rel}`: no edits came back for a change the check found missing here"
                         + (f"; the file was then written whole ({whole})" if whole else "; kept as it was"))
            return
        broke = refused(rel, written[rel], body)
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
            if again:
                steps.info("%s: the edits weren't kept again (%s); not kept.", rel, again[:200])
                notes.append(f"`{rel}`: its edits were refused twice ({again}), so they weren't kept")
                smithy_diagnose.record(hist, "fix", file=rel, kept=False, why=again[:160])
                return
            note += f" (the first edits broke the file: {broke}; asked again)"
        if body != written[rel]:
            inside(folder, rel).write_text(body, encoding="utf-8")
            written[rel] = body
        if "**Not applied" in note and (whole := _rewrite_whole(rel, what)):
            note += f"; the missed changes were then made by writing the file whole ({whole})"
        notes.append(note)
        smithy_diagnose.record(hist, "fix", file=rel, kept=True, broke_first=bool(broke),
                               missed="**Not applied" in note)

    def _rewrite_whole(rel: str, what: str) -> str:
        """Edits that couldn't be found (task 51: 0 of 2 went in, twice): the file is written whole instead,
        with the current text to keep. Only kept when it's no shorter than half the old one, isn't a refusal
        and (Python, JSON) still parses; else the old file stays. Returns how it went, or '' (not tried)."""
        old = written[rel]
        if len(old) > 24_000 or rel.lower().endswith(".md"):
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
    locked.update(s["test"] for s in smithy_diagnose.stuck_tests(smithy_diagnose.read(hist)))  # from earlier runs

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
        problems, checked = build_problems(folder, code, stdlib_only)
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

    def files_for(n: int) -> list[str]:
        """The files to fix for item n, at most 3: the ones the check's note names, else the ones the whole item
        names (its "Files:" lines, names without .py), in the order they're named; else the longest. Never a file
        the plan keeps the same (Nov 15, task 56: the notes named no file, the item's first line neither, so all
        three items went to academy_sources.py, the longest, which the request said to leave as it is)."""
        pick = [r for r in code if r not in same] or list(code)
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
        steps.info("Checking the %d changes asked for against the files.", len(items))
    # a limit reached part way (the task's cloud tokens, a step too long for this PC) ends the checks,
    # not the task: what's true so far is reported, NOT COMPLETE (Nov 14, task 38 failed instead)
    stopped = ""
    ticks: dict[int, tuple[str, str]] = {}
    try:
        ticks, problems, checked = check()
        for rnd in range(1, rounds + 1):
            missing = [n for n, (state, _) in ticks.items() if state == "MISSING"]   # NOT SAID isn't fixed blind
            if not missing and not problems:
                break
            steps.info("Check round %d of %d: %s; asking for fixes.", rnd, rounds, ", ".join(filter(None, [
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
                for rel in code:
                    mine = [x for x in problems if x.startswith(rel + " ")]
                    if mine:
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
            stuck = smithy_diagnose.no_progress(smithy_diagnose.read(hist))
            if stuck and stuck >= int(ctx.cfg.get("diagnose_stop_after", 2)) > 0:
                # the same things missing and the same problems after fixes that changed the files: more rounds
                # spend tokens on the same answer (Nov 16, self-diagnosis)
                steps.info("The last %d checks found the same things, though the fixes changed the files; "
                           "stopping the fix rounds here.", stuck + 1)
                notes.append(f"**Fix rounds stopped early:** the last {stuck + 1} checks found the same things "
                             "missing and the same problems, though the files changed.")
                break
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

    # the tests, in a throwaway Windows Sandbox (smithy_sandbox; off unless sandbox_tests is on). Only for the
    # whole build, once it builds clean; a failure gets one fix round (sandbox_fix_rounds) and runs again.
    sandbox = {"status": "off", "tests": {}, "output": {}, "why": ""}
    # a problem the code runs with all the same (a name defined twice) doesn't keep the tests from running; it
    # still leaves the build NOT COMPLETE (Nov 17, task 58: one repeated import meant its tests never ran)
    if only is None and not stopped and not blocks_tests(problems) and any(smithy_sandbox.is_test(r) for r in code):
        stop = getattr(ctx, "_should_stop", None)
        try:
            for rnd in range(int(ctx.cfg.get("sandbox_fix_rounds", 1)) + 1):
                sandbox = smithy_sandbox.run_tests(job_id, folder, code, ctx.cfg, SMITHY.results_dir, stop, rnd + 1)
                smithy_diagnose.record(hist, "sandbox", status=sandbox["status"],
                                       failing=sorted({name for t, st in sandbox["tests"].items() if st != "passed"
                                                       for name in (smithy_diagnose.assertions(
                                                           sandbox.get("full", {}).get(t, sandbox["output"].get(t, "")))
                                                           or {t: ""})}),
                                       asserts={k: v for t in sandbox["tests"] for k, v in smithy_diagnose.assertions(
                                           sandbox.get("full", {}).get(t, sandbox["output"].get(t, ""))).items()})
                # a test that has failed in several runs in a row is locked: a test fix may not change it (Nov 16,
                # task 57: one that failed 4 times was narrowed until it passed)
                locked.clear()
                locked.update(s["test"] for s in smithy_diagnose.stuck_tests(smithy_diagnose.read(hist)))
                if sandbox["status"] != "failed" or rnd >= int(ctx.cfg.get("sandbox_fix_rounds", 1)):
                    break
                before = {r: (folder / r).read_bytes() for r in code if (folder / r).exists()}
                for t, state in sandbox["tests"].items():
                    if state == "passed":
                        continue
                    out = sandbox["output"].get(t, "")
                    same = {f["path"] for f in plan.get("files", []) if f.get("change") == "same"}
                    named = _failing_files(sandbox.get("full", {}).get(t, out), t, code, same) or \
                        [r for r in _imported_by(folder, t, code) if r not in same] or [t]
                    shown = _failing_test_code(folder / t, sandbox.get("full", {}).get(t, out))
                    # what the change asks for, where it names these files or has lettered checks, so a test that
                    # disagrees with the request can be put right instead of the code being bent to it (Nov 16,
                    # task 56: the test's made-up README had 2 glossary terms where check (a) says three, and the
                    # fix made the lesson give 10 entries); the test file goes first, then the code
                    asked = [items[n] for n in items if sub_points(items[n])
                             or _names(items[n], [{"path": r} for r in named], stems=True)][:3]
                    wanted = ("\n\nWhat the change asks for (the request; it decides who's right):\n\n"
                              + "\n\n".join(asked)) if asked else ""
                    named = ([t] if t in named else []) + [r for r in named if r != t]
                    full = sandbox.get("full", {}).get(t, out)
                    none_ran = bool(re.search(r"\bRan 0 tests?\b|NO TESTS RAN", full))
                    if none_ran:
                        # no test ran: the test file's own fault, never the code's (Nov 16, task 57: unittest.main()
                        # inside the test class; the fix went to academy_confusion.py and began rewriting it whole)
                        named = [t]
                        wanted += ("\n\nNo test ran at all (\"Ran 0 tests\"). The cause is in this test file: most "
                                   "often unittest.main() inside a class or before the tests, or tests that aren't "
                                   "methods named test_... of a unittest.TestCase class. Fix only that.")
                    said, held, judged = "", [], False
                    failing = re.findall(r"(?m)^(?:FAIL|ERROR): (\w+)", full)
                    stuck_here = [n for n in failing if n in locked]
                    if stuck_here:
                        wanted += ("\n\nThese tests have failed in several test runs in a row and are locked: don't "
                                   "change them in the test file; " + ", ".join(stuck_here) + ".")
                        notes.append(f"**`{t}`: {', '.join(stuck_here)} failed in several test runs in a row**, so "
                                     "the test fix may not change them: the code is fixed for them, or you decide "
                                     "whether the test or the code is right.")
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
                                    and n not in locked]          # locked: the code is the only way left
                            if held:
                                smithy_diagnose.record(hist, "test_disagrees", test=t, tests=held)
                                notes.append(f"**`{t}`: {', '.join(held)} may disagree with the request.** A fix "
                                             "for the test was tried, but they came out the same, so the code wasn't "
                                             "changed to fit them this round. Check them against the request.")
                            left = [n for n in failing if n not in held]
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
                        fix(rel, f"{t} failed when it was run (in a throwaway Sandbox). Its last lines:\n{out}\n\n"
                                 + (f"The failing tests, as written in {t}:\n```python\n{shown}\n```\n\n" if shown else "")
                                 + how + wanted)
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
    if ran:
        pass
    elif wants_tests:
        left.append("its tests weren't run (The Smithy runs nothing; the request asks for them to pass)")
        notes.append("**Tests not run:** the request asks for "
                     + (", ".join(f"`{t}`" for t in tests) if tests else "its tests") + " to pass, and The Smithy "
                     "never runs code, so that isn't shown.")
        truth.append("Its tests have NOT been run, so don't say they pass; say \"Tests not run yet\".")
    elif tests:
        notes.append("**Tests not run:** " + ", ".join(f"`{t}`" for t in tests) + " (The Smithy never runs code).")
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
        truth.append("Of the numbered changes asked for, these are DONE: " + (", ".join(map(str, done)) or "none")
                     + (". These are NOT done, so don't describe them as done; list them under \"Not done yet\": "
                        + "; ".join(items[n].split("\n")[0][:160] for n in not_done) if not_done else "."))
    if hist is not None:
        smithy_diagnose.record(hist, "end", left=left, stopped=stopped[:160])
        found = smithy_diagnose.self_check(smithy_diagnose.read(hist), items)
        notes.append("**Self-check:** " + ("nothing found." if not found else "\n- " + "\n- ".join(found)))
        if found:
            steps.info("Self-check: %s", " | ".join(x[:140] for x in found))
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


# ---------------------------------------------------------------- builds: read, ask, propose (Nov 14)
# the user: "Build for The Smithy to read a prompt, ask for clarification where needed, create a plan
# offered with a yes/no prompt and make any adaptations for further user input." A new build is a
# proposal first (build_ask_first, on): its questions, what doesn't fit together, what it will assume, and
# the plan, then it waits. Her reply (smithy.py reply ID TEXT, or the app's Plan window): yes builds it
# with that plan, no drops it, anything else is answers or changes and gets a revised proposal.
def ask_first(cfg: dict, p: dict) -> bool:
    """Whether this build is proposed first (not a new version, not one she already said yes to)."""
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
                       + f"\n  It starts:\n  " + first[:900].replace("\n", "\n  "))
    return "\n".join(out)[:limit_chars]


# -- what The Smithy can't do here, said before she says yes (suggestion 6)
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


# -- cost and time, before she says yes (suggestion 2): its own token limit, and what earlier builds of
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
    """What a revised plan changed, in plain words (so she can see her answers took effect)."""
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
    """The proposal as she reads it (the result, voice, and the app's Plan window)."""
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
    """Read the request, ask what's unclear, propose a plan; nothing is built. Waits for her reply."""
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
    if p.get("proposal_of"):                            # what her answers changed (suggestion 8)
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
    """Her reply to a proposal: yes -> the build is queued with that plan; no -> dropped; anything else ->
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


def carry_on(job_id: int) -> str:
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


def _result_head(row) -> str:
    try:
        return Path(row["result_path"]).read_text(encoding="utf-8", errors="replace")[:2000] if row["result_path"] else ""
    except OSError:
        return ""


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
    # minute her second Cerebras key came in, which "defeats the purpose of the second key completely")
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

    # 0. a new build is a proposal first: questions and a plan, then it waits for her yes (Nov 14)
    if p.get("proposal_of") or (not parent and ask_first(ctx.cfg, p)):
        return run_proposal(ctx, p)
    if p.get("plan_task") and not plan_path.exists():     # the plan she said yes to
        agreed = results / f"{int(p['plan_task']):05d}-plan.json"
        if agreed.exists():
            shutil.copyfile(agreed, plan_path)
            steps.info("Building with the plan from task %d, which you said yes to.", int(p["plan_task"]))

    # 1. the plan (kept, so a paused build carries on instead of starting again)
    if plan_path.exists():
        saved = json.loads(plan_path.read_text(encoding="utf-8"))
        summary, desc, plan = saved["summary"], saved["desc"], saved["plan"]
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
        if f.get("change") == "same" and old_path is not None:
            steps.info("%s stays the same: copied.", rel)
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
                                    others=written)
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
                    others=written)
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

    files = sorted(written) + ["README.md"]
    if cut_files or refused_files:                    # incomplete files count against "checks clean" too
        left = left + [f"{len(cut_files) + len(refused_files)} file(s) cut off or left out"]
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
    out = {"request": _text(p, "request", "what to build", 3)}
    if joomla:
        out["joomla"] = True
    if p.get("path"):                                        # a file to base it on (optional)
        out["path"] = _file(p)
    if p.get("no_ask"):                                      # build straight away, no plan to answer first
        out["no_ask"] = True
    # a plan's links, kept: the worker checks every task's params again before it runs (Nov 14, task 46 lost
    # plan_task and planned again; tasks 44 and 45 lost proposal_of and her answers)
    for key in ("plan_task", "proposal_of"):
        if p.get(key):
            out[key] = int(p[key])
    if p.get("answer"):
        out["answer"] = str(p["answer"])[:4000]
    return out


def _check_followup_any(p):
    """A follow-up question, or on a build, a new version of it."""
    out = _check_followup(p)
    parent = SMITHY.get(out["parent_id"])
    if load_proposal(out["parent_id"]) is not None and not parent["artifact"]:   # a plan, not a build yet
        raise ValueError(f"task {out['parent_id']} is a plan waiting for your answer: reply to it instead "
                         f"(smithy.py reply {out['parent_id']} yes, no, or your answers)")
    if parent["type"] in ("build", "joomla") or "change" in json.loads(parent["params"] or "{}"):
        pp = json.loads(parent["params"] or "{}")
        out = {"parent_id": out["parent_id"], "change": out["question"], "version": int(pp.get("version", 1)) + 1,
               "question": out["question"]}
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
    words = re.sub(r"^(?:me\s+)?(?:a|an|the)\s+", "", p["request"].strip(), flags=re.I).split()[:7]
    while len(words) > 2 and words[-1].lower().strip(",.") in _SMALL_WORDS:   # don't end on "the"
        words.pop()
    what = ("the Joomla task: " if p.get("joomla") else "the build: ") + " ".join(words).rstrip(",.;:")
    if p.get("proposal_of"):
        return "the revised plan for " + what
    return ("the plan for " + what) if _proposes(p) else what


_SMALL_WORDS = {"a", "an", "the", "and", "or", "of", "in", "on", "to", "for", "that", "which", "with", "my", "your",
                "it", "its", "is", "are", "from", "by", "at", "as"}


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
                      max_tokens=3000, run=_with_base_file(run_build), minutes_key="max_build_minutes", coding=True),
    "build": JobType("build", "Build a project from scratch: plan, files, README (never run).", "code",
                     None, _check_build, _title_build,
                     max_tokens=3000, run=_with_base_file(run_build), minutes_key="max_build_minutes", coding=True),
    "followup": JobType("followup", "A follow-up question; on a build, a new version of it.", "code",
                        None, _check_followup_any,
                        lambda p: (f"version {p['version']} of Smithy task {p['parent_id']}" if "change" in p
                                   else f"the answer about Smithy task {p['parent_id']}"),
                        max_tokens=1000, run=_run_followup, minutes_key="max_build_minutes", coding=True),
}
if smithy_check is not None:                     # not in the shared copy of Othos
    REGISTRY["check"] = JobType(
        "check", "Study a program with REA/Ghidra: FEATURES, SPECS and AGENT for a clean-room rebuild.",
        "code", None, smithy_check.check_params, lambda p: f"the check of {Path(p['path']).name}",
        max_tokens=1500, run=smithy_check.run_check, minutes_key="max_check_minutes", coding=True)
