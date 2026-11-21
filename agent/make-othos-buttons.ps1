Add-Type -AssemblyName System.Drawing
$IconDir = Join-Path $PSScriptRoot "icons"
New-Item -ItemType Directory -Force -Path $IconDir | Out-Null

# One .ico holding PNG images at several sizes (Windows Vista+ reads PNG-in-ICO)
function Make-Icon($path, [scriptblock]$drawSymbol) {
    $sizes = 256, 48, 32, 16
    $pngs = foreach ($s in $sizes) {
        $bmp = New-Object Drawing.Bitmap $s, $s
        $g = [Drawing.Graphics]::FromImage($bmp)
        $g.SmoothingMode = 'AntiAlias'
        $g.Clear([Drawing.Color]::Transparent)
        # dark chip with a green border, like the Circuit Board
        $r = [int]($s * 0.18); $pad = [Math]::Max(1, [int]($s * 0.04)); $w = $s - 2 * $pad
        $gp = New-Object Drawing.Drawing2D.GraphicsPath
        $gp.AddArc($pad, $pad, $r, $r, 180, 90); $gp.AddArc($pad + $w - $r, $pad, $r, $r, 270, 90)
        $gp.AddArc($pad + $w - $r, $pad + $w - $r, $r, $r, 0, 90); $gp.AddArc($pad, $pad + $w - $r, $r, $r, 90, 90)
        $gp.CloseFigure()
        $g.FillPath((New-Object Drawing.SolidBrush ([Drawing.Color]::FromArgb(255, 10, 22, 16))), $gp)
        $g.DrawPath((New-Object Drawing.Pen ([Drawing.Color]::FromArgb(255, 61, 220, 132)), ([Math]::Max(1, $s * 0.05))), $gp)
        & $drawSymbol $g $s
        $g.Dispose()
        $ms = New-Object IO.MemoryStream
        $bmp.Save($ms, [Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()
        , $ms.ToArray()
    }
    $out = New-Object IO.MemoryStream
    $bw = New-Object IO.BinaryWriter $out
    $bw.Write([UInt16]0); $bw.Write([UInt16]1); $bw.Write([UInt16]$sizes.Count)
    $offset = 6 + 16 * $sizes.Count
    for ($i = 0; $i -lt $sizes.Count; $i++) {
        $s = $sizes[$i]; $d = if ($s -ge 256) { 0 } else { $s }
        $bw.Write([byte]$d); $bw.Write([byte]$d); $bw.Write([byte]0); $bw.Write([byte]0)
        $bw.Write([UInt16]1); $bw.Write([UInt16]32)
        $bw.Write([UInt32]$pngs[$i].Length); $bw.Write([UInt32]$offset)
        $offset += $pngs[$i].Length
    }
    foreach ($p in $pngs) { $bw.Write($p) }
    [IO.File]::WriteAllBytes($path, $out.ToArray())
}

$green = [Drawing.Color]::FromArgb(255, 61, 220, 132)
$gold  = [Drawing.Color]::FromArgb(255, 231, 195, 104)

# Tray icons: a ring "O" on the chip, green when everything is up, gold when part of Othos is down
foreach ($t in @(@{f = "othos-tray"; c = $green}, @{f = "othos-tray-warn"; c = $gold})) {
    $col = $t.c
    Make-Icon "$IconDir\$($t.f).ico" {
        param($g, $s)
        $pen = New-Object Drawing.Pen $col, ([float][Math]::Max(1.5, $s * 0.12))
        $g.DrawEllipse($pen, [float]($s * 0.28), [float]($s * 0.28), [float]($s * 0.44), [float]($s * 0.44))
    }.GetNewClosure()
}

# Shortcuts: build-othos-app.ps1 makes the one "Othos" shortcut (Othos.exe).
# The separate Start Othos / Stop Othos shortcuts and their icons were retired on 2026-11-08.
