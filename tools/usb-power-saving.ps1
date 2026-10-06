# Disable (or restore) USB power saving on the capture PC.
#
# Why: on a Modern Standby notebook Windows may park USB hubs/devices; the
# FTDI RS485 adapter then stays enumerated but goes deaf mid-capture.
#
# Run from an ELEVATED PowerShell or cmd:
#   powershell -ExecutionPolicy Bypass -File C:\Dev-Free\inverter\tools\usb-power-saving.ps1
#   powershell -ExecutionPolicy Bypass -File C:\Dev-Free\inverter\tools\usb-power-saving.ps1 -Restore
#
# What it does:
#   1. Clears "Allow the computer to turn off this device to save power" on
#      every USB hub / USB device / FTDI port that has the flag.
#   2. Sets the power plan: USB selective suspend off, display and sleep
#      timeout never (AC and battery).
# The previous state is saved to usb-power-saving.backup.json next to this
# script; -Restore puts exactly that state back.
param([switch]$Restore)

$ErrorActionPreference = 'Stop'
$backup = Join-Path $PSScriptRoot 'usb-power-saving.backup.json'
$usbSub = '2a737441-1930-4402-8d77-b2bebba308a3'
$usbSusp = '48e6b7a6-50f5-4782-a5d4-53bb8f07e226'

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
          ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Write-Host 'Not elevated - start the console with "Run as administrator".' -ForegroundColor Red; exit 1 }

function Get-UsbPowerFlags {
    Get-CimInstance -Namespace root\wmi -ClassName MSPower_DeviceEnable |
        Where-Object { $_.InstanceName -match '^(USB|FTDIBUS|USB4)\\' }
}

if ($Restore) {
    if (-not (Test-Path $backup)) { Write-Host "No backup file ($backup) - nothing to restore." -ForegroundColor Red; exit 1 }
    $saved = Get-Content $backup -Raw | ConvertFrom-Json
    $want = @{}
    foreach ($d in $saved.devices) { $want[$d.InstanceName] = [bool]$d.Enable }
    foreach ($f in Get-UsbPowerFlags) {
        if ($want.ContainsKey($f.InstanceName) -and $f.Enable -ne $want[$f.InstanceName]) {
            Set-CimInstance -InputObject $f -Property @{ Enable = $want[$f.InstanceName] }
            Write-Host ("restored  {0} -> {1}" -f $f.InstanceName, $want[$f.InstanceName])
        }
    }
    powercfg /setacvalueindex SCHEME_CURRENT $usbSub $usbSusp $saved.usbSuspendAc
    powercfg /setdcvalueindex SCHEME_CURRENT $usbSub $usbSusp $saved.usbSuspendDc
    powercfg /change monitor-timeout-ac $saved.monitorAcMin
    powercfg /change monitor-timeout-dc $saved.monitorDcMin
    powercfg /change standby-timeout-ac $saved.standbyAcMin
    powercfg /change standby-timeout-dc $saved.standbyDcMin
    powercfg /setactive SCHEME_CURRENT
    Write-Host 'Power settings restored.' -ForegroundColor Green
    exit 0
}

$flags = @(Get-UsbPowerFlags)
if (-not (Test-Path $backup)) {
    # Plan values as they were before the 2026-10-06 change (display 5 min,
    # sleep never on AC / 10 min on battery, USB selective suspend on).
    [pscustomobject]@{
        saved        = (Get-Date).ToString('s')
        usbSuspendAc = 1; usbSuspendDc = 1
        monitorAcMin = 5; monitorDcMin = 5
        standbyAcMin = 0; standbyDcMin = 10
        devices      = @($flags | ForEach-Object { [pscustomobject]@{ InstanceName = $_.InstanceName; Enable = $_.Enable } })
    } | ConvertTo-Json -Depth 4 | Out-File -Encoding utf8 $backup
    Write-Host "Previous state saved to $backup"
}

$changed = 0
foreach ($f in $flags) {
    if ($f.Enable) {
        try {
            Set-CimInstance -InputObject $f -Property @{ Enable = $false }
            Write-Host ("power-off flag cleared  {0}" -f $f.InstanceName)
            $changed++
        } catch {
            Write-Host ("FAILED  {0}: {1}" -f $f.InstanceName, $_.Exception.Message) -ForegroundColor Yellow
        }
    }
}
powercfg /setacvalueindex SCHEME_CURRENT $usbSub $usbSusp 0
powercfg /setdcvalueindex SCHEME_CURRENT $usbSub $usbSusp 0
powercfg /change monitor-timeout-ac 0
powercfg /change monitor-timeout-dc 0
powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 0
powercfg /setactive SCHEME_CURRENT

Write-Host ''
Write-Host ("{0} device flag(s) cleared; USB selective suspend off; display/sleep timeout never." -f $changed) -ForegroundColor Green
Write-Host 'Still allowed to power off (should be empty):'
Get-UsbPowerFlags | Where-Object Enable | Select-Object InstanceName | Format-Table -AutoSize
