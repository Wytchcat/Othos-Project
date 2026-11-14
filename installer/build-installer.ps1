# build-installer.ps1
# Version 11 - Build OthosSetup.exe using the system C# compiler
# ---------------------------------------------------------
# This script locates csc.exe (Framework64 then Framework v4.0.30319),
# compiles OthosSetup.cs into a WinExe, and prints the resulting path.
# It does not copy resources, ask for input, or launch anything.

# Locate csc.exe
$cscPath = $null
$possible = @(
    Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    Join-Path $env:SystemRoot 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
)
foreach ($p in $possible) {
    if (Test-Path $p) {
        $cscPath = $p
        break
    }
}
if (-not $cscPath) {
    Write-Error "csc.exe not found. Require .NET Framework v4.0."
    exit 1
}

# Verify source file exists
$sourceFile = Join-Path $PSScriptRoot 'OthosSetup.cs'
if (-not (Test-Path $sourceFile)) {
    Write-Error "Source file OthosSetup.cs not found in $PSScriptRoot."
    exit 1
}

# Define output path
$outputExe = Join-Path $PSScriptRoot 'OthosSetup.exe'

# Build argument list
$buildArgs = @(
    '/target:winexe'                # Windows GUI app
    '/optimize'                     # Enable optimizations
    '/nologo'                       # Suppress compiler banner
    '/r:System.Windows.Forms.dll'   # Reference WinForms
    '/r:System.Drawing.dll'         # Reference Drawing
    "/out:`"$outputExe`""           # Output file
    "`"$sourceFile`""               # Source file
)

Write-Host "Compiling OthosSetup.exe..."
& $cscPath @buildArgs
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "`nBuild succeeded. OthosSetup.exe created at:" -ForegroundColor Green
    Write-Host "   $outputExe"
    Write-Host "Put OthosSetup.exe in the Othos folder, next to agent and ui."
} else {
    Write-Error "`nBuild failed with exit code $exitCode."
    exit $exitCode
}
