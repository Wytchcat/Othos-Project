# Othos tray icon: sits with the other icons by the clock while Othos runs.
# Green ring = LiveKit server, agent and call page all up. Gold ring = part of Othos is down.
# Double-click opens the call page; right-click for the menu. Started by start-othos.ps1.
$AgentDir = $PSScriptRoot
$Url      = "http://127.0.0.1:3000"
$PidFile  = "$AgentDir\logs\pids.txt"

Add-Type -AssemblyName System.Windows.Forms, System.Drawing
. "$AgentDir\open-private.ps1"   # Open-Private: private browser window

# only one tray icon at a time
$created = $false
$mutex = New-Object Threading.Mutex($true, "Local\OthosTray", [ref]$created)
if (-not $created) { exit 0 }

$iconOk   = New-Object Drawing.Icon "$AgentDir\icons\othos-tray.ico", 32, 32
$iconWarn = New-Object Drawing.Icon "$AgentDir\icons\othos-tray-warn.ico", 32, 32

$tray = New-Object Windows.Forms.NotifyIcon
$tray.Icon = $iconWarn
$tray.Text = "Othos: starting"
$tray.Visible = $true

$menu = New-Object Windows.Forms.ContextMenuStrip
$status = $menu.Items.Add("Othos: starting")
$status.Enabled = $false
[void]$menu.Items.Add("-")
$menu.Items.Add("Open call page").add_Click({ Open-Private $Url })
$menu.Items.Add("Open logs folder").add_Click({ Start-Process explorer.exe "$AgentDir\logs" })
[void]$menu.Items.Add("-")
$menu.Items.Add("Stop Othos").add_Click({
    Start-Process powershell.exe -WindowStyle Hidden -ArgumentList `
        "-NoProfile -ExecutionPolicy Bypass -File `"$AgentDir\stop-othos.ps1`""
})
$menu.Items.Add("Hide this icon").add_Click({ $tray.Visible = $false; [Windows.Forms.Application]::Exit() })
$tray.ContextMenuStrip = $menu
$tray.add_DoubleClick({ Open-Private $Url })

function Test-Port($port) {
    [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

$started = Get-Date
$timer = New-Object Windows.Forms.Timer
$timer.Interval = 5000
$timer.add_Tick({
    # Stop Othos removes the pid file; then the tray icon goes too
    if (-not (Test-Path $PidFile) -and ((Get-Date) - $started).TotalSeconds -gt 20) {
        $tray.Visible = $false; [Windows.Forms.Application]::Exit(); return
    }
    $down = @()
    if (-not (Test-Port 7880)) { $down += "LiveKit server" }
    if (-not (Test-Port 8081)) { $down += "agent" }
    if (-not (Test-Port 3000)) { $down += "call page" }
    if ($down.Count -eq 0) { $text = "Othos: running"; $tray.Icon = $iconOk }
    else { $text = "Othos: " + ($down -join ", ") + " not running"; $tray.Icon = $iconWarn }
    $tray.Text = $text.Substring(0, [Math]::Min(63, $text.Length))   # tooltip limit
    $status.Text = $text
})
$timer.Start()

[Windows.Forms.Application]::Run()
$timer.Stop(); $tray.Dispose(); $mutex.ReleaseMutex()
