# Opens the "launcher": an interactive Claude session with Remote Control whose only job is to open new
# terminals on request ("open a session in ~/code/site"). From your phone you talk to it in the Claude app
# and it runs new-session.ps1. Its instructions live in launcher\CLAUDE.md. Restarts 5 s after it exits.
. "$PSScriptRoot\common.ps1"
$cfg = Get-KitConfig
$claude = Resolve-KitExe $cfg.claude 'Claude Code'
Clear-ClaudeEnv
$folder = Join-Path $KitRoot 'launcher'

$command = New-PsTabCommand ("`$Host.UI.RawUI.WindowTitle = 'launcher'; " +
    "while (`$true) { & $(ConvertTo-PsLiteral $claude) --remote-control 'launcher'; " +
    "Write-Host 'launcher closed, restarting in 5 s (Ctrl+C to stop)' -ForegroundColor Yellow; Start-Sleep 5 }")

# One window for everything: a tab in "remote" if it is open, otherwise a window of its own.
if (Open-TabInWindow $cfg 'remote' $folder $command) {
    Clear-WindowSocket $cfg 'launcher'
    exit 0
}
$socket = Start-WezWindow $cfg $folder $command
if (-not $socket) { Write-Error 'WezTerm window did not come up in 30 s.'; exit 1 }
Set-WindowSocket $cfg 'launcher' $socket
