#!/usr/bin/env python3
"""Unit-test the game-mode-guard classifier against real process shapes.

Exercises _classify() directly so we test detection logic without launching
real games. Covers the three launch paths on this box (Steam, Heroic/umu,
Lutris/Wine) plus the false-positive guards (launchers open, Wine plumbing,
browsers).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".hermes" / "scripts"))
import importlib.util

spec = importlib.util.spec_from_file_location(
    "gmg", str(Path.home() / ".hermes" / "scripts" / "game-mode-guard.py"))
gmg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gmg)

# (description, exe, cmdline, should_match)
CASES = [
    # --- SHOULD MATCH: real game launch paths ---
    ("Steam/Proton game", "/steam/ubuntu12_64/reaper", "reaper 730", True),
    ("Heroic via umu", "/usr/bin/umu-run", "umu-run /path/Wow.exe", True),
    ("Lutris wine preloader", "/usr/bin/wine64-preloader", "wine64-preloader", True),
    ("WoW.exe as exe", "/games/bn/drive_c/Wow.exe", "", True),
    ("Diablo IV via cmdline",
     "/usr/bin/wine64", "wine64 C:\\Games\\Diablo IV.exe", True),
    ("WowClassic.exe", "/x/drive_c/WowClassic.exe", "", True),

    # --- SHOULD NOT MATCH: launchers, plumbing, browsers ---
    ("Steam client", "/path/steam", "steam", False),
    ("Steam webhelper", "/path/steamwebhelper", "steamwebhelper", False),
    ("Heroic Electron shell", "/usr/lib/heroic/heroic", "heroic", False),
    ("Lutris shell", "/usr/bin/lutris", "lutris", False),
    ("Battle.net launcher alone", "/x/drive_c/Battle.net.exe", "", False),
    ("Agent updater alone", "/x/drive_c/Agent.exe", "", False),
    ("Wine server", "/usr/bin/wineserver", "wineserver", False),
    ("Wine services.exe", "/x/drive_c/windows/services.exe", "", False),
    ("explorer.exe in prefix", "/x/drive_c/windows/explorer.exe", "", False),
    ("Ollama itself", "/usr/local/lib/ollama/llama-server", "llama-server", False),
    ("Chrome", "/opt/google/chrome/chrome", "chrome", False),
    ("Python script", "/usr/bin/python3", "python3 probe.py", False),
]


def main() -> int:
    failures = 0
    # `reaper` requires a real running Steam app id. The test box has none, so
    # monkeypatch the lookup for the duration to isolate the reaper logic.
    real_steam = gmg._steam_running_app
    for desc, exe, cmd, want in CASES:
        gmg._steam_running_app = (lambda: "730") if desc.startswith("Steam/Proton") \
            else real_steam
        got = gmg._classify(exe, cmd)
        matched = got is not None
        ok = matched == want
        if not ok:
            failures += 1
        mark = "ok  " if ok else "FAIL"
        verdict = "MATCH " if matched else "ignore"
        print(f"  [{mark}] {verdict} {desc:32s} -> {got!r}")
    gmg._steam_running_app = real_steam
    print()
    if failures:
        print(f"{failures}/{len(CASES)} FAILED")
        return 1
    print(f"all {len(CASES)} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
