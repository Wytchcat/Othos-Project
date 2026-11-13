# Builds app\Othos.exe from app\Othos.cs with the C# compiler that comes with
# Windows, then puts one "Othos" shortcut on the Desktop and in the Start menu
# (replacing the old separate Start Othos / Stop Othos shortcuts).
# Run again after changing Othos.cs. Close the Othos window first.
$A   = $PSScriptRoot
$csc = "C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"

# The app's icon: personal.json's assistant.logo if it's an .ico file, else Othos's own.
# (The window also shows a PNG logo; the .exe and shortcut icon needs an .ico.)
$ico = "$A\icons\othos-tray.ico"
try {
    $logo = (Get-Content "$A\personal.json" -Raw -Encoding UTF8 | ConvertFrom-Json).assistant.logo
    if ($logo) {
        if (-not [IO.Path]::IsPathRooted($logo)) { $logo = Join-Path $A $logo }
        if ($logo -like "*.ico" -and (Test-Path $logo)) { $ico = $logo; "icon: $ico" }
    }
} catch { }

& $csc /nologo /target:winexe /optimize+ "/win32icon:$ico" "/out:$A\app\Othos.exe" `
    /r:System.Windows.Forms.dll /r:System.Drawing.dll "$A\app\Othos.cs"
if ($LASTEXITCODE -ne 0) { throw "Build failed" }
"built $A\app\Othos.exe"

$ws = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
    foreach ($old in "Start Othos.lnk", "Stop Othos.lnk") {
        if (Test-Path "$dir\$old") { Remove-Item "$dir\$old"; "removed $dir\$old" }
    }
    $lnk = $ws.CreateShortcut("$dir\Othos.lnk")
    $lnk.TargetPath = "$A\app\Othos.exe"
    $lnk.WorkingDirectory = $A
    $lnk.IconLocation = "$A\app\Othos.exe,0"
    $lnk.Description = "Start Othos; close its window to stop it"
    $lnk.Save()
    "made $dir\Othos.lnk"
}
