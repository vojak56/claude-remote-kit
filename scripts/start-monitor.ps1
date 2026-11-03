# Opens the terminal monitor (monitor\monitor.py) as a tab in the "remote" window, or in its own window.
# Also starts the web monitor in the background (hidden, restarts itself) unless -NoWeb is given or its
# port is already taken. If the monitor crashes it restarts after 5 s.
param([switch]$NoWeb)
. "$PSScriptRoot\common.ps1"
$cfg = Get-KitConfig
$python = Resolve-KitExe $cfg.python 'Python'
Clear-ClaudeEnv   # NO_COLOR would make the monitor colourless
$folder = Join-Path $KitRoot 'monitor'
$script = Join-Path $folder 'monitor.py'

$command = New-PsTabCommand ("`$Host.UI.RawUI.WindowTitle = 'monitor'; [Console]::OutputEncoding = [Text.Encoding]::UTF8; " +
    "`$env:PYTHONIOENCODING = 'utf-8'; " +
    "while (`$true) { & $(ConvertTo-PsLiteral $python) $(ConvertTo-PsLiteral $script); " +
    "Write-Host 'monitor stopped, restarting in 5 s (Ctrl+C to stop)' -ForegroundColor Yellow; Start-Sleep 5 }")

if (-not $NoWeb -and -not (Get-NetTCPConnection -LocalPort $cfg.web_port -State Listen -ErrorAction SilentlyContinue)) {
    Start-Process powershell.exe -WindowStyle Hidden -ArgumentList (Join-ProcessArgs @(
        '-NoLogo', '-ExecutionPolicy', 'Bypass', '-File', "$PSScriptRoot\start-web-monitor.ps1"))
}

if (Open-TabInWindow $cfg 'remote' $folder $command) {
    Clear-WindowSocket $cfg 'monitor'
    exit 0
}
$socket = Start-WezWindow $cfg $folder $command @('--config', 'initial_cols=190', '--config', 'initial_rows=52')
if (-not $socket) { Write-Error 'WezTerm window did not come up in 30 s.'; exit 1 }
Set-WindowSocket $cfg 'monitor' $socket
