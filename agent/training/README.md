# Training groundwork (nothing here trains yet)

Fine-tuning needs the new build (a 16 GB GPU). These files are the part that can be done now, and they are useful
without any training: `score.py` also tests the code's own tool-choice guards.

| File | What it does |
|---|---|
| `eval_set.jsonl` | Held-out test: what a person says, and which tools should be offered (empty = just talk). |
| `score.py` | Scores the code guards (offline), or a local Ollama model with `--model NAME`. |
| `make_synthetic.py` | Writes `synthetic.jsonl`: invented examples for training. No call logs, no profile, no real speech. |

Check the code guards: `.venv/Scripts/python.exe training/score.py`

## Rules
- Training data is invented or hand-written. Call logs hold speech and other people's voices: never used without an explicit opt-in, redacted, and kept on this PC.
- The test set never overlaps the training set (`make_synthetic.py` drops any phrase that is in `eval_set.jsonl`).
- Baseline first: score the stock model. If it already does well, don't train.
- Public copy: ship these scripts and the test set, never weights or personal data.

## Plan (new build)
1. Baseline: `score.py --model <stock model>`.
2. Install CUDA drivers and Unsloth or Axolotl in a separate venv (ask first).
3. QLoRA on the 9B voice model, thinking off; merge, convert to GGUF, register in Ollama as `othos-voice`.
4. Score the same test set, then listen in real calls. Keep the stock model as the fallback.
5. Train only with gaming mode off (it shares the 16 GB of VRAM).

## Never train on these
- **Stack Overflow / Stack Exchange dumps.** Their download terms allow Stack Overflow to deny access if the data is used to train an LLM. Othos searches them (retrieval) and cites them; it never uses them as training data.
