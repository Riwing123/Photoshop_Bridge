param(
  [string]$PythonExe = "",
  [string]$ModelPath = "",
  [string]$Config = "configs/sam2.1/sam2.1_hiera_b+.yaml"
)

$ErrorActionPreference = "Continue"
$BackendDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkspaceDir = Split-Path -Parent $BackendDir
$SamPython = Join-Path $WorkspaceDir ".venv-sam\Scripts\python.exe"
if (-not $PythonExe) {
  $PythonExe = $SamPython
}
if (-not $ModelPath) {
  $ModelPath = Join-Path $BackendDir "models\sam2\sam2.1_hiera_base_plus.pt"
}
$LogPath = Join-Path $BackendDir "runtime\sam-worker.log"
$env:PS_AGENT_SAM_MODEL_PATH = $ModelPath
$env:PS_AGENT_SAM_CONFIG = $Config
$env:PS_AGENT_SAM_HOST = "127.0.0.1"
$env:PS_AGENT_SAM_PORT = "17861"
$env:PYTHONUNBUFFERED = "1"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogPath) | Out-Null
"[$(Get-Date -Format o)] starting SAM worker with $PythonExe" | Out-File -FilePath $LogPath -Append -Encoding utf8
Set-Location -LiteralPath $WorkspaceDir
$WorkerPath = Join-Path $BackendDir "sam_worker.py"
$Command = "cd /d ""$WorkspaceDir"" && ""$PythonExe"" ""$WorkerPath"" >> ""$LogPath"" 2>&1"
cmd.exe /c $Command
"[$(Get-Date -Format o)] SAM worker exited with code $LASTEXITCODE" | Out-File -FilePath $LogPath -Append -Encoding utf8
