# Othos-agent: keep requirements.txt locked to exact versions.
#
#   powershell -ExecutionPolicy Bypass -File update-requirements.ps1          re-lock from .venv
#   powershell -ExecutionPolicy Bypass -File update-requirements.ps1 -Check   only check (used by the git pre-commit hook)
#
# Rule (since 2026-11-09): every package line is "name==version". After installing or
# upgrading anything in .venv, run this script and commit requirements.txt with the change.
param([switch]$Check)

$A    = $PSScriptRoot
$Py   = Join-Path $A ".venv\Scripts\python.exe"
$Req  = Join-Path $A "requirements.txt"

if (-not (Test-Path $Py)) { Write-Host "No .venv found at $Py"; exit 1 }
$freeze = & $Py -m pip freeze --exclude-editable
if ($LASTEXITCODE -ne 0) { Write-Host "pip freeze failed"; exit 1 }
$pyver = (& $Py --version) -replace '^Python\s+', ''

function Packages([string[]]$lines) {
    $lines | Where-Object { $_.Trim() -and -not $_.TrimStart().StartsWith('#') } | ForEach-Object { $_.Trim() }
}

if ($Check) {
    $lines = Packages (Get-Content $Req)
    $unpinned = $lines | Where-Object { $_ -notmatch '^[A-Za-z0-9_.\-\[\],]+==[^=\s]+$' }
    $problems = @()
    if ($unpinned) { $problems += "These lines have no exact version (name==version):"; $problems += $unpinned | ForEach-Object { "  $_" } }
    $want = @($freeze | Sort-Object); $have = @($lines | Sort-Object)
    $diff = Compare-Object $have $want
    if ($diff) {
        $problems += "requirements.txt doesn't match what's installed in .venv:"
        $problems += $diff | ForEach-Object { if ($_.SideIndicator -eq '=>') { "  installed, not listed: $($_.InputObject)" } else { "  listed, not installed: $($_.InputObject)" } }
    }
    if ($problems) {
        $problems | ForEach-Object { Write-Host $_ }
        Write-Host "Fix: powershell -ExecutionPolicy Bypass -File update-requirements.ps1, then git add requirements.txt"
        exit 1
    }
    Write-Host "requirements.txt: all $($lines.Count) packages pinned and matching .venv"
    exit 0
}

$version = (git -C $A describe --tags --abbrev=0 2>$null)
if (-not $version) { $version = "untagged" }
$header = @(
 "# Othos-agent: exact package versions that work (pip freeze of .venv, Python $pyver, $(Get-Date -Format yyyy-MM-dd), Othos $version)."
 "# Every line must be name==version (checked by the git pre-commit hook). Don't edit by hand."
 "# After installing or upgrading anything in .venv, re-lock with:"
 "#   powershell -ExecutionPolicy Bypass -File update-requirements.ps1"
 "# Fresh install (new build): Python 3.12 only - livekit-agents 1.8.11 doesn't install on 3.15."
 "#   py -3.12 -m venv .venv"
 "#   .venv\Scripts\python.exe -m pip install -r requirements.txt"
 "# Direct needs (the rest are what these pull in): livekit-agents and plugins (openai, silero,"
 "# deepgram, rime, turn-detector), python-dotenv, tavily-python, httpx, psutil (gaming.py)."
)
($header + $freeze) | Set-Content $Req -Encoding ascii   # ASCII: PowerShell's '>' would write UTF-16
Write-Host "requirements.txt locked: $($freeze.Count) packages (Python $pyver, Othos $version)"
