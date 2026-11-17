# Othos Setup (optional windowed installer)

`OthosSetup.exe` is a small Windows window that does what `setup.ps1` does, step by step:
it checks that Python 3.12, Node.js and Ollama are installed (listing every missing one at
once, with a link each), then runs `setup.ps1` and shows the steps and the log.

You don't need it: `setup.ps1` in the Othos folder does the same from PowerShell.

## Build it

Nothing to install: it compiles with the C# compiler that comes with Windows.

```powershell
powershell -ExecutionPolicy Bypass -File installer\build-installer.ps1
```

This makes `installer\OthosSetup.exe`. **Move it into the Othos folder, next to `agent` and
`ui`** (it looks for both), then open it.

## Notes

- The window can be resized, and every message box uses the same colours as the window.
- It never uses the internet itself; the downloads are done by `setup.ps1`.
- If a step stops, the box says which step and shows the error line. Install what it names,
  then press **Try Again**.
- After a good install it asks one optional question, "Connect your Google account?", with
  the benefits and the risks. **Not now** is the default, and Connect is off until Othos's
  Connect Google window is built. It makes no network call; it only saves your answer in
  `agent\state\google-prompt.json` (answer, don't-ask-again, date).
- The source is `OthosSetup.cs` (C# 5, one file).
