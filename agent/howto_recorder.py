"""The how-to recorder: you narrate what you're doing, Othos writes it down as steps,
with a picture of the window you're working in, and stays quiet while you work.

    "Othos, start a how-to: adding a ZOO item"     starts recording ("Recording.")
    (anything you say)                             one step + a screenshot, no reply
    "scratch that"                                 removes the last step (and its picture)
    "note: the Save button is top left"            a tip on the last step
    "take a picture" / "snap"                      another picture for the last step
    "no picture" / "skip the picture"              removes the last step's pictures
    "pause the how-to" / "resume the how-to"       while paused Othos talks normally
    "how many steps?"                              how far you've got
    "finish the how-to"                            saves it (and queues the write-up, if installed)
    "write up the how-to"                          write up the last one later

Files (git-ignored, on this computer only): drafts\\howto\\<date>-<name>\\ with
steps.json, step-01.png..., raw.md (what you said), and after the write-up
guide.md and guide.html. Nothing is sent anywhere. The pictures never go to a model:
the write-up is from the words only.

Privacy, in code: only the window in front is captured, never the whole screen; no
picture when that window belongs to a chat or password program (PRIVATE_APPS) or its
title mentions a password. Say "no picture" to drop one. Don't read passwords aloud
(say "type the password"); key-like strings are removed before a model sees the text.
A recording survives an agent restart (state\\howto-recording.json).
"""
from __future__ import annotations

import ctypes
import json
import logging
import re
import time
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE / "drafts" / "howto"
STATE = HERE / "state" / "howto-recording.json"

log = logging.getLogger("othos")

# no picture while one of these is the window in front
PRIVATE_APPS = {"signal.exe", "discord.exe", "telegram.exe", "whatsapp.exe", "keepass.exe", "keepassxc.exe",
                "bitwarden.exe", "1password.exe", "lastpass.exe", "dashlane.exe", "lockapp.exe",
                "credentialuibroker.exe", "consent.exe"}
_PRIVATE_TITLE = re.compile(r"password|passwort|credential|\bsign[\s-]?in\b|\b2fa\b|authenticator", re.I)

_HOWTO = r"how[\s-]*to"
_LEAD = r"^\W*(?:(?:othos|osos|okay|ok|oops|um|uh|and|so)\W+)*"
START = re.compile(rf"\b(?:start|begin|record|make|do)\s+(?:a\s+|another\s+|recording\s+(?:a\s+)?)?(?:new\s+)?{_HOWTO}\b"
                   rf"[\s,:;.\-]*(?:(?:for|on|about|called|named)\s+)?(?P<title>.*)", re.I | re.S)
FINISH = re.compile(rf"{_LEAD}(?:finish|stop|end|close|wrap\s+up|done\s+with)\s+(?:the\s+|this\s+|my\s+)?"
                    rf"(?:{_HOWTO}|recording)\b|{_LEAD}that'?s\s+the\s+end\s+of\s+the\s+{_HOWTO}\b", re.I)
SCRATCH = re.compile(rf"{_LEAD}(?:scratch|delete|undo|strike|remove)\s+that\b", re.I)
NOTE = re.compile(rf"{_LEAD}(?:note|tip)\s*[:,.\-]?\s+(?P<n>\S.*)", re.I | re.S)
PAUSE = re.compile(rf"{_LEAD}pause\s+(?:the\s+|this\s+)?(?:{_HOWTO}|recording)\b", re.I)
RESUME = re.compile(rf"{_LEAD}(?:resume|unpause|continue|carry\s+on(?:\s+with)?)\s+(?:the\s+|this\s+)?(?:{_HOWTO}|recording)\b", re.I)
SHOT = re.compile(rf"{_LEAD}(?:take\s+(?:a|another|one\s+more)\s+(?:picture|screenshot|snapshot|shot)|snap(?:\s+(?:it|that))?|"
                  rf"(?:another\s+)?picture(?:\s+please)?)\W*$", re.I)
NO_SHOT = re.compile(rf"{_LEAD}(?:no|skip|delete|remove|drop)\s+(?:the\s+|that\s+)?(?:picture|screenshot|snapshot)s?\W*$", re.I)
COUNT = re.compile(rf"{_LEAD}how\s+many\s+steps\b", re.I)
WRITE_UP = re.compile(rf"\bwrite\s+up\s+(?:the\s+|my\s+|that\s+)?(?:last\s+)?{_HOWTO}\b", re.I)
_FILLER = re.compile(r"^\W*(?:(?:um+|uh+|er+|hmm+|okay|ok|so|and|right|alright|yeah|yes)\W*)*$", re.I)


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40].strip("-") or "how-to"


# ---------------------------------------------------------------- the picture
def _foreground() -> tuple[int, str, str]:
    """(window handle, program file name, title) of the window in front."""
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ""
    try:
        import psutil
        exe = psutil.Process(pid.value).name().lower()
    except Exception:
        pass
    return hwnd, exe, buf.value


def _bounds(hwnd: int) -> tuple[int, int, int, int] | None:
    """The window's edges in screen pixels (without the invisible resize border)."""
    rect = wintypes.RECT()
    try:
        if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) == 0:
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return rect.left, rect.top, rect.right, rect.bottom
    return None


def screenshot(path: Path) -> str:
    """Save the window in front to path. '' when saved, else why not (said aloud only if asked)."""
    user32 = ctypes.windll.user32
    old = None
    try:
        # real pixels, not scaled ones, for this thread only (the window edges and the picture agree)
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        old = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    try:
        hwnd, exe, title = _foreground()
        if not hwnd or user32.IsIconic(hwnd):
            return "no window in front"
        if exe in PRIVATE_APPS or _PRIVATE_TITLE.search(title):
            return "a private window was in front"
        box = _bounds(hwnd)
        if not box or box[2] - box[0] < 50 or box[3] - box[1] < 50:
            return "the window in front was too small"
        from PIL import ImageGrab
        img = ImageGrab.grab(bbox=box, all_screens=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path, optimize=True)
        return ""
    except Exception as e:
        log.warning("how-to screenshot failed: %s", e)
        return "the picture didn't work"
    finally:
        if old:
            try:
                user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old))
            except Exception:
                pass


# ---------------------------------------------------------------- the recording
class Recorder:
    def __init__(self):
        self.folder: Path | None = None
        self.title = ""
        self.steps: list[dict] = []
        self.paused = False
        self._resume()

    # -- saved as it goes, so nothing is lost if the agent stops
    def _resume(self) -> None:
        try:
            s = json.loads(STATE.read_text(encoding="utf-8"))
            folder = Path(s["folder"])
            if folder.is_dir():
                self.folder, self.paused = folder, bool(s.get("paused"))
                data = json.loads((folder / "steps.json").read_text(encoding="utf-8"))
                self.title, self.steps = data.get("title", ""), data.get("steps", [])
        except Exception:
            pass

    def _save(self) -> None:
        if self.folder is None:
            return
        (self.folder / "steps.json").write_text(json.dumps(
            {"title": self.title, "started": self.folder.name[:16], "steps": self.steps}, indent=2) + "\n", encoding="utf-8")
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"folder": str(self.folder), "paused": self.paused}) + "\n", encoding="utf-8")

    @property
    def recording(self) -> bool:
        return self.folder is not None

    def start(self, title: str) -> str:
        title = re.sub(r"^\W+|[\s.!?]+$", "", title).strip()
        self.title = title[:1].upper() + title[1:] if title else "How-to"
        self.folder = ROOT / f"{time.strftime('%Y-%m-%d-%H%M')}-{slug(self.title)}"
        n = 2
        while self.folder.exists():
            self.folder = self.folder.with_name(f"{self.folder.name}-{n}")
            n += 1
        self.folder.mkdir(parents=True)
        self.steps, self.paused = [], False
        self._save()
        log.info("how-to recording started: %s", self.folder)
        return "Recording. Go ahead; I'll stay quiet until you say finish the how-to."

    def _shot(self, step: dict) -> str:
        name = f"step-{len(self.steps):02d}" + (f"-{len(step['pictures']) + 1}" if step["pictures"] else "") + ".png"
        why = screenshot(self.folder / name)
        if not why:
            step["pictures"].append(name)
        return why

    def add(self, text: str) -> None:
        text = re.sub(r"^\W*(?:othos|osos)\W+", "", text, flags=re.I).strip()    # "Othos, click New" -> "click New"
        step = {"said": text, "notes": [], "pictures": [], "at": time.strftime("%H:%M:%S")}
        self.steps.append(step)
        self._shot(step)
        self._save()

    def _drop_pictures(self, step: dict) -> None:
        for name in step["pictures"]:
            try:
                (self.folder / name).unlink()
            except OSError:
                pass
        step["pictures"] = []

    def finish(self) -> Path:
        folder = self.folder
        lines = [f"# {self.title}", "", f"What was said, step by step ({len(self.steps)} steps). "
                 "The write-up goes in guide.md.", ""]
        for i, s in enumerate(self.steps, 1):
            lines.append(f"{i}. {s['said']}")
            lines += [f"   - Note: {n}" for n in s["notes"]]
            lines += [f"   - ![step {i}]({p})" for p in s["pictures"]]
        (folder / "raw.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._save()
        STATE.unlink(missing_ok=True)
        self.folder, self.steps, self.title, self.paused = None, [], "", False
        log.info("how-to recording finished: %s", folder)
        return folder

    # -- one turn while recording: what to say ('' = nothing), or None = not for the recorder
    def handle(self, text: str) -> str | None:
        if not self.recording:
            return None
        if FINISH.search(text):
            return ""                      # the caller finishes it (it may queue a write-up)
        if self.paused:
            if RESUME.search(text):
                self.paused = False
                self._save()
                return "Recording again."
            return None                    # paused: Othos talks normally
        if PAUSE.search(text):
            self.paused = True
            self._save()
            return "Paused. Say resume the how-to when you're ready."
        if COUNT.search(text):
            n = len(self.steps)
            return f"{n} step{'s' if n != 1 else ''} so far."
        if SCRATCH.search(text):
            if not self.steps:
                return "There's no step to scratch yet."
            self._drop_pictures(self.steps.pop())
            self._save()
            return "Scratched."
        if m := NOTE.match(text):
            if not self.steps:
                self.add(m.group("n"))     # a note before any step becomes the first step
                return ""
            self.steps[-1]["notes"].append(m.group("n").strip())
            self._save()
            return "Noted."
        if SHOT.match(text):
            if not self.steps:
                return "Say the first step, then I'll add the picture to it."
            why = self._shot(self.steps[-1])
            self._save()
            return f"No picture: {why}." if why else "Got it."
        if NO_SHOT.match(text):
            if self.steps:
                self._drop_pictures(self.steps[-1])
                self._save()
            return "Picture removed."
        if _FILLER.match(text):
            return ""
        self.add(text)
        return ""


def latest_unwritten() -> Path | None:
    """The newest finished how-to without a guide yet."""
    if not ROOT.is_dir():
        return None
    for d in sorted((p for p in ROOT.iterdir() if p.is_dir()), reverse=True):
        if (d / "raw.md").exists() and not (d / "guide.md").exists():
            return d
    return None
