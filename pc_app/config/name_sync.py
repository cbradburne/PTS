"""
NameSync — camera and position names, kept on the hub and shared by every
device (2026-09-26).

The hub holds the names (firmware/shared/names.h).  This app shows what the hub
says, sends it every edit, and applies whatever any device changes — the hub
sends every client each change.  Nothing here assumes this app shows every
camera: it asks for, and applies, the cameras in `shown`, keyed by camera
number, and keeps anything else the hub sends without drawing it.

Three modes:
  "unknown"  just started, or the link has just come back, and the hub has not
             answered yet.  Edits go to the hub AND to temp.json, so they
             survive whichever way it turns out.
  "hub"      the hub answered: it keeps the names.  Edits go to the hub; a copy
             of what it says is kept in Documents/PTS/hub_names.json so the
             next launch opens on it.
  "local"    connected for LOCAL_AFTER_S without an answer: hub firmware from
             before names.  Everything works as it did — edits to temp.json,
             Default.json at startup — until a hub that answers turns up.

Moving to the hub.  The first time this PC meets a hub that keeps names and
finds it holds NONE, it sends the hub the names it is showing — that is how the
names on the production PC reach the hub the day it is flashed.  Once only
(hub_names.json records it), and never over names the hub already has.

An edit made while the link is down is kept and sent when it returns, ahead of
the request for the hub's names, so the answer includes it.
"""
from __future__ import annotations

import logging
import time

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from comms.protocol import NAME_SLOT_CAMERA, NAMES_SLOTS, fit_name
from config import name_store

log = logging.getLogger(__name__)

LOCAL_AFTER_S  = 15.0   # connected this long with no answer → firmware without names
RETRY_AFTER_S  = 4.0    # a camera asked for and not heard → ask for it again
TICK_MS        = 1000


class NameSync(QObject):
    changed      = pyqtSignal()      # names applied: redraw cameras and positions
    mode_changed = pyqtSignal(str)   # "unknown" | "hub" | "local"

    def __init__(self, mm, bridge, store, config, shown=None, clock=None,
                 timer: bool = True, parent=None):
        super().__init__(parent)
        self._mm, self._bridge = mm, bridge
        self._store, self._config = store, config
        self.shown = list(shown) if shown is not None else list(range(1, 6))
        self._now = clock or time.monotonic
        self.mode = "unknown"
        self._rev = None
        self._link_up = False
        self._link_up_at = 0.0
        self._asked_at = -1e9
        self._want: set[int] = set()
        self._pending: dict[tuple[int, int], str] = {}   # edits the hub has not had
        self.hub_names: dict[int, tuple[str, list[str]]] = {}   # every camera heard
        self.migrated = name_store.CACHE_PATH.exists()
        mm.names_received.connect(self._on_names)
        mm.names_rev_received.connect(self._on_rev)
        self._cache_timer = QTimer(self)
        self._cache_timer.setSingleShot(True)
        self._cache_timer.setInterval(1000)
        self._cache_timer.timeout.connect(self._save_cache)
        if timer:
            self._timer = QTimer(self)
            self._timer.timeout.connect(self.tick)
            self._timer.start(TICK_MS)

    # ------------------------------------------------------------------
    # Edits — the only way the rest of the app changes a name
    # ------------------------------------------------------------------
    def set_camera_name(self, cam: int, name: str) -> None:
        name = fit_name(name).decode("utf-8", "replace")
        self._config.mount(cam).label = name
        self._edit([(cam, NAME_SLOT_CAMERA, name)])

    def set_position_name(self, cam: int, slot: int, name: str) -> None:
        """A blank name, or the slot's own number, is the default: no name."""
        name = fit_name(name).decode("utf-8", "replace")
        if name in ("", str(slot + 1)):
            name = ""
        self._store.set_label(cam, slot, name or str(slot + 1))
        self._edit([(cam, slot, name)])

    def clear_positions(self, cam: int, slots) -> None:
        """Names back to the slot numbers — a cleared bank takes its names too."""
        entries = []
        for s in slots:
            self._store.set_label(cam, s, str(s + 1))
            entries.append((cam, s, ""))
        self._edit(entries)

    def upload_all(self) -> None:
        """Every name this app shows, to the hub — a Load, or the move to it."""
        self._edit(name_store.entries(self._store, self._config, self.shown))

    def _edit(self, entries) -> None:
        if self.mode != "hub":
            # The July model, for firmware without names — and while unknown,
            # a copy in case that is what this hub turns out to be.
            name_store.save_temp(self._store, self._config)
            if self.mode == "local":
                return
        for cam, slot, name in entries:
            self._pending[(cam, slot)] = name
        if self._link_up:
            self._flush()

    def _flush(self) -> None:
        if not self._pending:
            return
        self._mm.send_names([(c, s, n) for (c, s), n in self._pending.items()])
        self._pending.clear()

    # ------------------------------------------------------------------
    # The link, and what the hub says
    # ------------------------------------------------------------------
    def tick(self) -> None:
        now = self._now()
        if not self._bridge.connected:
            self._link_up = False
            return
        if not self._link_up:                 # just connected, or back
            self._link_up = True
            self._link_up_at = now
            if self.mode == "local":
                self._set_mode("unknown")     # a reflashed hub may answer now
            self._flush()                     # edits first: the answer then has them
            self._ask(self.shown)
            return
        if self.mode == "unknown" and now - self._link_up_at >= LOCAL_AFTER_S:
            self._set_mode("local")
            log.info("The hub keeps no names (firmware from before 2026-09-26) — "
                     "names stay on this PC, as before")
            return
        if self._want and now - self._asked_at >= RETRY_AFTER_S:
            self._ask(sorted(self._want))     # an answer went missing

    def _ask(self, cams) -> None:
        cams = [c for c in cams]
        self._want |= set(cams)
        self._asked_at = self._now()
        self._mm.request_names(cams)

    def _set_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.mode = mode
            self.mode_changed.emit(mode)

    def _hub_answered(self, info=None) -> None:
        """The hub keeps names.  Decide, once, whether this PC's move to it."""
        if self.mode == "hub":
            return
        if not self.migrated and info is None:
            # A camera's names arrived ahead of the rev.  Only the rev says
            # whether the hub holds any names at all, and applying an EMPTY
            # hub's answer now would wipe the very names about to be sent to
            # it — so they are held, undrawn, until it comes.
            return
        self._set_mode("hub")
        if not self.migrated and info.get("named") == 0:
            log.info("The hub has no names yet — sending it this PC's")
            self.upload_all()
        else:
            log.info("Names are kept on the hub and shared with every device")
            for cam, (name, slots) in list(self.hub_names.items()):
                self._apply(cam, name, slots)
        self.migrated = True
        self._cache_timer.start()             # hub_names.json: this PC has moved

    def _on_rev(self, info: dict) -> None:
        self._hub_answered(info)
        if info["rev"] != self._rev:
            # Something changed, and whether its CMD_NAMES reached us cannot be
            # told from here — so ask again for everything shown, even with an
            # answer outstanding: waiting on that one would miss a change to a
            # different camera.  After connecting this repeats the first
            # request once; names change rarely, and a repeat is cheap.
            self._rev = info["rev"]
            self._ask(self.shown)

    def _on_names(self, cam: int, name: str, slots: list) -> None:
        self._want.discard(cam)
        self.hub_names[cam] = (name, list(slots))
        self._hub_answered()
        if self.mode == "hub":
            self._apply(cam, name, slots)

    def _apply(self, cam: int, name: str, slots: list) -> None:
        if cam not in self.shown:
            return                             # kept, not drawn
        changed = False
        if (cam, NAME_SLOT_CAMERA) not in self._pending \
                and self._config.mount(cam).label != name:
            self._config.mount(cam).label = name
            changed = True
        for s in range(min(NAMES_SLOTS, len(slots))):
            if (cam, s) in self._pending:
                continue                       # our edit is on its way: keep it
            want = slots[s] or str(s + 1)
            if self._store.get_label(cam, s) != want:
                self._store.set_label(cam, s, want)
                changed = True
        if changed:
            self.changed.emit()
        if self.mode == "hub":
            self._cache_timer.start()

    def _save_cache(self) -> None:
        try:
            name_store.save_to(name_store.CACHE_PATH, self._store, self._config)
        except Exception as e:
            log.warning("Could not keep the hub's names in %s: %s",
                        name_store.CACHE_PATH, e)
