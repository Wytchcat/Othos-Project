# Othos setup: runs agent\setup.ps1 (see README.md). Run from this folder:
#   powershell -ExecutionPolicy Bypass -File setup.ps1
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -Mode offline
& (Join-Path $PSScriptRoot "agent\setup.ps1") @args
exit $LASTEXITCODE
