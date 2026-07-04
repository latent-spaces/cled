---
name: cled
description: Set up and launch CLED, the ambient keyboard daemon that shows your
  Claude Code agents' status on your keyboard via OpenRGB. Use when the user asks
  to start, launch, run, or set up CLED.
disable-model-invocation: true
---

# Launch CLED

CLED is a long-running daemon that must run in the user's GUI terminal session, so
launch it in a dedicated iTerm2 window that outlives this turn. Run the preflight in
order; stop with the specific fix if any check fails — don't launch a daemon that
can't work.

## Preflight

1. **macOS** — `uname -s` must print `Darwin` (agent detection uses iTerm2 +
   AppleScript). Else stop.
2. **OpenRGB installed** — `osascript -e 'id of app "OpenRGB"'` must succeed. Else
   offer `brew install --cask openrgb` and stop.
3. **A controllable keyboard** — if `nc -z 127.0.0.1 6742` fails, start the server as
   CLED does: `open -a OpenRGB --args --server --noautoconnect`, then poll the port
   (up to ~30s; HID detection on macOS takes ~10s). Then
   `/Applications/OpenRGB.app/Contents/MacOS/OpenRGB --client 127.0.0.1:6742 --list-devices`
   must show a device with `Type: Keyboard` and `Direct` in its `Modes:`. Report the
   model; else stop — the keyboard isn't OpenRGB-controllable.
4. **iTerm2** — `osascript -e 'id of app "iTerm2"'` must succeed. Else offer
   `brew install --cask iterm2` and stop.

## Launch

Open a dedicated iTerm2 window running the daemon (it persists in the GUI session,
independent of this Claude session):

    osascript <<'END'
    tell application "iTerm2"
      set w to (create window with default profile)
      tell current session of w to write text "uvx cled"
    end tell
    END

`uvx cled` fetches CLED from PyPI and starts it. (Before CLED is published to PyPI,
use `uvx --from git+https://github.com/latent-spaces/cled cled` instead.)

## Confirm

Tell the user CLED is now running in a new iTerm2 window — number row = agent tabs
(red busy / green idle / amber stale / blue other), F-keys = RAM, numpad = per-core
CPU, Enter = heartbeat. To stop it: Ctrl-C in that window (OpenRGB keeps running).
