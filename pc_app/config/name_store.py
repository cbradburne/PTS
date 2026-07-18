"""
Name sets — the 50 position names (10 × 5 cameras) plus the 5 camera names,
saved as JSON files in the user's Documents folder.

Model (as requested):
  - Files live in ~/Documents/PTS/.
  - Default.json holds the startup names.  On launch the app loads it (and
    creates it, seeded from the current numeric/"Cam N" defaults, if missing).
  - Editing a name writes the current full set to temp.json ONLY — no saved
    file (Default.json or a user file) is touched automatically.
  - The Config dialog's Save / Load / Set Defaults buttons are the only things
    that write Default.json or a user-chosen file.

A name set on disk:
  {
    "cameras":   {"1": "Cam 1", ..., "5": "Cam 5"},
    "positions": {"1": {"1": "1", ..., "10": "10"}, ..., "5": {...}}
  }
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

NUM_MOUNTS    = 5
NUM_POSITIONS = 10

# ~/Documents/PTS — created on demand.  Kept in a PTS subfolder so the generic
# Default.json / temp.json names don't collide with anything in Documents root.
NAMES_DIR    = Path.home() / "Documents" / "PTS"
DEFAULT_PATH = NAMES_DIR / "Default.json"
TEMP_PATH    = NAMES_DIR / "temp.json"


def ensure_dir() -> None:
    NAMES_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Collect / apply against the live PositionStore + AppConfig
# ---------------------------------------------------------------------------

def collect(store, config) -> dict:
    """Read the current names from the PositionStore + AppConfig into a dict."""
    return {
        "cameras": {
            str(m): config.mount_label(m) for m in range(1, NUM_MOUNTS + 1)
        },
        "positions": {
            str(m): {str(s + 1): store.get_label(m, s)
                     for s in range(NUM_POSITIONS)}
            for m in range(1, NUM_MOUNTS + 1)
        },
    }


def apply(data: dict, store, config) -> None:
    """Apply names from a loaded dict into the PositionStore + AppConfig.

    Best-effort and tolerant: missing or malformed entries are left at their
    current values, so a partial/old file never blanks a name.
    """
    cams = data.get("cameras", {}) if isinstance(data, dict) else {}
    for m in range(1, NUM_MOUNTS + 1):
        name = cams.get(str(m))
        if isinstance(name, str):
            config.mount(m).label = name

    poss = data.get("positions", {}) if isinstance(data, dict) else {}
    for m in range(1, NUM_MOUNTS + 1):
        mp = poss.get(str(m), {})
        if not isinstance(mp, dict):
            continue
        for s in range(NUM_POSITIONS):
            name = mp.get(str(s + 1))
            if isinstance(name, str):
                store.set_label(m, s, name)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def save_to(path, store, config) -> None:
    ensure_dir()
    Path(path).write_text(json.dumps(collect(store, config), indent=2),
                          encoding="utf-8")
    log.info("Saved names to %s", path)


def load_from(path, store, config) -> None:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    apply(data, store, config)
    log.info("Loaded names from %s", path)


def save_temp(store, config) -> None:
    """Auto-called on any name edit — working copy only, never a saved set."""
    try:
        save_to(TEMP_PATH, store, config)
    except Exception as e:
        log.warning("Could not write temp names: %s", e)


def save_default(store, config) -> None:
    """'Set Defaults' — make the current names the startup default."""
    save_to(DEFAULT_PATH, store, config)


def load_default(store, config) -> bool:
    """Load Default.json at startup.  If it doesn't exist, create it from the
    current (numeric / 'Cam N') defaults so it exists for next time.  Returns
    True if an existing Default.json was applied."""
    ensure_dir()
    if DEFAULT_PATH.exists():
        try:
            load_from(DEFAULT_PATH, store, config)
            return True
        except Exception as e:
            log.error("Failed to load %s: %s", DEFAULT_PATH, e)
            return False
    try:
        save_default(store, config)   # seed it
        log.info("Created %s from current defaults", DEFAULT_PATH)
    except Exception as e:
        log.warning("Could not create %s: %s", DEFAULT_PATH, e)
    return False
