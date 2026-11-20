"""The Smithy's replay check: `smithy.py replay TASK_ID [RUN]`.

Takes a task's last Sandbox test run (the files it tested and what the tests printed) and runs only The Smithy's own
decisions on it, with no model and nothing run: which files each failing test's fix would go to, whether a test that
fails calls a file that gets no fix, which build problems hold the build back, and how the locked and judged tests
stand. Seconds instead of a 30-minute run (Nov 17, the user: "a different angle of attack"; task 58 lost three runs
to fixes sent to the wrong file, each found only after the run failed).
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import smithy_diagnose
import smithy_jobs as sj
from worker_queue import SMITHY

_FAILED = re.compile(r"(?m)^(?:FAIL|ERROR): (\w+)")


def _runs(job_id: int) -> list[Path]:
    """The task's Sandbox runs that finished (they have the tests' output), oldest first."""
    root = SMITHY.results_dir / "sandbox-runs"
    found = [d for d in root.glob(f"{job_id:05d}-*") if (d / "out" / "codes.txt").exists() and (d / "in").is_dir()]
    return sorted(found, key=lambda d: (d / "out" / "codes.txt").stat().st_mtime)


def _payload(job_id: int) -> dict:
    row = SMITHY.get(job_id)
    if row is None:
        raise ValueError(f"there's no Smithy task {job_id}")
    p = json.loads(dict(row)["params"] or "{}")
    parent = p.get("parent_id")
    while parent and not p.get("request"):                 # a follow-up: the request is its first build's
        prow = SMITHY.get(int(parent))
        if prow is None:
            break
        pp = json.loads(dict(prow)["params"] or "{}")
        p["request"], parent = pp.get("request", ""), pp.get("parent_id")
    return p


def _calls_by_test(folder: Path, test: str, failing: list[str], code: list[str]) -> dict[str, list[str]]:
    return {n: sj._called_files(folder, test, [n], code) for n in failing}


def replay(job_id: int, run: int | None = None) -> str:
    runs = _runs(job_id)
    if not runs:
        raise ValueError(f"task {job_id} has no finished Sandbox test run to replay")
    d = next((r for r in runs if r.name == f"{job_id:05d}-{run}"), None) if run else runs[-1]
    if d is None:
        raise ValueError(f"task {job_id} has no finished Sandbox run {run} (there are: "
                         + ", ".join(r.name.split("-")[-1] for r in runs) + ")")
    folder = d / "in"
    p = _payload(job_id)
    plan_path = SMITHY.results_dir / f"{job_id:05d}-plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8")).get("plan", {}) if plan_path.exists() else {}
    same = {f["path"] for f in plan.get("files", []) if f.get("change") == "same"}
    code = sorted(r.name for r in folder.iterdir() if r.is_file())
    decided = sj.decisions(job_id)
    wrong = {n for n, v in decided.items() if v.get("side") in ("test", "both")}
    code_side = {n for n, v in decided.items() if v.get("side") in ("code", "both")}
    codes = {}
    for line in (d / "out" / "codes.txt").read_text(encoding="utf-8", errors="replace").splitlines():
        name, _, rc = line.rpartition(" ")
        if name:
            codes[name.strip()] = rc.strip()
    out = [f"# Replay of task {job_id}, Sandbox run {d.name.split('-')[-1]} (no model; nothing run)", ""]
    warnings: list[str] = []

    # 1. each failing test file: where its fix would go
    for t, rc in codes.items():
        text = (d / "out" / (Path(t).stem + ".txt"))
        printed = text.read_text(encoding="utf-8", errors="replace") if text.exists() else ""
        failing = _FAILED.findall(printed)
        if rc == "0":
            out.append(f"- `{t}`: passed")
            continue
        out.append(f"- `{t}`: failed ({len(failing)} failing: {', '.join(failing) or 'none named'})")
        named = sj.fix_files(folder, t, printed, code, same)
        code_wrong = [n for n in failing if n in code_side]
        named = sj.fix_files_judged(folder, t, named, code, same, any(n in wrong for n in failing), code_wrong,
                                    any(n not in code_wrong for n in failing))
        sent = named[:4]
        out.append(f"  - the fix would go to: {', '.join(sent) or 'nothing'}"
                   + (f" (and not: {', '.join(named[4:])}, past the 4 a round asks)" if named[4:] else ""))
        if code_wrong and all(sj._is_test(r) for r in sent):
            # Nov 18, task 60: a test judged "code is wrong" had its fix sent only to its own locked test file
            warnings.append(f"{', '.join(f'`{n}`' for n in code_wrong)} judged \"the code is wrong\", but the fix "
                            f"would go only to test files: {', '.join(sent) or 'nothing'}")
        for n, files in _calls_by_test(folder, t, failing, code).items():
            side = decided.get(n, {}).get("side")
            out.append(f"  - `{n}` calls {', '.join(files) or 'no build file'}"
                       + (f"; judged: {'the test and the code are both' if side == 'both' else f'the {side} is'} wrong"
                          if side else ""))
            missed = [r for r in files if r not in sent and (r not in same or n in code_side)]
            if side != "test" and missed and not any(r in sent for r in files):
                warnings.append(f"`{n}` calls {', '.join(missed)}, which get no fix (the fix goes to "
                                f"{', '.join(sent)})")
        if t == sj.REQUEST_TESTS and not any(n in wrong for n in failing):
            bad = [r for r in sent if sj._is_test(r)]
            if bad:
                warnings.append(f"{t}'s code fix would go to a test file: {', '.join(bad)}")
        # a file a code-judged test calls may be opened for its fix, though the plan keeps it the same
        opened = {r for n in code_wrong for r in sj._called_files(folder, t, [n], code)}
        if [r for r in sent if r in same and r not in opened]:
            warnings.append(f"`{t}`'s fix would go to a file the plan keeps the same: "
                            + ", ".join(r for r in sent if r in same and r not in opened))

    # 2. the build check: what holds it back, and what's only unused code
    stdlib_only = bool(sj._STDLIB_ONLY.search(p.get("request", "")))
    net_ok = sj.net_ok_for(p.get("request", "") + "\n" + p.get("change", ""))
    problems, _ = sj.build_problems(folder, code, stdlib_only, net_ok)
    dead = [x for x in problems if sj._DEAD_CODE.match(x)]
    real = [x for x in problems if not sj._DEAD_CODE.match(x)]
    out += ["", f"**Build check:** {len(real)} problem{'s' if len(real) != 1 else ''}"
            + (f", {len(dead)} unused code (doesn't hold the build)" if dead else "")]
    out += [f"- {x}" for x in real[:15]]
    if sj.blocks_tests(real):
        warnings.append(f"{len(sj.blocks_tests(real))} build problem(s) would stop the Sandbox tests running")

    # 3. tests that keep failing, and how they're judged
    hist = SMITHY.results_dir / f"{job_id:05d}-history.jsonl"
    stuck = smithy_diagnose.stuck_tests(smithy_diagnose.read(hist)) if hist.exists() else []
    if stuck or decided:
        out += ["", "**Locked and judged tests:**"]
        for s in stuck:
            v = decided.get(s["test"], {})
            out.append(f"- `{s['test']}`: failed {s['runs']} runs in a row"
                       + (f"; judged {v.get('times', 1)}x: the {v.get('side')} is wrong" if v else "; not judged yet"))
        for n, v in decided.items():
            if n not in {s["test"] for s in stuck}:
                out.append(f"- `{n}`: judged: the {v.get('side')} is wrong (passing now, or not in this run)")

    # 4. the request's own tests, intact?
    req = folder / sj.REQUEST_TESTS
    if req.exists():
        try:
            n_tests = sum(isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) and x.name.startswith("test")
                          for x in ast.walk(ast.parse(req.read_text(encoding="utf-8", errors="replace"))))
            out += ["", f"**{sj.REQUEST_TESTS}:** {n_tests} tests"]
        except SyntaxError as e:
            warnings.append(f"{sj.REQUEST_TESTS} doesn't parse: {e}")

    out += ["", "**Warnings:** " + ("none" if not warnings else "")]
    out += [f"- {w}" for w in warnings]
    return "\n".join(out)
