# ADB Music Sync build script (Windows).
# Usage: from the repo root run `powershell -ExecutionPolicy Bypass -File build.ps1`
# Produces a portable .exe plus a zipped portable build under dist/.

$ErrorActionPreference = "Stop"

$Version = "0.1.0"
$AppName = "adb-music-sync"
$DistDir = Join-Path $PSScriptRoot "dist"
$BuildDir = Join-Path $PSScriptRoot "build"

Write-Host "==> Building $AppName v$Version"

# 1. Ensure dependencies
python -m pip install --upgrade pip
python -m pip install .[dev]

# 2. PyInstaller build
python -m PyInstaller `
    --noconfirm --clean `
    --name $AppName `
    --onefile `
    --windowed `
    --collect-all PySide6 `
    --paths src `
    "$PSScriptRoot\src\adb_music_sync\__main__.py"

$Exe = Join-Path $DistDir "$AppName.exe"
if (-not (Test-Path $Exe)) {
    throw "Build failed: $Exe not produced"
}
Write-Host "==> Built $Exe"

# 3. Assemble portable build (exe + platform-tools hint + README)
$ReleaseDir = Join-Path $PSScriptRoot "release"
$PortableDir = Join-Path $ReleaseDir "$AppName-$Version"
New-Item -ItemType Directory -Force -Path $PortableDir | Out-Null
Copy-Item $Exe "$PortableDir\$AppName.exe"
New-Item -ItemType Directory -Force -Path "$PortableDir\platform-tools" | Out-Null
Copy-Item "$PSScriptRoot\README.md" "$PortableDir\README.md"

# 4. Zip it
$ZipPath = Join-Path $ReleaseDir "$AppName-$Version-windows.zip"
if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
Compress-Archive -Path "$PortableDir\*" -DestinationPath $ZipPath

Write-Host "==> Portable build: $ZipPath"
Write-Host "==> Done."