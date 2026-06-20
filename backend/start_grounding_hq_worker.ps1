param(
  [string]$PythonExe = "",
  [string]$GroundingDinoModelPath = "",
  [string]$GroundingDinoConfigPath = "",
  [string]$HQSamModelPath = "",
  [string]$HQSamModelType = "vit_l"
)

$ErrorActionPreference = "Continue"
$BackendDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkspaceDir = Split-Path -Parent $BackendDir
$SamPython = Join-Path $WorkspaceDir ".venv-sam\Scripts\python.exe"
if (-not $PythonExe) {
  $PythonExe = $SamPython
}
if (-not $GroundingDinoModelPath) {
  $GroundingDinoModelPath = Join-Path $BackendDir "models\grounding_dino\groundingdino_swint_ogc.pth"
}
if (-not $GroundingDinoConfigPath) {
  $GroundingDinoConfigPath = Join-Path $BackendDir "models\grounding_dino\GroundingDINO_SwinT_OGC.py"
}
if (-not $HQSamModelPath) {
  $HQSamModelPath = Join-Path $BackendDir "models\sam_hq\sam_hq_vit_l.pth"
}
$LogPath = Join-Path $BackendDir "runtime\grounding-hq-worker.log"
$ErrLogPath = Join-Path $BackendDir "runtime\grounding-hq-worker.err.log"
$LauncherLogPath = Join-Path $BackendDir "runtime\grounding-hq-worker-launcher.log"
$env:PS_AGENT_GROUNDING_HQ_HOST = "127.0.0.1"
$env:PS_AGENT_GROUNDING_HQ_PORT = "17862"
$env:PS_AGENT_GROUNDING_DINO_MODEL_PATH = $GroundingDinoModelPath
$env:PS_AGENT_GROUNDING_DINO_CONFIG_PATH = $GroundingDinoConfigPath
$env:PS_AGENT_GROUNDING_DEVICE = "auto"
$env:PS_AGENT_HQSAM_MODEL_PATH = $HQSamModelPath
$env:PS_AGENT_HQSAM_MODEL_TYPE = $HQSamModelType
$env:PS_AGENT_HQSAM_DEVICE = "auto"
$env:PYTHONUNBUFFERED = "1"
$PathValue = [Environment]::GetEnvironmentVariable("Path", "Process")
if (-not $PathValue) {
  $PathValue = [Environment]::GetEnvironmentVariable("PATH", "Process")
}
Remove-Item Env:PATH -ErrorAction SilentlyContinue
Remove-Item Env:Path -ErrorAction SilentlyContinue
if ($PathValue) {
  $env:Path = $PathValue
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogPath) | Out-Null
"[$(Get-Date -Format o)] starting Grounding HQ worker with $PythonExe" | Out-File -FilePath $LogPath -Append -Encoding utf8
Set-Location -LiteralPath $WorkspaceDir
$WorkerPath = Join-Path $BackendDir "grounding_hq_worker.py"
$Process = Start-Process -FilePath $PythonExe -ArgumentList @($WorkerPath) -WorkingDirectory $WorkspaceDir -WindowStyle Hidden -RedirectStandardOutput $LogPath -RedirectStandardError $ErrLogPath -PassThru
"[$(Get-Date -Format o)] launched Grounding HQ worker process $($Process.Id)" | Out-File -FilePath $LauncherLogPath -Append -Encoding utf8
