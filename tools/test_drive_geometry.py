"""Teensy drive geometry: tooth counts are the source, and nothing moved.

The pulley and gear ratios used to be written out as FINISHED numbers in two
files — the slider's 40 mm/rev in MountMotion.cpp and again inside the nominal
step size in MountMotion.h, pan/tilt's 7.5:1 likewise. Fitting a different
pulley meant editing both, and missing one gave a mount that MOVES at one scale
and REPORTS ITS POSITION at another: a fault that looks mechanical and isn't.

Now the tooth counts are the only hand-edited numbers. This checks two things:

  1. the derived constants are BIT-IDENTICAL to the hardcoded ones they
     replaced, because this is motion code on real hardware and a refactor that
     quietly shifts a ratio is worse than no refactor;
  2. no second copy has crept back into the .cpp.

The geometry block is extracted from MountMotion.h and compiled, so what is
tested is the header that ships, not a transcription of it.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
import re, shutil, subprocess, tempfile

H = (REPO / "firmware/teensy41_mount/MountMotion.h").read_text()
CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()

# ---- 1. the tooth counts are present and are the editable thing ------------
print("1. drive geometry block:")
teeth = {}
for name in ("SLIDER_PULLEY_TEETH", "PAN_DRIVEN_TEETH", "PAN_DRIVER_TEETH",
             "TILT_DRIVEN_TEETH", "TILT_DRIVER_TEETH"):
    m = re.search(r"#define\s+" + name + r"\s+(\d+)", H)
    assert m, f"{name} is not defined in MountMotion.h"
    teeth[name] = int(m.group(1))
print("   " + ", ".join(f"{k.split('_')[0].lower()} {v}"
                        for k, v in list(teeth.items())[:1])
      + f", pan {teeth['PAN_DRIVEN_TEETH']}/{teeth['PAN_DRIVER_TEETH']}"
      + f", tilt {teeth['TILT_DRIVEN_TEETH']}/{teeth['TILT_DRIVER_TEETH']}   OK")

# ---- 2. no second copy left in the .cpp ------------------------------------
# These are the exact declarations that used to shadow the header's.
for gone in ("static constexpr float GEAR_RATIO_PAN",
             "static constexpr float GEAR_RATIO_TILT",
             "static constexpr float SLIDER_MM_PER_REV",
             "static constexpr float MOTOR_DEG_PER_STEP_PT"):
    assert gone not in CPP, f"MountMotion.cpp has its own {gone.split()[-1]} again"
# ...and the microstep array must come from the header, not a literal list.
assert "MICROSTEPS[4] = { MICROSTEPS_PAN" in CPP, \
    "the microstep array is a literal list again"
assert "{ 256, 256, 32, 32 }" not in CPP, "the old literal microstep list is back"
print("2. no duplicate ratios left in MountMotion.cpp        OK")

# ---- 3. the derived values must be bit-identical to the originals ----------
if not shutil.which("c++"):
    print("\n3. no C++ compiler — skipping the numeric comparison.")
    print("\nALL CHECKS PASSED")
    raise SystemExit(0)

# Pull the real block out of the header rather than retyping it.
start = H.index("#define SLIDER_PULLEY_TEETH")
end = H.index("NOMINAL_SLIDER_MM_PER_STEP")
end = H.index(";", end) + 1
block = H[start:end]

harness = r"""
#include <cstdio>
#include <cstring>
""" + block + r"""

// The values as they were written before the tooth counts became the source.
constexpr float OLD_GEAR_PAN     = 7.5f;
constexpr float OLD_GEAR_TILT    = 7.5f;
constexpr float OLD_MM_PER_REV   = 40.0f;
constexpr float OLD_PAN_DPS      = 0.9f  / (256.0f * 7.5f);
constexpr float OLD_TILT_DPS     = 0.9f  / (256.0f * 7.5f);
constexpr float OLD_SLIDER_MMPS  = 40.0f / (32.0f  * 200.0f);
static const unsigned short OLD_MICROSTEPS[4] = { 256, 256, 32, 32 };
static const unsigned short NEW_MICROSTEPS[4] = { MICROSTEPS_PAN, MICROSTEPS_TILT,
                                                  MICROSTEPS_SLIDER, MICROSTEPS_ZOOM };
static int bad = 0;
static void same(const char *w, float o, float n) {
    if (memcmp(&o, &n, sizeof(float)) != 0) { bad++; printf("DIFF %s %.9g %.9g\n", w, o, n); }
}
// physToUSteps in both forms, over the whole speed range the presets can ask for.
static float phys_old(int a, float v) {
    switch (a) {
        case 0: return v * OLD_MICROSTEPS[0] * OLD_GEAR_PAN  / 0.9f;
        case 1: return v * OLD_MICROSTEPS[1] * OLD_GEAR_TILT / 0.9f;
        case 2: return v * OLD_MICROSTEPS[2] * (360.0f / 1.8f) / OLD_MM_PER_REV;
        case 3: return v * OLD_MICROSTEPS[3] * 1.0f / 1.8f;
    } return v;
}
static float phys_new(int a, float v) {
    switch (a) {
        case 0: return v * NEW_MICROSTEPS[0] * GEAR_RATIO_PAN  / MOTOR_DEG_PER_STEP_PT;
        case 1: return v * NEW_MICROSTEPS[1] * GEAR_RATIO_TILT / MOTOR_DEG_PER_STEP_PT;
        case 2: return v * NEW_MICROSTEPS[2] * SLIDER_FULL_STEPS_PER_REV / SLIDER_MM_PER_REV;
        case 3: return v * NEW_MICROSTEPS[3] * GEAR_RATIO_ZOOM / MOTOR_DEG_PER_STEP_SZ;
    } return v;
}
int main() {
    same("GEAR_RATIO_PAN",  OLD_GEAR_PAN,  GEAR_RATIO_PAN);
    same("GEAR_RATIO_TILT", OLD_GEAR_TILT, GEAR_RATIO_TILT);
    same("SLIDER_MM_PER_REV", OLD_MM_PER_REV, SLIDER_MM_PER_REV);
    same("PAN_DEG_PER_STEP",    OLD_PAN_DPS,     NOMINAL_PAN_DEG_PER_STEP);
    same("TILT_DEG_PER_STEP",   OLD_TILT_DPS,    NOMINAL_TILT_DEG_PER_STEP);
    same("SLIDER_MM_PER_STEP",  OLD_SLIDER_MMPS, NOMINAL_SLIDER_MM_PER_STEP);
    for (int i = 0; i < 4; i++)
        if (OLD_MICROSTEPS[i] != NEW_MICROSTEPS[i]) { bad++; printf("DIFF microsteps %d\n", i); }
    const float vs[] = { 0.001f, 0.5f, 1.0f, 7.3f, 45.0f, 250.0f, 1000.0f };
    for (int a = 0; a < 4; a++) for (float v : vs) same("physToUSteps", phys_old(a,v), phys_new(a,v));
    printf("%d\n", bad);
    return bad != 0;
}
"""
d = pathlib.Path(tempfile.mkdtemp())
(d / "g.cpp").write_text(harness)
r = subprocess.run(["c++", "-std=c++17", "-O2", "-o", str(d / "g"), str(d / "g.cpp")],
                   capture_output=True, text=True)
assert r.returncode == 0, f"harness did not compile:\n{r.stderr[:800]}"
r = subprocess.run([str(d / "g")], capture_output=True, text=True)
print("\n3. derived vs the hardcoded values they replaced:")
assert r.returncode == 0, "the refactor CHANGED a value:\n" + r.stdout
print("   6 constants, 4 microstep counts, 28 physToUSteps cases")
print("   every one bit-identical — behaviour unchanged        OK")

# ---- 4. the teeth actually drive the result --------------------------------
# A block that derives nothing would pass everything above.
probe = harness.replace(f"#define SLIDER_PULLEY_TEETH    {teeth['SLIDER_PULLEY_TEETH']}",
                        "#define SLIDER_PULLEY_TEETH    30")
(d / "p.cpp").write_text(probe.replace("return bad != 0;", "return 0;"))
r2 = subprocess.run(["c++", "-std=c++17", "-O2", "-o", str(d / "p"), str(d / "p.cpp")],
                    capture_output=True, text=True)
assert r2.returncode == 0, r2.stderr[:400]
r2 = subprocess.run([str(d / "p")], capture_output=True, text=True)
assert "DIFF SLIDER_MM_PER_REV" in r2.stdout, \
    "changing SLIDER_PULLEY_TEETH changed nothing — the value is not derived"
assert "DIFF SLIDER_MM_PER_STEP" in r2.stdout, \
    "the nominal step size does not follow the pulley"
print("\n4. changing the pulley to 30t moves both the mm/rev and")
print("   the nominal step size                                OK")

print("\nALL CHECKS PASSED")
