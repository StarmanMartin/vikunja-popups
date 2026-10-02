# Install or update vikunja-popups for the current Windows user.
# Needs MSYS2 (https://www.msys2.org), which provides GTK 3 and PyGObject.
# Safe to re-run: it updates the code, keeps the config and restarts the app
# if it is running.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Msys2Root C:\msys64] [-NoAutostart]

param(
    [string]$Msys2Root = "C:\msys64",
    [switch]$NoAutostart
)

$ErrorActionPreference = "Stop"

$AppName = "vikunja-popups"
$SourceDir = $PSScriptRoot
$AppDir = Join-Path $env:LOCALAPPDATA "Programs\$AppName"
$ConfigDir = Join-Path $env:APPDATA $AppName
$StateDir = Join-Path $env:LOCALAPPDATA $AppName
$PidFile = Join-Path $StateDir "app.pid"
$Programs = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$Startup = Join-Path $Programs "Startup"

$AppFiles = @(
    "app.py", "app_control.py", "ai_mail.py", "config.py", "github_client.py",
    "mail_client.py", "opencode_models.py", "settings.py", "vikunja_client.py",
    "vikunja-popups-settings.svg"
)
# GTK 3, PyGObject, requests and the SVG loader for the icons.
$Packages = @(
    "mingw-w64-ucrt-x86_64-gtk3",
    "mingw-w64-ucrt-x86_64-python-gobject",
    "mingw-w64-ucrt-x86_64-python-requests",
    "mingw-w64-ucrt-x86_64-librsvg"
)

# --- MSYS2 packages ----------------------------------------------------------
$Bash = Join-Path $Msys2Root "usr\bin\bash.exe"
$Python = Join-Path $Msys2Root "ucrt64\bin\python.exe"
$PythonW = Join-Path $Msys2Root "ucrt64\bin\pythonw.exe"

if (-not (Test-Path $Bash)) {
    Write-Error ("MSYS2 was not found in $Msys2Root. Install it from https://www.msys2.org " +
        "and run this script again (use -Msys2Root if it is somewhere else).")
}

function Invoke-Msys2([string]$Command) {
    # A login shell sets up the MSYS2 environment pacman needs.
    $env:MSYSTEM = "UCRT64"
    $env:CHERE_INVOKING = "1"
    # Out-Host keeps bash's output out of the function's return value.
    & $Bash -lc $Command | Out-Host
    return $LASTEXITCODE
}

$packageList = $Packages -join " "
if ((Invoke-Msys2 "pacman -Q $packageList >/dev/null 2>&1") -ne 0) {
    Write-Host "Updating MSYS2 and installing GTK 3 / PyGObject ..."
    # The first upgrade may only update the core and has to be repeated.
    Invoke-Msys2 "pacman -Syu --noconfirm" | Out-Null
    Invoke-Msys2 "pacman -Syu --noconfirm" | Out-Null
    if ((Invoke-Msys2 "pacman -S --needed --noconfirm $packageList") -ne 0) {
        Write-Error ("Installing the MSYS2 packages failed. Open 'MSYS2 UCRT64', run " +
            "'pacman -Syu' until nothing is left to update, then run this script again.")
    }
} else {
    Write-Host "MSYS2 packages already installed."
}

& $Python -c "import gi, requests; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk"
if ($LASTEXITCODE -ne 0) {
    Write-Error "GTK 3 bindings are not importable from $Python"
}

# --- Stop a running instance --------------------------------------------------
$wasRunning = $false
if (Test-Path $PidFile) {
    $oldPid = (Get-Content $PidFile -Raw).Trim()
    $process = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
    if ($process -and $process.ProcessName -like "python*") {
        Stop-Process -Id $oldPid -Force
        $process.WaitForExit(5000) | Out-Null
        $wasRunning = $true
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

# --- Application ---------------------------------------------------------------
Write-Host "Installing application to $AppDir ..."
New-Item -ItemType Directory -Force -Path $AppDir, $ConfigDir, $StateDir | Out-Null
foreach ($file in $AppFiles) {
    Copy-Item (Join-Path $SourceDir $file) (Join-Path $AppDir $file) -Force
}
Remove-Item (Join-Path $AppDir "__pycache__") -Recurse -Force -ErrorAction SilentlyContinue

# Shortcut icon, converted from the SVG with gdk-pixbuf.
$Svg = Join-Path $AppDir "vikunja-popups-settings.svg"
$Ico = Join-Path $AppDir "vikunja-popups.ico"
& $Python -c "import sys, gi; gi.require_version('GdkPixbuf', '2.0'); from gi.repository import GdkPixbuf; GdkPixbuf.Pixbuf.new_from_file_at_size(sys.argv[1], 64, 64).savev(sys.argv[2], 'ico', [], [])" $Svg $Ico
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Could not create the shortcut icon; the shortcuts use Python's icon."
    $Ico = $null
}

# --- Configuration ---------------------------------------------------------------
$ConfigFile = Join-Path $ConfigDir "config.json"
$createdConfig = $false
if (-not (Test-Path $ConfigFile)) {
    Copy-Item (Join-Path $SourceDir "config.example.json") $ConfigFile
    $createdConfig = $true
}

# --- Shortcuts ---------------------------------------------------------------------
$Shell = New-Object -ComObject WScript.Shell
function New-Shortcut([string]$Path, [string]$Script, [string]$Description) {
    $link = $Shell.CreateShortcut($Path)
    $link.TargetPath = $PythonW
    $link.Arguments = '"' + (Join-Path $AppDir $Script) + '"'
    $link.WorkingDirectory = $AppDir
    $link.Description = $Description
    if ($Ico) { $link.IconLocation = "$Ico,0" }
    $link.Save()
}

New-Shortcut (Join-Path $Programs "Vikunja Popups.lnk") "app.py" "Vikunja task popups"
New-Shortcut (Join-Path $Programs "Vikunja Popups Settings.lnk") "settings.py" "Configure the Vikunja task popups"
$StartupLink = Join-Path $Startup "Vikunja Popups.lnk"
if ($NoAutostart) {
    Remove-Item $StartupLink -Force -ErrorAction SilentlyContinue
} else {
    New-Shortcut $StartupLink "app.py" "Vikunja task popups"
}

if ($wasRunning) {
    Start-Process -FilePath $PythonW -ArgumentList ('"' + (Join-Path $AppDir "app.py") + '"') -WorkingDirectory $AppDir
    Write-Host "Restarted the popup app."
}

Write-Host ""
Write-Host "Installed."
if ($createdConfig) {
    Write-Host "Created $ConfigFile - check base_url and token."
}
Write-Host "Settings:  open 'Vikunja Popups Settings' from the Start menu"
Write-Host "Start:     'Vikunja Popups' in the Start menu$(if (-not $NoAutostart) { ' (also starts at login)' })"
Write-Host "Logs:      $(Join-Path $StateDir 'app.log')"
