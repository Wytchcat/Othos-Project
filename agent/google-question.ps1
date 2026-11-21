# The "Connect your Google account?" question at set-up (called by setup.ps1).
# Same words as the installer's page (google-prompt.txt); same record (state\google-prompt.json), so you are never asked twice.
#   Asked only when run by a person in a console (the installer shows its own page, and its output is redirected),
#   and only when the record allows it: not when "dont_ask_again" is true or the answer was "connect".
# Nothing is connected here and no network call is made: "y" opens the Connect Google window (google-connect.ps1), where the user signs in.
param([switch]$Force, [string]$Reply = $null, [string]$StateDir = "")

$A = $PSScriptRoot
if (-not $StateDir) { $StateDir = Join-Path $A "state" }
$record = Join-Path $StateDir "google-prompt.json"
$textFile = Join-Path $A "google-prompt.txt"

$ask = $true
if (Test-Path $record) {
    try {
        $g = Get-Content $record -Raw | ConvertFrom-Json
        if ($g.dont_ask_again -eq $true -or $g.answer -eq "connect") { $ask = $false }
    } catch { }          # a missing or broken file means ask
}
if (-not $Force -and ([Console]::IsInputRedirected -or [Console]::IsOutputRedirected)) { $ask = $false }   # the installer asks on its own page
if (-not $ask) { return }

function Save-Answer([string]$answer, [bool]$never) {
    New-Item -ItemType Directory -Force $StateDir | Out-Null
    $json = '{"answer": "' + $answer + '", "dont_ask_again": ' + $(if ($never) { "true" } else { "false" }) +
            ', "asked_on": "' + (Get-Date -Format "yyyy-MM-dd") + '"}'
    [IO.File]::WriteAllText($record, $json + "`n", (New-Object Text.UTF8Encoding $false))
}

Write-Host ""
Write-Host "== Google (optional)" -ForegroundColor Cyan
Write-Host "   Connect your Google account? Othos could read your Google Calendar, Sheets, Docs, Gmail and"
Write-Host "   Google Voice voicemails by voice. Everything starts read-only, and you can undo it any time."
Write-Host "   Type:  N = not now (default)   more = the benefits and the risks   never = don't ask again   y = connect"
$shown = $false
while ($true) {
    $r = if ($null -ne $Reply -and -not $shown) { $Reply } elseif ($null -ne $Reply) { "n" } else { Read-Host "   [N/more/never/y]" }
    $r = ("" + $r).Trim().ToLower()
    if ($r -eq "more" -or $r -eq "m") {
        Write-Host ""
        if (Test-Path $textFile) { Get-Content $textFile | ForEach-Object { Write-Host "   $_" } }
        Write-Host ""
        Write-Host "   Type:  N = not now (default)   never = don't ask again   y = connect"
        $shown = $true
        Save-Answer "show_how" $false
        continue
    }
    if ($r -eq "never") { Save-Answer $(if ($shown) { "show_how" } else { "not_now" }) $true; Write-Host "   ok: won't ask again (Othos's Connect Google window will still be there later)." -ForegroundColor Green; break }
    if ($r -eq "y" -or $r -eq "yes") {
        $win = Join-Path $A "google-connect.ps1"
        if ((Test-Path $win) -and -not $Reply) {
            Start-Process powershell.exe -WindowStyle Hidden -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$win`""
        }
        Write-Host "   Opening Connect Google: press Connect there to sign in (you can also open it later with google-connect.ps1)." -ForegroundColor Green
        Save-Answer "connect" $false
        break
    }
    Save-Answer $(if ($shown) { "show_how" } else { "not_now" }) $false
    Write-Host "   ok: not now. Nothing was connected." -ForegroundColor Green
    break
}
