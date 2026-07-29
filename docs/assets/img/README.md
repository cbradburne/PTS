# Site images

Drop photos/screenshots here with these exact filenames and the matching
slot on the site fills itself in (no HTML edits needed):

| File | Where it appears |
|---|---|
| `hero.jpg` | Hero — the rig in production (landing page) |
| `mount-pole.jpg` | Mounts — head on a clamp, built without the slider |
| `mount-home.svg` | Mounts — the round AMOLED status screen |
| `hub-home.svg` | Hub — the 7" touchscreen home screen |
| `phone-landscape.png` | Hub — the web app on a phone |
| `lookat-move.mp4` | Look-at — a move mid-travel (video, with `.jpg` poster) |
| `cv-window.png` | CV tracking — the tracking window with a lock-on target |
| `pc-app.png` | Control surfaces — PC app screenshot |
| `cad.png` | Build it — CAD model |
| `pcb.jpg` | Build it — the mount PCB |

Landscape ~1600px wide works best; JPG for photos, PNG for screenshots.

## Video

Clips are 960×540 H.264 MP4, no audio, `-movflags +faststart`, ~1.5 MB per
40 s — small enough to sit in the repo, and they play inline everywhere
(`muted` + `playsinline` is what lets iOS Safari play them in the page). Each
has a same-named `.jpg` poster frame, pulled from the encoded clip.

Keep the **camera master out of git** — `docs/assets/img/*.mov` is gitignored.
Binaries don't delta-compress, so a master committed once stays in history
forever. Encode to MP4 in a scratch directory, check it, then commit only the
final file. To re-encode from a master:

```
ffmpeg -ss <start> -to <end> -i master.mov -an \
  -vf "fps=24,scale=960:540:flags=lanczos" \
  -c:v libx264 -preset slow -crf 31 -pix_fmt yuv420p \
  -profile:v main -movflags +faststart -g 48 out.mp4
```

## Manual pages

Pages under `docs/manual/` use the same self-filling photo-slot convention.
So far:

| File | Where it appears |
|---|---|
| `rig-overview.jpg` | Getting started — a whole rig |
| `mount-home.svg` | The mounts — round AMOLED status screen |
| `mount-level.svg` | The mounts — spirit-level screen |
| `mount-setup.svg` | The mounts + Pairing — Setup screen |
| `hub-home.svg` | Hub touchscreen — home screen (hero) |
| `hub-positions.svg` | Hub touchscreen — Positions screen |
| `hub-detail.svg` | Hub touchscreen — per-camera control |
| `hub-config.svg` | Hub touchscreen — Config screen |
| `hub-pairing.svg` | Hub touchscreen — Paired Mounts |
| `phone-portrait.png` | Web app — phone in portrait (hero) |
| `phone-landscape.png` | Web app — phone in landscape |
| `ipad-landscape.png` | Web app — extended view on a tablet |
| `lookat-move.mp4` | Look-at — a full move, camera POV |
| `lookat-reaim.mp4` | Look-at — re-aiming to a second subject mid-move |
| `slider-rail.mp4` | The mounts — the head travelling its rail |
| `mount-rail.jpg` | The mounts — the head on its slider rail |
| `mount-pole.jpg` / `mount-pole2.jpg` | The mounts — the slider-less clamp build |
| `slider-production.mp4` | The mounts — slider move during a live set |
| `slider-venue.mp4` | The mounts — the same rig from the back of the hall |

The hub and mount screens are SVG recreations of the 7" console and the 1.75"
round AMOLED rather than photographs — they stay crisp at any size and are far
smaller than a photo of a panel. Round mount screens use the `round` figure
class so the square artwork is capped and centred. The
phone/tablet web-app shots are clean screen captures, so PNG. More slots will be
added as manual sections are written.
