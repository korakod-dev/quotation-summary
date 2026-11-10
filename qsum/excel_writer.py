"""Build the .xlsx: one sheet per configured company, an optional catch-all sheet, then "สรุป".

All figures are written as plain values (not formulas) so they show in any viewer (Quick Look,
Excel, Numbers). The workbook is regenerated from scratch each run, so manual edits are not kept.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .config import SUMMARY_SHEET, UNMATCHED_SHEET, Company
from .records import Entry

MONEY = "#,##0.00"
DATE_FMT = "dd/mm/yyyy"
HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")
TITLE_FONT = Font(bold=True, size=14)
BOLD = Font(bold=True)
WARN_FONT = Font(color="C00000")
TOP_BORDER = Border(top=Side(style="thin"))

COMPANY_HEADERS = ["วันที่", "เลขที่ใบเสนอราคา", "ลูกค้า", "รายการสินค้า", "ยอดก่อน VAT", "VAT",
                   "ยอดรวม", "ชื่อไฟล์", "เพิ่มเข้าระบบเมื่อ", "ตรวจสอบ"]
COMPANY_WIDTHS = [12, 22, 38, 42, 15, 13, 15, 44, 16, 50]


def _d(iso: Optional[str]) -> Optional[date]:
    return date.fromisoformat(iso) if iso else None


def _header(ws, row: int, headers: List[str]) -> None:
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=row, column=i, value=h)
        c.font = BOLD
        c.fill = HEADER_FILL
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _sum(entries: List[Entry], key: str) -> float:
    return round(sum((e.record.get(key) or 0.0) for e in entries), 2)


def _write_company_sheet(ws, entries: List[Entry]) -> None:
    _header(ws, 1, COMPANY_HEADERS)
    ordered = sorted(entries, key=lambda e: (e.record.get("date") or "9999", e.record.get("quote_no") or "", e.file_name))
    r = 1
    for e in ordered:
        r += 1
        rec = e.record
        items = "; ".join(i["description"] for i in rec.get("items", []) if i.get("description"))
        warnings = list(e.warnings)
        if e.method == "text":
            warnings.append("อ่านด้วย pdftotext (ไม่ได้ใช้ Claude) ควรเช็กเทียบกับ PDF")
        row = [_d(rec.get("date")), rec.get("quote_no"), rec.get("customer"), items,
               rec.get("subtotal"), rec.get("vat"), rec.get("total"), e.file_name,
               _d(e.first_seen), " | ".join(warnings)]
        for col, v in enumerate(row, 1):
            ws.cell(row=r, column=col, value=v)
        ws.cell(row=r, column=1).number_format = DATE_FMT
        ws.cell(row=r, column=9).number_format = DATE_FMT
        for col in (5, 6, 7):
            ws.cell(row=r, column=col).number_format = MONEY
        if warnings:
            ws.cell(row=r, column=10).font = WARN_FONT
    last_data = r
    # Total row after one blank row (keeps the autofilter range clean).
    tr = last_data + 2
    ws.cell(row=tr, column=4, value="รวม (%d ใบ)" % len(entries)).font = BOLD
    for col, key in ((5, "subtotal"), (6, "vat"), (7, "total")):
        c = ws.cell(row=tr, column=col, value=_sum(entries, key))
        c.number_format = MONEY
        c.font = BOLD
    for col in range(1, len(COMPANY_HEADERS) + 1):
        ws.cell(row=tr, column=col).border = TOP_BORDER
    for i, w in enumerate(COMPANY_WIDTHS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    if last_data >= 2:
        ws.auto_filter.ref = "A1:%s%d" % (get_column_letter(len(COMPANY_HEADERS)), last_data)


def week_bucket(d: date) -> Tuple[date, str]:
    """ISO week (Mon-Sun): returns (monday, label) e.g. (2026-11-02, '2026-W45 (02/11-08/11)')."""
    iso_year, iso_week, iso_day = d.isocalendar()
    monday = d - timedelta(days=iso_day - 1)
    sunday = monday + timedelta(days=6)
    return monday, "%d-W%02d (%s-%s)" % (iso_year, iso_week, monday.strftime("%d/%m"), sunday.strftime("%d/%m"))


def _period_table(ws, row: int, title: str, first_col: str, columns: List[str],
                  buckets: List[Tuple[str, Dict[str, List[Entry]]]]) -> int:
    """Write a period x company table of total (incl. VAT). Returns the next free row."""
    ws.cell(row=row, column=1, value=title).font = Font(bold=True, size=12)
    row += 1
    _header(ws, row, [first_col] + columns + ["รวม", "จำนวนใบ"])
    grand = {c: 0.0 for c in columns}
    n_total = 0
    for label, by_col in buckets:
        row += 1
        ws.cell(row=row, column=1, value=label)
        line = 0.0
        n = 0
        for j, col in enumerate(columns, 2):
            v = _sum(by_col.get(col, []), "total")
            c = ws.cell(row=row, column=j, value=v)
            c.number_format = MONEY
            line += v
            n += len(by_col.get(col, []))
            grand[col] += v
        c = ws.cell(row=row, column=len(columns) + 2, value=round(line, 2))
        c.number_format = MONEY
        c.font = BOLD
        ws.cell(row=row, column=len(columns) + 3, value=n)
        n_total += n
    row += 1
    ws.cell(row=row, column=1, value="รวมทั้งหมด").font = BOLD
    for j, col in enumerate(columns, 2):
        c = ws.cell(row=row, column=j, value=round(grand[col], 2))
        c.number_format = MONEY
        c.font = BOLD
    c = ws.cell(row=row, column=len(columns) + 2, value=round(sum(grand.values()), 2))
    c.number_format = MONEY
    c.font = BOLD
    ws.cell(row=row, column=len(columns) + 3, value=n_total).font = BOLD
    for col in range(1, len(columns) + 4):
        ws.cell(row=row, column=col).border = TOP_BORDER
    return row + 2


def _write_summary(ws, groups: Dict[str, List[Entry]], sheet_order: List[str], issues: List[str],
                   generated: datetime, n_pdf: int) -> None:
    ws["A1"] = "สรุปใบเสนอราคาที่ขายงานได้"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = "อัปเดตล่าสุด %s | PDF ทั้งหมด %d ไฟล์ | ยอดรวมรวม VAT แล้ว | แบ่งงวดตามวันที่บนใบเสนอราคา (สัปดาห์ ISO จันทร์-อาทิตย์)" % (
        generated.strftime("%d/%m/%Y %H:%M"), n_pdf)
    row = 4

    # A) totals per company
    ws.cell(row=row, column=1, value="ยอดรวมแยกตามบริษัท").font = Font(bold=True, size=12)
    row += 1
    _header(ws, row, ["บริษัท", "จำนวนใบ", "ยอดก่อน VAT", "VAT", "ยอดรวม"])
    for name in sheet_order:
        row += 1
        es = groups.get(name, [])
        ws.cell(row=row, column=1, value=name)
        ws.cell(row=row, column=2, value=len(es))
        for col, key in ((3, "subtotal"), (4, "vat"), (5, "total")):
            ws.cell(row=row, column=col, value=_sum(es, key)).number_format = MONEY
    row += 1
    all_entries = [e for name in sheet_order for e in groups.get(name, [])]
    ws.cell(row=row, column=1, value="รวมทั้งหมด").font = BOLD
    ws.cell(row=row, column=2, value=len(all_entries)).font = BOLD
    for col, key in ((3, "subtotal"), (4, "vat"), (5, "total")):
        c = ws.cell(row=row, column=col, value=_sum(all_entries, key))
        c.number_format = MONEY
        c.font = BOLD
    for col in range(1, 6):
        ws.cell(row=row, column=col).border = TOP_BORDER
    row += 2

    # B) per month, C) per week (newest first; undated quotes last)
    months: Dict[str, Dict[str, List[Entry]]] = {}
    weeks: Dict[date, Tuple[str, Dict[str, List[Entry]]]] = {}
    undated: Dict[str, List[Entry]] = {}
    for name in sheet_order:
        for e in groups.get(name, []):
            d = _d(e.record.get("date"))
            if d is None:
                undated.setdefault(name, []).append(e)
                continue
            months.setdefault(d.strftime("%Y-%m"), {}).setdefault(name, []).append(e)
            monday, label = week_bucket(d)
            weeks.setdefault(monday, (label, {}))[1].setdefault(name, []).append(e)
    month_rows = [(k, months[k]) for k in sorted(months, reverse=True)]
    week_rows = [weeks[k] for k in sorted(weeks, reverse=True)]
    if undated:
        month_rows.append(("ไม่ระบุวันที่", undated))
        week_rows.append(("ไม่ระบุวันที่", undated))
    row = _period_table(ws, row, "ยอดรวมแยกตามเดือน", "เดือน", sheet_order, month_rows)
    row = _period_table(ws, row, "ยอดรวมแยกตามสัปดาห์", "สัปดาห์", sheet_order, week_rows)

    # D) issues
    ws.cell(row=row, column=1, value="รายการที่ต้องตรวจสอบ").font = Font(bold=True, size=12)
    row += 1
    if not issues:
        ws.cell(row=row, column=1, value="ไม่มี")
    for msg in issues:
        c = ws.cell(row=row, column=1, value=msg)
        c.font = WARN_FONT
        row += 1

    ws.column_dimensions["A"].width = 34
    for i in range(2, 9):
        ws.column_dimensions[get_column_letter(i)].width = 18


def write_workbook(path: Path, companies: List[Company], entries: List[Entry], issues: List[str],
                   n_pdf: int, generated: Optional[datetime] = None) -> List[str]:
    """Write the workbook atomically. Returns the sheet names in order."""
    generated = generated or datetime.now()
    groups: Dict[str, List[Entry]] = {c.sheet_name: [] for c in companies}
    unmatched: List[Entry] = []
    for e in entries:
        if e.company:
            groups[e.company.sheet_name].append(e)
        else:
            unmatched.append(e)
    sheet_order = [c.sheet_name for c in companies]
    if unmatched:
        groups[UNMATCHED_SHEET] = unmatched
        sheet_order.append(UNMATCHED_SHEET)

    wb = Workbook()
    wb.remove(wb.active)
    for name in sheet_order:
        _write_company_sheet(wb.create_sheet(name), groups[name])
    _write_summary(wb.create_sheet(SUMMARY_SHEET), groups, sheet_order, issues, generated, n_pdf)

    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file then rename: a half-written workbook never replaces a good one.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".xlsx.tmp")
    os.close(fd)
    try:
        wb.save(tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return wb.sheetnames
