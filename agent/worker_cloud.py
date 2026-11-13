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


PROVIDERS: dict[str, Provider] = {p.name: p for p in [
    # local first
    Provider("lmstudio", "LM Studio", "openai", "http://127.0.0.1:1234/v1", "", local=True,
             note="LM Studio's local server (Developer tab, Start Server). Nothing leaves this PC."),
    # cloud
    Provider("anthropic", "Claude", "anthropic", "https://api.anthropic.com", "ANTHROPIC_API_KEY", "claude-opus-5-5",
             note="An Anthropic API key (console.anthropic.com), billed separately from a Claude subscription."),
    Provider("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", trains=True,
             note="One key for many models. Models ending in :free may be used for training."),
    Provider("deepseek", "DeepSeek", "openai", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "deepseek-chat"),
    Provider("mistral", "Mistral", "openai", "https://api.mistral.ai/v1", "MISTRAL_API_KEY", "codestral-latest"),
    Provider("nvidia", "NVIDIA", "openai", "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY",
             note="build.nvidia.com; trial credits."),
    Provider("gemini", "Gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY",
             trains=True, note="Google's free tier may use what's sent to improve its products."),
    Provider("groq", "Groq", "openai", "https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    Provider("cerebras", "Cerebras", "openai", "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY"),
    Provider("together", "Together", "openai", "https://api.together.xyz/v1", "TOGETHER_API_KEY"),
    Provider("fireworks", "Fireworks", "openai", "https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY"),
    Provider("openai", "OpenAI", "openai", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    Provider("huggingface", "Hugging Face", "openai", "https://router.huggingface.co/v1", "HF_TOKEN"),
    # FreeLLMAPI (github.com/tashfeenahmed/freellmapi): a proxy you run yourself that pools many
    # providers' free tiers behind one local address; its key comes from its own dashboard.
    # Free tiers may train on what's sent, so it counts as cloud.
    Provider("freellmapi", "FreeLLMAPI", "openai", "http://127.0.0.1:3001/v1", "FREELLMAPI_KEY", trains=True,
             proxy=True, note="Runs on this PC (server: port 3001; desktop app: 31415, set in local_urls) but sends to free cloud tiers; its authors call it for "
                              "personal experimentation."),
]}


def spoken_names() -> dict[str, str]:
    """{"deep seek": "deepseek", ...}: what voice Othos may hear, lower case."""
    out = {}
    for p in PROVIDERS.values():
        for s in {p.spoken.lower(), p.name, re.sub(r"(?<=[a-z])(?=[A-Z])", " ", p.spoken).lower()}:
            out[s] = p.name
    out.update({"deep seek": "deepseek", "open router": "openrouter", "anthropic": "anthropic",
                "open ai": "openai", "hugging face": "huggingface", "l m studio": "lmstudio",
                "lm studio": "lmstudio", "codestral": "mistral", "free llm api": "freellmapi",
                "free l l m api": "freellmapi", "free llm a p i": "freellmapi"})
    return out


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


# Where each provider hands out keys (the window's "Get a key"); its own site, nothing else.
KEY_PAGES = {
    "lmstudio": "https://lmstudio.ai", "anthropic": "https://console.anthropic.com/settings/keys",
    "openrouter": "https://openrouter.ai/keys", "deepseek": "https://platform.deepseek.com/api_keys",
    "mistral": "https://console.mistral.ai/api-keys", "nvidia": "https://build.nvidia.com",
    "gemini": "https://aistudio.google.com/apikey", "groq": "https://console.groq.com/keys",
    "cerebras": "https://cloud.cerebras.ai", "together": "https://api.together.ai/settings/api-keys",
    "fireworks": "https://fireworks.ai/account/api-keys", "openai": "https://platform.openai.com/api-keys",
    "huggingface": "https://huggingface.co/settings/tokens", "freellmapi": "https://github.com/tashfeenahmed/freellmapi",
}


# ---------------------------------------------------------------- the Providers window's commands
def command(argv: list[str], config_path: Path) -> bool:
    """`providers`, `provider-set NAME [on|off] [model=M] [accept=yes|no]`, `set-key NAME` (key on
    stdin), `del-key NAME`, `test-provider NAME`, `set-limits TOKENS_PER_DAY USD_PER_DAY`.
    Shared by both workers (worker_engine.main). True if it was one of these."""
    import sys
    import worker_keys
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd not in ("providers", "provider-set", "set-key", "del-key", "test-provider", "set-limits"):
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

    if cmd == "providers":
        env = {k: v for k, v in dotenv_values(HERE / ".env").items() if v}
        store = worker_keys.saved()
        rows = []
        for p in PROVIDERS.values():
            k = (store.get(p.key_env) or env.get(p.key_env, "")) if p.key_env else ""
            rows.append({"name": p.name, "label": p.spoken, "note": p.note,
                         "where": "local" if p.local else "proxy" if p.proxy else "cloud",
                         "trains": p.trains, "needs_key": bool(p.key_env), "key_env": p.key_env,
                         "key": worker_keys.last4(k), "key_from": "saved" if p.key_env in store else ".env" if k else "",
                         "on": p.name in [str(e).lower() for e in cc.get("enabled", [])],
                         "model": (cc.get("models") or {}).get(p.name, "") or p.model,
                         "accepted": p.name in [str(e).lower() for e in cc.get("accept_data_use", [])],
                         "key_page": KEY_PAGES.get(p.name, "")})
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
        key = _keys().get(p.key_env, "") if p.key_env else ""
        if p.key_env and not key:
            print(f"No key saved for {p.spoken}.")
            return True
        if not model and not p.local:
            print(f"Set a model for {p.spoken} first.")
            return True
        try:
            b = (AnthropicCloud if p.kind == "anthropic" else OpenAICompatible)(p, key, _base_url(p, cc), cc)
            out = b.chat(model, [{"role": "user", "content": "Reply with the single word OK."}], 2, 20, lambda c=0: None)
            print(f"Works: {p.spoken} answered “{out.strip()[:40]}”.")
        except (RuntimeError, ValueError) as e:
            print(f"Not working: {str(e)[:220]}")
        except Exception as e:
            print(f"Not working: couldn't reach {p.spoken} ({type(e).__name__}).")
    return True


def _base_url(p: Provider, cc: dict) -> str:
    url = p.base_url
    if (p.local or p.proxy) and cc.get("local_urls", {}).get(p.name):   # a server on this PC, another port
        url = cc["local_urls"][p.name].rstrip("/")
        if urlparse(url).hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(f"{p.spoken} must be on this PC (127.0.0.1), not {urlparse(url).hostname}")
    return url


def pick(cfg: dict, raw_params: dict, task_type: str):
    """(backend, model) for this task, or None for the worker's own local model.
    Raises ValueError (the task fails with that reason) when a safeguard says no."""
    cc = cfg.get("cloud") or {}
    name = str(raw_params.get("provider") or cc.get("default") or "").lower().strip()
    if not name or name == "local":
        return None
    p = PROVIDERS.get(name)
    if p is None:
        raise ValueError(f"'{name}' isn't a provider I know (known: {', '.join(PROVIDERS)})")
    if name not in [str(e).lower() for e in cc.get("enabled", [])]:
        raise ValueError(f"{p.spoken} isn't turned on (add it to cloud.enabled in the settings)")
    if task_type == "check" and not p.local and not cc.get("allow_check_task"):
        raise ValueError("the check task stays on this PC (it holds decompiled code); set cloud.allow_check_task to send it")
    model = str(raw_params.get("model") or (cc.get("models") or {}).get(name) or p.model)
    if not model and not p.local:
        raise ValueError(f"no model set for {p.spoken} (cloud.models.{name} in the settings)")
    if p.trains and name not in [str(e).lower() for e in cc.get("accept_data_use", [])]:
        if name != "openrouter" or model.endswith(":free"):
            raise ValueError(f"{p.spoken}'s free tier may use what's sent for training; add it to "
                             "cloud.accept_data_use if that's all right")
    key = ""
    if p.key_env:
        key = _keys().get(p.key_env, "")
        if not key:
            raise ValueError(f"there's no {p.key_env} in .env for {p.spoken}")
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


def _check_budget(cc: dict, name: str, task_tokens: int) -> None:
    u = _today(_usage())
    used = sum(v.get("in", 0) + v.get("out", 0) for v in u["providers"].values())
    usd = sum(v.get("usd", 0.0) for v in u["providers"].values())
    if task_tokens > int(cc.get("max_tokens_per_task", 200_000)):
        raise ValueError(f"this task went over max_tokens_per_task ({cc.get('max_tokens_per_task', 200_000):,})")
    if used + task_tokens > int(cc.get("max_tokens_per_day", 1_000_000)):
        raise ValueError(f"today's cloud limit is used up (max_tokens_per_day {cc.get('max_tokens_per_day', 1_000_000):,})")
    if cc.get("max_usd_per_day") is not None and usd > float(cc["max_usd_per_day"]):
        raise ValueError(f"today's cloud spending limit is used up (${cc['max_usd_per_day']})")


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
                    raise RuntimeError(_refused(self.p.spoken, r.status_code,
                                                _scrub(r.read().decode(errors="replace"), self.key)))
                accepted = True                         # only an accepted call counts as use
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
            raise RuntimeError(_refused("Claude", e.status_code, _scrub(str(e.message), self.key))) from None
        except anthropic.APIConnectionError:
            accepted = False
            raise RuntimeError("couldn't reach Anthropic (network)") from None
        finally:
            if accepted:
                _record(self.cc, self.p.name, tin or sum(len(m["content"]) for m in msgs) // 4,
                    tout or sum(map(len, out)) // 4)
        return "".join(out)
