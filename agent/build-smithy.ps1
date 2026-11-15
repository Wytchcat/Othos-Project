# Builds app\Smithy.exe (the Othos coding worker's own taskbar app) from
# app\WorkerApp.cs (shared with Firestarter, compiled with /define:SMITHY) with the
# C# compiler that comes with Windows, draws its hammer-on-an-anvil icon, and makes a
# "The Smithy" shortcut on the Desktop and in the Start menu.
# Close The Smithy first. Run: powershell -ExecutionPolicy Bypass -File build-smithy.ps1
$A   = $PSScriptRoot
$csc = "C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
$ico = "$A\icons\smithy.ico"
Add-Type -AssemblyName System.Drawing

# --- icon: a hammer over an anvil, steel blue, on the dark Othos chip (PNG-in-ICO, several sizes)
$sizes = 256, 48, 32, 16
$pngs = foreach ($s in $sizes) {
    $bmp = New-Object Drawing.Bitmap $s, $s
    $g = [Drawing.Graphics]::FromImage($bmp); $g.SmoothingMode = 'AntiAlias'; $g.Clear([Drawing.Color]::Transparent)
    $r = [int]($s * 0.18); $pad = [Math]::Max(1, [int]($s * 0.04)); $w = $s - 2 * $pad
    $chip = New-Object Drawing.Drawing2D.GraphicsPath
    $chip.AddArc($pad, $pad, $r, $r, 180, 90); $chip.AddArc($pad + $w - $r, $pad, $r, $r, 270, 90)
    $chip.AddArc($pad + $w - $r, $pad + $w - $r, $r, $r, 0, 90); $chip.AddArc($pad, $pad + $w - $r, $r, $r, 90, 90); $chip.CloseFigure()
    $g.FillPath((New-Object Drawing.SolidBrush ([Drawing.Color]::FromArgb(255, 10, 12, 16))), $chip)
    $g.DrawPath((New-Object Drawing.Pen ([Drawing.Color]::FromArgb(255, 120, 170, 220)), ([Math]::Max(1, $s * 0.05))), $chip)

    # the anvil: horn on the left, flat face, narrow waist, wide foot
    $pts = @(@(0.12, 0.57), @(0.80, 0.57), @(0.80, 0.66), @(0.65, 0.68), @(0.61, 0.76), @(0.75, 0.84),
             @(0.29, 0.84), @(0.43, 0.76), @(0.39, 0.68), @(0.25, 0.66)) |
           ForEach-Object { New-Object Drawing.PointF ([float]($_[0] * $s)), ([float]($_[1] * $s)) }
    $g.FillPolygon((New-Object Drawing.SolidBrush ([Drawing.Color]::FromArgb(255, 150, 185, 220))), [Drawing.PointF[]]$pts)

    # the hammer, tilted, its head just above the anvil's face
    $g.TranslateTransform([float]($s * 0.50), [float]($s * 0.42)); $g.RotateTransform(35)
    $g.FillRectangle((New-Object Drawing.SolidBrush ([Drawing.Color]::FromArgb(255, 95, 125, 165))),
        [float](-0.03 * $s), [float](-0.40 * $s), [float](0.06 * $s), [float](0.36 * $s))
    $g.FillRectangle((New-Object Drawing.SolidBrush ([Drawing.Color]::FromArgb(255, 120, 170, 220))),
        [float](-0.14 * $s), [float](-0.07 * $s), [float](0.28 * $s), [float](0.13 * $s))
    $g.ResetTransform()

    # two sparks (a nod to Firestarter)
    if ($s -ge 32) {
        $spark = New-Object Drawing.SolidBrush ([Drawing.Color]::FromArgb(255, 255, 160, 70))
        $d = [float]($s * 0.06)
        $g.FillEllipse($spark, [float]($s * 0.22), [float]($s * 0.45), $d, $d)
        $g.FillEllipse($spark, [float]($s * 0.74), [float]($s * 0.47), [float]($d * 0.8), [float]($d * 0.8))
    }
    $g.Dispose()
    $ms = New-Object IO.MemoryStream; $bmp.Save($ms, [Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()
    , $ms.ToArray()
}
$out = New-Object IO.MemoryStream; $bw = New-Object IO.BinaryWriter $out
$bw.Write([UInt16]0); $bw.Write([UInt16]1); $bw.Write([UInt16]$sizes.Count)
$offset = 6 + 16 * $sizes.Count
for ($i = 0; $i -lt $sizes.Count; $i++) {
    $d = if ($sizes[$i] -ge 256) { 0 } else { $sizes[$i] }
    $bw.Write([byte]$d); $bw.Write([byte]$d); $bw.Write([byte]0); $bw.Write([byte]0)
    $bw.Write([UInt16]1); $bw.Write([UInt16]32); $bw.Write([UInt32]$pngs[$i].Length); $bw.Write([UInt32]$offset)
    $offset += $pngs[$i].Length
}
foreach ($p in $pngs) { $bw.Write($p) }
New-Item -ItemType Directory -Force "$A\icons" | Out-Null
[IO.File]::WriteAllBytes($ico, $out.ToArray())
"made $ico"

# --- build
& $csc /nologo /target:winexe /optimize+ /define:SMITHY "/win32icon:$ico" "/out:$A\app\Smithy.exe" `
    /r:System.Windows.Forms.dll /r:System.Drawing.dll /r:System.Web.Extensions.dll "$A\app\WorkerApp.cs" "$A\app\WindowFit.cs"
if ($LASTEXITCODE -ne 0) { throw "Build failed" }
"built $A\app\Smithy.exe"

# --- shortcuts
$ws = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
    $lnk = $ws.CreateShortcut("$dir\The Smithy.lnk")
    $lnk.TargetPath = "$A\app\Smithy.exe"
    $lnk.WorkingDirectory = $A
    $lnk.IconLocation = "$A\app\Smithy.exe,0"
    $lnk.Description = "Othos coding worker: open to start, close the window to stop"
    $lnk.Save()
    "made $dir\The Smithy.lnk"
}
