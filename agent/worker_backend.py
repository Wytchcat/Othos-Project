"""Firestarter and the Smithy: the programs that run their models. CPU only, always.

- OllamaBackend: Ollama's own API with num_gpu 0 on every request (no layers on
  the graphics card). With ollama.start_own (new build) the worker also runs its
  own Ollama on own_port with the graphics card hidden (CUDA_VISIBLE_DEVICES=-1),
  so it can't touch the GPU even by mistake.
- LlamaServerBackend: llama.cpp's llama-server (or ik_llama.cpp's), started with
  -ngl 0 and the graphics card hidden. Threads are fixed per start, so a change
  (held back during a game) restarts it; the model is still in memory, so that's quick.

Both stream, and check should_stop(chars_so_far) between pieces so a task can be paused or
stopped mid-answer.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

import httpx
import psutil

import worker_shared

log = logging.getLogger("worker")

CPU_ONLY_ENV = {"CUDA_VISIBLE_DEVICES": "-1", "HIP_VISIBLE_DEVICES": "-1", "GGML_VK_VISIBLE_DEVICES": ""}


class Stopped(Exception):
    """should_stop() asked to stop; .reason says why."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _set_priority(proc: subprocess.Popen | None, low: bool) -> None:
    """Below-normal priority for a process the worker started (and its children)."""
    if proc is None or proc.poll() is not None:
        return
    level = psutil.BELOW_NORMAL_PRIORITY_CLASS if low else psutil.NORMAL_PRIORITY_CLASS
    try:
        p = psutil.Process(proc.pid)
        for q in [p, *p.children(recursive=True)]:
            q.nice(level)
    except Exception as e:
        log.warning("couldn't change priority: %s", e)


def _read_timeout(cfg: dict) -> httpx.Timeout:
    """How long to wait for the local model's next words: local_read_seconds (default 600). A long step on a
    small CPU can take longer than 10 minutes before the first word (The Smithy, Nov 15: task 51 timed out)."""
    return httpx.Timeout(30, read=float(cfg.get("local_read_seconds", 600)))


def _wait_http(url: str, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


class OllamaBackend:
    name = "ollama"

    def __init__(self, cfg: dict, log_dir: Path, prefix: str = "worker"):
        o = cfg.get("ollama", {})
        self.cfg = cfg
        self.own: subprocess.Popen | None = None
        self.url = o.get("url", "http://127.0.0.1:11434").rstrip("/")
        if o.get("start_own"):
            port = int(o.get("own_port", 11435))
            self.url = f"http://127.0.0.1:{port}"
            if not _wait_http(f"{self.url}/api/version", 1):
                exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
                env = {**os.environ, **CPU_ONLY_ENV, "OLLAMA_HOST": f"127.0.0.1:{port}"}
                logf = open(log_dir / f"{prefix}-ollama.log", "ab")
                self.own = subprocess.Popen([str(exe), "serve"], env=env, stdout=logf, stderr=logf,
                                            creationflags=subprocess.CREATE_NO_WINDOW)
                log.info("started the worker's own CPU-only Ollama on port %d (pid %d)", port, self.own.pid)
        elif not _wait_http(f"{self.url}/api/version", 1):
            # the worker runs without voice Othos: start the shared Ollama hidden, like
            # Start Othos does, and leave it running (voice Othos and the other worker use it too)
            exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
            # a hidden console that its helpers (llama-server, gpu-discover) share. Not DETACHED_PROCESS:
            # that gives Ollama no console at all, Windows then ignores CREATE_NO_WINDOW, and every helper
            # opened its own Terminal window when The Smithy started (Nov 14).
            # Started through a short-lived cmd ("start /B"), so it is not a child of this worker: the apps
            # stop a worker with "taskkill /T" (the whole family), which used to take the shared Ollama
            # down too, for the other worker and voice Othos (Nov 14). Once that cmd has exited, taskkill
            # can't find Ollama through it.
            # Its output goes to NUL, not this worker's own: otherwise Ollama inherits the worker's log
            # (logs\worker.log, smithy.log) and holds it open, and the app can't start its worker again
            # while Ollama runs ("Worker stopped", Nov 14).
            subprocess.Popen(f'cmd.exe /d /s /c "start "" /B "{exe}" serve >> "{log_dir / "ollama.log"}" 2>&1"',
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW)
            log.info("Ollama wasn't running: started it hidden, apart from this worker")
        if not _wait_http(f"{self.url}/api/version", 30):
            raise RuntimeError(f"Ollama isn't answering at {self.url}")

    def list_models(self) -> list[str]:
        try:
            return sorted(m["name"] for m in httpx.get(f"{self.url}/api/tags", timeout=10).json().get("models", []))
        except Exception:
            return []

    def set_low_priority(self, low: bool) -> None:
        _set_priority(self.own, low)   # only its own Ollama: the shared one also serves voice Othos

    def chat(self, model: str, messages: list[dict], threads: int, max_tokens: int, should_stop) -> str:
        body = {"model": model, "messages": messages, "stream": True,
                "keep_alive": self.cfg.get("keep_alive", "10m"),
                "options": {"num_gpu": 0, "num_thread": threads,
                            "num_ctx": int(self.cfg.get("context_tokens", 8192)), "num_predict": max_tokens}}
        schema = worker_shared.ANSWER_SCHEMA.get()
        if schema:
            body["format"] = schema                    # Ollama holds the answer to this shape
        out = []
        with httpx.stream("POST", f"{self.url}/api/chat", json=body, timeout=_read_timeout(self.cfg)) as r:
            if r.status_code != 200:
                raise RuntimeError(f"Ollama error {r.status_code}: {r.read().decode(errors='replace')[:300]}")
            for line in r.iter_lines():
                should_stop(sum(map(len, out)))
                if not line.strip():
                    continue
                piece = json.loads(line)
                if piece.get("error"):
                    raise RuntimeError(piece["error"])
                out.append(piece.get("message", {}).get("content", ""))
                if piece.get("done"):
                    break
        return "".join(out)

    def close(self) -> None:
        if self.own and self.own.poll() is None:
            self.own.terminate()


class LlamaServerBackend:
    name = "llama-server"

    def __init__(self, cfg: dict, log_dir: Path, prefix: str = "worker"):
        s = cfg.get("llama_server", {})
        self.cfg, self.log_dir, self.prefix = cfg, log_dir, prefix
        self.exe = s.get("exe", "")
        self.models_dir = Path(s.get("models_dir", "") or ".")
        self.port = int(s.get("port", 11436))
        self.extra = [str(a) for a in s.get("extra_args", [])]
        self.url = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None
        self.loaded: tuple[str, int] | None = None
        if not self.exe or not Path(self.exe).exists():
            raise RuntimeError(f"llama-server.exe not found: '{self.exe}' (set llama_server.exe in the settings)")

    def list_models(self) -> list[str]:
        return sorted(p.stem for p in self.models_dir.glob("*.gguf"))

    def _ensure(self, model: str, threads: int) -> None:
        if self.loaded == (model, threads) and self.proc and self.proc.poll() is None:
            return
        self.close()
        path = self.models_dir / f"{model}.gguf"
        if not path.exists():
            raise RuntimeError(f"model file not found: {path}")
        cmd = [self.exe, "-m", str(path), "-t", str(threads), "-ngl", "0",
               "-c", str(int(self.cfg.get("context_tokens", 8192))),
               "--host", "127.0.0.1", "--port", str(self.port), *self.extra]
        logf = open(self.log_dir / f"{self.prefix}-llama-server.log", "ab")
        self.proc = subprocess.Popen(cmd, env={**os.environ, **CPU_ONLY_ENV}, stdout=logf, stderr=logf,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        log.info("started llama-server: %s, %d threads (pid %d)", model, threads, self.proc.pid)
        if not _wait_http(f"{self.url}/health", 600):
            raise RuntimeError("llama-server didn't become ready")
        self.loaded = (model, threads)

    def set_low_priority(self, low: bool) -> None:
        _set_priority(self.proc, low)

    def chat(self, model: str, messages: list[dict], threads: int, max_tokens: int, should_stop) -> str:
        self._ensure(model, threads)
        body = {"model": model, "messages": messages, "stream": True, "max_tokens": max_tokens}
        schema = worker_shared.ANSWER_SCHEMA.get()
        if schema:
            body["json_schema"] = schema               # llama-server holds the answer to this shape
        out = []
        with httpx.stream("POST", f"{self.url}/v1/chat/completions", json=body,
                          timeout=_read_timeout(self.cfg)) as r:
            if r.status_code != 200:
                raise RuntimeError(f"llama-server error {r.status_code}: {r.read().decode(errors='replace')[:300]}")
            for line in r.iter_lines():
                should_stop(sum(map(len, out)))
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                delta = json.loads(data)["choices"][0].get("delta", {})
                out.append(delta.get("content") or "")
        return "".join(out)

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except Exception:
                self.proc.kill()
        self.proc, self.loaded = None, None


def available_models(cfg: dict) -> list[str]:
    """Models the switcher can offer, without starting anything."""
    if cfg.get("backend") == "llama-server":
        d = Path(cfg.get("llama_server", {}).get("models_dir", "") or ".")
        return sorted(p.stem for p in d.glob("*.gguf"))
    o = cfg.get("ollama", {})
    url = f"http://127.0.0.1:{o.get('own_port', 11435)}" if o.get("start_own") else o.get("url", "http://127.0.0.1:11434")
    try:
        return sorted(m["name"] for m in httpx.get(f"{url.rstrip('/')}/api/tags", timeout=10).json().get("models", []))
    except Exception:
        return []


def make_backend(cfg: dict, log_dir: Path, prefix: str = "worker"):
    kind = cfg.get("backend", "ollama")
    if kind == "llama-server":
        return LlamaServerBackend(cfg, log_dir, prefix)
    if kind != "ollama":
        raise RuntimeError(f"unknown backend '{kind}' in the settings (use ollama or llama-server)")
    return OllamaBackend(cfg, log_dir, prefix)
