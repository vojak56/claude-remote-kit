# Opens one WezTerm window, "remote", with a tab per project from projects.txt.
# Each tab runs remote-claude.ps1, so every project shows up as an environment in the Claude app.
# Run it at login (install-autostart.ps1) or by hand after editing projects.txt.
. "$PSScriptRoot\common.ps1"
$cfg = Get-KitConfig
Clear-ClaudeEnv

$projects = @(Get-KitProjects $cfg)
if (-not $projects) {
    Write-Error "No projects found. Copy projects.example.txt to projects.txt and list your folders there ($($cfg.projects_file))."
    exit 1
}
if (Get-WezPane $cfg (Get-WindowSocket $cfg 'remote')) {
    Write-Error 'The "remote" window is already open. Close it first, or add a folder with new-session.ps1 -Persistent.'
    exit 1
}

function Get-TabCommand($p) {
    @('powershell.exe', '-NoLogo', '-NoExit', '-ExecutionPolicy', 'Bypass', '-File', "$PSScriptRoot\remote-claude.ps1",
      '-Folder', $p.Folder, '-Name', $p.Name)
}

# The first project opens the window; the rest become tabs in that same window (addressed by its socket,
# so tabs never land in some other WezTerm window).
$socket = Start-WezWindow $cfg $projects[0].Folder (Get-TabCommand $projects[0])
if (-not $socket) { Write-Error 'WezTerm window did not come up in 30 s.'; exit 1 }
Set-WindowSocket $cfg 'remote' $socket
foreach ($p in $projects | Select-Object -Skip 1) {
    Open-TabInWindow $cfg 'remote' $p.Folder (Get-TabCommand $p) | Out-Null
}
Write-Host "Opened $($projects.Count) remote environment(s). They appear in the Claude app within a few seconds."
