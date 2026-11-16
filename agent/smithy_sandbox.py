"""The Smithy: a build's tests, run in a throwaway Windows Sandbox (review step 3, OK'd Nov 14: "Your top pick").

The Smithy never runs anything on this PC. With `sandbox_tests: true` in smithy.json (off by default), a
build's test files may run inside Windows Sandbox:
- networking OFF, no clipboard, printer, microphone or camera; the Sandbox is thrown away afterwards
- only three folders are mapped: the build's files (read-only), the Python installer (read-only, copied from
  Downloads only after its signature checks as the Python Software Foundation's) and one output folder
- Python 3.12.10 is installed inside, the build is copied to C:\\Work there, each test runs with a time limit,
  and only its output and exit code come back
- one Sandbox at a time: if one is already running (an installer test), the tests aren't run, and it says so
- the Sandbox opens a window while it runs (Windows needs a signed-in session to run anything in it)

Nothing from the Sandbox is applied, and the copied installer is deleted afterwards (the user's rule for kits).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from xml.sax.saxutils import escape

log = logging.getLogger("worker")
steps = logging.getLogger("worker.step")
_NO_WINDOW = 0x08000000
PYTHON_SIGNER = "Python Software Foundation"


def is_test(rel: str) -> bool:
    return bool(re.match(r"(?i)^(?:test_\w*|\w*_test)\.py$", Path(rel).name))


def _wsb(*args: str, timeout: float = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(["wsb", *args], capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "Windows Sandbox's wsb command isn't on this PC"
    except subprocess.TimeoutExpired:
        return 124, "wsb didn't answer in time"


def running() -> list[str]:
    code, out = _wsb("list", "--raw")
    if code != 0:
        return []
    try:
        data = json.loads(out)
        ids = data.get("WindowsSandboxEnvironments") or data.get("environments") or data
        return [str(x.get("Id") or x.get("id") or x) for x in ids] if isinstance(ids, list) else []
    except Exception:
        return re.findall(r"[0-9a-f]{8}-[0-9a-f-]{27}", out, re.I)


def _remember(path: Path, sid: str, add: bool) -> None:
    """The Sandboxes The Smithy started, and which run of it (process id) started each: started.json."""
    try:
        d = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        d = {}
    if add:
        d[sid] = os.getpid()
    else:
        d.pop(sid, None)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass


def _stop_leftovers(path: Path) -> list[str]:
    """Stop the Sandboxes an earlier run of The Smithy started and never stopped (Nov 16: closed mid-test at 8:03,
    its Sandbox kept running, and the next run's tests were skipped as "another Sandbox is running"). Only ones it
    recorded, from another run; one this run is using, or any Sandbox it didn't start, is left alone."""
    try:
        d = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return []
    now = set(running())
    stopped = []
    for sid, pid in list(d.items()):
        if sid in now and pid != os.getpid():
            _wsb("stop", "--id", sid, timeout=120)
            log.info("Sandbox %s was left running by an earlier run of The Smithy; stopped it", sid)
            steps.info("A Sandbox left over from an earlier run was stopped, so the tests can run.")
            stopped.append(sid)
        if sid not in now or sid in stopped:
            d.pop(sid, None)
    try:
        path.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass
    return stopped


def _signed_by_python(path: Path) -> bool:
    ps = ("$s = Get-AuthenticodeSignature -LiteralPath $env:SMITHY_PY; "
          "if ($s.Status -eq 'Valid' -and $s.SignerCertificate.Subject -match 'Python Software Foundation') "
          "{ 'ok' } else { 'no' }")
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                           text=True, timeout=60, creationflags=_NO_WINDOW, env={**os.environ, "SMITHY_PY": str(path)})
        return p.stdout.strip() == "ok"
    except Exception:
        return False


def _config(kit: Path) -> str:
    def mapped(host: Path, inside: str, read_only: bool) -> str:
        return (f"<MappedFolder><HostFolder>{escape(str(host))}</HostFolder><SandboxFolder>{inside}</SandboxFolder>"
                f"<ReadOnly>{'true' if read_only else 'false'}</ReadOnly></MappedFolder>")
    return ("<Configuration><Networking>Disable</Networking><vGPU>Disable</vGPU>"
            "<ClipboardRedirection>Disable</ClipboardRedirection><PrinterRedirection>Disable</PrinterRedirection>"
            "<AudioInput>Disable</AudioInput><VideoInput>Disable</VideoInput><MemoryInMB>3072</MemoryInMB>"
            "<MappedFolders>" + mapped(kit / "in", r"C:\Kit\in", True) + mapped(kit / "setup", r"C:\Kit\setup", True)
            + mapped(kit / "out", r"C:\Kit\out", False) + "</MappedFolders></Configuration>")


def _script(installer: str, tests: list[str], per_test_seconds: int) -> str:
    lines = ["@echo off",
             f"start /wait C:\\Kit\\setup\\{installer} /quiet InstallAllUsers=0 PrependPath=0 Include_test=0 "
             "Include_launcher=0 Include_doc=0 Include_tcltk=0 TargetDir=C:\\Py",
             "if not exist C:\\Py\\python.exe (echo Python didn't install> C:\\Kit\\out\\setup.txt& echo done> "
             "C:\\Kit\\out\\done.txt& exit /b 1)",
             "xcopy C:\\Kit\\in C:\\Work\\ /e /i /q /y >nul",
             "cd /d C:\\Work"]
    for t in tests:
        name = Path(t).stem
        run = f"C:\\Py\\python.exe {t}"
        lines += [f"powershell -NoProfile -Command \"$p = Start-Process -FilePath cmd.exe -ArgumentList '/c {run} > "
                  f"C:\\Kit\\out\\{name}.txt 2>&1' -WorkingDirectory C:\\Work -PassThru -WindowStyle Hidden; "
                  f"if (-not $p.WaitForExit({per_test_seconds * 1000})) {{ Stop-Process -Id $p.Id -Force; "
                  f"Add-Content C:\\Kit\\out\\codes.txt '{t} timeout' }} else {{ Add-Content C:\\Kit\\out\\codes.txt "
                  f"('{t} ' + $p.ExitCode) }}\""]
    lines.append("echo done> C:\\Kit\\out\\done.txt")
    return "\r\n".join(lines) + "\r\n"


def run_tests(job_id: int, folder: Path, rels: list[str], cfg: dict, results_dir: Path, should_stop=None,
              round_no: int = 1) -> dict:
    """{"status": "off"|"busy"|"unavailable"|"passed"|"failed"|"timeout", "tests": {name: "passed"|"failed"|
    "timeout"|"not run"}, "output": {name: last lines}, "why": plain words}."""
    tests = [r for r in rels if is_test(r)]
    if not cfg.get("sandbox_tests"):
        return {"status": "off", "tests": {}, "output": {}, "why": "Sandbox test runs are off (sandbox_tests)"}
    if not tests:
        return {"status": "off", "tests": {}, "output": {}, "why": "the build has no test files"}
    started_file = results_dir / "sandbox-runs" / "started.json"
    if running():
        _stop_leftovers(started_file)
    if running():
        return {"status": "busy", "tests": {t: "not run" for t in tests}, "output": {},
                "why": "another Windows Sandbox is running (only one at a time)"}
    installer = Path(cfg.get("sandbox_python") or Path.home() / "Downloads" / "python-3.12.10-amd64.exe")
    if not installer.is_file() or not _signed_by_python(installer):
        return {"status": "unavailable", "tests": {t: "not run" for t in tests}, "output": {},
                "why": f"the Python installer ({installer.name}) isn't there or isn't signed by the {PYTHON_SIGNER}"}
    kit = results_dir / "sandbox-runs" / f"{job_id:05d}-{round_no}"
    if kit.exists():
        shutil.rmtree(kit, ignore_errors=True)
    for d in ("in", "setup", "out"):
        (kit / d).mkdir(parents=True)
    for r in rels:                                         # the build's own files only
        target = kit / "in" / r
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(folder / r, target)
    shutil.copyfile(installer, kit / "setup" / installer.name)
    per_test = int(cfg.get("sandbox_test_seconds", 120))
    (kit / "setup" / "run.cmd").write_text(_script(installer.name, tests, per_test), encoding="ascii")
    limit = time.monotonic() + 60 * float(cfg.get("sandbox_minutes", 15))
    sid, viewer = "", None
    try:
        steps.info("Running the tests in a throwaway Windows Sandbox (networking off): %s.", ", ".join(tests))
        code, out = _wsb("start", "--config", _config(kit), "--raw", timeout=180)
        m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", out, re.I)
        if code != 0 or not m:
            return {"status": "unavailable", "tests": {t: "not run" for t in tests}, "output": {},
                    "why": "Windows Sandbox didn't start: " + out.strip()[:200]}
        sid = m.group(0)
        _remember(started_file, sid, add=True)
        viewer = subprocess.Popen(["wsb", "connect", "--id", sid], creationflags=_NO_WINDOW)   # a signed-in session
        started, runner = False, None
        while time.monotonic() < limit and not started:
            if should_stop:
                should_stop(0)
            # left running (never cut off: that could stop the tests inside); retried only when it fails at
            # once, which it does until the signed-in session is ready (0x80070520)
            runner = subprocess.Popen(["wsb", "exec", "--id", sid, "-r", "ExistingLogin", "-c", r"C:\Kit\setup\run.cmd"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
            try:
                started = runner.wait(timeout=20) == 0
            except subprocess.TimeoutExpired:
                started = True                             # still going: the script is running
            if not started:
                time.sleep(5)
        while time.monotonic() < limit and not (kit / "out" / "done.txt").exists():
            if should_stop:
                should_stop(0)
            time.sleep(3)
        timed_out = not (kit / "out" / "done.txt").exists()
    finally:
        if sid:
            _wsb("stop", "--id", sid, timeout=120)
            _remember(started_file, sid, add=False)
        if viewer is not None:
            try:
                viewer.kill()
            except Exception:
                pass
        shutil.rmtree(kit / "setup", ignore_errors=True)    # the copied installer goes
    codes = {}
    codes_file = kit / "out" / "codes.txt"
    if codes_file.exists():
        for line in codes_file.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.strip().rsplit(" ", 1)
            if len(parts) == 2:
                codes[parts[0]] = "timeout" if parts[1] == "timeout" else "passed" if parts[1] == "0" else "failed"
    result = {t: codes.get(t, "timeout" if timed_out else "not run") for t in tests}
    output, full = {}, {}                  # full: the whole output, so a fix can find every file a failure names
    for t in tests:                        # (task 55: the traceback naming academy_readme.py was above the last 25)
        f = kit / "out" / f"{Path(t).stem}.txt"
        if f.exists():
            full[t] = f.read_text(encoding="utf-8", errors="replace")
            output[t] = "\n".join(full[t].splitlines()[-60:])
    if (kit / "out" / "setup.txt").exists():
        return {"status": "unavailable", "tests": result, "output": output, "full": full,
                "why": "Python didn't install in the Sandbox"}
    status = ("timeout" if timed_out or "timeout" in result.values() else
              "passed" if all(v == "passed" for v in result.values()) else "failed")
    log.info("build %d: Sandbox tests %s: %s", job_id, status, result)
    steps.info("Sandbox test run: %s.", ", ".join(f"{t} {v}" for t, v in result.items()))
    return {"status": status, "tests": result, "output": output, "full": full, "why": ""}
