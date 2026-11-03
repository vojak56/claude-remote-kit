# Adds shortcuts to your Startup folder so the remote window and the monitor come up at every login.
#   install-autostart.ps1              install (remote environments + monitor)
#   install-autostart.ps1 -Launcher    also start the launcher session
#   install-autostart.ps1 -Remove      remove the shortcuts
param([switch]$Launcher, [switch]$Remove)
$startup = [Environment]::GetFolderPath('Startup')
$names = 'Claude remote environments', 'Claude monitor', 'Claude launcher'

if ($Remove) {
    foreach ($name in $names) {
        $path = Join-Path $startup "$name.lnk"
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path; Write-Host "Removed: $name" }
    }
    exit 0
}

$items = [ordered]@{ 'Claude remote environments' = 'start-remote.ps1'; 'Claude monitor' = 'start-monitor.ps1' }
if ($Launcher) { $items['Claude launcher'] = 'start-launcher.ps1' }
$shell = New-Object -ComObject WScript.Shell
foreach ($name in $items.Keys) {
    $lnk = $shell.CreateShortcut((Join-Path $startup "$name.lnk"))
    $lnk.TargetPath = 'powershell.exe'
    $lnk.Arguments = "-NoLogo -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSScriptRoot\$($items[$name])`""
    $lnk.WorkingDirectory = $PSScriptRoot
    $lnk.WindowStyle = 7   # minimized
    $lnk.Save()
    Write-Host "Added: $name"
}
