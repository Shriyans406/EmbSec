# Usage (from the project root):  .\tools\build_firmware.ps1 phase1_echo
param(
    [Parameter(Mandatory = $true)][string]$Project
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

# Read PICO_SDK_PATH and PICO_TOOLCHAIN_PATH from .env
$envFile = Join-Path $root '.env'
if (-not (Test-Path $envFile)) { throw ".env not found at $envFile" }
$cfg = @{}
Get-Content $envFile | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $cfg[$Matches[1]] = $Matches[2].Trim() }
}
$sdk = $cfg['PICO_SDK_PATH']
$tc = $cfg['PICO_TOOLCHAIN_PATH']
if (-not $sdk -or -not $tc) { throw 'PICO_SDK_PATH / PICO_TOOLCHAIN_PATH missing in .env' }

$pico = Split-Path -Parent $sdk   # e.g. C:\Program Files\Raspberry Pi\Pico SDK v1.5.1
$env:PICO_SDK_PATH = $sdk
$env:Path = "$tc;$pico\cmake\bin;$pico\ninja;$env:Path"

$src = Join-Path $root "firmware\$Project"
if (-not (Test-Path (Join-Path $src 'CMakeLists.txt'))) { throw "No CMakeLists.txt in $src" }
$build = Join-Path $src 'build'
if (Test-Path $build) { Remove-Item -Recurse -Force $build }

Write-Host "Configuring $Project ..."
cmake -S $src -B $build -G Ninja
if ($LASTEXITCODE -ne 0) { throw 'cmake configure failed' }

Write-Host "Building $Project ..."
cmake --build $build
if ($LASTEXITCODE -ne 0) { throw 'build failed' }

$uf2 = Get-ChildItem $build -Filter *.uf2 | Select-Object -First 1
if (-not $uf2) { throw 'build finished but no .uf2 produced' }
Write-Host ""
Write-Host "BUILD OK: $($uf2.FullName) ($($uf2.Length) bytes)" -ForegroundColor Green