"""The Smithy: task types. Coding only, CPU only; everything it makes is a draft.

- review    a code file: bugs, risks, small improvements
- fix       a code file: a diff (never applied)
- script    a script (saved, never run)
- joomla    a Joomla / YOOtheme override or CSS, built like a small project
- build     a project from scratch: a plan first, then each file in turn, laid
            out like the real project in its own folder, with a README
- followup  a question about an earlier result; on a build or Joomla task it
            makes a new version of the build, in its own folder

Rules, enforced here in code, not in the prompt:
- Files are only read through worker_files (allowlisted folders, private files
  blocked, keys redacted before anything reaches the model).
- A build only writes inside its own folder, drafts\\smithy\\NNNNN-<name>\\: every
  path the model plans is checked (relative, no "..", no drive, no hidden or key
  files, no programs) and checked again after resolving. Nothing is run or
  installed; the user copies what they want into place themselves.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path, PurePosixPath

import worker_files
from worker_engine import FORMAT, JobType, plain_summary, split_result
from worker_queue import SMITHY
from worker_shared import RULES, USER, USERS, redact

log = logging.getLogger("worker")

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
                 ".pdf", ".db", ".sqlite"}
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
                     "\"changed\" or \"new\". Keep the same name."})
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
                 f"Now write the complete file {rel} ({purpose}). " +
                 ("Reply with only the file's text, no outer code fence." if md else
                  "Reply with only one fenced code block that holds the whole file, nothing before or after it.")})
    return msgs


def _readme_messages(p: dict, desc: str, plan: dict, written: dict[str, str]) -> list[dict]:
    flavour = ("\n\n" + JOOMLA) if p.get("joomla") else ""
    listing = "\n".join(f"- {path} ({len(body.splitlines())} lines)" for path, body in written.items())
    return [{"role": "system", "content": SYSTEM + flavour},
            {"role": "user", "content": f"The project: {redact(p['request'])}\n\nThe design:\n{desc[:4000]}\n\n"
                                        f"Its files:\n{listing}\n\nWrite its README.md: what it is, what each file "
                                        "does, where each file goes in the real project, how to install or use "
                                        "it, and anything to check first. Plain markdown, no outer code fence."}]


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


def run_build(ctx, p: dict):
    """Plan, then each file, then the README. Picks up where it left off after a pause."""
    job_id = ctx.row["id"]
    max_files = int(ctx.cfg.get("max_build_files", 20))
    budget = int(ctx.cfg.get("context_tokens", 8192)) * 3 // 2      # characters of earlier files to show
    results = SMITHY.results_dir
    results.mkdir(parents=True, exist_ok=True)
    plan_path = results / f"{job_id:05d}-plan.json"
    parent = _parent_build(p["parent_id"]) if p.get("parent_id") else None
    if p.get("parent_id") and parent is None:
        raise ValueError(f"Smithy task {p['parent_id']} isn't a build I can make a new version of")
    if parent:
        p = {**parent["params"], **p, "request": parent["params"].get("request", "") +
             f"\n\nNew version: {p['change']}"}

    # 1. the plan (kept, so a paused build carries on instead of starting again)
    if plan_path.exists():
        saved = json.loads(plan_path.read_text(encoding="utf-8"))
        summary, desc, plan = saved["summary"], saved["desc"], saved["plan"]
    else:
        ctx.step(0.0, 0.10)
        msgs = _plan_messages(p, max_files, parent)
        answer = ctx.chat(msgs, 1200)
        try:
            summary, desc, plan = parse_plan(answer, max_files)
        except ValueError as problem:
            # one more try, saying what was wrong (small models sometimes drift)
            log.info("task %d: plan not usable (%s); asking again", job_id, problem)
            again = ctx.chat(msgs + [{"role": "assistant", "content": answer},
                                     {"role": "user", "content": f"I can't use that plan: {problem}. Reply with only "
                                      "the corrected ```json block (name and files), with real relative paths, "
                                      "nothing else."}], 800)
            try:
                summary, desc, plan = parse_plan(answer + "\n\n" + again, max_files)
            except ValueError:
                # kept so it can be looked at; the task fails with the reason
                (results / f"{job_id:05d}-plan-unreadable.txt").write_text(
                    answer + "\n\n---\n\n" + again, encoding="utf-8")
                raise
        if parent:
            plan["name"] = parent["plan"].get("name") or plan["name"]
        plan_path.write_text(json.dumps({"summary": summary, "desc": desc, "plan": plan}, indent=2), encoding="utf-8")
    version = int(p.get("version", 1))
    name = plan["name"] or slug(p["request"])
    folder = results / (f"{job_id:05d}-{name}" + (f"-v{version}" if version > 1 else ""))
    folder.mkdir(parents=True, exist_ok=True)

    # 2. each file in turn, with the plan and the files so far as context
    written: dict[str, str] = {}
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
            shutil.copyfile(old_path, target)
            written[rel] = target.read_text(encoding="utf-8", errors="replace")
            continue
        ctx.step(0.10 + 0.80 * i / n, 0.10 + 0.80 * (i + 1) / n)
        old = old_path.read_text(encoding="utf-8", errors="replace") if old_path else None
        text = ctx.chat(_file_messages(p, desc, plan, rel, written, budget, old), 3000)
        body = _strip_outer_fence(text, rel.lower().endswith(".md"))
        target.write_text(body, encoding="utf-8")
        written[rel] = body

    # 3. the README, last
    readme = inside(folder, "README.md")
    if not readme.exists():
        ctx.step(0.90, 0.95)
        readme.write_text(_strip_outer_fence(ctx.chat(_readme_messages(p, desc, plan, written), 1500), True),
                          encoding="utf-8")

    files = sorted(written) + ["README.md"]
    what = "Joomla override" if p.get("joomla") else "project"
    spoken = (f"I built {'version ' + str(version) + ' of ' if version > 1 else ''}the {what} called "
              f"{name.replace('-', ' ')}: {len(files)} files, in its own folder in the Smithy's drafts. "
              "Nothing has been installed or run. " + plain_summary(summary, 2))
    written["README.md"] = readme.read_text(encoding="utf-8", errors="replace")
    listing = "\n".join(f"- `{rel}` ({len(written[rel].splitlines())} lines)" for rel in files)
    full = (f"**Folder:** `{folder}`\n\n{desc}\n\n### Files\n\n{listing}\n\n"
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
                   lambda p: f"the fix for {Path(p['path']).name}", max_tokens=1500, artifact="diff", coding=True),
    "script": JobType("script", "Write a script (saved as a draft, never run).", "code",
                      _script, lambda p: {"request": _text(p, "request", "what the script should do", 3)},
                      lambda p: "the script you asked for", max_tokens=1500, artifact="script", coding=True),
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
