# GPU Game Guard

Stop local AI models and GPU-using background services from competing with games
on the same GPU.

Built for a single-GPU Linux desktop (RTX 4090) that runs local LLM/vision
inference, where "the model can sit warm in VRAM" and "games need the whole
card" are fundamentally in conflict. This repo makes the second win,
automatically, without the user ever thinking about it.

## The problem

A local model kept resident in VRAM is great for latency (no cold load) and
terrible for gaming (it is holding ~10 GB of a 24 GB card). Telling the user to
remember to unload it before playing does not work. Neither does evicting once
at game start, because **background services re-claim the model mid-game**.

The concrete failure, observed live while playing World of Warcraft:

| Time | Event | VRAM |
|---|---|---|
| launch | guard detects the game, evicts | 1,223 MiB |
| +30s | Ring camera poller classifies a snapshot, model returns | **16,476 MiB** |

The Ring poller sends no `keep_alive`, so Ollama's 5-minute default applies and
the vision model comes straight back. One-shot eviction is not enough on any box
where something else uses the GPU on a timer.

## What it does

`game-mode-guard` watches for a game starting and:

1. **Suspends** GPU-using background services (only the ones that actually
   touch Ollama — a daemon-driven Ring poller in this case).
2. **Evicts** resident Ollama models and waits for the driver to reclaim pages.
3. On game exit, **restores** the suspended services.

It is a ~15 MB resident daemon, `Nice=10`, `CPUQuota=10%`, `MemoryMax=64M`, so
it can never become the heavy background task that gaming performance forbids.

## Detection is process-based, on purpose

A GPU-usage threshold only trips *after* frame times are already ruined.
Detecting the game process at launch means the card is clear before the game
asks for it.

Three launch paths are covered, each with a different process shape:

| Path | Process matched |
|---|---|
| Steam / Proton | `reaper` (requires a live `RunningAppID`) |
| Heroic | `umu-run` (Heroic does not use Steam's reaper) |
| Lutris / Wine | `wine64-preloader`, or the game `.exe` itself (`WoW.exe`, `Diablo IV.exe`) |

Deliberately **not** matched: launcher shells (`steam`, `heroic`, `lutris`),
Wine plumbing (`wineserver`, `wineboot`, `services.exe`, `explorer.exe`),
browsers, and the `Battle.net.exe` / `Agent.exe` updater on its own. An open
launcher is not a running game.

### The bug that taught us this

The first matcher tested the ignore-list against the **full executable path**.
`"steam"` is in that list, and the real Steam reaper lives at
`/steam/ubuntu12_64/reaper` — so it silently ignored *every Steam/Proton game*.
Not a crash; a guard that quietly never fired.

Ignore matches now test the **basename only**.
`scripts/test-game-mode-guard.py` covers this case (18 cases: every launch path
plus every false-positive risk). Run it after any edit to the matcher.

## Files

| File | Purpose |
|---|---|
| `scripts/game-mode-guard.py` | The daemon: detection, eviction, suspend/restore |
| `scripts/test-game-mode-guard.py` | Classifier regression tests (no games needed) |
| `scripts/power-why` | Companion: explains what is drawing UPS power |
| `systemd/game-mode-guard.service` | User unit |

## Install

```bash
mkdir -p ~/.hermes/scripts ~/.config/systemd/user
cp scripts/game-mode-guard.py scripts/test-game-mode-guard.py ~/.hermes/scripts/
cp scripts/power-why ~/.local/bin/ && chmod +x ~/.local/bin/power-why
cp systemd/game-mode-guard.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now game-mode-guard.service

# sanity
python3 ~/.hermes/scripts/test-game-mode-guard.py
python3 ~/.hermes/scripts/game-mode-guard.py status
```

## Usage

```bash
python3 ~/.hermes/scripts/game-mode-guard.py status       # state, games, VRAM, suspended units
python3 ~/.hermes/scripts/game-mode-guard.py check        # dry look
python3 ~/.hermes/scripts/game-mode-guard.py check --evict  # force a pass
tail -f ~/.hermes/logs/game-mode.log                      # GAME START / exit events

# power-why
power-why                 # what is drawing UPS power, in one screen
power-why --watch 5       # live refresh
power-why --json          # machine-readable
```

`--no-suspend` runs evict-only if you would rather not stop background services.

## Configuration

Edit `GPU_SERVICES` near the top of `game-mode-guard.py` to list the units that
should be suspended. Keep it to units that actually touch the GPU; anything that
does not is left running.

## Safety properties

- Only units that were **active** are stopped, and only those restarted — a
  disabled unit is never started behind your back.
- The suspended list is **persisted to disk**, so a daemon restart mid-game
  still restores services on exit.
- Every tick re-reconciles: services still up get suspended, any model that
  reappears gets evicted. Verified self-healing.
- A crash loop is not silent — errors go to the log.
- Game active state is exposed as JSON at `~/.hermes/state/game-mode.json` for
  other tooling to read.

## Caveat

Restore fires when the *game process* exits. If a launcher leaves a stray Wine
process alive after you quit, `game_active` can linger. Check with
`game-mode-guard.py status`; `systemctl --user start <unit>` gets you back
immediately.

## Companion: power-why

`power-why` answers "why is the UPS loaded?" in one command. It reads
`apcaccess LOADPCT` and breaks it down against GPU draw, per-process CPU, top
VRAM consumers and resident local-AI models. It prints the honest caveat that
the UPS figure also covers PSU conversion loss, UPS float overhead, monitors,
case screen, USB, fans and pumps, so the visible parts never sum to the whole —
a high figure with every process idle is normal for a loaded desktop.

Handles the field quirks of a CyberPower CP1500PFCLCDa (no `BATTV`/`BATTDATE`;
a missing field makes `grep` exit 1, and `set -e` turns that into silent death,
so every optional read is guarded).

## License

MIT
