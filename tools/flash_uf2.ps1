# Usage (from the project root):  .\tools\flash_uf2.ps1 firmware\phase1_echo\build\aeisp_phase1_echo.uf2
# Temporary manual flasher for Phase 1. Phase 4 replaces it with a hardened, approval-gated version.
param(
    [Parameter(Mandatory = $true)][string]$Uf2
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

$uf2Path = Resolve-Path $Uf2 -ErrorAction Stop
if ($uf2Path.Path -notlike '*.uf2') { throw 'File must be a .uf2' }

$envFile = Join-Path $root '.env'
$cfg = @{}
Get-Content $envFile | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $cfg[$Matches[1]] = $Matches[2].Trim() }
}
$picotool = $cfg['PICOTOOL_PATH']

function Get-BootDrive {
    Get-Volume -ErrorAction SilentlyContinue | Where-Object { $_.FileSystemLabel -eq 'RPI-RP2' -and $_.DriveLetter } | Select-Object -First 1
}

$drive = Get-BootDrive
if (-not $drive) {
    Write-Host 'Board is not in BOOTSEL mode. Asking it to reboot into BOOTSEL ...'
    & $picotool reboot -f -u
    if ($LASTEXITCODE -ne 0) {
        throw 'picotool could not reboot the board. Hold the BOOT button while plugging in USB, then run this script again.'
    }
    for ($i = 0; $i -lt 30 -and -not $drive; $i++) {
        Start-Sleep -Milliseconds 500
        $drive = Get-BootDrive
    }
}
if (-not $drive) { throw 'RPI-RP2 drive did not appear within 15 seconds.' }

$dest = "$($drive.DriveLetter):\"
Write-Host "Copying $($uf2Path.Path) to $dest ..."
Copy-Item $uf2Path.Path $dest -Force
Write-Host 'Copied. The board reboots by itself. Waiting for its serial port ...'

$found = $null
for ($i = 0; $i -lt 40 -and -not $found; $i++) {
    Start-Sleep -Milliseconds 500
    $found = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
        Where-Object { $_.InstanceId -like 'USB\VID_2E8A&PID_*&MI_00*' -and $_.Class -eq 'Ports' } |
        Select-Object -First 1
}
if ($found) {
    Write-Host "FLASH OK: $($found.FriendlyName)" -ForegroundColor Green
} else {
    Write-Host 'Copied, but no serial port appeared in 20 seconds. Unplug and replug the board, then run: python -m app.hardware.cli ports' -ForegroundColor Yellow
}