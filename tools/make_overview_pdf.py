#!/usr/bin/env python3
"""
make_overview_pdf.py — generate docs/PTS_Overview.pdf

A polished two/three-page project overview: what PTS is, how the pieces fit,
and the full OSC control reference for Bitfocus Companion / QLab operators.
Regenerate after protocol or feature changes:

    python3 tools/make_overview_pdf.py
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (HRFlowable, PageBreak, Paragraph,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

OUT = Path(__file__).resolve().parent.parent / "docs" / "PTS_Overview.pdf"

# ── palette (matches the app's dark-UI identity) ────────────────────────────
INK      = colors.HexColor("#1A1A1A")
ACCENT   = colors.HexColor("#1565C0")   # PTS blue
ACCENT2  = colors.HexColor("#0E4A86")
DIM      = colors.HexColor("#5A6472")
SURFACE  = colors.HexColor("#EEF2F7")
ZEBRA    = colors.HexColor("#F7FAFD")
RED      = colors.HexColor("#B71C1C")
GREEN    = colors.HexColor("#2E7D32")
CAM_COLS = ["#66935B", "#4A7FB5", "#B09A2F", "#4E9188", "#8E6FA8"]

S = {
    "title":  ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=26,
                             textColor=colors.white, leading=30),
    "subtitle": ParagraphStyle("subtitle", fontName="Helvetica", fontSize=11.5,
                               textColor=colors.HexColor("#CFE2F7"), leading=15),
    "h1":     ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=15,
                             textColor=ACCENT2, spaceBefore=14, spaceAfter=6),
    "h2":     ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11.5,
                             textColor=INK, spaceBefore=10, spaceAfter=4),
    "body":   ParagraphStyle("body", fontName="Helvetica", fontSize=9.6,
                             textColor=INK, leading=13.6),
    "bodydim": ParagraphStyle("bodydim", fontName="Helvetica", fontSize=9,
                              textColor=DIM, leading=12.6),
    "cell":   ParagraphStyle("cell", fontName="Helvetica", fontSize=9,
                             textColor=INK, leading=12),
    "cellb":  ParagraphStyle("cellb", fontName="Helvetica-Bold", fontSize=9,
                             textColor=INK, leading=12),
    "mono":   ParagraphStyle("mono", fontName="Courier-Bold", fontSize=8.8,
                             textColor=ACCENT2, leading=12),
    "monoc":  ParagraphStyle("monoc", fontName="Courier", fontSize=8.8,
                             textColor=INK, leading=12),
    "th":     ParagraphStyle("th", fontName="Helvetica-Bold", fontSize=9,
                             textColor=colors.white, leading=11.5),
    "diagram": ParagraphStyle("diagram", fontName="Helvetica-Bold", fontSize=9,
                              textColor=INK, leading=11.5,
                              alignment=TA_CENTER),
    "diagdim": ParagraphStyle("diagdim", fontName="Helvetica", fontSize=7.6,
                              textColor=DIM, leading=9.6,
                              alignment=TA_CENTER),
    "foot":   ParagraphStyle("foot", fontName="Helvetica", fontSize=8,
                             textColor=DIM),
}


def P(text, style="cell"):
    return Paragraph(text, S[style])


def header_band(canvas, doc):
    canvas.saveState()
    w, h = A4
    canvas.setFillColor(ACCENT2)
    canvas.rect(0, h - 6 * mm, w, 6 * mm, stroke=0, fill=1)
    for i, c in enumerate(CAM_COLS):
        canvas.setFillColor(colors.HexColor(c))
        canvas.rect(i * (w / 5), h - 7.6 * mm, w / 5, 1.6 * mm, stroke=0, fill=1)
    canvas.setFillColor(DIM)
    canvas.setFont("Helvetica", 7.5)
    canvas.drawString(16 * mm, 9 * mm, "PTS - Pan/Tilt/Slider Camera Mount Control System")
    canvas.drawRightString(w - 16 * mm, 9 * mm, f"github.com/cbradburne/PTS   |   page {doc.page}")
    canvas.restoreState()


def title_block():
    inner = Table(
        [[Paragraph("PTS", S["title"]),
          Paragraph("<b>Pan / Tilt / Slider</b><br/>"
                    "A five-mount motorised camera system for live video "
                    "production - wireless, self-healing, and controllable "
                    "from everything in the booth.", S["subtitle"])]],
        colWidths=[34 * mm, 144 * mm])
    inner.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), ACCENT2),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",   (0, 0), (0, 0), 14),
        ("RIGHTPADDING",  (1, 0), (1, 0), 14),
        ("TOPPADDING",    (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
    ]))
    return inner


def styled_table(rows, widths, header=True, zebra=True, align_left_col=False):
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",    (0, 0), (-1, -1), 3.6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.6),
        ("LEFTPADDING",   (0, 0), (-1, -1), 7),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 7),
        ("LINEBELOW",     (0, 0), (-1, -1), 0.4, colors.HexColor("#D7DFE8")),
        ("BOX",           (0, 0), (-1, -1), 0.6, colors.HexColor("#C3CEDA")),
    ]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), ACCENT),
                  ("LINEBELOW",  (0, 0), (-1, 0), 0.8, ACCENT2)]
        if zebra:
            for r in range(1, len(rows)):
                if r % 2 == 0:
                    style.append(("BACKGROUND", (0, r), (-1, r), ZEBRA))
    t.setStyle(TableStyle(style))
    return t


def arch_diagram():
    def box(title, sub, bg="#FFFFFF", border="#C3CEDA", w=70 * mm):
        t = Table([[Paragraph(title, S["diagram"])],
                   [Paragraph(sub, S["diagdim"])]],
                  colWidths=[w])
        t.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor(bg)),
            ("BOX",           (0, 0), (-1, -1), 0.8, colors.HexColor(border)),
            ("TOPPADDING",    (0, 0), (-1, 0), 4),
            ("BOTTOMPADDING", (0, 1), (-1, 1), 4),
            ("TOPPADDING",    (0, 1), (-1, 1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 0),
        ]))
        return t

    top = Table([[box("Control surfaces",
                      "PC app (PyQt6, joystick, CV tracking) · web app on phones · "
                      "7\" hub touchscreen · Bitfocus Companion + QLab via OSC",
                      "#EDF3FA", "#9DB8D4", 120 * mm)]], colWidths=[178 * mm])
    mid = Table([[box("HUB - ESP32-S3",
                      "WiFi AP 'CamMount' · TCP / WebSocket / OSC servers · "
                      "USB serial to PC · UART to hub display · autonomous self-recovery",
                      "#E3EDF8", "#1565C0", 120 * mm)]], colWidths=[178 * mm])
    for t in (top, mid):
        t.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER")]))

    mounts = Table([[box(f"CAM {i + 1}",
                         "ESP32 AMOLED bridge<br/>Teensy 4.1 + TMC2209<br/>"
                         "pan · tilt · slider · zoom",
                         "#FFFFFF", CAM_COLS[i], 33.5 * mm) for i in range(5)]],
                   colWidths=[35.6 * mm] * 5)
    mounts.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 1),
        ("RIGHTPADDING", (0, 0), (-1, -1), 1),
    ]))

    arrow1 = Paragraph("- - - -  LAN / WiFi / USB  - - - -", S["diagdim"])
    arrow2 = Paragraph("- - - -  ESP-NOW wireless star (touch-screen pairing, "
                       "channel self-healing)  - - - -", S["diagdim"])
    return [top, arrow1, mid, arrow2, mounts]


def build():
    doc = SimpleDocTemplate(
        str(OUT), pagesize=A4,
        leftMargin=16 * mm, rightMargin=16 * mm,
        topMargin=14 * mm, bottomMargin=16 * mm,
        title="PTS - Camera Mount Control System",
        author="Colin Bradburne")

    el = []
    el.append(title_block())
    el.append(Spacer(1, 8))

    el.append(Paragraph(
        "PTS drives five motorised camera mounts over a dedicated wireless "
        "network. Each mount pairs a Teensy 4.1 motion controller (TMC2209 "
        "silent stepper drivers, StallGuard homing) with an ESP32 bridge and "
        "round touchscreen; a central hub links every mount to the operator's "
        "control surfaces. Positions, speed presets and subject calibrations "
        "live on the mounts themselves - controllers can come and go "
        "mid-show.", S["body"]))
    el.append(Spacer(1, 2))

    el.append(Paragraph("System architecture", S["h1"]))
    for item in arch_diagram():
        el.append(item)
        el.append(Spacer(1, 3))
    el.append(Spacer(1, 2))

    el.append(Paragraph("Highlights", S["h1"]))
    hl = [
        [P("<b>Look-at tracking</b>", "cell"),
         P("Camera keeps aiming at a calibrated 3D subject while the slider "
           "travels - subjects are calibrated in two taps and switchable "
           "mid-move from any controller.", "cell")],
        [P("<b>Zero-config pairing</b>", "cell"),
         P("No MAC addresses or per-unit firmware: hold a mount's screen, "
           "pick CAM 1-5, tap your hub. The hub learns mounts on first "
           "contact; conflicts resolve with one tap on the hub display.", "cell")],
        [P("<b>Self-healing</b>", "cell"),
         P("Autonomous recovery ladders on hub and mounts (reinit then "
           "restart), idle maintenance restarts, watchdogs at every layer, "
           "and a motion-side dead-man: a mount stops within 500 ms of "
           "losing its control stream.", "cell")],
        [P("<b>Health telemetry</b>", "cell"),
         P("Every node reports heap, loop timing, link quality and reset "
           "cause every 10 s into the PC log - degradation is visible hours "
           "before it becomes a symptom.", "cell")],
        [P("<b>Show-control ready</b>", "cell"),
         P("OSC servers on both the hub and the PC app put every function on "
           "Stream Deck buttons and QLab cue stacks (reference overleaf).", "cell")],
    ]
    el.append(styled_table(
        [[P("<b>Feature</b>", "th"), P("<b>What it does</b>", "th")]] + hl,
        [38 * mm, 140 * mm]))

    el.append(PageBreak())

    # ── OSC page ─────────────────────────────────────────────────────────
    el.append(Paragraph("OSC control reference  -  Companion / QLab", S["h1"]))
    el.append(Paragraph(
        "Two OSC servers listen on <b>UDP port 9700</b> and accept the same "
        "commands, so Companion pages fire unchanged against either target. "
        "<b>Feedback is sent by the hub only</b> - point Companion at the hub "
        "for buttons that light up to show what the rig is doing:", S["body"]))
    el.append(Spacer(1, 5))
    el.append(styled_table([
        [P("<b>Target</b>", "th"), P("<b>Address</b>", "th"), P("<b>When to use</b>", "th")],
        [P("<b>The hub itself</b>", "cellb"), P("192.168.4.1", "monoc"),
         P("No PC needed - hub + display + mounts + Stream Deck is a complete "
           "rig. Reach it via a WiFi-to-LAN bridge joined to the CamMount AP, "
           "or any machine joined to CamMount directly.", "cell")],
        [P("<b>PC app machine</b>", "cellb"), P("that PC's IP", "monoc"),
         P("When the PC app is running anyway. Configurable via osc_enabled / "
           "osc_port in the app config.", "cell")],
    ], [30 * mm, 32 * mm, 116 * mm]))
    el.append(Spacer(1, 4))
    el.append(Paragraph(
        "Companion: add a <b>Generic: OSC</b> connection with the target IP "
        "and port 9700, then use its Send message actions as below. Integer "
        "arguments preferred; floats (QLab network cues) are accepted.",
        S["bodydim"]))

    el.append(Paragraph("Command reference", S["h2"]))
    cmds = [
        ["/pts/estop", "-", "E-STOP every mount", True],
        ["/pts/cam/N/estop", "-", "E-STOP mount N", True],
        ["/pts/cam/N/goto", "slot 1-10", "Recall a stored position (uses the mount's active speed presets)", False],
        ["/pts/cam/N/store", "slot 1-10", "Store the current position", False],
        ["/pts/cam/N/clear", "slot 1-10", "Clear a stored position", False],
        ["/pts/cam/N/jog", "pan tilt slider zoom", "Velocities -1000..1000; re-streamed at 20 Hz until stopped", False],
        ["/pts/cam/N/jog/stop", "-", "Stop jogging (put on the button's release action)", False],
        ["/pts/cam/N/jog/left  /right  /up  /down", "1 press, 0 release", "Pan and tilt from plain buttons; full deflection at the active preset speed", False],
        ["/pts/cam/N/jog/slide/left  /slide/right", "1 / 0", "Slide along the rail", False],
        ["/pts/cam/N/jog/zoom/in  /zoom/out", "1 / 0", "Zoom", False],
        ["/pts/cam/N/speed/pt", "1-4", "Active pan/tilt speed preset", False],
        ["/pts/cam/N/speed/sl", "1-4", "Active slider speed preset", False],
        ["/pts/cam/N/speed/pt/up  /down", "-", "Pan/tilt preset one step, clamped at 1 and 4", False],
        ["/pts/cam/N/speed/sl/up  /down", "-", "Slider preset one step, clamped", False],
        ["/pts/cam/N/speed/pt/inc  /sl/inc", "-", "Cycle the preset 1-2-3-4-1", False],
        ["/pts/cam/N/subject", "0-7", "Select look-at subject; switches live during a move", False],
        ["/pts/cam/N/lookat", "0 or 1", "Look-at slider move to min (0) or max (1) with the selected subject", False],
        ["/pts/cam/N/autofocus", "-", "Instantaneous autofocus on that mount's Blackmagic camera (Bluetooth)", False],
        ["/pts/refresh", "-", "Resend all feedback for every mount", False],
        ["/pts/cam/N/refresh", "-", "Resend all feedback for mount N", False],
    ]
    rows = [[P("<b>Address</b>", "th"), P("<b>Arguments</b>", "th"), P("<b>Action</b>", "th")]]
    for addr, args, desc, danger in cmds:
        a = Paragraph(addr, S["mono"])
        if danger:
            a = Paragraph(f'<font color="#B71C1C">{addr}</font>', S["mono"])
        rows.append([a, P(args, "monoc"), P(desc, "cell")])
    el.append(styled_table(rows, [46 * mm, 34 * mm, 98 * mm]))
    el.append(Paragraph(
        "N = camera 1-5. Slots and speed presets are 1-based, subjects "
        "0-based - matching every screen in the system.", S["bodydim"]))

    el.append(Paragraph("Feedback - buttons that show the rig", S["h2"]))
    el.append(Paragraph(
        "The hub sends state back to whoever last commanded it, so a Stream "
        "Deck shows which shot a camera is on, whether a mount is online and "
        "what speed it is set to - with nothing tracked in Companion. Enable "
        "<b>Listen for Feedback</b> on the OSC connection, then add an "
        "<i>OSC: Listen for OSC messages (Integer)</i> feedback matching an "
        "address and value below.", S["body"]))
    el.append(Spacer(1, 4))
    fb = [
        ["/pts/cam/N/active", "0 / 1", "Mount is online"],
        ["/pts/cam/N/state", "int", "Mount state; 0 is idle"],
        ["/pts/cam/N/target", "0-10", "Slot being moved to, 0 = not moving to one"],
        ["/pts/cam/N/speed/pt", "1-4", "Active pan/tilt preset"],
        ["/pts/cam/N/speed/sl", "0-4", "Active slider preset; 0 = this mount has no rail"],
        ["/pts/cam/N/slot/M/state", "0-3", "0 empty, 1 occupied, 2 moving here, 3 arrived"],
    ]
    rows = [[P("<b>Address</b>", "th"), P("<b>Value</b>", "th"), P("<b>Meaning</b>", "th")]]
    for addr, val, desc in fb:
        rows.append([Paragraph(addr, S["mono"]), P(val, "monoc"), P(desc, "cell")])
    el.append(styled_table(rows, [46 * mm, 34 * mm, 98 * mm]))
    el.append(Paragraph(
        "Slot state is one address per slot carrying one value, so a shot "
        "button colours itself from a single feedback with nothing to combine "
        "- the same stored / moving-to / arrived picture the hub display and "
        "the web app show. Only changes are sent, so a rig at rest is silent; "
        "everything is resent every 5 s, and in full the first time an address "
        "speaks to the hub, so a surface that joins late catches up on its own. "
        "<b>If nothing arrives</b>, check Companion's Source Port is the port it "
        "also listens on, and that its machine is on the same subnet as the hub "
        "- commands still arrive across a subnet mismatch, replies cannot get "
        "back, and UDP reports no error either way.", S["bodydim"]))

    el.append(Paragraph("Button recipes", S["h2"]))
    rec = [
        [P("<b>Recall shot 3, cam 2</b>", "cellb"),
         P("Press: <font face='Courier'>/pts/cam/2/goto</font> with int 3", "cell")],
        [P("<b>Hold-to-jog pan-left, cam 1</b>", "cellb"),
         P("Press: <font face='Courier'>/pts/cam/1/jog/left</font> int 1 &nbsp;&nbsp;"
           "Release: same address, int 0", "cell")],
        [P("<b>Shot button that shows its state</b>", "cellb"),
         P("Press: <font face='Courier'>/pts/cam/1/goto</font> int 7. Feedback on "
           "<font face='Courier'>/pts/cam/1/slot/7/state</font>: 1 red (stored), "
           "2 amber (on its way), 3 green (arrived).", "cell")],
        [P("<b>One speed button per camera</b>", "cellb"),
         P("<font face='Courier'>/pts/cam/1/speed/pt/inc</font> cycles 1-2-3-4-1 per "
           "press. Add a feedback on <font face='Courier'>/pts/cam/1/speed/pt</font> "
           "to show which preset is live.", "cell")],
        [P("<b>Look-at pair, cam 3</b>", "cellb"),
         P("Button A: <font face='Courier'>/pts/cam/3/subject</font> int 2 &nbsp;&nbsp; "
           "Button B: <font face='Courier'>/pts/cam/3/lookat</font> int 1 "
           "(press A mid-move to retarget live)", "cell")],
        [P("<b>QLab cue</b>", "cellb"),
         P("Network cue to port 9700: <font face='Courier'>/pts/cam/2/goto 4</font> - "
           "camera moves fire from the cue stack, in time with light and sound.", "cell")],
        [P("<b>Show-stopper</b>", "cellb"),
         P("A big red <font face='Courier'>/pts/estop</font> (no argument).", "cell")],
    ]
    el.append(styled_table([[P("<b>Button</b>", "th"), P("<b>Setup</b>", "th")]] + rec,
                           [42 * mm, 136 * mm]))

    el.append(Paragraph("Safety", S["h2"]))
    el.append(Paragraph(
        "<b>-</b> OSC jogs are re-streamed at 20 Hz with a 15 s TTL, so a lost "
        "release message cannot run a mount away; prefer goto / lookat for "
        "long moves.<br/>"
        "<b>-</b> The mounts' own 500 ms dead-man is the final backstop: if the "
        "hub or PC dies mid-jog, motion stops regardless of the controller.<br/>"
        "<b>-</b> E-STOP over OSC clears held jog streams before broadcasting "
        "the stop.", S["body"]))

    doc.build(el, onFirstPage=header_band, onLaterPages=header_band)
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    build()
