# Runs the web monitor (monitor\web.py) and restarts it 5 s after any exit. Output goes to
# state\web-monitor.log. start-monitor.ps1 launches this hidden; to stop it, end this powershell process.
. "$PSScriptRoot\common.ps1"
$cfg = Get-KitConfig
$python = Resolve-KitExe $cfg.python 'Python'
$script = Join-Path $KitRoot 'monitor\web.py'
$log = Join-Path $cfg.state_dir 'web-monitor.log'
$env:PYTHONIOENCODING = 'utf-8'
$env:CRK_WEB_PORT = "$($cfg.web_port)"
Clear-ClaudeEnv
while ($true) {
    Add-Content -LiteralPath $log -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  start" -Encoding UTF8
    & $python $script *>> $log
    Add-Content -LiteralPath $log -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  exited (code $LASTEXITCODE), restarting in 5 s" -Encoding UTF8
    Start-Sleep 5
}
