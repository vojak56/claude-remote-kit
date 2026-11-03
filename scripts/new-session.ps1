# Opens a new Claude session with Remote Control in any folder, as a tab in the kit's WezTerm windows.
#   new-session.ps1 <folder> [-Name "title"]             one-off session (shows up in the Claude app)
#   new-session.ps1 <folder> -Name "title" -Persistent   project environment: appended to projects.txt,
#                                                        comes back at every login, any number of sessions inside
param(
    [Parameter(Mandatory)][string]$Folder,
    [string]$Name,
    [switch]$Persistent
)
. "$PSScriptRoot\common.ps1"
$cfg = Get-KitConfig
$claude = Resolve-KitExe $cfg.claude 'Claude Code'
Clear-ClaudeEnv

$Folder = (Resolve-Path -LiteralPath $Folder -ErrorAction Stop).Path
if (-not $Name) { $Name = Split-Path $Folder -Leaf }
if ($Name -match '\|') { Write-Error 'The name cannot contain "|".'; exit 1 }

if ($Persistent) {
    Add-Content -LiteralPath $cfg.projects_file -Value "$Name | $Folder" -Encoding UTF8
    $command = @('powershell.exe', '-NoLogo', '-NoExit', '-ExecutionPolicy', 'Bypass', '-File',
        "$PSScriptRoot\remote-claude.ps1", '-Folder', $Folder, '-Name', $Name)
} else {
    $command = New-PsTabCommand ("`$Host.UI.RawUI.WindowTitle = $(ConvertTo-PsLiteral "session - $Name"); " +
        "Get-ChildItem env: | Where-Object { `$_.Name -match '^(CLAUDECODE|CLAUDE_CODE_|NO_COLOR)' } | ForEach-Object { Remove-Item `"env:`$(`$_.Name)`" }; " +
        "& $(ConvertTo-PsLiteral $claude) --remote-control $(ConvertTo-PsLiteral $Name)")
}

# Prefer the launcher window (keeps project tabs tidy), then the remote window, else a new window.
$opened = (Open-TabInWindow $cfg 'launcher' $Folder $command) -or (Open-TabInWindow $cfg 'remote' $Folder $command)
if (-not $opened) {
    $socket = Start-WezWindow $cfg $Folder $command
    if (-not $socket) { Write-Error 'WezTerm window did not come up in 30 s.'; exit 1 }
    if ($Persistent) { Set-WindowSocket $cfg 'remote' $socket }
}
Write-Host "Opened '$Name' in $Folder. It shows up in the Claude app in a few seconds."
