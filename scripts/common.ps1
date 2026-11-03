# Shared helpers for the kit's scripts. Dot-source it:  . "$PSScriptRoot\common.ps1"
#
# Settings come from config.json in the kit folder (or the file named by $env:CRK_CONFIG).
# Every key is optional; see config.example.json for the defaults.

$KitRoot = Split-Path -Parent $PSScriptRoot

function Resolve-KitPath([string]$Value) {
    $v = [Environment]::ExpandEnvironmentVariables($Value)
    if ([IO.Path]::IsPathRooted($v)) { return $v }
    return Join-Path $KitRoot $v
}

function Resolve-KitExe([string]$Value, [string]$What) {
    $v = [Environment]::ExpandEnvironmentVariables($Value)
    if ($v -match '[\\/]') {
        if (-not (Test-Path -LiteralPath $v)) { throw "$What not found at '$v' (check config.json)" }
        return $v
    }
    $cmd = Get-Command $v -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cmd) { throw "$What ('$v') is not on PATH. Install it or set its full path in config.json." }
    return $cmd.Source
}

function Get-KitConfig {
    $cfg = [ordered]@{
        claude         = 'claude'
        wezterm        = 'wezterm'
        wezterm_config = ''
        python         = 'python'
        state_dir      = 'state'
        projects_file  = 'projects.txt'
        web_port       = 8790
    }
    $path = if ($env:CRK_CONFIG) { $env:CRK_CONFIG } else { Join-Path $KitRoot 'config.json' }
    if (Test-Path -LiteralPath $path) {
        $json = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($key in @($cfg.Keys)) {
            if ($null -ne $json.$key -and "$($json.$key)" -ne '') { $cfg[$key] = $json.$key }
        }
        if ($json.monitor -and $json.monitor.web_port) { $cfg.web_port = [int]$json.monitor.web_port }
    }
    if ($env:CRK_WEB_PORT) { $cfg.web_port = [int]$env:CRK_WEB_PORT }
    if ($env:CRK_STATE_DIR) { $cfg.state_dir = $env:CRK_STATE_DIR }

    $cfg.state_dir = Resolve-KitPath $cfg.state_dir
    $cfg.projects_file = Resolve-KitPath $cfg.projects_file
    New-Item -ItemType Directory -Force -Path $cfg.state_dir | Out-Null
    if ($cfg.wezterm_config) { $env:WEZTERM_CONFIG_FILE = Resolve-KitPath $cfg.wezterm_config }
    return [pscustomobject]$cfg
}

# Claude Code sets these in its own shells; a child claude started with them would not save its history.
function Clear-ClaudeEnv {
    Get-ChildItem env: | Where-Object { $_.Name -match '^(CLAUDECODE|CLAUDE_CODE_|NO_COLOR)' } |
        ForEach-Object { Remove-Item "env:$($_.Name)" }
}

# Quote a value for a PowerShell single-quoted string.
function ConvertTo-PsLiteral([string]$Value) { "'" + ($Value -replace "'", "''") + "'" }

# Start-Process takes one flat argument string on Windows PowerShell; quote what needs it.
function Join-ProcessArgs([string[]]$ArgList) {
    ($ArgList | ForEach-Object { if ($_ -match '\s|"') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }) -join ' '
}

function Get-WezGui($Cfg) {
    $exe = Resolve-KitExe $Cfg.wezterm 'WezTerm'
    $gui = Join-Path (Split-Path $exe) 'wezterm-gui.exe'
    if (-not (Test-Path -LiteralPath $gui)) { throw "wezterm-gui.exe not found next to $exe" }
    return $gui
}

# The first pane of the WezTerm window behind a socket, or $null if the window is gone.
function Get-WezPane($Cfg, [string]$Socket) {
    if (-not $Socket) { return $null }
    $wez = Resolve-KitExe $Cfg.wezterm 'WezTerm'
    $env:WEZTERM_UNIX_SOCKET = $Socket
    $list = & $wez cli --no-auto-start list --format json 2>$null
    if (-not $list) { return $null }
    return ($list | ConvertFrom-Json) | Select-Object -First 1
}

# Each named window ("remote", "launcher", "monitor") records its socket in state\wezterm-<label>.txt,
# so other scripts can open tabs in it and the monitor can count its tabs.
function Get-WindowSocket($Cfg, [string]$Label) {
    $file = Join-Path $Cfg.state_dir "wezterm-$Label.txt"
    if (Test-Path -LiteralPath $file) { return (Get-Content -LiteralPath $file -Raw -Encoding UTF8).Trim() }
    return $null
}

function Set-WindowSocket($Cfg, [string]$Label, [string]$Socket) {
    Set-Content -LiteralPath (Join-Path $Cfg.state_dir "wezterm-$Label.txt") -Value $Socket -Encoding UTF8
}

# Forget a window label (used when the thing runs as a tab of another window instead).
function Clear-WindowSocket($Cfg, [string]$Label) {
    Remove-Item -LiteralPath (Join-Path $Cfg.state_dir "wezterm-$Label.txt") -ErrorAction SilentlyContinue
}

# Opens $Command as a new tab in the window labelled $Label. Returns $false if that window is not open.
function Open-TabInWindow($Cfg, [string]$Label, [string]$Cwd, [string[]]$Command) {
    $socket = Get-WindowSocket $Cfg $Label
    $pane = Get-WezPane $Cfg $socket
    if (-not $pane) { return $false }
    $wez = Resolve-KitExe $Cfg.wezterm 'WezTerm'
    & $wez cli --no-auto-start spawn --pane-id $pane.pane_id --cwd $Cwd -- @Command | Out-Null
    return $true
}

# Opens a new WezTerm window running $Command and waits until it accepts CLI commands.
# Returns the window's socket path, or $null after 30 s.
function Start-WezWindow($Cfg, [string]$Cwd, [string[]]$Command, [string[]]$GuiArgs = @()) {
    $gui = Get-WezGui $Cfg
    $sockets = Join-Path $env:USERPROFILE '.local\share\wezterm'
    $started = Get-Date
    $argList = $GuiArgs + @('start', '--always-new-process', '--cwd', $Cwd, '--') + $Command
    Start-Process $gui -ArgumentList (Join-ProcessArgs $argList)
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep 1
        $sock = Get-ChildItem $sockets -Filter 'gui-sock-*' -ErrorAction SilentlyContinue |
            Where-Object { $_.CreationTime -ge $started.AddSeconds(-1) } | Sort-Object CreationTime | Select-Object -Last 1
        if ($sock -and (Get-WezPane $Cfg $sock.FullName)) { return $sock.FullName }
    }
    return $null
}

# A command line that runs an inline PowerShell script in a tab.
function New-PsTabCommand([string]$Script) {
    @('powershell.exe', '-NoLogo', '-NoExit', '-ExecutionPolicy', 'Bypass', '-Command', $Script)
}

# Reads projects.txt: "name | folder" per line, # starts a comment. Missing folders are skipped with a warning.
function Get-KitProjects($Cfg) {
    if (-not (Test-Path -LiteralPath $Cfg.projects_file)) { return @() }
    foreach ($line in Get-Content -LiteralPath $Cfg.projects_file -Encoding UTF8) {
        if ($line -match '^\s*(#|$)' -or $line -notmatch '\|') { continue }
        $name, $folder = ($line -split '\|', 2) | ForEach-Object { $_.Trim() }
        $folder = [Environment]::ExpandEnvironmentVariables($folder)
        if (Test-Path -LiteralPath $folder) { [pscustomobject]@{ Name = $name; Folder = $folder } }
        else { Write-Warning "projects.txt: folder not found, skipped: $folder" }
    }
}
