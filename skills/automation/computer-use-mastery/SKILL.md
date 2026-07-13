---
name: computer-use-mastery
description: "General GUI automation mastery with the computer_use toolset (cua-driver): architecture, failure diagnosis, capture modes, menus/dialogs, and multi-agent desktop etiquette. Applies to ANY desktop app or browser, not just game dev."
tags: [automation, computer-use, gui, desktop, windows, cua-driver]
platforms: [windows]
---
# Computer Use Mastery (cua-driver)

The `computer_use` toolset can drive ANY desktop app or browser like a human. These are
the studio's production-proven rules (every one was learned from a real 2026-07 incident).

## Architecture — know what you're talking to
- A logon **daemon** (`cua-driver serve`) owns `\\.\pipe\cua-driver` and performs all real
  capture/input in the interactive session. Your session's `cua-driver mcp` process is only
  a forwarder. The hermes toolset talks to the forwarder, which talks to the daemon.
- A driver process with a dead parent is NORMAL daemon behavior — never "clean up" driver
  processes by killing them. Daemon restart = `cua-driver autostart kick` (Scheduled Task).
- Per-call timeout is 90s (`HERMES_CUA_CALL_TIMEOUT`). Binary lives at
  `%LOCALAPPDATA%\Programs\Cua\cua-driver\bin\cua-driver.exe` (`status`, `doctor`,
  `list-tools` are safe read-only diagnostics).

## Diagnose the failure BEFORE picking a fix
- **"daemon transport error" / pipe missing** → daemon is down → `cua-driver autostart kick`.
- **Bare timeout** ("capture failed:" with no message, call ran the full timeout) → daemon is
  FINE; the target app's UI thread is busy. SOM/AX captures walk the app's UIA tree, and UIA
  reads execute on the app's UI thread — modal dialogs, saves, long computations block them.
  Restarting the daemon does nothing here. Switch to vision captures (below) or wait.
- **"denied by user"** → gateway approval flow, not a real denial (auto_approve is enabled
  studio-wide; if you see this, the gateway config regressed — flag it, don't loop).
- Never retry the same failing call more than twice. Switch technique and say so in a comment.

## Capture modes — the core tactical choice
- `mode: "som"` (default): screenshot + numbered UIA elements. Best when the app exposes a
  real accessibility tree and is idle.
- `mode: "vision"`: pure screenshot, NO UIA walk — can never block on a busy app. Use it when
  SOM times out, when the app is mid-modal/saving/computing, or for pixel-only UIs.
- `mode: "ax"`: tree only, no pixels — cheap for structural checks.
- **LOOK at every capture with your own vision before acting or citing it.** File size and
  "capture returned ok" are not evidence.

## App-name matching (the `app=` filter)
The `app=` filter matches the PROCESS-derived name (exe name, often no spaces —
e.g. `MarvelousDesigner_Personal`), NOT the window title ("Marvelous Designer
Personal") or the display name list_apps prints. "no on-screen window matched" while
list_apps shows the app running = name mismatch, not a hidden window. Fixes: use the
exe-style name, or skip `app=` and `bring_to_front` by pid, then capture frontmost.
And remember: a vision capture scoped to explorer.exe shows WALLPAPER — that is not
evidence the target app is hidden.

## Clicking what the AX tree can't see
Many apps (Qt, games, custom toolkits) render menus/widgets that never appear as UIA
elements. They still exist as PIXELS:
1. Click the parent (top-level menu items usually ARE exposed), 2. capture, 3. read the
   target's position from the screenshot, 4. click by **coordinates**.
- **DPI-scale coordinates**: capture pixels ≠ screen points on scaled displays. Get the
  scale factor from `get_screen_size` and multiply before clicking. (The #1 cause of
  "my clicks land nowhere".)
- Keyboard is often better than mouse: `alt+f`, arrow keys, `enter`, `escape` with REAL key
  syntax (an empty key string is a call bug, not a blocked app).

## Typing into floating dialogs/panels
`type_text` posts WM_CHAR to the pid's MAIN top-level window. A floating dialog or
tool panel (library browsers, palettes) is its OWN top-level window — typed chars land
in the wrong hwnd and the field stays empty ("0 chars delivered"). Don't retry: either
target the dialog's window explicitly (window_id), avoid typing entirely (scroll +
double-click items directly), or route through the app's scripting API / a standard
file dialog. Standard Open/Save dialogs are fine — they take focus properly.

## File dialogs (Open/Save) — two actions, done
Standard Windows dialogs DO expose real AX elements. Click the filename field, type the
FULL absolute path, press enter. NEVER browse the folder tree (huge UIA surface, slow,
flaky, pointless).

## Context budget — captures are heavy
Every capture's screenshot lives in your conversation history for the rest of the run
(large PNGs are auto-transcoded to JPEG, but they still cost). A GUI session that takes
captures blindly dies of context bloat: requests slow down, then stall entirely
(observed 2026-07-05: ~30 full-res captures killed a worker's provider requests).
- Capture with PURPOSE: one capture per verification, not per curiosity. Scope to the
  app (`app=`) instead of the whole desktop when possible.
- **Checkpoint long GUI sessions**: every ~15-20 captures, write a kanban comment with
  exact current state + next step. If the run then dies or is recycled, the next worker
  resumes from the comment instead of re-discovering everything.
- If your requests are visibly slowing (multi-minute turns), checkpoint NOW and block
  the card with kind=transient asking for a fresh run — cheaper than grinding to a stall.

## Etiquette on a shared desktop
- `acquire_desktop_lock` before any mutating GUI session; `release_desktop_lock` after —
  release between phases if your task alternates GUI and non-GUI work. `capture` is
  read-only and free.
- Respect other agents' leases — wait, don't steal. If you'll hold the desktop long,
  announce it (card comment) so others can re-plan or park dependent work.
- Foreground matters: background PostMessage input reaches a window, but native menus and
  dialogs only RENDER when the app is foreground — `bring_to_front` under the lock first.
- Verify every step from the next capture (act → capture → decode → correct). A GUI step
  without a verifying capture didn't happen.

## Standing authorizations (producer-granted, cite this section)
- Brave/browser SITE PERMISSIONS for fab.com are pre-authorized: "allow multiple automatic
  downloads" prompts and equivalent download-permission toggles — click Allow, screenshot the
  state, continue (rulings 2026-07-06 t_ea6122fa / 2026-07-07 t_2769bd5b). These are browser
  settings, NOT legal agreements.
- STILL FORBIDDEN without a human: any EULA/license acceptance checkbox or modal (Fab in-editor
  EULA included), any payment/cart UI, any account setting change, anything priced above $0.

## Browser fallback
When the `browser` tool hits a login wall, captcha, or anti-bot block on a site the human
is logged into in a desktop browser, switch to computer_use on that browser window instead
of giving up (authorized). Same rules: desktop lock, capture-verify, coordinates when the
DOM-side is unreachable.
