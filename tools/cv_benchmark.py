#!/usr/bin/env python3
"""
cv_benchmark.py — measure the CV tracking cost on THIS machine.

The PC app's CV tracking has two costs that scale very differently:

  * YOLO person detection — runs every DETECT_EVERY_N ticks (~3x/s)
  * the correlation tracker — runs EVERY tick (30x/s) once a person is selected

so the tracker usually dominates, and the right settings depend on the CPU.
This script measures both, then prints the per-second budget for the current
settings and for a faster candidate, so the choice is made on numbers.

Run it on the machine you actually operate from:

    python tools/cv_benchmark.py                # synthetic frame
    python tools/cv_benchmark.py --device 0     # grab a real frame from capture

Nothing is modified — it only times things.  Needs the same packages the PC app
uses (ultralytics, opencv-contrib-python, numpy); the YOLO weights download on
first use if they aren't cached already.  Paste the whole output back.
"""
from __future__ import annotations

import argparse
import logging
import platform
import sys
import time
import warnings

warnings.filterwarnings("ignore")
logging.getLogger("ultralytics").setLevel(logging.ERROR)

try:
    import cv2
    import numpy as np
except ImportError as exc:
    sys.exit(f"Missing package: {exc}.  pip install opencv-contrib-python numpy")

# Mirrors pc_app/cv/tracking_loop.py
TRACK_RATE_HZ  = 30
DETECT_EVERY_N = 10
TRACK_SCALE    = 0.5
CAPTURE_W, CAPTURE_H = 1280, 720


def timeit(fn, budget_s: float = 1.5, min_n: int = 3, max_n: int = 30) -> float:
    """Time fn() adaptively — few iterations on slow machines. Returns ms."""
    fn()                                    # warm up
    t0 = time.perf_counter()
    fn()
    one = time.perf_counter() - t0
    n = max(min_n, min(max_n, int(budget_s / max(one, 1e-6))))
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t0) / n * 1000.0


def get_frame(device: int | None) -> np.ndarray:
    if device is not None:
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else 0
        cap = cv2.VideoCapture(device, backend) if backend else cv2.VideoCapture(device)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  CAPTURE_W)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_H)
        for _ in range(5):                  # let auto-exposure settle
            ok, frame = cap.read()
        cap.release()
        if ok and frame is not None:
            print(f"  real frame from capture device {device}: "
                  f"{frame.shape[1]}x{frame.shape[0]}")
            return frame
        print(f"  ! could not read device {device} — using a synthetic frame")
    frame = (np.random.rand(CAPTURE_H, CAPTURE_W, 3) * 255).astype("uint8")
    cv2.rectangle(frame, (560, 120), (720, 620), (240, 240, 240), -1)
    return frame


def main() -> int:
    ap = argparse.ArgumentParser(description="CV tracking benchmark")
    ap.add_argument("--device", type=int, default=None,
                    help="capture device index to grab a real frame from")
    args = ap.parse_args()

    print("=" * 68)
    print("PTS CV benchmark")
    print("=" * 68)
    print(f"  {platform.system()} {platform.release()}  {platform.machine()}")
    print(f"  CPU: {platform.processor() or 'unknown'}  cores={cv2.getNumberOfCPUs()}")
    print(f"  python {sys.version.split()[0]}  opencv {cv2.__version__}")
    try:
        import ultralytics
        print(f"  ultralytics {ultralytics.__version__}")
    except ImportError:
        print("  ultralytics NOT installed — detection cannot be measured")
    for opt in ("onnxruntime", "openvino"):
        try:
            mod = __import__(opt)
            print(f"  {opt} {getattr(mod, '__version__', '?')} (export path available)")
        except ImportError:
            print(f"  {opt}: not installed")
    print()

    frame = get_frame(args.device)
    h, w = frame.shape[:2]
    tw, th = int(w * TRACK_SCALE), int(h * TRACK_SCALE)
    small = cv2.resize(frame, (tw, th))

    # ── 1. YOLO detection vs input size ────────────────────────────────────
    print(f"-- YOLO person detection ({w}x{h} input) " + "-" * 24)
    det_ms: dict[int, float] = {}
    try:
        from ultralytics import YOLO
        model = YOLO("yolov8n.pt")
        for sz in (640, 512, 416, 320, 256):
            ms = timeit(lambda s=sz: model(frame, imgsz=s, verbose=False, classes=[0]))
            det_ms[sz] = ms
            note = "  <- ultralytics default (what the app uses now)" if sz == 640 else ""
            print(f"  imgsz={sz:<4} {ms:8.1f} ms/frame{note}")
    except Exception as exc:
        print(f"  detection unavailable: {type(exc).__name__}: {exc}")
    print()

    # ── 2. Correlation tracker ─────────────────────────────────────────────
    print(f"-- Correlation tracker ({tw}x{th}, runs EVERY tick) " + "-" * 14)
    bbox = (int(tw * 0.42), int(th * 0.15), int(tw * 0.14), int(th * 0.70))
    trackers = [
        ("CSRT (current)", lambda: cv2.legacy.TrackerCSRT_create()
            if hasattr(cv2, "legacy") else cv2.TrackerCSRT.create()),
        ("MOSSE",          lambda: cv2.legacy.TrackerMOSSE_create()),
        ("KCF",            lambda: cv2.legacy.TrackerKCF_create()
            if hasattr(cv2, "legacy") else cv2.TrackerKCF.create()),
        ("MedianFlow",     lambda: cv2.legacy.TrackerMedianFlow_create()),
    ]
    trk: dict[str, tuple[float, float]] = {}
    for name, factory in trackers:
        try:
            init_ms = timeit(lambda f=factory: f().init(small, bbox), budget_s=1.0)
            t = factory()
            t.init(small, bbox)
            upd_ms = timeit(lambda t=t: t.update(small))
            trk[name] = (init_ms, upd_ms)
            print(f"  {name:<16} init {init_ms:7.1f} ms | update {upd_ms:7.2f} ms")
        except Exception as exc:
            print(f"  {name:<16} unavailable ({type(exc).__name__}) "
                  f"— needs opencv-contrib-python")
    print()

    # ── 3. Per-second budget ───────────────────────────────────────────────
    det_per_s = TRACK_RATE_HZ / DETECT_EVERY_N          # ~3 detections/s
    print("-- Cost per second of tracking " + "-" * 34)
    print(f"  (tick budget at {TRACK_RATE_HZ} Hz = {1000/TRACK_RATE_HZ:.1f} ms; "
          f"detections {det_per_s:.0f}/s; re-anchors {det_per_s:.0f}/s)")

    def budget(label: str, tracker: str, imgsz: int) -> None:
        if tracker not in trk or imgsz not in det_ms:
            print(f"  {label:<34} n/a (missing measurement)")
            return
        init_ms, upd_ms = trk[tracker]
        total = det_ms[imgsz] * det_per_s + upd_ms * TRACK_RATE_HZ + init_ms * det_per_s
        print(f"  {label:<34} {total:7.0f} ms/s   ({total/10:.0f}% of one core)")

    budget("NOW    CSRT + imgsz 640", "CSRT (current)", 640)
    budget("FAST   MOSSE + imgsz 320", "MOSSE", 320)
    budget("MIDDLE MOSSE + imgsz 416", "MOSSE", 416)
    budget("ALT    KCF + imgsz 320", "KCF", 320)
    print()
    print("  >100% of one core means the loop cannot keep up and tracking lags.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
