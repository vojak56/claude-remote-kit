# Keeps one folder reachable from the Claude app and claude.ai/code: runs `claude remote-control`
# in that folder and restarts it 15 s after it exits (crash, sleep, network loss).
# In the app every such folder is an environment; new sessions are spawned in it from your phone.
# Started by start-remote.ps1, one tab per project. To stop: close the tab.
param(
    [Parameter(Mandatory)][string]$Folder,
    [string]$Name
)
. "$PSScriptRoot\common.ps1"
$cfg = Get-KitConfig
$claude = Resolve-KitExe $cfg.claude 'Claude Code'
$Folder = (Resolve-Path -LiteralPath $Folder -ErrorAction Stop).Path
if (-not $Name) { $Name = Split-Path $Folder -Leaf }

Clear-ClaudeEnv
Set-Location -LiteralPath $Folder
$Host.UI.RawUI.WindowTitle = "remote - $Name"

while ($true) {
    # wait for the network: after boot or wake, Wi-Fi and VPNs come up a bit later
    for ($i = 0; $i -lt 60; $i++) {
        try { Invoke-WebRequest 'https://api.anthropic.com' -Method Head -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop | Out-Null; break }
        catch { if ($_.Exception.Response) { break } }   # any HTTP answer means the network is up
        Start-Sleep 5
    }
    Write-Host "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  starting Remote Control: $Name ($Folder)" -ForegroundColor Green
    & $claude remote-control --name $Name --spawn same-dir
    Write-Host "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  claude exited (code $LASTEXITCODE), restarting in 15 s" -ForegroundColor Yellow
    Start-Sleep 15
}
