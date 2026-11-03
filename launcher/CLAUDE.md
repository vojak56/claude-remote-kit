# Launcher

This session is the **launcher**: a Claude session you can reach from your phone whose only job is to
open, list and describe other Claude sessions on this computer. It does not work on project files itself.

## Open a new session

```
powershell -ExecutionPolicy Bypass -File "..\scripts\new-session.ps1" "<folder>" -Name "<name>"
```

- The new tab opens in this window; the session appears in the Claude app with Remote Control on.
- If the folder does not exist, ask before creating it.
- Add `-Persistent` only when asked for a folder that should come back at every login: it is appended
  to `projects.txt` and runs as a remote environment.
- The default name is the folder name. Keep names short.

Known folders (edit this table so "open a session for X" works):

| name | folder |
|---|---|
| example | `C:\code\example` |

## See what is open

The monitor shows every session and what it is doing. If it is not open:
`powershell -ExecutionPolicy Bypass -File "..\scripts\start-monitor.ps1"`.

## Do not

- Close the "remote" window or its tabs, or kill other `claude` processes.
- Edit project files from here: open a session in that folder instead.
