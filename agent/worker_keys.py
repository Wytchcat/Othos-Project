"""API keys for the workers' model providers, encrypted with Windows' own protection (DPAPI).

Saved outside the Othos folder, in %LOCALAPPDATA%\\Othos\\keys.dat, so they never go into
git or the shared copy, and only this Windows account on this PC can decrypt them. A key
can also sit in .env (the advanced way); a saved key wins over .env.

Keys come in on stdin (`set-key NAME`), never on a command line where other programs can
see them, and are never printed or logged: only the last 4 characters are ever shown.
No packages needed (ctypes + crypt32).
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
from pathlib import Path

STORE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Othos" / "keys.dat"
_ENTROPY = b"Othos worker provider keys v1"        # ties the data to this program as well


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _Blob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _call(fn, data: bytes) -> bytes:
    out, ent = _Blob(), _blob(_ENTROPY)
    if not fn(ctypes.byref(_blob(data)), None, ctypes.byref(ent), None, None, 0x01, ctypes.byref(out)):  # UI_FORBIDDEN
        raise OSError("Windows couldn't protect or unprotect the key")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _protect(text: str) -> str:
    c = ctypes.windll.crypt32
    return base64.b64encode(_call(lambda *a: c.CryptProtectData(a[0], "Othos", *a[2:]), text.encode("utf-8"))).decode()


def _unprotect(b64: str) -> str:
    return _call(ctypes.windll.crypt32.CryptUnprotectData, base64.b64decode(b64)).decode("utf-8")


def _load() -> dict:
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    tmp.replace(STORE)


def saved() -> dict[str, str]:
    """{KEY_NAME: key} for every saved key this account can decrypt."""
    out = {}
    for name, b64 in _load().items():
        try:
            out[name] = _unprotect(b64)
        except Exception:
            pass                                  # another account's or a damaged entry: skipped
    return out


def set_key(name: str, value: str) -> None:
    value = value.strip().strip('"').strip("'")
    if not name or not value or any(c.isspace() for c in value) or len(value) > 500:
        raise ValueError("that doesn't look like a key (no spaces; paste it exactly)")
    d = _load()
    d[name] = _protect(value)
    _save(d)


def del_key(name: str) -> bool:
    d = _load()
    if name in d:
        del d[name]
        _save(d)
        return True
    return False


def last4(value: str) -> str:
    return "•" * 8 + value[-4:] if value else ""
