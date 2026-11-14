"""The Smithy, the Othos coding worker: reviews, fixes, scripts, Joomla overrides
and builds from scratch, from its own queue, on the CPU only.

Runs independently of voice Othos and of Firestarter (started by The Smithy app);
the two take turns on the CPU when both have work. Settings: smithy.json. Tasks:
smithy_jobs.py. Queue: state\\smithy.db. Results go to drafts\\smithy\\ (a build
gets its own folder there) and wait for the user's review; nothing is applied, run
or installed. Work log: workers\\Smithy.md. Engine (shared with Firestarter):
worker_engine.py.

    smithy.py run                          the worker itself (the app starts this)
    smithy.py add review PATH
    smithy.py add fix PATH WHAT
    smithy.py add script WHAT
    smithy.py add build WHAT               a project from scratch
    smithy.py add joomla WHAT [--file PATH]
    smithy.py add followup ID QUESTION     (on a build: a new version)
    smithy.py add ... --provider NAME      on another model provider (worker_cloud.py; off by default)
    smithy.py cloud | set-provider NAME|local
    smithy.py list | status | types | log
    smithy.py cancel [ID]
    smithy.py models
    smithy.py set-model code NAME
"""
from __future__ import annotations

import sys
from pathlib import Path

import worker_engine
from smithy_jobs import REGISTRY
from worker_queue import SMITHY

HERE = Path(__file__).parent


def _add(kind: str, rest: list[str]) -> dict:
    path = ""
    if "--file" in rest:
        i = rest.index("--file")
        path, rest = rest[i + 1], rest[:i] + rest[i + 2:]
    return {"review": lambda: {"path": rest[0]},
            "fix": lambda: {"path": rest[0], "request": " ".join(rest[1:])},
            "script": lambda: {"request": " ".join(rest)},
            "build": lambda: {"request": " ".join(rest), "path": path},
            "joomla": lambda: {"request": " ".join(rest), "path": path},
            "followup": lambda: {"parent_id": rest[0], "question": " ".join(rest[1:])},
            "check": lambda: {"path": " ".join(rest)}}[kind]()


PROFILE = worker_engine.Profile(
    queue=SMITHY, config_path=HERE / "smithy.json", registry=REGISTRY, log_name="smithy.log",
    script="smithy.py", tiers=("code",),
    add_usage=("usage: smithy.py add review PATH | fix PATH WHAT | script WHAT | build WHAT | "
               "joomla WHAT [--file PATH] | followup TASK_ID QUESTION"
               + (" | check PROGRAM.exe" if "check" in REGISTRY else "")      # not in the shared copy
               + "   (any: --provider NAME [--model NAME])"),
    add_parse=_add)

def _cloud(argv: list[str]) -> bool:
    """The window's Provider row: `cloud` (JSON: what's turned on, the default, today's use)
    and `set-provider NAME|local`. Turning a provider ON stays in smithy.json + .env."""
    import json
    import worker_cloud
    import worker_shared
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd not in ("cloud", "set-provider"):
        return False
    path = HERE / "smithy.json"
    cfg = worker_shared.load_json(path)
    cc = cfg.get("cloud") or {}
    if cmd == "cloud":
        ready, why = [], {}
        for name in cc.get("enabled", []):
            p = worker_cloud.PROVIDERS.get(str(name).lower())
            if not p:
                continue
            try:
                worker_cloud.pick(cfg, {"provider": p.name, "no_switch": True}, "review")
                ready.append({"name": p.name, "label": p.spoken + (" (this PC)" if p.local else " (this PC, then cloud)" if p.proxy else " (cloud)")})
            except ValueError as e:
                why[p.name] = str(e)
        order = [str(n).lower() for n in cc.get("switch_order", [])]
        in_order = [r["label"].split(" (")[0] for n in order for r in ready if r["name"] == n]
        if in_order:                                   # one after another as free tokens run out (worker_cloud.Chain)
            ready.insert(0, {"name": "auto", "label": "Switching: " + ", ".join(in_order) + ", then this PC"})
        u = worker_cloud.usage_today()
        tot = {"tokens": sum(v.get("in", 0) + v.get("out", 0) for v in u["providers"].values()),
               "usd": round(sum(v.get("usd", 0.0) for v in u["providers"].values()), 4),
               "calls": sum(v.get("calls", 0) for v in u["providers"].values())}
        print(json.dumps({"ready": ready, "not_ready": why, "default": cc.get("default", "") or "local",
                          "today": tot, "max_tokens_per_day": cc.get("max_tokens_per_day", 1_000_000),
                          "max_usd_per_day": cc.get("max_usd_per_day")}))
        return True
    name = (argv[2] if len(argv) > 2 else "").lower()
    if name not in ("", "local"):
        try:
            worker_cloud.pick(cfg, {"provider": name}, "review")      # every safeguard, before saving
        except ValueError as e:
            raise SystemExit(f"can't use that: {e}")
    cfg.setdefault("cloud", {})["default"] = "" if name in ("", "local") else name
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    said = 'the local model' if name in ('', 'local') else 'switching (the switch order, then the local model)' if name == 'auto' else name
    print(f"tasks now use {said} (from the next task)")
    return True


if __name__ == "__main__":
    if not _cloud(sys.argv):
        worker_engine.main(PROFILE, sys.argv)
