#requires -version 5.1
<#
.SYNOPSIS
    Set up tokmon as a sync client on Windows.

.DESCRIPTION
    Installs tokmon in a venv, writes %USERPROFILE%\.tokmon\sync.toml, and
    registers a Task Scheduler entry that runs `tokmon push` every 10 minutes.
    The push runs through a hidden wscript wrapper so no console window flashes
    on the desktop; output is appended to %USERPROFILE%\.tokmon\sync.log.

    Prerequisites (script will check and tell you what's missing):
      - Python 3.11 or newer (winget install Python.Python.3.12)
      - OpenSSH client (built-in on Windows 10/11; otherwise:
        Settings > Apps > Optional features > OpenSSH Client)
      - rsync AND ssh from MSYS2 (the native Windows OpenSSH cannot carry
        rsync's protocol stream, so tokmon uses the MSYS2 ssh):
          winget install --id MSYS2.MSYS2 -e
          C:\msys64\usr\bin\pacman -S --noconfirm rsync openssh
        Then add C:\msys64\usr\bin to PATH so rsync is found.

    Run from the repo root, in a regular (non-admin) PowerShell:
      .\deploy\setup-client-windows.ps1 -PiUser edgecaser -PiHost pi-gateway -PiPath /home/edgecaser

    Or set env vars and just run .\deploy\setup-client-windows.ps1:
      $env:TOKMON_PI_USER  = "edgecaser"
      $env:TOKMON_PI_HOST  = "pi-gateway"
      $env:TOKMON_PI_PATH  = "/home/edgecaser"

.PARAMETER PiUser
    SSH user on the Pi.

.PARAMETER PiHost
    SSH hostname (Tailscale name preferred, e.g. "pi-gateway").

.PARAMETER PiPath
    Absolute home path on the Pi where ~/sync/ and ~/.tokmon/ live.

.PARAMETER IntervalMinutes
    How often the scheduled task fires. Default 10.
#>
[CmdletBinding()]
param(
    [string]$PiUser = $env:TOKMON_PI_USER,
    [string]$PiHost = $env:TOKMON_PI_HOST,
    [string]$PiPath = $env:TOKMON_PI_PATH,
    [int]$IntervalMinutes = 10
)

$ErrorActionPreference = 'Stop'

function Write-Step($msg)  { Write-Host "▶ $msg" -ForegroundColor Cyan }
function Write-OK($msg)    { Write-Host "  $msg ✓" -ForegroundColor Green }
function Write-Fail($msg)  { Write-Host "  $msg" -ForegroundColor Red }

if (-not $PiUser -or -not $PiHost -or -not $PiPath) {
    Write-Fail "PiUser, PiHost, and PiPath are required."
    Write-Host 'Usage: .\deploy\setup-client-windows.ps1 -PiUser <u> -PiHost <h> -PiPath /home/<u>'
    exit 1
}

Write-Step "tokmon Windows client setup"

# --- 1. Prerequisites ---------------------------------------------------------
Write-Step "checking prerequisites"

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $python) {
    Write-Fail "Python not on PATH. Install Python 3.11+: winget install Python.Python.3.12"
    exit 1
}
$pyVer = & $python.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$verParts = $pyVer -split '\.'
if ([int]$verParts[0] -lt 3 -or ([int]$verParts[0] -eq 3 -and [int]$verParts[1] -lt 11)) {
    Write-Fail "Python ${pyVer} is too old. Need 3.11+."
    exit 1
}
Write-OK "python ${pyVer} at $($python.Source)"

$ssh = Get-Command ssh -ErrorAction SilentlyContinue
if (-not $ssh) {
    Write-Fail 'ssh not on PATH. Enable OpenSSH Client in: Settings, Apps, Optional features.'
    exit 1
}
Write-OK "ssh at $($ssh.Source)"

$rsync = Get-Command rsync -ErrorAction SilentlyContinue
if (-not $rsync) {
    Write-Fail 'rsync not on PATH.'
    Write-Host '  Install MSYS2, then add rsync and ssh inside it:'
    Write-Host '    winget install --id MSYS2.MSYS2 -e'
    Write-Host '    C:\msys64\usr\bin\pacman -S --noconfirm rsync openssh'
    Write-Host '  Then add C:\msys64\usr\bin to your PATH and re-run this script.'
    exit 1
}
Write-OK "rsync at $($rsync.Source)"

# The MSYS2/Cygwin rsync cannot drive native Windows OpenSSH (the protocol
# stream closes with 0 bytes, rsync error 12). tokmon points rsync at the
# MSYS2/Cygwin ssh (/usr/bin/ssh), so that ssh must sit next to rsync.
$rsyncDir = Split-Path -Parent $rsync.Source
$isUnixRsync = (Test-Path (Join-Path $rsyncDir 'msys-2.0.dll')) -or `
               (Test-Path (Join-Path $rsyncDir 'cygwin1.dll'))
if ($isUnixRsync) {
    if (-not (Test-Path (Join-Path $rsyncDir 'ssh.exe'))) {
        Write-Fail 'rsync is the MSYS2/Cygwin build but its ssh is missing.'
        Write-Host "    Install it:  $rsyncDir\pacman -S --noconfirm openssh"
        exit 1
    }
    Write-OK "ssh for rsync at $rsyncDir\ssh.exe"
}

# --- 2. SSH reachability ------------------------------------------------------
Write-Step "testing SSH to $PiUser@$PiHost"
$sshTest = & ssh -o BatchMode=yes -o ConnectTimeout=5 "$PiUser@$PiHost" 'echo ok' 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Fail 'SSH failed. Ensure Tailscale is up and your key is authorized on the Pi.'
    $keyHint = 'type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh ' + $PiUser + '@' + $PiHost + ' "cat >> ~/.ssh/authorized_keys"'
    Write-Host "  To copy your key: $keyHint"
    exit 1
}
Write-OK "SSH"

# --- 3. Venv + install --------------------------------------------------------
$repoRoot  = Split-Path -Parent $PSScriptRoot
$venvDir   = Join-Path $repoRoot ".venv"
$venvPy    = Join-Path $venvDir "Scripts\python.exe"
$venvTokmon = Join-Path $venvDir "Scripts\tokmon.exe"

if (-not (Test-Path $venvPy)) {
    Write-Step "creating venv at $venvDir"
    & $python.Source -m venv $venvDir
}
Write-Step "installing tokmon (editable)"
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet -e $repoRoot
if (-not (Test-Path $venvTokmon)) {
    Write-Fail "tokmon.exe not produced — pip install may have failed."
    exit 1
}
Write-OK "tokmon installed at $venvTokmon"

# --- 4. Write sync.toml -------------------------------------------------------
Write-Step "writing sync config"
& $venvTokmon sync set --pi-user $PiUser --pi-host $PiHost --pi-path $PiPath

# --- 5. Register scheduled task -----------------------------------------------
Write-Step "registering Task Scheduler entry"
$taskName = "tokmon-sync"

# Remove prior entry if present so this is idempotent
Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | ForEach-Object {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

$logDir = Join-Path $env:USERPROFILE ".tokmon"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$logPath = Join-Path $logDir "sync.log"

# Launch the push through a hidden wscript wrapper. Running tokmon.exe (a console
# app) directly from the task flashes a terminal on the desktop every interval.
# wscript.exe has no console of its own and starts the command with SW_HIDE, so
# nothing paints. Output appends to the log and the push exit code is propagated
# back so the task's Last Run Result still reflects success/failure.
$launcher = Join-Path $logDir "tokmon-sync.vbs"
$vbs = @"
' tokmon sync launcher (generated by setup-client-windows.ps1)
' Runs tokmon push with no console window so the scheduled task never flashes a
' terminal on the desktop. stdout/stderr append to the log; the push exit code is
' propagated so the task's Last Run Result still reflects success/failure.
Option Explicit
Dim exe, log, cmd, rc
exe = "$venvTokmon"
log = "$logPath"
cmd = "cmd /s /c " & """" & """" & exe & """" & " push >> " & """" & log & """" & " 2>&1" & """"
rc = CreateObject("WScript.Shell").Run(cmd, 0, True)
WScript.Quit rc
"@
Set-Content -Path $launcher -Value $vbs -Encoding ASCII

$action = New-ScheduledTaskAction `
    -Execute "wscript.exe" `
    -Argument "//B //Nologo `"$launcher`"" `
    -WorkingDirectory $repoRoot

$trigger = New-ScheduledTaskTrigger `
    -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Push ~/.claude/projects to the tokmon Pi every $IntervalMinutes min" | Out-Null

Write-OK "scheduled task '$taskName' registered (every $IntervalMinutes min)"

# --- 6. First push ------------------------------------------------------------
Write-Step "running first push"
& $venvTokmon push
if ($LASTEXITCODE -ne 0) {
    Write-Fail "First push failed with exit $LASTEXITCODE — check rsync output above."
    exit $LASTEXITCODE
}
Write-OK "initial sync"

Write-Host ""
Write-Step "done"
Write-Host "    Pi dashboard:    http://${PiHost}:8765/"
Write-Host "    Force a push:    $venvTokmon push"
Write-Host "    Task status:     Get-ScheduledTask -TaskName tokmon-sync"
Write-Host '    Remove task:     Unregister-ScheduledTask -TaskName tokmon-sync -Confirm:$false'
Write-Host "    Log (if any):    $logPath"
