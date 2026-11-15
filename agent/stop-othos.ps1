param([switch]$Quiet)   # -Quiet: no pop-up at all (used by Othos.exe when its window closes)
$LogDir  = Join-Path $PSScriptRoot "logs"
$pidFile = "$LogDir\pids.txt"

. "$PSScriptRoot\othos-box.ps1"   # Show-OthosBox: a message box in Othos's colours and logo
function Say($msg) { Show-OthosBox $msg }

if (-not (Test-Path $pidFile)) {
    if ($Quiet) { exit 0 }
    Say "Othos isn't running from the Start Othos button, so there is nothing to stop.`n`nIf it was started from Claude Code, stop it there."
    exit 0
}

# stops the LiveKit server, agent, call page and tray icon (Ollama is left running)
Get-Content $pidFile | ForEach-Object {
    if ($_ -match '^\d+$') { taskkill /PID $_ /T /F 2>$null | Out-Null }
}
Remove-Item $pidFile   # done; no pop-up (the tray icon disappearing shows it stopped)
