# Othos Circuit Board visualizer

The call-screen visualizer: "The Circuit Board" face from
[ai-visualizer](https://github.com/jaredrhod/ai-visualizer) by Jared Rhodenizer,
copied at upstream commit `6921e1d` (2026-08-30).

**Credit:** the Circuit Board graphics are by Jared Rhodenizer,
[jaredrhod.com](https://jaredrhod.com).

**License:** AGPL-3.0-or-later (see `LICENSE` in this folder). These files remain
under that license, and so does Othos-ui as a whole now that it includes them: if
Othos-ui is ever shared with someone, or run as a service other people use, its
source must be offered under the AGPL too. Private use on your own machine is fine.

## Files

- `board.html`: the board face (upstream `faces/board/index.html`)
- `core.js`: the shared plumbing every face uses (upstream `core.js`)
- `LICENSE`: the upstream AGPL-3.0 license text

## Othos changes (reapply if you ever pull a newer upstream copy)

All are marked with `Othos` comments in the code.

- **Embed mode** (`core.js`, `?embed=1`): no polling of a Python server. The Othos page
  (`components/app/othos-board.tsx`) sends the agent state and voice waveform with
  `postMessage`. The thinking sound and microphone are off.
- **Frame-rate cap** (`core.js`): 30 fps while listening or speaking, 10 fps while
  thinking (the model is reading and writing then), 8 fps while idle (`?maxfps=`,
  `?thinkfps=`, `?idlefps=`). Upstream redraws at the display refresh rate.
- **Off switch** (Othos page): `http://127.0.0.1:3000/?board=off` runs a call without
  the board, for comparing reply delay.
- **Resolution** (`core.js` + `board.html`): `?res=0.5` draws at half the real screen
  pixels. Upstream capped sharpness at 1.25x, which looked blurry on 4K.
- **Less per-frame work** (`board.html`): the clock and demo text only touch the page
  when they change, and the CSS spinner turns in 12 steps instead of at 60 fps.
- `?lite=1` (`board.html`) skips the grain, sheen and chip-text glow (measured: no
  real gain, so it is off by default). `?hud=1` shows the frame rate.
- `board.html` loads `core.js` from this folder instead of `../../core.js`.
- **Name without dots** (`core.js`): Othos isn't an acronym, so the Othos page passes
  `?dots=space`, which shows "O T H O S" instead of upstream's dotted "O.T.H.O.S.". The
  spaces keep the thinking scramble (it churns every character) almost as long as upstream's.
  `?dots=0` shows a plain "OTHOS".
- **The CPU workers' chips** (`board.html`, `core.js`): two background ICs stand for
  Othos's CPU workers, picked largest-first among the ICs that a trace from the main
  chip runs to (then the largest other ICs, with a dashed link drawn), so they're the
  same chips each time. The Othos page reads `/api/worker` every 3 s and passes
  `workers: {firestarter, smithy}` in its `othos-av` message:
  - **Firestarter** (site work): `working` = ember glow and ember signals out to it,
    FIRESTARTER printed on the chip; `ready` = the board's magenta, with READY on a second
    line and signals back to the main chip;
  - **The Smithy** (coding): `working` = blue, green and cyan in turn, THE SMITHY
    printed on the chip; `ready` = the board's purple, with READY on a second line;
  - `idle` / `off`: they fade back to normal chips.

Measured against llama3.2:3b on the i7-1355U / Iris Xe laptop, Opera at about
3456x1944, half resolution: at 30 fps the board cost about 19% of prompt-reading speed
and 25% of writing speed; at 10 fps (now the thinking rate) about 4% and 3%.
