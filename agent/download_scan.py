"""Virus scan for files Othos or The Smithy download (models, programs, archives).

An antivirus can only be asked to scan on demand if it has a command-line scanner. Two do here:
Microsoft Defender (built into Windows) and ClamAV (free). For the other products in the list
the answer is "unchecked", said plainly: Othos never claims a file is safe when nothing scanned it.

    download_scan.py status              -> JSON: what's installed, what's chosen, whether a scan can run
    download_scan.py products            -> JSON: the drop-down list (name, price, note, installed)
    download_scan.py choose "NAME"       -> remember the choice (personal.json "antivirus")
    download_scan.py scan PATH [--kind data|program]

A scan returns {"status": "clean" | "found" | "unchecked" | "ask", "engine", "detail", "products"}.
 - found:     the file is moved to a "quarantine" folder next to it and the caller must stop.
 - unchecked: nothing could scan it. Data files (.gguf) still get a header check; programs and
              archives should be left for the person to decide.
 - ask:       Defender is off and no product has been chosen: show the drop-down (the "products" list).

Only the file's path goes to the scanner on this PC. Nothing is sent anywhere.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PERSONAL = HERE / "personal.json"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SCAN_TIMEOUT = 30 * 60   # seconds: a multi-GB model takes a while

# (name, price, note, can_scan_on_demand). Eight well-known products, then the two catch-alls.
PRODUCTS = [
    ("Microsoft Defender", "free, built into Windows", "The default. Othos can ask it to scan each download.", True),
    ("ClamAV", "free, open source", "Install ClamAV for Windows; Othos can ask it to scan each download.", True),
    ("Avast / AVG Free", "free", "Othos can't ask it to scan on demand; it relies on its live protection.", False),
    ("Malwarebytes", "paid (free trial)", "Othos can't ask it to scan on demand; it relies on its live protection.", False),
    ("Bitdefender", "paid (free edition exists)", "Othos can't ask it to scan on demand; it relies on its live protection.", False),
    ("ESET", "paid", "Othos can't ask it to scan on demand; it relies on its live protection.", False),
    ("Norton", "paid", "Othos can't ask it to scan on demand; it relies on its live protection.", False),
    ("Spybot Search & Destroy", "free edition (paid tiers)", "Othos can't ask it to scan on demand; it relies on its live protection.", False),
    ("Something else", "", "Othos can't ask it to scan on demand; it relies on its live protection.", False),
    ("None", "", "No scanning. Downloads are only checked for the right file type and their published checksum.", False),
]
_ALIASES = {"Microsoft Defender": ("defender",), "ClamAV": ("clam",), "Avast / AVG Free": ("avast", "avg"),
            "Malwarebytes": ("malwarebytes",), "Bitdefender": ("bitdefender",), "ESET": ("eset",), "Norton": ("norton",), "Spybot Search & Destroy": ("spybot",)}


def _ps(command: str, timeout: int = 20) -> str:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                           capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
        return r.stdout.strip()
    except Exception:
        return ""


def defender_exe() -> Path | None:
    p = Path(r"C:\ProgramData\Microsoft\Windows Defender\Platform\CurrentVersion\MpCmdRun.exe")
    if p.exists():
        return p
    base = Path(r"C:\ProgramData\Microsoft\Windows Defender\Platform")
    found = sorted(base.glob("*/MpCmdRun.exe")) if base.exists() else []
    return found[-1] if found else None


def defender_ready() -> bool:
    """Defender's scanner exists and its service is running (it can be passive next to another product)."""
    if not defender_exe():
        return False
    out = _ps("(Get-MpComputerStatus).AMServiceEnabled")
    return out.strip().lower() == "true"


def clam_exe() -> Path | None:
    on_path = shutil.which("clamscan")
    if on_path:
        return Path(on_path)
    for base in (r"C:\Program Files\ClamAV", r"C:\Program Files (x86)\ClamAV", r"C:\ClamAV"):
        p = Path(base) / "clamscan.exe"
        if p.exists():
            return p
    return None


def installed_products() -> list[str]:
    """Names Windows Security Center lists as running antivirus products (empty if it can't be read)."""
    out = _ps("Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct | "
              "ForEach-Object { '{0}|{1}' -f $_.displayName, $_.productState }")
    names = []
    for line in out.splitlines():
        name, _, state = line.partition("|")
        try:
            on = bool(int(state) & 0x1000)       # bit 12: the product's protection is on
        except ValueError:
            on = True
        if name.strip() and on:
            names.append(name.strip())
    return names


def match_product(display_name: str) -> str:
    low = display_name.lower()
    for product, words in _ALIASES.items():
        if any(w in low for w in words):
            return product
    return ""


def products() -> list[dict]:
    have = {match_product(n) for n in installed_products()}
    return [{"name": n, "price": price, "note": note, "installed": n in have, "scans_on_demand": scans}
            for n, price, note, scans in PRODUCTS]


def choice() -> str:
    try:
        v = json.loads(PERSONAL.read_text(encoding="utf-8")).get("antivirus", "")
        return v.strip() if isinstance(v, str) else ""
    except Exception:
        return ""


def choose(name: str) -> str:
    """Remember the person's pick. Only a name from the list is stored."""
    names = [p[0] for p in PRODUCTS]
    if name not in names:
        raise ValueError(f"pick one of: {', '.join(names)}")
    try:
        data = json.loads(PERSONAL.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except FileNotFoundError:
        data = {}
    data["antivirus"] = name
    PERSONAL.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return name


def check_gguf(path: Path) -> bool:
    """A model file must really be a GGUF. (Other model formats can carry code that runs when loaded.)"""
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"GGUF"
    except OSError:
        return False


def check_pdf(path: Path) -> bool:
    """A PDF starts with %PDF (within its first kilobyte)."""
    try:
        with open(path, "rb") as fh:
            return b"%PDF-" in fh.read(1024)
    except OSError:
        return False


def quarantine(path: Path) -> Path:
    q = path.parent / "quarantine"
    q.mkdir(exist_ok=True)
    dest = q / (path.name + ".blocked")
    shutil.move(str(path), str(dest))
    return dest


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=SCAN_TIMEOUT, creationflags=NO_WINDOW)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "the scan took too long"
    except OSError as e:
        return -1, str(e)


_DEFENDER_THREAT = re.compile(r"found\s+[1-9]\d*\s+threats?\b", re.I)
_DEFENDER_FAILED = re.compile(r"failed with hr\s*=", re.I)


def scan(path, kind: str = "data") -> dict:
    """Scan one file with the chosen product. kind: "data" (a model) or "program" (exe, zip, installer)."""
    path = Path(path)
    if not path.exists():
        # real-time protection may have removed it the moment it was written
        return {"status": "found", "engine": "", "detail": f"{path.name} disappeared: antivirus real-time protection likely removed it"}
    pick = choice()
    if not pick:
        if defender_ready():
            pick = "Microsoft Defender"
        else:
            return {"status": "ask", "engine": "", "products": products(),
                    "detail": "Microsoft Defender isn't running. Choose the antivirus you use."}
    if pick == "Microsoft Defender":
        exe = defender_exe()
        if not exe or not defender_ready():
            return {"status": "unchecked", "engine": pick, "detail": "Microsoft Defender isn't running, so nothing scanned this file."}
        code, text = _run([str(exe), "-Scan", "-ScanType", "3", "-File", str(path), "-DisableRemediation"])
        # MpCmdRun exits 2 both for "threat found" and for scan errors (e.g. "Failed with hr = 0x80508023" for a file
        # Defender can't see), so a threat is only reported when its output says so
        if _DEFENDER_THREAT.search(text):
            return {"status": "found", "engine": pick, "detail": "Microsoft Defender reported a threat."}
        if code == 0 and not _DEFENDER_FAILED.search(text):
            return {"status": "clean", "engine": pick, "detail": "Microsoft Defender found no threats."}
        why = (_DEFENDER_FAILED.search(text) and text[text.lower().find("failed with"):][:60].strip()) or text.strip()[-120:] or f"exit code {code}"
        return {"status": "unchecked", "engine": pick,
                "detail": f"Microsoft Defender couldn't finish the scan ({why}). The file was not treated as a threat."}
    if pick == "ClamAV":
        exe = clam_exe()
        if not exe:
            return {"status": "unchecked", "engine": pick, "detail": "ClamAV's scanner (clamscan.exe) wasn't found."}
        code, text = _run([str(exe), "--no-summary", str(path)])
        if code == 0:
            return {"status": "clean", "engine": pick, "detail": "ClamAV found no threats."}
        if code == 1:
            return {"status": "found", "engine": pick, "detail": "ClamAV reported a threat."}
        return {"status": "unchecked", "engine": pick, "detail": f"ClamAV couldn't finish the scan ({text.strip()[-120:] or code})."}
    why = ("you chose no antivirus" if pick == "None"
           else f"Othos can't ask {pick} to scan on demand, so it is relying on {pick}'s live protection")
    return {"status": "unchecked", "engine": pick, "detail": f"Not scanned: {why}."}


def vet_download(path, kind: str = "data") -> dict:
    """What a downloader calls on a finished file. Adds the type check and the quarantine to scan().

    Returns the scan result plus "ok": True when the file may be used. Programs and archives that were
    not actually scanned are not ok: the person decides ("ask_person": True)."""
    path = Path(path)
    if kind == "data" and re.search(r"\.gguf(\.part)?$", path.name, re.I) and not check_gguf(path):
        q = quarantine(path)
        return {"status": "found", "ok": False, "engine": "", "detail": f"{path.name} isn't a real GGUF model file, so it was set aside in {q.parent}."}
    if path.suffix.lower() == ".pdf" and not check_pdf(path):
        q = quarantine(path)
        return {"status": "found", "ok": False, "engine": "", "detail": f"{path.name} isn't a real PDF, so it was set aside in {q.parent}."}
    r = scan(path, kind)
    if r["status"] == "found":
        if path.exists():
            quarantine(path)
        r["detail"] += " The file was set aside in a quarantine folder and not used."
        r["ok"] = False
    elif r["status"] == "clean":
        r["ok"] = True
    else:   # unchecked or ask
        r["ok"] = kind == "data"
        r["ask_person"] = kind != "data"
    return r


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "status":
        print(json.dumps({"defender_ready": defender_ready(), "chosen": choice(), "installed": installed_products(),
                          "clam": bool(clam_exe())}))
    elif cmd == "products":
        print(json.dumps(products()))
    elif cmd == "choose" and len(argv) > 2:
        print(json.dumps({"chosen": choose(argv[2])}))
    elif cmd == "scan" and len(argv) > 2:
        kind = argv[argv.index("--kind") + 1] if "--kind" in argv else "data"
        r = vet_download(argv[2], kind)
        print(json.dumps(r))
        return 0 if r.get("ok") else 1
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
