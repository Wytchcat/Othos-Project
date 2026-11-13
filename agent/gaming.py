"""Othos gaming mode: notice a game is running and move Othos out of its way.

Plan: Documents\\Othos_future\\gaming mode handoff.md. Settings: gaming.json.

- Detection is local: running program names (psutil), plus a window title check
  for games that show up as a generic program (Minecraft Java = javaw.exe).
- Each game has a profile. "gpu-heavy" moves the voice model to the CPU-only
  gaming model (othos-gaming) and unloads the normal one; every profile pauses
  the CPU worker (no worker exists yet: the pause is recorded in
  state\\gaming-status.json for it to read).
- Overrides: "gaming mode on/off" by voice, or the Othos window's Gaming button,
  both via state\\gaming-override.txt. An override wins until the game that was
  running when it was set closes (or, with no game, until it's turned off).
- Never switches mid-reply, says "Gaming mode on/off" when it changes, and if the
  gaming model fails it falls back to the normal model instead of breaking the call.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wt
import fnmatch
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import psutil

log = logging.getLogger("othos")

HERE = Path(__file__).parent
CONFIG_PATH = Path(os.getenv("OTHOS_GAMING_CONFIG", HERE / "gaming.json"))
STATE_DIR = HERE / "state"
OVERRIDE_PATH = STATE_DIR / "gaming-override.txt"   # "on", "off" or empty
STATUS_PATH = STATE_DIR / "gaming-status.json"      # what Othos is doing now (for the window / worker)


# ---------------------------------------------------------------- config
@dataclass
class Game:
    name: str
    process: str
    profile: str
    window_title: str = ""


@dataclass
class Config:
    check_seconds: float = 10.0
    normal_model: str = ""
    gaming_model: str = "othos-gaming"
    profiles: dict = field(default_factory=dict)
    override_profile: str = "gpu-heavy"
    games: list[Game] = field(default_factory=list)


def load_config(path: Path = CONFIG_PATH) -> Config:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))  # Notepad++ may save a BOM
    except FileNotFoundError:
        return Config()
    except Exception as e:  # a typo in the file must not stop Othos
        log.warning("gaming.json couldn't be read (%s); gaming mode is off", e)
        return Config()
    games = [Game(g["name"], g["process"], g.get("profile", "gpu-heavy"), g.get("window_title", ""))
             for g in raw.get("games", []) if g.get("enabled", True) and g.get("process")]
    return Config(
        check_seconds=float(raw.get("check_seconds", 10)),
        normal_model=raw.get("normal_model") or os.getenv("OLLAMA_MODEL", "llama3.2:3b"),
        gaming_model=raw.get("gaming_model") or "othos-gaming",
        profiles=raw.get("profiles", {}),
        override_profile=raw.get("override_profile", "gpu-heavy"),
        games=games,
    )


# ---------------------------------------------------------------- detection
def _window_titles_by_pid() -> dict[int, list[str]]:
    """Visible top-level window titles per process id (Windows only)."""
    titles: dict[int, list[str]] = {}
    try:
        user32 = ctypes.windll.user32
    except AttributeError:
        return titles

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def each(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                pid = wt.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                titles.setdefault(pid.value, []).append(buf.value)
        return True

    user32.EnumWindows(each, 0)
    return titles


def detect(cfg: Config) -> Game | None:
    """The first listed game that is running, or None."""
    if not cfg.games:
        return None
    running: dict[str, list[int]] = {}
    for p in psutil.process_iter(["name", "pid"]):
        name = (p.info.get("name") or "").lower()
        if name:
            running.setdefault(name, []).append(p.info["pid"])
    titles = None
    for g in cfg.games:
        pattern = g.process.lower()
        pids = [pid for name, ps in running.items() if fnmatch.fnmatch(name, pattern) for pid in ps]
        if not pids:
            continue
        if not g.window_title:
            return g
        titles = titles if titles is not None else _window_titles_by_pid()
        want = g.window_title.lower()
        if any(want in t.lower() for pid in pids for t in titles.get(pid, [])):
            return g
    return None


# ---------------------------------------------------------------- override file
def read_override() -> str:
    try:
        v = OVERRIDE_PATH.read_text(encoding="utf-8").strip().lower()
    except FileNotFoundError:
        return ""
    return v if v in ("on", "off") else ""


def write_override(value: str) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    OVERRIDE_PATH.write_text(value, encoding="utf-8")


# ---------------------------------------------------------------- the mode itself
@dataclass
class Mode:
    on: bool = False
    profile: str = ""
    game: str = ""
    why: str = ""            # "detected" or "override"
    voice_on_cpu: bool = False
    pause_worker: bool = False
    speech_on_cpu: bool = False

    def describe(self) -> str:
        if not self.on:
            return "Gaming mode is off."
        what = f"for {self.game}" if self.game else "(turned on by hand)"
        where = "the lighter CPU model" if self.voice_on_cpu else "the normal model"
        return f"Gaming mode is on {what}, {self.profile}: I'm using {where}."


class GamingMonitor:
    """Runs for one call. The agent asks .voice_llm_name() at the start of each turn."""

    def __init__(self, session, ollama_root: str, keep_alive: str = "-1m"):
        self.session = session
        self.ollama_root = ollama_root
        self.keep_alive = keep_alive
        self.cfg = load_config()
        self.mode = Mode()
        self.failed_gaming_model = False
        self.cpu_llm = None  # the agent's LLM object for gaming_model (set in entrypoint)
        self._override_game: str | None = None  # game running when the override was set
        self._task: asyncio.Task | None = None

    # -- what the agent uses
    @property
    def use_gaming_model(self) -> bool:
        return self.mode.on and self.mode.voice_on_cpu and not self.failed_gaming_model

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def set_override(self, value: str) -> Mode:
        """'on' / 'off' from the user's voice command; applied straight away (between turns)."""
        write_override(value)
        game = await asyncio.to_thread(detect, self.cfg)
        self._override_game = game.name if game else None
        # no waiting for "between turns": this runs inside the user's turn, and the reply
        # is spoken by code (the model isn't used this turn)
        await self._update(game, announce=False, wait=False)
        return self.mode

    # -- the loop
    async def _loop(self) -> None:
        if not self.cfg.games and not read_override():
            log.info("gaming mode: no games listed in gaming.json")
        first = True
        while True:
            try:
                game = await asyncio.to_thread(detect, self.cfg)
                await self._update(game, announce=not first)
                first = False
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("gaming mode check failed: %s", e)
            await asyncio.sleep(self.cfg.check_seconds)

    def _wanted(self, game: Game | None) -> Mode:
        override = read_override()
        # an override lasts until the game that was running when it was set closes
        if override and self._override_game is not None and (game is None or game.name != self._override_game):
            log.info("gaming mode: %s closed, back to automatic", self._override_game)
            write_override("")
            override, self._override_game = "", None
        if override == "off" and game is None:
            # nothing to keep off: back to automatic, so a game started later is noticed
            write_override("")
            return Mode()
        if override == "off":
            return Mode(on=False, why="override")
        if override == "on" or game:
            name = game.profile if game else self.cfg.override_profile
            p = self.cfg.profiles.get(name, {})
            return Mode(on=True, profile=name, game=game.name if game else "",
                        why="override" if override == "on" else "detected",
                        voice_on_cpu=bool(p.get("voice_on_cpu")), pause_worker=bool(p.get("pause_worker")),
                        speech_on_cpu=bool(p.get("speech_on_cpu")))
        return Mode()

    async def _between_turns(self, max_wait: float = 60.0) -> None:
        """Never switch mid-reply: wait while Othos is thinking or speaking."""
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            state = getattr(self.session, "agent_state", "listening")
            if state not in ("thinking", "speaking"):
                return
            await asyncio.sleep(0.5)

    async def _update(self, game: Game | None, announce: bool, wait: bool = True) -> None:
        want = self._wanted(game)
        same = (want.on, want.profile, want.game, want.voice_on_cpu) == \
               (self.mode.on, self.mode.profile, self.mode.game, self.mode.voice_on_cpu)
        if same:
            self.mode.why = want.why
            return
        if wait:
            await self._between_turns()
        was_cpu = self.use_gaming_model
        self.mode, self.failed_gaming_model = want, False
        log.info("gaming mode: %s", want.describe())
        if want.voice_on_cpu and not was_cpu:
            await self._to_cpu_model()
        elif was_cpu and not want.voice_on_cpu:
            await self._to_normal_model()
        if want.speech_on_cpu:
            log.info("gaming mode: speech is cloud-based on this machine; nothing to move")
        self._write_status()
        if announce:
            try:
                self.session.say("Gaming mode on." if want.on else "Gaming mode off.", allow_interruptions=True)
            except Exception as e:
                log.warning("couldn't announce gaming mode: %s", e)

    # -- Ollama: load / unload
    async def _ollama(self, model: str, keep_alive) -> bool:
        try:
            async with httpx.AsyncClient(timeout=90) as c:
                r = await c.post(f"{self.ollama_root}/api/generate", json={"model": model, "keep_alive": keep_alive})
                return r.status_code == 200
        except Exception as e:
            log.warning("gaming mode: Ollama call for %s failed: %s", model, e)
            return False

    # Unload first, then load: the two are never in memory together, and on the laptop
    # (where othos-gaming is llama3.2:3b with CPU settings, sharing one model file)
    # unloading llama3.2 afterwards also unloaded the gaming model that had just loaded.
    async def _to_cpu_model(self) -> None:
        t = time.monotonic()
        await self._ollama(self.cfg.normal_model, 0)   # free its memory / VRAM for the game
        if not await self._ollama(self.cfg.gaming_model, self.keep_alive):
            # fall back to the normal model rather than breaking the call
            self.failed_gaming_model = True
            log.warning("gaming mode: %s didn't load; back on %s", self.cfg.gaming_model, self.cfg.normal_model)
            await self._ollama(self.cfg.normal_model, self.keep_alive)
            return
        log.info("gaming mode: voice moved to %s in %.1f s", self.cfg.gaming_model, time.monotonic() - t)

    async def _to_normal_model(self) -> None:
        t = time.monotonic()
        await self._ollama(self.cfg.gaming_model, 0)
        await self._ollama(self.cfg.normal_model, self.keep_alive)
        log.info("gaming mode: voice back on %s in %.1f s", self.cfg.normal_model, time.monotonic() - t)

    def _write_status(self) -> None:
        try:
            STATE_DIR.mkdir(exist_ok=True)
            m = self.mode
            STATUS_PATH.write_text(json.dumps({
                "on": m.on, "profile": m.profile, "game": m.game, "why": m.why,
                "voice_model": self.cfg.gaming_model if self.use_gaming_model else self.cfg.normal_model,
                "pause_worker": m.pause_worker, "since": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, indent=2), encoding="utf-8")
        except Exception as e:
            log.warning("couldn't write gaming status: %s", e)
