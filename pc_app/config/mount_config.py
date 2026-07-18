"""
Per-mount configuration — persisted to JSON on disk.

Stores:
  - Orientation flags (pan_invert, slider_invert)
  - 4 speed presets per axis group
  - StallGuard thresholds
  - Last known limits (slider, zoom)
  - CV tracking mount assignment
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

CONFIG_FILE = Path(__file__).parent.parent / "data" / "config.json"


@dataclass
class SpeedPreset:
    max_speed: int    # physical: deg/sec (PAN/TILT/ZOOM) or mm/sec (SLIDER)
    accel:     int    # physical: deg/sec² (PAN/TILT/ZOOM) or mm/sec² (SLIDER)


@dataclass
class AxisGroupPresets:
    preset_1: SpeedPreset = field(default_factory=lambda: SpeedPreset(1,  1))
    preset_2: SpeedPreset = field(default_factory=lambda: SpeedPreset(5,  5))
    preset_3: SpeedPreset = field(default_factory=lambda: SpeedPreset(10, 10))
    preset_4: SpeedPreset = field(default_factory=lambda: SpeedPreset(15, 15))

    def get(self, preset: int) -> SpeedPreset:
        return getattr(self, f"preset_{preset}")

    def set(self, preset: int, sp: SpeedPreset) -> None:
        setattr(self, f"preset_{preset}", sp)


# Default presets for pan/tilt (deg/sec, deg/sec²)
_DEFAULT_PT_PRESETS = lambda: AxisGroupPresets(
    SpeedPreset(1, 1), SpeedPreset(5, 5), SpeedPreset(10, 10), SpeedPreset(15, 15)
)

# Default presets for slider (mm/sec, mm/sec²)
_DEFAULT_SL_PRESETS = lambda: AxisGroupPresets(
    SpeedPreset(1, 1), SpeedPreset(10, 10), SpeedPreset(20, 20), SpeedPreset(40, 40)
)

# Default zoom preset (single, independent — deg/sec, deg/sec²)
_DEFAULT_ZM_PRESET = lambda: SpeedPreset(40, 40)


@dataclass
class MountConfig:
    mount_id:       int
    label:          str  = ""     # display name; falls back to "Cam {mount_id}" if blank
    has_slider:     bool = False
    pan_invert:     bool = False
    tilt_invert:    bool = False
    slider_invert:  bool = False
    zoom_invert:    bool = False
    lanc_zoom:      bool = False
    look_at_mode:   bool = False
    pan_tilt_presets: AxisGroupPresets = field(default_factory=_DEFAULT_PT_PRESETS)
    slider_presets:   AxisGroupPresets = field(default_factory=_DEFAULT_SL_PRESETS)
    zoom_preset:      SpeedPreset      = field(default_factory=_DEFAULT_ZM_PRESET)
    stall_threshold_slider: int = 80
    stall_threshold_zoom:   int = 80
    slider_min: Optional[int] = None
    slider_max: Optional[int] = None
    zoom_min:   Optional[int] = None
    zoom_max:   Optional[int] = None


@dataclass
class AppConfig:
    mounts: dict[int, MountConfig] = field(default_factory=dict)
    cv_mount_id: int = 1               # which mount uses CV tracking
    bridge_mode: str = "tcp"           # "tcp" or "serial"
    bridge_host: str = "192.168.4.1"   # hub AP IP (TCP mode)
    bridge_tcp_port: int = 7777        # hub TCP port
    bridge_port: str = ""              # serial port name (serial mode)
    joystick_deadzone: float = 0.08
    osc_enabled: bool = True           # OSC control server (Companion / QLab)
    osc_port: int = 9700               # UDP port for /pts/... addresses
    virtual_keyboard: bool = True      # on-screen keyboard for text entry
                                       # (touchscreen setups with no keyboard)

    def mount(self, mount_id: int) -> MountConfig:
        if mount_id not in self.mounts:
            self.mounts[mount_id] = MountConfig(mount_id=mount_id)
        return self.mounts[mount_id]

    def mount_label(self, mount_id: int) -> str:
        """Return the display label, falling back to 'Cam N' if unset."""
        lbl = self.mount(mount_id).label.strip()
        return lbl if lbl else f"Cam {mount_id}"


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def _sp_from_dict(d: dict) -> SpeedPreset:
    return SpeedPreset(max_speed=d["max_speed"], accel=d["accel"])

def _agp_from_dict(d: dict) -> AxisGroupPresets:
    return AxisGroupPresets(
        preset_1=_sp_from_dict(d["preset_1"]),
        preset_2=_sp_from_dict(d["preset_2"]),
        preset_3=_sp_from_dict(d["preset_3"]),
        preset_4=_sp_from_dict(d["preset_4"]),
    )

def _mount_from_dict(d: dict) -> MountConfig:
    mc = MountConfig(mount_id=d["mount_id"])
    mc.label         = d.get("label",          "")
    mc.has_slider    = d.get("has_slider",    True)
    mc.pan_invert    = d.get("pan_invert",    False)
    mc.tilt_invert   = d.get("tilt_invert",   False)
    mc.slider_invert = d.get("slider_invert", False)
    mc.zoom_invert   = d.get("zoom_invert",   False)
    mc.lanc_zoom     = d.get("lanc_zoom",     False)
    mc.look_at_mode  = d.get("look_at_mode",  False)
    if "pan_tilt_presets" in d:
        mc.pan_tilt_presets = _agp_from_dict(d["pan_tilt_presets"])
    # New format: separate slider_presets + zoom_preset
    if "slider_presets" in d:
        mc.slider_presets = _agp_from_dict(d["slider_presets"])
    elif "slider_zoom_presets" in d:
        # Migrate old format — use slider_zoom_presets as slider_presets, keep zoom default
        mc.slider_presets = _agp_from_dict(d["slider_zoom_presets"])
    if "zoom_preset" in d:
        mc.zoom_preset = _sp_from_dict(d["zoom_preset"])
    mc.stall_threshold_slider = d.get("stall_threshold_slider", 80)
    mc.stall_threshold_zoom   = d.get("stall_threshold_zoom",   80)
    mc.slider_min = d.get("slider_min")
    mc.slider_max = d.get("slider_max")
    mc.zoom_min   = d.get("zoom_min")
    mc.zoom_max   = d.get("zoom_max")
    return mc


def load_config() -> AppConfig:
    """Load app-level settings from JSON.

    Mount-specific settings (speed presets, orientation, stall thresholds) are
    stored exclusively in each mount's EEPROM and are fetched live via
    CMD_GET_CONFIG when the config dialog opens.  Only the display label and
    app-level connection settings are persisted here.
    """
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Ensure all 5 mount slots exist with defaults
    cfg = AppConfig()
    for i in range(1, 6):
        cfg.mounts[i] = MountConfig(mount_id=i)

    if not CONFIG_FILE.exists():
        return cfg

    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
        cfg.cv_mount_id       = data.get("cv_mount_id", 1)
        cfg.bridge_mode       = data.get("bridge_mode", "tcp")
        cfg.bridge_host       = data.get("bridge_host", "192.168.4.1")
        cfg.bridge_tcp_port   = data.get("bridge_tcp_port", 7777)
        cfg.bridge_port       = data.get("bridge_port", "")
        cfg.joystick_deadzone = data.get("joystick_deadzone", 0.08)
        cfg.osc_enabled       = data.get("osc_enabled", True)
        cfg.osc_port          = data.get("osc_port", 9700)
        cfg.virtual_keyboard  = data.get("virtual_keyboard", True)
        # Load only the display label for each mount — everything else is in EEPROM
        for mid_str, md in data.get("mounts", {}).items():
            mid = int(mid_str)
            if mid in cfg.mounts:
                cfg.mounts[mid].label = md.get("label", "")
        return cfg
    except Exception as e:
        log.error(f"Failed to load config: {e}")
        return cfg


def save_config(cfg: AppConfig) -> None:
    """Save app-level settings to JSON.

    Mount-specific settings (speed presets, orientation, stall thresholds) are
    NOT saved here — they live only in each mount's EEPROM, written via the
    protocol commands CMD_SAVE_SPEEDS / CMD_SET_ORIENTATION / CMD_SET_STALL_THRESHOLD.
    """
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = {
            "cv_mount_id":       cfg.cv_mount_id,
            "bridge_mode":       cfg.bridge_mode,
            "bridge_host":       cfg.bridge_host,
            "bridge_tcp_port":   cfg.bridge_tcp_port,
            "bridge_port":       cfg.bridge_port,
            "joystick_deadzone": cfg.joystick_deadzone,
            "osc_enabled":       cfg.osc_enabled,
            "osc_port":          cfg.osc_port,
            "virtual_keyboard":  cfg.virtual_keyboard,
            # Only the display label is app-specific; all other mount settings are in EEPROM
            "mounts": {
                str(mid): {"mount_id": mc.mount_id, "label": mc.label}
                for mid, mc in cfg.mounts.items()
            }
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.error(f"Failed to save config: {e}")
