# Look-at geometry animation

Source for `docs/assets/img/lookat-geometry.mp4`, kept out of `docs/` because
that directory is the published GitHub Pages site — the master alone is 7.4 MB
and the Claude Design export is another ~1 MB of JSX and screenshots that no
visitor needs.

- `master.mp4` — the export, 1280x720, 2.2 Mbps
- `source/` — the Claude Design project it came from

## Re-encoding after a change

    ffmpeg -y -i master.mp4 -vf "scale=960:-2" -c:v libx264 -crf 30 \
        -preset slow -profile:v high -pix_fmt yuv420p -movflags +faststart -an \
        ../../docs/assets/img/lookat-geometry.mp4

    ffmpeg -y -ss 2 -i master.mp4 -vf "scale=960:-2" -frames:v 1 -q:v 4 \
        ../../docs/assets/img/lookat-geometry.jpg

960 wide matches the other clips on the site. CRF 30 takes the master from
7.4 MB to 0.40 MB with no visible artefacts — line art and flat backgrounds
compress far better than the live footage the 2.2 Mbps export was rated for.

## Why the .mp4 and not the HTML export

`source/` renders through React and ReactDOM loaded from `unpkg.com`, with JSX
compiled in the browser by `support.js`, plus Inter from Google Fonts. Small on
disk, but it cannot render without a CDN round-trip, and it carries an authoring
UI (tweaks panel, motion editor) that has no place in a manual page. The encoded
video is self-contained, smaller in practice, and needs no runtime.
