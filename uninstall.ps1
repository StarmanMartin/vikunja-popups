# Remove vikunja-popups for the current Windows user. Keeps the config.
#
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1

$ErrorActionPreference = "Stop"

$AppName = "vikunja-popups"
$AppDir = Join-Path $env:LOCALAPPDATA "Programs\$AppName"
$ConfigDir = Join-Path $env:APPDATA $AppName
$PidFile = Join-Path $env:LOCALAPPDATA "$AppName\app.pid"
$Programs = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"

if (Test-Path $PidFile) {
    $oldPid = (Get-Content $PidFile -Raw).Trim()
    $process = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
    if ($process -and $process.ProcessName -like "python*") {
        Stop-Process -Id $oldPid -Force
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

foreach ($link in @(
    (Join-Path $Programs "Vikunja Popups.lnk"),
    (Join-Path $Programs "Vikunja Popups Settings.lnk"),
    (Join-Path $Programs "Startup\Vikunja Popups.lnk")
)) {
    Remove-Item $link -Force -ErrorAction SilentlyContinue
}
Remove-Item $AppDir -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "Application removed. MSYS2 and its packages were left installed."
Write-Host "Configuration was kept at:"
Write-Host "  $ConfigDir"
