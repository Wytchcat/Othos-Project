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
  into a temporary folder that's deleted), PowerShell, Python and JSON files are parsed. The
  result is never run. On a new version, each numbered change asked for is checked off.
"""
from __future__ import annotations

import base64
import difflib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

try:                                             # the check task: left out of the shared copy of Othos
    import smithy_check
except ImportError:
    smithy_check = None
import worker_files
from worker_engine import FORMAT, NOT_COMPLETE, JobType, plain_summary, save_artifact, split_result
from worker_queue import SMITHY
from worker_shared import RULES, USER, USERS, redact

log = logging.getLogger("worker")
steps = logging.getLogger("worker.step")     # plain step lines (Progress window, Othos's Task Mode)

SYSTEM = (f"You are the Smithy, a careful coding assistant for Othos on {USERS} Windows 11 PC. "
          "You review code, draft fixes, write scripts, and build small projects from scratch. " + RULES)
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
               "followed by the new ones. Don't send the whole file.")
_EDIT = re.compile(r"<<<<<<< FIND[ \t]*\n([\s\S]*?)\n=======[ \t]*\n([\s\S]*?)\n?>>>>>>> REPLACE")


def parse_edits(text: str) -> list[tuple[str, str]]:
    """The (find, replace) pairs in a reply, in order."""
    return [(m.group(1), m.group(2)) for m in _EDIT.finditer(text.replace("\r\n", "\n"))]


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


def apply_edits(body: str, edits: list[tuple[str, str]], brace: bool = False) -> tuple[str, int, list[str]]:
    """Apply the pairs in order. Returns (new body, how many applied, problems for the ones that weren't).
    A pair that can't be found exactly once is skipped, never guessed. brace: a brace language (C#, JS,
    PHP, PowerShell, CSS...), where lines that lost their indentation get it back."""
    done, problems = 0, []
    for k, (find, repl) in enumerate(edits, 1):
        where = _find_once(body, find)
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
                                            + f"; every other line stays exactly as it is.{csharp}\n\n{EDIT_FORMAT}"})
    return msgs


def _edit_file(ctx, job_id: int, msgs: list[dict], rel: str, old: str,
               none_ok: bool = True) -> tuple[str, str]:
    """A long file changed by find/replace pairs. Returns (new body, a note for the result).
    No edits at all: the file is kept as it was (the plan can call a file "changed" that the change
    doesn't touch; Nov 13, task 18 failed on that), or with none_ok=False, ValueError."""
    steps.info("Editing %s: asking for the changes as edits.", rel)
    reply = ctx.chat(msgs, 6000)
    edits = parse_edits(reply)
    if not edits:
        if none_ok:
            log.info("build %d: %s, no edits sent; kept as it was", job_id, rel)
            steps.info("%s needs no change; kept exactly as it was.", rel)
            return old, f"`{rel}`: no edits sent, so kept exactly as it was"
        raise ValueError(f"the changes to {rel} didn't come back as edits I could apply")
    brace = Path(rel).suffix.lower() in _BRACES
    body, done, problems = apply_edits(old, edits, brace)
    if problems:                                          # one more try for the ones that missed
        log.info("build %d: %s, %d of %d edits applied; asking again for %d", job_id, rel, done, len(edits),
                 len(problems))
        steps.info("%s: %d of %d edits went in; %d couldn't be found, asking again for %s.", rel, done, len(edits),
                   len(problems), "it" if len(problems) == 1 else "them")
        again = ctx.chat(msgs + [{"role": "assistant", "content": reply},
                                 {"role": "user", "content":
                                  "These edits couldn't be applied, so they weren't:\n- " + "\n- ".join(problems)
                                  + f"\n\nThe others are already in. Here is {rel} now:\n```\n{body}\n```\n\n"
                                  "Send only the missed edits again, with the FIND lines copied exactly from it. "
                                  + EDIT_FORMAT}], 6000)
        more = parse_edits(again)
        body, done2, problems = apply_edits(body, more, brace)
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


def build_problems(folder: Path, rels: list[str]) -> tuple[list[str], list[str]]:
    """(problems, what was checked). C# compiled together, PowerShell / Python / JSON parsed. Nothing runs."""
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
        cs = script["files"]
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
    return problems, checked


_ITEM = re.compile(r"(?m)^\s*(\d{1,2})[.)]\s+(\S.*)$")
_TICK = re.compile(r"(?mi)^[^\w\n]*(?:item[ \t]*)?(\d{1,2})[ \t]*[:.)\-][ \t]*\**(DONE|MISSING)\**\b[ \t:\-â€“]*([^\n]*)$")


def numbered_items(change: str) -> dict[int, str]:
    """The numbered changes in a request ("1. ...", "2) ..."), each with its following lines."""
    items, marks = {}, list(_ITEM.finditer(change or ""))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(change)
        items[int(m.group(1))] = change[m.start():end].strip()
    return items


def _checklist(ctx, p: dict, desc: str, plan: dict, written: dict[str, str], items: dict[int, str],
               cap: int) -> dict[int, tuple[str, str]]:
    """{item number: ("DONE" | "MISSING" | "NOT SAID", note)} for the numbered changes, from the files as they are."""
    files = "\n\n".join(f"--- {path} ---\n{body[:cap]}" + ("\n(... cut here)" if len(body) > cap else "")
                        for path, body in written.items() if not path.lower().endswith(".md"))
    reply = ctx.chat([{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": f"The project: {redact(p['request'])}\n\nIts files now:\n\n{files}"},
                      {"role": "user", "content":
                       "These changes were asked for:\n\n" + redact("\n".join(items.values())) +
                       "\n\nCheck the files above, not the request: for each numbered change, reply with one line, "
                       "exactly \"N: DONE\" or \"N: MISSING - the file and what's missing\". Only DONE when the code "
                       "above really does it. No other text."}], 1500)
    got = {int(m.group(1)): (m.group(2).upper(), m.group(3).strip()[:200]) for m in _TICK.finditer(reply)}
    return {n: got.get(n, ("NOT SAID", "")) for n in items}


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
    items = only if only is not None else numbered_items(p.get("change", "")) if p.get("parent_id") else {}
    use_list = len(items) >= (1 if only is not None else 2)

    def fix(rel: str, what: str) -> None:
        body, note = _edit_file(ctx, job_id, _edit_messages(p, desc, plan, rel, written, written[rel], what, cap),
                                rel, written[rel])
        if body != written[rel]:
            inside(folder, rel).write_text(body, encoding="utf-8")
            written[rel] = body
        notes.append(note)

    def check() -> tuple[dict[int, tuple[str, str]], list[str], list[str]]:
        ticks = _checklist(ctx, p, desc, plan, written, items, cap) if use_list else {}
        problems, checked = build_problems(folder, code)
        return ticks, problems, checked

    if use_list:
        steps.info("Checking the %d changes asked for against the files.", len(items))
    ticks, problems, checked = check()
    for rnd in range(1, rounds + 1):
        missing = [n for n, (state, _) in ticks.items() if state != "DONE"]
        if not missing and not problems:
            break
        steps.info("Check round %d of %d: %s; asking for fixes.", rnd, rounds, ", ".join(filter(None, [
            f"{len(missing)} change{'s' if len(missing) != 1 else ''} not in the files yet" if missing else "",
            f"{len(problems)} build problem{'s' if len(problems) != 1 else ''}" if problems else ""])))
        if missing:                                          # each to the file the check named, else the longest
            by_file: dict[str, list[int]] = {}
            for n in missing:
                said = ticks[n][1].lower()
                rel = next((r for r in code if r.lower() in said or Path(r).name.lower() in said),
                           max(code, key=lambda r: len(written[r])) if code else None)
                if rel:
                    by_file.setdefault(rel, []).append(n)
            for rel, nums in by_file.items():
                fix(rel, "Make only these changes, which aren't in the file yet:\n\n" + "\n\n".join(items[n] for n in nums))
        if problems:
            problems, checked = build_problems(folder, code)    # again: the edits above may have changed them
            for rel in code:
                mine = [x for x in problems if x.startswith(rel + " ")]
                if mine:
                    fix(rel, "Fix only these build errors (Windows' own C# compiler / PowerShell parser); every "
                             "other line stays exactly as it is. If a class is defined twice across files, keep "
                             "one:\n- " + "\n- ".join(mine[:30]))
        ticks, problems, checked = check()

    # what's true now, for the result and the README
    left, truth = [], []
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
    if ticks:
        done = [n for n in ticks if n not in not_done]
        if not_done:
            left.append(f"{len(not_done)} of {len(ticks)} changes not done ({', '.join(map(str, not_done))})")
        notes.append(f"**Changes asked for:** {len(done)} of {len(ticks)} done"
                     + (". **Not done:** " + "; ".join(f"{n} ({ticks[n][1] or ticks[n][0].lower()})" for n in not_done)
                        if not_done else "."))
        truth.append("Of the numbered changes asked for, these are DONE: " + (", ".join(map(str, done)) or "none")
                     + (". These are NOT done, so don't describe them as done; list them under \"Not done yet\": "
                        + "; ".join(items[n].split("\n")[0][:160] for n in not_done) if not_done else "."))
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
        plan_path.write_text(json.dumps({"summary": summary, "desc": desc, "plan": plan}, indent=2), encoding="utf-8")
        steps.info("Plan ready: %d file%s: %s, and the README.", len(plan["files"]), "" if len(plan["files"]) == 1 else "s",
                   ", ".join(f["path"] for f in plan["files"]))
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
        if target.exists():                                           # done before a pause
            written[rel] = target.read_text(encoding="utf-8", errors="replace")
            continue
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
            body, note = _edit_file(ctx, job_id, _edit_messages(p, desc, plan, rel, written, old), rel, old)
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
            for rel in staged:
                body, note = _edit_file(ctx, job_id, _edit_messages(
                    ps, desc, plan, rel, written, written[rel],
                    f"This is stage {s + 1} of {len(chunks)}: make only the changes above now (the others come in "
                    f"their own stages). If none of them belong in {rel}, send no edits", budget), rel, written[rel])
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
            text = _strip_outer_fence(ctx.chat(_readme_messages(p, desc, plan, written, truth), 1500), True)
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
    return out


def _check_followup_any(p):
    """A follow-up question, or on a build, a new version of it."""
    out = _check_followup(p)
    parent = SMITHY.get(out["parent_id"])
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
            p = {**p, "request": p["request"] + "\n\nBase it on this existing file (don't change the original):\n"
                 + _code_block(p["path"])}
        return build(ctx, p)
    return run


def _title_build(p):
    words = re.sub(r"^(?:me\s+)?(?:a|an|the)\s+", "", p["request"].strip(), flags=re.I).split()[:7]
    while len(words) > 2 and words[-1].lower().strip(",.") in _SMALL_WORDS:   # don't end on "the"
        words.pop()
    return ("the Joomla task: " if p.get("joomla") else "the build: ") + " ".join(words).rstrip(",.;:")


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
