# Restart only the Othos agent (after agent.py changes), keeping the rest of Othos running.
# Refuses if Othos isn't running from the taskbar app, or if a call is open. The old
# agent.log is kept in logs\old (newest 20) instead of being overwritten.
#   powershell -ExecutionPolicy Bypass -File restart-agent.ps1
$A = $PSScriptRoot
$L = Join-Path $A "logs"
$Py = Join-Path $A ".venv\Scripts\python.exe"

if (-not (Test-Path "$L\pids.txt") -or -not (Get-Process Othos -ErrorAction SilentlyContinue)) {
    Write-Host "Othos isn't running from the taskbar app: nothing to restart (changes apply when it starts)."; exit 0
}
$log = Get-Content "$L\agent.log" -ErrorAction SilentlyContinue
$req = ($log | Select-String 'received job request' | Select-Object -Last 1).LineNumber
$exit = ($log | Select-String 'job exiting' | Select-Object -Last 1).LineNumber
if ($req -and $req -gt $exit) { Write-Host "A call is open: not restarting."; exit 1 }

Push-Location $A
$env:PYTHONIOENCODING = "utf-8"
& $Py -c "import agent" 2>$null
if ($LASTEXITCODE -ne 0) { Pop-Location; Write-Host "agent.py doesn't import: not restarting."; exit 1 }
Pop-Location

$pids = Get-Content "$L\pids.txt"
taskkill /PID $pids[1] /T /F | Out-Null
Start-Sleep 2
if (Test-Path "$L\agent.log") {   # keep the old log
    $old = Join-Path $L "old"; New-Item -ItemType Directory -Force -Path $old | Out-Null
    Move-Item "$L\agent.log" (Join-Path $old ("agent-{0:yyyy-MM-dd-HHmmss}.log" -f (Get-Item "$L\agent.log").LastWriteTime)) -Force
    Get-ChildItem $old -Filter "agent-*.log" | Sort-Object LastWriteTime -Descending | Select-Object -Skip 20 | Remove-Item -Force
}
$p = Start-Process cmd.exe -WorkingDirectory $A -WindowStyle Hidden -PassThru -ArgumentList `
    "/s /c `"`"$Py`" -u agent.py dev > `"$L\agent.log`" 2>&1`""
$pids[1] = "$($p.Id)"; $pids | Set-Content "$L\pids.txt"
$deadline = (Get-Date).AddSeconds(40)
do { Start-Sleep 2; $ok = Select-String -Path "$L\agent.log" -Pattern 'registered worker' -Quiet } until ($ok -or (Get-Date) -gt $deadline)
Write-Host "agent registered: $ok"
