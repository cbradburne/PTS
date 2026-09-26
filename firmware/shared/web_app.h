#pragma once
/*
 * web_app.h — Mobile control web app, served by the hub over HTTP.
 *
 * Served at http://192.168.4.1/  once the phone connects to the hub's WiFi.
 * (The WebSocket below uses location.hostname, so it follows whatever IP the
 *  page was loaded from — no hard-coded address to keep in sync.)
 *
 * Portrait  : cam select (1-5), 10 position buttons, SET / EDIT / E-STOP
 * Landscape : cam select (1-5) + E-STOP bar, dual virtual joysticks
 *               Left  joystick → SLIDER (X) / ZOOM (Y)
 *               Right joystick → PAN   (X) / TILT (Y)
 *
 * Position slot coordinates are stored on the Teensy (volatile RAM).
 * The web app only tracks three slot states per position:
 *   empty / stored / at-stored-position
 * These come from slot_occupied_mask / slot_at_mask in every STATUS packet.
 * Recall sends CMD_GOTO_SLOT (slot index only) — no coordinates needed.
 *
 * Position labels are stored in browser localStorage.
 */

static const char WEB_APP_HTML[] PROGMEM = R"rawhtml(
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>CamMount</title>
<style>
:root {
  --cam-color: #A5D6A7;   /* bright accent — updated by JS; used by arc indicators */
  --cam-bg:    #1B3A1D;   /* dark tile colour — updated by JS; used by pos-btn fill */
  --bg:       #121212;
  --surf:     #1e1e1e;
  --surf2:    #2a2a2a;
  --border:   #333;
  --text:     #e0e0e0;
  --dim:      #777;
  --blue:     #1565c0;
  --blue-lit: #2196f3;
  --orange:   #e65100;
  --red:      #b71c1c;
  --red-lit:  #ef5350;
  --green:    #388e3c;
  --green-lit:#66bb6a;
  --yellow:   #ffc107;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;}
html,body{width:100%;height:100%;overflow:hidden;background:var(--bg);color:var(--text);
  font-family:system-ui,sans-serif;touch-action:none;user-select:none;}

/* ---- views ---- */
/* viewport-fit=cover (see the meta tag) makes the page cover the WHOLE screen
   rather than letting iOS inset it to the safe area.  Added because a mount
   operator running this from the Home Screen — a standalone web app, no browser
   chrome — lost a band across the bottom of the landscape screen to an inset
   the page never got to use.  Without viewport-fit=cover that band is not
   unused page, it is outside the page entirely, so no amount of layout work
   inside could reclaim it.
   The insets then have to be honoured by hand, and NOT symmetrically:
     top         PORTRAIT puts the status bar and Dynamic Island here and they
                 sit OVER the page, so the first row must be padded clear or it
                 is simply unreadable — which is exactly what happened: the cam
                 bar came up underneath the clock and battery.  In landscape
                 this inset is 0, so the rule costs nothing there.
     left/right  the notch physically covers content, so pad it away
     bottom      the home indicator is a translucent bar over the top of the
                 app.  Background and layout may run underneath it; only
                 touch targets need to stay clear, which #gc-bottom does
                 below.  Padding it away here would give back exactly the
                 band this change exists to recover. */
.view{display:none;width:100%;height:100%;flex-direction:column;
  padding-top:env(safe-area-inset-top);
  padding-left:env(safe-area-inset-left);padding-right:env(safe-area-inset-right);}
.view.show{display:flex;}

/* ---- cam bar ---- */
.cam-bar{display:flex;flex-direction:row;gap:5px;padding:6px;
  background:var(--surf);border-bottom:1px solid var(--border);flex-shrink:0;}
.cam-btn{flex:1;padding:8px 2px;background:var(--surf2);border:1px solid var(--border);
  border-radius:6px;color:var(--text);font-size:12px;cursor:pointer;text-align:center;
  position:relative;line-height:1.3;}
.cam-btn.sel{background:var(--blue);border-color:var(--blue-lit);}
.cam-btn .dot{display:inline-block;width:6px;height:6px;border-radius:50%;
  background:var(--dim);margin-left:3px;vertical-align:middle;}
.cam-btn .dot.on{background:var(--green-lit);}

/* ---- portrait position grid ---- */
/* Grid keeps its natural (2-row) height and never shrinks, so the position
   buttons are always fully visible; the joystick area below flexes instead.
   Fixes the top row being clipped in mobile Safari, where the toolbar leaves
   less height than standalone (home-screen) mode. */
.pos-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;padding:8px;
  flex:0 0 auto;align-content:start;overflow-y:auto;background:var(--cam-bg);}
.pos-btn{aspect-ratio:1;background:var(--surf2);border:6px solid var(--border);
  border-radius:8px;color:var(--dim);font-size:11px;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  text-align:center;padding:4px;line-height:1.2;word-break:break-word;position:relative;}
.pos-btn.stored   {color:var(--text);border-color:var(--red-lit);}
.pos-btn.at-pos   {border-color:var(--green-lit);box-shadow:0 0 6px rgba(102,187,106,.4);}
.pos-btn.moving-to{border-color:var(--yellow);box-shadow:0 0 8px rgba(255,193,7,.5);}
.pos-btn.moving-to.flash-off{border-color:var(--surf2);box-shadow:none;}
.pos-btn.set-mode    {border-color:var(--orange);color:var(--text);}
.pos-btn.edit-mode   {border-color:#888;color:var(--text);}
.pos-btn.la-subject  {color:var(--dim);}
.pos-btn.la-stored   {border-color:var(--red-lit);color:var(--text);}
.pos-btn.la-active   {border-color:var(--green-lit);color:var(--text);box-shadow:0 0 6px rgba(102,187,106,.4);}
.pos-btn.la-arrow    {font-size:18px;color:var(--text);border-color:var(--border);}
.pos-btn.la-arrow.la-moving           {border-color:var(--yellow);box-shadow:0 0 4px rgba(255,193,7,.4);}
.pos-btn.la-arrow.la-moving.flash-off {border-color:var(--border);box-shadow:none;}
.pos-btn.la-arrow.la-done             {border-color:var(--green-lit);box-shadow:0 0 6px rgba(102,187,106,.4);}
.ext-pcell.la-arrow.la-moving         {border-color:var(--yellow);}
.ext-pcell.la-arrow.la-done           {border-color:var(--green-lit);}

/* ---- subject calibration bottom-sheet ---- */
#calib-sheet{position:fixed;inset:0;background:rgba(0,0,0,.65);
  display:none;flex-direction:column;align-items:stretch;justify-content:flex-end;z-index:50;}
#calib-sheet.show{display:flex;}
#calib-card{background:var(--surf);border-radius:14px 14px 0 0;padding:20px 16px 32px;
  border-top:1px solid var(--border);}
#calib-title{font-size:15px;font-weight:600;margin-bottom:6px;color:var(--text);}
#calib-detail{font-size:12px;color:var(--dim);margin-bottom:18px;line-height:1.6;}
.calib-btns{display:flex;gap:8px;}
.calib-btn{flex:1;padding:14px 4px;border-radius:8px;border:1px solid var(--border);
  font-size:14px;font-weight:600;cursor:pointer;background:var(--surf2);color:var(--text);}
#calib-set-btn{background:var(--blue);border-color:var(--blue-lit);}
#calib-cancel-btn.ok{background:var(--green);border-color:var(--green-lit);color:#fff;}

/* ---- mounts (pairing) page ---- */
.mnt-wrap{max-width:560px;margin:0 auto;width:100%;}
.mnt-h{font-size:16px;font-weight:600;color:var(--text);margin:4px 0 8px;}
.mnt-note{font-size:12px;color:var(--dim);line-height:1.6;margin:0 0 16px;}
.mnt-row{display:flex;align-items:center;gap:10px;padding:12px;margin-bottom:8px;
  background:var(--surf);border:1px solid var(--border);border-radius:10px;}
.mnt-dot{width:12px;height:12px;border-radius:50%;flex-shrink:0;}
.mnt-cam{font-weight:600;color:var(--text);font-size:14px;width:64px;flex-shrink:0;}
.mnt-mac{flex:1;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;color:var(--text);}
.mnt-mac.un{color:var(--dim);font-family:inherit;font-style:italic;}
.mnt-forget{padding:8px 16px;border-radius:8px;border:1px solid var(--red);
  background:transparent;color:var(--red);font-size:13px;font-weight:600;cursor:pointer;}
.mnt-forget:active{background:var(--red);color:#fff;}
.mnt-forget-sp{width:1px;}
/* Route badge: quiet, because it is context for the RSSI rather than a
   fault.  A mount reads -40 with a satellite beside it and -85 without,
   and this is the only thing on screen that says which. */
.mnt-via{font-size:11px;font-weight:600;letter-spacing:.04em;color:var(--dim);
  border:1px solid var(--dim);border-radius:6px;padding:3px 7px;margin-right:10px;
  white-space:nowrap;flex-shrink:0;}
/* ---- pairing-conflict sheet (shows over any page, like the hub display) ---- */
#pair-sheet{position:fixed;inset:0;background:rgba(0,0,0,.7);display:none;
  flex-direction:column;align-items:center;justify-content:center;z-index:60;padding:16px;}
#pair-card{background:var(--surf);border:2px solid var(--red);border-radius:14px;
  padding:20px 18px;max-width:460px;width:100%;}
#pair-title{font-size:16px;font-weight:700;color:var(--red);margin-bottom:10px;}
#pair-detail{font-size:13px;color:var(--text);margin-bottom:20px;line-height:1.6;}
.pair-btns{display:flex;gap:8px;}
.pair-btn{flex:1;padding:14px 4px;border-radius:8px;border:1px solid var(--border);
  font-size:14px;font-weight:600;cursor:pointer;background:var(--surf2);color:var(--text);}
.pair-btn.pair-replace{background:var(--red);border-color:var(--red);color:#fff;}

/* ---- portrait ctrl bar ---- */
.ctrl-bar{display:flex;gap:6px;padding:8px;background:var(--surf);
  border-top:1px solid var(--border);flex-shrink:0;}
.ctrl-btn{flex:1;padding:13px 4px;border-radius:8px;border:1px solid var(--border);
  font-size:13px;font-weight:600;cursor:pointer;background:var(--surf2);color:var(--text);}
.ctrl-btn.armed{background:var(--orange);border-color:var(--orange);color:#fff;}
.ctrl-btn.clear-armed{background:var(--red-lit);border-color:var(--red-lit);color:#fff;}
#btn-estop-p,#btn-estop-l{background:var(--red);border-color:var(--red);color:#fff;}
#btn-estop-p:active,#btn-estop-l:active{background:var(--red-lit);}

/* ---- status bar ---- */
/* The bottom bars carry the safe-area inset themselves rather than the .view
   doing it, so the BAR's background runs to the physical bottom edge while its
   contents stay above the home indicator.  Padding the view instead would leave
   the same dead band this change exists to remove — just tinted --bg rather
   than black.  max() so a device with no inset keeps the original padding. */
.stat-bar{padding:4px 8px max(4px,env(safe-area-inset-bottom)) 8px;
  background:var(--surf);border-top:1px solid var(--border);
  font-size:11px;color:var(--dim);text-align:center;flex-shrink:0;}
.ws-dot{display:inline-block;width:7px;height:7px;border-radius:50%;
  background:var(--red-lit);margin-right:5px;vertical-align:middle;}
.ws-dot.on{background:var(--green-lit);}

/* ---- landscape top bar ---- */
#l-top{display:flex;align-items:stretch;padding:5px 6px;gap:6px;
  background:var(--surf);border-bottom:1px solid var(--border);flex-shrink:0;}
#l-top .cam-bar{flex:1;padding:0;background:none;border:none;}
#btn-estop-l{padding:8px 14px;border-radius:6px;font-size:13px;border:none;
  white-space:nowrap;flex-shrink:0;}

/* ---- fullscreen button ---- */
#btn-fs{width:36px;height:36px;border-radius:6px;border:1px solid var(--border);
  background:var(--surf2);color:var(--dim);cursor:pointer;flex-shrink:0;padding:0;
  display:flex;align-items:center;justify-content:center;}
#btn-fs.active{color:var(--blue-lit);border-color:var(--blue-lit);}

/* ---- landscape joystick area ---- */
#joy-area{flex:1;display:flex;flex-direction:column;justify-content:center;
  padding:6px 10px;gap:4px;overflow:hidden;background:var(--cam-bg);}
#arc-row{display:flex;flex-direction:row;justify-content:space-around;align-items:center;
  flex-shrink:0;padding:0 4px;}
.arc-cell{flex:1;display:flex;justify-content:center;}
#controls-row{display:flex;flex-direction:row;align-items:center;
  justify-content:space-around;gap:8px;overflow:hidden;}
.joy-panel{display:flex;flex-direction:column;align-items:center;
  gap:4px;flex:1;min-width:0;}
.joy-title{font-size:12px;color:var(--dim);letter-spacing:.05em;}
canvas.jc{display:block;touch-action:none;}
.joy-axes{display:flex;width:100%;justify-content:space-between;
  font-size:10px;color:var(--dim);padding:0 4px;}
canvas.arc-c{display:block;touch-action:none;}
/* ---- horizontal slider ---- */
.hsl-group{display:flex;flex-direction:column;align-items:center;gap:3px;width:100%;}
canvas.hsl-c{display:block;touch-action:none;}

/* ---- portrait joystick + sliders ---- */
#p-ctrl-area{display:flex;flex-direction:column;align-items:center;gap:6px;
  padding:8px;background:var(--surf);border-top:1px solid var(--border);
  flex:1 1 auto;min-height:0;overflow:hidden;justify-content:center;}

/* ---- portrait speed dials ---- */
.dial-bar{display:flex;flex-direction:row;justify-content:center;align-items:center;
  gap:32px;padding:6px 8px;background:var(--surf);border-top:1px solid var(--border);
  flex-shrink:0;}
.dial-wrap{display:flex;flex-direction:column;align-items:center;gap:3px;
  cursor:pointer;-webkit-tap-highlight-color:transparent;}
.dial-wrap canvas{display:block;touch-action:none;}
.dial-lbl{font-size:10px;color:var(--dim);letter-spacing:.05em;text-transform:uppercase;}

/* ---- game-controller view ---- */
/* Cam buttons scale with the screen instead of being a fixed 34 px.
   On a phone held in landscape these are the most-hit targets on the page and
   they were the smallest thing on it, while the slot grid quietly absorbed
   every spare pixel — it is the only flex:1 child, so all slack went there.
   Sizing the padding in vh spends that height on the buttons instead, and
   keeps doing so on a bigger screen without another breakpoint.
   clamp() bounds both ends: never smaller than the old 8 px on a very short
   viewport, never so tall on a tablet that it crowds the grid. */
#gc-cam-bar .cam-btn{padding:clamp(8px,4.2vh,30px) 2px;}
.gc-pos-grid{grid-template-columns:repeat(10,minmax(0,84px));justify-content:center;
  flex:1 1 auto;align-content:center;}
#gc-ctrl-row{display:flex;flex-direction:row;justify-content:center;align-items:center;
  gap:22px;padding:6px 8px;background:var(--surf);border-top:1px solid var(--border);flex-shrink:0;}
#gc-ctrl-row .ctrl-btn{flex:0 0 auto;padding:14px 30px;font-size:14px;}
/* E-STOP lives in this bar, so the inset here is not cosmetic: without it the
   button would sit under the home indicator, where a press that misses becomes
   a swipe-up out of the app — on a live rig, at the exact moment somebody is
   reaching for the stop. */
#gc-bottom{display:flex;align-items:center;justify-content:space-between;gap:8px;
  padding:6px 10px max(6px,env(safe-area-inset-bottom)) 10px;
  background:var(--surf);border-top:1px solid var(--border);flex-shrink:0;
  font-size:11px;color:var(--dim);}
#gc-bottom .gc-stat{flex:1;text-align:center;}
#gc-estop{background:var(--red);border-color:var(--red);color:#fff;flex:0 0 auto;padding:10px 22px;}
#gc-estop:active{background:var(--red-lit);}
#gc-exit{background:var(--blue);border-color:var(--blue-lit);color:#fff;flex:0 0 auto;}

/* ============================================================
   Extended view  (tablet / large screen)
   ============================================================ */
#ext-view{flex-direction:column;}
.ext-header{display:flex;align-items:center;gap:8px;padding:8px 12px;
  background:var(--surf);border-bottom:1px solid var(--border);flex-shrink:0;}
.view-toggle-btn{padding:5px 10px;border-radius:6px;border:1px solid var(--border);
  background:var(--surf2);color:var(--dim);font-size:11px;cursor:pointer;
  flex-shrink:0;white-space:nowrap;}
.ext-nav{display:flex;gap:4px;flex:1;justify-content:center;}
.ext-tab{padding:7px 20px;border-radius:6px;border:1px solid var(--border);
  background:var(--surf2);color:var(--dim);font-size:13px;cursor:pointer;font-weight:500;}
.ext-tab.active{background:var(--blue);border-color:var(--blue-lit);color:#fff;}
.ext-tab.armed{background:var(--orange);border-color:var(--orange);color:#fff;}
.ext-hdr-btn{padding:7px 20px;border-radius:6px;border:1px solid var(--border);
  background:var(--surf2);color:var(--dim);font-size:13px;cursor:pointer;font-weight:500;}
.ext-hdr-btn.armed{background:var(--orange);border-color:var(--orange);color:#fff;}
.ext-estop-btn{padding:7px 14px;border-radius:6px;border:none;
  background:var(--red);color:#fff;font-size:13px;font-weight:600;cursor:pointer;flex-shrink:0;}
.ext-body{flex:1;overflow:hidden;position:relative;}
.ext-page{display:none;width:100%;height:100%;overflow-y:auto;padding:10px;
  flex-direction:column;gap:10px;}
.ext-page.show{display:flex;}

.ext-dial-wrap{display:flex;flex-direction:column;align-items:center;gap:3px;}

/* --- Positions page --- */
/* The 5 rows share the grid-area height (flex:1 each) so all five always fit,
   shrinking on short windows instead of overflowing and hiding CAM 5. */
.ext-pos-table{display:flex;flex-direction:column;gap:6px;flex:1;min-height:0;height:100%;}
.ext-pos-row{display:flex;gap:4px;align-items:stretch;flex:1 1 0;min-height:0;}
.ext-pos-cam-lbl{width:56px;flex-shrink:0;font-size:11px;font-weight:700;
  text-align:right;padding-right:8px;align-self:center;}
.ext-pos-cells{display:flex;gap:4px;flex:1;min-height:0;}
/* No fixed aspect-ratio: cells fill the (flexing) row height, so the grid
   scales with the window rather than being locked to a width-derived square. */
.ext-pcell{flex:1;min-height:0;border-radius:5px;background:var(--surf2);
  border:4px solid var(--border);font-size:24px;color:var(--dim);
  display:flex;align-items:center;justify-content:center;cursor:pointer;
  text-align:center;padding:2px;line-height:1.1;word-break:break-all;overflow:hidden;}
.ext-pcell.stored{border-color:var(--red-lit);color:var(--text);}
.ext-pcell.at-pos{background:rgba(102,187,106,.15);border-color:var(--green-lit);color:var(--text);}
.ext-pcell.moving{border-color:var(--yellow);}
.ext-pcell.moving.flash-off{border-color:var(--surf2);}
.ext-pcell.la-arrow{font-size:26px;color:var(--text);}
/* Per-row speed dials on positions page */
.ext-pos-dials{display:flex;gap:6px;padding-left:8px;flex-shrink:0;align-items:center;align-self:center;}
.ext-pos-dial-wrap{display:flex;flex-direction:column;align-items:center;gap:1px;}
.ext-pos-dial-cv{width:40px;height:40px;display:block;}
.ext-pos-dial-lbl{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.04em;}
/* Positions page — grid top, control strip bottom */
#ext-page-positions{flex-direction:column;padding:0;gap:0;overflow:hidden;}
.ext-pos-grid-area{flex:1;min-height:0;overflow:hidden;padding:6px 8px;}
/* Bottom control strip is a static share of the page height; the joystick
   inside resizes to it (sizeExtPosControls, re-run on window resize). */
.ext-pos-ctrl-strip{flex-shrink:0;height:38%;border-top:1px solid var(--border);
  display:flex;flex-direction:row;align-items:stretch;}
/* Slider column */
.ext-pos-sliders{flex-shrink:0;display:flex;flex-direction:column;
  justify-content:space-evenly;gap:0;padding:8px 8px;border-right:1px solid var(--border);}
.ext-pos-sl-grp{display:flex;flex-direction:column;align-items:center;gap:3px;}
.ext-pos-sl-title{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.04em;}
/* Middle: cam bar + set/clear + dials */
.ext-pos-middle{flex:1;display:flex;flex-direction:column;justify-content:center;
  gap:6px;padding:8px 12px;border-right:1px solid var(--border);}
.ext-pos-ctrls{flex:1;display:flex;flex-direction:row;align-items:center;
  justify-content:center;gap:16px;flex-wrap:wrap;}
.ext-pos-act-btn{padding:11px 22px;border-radius:8px;background:var(--surf2);
  border:1px solid var(--border);color:var(--text);font-size:14px;font-weight:600;cursor:pointer;text-align:center;}
.ext-pos-act-btn.armed{background:var(--orange);border-color:var(--orange);color:#fff;}
.ext-pos-act-btn.clear-armed{background:var(--red-lit);border-color:var(--red-lit);color:#fff;}
/* Joystick section */
.ext-pos-joy-wrap{display:flex;flex-direction:column;align-items:center;
  justify-content:center;padding:6px 8px;flex-shrink:0;}
.ext-pcell.ext-pos-sel{border-color:var(--green-lit);box-shadow:0 0 0 2px var(--green-lit);color:var(--text);}

/* --- Config page --- */
.ext-config-cams{display:flex;flex-direction:row;gap:10px;overflow-x:auto;justify-content:space-evenly;}
.ext-config-cam{flex:1;min-width:160px;max-width:220px;background:var(--surf);border-radius:10px;
  border:1px solid var(--border);border-left-width:3px;padding:10px 10px 14px;
  display:flex;flex-direction:column;gap:0;overflow-y:auto;}
.ext-cfg-name{font-size:14px;font-weight:700;display:flex;align-items:center;gap:7px;
  margin-bottom:8px;}
.ext-cfg-dot{width:8px;height:8px;border-radius:50%;background:var(--dim);flex-shrink:0;}
.ext-cfg-dot.on{background:var(--green-lit);}
/* Action buttons */
.ext-cfg-actions{display:flex;flex-direction:column;gap:5px;margin-bottom:12px;}
.ext-cfg-act{width:100%;padding:7px 4px;border-radius:6px;border:1px solid var(--border);
  background:var(--surf2);color:var(--text);font-size:11px;font-weight:600;cursor:pointer;
  text-align:center;white-space:nowrap;}
.ext-cfg-act:disabled{opacity:.35;cursor:default;}
.ext-cfg-act.home{background:var(--blue);border-color:var(--blue-lit);color:#fff;}
.ext-cfg-act.home:disabled{background:var(--surf2);border-color:var(--border);color:var(--dim);}
.ext-cfg-act.refbtn{background:var(--blue);border-color:var(--blue-lit);color:#fff;}
.ext-cfg-limits{margin-top:12px;}
.ext-cfg-limrow{display:flex;flex-direction:row;gap:6px;}
.ext-cfg-act.limbtn{flex:1;width:auto;background:var(--orange);border-color:var(--orange);color:#fff;}
.ext-cfg-act.limbtn:disabled{background:var(--surf2);border-color:var(--border);color:var(--dim);}
.ext-cfg-act.refbtn:disabled{background:var(--surf2);border-color:var(--border);color:var(--dim);}
/* Speed preset tables */
.ext-spd-section{margin-bottom:10px;}
.ext-spd-hdr{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.05em;
  margin-bottom:4px;padding-bottom:2px;border-bottom:1px solid var(--border);}
.ext-spd-row{display:flex;align-items:center;gap:3px;margin-bottom:3px;}
.ext-spd-lbl{font-size:10px;color:var(--dim);width:12px;flex-shrink:0;text-align:right;}
.ext-spd-inp{width:48px;background:var(--surf2);border:1px solid var(--border);border-radius:4px;
  color:var(--text);font-size:11px;padding:2px 3px;text-align:right;min-width:0;}
.ext-spd-inp:disabled{opacity:.35;}
.ext-spd-inp:focus{outline:1px solid var(--blue-lit);border-color:var(--blue-lit);}
.ext-spd-unit{font-size:9px;color:var(--dim);white-space:nowrap;}
/* Orientation flags — one per line */
.ext-ori-section{margin-top:2px;}
.ext-ori-hdr{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.05em;
  margin-bottom:4px;padding-bottom:2px;border-bottom:1px solid var(--border);}
.ext-ori-flag-row{display:flex;align-items:center;justify-content:space-between;
  padding:3px 0;border-bottom:1px solid rgba(255,255,255,.04);}
.ext-ori-flag-lbl{font-size:11px;color:var(--text);}
.ext-ori-btn{position:relative;flex-shrink:0;width:44px;height:26px;border-radius:13px;
  border:none;background:#555;padding:0;cursor:pointer;
  transition:background .2s;}
.ext-ori-btn::after{content:'';position:absolute;top:3px;left:3px;width:20px;height:20px;
  border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.4);
  transition:transform .2s;}
.ext-ori-btn.on{background:var(--blue);}
.ext-ori-btn.on::after{transform:translateX(18px);}
.ext-ori-btn:disabled{opacity:.35;cursor:default;}
</style>
</head>
<body>

<!-- ======================================================= PORTRAIT -->
<div id="portrait-view" class="view">
  <div class="cam-bar" id="p-cam-bar"></div>
  <div class="pos-grid" id="p-pos-grid"></div>
  <div id="p-ctrl-area">
    <div class="hsl-group">
      <span class="joy-title">&#8592; ZOOM &#8594;</span>
      <canvas class="hsl-c" id="p-hsl-zoom"></canvas>
    </div>
    <div class="hsl-group">
      <span class="joy-title">&#8592; SLIDER &#8594;</span>
      <canvas class="hsl-c" id="p-hsl-slider"></canvas>
    </div>
    <canvas class="jc" id="p-joy"></canvas>
    <div class="joy-axes"><span>&#8592; PAN &#8594;</span><span>&#8593; TILT &#8595;</span></div>
  </div>
  <div class="dial-bar">
    <button class="ctrl-btn" id="btn-edit" style="flex:0 0 auto;padding:8px 14px;">EDIT</button>
    <div class="dial-wrap" id="p-dial-sz-wrap">
      <canvas id="dial-sz" width="72" height="72"></canvas>
      <span class="dial-lbl">Slider</span>
    </div>
    <div class="dial-wrap" id="p-dial-pt-wrap">
      <canvas id="dial-pt" width="72" height="72"></canvas>
      <span class="dial-lbl">Pan / Tilt</span>
    </div>
    <button class="ctrl-btn" id="btn-estop-p" style="flex:0 0 auto;padding:8px 14px;">E-STOP</button>
  </div>
  <div class="ctrl-bar">
    <button class="ctrl-btn" id="btn-clear-p">CLEAR</button>
    <button class="ctrl-btn" id="btn-set">SET</button>
  </div>
  <div class="stat-bar" style="display:flex;align-items:center;justify-content:space-between;">
    <div><span class="ws-dot" id="p-dot"></span><span id="p-stat">Connecting…</span></div>
    <button class="view-toggle-btn" id="btn-to-ext-p">&#8862; Extended</button>
  </div>
</div>

<!-- ======================================================= LANDSCAPE -->
<div id="landscape-view" class="view">
  <div id="l-top">
    <div class="cam-bar" id="l-cam-bar"></div>
    <button id="btn-fs" title="Fullscreen">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor"
           stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
        <path d="M2 6V2h4M10 2h4v4M2 10v4h4M14 10v4h-4"/>
      </svg>
    </button>
    <button class="ctrl-btn" id="btn-estop-l">E-STOP</button>
  </div>
  <div id="joy-area">
    <div id="arc-row">
      <div class="arc-cell"><canvas id="sz-arc" class="arc-c"></canvas></div>
      <div class="arc-cell"><canvas id="pt-arc" class="arc-c"></canvas></div>
    </div>
    <div id="controls-row">
      <div class="joy-panel" id="joy-left-panel">
        <div class="hsl-group">
          <span class="joy-title">&#8592; ZOOM &#8594;</span>
          <canvas class="hsl-c" id="hsl-zoom"></canvas>
        </div>
        <div class="hsl-group">
          <span class="joy-title">&#8592; SLIDER &#8594;</span>
          <canvas class="hsl-c" id="hsl-slider"></canvas>
        </div>
      </div>
      <div class="joy-panel" id="joy-right-panel">
        <canvas class="jc" id="joy-right"></canvas>
      </div>
    </div>
  </div>
  <div class="stat-bar" style="display:flex;align-items:center;justify-content:space-between;">
    <button class="view-toggle-btn" id="btn-to-gc-l">&#127918; GC</button>
    <div><span class="ws-dot" id="l-dot"></span><span id="l-stat">Connecting…</span></div>
    <button class="view-toggle-btn" id="btn-to-ext-l">&#8862; Extended</button>
  </div>
</div>

<!-- =================================================== GAME-CONTROLLER -->
<!-- Stripped-down landscape screen for when a Bluetooth pad handles motion:
     pick the camera, recall/set/clear positions and set speeds; no on-screen
     jog controls. -->
<div id="gc-view" class="view">
  <div class="cam-bar" id="gc-cam-bar"></div>
  <div class="pos-grid gc-pos-grid" id="gc-pos-grid"></div>
  <div id="gc-ctrl-row">
    <div class="dial-wrap" id="gc-dial-sz-wrap">
      <canvas id="gc-dial-sz"></canvas>
      <span class="dial-lbl">Slider</span>
    </div>
    <button class="ctrl-btn" id="gc-clear">CLEAR</button>
    <button class="ctrl-btn" id="gc-set">SET</button>
    <div class="dial-wrap" id="gc-dial-pt-wrap">
      <canvas id="gc-dial-pt"></canvas>
      <span class="dial-lbl">Pan / Tilt</span>
    </div>
  </div>
  <div id="gc-bottom">
    <button class="view-toggle-btn" id="gc-exit">&#127918; GC</button>
    <div class="gc-stat"><span class="ws-dot" id="gc-dot"></span><span id="gc-stat">Connecting…</span></div>
    <button class="ctrl-btn" id="gc-estop">E-STOP</button>
  </div>
</div>

<!-- ====================================================== CALIB OVERLAY -->
<div id="calib-sheet">
  <div id="calib-card">
    <div id="calib-title"></div>
    <div id="calib-detail"></div>
    <div class="calib-btns">
      <button class="calib-btn" id="calib-cancel-btn">Cancel</button>
    </div>
  </div>
</div>

<div id="pair-sheet">
  <div id="pair-card">
    <div id="pair-title">Pairing conflict</div>
    <div id="pair-detail"></div>
    <div class="pair-btns">
      <button class="pair-btn pair-replace" id="pair-replace-btn">Replace</button>
      <button class="pair-btn" id="pair-ignore-btn">Ignore</button>
    </div>
  </div>
</div>

<!-- ===================================================== EXTENDED VIEW -->
<div id="ext-view" class="view">
  <!-- Header: back button | nav tabs | ws-dot + e-stop -->
  <div class="ext-header">
    <button class="view-toggle-btn" id="btn-ext-to-mobile">&#9664; Mobile</button>
    <nav class="ext-nav">
      <button class="ext-tab active" data-page="positions">Home</button>
      <button class="ext-tab" data-page="config">Config</button>
      <button class="ext-tab" data-page="mounts">Mounts</button>
    </nav>
    <div style="display:flex;align-items:center;gap:8px;">
      <button class="ext-hdr-btn" id="btn-edit-ext">EDIT</button>
      <span class="ws-dot" id="ext-ws-dot"></span>
      <button class="ext-estop-btn" id="btn-estop-ext">E-STOP</button>
    </div>
  </div>

  <div class="ext-body">

    <!-- ---- Positions overview page ---- -->
    <div class="ext-page show" id="ext-page-positions">
      <!-- Top: full-width 5×10 grid -->
      <div class="ext-pos-grid-area">
        <div class="ext-pos-table" id="ext-pos-table"></div>
      </div>
      <!-- Bottom: control strip -->
      <div class="ext-pos-ctrl-strip">
        <!-- Slider + zoom HSliders -->
        <div class="ext-pos-sliders" id="ext-pos-sliders">
          <div class="ext-pos-sl-grp">
            <span class="ext-pos-sl-title">&#8592; ZOOM &#8594;</span>
            <canvas class="hsl-c" id="ext-pos-hsl-zoom"></canvas>
          </div>
          <div class="ext-pos-sl-grp">
            <span class="ext-pos-sl-title">&#8592; SLIDER &#8594;</span>
            <canvas class="hsl-c" id="ext-pos-hsl-slider"></canvas>
          </div>
        </div>
        <!-- Middle: cam selector + SET/CLEAR + speed dials -->
        <div class="ext-pos-middle">
          <div class="cam-bar" id="ext-pos-cam-bar" style="flex-wrap:wrap;gap:4px;margin-bottom:auto;"></div>
          <!-- Slider dial | CLEAR | SET | Pan/Tilt dial — same row order as the GC screen -->
          <div class="ext-pos-ctrls">
            <div class="ext-dial-wrap">
              <canvas id="ext-pos-dial-sz" width="70" height="70"></canvas>
              <span class="dial-lbl">Slider</span>
            </div>
            <button id="ext-pos-clear-btn" class="ext-pos-act-btn">CLEAR</button>
            <button id="ext-pos-set-btn" class="ext-pos-act-btn">SET</button>
            <div class="ext-dial-wrap">
              <canvas id="ext-pos-dial-pt" width="70" height="70"></canvas>
              <span class="dial-lbl">Pan / Tilt</span>
            </div>
          </div>
        </div>
        <!-- Joystick -->
        <div class="ext-pos-joy-wrap" id="ext-pos-joy-wrap">
          <canvas class="jc" id="ext-pos-joy" style="flex-shrink:0;"></canvas>
          <div class="joy-axes" style="width:100%;max-width:200px;">
            <span>&#8592; PAN &#8594;</span><span>&#8593; TILT &#8595;</span>
          </div>
        </div>
      </div>
    </div>

    <!-- ---- Config page ---- -->
    <div class="ext-page" id="ext-page-config">
      <div class="ext-config-cams" id="ext-config-cams"></div>
    </div>

    <div class="ext-page" id="ext-page-mounts">
      <div class="mnt-wrap">
        <h2 class="mnt-h">Paired mounts</h2>
        <p class="mnt-note">Each mount picks its camera number and hub on its own
          screen; the hub binds it on first contact. This is the hub's live
          pairing table &mdash; <b>Forget</b> frees a slot (a live mount re-pairs
          itself within ~5&nbsp;s, so use it for a retired unit). If two mounts
          claim the same number, a conflict prompt appears here to Replace or
          Ignore.</p>
        <div id="ext-mnt-list"></div>
      </div>
    </div>

  </div><!-- .ext-body -->
</div><!-- #ext-view -->

<script>
// ============================================================
//  Protocol  (mirrors protocol.h)
// ============================================================
const CMD_JOG                = 0x01;
const CMD_SET_SPEED_PRESET   = 0x04;  // 10B: group(1)+preset(1)+speed_u32be(4)+accel_u32be(4)
const CMD_E_STOP             = 0x08;
const CMD_STORE_POS          = 0x0C;
const CMD_CLEAR_POS          = 0x0D;
const CMD_SET_ACTIVE_PRESET  = 0x0E;
const CMD_GOTO_SLOT          = 0x10;
const CMD_SET_ORIENTATION    = 0x07;  // 1B flags, or 3B flags+int16 tilt tenths
const CMD_FIND_LIMITS        = 0x06;  // 2B: axis(1) + stall_threshold(1)
const CMD_FIND_HOME          = 0x13;  // 2B: axis(1) + stall_threshold(1)
const CMD_SET_STALL_THRESHOLD = 0x14; // 2B: axis(1) + threshold(1) — persist StallGuard threshold
const CMD_GET_CONFIG         = 0x12;  // no payload — request orientation + speeds
const CMD_ADD_SUBJECT_START  = 0x20;  // 17B: subject_id(1) + name(16)
const CMD_ADD_SUBJECT_SET_A  = 0x21;  // no payload — record point A
const CMD_ADD_SUBJECT_SET_B  = 0x22;  // no payload — record point B + solve
const CMD_ADD_SUBJECT_ABORT  = 0x23;  // no payload — cancel calibration
const CMD_SET_REF            = 0x25;  // 1B: subject_id — set pan/tilt session reference
const CMD_START_LOOK_AT_MOVE = 0x27;  // 3B: subject_id, direction(0=left/1=right), speed_preset
const CMD_SWITCH_SUBJECT     = 0x28;  // 1B: subject_id
const CMD_LOOK_AT_STATUS     = 0x91;  // 14B: slider_mm(4f)+pan_deg(4f)+tilt_deg(4f)+subj_id(1)+flags(1)
const CMD_STATUS             = 0x80;
const CMD_CONFIG_REPORT      = 0x86;  // 77B: ori(1)+speeds(72)+stall(2)+tilt(2)
const CMD_CALIB_PROMPT       = 0x93;  // 1B sub-state
const CMD_LA_MOVE_DIR        = 0x94;  // hub-injected: direction(1) — 0=min/◀, 1=max/▶, 0xFF=stopped
// STATUS target_slot: 8/9 mean the look-at arrows, and ONLY in look-at mode —
// on any other mount they are ordinary position slots 9 and 10.
const TARGET_SLOT_LA_MIN     = 8;
const TARGET_SLOT_LA_MAX     = 9;
const SLOT_LA_LEFT_END       = 8;   // slot_at bit: slider parked at the left end
const SLOT_LA_RIGHT_END      = 9;   // slot_at bit: slider parked at the right end
// Pairing management (hub owns the mount table; these view/set/clear it)
const CMD_GET_MOUNT_TABLE    = 0x9B;  // →hub, no payload: request a MOUNT_TABLE push
const CMD_MOUNT_TABLE        = 0x9C;  // hub→: 30B = 5 × MAC(6); all-zero slot = unbound
const CMD_PAIR_CONFLICT      = 0x9D;  // hub→: 13B = cam(1)+new_mac(6)+old_mac(6); cam=0 = dismiss
const CMD_PAIR_DECIDE        = 0x9E;  // →hub: 8B = cam(1)+decision(1: 1=replace, 0=ignore)+new_mac(6)
const CMD_PAIR_FORGET        = 0x9F;  // →hub: 1B = cam — clear (unbind) that slot
const CMD_MOUNT_ROUTE        = 0xA0;  // hub→: 5B = per-cam 0 = direct, N = via satellite N
const CMD_SAT_NAMES          = 0xA4;  // hub→: SAT_SLOTS × SAT_NAME_LEN, in slot order
const SAT_SLOTS              = 6;     // mirrors protocol.h
const SAT_NAME_LEN           = 13;    // SAT_NAME_MAX + NUL
// CalibPrompt sub-states (mirrors protocol.h CalibPrompt enum)
const CP_MOVING_TO_A = 0x01;  // slider moving to home — wait
const CP_WAIT_SET_A  = 0x02;  // at home: aim then Set A
const CP_MOVING_TO_B = 0x03;  // slider moving to far end — wait
const CP_WAIT_SET_B  = 0x04;  // at far end: aim again then Set B
const CP_SOLVED      = 0x05;  // 3D position solved and saved
const CP_ERROR       = 0x06;  // calibration failed (bad geometry)
const STATE_JOGGING          = 1;
const STATE_MOVING_TO_POS    = 2;
const STATE_LOOK_AT_MOVE     = 5;
const FLAG_AT_MIN_LIMIT      = 0x01;
const FLAG_AT_MAX_LIMIT      = 0x02;
const FLAG_HAS_SLIDER        = 0x10;
const FLAG_LOOK_AT_ACTIVE    = 0x40;
const FLAG_LOOK_AT_MODE      = 0x80;
// Speed group identifiers (CMD_SET_SPEED_PRESET payload byte 0)
const GROUP_PAN_TILT    = 0;
const GROUP_SLIDER_ZOOM = 1;
const GROUP_ZOOM        = 2;
// Axis identifiers (CMD_FIND_HOME payload byte 0)
const AXIS_PAN    = 0;
const AXIS_TILT   = 1;
const AXIS_SLIDER = 2;
const AXIS_ZOOM   = 3;
// Orientation byte bit masks (CMD_SET_ORIENTATION / CMD_CONFIG_REPORT payload[0])
const ORI_PAN_INV    = 0x01;
const ORI_SLIDER_INV = 0x02;
const ORI_HAS_SLIDER = 0x04;
const ORI_ZOOM_INV   = 0x08;
const ORI_LANC_ZOOM  = 0x10;
const ORI_TILT_INV   = 0x20;
const ORI_LOOK_AT    = 0x40;
const NUM_MOUNTS = 5;
// How long without a STATUS before this app calls a mount disconnected.
// Mirrors MOUNT_PRESENCE_TIMEOUT_MS in shared/protocol.h — see the note there.
// Was a bare 3000, written when a mount sent STATUS at 10 Hz.  It now sends on
// change plus a 5 s refresh, so EVERY ordinary gap exceeded that timeout: the
// mount flapped disconnected between each pair of packets and the extended
// page's speed dials, which draw blank for a disconnected mount, blinked in
// time with it.  Third place the same 10 Hz assumption was buried.
const MOUNT_PRESENCE_TIMEOUT_MS = 16000;
const NUM_SLOTS  = 10;

// ---- CRC-16/CCITT-FALSE ----
function crc16(buf, s, e) {
    let c = 0xFFFF;
    for (let i = s; i < e; i++) {
        c ^= buf[i] << 8;
        for (let b = 0; b < 8; b++)
            c = (c & 0x8000) ? ((c << 1) ^ 0x1021) & 0xFFFF : (c << 1) & 0xFFFF;
    }
    return c;
}

// ---- Packet builder ----
let _seq = 0;
function nextSeq() { return (_seq = (_seq + 1) & 0xFFFF); }

function buildPkt(mountId, cmd, payload) {
    const plen = payload ? payload.length : 0;
    const len  = 4 + plen;
    const buf  = new Uint8Array(9 + plen);
    const seq  = nextSeq();
    buf[0]=0xAA; buf[1]=0x55; buf[2]=len;
    buf[3]=mountId; buf[4]=(seq>>8)&0xFF; buf[5]=seq&0xFF; buf[6]=cmd;
    if (payload) buf.set(payload, 7);
    const crc = crc16(buf, 2, 7 + plen);
    buf[7+plen]=(crc>>8)&0xFF; buf[8+plen]=crc&0xFF;
    return buf;
}

// Per-mount accent colours — match the AMOLED MOUNT_ACCENT_HEX array (bright, for arcs)
const CAM_ACCENT = ['', '#A5D6A7', '#90CAF9', '#D4B800', '#80CBC4', '#CE93D8'];
// Per-mount dark tile colours — match the hub display C_CAM_BG array (muted, for button fills)
const CAM_BG     = ['', '#1B3A1D', '#1A2E45', '#332B00', '#002E2A', '#2E1040'];

let _ptPreset = 2, _szPreset = 2;

function setPreset(which, val, sendToMount) {
    // Always update the local variable so jog packets carry the correct preset immediately.
    if (which === 'pt') _ptPreset = val;
    else                _szPreset = val;
    if (sendToMount) {
        // Send command; visuals will update when the STATUS confirmation arrives from the mount.
        const grp = (which === 'pt') ? GROUP_PAN_TILT : GROUP_SLIDER_ZOOM;
        const p = new Uint8Array(2);
        p[0] = grp; p[1] = val;
        wsSend(buildPkt(selCam, CMD_SET_ACTIVE_PRESET, p));
    } else {
        // STATUS arrived — now update visuals.  A slider-less mount renders its
        // slider dial at preset 0 (dormant), like a disconnected axis.
        if (which === 'sz' && !camHasSlider(selCam)) val = 0;
        // Landscape arc indicators
        if (which === 'pt' && arcPT) arcPT.update(val);
        if (which === 'sz' && arcSZ) arcSZ.update(val);
        // Portrait speed dials
        if (which === 'pt' && dialPT) dialPT.update(val);
        if (which === 'sz' && dialSZ) dialSZ.update(val);
        // Game-controller view dials
        if (which === 'pt' && gcDialPT) gcDialPT.update(val);
        if (which === 'sz' && gcDialSZ) gcDialSZ.update(val);
        // Extended detail dials

        // Extended positions-page dials
        if (which === 'pt' && extPosDialPT) extPosDialPT.update(val);
        if (which === 'sz' && extPosDialSZ) extPosDialSZ.update(val);
    }
}

function mkJog(id, pan, tilt, slider, zoom) {
    const p = new Uint8Array(10);
    const v = new DataView(p.buffer);
    v.setInt16(0, clamp(pan),    false);
    v.setInt16(2, clamp(tilt),   false);
    v.setInt16(4, clamp(slider), false);
    v.setInt16(6, clamp(zoom),   false);
    p[8] = _ptPreset;
    p[9] = _szPreset;
    return buildPkt(id, CMD_JOG, p);
}

// Recall by slot index only — mount looks up its own stored coordinates
function mkGotoSlot(id, slot) {
    const p = new Uint8Array(2);
    p[0] = slot & 0xFF;
    p[1] = _ptPreset;
    return buildPkt(id, CMD_GOTO_SLOT, p);
}

function mkEStop(id) { return buildPkt(id, CMD_E_STOP, null); }

function mkSwitchSubject(id, subjId) {
    const p = new Uint8Array(1);
    p[0] = subjId & 0x07;
    return buildPkt(id, CMD_SWITCH_SUBJECT, p);
}

function mkStartLookAtMove(id, subjId, direction, speedPreset) {
    const p = new Uint8Array(3);
    p[0] = subjId & 0x07;
    p[1] = direction;     // 0 = go left/min, 1 = go right/max
    p[2] = speedPreset;
    return buildPkt(id, CMD_START_LOOK_AT_MOVE, p);
}

function mkGetConfig(id)  { return buildPkt(id, CMD_GET_CONFIG, null); }

// ---- Pairing management (reads/writes the hub's mount table; nothing local) ----
let mountTable   = [];      // 5 × [6 MAC bytes]; empty until the first MOUNT_TABLE
let mountRoute = [];      // per-cam: 0 = direct, N = relayed by satellite N
// Satellite slot -> location name, so a relayed mount reads "via Foyer" rather
// than "via SAT 2".  The hub has broadcast these all along -- CMD_SAT_NAMES,
// _ws.binaryAll -- and this page was the one client ignoring them, which is why
// it disagreed with the PC app about the same rig.  A slot that never named
// itself stays absent so the number shows through rather than a blank.
let satNames = {};
let pairConflict = null;    // {cam, newMac[6], oldMac[6]} while a conflict is live

function macStr(m) {
    return (m && m.some(b => b))
        ? m.map(b => b.toString(16).padStart(2, '0')).join(':') : null;
}
function reqMountTable() { wsSend(buildPkt(0xFE, CMD_GET_MOUNT_TABLE, null)); }
function pairForget(cam) { wsSend(buildPkt(0xFE, CMD_PAIR_FORGET, [cam])); }
function pairDecide(cam, decision) {
    const mac = (pairConflict && pairConflict.cam === cam)
        ? pairConflict.newMac : [0, 0, 0, 0, 0, 0];
    wsSend(buildPkt(0xFE, CMD_PAIR_DECIDE, [cam, decision & 1, ...mac]));
    // The hub echoes an updated table + a cam=0 dismiss, which clears the UI.
}

function refreshMounts() {
    const el = document.getElementById('ext-mnt-list');
    if (!el) return;
    let h = '';
    for (let i = 1; i <= NUM_MOUNTS; i++) {
        const mac = macStr(mountTable[i - 1]);
        h += '<div class="mnt-row">'
           +   '<span class="mnt-dot" style="background:' + (CAM_ACCENT[i] || '#888') + '"></span>'
           +   '<span class="mnt-cam">CAM ' + i + '</span>'
           +   '<span class="mnt-mac' + (mac ? '' : ' un') + '">'
           +     (mac || '— unpaired —') + '</span>'
           +   (mountRoute[i - 1] ? '<span class="mnt-via">via '
                                        + (satNames[mountRoute[i - 1]]
                                           || ('SAT ' + mountRoute[i - 1]))
                                        + '</span>' : '')
           +   (mac ? '<button class="mnt-forget" data-cam="' + i + '">Forget</button>'
                    : '<span class="mnt-forget-sp"></span>')
           + '</div>';
    }
    el.innerHTML = h;
    el.querySelectorAll('.mnt-forget').forEach(b =>
        b.addEventListener('click', () => pairForget(parseInt(b.dataset.cam, 10))));
}

function renderConflict() {
    const sheet = document.getElementById('pair-sheet');
    if (!sheet) return;
    if (!pairConflict) { sheet.style.display = 'none'; return; }
    const c = pairConflict, oldM = macStr(c.oldMac);
    document.getElementById('pair-detail').innerHTML =
        'A new device <b>' + macStr(c.newMac) + '</b> is claiming <b>CAM ' + c.cam + '</b>'
        + (oldM ? ', which is currently paired to <b>' + oldM + '</b>.' : '.')
        + '<br><br><b>Replace</b> binds the new device to CAM ' + c.cam
        + '; <b>Ignore</b> keeps the current one.';
    sheet.style.display = 'flex';
}
function mkSetSpeedPreset(id, group, preset, speed, accel) {
    const p = new Uint8Array(10);
    p[0]=group; p[1]=preset;
    p[2]=(speed>>24)&0xFF; p[3]=(speed>>16)&0xFF; p[4]=(speed>>8)&0xFF; p[5]=speed&0xFF;
    p[6]=(accel>>24)&0xFF; p[7]=(accel>>16)&0xFF; p[8]=(accel>>8)&0xFF; p[9]=accel&0xFF;
    return buildPkt(id, CMD_SET_SPEED_PRESET, p);
}

// tiltDeg omitted or null sends the FLAGS ONLY, which the mount reads as
// "tilt unchanged".  That matters: a flag toggle must not carry a tilt of 0
// just because this page has not been told the real one yet.
function mkSetOrientation(id, oriByte, tiltDeg) {
    if (tiltDeg === undefined || tiltDeg === null) {
        const p = new Uint8Array(1); p[0] = oriByte & 0xFF;
        return buildPkt(id, CMD_SET_ORIENTATION, p);
    }
    const t = Math.round(tiltDeg * 10) & 0xFFFF;   // int16, tenths of a degree
    const p = new Uint8Array(3);
    p[0] = oriByte & 0xFF;
    p[1] = (t >> 8) & 0xFF;
    p[2] = t & 0xFF;
    return buildPkt(id, CMD_SET_ORIENTATION, p);
}

// CMD_FIND_LIMITS: 2 bytes — axis (1B) + stall threshold (1B).  Measures the
// full travel of the axis (both ends), where Find Home only seeks the min end.
function mkFindLimits(id, axis) {
    const stored = axis === AXIS_SLIDER ? camSt[id].slThresh : camSt[id].zmThresh;
    const p = new Uint8Array(2); p[0] = axis; p[1] = (stored !== null) ? stored : 80;
    return buildPkt(id, CMD_FIND_LIMITS, p);
}

// CMD_FIND_HOME: 2 bytes — axis (1B) + stall threshold (1B)
// Uses the stored per-mount threshold from CONFIG_REPORT; falls back to 80.
function mkFindHome(id, axis) {
    const stored = axis === AXIS_SLIDER ? camSt[id].slThresh : camSt[id].zmThresh;
    const p = new Uint8Array(2); p[0] = axis; p[1] = (stored !== null) ? stored : 80;
    return buildPkt(id, CMD_FIND_HOME, p);
}

// CMD_SET_STALL_THRESHOLD: 2 bytes — axis (1B) + threshold (1B, 0–255)
function mkSetStallThreshold(id, axis, threshold) {
    const p = new Uint8Array(2); p[0] = axis; p[1] = threshold & 0xFF;
    return buildPkt(id, CMD_SET_STALL_THRESHOLD, p);
}

// CMD_SET_REF: set current pan/tilt as session reference for subject_id
function mkSetRef(id, subjId) {
    const p = new Uint8Array(1); p[0] = subjId & 0x07;
    return buildPkt(id, CMD_SET_REF, p);
}

function mkAddSubjectStart(id, subjId, name) {
    const p = new Uint8Array(17);
    p[0] = subjId & 0x07;
    const enc = new TextEncoder().encode(name.substring(0, 16));
    p.set(enc, 1);
    return buildPkt(id, CMD_ADD_SUBJECT_START, p);
}
function mkAddSubjectSetA(id)  { return buildPkt(id, CMD_ADD_SUBJECT_SET_A, null); }
function mkAddSubjectSetB(id)  { return buildPkt(id, CMD_ADD_SUBJECT_SET_B, null); }
function mkAddSubjectAbort(id) { return buildPkt(id, CMD_ADD_SUBJECT_ABORT, null); }

function mkStorePos(id, slot) {
    const p = new Uint8Array(1); p[0] = slot;
    return buildPkt(id, CMD_STORE_POS, p);
}

function mkClearPos(id, slot) {
    const p = new Uint8Array(1); p[0] = slot;
    return buildPkt(id, CMD_CLEAR_POS, p);
}

function clamp(v) { return Math.max(-1000, Math.min(1000, Math.round(v))); }

// ============================================================
//  State
// ============================================================
let selCam = 1;
let uiMode = 'move';  // 'move' | 'set' | 'edit' | 'clear'

function makeCamState() {
    return {
        connected:        false,
        state:            0,      // MountState from STATUS
        flags:            0,
        activePtPreset:   2,
        activeSlPreset:   2,
        slotOccupied:     0,      // bitmask bits 0-9: slot has stored position
        slotAt:           0,      // bitmask bits 0-9: currently AT that slot
        targetSlot:       0xFF,   // slot being moved to (0xFF = none) — from Teensy STATUS
        activeLaSubject:  -1,     // look-at mode: locally selected subject index (-1 = none)
        laArrow:          null,   // null=idle, 'left'=◀ moving, 'right'=▶ moving, 'left-done', 'right-done'
        calibPhase:        0,      // current CALIB_PROMPT phase (0 = idle)
        calibSubjectId:   -1,      // subject slot being calibrated
        oriByte:          null,    // orientation flags byte from CONFIG_REPORT (null = not yet received)
        ptPresets:        null,    // [{spd,acc}×4] from CONFIG_REPORT, null until received
        slPresets:        null,    // [{spd,acc}×4] from CONFIG_REPORT, null until received
        zmPreset:         null,    // {spd,acc} from CONFIG_REPORT, null until received
        sliderTilt:       null,    // rail inclination in degrees from CONFIG_REPORT
                                   // (null until received).  Read-only here — set
                                   // in the PC app; shown so the value in force is
                                   // visible from the phone.
        slThresh:         null,    // slider StallGuard threshold (0–255) from CONFIG_REPORT
        zmThresh:         null,    // zoom   StallGuard threshold (0–255) from CONFIG_REPORT
    };
}

function camIsLookAt(cam) {
    return !!(camSt[cam].flags & FLAG_HAS_SLIDER) && !!(camSt[cam].flags & FLAG_LOOK_AT_MODE);
}

// Slider-less mounts show their slider dial dormant (preset 0 — the same look as
// a disconnected axis) and ignore taps on it, matching the PC app and the hub
// display.  A live, tappable slider speed for an axis that isn't fitted is a lie.
function camHasSlider(cam) {
    return !!(camSt[cam].flags & FLAG_HAS_SLIDER);
}

const camSt = {};
for (let i = 1; i <= NUM_MOUNTS; i++) camSt[i] = makeCamState();

const _stTime = {};

// ---- Labels only in localStorage (coords live on Teensy) ----
function lblKey(cam, slot) { return `cm_c${cam}_s${slot}_lbl`; }
function loadLabel(cam, slot) {
    try {
        const v = localStorage.getItem(lblKey(cam, slot)) || '';
        // Strip legacy default labels ('Subject N', 'P1'–'P10') that should never persist
        if (/^Subject \d+$/.test(v) || /^P\d+$/.test(v)) { localStorage.removeItem(lblKey(cam, slot)); return ''; }
        return v;
    } catch(e) { return ''; }
}
function saveLabel(cam, slot, label) {
    try { localStorage.setItem(lblKey(cam, slot), label); } catch(e) {}
}
function clearLabel(cam, slot) {
    try { localStorage.removeItem(lblKey(cam, slot)); } catch(e) {}
}

// ============================================================
//  Flash timer (500 ms — drives moving-to border)
// ============================================================
let _flashOn = false;
setInterval(() => {
    _flashOn = !_flashOn;
    // Repaint if any mount has an active target or a moving look-at arrow.
    let any = false;
    for (let i = 1; i <= NUM_MOUNTS; i++) {
        const la = camSt[i].laArrow;
        if (camSt[i].targetSlot >= 0 || la === 'left' || la === 'right') { any = true; break; }
    }
    if (any) {
        if (_extActive) refreshExtAll();
        else {
            const la = camSt[selCam].laArrow;
            if (camSt[selCam].targetSlot >= 0 || la === 'left' || la === 'right') refreshPosGrid();
        }
    }
}, 500);

// ============================================================
//  WebSocket
// ============================================================
let ws = null;
let _wsRetryTimer = null;

function wsSend(buf) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(buf.buffer);
}
// Jog-only variant: drop if send buffer is building up.
// Prevents runaway bufferedAmount that causes the ESP32 to kill the connection,
// while tolerating brief WiFi delays without dropping every packet.
function wsSendJog(buf) {
    if (ws && ws.readyState === WebSocket.OPEN && ws.bufferedAmount < 512)
        ws.send(buf.buffer);
}

function _wsScheduleRetry() {
    if (_wsRetryTimer !== null) return;   // already pending
    _wsRetryTimer = setTimeout(() => { _wsRetryTimer = null; wsConnect(); }, 2000);
}

function wsConnect() {
    if (_wsRetryTimer !== null) { clearTimeout(_wsRetryTimer); _wsRetryTimer = null; }
    // Don't stack connections — skip if one is already open or mid-handshake
    if (ws && ws.readyState <= WebSocket.OPEN) return;
    try {
        const sock = new WebSocket('ws://' + location.hostname + '/ws');
        ws = sock;
        sock.binaryType = 'arraybuffer';
        // Capture `sock` (not `ws`) in each handler so a stale socket's events
        // never operate on a newer socket that has already replaced `ws`.
        sock.onopen    = () => { if (ws === sock) { setWsSt(true); reqMountTable(); } };
        sock.onclose   = () => { if (ws === sock) { setWsSt(false); _wsScheduleRetry(); } };
        sock.onerror   = () => sock.close();   // close THIS socket, not whatever ws points to now
        sock.onmessage = e => onPkt(new Uint8Array(e.data));
    } catch(e) { _wsScheduleRetry(); }
}

// Reconnect immediately when the app returns to foreground.
// Browsers throttle/suspend setTimeout while hidden, so the 2-second retry
// timer may never fire after a screen lock or app switch.
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible')
        if (!ws || ws.readyState >= WebSocket.CLOSING) wsConnect();
});
// iOS Safari bfcache restore / app resume — visibilitychange is unreliable there.
window.addEventListener('pageshow', e => {
    if (e.persisted || !ws || ws.readyState >= WebSocket.CLOSING) wsConnect();
});

function setWsSt(ok) {
    ['p-dot','l-dot','gc-dot','ext-ws-dot'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.className = 'ws-dot' + (ok ? ' on' : '');
    });
    const txt = ok ? 'Connected to hub' : 'Reconnecting…';
    ['p-stat','l-stat','gc-stat'].forEach(id => {
        const el = document.getElementById(id); if (el) el.textContent = txt;
    });
}

function onPkt(buf) {
    // The hub may concatenate multiple Teensy packets into one WebSocket frame
    // (e.g. CALIB_PROMPT + STATUS in the same ESP-NOW relay message).
    // Walk the entire buffer so every packet is processed.
    let off = 0;
    while (off + 9 <= buf.length) {
        if (buf[off] !== 0xAA || buf[off + 1] !== 0x55) { off++; continue; }
        const lenF  = buf[off + 2];
        const plen  = lenF - 4;
        if (plen < 0 || off + 9 + plen > buf.length) { off++; continue; }
        const total = 9 + plen;
        const calc  = crc16(buf, off + 2, off + 7 + plen);
        const recv  = (buf[off + 7 + plen] << 8) | buf[off + 8 + plen];
        if (calc !== recv) { off++; continue; }
        _onOnePkt(buf, off);
        off += total;
    }
}

function _onOnePkt(buf, off) {
    const mountId = buf[off + 3];
    const cmd     = buf[off + 6];
    const plen    = buf[off + 2] - 4;

    if (cmd === CMD_STATUS && plen >= 2 && mountId >= 1 && mountId <= NUM_MOUNTS) {
        // STATUS payload (10 bytes):
        //   [0] state  [1] flags  [2] pt_preset  [3] sl_preset
        //   [4..5] slot_occupied_mask  [6..7] slot_at_mask  [8] target_slot  [9] active_la_subject
        const v  = new DataView(buf.buffer, buf.byteOffset + off + 7);
        const cs = camSt[mountId];

        cs.prevState  = cs.state;
        cs.prevFlags  = cs.flags;
        cs.state      = v.getUint8(0);
        cs.flags      = v.getUint8(1);
        cs.connected  = true;
        _stTime[mountId] = Date.now();

        // Clear the locally-selected look-at subject whenever look-at mode toggles.
        const prevLa = !!(cs.prevFlags & FLAG_HAS_SLIDER) && !!(cs.prevFlags & FLAG_LOOK_AT_MODE);
        if (prevLa !== camIsLookAt(mountId)) {
            cs.activeLaSubject = -1;
        }

        if (plen >= 9) {
            const pt = v.getUint8(2);
            const sl = v.getUint8(3);
            if (pt >= 1 && pt <= 4) {
                const was = cs.activePtPreset;
                cs.activePtPreset = pt;
                if (mountId === selCam) setPreset('pt', pt, false);
                // The extended positions page draws a dial per mount from
                // camSt, not just for selCam, so it needs telling too — it is
                // the page whose dials used to move on touch.
                if (was !== pt && _extActive && _extPage === 'positions')
                    refreshExtPositions();
            }
            if (sl >= 1 && sl <= 4) {
                const was = cs.activeSlPreset;
                cs.activeSlPreset = sl;
                if (mountId === selCam) setPreset('sz', sl, false);
                if (was !== sl && _extActive && _extPage === 'positions')
                    refreshExtPositions();
            }
            cs.slotOccupied = v.getUint16(4, false);
            cs.slotAt       = v.getUint16(6, false);
            cs.targetSlot   = v.getUint8(8);
            // Byte [9]: active look-at subject (0-7) or 0xFF = none.
            if (plen >= 10) {
                const subj = v.getUint8(9);
                cs.activeLaSubject = (subj <= 7) ? subj : -1;
            }
        } else {
            cs.targetSlot = 0xFF;
        }

        // If a look-at calibration move just finished, treat it as slider-arrived.
        // The Teensy does not reliably send CP_WAIT_SET_B (sub=4), so we detect
        // the transition from STATE_MOVING_TO_POS in STATUS instead — same approach
        // used by the esp32_display fix.
        if (camIsLookAt(mountId) &&
                cs.calibPhase === CP_MOVING_TO_B &&
                cs.prevState  === STATE_MOVING_TO_POS &&
                cs.state      !== STATE_MOVING_TO_POS) {
            cs.calibPhase = CP_WAIT_SET_B;
            if (mountId === selCam) {
                hideCalibSheet();
                refreshPosGrid();   // re-arms SET button
                return;
            }
        }

        // Look-at arrow state — driven by target_slot, which the MOUNT sets when
        // a look-at move actually starts and clears when the controller releases
        // the axes (arrival, E-stop or abort alike).
        //
        // This used to follow CMD_LA_MOVE_DIR, injected by the hub as it relayed
        // the command — the hub's intent, not the mount's state — so a press lost
        // on the radio left an arrow flashing for a move that never ran, with
        // nothing able to correct it.  Reading the mount also retires the race
        // window that held 'moving' while waiting for the Teensy to enter
        // LOOK_AT_MOVE: target_slot only appears once it genuinely has.
        if (camIsLookAt(mountId)) {
            // Green comes from the mount, not from "a move just finished here".
            // slot_at bits 8/9 say the slider IS parked at that end, so the
            // arrow clears itself when it leaves — whoever moved it. The latch
            // this replaces could only see moves this page had watched, and its
            // AT_MIN/AT_MAX fallback was unreliable anyway: those flags are set
            // by whichever axis hits a limit and cleared by whichever is checked
            // last, so they say nothing certain about the slider.
            if      (cs.targetSlot === TARGET_SLOT_LA_MIN) cs.laArrow = 'left';
            else if (cs.targetSlot === TARGET_SLOT_LA_MAX) cs.laArrow = 'right';
            else if (cs.slotAt & (1 << SLOT_LA_LEFT_END))  cs.laArrow = 'left-done';
            else if (cs.slotAt & (1 << SLOT_LA_RIGHT_END)) cs.laArrow = 'right-done';
            else                                          cs.laArrow = null;
        }

        refreshCamBtns();
        if (mountId === selCam) refreshPosGrid();
        if (_extActive) refreshExtAll();
    }

    if (cmd === CMD_LOOK_AT_STATUS && plen >= 13 && mountId >= 1 && mountId <= NUM_MOUNTS) {
        // payload[12] = active subject_id (0-7, or 0xFF = none/deselected)
        const subjId = buf[off + 7 + 12];
        const newActive = (subjId <= 7) ? subjId : -1;
        if (newActive !== camSt[mountId].activeLaSubject) {
            camSt[mountId].activeLaSubject = newActive;
            if (mountId === selCam) refreshPosGrid();
            if (_extActive) refreshExtAll();
        }
    }

    if (cmd === CMD_CALIB_PROMPT && plen >= 1 && mountId >= 1 && mountId <= NUM_MOUNTS) {
        const phase = buf[off + 7];
        camSt[mountId].calibPhase = phase;
        if (mountId === selCam) {
            if (phase === CP_MOVING_TO_A || phase === CP_MOVING_TO_B) {
                // Show "moving" overlay — Cancel only, no Set button
                updateCalibSheet(phase);
                document.getElementById('calib-sheet').classList.add('show');
            } else if (phase === CP_WAIT_SET_A) {
                // User was already aimed at the subject when they tapped the slot —
                // confirm point A automatically, no user action required.
                wsSend(mkAddSubjectSetA(selCam));
            } else if (phase === CP_WAIT_SET_B) {
                // Slider reached the far end — dismiss overlay so the user can use
                // the joystick to re-aim.  SET button re-arms to confirm point B.
                hideCalibSheet();
                refreshPosGrid();   // re-arms portrait SET button via calibPhase check
                // Extended Home page: re-arm SET button for point-B confirmation
                if (_extActive && _extPage === 'positions') refreshExtPositions();
            } else if (phase === CP_SOLVED) {
                const solvedSlot = camSt[mountId].calibSubjectId;
                camSt[mountId].calibSubjectId = -1;
                camSt[mountId].calibPhase = 0;
                _extLaSetMode = false; _extSetMode = false;
                // Auto-select the newly added subject so it shows a green border
                if (solvedSlot >= 0) camSt[mountId].activeLaSubject = solvedSlot;
                refreshPosGrid();
                if (_extActive && _extPage === 'positions') refreshExtPositions();
            } else if (phase === CP_ERROR) {
                camSt[mountId].calibPhase = 0;
                _extLaSetMode = false; _extSetMode = false;
                hideCalibSheet();
                refreshPosGrid();
                if (_extActive && _extPage === 'positions') refreshExtPositions();
            }
        }
    }

    if (cmd === CMD_CONFIG_REPORT && plen >= 1 && mountId >= 1 && mountId <= NUM_MOUNTS) {
        const cs   = camSt[mountId];
        const base = off + 7;   // payload byte 0 in the raw buffer
        cs.oriByte = buf[base]; // orientation flags always present
        if (plen >= 73) {       // full report includes 4×PT + 4×SL + 1×ZM presets (72 speed bytes)
            function _r32(o) { return ((buf[base+o]<<24)|(buf[base+o+1]<<16)|(buf[base+o+2]<<8)|buf[base+o+3])>>>0; }
            cs.ptPresets = [];
            for (let p = 0; p < 4; p++) { const o=1+p*8; cs.ptPresets.push({spd:_r32(o),   acc:_r32(o+4)}); }
            cs.slPresets = [];
            for (let p = 0; p < 4; p++) { const o=33+p*8; cs.slPresets.push({spd:_r32(o),  acc:_r32(o+4)}); }
            cs.zmPreset = { spd: _r32(65), acc: _r32(69) };
        }
        if (plen >= 75) {       // stall thresholds: slider at byte 73, zoom at byte 74
            cs.slThresh = buf[base + 73];
            cs.zmThresh = buf[base + 74];
        }
        if (plen >= 77) {       // rail inclination, int16 tenths of a degree
            let t = (buf[base + 75] << 8) | buf[base + 76];
            if (t & 0x8000) t -= 0x10000;          // sign-extend
            cs.sliderTilt = t / 10;
        }
        if (_extActive && _extPage === 'config') refreshExtConfig();
    }

    if (cmd === CMD_MOUNT_ROUTE && plen >= 5) {
        const base = off + 7;
        mountRoute = Array.from(buf.subarray(base, base + NUM_MOUNTS));
        if (_extActive && _extPage === 'mounts') refreshMounts();
    }

    if (cmd === CMD_SAT_NAMES && plen >= SAT_SLOTS * SAT_NAME_LEN) {
        // Same layout the PC app decodes: fixed-width NUL-padded slots, slot i
        // reported as i+1 to match what CMD_MOUNT_ROUTE puts in mountRoute.
        const base = off + 7;
        satNames = {};
        for (let i = 0; i < SAT_SLOTS; i++) {
            const raw = buf.subarray(base + i * SAT_NAME_LEN,
                                     base + (i + 1) * SAT_NAME_LEN);
            let end = raw.indexOf(0);
            if (end < 0) end = raw.length;
            const name = new TextDecoder().decode(raw.subarray(0, end)).trim();
            if (name) satNames[i + 1] = name;
        }
        if (_extActive && _extPage === 'mounts') refreshMounts();
    }

    if (cmd === CMD_MOUNT_TABLE && plen >= 30) {
        const base = off + 7;
        mountTable = [];
        for (let i = 0; i < NUM_MOUNTS; i++)
            mountTable.push(Array.from(buf.subarray(base + i * 6, base + i * 6 + 6)));
        if (_extActive && _extPage === 'mounts') refreshMounts();
    }

    if (cmd === CMD_PAIR_CONFLICT && plen >= 13) {
        const base = off + 7, cam = buf[base];
        pairConflict = (cam >= 1 && cam <= NUM_MOUNTS) ? {
            cam:    cam,
            newMac: Array.from(buf.subarray(base + 1, base + 7)),
            oldMac: Array.from(buf.subarray(base + 7, base + 13)),
        } : null;                       // cam 0 = dismiss
        renderConflict();
    }
}

// Disconnect detection — no STATUS for 3 s
setInterval(() => {
    const now = Date.now();
    let changed = false;
    for (let i = 1; i <= NUM_MOUNTS; i++) {
        const was = camSt[i].connected;
        camSt[i].connected = !!_stTime[i] &&
                             (now - _stTime[i] < MOUNT_PRESENCE_TIMEOUT_MS);
        if (was !== camSt[i].connected) {
            camSt[i].targetSlot = 0xFF;
            if (!camSt[i].connected) {
                camSt[i].oriByte    = null;   // stale config on disconnect
                camSt[i].ptPresets  = null;
                camSt[i].slPresets  = null;
                camSt[i].zmPreset   = null;
                camSt[i].slThresh   = null;
                camSt[i].zmThresh   = null;
            }
            changed = true;
        }
    }
    if (changed) refreshCamBtns();
    if (changed && _extActive) refreshExtAll();
}, 500);

// ============================================================
//  Cam buttons  (built once, shared between views)
// ============================================================
function makeCamBtns(containerId) {
    const bar = document.getElementById(containerId);
    bar.innerHTML = '';
    for (let i = 1; i <= NUM_MOUNTS; i++) {
        const btn = document.createElement('button');
        btn.className = 'cam-btn';
        btn.dataset.cam = i;
        btn.innerHTML = `CAM ${i}<span class="dot" id="${containerId}-dot${i}"></span>`;
        btn.addEventListener('click', () => {
            // Positions cam bar: CLEAR armed → clear all 10 slots for this camera
            if (containerId === 'ext-pos-cam-bar' && _extClearMode) {
                const cs = camSt[i];
                if (cs.connected) {
                    for (let s = 0; s < NUM_SLOTS; s++) {
                        if (cs.slotOccupied & (1 << s)) wsSend(mkClearPos(i, s));
                    }
                }
                if (_extActive && _extPage === 'positions') refreshExtPositions();
                return;
            }
            selCam = i;
            const col = CAM_ACCENT[i];
            document.documentElement.style.setProperty('--cam-color', col);
            document.documentElement.style.setProperty('--cam-bg', CAM_BG[i]);
            refreshCamBtns();
            refreshPosGrid();
            const cs = camSt[i];
            setPreset('pt', cs.activePtPreset, false);
            setPreset('sz', cs.activeSlPreset, false);
            if (arcPT) arcPT.setColor(col);
            if (arcSZ) arcSZ.setColor(col);
            if (_extActive && _extPage === 'positions') refreshExtPositions();
        });
        bar.appendChild(btn);
    }
}

function refreshCamBtns() {
    document.querySelectorAll('.cam-btn').forEach(btn => {
        const id = parseInt(btn.dataset.cam);
        btn.classList.toggle('sel', id === selCam);
    });
    ['p-cam-bar','l-cam-bar','gc-cam-bar','ext-pos-cam-bar'].forEach(barId => {
        for (let i = 1; i <= NUM_MOUNTS; i++) {
            const dot = document.getElementById(`${barId}-dot${i}`);
            if (dot) dot.className = 'dot' + (camSt[i].connected ? ' on' : '');
        }
    });
}

// ============================================================
//  Portrait — position grid
// ============================================================
function buildPosGrid() {
    // Portrait grid and the game-controller grid share the same slot buttons and
    // click handler; refreshPosGrid() paints both from the selected camera's state.
    ['p-pos-grid', 'gc-pos-grid'].forEach(gridId => {
        const grid = document.getElementById(gridId);
        if (!grid) return;
        grid.innerHTML = '';
        for (let s = 0; s < NUM_SLOTS; s++) {
            const btn = document.createElement('button');
            btn.className = 'pos-btn';
            btn.dataset.slot = s;
            btn.addEventListener('click', () => onPosClick(s));
            grid.appendChild(btn);
        }
    });
}

function refreshPosGrid() {
    const cs = camSt[selCam];
    const la = camIsLookAt(selCam);

    // SET button: armed in set-mode, or when waiting for the user to confirm
    // look-at point B (slider has arrived; user re-aims then presses SET).
    const waitingSetB = la && cs.calibPhase === CP_WAIT_SET_B;
    const setArmed = uiMode === 'set' || waitingSetB;
    document.getElementById('btn-set').classList.toggle('armed', setArmed);
    const gcSetBtn = document.getElementById('gc-set');
    if (gcSetBtn) gcSetBtn.classList.toggle('armed', setArmed);

    ['p-pos-grid', 'gc-pos-grid'].forEach(gridId => {
    for (let s = 0; s < NUM_SLOTS; s++) {
        const btn = document.querySelector(`#${gridId} [data-slot="${s}"]`);
        if (!btn) continue;

        if (la) {
            if (s >= 8) {
                // Arrow buttons — border reflects look-at move direction state.
                btn.textContent = s === 8 ? '◀' : '▶';
                const dir = s === 8 ? 'left' : 'right';
                const arr = cs.laArrow;
                let cls = 'pos-btn la-arrow';
                if (arr === dir)             cls += ' la-moving' + (_flashOn ? '' : ' flash-off');
                else if (arr === dir+'-done') cls += ' la-done';
                btn.className = cls;
            } else {
                // Subject buttons 1-8
                const stored = !!(cs.slotOccupied & (1 << s));
                const active = (s === cs.activeLaSubject);
                const lbl = loadLabel(selCam, s);
                btn.textContent = lbl || `${s + 1}`;
                btn.className = 'pos-btn la-subject'
                    + (stored ? ' la-stored' : '')
                    + (active ? ' la-active' : '')
                    + (uiMode === 'edit' ? ' edit-mode' : '');
            }
        } else {
            const occupied  = !!(cs.slotOccupied & (1 << s));
            const atPos     = !!(cs.slotAt       & (1 << s));
            const isTarget  = (cs.targetSlot !== 0xFF && cs.targetSlot === s);
            const lbl       = loadLabel(selCam, s);

            btn.textContent = lbl || `${s + 1}`;

            let cls = 'pos-btn';
            if (isTarget) {
                cls += ' moving-to';
                if (!_flashOn) cls += ' flash-off';
            } else if (occupied && atPos) {
                cls += ' stored at-pos';
            } else if (occupied) {
                cls += ' stored';
            }
            if (uiMode === 'edit') cls += ' edit-mode';
            btn.className = cls;
        }
    }
    });
}

function onPosClick(slot) {
    const cs = camSt[selCam];

    if (camIsLookAt(selCam)) {
        if (!cs.connected) return;
        if (slot >= 8) {
            // Arrow buttons — start look-at move to slider limit
            const subj = cs.activeLaSubject >= 0 ? cs.activeLaSubject : 0;
            const dir  = (slot === 8) ? 0 : 1;   // 0=left/min, 1=right/max
            wsSend(mkStartLookAtMove(selCam, subj, dir, cs.activeSlPreset || 2));
        } else {
            if (uiMode === 'set') {
                if (!cs.connected) { alert('Camera not connected'); return; }
                cs.calibSubjectId = slot;
                wsSend(mkAddSubjectStart(selCam, slot, ''));
                setUiMode('move');
            } else if (uiMode === 'clear') {
                if (!(cs.slotOccupied & (1 << slot))) return;
                if (cs.activeLaSubject === slot) cs.activeLaSubject = -1;
                cs.slotOccupied &= ~(1 << slot);
                wsSend(mkClearPos(selCam, slot));
                refreshPosGrid();
            } else if (uiMode === 'edit') {
                const occupied = !!(cs.slotOccupied & (1 << slot));
                if (!occupied) return;
                const cur = loadLabel(selCam, slot) || String(slot + 1);
                const lbl = prompt('Subject name (clear to delete):', cur);
                if (lbl === null) return;
                if (lbl.trim() === '') {
                    clearLabel(selCam, slot);
                    wsSend(mkClearPos(selCam, slot));
                } else {
                    saveLabel(selCam, slot, lbl.trim());
                }
                refreshPosGrid();
            } else {
                // Move mode — toggle local selection and send switch command
                if (cs.activeLaSubject === slot) {
                    cs.activeLaSubject = -1;
                } else {
                    cs.activeLaSubject = slot;
                    wsSend(mkSwitchSubject(selCam, slot));
                }
                refreshPosGrid();
            }
        }
        return;
    }

    if (uiMode === 'set') {
        if (!cs.connected) { alert('Camera not connected'); return; }
        // Store current mount position — mount sends back updated STATUS immediately
        wsSend(mkStorePos(selCam, slot));
        setUiMode('move');

    } else if (uiMode === 'clear') {
        if (!(cs.slotOccupied & (1 << slot))) return;
        if (cs.slotAt & (1 << slot)) cs.slotAt &= ~(1 << slot);
        cs.slotOccupied &= ~(1 << slot);
        wsSend(mkClearPos(selCam, slot));
        refreshPosGrid();

    } else if (uiMode === 'edit') {
        const occupied = !!(cs.slotOccupied & (1 << slot));
        if (!occupied) return;
        const cur = loadLabel(selCam, slot) || `P${slot+1}`;
        const lbl = prompt('Position name (clear to delete):', cur);
        if (lbl === null) return;
        if (lbl.trim() === '') {
            clearLabel(selCam, slot);
            wsSend(mkClearPos(selCam, slot));
        } else {
            saveLabel(selCam, slot, lbl.trim());
        }
        refreshPosGrid();

    } else {
        // Move mode — recall by slot index; mount uses its own stored coordinates.
        // targetSlot will be set by the next STATUS packet from the Teensy,
        // which arrives on all connected clients simultaneously.
        const occupied = !!(cs.slotOccupied & (1 << slot));
        if (!occupied) return;
        if (!cs.connected) { alert('Camera not connected'); return; }
        wsSend(mkGotoSlot(selCam, slot));
    }
}

function setUiMode(m) {
    uiMode = m;
    document.getElementById('btn-edit').classList.toggle('armed', m === 'edit');
    const editBtnExt = document.getElementById('btn-edit-ext');
    if (editBtnExt) editBtnExt.classList.toggle('armed', m === 'edit');
    const clearBtn = document.getElementById('btn-clear-p');
    if (clearBtn) clearBtn.classList.toggle('clear-armed', m === 'clear');
    const gcClearBtn = document.getElementById('gc-clear');
    if (gcClearBtn) gcClearBtn.classList.toggle('clear-armed', m === 'clear');
    refreshPosGrid();   // refreshPosGrid manages btn-set / gc-set armed state
    if (_extActive && _extPage === 'positions') refreshExtPositions();
}

// SET click — shared by the portrait and game-controller SET buttons: confirm
// look-at point B when we're waiting for it, otherwise toggle set-mode.
function doSetClick() {
    const cs = camSt[selCam];
    if (camIsLookAt(selCam) && cs.calibPhase === CP_WAIT_SET_B) {
        wsSend(mkAddSubjectSetB(selCam));
    } else {
        setUiMode(uiMode === 'set' ? 'move' : 'set');
    }
}

function updateCalibSheet(phase) {
    const titleEl = document.getElementById('calib-title');
    const detEl   = document.getElementById('calib-detail');
    if (phase === CP_MOVING_TO_A || phase === CP_MOVING_TO_B) {
        titleEl.textContent = 'Moving slider…';
        detEl.textContent   = 'Wait for the slider to reach the other end.';
    } else if (phase === CP_ERROR) {
        titleEl.textContent = '✗ Failed';
        detEl.textContent   = 'Calibration failed. Check slider range and try again.';
    }
}

function hideCalibSheet() {
    document.getElementById('calib-sheet').classList.remove('show');
}

// ============================================================
//  Speed Dial  (portrait — arc matches hub display; tap to cycle preset 1→4)
// ============================================================
let dialPT = null, dialSZ = null;
let gcDialPT = null, gcDialSZ = null;

class SpeedDial {
    // Arc geometry mirrors the LVGL hub-display dial:
    //   300° sweep, start at 120° CW from right (≈ 8 o'clock)
    //   4 dots at the exact arc positions for each preset level
    //   indicator fills (preset-1)/3 of the sweep; dot[i] lights when i < preset
    constructor(canvasId, which) {
        this.cv     = document.getElementById(canvasId);
        this.ctx    = this.cv.getContext('2d');
        this.which  = which;   // 'pt' or 'sz'
        this.preset = 2;
        // Crisp rendering on high-DPI screens
        const dpr = window.devicePixelRatio || 1;
        const css = 90;
        this._css = css;
        this.cv.style.width  = css + 'px';
        this.cv.style.height = css + 'px';
        this.cv.width  = css * dpr;
        this.cv.height = css * dpr;
        this.ctx.scale(dpr, dpr);
        // Tap / click cycles the preset for this group
        this.cv.addEventListener('click', () => {
            if (this.which === 'sz' && !camHasSlider(selCam)) return;  // dormant
            setPreset(this.which, (this.preset % 4) + 1, true);
        });
        this.draw();
    }

    update(n) {
        this.preset = Math.max(0, Math.min(4, n));   // 0 = dormant (e.g. slider-less mount)
        this.draw();
    }

    draw() {
        const c  = this.ctx;
        const sz = this._css;
        const cx = sz / 2, cy = sz / 2;
        const R  = sz * 0.32;   // arc track radius — pulled inward to leave room for dots
        const Rd = sz * 0.38;   // dot ring radius — sits outside the arc track
        const tw = sz * 0.09;   // background track width
        const iw = sz * 0.10;   // indicator width
        const dr = sz * 0.028;  // dot radius
        const n  = this.preset;
        const N  = 5;           // 5 dots → 4 segments → 4 speed levels
        const dormant = (n <= 0);                    // slider-less / disabled axis
        const lit = dormant ? '#3a3a3a' : '#00aeef';

        const startA = (120 * Math.PI) / 180;   // 8-o'clock
        const sweepA = (300 * Math.PI) / 180;   // 300° clockwise to 2-o'clock
        // Fill covers n out of 4 segments (dot 0 → dot n)
        const fillA  = startA + (n / 4) * sweepA;

        c.clearRect(0, 0, sz, sz);

        // Background track
        c.beginPath();
        c.arc(cx, cy, R, startA, startA + sweepA);
        c.strokeStyle = '#1a1a1a';
        c.lineWidth   = tw;
        c.lineCap     = 'round';
        c.stroke();

        // Filled indicator — covers segments 0 through n-1 (none when dormant)
        if (!dormant) {
            c.beginPath();
            c.arc(cx, cy, R, startA, fillA);
            c.strokeStyle = lit;
            c.lineWidth   = iw;
            c.lineCap     = 'round';
            c.stroke();
        }

        // 5 dots on a wider ring (Rd) — sit outside the arc track; dot i lit when i <= n
        for (let i = 0; i < N; i++) {
            const a  = startA + (i / (N - 1)) * sweepA;
            const dx = cx + Rd * Math.cos(a);
            const dy = cy + Rd * Math.sin(a);
            c.beginPath();
            c.arc(dx, dy, dr, 0, Math.PI * 2);
            c.fillStyle = (!dormant && i <= n) ? lit : '#252525';
            c.fill();
        }

        // Centre number — a dash when dormant (mount has no slider)
        c.fillStyle      = dormant ? '#555' : lit;
        c.font           = `bold ${Math.round(sz * 0.34)}px system-ui,sans-serif`;
        c.textAlign      = 'center';
        c.textBaseline   = 'middle';
        c.fillText(dormant ? '–' : n, cx, cy);
    }
}

// ============================================================
//  ArcPreset  (landscape — arc speed indicator, style matching the AMOLED)
//   mirror=false → left panel (SZ): dots/fill grow left→right
//   mirror=true  → right panel (PT): dots/fill grow right→left
// ============================================================
class ArcPreset {
    constructor(canvasId, which, mirror) {
        this.cv     = document.getElementById(canvasId);
        this.ctx    = this.cv.getContext('2d');
        this.which  = which;
        this.mirror = mirror;
        this.preset = 2;
        this.color  = CAM_ACCENT[1];
        this._dpr   = window.devicePixelRatio || 1;
        this._w = 0; this._h = 0;
        this._R = 0; this._cx = 0; this._cy = 0;
        // Tap cycles the preset (same as portrait SpeedDial)
        this.cv.addEventListener('click', () =>
            setPreset(this.which, (this.preset % 4) + 1, true));
    }

    resize(w) {
        const HALF   = 20 * Math.PI / 180;  // ±20° = 40° total sweep
        const DOT_R  = 10;          // must match draw()
        const LINE_W = 12;          // must match draw()
        // Horizontal padding so dots + stroke don't bleed off the canvas edges
        const H_PAD  = DOT_R + Math.ceil(LINE_W / 2);  // 16 px each side
        // Top padding must fit the arc-apex dot (which sits at y = TOP_PAD)
        const TOP_PAD= DOT_R + 3;   // 13 px
        const BOT_PAD= 4;
        const dpr    = this._dpr;
        // Shrink the arc chord by H_PAD on each side so endpoints sit inside canvas
        const arc_w  = Math.max(20, w - 2 * H_PAD);
        const R      = (arc_w / 2) / Math.sin(HALF);
        const sagitta= R * (1 - Math.cos(HALF));
        const h      = Math.ceil(TOP_PAD + sagitta + DOT_R + BOT_PAD);
        this._w = w; this._h = h; this._R = R;
        this._cx = w / 2;           // centred on full canvas width
        // Circle centre is below the canvas so the arc arches upward
        this._cy = TOP_PAD + R;
        const cw = Math.round(w * dpr);
        const ch = Math.round(h * dpr);
        if (this.cv.width !== cw || this.cv.height !== ch) {
            this.cv.width  = cw; this.cv.height = ch;
            this.cv.style.width  = w + 'px';
            this.cv.style.height = h + 'px';
            this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        }
        this.draw();
    }

    update(n) { this.preset = Math.max(1, Math.min(4, n)); this.draw(); }
    setColor(hex) { this.color = hex; this.draw(); }

    draw() {
        if (!this._w) return;
        const c  = this.ctx;
        const w  = this._w, h = this._h;
        const cx = this._cx, cy = this._cy, R = this._R;
        const n  = this.preset;
        const col = this.color;
        const dim = '#262626';
        c.clearRect(0, 0, w, h);

        // Arc spans 250°→290° (±20° around 270° = the "top" of the circle below).
        // Clockwise through 270° produces the upward arch shape.
        const DEG = Math.PI / 180;
        const A0 = 250 * DEG;   // left end of arc
        const A4 = 290 * DEG;   // right end of arc
        const N  = 5;           // 5 dots → 4 segments → 4 speed levels
        const DOT_R  = 10;
        const LINE_W = 12;

        // Evenly-spaced dot angles along the arc
        const angles = [];
        for (let i = 0; i < N; i++)
            angles.push(A0 + (i / (N - 1)) * (A4 - A0));

        // Dot screen coordinates
        const pts = angles.map(a => ({
            x: cx + R * Math.cos(a),
            y: cy + R * Math.sin(a)
        }));

        // 4 arc segments between consecutive dots
        for (let i = 0; i < 4; i++) {
            // not-mirrored (SZ/left):  segment i lit when i < n  (fills left→right)
            // mirrored     (PT/right): segment i lit when i >= 4-n (fills right→left)
            const lit = this.mirror ? (i >= 4 - n) : (i < n);
            c.beginPath();
            c.arc(cx, cy, R, angles[i], angles[i + 1]);
            c.strokeStyle = lit ? col : dim;
            c.lineWidth   = LINE_W;
            c.lineCap     = 'butt';
            c.stroke();
        }

        // 5 dots on top of the arc segments
        for (let i = 0; i < N; i++) {
            // not-mirrored: dot i lit when i <= n   (leftmost dot always lit at preset≥1)
            // mirrored:     dot i lit when i >= N-1-n (rightmost dot always lit)
            const lit = this.mirror ? (i >= N - 1 - n) : (i <= n);
            c.beginPath();
            c.arc(pts[i].x, pts[i].y, DOT_R, 0, Math.PI * 2);
            c.fillStyle = lit ? col : dim;
            c.fill();
        }
    }
}

// ============================================================
//  HSlider — 1-axis horizontal drag widget, snaps to centre on release
// ============================================================
// Helper: rounded-rect path (Canvas Level 1 compatible)
function rrPath(c,x,y,w,h,r){
    c.moveTo(x+r,y);c.lineTo(x+w-r,y);c.quadraticCurveTo(x+w,y,x+w,y+r);
    c.lineTo(x+w,y+h-r);c.quadraticCurveTo(x+w,y+h,x+w-r,y+h);
    c.lineTo(x+r,y+h);c.quadraticCurveTo(x,y+h,x,y+h-r);
    c.lineTo(x,y+r);c.quadraticCurveTo(x,y,x+r,y);c.closePath();
}

class HSlider {
    constructor(canvasId, onChange) {
        this.cv  = document.getElementById(canvasId);
        this.ctx = this.cv.getContext('2d');
        this.val = 0;   // normalised [-1, 1]
        this.pid = null;
        this.cb  = onChange;
        this.cv.addEventListener('pointerdown',        e => this._dn(e));
        this.cv.addEventListener('pointermove',        e => this._mv(e));
        this.cv.addEventListener('pointerup',          e => this._up(e));
        this.cv.addEventListener('pointercancel',      e => this._up(e));
        this.cv.addEventListener('lostpointercapture', e => {
            if (e.pointerId !== this.pid) return;
            this.pid = null; this.val = 0; this.cb(0); this.draw();
        });
    }

    resize(w, h) { this.cv.width = w; this.cv.height = h; this.draw(); }

    _tw()  { return Math.round(this.cv.height * 0.82); }   // thumb width ≈ square
    _maxR(){ return (this.cv.width - this._tw()) / 2; }    // max pixel deflection

    _norm(e) {
        const r   = this.cv.getBoundingClientRect();
        const cx  = this.cv.width / 2;
        const dx  = e.clientX - r.left - cx;
        const mx  = this._maxR();
        const raw = mx > 0 ? Math.max(-1, Math.min(1, dx / mx)) : 0;
        // Apply deadzone
        const dead = 0.04;
        if (Math.abs(raw) < dead) return 0;
        return Math.sign(raw) * (Math.abs(raw) - dead) / (1 - dead);
    }

    _dn(e) {
        if (this.pid !== null) return;
        this.pid = e.pointerId;
        this.cv.setPointerCapture(e.pointerId);
        this.val = this._norm(e); this.cb(this.val); this.draw();
        e.preventDefault();
    }
    _mv(e) {
        if (e.pointerId !== this.pid) return;
        this.val = this._norm(e); this.cb(this.val); this.draw();
        e.preventDefault();
    }
    _up(e) {
        if (e.pointerId !== this.pid) return;
        this.pid = null; this.val = 0; this.cb(0); this.draw();
    }

    cancel() {
        if (this.pid === null) return;
        try { this.cv.releasePointerCapture(this.pid); } catch(e) {}
        this.pid = null; this.val = 0; this.cb(0); this.draw();
    }

    get active() { return this.pid !== null; }

    draw() {
        const c = this.ctx;
        const w = this.cv.width, h = this.cv.height;
        const act = this.active;
        const tw = this._tw(), th = tw;
        const mx = this._maxR();
        const cx = w / 2 + this.val * mx;
        const ty = (h - th) / 2;

        c.clearRect(0, 0, w, h);

        // Track
        const tr = h * 0.28;
        c.beginPath(); rrPath(c, 0, (h - tr*2)/2, w, tr*2, tr);
        c.fillStyle = act ? 'rgba(33,150,243,0.12)' : '#1c1c1c'; c.fill();
        c.strokeStyle = act ? '#2196f3' : '#3a3a3a'; c.lineWidth = 1.5; c.stroke();

        // Centre tick
        c.beginPath();
        c.moveTo(w/2, h*0.22); c.lineTo(w/2, h*0.78);
        c.strokeStyle = '#404040'; c.lineWidth = 1; c.stroke();

        // Thumb
        const g = c.createLinearGradient(cx-tw/2, ty, cx+tw/2, ty+th);
        g.addColorStop(0, act ? '#90caf9' : '#5a5a5a');
        g.addColorStop(1, act ? '#1565c0' : '#2e2e2e');
        c.beginPath(); rrPath(c, cx-tw/2, ty, tw, th, 7);
        c.fillStyle = g; c.fill();
        c.strokeStyle = act ? '#64b5f6' : '#555'; c.lineWidth = 1.5; c.stroke();
    }
}

// ============================================================
//  Joystick
// ============================================================
class Joystick {
    constructor(canvasId, onChange) {
        this.cv  = document.getElementById(canvasId);
        this.ctx = this.cv.getContext('2d');
        this.x   = 0;
        this.y   = 0;
        this.pid = null;
        this.cb  = onChange;
        this.cv.addEventListener('pointerdown',      e => this._dn(e));
        this.cv.addEventListener('pointermove',      e => this._mv(e));
        this.cv.addEventListener('pointerup',        e => this._up(e));
        this.cv.addEventListener('pointercancel',    e => this._up(e));
        // Safety net: browser may fire lostpointercapture without a matching
        // pointercancel (e.g. when the canvas is hidden during orientation change).
        // Without this, this.pid stays non-null and all subsequent touches are blocked.
        this.cv.addEventListener('lostpointercapture', e => {
            if (e.pointerId !== this.pid) return;
            this.pid = null; this.x = 0; this.y = 0;
            this.cb(0, 0); this.draw();
        });
    }

    resize(sz) { this.cv.width = sz; this.cv.height = sz; this.draw(); }

    _R()  { return this.cv.width * 0.42; }
    _cxy(){ return [this.cv.width/2, this.cv.height/2]; }

    _norm(e) {
        const r  = this.cv.getBoundingClientRect();
        const [cx,cy] = this._cxy(), R = this._R();
        let dx = e.clientX - r.left - cx;
        let dy = e.clientY - r.top  - cy;
        const d = Math.sqrt(dx*dx + dy*dy);
        if (d > R) { dx = dx/d*R; dy = dy/d*R; }
        // Circular → square mapping: scale so the largest component reaches ±1.0
        // at full deflection in any direction.  At 45° both axes hit 1.0;
        // magnitude is preserved so partial deflection gives proportional speed.
        let x = dx/R, y = dy/R;
        const mag  = Math.sqrt(x*x + y*y);
        const maxC = Math.max(Math.abs(x), Math.abs(y));
        if (maxC > 0) { x = x/maxC*mag; y = y/maxC*mag; }
        return [x, y];
    }

    _dn(e) {
        if (this.pid !== null) return;
        this.pid = e.pointerId;
        this.cv.setPointerCapture(e.pointerId);
        [this.x, this.y] = this._norm(e);
        this.cb(this.x, this.y);
        this.draw();
        e.preventDefault();
    }

    _mv(e) {
        if (e.pointerId !== this.pid) return;
        [this.x, this.y] = this._norm(e);
        this.cb(this.x, this.y);
        this.draw();
        e.preventDefault();
    }

    _up(e) {
        if (e.pointerId !== this.pid) return;
        this.pid = null;
        this.x = 0; this.y = 0;
        this.cb(0, 0);
        this.draw();
    }

    // Explicitly release capture and reset state — called when the joystick view
    // is hidden (orientation change) to prevent the captured-but-invisible canvas
    // from absorbing all subsequent touch events on the portrait UI.
    cancel() {
        if (this.pid === null) return;
        try { this.cv.releasePointerCapture(this.pid); } catch(e) {}
        this.pid = null; this.x = 0; this.y = 0;
        this.cb(0, 0); this.draw();
    }

    get active() { return this.pid !== null; }

    draw() {
        const c = this.ctx;
        const w = this.cv.width, h = this.cv.height;
        const [cx, cy] = [w/2, h/2];
        const R = this._R(), kr = R * 0.26;
        const act = this.active;

        c.clearRect(0, 0, w, h);

        c.beginPath(); c.arc(cx, cy, R, 0, Math.PI*2);
        c.strokeStyle = act ? '#2196f3' : '#3a3a3a'; c.lineWidth = 2; c.stroke();

        if (act) {
            c.beginPath(); c.arc(cx, cy, R-1, 0, Math.PI*2);
            c.fillStyle = 'rgba(33,150,243,0.06)'; c.fill();
        }

        c.strokeStyle = '#2a2a2a'; c.lineWidth = 1;
        c.beginPath(); c.moveTo(cx-R, cy); c.lineTo(cx+R, cy); c.stroke();
        c.beginPath(); c.moveTo(cx, cy-R); c.lineTo(cx, cy+R); c.stroke();

        const kx = cx + this.x * R, ky = cy + this.y * R;
        const g = c.createRadialGradient(kx-kr*.35, ky-kr*.35, 0, kx, ky, kr);
        g.addColorStop(0, act ? '#90caf9' : '#666');
        g.addColorStop(1, act ? '#1565c0' : '#333');
        c.beginPath(); c.arc(kx, ky, kr, 0, Math.PI*2);
        c.fillStyle = g; c.fill();
        c.strokeStyle = act ? '#64b5f6' : '#555'; c.lineWidth = 1.5; c.stroke();
    }
}

// ============================================================
//  JOG loop  (20 Hz, EMA-smoothed)
// ============================================================
let _jl = {x:0, y:0};   // raw finger position (set by Joystick callbacks)
let _jr = {x:0, y:0};
let _lastJogSent = 0;
let _jogRafHandle = null;  // non-null = loop is running; value = cancelAnimationFrame token

// Send the current joystick velocity.
// force=true  → wsSend  (never dropped; used for zero-velocity stop on release)
// force=false → wsSendJog, rate-limited to 80 ms (drops if buffer backing up)
function _sendJog(force) {
    const pan    = clamp(_jr.x *  1000);
    const tilt   = clamp(_jr.y * -1000);
    const slider = clamp(_jl.x *  1000);   // hsl-slider: right = positive
    const zoom   = clamp(_jl.y *  1000);   // hsl-zoom:   right = zoom in = positive

    // Any movement: immediately clear slotAt so green "at-position" border drops
    // without waiting for the next STATUS packet (mirrors hub display behaviour).
    if (pan !== 0 || tilt !== 0 || slider !== 0 || zoom !== 0) {
        const cs = camSt[selCam];
        if (cs.slotAt !== 0) {
            cs.slotAt = 0;
            refreshPosGrid();
            if (_extActive && _extPage === 'positions') refreshExtPositions();
        }
        // In look-at mode, also deselect the active subject immediately.
        if (camIsLookAt(selCam) && cs.activeLaSubject !== -1) {
            cs.activeLaSubject = -1;
            refreshPosGrid();
        }
    }

    if (force) {
        wsSend(mkJog(selCam, pan, tilt, slider, zoom));
        _lastJogSent = performance.now();
    } else {
        const now = performance.now();
        if (now - _lastJogSent < 80) return;
        _lastJogSent = now;
        wsSendJog(mkJog(selCam, pan, tilt, slider, zoom));
    }
}

// requestAnimationFrame keepalive — one chain, started on touch-down, hard-
// cancelled on touch-up via cancelAnimationFrame so no zombie chains accumulate.
function _jogFrame() {
    _sendJog(false);
    _jogRafHandle = requestAnimationFrame(_jogFrame);
}

function _startJogLoop() {
    if (_jogRafHandle !== null) return;          // already running
    _jogRafHandle = requestAnimationFrame(_jogFrame);
}

function _stopJogLoop() {
    if (_jogRafHandle !== null) {
        cancelAnimationFrame(_jogRafHandle);     // kills the pending callback
        _jogRafHandle = null;
    }
    _sendJog(true);   // guaranteed zero-velocity stop
}

// ============================================================
//  Gamepad  (Bluetooth / USB controller — Gamepad API)
// ------------------------------------------------------------
//   left stick  X  -> slider      right stick X -> pan
//   (left stick Y unused)         right stick Y -> tilt
//   LT / RT triggers -> zoom      (zoom = RT - LT, so both fully
//                                  pressed cancels to 0)
// Drives the *selected* camera through the same jog path as the
// on-screen sticks (_jl / _jr + _sendJog), so the at-position and
// look-at side-effects and the 80 ms rate-limit come along for free.
// ============================================================
let _padRaf   = null;    // non-null while polling a connected pad
let _padDrive = false;   // pad is commanding motion (owes a zero-stop on release)

// Per-axis deadzone with edge rescaling (same shape as the on-screen sticks).
function _padDead(v, dz) {
    const a = Math.abs(v);
    if (a < dz) return 0;
    return Math.sign(v) * (a - dz) / (1 - dz);
}
function _padAxis(gp, i) { return (gp.axes && gp.axes.length > i) ? gp.axes[i] : 0; }
function _padTrig(gp, i) { const b = gp.buttons && gp.buttons[i];
    return b == null ? 0 : (typeof b === 'object' ? b.value : b); }

function _padActivePad() {
    const pads = navigator.getGamepads ? navigator.getGamepads() : [];
    for (const p of pads) if (p && p.connected) return p;
    return null;
}

function _padPoll() {
    const gp = _padActivePad();
    if (gp) {
        const DZ = 0.10, TDZ = 0.04;
        let   slider = _padDead(_padAxis(gp, 0), DZ);   // left stick X
        const pan    = _padDead(_padAxis(gp, 2), DZ);   // right stick X
        const tilt   = _padDead(_padAxis(gp, 3), DZ);   // right stick Y (down = +, as on-screen)
        const lt     = _padDead(_padTrig(gp, 6), TDZ);  // left trigger  -> zoom out
        const rt     = _padDead(_padTrig(gp, 7), TDZ);  // right trigger -> zoom in
        const zoom   = rt - lt;                          // both fully pressed = 0
        if (!camHasSlider(selCam)) slider = 0;           // this mount has no rail

        if (pan || tilt || slider || zoom) {
            _jr.x = pan;    _jr.y = tilt;
            _jl.x = slider; _jl.y = zoom;
            _padDrive = true;
            _sendJog(false);            // rate-limited to 80 ms internally
        } else if (_padDrive) {
            _jr.x = _jr.y = _jl.x = _jl.y = 0;
            _sendJog(true);             // guaranteed zero-velocity stop
            _padDrive = false;
        }
    }
    _padRaf = requestAnimationFrame(_padPoll);
}

function _startPad() { if (_padRaf === null) _padRaf = requestAnimationFrame(_padPoll); }
function _stopPad() {
    if (_padRaf !== null) { cancelAnimationFrame(_padRaf); _padRaf = null; }
    if (_padDrive) { _jr.x = _jr.y = _jl.x = _jl.y = 0; _sendJog(true); _padDrive = false; }
}

// Small auto-hiding toast so the operator knows a controller is live.
let _padToastEl = null, _padToastT = null;
function _padToast(msg) {
    if (!_padToastEl) {
        _padToastEl = document.createElement('div');
        _padToastEl.style.cssText = 'position:fixed;left:50%;bottom:18px;transform:translateX(-50%);' +
            'background:#1565c0;color:#fff;font:600 13px system-ui,sans-serif;padding:8px 14px;' +
            'border-radius:8px;z-index:9999;pointer-events:none;opacity:0;transition:opacity .2s;' +
            'box-shadow:0 4px 16px rgba(0,0,0,.4)';
        document.body.appendChild(_padToastEl);
    }
    _padToastEl.textContent = msg;
    _padToastEl.style.opacity = '1';
    clearTimeout(_padToastT);
    _padToastT = setTimeout(() => { _padToastEl.style.opacity = '0'; }, 2200);
}

if ('getGamepads' in navigator) {
    // Most browsers only surface the pad after the first button press.
    window.addEventListener('gamepadconnected', e => {
        const id = (e.gamepad && e.gamepad.id ? e.gamepad.id.split('(')[0] : 'Controller').trim();
        _padToast('🎮 ' + (id || 'Controller') + ' connected');
        _startPad();
    });
    window.addEventListener('gamepaddisconnected', () => {
        if (!_padActivePad()) { _stopPad(); _padToast('🎮 Controller disconnected'); }
    });
}

// ============================================================
//  Fullscreen  (Fullscreen API — Android Chrome; hidden on iOS where unavailable)
// ============================================================
function toggleFullscreen() {
    const el  = document.documentElement;
    const isFs = !!(document.fullscreenElement || document.webkitFullscreenElement);
    if (isFs) {
        (document.exitFullscreen || document.webkitExitFullscreen || function(){}).call(document);
    } else {
        const req = el.requestFullscreen || el.webkitRequestFullscreen;
        if (req) req.call(el).catch(() => {});
    }
}

function _onFsChange() {
    const isFs = !!(document.fullscreenElement || document.webkitFullscreenElement);
    const btn = document.getElementById('btn-fs');
    if (btn) btn.classList.toggle('active', isFs);
    // Viewport height changes when browser chrome hides — refit joysticks
    if (_curView === 'landscape') requestAnimationFrame(sizeJoysticks);
}
document.addEventListener('fullscreenchange',        _onFsChange);
document.addEventListener('webkitfullscreenchange',  _onFsChange);

// ============================================================
//  Orientation
// ============================================================
let _curView = '';
let _gcMode = false;

function checkOrientation() {
    if (_gcMode) return;   // game-controller view owns the screen until GC is toggled off
    const land = window.innerWidth > window.innerHeight;
    const next = land ? 'landscape' : 'portrait';
    if (next === _curView) return;
    _curView = next;
    // Release pointer captures on whichever set of controls is going offscreen.
    // A captured-but-display:none canvas on Android Chrome absorbs all subsequent
    // touch events, making every button on the new view unresponsive.
    if (next === 'landscape') {
        if (pHslZoom)   pHslZoom.cancel();
        if (pHslSlider) pHslSlider.cancel();
        if (pJoy)       pJoy.cancel();
    } else {
        if (hslZoom)   hslZoom.cancel();
        if (hslSlider) hslSlider.cancel();
        if (joyRight)  joyRight.cancel();
    }
    _stopJogLoop();
    document.getElementById('portrait-view').classList.toggle('show', next==='portrait');
    document.getElementById('landscape-view').classList.toggle('show', next==='landscape');
    if (next === 'landscape') requestAnimationFrame(sizeJoysticks);
    else                      requestAnimationFrame(sizePortraitControls);
}

function sizePortraitControls() {
    const area = document.getElementById('p-ctrl-area');
    if (!area) return;
    const aw  = area.clientWidth - 16;   // subtract padding
    const slW = Math.max(80, Math.min(aw, 420));
    const slH = Math.max(36, Math.min(54, Math.floor(aw * 0.13)));
    if (pHslZoom)   pHslZoom.resize(slW, slH);
    if (pHslSlider) pHslSlider.resize(slW, slH);
    // Size the joystick to the height p-ctrl-area actually got (it now flexes)
    // minus the two sliders and the pan/tilt row — so on a short viewport it
    // shrinks instead of starving the position grid.  Still width-bounded.
    const slidersH = 2 * (slH + 18);                      // 2 slider groups (canvas+label+gap)
    const joyByH   = area.clientHeight - slidersH - 46;   // pan/tilt row + padding + gaps
    const joySz = Math.max(90, Math.min(aw, joyByH, 220));
    if (pJoy)       pJoy.resize(joySz);
}

function sizeJoysticks() {
    const joyArea = document.getElementById('joy-area');
    if (!joyArea) return;
    const pw  = (joyArea.clientWidth / 2) - 20;
    const slW = Math.max(80, Math.min(pw, 360));
    // Size arcs first so arc-row gains its natural height
    if (arcSZ) arcSZ.resize(slW);
    if (arcPT) arcPT.resize(slW);
    // Remaining height for controls (joy-area height minus arc row)
    const arcRow = document.getElementById('arc-row');
    const arcH   = arcRow ? arcRow.offsetHeight : 0;
    const ph  = Math.max(60, joyArea.clientHeight - arcH - 16);
    // Right joystick: square, bounded by both dimensions
    const sz  = Math.max(60, Math.min(ph, pw, 340));
    if (joyRight) joyRight.resize(sz);
    // Left sliders: wide (full left panel), height splits available space evenly
    const slH = Math.max(40, Math.min(Math.floor((ph - 16) / 2), 80));
    if (hslZoom)   hslZoom.resize(slW, slH);
    if (hslSlider) hslSlider.resize(slW, slH);
}

// ============================================================
//  Extended view  — state, page navigation, build + refresh
// ============================================================
let _extActive   = false;
let _extPage     = 'positions';
let extJoy       = null;
let extHslZoom   = null, extHslSlider = null;
// Positions page jog controls
let extPosJoy       = null;
let extPosHslZoom   = null, extPosHslSlider = null;
let extPosDialPT    = null, extPosDialSZ    = null;
let _extSetMode   = false;  // true: SET armed — cell click stores, cam click stores all
let _extClearMode = false;  // true: CLEAR armed — cell click clears, cam click clears all 10
let _extLaSetMode = false;  // true: selCam is LA, SET pressed, waiting for slot pick (step 3→4)
const _EXT_KEY   = 'cc_extended_view';

function _extAutoDetect() {
    return window.matchMedia('(min-width: 1024px)').matches && navigator.maxTouchPoints > 0;
}
function _loadExtPref() {
    try { const v = localStorage.getItem(_EXT_KEY); if (v !== null) return v === '1'; } catch(e) {}
    return _extAutoDetect();
}
function _saveExtPref(on) {
    try { localStorage.setItem(_EXT_KEY, on ? '1' : '0'); } catch(e) {}
}

function setGcView(on) {
    _gcMode = on;
    if (on) {
        // Drop any on-screen jog widgets — the physical pad handles motion now.
        [pHslZoom, pHslSlider, pJoy, hslZoom, hslSlider, joyRight]
            .forEach(w => w && w.cancel && w.cancel());
        _stopJogLoop();
        _curView = 'gc';
        document.getElementById('portrait-view').classList.remove('show');
        document.getElementById('landscape-view').classList.remove('show');
        document.getElementById('ext-view').classList.remove('show');
        document.getElementById('gc-view').classList.add('show');
        refreshCamBtns();
        refreshPosGrid();
        const cs = camSt[selCam];
        setPreset('pt', cs.activePtPreset, false);
        setPreset('sz', cs.activeSlPreset, false);
        setWsSt(!!(ws && ws.readyState === WebSocket.OPEN));
    } else {
        _curView = '';
        document.getElementById('gc-view').classList.remove('show');
        checkOrientation();
    }
}

function setExtView(on) {
    _extActive = on;
    _saveExtPref(on);
    if (on) {
        [pHslZoom, pHslSlider, pJoy, hslZoom, hslSlider, joyRight]
            .forEach(w => w && w.cancel && w.cancel());
        _stopJogLoop();
        _curView = 'extended';
        document.getElementById('portrait-view').classList.remove('show');
        document.getElementById('landscape-view').classList.remove('show');
        document.getElementById('ext-view').classList.add('show');
        showExtPage(_extPage);
    } else {
        [extJoy, extHslZoom, extHslSlider]
            .forEach(w => w && w.cancel && w.cancel());
        _stopJogLoop();
        _curView = '';
        document.getElementById('ext-view').classList.remove('show');
        checkOrientation();
    }
}

function showExtPage(page) {
    _extPage = page;
    document.querySelectorAll('.ext-tab').forEach(t =>
        t.classList.toggle('active', t.dataset.page === page));
    document.querySelectorAll('.ext-page').forEach(p =>
        p.classList.toggle('show', p.id === 'ext-page-' + page));
    if      (page === 'positions') { refreshExtPositions(); requestAnimationFrame(sizeExtPosControls); }
    else if (page === 'config') {
        // Request fresh CONFIG_REPORT from every connected mount so orientation
        // toggle chips reflect current hardware state (oriByte stays null until received).
        for (let i = 1; i <= NUM_MOUNTS; i++)
            if (camSt[i].connected) wsSend(mkGetConfig(i));
        refreshExtConfig();
    }
    else if (page === 'mounts') { reqMountTable(); refreshMounts(); }
}

// ---- Status tiles ----
function onExtPosClick(slot) {
    onPosClick(slot);
}

// ---- Positions overview page ----
function buildExtPositionsTable() {
    const table = document.getElementById('ext-pos-table');
    if (!table) return;
    table.innerHTML = '';
    for (let i = 1; i <= NUM_MOUNTS; i++) {
        const row = document.createElement('div');
        row.className = 'ext-pos-row';
        const lbl = document.createElement('div');
        lbl.className = 'ext-pos-cam-lbl';
        lbl.textContent = 'CAM ' + i;
        lbl.style.color = CAM_ACCENT[i];
        row.appendChild(lbl);
        const cells = document.createElement('div');
        cells.className = 'ext-pos-cells';
        for (let s = 0; s < NUM_SLOTS; s++) {
            const cell = document.createElement('div');
            cell.className = 'ext-pcell';
            cell.id = 'ext-pcell-' + i + '-' + s;
            cell.textContent = loadLabel(i, s) || (s + 1);
            cell.addEventListener('click', () => {
                const cs2 = camSt[i];
                if (!cs2.connected) return;            // camera offline
                // EDIT mode: rename label for this slot
                if (uiMode === 'edit') {
                    const cur = loadLabel(i, s) || String(s + 1);
                    const lbl = prompt('Label for slot ' + (s + 1) + ':', cur);
                    if (lbl === null) return;
                    if (lbl.trim() === '') clearLabel(i, s);
                    else saveLabel(i, s, lbl.trim());
                    refreshExtPositions();
                    return;
                }
                if (camIsLookAt(i) && s >= 8) {
                    // LA nav arrows — start look-at slider move to limit
                    const subj = cs2.activeLaSubject >= 0 ? cs2.activeLaSubject : 0;
                    const dir  = (s === 8) ? 0 : 1;   // 0=left/min, 1=right/max
                    wsSend(mkStartLookAtMove(i, subj, dir, cs2.activeSlPreset || 2));
                    return;
                }
                // LA SET mode: pick slot to kick off AddSubjectStart
                if (camIsLookAt(i) && i === selCam && _extLaSetMode) {
                    cs2.calibSubjectId = s;
                    wsSend(mkAddSubjectStart(i, s, ''));
                    _extLaSetMode = false;
                    refreshExtPositions();
                    return;
                }
                // SET armed: store current position to this slot, then disarm
                if (_extSetMode) {
                    wsSend(mkStorePos(i, s));
                    _extSetMode = false;
                    refreshExtPositions();
                    return;
                }
                // CLEAR armed: clear this slot
                if (_extClearMode) {
                    if (cs2.slotOccupied & (1 << s)) wsSend(mkClearPos(i, s));
                    refreshExtPositions();
                    return;
                }
                // Normal: recall
                if (!(cs2.slotOccupied & (1 << s))) return;
                if (camIsLookAt(i)) {
                    // LA mode: switch subject (pan/tilt tracks it), toggle deselect
                    if (cs2.activeLaSubject === s) {
                        cs2.activeLaSubject = -1;
                    } else {
                        cs2.activeLaSubject = s;
                        wsSend(mkSwitchSubject(i, s));
                    }
                    refreshExtPositions();
                } else {
                    wsSend(mkGotoSlot(i, s));
                }
            });
            cells.appendChild(cell);
        }
        row.appendChild(cells);
        // Per-row speed dials: SZ then PT — tap to cycle preset 1→2→3→4→1
        const dialWrap = document.createElement('div');
        dialWrap.className = 'ext-pos-dials';
        [['sz','SZ'],['pt','PT']].forEach(([which, dlbl]) => {
            const wrap = document.createElement('div');
            wrap.className = 'ext-pos-dial-wrap';
            wrap.style.cursor = 'pointer';
            const cv = document.createElement('canvas');
            cv.id = 'ext-pos-dial-' + which + '-' + i;
            cv.className = 'ext-pos-dial-cv';
            const lblEl = document.createElement('div');
            lblEl.className = 'ext-pos-dial-lbl';
            lblEl.textContent = dlbl;
            wrap.appendChild(cv); wrap.appendChild(lblEl);
            wrap.addEventListener('click', () => {
                const cs2 = camSt[i];
                if (!cs2.connected) return;
                if (which === 'sz' && !camHasSlider(i)) return;   // dormant — no slider
                const cur = (which === 'sz') ? cs2.activeSlPreset : cs2.activePtPreset;
                const next = (cur % 4) + 1;
                const grp = (which === 'sz') ? GROUP_SLIDER_ZOOM : GROUP_PAN_TILT;
                const p = new Uint8Array(2); p[0] = grp; p[1] = next;
                wsSend(buildPkt(i, CMD_SET_ACTIVE_PRESET, p));
                // Nothing moves here.  A dial shows what the MOUNT reports, not
                // what we just asked it for: STATUS carries the active presets,
                // and the handler for it redraws this page.  Updating locally
                // first made the dial answer for the mount — so a command lost
                // on the radio left the display showing a speed the rig was not
                // running at, which is the one thing a speed indicator must
                // never do.  Every other dial in this app already waited; this
                // page was the exception.
            });
            dialWrap.appendChild(wrap);
        });
        row.appendChild(dialWrap);
        table.appendChild(row);
    }
}

// Draw a compact preset arc dial onto a canvas element.
function _drawPosPresetDial(cv, preset, color) {
    const dpr = window.devicePixelRatio || 1;
    const sz  = 40;
    cv.width  = sz * dpr; cv.height = sz * dpr;
    const c = cv.getContext('2d');
    c.scale(dpr, dpr);
    const cx = sz/2, cy = sz/2;
    const R = sz*0.36, tw = sz*0.10, iw = sz*0.08;
    const startA = Math.PI*0.7, sweepA = Math.PI*1.6;
    const n = Math.max(0, Math.min(4, preset));
    c.clearRect(0, 0, sz, sz);
    c.beginPath(); c.arc(cx,cy,R,startA,startA+sweepA);
    c.strokeStyle='#1a1a1a'; c.lineWidth=tw; c.lineCap='round'; c.stroke();
    if (n > 0) {
        c.beginPath(); c.arc(cx,cy,R,startA,startA+(n/4)*sweepA);
        c.strokeStyle=color; c.lineWidth=iw; c.lineCap='round'; c.stroke();
    }
    c.fillStyle = n > 0 ? color : '#444';
    c.font = `bold ${Math.round(sz*0.30)}px system-ui,sans-serif`;
    c.textAlign='center'; c.textBaseline='middle';
    c.fillText(n > 0 ? n : '—', cx, cy);
}

function refreshExtPositions() {
    const setBtn   = document.getElementById('ext-pos-set-btn');
    const clearBtn = document.getElementById('ext-pos-clear-btn');
    const selCs    = selCam > 0 ? camSt[selCam] : null;
    const selLa    = selCam > 0 && camIsLookAt(selCam);
    const waitSetB = selLa && selCs && selCs.calibPhase === CP_WAIT_SET_B;
    const setArmed = _extSetMode || _extLaSetMode || waitSetB;
    if (setBtn) {
        setBtn.classList.toggle('armed', setArmed);
        setBtn.textContent = (waitSetB || _extLaSetMode) ? 'SET ✓' : 'SET';
    }
    if (clearBtn) clearBtn.classList.toggle('clear-armed', _extClearMode);

    // Position cells and per-row speed dials
    for (let i = 1; i <= NUM_MOUNTS; i++) {
        const cs    = camSt[i];
        const la    = camIsLookAt(i);
        const color = CAM_ACCENT[i];
        for (let s = 0; s < NUM_SLOTS; s++) {
            const cell = document.getElementById('ext-pcell-' + i + '-' + s);
            if (!cell) continue;
            if (la && s >= 8) {
                cell.textContent = s === 8 ? '◀' : '▶';
                const dir = s === 8 ? 'left' : 'right';
                const arr = cs.laArrow;
                let cls = 'ext-pcell la-arrow';
                if (arr === dir && _flashOn)   cls += ' la-moving';
                else if (arr === dir+'-done')  cls += ' la-done';
                cell.className = cls;
                continue;
            }
            cell.textContent = loadLabel(i, s) || (s + 1);
            const occ = !!(cs.slotOccupied & (1 << s));
            // LA subjects: green = currently tracked subject (activeLaSubject), not slotAt
            const at  = la ? (s === cs.activeLaSubject) : !!(cs.slotAt & (1 << s));
            const tgt = (cs.targetSlot !== 0xFF && cs.targetSlot === s);
            let cls = 'ext-pcell';
            // Structured exactly as the portrait grid: stay in the `moving`
            // branch for the whole flash and mark the off-beat with `flash-off`.
            //
            // Testing `tgt && _flashOn` instead dropped OUT of the branch on the
            // off-beat and fell through to `stored`, so a saved slot being moved
            // to flashed yellow-to-RED — the colour that means "occupied" —
            // rather than yellow-to-grey.  The cell was telling the truth twice
            // a second and contradicting itself.
            if (tgt) {
                cls += ' moving';
                if (!_flashOn) cls += ' flash-off';
            }
            else if (occ && at)     cls += ' at-pos';
            else if (occ)           cls += ' stored';
            if (uiMode === 'edit')  cls += ' edit-mode';
            cell.className = cls;
        }
        // Per-row speed dials
        const cvSZ = document.getElementById('ext-pos-dial-sz-' + i);
        const cvPT = document.getElementById('ext-pos-dial-pt-' + i);
        if (cvSZ) _drawPosPresetDial(cvSZ,
                      (cs.connected && camHasSlider(i)) ? cs.activeSlPreset : 0, color);
        if (cvPT) _drawPosPresetDial(cvPT, cs.connected ? cs.activePtPreset : 0, color);
    }
}

// ---- Config page ----
// Orientation bit definitions: [mask, short label, tooltip]
const _ORI_FLAGS = [
    [ORI_PAN_INV,    'Pan ⇄',    'Invert pan direction'],
    [ORI_TILT_INV,   'Tilt ⇅',   'Invert tilt direction'],
    [ORI_SLIDER_INV, 'Slider ⇄', 'Invert slider direction'],
    [ORI_ZOOM_INV,   'Zoom ⇄',   'Invert zoom direction'],
    [ORI_LANC_ZOOM,  'LANC',      'Use LANC for zoom control'],
    [ORI_HAS_SLIDER, 'Has Slider','Physical slider axis present'],
    [ORI_LOOK_AT,    'Look-At',   'Look-At tracking mode'],
];

function buildExtConfig() {
    const container = document.getElementById('ext-config-cams');
    if (!container) return;
    container.innerHTML = '';

    // Speed group descriptors: [key, label, group-const, count, spd-unit, acc-unit]
    const SPD_SECS = [
        ['pt', 'Pan / Tilt', GROUP_PAN_TILT,    4, 'deg/s',  'deg/s²'],
        ['sl', 'Slider',     GROUP_SLIDER_ZOOM,  4, 'mm/s',   'mm/s²' ],
        ['zm', 'Zoom',       GROUP_ZOOM,         1, 'spd',    'acc'   ],
    ];

    for (let i = 1; i <= NUM_MOUNTS; i++) {
        const accent = CAM_ACCENT[i];
        const card = document.createElement('div');
        card.className = 'ext-config-cam';
        card.style.borderLeftColor = accent;

        // ---- Header ----
        card.innerHTML = '<div class="ext-cfg-name">'
            + '<div class="ext-cfg-dot" id="ext-cfg-dot' + i + '"></div>'
            + '<span style="color:' + accent + '">CAM ' + i + '</span>'
            + '</div>';

        // ---- Action buttons (top) ----
        const actRow = document.createElement('div');
        actRow.className = 'ext-cfg-actions';
        [
            ['ext-cfg-home-sl-'+i, '⌂ Home Slider', 'home',
                () => { if (camSt[i].connected) wsSend(mkFindHome(i, AXIS_SLIDER)); }],
            ['ext-cfg-home-zm-'+i, '⌂ Home Zoom',   'home',
                () => { if (camSt[i].connected) wsSend(mkFindHome(i, AXIS_ZOOM)); }],
        ].forEach(([id, lbl, cls, fn]) => {
            const b = document.createElement('button');
            b.id = id; b.textContent = lbl;
            b.className = 'ext-cfg-act' + (cls ? ' '+cls : '');
            b.addEventListener('click', fn);
            actRow.appendChild(b);
        });
        const refGap = document.createElement('div');
        refGap.style.height = '6px';
        actRow.appendChild(refGap);
        const refBtn = document.createElement('button');
        refBtn.id = 'ext-cfg-ref-'+i; refBtn.textContent = '◎ Set Ref 0/0';
        refBtn.className = 'ext-cfg-act refbtn';
        refBtn.addEventListener('click', () => { if (camSt[i].connected) { selCam = i; wsSend(mkSetRef(i, 0)); } });
        actRow.appendChild(refBtn);
        card.appendChild(actRow);

        // ---- Speed preset sections ----
        SPD_SECS.forEach(([key, lbl, grp, count, uSpd, uAcc]) => {
            const sec = document.createElement('div');
            sec.className = 'ext-spd-section';
            sec.innerHTML = '<div class="ext-spd-hdr">' + lbl + '</div>';
            for (let p = 1; p <= count; p++) {
                const row = document.createElement('div');
                row.className = 'ext-spd-row';

                // Preset number label (blank for single-preset zoom)
                const numLbl = document.createElement('span');
                numLbl.className = 'ext-spd-lbl';
                numLbl.textContent = count > 1 ? p : '';
                row.appendChild(numLbl);

                // Speed input
                const sIn = document.createElement('input');
                sIn.type = 'number'; sIn.min = '1'; sIn.step = '1';
                sIn.className = 'ext-spd-inp';
                sIn.id = 'ext-spd-'+key+'-s-'+i+'-'+p;
                row.appendChild(sIn);

                const uSpdEl = document.createElement('span');
                uSpdEl.className = 'ext-spd-unit';
                uSpdEl.textContent = uSpd;
                row.appendChild(uSpdEl);

                // Accel input
                const aIn = document.createElement('input');
                aIn.type = 'number'; aIn.min = '1'; aIn.step = '1';
                aIn.className = 'ext-spd-inp';
                aIn.id = 'ext-spd-'+key+'-a-'+i+'-'+p;
                row.appendChild(aIn);

                const uAccEl = document.createElement('span');
                uAccEl.className = 'ext-spd-unit';
                uAccEl.textContent = uAcc;
                row.appendChild(uAccEl);

                // Send on change (blur keeps typing comfortable)
                const _i=i, _grp=grp, _p=p;
                const send = () => _extSendSpeed(_i, _grp, _p, sIn, aIn);
                sIn.addEventListener('change', send);
                aIn.addEventListener('change', send);

                sec.appendChild(row);
            }
            card.appendChild(sec);
        });

        // ---- Stall detection thresholds (under Zoom) ----
        const stallSec = document.createElement('div');
        stallSec.className = 'ext-spd-section';
        stallSec.innerHTML = '<div class="ext-spd-hdr">Stall Detection</div>';
        [
            ['Slider', AXIS_SLIDER, 'ext-thresh-sl-'+i],
            ['Zoom',   AXIS_ZOOM,   'ext-thresh-zm-'+i],
        ].forEach(([axLbl, axis, inpId]) => {
            const row = document.createElement('div');
            row.className = 'ext-spd-row';
            const lblEl = document.createElement('span');
            lblEl.className = 'ext-spd-unit';
            lblEl.style.cssText = 'width:34px;flex-shrink:0;color:var(--text);';
            lblEl.textContent = axLbl;
            row.appendChild(lblEl);
            const inp = document.createElement('input');
            inp.type = 'number'; inp.min = '0'; inp.max = '255'; inp.step = '1';
            inp.className = 'ext-spd-inp';
            inp.id = inpId;
            const _i = i, _axis = axis;
            inp.addEventListener('change', () => {
                const v = Math.max(0, Math.min(255, parseInt(inp.value) || 0));
                inp.value = v;
                if (camSt[_i].connected) {
                    if (_axis === AXIS_SLIDER) camSt[_i].slThresh = v;
                    else                       camSt[_i].zmThresh = v;
                    wsSend(mkSetStallThreshold(_i, _axis, v));
                }
            });
            row.appendChild(inp);
            const unit = document.createElement('span');
            unit.className = 'ext-spd-unit';
            unit.textContent = '0–255';
            row.appendChild(unit);
            stallSec.appendChild(row);
        });
        card.appendChild(stallSec);

        // ---- Rail geometry (reported by the mount, set in the PC app) ----
        // Read-only here on purpose: the tilt is a physical property of how the
        // rig is rigged, not a per-show setting, so it is set once in the PC app
        // and mirrored everywhere else.  Shown so that a wrong value can be spotted
        // from the phone at the rig, where the PC app may not be to hand.
        const geoSec = document.createElement('div');
        geoSec.className = 'ext-spd-section';
        geoSec.id = 'ext-cfg-geo-sec-' + i;
        geoSec.innerHTML = '<div class="ext-spd-hdr">Rail Geometry</div>';
        {
            const row = document.createElement('div');
            row.className = 'ext-spd-row';
            const lblEl = document.createElement('span');
            lblEl.className = 'ext-spd-unit';
            lblEl.style.cssText = 'width:64px;flex-shrink:0;color:var(--text);';
            lblEl.textContent = 'Tilt';
            row.appendChild(lblEl);
            const inp = document.createElement('input');
            inp.type = 'number'; inp.min = '-90'; inp.max = '90'; inp.step = '0.5';
            inp.className = 'ext-spd-inp';
            inp.id = 'ext-tilt-' + i;
            const _i2 = i;
            // Same rule as every other box on this page: it goes to the mount
            // when the box is LEFT, not per keystroke.  'change' is blur or
            // Enter; typing "-21" would otherwise send "-" then "-2" on the way.
            inp.addEventListener('change', () => {
                let v = parseFloat(inp.value);
                if (!isFinite(v)) v = 0;
                v = Math.max(-90, Math.min(90, Math.round(v * 2) / 2));
                inp.value = v.toFixed(1);
                const cs = camSt[_i2];
                if (!cs.connected || cs.oriByte === null) return;
                cs.sliderTilt = v;
                wsSend(mkSetOrientation(_i2, cs.oriByte, v));
            });
            row.appendChild(inp);
                const hint = document.createElement('span');
            hint.className = 'ext-spd-unit';
            hint.textContent = 'deg, 0 = horizontal';
            row.appendChild(hint);
            geoSec.appendChild(row);
        }
        card.appendChild(geoSec);

        // ---- Orientation flags (one per line) ----
        const oriSec = document.createElement('div');
        oriSec.className = 'ext-ori-section';
        oriSec.innerHTML = '<div class="ext-ori-hdr">Mount Options</div>';
        oriSec.id = 'ext-cfg-ori-sec-' + i;
        _ORI_FLAGS.forEach(([mask, lbl, title]) => {
            const flagRow = document.createElement('div');
            flagRow.className = 'ext-ori-flag-row';
            const lblEl = document.createElement('span');
            lblEl.className = 'ext-ori-flag-lbl';
            lblEl.textContent = lbl;
            lblEl.title = title;
            const btn = document.createElement('button');
            btn.className = 'ext-ori-btn';
            btn.dataset.mask = mask;
            const _i = i;
            btn.addEventListener('click', () => _extToggleOri(_i, mask));
            flagRow.appendChild(lblEl);
            flagRow.appendChild(btn);
            oriSec.appendChild(flagRow);
        });
        card.appendChild(oriSec);

        // ---- Find Limits (bottom) — measures full travel of each axis.
        // The hub + a phone is a complete rig with no PC and no console, so this
        // has to live here too; without it a phone-only setup can't set up a
        // mount from scratch.
        const limSec = document.createElement('div');
        limSec.className = 'ext-cfg-limits';
        limSec.innerHTML = '<div class="ext-spd-hdr">Find Limits</div>';
        const limRow = document.createElement('div');
        limRow.className = 'ext-cfg-limrow';
        [
            ['ext-cfg-lim-sl-'+i, 'Slider', AXIS_SLIDER],
            ['ext-cfg-lim-zm-'+i, 'Zoom',   AXIS_ZOOM],
        ].forEach(([id, lbl, axis]) => {
            const b = document.createElement('button');
            b.id = id; b.textContent = lbl;
            b.className = 'ext-cfg-act limbtn';
            const _i = i, _axis = axis;
            b.addEventListener('click', () => {
                if (!camSt[_i].connected) return;
                if (!confirm('Find Limits drives ' + lbl.toLowerCase() +
                             ' to BOTH ends of its travel on CAM ' + _i +
                             '.\n\nMake sure the mount is clear to move.')) return;
                wsSend(mkFindLimits(_i, _axis));
            });
            limRow.appendChild(b);
        });
        limSec.appendChild(limRow);
        card.appendChild(limSec);

        container.appendChild(card);
    }
}

function _extSendSpeed(cam, group, preset, sEl, aEl) {
    if (!camSt[cam].connected) return;
    const spd = Math.max(1, Math.round(parseFloat(sEl.value) || 1));
    const acc = Math.max(1, Math.round(parseFloat(aEl.value) || 1));
    sEl.value = spd;
    aEl.value = acc;
    wsSend(mkSetSpeedPreset(cam, group, preset, spd, acc));
    // Optimistically update local cache so refreshExtConfig() reflects it immediately
    const cs = camSt[cam];
    if (group === GROUP_PAN_TILT   && cs.ptPresets) cs.ptPresets[preset-1] = {spd, acc};
    if (group === GROUP_SLIDER_ZOOM && cs.slPresets) cs.slPresets[preset-1] = {spd, acc};
    if (group === GROUP_ZOOM        && cs.zmPreset)  cs.zmPreset             = {spd, acc};
}

function _extToggleOri(cam, mask) {
    const cs = camSt[cam];
    if (!cs.connected || cs.oriByte === null) return;
    cs.oriByte ^= mask;                              // flip bit locally
    wsSend(mkSetOrientation(cam, cs.oriByte));       // send to mount
    // Teensy echoes a CONFIG_REPORT which will update oriByte authoritatively
    refreshExtConfig();
}

function refreshExtConfig() {
    for (let i = 1; i <= NUM_MOUNTS; i++) {
        const cs        = camSt[i];
        const connected = cs.connected;
        const oriKnown  = cs.oriByte !== null;

        // Connection dot
        const dot = document.getElementById('ext-cfg-dot' + i);
        if (dot) dot.className = 'ext-cfg-dot' + (connected ? ' on' : '');

        // Action buttons
        ['ext-cfg-home-sl-'+i, 'ext-cfg-home-zm-'+i, 'ext-cfg-ref-'+i,
         'ext-cfg-lim-sl-'+i, 'ext-cfg-lim-zm-'+i].forEach(id => {
            const b = document.getElementById(id);
            if (b) b.disabled = !connected;
        });

        // Speed inputs — populate from camSt when not actively editing
        const _setInp = (id, val) => {
            const el = document.getElementById(id);
            if (!el) return;
            el.disabled = !connected;
            // Don't overwrite value while the user is typing in that field
            if (val !== undefined && document.activeElement !== el) el.value = val;
        };
        for (let p = 1; p <= 4; p++) {
            const ptP = cs.ptPresets && cs.ptPresets[p-1];
            _setInp('ext-spd-pt-s-'+i+'-'+p, ptP ? ptP.spd : undefined);
            _setInp('ext-spd-pt-a-'+i+'-'+p, ptP ? ptP.acc : undefined);
            const slP = cs.slPresets && cs.slPresets[p-1];
            _setInp('ext-spd-sl-s-'+i+'-'+p, slP ? slP.spd : undefined);
            _setInp('ext-spd-sl-a-'+i+'-'+p, slP ? slP.acc : undefined);
        }
        _setInp('ext-spd-zm-s-'+i+'-1', cs.zmPreset ? cs.zmPreset.spd : undefined);
        _setInp('ext-spd-zm-a-'+i+'-1', cs.zmPreset ? cs.zmPreset.acc : undefined);

        // Stall threshold inputs
        _setInp('ext-thresh-sl-'+i, cs.slThresh !== null ? cs.slThresh : undefined);
        _setInp('ext-thresh-zm-'+i, cs.zmThresh !== null ? cs.zmThresh : undefined);

        // Rail geometry — only meaningful on a mount that has a slider
        const geo = document.getElementById('ext-cfg-geo-sec-' + i);
        if (geo) geo.style.display = (oriKnown && (cs.oriByte & 0x04)) ? '' : 'none';
        _setInp('ext-tilt-' + i,
                 (cs.sliderTilt === null) ? undefined : cs.sliderTilt.toFixed(1));

        // Orientation toggles — disabled until CONFIG_REPORT received
        document.querySelectorAll('#ext-cfg-ori-sec-' + i + ' .ext-ori-btn').forEach(btn => {
            const mask = parseInt(btn.dataset.mask);
            btn.disabled = !oriKnown;
            const on = oriKnown && !!(cs.oriByte & mask);
            btn.classList.toggle('on', on);
        });
    }
}

// ---- Refresh all + size controls ----
function refreshExtAll() {
    if (!_extActive) return;
    const extDot = document.getElementById('ext-ws-dot');
    if (extDot) extDot.className =
        'ws-dot' + ((ws && ws.readyState === WebSocket.OPEN) ? ' on' : '');
    if      (_extPage === 'positions') refreshExtPositions();
    else if (_extPage === 'config')    refreshExtConfig();
}

function sizeExtPosControls() {
    const sliders = document.getElementById('ext-pos-sliders');
    if (!sliders || !sliders.offsetParent) return;
    const strip   = document.querySelector('.ext-pos-ctrl-strip');
    const joyWrap = document.getElementById('ext-pos-joy-wrap');
    const stripH  = strip ? strip.clientHeight : 240;
    // Joystick size drives the column width
    const joySz   = Math.max(80, Math.min(stripH - 30, 300));
    const colW    = joySz + 16; // joy size + horizontal padding
    sliders.style.width = colW + 'px';
    if (joyWrap) joyWrap.style.width = colW + 'px';
    if (extPosJoy) extPosJoy.resize(joySz);
    // Sliders fill the column width
    const slW = colW - 16;
    const slH = 40;
    if (extPosHslSlider) extPosHslSlider.resize(slW, slH);
    if (extPosHslZoom)   extPosHslZoom.resize(slW, slH);
}

function sizeExtControls() {
    const right = document.querySelector('.ext-detail-right');
    if (!right || !right.offsetParent) return;
    const w = right.clientWidth - 20;
    const h = right.clientHeight;
    const slW = Math.max(80, Math.min(w, 400));
    const slH = Math.max(36, Math.min(54, 50));
    const joySz = Math.max(80, Math.min(w, h - 290, 300));
    if (extHslZoom)   extHslZoom.resize(slW, slH);
    if (extHslSlider) extHslSlider.resize(slW, slH);
    if (extJoy)       extJoy.resize(joySz);
}

// ============================================================
//  Init
// ============================================================
let joyRight = null;
let hslSlider = null, hslZoom = null;
let arcPT = null, arcSZ = null;
let pJoy = null;
let pHslSlider = null, pHslZoom = null;

window.addEventListener('DOMContentLoaded', () => {
    makeCamBtns('p-cam-bar');
    makeCamBtns('l-cam-bar');
    makeCamBtns('gc-cam-bar');
    buildPosGrid();
    refreshPosGrid();
    refreshCamBtns();

    document.getElementById('btn-set').addEventListener('click', doSetClick);
    document.getElementById('gc-set').addEventListener('click', doSetClick);
    document.getElementById('btn-edit').addEventListener('click',
        () => setUiMode(uiMode === 'edit' ? 'move' : 'edit'));
    document.getElementById('btn-edit-ext').addEventListener('click',
        () => setUiMode(uiMode === 'edit' ? 'move' : 'edit'));
    document.getElementById('btn-clear-p').addEventListener('click',
        () => setUiMode(uiMode === 'clear' ? 'move' : 'clear'));
    document.getElementById('btn-estop-p').addEventListener('click',
        () => wsSend(mkEStop(0x00)));
    document.getElementById('btn-estop-l').addEventListener('click',
        () => wsSend(mkEStop(0x00)));
    // ---- Game-controller view ----
    document.getElementById('gc-estop').addEventListener('click',
        () => wsSend(mkEStop(0x00)));
    document.getElementById('gc-clear').addEventListener('click',
        () => setUiMode(uiMode === 'clear' ? 'move' : 'clear'));
    document.getElementById('btn-to-gc-l').addEventListener('click',
        () => setGcView(true));
    document.getElementById('gc-exit').addEventListener('click',
        () => setGcView(false));

    document.getElementById('calib-cancel-btn').addEventListener('click', () => {
        const cs = camSt[selCam];
        wsSend(mkAddSubjectAbort(selCam));
        cs.calibPhase     = 0;
        cs.calibSubjectId = -1;
        hideCalibSheet();
        refreshPosGrid();   // disarms SET button
    });

    // Fullscreen button — hide on platforms that don't support the API (iOS Safari)
    const fsBtn = document.getElementById('btn-fs');
    if (document.documentElement.requestFullscreen || document.documentElement.webkitRequestFullscreen) {
        fsBtn.addEventListener('click', toggleFullscreen);
    } else {
        fsBtn.style.display = 'none';
    }

    // Landscape arc indicators — must be created before setPreset() is called below
    arcPT = new ArcPreset('pt-arc', 'pt', true);   // right panel: fills right→left
    arcSZ = new ArcPreset('sz-arc', 'sz', false);  // left panel:  fills left→right
    // Portrait speed dials
    dialPT = new SpeedDial('dial-pt', 'pt');
    dialSZ = new SpeedDial('dial-sz', 'sz');
    gcDialPT = new SpeedDial('gc-dial-pt', 'pt');
    gcDialSZ = new SpeedDial('gc-dial-sz', 'sz');
    setPreset('pt', 2, false);
    setPreset('sz', 2, false);

    function _anyActive() {
        return (hslZoom && hslZoom.active) ||
               (hslSlider && hslSlider.active) ||
               (joyRight && joyRight.active) ||
               (pHslZoom && pHslZoom.active) ||
               (pHslSlider && pHslSlider.active) ||
               (pJoy && pJoy.active) ||
               (extHslZoom && extHslZoom.active) ||
               (extHslSlider && extHslSlider.active) ||
               (extJoy && extJoy.active) ||
               (extPosHslZoom && extPosHslZoom.active) ||
               (extPosHslSlider && extPosHslSlider.active) ||
               (extPosJoy && extPosJoy.active);
    }
    hslZoom   = new HSlider('hsl-zoom',   v => {
        _jl.y = v;  // right = zoom in = positive
        if (_curView !== 'landscape') return;
        if (_anyActive()) _startJogLoop(); else _stopJogLoop();
    });
    hslSlider = new HSlider('hsl-slider', v => {
        _jl.x = v;  // right = slider right = positive
        if (_curView !== 'landscape') return;
        if (_anyActive()) _startJogLoop(); else _stopJogLoop();
    });
    joyRight = new Joystick('joy-right', (x,y) => {
        _jr.x=x; _jr.y=y;
        if (_curView !== 'landscape') return;
        if (_anyActive()) _startJogLoop(); else _stopJogLoop();
    });
    pHslZoom   = new HSlider('p-hsl-zoom', v => {
        _jl.y = v;
        if (_curView !== 'portrait') return;
        if (_anyActive()) _startJogLoop(); else _stopJogLoop();
    });
    pHslSlider = new HSlider('p-hsl-slider', v => {
        _jl.x = v;
        if (_curView !== 'portrait') return;
        if (_anyActive()) _startJogLoop(); else _stopJogLoop();
    });
    pJoy = new Joystick('p-joy', (x,y) => {
        _jr.x=x; _jr.y=y;
        if (_curView !== 'portrait') return;
        if (_anyActive()) _startJogLoop(); else _stopJogLoop();
    });

    checkOrientation();
    window.addEventListener('resize', () => {
        if (_extActive) {
            if (_extPage === 'positions') sizeExtPosControls();
            else sizeExtControls();
        } else if (_gcMode) {
            // GC view is CSS-driven (fixed-size dials, CSS grid) — nothing to size.
        } else {
            checkOrientation();
            if (_curView === 'landscape') sizeJoysticks();
            else                          sizePortraitControls();
        }
    });

    // ---- Extended view: build DOM structures ----
    makeCamBtns('ext-pos-cam-bar');
    buildExtPositionsTable();
    buildExtConfig();

    // Positions-page speed dials
    extPosDialPT = new SpeedDial('ext-pos-dial-pt', 'pt');
    extPosDialSZ = new SpeedDial('ext-pos-dial-sz', 'sz');

    // Positions-page joystick & sliders
    extPosHslZoom = new HSlider('ext-pos-hsl-zoom', v => {
        _jl.y = v;
        if (_curView !== 'extended') return;
        if (_anyActive()) _startJogLoop(); else _stopJogLoop();
    });
    extPosHslSlider = new HSlider('ext-pos-hsl-slider', v => {
        _jl.x = v;
        if (_curView !== 'extended') return;
        if (_anyActive()) _startJogLoop(); else _stopJogLoop();
    });
    extPosJoy = new Joystick('ext-pos-joy', (x, y) => {
        _jr.x = x; _jr.y = y;
        if (_curView !== 'extended') return;
        if (_anyActive()) _startJogLoop(); else _stopJogLoop();
    });

    // Positions-page SET / CLEAR buttons — operate on all selected cam+slot pairs
    document.getElementById('ext-pos-set-btn').addEventListener('click', () => {
        const cs = camSt[selCam];
        const la = camIsLookAt(selCam);
        _extClearMode = false;  // SET disarms CLEAR
        if (la && cs.calibPhase === CP_WAIT_SET_B) {
            wsSend(mkAddSubjectSetB(selCam));
            _extLaSetMode = false;
        } else if (la && cs.connected) {
            _extLaSetMode = !_extLaSetMode;
            _extSetMode = false;
        } else {
            _extSetMode = !_extSetMode;
            _extLaSetMode = false;
        }
        refreshExtPositions();
    });
    document.getElementById('ext-pos-clear-btn').addEventListener('click', () => {
        _extSetMode = false;    // CLEAR disarms SET
        _extLaSetMode = false;
        _extClearMode = !_extClearMode;
        refreshExtPositions();
    });

    // ---- Extended view: button wiring ----
    document.getElementById('btn-ext-to-mobile').addEventListener('click',
        () => setExtView(false));
    document.getElementById('btn-to-ext-p').addEventListener('click',
        () => setExtView(true));
    document.getElementById('btn-to-ext-l').addEventListener('click',
        () => setExtView(true));
    document.getElementById('btn-estop-ext').addEventListener('click',
        () => wsSend(mkEStop(0x00)));
    document.querySelectorAll('.ext-tab').forEach(tab =>
        tab.addEventListener('click', () => showExtPage(tab.dataset.page)));
    document.getElementById('pair-replace-btn').addEventListener('click',
        () => { if (pairConflict) pairDecide(pairConflict.cam, 1); });
    document.getElementById('pair-ignore-btn').addEventListener('click',
        () => { if (pairConflict) pairDecide(pairConflict.cam, 0); });

    // ---- Extended view: auto-activate if pref says so ----
    // Small delay ensures checkOrientation() runs first and portrait/landscape
    // views are fully initialised before we hide them.
    if (_loadExtPref()) setTimeout(() => setExtView(true), 60);

    wsConnect();
});
</script>
</body>
</html>
)rawhtml";
