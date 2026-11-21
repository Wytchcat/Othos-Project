# Folders are found from this script's own place, so Othos can live anywhere:
# Othos-agent (this folder), its sibling Othos-ui (or "ui" in the shared copy, where
# they're Othos\agent and Othos\ui), and the LiveKit server in a sibling livekit_server folder. OTHOS_UI_DIR / OTHOS_LIVEKIT_DIR override them.
$AgentDir    = $PSScriptRoot
$Parent      = Split-Path $AgentDir -Parent
$UiDir       = if ($env:OTHOS_UI_DIR) { $env:OTHOS_UI_DIR } elseif (Test-Path (Join-Path $Parent "Othos-ui\package.json")) { Join-Path $Parent "Othos-ui" } else { Join-Path $Parent "ui" }
$LiveKitDir  = if ($env:OTHOS_LIVEKIT_DIR) { $env:OTHOS_LIVEKIT_DIR } else { Join-Path $Parent "livekit_server" }
if (-not (Test-Path "$LiveKitDir\livekit-server.exe") -and ($lk = Get-Command livekit-server.exe -ErrorAction SilentlyContinue)) {
    $LiveKitDir = Split-Path $lk.Source -Parent
}
$Pnpm        = Join-Path $env:APPDATA "npm\pnpm.cmd"
if (-not (Test-Path $Pnpm) -and ($pc = Get-Command pnpm.cmd -ErrorAction SilentlyContinue)) { $Pnpm = $pc.Source }
$LogDir      = "$AgentDir\logs"
$OllamaModel = "llama3.2:3b"
$Url         = "http://127.0.0.1:3000"   # 127.0.0.1, not localhost (IPv6 issue)

. "$AgentDir\othos-box.ps1"      # Show-OthosBox: a message box in Othos's colours and logo
function Say($msg) { Show-OthosBox $msg }
function Fail($msg) { Say $msg; exit 1 }
. "$AgentDir\open-private.ps1"   # Open-Private: the call page in a private browser window

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$missing = @("$LiveKitDir\livekit-server.exe", "$AgentDir\agent.py",
             "$AgentDir\.venv\Scripts\python.exe", "$UiDir\package.json", $Pnpm) | Where-Object { -not (Test-Path $_) }
if ($missing) {
    Fail ("Othos isn't set up yet.`n`nRun setup.ps1 first (README, step 1), then start Othos again.`n`nMissing:`n" +
          (($missing | ForEach-Object { "  $_" }) -join "`n"))
}

# Refuse to start twice: LiveKit server (7880), agent (8081), web page (3000)
$busy = @()
foreach ($port in 7880, 8081, 3000) {
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { $busy += $port }
}
if ($busy.Count -eq 3) {
    if (Test-Path "$LogDir\pids.txt") {   # started by this button: make sure the tray icon is back
        Start-Process powershell.exe -WindowStyle Hidden -ArgumentList `
            "-NoProfile -ExecutionPolicy Bypass -STA -File `"$AgentDir\othos-tray.ps1`""
    }
    Open-Private $Url   # already running: just open the page, no pop-up
    exit 0
}
if ($busy.Count -gt 0) {
    Fail ("Part of Othos is already running (port " + ($busy -join ", ") + ").`n`n" +
          "If it was started from Claude Code, stop it there first. Otherwise press Stop Othos, then Start Othos again.")
}

$pids = @()
# Keep the previous run's log in logs\old (newest 20 per log) instead of overwriting it:
# a restart once wiped the log of the call that needed reviewing.
function Save-OldLog($name) {
    $log = Join-Path $LogDir "$name.log"
    if ((Test-Path $log) -and (Get-Item $log).Length -gt 0) {
        $old = Join-Path $LogDir "old"
        New-Item -ItemType Directory -Force -Path $old | Out-Null
        Move-Item $log (Join-Path $old ("{0}-{1:yyyy-MM-dd-HHmmss}.log" -f $name, (Get-Item $log).LastWriteTime)) -Force
        Get-ChildItem $old -Filter "$name-*.log" | Sort-Object LastWriteTime -Descending | Select-Object -Skip 20 | Remove-Item -Force
    }
}

function Start-Hidden($name, $workDir, $cmdLine) {
    Save-OldLog $name
    $p = Start-Process -FilePath "cmd.exe" `
        -ArgumentList "/s /c `"$cmdLine > `"$LogDir\$name.log`" 2>&1`"" `
        -WorkingDirectory $workDir -WindowStyle Hidden -PassThru
    $script:pids += $p.Id
}

# No llama tray icon while Othos runs: close only Ollama's tray app. Its model server
# keeps running (models stay loaded), and Stop Othos leaves Ollama alone.
Stop-Process -Name "ollama app" -Force -ErrorAction SilentlyContinue
function Test-Ollama { try { $c = New-Object Net.Sockets.TcpClient("127.0.0.1", 11434); $c.Close(); $true } catch { $false } }
if (-not (Test-Ollama)) {
    # Ollama wasn't running at all: start its server hidden (no icon). Not in pids.txt,
    # so Stop Othos leaves it running.
    $OllamaDir = "$env:LOCALAPPDATA\Programs\Ollama"
    Start-Process cmd.exe -WorkingDirectory $OllamaDir -WindowStyle Hidden -ArgumentList `
        "/s /c `"`"$OllamaDir\ollama.exe`" serve > `"$LogDir\ollama.log`" 2>&1`""
    $deadline = (Get-Date).AddSeconds(20)
    do { Start-Sleep -Milliseconds 500 } until ((Test-Ollama) -or (Get-Date) -gt $deadline)
    if (-not (Test-Ollama)) { Say "Ollama didn't start within 20 seconds. Check $LogDir\ollama.log." }
}

# Warm the Ollama model in the background (fails quietly if Ollama is not running).
# Not when voice-llm.json puts the conversation on llama-server: the agent starts that and
# would only unload Ollama's copy again.
$onLlamaServer = $false
try { $onLlamaServer = ((Get-Content "$AgentDir\voice-llm.json" -Raw -Encoding utf8 | ConvertFrom-Json).backend -eq "llama-server") } catch { }
if (-not $onLlamaServer) {
    $body = '{\"model\":\"' + $OllamaModel + '\",\"keep_alive\":\"-1m\"}'
    Start-Process curl.exe -WindowStyle Hidden `
        -ArgumentList "-s http://127.0.0.1:11434/api/generate -d `"$body`""
}

Start-Hidden "server" $LiveKitDir "`"$LiveKitDir\livekit-server.exe`" --dev"
Start-Sleep -Seconds 4
Start-Hidden "agent" $AgentDir "`"$AgentDir\.venv\Scripts\python.exe`" -u agent.py dev"
Start-Sleep -Seconds 4
Start-Hidden "ui" $UiDir "`"$Pnpm`" dev"

# Tray icon by the clock (green = running, gold = part of Othos down); Stop Othos removes it
$tray = Start-Process powershell.exe -WindowStyle Hidden -PassThru -ArgumentList `
    "-NoProfile -ExecutionPolicy Bypass -STA -File `"$AgentDir\othos-tray.ps1`""
$pids += $tray.Id

$pids | Set-Content "$LogDir\pids.txt"

# Wait for the web page, then open the browser
$deadline = (Get-Date).AddSeconds(90)
do {
    Start-Sleep -Seconds 2
    try { $c = New-Object Net.Sockets.TcpClient("127.0.0.1", 3000); $c.Close(); $ready = $true }
    catch { $ready = $false }
} until ($ready -or (Get-Date) -gt $deadline)

if ($ready) { Open-Private $Url }
else { Say "Othos started, but the page didn't answer within 90 seconds. Check $LogDir\ui.log." }
