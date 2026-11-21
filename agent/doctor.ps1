# Othos doctor: checks the set-up and says what's missing. Changes nothing.
#   powershell -ExecutionPolicy Bypass -File doctor.ps1
$A  = $PSScriptRoot
$Parent = Split-Path $A -Parent
$Ui = if ($env:OTHOS_UI_DIR) { $env:OTHOS_UI_DIR } elseif (Test-Path (Join-Path $Parent "Othos-ui\package.json")) { Join-Path $Parent "Othos-ui" } else { Join-Path $Parent "ui" }  # Othos-ui, or ui in the shared copy
$Lk = if ($env:OTHOS_LIVEKIT_DIR) { $env:OTHOS_LIVEKIT_DIR } else { Join-Path $Parent "livekit_server" }
$script:problems = 0

function Ok($t)   { Write-Host "  ok   $t" -ForegroundColor Green }
function Info($t) { Write-Host "  --   $t" -ForegroundColor Gray }
function Bad($t)  { Write-Host "  FIX  $t" -ForegroundColor Red; $script:problems++ }
function Head($t) { Write-Host ""; Write-Host $t -ForegroundColor Cyan }

function Read-Env($path) {
    $h = @{}
    if (Test-Path $path) {
        foreach ($line in Get-Content $path) {
            if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $h[$matches[1]] = $matches[2].Trim().Trim('"') }
        }
    }
    return $h
}

Head "Programs"
$venvPy = Join-Path $A ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    $v = & $venvPy -c "import sys; print('%d.%d' % sys.version_info[:2])"
    if ($v -eq "3.12") { Ok ".venv uses Python 3.12" } else { Bad ".venv uses Python $v; Othos needs 3.12. Delete .venv and run setup.ps1." }
    & $venvPy -c "import livekit.agents" 2>$null
    if ($LASTEXITCODE -eq 0) { Ok "livekit-agents installed" } else { Bad "packages missing: .venv\Scripts\python.exe -m pip install -r requirements.txt" }
} else {
    # no .venv yet: the usual cause on a new PC is a missing Python 3.12, so say so
    $py312 = $false
    if (Get-Command py -ErrorAction SilentlyContinue) { & py -3.12 --version *> $null; $py312 = ($LASTEXITCODE -eq 0) }
    if ($py312) { Ok "Python 3.12 found (py launcher)"; Bad "no .venv: run setup.ps1" }
    else { Bad "Python 3.12 not found: install 3.12.x from https://www.python.org/downloads/ (tick 'py launcher'), then run setup.ps1" }
}
foreach ($t in "node", "pnpm", "ollama") {
    if (Get-Command $t -ErrorAction SilentlyContinue) { Ok "$t found" } else { Bad "$t not found (setup.ps1 says where to get it)" }
}
if ((Test-Path "$Lk\livekit-server.exe") -or (Get-Command livekit-server.exe -ErrorAction SilentlyContinue)) { Ok "LiveKit server found" }
else { Bad "LiveKit server not in $Lk (setup.ps1 downloads it)" }
if (Test-Path "$Ui\package.json") { Ok "Othos-ui at $Ui" } else { Bad "Othos-ui not found at $Ui" }
if (Test-Path "$Ui\node_modules") { Ok "Othos-ui packages installed" } else { Bad "run: pnpm install (in $Ui)" }

Head "Settings"
$envPath = Join-Path $A ".env"
$e = Read-Env $envPath
$speech = if ($e["OTHOS_SPEECH"]) { $e["OTHOS_SPEECH"].ToLower() } else { "cloud" }
if (-not (Test-Path $envPath)) {
    # one cause, one line: without .env every key below would be empty too
    Bad ".env missing (so none of its keys are set): run setup.ps1, or copy .env.example to .env and fill it in"
} else {
    Info "speech: $speech"
    $need = @("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
    if ($speech -eq "cloud") { $need += "DEEPGRAM_API_KEY", "RIME_API_KEY" } else { $need += "LOCAL_STT_URL", "LOCAL_TTS_URL" }
    foreach ($k in $need) { if ($e[$k]) { Ok "$k set" } else { Bad "$k is empty in .env" } }
}
if ($e["TAVILY_API_KEY"]) { Ok "TAVILY_API_KEY set" } else { Info "TAVILY_API_KEY empty: web search off (free sources still work)" }
if (-not $e["RAVELRY_KEY"] -and $e["RAVERLY_KEY"]) { $e["RAVELRY_KEY"] = $e["RAVERLY_KEY"] }  # old spelling, still accepted
foreach ($k in "GUARDIAN_API_KEY", "NYT_API_KEY", "YOUTUBE_API_KEY", "RAVELRY_KEY", "TWITCH_CLIENT_ID") {
    if (-not $e[$k]) { Info "$k empty (optional)" }
}
$pj = Join-Path $A "personal.json"
if (Test-Path $pj) {
    try { $p = Get-Content $pj -Raw -Encoding utf8 | ConvertFrom-Json; Ok "personal.json reads fine"
          if (-not $p.name) { Info "personal.json has no name (Othos won't use one)" }
          if (-not $p.weather_latlon) { Info "personal.json has no weather_latlon (no weather at call start)" } }
    catch { Bad "personal.json isn't valid JSON: $($_.Exception.Message)" }
} else { Bad "personal.json missing: copy personal.example.json to personal.json" }
# this PC's saved specs (system_specs.py): reading them again takes about 10 seconds
$specs = Join-Path $A "state\system-specs.json"
if ((Test-Path $venvPy) -and (Test-Path (Join-Path $A "system_specs.py"))) {
    if (-not (Test-Path $specs)) { Info "PC specs not checked yet ('Check my PC...' in the Othos window)" }
    elseif ((Get-Content $specs -Raw) -match '"declined":\s*true') { Info "PC specs: you chose not to check ('Check my PC...' does it any time)" }
    else {
        $why = & $venvPy (Join-Path $A "system_specs.py") --stale
        if ($LASTEXITCODE -eq 0) { Ok "PC specs are current" }
        else { Info "$why Run 'Check my PC...' in the Othos window for new advice." }
    }
}
if (Test-Path (Join-Path $A "profile.md")) { Ok "profile.md there" } else { Info "profile.md missing (optional): copy profile.example.md" }
# which engine runs the conversation (voice-llm.json; llm_server.py reads it the same way)
$vl = @{ backend = "ollama"; exe = ""; port = 11440 }
try {
    $j = Get-Content (Join-Path $A "voice-llm.json") -Raw -Encoding utf8 | ConvertFrom-Json
    if ($j.backend) { $vl.backend = ([string]$j.backend).ToLower() }
    if ($j.llama_server_exe) { $vl.exe = [Environment]::ExpandEnvironmentVariables([string]$j.llama_server_exe) }
    if ($j.port) { $vl.port = [int]$j.port }
} catch { }
if ($vl.backend -eq "llama-server") {
    $exe = if ($vl.exe) { if (Test-Path $vl.exe) { $vl.exe } } else {
        Get-ChildItem (Join-Path ([Environment]::GetFolderPath("MyDocuments")) "llama.cpp") -Recurse -Filter llama-server.exe -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName }
    if ($exe) { Ok "conversation on llama-server ($exe)" }
    else { Bad "voice-llm.json says llama-server, but no llama-server.exe was found: set its backend to ollama, or set llama_server_exe" }
} else { Info "conversation on Ollama (voice-llm.json)" }

Head "127.0.0.1, not localhost"
$uiEnv = Read-Env (Join-Path $Ui ".env.local")
$found = $false
foreach ($pair in @(@(".env", $e), @("Othos-ui\.env.local", $uiEnv))) {
    foreach ($k in $pair[1].Keys) {
        if ($pair[1][$k] -match "localhost") { Bad "$($pair[0]) $k uses localhost; use 127.0.0.1 (localhost can pick IPv6 and fail)"; $found = $true }
    }
}
if (-not $found) { Ok "no localhost addresses" }
if ($uiEnv["LIVEKIT_URL"] -and $e["LIVEKIT_URL"] -and $uiEnv["LIVEKIT_URL"] -ne $e["LIVEKIT_URL"]) {
    Bad "LIVEKIT_URL differs: .env has $($e['LIVEKIT_URL']), Othos-ui\.env.local has $($uiEnv['LIVEKIT_URL'])"
}

Head "Ports"
$ports = [ordered]@{ "7880" = "LiveKit server"; "8081" = "agent"; "3000" = "call page"; "11434" = "Ollama"; "11436" = "The Smithy's own Ollama" }
if ($speech -eq "local") { $ports["8000"] = "local speech-to-text"; $ports["8880"] = "Kokoro voice" }
if ($vl.backend -eq "llama-server") { $ports["$($vl.port)"] = "the conversation's llama-server" }
# Othos's own servers: in use is how it should be (The Smithy's Ollama is only there when smithy.json starts it)
$ownPorts = @(11436, $(if ($vl.backend -eq "llama-server") { $vl.port }))
$othosUp = Test-Path (Join-Path $A "logs\pids.txt")
foreach ($key in @($ports.Keys)) {
    $port = [int]$key
    $c = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) {
        $proc = (Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue).ProcessName
        $msg = "$port ($($ports[$key])) in use by $proc"
        if ($port -eq 11434 -or $port -in $ownPorts -or ($speech -eq "local" -and $port -in 8000, 8880)) { Ok $msg }
        elseif ($othosUp) { Info "$msg (Othos is running)" }
        else { Bad "$msg, and Othos isn't running: close that program, or Othos can't start" }
    } else {
        if ($port -eq 11434) { Bad "nothing on 11434: start Ollama" }
        elseif ($speech -eq "local" -and $port -in 8000, 8880) { Bad "nothing on $port ($($ports[$key])): start it" }
        elseif ($port -eq $vl.port -and $vl.backend -eq "llama-server") { Info "$port free ($($ports[$key]); the agent starts it with Othos)" }
        else { Ok "$port free ($($ports[$key]))" }
    }
}

Head "Models (Ollama)"
if (Get-Command ollama -ErrorAction SilentlyContinue) {
    $have = (ollama list 2>$null) -join "`n"
    $want = @(($(if ($e["OLLAMA_MODEL"]) { $e["OLLAMA_MODEL"] } else { "llama3.2:3b" })), "nomic-embed-text", "moondream:1.8b", "qwen2.5-coder:3b")
    foreach ($m in $want) { if ($have -match [regex]::Escape($m)) { Ok $m } else { Bad "$m missing: ollama pull $m" } }
    if ($have -notmatch "othos-gaming") { Info "othos-gaming missing (gaming mode only): make-gaming-model.ps1" }
}

Write-Host ""
if ($script:problems) { Write-Host "$($script:problems) thing(s) to fix (FIX lines above)." -ForegroundColor Yellow; exit 1 }
Write-Host "All good." -ForegroundColor Green
