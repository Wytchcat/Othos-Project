"""This PC's specs, read locally, and setup advice from them (local options first).

Asked once on first start (setup.ps1 or the Othos window): "May I check your current
system specs...?". Yes saves them to state\\system-specs.json; No saves
{"declined": true} there, so nothing asks again (Firestarter on its own checks only
whether that file exists). "Check my PC..." in the Othos window runs it any time.

Nothing leaves this computer: Windows' own hardware information (CIM, the registry),
nvidia-smi when present, and a look for installed tools. The machine id is a hash of
Windows' MachineGuid, only used to notice the specs came from another PC.

Shared: Firestarter's worker_specs.py (worker.json advice and apply) and the Smithy read
the same file. Othos's own advice covers the voice model and speech; apply() changes
only OLLAMA_MODEL in .env, after a backup in state\\backups\\.

    python system_specs.py --read [--advice] [--json]   read now and save
    python system_specs.py --rows                       advice as fit<TAB>area<TAB>text (the window)
    python system_specs.py --advice [--from file.json]  advice (saved specs, or a file)
    python system_specs.py --apply                      apply the advice (.env + workers)
    python system_specs.py --decline                    save the No
    python system_specs.py --stale                      exit 1 + reasons if out of date
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state"
PATH = STATE / "system-specs.json"
BACKUPS = STATE / "backups"
ENV = HERE / ".env"
MAX_AGE_DAYS = 90
_NOWIN = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

# Voice Othos's conversation models (Ollama tags), smallest first: (tag, download GB,
# memory GB it needs to run with a short context). The voice reply has to start fast,
# so on a CPU only the 3B model is quick enough.
VOICE_MODELS = [
    ("llama3.2:3b", 2.0, 3.5),
    ("qwen2.5:7b", 4.7, 6.5),
    ("qwen2.5:14b", 9.0, 11.5),
    ("qwen2.5:32b", 20.0, 23.0),
]
DEFAULT_MODEL = "llama3.2:3b"
GPU_SPARE_GB = 2.0  # left for the camera model (moondream) and the notes search


# ---------------------------------------------------------------- reading
def _run(cmd: list[str], timeout: int = 30) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace", creationflags=_NOWIN)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


_CIM = r"""
$ErrorActionPreference = 'SilentlyContinue'
$c = Get-CimInstance Win32_Processor | Select-Object -First 1
$cs = Get-CimInstance Win32_ComputerSystem
$os = Get-CimInstance Win32_OperatingSystem
[pscustomobject]@{
  cpu = @{ name = "$($c.Name)".Trim(); cores = [int]$c.NumberOfCores; threads = [int]$c.NumberOfLogicalProcessors }
  ram = [double]$cs.TotalPhysicalMemory
  video = @(Get-CimInstance Win32_VideoController | ForEach-Object { @{ name = "$($_.Name)"; ram = [double]$_.AdapterRAM } })
  disks = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object { @{ drive = $_.DeviceID; free = [double]$_.FreeSpace; total = [double]$_.Size } })
  windows = "$($os.Caption) $($os.Version)".Trim()
} | ConvertTo-Json -Depth 4 -Compress
"""


def _gb(b) -> float:
    try:
        return round(float(b) / 1024 ** 3, 1)
    except Exception:
        return 0.0


def _registry_vram() -> dict:
    """{card name: GB} from the display drivers' qwMemorySize (Win32 caps AdapterRAM at 4 GB)."""
    out = {}
    try:
        import winreg
        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                sub = winreg.EnumKey(k, i)
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(k, sub) as s:
                        name = winreg.QueryValueEx(s, "DriverDesc")[0]
                        size = winreg.QueryValueEx(s, "HardwareInformation.qwMemorySize")[0]
                        if isinstance(size, bytes):
                            size = int.from_bytes(size[:8], "little")
                        out[str(name)] = _gb(size)
                except OSError:
                    pass
    except Exception:
        pass
    return out


def _nvidia() -> dict:
    nvsmi = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
    if not Path(nvsmi).exists():
        return {}
    txt = _run([nvsmi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], 15)
    out = {}
    for line in txt.splitlines():
        if "," in line:
            name, mib = line.rsplit(",", 1)
            try:
                out[name.strip()] = round(int(mib.strip()) / 1024, 1)
            except ValueError:
                pass
    return out


def _integrated(name: str) -> bool:
    n = name.lower()
    return (("intel" in n and any(w in n for w in ("uhd", "iris", "hd graphics", "arc graphics")))
            or bool(re.search(r"amd radeon\(tm\) graphics|radeon graphics$|vega \d+ graphics", n))
            or "microsoft basic" in n or "remote display" in n)


def _machine_id() -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            guid = winreg.QueryValueEx(k, "MachineGuid")[0]
        return hashlib.sha256(("othos:" + guid).encode()).hexdigest()[:16]
    except Exception:
        return ""


def _tools() -> dict:
    la, ad = os.getenv("LOCALAPPDATA", ""), os.getenv("APPDATA", "")

    def found(*paths) -> bool:
        return any(p and Path(p).exists() for p in paths)

    llama_exe = ""
    try:
        llama_exe = (json.loads((HERE / "worker.json").read_text(encoding="utf-8"))
                     .get("llama_server") or {}).get("exe") or ""
    except Exception:
        pass
    ghidra = os.getenv("GHIDRA_INSTALL_DIR", "")
    return {
        "ollama": bool(shutil.which("ollama")) or found(Path(la) / "Programs/Ollama/ollama.exe"),
        "llama_server": bool(shutil.which("llama-server")) or found(llama_exe),
        "lm_studio": bool(shutil.which("lms")) or found(Path(la) / "Programs/LM Studio",
                                                         Path(la) / "Programs/lm-studio"),
        "node": bool(shutil.which("node")),
        "rea": found(Path(ad) / "npm/node_modules/rea-agents"),
        "ghidra": bool(ghidra) and found(ghidra),
        "java": bool(shutil.which("java")) or found(os.getenv("JAVA_HOME", "")),
        "nvidia_smi": bool(shutil.which("nvidia-smi")) or found(r"C:\Windows\System32\nvidia-smi.exe"),
    }


def read() -> dict:
    """A fresh local read (saves nothing)."""
    raw = {}
    txt = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _CIM], 60)
    try:
        raw = json.loads(txt) if txt.strip() else {}
    except json.JSONDecodeError:
        raw = {}
    nv, reg = _nvidia(), _registry_vram()
    gpus = []
    for v in raw.get("video") or []:
        name = (v.get("name") or "").strip()
        if not name:
            continue
        vram = nv.get(name) or reg.get(name) or _gb(v.get("ram"))
        gpus.append({"name": name, "vram_gb": vram, "integrated": _integrated(name),
                     "nvidia": "nvidia" in name.lower()})
    cpu = raw.get("cpu") or {}
    return {
        "read_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "machine_id": _machine_id(),
        "cpu": {"name": cpu.get("name", ""), "cores": int(cpu.get("cores") or os.cpu_count() or 0),
                "threads": int(cpu.get("threads") or os.cpu_count() or 0)},
        "ram_gb": _gb(raw.get("ram")),
        "gpus": gpus,
        "disks": [{"drive": d.get("drive", ""), "free_gb": _gb(d.get("free")),
                   "total_gb": _gb(d.get("total"))} for d in raw.get("disks") or []],
        "windows": raw.get("windows", ""),
        "tools": _tools(),
    }


# ---------------------------------------------------------------- saving
def save(specs: dict) -> None:
    STATE.mkdir(exist_ok=True)
    PATH.write_text(json.dumps(specs, indent=2), encoding="utf-8")


def load() -> dict | None:
    try:
        d = json.loads(PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def asked() -> bool:
    """True once the question has been answered (Yes or No)."""
    return PATH.exists()


def declined() -> bool:
    d = load()
    return bool(d and d.get("declined"))


def decline() -> None:
    save({"declined": True, "asked_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")})


def stale(saved: dict | None = None) -> list[str]:
    """Why the saved specs are out of date ([] = current, or nothing saved / declined)."""
    saved = saved if saved is not None else load()
    if not saved or saved.get("declined"):
        return []
    why = []
    now = read()
    if saved.get("machine_id") and now["machine_id"] and saved["machine_id"] != now["machine_id"]:
        why.append("they were read on another PC")
    try:
        age = dt.datetime.now().astimezone() - dt.datetime.fromisoformat(saved["read_at"])
        if age.days > MAX_AGE_DAYS:
            why.append(f"they're {age.days} days old")
    except Exception:
        why.append("they have no date")
    if abs((saved.get("ram_gb") or 0) - now["ram_gb"]) > 1:
        why.append(f"the RAM changed ({saved.get('ram_gb')} GB then, {now['ram_gb']} GB now)")
    if sorted(g["name"] for g in saved.get("gpus") or []) != sorted(g["name"] for g in now["gpus"]):
        why.append("the graphics card changed")
    return why


# ---------------------------------------------------------------- advice
def _fit(need: float, have: float) -> str:
    return "fits" if have >= need else "tight" if have >= need * 0.85 else "won't fit"


def best_gpu(specs: dict) -> dict | None:
    cards = [g for g in specs.get("gpus") or [] if not g.get("integrated") and g.get("vram_gb", 0) >= 4]
    return max(cards, key=lambda g: (g.get("nvidia", False), g["vram_gb"]), default=None)


def voice_model(specs: dict) -> tuple[str, str]:
    """(model tag, why) for voice Othos: the biggest that fits the graphics card, else 3B."""
    gpu = best_gpu(specs)
    if not gpu:
        return DEFAULT_MODEL, "no graphics card Ollama can use, so the fast 3B model on the CPU"
    room = gpu["vram_gb"] - GPU_SPARE_GB
    tag = DEFAULT_MODEL
    for t, _dl, need in VOICE_MODELS:
        if need <= room:
            tag = t
    return tag, f"the biggest that fits {gpu['name']} ({gpu['vram_gb']:g} GB), with {GPU_SPARE_GB:g} GB left for the camera and notes"


def advise(specs: dict) -> list[dict]:
    """[{"area", "advice", "fit"}], local first. Othos's part: voice model, speech, disk, tools."""
    out = []

    def add(area, advice, fit):
        out.append({"area": area, "advice": advice, "fit": fit})

    ram, threads = specs.get("ram_gb") or 0, (specs.get("cpu") or {}).get("threads") or 0
    gpu = best_gpu(specs)
    tools = specs.get("tools") or {}

    add("PC", f"{(specs.get('cpu') or {}).get('name') or 'CPU'}, {threads} threads, {ram:g} GB RAM"
        + (f", {gpu['name']} ({gpu['vram_gb']:g} GB)" if gpu else ", no usable graphics card"),
        _fit(14, ram))  # "16 GB" PCs report about 15.7
    if not tools.get("ollama"):
        add("Ollama", "Ollama isn't installed: it runs all of Othos's models. Get it from ollama.com.", "won't fit")

    tag, why = voice_model(specs)
    add("Voice model", f"OLLAMA_MODEL={tag}: {why}.", "fits" if gpu or ram >= 8 else _fit(8, ram))
    for t, dl, need in VOICE_MODELS:
        if t == tag:
            continue
        have = (gpu["vram_gb"] - GPU_SPARE_GB) if gpu else 0
        if gpu:
            fit = _fit(need, have)
            add("Voice model", f"{t} (needs about {need:g} GB on the graphics card, a {dl:g} GB download)", fit)
        elif t == "qwen2.5:7b":
            add("Voice model", f"{t} would run on the CPU but too slowly for voice (several seconds per reply)", "tight")

    # speech: local first (the user's rule), each with a can-this-PC-run-it check
    if gpu and gpu.get("nvidia") and gpu["vram_gb"] - GPU_SPARE_GB - dict((t, n) for t, _d, n in VOICE_MODELS)[tag] >= 1.5:
        add("Speech", "Local speech (OTHOS_SPEECH=local: Speaches + Kokoro on this PC) fits next to the voice model. "
            "Nothing you say leaves the PC. README 'Fully offline' has the steps.", "fits")
    elif gpu:
        add("Speech", "Local speech would share the graphics card with the voice model: possible with a smaller model. "
            "Cloud speech (Deepgram + Rime) is the safe choice.", "tight")
    else:
        add("Speech", "Local speech on the CPU is too slow for a conversation here. Keep cloud speech "
            "(OTHOS_SPEECH=cloud: Deepgram hears, Rime speaks; your audio goes to them, Deepgram's training opt-out is on).",
            "won't fit")

    models_drive = (os.getenv("OLLAMA_MODELS") or str(Path.home()))[:2].upper()
    disk = next((d for d in specs.get("disks") or [] if d.get("drive", "").upper() == models_drive), None)
    if disk:
        add("Disk", f"{disk['free_gb']:g} GB free on {disk['drive']} for models (Othos needs about 10 GB; bigger voice models more).",
            _fit(10, disk["free_gb"]))
    return out


# ---------------------------------------------------------------- applying
def _installed_models() -> set[str]:
    base = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/").removesuffix("/v1")
    try:
        import urllib.request
        with urllib.request.urlopen(base + "/api/tags", timeout=5) as r:
            return {m.get("name", "") for m in json.load(r).get("models", [])}
    except Exception:
        return set()


def _env_model() -> str:
    try:
        m = re.search(r"(?m)^OLLAMA_MODEL=(.*)$", ENV.read_text(encoding="utf-8"))
        return m.group(1).strip() if m else ""
    except Exception:
        return ""


def _backup(path: Path) -> Path:
    BACKUPS.mkdir(parents=True, exist_ok=True)
    dst = BACKUPS / f"{path.name}-{dt.datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(path, dst)
    return dst


def apply(specs: dict) -> list[str]:
    """Apply Othos's advice: OLLAMA_MODEL in .env (backed up first). Returns what changed."""
    tag, _ = voice_model(specs)
    now = _env_model() or DEFAULT_MODEL
    if tag == now:
        return [f"Voice model: already {tag}, nothing to change."]
    if not ENV.exists():
        return ["Voice model: no .env yet (run setup.ps1 first)."]
    have = _installed_models()
    if tag not in have and f"{tag}:latest" not in have:
        return [f"Voice model: not changed. Download it first with: ollama pull {tag}  (then apply again)."]
    b = _backup(ENV)
    txt = ENV.read_text(encoding="utf-8")
    if re.search(r"(?m)^OLLAMA_MODEL=", txt):
        txt = re.sub(r"(?m)^OLLAMA_MODEL=.*$", f"OLLAMA_MODEL={tag}", txt)
    else:
        txt = txt.rstrip("\n") + f"\nOLLAMA_MODEL={tag}\n"
    with open(ENV, "w", encoding="utf-8", newline="") as f:
        f.write(txt)
    return [f"Voice model: {now} -> {tag} in .env (backup: state\\backups\\{b.name}). Close and reopen Othos to use it."]


# ---------------------------------------------------------------- Firestarter's part (optional)
def _workers():
    try:
        import worker_specs  # Firestarter's worker.json advice; not there without Firestarter
        return worker_specs
    except ImportError:
        return None


def all_advice(specs: dict) -> list[dict]:
    out = advise(specs)
    w = _workers()
    if w:
        try:
            out += w.advise(specs)
        except Exception as e:
            out.append({"area": "Firestarter", "advice": f"its advice failed: {e}", "fit": "tight"})
    return out


def apply_all(specs: dict) -> list[str]:
    out = apply(specs)
    w = _workers()
    if w:
        try:
            changes = w.apply(specs)
            out += changes
            if changes:
                out.append("Close and reopen Firestarter to use its new settings.")
        except Exception as e:
            out.append(f"Firestarter: its settings weren't changed ({e}).")
    return out


# ---------------------------------------------------------------- command line
_MARK = {"fits": "[ok]  ", "tight": "[tight]", "won't fit": "[no]  "}


def advice_text(specs: dict) -> str:
    lines = []
    for a in all_advice(specs):
        lines.append(f"{_MARK.get(a['fit'], '      ')} {a['area']}: {a['advice']}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = set(argv)
    src = argv[argv.index("--from") + 1] if "--from" in argv and argv.index("--from") + 1 < len(argv) else ""
    if "--decline" in args:
        decline(); print("Saved: don't ask again. 'Check my PC...' in the Othos window checks any time.")
        return 0
    if "--asked" in args:
        return 0 if asked() else 1
    if "--stale" in args:
        why = stale()
        if why:
            print("The saved PC specs are out of date: " + "; ".join(why) + ".")
        return 1 if why else 0
    specs = None
    if src:
        specs = json.loads(Path(src).read_text(encoding="utf-8"))
    elif "--read" in args:
        specs = read(); save(specs)
    else:
        specs = load()
        if not specs or specs.get("declined"):
            specs = read(); save(specs)
    if "--json" in args:
        print(json.dumps(specs, indent=2))
    if "--rows" in args:  # for the Othos window: fit<TAB>area<TAB>advice, one per line
        for a in all_advice(specs):
            print(chr(9).join(re.sub(r"\s+", " ", str(a.get(k, ""))) for k in ("fit", "area", "advice")))
    if "--advice" in args:
        print(advice_text(specs))
    if "--apply" in args:
        print("\n".join(apply_all(specs)))
    if not args & {"--json", "--advice", "--apply", "--rows"}:
        print(f"Saved {PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
