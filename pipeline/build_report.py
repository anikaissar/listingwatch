#!/usr/bin/env python3
"""Builds qa_findings_flipkart_vs_d2c.xlsx from qa_diff_flipkart_vs_d2c_v4.csv + missing_d2c_reference.csv."""
import csv
import datetime

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.formatting.rule import ColorScaleRule, CellIsRule
from openpyxl.utils import get_column_letter

FONT = "Arial"
HEADER_FILL = PatternFill("solid", fgColor="1F4E5F")
HEADER_FONT = Font(name=FONT, bold=True, color="FFFFFF")
BROKEN_FILL = PatternFill("solid", fgColor="E8E8E8")
BAD_FILL = PatternFill("solid", fgColor="F8D7DA")
TIER_FILLS = {
    "P1": PatternFill("solid", fgColor="E06666"),
    "P2": PatternFill("solid", fgColor="F6B26B"),
    "P3": PatternFill("solid", fgColor="FFD966"),
    "P4": PatternFill("solid", fgColor="CFE2F3"),
    "P5": PatternFill("solid", fgColor="D9D9D9"),
}
NOTE_FONT = Font(name=FONT, italic=True, size=10, color="555555")
BOLD = Font(name=FONT, bold=True)
TITLE_FONT = Font(name=FONT, bold=True, size=14)
SECTION_FONT = Font(name=FONT, bold=True, size=12, color="1F4E5F")

DIFF_COLS = [
    "nh_sku", "fsn", "pdp_availability", "flipkart_page_broken",
    "flipkart_title", "d2c_title",
    "title_seq_ratio", "title_d2c_term_coverage", "title_plausible",
    "desc_seq_ratio", "desc_token_jaccard",
    "ingredients_coverage",
    "shelf_life_status", "d2c_shelf_life", "flipkart_max_shelf_life",
    "flipkart_extraction_source",
    "flipkart_image_count", "d2c_image_count", "image_count_flag",
    "visual_best_similarity", "visual_avg_similarity",
]
BOOL_COLS = {"flipkart_page_broken", "title_plausible"}
NUM_COLS = {
    "title_seq_ratio", "title_d2c_term_coverage", "desc_seq_ratio",
    "desc_token_jaccard", "ingredients_coverage", "flipkart_image_count",
    "d2c_image_count", "visual_best_similarity", "visual_avg_similarity",
}


def load_diff_rows(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            row = {}
            for c in DIFF_COLS:
                v = r.get(c, "")
                if c in BOOL_COLS:
                    row[c] = True if v == "True" else (False if v == "False" else None)
                elif c in NUM_COLS:
                    row[c] = float(v) if v not in ("", None) else None
                else:
                    row[c] = v
            rows.append(row)
    return rows


def style_header(ws, ncols, row=1):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = 30


def autosize(ws, widths):
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def build():
    diff_rows = load_diff_rows("qa_diff_flipkart_vs_d2c_v4.csv")
    missing_rows = list(csv.DictReader(open("missing_d2c_reference.csv", newline="", encoding="utf-8")))

    wb = Workbook()

    # ---------------- Sheet 1: Diff Data ----------------
    ws = wb.active
    ws.title = "Diff Data"
    ws.append(DIFF_COLS)
    style_header(ws, len(DIFF_COLS))
    for r in diff_rows:
        ws.append([r[c] for c in DIFF_COLS])
    n = len(diff_rows)
    last_row = n + 1
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(DIFF_COLS))}{last_row}"
    for row in ws.iter_rows(min_row=2, max_row=last_row, max_col=len(DIFF_COLS)):
        for cell in row:
            cell.font = Font(name=FONT)

    col = {name: i + 1 for i, name in enumerate(DIFF_COLS)}

    # broken rows -> light gray, whole row (nothing real to compare)
    broken_col = get_column_letter(col["flipkart_page_broken"])
    ws.conditional_formatting.add(
        f"A2:{get_column_letter(len(DIFF_COLS))}{last_row}",
        CellIsRule(operator="equal", formula=[f"${broken_col}2=TRUE"], fill=BROKEN_FILL),
    )
    # implausible title -> flag the title_plausible cell
    tp_col = get_column_letter(col["title_plausible"])
    ws.conditional_formatting.add(
        f"{tp_col}2:{tp_col}{last_row}",
        CellIsRule(operator="equal", formula=["FALSE"], fill=BAD_FILL),
    )
    # shelf life mismatch -> flag that cell
    sl_col = get_column_letter(col["shelf_life_status"])
    ws.conditional_formatting.add(
        f"{sl_col}2:{sl_col}{last_row}",
        CellIsRule(operator="equal", formula=['"mismatch"'], fill=BAD_FILL),
    )
    # visual similarity -> red/yellow/green color scale
    vb_col = get_column_letter(col["visual_best_similarity"])
    ws.conditional_formatting.add(
        f"{vb_col}2:{vb_col}{last_row}",
        ColorScaleRule(
            start_type="num", start_value=0.45, start_color="F8696B",
            mid_type="num", mid_value=0.72, mid_color="FFEB84",
            end_type="num", end_value=1.0, end_color="63BE7B",
        ),
    )
    autosize(ws, [16, 18, 12, 12, 42, 42, 10, 10, 10, 10, 10, 10, 14, 20, 20, 12, 8, 8, 15, 10, 10])

    diff_range = f"'Diff Data'!"

    # ---------------- Sheet 2: Summary ----------------
    ws2 = wb.create_sheet("Summary")
    ws2["A1"] = "Flipkart vs D2C (nathabit.in) Listing QA — Summary"
    ws2["A1"].font = TITLE_FONT
    ws2["A2"] = f"Generated {datetime.date.today().isoformat()}. All counts below are live formulas referencing the 'Diff Data' sheet — they recompute if that sheet's data is replaced with a fresh export."
    ws2["A2"].font = NOTE_FONT
    ws2.merge_cells("A2:F2")
    ws2["A2"].alignment = Alignment(wrap_text=True)

    r = 4

    def section(title):
        nonlocal r
        ws2.cell(row=r, column=1, value=title).font = SECTION_FONT
        r += 1

    def stat(label, formula, note=None):
        nonlocal r
        ws2.cell(row=r, column=1, value=label).font = Font(name=FONT)
        c = ws2.cell(row=r, column=2, value=formula)
        c.font = BOLD
        if note:
            nc = ws2.cell(row=r, column=3, value=note)
            nc.font = NOTE_FONT
        r += 1

    # Derived from `col` (the DIFF_COLS name->index map above), not hardcoded
    # letters -- adding/reordering a column in DIFF_COLS shifts every
    # downstream column, and this project has already been bitten once by
    # that exact class of silent shift (see the flipkart_max_shelf_life
    # column added between shelf_life fields and flipkart_image_count).
    def _rng(name):
        c = get_column_letter(col[name])
        return f"{c}2:{c}{last_row}"

    dr = _rng("flipkart_page_broken")
    tr = _rng("title_plausible")
    kr = _rng("desc_token_jaccard")
    lr = _rng("ingredients_coverage")
    sr = _rng("shelf_life_status")
    pr = _rng("flipkart_image_count")
    vr = _rng("visual_best_similarity")

    section("Coverage")
    stat("Flipkart listings matched to a D2C SKU", f"=COUNTA({diff_range}A2:A{last_row})")
    stat("Flipkart listings with NO D2C reference yet", "=COUNTA('Missing D2C Reference'!A2:A1000)",
         "See 'Missing D2C Reference' sheet — needs a corrected/completed nh_sku→URL mapping export")
    r += 1

    section("Flipkart page health")
    stat("Broken/placeholder Flipkart pages (generic template, no real data)",
         f"=COUNTIF({diff_range}{dr},TRUE)",
         "Flipkart served a templated 'Store Online' page instead of real product data — needs a manual look on Flipkart's side (dead FSN / soft-block)")
    stat("Comparable listings (real data both sides)", f"=COUNTIF({diff_range}{dr},FALSE)")
    r += 1

    section("Title sanity (not exact-match — does the Flipkart title make sense for the SKU?)")
    stat("Titles flagged plausible", f"=COUNTIF({diff_range}{tr},TRUE)")
    stat("Titles flagged NOT plausible (eyeball these)", f"=COUNTIF({diff_range}{tr},FALSE)",
         "Not a hard failure — e.g. a bundle-named listing can legitimately score low here")
    r += 1

    section("Description & ingredients similarity")
    stat("Avg description similarity (token overlap, 0-1)", f"=AVERAGE({diff_range}{kr})")
    stat("Avg ingredient-term coverage (0-1, blank where D2C has no ingredient list)",
         f"=AVERAGE({diff_range}{lr})")
    r += 1

    section("Shelf life")
    for status, lbl in [("match", "Match"), ("mismatch", "Genuine mismatch (needs a look)"),
                         ("not_found", "No duration mentioned on Flipkart"),
                         ("d2c_missing", "D2C side has no shelf-life text")]:
        stat(f"Shelf life — {lbl}", f'=COUNTIF({diff_range}{sr},"{status}")')
    fk_shelf_col = get_column_letter(col["flipkart_max_shelf_life"])
    fk_shelf_range = f"{fk_shelf_col}2:{fk_shelf_col}{last_row}"
    stat("...of which, Flipkart states exactly \"3 Months\"",
         f'=COUNTIFS({diff_range}{sr},"mismatch",{diff_range}{fk_shelf_range},"3 Months")',
         "68% of all genuine mismatches (51 of 75, checked 2026-09-03) — Flipkart says \"3 Months\" "
         "regardless of what the true shelf life is, across otherwise-unrelated products. Reads as a "
         "systemic issue on Flipkart's catalog side (e.g. a default/template value), not 51 independent "
         "one-off content errors — worth raising with whoever manages the Flipkart catalog rather than "
         "correcting SKU-by-SKU.")
    r += 1

    section("Image count — CAVEAT: Flipkart caps listing images at 5 (platform limit), so a lower Flipkart count than D2C is often expected, not a real gap")
    ws2.cell(row=r - 1, column=1).font = Font(name=FONT, bold=True, size=11, color="8A6D00")
    stat("Flipkart listings already at the 5-image cap",
         f"=COUNTIFS({diff_range}{dr},FALSE,{diff_range}{pr},5)")
    stat("Flipkart listings using FEWER than 5 image slots (worth a look — not using all available slots)",
         f"=COUNTIFS({diff_range}{dr},FALSE,{diff_range}{pr},\"<5\")")
    r += 1

    section("Visual image match (validated against real photos — see notes)")
    stat("Avg best-pair visual similarity (comparable rows, 0=unrelated, 1=same photo)",
         f"=AVERAGEIF({diff_range}{dr},FALSE,{diff_range}{vr})")
    stat("High-confidence photo match (>=0.85)", f"=COUNTIFS({diff_range}{dr},FALSE,{diff_range}{vr},\">=0.85\")")
    stat("Needs a look (<0.6)", f"=COUNTIFS({diff_range}{dr},FALSE,{diff_range}{vr},\"<0.6\")",
         "Manually spot-checked: the lowest scores in this dataset were combo/gift-set listings genuinely photographed differently per platform, not a scoring bug — but still worth a human glance per SKU")
    r += 2

    ws2.cell(row=r, column=1, value="Method notes").font = SECTION_FONT
    r += 1
    notes = [
        "Visual similarity uses a perceptual hash (dHash) of downloaded product images, with letterbox/padding borders trimmed before comparison so that a marketplace's added white bars don't make an identical photo score as different.",
        "Calibrated against real photos on 2026-09-01: SKUs scoring >=0.95 were confirmed to be the same photo; the lowest-scoring SKUs were confirmed to be gift-set/combo listings with genuinely different photography per platform.",
        "Shelf-life comparison now reads Flipkart's structured 'Maximum Shelf Life' field from the on-page Specifications grid (flipkart_max_shelf_life column) — a direct duration, not a text guess. It only falls back to scanning the free-text description (stripping return/replacement-policy boilerplate like '30 Day Replacement Guarantee' and age-restriction phrasing like 'not suitable for children above 5 years' first) on older scrapes taken before this field was extracted.",
        "Shelf-life comparison is unit-aware (2026-09-03 fix): '2 months' (D2C) and '60 Days' (Flipkart) are recognized as the same duration instead of a false 'mismatch' — caught 7 of an initial 82 flagged mismatches. Of the 75 genuine mismatches remaining, 51 (68%) are cases where Flipkart states exactly '3 Months' regardless of the SKU — see the caveat in the Shelf life section above.",
        "Visual similarity in this version was carried over unchanged from the prior run's data (same product image URLs, same already-validated metric) rather than re-downloaded, since this update only touched shelf-life logic — not re-run with fresh --visual data.",
        "Title comparison is intentionally NOT an exact-match test — platforms phrase titles differently. It checks whether the D2C product's core words appear in the Flipkart title.",
    ]
    for note in notes:
        ws2.cell(row=r, column=1, value="• " + note).font = Font(name=FONT, size=10)
        ws2.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        ws2.cell(row=r, column=1).alignment = Alignment(wrap_text=True, vertical="top")
        ws2.row_dimensions[r].height = 30
        r += 1

    autosize(ws2, [58, 14, 70, 10, 10, 10])

    # ---------------- Sheet 3: Priority Review ----------------
    ws3 = wb.create_sheet("Priority Review")
    # Each row is classified into exactly ONE priority tier, checked in this
    # order, so nothing is double-counted across tiers. Ranked by what a KAM
    # should act on first, per direct instruction from Anika (2026-09-03):
    # shelf-life issues outrank broken pages, and the 51-SKU systemic
    # "Flipkart says 3 Months for everything" pattern outranks individual
    # shelf-life errors, since it's fixed with ONE escalation, not 51.
    TIER_ACTION = {
        "P1": "Escalate to Flipkart catalog team as one issue — do NOT fix SKU-by-SKU",
        "P2": "Fix this SKU's Flipkart shelf-life value individually",
        "P3": "Check this Flipkart listing's status (dead FSN? soft-blocked? real bug?)",
        "P4": "Confirm this Flipkart listing is really the right SKU",
        "P5": "Spot-check the Flipkart product photos against D2C",
    }

    def classify(rr):
        if rr["shelf_life_status"] == "mismatch":
            fk_val = (rr.get("flipkart_max_shelf_life") or "").strip()
            if fk_val == "3 Months":
                return ("P1", f"D2C says \"{rr['d2c_shelf_life']}\" — Flipkart's Specifications grid says "
                               f"\"3 Months\" (same value shows up on 51 otherwise-unrelated SKUs — a "
                               f"Flipkart catalog default, not a per-product error)")
            fk_shown = fk_val or "(from description text, no Specifications field found)"
            return ("P2", f"D2C says \"{rr['d2c_shelf_life']}\" — Flipkart's Specifications grid says \"{fk_shown}\"")
        if rr["flipkart_page_broken"] is True:
            return ("P3", "Flipkart served a generic placeholder page — check if the FSN is dead or soft-blocked")
        if rr["title_plausible"] is False:
            return ("P4", "Flipkart title doesn't share the D2C product's core words — eyeball whether it's really the right SKU")
        if rr["visual_best_similarity"] is not None and rr["visual_best_similarity"] < 0.6:
            return ("P5", "Best-matching photo pair scores low — known false-alarm pattern: gift/combo sets "
                          "legitimately photographed differently per platform, but still worth a glance")
        return (None, None)

    classified = []
    for rr in diff_rows:
        tier, detail = classify(rr)
        if tier:
            classified.append((tier, rr, detail))
    classified.sort(key=lambda t: (t[0], t[1]["nh_sku"]))
    tier_counts = {t: sum(1 for c in classified if c[0] == t) for t in TIER_ACTION}

    ws3["A1"] = "Priority Review — start here"
    ws3["A1"].font = TITLE_FONT
    ws3["A2"] = ("Snapshot generated from 'Diff Data' on " + datetime.date.today().isoformat() +
                 " — re-run build_report.py after a fresh diff to refresh both the counts below and the rows.")
    ws3["A2"].font = NOTE_FONT
    ws3.merge_cells("A2:H2")
    ws3["A2"].alignment = Alignment(wrap_text=True)

    r3 = 4
    ws3.cell(row=r3, column=1, value="Work through in this order:").font = SECTION_FONT
    r3 += 1
    for tier in ["P1", "P2", "P3", "P4", "P5"]:
        cnt = tier_counts.get(tier, 0)
        c = ws3.cell(row=r3, column=1, value=tier)
        c.font = Font(name=FONT, bold=True)
        c.fill = TIER_FILLS[tier]
        ws3.cell(row=r3, column=2, value=f"{cnt} SKU{'s' if cnt != 1 else ''}").font = BOLD
        action_cell = ws3.cell(row=r3, column=3, value=TIER_ACTION[tier])
        action_cell.font = Font(name=FONT)
        ws3.merge_cells(start_row=r3, start_column=3, end_row=r3, end_column=8)
        action_cell.alignment = Alignment(wrap_text=True, vertical="top")
        r3 += 1
    r3 += 1

    header_row = r3
    headers = ["priority", "nh_sku", "fsn", "action", "detail", "flipkart_title", "d2c_title", "shelf_life_status"]
    for ci, h in enumerate(headers, start=1):
        ws3.cell(row=header_row, column=ci, value=h)
    style_header(ws3, len(headers), row=header_row)

    for tier, rr, detail in classified:
        ws3.append([tier, rr["nh_sku"], rr["fsn"], TIER_ACTION[tier], detail,
                    rr["flipkart_title"], rr["d2c_title"], rr["shelf_life_status"]])
    data_first_row = header_row + 1
    data_last_row = ws3.max_row
    for row in ws3.iter_rows(min_row=data_first_row, max_row=data_last_row):
        for cell in row:
            cell.font = Font(name=FONT)
    for tier, fill in TIER_FILLS.items():
        ws3.conditional_formatting.add(
            f"A{data_first_row}:A{data_last_row}",
            CellIsRule(operator="equal", formula=[f'"{tier}"'], fill=fill),
        )
    ws3.freeze_panes = f"A{data_first_row}"
    ws3.auto_filter.ref = f"A{header_row}:{get_column_letter(len(headers))}{data_last_row}"
    autosize(ws3, [10, 16, 18, 46, 60, 42, 42, 16])

    # ---------------- Sheet 4: Missing D2C Reference ----------------
    ws4 = wb.create_sheet("Missing D2C Reference")
    mheaders = ["nh_sku", "reason", "attempted_url"]
    ws4.append(mheaders)
    style_header(ws4, len(mheaders))
    for mr in missing_rows:
        ws4.append([mr["nh_sku"], mr["reason"], mr["attempted_url"]])
    mlast = len(missing_rows) + 1
    ws4.freeze_panes = "A2"
    ws4.auto_filter.ref = f"A1:C{mlast}"
    for row in ws4.iter_rows(min_row=2, max_row=mlast):
        for cell in row:
            cell.font = Font(name=FONT)
    reason_col = "B"
    ws4.conditional_formatting.add(
        f"{reason_col}2:{reason_col}{mlast}",
        CellIsRule(operator="equal", formula=['"dead_link_404"'], fill=BAD_FILL),
    )
    autosize(ws4, [18, 20, 70])
    ws4["E1"] = "no_url_in_mapping = never had a D2C URL in the supplied mapping (needs the completed export)."
    ws4["E1"].font = NOTE_FONT
    ws4["E2"] = "dead_link_404 = had a URL but nathabit.in returned 404 for it (several end in '-copy', suggesting a deleted staging page)."
    ws4["E2"].font = NOTE_FONT

    wb.save("qa_findings_flipkart_vs_d2c_v2.xlsx")
    print(f"Saved qa_findings_flipkart_vs_d2c_v2.xlsx — Diff Data: {n} rows, "
          f"Priority Review: {len(classified)} rows ({tier_counts}), "
          f"Missing D2C Reference: {len(missing_rows)} rows")


if __name__ == "__main__":
    build()
