#!/usr/bin/env python3
"""Regenerate the manual's Bill of materials page from the spreadsheet.

    python3 tools/gen_bom.py

Reads  docs/assets/BoM.xlsx
Writes docs/manual/bom.html   (only the two generated blocks — the page's own
                               prose, styling and callouts are left alone)

Workbook layout it expects
--------------------------
Parts sheet
    A = Item, B = Qty, C = Buy (the cell carries the hyperlink; its text is the
    vendor label).  A row with only A filled is a section banner.

Nuts&Bolts-List sheet
    B  = fastener name, C..  = one column per assembly (names in row 2),
    plus columns headed "Total" and "Link" which are not assemblies:
    the page computes its own total, and renders the link as its own cell.
    A blank/"-" fastener cell is a group separator.

Editing note
------------
Rebuild sheets wholesale rather than using openpyxl's insert_rows()/delete_rows():
those do NOT move hyperlinks, which silently reattaches every link below the
edit to the wrong item.
"""
import html
import pathlib
import re
from urllib.parse import urlparse

import openpyxl

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "assets" / "BoM.xlsx"
OUT = ROOT / "docs" / "manual" / "bom.html"

VENDORS = {
    "amazon.co.uk": "Amazon", "amazon.com": "Amazon", "ooznest.co.uk": "Ooznest",
    "thomann.co.uk": "Thomann", "thomann.de": "Thomann",
    "simplybearings.co.uk": "Simply Bearings",
}
E = html.escape


def vendor(url, fallback=""):
    host = (urlparse(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return VENDORS.get(host, fallback or host or "Buy")


def read_parts(ws):
    """-> [{title, items:[(name, qty, url, label)]}]"""
    groups, cur = [], None
    for r in range(2, ws.max_row + 1):
        a, b, c = (ws.cell(r, i).value for i in (1, 2, 3))
        a = str(a).strip() if a is not None else ""
        if not a:
            continue
        qty = "" if b is None else str(b).strip()
        lbl = "" if c is None else str(c).strip()
        if not qty and not lbl:                     # section banner
            cur = {"title": a, "items": []}
            groups.append(cur)
            continue
        if cur is None:
            cur = {"title": "Parts", "items": []}
            groups.append(cur)
        link = ws.cell(r, 3).hyperlink.target if ws.cell(r, 3).hyperlink else ""
        cur["items"].append((a, qty, link, lbl))
    return [g for g in groups if g["items"]]


def read_matrix(ws):
    """-> (assemblies, rows) where a row is ('sep',) or (name, qtys, url, label)"""
    HR = 2
    cols = [c for c in range(3, ws.max_column + 1)
            if ws.cell(HR, c).value and str(ws.cell(HR, c).value).strip()]
    assemblies = [(c, str(ws.cell(HR, c).value).strip()) for c in cols
                  if str(ws.cell(HR, c).value).strip().lower() not in ("total", "link")]
    link_col = next((c for c in cols
                     if str(ws.cell(HR, c).value).strip().lower() == "link"), None)
    rows = []
    for r in range(HR + 1, ws.max_row + 1):
        name = ws.cell(r, 2).value
        name = str(name).strip() if name is not None else ""
        if name in ("", "-"):
            if rows and rows[-1] != ("sep",):
                rows.append(("sep",))
            continue
        qtys = []
        for c, _ in assemblies:
            v = ws.cell(r, c).value
            qtys.append("" if v is None or str(v).strip() == "" else str(v).strip())
        cell = ws.cell(r, link_col) if link_col else None
        url = cell.hyperlink.target if (cell is not None and cell.hyperlink) else ""
        lbl = str(cell.value).strip() if (cell is not None and cell.value) else ""
        rows.append((name, qtys, url, lbl))
    while rows and rows[0] == ("sep",):
        rows.pop(0)
    while rows and rows[-1] == ("sep",):
        rows.pop()
    return assemblies, rows


def buy_cell(url, lbl):
    if not url:
        return '<span class="dim">&#8212;</span>'
    return (f'<a href="{E(url)}" target="_blank" rel="noopener">'
            f'{E(vendor(url, lbl))} &#8599;</a>')


def render_parts(groups):
    out = []
    for g in groups:
        out.append(f"    <h3>{E(g['title'])}</h3>")
        rows = [f'      <tr><td>{E(n)}</td>'
                f'<td class="num">{E(q) if q else chr(38) + "#8212;"}</td>'
                f'<td>{buy_cell(u, l)}</td></tr>'
                for n, q, u, l in g["items"]]
        out.append('    <table class="bom">\n'
                   '      <tr><th>Item</th><th class="num">Qty</th><th>Buy</th></tr>\n'
                   + "\n".join(rows) + "\n    </table>")
    return "\n".join(out)


def render_matrix(assemblies, rows):
    def total(qtys):
        t = 0
        for q in qtys:
            try:
                t += int(float(q))
            except (ValueError, TypeError):
                pass
        return t

    head = "".join(f"<th>{E(a)}</th>" for _, a in assemblies)
    body = []
    for row in rows:
        if row == ("sep",):
            body.append(f'        <tr class="msep">'
                        f'<td colspan="{len(assemblies) + 3}"></td></tr>')
            continue
        name, qtys, url, lbl = row
        cells = "".join(f"<td>{E(q)}</td>" for q in qtys)
        body.append(f'        <tr><th class="rowh">{E(name)}</th>{cells}'
                    f'<td class="tot">{total(qtys)}</td>'
                    f'<td class="lnk">{buy_cell(url, lbl)}</td></tr>')
    return ('    <div class="matrix-wrap">\n      <table class="matrix">\n'
            f'        <thead><tr><th class="rowh">Fastener</th>{head}'
            '<th class="tot">Total</th><th class="lnk">Buy</th></tr></thead>\n'
            '        <tbody>\n' + "\n".join(body) + '\n        </tbody>\n'
            '      </table>\n    </div>')


def main():
    wb = openpyxl.load_workbook(SRC, data_only=True)
    groups = read_parts(wb["Parts"])
    assemblies, rows = read_matrix(wb["Nuts&Bolts-List"])
    n_parts = sum(len(g["items"]) for g in groups)

    page = OUT.read_text()
    page = re.sub(r'(<h2>Parts</h2>.*?</p>\n)(.*?)(\n\n    <h2>Fasteners</h2>)',
                  lambda m: m.group(1) + render_parts(groups) + m.group(3),
                  page, flags=re.S)
    page = re.sub(r'(<h2>Fasteners</h2>.*?</p>\n)(.*?)(\n\n    <div class="tip")',
                  lambda m: m.group(1) + render_matrix(assemblies, rows) + m.group(3),
                  page, flags=re.S)
    page = re.sub(r'(<p>)\d+( line items\.)',
                  lambda m: m.group(1) + str(n_parts) + m.group(2), page)
    OUT.write_text(page)

    n_rows = sum(1 for r in rows if r != ("sep",))
    linked = sum(1 for r in rows if r != ("sep",) and r[2])
    print(f"wrote {OUT.relative_to(ROOT)}")
    print(f"  parts     : {n_parts} items in {len(groups)} groups")
    print(f"  fasteners : {n_rows} rows x {len(assemblies)} assemblies "
          f"({linked} with a buy link)")


if __name__ == "__main__":
    main()
