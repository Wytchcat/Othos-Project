# A message box in Othos's colours and logo, for start-othos.ps1 and stop-othos.ps1
# (never the plain white Windows box). Resizable; Enter, Esc and the X all just close it.
# Use:  . "$PSScriptRoot\othos-box.ps1";  Show-OthosBox "text"
function Show-OthosBox([string]$Message, [string]$Title = "") {
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()
    # the assistant's name and logo from personal.json (the Othos app reads them the same way)
    $name = "Othos"; $logo = ""
    try {
        $a = (Get-Content (Join-Path $PSScriptRoot "personal.json") -Raw -Encoding UTF8 | ConvertFrom-Json).assistant
        if ($a.name) { $name = [string]$a.name }
        if ($a.logo) { $logo = [string]$a.logo; if (-not [IO.Path]::IsPathRooted($logo)) { $logo = Join-Path $PSScriptRoot $logo } }
    } catch { }
    if (-not $Title) { $Title = $name }
    $ico = $null
    try {
        if ($logo -like "*.ico" -and (Test-Path $logo)) { $ico = New-Object System.Drawing.Icon $logo }
        else { $ico = New-Object System.Drawing.Icon (Join-Path $PSScriptRoot "icons\othos-tray.ico") }
    } catch { }

    $bg    = [System.Drawing.Color]::FromArgb(2, 7, 5)
    $panel = [System.Drawing.Color]::FromArgb(5, 12, 8)
    $green = [System.Drawing.Color]::FromArgb(61, 220, 132)
    $ink   = [System.Drawing.Color]::FromArgb(200, 230, 210)

    $f = New-Object System.Windows.Forms.Form
    $f.Text = $Title; $f.BackColor = $bg; $f.ForeColor = $ink
    $f.Font = New-Object System.Drawing.Font("Segoe UI", 10)
    $f.FormBorderStyle = "Sizable"; $f.MaximizeBox = $true; $f.MinimizeBox = $false
    $f.ShowInTaskbar = $true; $f.StartPosition = "CenterScreen"; $f.TopMost = $true
    if ($ico) { $f.Icon = $ico }
    $f.MinimumSize = New-Object System.Drawing.Size(340, 170)
    $wa = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
    $f.ClientSize = New-Object System.Drawing.Size ([Math]::Min(480, $wa.Width - 40)), ([Math]::Min(230, $wa.Height - 40))

    $ok = New-Object System.Windows.Forms.Button
    $ok.Text = "OK"; $ok.FlatStyle = "Flat"; $ok.BackColor = $panel; $ok.ForeColor = $green
    $ok.FlatAppearance.BorderColor = $green; $ok.Size = New-Object System.Drawing.Size(96, 32)
    $ok.DialogResult = "OK"
    $bar = New-Object System.Windows.Forms.FlowLayoutPanel
    $bar.Dock = "Bottom"; $bar.Height = 56; $bar.BackColor = $panel
    $bar.FlowDirection = "RightToLeft"; $bar.Padding = New-Object System.Windows.Forms.Padding(12, 10, 12, 10)
    $bar.Controls.Add($ok)

    $lbl = New-Object System.Windows.Forms.Label
    $lbl.Dock = "Fill"; $lbl.Padding = New-Object System.Windows.Forms.Padding(16, 14, 16, 8)
    $lbl.Text = $Message; $lbl.ForeColor = $ink; $lbl.BackColor = $bg

    $f.Controls.Add($lbl); $f.Controls.Add($bar)
    $f.AcceptButton = $ok; $f.CancelButton = $ok
    # a dark title bar to match (Windows 10 build 18985 and later; ignored if not supported)
    $f.Add_HandleCreated({
        try {
            if (-not ("OthosDwm" -as [type])) {
                Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class OthosDwm { [DllImport("dwmapi.dll")] public static extern int DwmSetWindowAttribute(IntPtr h, int a, ref int v, int s); }'
            }
            $one = 1; [void][OthosDwm]::DwmSetWindowAttribute($this.Handle, 20, [ref]$one, 4)
        } catch { }
    })
    [void]$f.ShowDialog()
    $f.Dispose()
}
