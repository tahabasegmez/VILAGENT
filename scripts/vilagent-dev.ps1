param(
  [string]$CondaEnvPath = "D:\code\envs\win\vilagent",
  [switch]$SkipElectron,
  [switch]$SkipServiceStart,
  [int]$FrontendPort = 3000,
  [int]$GatewayPort = 8001,
  [int]$WaitSeconds = 90
)

$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$frontendRoot = Join-Path $repoRoot "frontend"
$backendRoot = Join-Path $repoRoot "backend"
$logsRoot = Join-Path $repoRoot "logs"
$gatewayLogPath = Join-Path $logsRoot "gateway.log"
$frontendLogPath = Join-Path $logsRoot "frontend.log"
$launcherLogPath = Join-Path $logsRoot "vilagent-launcher.log"

function Import-DotEnvFile([string]$Path) {
  if (-not (Test-Path $Path)) {
    return
  }
  Get-Content -Path $Path | ForEach-Object {
    $line = $_.Trim()
    if ($line.Length -eq 0 -or $line.StartsWith("#")) {
      return
    }
    $separatorIndex = $line.IndexOf("=")
    if ($separatorIndex -le 0) {
      return
    }
    $key = $line.Substring(0, $separatorIndex).Trim()
    if ($key -notmatch "^[A-Za-z_][A-Za-z0-9_]*$") {
      return
    }
    $value = $line.Substring($separatorIndex + 1).Trim()
    if (
      ($value.StartsWith('"') -and $value.EndsWith('"')) -or
      ($value.StartsWith("'") -and $value.EndsWith("'"))
    ) {
      $value = $value.Substring(1, $value.Length - 2)
    }
    if (-not [Environment]::GetEnvironmentVariable($key, "Process")) {
      [Environment]::SetEnvironmentVariable($key, $value, "Process")
    }
  }
}

Import-DotEnvFile (Join-Path $repoRoot ".env")
if (-not $env:VILAGENT_COMPUTER_USE_ARCHITECTURE) {
  $env:VILAGENT_COMPUTER_USE_ARCHITECTURE = "react_graph"
}
$textPreset = if ($env:VILAGENT_TEXT_PROVIDER_PRESET) { $env:VILAGENT_TEXT_PROVIDER_PRESET.ToLowerInvariant() } else { "gemini" }
if (-not $env:VILAGENT_TEXT_MODEL_PROVIDER) {
  $env:VILAGENT_TEXT_MODEL_PROVIDER = "api"
}
if ($textPreset -eq "glm") {
  if (-not $env:VILAGENT_TEXT_MODEL_CONFIG_NAME) {
    $env:VILAGENT_TEXT_MODEL_CONFIG_NAME = "vilagent-text-glm"
  }
  if (-not $env:VILAGENT_TEXT_MODEL_USE) {
    $env:VILAGENT_TEXT_MODEL_USE = "langchain_openai:ChatOpenAI"
  }
  if (-not $env:VILAGENT_TEXT_MODEL_NAME -and $env:VILAGENT_GLM_MODEL_NAME) {
    $env:VILAGENT_TEXT_MODEL_NAME = $env:VILAGENT_GLM_MODEL_NAME
  }
  if (-not $env:VILAGENT_TEXT_API_KEY -and $env:VILAGENT_GLM_API_KEY) {
    $env:VILAGENT_TEXT_API_KEY = $env:VILAGENT_GLM_API_KEY
  }
} else {
  if (-not $env:VILAGENT_TEXT_MODEL_CONFIG_NAME) {
    $env:VILAGENT_TEXT_MODEL_CONFIG_NAME = "vilagent-text-gemini"
  }
  if (-not $env:VILAGENT_TEXT_MODEL_USE) {
    $env:VILAGENT_TEXT_MODEL_USE = "langchain_google_genai:ChatGoogleGenerativeAI"
  }
  if (-not $env:VILAGENT_TEXT_MODEL_NAME -and $env:VILAGENT_GEMINI_MODEL_NAME) {
    $env:VILAGENT_TEXT_MODEL_NAME = $env:VILAGENT_GEMINI_MODEL_NAME
  }
  if (-not $env:VILAGENT_TEXT_API_KEY -and $env:VILAGENT_GEMINI_API_KEY) {
    $env:VILAGENT_TEXT_API_KEY = $env:VILAGENT_GEMINI_API_KEY
  }
  if (-not $env:GEMINI_API_KEY -and $env:VILAGENT_GEMINI_API_KEY) {
    $env:GEMINI_API_KEY = $env:VILAGENT_GEMINI_API_KEY
  }
  if (-not $env:VILAGENT_GEMINI_API_KEY -and $env:GEMINI_API_KEY) {
    $env:VILAGENT_GEMINI_API_KEY = $env:GEMINI_API_KEY
  }
}
if (-not $env:VILAGENT_GEMINI_MODEL_NAME) {
  $env:VILAGENT_GEMINI_MODEL_NAME = "gemini-2.5-flash"
}
if (-not $env:VILAGENT_GLM_MODEL_NAME) {
  $env:VILAGENT_GLM_MODEL_NAME = "glm-4.5-flash"
}
if (-not $env:VILAGENT_GLM_BASE_URL) {
  $env:VILAGENT_GLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
}
if ($null -eq [Environment]::GetEnvironmentVariable("VILAGENT_GEMINI_API_KEY", "Process")) {
  $env:VILAGENT_GEMINI_API_KEY = ""
}
if ($null -eq [Environment]::GetEnvironmentVariable("VILAGENT_GLM_API_KEY", "Process")) {
  $env:VILAGENT_GLM_API_KEY = ""
}
if ($null -eq [Environment]::GetEnvironmentVariable("VILAGENT_TEXT_API_KEY", "Process")) {
  $env:VILAGENT_TEXT_API_KEY = ""
}
if (-not $env:VILAGENT_TEXT_MODEL_USE) {
  $env:VILAGENT_TEXT_MODEL_USE = "langchain_google_genai:ChatGoogleGenerativeAI"
}

$activeCondaPrefix = if ($env:CONDA_PREFIX) { $env:CONDA_PREFIX } else { $CondaEnvPath }
$condaScripts = Join-Path $activeCondaPrefix "Scripts"
$condaLibraryBin = Join-Path $activeCondaPrefix "Library\bin"
$internalAuthToken = if ($env:VILAGENT_INTERNAL_AUTH_TOKEN) { $env:VILAGENT_INTERNAL_AUTH_TOKEN } else { "vilagent-local-dev-token" }
$env:VILAGENT_INTERNAL_AUTH_TOKEN = $internalAuthToken
$env:VILAGENT_INTERNAL_GATEWAY_BASE_URL = if ($env:VILAGENT_INTERNAL_GATEWAY_BASE_URL) { $env:VILAGENT_INTERNAL_GATEWAY_BASE_URL } else { "http://127.0.0.1:$GatewayPort" }

function Write-Step([string]$Message) {
  Write-Host "[VILAGENT] $Message" -ForegroundColor Cyan
}

function Write-Warn([string]$Message) {
  Write-Host "[VILAGENT] $Message" -ForegroundColor Yellow
}

function Test-Port([int]$Port) {
  try {
    $client = New-Object Net.Sockets.TcpClient
    $iar = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
    $connected = $iar.AsyncWaitHandle.WaitOne(500, $false)
    if ($connected) {
      $client.EndConnect($iar)
    }
    $client.Close()
    return $connected
  } catch {
    return $false
  }
}

function Wait-Port([int]$Port, [int]$TimeoutSeconds) {
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    if (Test-Port $Port) {
      return
    }
    Start-Sleep -Seconds 1
  }
  throw "Timed out waiting for localhost:$Port"
}

function Get-DevPortOwners([int]$Port) {
  $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
  if ($null -eq $connections) {
    return @()
  }
  return @($connections | Select-Object -ExpandProperty OwningProcess -Unique)
}

function Get-ProcessCommandLine([int]$ProcessId) {
  try {
    $escaped = [string]$ProcessId
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$escaped" -ErrorAction Stop
    return [string]$process.CommandLine
  } catch {
    return ""
  }
}

function Test-IsVilagentDevProcess([int]$ProcessId, [int]$Port) {
  $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
  if ($null -eq $process) {
    return $false
  }
  if ($Port -eq $GatewayPort) {
    return $true
  }
  if ($Port -eq $FrontendPort -and $process.ProcessName -in @("node", "pnpm", "electron")) {
    return $true
  }
  $commandLine = Get-ProcessCommandLine $ProcessId
  $haystack = "$($process.ProcessName) $commandLine"
  $repoNeedle = [Regex]::Escape([string]$repoRoot)
  if ($haystack -match $repoNeedle) {
    return $true
  }
  if ($haystack -match "uvicorn.*app\.gateway\.app:app") {
    return $true
  }
  if ($haystack -match "next(\.cmd)?\s+dev|pnpm(\.cmd)?\s+dev|electron(\.cmd)?") {
    return $true
  }
  return $false
}

function Clear-DevPortOwners([int[]]$Ports) {
  $blocked = @()
  $killed = @()
  foreach ($port in $Ports) {
    foreach ($processId in (Get-DevPortOwners $port)) {
      $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
      $name = if ($process) { $process.ProcessName } else { "unknown" }
      if ($null -eq $process) {
        Write-Warn "Port $port is reported as owned by stale PID $processId. Waiting for Windows to release it."
        Start-Sleep -Seconds 2
        $ownersAfterWait = Get-DevPortOwners $port
        if ($ownersAfterWait -notcontains $processId) {
          continue
        }
        Write-Warn "Stale PID $processId still appears in the TCP table; ignoring this stale owner for this cleanup pass."
        continue
      }
      if (Test-IsVilagentDevProcess $processId $port) {
        Write-Warn ("Stopping old VILAGENT dev process on port {0}: PID {1} ({2})." -f $port, $processId, $name)
        Stop-Process -Id $processId -Force -ErrorAction Stop
        $killed += $processId
      } else {
        Write-Warn "Port $port is in use by non-VILAGENT/unknown PID $processId ($name)."
        $blocked += "$port/PID:$processId"
      }
    }
  }
  if ($blocked.Count -gt 0) {
    throw "Required dev port(s) are used by unknown processes: $($blocked -join ', '). Stop them manually or change ports."
  }
  if ($killed.Count -gt 0) {
    Start-Sleep -Seconds 2
  }
}

function Show-PortOwner([int]$Port) {
  $owners = Get-DevPortOwners $Port
  if ($owners.Count -eq 0) {
    return $false
  }
  $hasLiveOwner = $false
  foreach ($processId in $owners) {
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
      Write-Warn "Port $Port still reports stale PID $processId; no live process was found."
      continue
    }
    $hasLiveOwner = $true
    $name = if ($process) { $process.ProcessName } else { "unknown" }
    Write-Warn "Port $Port is already in use by PID $processId ($name). Stop old VILAGENT/Next/Gateway processes before starting a clean run."
  }
  return $hasLiveOwner
}

function Show-DevPortOwners {
  $gatewayBusy = Show-PortOwner $GatewayPort
  $frontendBusy = Show-PortOwner $FrontendPort
  return $gatewayBusy -or $frontendBusy
}

function Test-ConfigReadiness([string]$ConfigPath) {
  if (-not (Test-Path $ConfigPath)) {
    return
  }
  $missingEnvRefs = @()
  Get-Content -Path $ConfigPath | ForEach-Object {
    $matches = [Regex]::Matches($_, '\$([A-Za-z_][A-Za-z0-9_]*)')
    foreach ($match in $matches) {
      $name = $match.Groups[1].Value
      if (-not [Environment]::GetEnvironmentVariable($name, "Process")) {
        $missingEnvRefs += $name
      }
    }
  }
  $missingEnvRefs = @($missingEnvRefs | Sort-Object -Unique)
  if ($missingEnvRefs.Count -gt 0) {
    Write-Warn "config.yaml references missing environment variable(s): $($missingEnvRefs -join ', ')"
    Write-Warn "Add them to .env in the repository root or replace the corresponding config.yaml value."
  }
  $modelsLine = Select-String -Path $ConfigPath -Pattern '^models:\s*$' -SimpleMatch:$false | Select-Object -First 1
  if ($modelsLine) {
    $afterModels = Get-Content -Path $ConfigPath | Select-Object -Skip $modelsLine.LineNumber -First 40
    $hasModelItem = $afterModels | Select-String -Pattern '^\s*-\s+name:\s+' | Select-Object -First 1
    if (-not $hasModelItem) {
      Write-Warn "config.yaml has no active model under models:. VILAGENT can start, but Text model health will fail until you uncomment/configure a model."
    }
  }
}

function Get-CommandPath([string]$Name) {
  $condaCandidates = @(
    (Join-Path $activeCondaPrefix $Name),
    (Join-Path $condaScripts $Name),
    (Join-Path $activeCondaPrefix "$Name.exe"),
    (Join-Path $condaScripts "$Name.exe"),
    (Join-Path $activeCondaPrefix "$Name.cmd"),
    (Join-Path $condaScripts "$Name.cmd")
  )
  foreach ($candidate in $condaCandidates) {
    if (Test-Path $candidate) {
      return (Resolve-Path $candidate).Path
    }
  }
  $command = Get-Command $Name -ErrorAction SilentlyContinue
  if ($null -eq $command) {
    return $null
  }
  return $command.Source
}

function Start-HiddenProcess(
  [string]$FilePath,
  [string[]]$ArgumentList,
  [string]$WorkingDirectory,
  [hashtable]$Environment = @{},
  [string]$StdoutPath = "",
  [string]$StderrPath = ""
) {
  $startInfo = New-Object System.Diagnostics.ProcessStartInfo
  $startInfo.FileName = $FilePath
  $startInfo.Arguments = ($ArgumentList | ForEach-Object { ConvertTo-ProcessArgument $_ }) -join " "
  $startInfo.WorkingDirectory = $WorkingDirectory
  $startInfo.UseShellExecute = $false
  $startInfo.CreateNoWindow = $true
  $startInfo.Environment["CONDA_PREFIX"] = $activeCondaPrefix
  $startInfo.Environment["PYTHONNOUSERSITE"] = "1"
  $startInfo.Environment["PYTHONPATH"] = ".;packages\harness"
  $startInfo.Environment["PYTHONIOENCODING"] = "utf-8"
  $startInfo.Environment["PYTHONUTF8"] = "1"
  if ($startInfo.Environment.ContainsKey("PYTHONHOME")) {
    $startInfo.Environment.Remove("PYTHONHOME")
  }
  $existingPath = $startInfo.Environment["PATH"]
  $startInfo.Environment["PATH"] = "$activeCondaPrefix;$condaScripts;$condaLibraryBin;$existingPath"
  if ($StdoutPath) {
    $startInfo.RedirectStandardOutput = $true
  }
  if ($StderrPath) {
    $startInfo.RedirectStandardError = $true
  }
  foreach ($key in $Environment.Keys) {
    $startInfo.Environment[$key] = [string]$Environment[$key]
  }
  $process = [System.Diagnostics.Process]::Start($startInfo)
  if ($StdoutPath) {
    $process.BeginOutputReadLine()
    Register-ObjectEvent -InputObject $process -EventName OutputDataReceived -Action {
      if ($EventArgs.Data) {
        Add-Content -Path $Event.MessageData -Value $EventArgs.Data
      }
    } -MessageData $StdoutPath | Out-Null
  }
  if ($StderrPath) {
    $process.BeginErrorReadLine()
    Register-ObjectEvent -InputObject $process -EventName ErrorDataReceived -Action {
      if ($EventArgs.Data) {
        Add-Content -Path $Event.MessageData -Value $EventArgs.Data
      }
    } -MessageData $StderrPath | Out-Null
  }
  return $process
}

function ConvertTo-ProcessArgument([string]$Value) {
  if ($Value -match '^[A-Za-z0-9_./:=+-]+$') {
    return $Value
  }
  return '"' + ($Value -replace '\\', '\\' -replace '"', '\"') + '"'
}

function Start-ServicesWithMake() {
  $makePath = Get-CommandPath "make"
  if ($null -eq $makePath) {
    return $false
  }
  Write-Step "Starting gateway and Next.js services with make dev-daemon"
  Push-Location $repoRoot
  try {
    make dev-daemon
  } finally {
    Pop-Location
  }
  return $true
}

function Start-ServicesWithGitBash() {
  $bashPath = Get-CommandPath "bash"
  if ($null -eq $bashPath) {
    return $false
  }
  if ($bashPath -like "*\Windows\system32\bash.exe") {
    Write-Warn "Found WSL bash at $bashPath, not Git Bash. Skipping serve.sh fallback."
    return $false
  }
  Write-Step "Starting gateway and Next.js services with Git Bash serve.sh"
  $process = Start-HiddenProcess -FilePath $bashPath -ArgumentList @("./scripts/serve.sh", "--dev", "--daemon", "--skip-install") -WorkingDirectory $repoRoot -StdoutPath $launcherLogPath -StderrPath $launcherLogPath
  Write-Step "Git Bash launcher PID: $($process.Id). Log: $launcherLogPath"
  return $true
}

function Start-ServicesNatively() {
  Write-Step "Starting gateway and Next.js services with native PowerShell fallback"
  New-Item -ItemType Directory -Force -Path $logsRoot | Out-Null

  $uvPath = Get-CommandPath "uv"
  $pythonPath = Get-CommandPath "python"
  if ($null -ne $uvPath) {
    $gatewayArgs = @("run", "uvicorn", "app.gateway.app:app", "--host", "127.0.0.1", "--port", "$GatewayPort", "--http", "h11", "--ws", "none")
    $gatewayProcess = Start-HiddenProcess -FilePath $uvPath -ArgumentList $gatewayArgs -WorkingDirectory $backendRoot -StdoutPath $gatewayLogPath -StderrPath $gatewayLogPath
    Write-Step "Gateway PID: $($gatewayProcess.Id). Log: $gatewayLogPath"
  } elseif ($null -ne $pythonPath) {
    Test-BackendPythonDependencies -PythonPath $pythonPath
    Write-Warn "uv is not on PATH; using python -m uvicorn. Install/sync backend deps if this fails."
    $gatewayArgs = @("-m", "uvicorn", "app.gateway.app:app", "--host", "127.0.0.1", "--port", "$GatewayPort", "--http", "h11", "--ws", "none")
    $gatewayProcess = Start-HiddenProcess -FilePath $pythonPath -ArgumentList $gatewayArgs -WorkingDirectory $backendRoot -StdoutPath $gatewayLogPath -StderrPath $gatewayLogPath
    Write-Step "Gateway PID: $($gatewayProcess.Id). Log: $gatewayLogPath"
  } else {
    throw "Neither uv nor python is available on PATH for the Gateway."
  }

  $corepackPath = Get-CommandPath "corepack"
  $pnpmPath = Get-CommandPath "pnpm"
  if ($null -ne $corepackPath) {
    $frontendProcess = Start-CmdProcess -Command "corepack pnpm dev" -WorkingDirectory $frontendRoot -StdoutPath $frontendLogPath -StderrPath $frontendLogPath
    Write-Step "Frontend PID: $($frontendProcess.Id). Log: $frontendLogPath"
  } elseif ($null -ne $pnpmPath) {
    $frontendProcess = Start-HiddenProcess -FilePath $pnpmPath -ArgumentList @("dev") -WorkingDirectory $frontendRoot -StdoutPath $frontendLogPath -StderrPath $frontendLogPath
    Write-Step "Frontend PID: $($frontendProcess.Id). Log: $frontendLogPath"
  } else {
    throw "Neither corepack nor pnpm is available on PATH for the frontend."
  }
}

function Test-BackendPythonDependencies([string]$PythonPath) {
  $packages = @("fastapi", "starlette", "pydantic", "uvicorn", "cryptography", "multipart", "websockets", "vilagent")
  $missing = @()
  $oldPath = $env:PATH
  $oldNoUserSite = $env:PYTHONNOUSERSITE
  $oldPythonPath = $env:PYTHONPATH
  $oldPythonHome = $env:PYTHONHOME
  $env:PATH = "$activeCondaPrefix;$condaScripts;$condaLibraryBin;$oldPath"
  $env:PYTHONNOUSERSITE = "1"
  $env:PYTHONPATH = "$backendRoot\packages\harness"
  Remove-Item Env:\PYTHONHOME -ErrorAction SilentlyContinue
  foreach ($package in $packages) {
    $check = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('$package') else 1)"
    $exitCode = Invoke-PythonDependencyCheck -PythonPath $PythonPath -Code $check
    if ($exitCode -ne 0) {
      $missing += $package
    }
  }
  $pyjwtCheck = "import jwt, sys; sys.exit(0 if hasattr(jwt, 'encode') and hasattr(jwt, 'decode') else 1)"
  $pyjwtExitCode = Invoke-PythonDependencyCheck -PythonPath $PythonPath -Code $pyjwtCheck
  if ($pyjwtExitCode -ne 0) {
    $missing += "PyJWT"
  }
  $pythonMultipartCheck = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('python_multipart') or importlib.util.find_spec('multipart') else 1)"
  $pythonMultipartExitCode = Invoke-PythonDependencyCheck -PythonPath $PythonPath -Code $pythonMultipartCheck
  if ($pythonMultipartExitCode -ne 0) {
    $missing += "python-multipart"
  }
  $wrongMultipartCheck = "import importlib.metadata, sys; names={d.metadata['Name'].lower() for d in importlib.metadata.distributions()}; sys.exit(1 if 'multipart' in names and 'python-multipart' not in names else 0)"
  $wrongMultipartExitCode = Invoke-PythonDependencyCheck -PythonPath $PythonPath -Code $wrongMultipartCheck
  if ($wrongMultipartExitCode -ne 0) {
    $missing += "remove-wrong-multipart"
  }
  $websocketsSyncCheck = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('websockets.sync.client') else 1)"
  $websocketsSyncExitCode = Invoke-PythonDependencyCheck -PythonPath $PythonPath -Code $websocketsSyncCheck
  if ($websocketsSyncExitCode -ne 0) {
    $missing += "websockets>=12"
  }
  $env:PATH = $oldPath
  if ($null -eq $oldNoUserSite) { Remove-Item Env:\PYTHONNOUSERSITE -ErrorAction SilentlyContinue } else { $env:PYTHONNOUSERSITE = $oldNoUserSite }
  if ($null -eq $oldPythonPath) { Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue } else { $env:PYTHONPATH = $oldPythonPath }
  if ($null -ne $oldPythonHome) { $env:PYTHONHOME = $oldPythonHome }
  if ($missing.Count -gt 0) {
    Write-Warn "Missing Python dependencies in the active environment: $($missing -join ', ')"
    Write-Warn "Install manually, for example: python -m pip uninstall -y jwt multipart; python -m pip install -e .\backend\packages\harness fastapi starlette pydantic `"uvicorn[standard]`" PyJWT cryptography python-multipart `"websockets>=12`""
    Write-Warn "For FastAPI form errors specifically: python -m pip uninstall -y multipart python-multipart; python -m pip install --no-cache-dir --force-reinstall python-multipart"
  }
}

function Invoke-PythonDependencyCheck([string]$PythonPath, [string]$Code) {
  try {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $PythonPath
    $psi.Arguments = "-c " + (ConvertTo-ProcessArgument $Code)
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::Start($psi)
    $process.WaitForExit()
    return $process.ExitCode
  } catch {
    return 1
  }
}

function Start-CmdProcess([string]$Command, [string]$WorkingDirectory, [string]$StdoutPath, [string]$StderrPath) {
  return Start-HiddenProcess -FilePath "cmd.exe" -ArgumentList @("/c", $Command) -WorkingDirectory $WorkingDirectory -StdoutPath $StdoutPath -StderrPath $StderrPath
}

function Show-LogTail([string]$Path, [int]$Lines = 40) {
  if (-not (Test-Path $Path)) {
    Write-Warn "Log not found: $Path"
    return
  }
  Write-Warn "Last $Lines lines from $Path"
  Get-Content -Path $Path -Tail $Lines | ForEach-Object {
    Write-Host $_
  }
}

function Show-MissingModuleHint([string]$Path) {
  if (-not (Test-Path $Path)) {
    return
  }
  $content = Get-Content -Path $Path -Tail 200 -ErrorAction SilentlyContinue
  $missing = $content | Select-String -Pattern "ModuleNotFoundError: No module named '([^']+)'" | Select-Object -Last 1
  if ($missing -and $missing.Matches.Count -gt 0) {
    $moduleName = $missing.Matches[0].Groups[1].Value
    Write-Warn "Detected missing Python module for Gateway: $moduleName"
    $packageName = Convert-ModuleNameToPipPackage $moduleName
    Write-Warn "Install it into this conda env, not user-site: python -m pip install $packageName"
  }
}

function Convert-ModuleNameToPipPackage([string]$ModuleName) {
  $mapping = @{
    "jwt" = "PyJWT"
    "multipart" = "python-multipart"
  }
  if ($mapping.ContainsKey($ModuleName)) {
    return $mapping[$ModuleName]
  }
  return $ModuleName
}

function Wait-PortOrShowLogs([int]$Port, [int]$TimeoutSeconds, [string[]]$LogPaths) {
  try {
    Wait-Port $Port $TimeoutSeconds
  } catch {
    Write-Warn $_.Exception.Message
    foreach ($path in $LogPaths) {
      Show-LogTail $path
      Show-MissingModuleHint $path
    }
    throw
  }
}

function Invoke-FrontendCommand([string]$Command) {
  $startInfo = New-Object System.Diagnostics.ProcessStartInfo
  $startInfo.FileName = "cmd.exe"
  $startInfo.Arguments = "/c $Command"
  $startInfo.WorkingDirectory = $frontendRoot
  $startInfo.UseShellExecute = $false
  $startInfo.CreateNoWindow = $true
  $startInfo.RedirectStandardOutput = $true
  $startInfo.RedirectStandardError = $true
  $process = [System.Diagnostics.Process]::Start($startInfo)
  $stdout = $process.StandardOutput.ReadToEnd()
  $stderr = $process.StandardError.ReadToEnd()
  $process.WaitForExit()
  return @{
    ExitCode = $process.ExitCode
    Stdout = $stdout
    Stderr = $stderr
  }
}

function Show-ElectronInstallHint {
  Write-Warn "Electron command/binary is missing or Electron install scripts did not finish."
  Write-Warn "Run these commands, then start VILAGENT again:"
  Write-Host "  cd $frontendRoot" -ForegroundColor Yellow
  Write-Host "  corepack pnpm approve-builds" -ForegroundColor Yellow
  Write-Host "  corepack pnpm rebuild electron" -ForegroundColor Yellow
  Write-Host "  corepack pnpm exec electron --version" -ForegroundColor Yellow
  Write-Warn "If the version check still fails, run 'corepack pnpm install --ignore-scripts=false' in frontend and rebuild Electron again."
}

function Test-ElectronReady {
  if (-not (Test-Path (Join-Path $frontendRoot "node_modules"))) {
    Write-Warn "frontend/node_modules is missing. Run 'corepack pnpm install' in frontend first."
    return $false
  }

  $electronCheck = Invoke-FrontendCommand "corepack pnpm exec electron --version"
  if ($electronCheck.ExitCode -eq 0) {
    $version = $electronCheck.Stdout.Trim()
    if ($version) {
      Write-Step "Electron ready: $version"
    }
    return $true
  }

  Show-ElectronInstallHint
  return $false
}

Write-Step "Repository: $repoRoot"
if ($env:CONDA_PREFIX -and ((Resolve-Path $env:CONDA_PREFIX).Path -ieq (Resolve-Path $CondaEnvPath).Path)) {
  Write-Step "Conda env active: $env:CONDA_PREFIX"
} else {
  Write-Host "[VILAGENT] Expected conda env: $CondaEnvPath" -ForegroundColor Yellow
  Write-Host "[VILAGENT] Current CONDA_PREFIX: $env:CONDA_PREFIX" -ForegroundColor Yellow
  Write-Host "[VILAGENT] Continue only if dependencies are available in the current shell." -ForegroundColor Yellow
}

$configPath = Join-Path $repoRoot "config.yaml"
if (-not (Test-Path $configPath)) {
  $configExamplePath = Join-Path $repoRoot "config.example.yaml"
  if (Test-Path $configExamplePath) {
    Copy-Item -Path $configExamplePath -Destination $configPath
    Write-Warn "config.yaml was missing, so it was copied from config.example.yaml. Fill model settings before real runs."
  } else {
    Write-Warn "config.yaml is missing. Copy config.example.yaml to config.yaml and fill model settings."
  }
}
Test-ConfigReadiness $configPath
if (-not (Test-Path (Join-Path $repoRoot ".env"))) {
  Write-Warn ".env is missing. Copy .env.example to .env and fill pyngrok/API values."
}

if (-not $SkipServiceStart) {
  Clear-DevPortOwners @($GatewayPort, $FrontendPort)
  if (Show-DevPortOwners) {
    throw "Required VILAGENT dev ports are still in use after cleanup. Stop the listed PID(s), then run scripts\vilagent-dev.ps1 again."
  }
}

if (-not $SkipServiceStart) {
  if (-not (Start-ServicesWithMake)) {
    Write-Warn "make is not available on PATH."
    if (-not (Start-ServicesWithGitBash)) {
      Write-Warn "Git Bash bash.exe is not available on PATH."
      Start-ServicesNatively
    }
  }
} else {
  Write-Step "Service startup skipped."
}

Write-Step "Waiting for Gateway localhost:$GatewayPort"
Wait-PortOrShowLogs $GatewayPort $WaitSeconds @($launcherLogPath, $gatewayLogPath)
Write-Step "Waiting for Frontend localhost:$FrontendPort"
Wait-PortOrShowLogs $FrontendPort $WaitSeconds @($launcherLogPath, $frontendLogPath)

if (-not $SkipElectron) {
  Write-Step "Opening Electron Operator"
  if (Test-ElectronReady) {
    Push-Location $frontendRoot
    try {
      $env:VILAGENT_OPERATOR_URL = "http://localhost:$FrontendPort/operator"
      corepack pnpm electron:dev
    } finally {
      Pop-Location
    }
  } else {
    Write-Warn "Electron launch skipped. The web operator is available at http://localhost:$FrontendPort/operator"
  }
} else {
  Write-Step "Electron skipped. Open http://localhost:$FrontendPort/operator"
}
