"""
Position label store — 5 mounts × 10 positions = 50 labels.

Labels are in-memory only and reset to numeric defaults ("1"–"10") on every
app launch.  They are not persisted to disk.
"""
from __future__ import annotations

NUM_MOUNTS    = 5
NUM_POSITIONS = 10


class PositionStore:
    """
    Holds per-slot labels for all 5 mounts (in-memory, session only).

    Slot occupancy / coordinates live on the Teensy and are accessed via
    MountManager.state(mount_id).slot_occupied_mask and .slots[].

    Access: store.get_label(mount_id, slot)  (mount_id: 1-5, slot: 0-9)
    """

    def __init__(self):
        # _labels[mount_id][slot] = label string, defaults to "1"–"10"
        self._labels: dict[int, list[str]] = {
            mid: [str(i + 1) for i in range(NUM_POSITIONS)]
            for mid in range(1, NUM_MOUNTS + 1)
        }

    def get_label(self, mount_id: int, slot: int) -> str:
        return self._labels[mount_id][slot]

    def set_label(self, mount_id: int, slot: int, label: str) -> None:
        self._labels[mount_id][slot] = label

    def clear_mount(self, mount_id: int) -> None:
        """Reset all slot labels for a mount to their numeric defaults."""
        if mount_id in self._labels:
            self._labels[mount_id] = [str(i + 1) for i in range(NUM_POSITIONS)]

    def clear_slots(self, mount_id: int, slots: set[int]) -> None:
        """Reset the labels of the given slot indices to their numeric defaults."""
        if mount_id not in self._labels:
            return
        for slot in slots:
            if 0 <= slot < NUM_POSITIONS:
                self._labels[mount_id][slot] = str(slot + 1)
