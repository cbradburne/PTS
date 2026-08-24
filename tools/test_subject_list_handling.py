"""A logging line must never cost the app the packet it was logging.

Reported on the rig: `Bad SUBJECT_LIST from mount 5: 'NoneType' object has no
attribute 'valid'`, repeating for as long as the app ran.

The message was a lie twice over. The packet was fine, and the mount had done
nothing wrong — the fault was in this app. And it was not merely a noisy log
line: the handler stored the subject list and emitted its signal AFTER the
logging, inside the same try, so the exception skipped both. st.subjects kept
its initial [None] * MAX_SUBJECTS, which is what the next list was compared
against, which raised the same exception. A permanent loop in which the PC app
never learned about any subject at all, while blaming the mount for it.

Two rules come out of it, and this file pins both:

  Store and publish before doing anything optional with the data.
  Report a fault where it happened. "Bad X from mount N" should mean the mount
  sent something bad.

This is the second time this shape has appeared. "WARNING Bad REF_CONFIRMED
from mount 5" was an AttributeError on this app's own field names, and the
operator quite reasonably asked how their 0/0 reference could be bad.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re, logging
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

from comms.mount_manager import MountManager
from comms.protocol import SubjectRecord, MAX_SUBJECTS

MM = (REPO / "pc_app/comms/mount_manager.py").read_text()

# ---- 1. the ordering that caused the loop ----------------------------------
print("1. the SUBJECT_LIST handler:")
h = MM[MM.index("elif pkt.cmd == Cmd.SUBJECT_LIST:"):]
h = h[:h.index("\n        elif ")]

store  = h.index("st.subjects = subjects")
emit   = h.index("self.subject_list_received.emit(mid)")
logcall = h.index("self._log_solved_subjects(")
assert store < logcall and emit < logcall, \
    "the list is stored/published after the logging again — an exception in the\n" \
    "    logging would skip both and strand st.subjects at its initial value"
print("   stored and published BEFORE anything optional      OK")

# The decode is what "Bad SUBJECT_LIST" may describe — nothing else.
dec = h.index("decode_subject_list(pkt.payload)")
bad = h.index('f"Bad SUBJECT_LIST from mount {mid}')
assert dec < bad < store, \
    "the 'Bad SUBJECT_LIST' handler no longer wraps only the decode"
print("   'Bad SUBJECT_LIST' covers only the decode           OK")

# ---- 2. and the app owns its own faults -------------------------------------
print("\n2. where a logging fault is reported:")
helper = MM[MM.index("def _log_solved_subjects"):]
helper = helper[:helper.index("\n    def ", 1)]
assert "SUBJECT logging failed for mount %d" in helper, \
    "a fault in the logging would surface as the mount's fault again"
assert "if s and s.valid" in helper, \
    "entries are dereferenced without checking they are records"
print("   named as this app's fault, not the mount's          OK")

# ---- 3. it survives the state that actually crashed -------------------------
# A mount that has not yet sent a list has [None] * MAX_SUBJECTS, and that is
# precisely what the first real list gets compared against.
print("\n3. driven through the case from the rig:")
mm = MountManager.__new__(MountManager)
fresh  = [None] * MAX_SUBJECTS
solved = [SubjectRecord(0, True, "Lectern", -1345.0, -1353.0, 5079.0)] + \
         [SubjectRecord(i, False, "") for i in range(1, MAX_SUBJECTS)]

seen: list[str] = []
handler = logging.Handler()
handler.emit = lambda r: seen.append(r.getMessage())
lg = logging.getLogger("comms.mount_manager")
lg.addHandler(handler)
lg.setLevel(logging.INFO)

mm._log_solved_subjects(5, fresh, solved)          # used to raise
assert any("Lectern" in m and "5079" in m for m in seen), \
    f"the first list logged nothing: {seen}"
assert not any("failed" in m or "Bad " in m for m in seen), \
    f"the first list still errors: {seen}"
print("   first list against [None]*8 — logs, does not raise  OK")

seen.clear()
mm._log_solved_subjects(5, solved, solved)
assert seen == [], f"an unchanged list should say nothing, said {seen}"
print("   unchanged list is silent                            OK")

seen.clear()
moved = list(solved)
moved[0] = SubjectRecord(0, True, "Lectern", -1400.0, -1300.0, 5200.0)
mm._log_solved_subjects(5, solved, moved)
assert any("5200" in m for m in seen), f"a moved subject was not reported: {seen}"
print("   a re-solved subject reports its new position        OK")

seen.clear()
mm._log_solved_subjects(5, [None] * MAX_SUBJECTS, [None] * MAX_SUBJECTS)
assert seen == [], f"an all-empty pair should say nothing, said {seen}"
print("   two empty lists are silent                          OK")

lg.removeHandler(handler)
print("\nALL CHECKS PASSED")
