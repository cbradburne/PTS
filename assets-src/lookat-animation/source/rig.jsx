/* Camera mount pan/tilt/slider geometry animation — Nocturne palette. */

const W = 1600, H = 900;
const CX = 768, CY = 368;

const C = {
  bg: '#161826',
  lineFill: '#191b2b',
  text: '#e9e9ed',
  n300: '#cfd3e5',
  n400: '#b2b6ca',
  n500: '#9397ab',
  n600: '#75798c',
  n700: '#595d6c',
  n800: '#3f424d',
  n900: '#292b31',
  a300: '#d2cefd',
  a400: '#b5abfc',
  a500: '#968ae0',
  a600: '#796cbf',
  a700: '#5d5294',
  a800: '#423a6a',
};

/* ---------- geometry (set from tweaks each render, read by scenes) ---------- */
let G = {half: 1.5, subjY: 1.0, faceZ: 1.62, pivotZ: 0.26, fan: true, grid: true};

const face = () => [0, G.subjY - 0.10, G.faceZ];

/* ---------- small math ---------- */
const lerp = (a, b, t) => a + (b - a) * t;
const cl01 = t => (t < 0 ? 0 : t > 1 ? 1 : t);
const sm = t => {t = cl01(t); return t * t * (3 - 2 * t);};
const seg = (t, a, b) => sm((t - a) / (b - a));
const eio = t => {t = cl01(t); return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;};
const DEG = 180 / Math.PI;

const hx = h => [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
const mixA = (x, y, t) => [lerp(x[0], y[0], t), lerp(x[1], y[1], t), lerp(x[2], y[2], t)];
const rgbs = a => 'rgb(' + Math.round(a[0]) + ',' + Math.round(a[1]) + ',' + Math.round(a[2]) + ')';
const mix = (a, b, t) => rgbs(mixA(hx(a), hx(b), t));
const SH_DARK = hx('#2c3040'), SH_LIT = hx('#8d92a8'), BG_A = hx('#161826'), LF_A = hx('#191b2b');

const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const add = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
const mul = (a, k) => [a[0] * k, a[1] * k, a[2] * k];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const norm = a => {const l = Math.hypot(a[0], a[1], a[2]) || 1; return [a[0] / l, a[1] / l, a[2] / l];};

/* ---------- camera ---------- */
const CAM = {
  A0: {az: -52, el: 46, f: 9, sc: 188, t: [0, 0.45, 0.86]},
  A1: {az: -46, el: 38, f: 9, sc: 198, t: [0, 0.45, 0.88]},
  B0: {az: -40, el: 31, f: 10, sc: 206, t: [0, 0.50, 0.90]},
  B1: {az: -12, el: 20, f: 11, sc: 222, t: [0, 0.50, 0.92]},
  EL: {az: -82, el: 5, f: 400, sc: 228, t: [0, 0.50, 0.94]},
};
const camLerp = (a, b, t) => ({
  az: lerp(a.az, b.az, t), el: lerp(a.el, b.el, t),
  f: lerp(a.f, b.f, t), sc: lerp(a.sc, b.sc, t),
  t: [lerp(a.t[0], b.t[0], t), lerp(a.t[1], b.t[1], t), lerp(a.t[2], b.t[2], t)],
});

function proj(p, cam) {
  const az = cam.az / DEG, el = cam.el / DEG;
  const dx = p[0] - cam.t[0], dy = p[1] - cam.t[1], dz = p[2] - cam.t[2];
  const x1 = dx * Math.cos(az) - dy * Math.sin(az);
  const y1 = dx * Math.sin(az) + dy * Math.cos(az);
  const depth = y1 * Math.cos(el) - dz * Math.sin(el);
  const up = dz * Math.cos(el) + y1 * Math.sin(el);
  const k = cam.f / Math.max(0.5, cam.f + depth);
  return [CX + x1 * cam.sc * k, CY - up * cam.sc * k, depth];
}
const pt = (p, cam) => {const q = proj(p, cam); return q[0].toFixed(1) + ',' + q[1].toFixed(1);};
const path = (pts, cam) => pts.map(p => pt(p, cam)).join(' ');

/* ---------- solids ---------- */
const FACES = [[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4], [2, 3, 7, 6], [3, 0, 4, 7], [1, 2, 6, 5]];

function boxVerts(c, ex, ey, ez, w, d, h) {
  const X = mul(ex, w / 2), Y = mul(ey, d / 2), Z = mul(ez, h / 2);
  const v = (sx, sy, sz) => add(add(add(c, mul(X, sx)), mul(Y, sy)), mul(Z, sz));
  return [v(-1, -1, -1), v(1, -1, -1), v(1, 1, -1), v(-1, 1, -1), v(-1, -1, 1), v(1, -1, 1), v(1, 1, 1), v(-1, 1, 1)];
}
const EX = [1, 0, 0], EY = [0, 1, 0], EZ = [0, 0, 1];
const abox = (cx, cy, cz, w, d, h) => boxVerts([cx, cy, cz], EX, EY, EZ, w, d, h);

function collect(verts, out) {
  for (const f of FACES) {
    const a = verts[f[0]], b = verts[f[1]], c = verts[f[2]];
    const n = norm(cross(sub(b, a), sub(c, a)));
    out.push({v: f.map(i => verts[i]), n});
  }
}

const LIGHT = norm([-0.45, -0.55, 0.78]);

/* ---------- the rig + subject as solids ---------- */
function buildSolids(s) {
  const out = [];
  const half = G.half, pz = G.pivotZ;
  /* rail beam + end blocks */
  collect(abox(0, 0, 0.07, half * 2, 0.11, 0.06), out);
  collect(abox(-half + 0.06, 0, 0.02, 0.16, 0.20, 0.04), out);
  collect(abox(half - 0.06, 0, 0.02, 0.16, 0.20, 0.04), out);
  /* carriage + pan yoke */
  collect(abox(s, 0, 0.145, 0.24, 0.18, 0.09), out);
  collect(abox(s, 0, 0.225, 0.11, 0.11, 0.07), out);

  /* camera head, oriented along the sight line */
  const P = [s, 0, pz], F = face();
  const A = norm(sub(F, P));
  const Rt = norm(cross(A, EZ));
  const Up = cross(Rt, A);
  const body = boxVerts(add(P, mul(A, 0.02)), Rt, A, Up, 0.17, 0.24, 0.13);
  collect(body, out);
  const lens = boxVerts(add(P, mul(A, 0.20)), Rt, A, Up, 0.085, 0.14, 0.085);
  collect(lens, out);

  /* blocky performer */
  const y = G.subjY, fz = G.faceZ;
  collect(abox(0, y, fz, 0.20, 0.19, 0.25), out);            // head
  collect(abox(0, y, fz - 0.19, 0.09, 0.09, 0.13), out);      // neck
  collect(abox(0, y, 1.19, 0.40, 0.23, 0.50), out);           // torso
  collect(abox(0, y, 0.885, 0.35, 0.25, 0.13), out);          // hips
  collect(abox(-0.10, y, 0.41, 0.135, 0.15, 0.82), out);
  collect(abox(0.10, y, 0.41, 0.135, 0.15, 0.82), out);
  collect(abox(-0.255, y - 0.01, 1.15, 0.095, 0.11, 0.50), out);
  collect(abox(0.255, y - 0.01, 1.15, 0.095, 0.11, 0.50), out);
  return out;
}

/* ---------- annotation helpers ---------- */
function arc3(center, u, v, a0, a1, r, n) {
  const out = [];
  for (let i = 0; i <= n; i++) {
    const a = lerp(a0, a1, i / n);
    out.push(add(center, add(mul(u, Math.cos(a) * r), mul(v, Math.sin(a) * r))));
  }
  return out;
}

function angles(s) {
  const F = face(), P = [s, 0, G.pivotZ];
  const pan = Math.atan2(F[0] - s, F[1]) * DEG;
  const d = Math.hypot(F[0] - s, F[1]);
  const tilt = Math.atan2(F[2] - P[2], d) * DEG;
  return {pan, tilt, d};
}

/* ---------- HUD ---------- */
function Readout({label, value, frac, color, x, y, op}) {
  return (
    <g opacity={op}>
      <text fontFamily='"Inter", system-ui, sans-serif' x={x} y={y} fill={C.n500} fontSize="17" letterSpacing="2.4" fontWeight="500">{label}</text>
      <text fontFamily='"Inter", system-ui, sans-serif' x={x} y={y + 44} fill={color} fontSize="42" fontWeight="500"
            style={{fontVariantNumeric: 'tabular-nums'}}>{value}</text>
      <rect x={x} y={y + 62} width="210" height="3" fill={C.n800} />
      <rect x={x} y={y + 62} width={Math.max(0, 210 * cl01(frac))} height="3" fill={color} />
    </g>
  );
}

/* ---------- the frame ---------- */
function Rig({s, shade, anno, cam, trailTo, trailOp, label}) {
  const F = face(), P = [s, 0, G.pivotZ];
  const {pan, tilt, d} = angles(s);
  const solids = buildSolids(s);
  const drawn = solids.map(f => {
    const q = f.v.map(p => proj(p, cam));
    const depth = (q[0][2] + q[1][2] + q[2][2] + q[3][2]) / 4;
    const lam = Math.max(0, dot(f.n, LIGHT));
    const fog = cl01((depth + 1.6) / 3.4);
    const solid = mixA(mixA(SH_DARK, SH_LIT, 0.18 + 0.82 * lam), BG_A, 0.18 * fog);
    return {
      pts: q.map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' '),
      depth,
      fill: rgbs(mixA(LF_A, solid, shade)),
      stroke: mix(C.n400, '#20232f', shade),
    };
  }).sort((a, b) => b.depth - a.depth);

  const gridOp = (1 - shade) * 0.55;
  const gridLines = [];
  if (G.grid) {
    const x0 = -G.half - 0.45, x1 = G.half + 0.45, y0 = -0.5, y1 = G.subjY + 0.6;
    for (let x = Math.ceil(x0 * 2) / 2; x <= x1; x += 0.5) gridLines.push([[x, y0, 0], [x, y1, 0]]);
    for (let y = Math.ceil(y0 * 2) / 2; y <= y1; y += 0.5) gridLines.push([[x0, y, 0], [x1, y, 0]]);
  }

  /* fan of sight lines already traversed */
  const fan = [];
  if (G.fan) {
    for (let x = -G.half; x <= trailTo + 1e-6; x += 0.1875) fan.push(x);
  }

  /* pan arc on the floor at the carriage */
  const panRef = 0.52;
  const panArc = arc3([s, 0, 0.004], EY, EX, 0, Math.atan2(F[0] - s, F[1]), panRef, 26);
  /* tilt arc in the vertical plane through P and F */
  const hdir = norm([F[0] - s, F[1], 0]);
  const tiltArc = arc3(P, hdir, EZ, 0, Math.atan2(F[2] - P[2], d), 0.62, 26);

  const panMid = proj(panArc[Math.round(panArc.length * 0.55)], cam);
  const panHub = proj([s, 0, 0.004], cam);
  const panLabel = [panHub[0] + (panMid[0] - panHub[0]) * 1.46,
                    panHub[1] + (panMid[1] - panHub[1]) * 1.46 - 10];

  const dimY = -0.46;
  /* how far into the side view we are — world X becomes the depth axis there, so the
     dimension offsets have to migrate onto axes that still read across the screen */
  const sv = cl01((Math.abs(cam.az) - 48) / 26);
  const offX = -(G.half + 0.42) * (1 - sv);  // stand-off: outboard of the rail in 3/4 …
  const offZ = 0.006 - 0.40 * sv;            // … and dropped below the floor in side view
  const facX = 0.60 * (1 - sv);              // face height: outboard of the figure …
  const facY = F[1] + 0.78 * sv;             // … and pulled forward of it in side view

  return (
    <div data-screen-label={label} style={{width: W, height: H, background: C.bg, position: 'relative'}}>
      <svg width={W} height={H} viewBox={'0 0 ' + W + ' ' + H}
           fontFamily='"Inter", system-ui, sans-serif' style={{display: 'block', fontFamily: '"Inter", system-ui, sans-serif'}}>
        <defs>
          <radialGradient id="vig" cx="50%" cy="48%" r="72%">
            <stop offset="55%" stopColor="#1b1e30" />
            <stop offset="100%" stopColor="#12131f" />
          </radialGradient>
        </defs>
        <rect x="0" y="0" width={W} height={H} fill="url(#vig)" />

        {/* floor */}
        <polygon points={path([[-G.half - 0.6, -0.6, 0], [G.half + 0.6, -0.6, 0], [G.half + 0.6, G.subjY + 0.7, 0], [-G.half - 0.6, G.subjY + 0.7, 0]], cam)}
                 fill="#1d2031" opacity={0.35 + 0.45 * shade} />
        <g stroke={C.n800} strokeWidth="1" opacity={gridOp}>
          {gridLines.map((l, i) => <line key={i} x1={proj(l[0], cam)[0]} y1={proj(l[0], cam)[1]} x2={proj(l[1], cam)[0]} y2={proj(l[1], cam)[1]} />)}
        </g>

        {/* fan of past sight lines */}
        <g opacity={trailOp * (1 - shade)}>
          {fan.map((x, i) => {
            const a = proj([x, 0, G.pivotZ], cam), b = proj(F, cam);
            return <line key={i} x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} stroke={C.a700} strokeWidth="1" opacity="0.5" />;
          })}
        </g>

        {/* solids */}
        <g>
          {drawn.map((f, i) => (
            <polygon key={i} points={f.pts} fill={f.fill} stroke={f.stroke}
                     strokeWidth={1 + 0.1 * (1 - shade)} strokeLinejoin="round" />
          ))}
        </g>

        {/* annotations */}
        <g opacity={anno}>
          {/* slider dimension — collapses to a point in side view, so fade it there */}
          <g opacity={1 - cl01((Math.abs(cam.az) - 50) / 18)}>
          <line x1={proj([-G.half, dimY, 0.004], cam)[0]} y1={proj([-G.half, dimY, 0.004], cam)[1]}
                x2={proj([G.half, dimY, 0.004], cam)[0]} y2={proj([G.half, dimY, 0.004], cam)[1]}
                stroke={C.n600} strokeWidth="1.4" />
          {[-G.half, G.half].map((x, i) => (
            <line key={i} x1={proj([x, dimY - 0.09, 0.004], cam)[0]} y1={proj([x, dimY - 0.09, 0.004], cam)[1]}
                  x2={proj([x, dimY + 0.09, 0.004], cam)[0]} y2={proj([x, dimY + 0.09, 0.004], cam)[1]}
                  stroke={C.n600} strokeWidth="1.4" />
          ))}
          <text fontFamily='"Inter", system-ui, sans-serif' x={proj([0, dimY, 0.004], cam)[0]} y={proj([0, dimY, 0.004], cam)[1] + 30} fill={C.n400}
                fontSize="20" textAnchor="middle" letterSpacing="1.4">
            SLIDER {(G.half * 2).toFixed(2)} m
          </text>
          </g>

          {/* stand-off — witness lines out to a dimension clear of the rail */}
          <g stroke={C.n700} strokeWidth="1" strokeDasharray="4 5">
            <line x1={proj([0, 0, 0.006], cam)[0]} y1={proj([0, 0, 0.006], cam)[1]}
                  x2={proj([offX - 0.1 * (1 - sv), 0, offZ], cam)[0]} y2={proj([offX - 0.1 * (1 - sv), 0, offZ], cam)[1]} />
            <line x1={proj([0, G.subjY, 0.006], cam)[0]} y1={proj([0, G.subjY, 0.006], cam)[1]}
                  x2={proj([offX - 0.1 * (1 - sv), G.subjY, offZ], cam)[0]} y2={proj([offX - 0.1 * (1 - sv), G.subjY, offZ], cam)[1]} />
          </g>
          <line x1={proj([offX, 0, offZ], cam)[0]} y1={proj([offX, 0, offZ], cam)[1]}
                x2={proj([offX, G.subjY, offZ], cam)[0]} y2={proj([offX, G.subjY, offZ], cam)[1]}
                stroke={C.n500} strokeWidth="1.4" />
          {[0, G.subjY].map((y, i) => (
            <line key={i} x1={proj([offX - 0.09 * (1 - sv), y, offZ - 0.09 * sv], cam)[0]} y1={proj([offX - 0.09 * (1 - sv), y, offZ - 0.09 * sv], cam)[1]}
                  x2={proj([offX + 0.09 * (1 - sv), y, offZ + 0.09 * sv], cam)[0]} y2={proj([offX + 0.09 * (1 - sv), y, offZ + 0.09 * sv], cam)[1]}
                  stroke={C.n500} strokeWidth="1.4" />
          ))}
          <text fontFamily='"Inter", system-ui, sans-serif' x={proj([offX, G.subjY / 2, offZ], cam)[0] - 14 * (1 - sv)} y={proj([offX, G.subjY / 2, offZ], cam)[1] + 6 + 22 * sv}
                fill={C.n400} fontSize="19" textAnchor="end" letterSpacing="1.2">{G.subjY.toFixed(2)} m</text>

          {/* face height — offset clear of the figure */}
          <g stroke={C.n700} strokeWidth="1" strokeDasharray="4 5">
            <line x1={proj([0.16 * (1 - sv), F[1], G.faceZ], cam)[0]} y1={proj([0.16 * (1 - sv), F[1], G.faceZ], cam)[1]}
                  x2={proj([facX + 0.1 * (1 - sv), facY, G.faceZ], cam)[0]} y2={proj([facX + 0.1 * (1 - sv), facY, G.faceZ], cam)[1]} />
            <line x1={proj([0.16 * (1 - sv), F[1], 0], cam)[0]} y1={proj([0.16 * (1 - sv), F[1], 0], cam)[1]}
                  x2={proj([facX + 0.1 * (1 - sv), facY, 0], cam)[0]} y2={proj([facX + 0.1 * (1 - sv), facY, 0], cam)[1]} />
          </g>
          <line x1={proj([facX, facY, 0], cam)[0]} y1={proj([facX, facY, 0], cam)[1]}
                x2={proj([facX, facY, G.faceZ], cam)[0]} y2={proj([facX, facY, G.faceZ], cam)[1]}
                stroke={C.n500} strokeWidth="1.4" />
          {[0, G.faceZ].map((z, i) => (
            <line key={i} x1={proj([facX - 0.09 * (1 - sv), facY - 0.09 * sv, z], cam)[0]} y1={proj([facX - 0.09 * (1 - sv), facY - 0.09 * sv, z], cam)[1]}
                  x2={proj([facX + 0.09 * (1 - sv), facY + 0.09 * sv, z], cam)[0]} y2={proj([facX + 0.09 * (1 - sv), facY + 0.09 * sv, z], cam)[1]}
                  stroke={C.n500} strokeWidth="1.4" />
          ))}
          <text fontFamily='"Inter", system-ui, sans-serif' x={proj([facX, facY, G.faceZ * 0.52], cam)[0] + 14} y={proj([facX, facY, G.faceZ * 0.52], cam)[1] + 6}
                fill={C.n400} fontSize="19" letterSpacing="1.2">{G.faceZ.toFixed(2)} m</text>

          {/* pan reference + arc — degenerate in side view */}
          <g opacity={1 - cl01((Math.abs(cam.az) - 50) / 28)}>
          <line x1={proj([s, 0, 0.004], cam)[0]} y1={proj([s, 0, 0.004], cam)[1]}
                x2={proj([s, panRef + 0.12, 0.004], cam)[0]} y2={proj([s, panRef + 0.12, 0.004], cam)[1]}
                stroke={C.n600} strokeWidth="1.2" strokeDasharray="5 5" />
          <polyline points={path(panArc, cam)} fill="none" stroke={C.n300} strokeWidth="2.4" />
          <text fontFamily='"Inter", system-ui, sans-serif' x={panLabel[0]} y={panLabel[1]}
                opacity={cl01((Math.abs(pan) - 3) / 7)}
                fill={C.n300} fontSize="21" textAnchor="middle"
                style={{fontVariantNumeric: 'tabular-nums'}}>{pan.toFixed(1)}°</text>
          </g>

          {/* tilt reference + arc */}
          <line x1={proj(P, cam)[0]} y1={proj(P, cam)[1]}
                x2={proj(add(P, mul(hdir, 0.74)), cam)[0]} y2={proj(add(P, mul(hdir, 0.74)), cam)[1]}
                stroke={C.a800} strokeWidth="1.2" strokeDasharray="5 5" />
          <polyline points={path(tiltArc, cam)} fill="none" stroke={C.a400} strokeWidth="2.6" />
          <text fontFamily='"Inter", system-ui, sans-serif' x={proj(tiltArc[Math.round(tiltArc.length * 0.5)], cam)[0] + 26}
                y={proj(tiltArc[Math.round(tiltArc.length * 0.5)], cam)[1] + 2}
                fill={C.a400} fontSize="21"
                style={{fontVariantNumeric: 'tabular-nums'}}>{tilt.toFixed(1)}°</text>
        </g>

        {/* live sight line — always on */}
        <g>
          <line x1={proj(P, cam)[0]} y1={proj(P, cam)[1]} x2={proj(F, cam)[0]} y2={proj(F, cam)[1]}
                stroke={C.a300} strokeWidth="2.2" opacity={0.35 + 0.65 * anno} />
          <circle cx={proj(F, cam)[0]} cy={proj(F, cam)[1]} r="13" fill="none" stroke={C.a300} strokeWidth="1.6" opacity={0.35 + 0.65 * anno} />
          <line x1={proj(F, cam)[0] - 22} y1={proj(F, cam)[1]} x2={proj(F, cam)[0] + 22} y2={proj(F, cam)[1]} stroke={C.a300} strokeWidth="1" opacity={0.55 * anno} />
          <line x1={proj(F, cam)[0]} y1={proj(F, cam)[1] - 22} x2={proj(F, cam)[0]} y2={proj(F, cam)[1] + 22} stroke={C.a300} strokeWidth="1" opacity={0.55 * anno} />
        </g>

        {/* header */}
        <text fontFamily='"Inter", system-ui, sans-serif' x="64" y="76" fill={C.n600} fontSize="16" letterSpacing="4">CAMERA MOUNT</text>
        <text fontFamily='"Inter", system-ui, sans-serif' x="64" y="118" fill={C.text} fontSize="34" fontWeight="500" letterSpacing="0.4">Pan · Tilt · Slider</text>

        {/* readouts — in a reserved strip, clear of the geometry */}
        <g opacity={anno}>
          <rect x="0" y="758" width={W} height={H - 758} fill="#12131f" />
          <line x1="64" y1="758" x2={W - 64} y2="758" stroke={C.n800} strokeWidth="1" />
        </g>
        <Readout label="SLIDER" value={(s >= 0 ? '+' : '') + s.toFixed(2) + ' m'} frac={(s + G.half) / (2 * G.half)} color={C.text} x={64} y={806} op={anno} />
        <Readout label="PAN" value={(pan >= 0 ? '+' : '') + pan.toFixed(1) + '°'} frac={(pan + 70) / 140} color={C.n300} x={318} y={806} op={anno} />
        <Readout label="TILT" value={tilt.toFixed(1) + '°'} frac={tilt / 70} color={C.a400} x={572} y={806} op={anno} />
      </svg>
    </div>
  );
}

/* ---------- scenes ---------- */
const useScene = (...a) => window.useScene(...a);
const label = (i, lt) => 'scene ' + (i + 1) + ' · ' + lt.toFixed(0) + 's';

function Establish() {
  const {progress, localTime, index} = useScene();
  return <Rig s={-G.half} shade={1} anno={0} trailTo={-G.half} trailOp={0}
              cam={camLerp(CAM.A0, CAM.A1, sm(progress))} label={label(index, localTime)} />;
}

function Flatten() {
  const {progress, localTime, index} = useScene();
  return <Rig s={-G.half} shade={1 - seg(progress, 0.08, 0.62)} anno={seg(progress, 0.38, 1)}
              trailTo={-G.half} trailOp={seg(progress, 0.6, 1)}
              cam={camLerp(CAM.A1, CAM.B0, sm(progress))} label={label(index, localTime)} />;
}

function Sweep() {
  const {progress, localTime, index} = useScene();
  const s = lerp(-G.half, G.half, eio(cl01((progress - 0.05) / 0.9)));
  return <Rig s={s} shade={0} anno={1} trailTo={s} trailOp={1}
              cam={camLerp(CAM.B0, CAM.B1, progress)} label={label(index, localTime)} />;
}

function Elevation() {
  const {progress, localTime, index} = useScene();
  const s = lerp(G.half, 0, eio(cl01((progress - 0.34) / 0.66)));
  return <Rig s={s} shade={0} anno={1} trailTo={G.half} trailOp={1}
              cam={camLerp(CAM.B1, CAM.EL, sm(cl01(progress / 0.46)))} label={label(index, localTime)} />;
}

function Rewind() {
  const {progress, localTime, index} = useScene();
  const s = lerp(0, -G.half, eio(cl01(progress / 0.58)));
  return <Rig s={s} shade={seg(progress, 0.44, 1)} anno={1 - seg(progress, 0, 0.42)}
              trailTo={G.half} trailOp={1 - seg(progress, 0, 0.42)}
              cam={camLerp(CAM.EL, CAM.A0, sm(progress))} label={label(index, localTime)} />;
}

/* ---------- root ---------- */
function RigPiece() {
  const {SceneStage, useTweaks, TweaksPanel, TweakSection, TweakSlider, TweakToggle} = window;
  const [t, setTweak] = useTweaks(window.TWEAK_DEFAULTS);
  G = {
    half: t.sliderLength / 2,
    subjY: t.subjectDistance,
    faceZ: t.faceHeight,
    pivotZ: t.pivotHeight,
    fan: t.showFan,
    grid: t.showGrid,
  };
  return (
    <div style={{width: '100%', height: '100%', background: C.bg, display: 'flex', alignItems: 'center', justifyContent: 'center'}}>
      <SceneStage width={W} height={H} scenes={window.OM_SCENES} playback={window.OM_PLAYBACK} bg={C.bg}>
        {{Establish, Flatten, Sweep, Elevation, Rewind}}
      </SceneStage>
      <TweaksPanel>
        <TweakSection label="Geometry" />
        <TweakSlider label="Slider length" value={t.sliderLength} min={1} max={4} step={0.1} unit="m"
                     onChange={v => setTweak('sliderLength', v)} />
        <TweakSlider label="Subject distance" value={t.subjectDistance} min={0.6} max={2.4} step={0.05} unit="m"
                     onChange={v => setTweak('subjectDistance', v)} />
        <TweakSlider label="Face height" value={t.faceHeight} min={1.2} max={1.95} step={0.01} unit="m"
                     onChange={v => setTweak('faceHeight', v)} />
        <TweakSlider label="Tilt pivot height" value={t.pivotHeight} min={0.1} max={1.2} step={0.02} unit="m"
                     onChange={v => setTweak('pivotHeight', v)} />
        <TweakSection label="Display" />
        <TweakToggle label="Sight-line fan" value={t.showFan} onChange={v => setTweak('showFan', v)} />
        <TweakToggle label="Floor grid" value={t.showGrid} onChange={v => setTweak('showGrid', v)} />
        <TweakToggle label="Motion editor" value={t.motionEditor} onChange={v => setTweak('motionEditor', v)} />
      </TweaksPanel>
    </div>
  );
}

window.RigPiece = RigPiece;
