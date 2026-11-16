"""Model downloads for the "Get models" window of Firestarter and the Smithy (and the command line).

Sources: Hugging Face (GGUF files, via its public API), the Ollama library, or a
direct https link to a .gguf file. Only .gguf files are accepted: they're model
data, not programs (.bin/.pt/.pkl can carry code and are refused).

Where models go depends on the worker's settings "backend" (worker.json for Firestarter;
--config smithy.json, given first, for the Smithy):
- ollama: Hugging Face models are pulled by Ollama itself (hf.co/REPO:QUANT);
  library models with /api/pull; a direct-link .gguf is downloaded, imported
  with "ollama create", then the downloaded copy is deleted (Ollama keeps its own).
- llama-server: .gguf files are saved into llama_server.models_dir.

Downloads resume from a .part file, and Hugging Face files are checked against
the SHA-256 that Hugging Face publishes. Progress is printed as JSON lines:
  {"event": "progress", "done": N, "total": N, "status": "..."}
  {"event": "done", "model": "..."}     {"event": "error", "message": "..."}

    model_fetch.py search QUERY        -> JSON list of GGUF repos
    model_fetch.py files REPO          -> JSON list of .gguf files (size, sha256)
    model_fetch.py target              -> JSON: backend, folder, free bytes
    model_fetch.py hf REPO FILE        -> download (progress lines)
    model_fetch.py ollama NAME         -> ollama pull (progress lines)
    model_fetch.py url HTTPS_URL       -> download a .gguf (progress lines)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

HERE = Path(__file__).parent
CONFIG = HERE / "worker.json"
HF = "https://huggingface.co"
UA = {"User-Agent": "Othos-workers/1.0 (local model downloader)"}
# well-known publishers of GGUF files (shown with a star)
TRUSTED = {"ggml-org", "bartowski", "unsloth", "lmstudio-community", "Qwen", "google", "meta-llama",
           "microsoft", "mistralai", "openai", "nvidia", "ibm-granite", "deepseek-ai", "allenai", "HuggingFaceTB"}
REPO_RX = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")
FILE_RX = re.compile(r"^[A-Za-z0-9_.\-/]+\.gguf$")
OLLAMA_NAME_RX = re.compile(r"^[a-z0-9][a-z0-9_.\-/]*(:[A-Za-z0-9_.\-]+)?$", re.I)
QUANT_RX = re.compile(r"(?i)(IQ\d_[A-Z0-9_]+|Q\d(?:_[A-Z0-9]+)*|BF16|F16|F32|MXFP4)")


def emit(**kw) -> None:
    print(json.dumps(kw), flush=True)


def cfg() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8-sig"))


def ollama_url(c: dict) -> str:
    o = c.get("ollama", {})
    return (f"http://127.0.0.1:{o.get('own_port', 11435)}" if o.get("start_own")
            else o.get("url", "http://127.0.0.1:11434")).rstrip("/")


def models_dir(c: dict) -> Path:
    d = c.get("llama_server", {}).get("models_dir") or str(HERE.parent / "models")
    p = Path(d)
    p.mkdir(parents=True, exist_ok=True)
    return p


def target() -> dict:
    c = cfg()
    backend = c.get("backend", "ollama")
    if backend == "ollama":
        store = Path(os.environ.get("OLLAMA_MODELS") or Path.home() / ".ollama" / "models")
        folder = store if store.exists() else Path.home()
    else:
        folder = models_dir(c)
    return {"backend": backend, "folder": str(folder), "free": shutil.disk_usage(folder).free,
            "ollama": ollama_url(c)}


# ---------------------------------------------------------------- Hugging Face
def search(query: str) -> list[dict]:
    r = httpx.get(f"{HF}/api/models", headers=UA, timeout=20, params={
        "search": query, "filter": "gguf", "sort": "downloads", "direction": "-1", "limit": 40})
    r.raise_for_status()
    out = []
    for m in r.json():
        rid = m.get("id") or m.get("modelId", "")
        if REPO_RX.match(rid):
            out.append({"id": rid, "downloads": m.get("downloads", 0), "likes": m.get("likes", 0),
                        "trusted": rid.split("/")[0] in TRUSTED,
                        "gated": bool(m.get("gated"))})
    out.sort(key=lambda m: (not m["trusted"], -m["downloads"]))
    return out


def _check_repo(repo: str) -> None:
    if not REPO_RX.match(repo) or any(p in (".", "..") or p.startswith(".") for p in repo.split("/")):
        raise ValueError("not a Hugging Face model name (like Qwen/Qwen2.5-0.5B-Instruct-GGUF)")


def files(repo: str) -> list[dict]:
    _check_repo(repo)
    r = httpx.get(f"{HF}/api/models/{repo}/tree/main", headers=UA, timeout=30, params={"recursive": "true"})
    r.raise_for_status()
    parts: dict[str, dict] = {}
    for f in r.json():
        path = f.get("path", "")
        if (f.get("type") != "file" or not path.lower().endswith(".gguf") or not FILE_RX.match(path)
                or ".." in path.split("/")):
            continue
        # a model split into parts (-00001-of-00003.gguf) is shown and downloaded as one
        key = re.sub(r"-\d{5}-of-\d{5}(?=\.gguf$)", "", path)
        e = parts.setdefault(key, {"name": key, "files": [], "size": 0, "sizes": [], "sha256": []})
        e["files"].append(path)
        size = int((f.get("lfs") or {}).get("size") or f.get("size") or 0)
        e["size"] += size
        e["sizes"].append(size)
        e["sha256"].append((f.get("lfs") or {}).get("oid", ""))
    out = []
    for e in parts.values():
        order = sorted(range(len(e["files"])), key=lambda i: e["files"][i])
        for k in ("files", "sizes", "sha256"):
            e[k] = [e[k][i] for i in order]
        q = QUANT_RX.search(Path(e["name"]).name)
        e["quant"] = q.group(1).upper() if q else ""
        e["mmproj"] = "mmproj" in e["name"].lower()   # vision add-on, not a model by itself
        out.append(e)
    out.sort(key=lambda e: (e["mmproj"], e["size"]))
    return out


def _download(url: str, dest: Path, size: int = 0, sha256: str = "", label: str = "") -> None:
    """Resumable download to dest (via dest.part), with SHA-256 check if given."""
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    if size and have > size:
        have = 0
        part.unlink()
    if size and shutil.disk_usage(dest.parent).free < size - have + 1_000_000_000:
        raise RuntimeError("not enough free disk space")
    done = total = have
    if not (size and have == size):   # a .part that's already complete only needs checking
        headers = {**UA, **({"Range": f"bytes={have}-"} if have else {})}
        with httpx.stream("GET", url, headers=headers, follow_redirects=True,
                          timeout=httpx.Timeout(30, read=120)) as r:
            if any(not str(h.url).startswith("https://") for h in [*r.history, r]):
                raise RuntimeError("refused a redirect to a non-https address")
            if r.status_code == 416 and have:
                pass                          # nothing left to send: the .part is complete
            elif r.status_code == 200 and have:
                have = 0                      # the server ignored the resume: start over
                part.unlink(missing_ok=True)
            elif r.status_code not in (200, 206):
                raise RuntimeError(f"download failed: HTTP {r.status_code}")
            if r.status_code != 416:
                total = size or (have + int(r.headers.get("content-length", 0)))
                done, last = have, 0.0
                with open(part, "ab" if have else "wb") as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        fh.write(chunk)
                        done += len(chunk)
                        if time.monotonic() - last > 0.5:
                            last = time.monotonic()
                            emit(event="progress", done=done, total=total, status=f"Downloading {label}")
    if sha256:
        emit(event="progress", done=done, total=total, status=f"Checking {label}")
        h = hashlib.sha256()
        with open(part, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 22), b""):
                h.update(block)
        if h.hexdigest() != sha256:
            part.unlink()
            raise RuntimeError(f"{label}: checksum doesn't match Hugging Face's, so the file was deleted. "
                               "Try again to download it fresh.")
    _vet(part, label)
    part.replace(dest)


def _vet(part: Path, label: str) -> None:
    """Virus scan and file-type check before a download is used (download_scan.py). Skipped quietly if
    that file isn't beside this one (a copy without it)."""
    try:
        import download_scan
    except ImportError:
        return
    emit(event="progress", done=0, total=0, status=f"Scanning {label}")
    r = download_scan.vet_download(part, "data")
    if r["status"] == "ask":
        # Defender is off and no product is chosen: the window shows a drop-down (r["products"]) and
        # saves the pick with `download_scan.py choose NAME`. This download carries on (a model file is
        # data and has passed the type check); the next one uses the pick.
        emit(event="av_needed", products=r["products"])
    elif not r["ok"]:
        raise RuntimeError(f"{label}: {r['detail']}")
    elif r["status"] == "unchecked":
        emit(event="progress", done=0, total=0, status=r["detail"])


def get_hf(repo: str, name: str) -> None:
    entry = next((e for e in files(repo) if e["name"] == name), None)
    if entry is None:
        raise ValueError(f"{name} isn't a .gguf file in {repo}")
    c = cfg()
    if c.get("backend", "ollama") == "ollama" and len(entry["files"]) == 1:
        tag = entry["quant"] or "latest"
        pull_ollama(f"hf.co/{repo}:{tag}", allow_hf=True)
        return
    folder = models_dir(c)
    for i, (path, size, sha) in enumerate(zip(entry["files"], entry["sizes"], entry["sha256"]), 1):
        dest = folder / Path(path).name
        if dest.exists():
            continue
        label = Path(path).name + (f" (part {i} of {len(entry['files'])})" if len(entry["files"]) > 1 else "")
        _download(f"{HF}/{repo}/resolve/main/{quote(path)}", dest, size, sha, label)
    first = folder / Path(entry["files"][0]).name
    if c.get("backend", "ollama") == "ollama":
        _ollama_create(first, c)
    else:
        emit(event="done", model=first.stem)


# ---------------------------------------------------------------- Ollama
def pull_ollama(name: str, allow_hf: bool = False) -> None:
    if not OLLAMA_NAME_RX.match(name) and not (allow_hf and name.startswith("hf.co/")):
        raise ValueError("that isn't an Ollama model name (like qwen3:14b)")
    with httpx.stream("POST", f"{ollama_url(cfg())}/api/pull", json={"model": name, "stream": True},
                      timeout=httpx.Timeout(30, read=600)) as r:
        if r.status_code != 200:
            raise RuntimeError(f"Ollama error {r.status_code}: {r.read().decode(errors='replace')[:300]}")
        last = 0.0
        for line in r.iter_lines():
            if not line.strip():
                continue
            p = json.loads(line)
            if p.get("error"):
                raise RuntimeError(p["error"])
            if time.monotonic() - last > 0.5 or p.get("status") == "success":
                last = time.monotonic()
                emit(event="progress", done=int(p.get("completed") or 0), total=int(p.get("total") or 0),
                     status=str(p.get("status", "")))
    emit(event="done", model=name)


def _ollama_create(gguf: Path, c: dict) -> None:
    """Import a downloaded .gguf into Ollama, then delete the download (Ollama keeps its own copy)."""
    name = re.sub(r"[^a-z0-9_.\-]+", "-", re.sub(r"-\d{5}-of-\d{5}$", "", gguf.stem).lower()).strip("-.")
    emit(event="progress", done=0, total=0, status=f"Adding {name} to Ollama")
    with tempfile.TemporaryDirectory() as d:
        mf = Path(d) / "Modelfile"
        mf.write_text(f"FROM {gguf}\n", encoding="utf-8")
        env = {**os.environ, "OLLAMA_HOST": ollama_url(c).replace("http://", "")}
        r = subprocess.run(["ollama", "create", name, "-f", str(mf)], capture_output=True, text=True, env=env,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"ollama create failed: {(r.stderr or r.stdout)[-300:]}")
    for p in gguf.parent.glob(re.sub(r"-\d{5}-of-\d{5}\.gguf$", "", gguf.name).replace(".gguf", "") + "*.gguf"):
        p.unlink(missing_ok=True)
    emit(event="done", model=name)


# ---------------------------------------------------------------- direct link
def get_url(url: str) -> None:
    u = urlparse(url)
    name = Path(u.path).name
    if u.scheme != "https" or not name.lower().endswith(".gguf") or not FILE_RX.match(name):
        raise ValueError("only https links to a .gguf file are accepted")
    c = cfg()
    dest = models_dir(c) / name
    if not dest.exists():
        _download(url, dest, 0, "", name)
    if c.get("backend", "ollama") == "ollama":
        _ollama_create(dest, c)
    else:
        emit(event="done", model=dest.stem)


def main(argv: list[str]) -> int:
    global CONFIG
    if len(argv) > 2 and argv[1] == "--config":
        if argv[2] not in ("worker.json", "smithy.json"):      # only the two workers' settings
            emit(event="error", message="--config must be worker.json or smithy.json")
            return 1
        CONFIG = HERE / argv[2]
        argv = [argv[0], *argv[3:]]
    cmd, args = (argv[1] if len(argv) > 1 else ""), argv[2:]
    try:
        if cmd == "search":
            print(json.dumps(search(" ".join(args))))
        elif cmd == "files":
            print(json.dumps(files(args[0])))
        elif cmd == "target":
            print(json.dumps(target()))
        elif cmd == "hf":
            get_hf(args[0], args[1])
        elif cmd == "ollama":
            pull_ollama(args[0])
        elif cmd == "url":
            get_url(args[0])
        else:
            print(__doc__)
            return 1
        return 0
    except Exception as e:
        emit(event="error", message=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
