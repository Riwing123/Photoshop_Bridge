param(
  [string]$PythonExe = ""
)

$ErrorActionPreference = "Continue"
$BackendDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkspaceDir = Split-Path -Parent $BackendDir
$VenvPython = Join-Path $WorkspaceDir ".venv\Scripts\python.exe"
if (-not $PythonExe) {
  if (Test-Path -LiteralPath $VenvPython) {
    $PythonExe = $VenvPython
  } else {
    $PythonExe = "C:\Users\yfy25\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
  }
}
$LogPath = Join-Path $BackendDir "runtime\ps-agent-backend.log"
$ErrLogPath = Join-Path $BackendDir "runtime\ps-agent-backend.stderr.log"
$LauncherLogPath = Join-Path $BackendDir "runtime\ps-agent-backend-launcher.log"
$env:MPLCONFIGDIR = Join-Path $BackendDir "runtime\matplotlib"
$env:GLOG_minloglevel = "2"
$env:ABSL_LOGGING_MIN_LOG_LEVEL = "2"
$env:TF_CPP_MIN_LOG_LEVEL = "2"
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
New-Item -ItemType Directory -Force -Path $env:MPLCONFIGDIR | Out-Null
"[$(Get-Date -Format o)] starting backend with $PythonExe" | Out-File -FilePath $LogPath -Append -Encoding utf8
Set-Location -LiteralPath $WorkspaceDir
$AppPath = Join-Path $BackendDir "app.py"
$proc = Start-Process `
  -FilePath $PythonExe `
  -ArgumentList @($AppPath) `
  -WorkingDirectory $WorkspaceDir `
  -WindowStyle Hidden `
  -RedirectStandardOutput $LogPath `
  -RedirectStandardError $ErrLogPath `
  -PassThru
"[$(Get-Date -Format o)] backend process launched pid=$($proc.Id) stderr=$ErrLogPath" | Out-File -FilePath $LauncherLogPath -Append -Encoding utf8
