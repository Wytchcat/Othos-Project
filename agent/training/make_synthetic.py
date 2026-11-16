"""Write training/synthetic.jsonl: invented examples of which tool fits what a person says.

Nothing here comes from call logs, profile.md or anyone's real speech. Phrases are built from templates
and invented topics. Any phrase that also appears in eval_set.jsonl is dropped, so the test set stays unseen.

  .venv/Scripts/python.exe training/make_synthetic.py
"""
import itertools
import json
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
random.seed(7)  # same file every run

THINGS = ["butcher", "bakery", "hardware store", "vet", "pharmacy", "tailor", "bike shop", "florist", "tire shop",
          "barber", "plumber", "fabric store"]
FACTS = ["how far the moon is", "who wrote Dracula", "when the Berlin Wall fell", "how many bones a human has",
         "what the boiling point of water is at altitude", "who painted the Mona Lisa", "how long a marathon is"]
TOPICS = ["recipes", "gardening", "books", "movies"]
CHAT = ["Hi there", "Good evening", "How was your day?", "Thank you", "That's funny", "Okay, got it", "Never mind",
        "Say something nice", "I'm tired today"]
NOTES = ["Save a note that the car needs oil", "Make a note: water the ferns", "Remember that the meeting moved to noon",
         "What did I note about the car?", "Read me my notes about the ferns"]

rows = []


def add(say, tools, has_results=False):
    rows.append({"say": say, "tools": tools, "has_results": has_results})


for t, frame in itertools.product(THINGS, ["Find me a {} near me", "Is there a {} nearby?", "Where's the closest {}?",
                                           "Can you recommend a {} in my area?", "Any {}s around here?"]):
    add(frame.format(t), ["web_search"])
for f, frame in itertools.product(FACTS, ["Do you know {}?", "I'd like to know {}", "Tell me {}", "Look up {}"]):
    add(frame.format(f), ["web_search"])
for t, frame in itertools.product(TOPICS, ["What's new in {}?"]):
    add(frame.format(t), ["web_search"])
for c in CHAT:
    add(c, [])
for n in NOTES:
    add(n, ["save_note", "search_notes"])
for s in ["More please", "Keep going", "Read the next one"]:
    add(s, ["more_results", "web_search"], has_results=True)
for s in ["What was the second one again?", "Say that again"]:
    add(s, [], has_results=True)

held_out = {json.loads(l)["say"].lower() for l in (HERE / "eval_set.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()}
rows = [r for r in rows if r["say"].lower() not in held_out]
random.shuffle(rows)
out = HERE / "synthetic.jsonl"
out.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
print(f"wrote {len(rows)} examples to {out.name}")
