#!/usr/bin/env python3
"""game-mode-guard — free the GPU when a game launches.

The local AI policy keeps a model resident in VRAM by design (warm latency),
but models are exclusive with gaming. This daemon watches for a demanding game
starting and (a) suspends GPU-using background services, (b) drops resident
Ollama models so the 4090 is clear before the game needs it, then (c) restores
the services when the game exits.

Design constraints (from the user's profile and POLICY.md):
  * Gaming performance is the top priority — act EARLY, not on first frame.
  * Never a heavy background task — poll on a long interval, tiny footprint.
  * Reuse the proven eviction path: Ollama's native /api/generate with
    keep_alive=0. `ollama stop` also works but does not wait for the driver to
    reclaim pages; we poll like the /local unload plugin does.

Detection is process-based (cheap) rather than GPU-usage based, so it fires at
process start rather than after the game has already begun competing for VRAM.

Suspension is necessary in addition to eviction: the Ring camera poller
(`ring-local.service`) re-claims the vision model with its own keep_alive every
30s, so evicting once is not enough — the card refills mid-game. Only services
that actually use the GPU are suspended (ring-local); the MQTT bridge, the
dashboard and the snooze bridge do not touch Ollama and keep running.

Modes:
  watch     (default) daemon loop: act on game start/exit
  check     single pass, no side effects except eviction if --evict
  --evict   with check: actually unload
  --once    alias for check --evict (for hooks / manual triggering)
  --no-suspend  do not stop/start the GPU services, only evict models
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

OLLAMA_API = "http://127.0.0.1:11434"
STATE_FILE = Path.home() / ".hermes" / "state" / "game-mode.json"
LOG_FILE = Path.home() / ".hermes" / "logs" / "game-mode.log"

# How often to look for a running game. Games are detected by process name, so
# a 5s poll costs essentially nothing (one /proc scan) while catching a launch
# well before the GPU is contended.
POLL_SECONDS = 5

# Processes that mean "the user is gaming", matched as substrings of the
# executable basename. Three launch paths exist on this box, each with a
# different process shape:
#
#   1. Steam / Proton   -> `reaper` (Steam's per-game process reaper)
#   2. Heroic (Proton)  -> `umu-run`  (Heroic launches through umu, NOT reaper)
#   3. Lutris (Wine)    -> `wine64-preloader` etc.  (Battle.net -> WoW, D4)
#
# Wine's own helper binaries (wineboot, wineserver, services.exe, plugplay)
# appear for *any* Wine prefix, including ones we start for non-game work, so
# the bare `wine` entries are deliberately NOT here. The preloaders are: they
# only appear when a Wine *program* is actually starting.
GAME_PROCESS_NAMES = (
    "reaper",            # Steam / Proton per-game reaper
    "umu-run",           # Heroic + umu (Proton runner used by Heroic)
    "wine64-preloader",  # Wine (Lutris: Battle.net/WoW/D4)
    "wine-preloader",
)

# Windows executables that ARE the game itself (WoW.exe, Diablo IV.exe are the
# actual game binaries under a Wine prefix), so name matching has to happen on
# the .exe, not on the Wine preloader.
GAME_EXE_HINTS = (
    "wow.exe", "wowclassic.exe", "world of warcraft", "wow_",
    "diablo", "d4.exe",
    "battle.net.exe", "agent.exe",
)

# Explicit "these are not games" guard: Steam's own client, the browsers, the
# launcher shells, and Wine's plumbing must never trigger an eviction. A
# launcher being open is NOT gaming — only a game process is.
IGNORE_NAMES = (
    "steamwebhelper",
    "steam",
    "pressure-vessel",
    "wineboot",
    "wineserver",
    "services.exe",
    "plugplay.exe",
    "svchost.exe",
    "explorer.exe",
    "rpcss.exe",
    "conhost.exe",
    "cmd.exe",
    "chromium",
    "chrome",
    "firefox",
    "heroic",          # the Electron launcher itself
    "lutris",          # the launcher shell itself
)

# A launcher (or its updater) on its own is not gaming. These only count when a
# real game process is present alongside; alone they mean "launcher is open".
LAUNCHER_ONLY = ("battle.net.exe", "agent.exe", "heroic.exe", "lutris.exe")

# Background services that use the GPU and should be suspended for the duration
# of a game. Deliberately ONLY the ones that talk to Ollama: ring-local polls
# the cameras and classifies snapshots with qwen2.5vl, so it re-claims VRAM
# every 30s. ring-mqtt, ring-dashboard and ring-snooze-chat do not touch
# Ollama and are left running. Each entry is (unit_name, restore_on_exit).
GPU_SERVICES = ("ring-local.service",)


def log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a") as fh:
        fh.write(f"{stamp} {msg}\n")


def _proc_ident(pid: str) -> tuple[str, str]:
    """(exe_basename, cmdline) for a pid. Empty strings when unreadable."""
    exe = ""
    try:
        exe = os.path.basename(os.readlink(f"/proc/{pid}/exe"))
    except OSError:
        pass
    cmd = ""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        pass
    return exe, cmd


def _classify(exe: str, cmd: str) -> str | None:
    """Return a human label if this process looks like a running game."""
    low_exe = exe.lower()
    low_cmd = cmd.lower()

    # Never match launcher shells / Wine plumbing / browsers. Match the
    # BASENAME only — an earlier version tested the full exe path, and
    # `"steam" in "/steam/ubuntu12_64/reaper"` silently ignored every
    # Steam/Proton game. The ignore list is about what the process IS, so the
    # directory it lives in must not participate.
    base_low = os.path.basename(low_exe)
    if any(ig in base_low for ig in IGNORE_NAMES):
        return None

    # 0) Launcher/updater binaries are never sufficient on their own: Battle.net
    #    and its Agent run whenever the launcher is open, with no game running.
    #    They can only be evidence in combination, so drop them here entirely.
    if any(l in low_exe for l in LAUNCHER_ONLY):
        return None

    # 1) Known launch wrappers (Steam reaper, Heroic's umu-run, Wine preloaders)
    for name in GAME_PROCESS_NAMES:
        if name in low_exe:
            if name == "reaper" and not _steam_running_app():
                # `reaper` also runs when the Steam client alone is starting a
                # non-game app; require a real running app id.
                continue
            return exe

    # 2) The game executable itself under Wine (WoW.exe, Diablo IV.exe).
    #    Match on exe first, then cmdline, since Wine may report the game exe
    #    only inside the command line with a translator process as `exe`.
    for hint in GAME_EXE_HINTS:
        if hint in low_exe:
            return exe
        if hint in low_cmd:
            # Require the hint to appear as a path/filename token, so article
            # prose in an unrelated cmdline cannot false-match.
            if (f"/{hint}" in low_cmd or f"\\{hint}" in low_cmd
                    or f" {hint}" in low_cmd or low_cmd.endswith(hint)):
                return f"{os.path.basename(hint)} (cmdline)"
    return None


def running_games() -> list[str]:
    """Return labels of likely game processes. Cheap /proc scan."""
    found: list[str] = []
    for pid_dir in os.listdir("/proc"):
        if not pid_dir.isdigit():
            continue
        exe, cmd = _proc_ident(pid_dir)
        if not exe and not cmd:
            continue
        label = _classify(exe, cmd)
        if label and label not in found:
            found.append(label)
    return found


def _steam_running_app() -> str | None:
    """Best-effort: the app id Steam currently considers running, if any.

    Reads the registry.vdf Steam writes, which records RunningAppID. Absent on
    a non-Steam box; every failure path returns None rather than raising.
    """
    for cand in (
        Path.home() / ".local/share/Steam/registry.vdf",
        Path.home() / ".steam/steam/registry.vdf",
    ):
        try:
            text = cand.read_text(errors="ignore")
        except OSError:
            continue
        import re
        m = re.search(r'"RunningAppID"\s+"(\d+)"', text)
        if m and m.group(1) != "0":
            return m.group(1)
    return None


def ollama_resident() -> list[str]:
    try:
        with urllib.request.urlopen(f"{OLLAMA_API}/api/ps", timeout=10) as r:
            return [m.get("name") or m.get("model")
                    for m in json.loads(r.read()).get("models", [])]
    except Exception:
        return []


def gpu_used() -> int | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10).stdout.strip().splitlines()
        return int(out[0])
    except Exception:
        return None


def evict() -> str:
    """Drop every resident model, then wait for the driver to reclaim."""
    resident = ollama_resident()
    if not resident:
        return "nothing resident"
    dropped, failed = [], []
    for name in resident:
        try:
            payload = json.dumps({"model": name, "keep_alive": 0}).encode()
            req = urllib.request.Request(
                f"{OLLAMA_API}/api/generate", data=payload,
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=60).read()
            dropped.append(name)
        except Exception as exc:
            failed.append(f"{name}({type(exc).__name__})")

    before = gpu_used()
    after = before
    # Ollama reports the model gone instantly; the driver reclaims on a lag
    # (~8s observed, 20037 -> 1135 MiB). Poll so we log the settled figure.
    for _ in range(15):
        time.sleep(1)
        if not ollama_resident():
            after = gpu_used()
            break
    bits = []
    if dropped:
        bits.append(f"unloaded {', '.join(dropped)}")
    if failed:
        bits.append(f"FAILED {', '.join(failed)}")
    if before is not None and after is not None:
        bits.append(f"VRAM {before}->{after} MiB")
    return "; ".join(bits) or "no change"


def _systemctl(*args: str, timeout: int = 30) -> tuple[int, str]:
    """Run a `systemctl --user` command. Never raises."""
    try:
        p = subprocess.run(["systemctl", "--user", *args],
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as exc:
        return -1, f"{type(exc).__name__}: {exc}"


def service_active(unit: str) -> bool:
    code, _ = _systemctl("is-active", unit)
    return code == 0


def suspend_services() -> list[str]:
    """Stop the GPU-using services. Returns the units actually stopped.

    Only units that were active are stopped and recorded, so a unit that was
    already off (e.g. the user disabled it) is never started behind their back.
    """
    stopped: list[str] = []
    for unit in GPU_SERVICES:
        if not service_active(unit):
            continue
        code, out = _systemctl("stop", unit)
        if code == 0:
            stopped.append(unit)
        else:
            log(f"suspend {unit} FAILED: {out}")
    return stopped


def restore_services(units: list[str]) -> None:
    """Start the units we stopped. Only restores what we suspended."""
    for unit in units:
        code, out = _systemctl("start", unit)
        if code == 0:
            log(f"restored {unit}")
        else:
            log(f"restore {unit} FAILED: {out}")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_FILE)


def watch(no_suspend: bool = False) -> None:
    log("game-mode-guard started (poll %ss%s)" % (
        POLL_SECONDS, ", no-suspend" if no_suspend else ""))
    state = load_state()
    active = state.get("game_active", False)
    # Units we stopped for the CURRENT game, persisted so a daemon restart
    # mid-game still restores them on exit instead of leaving Ring down.
    suspended = list(state.get("suspended", []))
    while True:
        try:
            games = running_games()
            now_active = bool(games)
            if now_active and not active:
                app = _steam_running_app()
                label = ",".join(games) + (f" appid={app}" if app else "")
                log(f"GAME START ({label})")
                if not no_suspend:
                    suspended = suspend_services()
                    if suspended:
                        log(f"suspended: {', '.join(suspended)}")
                log(f"evict: {evict()}")
                active = True
                save_state({"game_active": True, "since": time.time(),
                            "games": games, "appid": app,
                            "suspended": suspended})
            elif now_active:
                # Reconcile on every tick, including the first tick after a
                # daemon restart mid-game: the transition branch is skipped
                # then, so a stale state file must not leave the GPU services
                # running. Suspend anything still up, then evict anything that
                # came back.
                app = _steam_running_app()
                if not no_suspend:
                    still_up = [u for u in GPU_SERVICES if service_active(u)]
                    if still_up:
                        stopped = suspend_services()
                        suspended = sorted(set(suspended) | set(stopped))
                        if stopped:
                            log(f"suspended (reconcile): {', '.join(stopped)}")
                            save_state({"game_active": True,
                                        "since": state.get("since", time.time()),
                                        "games": games, "appid": app,
                                        "suspended": suspended})
                resident = ollama_resident()
                if resident:
                    log(f"model reloaded mid-game ({', '.join(resident)}) "
                        f"-> re-evicting")
                    log(f"evict: {evict()}")
            elif active:
                log("game exit -> restoring services and allowing local models")
                if suspended:
                    restore_services(suspended)
                suspended = []
                active = False
                save_state({"game_active": False, "since": time.time(),
                            "suspended": []})
        except Exception as exc:  # a crash loop must not be silent
            log(f"error: {type(exc).__name__}: {exc}")
        time.sleep(POLL_SECONDS)


def main() -> int:
    ap = argparse.ArgumentParser(description="Free the GPU when a game launches")
    ap.add_argument("mode", nargs="?", default="watch",
                    choices=["watch", "check", "status"])
    ap.add_argument("--evict", action="store_true",
                    help="with check: actually unload resident models")
    ap.add_argument("--once", action="store_true",
                    help="check and evict if a game is running (hook-friendly)")
    ap.add_argument("--no-suspend", action="store_true",
                    help="watch: only evict models, never stop GPU services")
    args = ap.parse_args()

    if args.mode == "watch":
        try:
            watch(no_suspend=args.no_suspend)
        except KeyboardInterrupt:
            log("game-mode-guard stopped")
            return 0
        return 0

    games = running_games()
    resident = ollama_resident()
    state = load_state()
    if args.mode == "status":
        print(f"game_active : {state.get('game_active', False)}")
        print(f"games       : {', '.join(games) if games else 'none'}")
        print(f"resident    : {', '.join(resident) if resident else 'none'}")
        print(f"vram_used   : {gpu_used()} MiB")
        print(f"suspended   : {', '.join(state.get('suspended') or []) or 'none'}")
        for unit in GPU_SERVICES:
            print(f"  {unit:28s} {'active' if service_active(unit) else 'inactive'}")
        return 0

    # check
    print(f"games running : {', '.join(games) if games else 'none'}")
    print(f"resident      : {', '.join(resident) if resident else 'none'}")
    if (args.evict or args.once) and games and resident:
        print("evict         :", evict())
    elif (args.evict or args.once):
        print("evict         : nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main())
