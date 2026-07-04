# CLED

**Claude/Codex LED** — an ambient daemon that turns your
keyboard into a live status panel for your AI coding agents (and your machine),
driven by [OpenRGB](https://openrgb.org).

CLED maps your running agent sessions
onto the keys: glance down and see, by color, which of your Claude Code / Codex
sessions is working, which is waiting on you, and how hard your machine is
breathing — without alt-tabbing to check.

<p align="center">
  <a href="https://nadavbh12.github.io/cled/"><img src="docs/cled-demo.gif" alt="CLED — your keyboard as a live agent panel" width="480"></a>
</p>

<p align="center">
  <b><a href="https://nadavbh12.github.io/cled/">Live demo &amp; install →</a></b><br>
  <sub>Demo video made with <a href="https://github.com/latent-spaces/brag"><code>/brag</code></a></sub>
</p>

## What you see

```
 F1 ─────────────── F12      RAM usage, fills left → right
 1  2  3  4 ... 9            one slot per agent tab in the focused iTerm2 window
                            ┌ red    = busy   (mid-turn, working)
                            ├ green  = idle   (waiting for you)
                            ├ amber  = stale  (idle > 20 min)
                            └ blue   = other  (tab isn't a recognized agent)
 Numpad 0–9                  per-core CPU load, green → yellow → red
 Enter                       rainbow heartbeat (the daemon is alive)
 everything else             dim
```

## Requirements
- A keyboard with LEDs!
- **macOS.** Agent detection reads the focused iTerm2 window via AppleScript, so
  iTerm2 + macOS are assumed.
- **[OpenRGB](https://openrgb.org)** installed (the app). CLED talks to its
  server on `127.0.0.1:6742` and launches it for you if it isn't running.
- **Python ≥ 3.11**, managed with [uv](https://docs.astral.sh/uv/).
- An OpenRGB-supported RGB keyboard. Developed against a **Corsair K70 RGB MK.2 SE**
  (Direct mode, 116 LEDs); key regions are addressed by OpenRGB LED name, so it
  generalizes to other boards where those names match.

## Install & run

CLED is on PyPI, so the quickest path — no clone — is with
[uv](https://docs.astral.sh/uv/):

```sh
uvx cled              # fetch and run in one step, nothing installed
```

For a persistent `cled` command on your `PATH`:

```sh
uv tool install cled
cled
```

**In Claude Code?** Install the plugin and let your agent do the setup — it checks
your machine (OpenRGB, a controllable keyboard, iTerm2) and launches the daemon in
its own window:

```
/plugin marketplace add latent-spaces/cled
/plugin install cled@cled
```

Then invoke it with `/cled`. The plugin is a thin launcher around the same `uvx
cled`, so the PyPI package above stays the single source of truth.

Or run from a clone, handy if you want to hack on it (see *Make it yours* below):

```sh
git clone https://github.com/latent-spaces/cled
cd cled
uv run cled
```

However you start it, the daemon connects to OpenRGB if it's already up, and
otherwise starts the server itself. Press `Ctrl-C` to quit (the OpenRGB server is
left running).

### macOS, no root

On macOS, OpenRGB can drive the keyboard as a **plain user — no `sudo`, no Input
Monitoring grant** — but only when the server runs inside your GUI/Aqua session.
CLED relies on this: it self-heals the server with

```sh
open -a OpenRGB --args --server --noautoconnect
```

which is the launch context that has HID access. The catch: **start CLED from a
terminal in your normal desktop session.** Running under `sudo` (or from a
non-GUI/CLI-orphan context) sees zero devices, and also breaks agent detection —
`Path.home()` and iTerm2 automation both need your user session.

## How it works

| File | Role |
|------|------|
| `rgb.py` | OpenRGB client + render loop (`RGBSession`). Self-heals: reconnects on connection errors, and refreshes the server when it detects a wake-from-sleep (a >60s wall-clock gap between frames means the HID handle went stale). |
| `agent_tabs.py` | Watches tabs in the focused iTerm2 window (`osascript` + `ps`) and maps each to a status — Claude Code reads its session JSONL, Codex reads its rollout state — to tell *busy* from *idle*. |
| `cled.py` | The daemon. Composes each frame (agents + CPU + RAM + heartbeat) and runs the loop at 10 fps. |

## Make it yours

CLED is deliberately small and self-contained — three short modules, the Python
standard library, and two dependencies (`openrgb-python`, `psutil`). There's no
framework and no hidden machinery; the whole thing fits in one sitting.

So fork it and bend it to your setup — a different key layout, your own status
colors, a provider for another agent or terminal. Each behavior lives in a few
small, obvious functions. Just ask Claude to change the behavior you want.
