# Pam installer for Windows (PowerShell 5.1 and 7).
# No admin rights, no execution policy change, no registry edits, no Docker.
# Re-running upgrades Pam in place and never touches the data directory.
# This script never asks for or handles secrets. Enter credentials in the web wizard.
#
# One-liner:
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/OWNER/pam-pm/main/install.ps1 | iex"
#
# Parameters:
#   -Version     Release tag to install (default: $DefaultVersion, update when tagging)
#   -Ref         Install from a branch instead, e.g. -Ref main
#   -Source      Local path or URL to install from (for testing)
#   -NoStart     Install but do not start Pam
#   -NoAutostart Do not enable start on login
#   -NoDesktop   Skip the desktop window and tray icon (browser only)
#   -Uninstall   Remove Pam, keeping the data directory
#   -RemoveData  With -Uninstall only: also delete the data directory (asks first)
#   -DryRun      Print the commands without running them

param(
    [string]$Version = "",
    [string]$Ref = "",
    [string]$Source = "",
    [switch]$NoStart,
    [switch]$NoAutostart,
    [switch]$NoDesktop,
    [switch]$Uninstall,
    [switch]$RemoveData,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# Default release tag. Update this constant when tagging a release.
# Mirrors the version in pyproject.toml and src/pam_pm/__init__.py (__version__).
$DefaultVersion = "v0.2.0"
$UvInstallScript = "https://astral.sh/uv/install.ps1"
$RepoArchiveBase = "https://github.com/OWNER/pam-pm/archive/refs"

function Show-Step([string]$Message) {
    Write-Output ""
    Write-Output "==> $Message"
}

function Stop-Fail {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '', Justification='Red color highlights the fatal error for the user.')]
    [CmdletBinding(SupportsShouldProcess=$true)]
    param([string]$Message)
    Write-Output ""
    Write-Host "FAILED: $Message" -ForegroundColor Red
    if ($PSCmdlet.ShouldProcess($Message, "Exit script")) {
        exit 1
    }
}

function Get-UvBinDir() {
    if (-not [string]::IsNullOrEmpty($env:USERPROFILE)) {
        return Join-Path $env:USERPROFILE ".local\bin"
    }
    return Join-Path $HOME ".local/bin"
}

function Get-PamExe() {
    $exeName = "pam"
    if ($env:OS -eq "Windows_NT") {
        $exeName = "pam.exe"
    }
    return Join-Path (Get-UvBinDir) $exeName
}

function Get-DataDir() {
    if ($null -ne $env:PAM_HOME -and $env:PAM_HOME.Trim() -ne "") {
        return $env:PAM_HOME
    }
    if (-not [string]::IsNullOrEmpty($env:LOCALAPPDATA)) {
        return Join-Path $env:LOCALAPPDATA "pam-pm"
    }
    $base = $env:USERPROFILE
    if ([string]::IsNullOrEmpty($base)) { $base = $HOME }
    return Join-Path $base "AppData\Local\pam-pm"
}

function Invoke-Step([string]$Label, [scriptblock]$Action, [string]$DryText) {
    if ($DryRun) {
        Write-Output "  [dry run] $DryText"
        return
    }
    Write-Output "  $Label"
    & $Action
}

if ($RemoveData -and -not $Uninstall) {
    Stop-Fail "-RemoveData only works together with -Uninstall."
}

if ($Uninstall) {
    Show-Step "Removing Pam (your data stays in place)."
    Invoke-Step -Label "Running: uv tool uninstall pam-pm" -Action {
        & uv tool uninstall pam-pm
    } -DryText "uv tool uninstall pam-pm"
    if ($RemoveData) {
        $dataDir = Get-DataDir
        if ($DryRun) {
            Write-Output "  [dry run] Ask for confirmation, then remove $dataDir"
        } else {
            $answer = Read-Host "Delete all Pam data in $dataDir ? Type YES to confirm"
            if ($answer -eq "YES") {
                if (Test-Path $dataDir) {
                    Remove-Item -Recurse -Force $dataDir
                    Write-Output "  Data directory removed."
                } else {
                    Write-Output "  Data directory not found, nothing to remove."
                }
            } else {
                Write-Output "  Keeping the data directory."
            }
        }
    } else {
        Write-Output "  Data kept in $(Get-DataDir)."
    }
    Write-Output ""
    Write-Output "Pam is uninstalled."
    exit 0
}

if ($Version -eq "") { $Version = $DefaultVersion }

$InstallSource = $Source
if ($InstallSource -eq "" -and $Ref -ne "") {
    $InstallSource = "$RepoArchiveBase/heads/$Ref.zip"
}
if ($InstallSource -eq "") {
    $InstallSource = "$RepoArchiveBase/tags/$Version.zip"
}

Show-Step "Step 1 of 4: Checking for uv (the Python tool installer)."
$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if ($null -eq $uvCmd) {
    Invoke-Step -Label "uv not found, installing it into your profile." -Action {
        $uvInstaller = Join-Path ([System.IO.Path]::GetTempPath()) "uv-install.ps1"
        Invoke-RestMethod -Uri $UvInstallScript -OutFile $uvInstaller
        try {
            & $uvInstaller
        } finally {
            Remove-Item -Path $uvInstaller -Force
        }
    } -DryText "irm $UvInstallScript | iex"
    $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
    if (($null -eq $uvCmd) -and (-not $DryRun)) {
        $candidate = Join-Path (Get-UvBinDir) "uv.exe"
        if (Test-Path $candidate) {
            $env:Path = "$(Get-UvBinDir);$env:Path"
            $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
        }
    }
    if (($null -eq $uvCmd) -and (-not $DryRun)) {
        Stop-Fail "uv installed but was not found. Open a new PowerShell window and run this script again."
    }
} else {
    Write-Output "  uv is already installed."
}

Show-Step "Step 2 of 4: Making uv available in this window."
if ($DryRun) {
    Write-Output "  [dry run] Add $(Get-UvBinDir) to session Path"
} else {
    $uvBin = Get-UvBinDir
    if ($env:Path -notlike "*$uvBin*") {
        $env:Path = "$uvBin;$env:Path"
    }
    Write-Output "  Done."
}

$DesktopWanted = -not $NoDesktop
$PamSpec = "pam-pm @ $InstallSource"
if ($DesktopWanted) {
    $PamSpec = "pam-pm[desktop] @ $InstallSource"
}

Show-Step "Step 3 of 4: Installing Pam from $InstallSource."
Write-Output "  Re-running later upgrades Pam in place. Your data is never touched."
Invoke-Step -Label "Running: uv tool install --python 3.12 --force ""$PamSpec""" -Action {
    & uv tool install --python 3.12 --force $PamSpec
} -DryText "uv tool install --python 3.12 --force ""$PamSpec"""

Invoke-Step -Label "Running: uv tool update-shell" -Action {
    & uv tool update-shell
} -DryText "uv tool update-shell"

if (-not $NoAutostart) {
    $pamAutostartExe = Get-PamExe
    Invoke-Step -Label "Running: $pamAutostartExe autostart on" -Action {
        if (Test-Path $pamAutostartExe) {
            & $pamAutostartExe autostart on
        } else {
            Write-Output "  pam not found yet, skipping autostart. Run 'pam autostart on' later."
        }
    } -DryText "$pamAutostartExe autostart on"
}
if ($DesktopWanted) {
    Write-Output "  Desktop window and tray icon installed. The console opens in the Pam window."
} else {
    Write-Output "  Browser-only install. The console opens in your browser."
}

$StartCommand = "start"
if ($DesktopWanted) {
    $StartCommand = "desktop"
}

if ($NoStart) {
    Write-Output ""
    Write-Output "Pam is installed. Start it any time with: pam $StartCommand"
    exit 0
}

if ($DesktopWanted) {
    Show-Step "Step 4 of 4: Starting Pam and opening the setup console in the Pam window."
} else {
    Show-Step "Step 4 of 4: Starting Pam and opening the setup console in your browser."
}
$pamExe = Get-PamExe
Invoke-Step -Label "Running: $pamExe $StartCommand" -Action {
    if (-not (Test-Path $pamExe)) {
        Stop-Fail "Pam installed but $pamExe was not found. Open a new terminal and run: pam $StartCommand"
    }
    & $pamExe $StartCommand
} -DryText "$pamExe $StartCommand"

Write-Output ""
if ($DesktopWanted) {
    Write-Output "The Pam window opens the setup console. Follow the wizard."
} else {
    Write-Output "Your browser opens the Pam setup console. Follow the wizard."
}
