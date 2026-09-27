"""
Name sets — the 50 position names (10 × 5 cameras) plus the 5 camera names,
saved as JSON files in the user's Documents folder.

Since 2026-09-26 the names themselves are the HUB's, shared by every PC app
and phone (config/name_sync.py).  What is left here:
  - Save / Load of whole sets, for keeping one per show.  Load sends the set
    to the hub, so every device takes it.
  - hub_names.json: the names this PC last heard from the hub, so it opens
    showing them before the hub has answered, and still has them when the hub
    cannot be reached.

With a hub whose firmware predates names, the app falls back to the model it
had before, which the user specified in July:
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
# The hub's names as this PC last heard them.  Its existence also records that
# this PC has met a hub that keeps names — see name_sync.NameSync.migrated.
CACHE_PATH   = NAMES_DIR / "hub_names.json"


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


def entries(store, config, cams=range(1, NUM_MOUNTS + 1)) -> list[tuple]:
    """Every name for these cameras as (cam, slot, name) for the hub.

    A default goes as "" — the hub keeps no name, and every client shows its
    own default — so a camera still called "Cam 2", or a slot still called
    "3", does not become a stored name that happens to look like one.
    """
    from comms.protocol import NAME_SLOT_CAMERA
    out = []
    for m in cams:
        lbl = config.mount(m).label.strip()
        out.append((m, NAME_SLOT_CAMERA, "" if lbl == f"Cam {m}" else lbl))
        for s in range(NUM_POSITIONS):
            name = store.get_label(m, s)
            out.append((m, s, "" if name == str(s + 1) else name))
    return out


def load_startup(store, config) -> str:
    """What the app shows before the hub has answered.

    A PC that has met a hub keeping names opens on what that hub last said;
    one that has not keeps the July behaviour and opens on Default.json.
    Returns which: "cache" or "default".
    """
    if CACHE_PATH.exists():
        try:
            load_from(CACHE_PATH, store, config)
            return "cache"
        except Exception as e:
            log.warning("Could not read %s (%s) — using Default.json", CACHE_PATH, e)
    load_default(store, config)
    return "default"


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
