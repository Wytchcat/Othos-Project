# Othos setup for Windows. Run once from the Othos-agent (or agent) folder, or with the
# top-level setup.ps1 of the shared copy:
#   powershell -ExecutionPolicy Bypass -File setup.ps1
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -Mode offline
# Modes:
#   mixed    (default) cloud speech (Deepgram + Rime), everything else local.
#            Works on a CPU-only laptop with 16 GB of RAM.
#   offline  local speech too (needs a graphics card; see README "Fully offline").
# Safe to run again: it skips what's already done and never overwrites your
# .env, personal.json or profile.md.
param([ValidateSet("mixed", "offline")][string]$Mode = "", [switch]$NoModels)

$ErrorActionPreference = "Stop"
$A  = $PSScriptRoot
$Parent = Split-Path $A -Parent
$Ui = if ($env:OTHOS_UI_DIR) { $env:OTHOS_UI_DIR } elseif (Test-Path (Join-Path $Parent "Othos-ui\package.json")) { Join-Path $Parent "Othos-ui" } else { Join-Path $Parent "ui" }  # Othos-ui, or ui in the shared copy
$Lk = if ($env:OTHOS_LIVEKIT_DIR) { $env:OTHOS_LIVEKIT_DIR } else { Join-Path $Parent "livekit_server" }

function Step($t) { Write-Host ""; Write-Host "== $t" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "   ok: $t" -ForegroundColor Green }
function Warn($t) { Write-Host "   !!  $t" -ForegroundColor Yellow }
function Stop-Setup($t) { Write-Host "   STOP: $t" -ForegroundColor Red; exit 1 }

if (-not $Mode) {
    Write-Host "Which setup?"
    Write-Host "  1  mixed   - cloud speech (Deepgram + Rime keys), local models. Any 16 GB PC."
    Write-Host "  2  offline - local speech too. Needs a graphics card (see README)."
    $Mode = if ((Read-Host "Type 1 or 2 [1]") -eq "2") { "offline" } else { "mixed" }
}
Write-Host "Setting up Othos ($Mode) in $A"

# ---- 1. tools
Step "Checking tools"
$py = $null
try { $v = & py -3.12 -c "import sys; print(sys.version.split()[0])" 2>$null; if ($LASTEXITCODE -eq 0) { $py = "3.12"; Ok "Python $v" } } catch {}
if (-not $py) {
    Stop-Setup ("Python 3.12 not found. Install it from https://www.python.org/downloads/ (3.12.x, not 3.13+: " +
                "livekit-agents 1.8 needs 3.12), tick 'py launcher', then run setup again.")
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Stop-Setup "Node.js not found. Install the LTS from https://nodejs.org/, then run setup again." }
Ok "Node $(node --version)"
if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
    Warn "pnpm not found; installing it with npm"
    npm install -g pnpm | Out-Null
}
Ok "pnpm $(pnpm --version)"
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) { Stop-Setup "Ollama not found. Install it from https://ollama.com/download, then run setup again." }
Ok "Ollama $((ollama --version) -replace '.*version is ','')"
if (-not (Test-Path (Join-Path $Ui "package.json"))) { Stop-Setup "Othos-ui not found at $Ui. Put the ui (Othos-ui) folder next to this one (or set OTHOS_UI_DIR)." }
Ok "Othos-ui at $Ui"

# ---- 2. LiveKit server (the local call server)
Step "LiveKit server"
if (Test-Path (Join-Path $Lk "livekit-server.exe")) { Ok "already in $Lk" }
else {
    $rel = Invoke-RestMethod "https://api.github.com/repos/livekit/livekit/releases/latest" -Headers @{ "User-Agent" = "othos-setup" }
    $asset = $rel.assets | Where-Object { $_.name -match "windows_amd64\.zip$" } | Select-Object -First 1
    if (-not $asset) { Stop-Setup "No Windows download in LiveKit's latest release. Get it from https://github.com/livekit/livekit/releases and unzip it to $Lk." }
    New-Item -ItemType Directory -Force $Lk | Out-Null
    $zip = Join-Path $env:TEMP $asset.name
    Write-Host "   downloading $($asset.name) ($([math]::Round($asset.size / 1MB)) MB) from GitHub"
    Invoke-WebRequest $asset.browser_download_url -OutFile $zip
    Expand-Archive $zip -DestinationPath $Lk -Force
    Remove-Item $zip
    Ok "LiveKit server $($rel.tag_name) in $Lk"
}

# ---- 3. Python environment (pinned requirements.txt)
Step "Python environment (.venv)"
$venvPy = Join-Path $A ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { & py -3.12 -m venv (Join-Path $A ".venv") }
& $venvPy -m pip install --upgrade pip -q
& $venvPy -m pip install -r (Join-Path $A "requirements.txt") -q
if ($LASTEXITCODE -ne 0) { Stop-Setup "pip couldn't install requirements.txt (see the messages above)." }
Ok "packages installed"
# the pre-commit check (requirements stay pinned), in this repo or the repo this folder is part of
$top = git -C $A rev-parse --show-toplevel 2>$null
if ($LASTEXITCODE -eq 0 -and $top) {
    $top = (Resolve-Path $top).Path
    $hooks = (Join-Path $A "githooks").Substring($top.Length).TrimStart([char]92).Replace([char]92, [char]47)   # \ -> /
    git -C $top config core.hooksPath $hooks
    Ok "pre-commit check on ($hooks)"
}

# ---- 4. the call page
Step "Call page (Othos-ui)"
Push-Location $Ui
try { pnpm install --silent; if ($LASTEXITCODE -ne 0) { Stop-Setup "pnpm install failed in $Ui." } } finally { Pop-Location }
Ok "pnpm packages installed"
$uiEnv = Join-Path $Ui ".env.local"
if (-not (Test-Path $uiEnv)) {
    Set-Content $uiEnv -Encoding utf8 -Value @(
        "LIVEKIT_URL=ws://127.0.0.1:7880", "LIVEKIT_API_KEY=devkey", "LIVEKIT_API_SECRET=secret",
        "# the coding model for the code chat", "OTHOS_CODE_MODEL=qwen2.5-coder:3b")
    Ok "made Othos-ui\.env.local (local LiveKit server)"
} else { Ok "Othos-ui\.env.local already there" }

# ---- 5. models (Ollama)
Step "Models"
$models = @("llama3.2:3b", "nomic-embed-text", "moondream:1.8b", "qwen2.5-coder:3b")
if ($NoModels) { Warn "skipped (-NoModels). Later: ollama pull $($models -join ', ')" }
else {
    $have = (ollama list) -join "`n"
    foreach ($m in $models) {
        if ($have -match [regex]::Escape($m)) { Ok "$m already pulled" }
        else { Write-Host "   pulling $m"; ollama pull $m; if ($LASTEXITCODE -eq 0) { Ok $m } else { Warn "couldn't pull $m" } }
    }
    if (Test-Path (Join-Path $A "make-gaming-model.ps1")) {
        & (Join-Path $A "make-gaming-model.ps1") | Out-Null; Ok "othos-gaming (CPU-only copy for gaming mode)"
    }
}

# ---- 6. your settings
Step "Your settings"
$toOpen = @()
foreach ($pair in @(@(".env.example", ".env"), @("personal.example.json", "personal.json"), @("profile.example.md", "profile.md"))) {
    $src = Join-Path $A $pair[0]; $dst = Join-Path $A $pair[1]
    if (Test-Path $dst) { Ok "$($pair[1]) already there (left as it is)" }
    else { Copy-Item $src $dst; Ok "made $($pair[1]) from $($pair[0])"; $toOpen += $dst }
}
# The assistant's name (personal.json "assistant": "name"); only asked for a new personal.json
$personal = Join-Path $A "personal.json"
if ($toOpen -contains $personal) {
    $who = (Read-Host "What would you like to call your assistant? (Enter for Othos)").Trim()
    if ($who -and $who -ne "Othos") {
        $esc = $who.Replace([string][char]92, [string][char]92 + [char]92).Replace('"', '\"')
        $txt = [IO.File]::ReadAllText($personal)
        $txt = ([regex]'("assistant"\s*:\s*\{\s*"name"\s*:\s*)""').Replace($txt, { param($m) $m.Groups[1].Value + '"' + $esc + '"' }, 1)
        [IO.File]::WriteAllText($personal, $txt, (New-Object Text.UTF8Encoding $false))
        Ok "your assistant is called $who (personal.json, assistant.name; README 'Make it yours' adds a logo)"
    } else { Ok "your assistant is called Othos" }
}
$envFile = Join-Path $A ".env"
if ($Mode -eq "offline" -and (Get-Content $envFile -Raw) -match "(?m)^OTHOS_SPEECH=cloud") {
    (Get-Content $envFile -Raw) -replace "(?m)^OTHOS_SPEECH=cloud", "OTHOS_SPEECH=local" | Set-Content $envFile -Encoding utf8 -NoNewline
    Ok "OTHOS_SPEECH=local in .env"
}
if ($Mode -eq "offline") {
    Warn ("Fully offline also needs two local speech servers running: Speaches (speech-to-text, port 8000) " +
          "and Kokoro-FastAPI (voice, port 8880). README 'Fully offline' has the steps; Tavily stays off without a key.")
} else {
    Warn "Mixed mode needs DEEPGRAM_API_KEY and RIME_API_KEY in .env (README says where to get them)."
}
# ---- 7. this PC's specs: the Othos window asks on its first start ("May I check your current
# system specs...?") and shows the advice in its own window; "Check my PC..." does it any time
$specsPy = Join-Path $A "system_specs.py"
if ((Test-Path $venvPy) -and (Test-Path $specsPy)) {
    & $venvPy $specsPy --asked
    if ($LASTEXITCODE -ne 0) {
        Step "This PC"
        Write-Host "   When the Othos window first opens (build-othos-app.ps1), it offers to check this PC's"
        Write-Host "   specs and suggest settings. Nothing leaves this computer. Without the window:"
        Write-Host "     .venv\Scripts\python.exe system_specs.py --read --advice"
    }
}

$npp = "C:\Program Files\Notepad++\notepad++.exe"
foreach ($f in $toOpen) {
    if (Test-Path $npp) { Start-Process $npp "`"$f`"" } else { Start-Process notepad.exe "`"$f`"" }
}

Step "Done"
if ($toOpen.Count) { Write-Host "   Fill in the files that just opened, then check everything with:" }
else { Write-Host "   Check everything with:" }
Write-Host "     powershell -ExecutionPolicy Bypass -File doctor.ps1"
Write-Host "   Start Othos with start-othos.bat, or build the taskbar app: build-othos-app.ps1"
