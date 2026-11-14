"""Cloud (and local-server) model providers for the workers, by API key. OFF unless turned on.

Approved by the user on Nov 12 2026 as an exception to "only Deepgram, Rime and Tavily get
data", with these safeguards, all enforced here in code:

- Off unless the worker's settings have a "cloud" section listing the provider in "enabled",
  and its key is in .env (never in the settings JSON). Local servers (LM Studio) need no key.
- A task uses its worker's local model unless the task names a provider (by voice, "using
  DeepSeek"; on the command line, --provider NAME) or "cloud.default" names one.
- Only to the provider's own fixed address (PROVIDERS below; a local server must be on
  127.0.0.1 / localhost). The key is sent only in that request's header and never logged.
- Everything sent passes the shared redaction filter (again, on top of each task's own).
- The Smithy's "check" task (decompiled code from someone else's program) stays local unless
  "cloud.allow_check_task" is true.
- Providers whose free tiers may use what you send for training ("trains" below) are refused
  unless "cloud.accept_data_use" lists them.
- Token limits per task and per day ("max_tokens_per_task", "max_tokens_per_day"), and
  optional prices ("prices": {"deepseek": [input, output] dollars per million tokens}) with
  "max_usd_per_day". The running total is in state\\cloud-usage.json.

Switching (approved Nov 13 2026): "cloud.switch_order" lists providers to use one after another.
A task for provider "auto" (or for a provider on that list) starts with the first ready one and,
when a provider says it's out of free tokens or credit (or can't be reached), carries on with
the next. When every one is out, the task waits where it is and asks (the app's Yes/No, or
Othos by voice): finish on this PC's model now, or wait until free tokens are back (it carries
on 5 minutes after); "cloud.when_all_out": "local" skips the question. Each key lives in one place (the
Smithy's), so no two programs spend the same free allowance. Every safeguard above applies to
each provider in turn; the check task never switches to the cloud.

Local options come first (the user's rule): LM Studio here; Ollama and llama.cpp are the
workers' own back ends (worker_backend.py). xAI is deliberately not offered.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import dotenv_values

import worker_shared

log = logging.getLogger("worker")
HERE = Path(__file__).parent
USAGE_PATH = HERE / "state" / "cloud-usage.json"


@dataclass(frozen=True)
class Provider:
    name: str                 # how it's written in settings and on the command line
    spoken: str               # how voice Othos says it
    kind: str                 # "openai" (OpenAI-compatible chat completions) | "anthropic"
    base_url: str             # fixed here, so settings can't send keys somewhere else
    key_env: str              # the key's name in .env ("" = no key: a local server)
    model: str = ""           # a default model, where one is clear; else set cloud.models.<name>
    trains: bool = False      # a free tier may use what's sent for training
    local: bool = False       # runs on this PC and nothing leaves it
    proxy: bool = False       # runs on this PC but passes everything on to cloud providers:
                              # its address must be on this PC, every cloud safeguard still applies
    note: str = ""
    alt_env: tuple = ()       # other names people often give the same key in .env
    account_env: str = ""     # a provider whose address holds an account ID (Cloudflare), from .env


PROVIDERS: dict[str, Provider] = {p.name: p for p in [
    # local first
    Provider("lmstudio", "LM Studio", "openai", "http://127.0.0.1:1234/v1", "", local=True,
             note="LM Studio's local server (Developer tab, Start Server). Nothing leaves this PC."),
    # cloud
    Provider("anthropic", "Claude", "anthropic", "https://api.anthropic.com", "ANTHROPIC_API_KEY", "claude-opus-5-5",
             note="An Anthropic API key (console.anthropic.com), billed separately from a Claude subscription."),
    # Defaults checked against each provider's live model list on Nov 13 2026; any other model
    # can be typed in the Providers window's model box (cloud.models.<name>).
    Provider("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
             "cohere/north-mini-code:free", trains=True,
             note="One key for many models. Models ending in :free may be used for training."),
    Provider("deepseek", "DeepSeek", "openai", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "deepseek-chat"),
    Provider("mistral", "Mistral", "openai", "https://api.mistral.ai/v1", "MISTRAL_API_KEY", "codestral-latest"),
    Provider("nvidia", "NVIDIA", "openai", "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY",
             note="build.nvidia.com; trial credits."),
    Provider("gemini", "Gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY",
             "gemini-flash-latest", trains=True, alt_env=("GOOGLE_API_KEY",), note="Google's free tier may use what's sent to improve its products."),
    Provider("groq", "Groq", "openai", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "openai/gpt-oss-120b"),
    Provider("cerebras", "Cerebras", "openai", "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "gpt-oss-120b",
             alt_env=("CEREBRAS_AI_KEY", "CEREBRAS_KEY")),
    Provider("together", "Together", "openai", "https://api.together.xyz/v1", "TOGETHER_API_KEY"),
    Provider("fireworks", "Fireworks", "openai", "https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY"),
    Provider("openai", "OpenAI", "openai", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    Provider("cloudflare", "Cloudflare", "openai", "https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1",
             "CLOUDFLARE_API_TOKEN", "@cf/openai/gpt-oss-120b", account_env="CLOUDFLARE_ACCOUNT_ID",
             alt_env=("CF_API_TOKEN", "CLOUDFLARE_API_KEY"),
             note="Workers AI: an account API token and the account ID (CLOUDFLARE_ACCOUNT_ID) from the dashboard."),
    Provider("vercel", "Vercel", "openai", "https://ai-gateway.vercel.sh/v1", "AI_GATEWAY_API_KEY", "openai/gpt-oss-120b",
             trains=True, alt_env=("VERCEL_API_KEY", "VERCEL_AI_GATEWAY_KEY"),
             note="Vercel's AI Gateway: many providers' models with one key. Free models only on the free tier."),
    Provider("huggingface", "Hugging Face", "openai", "https://router.huggingface.co/v1", "HF_TOKEN", "openai/gpt-oss-120b",
             alt_env=("HUGGINGFACE_API_KEY", "HUGGINGFACEHUB_API_TOKEN", "HF_API_KEY")),
]}


def spoken_names() -> dict[str, str]:
    """{"deep seek": "deepseek", ...}: what voice Othos may hear, lower case."""
    out = {}
    for p in PROVIDERS.values():
        for s in {p.spoken.lower(), p.name, re.sub(r"(?<=[a-z])(?=[A-Z])", " ", p.spoken).lower()}:
            out[s] = p.name
    out.update({"deep seek": "deepseek", "open router": "openrouter", "anthropic": "anthropic",
                "open ai": "openai", "hugging face": "huggingface", "l m studio": "lmstudio",
                "lm studio": "lmstudio", "codestral": "mistral",
                "cloud flare": "cloudflare", "vercel": "vercel", "versel": "vercel", "free providers": "auto", "switching": "auto", "switch order": "auto"})   # the switch order
    return out


# ---------------------------------------------------------------- free allowances
# How much each provider gives away, for the Providers window's order (most to least) and the
# "Sort by free allowance" switch order. score = about how many tokens a day it's worth (an
# estimate where the provider gives requests instead); 0 = paid only. Figures from each
# provider's own pages, checked on the date given; live figures (state\free-limits.json) win.
FREE_TIERS: dict[str, dict] = {
    "mistral": {"score": 33_000_000, "free": "about 1 billion tokens a month (may train on what's sent)",
                "checked": "2026-11-13", "source": "https://help.mistral.ai/en/articles/225174-what-are-the-limits-of-the-free-tier"},
    "cerebras": {"score": 1_000_000, "free": "1M tokens and 2,400 requests a day (5 a minute)",
                 "checked": "2026-11-13", "source": "https://inference-docs.cerebras.ai/support/rate-limits"},
    "cloudflare": {"score": 250_000, "free": "10,000 neurons a day (gpt-oss-120b: about 300K tokens in or 150K out)",
                   "checked": "2026-11-13", "source": "https://developers.cloudflare.com/workers-ai/platform/pricing/"},
    "groq": {"score": 200_000, "free": "about 1,000 requests and 200K tokens a day per model",
             "checked": "2026-11-13", "source": "https://console.groq.com/docs/rate-limits"},
    "nvidia": {"score": 150_000, "free": "40 requests a minute, no daily total (for testing, not everyday use)",
               "checked": "2026-11-13", "source": "https://build.nvidia.com"},
    "gemini": {"score": 100_000, "free": "Flash: a small daily number, set per project (see AI Studio)",
               "checked": "2026-11-13", "source": "https://aistudio.google.com/rate-limit"},
    "openrouter": {"score": 50_000, "free": "50 free requests a day (1,000 once $10 of credit has been bought)",
                   "checked": "2026-11-13", "source": "https://openrouter.ai/docs/api/reference/limits"},
    "vercel": {"score": 300_000, "free": "$5 of credit every 30 days on its free-tier models; needs a card on file first",
               "checked": "2026-11-13", "source": "https://vercel.com/docs/ai-gateway/pricing"},
    "huggingface": {"score": 0, "free": "none on a free account; $2 a month with PRO",
                    "checked": "2026-11-13", "source": "https://huggingface.co/docs/inference-providers/pricing"},
}
STALE_DAYS = 30                                        # older than this: "worth re-checking"
LIVE_PATH = HERE / "state" / "free-limits.json"


def _live() -> dict:
    try:
        return json.loads(LIVE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_live(name: str, info: dict) -> None:
    d = _live()
    d[name] = {**(d.get(name) or {}), **info, "seen": time.time()}
    LIVE_PATH.parent.mkdir(exist_ok=True)
    LIVE_PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")


def _note_limits(name: str, headers) -> None:
    """The limits a provider reports on each reply (Groq, Cerebras, Mistral...), kept for the window."""
    try:
        h = {k.lower(): v for k, v in headers.items() if "ratelimit" in k.lower()}
        if h:
            _save_live(name, {"headers": h})
    except Exception:
        pass


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def free_info(name: str) -> dict:
    """{"score", "free" (text), "live" (text or ""), "checked", "source", "stale"} for one provider."""
    t = dict(FREE_TIERS.get(name) or {"score": 0, "free": "", "checked": "", "source": ""})
    live = _live().get(name) or {}
    h = live.get("headers") or {}
    text = ""
    tday = _int(h.get("x-ratelimit-limit-tokens-day"))
    rday = _int(h.get("x-ratelimit-limit-requests-day")) or (_int(h.get("x-ratelimit-limit-requests")) if name == "groq" else 0)
    tmonth = _int(h.get("x-ratelimit-limit-tokens-month"))
    if tday:
        t["score"] = tday
        left = _int(h.get("x-ratelimit-remaining-tokens-day"))
        text = f"{tday:,} tokens a day" + (f" ({left:,} left at the last reply)" if left else "")
    elif tmonth:
        t["score"] = tmonth // 30
        text = f"{tmonth:,} tokens a month"
    elif rday:
        left = _int(h.get("x-ratelimit-remaining-requests-day") or h.get("x-ratelimit-remaining-requests"))
        text = f"{rday:,} requests a day" + (f" ({left:,} left at the last reply)" if left else "")
    if name == "openrouter" and live.get("free_requests_limit"):
        lim = int(live["free_requests_limit"])
        t["score"] = lim * 1000
        text = f"{lim:,} free requests a day, {int(live.get('free_requests_used', 0)):,} used today"
    if name == "huggingface" and "pro" in live:
        t["score"] = 100_000 if live["pro"] else 0
        text = "PRO: $2 of credit a month" if live["pro"] else "not PRO: nothing free"
    if text:
        t["live"] = text + time.strftime(" (checked %b %d)", time.localtime(live.get("seen", time.time()))).replace(" 0", " ")
    else:
        t["live"] = ""
    try:
        age = (time.time() - time.mktime(time.strptime(t["checked"], "%Y-%m-%d"))) / 86400 if t.get("checked") else 0
    except ValueError:
        age = 0
    t["stale"] = bool(t.get("free")) and not text and age > STALE_DAYS
    return t


def refresh_free_limits(enabled=()) -> dict:
    """Re-check what can be checked for free (no test requests): OpenRouter's account page gives
    today's free requests; Hugging Face's says whether the account is PRO. Run when models are
    checked (Get models, the Models window) and by the Providers window's Refresh.
    Only providers that are switched On (enabled) are contacted: with none On, nothing is sent."""
    keys, out = _keys(), {}
    on = {str(e).lower() for e in enabled}
    p = PROVIDERS["openrouter"]
    if "openrouter" in on and key_for(p, keys):
        try:
            r = httpx.get("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key_for(p, keys)}"}, timeout=20)
            d = (r.json() or {}).get("data") or {}
            fr = d.get("free_model_daily_requests") or {}
            if r.status_code in (200, 401, 403):
                _save_live("openrouter", {"key_ok": r.status_code == 200})
            if r.status_code == 200 and d.get("expires_at"):
                _save_live("openrouter", {"key_expires": str(d["expires_at"])})
            if r.status_code == 200 and fr:
                _save_live("openrouter", {"free_requests_limit": fr.get("limit"), "free_requests_used": fr.get("used", 0)})
                out["openrouter"] = "checked"
            else:
                out["openrouter"] = f"no answer ({r.status_code})"
        except Exception as e:
            out["openrouter"] = f"couldn't reach it ({type(e).__name__})"
    p = PROVIDERS["huggingface"]
    if "huggingface" in on and key_for(p, keys):
        try:
            r = httpx.get("https://huggingface.co/api/whoami-v2", headers={"Authorization": f"Bearer {key_for(p, keys)}"}, timeout=20)
            if r.status_code in (200, 401, 403):
                _save_live("huggingface", {"key_ok": r.status_code == 200})
            if r.status_code == 200:
                who = r.json() or {}
                exp = (((who.get("auth") or {}).get("accessToken") or {}).get("expiration")
                       or ((who.get("auth") or {}).get("accessToken") or {}).get("expiresAt"))
                _save_live("huggingface", {"pro": bool(who.get("isPro")), **({"key_expires": str(exp)} if exp else {})})
                out["huggingface"] = "checked"
            else:
                out["huggingface"] = f"no answer ({r.status_code})"
        except Exception as e:
            out["huggingface"] = f"couldn't reach it ({type(e).__name__})"
    p = PROVIDERS["vercel"]                           # its credits page: is the key accepted
    if "vercel" in on and key_for(p, keys):
        try:
            r = httpx.get("https://ai-gateway.vercel.sh/v1/credits", headers={"Authorization": f"Bearer {key_for(p, keys)}"},
                          timeout=20)
            if r.status_code in (200, 401, 403):
                _save_live("vercel", {"key_ok": r.status_code == 200})
            out["vercel"] = "checked" if r.status_code == 200 else f"no answer ({r.status_code})"
        except Exception as e:
            out["vercel"] = f"couldn't reach it ({type(e).__name__})"
    p = PROVIDERS["cloudflare"]                       # an account token says when it expires
    acc = str(keys.get(p.account_env) or "").strip().lower()
    if "cloudflare" in on and key_for(p, keys) and re.fullmatch(r"[0-9a-f]{32}", acc):
        try:
            r = httpx.get(f"https://api.cloudflare.com/client/v4/accounts/{acc}/tokens/verify",
                          headers={"Authorization": f"Bearer {key_for(p, keys)}"}, timeout=20)
            res = (r.json() or {}).get("result") or {}
            if r.status_code == 200 and res.get("status") == "active":
                _save_live("cloudflare", {"key_expires": str(res.get("expires_on") or ""), "key_ok": True})
                out["cloudflare"] = "checked"
            else:
                _save_live("cloudflare", {"key_ok": False})
                out["cloudflare"] = f"the token wasn't accepted ({r.status_code})"
        except Exception as e:
            out["cloudflare"] = f"couldn't reach it ({type(e).__name__})"
    return out


def key_expiry(name: str) -> tuple[str, bool]:
    """("Feb 12, 2027", soon) for a key that reports an end date (Cloudflare, OpenRouter, Hugging
    Face); soon = within 30 days or past. ("", False) when there's none or it's unknown."""
    raw = str((_live().get(name) or {}).get("key_expires") or "").strip()
    if not raw:
        return "", False
    try:
        from datetime import datetime, timezone
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        local = when.astimezone()
        days = (when - datetime.now(timezone.utc)).days
        return local.strftime("%b %d, %Y").replace(" 0", " "), days < 30
    except ValueError:
        return "", False


def ranked(names) -> list[str]:
    """Most free allowance first; paid-only ones last (in the order given)."""
    names = list(names)
    return sorted(names, key=lambda n: (-free_info(n)["score"], names.index(n)))


# ---------------------------------------------------------------- the checks
def _keys() -> dict:
    """.env keys, then the encrypted ones saved in the Providers window (those win)."""
    keys = {k: v for k, v in dotenv_values(HERE / ".env").items() if v}
    try:
        import worker_keys
        keys.update(worker_keys.saved())
    except Exception as e:                            # not on Windows, or the store can't be read
        log.warning("saved keys couldn't be read: %s", type(e).__name__)
    return keys


def key_for(p: Provider, keys: dict) -> str:
    """This provider's key: its own name first, then the other names people use for it."""
    for k in (p.key_env, *p.alt_env):
        if k and keys.get(k):
            return keys[k]
    return ""


# Where each provider hands out keys (the window's "Get a key"); its own site, nothing else.
KEY_PAGES = {
    "lmstudio": "https://lmstudio.ai", "anthropic": "https://console.anthropic.com/settings/keys",
    "openrouter": "https://openrouter.ai/keys", "deepseek": "https://platform.deepseek.com/api_keys",
    "mistral": "https://console.mistral.ai/api-keys", "nvidia": "https://build.nvidia.com",
    "gemini": "https://aistudio.google.com/apikey", "groq": "https://console.groq.com/keys",
    "cerebras": "https://cloud.cerebras.ai", "together": "https://api.together.ai/settings/api-keys",
    "fireworks": "https://fireworks.ai/account/api-keys", "openai": "https://platform.openai.com/api-keys",
    "huggingface": "https://huggingface.co/settings/tokens", "cloudflare": "https://dash.cloudflare.com/profile/api-tokens", "vercel": "https://vercel.com/dashboard/ai-gateway",
}


# ---------------------------------------------------------------- the Providers window's commands
def command(argv: list[str], config_path: Path) -> bool:
    """`providers`, `provider-set NAME [on|off] [model=M] [accept=yes|no] [order=N]`, `set-key NAME` (key on
    stdin), `del-key NAME`, `test-provider NAME`, `set-limits TOKENS_PER_DAY USD_PER_DAY`.
    Shared by both workers (worker_engine.main). True if it was one of these."""
    import sys
    import worker_keys
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd not in ("providers", "provider-set", "set-key", "del-key", "test-provider", "set-limits", "free-limits",
                   "switch-sort"):
        return False
    cfg = worker_shared.load_json(config_path)
    cc = cfg.setdefault("cloud", {"enabled": [], "default": "", "models": {}, "accept_data_use": [],
                                  "max_tokens_per_task": 200000, "max_tokens_per_day": 1000000,
                                  "prices": {}, "max_usd_per_day": 2.0, "local_urls": {}})

    def save():
        config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    def named() -> Provider:
        p = PROVIDERS.get((argv[2] if len(argv) > 2 else "").lower())
        if p is None:
            raise SystemExit("unknown provider")
        return p

    if cmd == "free-limits":                          # re-check free allowances (no test requests)
        print(json.dumps(refresh_free_limits(cc.get("enabled", []))))
        return True
    if cmd == "switch-sort":                          # the switch order, most free allowance first
        ready = [n for n in cc.get("enabled", []) if n in PROVIDERS and not PROVIDERS[n].local
                 and not _problem(cfg, PROVIDERS[n]) and free_info(n)["score"] > 0
                 and (_live().get(n) or {}).get("key_ok") is not False]    # not a key the provider refused
        cc["switch_order"] = ranked(ready)
        save()
        print(json.dumps({"switch_order": cc["switch_order"]}))
        return True
    if cmd == "providers":
        env = {k: v for k, v in dotenv_values(HERE / ".env").items() if v}
        store = worker_keys.saved()
        rows = []
        order = [str(x).lower() for x in cc.get("switch_order", [])]
        # local first, then the cloud by free allowance (most to least), then paid only
        names = [n for n, p in PROVIDERS.items() if p.local] + ranked(n for n, p in PROVIDERS.items() if not p.local)
        for p in (PROVIDERS[n] for n in names):
            fi = free_info(p.name)
            k = (key_for(p, store) or key_for(p, env)) if p.key_env else ""
            rows.append({"name": p.name, "label": p.spoken, "note": p.note,
                         "where": "local" if p.local else "proxy" if p.proxy else "cloud",
                         "trains": p.trains, "needs_key": bool(p.key_env), "key_env": p.key_env,
                         "key": worker_keys.last4(k), "key_from": "saved" if key_for(p, store) else ".env" if k else "",
                         "on": p.name in [str(e).lower() for e in cc.get("enabled", [])],
                         "model": (cc.get("models") or {}).get(p.name, "") or p.model,
                         "accepted": p.name in [str(e).lower() for e in cc.get("accept_data_use", [])],
                         "key_page": KEY_PAGES.get(p.name, ""), "problem": _problem(cfg, p),
                         "order": order.index(p.name) + 1 if p.name in order else 0,
                         "out": _out_until(p.name) > time.time(),
                         "free": fi.get("free", ""), "free_live": fi.get("live", ""), "free_checked": fi.get("checked", ""),
                         "free_source": fi.get("source", ""), "free_stale": fi.get("stale", False),
                         "paid_only": not p.local and fi["score"] <= 0,
                         "key_bad": bool(k) and (_live().get(p.name) or {}).get("key_ok") is False,
                         "key_expires": key_expiry(p.name)[0] if k else "", "key_expires_soon": key_expiry(p.name)[1] if k else False})
        print(json.dumps({"providers": rows, "max_tokens_per_day": cc.get("max_tokens_per_day", 1000000),
                          "max_usd_per_day": cc.get("max_usd_per_day", 2.0), "today": usage_today()}))
    elif cmd == "provider-set":
        p = named()
        on = [str(e).lower() for e in cc.get("enabled", [])]
        acc = [str(e).lower() for e in cc.get("accept_data_use", [])]
        for a in argv[3:]:
            if a in ("on", "off"):
                on = [x for x in on if x != p.name] + ([p.name] if a == "on" else [])
                if a == "off" and cc.get("default") == p.name:
                    cc["default"] = ""                    # back to the local model
            elif a.startswith("model="):
                cc.setdefault("models", {})[p.name] = a[6:].strip()
            elif a.startswith("order="):              # place in the switch order (0 = not in it)
                order = [str(x).lower() for x in cc.get("switch_order", []) if str(x).lower() != p.name]
                try:
                    n = int(a[6:])
                except ValueError:
                    raise SystemExit("order must be a number")
                if n > 0:
                    order.insert(min(n, len(order) + 1) - 1, p.name)
                cc["switch_order"] = order
            elif a in ("accept=yes", "accept=no"):
                acc = [x for x in acc if x != p.name] + ([p.name] if a == "accept=yes" else [])
        cc["enabled"], cc["accept_data_use"] = on, acc
        save()
        print("saved")
    elif cmd == "set-key":
        p = named()
        if not p.key_env:
            raise SystemExit(f"{p.spoken} needs no key")
        try:
            worker_keys.set_key(p.key_env, sys.stdin.readline())
        except ValueError as e:
            raise SystemExit(str(e))
        print("saved")
    elif cmd == "del-key":
        p = named()
        print("removed" if worker_keys.del_key(p.key_env) else "no saved key (a key in .env stays there)")
    elif cmd == "set-limits":
        try:
            cc["max_tokens_per_day"] = max(0, int(float(argv[2])))
            cc["max_usd_per_day"] = max(0.0, float(argv[3]))
        except (IndexError, ValueError):
            raise SystemExit("usage: set-limits TOKENS_PER_DAY USD_PER_DAY")
        save()
        print("saved")
    else:                                             # test-provider: one tiny request, nothing of the user's
        p = named()
        model = (cc.get("models") or {}).get(p.name) or p.model
        key = key_for(p, _keys()) if p.key_env else ""
        if p.key_env and not key:
            print(f"No key saved for {p.spoken}.")
            return True
        if not model and not p.local:
            print(f"Set a model for {p.spoken} first.")
            return True
        try:
            b = (AnthropicCloud if p.kind == "anthropic" else OpenAICompatible)(p, key, _base_url(p, cc), cc)
            out = b.chat(model, [{"role": "user", "content": "Reply with the single word OK."}], 2, 300,   # room for
                         lambda c=0: None)                                                          # reasoning models
            print(f"Works: {p.spoken} answered “{out.strip()[:40]}”.")
        except (RuntimeError, ValueError) as e:
            print(f"Not working: {str(e)[:220]}")
        except Exception as e:
            print(f"Not working: couldn't reach {p.spoken} ({type(e).__name__}).")
    return True


def _problem(cfg: dict, p: Provider) -> str:
    """Why a provider that's switched On can't be used yet ("" = ready, or it's Off)."""
    if p.name not in [str(e).lower() for e in (cfg.get("cloud") or {}).get("enabled", [])]:
        return ""
    try:
        pick(cfg, {"provider": p.name, "no_switch": True}, "review")
        return ""
    except ValueError as e:
        return str(e)


def _base_url(p: Provider, cc: dict, keys: dict | None = None) -> str:
    url = p.base_url
    if p.account_env:                                  # Cloudflare: .../accounts/<32 hex>/ai/v1, same host always
        acc = str((keys if keys is not None else _keys()).get(p.account_env) or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", acc):
            raise ValueError(f"no {p.account_env} in .env for {p.spoken} (32 letters and digits, from its dashboard)")
        url = url.replace("{account}", acc)
    if (p.local or p.proxy) and cc.get("local_urls", {}).get(p.name):   # a server on this PC, another port
        url = cc["local_urls"][p.name].rstrip("/")
        if urlparse(url).hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(f"{p.spoken} must be on this PC (127.0.0.1), not {urlparse(url).hostname}")
    return url


def pick(cfg: dict, raw_params: dict, task_type: str, local=None):
    """(backend, model) for this task, or None for the worker's own local model.
    local = (the worker's own backend, its model): the last stop when switching.
    Raises ValueError (the task fails with that reason) when a safeguard says no."""
    cc = cfg.get("cloud") or {}
    name = str(raw_params.get("provider") or cc.get("default") or "").lower().strip()
    if not name or name == "local":
        return None
    order = [str(n).lower() for n in cc.get("switch_order", [])]
    if name == "auto" or (name in order and not raw_params.get("no_switch")):
        if task_type == "check" and not cc.get("allow_check_task"):
            return None                                # the check task stays on this PC
        if name != "auto":                             # a provider asked for by name must itself be ready
            pick(cfg, {**raw_params, "no_switch": True}, task_type)
        names = order if name == "auto" else [name] + [n for n in order if n != name]
        links = []
        for n in names:
            try:
                params = {"provider": n, "no_switch": True}
                if n == name and raw_params.get("model"):
                    params["model"] = raw_params["model"]
                b, m = pick(cfg, params, task_type)
                links.append((n, PROVIDERS[n].spoken, b, m))
            except ValueError as e:
                log.info("switch order: %s skipped (%s)", n, e)
        if not links:
            if local is not None:                      # a task: nothing ready, so this PC's model; nothing sent
                log.info("switch order: no provider ready; using the local model")
                return None
            raise ValueError("no provider in the switch order is ready (Providers...: On, a key and a model)")
        return Chain(links, local), "switching"
    p = PROVIDERS.get(name)
    if p is None:
        raise ValueError(f"'{name}' isn't a provider I know (known: {', '.join(PROVIDERS)})")
    if name not in [str(e).lower() for e in cc.get("enabled", [])]:
        raise ValueError(f"{p.spoken} isn't turned on (add it to cloud.enabled in the settings)")
    if task_type == "check" and not p.local and not cc.get("allow_check_task"):
        raise ValueError("the check task stays on this PC (it holds decompiled code); set cloud.allow_check_task to send it")
    model = str(raw_params.get("model") or (cc.get("models") or {}).get(name) or p.model)
    if not model and not p.local:
        raise ValueError(f"no model set for {p.spoken}: type one in its model box (Providers…) or set cloud.models.{name}")
    if p.trains and name not in [str(e).lower() for e in cc.get("accept_data_use", [])]:
        if name != "openrouter" or model.endswith(":free"):
            raise ValueError(f"{p.spoken}'s free tier may use what's sent for training; add it to "
                             "cloud.accept_data_use if that's all right")
    key = ""
    if p.key_env:
        key = key_for(p, _keys())
        if not key:
            raise ValueError(f"no key for {p.spoken}: save one in Providers…, or put {p.key_env} in .env")
    _check_budget(cc, name, 0)
    backend = (AnthropicCloud if p.kind == "anthropic" else OpenAICompatible)(p, key, _base_url(p, cc), cc)
    return backend, model


# ---------------------------------------------------------------- spending
def _usage() -> dict:
    try:
        return json.loads(USAGE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _today(u: dict) -> dict:
    day = time.strftime("%Y-%m-%d")
    if u.get("day") != day:
        u.clear()
        u.update({"day": day, "providers": {}})
    return u


def _cost(cc: dict, name: str, tin: int, tout: int) -> float:
    price = (cc.get("prices") or {}).get(name)
    return (tin * float(price[0]) + tout * float(price[1])) / 1e6 if price else 0.0


class BudgetOut(ValueError):
    """One of the user's own cloud limits is used up (no provider can help; the local model can)."""


class Refused(RuntimeError):
    """A provider said no: .status is its HTTP status, .retry_after how long it asked us to wait."""
    def __init__(self, message: str, status: int = 0, retry_after: float = 0.0):
        super().__init__(message)
        self.status, self.retry_after = status, retry_after


def _retry_after(headers) -> float:
    try:
        return max(0.0, float((headers or {}).get("retry-after") or 0))
    except (TypeError, ValueError):
        return 0.0


def _check_budget(cc: dict, name: str, task_tokens: int) -> None:
    u = _today(_usage())
    used = sum(v.get("in", 0) + v.get("out", 0) for v in u["providers"].values())
    usd = sum(v.get("usd", 0.0) for v in u["providers"].values())
    if task_tokens > int(cc.get("max_tokens_per_task", 200_000)):
        raise BudgetOut(f"this task went over max_tokens_per_task ({cc.get('max_tokens_per_task', 200_000):,})")
    if used + task_tokens > int(cc.get("max_tokens_per_day", 1_000_000)):
        raise BudgetOut(f"today's cloud limit is used up (max_tokens_per_day {cc.get('max_tokens_per_day', 1_000_000):,})")
    if cc.get("max_usd_per_day") is not None and usd > float(cc["max_usd_per_day"]):
        raise BudgetOut(f"today's cloud spending limit is used up (${cc['max_usd_per_day']})")


def _record(cc: dict, name: str, tin: int, tout: int) -> None:
    u = _today(_usage())
    v = u["providers"].setdefault(name, {"in": 0, "out": 0, "usd": 0.0, "calls": 0})
    v["in"] += tin
    v["out"] += tout
    v["calls"] += 1
    v["usd"] = round(v["usd"] + _cost(cc, name, tin, tout), 6)
    USAGE_PATH.parent.mkdir(exist_ok=True)
    USAGE_PATH.write_text(json.dumps(u, indent=2), encoding="utf-8")


def usage_today() -> dict:
    return _today(_usage())


def _out_until(name: str) -> float:
    """When a provider that ran out may be tried again (0 = it's fine). Forgotten each new day."""
    return float((_today(_usage()).get("out") or {}).get(name, 0))


def _mark_out(name: str, seconds: float) -> None:
    u = _today(_usage())
    u.setdefault("out", {})[name] = time.time() + seconds
    USAGE_PATH.parent.mkdir(exist_ok=True)
    USAGE_PATH.write_text(json.dumps(u, indent=2), encoding="utf-8")


def _rest_for(e: Exception) -> float:
    """How long to leave a provider alone after it said no."""
    if isinstance(e, Refused):
        if e.status == 402:
            return 24 * 3600                           # no credit left
        if e.status == 429:
            return e.retry_after or 3600               # out of free tokens / over its limit
        if e.status in (401, 403, 404):
            return 3600                                # its key or model needs fixing
    return 600                                         # couldn't reach it, or it stopped mid-answer


# The user's answer while a task waits for free tokens (the app's Yes/No, voice, `out-choice`).
def _choice_path(status_path: Path) -> Path:
    return status_path.with_name(status_path.stem + "-choice.json")


def set_out_choice(status_path: Path, task_id: int, choice: str) -> None:
    """choice: "local" (finish on this PC now), "wait" (carry on when free tokens are back), "" (clear)."""
    _choice_path(status_path).write_text(json.dumps({"task": int(task_id), "choice": choice}), encoding="utf-8")


def out_choice(status_path: Path, task_id: int) -> str:
    try:
        d = json.loads(_choice_path(status_path).read_text(encoding="utf-8"))
        return str(d.get("choice") or "") if int(d.get("task", -1)) == int(task_id) else ""
    except Exception:
        return ""


class Chain:
    """The switch order as one back end: each step goes to the first provider that still has free
    tokens; when one says it's out, the same step goes to the next, and last to this PC's model.
    A step always carries the whole job so far, so switching loses nothing."""

    def __init__(self, links: list, local=None):
        self.links, self.local = links, local          # links: [(name, spoken, backend, model)]
        self.used: list[str] = []                      # who did each step, in order (for the result)
        self.ask = None                                # set by the engine: ask(free_again_ts) -> "local" | "retry"
        names = [l[1] for l in links] + (["this PC"] if local else [])
        self.name = "switching (" + " -> ".join(names) + ")"

    def set_low_priority(self, low: bool) -> None:
        pass

    def close(self) -> None:
        pass

    def _did(self, who: str) -> None:
        if not self.used or self.used[-1] != who:
            self.used.append(who)

    def chat(self, model: str, messages: list[dict], threads: int, max_tokens: int, should_stop) -> str:
        while True:
            out = self._try_links(messages, threads, max_tokens, should_stop)
            if out is not None:
                return out
            if self.ask is None:
                return self._local(None, messages, threads, max_tokens, should_stop)
            back = min(_out_until(l[0]) for l in self.links)
            if self.ask(back) == "local":
                return self._local(None, messages, threads, max_tokens, should_stop)
            # "retry": free tokens should be back; go round the order again

    def _try_links(self, messages, threads, max_tokens, should_stop):
        """The step's answer from the first provider with free tokens, or None when all are out."""
        for name, spoken, backend, m in self.links:
            if _out_until(name) > time.time():
                continue
            for attempt in (1, 2):
                try:
                    out = backend.chat(m, messages, threads, max_tokens, should_stop)
                    self._did(f"{spoken} ({m})")
                    return out
                except BudgetOut as e:                 # the user's own daily limit: no provider can help
                    log.warning("switching: %s; going to this PC's model", e)
                    return self._local(e, messages, threads, max_tokens, should_stop)
                except (Refused, RuntimeError, httpx.HTTPError, ValueError) as e:
                    wait = e.retry_after if isinstance(e, Refused) and e.status == 429 else 0
                    if attempt == 1 and 0 < wait <= 60:           # just "slow down": wait, same provider
                        log.info("switching: %s asked to wait %.0f s", spoken, wait)
                        end = time.monotonic() + wait
                        while time.monotonic() < end:
                            should_stop(0)
                            time.sleep(1)
                        continue
                    _mark_out(name, _rest_for(e))
                    log.warning("switching: %s can't take this step (%s); trying the next", spoken, str(e)[:160])
                    break
        return None

    def _local(self, why, messages, threads, max_tokens, should_stop) -> str:
        if not self.local:
            raise RuntimeError(str(why) if why else "every provider in the switch order is out of free tokens for now")
        self._did("this PC's model")
        backend, model = self.local
        return backend.chat(model, messages, threads, max_tokens, should_stop)


def _clean(messages: list[dict]) -> list[dict]:
    return [{"role": m["role"], "content": worker_shared.redact(str(m.get("content", "")))} for m in messages]


def _refused(who: str, status: int, body: str) -> str:
    """A refused call, said plainly (the provider's own words after it, cut short)."""
    plain = {401: "didn't accept the key (check it in .env)", 402: "says the account has no credit left (top it up "
             "on their site)", 403: "says this key may not do that", 404: "doesn't know that model (check "
             "cloud.models)", 429: "says too many requests or over its limit; try again later"}.get(status)
    return f"{who} {plain or f'error {status}'}" + (f" ({status}: {body[:200]})" if body else "")


def _scrub(text: str, key: str) -> str:
    return text.replace(key, "[key]") if key else text


# ---------------------------------------------------------------- the two kinds of API
class _Base:
    def __init__(self, p: Provider, key: str, url: str, cc: dict):
        self.p, self.key, self.url, self.cc = p, key, url, cc
        self.name = f"{p.name} ({'this PC' if p.local else 'this PC, then cloud' if p.proxy else 'cloud'})"

    def set_low_priority(self, low: bool) -> None:
        pass                                            # nothing runs on this PC (or LM Studio's own)

    def close(self) -> None:
        pass

    def _tick(self, should_stop, chars: int, tin: int) -> None:
        should_stop(chars)
        _check_budget(self.cc, self.p.name, tin + chars // 4)


class OpenAICompatible(_Base):
    def chat(self, model: str, messages: list[dict], threads: int, max_tokens: int, should_stop) -> str:
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        if self.p.name == "openrouter":
            headers["X-Title"] = "Othos"                # no personal details
        body = {"model": model, "messages": _clean(messages), "stream": True, "max_tokens": max_tokens,
                "stream_options": {"include_usage": True}}
        if self.p.local and not model:
            body.pop("model")
        out, tin, tout, accepted = [], 0, 0, False
        try:
            with httpx.stream("POST", f"{self.url}/chat/completions", json=body, headers=headers,
                              timeout=httpx.Timeout(30, read=600)) as r:
                if r.status_code != 200:
                    raise Refused(_refused(self.p.spoken, r.status_code,
                                           _scrub(r.read().decode(errors="replace"), self.key)),
                                  r.status_code, _retry_after(r.headers))
                accepted = True                         # only an accepted call counts as use
                _note_limits(self.p.name, r.headers)    # the limits it reports (for the Providers window)
                for line in r.iter_lines():
                    self._tick(should_stop, sum(map(len, out)), tin)
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    piece = json.loads(data)
                    if piece.get("usage"):
                        tin = int(piece["usage"].get("prompt_tokens") or 0)
                        tout = int(piece["usage"].get("completion_tokens") or 0)
                    for ch in piece.get("choices") or []:
                        out.append((ch.get("delta") or {}).get("content") or "")
        finally:
            text = "".join(out)
            if accepted and not self.p.local:
                _record(self.cc, self.p.name, tin or sum(len(str(m.get("content", ""))) for m in messages) // 4,
                        tout or len(text) // 4)
        return text


class AnthropicCloud(_Base):
    def chat(self, model: str, messages: list[dict], threads: int, max_tokens: int, should_stop) -> str:
        import anthropic                                # only loaded when Claude is used
        msgs = _clean(messages)
        system = "\n\n".join(m["content"] for m in msgs if m["role"] == "system")
        convo = [m for m in msgs if m["role"] != "system"] or [{"role": "user", "content": "Go ahead."}]
        client = anthropic.Anthropic(api_key=self.key, base_url=self.url, max_retries=2)
        out, tin, tout, accepted = [], 0, 0, False
        try:
            with client.beta.messages.stream(model=model, max_tokens=max(max_tokens, 4000), system=system,
                                             messages=convo, betas=["server-side-fallback-2026-07-01"],
                                             fallbacks="default") as stream:
                accepted = True
                for text in stream.text_stream:
                    out.append(text)
                    self._tick(should_stop, sum(map(len, out)), tin)
                final = stream.get_final_message()
                tin, tout = int(final.usage.input_tokens or 0), int(final.usage.output_tokens or 0)
                if final.stop_reason == "refusal":
                    raise RuntimeError("Claude declined this request")
        except anthropic.APIStatusError as e:
            accepted = accepted and e.status_code < 400
            raise Refused(_refused("Claude", e.status_code, _scrub(str(e.message), self.key)), e.status_code,
                          _retry_after(getattr(e.response, "headers", None))) from None
        except anthropic.APIConnectionError:
            accepted = False
            raise RuntimeError("couldn't reach Anthropic (network)") from None
        finally:
            if accepted:
                _record(self.cc, self.p.name, tin or sum(len(m["content"]) for m in msgs) // 4,
                    tout or sum(map(len, out)) // 4)
        return "".join(out)
