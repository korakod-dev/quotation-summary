"""Fallback extractor: `pdftotext -layout` + regexes. Needs no Claude token or API key.

Tuned on the layout of the sample quotations (header block with "เลขที่ / วันที่", seller block at
top-left, "ลูกค้า" block, numbered item table, "รวมเป็นเงิน / ภาษีมูลค่าเพิ่ม / จำนวนเงินรวมทั้งสิ้น").
It is less tolerant than Claude, so results from here are flagged in the Excel "ตรวจสอบ" column and
are re-extracted with Claude as soon as Claude (subscription token or API key) is available.

Requires poppler: `brew install poppler`.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

AMOUNT = r"([\d,]+\.\d{2})"
# Thai legal-entity prefixes that start the seller line.
ENTITY = re.compile(r"^(บริษัท|บจก\.?|ห้างหุ้นส่วนจำกัด|หจก\.?|ห้างหุ้นส่วน|ร้าน|company|co\.)", re.IGNORECASE)


class ExtractionError(Exception):
    """Extraction failed for this file (message is shown in the Excel issue list)."""


def _pdftotext_bin() -> str:
    for cand in (shutil.which("pdftotext"), "/opt/homebrew/bin/pdftotext", "/usr/local/bin/pdftotext"):
        if cand and Path(cand).exists():
            return cand
    raise ExtractionError("ไม่พบ pdftotext (ติดตั้งด้วย: brew install poppler)")


def pdf_to_text(path: Path) -> str:
    try:
        out = subprocess.run(
            [_pdftotext_bin(), "-layout", "-enc", "UTF-8", str(path), "-"],
            capture_output=True, timeout=60, check=True,
        )
    except subprocess.CalledProcessError as e:
        raise ExtractionError("pdftotext อ่านไฟล์ไม่ได้: %s" % e.stderr.decode("utf-8", "replace")[:120])
    except subprocess.TimeoutExpired:
        raise ExtractionError("pdftotext ใช้เวลานานเกินไป")
    return out.stdout.decode("utf-8", "replace")


def _cell_left(line: str) -> str:
    """Leftmost column of a -layout line (columns are separated by 2+ spaces)."""
    return re.split(r"\s{2,}", line.strip())[0]


def parse_text(raw_text: str) -> Dict[str, Any]:
    """Parse layout text into a raw record. Raises ExtractionError if nothing usable is found."""
    # Character-level repairs only: fix_thai() would squeeze the spaces that separate columns.
    text = _repair_chars(raw_text)
    if len(text.strip()) < 40:
        raise ExtractionError("PDF ไม่มีข้อความ (อาจเป็นไฟล์สแกน) ต้องใช้ Claude อ่าน")

    lines = [l.rstrip() for l in text.splitlines()]
    rec: Dict[str, Any] = {}

    m = re.search(r"เลขที่[ \t]+([A-Za-z]{1,5}[\w\-/]*\d[\w\-/]*(?:[ \t]+Rev\.?[ \t]*\d+)?)", text, re.IGNORECASE)
    rec["quote_no"] = m.group(1).strip() if m else None

    m = re.search(r"วันที่[ \t]+(\d{1,2}/\d{1,2}/\d{4})", text)
    rec["quote_date"] = m.group(1) if m else None

    # Seller = first entity-looking left cell before the "ลูกค้า" marker.
    cust_idx = next((i for i, l in enumerate(lines) if l.strip() == "ลูกค้า"), None)
    head = lines[: cust_idx if cust_idx is not None else 15]
    rec["seller_name"] = next((_cell_left(l) for l in head if ENTITY.match(l.strip())), None)

    # Tax ids: only look in the seller header (lines before "ลูกค้า"). Searching the whole document
    # would pick up the customer's id when the seller block has none.
    tax_ids = re.findall(r"เลขประจำ.*?(\d{13})", "\n".join(head))
    rec["seller_tax_id"] = tax_ids[0] if tax_ids else None

    if cust_idx is not None:
        rec["customer_name"] = next((_cell_left(l) for l in lines[cust_idx + 1:] if l.strip()), None)

    # Item rows: "#  description  qty unit  unit-price  amount"
    items: List[Dict[str, Any]] = []
    row = re.compile(r"^\s*(\d+)\s{2,}(.+?)\s{2,}([\d,]+(?:\.\d+)?)\s*(\S+)\s{2,}([\d,]+\.\d+)\s{2,}([\d,]+\.\d{2})\s*$")
    for l in lines:
        mm = row.match(l)
        if mm:
            items.append({
                "description": mm.group(2).strip(), "quantity": mm.group(3), "unit": mm.group(4),
                "unit_price": mm.group(5), "amount": mm.group(6),
            })
    rec["items"] = items

    m = re.search(r"รวมเป.{0,3}เงิน\s+" + AMOUNT, text)
    rec["subtotal"] = m.group(1) if m else None
    m = re.search(r"เพิ่ม\s*\d+\s*%\s+" + AMOUNT, text)
    rec["vat"] = m.group(1) if m else None
    m = re.search(r"ทั้งสิ้น\s+" + AMOUNT, text)
    rec["total"] = m.group(1) if m else None

    if not (rec["quote_no"] or rec["total"]):
        raise ExtractionError("ไม่พบเลขที่ใบเสนอราคา/ยอดรวมในไฟล์ (รูปแบบต่างจากตัวอย่าง) ต้องใช้ Claude อ่าน")
    return rec


def _repair_chars(s: str) -> str:
    """Character-level Thai repairs only; keeps whitespace so layout columns survive."""
    s = s.replace("ํา", "ำ").replace("ำา", "ำ")
    return re.sub(r"\)([่-๋])", r"\1)", s)


def extract(path: Path) -> Dict[str, Any]:
    return parse_text(pdf_to_text(path))
