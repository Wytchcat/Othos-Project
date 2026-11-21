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
    smithy.py add build WHAT               a project from scratch: first a plan for your OK
                                           (questions, assumptions, files); --no-ask builds straight away
    smithy.py reply ID yes|no|ANSWERS      answer a plan: yes builds it, no drops it, anything else
                                           is answers or changes and gets a revised plan
    smithy.py plans | plan ID              plans waiting for an answer (JSON, for the app)
    smithy.py continue ID [--force]        a stopped or failed build carries on from the files it wrote
                                           (refused after 25 kept fixes, or 4 test runs in a row, with no better
                                           result; --force overrides)
    smithy.py replay ID [RUN]              The Smithy's own decisions on a task's last test run, no model,
                                           nothing run: where each fix would go, and what would hold it back
    smithy.py decide ID TEST test|code|both  an override only: The Smithy judges a test that keeps failing
                                           itself; this replaces its judgment ("test" = the test is wrong,
                                           "code" = the code is wrong, "both" = each is)
    smithy.py add joomla WHAT [--file PATH]
    smithy.py add followup ID QUESTION     (on a build: a new version)
    smithy.py add ... --provider NAME      on another model provider (worker_cloud.py; off by default)
    smithy.py cloud | set-provider NAME|local
    smithy.py list | status | types | log
    smithy.py tidy [--discard]             what is waiting, replaced by a later kept version, or stuck;
                                           --discard drops the replaced ones (only when you say so)
    smithy.py cancel [ID]
    smithy.py models
    smithy.py set-model code NAME
"""
from __future__ import annotations

import sys
from pathlib import Path

import smithy_jobs
import worker_engine
from smithy_jobs import REGISTRY
from worker_queue import SMITHY

HERE = Path(__file__).parent


def _add(kind: str, rest: list[str]) -> dict:
    path = ""
    no_ask = "--no-ask" in rest                          # a build without the plan to answer first
    rest = [r for r in rest if r != "--no-ask"]
    if no_ask and kind in ("build", "joomla"):
        return {**_add(kind, rest), "no_ask": True}
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
               + "   (any: --provider NAME [--model NAME]; build/joomla: --no-ask builds without a plan to answer)"
               + "\n       smithy.py reply TASK_ID yes|no|ANSWERS"),
    add_parse=_add,
    on_idle=smithy_jobs.on_idle)        # restart-needed note; a plan nobody answered (build_ask_wait_hours)

def _cloud(argv: list[str]) -> bool:
    """The window's Provider row: `cloud` (JSON: what's turned on, the default, today's use)
    and `set-provider NAME|local`. Turning a provider ON stays in smithy.json + .env."""
    import json
    import worker_cloud
    import worker_shared
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "reply":                                   # her answer to a plan: yes | no | answers / changes
        rest = argv[3:]
        build_now = "--build" in rest                  # the app's "Build with these answers"
        rest = [r for r in rest if r != "--build"]
        if not rest or not argv[2].isdigit():
            raise SystemExit("usage: smithy.py reply TASK_ID [--build] yes|no|ANSWERS")
        text = sys.stdin.read() if rest == ["-"] else " ".join(rest)          # "-": the app sends it on stdin
        try:
            print(smithy_jobs.reply_to_proposal(int(argv[2]), text, build_now=build_now))
        except ValueError as e:
            raise SystemExit(f"can't do that: {e}")
        return True
    if cmd == "result":                                  # a task's result file, for the app's "Open its result"
        row = SMITHY.get(int(argv[2])) if len(argv) > 2 and argv[2].isdigit() else None
        print(row["result_path"] or "" if row is not None else "")
        return True
    if cmd == "tidy":                                    # what is waiting, replaced or stuck; --discard drops the replaced
        if "--discard" in argv[2:]:
            print("Discarded the replaced versions: " + (", ".join(map(str, smithy_jobs.discard_superseded())) or "none"))
        else:
            print(smithy_jobs.tidy_text(smithy_jobs.open_work()))
        return True
    if cmd == "continue":                                # a stopped or failed build carries on where it was
        if len(argv) < 3 or not argv[2].isdigit():
            raise SystemExit("usage: smithy.py continue TASK_ID [--force]")
        try:
            print(smithy_jobs.carry_on(int(argv[2]), force="--force" in argv[3:]))
        except ValueError as e:
            raise SystemExit(f"can't do that: {e}")
        return True
    if cmd == "replay":                                  # The Smithy's own decisions on a task's last test run, no model
        if len(argv) < 3 or not argv[2].isdigit() or (len(argv) > 3 and not argv[3].isdigit()):
            raise SystemExit("usage: smithy.py replay TASK_ID [RUN]")
        import smithy_replay
        try:
            print(smithy_replay.replay(int(argv[2]), int(argv[3]) if len(argv) > 3 else None))
        except ValueError as e:
            raise SystemExit(f"can't do that: {e}")
        return True
    if cmd == "decide":                                  # an override of The Smithy's own judgment of a locked test
        if len(argv) < 5 or not argv[2].isdigit():
            raise SystemExit("usage: smithy.py decide TASK_ID TEST_NAME test|code|both")
        try:
            print(smithy_jobs.decide(int(argv[2]), argv[3], argv[4].lower()))
        except ValueError as e:
            raise SystemExit(f"can't do that: {e}")
        return True
    if cmd == "plan":                                    # a plan waiting for an answer, as JSON (the app)
        if len(argv) < 3 or not argv[2].isdigit():
            raise SystemExit("usage: smithy.py plan TASK_ID")
        prop, row = smithy_jobs.load_proposal(int(argv[2])), SMITHY.get(int(argv[2]))
        print(json.dumps({"id": int(argv[2]), "waiting": bool(prop and row and row["status"] == "review"),
                          "title": row["title"] if row else "", "text": (prop or {}).get("text", ""),
                          "questions": [q if isinstance(q, dict) else {"q": q, "default": ""}
                                        for q in (prop or {}).get("questions", [])],
                          "conflicts": (prop or {}).get("conflicts", []),
                          "limits": (prop or {}).get("limits", []), "cost": (prop or {}).get("cost", "")}))
        return True
    if cmd == "plans":                                   # every plan waiting for an answer (the app)
        out = [{"id": r["id"], "title": r["title"]} for r in SMITHY.by_status("review", limit=50)
               if smithy_jobs.load_proposal(r["id"]) is not None]
        print(json.dumps(out))
        return True
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
