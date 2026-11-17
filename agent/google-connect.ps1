# Opens the Connect Google window: connect or disconnect a Google account for Othos (calendar read-only for now).
#   powershell -ExecutionPolicy Bypass -File google-connect.ps1
$A  = $PSScriptRoot
$Pw = Join-Path $A ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Pw)) { Write-Host "No .venv found. Run setup.ps1 first."; exit 1 }
Start-Process -FilePath $Pw -ArgumentList @((Join-Path $A "google_window.py")) -WorkingDirectory $A
