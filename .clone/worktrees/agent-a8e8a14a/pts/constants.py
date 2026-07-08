"""PTS application constants."""

import sys

# Position command characters (positions 1-10, index 0-9)
POSITION_CMDS = ['z', 'x', 'c', 'v', 'b', 'n', 'm', ',', '.', '/']

# Set-position command chars (uppercase or special, used when SetPosToggle active)
SET_POSITION_CMDS = ['Z', 'X', 'C', 'V', 'B', 'N', 'M', '<', '>', '?']

# Camera colors: bg (normal), bg_slide (slider mode pos1/pos10), text
CAM_COLORS = [
    {'bg': '#4C8A4C', 'bg_slide': '#40D140', 'text': '55FF55'},  # cam0 (1) green
    {'bg': '#405C80', 'bg_slide': '#5C8BC9', 'text': '5C8BC9'},  # cam1 (2) blue
    {'bg': '#807100', 'bg_slide': '#B4A21C', 'text': 'B4A21C'},  # cam2 (3) yellow
    {'bg': '#008071', 'bg_slide': '#01E6CC', 'text': '01E6CC'},  # cam3 (4) teal
    {'bg': '#8D5395', 'bg_slide': '#E97CF9', 'text': 'E97CF9'},  # cam4 (5) purple
]

# Button state border colors
BORDER_COLOR_SET = '#FFFF00'    # set, not at, not running
BORDER_COLOR_AT = '#00FF00'     # at position
BORDER_COLOR_NONE = 'grey'      # not set

# Settings dispatch: maps (letter, index) -> (cam_idx, field_name)
# letter: lowercase = PT, uppercase = SL
# cam letters: a/A=cam0, s/S=cam1, d/D=cam2, f/F=cam3, g/G=cam4
SETTINGS_CAM_MAP = {
    'a': 0, 'A': 0,
    's': 1, 'S': 1,
    'd': 2, 'D': 2,
    'f': 3, 'F': 3,
    'g': 4, 'G': 4,
}

# Speed indicator geometry for dial lines (PT speed values -> dial position)
# PT/SL speed values from device: 1=slowest, 3=slow, 5=fast, 7=fastest
PT_SPEED_DIAL = {
    1: (1, 5.75, 1.8),    # (dial_value, y_offset, height_factor)
    3: (2, 4.0,  3.55),
    5: (3, 2.25, 5.3),
    7: (4, 0.5,  7.05),
}

# Window sizing
WIN_SIZE_DESKTOP = 1.0
WIN_SIZE_WIN_LINUX = 0.7

def get_win_size():
    if sys.platform in ('win32', 'linux'):
        return WIN_SIZE_WIN_LINUX
    return WIN_SIZE_DESKTOP

# Serial
SERIAL_BAUD = 38400
KEEPALIVE_INTERVAL = 0.8
