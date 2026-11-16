# Opens the "Add documents" window: put PDFs, .txt and .md files into the local library without the shell.
#   powershell -ExecutionPolicy Bypass -File library-add.ps1
# (Double-click-friendly: no console window stays open.)
$A  = $PSScriptRoot
$Pw = Join-Path $A ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Pw)) { Write-Host "No .venv found. Run setup.ps1 first."; exit 1 }
Start-Process -FilePath $Pw -ArgumentList @((Join-Path $A "library_window.py")) -WorkingDirectory $A
