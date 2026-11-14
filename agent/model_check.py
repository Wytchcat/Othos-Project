"""The weekly model check: new or updated models for voice Othos and The Smithy.

Once a week the Othos window asks "Check for updated models?" (Yes / Not now), in the
same look as the PC check. "Check models..." in the window runs it any time. A setting
in personal.json changes how it starts:
    "model_check": "ask"     ask once a week (the default)
                   "auto"    check once a week without asking; the window shows a line
                             when something is new
                   "off"     never on its own ("Check models..." still works)

The check itself downloads nothing. Each model this PC can run gets a Get (or Update)
button in the window; a download starts only after a Yes, which shows its size and the
free space. Rows say whether this PC can run the model, from the PC check's RAM and
graphics-card figures (state\\system-specs.json); a model that won't fit gets no button.

What it compares:
- models-tested.json: the models tested with this version of Othos, per job (voice
  conversation, camera, notes search, coding / The Smithy).
- what's installed: Ollama's list (127.0.0.1) and The Smithy's model in smithy.json.
- newer releases in the same model families: the Ollama library (ollama.com), GGUF
  files on Hugging Face (trusted publishers only), and new Ollama / llama.cpp
  versions (GitHub releases).
Local models come first, then the cloud providers turned on in The Smithy's Providers
window (listed from smithy.json). Each check also re-checks those providers' free
allowances in the background (`smithy.py free-limits`: their own free account pages, your
saved keys, no test requests), so the Providers window's order stays right. xAI / Grok is never listed.

Privacy: this is the only part of Othos that contacts ollama.com, huggingface.co and
api.github.com, so it runs only on a Yes, from "Check models...", or with "auto". The
requests carry model family names only (and, as with any web request, this computer's
internet address); no cookies, no account, a generic program name.

    python model_check.py --rows      check now; fit<TAB>area<TAB>text per line (the window)
    python model_check.py             check now; readable text
    python model_check.py --due       prints ask / auto / (nothing): is the weekly check due?
    python model_check.py --snooze    "Not now": ask again in a week
    python model_check.py --last      the last check's rows, without checking again
    python model_check.py --space ollama|hf   free GB<TAB>folder for a download
    python model_check.py --pull TAG  download one the last check offered (the Get button)
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state"
RESULT = STATE / "model-check.json"
LIST = HERE / "models-tested.json"
SMITHY = HERE / "smithy.json"
PERSONAL = HERE / "personal.json"
ENV = HERE / ".env"
WEEK = dt.timedelta(days=7)
UA = {"User-Agent": "Othos-model-check/1.0"}
OLLAMA_WEB = "https://ollama.com"
HF = "https://huggingface.co"
GITHUB = "https://api.github.com"
NEVER = re.compile(r"(?i)\bx\.?ai\b|grok")          # never listed, anywhere
CPU_RESERVE_GB = 8.0    # RAM kept for Windows and voice Othos when The Smithy runs a model
MAX_NEW_PER_JOB = 3
# Hugging Face publishers whose GGUF files are listed (as in model_fetch.py's Get models)
TRUSTED = {"ggml-org", "bartowski", "unsloth", "lmstudio-community", "Qwen", "google", "meta-llama",
           "mistralai", "microsoft", "nvidia"}
NAME_RX = re.compile(r"^[a-z0-9][a-z0-9_.\-]*$")
SIZE_TAG = re.compile(r"(?i)^\d+(\.\d+)?[bm](-a\d+(\.\d+)?b)?$")


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _iso(t: dt.datetime) -> str:
    return t.isoformat(timespec="seconds")


# ---------------------------------------------------------------- settings and state
def mode() -> str:
    try:
        v = str(json.loads(PERSONAL.read_text(encoding="utf-8")).get("model_check", "")).strip().lower()
    except Exception:
        v = ""
    return v if v in ("ask", "auto", "off") else "ask"


def load_state() -> dict:
    try:
        d = json.loads(RESULT.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_state(d: dict) -> None:
    STATE.mkdir(exist_ok=True)
    RESULT.write_text(json.dumps(d, indent=2), encoding="utf-8")


def due() -> str:
    """"ask" / "auto" when the weekly check is due, else "". The first time, the week starts
    now (no question on the first start, which already asks about the PC check)."""
    m = mode()
    if m == "off":
        return ""
    st = load_state()
    try:
        nxt = dt.datetime.fromisoformat(st["next_at"])
    except Exception:
        st["next_at"] = _iso(_now() + WEEK)
        save_state(st)
        return ""
    return m if _now() >= nxt else ""


def snooze() -> None:
    st = load_state()
    st["asked_at"] = _iso(_now())
    st["next_at"] = _iso(_now() + WEEK)
    save_state(st)


def _env(key: str, default: str = "") -> str:
    v = os.getenv(key, "")
    if v:
        return v.strip()
    try:
        m = re.search(rf"(?m)^{re.escape(key)}=(.*)$", ENV.read_text(encoding="utf-8"))
        return m.group(1).strip() if m and m.group(1).strip() else default
    except Exception:
        return default


def _smithy() -> dict:
    try:
        return json.loads(SMITHY.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


# ---------------------------------------------------------------- this PC (no network)
def _http():
    import httpx
    return httpx


def _local_get(url: str) -> dict:
    try:
        r = _http().get(url, timeout=5)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def ollama_urls() -> list[str]:
    urls = [_env("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/").removesuffix("/v1")]
    o = _smithy().get("ollama") or {}
    if o.get("start_own"):
        urls.append(f"http://127.0.0.1:{o.get('own_port', 11436)}")
    elif o.get("url"):
        urls.append(str(o["url"]).rstrip("/"))
    out = []
    for u in urls:
        u = u.replace("localhost", "127.0.0.1")
        if u not in out:
            out.append(u)
    return out


def installed() -> dict[str, str]:
    """{"llama3.2:3b": "a80c4f17acd5...", ...}: Ollama's models (name -> digest)."""
    out = {}
    for u in ollama_urls():
        for m in _local_get(u + "/api/tags").get("models", []):
            n = m.get("name", "")
            if n:
                out[n] = m.get("digest", "")
                if n.endswith(":latest"):
                    out.setdefault(n[:-7], m.get("digest", ""))
    return out


def ollama_version() -> str:
    for u in ollama_urls():
        v = _local_get(u + "/api/version").get("version", "")
        if v:
            return v
    return ""


def pc() -> tuple[dict | None, str]:
    """(specs, note): the PC check's figures, read now (not saved) if it hasn't run yet."""
    try:
        import system_specs
    except Exception:
        return None, "the PC check (system_specs.py) is missing"
    s = system_specs.load()
    if s and s.get("declined"):
        return None, "you said no to the PC check, so fit isn't checked ('Check my PC...' runs it)"
    if not s:
        try:
            s = system_specs.read()
        except Exception as e:
            return None, f"couldn't read this PC ({e})"
    return s, ""


def room(specs: dict | None, runs_on: str) -> tuple[float, str]:
    """(GB a model of this job can use, where)."""
    if not specs:
        return 0.0, ""
    import system_specs
    ram = float(specs.get("ram_gb") or 0)
    gpu = system_specs.best_gpu(specs) if runs_on == "gpu" else None
    if gpu:
        return gpu["vram_gb"] - system_specs.GPU_SPARE_GB, f"on {gpu['name']}"
    return max(ram - CPU_RESERVE_GB, 0.0), "on the CPU"


def fit(need: float, have: float, specs: dict | None) -> str:
    if not specs or not need:
        return "info"
    return "fits" if have >= need else "tight" if have >= need * 0.85 else "won't fit"


def cpu_fit(need: float, have: float, specs: dict | None, job: dict, where: str) -> str:
    """fit(), but a voice model too big to answer quickly on the CPU is at best "tight"."""
    f = fit(need, have, specs)
    return "tight" if f == "fits" and slow(need, job, where) else f


def slow(need: float, job: dict, where: str) -> str:
    lim = job.get("cpu_quick_gb")
    return (" Too slow on the CPU for a conversation (several seconds per reply)."
            if lim and where == "on the CPU" and need > float(lim) else "")


# ---------------------------------------------------------------- online (only on a Yes)
class Offline(Exception):
    pass


def _get(url: str, params: dict | None = None, as_json: bool = False):
    try:
        r = _http().get(url, params=params, headers=UA, timeout=20, follow_redirects=False)
    except Exception as e:
        raise Offline(f"couldn't reach {url.split('/')[2]} ({type(e).__name__})")
    if r.status_code != 200:
        raise Offline(f"{url.split('/')[2]} answered {r.status_code}")
    return r.json() if as_json else r.text


def _gb(num: str, unit: str) -> float:
    v = float(num)
    return round(v / 1024 if unit.upper() == "MB" else v * 1024 if unit.upper() == "TB" else v, 1)


def library_search(word: str) -> list[dict]:
    """Ollama library models found for a word: [{"name", "updated", "sizes"}]."""
    page = _get(f"{OLLAMA_WEB}/search", {"q": word})
    out = []
    for block in page.split('<a href="/library/')[1:]:
        name = block.split('"', 1)[0]
        if not NAME_RX.match(name) or any(o["name"] == name for o in out):
            continue
        upd = re.search(r'title="Updated ([A-Z][a-z]+ \d{1,2}, \d{4})"', block[:4000])
        out.append({"name": name, "updated": upd.group(1) if upd else "",
                    "sizes": re.findall(r'>(\d+(?:\.\d+)?[bm](?:-a\d+(?:\.\d+)?b)?)</span>', block[:6000], re.I)})
    return out


def library_tags(name: str) -> dict[str, dict]:
    """{"qwen3-coder:30b": {"digest": "06c1097efce0", "gb": 19.0}, ...} from the tags page."""
    page = _get(f"{OLLAMA_WEB}/library/{name}/tags")
    out = {}
    for m in re.finditer(r'<a href="/library/(' + re.escape(name) + r':[A-Za-z0-9_.\-]+)" class="md:hidden(.{0,1500}?)</a>',
                         page, re.S):
        tag, body = m.group(1), m.group(2)
        dg = re.search(r'font-mono[^>]*>\s*([0-9a-f]{12})', body)
        sz = re.search(r'(\d+(?:\.\d+)?)\s*(MB|GB|TB)', body)
        out[tag] = {"digest": dg.group(1) if dg else "", "gb": _gb(*sz.groups()) if sz else 0.0}
    return out


def _date(text: str) -> dt.date | None:
    try:
        return dt.datetime.strptime(text, "%B %d, %Y").date()
    except Exception:
        return None


def hf_new(author: str, search: str, since: dt.date) -> list[dict]:
    """A trusted publisher's GGUF repos made since the tested list's date, newest first."""
    if author not in TRUSTED:
        return []
    rows = _get(f"{HF}/api/models", {"author": author, "search": search, "filter": "gguf", "sort": "createdAt",
                                     "direction": "-1", "limit": 10}, as_json=True)
    out = []
    for m in rows:
        rid = m.get("id") or m.get("modelId", "")
        made = (m.get("createdAt") or "")[:10]
        if "/" not in rid or rid.split("/")[0] not in TRUSTED or NEVER.search(rid) or made < since.isoformat():
            continue
        out.append({"id": rid, "made": made})
    return out[:2]


def hf_q4(rid: str) -> tuple[float, str]:
    """(size in GB, file name as Get models names it) of the repo's Q4_K_M file (the usual choice)."""
    try:
        files = _get(f"{HF}/api/models/{rid}/tree/main", {"recursive": "true"}, as_json=True)
    except Offline:
        return 0.0, ""
    groups: dict[str, int] = {}     # one model may be split into parts (-00001-of-00003.gguf)
    for f in files:
        p = f.get("path", "")
        if p.lower().endswith(".gguf") and "q4_k_m" in p.lower() and "mmproj" not in p.lower():
            key = re.sub(r"-\d{5}-of-\d{5}(?=\.gguf$)", "", p)
            groups[key] = groups.get(key, 0) + int((f.get("lfs") or {}).get("size") or f.get("size") or 0)
    if not groups:
        return 0.0, ""
    best = min(groups, key=lambda k: ("ud-" in k.lower(), len(k)))   # the plain Q4_K_M first
    return round(groups[best] / 1e9, 1), best


def github_latest(repo: str) -> str:
    return str(_get(f"{GITHUB}/repos/{repo}/releases/latest", as_json=True).get("tag_name", ""))


# ---------------------------------------------------------------- the check
def _ver(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def _need(gb: float) -> float:
    return round(gb * 1.2 + 0.5, 1)   # the download, plus room for a short context


def check() -> list[dict]:
    """[{"fit", "area", "text", "kind", "key"}]: kind = tested / update / new (since the tested
    list's date) / family (older, not tested) / program / cloud / note; key = the model (for "new since
    the last check")."""
    tested = json.loads(LIST.read_text(encoding="utf-8"))
    since = dt.date.fromisoformat(tested.get("tested_on", "2026-01-01"))
    have = installed()
    smithy = _smithy()
    code_model = (smithy.get("models") or {}).get("code", "")
    specs, pc_note = pc()
    rows: list[dict] = []
    errors: list[str] = []
    tags_cache: dict[str, dict] = {}
    search_cache: dict[str, list] = {}

    def add(fit_, area, text, kind, key="", action=""):
        """action: what the window's button does: pull|TAG|GB, hf|REPO|FILE|GB or open|URL.
        No button for a model this PC can't run (the user's choice)."""
        if not NEVER.search(text):
            rows.append({"fit": fit_, "area": area, "text": text, "kind": kind, "key": key,
                         "action": "" if fit_ == "won't fit" else action})

    def tags(name):
        if name not in tags_cache:
            try:
                tags_cache[name] = library_tags(name)
            except Offline as e:
                errors.append(str(e)); tags_cache[name] = {}
        return tags_cache[name]

    if not have:
        add("info", "Ollama", "Ollama isn't answering on this PC, so installed models can't be compared. "
            "Open Othos (it starts Ollama) and check again.", "note")
    if pc_note:
        add("info", "This PC", pc_note[0].upper() + pc_note[1:] + ".", "note")

    for key, job in (tested.get("jobs") or {}).items():
        area = job.get("title", key)
        space, where = room(specs, job.get("runs_on", "gpu"))
        known = {m["tag"].split(":")[0] for m in job.get("models", [])}
        in_use = code_model if key == "coding" else _env({"voice": "OLLAMA_MODEL", "camera": "VISION_MODEL",
                                                          "notes": "EMBED_MODEL"}.get(key, ""), "")
        # 1. the tested models: installed and current, an update out, or not installed
        for m in job.get("models", []):
            tag = m["tag"]
            base = tag.split(":")[0]
            need = float(m.get("memory_gb") or 0)
            f = cpu_fit(need, space, specs, job, where)
            mine = have.get(tag, "")
            lib = tags(base).get(tag if ":" in tag else tag + ":latest", {})
            using = " (in use)" if in_use and in_use.split(":")[0] == base and (in_use == tag or ":" not in in_use) else ""
            label = "Tested with your version" if m.get("tested", True) else "Planned (not tested here yet)"
            if mine and lib.get("digest") and not mine.startswith(lib["digest"]):
                gb = lib.get("gb") or m.get("download_gb")
                add(f, area, f"{tag}{using}: an update is out in the Ollama library (about {gb:g} GB).",
                    "update", tag + "@" + lib["digest"], f"pull|{tag}|{gb:g}")
            elif mine:
                add(f, area, f"{tag}{using}: installed, up to date. {label}.", "tested")
            else:
                add(f, area, f"{tag}: not installed. {label}; needs about {need:g} GB {where}, "
                    f"a {m.get('download_gb'):g} GB download." + slow(need, job, where)
                    + (f" {m['note'][0].upper() + m['note'][1:]}." if m.get("note") else ""), "tested", "",
                    f"pull|{tag}|{m.get('download_gb'):g}")
        # 2. newer releases in the same families (Ollama library), not tested yet
        fams = [re.compile(p) for p in job.get("families", [])]
        skip = [re.compile(p) for p in job.get("skip", [])]
        words = {re.match(r"\^?([a-z]+)", p).group(1) for p in job.get("families", []) if re.match(r"\^?([a-z]+)", p)}
        found = []
        for w in sorted(words):
            if w not in search_cache:
                try:
                    search_cache[w] = library_search(w)
                except Offline as e:
                    errors.append(str(e)); search_cache[w] = []
            for e in search_cache[w]:
                n = e["name"]
                if (n in known or any(x["name"] == n for x in found) or NEVER.search(n)
                        or not any(r.search(n) for r in fams) or any(s.search(n) for s in skip)):
                    continue
                upd = _date(e["updated"])
                if upd and upd < since - dt.timedelta(days=365):
                    continue     # an older family member, not a newer release
                found.append({**e, "date": upd or since})
        found.sort(key=lambda e: e["date"], reverse=True)
        for e in found[:MAX_NEW_PER_JOB]:
            all_tags = tags(e["name"])
            # plain sizes only (27b, 35b-a3b); not cloud, Mac-only (mlx) or other number formats
            sized = [(t, v) for t, v in all_tags.items() if v.get("gb") and SIZE_TAG.match(t.split(":", 1)[1])]
            if not sized and all_tags.get(e["name"] + ":latest", {}).get("gb"):
                sized = [(e["name"] + ":latest", all_tags[e["name"] + ":latest"])]
            sized.sort(key=lambda tv: tv[1]["gb"])
            if not sized:
                continue
            quick = float(job.get("cpu_quick_gb") or 0) if where == "on the CPU" else 0
            ok = [tv for tv in sized if _need(tv[1]["gb"]) <= (min(space, quick) if quick else space)]
            tag, v = (ok[-1] if ok else sized[0])
            need = _need(v["gb"])
            newer = e["date"] > since
            mine = " Installed." if tag in have else ""
            others = ", ".join(t.split(":")[1] for t, _ in sized if t != tag)
            add(cpu_fit(need, space, specs, job, where), area,
                ("New since your version's list" if newer else "Same family") + f", not tested yet: {tag}"
                + (f" (updated {e['updated']})" if e["updated"] else "")
                + f", a {v['gb']:g} GB download, needs about {need:g} GB {where}." + slow(need, job, where) + mine
                + (f" Other sizes: {others}." if others else ""), "new" if newer else "family", tag,
                "" if tag in have else f"pull|{tag}|{v['gb']:g}")
        # 3. GGUF files on Hugging Face (for llama-server), made since the tested list's date
        for h in job.get("hf", []):
            try:
                for r in hf_new(h.get("author", ""), h.get("search", ""), since):
                    gb, fname = hf_q4(r["id"])
                    need = _need(gb) if gb else 0
                    add(cpu_fit(need, space, specs, job, where), area,
                        f"New on Hugging Face ({r['made']}), for llama-server: {r['id']}"
                        + (f", Q4_K_M {gb:g} GB, needs about {need:g} GB {where}" if gb else "")
                        + ". Not tested with your version yet. Downloaded the way The Smithy's Get models does "
                        "(its settings decide where it goes).", "new", r["id"],
                        f"hf|{r['id']}|{fname}|{gb:g}" if fname else "")
            except Offline as e:
                errors.append(str(e))

    # 4. the programs that run the models (GitHub releases)
    try:
        latest, mine = github_latest("ollama/ollama"), ollama_version()
        if latest and mine and _ver(latest) > _ver(mine):
            add("info", "Ollama", f"Ollama {latest.lstrip('v')} is out (this PC has {mine}). "
                "Its download page opens in a private window; run the installer from there.",
                "update", "ollama@" + latest, "open|https://ollama.com/download/windows")
        elif latest and mine:
            add("info", "Ollama", f"Ollama {mine} is the newest.", "program")
        if smithy.get("backend") == "llama-server" or (smithy.get("llama_server") or {}).get("exe"):
            lc = github_latest("ggml-org/llama.cpp")
            if lc:
                add("info", "llama.cpp", f"The newest llama.cpp is {lc} (github.com/ggml-org/llama.cpp/releases).", "program")
    except Offline as e:
        errors.append(str(e))

    # 5. cloud providers turned on in The Smithy's Providers window (listed; none is contacted)
    cloud = smithy.get("cloud") or {}
    try:
        import worker_cloud
        provs = worker_cloud.PROVIDERS
    except Exception:
        provs = {}
    enabled = [n for n in cloud.get("enabled") or [] if not NEVER.search(str(n))]
    enabled.sort(key=lambda n: not getattr(provs.get(n), "local", False))     # local servers first
    for n in enabled:
        p = provs.get(n)
        label = getattr(p, "spoken", n)
        model = (cloud.get("models") or {}).get(n) or getattr(p, "model", "") or "(its default)"
        where_ = ("on this PC; nothing leaves it" if getattr(p, "local", False)
                  else "on this PC, but it sends to free cloud tiers" if getattr(p, "proxy", False) else "cloud")
        add("info", "Providers (The Smithy)", f"{label} is on ({where_}), model {model}. "
            "Newer models: the provider's own model list; change it in The Smithy's Providers window.", "cloud")

    for e in dict.fromkeys(errors):
        add("info", "Not checked", e[0].upper() + e[1:] + ". Try again later.", "note")
    return rows


def refresh_free_limits() -> None:
    """Re-check the providers' free allowances too (The Smithy's `smithy.py free-limits`): only the
    providers' own free account pages, with the keys saved in The Smithy's Providers window, no test
    requests. It runs in the background and only updates state\free-limits.json (the Providers
    window's order); nothing here waits for it. Skipped when no provider is turned on."""
    import subprocess
    script = HERE / "smithy.py"
    if not script.exists() or not (_smithy().get("cloud") or {}).get("enabled"):
        return
    try:
        subprocess.Popen([sys.executable, str(script), "free-limits"], cwd=str(HERE), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=0x08000000 if os.name == "nt" else 0)    # no console window
    except Exception:
        pass


def run() -> list[dict]:
    rows = check()
    refresh_free_limits()
    st = load_state()
    seen = set(st.get("seen") or [])
    fresh = [r["key"] for r in rows if r["kind"] in ("update", "new") and r["key"] and r["key"] not in seen]
    st.update(checked_at=_iso(_now()), next_at=_iso(_now() + WEEK), rows=rows, news=len(fresh),
              seen=sorted(seen | {r["key"] for r in rows if r["key"]}))
    save_state(st)
    return rows


# ---------------------------------------------------------------- command line
_MARK = {"fits": "[ok]   ", "tight": "[tight]", "won't fit": "[no]   ", "info": "       "}


def _print_rows(rows: list[dict], tabs: bool) -> None:
    for r in rows:
        act = r.get("action", "")
        if tabs:   # the Othos window: fit<TAB>area<TAB>text<TAB>action (fits / tight / won't fit / update / info)
            tag = "update" if r["kind"] == "update" and r["fit"] == "info" else r["fit"]
            print("\t".join(re.sub(r"\s+", " ", str(x)) for x in (tag, r["area"], r["text"], act)))
        else:
            a = act.split("|")
            how = (f"  -> ollama pull {a[1]}" if a[0] == "pull" else f"  -> model_fetch.py --config smithy.json hf {a[1]} {a[2]}"
                   if a[0] == "hf" else f"  -> {a[1]}" if a[0] == "open" else "")
            print(f"{_MARK.get(r['fit'], '       ')} {r['area']}: {r['text']}{how}")


# ---------------------------------------------------------------- downloads (the window's Get button)
# Only on her click and a Yes in the window. Progress as JSON lines, like model_fetch.py:
#   {"event": "progress", "done": N, "total": N, "status": "..."}  {"event": "done"}  {"event": "error"}
TAG_RX = re.compile(r"^[a-z0-9][a-z0-9_.\-/]*:[A-Za-z0-9_.\-]+$")


def _emit(**kw) -> None:
    print(json.dumps(kw), flush=True)


def _offered(action_start: str) -> bool:
    """Only what the last check offered can be downloaded from here."""
    return any(str(r.get("action", "")).startswith(action_start) for r in load_state().get("rows", []))


def ollama_store() -> Path:
    p = Path(_env("OLLAMA_MODELS") or Path.home() / ".ollama" / "models")
    return p if p.exists() else Path.home()


def space(kind: str) -> tuple[float, str]:
    """(free GB, folder) where a download of this kind goes."""
    import shutil
    if kind == "hf":
        import model_fetch
        model_fetch.CONFIG = SMITHY
        t = model_fetch.target()
        return round(t["free"] / 1e9, 1), t["folder"]
    folder = ollama_store()
    return round(shutil.disk_usage(folder).free / 1e9, 1), str(folder)


def pull(tag: str) -> int:
    """ollama pull TAG through voice Othos's Ollama (127.0.0.1), with progress lines."""
    if not TAG_RX.match(tag) or NEVER.search(tag) or "cloud" in tag or not _offered(f"pull|{tag}|"):
        _emit(event="error", message=f"{tag} isn't one the model check offered")
        return 1
    gb = next((float(r["action"].split("|")[2]) for r in load_state().get("rows", [])
               if str(r.get("action", "")).startswith(f"pull|{tag}|")), 0.0)
    free, folder = space("ollama")
    if gb and free < gb + 1:
        _emit(event="error", message=f"not enough space: {gb:g} GB needed, {free:g} GB free in {folder}")
        return 1
    import time
    try:
        with _http().stream("POST", ollama_urls()[0] + "/api/pull", json={"model": tag, "stream": True},
                            timeout=_http().Timeout(30, read=600)) as r:
            if r.status_code != 200:
                raise RuntimeError(f"Ollama answered {r.status_code}")
            last = 0.0
            for line in r.iter_lines():
                if not line.strip():
                    continue
                p = json.loads(line)
                if p.get("error"):
                    raise RuntimeError(p["error"])
                if time.monotonic() - last > 0.5 or p.get("status") == "success":
                    last = time.monotonic()
                    _emit(event="progress", done=int(p.get("completed") or 0), total=int(p.get("total") or 0),
                          status=str(p.get("status", "")))
    except Exception as e:
        _emit(event="error", message=str(e) or type(e).__name__)
        return 1
    _emit(event="done", model=tag)
    return 0


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = set(argv)
    if "--due" in args:
        print(due())
        return 0
    if "--space" in argv:     # --space ollama|hf: free GB<TAB>folder
        i = argv.index("--space")
        free, folder = space(argv[i + 1] if i + 1 < len(argv) else "ollama")
        print(f"{free:g}\t{folder}")
        return 0
    if "--pull" in argv:
        i = argv.index("--pull")
        return pull(argv[i + 1] if i + 1 < len(argv) else "")
    if "--snooze" in args:
        snooze()
        print("Not now: asking again in a week.")
        return 0
    rows = load_state().get("rows", []) if "--last" in args else run()
    _print_rows(rows, "--rows" in args)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
