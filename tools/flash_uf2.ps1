# Usage (from the project root):  .\tools\flash_uf2.ps1 firmware\phase1_echo\build\aeisp_phase1_echo.uf2
# Temporary manual flasher for Phase 1. Phase 4 replaces it with a hardened, approval-gated version.
#
# How it gets the board into the UF2 bootloader (tried in this order):
#   1. already in bootloader mode (RPI-RP2 drive present)
#   2. send "BOOTSEL" over the serial port   (works with our AEISP firmware, needs no driver)
#   3. open the port at 1200 baud and close it (Pico SDK "magic baud" reset, works on many firmwares)
#   4. picotool reboot -f -u                  (needs a WinUSB driver installed with Zadig)
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

function Wait-BootDrive([int]$Seconds) {
    $drive = $null
    for ($i = 0; $i -lt ($Seconds * 2) -and -not $drive; $i++) {
        $drive = Get-BootDrive
        if (-not $drive) { Start-Sleep -Milliseconds 500 }
    }
    return $drive
}

function Get-BoardPort {
    $d = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
        Where-Object { $_.InstanceId -like 'USB\VID_2E8A&PID_*&MI_00*' -and $_.Class -eq 'Ports' } |
        Select-Object -First 1
    if ($d -and $d.FriendlyName -match '\((COM\d+)\)') { return $Matches[1] }
    return $null
}

function Invoke-SerialBootsel([string]$Com) {
    $sp = New-Object System.IO.Ports.SerialPort $Com, 115200, 'None', 8, 'One'
    $sp.ReadTimeout = 1500
    $sp.WriteTimeout = 1500
    $sp.DtrEnable = $true
    $sp.NewLine = "`n"
    try {
        $sp.Open()
        $sp.DiscardInBuffer()
        $sp.WriteLine('BOOTSEL')
        try { return $sp.ReadLine().Trim() } catch { return $null }
    } finally {
        if ($sp.IsOpen) { $sp.Close() }
        $sp.Dispose()
    }
}

function Invoke-Touch1200([string]$Com) {
    $sp = New-Object System.IO.Ports.SerialPort $Com, 1200, 'None', 8, 'One'
    $sp.DtrEnable = $true
    try {
        $sp.Open()
        Start-Sleep -Milliseconds 300
    } finally {
        if ($sp.IsOpen) { $sp.Close() }
        $sp.Dispose()
    }
}

$drive = Get-BootDrive
if (-not $drive) {
    $com = Get-BoardPort
    if ($com) {
        Write-Host "Board found on $com. Method 1: sending BOOTSEL over serial ..."
        try {
            $r = Invoke-SerialBootsel $com
            Write-Host "  board replied: $r"
        } catch {
            Write-Host "  serial BOOTSEL failed: $($_.Exception.Message)"
        }
        $drive = Wait-BootDrive 8

        if (-not $drive) {
            $com = Get-BoardPort
            if ($com) {
                Write-Host "Method 2: 1200-baud reset on $com ..."
                try { Invoke-Touch1200 $com } catch { Write-Host "  1200-baud reset failed: $($_.Exception.Message)" }
                $drive = Wait-BootDrive 8
            }
        }
    } else {
        Write-Host 'No board serial port found.'
    }

    if (-not $drive -and $picotool -and (Test-Path $picotool)) {
        Write-Host 'Method 3: picotool reboot -f -u ...'
        & $picotool reboot -f -u 2>&1 | ForEach-Object { Write-Host "  $_" }
        $drive = Wait-BootDrive 10
    }
}
if (-not $drive) {
    throw 'Board did not enter bootloader mode. Unplug it, hold the BOOT button, plug it in, release, then run this script again.'
}

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
