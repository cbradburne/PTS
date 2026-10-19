"""A satellite that reconnects takes the place of its own dead link.

2026-10-19: both satellites were flashed and reconnected.  Each left its old
relay link open on the hub — no FIN came — and the hub polls every link on
every loop pass, dead or alive (~45 us each).  The Foyer's dead link was
closed within a minute, because cam5 is routed through it and the restarted
Foyer answered the hub's next send with a reset.  The Basement's carried
nothing, drew nothing, and stayed open almost two hours: the loop ran 2,340
passes/s until it closed at 12:15 and 2,610 after.  It also held one of the
six slots; enough restarts inside two hours and the satellite itself would be
turned away.

A satellite keeps ONE relay link, so a new one from an address that already
has a link proves the old one dead.  The hub closes it before placing the new.

  1. the accept path closes every link from the new link's address — release
     its mounts, clear its name and address, free the slot — BEFORE placing
     the new one, so it can never close the link it just placed;
  2. the address is compared the way the slot stores it;
  3. the ordinary disconnect path is unchanged.

The hub's loop is not built on the host; this reads its source, and each
mutant of the rule must be caught by its own check.  Run directly, or via
tools/run_tests.sh with the rest.
"""
import pathlib, re

REPO = pathlib.Path(__file__).resolve().parent.parent
HUB = re.sub(r"//[^\n]*", "", (REPO / "firmware/esp32_hub_eth/esp32_hub_eth.ino").read_text())

block = HUB[HUB.index("WiFiClient in = _sat_server.accept();"):HUB.index("MARK(SEC_SAT);")]
accept = block[:block.index("if (!placed)")]

print("1. a reconnecting satellite's old link is closed first:")
m = re.search(r"uint32_t from = \(uint32_t\)in\.remoteIP\(\);\s*"
              r"for \(int i = 0; i < MAX_SATELLITES; i\+\+\) \{\s*"
              r"if \(!_sat\[i\]\.active \|\| _sat\[i\]\.ip != from\) continue;(.*?)\n            \}",
              accept, re.S)
assert m, "the accept path does not look for a link already open from the new link's address"
close = m.group(1)
for need, what in (("_sat[i].client.stop();", "the old socket is not closed"),
                   ("_sat[i].active = false;", "the old slot is not freed"),
                   ("sat_release_mounts(i);", "mounts stay routed through the dead link"),
                   ("_sat[i].ip = 0;", "the old slot keeps the address, and would match again"),
                   ("_sat[i].name[0] = '\\0';", "the old slot keeps the satellite's name")):
    assert need in close, what
assert "break;" not in close, "only the first old link is closed; a second dead one would stay"
place = accept.index("bool placed = false;")
assert accept.index("uint32_t from") < place, \
    "the old link is looked for AFTER the new one is placed — it would close the link it just made"
print("   every link from that address: socket closed, mounts released,")
print("   name and address cleared, slot freed — before the new one is placed  OK")

print("\n2. the address is compared the way the slot stores it:")
stored = re.search(r"_sat\[i\]\.ip\s*=\s*\(uint32_t\)in\.remoteIP\(\);", accept[place:])
assert stored, "the slot no longer stores (uint32_t)in.remoteIP() — the comparison would never match"
print("   both (uint32_t)in.remoteIP()                                     OK")

print("\n3. the ordinary disconnect path is unchanged:")
rest = block[block.index("if (!placed)"):]
assert re.search(r"if \(!_sat\[i\]\.client\.connected\(\)\) \{\s*_sat\[i\]\.client\.stop\(\);\s*"
                 r"_sat\[i\]\.active = false;", rest), "a link that drops is no longer released"
assert "send_sat_names();" in rest, "a link that drops no longer updates the satellite list"
print("   a link that drops is still closed and the list re-sent            OK")

print("\nALL CHECKS PASSED")
