"""The focus button at the end of each camera row — and the switch that removes it.

2026-09-25: a crosshair between button 10 and the slider dial fires one
autofocus on that row's camera, the command Camera Control's Auto Focus sends.
It only means something with Blackmagic cameras paired to the mounts, and the
repo is public, so it is a Config option, off unless ticked.

WHAT THIS TEST IS PROTECTING.

  off is the old layout       unticked, the grid is the layout from before the
                              button existed — test_grid_scales pins that one
  the switch works both ways  _relayout() skips a height it has already laid
                              out, so a toggle at the same window size did
                              nothing until something forced it
  on makes room round it      the button sits between button 10 and the slider
                              dial; the buttons move out to the left end and the
                              dials to the right, both ends match, and what the
                              ends give up is room either side of the button
  each row its own camera     a tap sends that row's mount id and no other
  grey means untappable       no camera link, no command
  ready means linked          connected AND the camera link reports True —
                              "unpaired" and "no camera support" are not True
  the setting survives        Config ticks it, OK saves it, the next start
                              loads it; a config file from before has it off

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

from PyQt6.QtWidgets import QApplication, QWidget
from PyQt6.QtCore import QPoint
from PyQt6.QtTest import QTest
app = QApplication.instance() or QApplication([])

from ui.widgets.position_grid import (PositionGrid, _FOCUS_FRAC, _BTN_ASPECT,
                                      _FOCUS_PAD_FRAC, _ROW_MARGIN_FRAC,
                                      _ROW_MARGIN_FRAC_FOCUS)
from config.position_store import PositionStore
import config.mount_config as mc

GRID = PositionGrid(PositionStore())
GRID.show()


def bars(h):
    return round(h * 150 / 1080)          # as test_grid_scales: the top/bottom bars


def geometry(w, h, grid=None):
    grid = grid or GRID
    grid.resize(w, h - bars(h))
    for _ in range(3):
        app.processEvents()
        grid.layout().activate()
    def x(wid):
        p = wid.mapTo(grid._row_containers[1], QPoint(0, 0))
        return p.x(), p.x() + wid.width()
    g = dict(btn1=x(grid._buttons[(1, 0)]), btn2=x(grid._buttons[(1, 1)]),
             btn10=x(grid._buttons[(1, 9)]), sl=x(grid._sl_dials[1]),
             pt=x(grid._pt_dials[1]), row_w=grid._row_containers[1].width(),
             btn_h=grid._buttons[(1, 0)].height(),
             btn_w=grid._buttons[(1, 0)].width(),
             dial=grid._pt_dials[1].width(),
             gap=grid._row_layouts[1].spacing())
    # A hidden widget keeps whatever size it last had and takes no room, so
    # its size is only part of the layout while it is shown.
    f = grid._focus_btns[1]
    g["focus"] = x(f) if f.isVisible() else None
    g["focus_size"] = (f.width(), f.height()) if f.isVisible() else None
    return g


def check_room(g, where):
    """Ticked: the ends match, and the room they gave up is round the button."""
    left, right = g["btn1"][0], g["row_w"] - g["pt"][1]
    before, after = g["focus"][0] - g["btn10"][1], g["sl"][0] - g["focus"][1]
    assert abs(left - right) <= 1, \
        f"{where}: {left}px at the left end against {right}px at the right — the " \
        "rounding spare collected at one end instead of going round the button"
    assert abs(before - after) <= 1, \
        f"{where}: {before}px before the focus button and {after}px after it"
    assert min(before, after) >= g["gap"] + round(g["btn_h"] * _FOCUS_PAD_FRAC), \
        f"{where}: {before}/{after}px round the focus button is not the row's " \
        f"{g['gap']}px gap plus its pad — the ends kept the room"
    assert max(left, right) <= round(0.12 * g["btn_h"]) + 1, \
        f"{where}: {left}/{right}px at the ends of the row — they were meant to move " \
        "out towards the sides, leaving the room round the focus button"
    assert max(left, right) < min(before, after), \
        f"{where}: the ends ({left}/{right}px) have more room than the focus button " \
        f"({before}/{after}px)"


# ---- 1. off by default, and off is the old layout --------------------------
print("1. unticked:")
assert mc.AppConfig().focus_buttons is False, \
    "focus buttons are on by default — a rig without Blackmagic cameras gets a dead control"
off = geometry(1920, 1080)
assert off["focus"] is None, "the focus button shows on a grid nobody switched it on for"
assert all(not b.isVisible() for b in GRID._focus_btns.values())
assert off["btn10"][1] + off["gap"] == off["sl"][0], \
    "with the button hidden, button 10 and the slider dial are not one gap apart —\n" \
    "    the hidden button is still taking room, so 'off' is not the old layout"
assert _ROW_MARGIN_FRAC == 0.25, "the unticked row margin moved — that is the old look"
print(f"   hidden, and the row is the old one: button 1 at {off['btn1'][0]}, "
      f"{off['gap']}px gaps   OK")

# ---- 2. on makes room for it -------------------------------------------------
print("\n2. ticked:")
GRID.set_focus_buttons(True)
on = geometry(1920, 1080)
assert on["focus"] is not None, "ticking the option did not show the button — " \
    "the relayout was skipped at the same window size"
fx0, fx1 = on["focus"]
assert on["btn10"][1] < fx0 and fx1 < on["sl"][0], \
    f"the focus button is not between button 10 and the slider dial: {on}"
check_room(on, "1920x1080")
assert abs(on["focus_size"][0] - round(on["btn_h"] * _FOCUS_FRAC)) <= 1 and \
       on["focus_size"][0] == on["focus_size"][1], \
    f"the focus button is {on['focus_size']}, not a {round(on['btn_h'] * _FOCUS_FRAC)}px circle"
# The room comes from the gaps and the ends, never from the controls: the
# layout squeezes items silently when the sums are wrong, so check the sizes.
assert (on["btn_w"], on["btn_h"], on["dial"]) == (off["btn_w"], off["btn_h"], off["dial"]), \
    f"the buttons or dials changed size to make room: button {off['btn_w']}x{off['btn_h']} -> " \
    f"{on['btn_w']}x{on['btn_h']}, dial {off['dial']} -> {on['dial']}"
assert on["btn1"][0] < off["btn1"][0], "the buttons did not move left to make room"
assert on["pt"][1] > off["pt"][1], "the dials did not move right to make room"
assert on["btn2"][0] - on["btn1"][0] < off["btn2"][0] - off["btn1"][0], \
    "the buttons did not close up to make room"
assert on["btn1"][0] >= 0.08 * on["btn_h"], "button 1 is pinned against the band's edge"
assert _ROW_MARGIN_FRAC_FOCUS < _ROW_MARGIN_FRAC
print(f"   button 1 {off['btn1'][0]}->{on['btn1'][0]}, pitch "
      f"{off['btn2'][0] - off['btn1'][0]}->{on['btn2'][0] - on['btn1'][0]}, "
      f"{fx0 - on['btn10'][1]}px either side of the focus button, "
      f"dials end {off['pt'][1]}->{on['pt'][1]} of {on['row_w']}   OK")

# ---- 3. the switch works both ways, at the same size ------------------------
print("\n3. toggling at the same window size:")
GRID.set_focus_buttons(False)
assert geometry(1920, 1080) == off, "unticking did not restore the old layout exactly"
GRID.set_focus_buttons(True)
assert geometry(1920, 1080) == on, "ticking again did not give the same layout"
print("   on -> off -> on returns the same pixels each way   OK")

# ---- 4. on, it still fits every screen -----------------------------------------
print("\n4. ticked, other screens:")
# A fresh grid for each: once laid out large the grid cannot yet shrink back
# below the gaps it laid out there (an older bug, not this one's to catch),
# so one grid walked through the sizes would measure that instead.
for w, h in ((1024, 600), (1366, 768), (1512, 945), (2560, 1440), (3840, 2160)):
    fresh = PositionGrid(PositionStore())
    fresh.set_focus_buttons(True)
    fresh.show()
    g = geometry(w, h, fresh)
    assert g["row_w"] <= w, f"{w}x{h}: the row is {g['row_w']}px on a {w}px screen"
    assert g["focus"] is not None and g["btn10"][1] < g["focus"][0] < g["focus"][1] < g["sl"][0], \
        f"{w}x{h}: the focus button is not between button 10 and the dials: {g}"
    assert g["pt"][1] <= g["row_w"], f"{w}x{h}: the pan/tilt dial runs off the row"
    check_room(g, f"{w}x{h}")
    assert abs(g["btn_w"] / g["btn_h"] - _BTN_ASPECT) < 0.06, \
        f"{w}x{h}: the buttons were squeezed to {g['btn_w']}x{g['btn_h']} to fit the focus button"
    print(f"   {w}x{h}: focus {g['focus_size'][0]}px, dials end {g['pt'][1]} of {g['row_w']}")
    fresh.close()
print("   fits   OK")

# ---- 5. a tap is that row's camera, and only when it can take it ---------
print("\n5. tapping:")
sent = []
GRID.focus_requested.connect(sent.append)
for mid in range(1, 6):
    GRID._focus_btns[mid].click()
assert sent == [], f"a grey focus button sent {sent} — no camera link, no command"
for mid in range(1, 6):
    GRID.set_cam_ready(mid, True)
    GRID._focus_btns[mid].click()
assert sent == [1, 2, 3, 4, 5], f"rows sent {sent}, not their own mount ids"
assert GRID._focus_btns[5]._sent, "a tap did not show that the command went"
QTest.qWait(500)
assert not GRID._focus_btns[5]._sent, "the sent flash never ends"
GRID.set_cam_ready(3, False)
GRID._focus_btns[3].click()
assert sent == [1, 2, 3, 4, 5] and not GRID._focus_btns[3].isEnabled(), \
    "a camera that dropped its link still takes a tap"
print("   grey sends nothing; each row sends its own mount; the flash ends   OK")

# ---- 6. what "ready" means, from the main window ---------------------------
print("\n6. the main window's link check:")
import ui.main_window as mw


class _St:
    def __init__(self, c): self.connected = c


class _FakeMM:
    def __init__(self, conn): self._c = conn
    def state(self, m): return _St(self._c[m])


class _FakeBridge:
    def __init__(self, link): self._l = link
    def cam_ble_link(self, m): return self._l.get(m)


class _FakeGrid:
    def __init__(self): self.ready = {}; self.focus = []
    def set_cam_ready(self, m, r): self.ready[m] = r
    def set_focus_buttons(self, s): self.focus.append(s)
    def set_has_slider(self, *a): pass
    def set_look_at_mode(self, *a): pass


fake = type("W", (), {})()
fake._mm = _FakeMM({1: True, 2: True, 3: True, 4: True, 5: False})
fake._bridge = _FakeBridge({1: True, 2: False, 3: "unpaired", 4: None, 5: True})
fake._grid = _FakeGrid()
mw.MainWindow._refresh_cam_links(fake)
assert fake._grid.ready == {1: True, 2: False, 3: False, 4: False, 5: False}, \
    f"ready should be connected AND linked, nothing else: {fake._grid.ready}"
print("   linked only when connected and the link is True — off, unpaired,\n"
      "   no camera support and an offline mount are all grey   OK")

# ---- 7. the setting survives, and the grid follows it ----------------------
print("\n7. Config:")
with tempfile.TemporaryDirectory() as td:
    mc.CONFIG_FILE = pathlib.Path(td) / "config.json"
    mc.CONFIG_FILE.write_text('{"cv_mount_id": 2}')
    assert mc.load_config().focus_buttons is False, \
        "a config file from before the option has it on"
    from ui.dialogs.config_dialog import ConfigDialog

    class _Any:
        def __getattr__(self, n): return _Any()
        def __call__(self, *a, **k): return _Any()
        def __iter__(self): return iter(())
        def __bool__(self): return False

    cfg = mc.load_config()
    dlg = ConfigDialog.__new__(ConfigDialog)
    QWidget.__init__(dlg)
    dlg._config, dlg._store, dlg._mm, dlg._bridge = cfg, PositionStore(), _Any(), _Any()
    tab = dlg._build_general_tab()    # held: its checkbox dies with it
    assert not dlg._focus_check.isChecked(), "the checkbox does not show the setting"
    dlg._focus_check.setChecked(True)
    dlg._apply()
    assert mc.load_config().focus_buttons is True, "ticking it and pressing OK did not save it"

# The main window applies it when Config is accepted.
fake._config = cfg
fake._conn_snapshot = (cfg.bridge_mode, cfg.bridge_host, cfg.bridge_tcp_port, cfg.bridge_port)
fake._bridge.connected = True
fake._active_la_subject = {}
fake._grid.focus = []
mw.MainWindow._on_config_accepted(fake)
assert fake._grid.focus == [True], "accepting Config does not apply the option to the grid"
src = (REPO / "pc_app/ui/main_window.py").read_text()
assert "self._grid.set_focus_buttons(self._config.focus_buttons)\n        root.addWidget(self._grid" in src, \
    "the grid is not told the setting when the window is built"
assert "self._grid.focus_requested.connect(self._mm.send_cam_autofocus)" in src, \
    "a tap is not wired to the autofocus command"
print("   old files load it off; OK saves it; the grid follows at start and on OK   OK")

print("\nALL CHECKS PASSED")
