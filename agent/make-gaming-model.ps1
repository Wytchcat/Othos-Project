# Makes the CPU-only gaming voice model "othos-gaming" in Ollama from a model you
# already have (no download). It is the same model with "never use the GPU" and a
# thread limit built in, so a game keeps the graphics card and most CPU cores.
#   powershell -ExecutionPolicy Bypass -File make-gaming-model.ps1            (llama3.2:3b, 6 threads)
#   powershell -ExecutionPolicy Bypass -File make-gaming-model.ps1 -From gemma4:e4b -Threads 4
param([string]$From = "llama3.2:3b", [int]$Threads = 6, [string]$Name = "othos-gaming")

$dir = Join-Path $env:TEMP "othos-gaming-model"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
@"
FROM $From
PARAMETER num_gpu 0
PARAMETER num_thread $Threads
"@ | Set-Content "$dir\Modelfile" -Encoding ascii

ollama create $Name -f "$dir\Modelfile"
if ($LASTEXITCODE -eq 0) { "made $Name from $From (CPU only, $Threads threads)" }
