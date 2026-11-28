#pragma once
/*
 * Shim — the Teensy build includes the canonical shared protocol header.
 *
 * This file used to be a hand-maintained COPY of the protocol and it drifted:
 * its build_status() emitted a 9-byte STATUS (missing active_la_subject) and
 * its PayloadStartLookAtMove was the obsolete 6-byte layout.  The sketch code
 * happened to hand-build those payloads correctly, so the stale helpers were
 * unused landmines rather than live bugs — but the next person to call one
 * would have shipped the wrong wire format.
 *
 * Do NOT put protocol definitions in this file.  Edit the canonical header:
 *   firmware/shared/protocol.h
 * tools/check_protocol.py (pre-commit hook) verifies this file stays a shim.
 *
 * Two include paths because the Teensy platform compiles from a COPIED sketch
 * folder (breaking ../ relative includes); the sketch-local `shared` symlink
 * is dereferenced by the copy, so the copied build finds "shared/protocol.h".
 * ESP32 targets and IDE builds that compile in place use the ../ form.
 */
#if __has_include("../shared/protocol.h")
  #include "../shared/protocol.h"
#else
  #include "shared/protocol.h"
#endif
