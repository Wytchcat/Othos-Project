"""Score tool choice against training/eval_set.jsonl.

  .venv/Scripts/python.exe training/score.py                 # the code's own guards (offline, instant)
  .venv/Scripts/python.exe training/score.py --model NAME    # a local Ollama model picks the tools (slow on the laptop)

The code mode is a regression test for _tool_names_for. The model mode is the baseline for deciding whether
fine-tuning is worth doing at all (see training/README.md): if the stock model already scores well, don't train.
Nothing here is sent anywhere except to your own Ollama on localhost.
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TOOLS = {
    "web_search": "look something up on the web or the free sources (facts, news, places, weather, topics)",
    "more_results": "read the next results from the last search",
    "save_note": "save a note for the user",
    "search_notes": "look through the user's saved notes",
    "look": "use the camera",
}


def load(path: Path):
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def by_code(row) -> set[str]:
    import agent  # imported late: it needs the LiveKit keys set, even if dummy
    return set(agent._tool_names_for(row["say"], bool(row.get("has_results"))))


def by_model(row, model: str) -> set[str]:
    import httpx
    menu = "\n".join(f"- {k}: {v}" for k, v in TOOLS.items() if k != "look")
    prompt = (f"You are a voice assistant. Tools you may use:\n{menu}\n"
              f"{'Earlier search results exist.' if row.get('has_results') else 'No search results yet.'}\n"
              f"The user said: {row['say']!r}\n"
              'Reply with JSON only: {"tools": [names]}. Use [] if the user just wants to chat or the answer is already known.')
    r = httpx.post("http://127.0.0.1:11434/api/chat", timeout=300, json={
        "model": model, "stream": False, "format": "json", "think": False,
        "messages": [{"role": "user", "content": prompt}], "options": {"temperature": 0}})
    r.raise_for_status()
    try:
        got = json.loads(r.json()["message"]["content"]).get("tools", [])
    except (ValueError, KeyError, AttributeError):
        got = ["<unparsable>"]
    return {str(t) for t in got}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="Ollama model name; omit to score the code's own guards")
    ap.add_argument("--set", default=str(ROOT / "training" / "eval_set.jsonl"))
    args = ap.parse_args()
    os.environ.setdefault("LIVEKIT_API_KEY", "k")
    os.environ.setdefault("LIVEKIT_API_SECRET", "s")
    rows = [r for r in load(Path(args.set)) if not r.get("camera_only") and not (r.get("code_only") and not args.model)]
    wrong, right = [], 0
    for r in rows:
        want = set(r["tools"])
        got = by_model(r, args.model) if args.model else by_code(r)
        if got == want:
            right += 1
        else:
            wrong.append((r["id"], r["say"], sorted(want), sorted(got)))
    print(f"{'model ' + args.model if args.model else 'code guards'}: {right}/{len(rows)} correct")
    for i, say, want, got in wrong:
        print(f"  {i}: {say!r} wanted {want} got {got}")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
