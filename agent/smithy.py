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
            "followup": lambda: {"parent_id": rest[0], "question": " ".join(rest[1:])}}[kind]()


PROFILE = worker_engine.Profile(
    queue=SMITHY, config_path=HERE / "smithy.json", registry=REGISTRY, log_name="smithy.log",
    script="smithy.py", tiers=("code",),
    add_usage=("usage: smithy.py add review PATH | fix PATH WHAT | script WHAT | build WHAT | "
               "joomla WHAT [--file PATH] | followup TASK_ID QUESTION"),
    add_parse=_add)

if __name__ == "__main__":
    worker_engine.main(PROFILE, sys.argv)
