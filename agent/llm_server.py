"""Voice Othos's conversation model on llama.cpp's own server (llama-server), when voice-llm.json says so.

Why (Nov 19, the cache slots plan in Othos_future\\code): reading the prompt is the slow part on this CPU, and
the model's server skips what it has already read only while the start of the prompt stays the same. With Ollama,
offering tools changed the top of the prompt, so the whole call was read again (the 30 s reply on the Nov 19 call).
Here:
- llama32-othos.jinja keeps the top of the prompt the same on every turn (tools go into the newest message);
- two slots: slot 0 is the call and nothing else uses it, so nothing wipes its memory;
- the instructions, once read, are saved to a file and loaded at the next call's start ("parking", an idea from
  colibri by Vincenzo Fornaro, Apache-2.0; no colibri code is used); the file holds the model's working numbers,
  not readable text, and stays in state\\slots on this PC;
- "cache reuse": when the instructions change mid-call (the weather arriving), only the changed part is read.
It opens the llama3.2 file Ollama already has (no model download). Ollama still runs notes search, the camera and
gaming mode. Nothing here leaves this PC: the server listens on 127.0.0.1 only.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import httpx

log = logging.getLogger("othos")
HERE = Path(__file__).resolve().parent
SETTINGS = HERE / "voice-llm.json"
TEMPLATE = HERE / "llama32-othos.jinja"
SLOT_DIR = HERE / "state" / "slots"
SLOT_FILE = "othos-call.bin"
CALL_SLOT = 0          # the call's own slot; slot 1 is for anything else that uses this model


def settings() -> dict:
    try:
        raw = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return {"backend": str(raw.get("backend") or "ollama").lower(), "llama_server_exe": raw.get("llama_server_exe") or "",
            "port": int(raw.get("port") or 11440), "threads": int(raw.get("threads") or 0),
            "context_per_slot": int(raw.get("context_per_slot") or 6144),
            # tokens read per step: llama-server notices a cancelled request only between steps, so a smaller step
            # lets the next turn start sooner (Nov 20, 8:20 call: a cancelled request held the slot 23 s)
            "batch": max(32, int(raw.get("batch") or 128))}


def enabled() -> bool:
    return settings()["backend"] == "llama-server"


def root() -> str:
    return f"http://127.0.0.1:{settings()['port']}"


def find_exe(setting: str = "") -> Path | None:
    """The llama-server.exe to run: the setting, else the newest under Documents\\llama.cpp."""
    if setting:
        p = Path(os.path.expandvars(setting))
        return p if p.is_file() else None
    found = sorted((Path.home() / "Documents" / "llama.cpp").glob("*/llama-server.exe"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0] if found else None


def ollama_gguf(model: str = "llama3.2:3b", models_dir: Path | None = None) -> Path | None:
    """The model file Ollama already has for `model` ("llama3.2:3b"), read from Ollama's own list of its files."""
    base = models_dir or Path(os.getenv("OLLAMA_MODELS") or Path.home() / ".ollama" / "models")
    name, _, tag = model.partition(":")
    ns, _, short = name.rpartition("/")
    manifest = base / "manifests" / "registry.ollama.ai" / (ns or "library") / short / (tag or "latest")
    try:
        layers = json.loads(manifest.read_text(encoding="utf-8"))["layers"]
    except (OSError, ValueError, KeyError):
        return None
    digest = next((x["digest"] for x in layers if x.get("mediaType") == "application/vnd.ollama.image.model"), "")
    blob = base / "blobs" / digest.replace(":", "-")
    return blob if digest and blob.is_file() else None


def threads(setting: int = 0) -> int:
    """This PC's cores minus 2 (hearing, the voice and the call need some: Nov 19, the voice detector fell 19 s
    behind while the processor was full)."""
    if setting > 0:
        return setting
    try:
        import psutil
        cores = psutil.cpu_count(logical=False) or os.cpu_count() or 4
    except Exception:
        cores = os.cpu_count() or 4
    return max(2, cores - 2)


def command(exe: Path, gguf: Path, s: dict) -> list[str]:
    slots = 2
    return [str(exe), "-m", str(gguf), "--host", "127.0.0.1", "--port", str(s["port"]),
            "-c", str(s["context_per_slot"] * slots), "--parallel", str(slots), "-t", str(threads(s["threads"])),
            "--jinja", "--chat-template-file", str(TEMPLATE),
            "--cache-reuse", "256",                       # after a mid-call change, re-read only what changed
            "-b", str(s.get("batch", 128)), "-ub", str(s.get("batch", 128)),   # a cancelled turn lets go within a step
            "--slot-save-path", str(SLOT_DIR),
            "-fa", "on", "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",   # half the memory for what's read
            "--no-webui"]


def starting() -> bool:
    """Its program is running for our port but not answering yet (still loading the model)."""
    return enabled() and _on_port(settings()["port"]) and not running()


def running(base: str | None = None, timeout: float = 1.5) -> bool:
    try:
        return httpx.get(f"{base or root()}/health", timeout=timeout).status_code == 200
    except Exception:
        return False


_START_LOCK = threading.Lock()


def _on_port(port: int) -> bool:
    """Is a llama-server for this port already running (started, perhaps still loading its model)?"""
    try:
        import psutil
        for p in psutil.process_iter(["name", "cmdline"]):
            if (p.info["name"] or "").lower() == "llama-server.exe" and "--port" in (p.info["cmdline"] or []):
                cmd = p.info["cmdline"]
                if cmd[cmd.index("--port") + 1:cmd.index("--port") + 2] == [str(port)]:
                    return True
    except Exception:
        pass
    return False


class _FileLock:
    """Held while checking and starting, so two job processes starting at once (the call's prewarm runs in several)
    can't both start a server (Nov 19, 12:17: two servers, 2.7 GB held by the idle one)."""

    def __init__(self, path: Path):
        self.path, self.f = path, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "a+b")
        try:
            import msvcrt
            end = time.monotonic() + 30
            while True:
                try:
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > end:
                        break
                    time.sleep(0.2)
        except ImportError:
            pass
        return self

    def __exit__(self, *a):
        try:
            import msvcrt
            self.f.seek(0)
            msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        self.f.close()


def _wait_ready(wait: float) -> bool:
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if running():
            return True
        time.sleep(1.0)
    return False


def start(model: str = "llama3.2:3b", wait: float = 90.0) -> bool:
    """Start llama-server hidden if it isn't running or already starting (blocking; call it from a thread).
    True once it answers."""
    s = settings()
    if running():
        return True
    exe, gguf = find_exe(s["llama_server_exe"]), ollama_gguf(model)
    if not exe or not gguf:
        log.warning("llama-server not started: %s", "llama-server.exe not found" if not exe else f"Ollama has no {model}")
        return False
    with _START_LOCK, _FileLock(SLOT_DIR / ".start.lock"):
        if running():
            return True
        if _on_port(s["port"]):
            log.info("llama-server already starting: waiting for it")
        else:
            SLOT_DIR.mkdir(parents=True, exist_ok=True)
            logs = HERE / "logs"
            logs.mkdir(exist_ok=True)
            with open(logs / "llama-server.log", "ab") as out:
                subprocess.Popen(command(exe, gguf, s), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), cwd=str(exe.parent))
            log.info("llama-server starting on port %d with %d threads", s["port"], threads(s["threads"]))
    if _wait_ready(wait):
        log.info("llama-server ready")
        return True
    log.warning("llama-server didn't answer within %.0f s", wait)
    return False


_HTTP: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    """One shared client for the local server. Plain http on 127.0.0.1, so no certificates to load: building a
    default client loaded them every time (about 1 s of processor time, freezing the call on every turn: the Nov 19
    13:31 call's "event loop blocked ... create_default_context")."""
    global _HTTP
    if _HTTP is None or _HTTP.is_closed:
        _HTTP = httpx.AsyncClient(verify=False)
    return _HTTP


async def wait_ready(wait: float = 90.0, client: httpx.AsyncClient | None = None) -> bool:
    """True once the server answers /health (it answers 503 while it loads the model). Fast when it's up already."""
    import asyncio
    end = time.monotonic() + wait
    c = client or _http()
    while True:
        try:
            if (await c.get(f"{root()}/health", timeout=3)).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        if time.monotonic() >= end:
            return False
        await asyncio.sleep(0.5)


def request_extras() -> dict:
    """Sent with every conversation request: the call's own slot, and keep what's been read."""
    return {"id_slot": CALL_SLOT, "cache_prompt": True}


async def warm(instructions: str, client: httpx.AsyncClient | None = None) -> None:
    """At a call's start: load the instructions read on the last call, read whatever changed (the time line, last),
    and save it for the next call. Runs while the greeting is spoken; errors are logged and ignored."""
    base = root()
    c = client or _http()
    t = time.monotonic()
    try:
        # the server answers 503 while it loads the model (Nov 19: a warm-up sent then read nothing, said "ready in
        # 0.5 s", and saved an empty slot): wait for it, and count only answers that are 200
        if not await wait_ready(client=c):
            log.info("llama-server warm-up skipped: the server never answered")
            return
        r = await c.post(f"{base}/slots/{CALL_SLOT}?action=restore", json={"filename": SLOT_FILE}, timeout=20)
        restored = r.status_code == 200
        (await c.post(f"{base}/v1/chat/completions", timeout=120, json={
            "messages": [{"role": "system", "content": instructions}], "max_tokens": 1, "temperature": 0,
            **request_extras()})).raise_for_status()
        (await c.post(f"{base}/slots/{CALL_SLOT}?action=save", json={"filename": SLOT_FILE}, timeout=20)).raise_for_status()
        log.info("llama-server: instructions ready in %.1f s (%s)", time.monotonic() - t,
                 "kept from the last call" if restored else "read fresh")
    except Exception as e:
        log.info("llama-server warm-up skipped: %s", type(e).__name__)
