# Othos doctor: runs agent\doctor.ps1, which checks the set-up and changes nothing.
#   powershell -ExecutionPolicy Bypass -File doctor.ps1
& (Join-Path $PSScriptRoot "agent\doctor.ps1") @args
exit $LASTEXITCODE
