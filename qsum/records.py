"""Quotation record: normalisation, company matching, validation, de-duplication.

A "raw" record is whatever an extractor returns (Claude tool input or the pdftotext parser).
`normalize_record` turns it into the clean dict that is cached and written to Excel:

    quote_no, quote_base, rev, date (ISO str|None), seller_name, seller_tax_id,
    customer, items[{description, quantity, unit, unit_price, amount}],
    subtotal, vat, total
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from .config import Company

# ---------------------------------------------------------------- Thai text cleanup

def fix_thai(s: Optional[str]) -> str:
    """Repair the broken Thai text layer some quotation PDFs produce.

    - "จำากัด" (sara-am + extra sara-aa) -> "จำกัด"
    - "จํา" (nikhahit + sara-aa) -> "จำ"
    - tone mark landing after a closing paren "ใหญ)่" -> "ใหญ่)"
    """
    if not s:
        return ""
    s = s.replace("ํา", "ำ")      # nikhahit + sara aa -> sara am
    s = s.replace("ำา", "ำ")      # sara am + sara aa -> sara am
    s = re.sub(r"\)([่-๋])", r"\1)", s)
    return re.sub(r"[ \t]+", " ", s).strip()


_HEAD_OFFICE = re.compile(r"\s*\((?:สำนักงานใหญ่|สนญ\.?|head\s*office)\)\s*$", re.IGNORECASE)


def clean_name(s: Optional[str]) -> str:
    """Fix Thai artefacts and drop the trailing "(สำนักงานใหญ่)" marker."""
    return _HEAD_OFFICE.sub("", fix_thai(s)).strip()


# ---------------------------------------------------------------- field parsers

def parse_amount(v: Any) -> Optional[float]:
    """'1,234.50' / 1234.5 / '1234.50 บาท' -> 1234.5 ; anything unparsable -> None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(v))
    if not m:
        return None
    return round(float(m.group(0).replace(",", "")), 2)


def parse_date(v: Any) -> Optional[str]:
    """Accept ISO 'YYYY-MM-DD' or 'DD/MM/YYYY' (day first, as printed on the quotes).

    Buddhist-era years (> 2400) are converted to Gregorian. Returns ISO string or None.
    """
    if not v:
        return None
    s = str(v).strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.match(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})", s)
        if not m:
            return None
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y > 2400:
        y -= 543
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


_REV = re.compile(r"^(.*?)[\s\-_]*rev\.?\s*(\d+)\s*$", re.IGNORECASE)


def split_quote_no(quote_no: str) -> Tuple[str, int]:
    """'QT0000000001 Rev.1' -> ('QT0000000001', 1) ; no Rev -> (quote_no, 0)."""
    q = (quote_no or "").strip()
    m = _REV.match(q)
    if m:
        return m.group(1).strip(), int(m.group(2))
    return q, 0


def normalize_record(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce an extractor's output into the canonical record (never raises on bad fields)."""
    quote_no = fix_thai(str(raw.get("quote_no") or "")).strip()
    base, rev = split_quote_no(quote_no)
    items: List[Dict[str, Any]] = []
    for it in raw.get("items") or []:
        if not isinstance(it, dict):
            continue
        items.append({
            "description": fix_thai(str(it.get("description") or "")),
            "quantity": parse_amount(it.get("quantity")),
            "unit": fix_thai(str(it.get("unit") or "")),
            "unit_price": parse_amount(it.get("unit_price")),
            "amount": parse_amount(it.get("amount")),
        })
    return {
        "quote_no": quote_no,
        "quote_base": base,
        "rev": rev,
        "date": parse_date(raw.get("quote_date") or raw.get("date")),
        "seller_name": clean_name(raw.get("seller_name")),
        "seller_tax_id": re.sub(r"\D", "", str(raw.get("seller_tax_id") or "")),
        "customer": clean_name(raw.get("customer_name") or raw.get("customer")),
        "items": items,
        "subtotal": parse_amount(raw.get("subtotal")),
        "vat": parse_amount(raw.get("vat")),
        "total": parse_amount(raw.get("total")),
    }


# ---------------------------------------------------------------- company matching

def _squash(s: str) -> str:
    """Case/space/punctuation-insensitive form used for alias matching."""
    return re.sub(r"[\s.,()\-]+", "", fix_thai(s)).lower()


def match_company(rec: Dict[str, Any], companies: List[Company]) -> Optional[Company]:
    """Match the seller to a configured company: tax id first, then name aliases."""
    tid = rec.get("seller_tax_id") or ""
    if tid:
        for c in companies:
            if c.tax_id and c.tax_id == tid:
                return c
    seller = _squash(rec.get("seller_name") or "")
    if seller:
        for c in companies:
            for a in [c.display_name] + c.aliases:
                a = _squash(a)
                # Min length keeps empty/placeholder names from matching everything.
                if len(a) >= 4 and a in seller:
                    return c
    return None


# ---------------------------------------------------------------- validation

def validate_record(rec: Dict[str, Any]) -> List[str]:
    """Return Thai warnings for a record (empty list = looks fine)."""
    w: List[str] = []
    if not rec.get("quote_no"):
        w.append("ไม่พบเลขที่ใบเสนอราคา")
    if not rec.get("date"):
        w.append("ไม่พบวันที่")
    if not rec.get("customer"):
        w.append("ไม่พบชื่อลูกค้า")
    sub, vat, tot = rec.get("subtotal"), rec.get("vat"), rec.get("total")
    if sub is None or tot is None:
        w.append("ไม่พบยอดก่อน VAT หรือยอดรวม")
    else:
        v = vat or 0.0
        if abs(sub + v - tot) > 0.02:
            w.append("ยอดไม่ลงตัว: ก่อน VAT %s + VAT %s != รวม %s" % (_f(sub), _f(v), _f(tot)))
        if vat is not None and sub > 0 and vat > 0 and abs(vat - round(sub * 0.07, 2)) > 1.0:
            w.append("VAT ไม่ใช่ 7%% ของยอดก่อน VAT (VAT %s)" % _f(vat))
        item_sum = sum(i["amount"] for i in rec.get("items", []) if i.get("amount") is not None)
        if rec.get("items") and item_sum and abs(item_sum - sub) > 0.02:
            w.append("ผลรวมรายการ %s != ยอดก่อน VAT %s (อาจมีส่วนลด)" % (_f(item_sum), _f(sub)))
    return w


def _f(x: float) -> str:
    return "{:,.2f}".format(x)


# ---------------------------------------------------------------- entry + de-dup

@dataclass
class Entry:
    """One PDF file, after extraction."""
    file_name: str                       # path relative to the source folder
    sha256: str
    record: Dict[str, Any]
    method: str                          # "claude" | "text"
    first_seen: str                      # ISO date the file was first processed
    company: Optional[Company] = None
    warnings: List[str] = field(default_factory=list)


def dedupe(entries: List[Entry]) -> Tuple[List[Entry], List[Tuple[Entry, str]]]:
    """Keep one entry per (seller, quote number): the highest Rev wins.

    Returns (kept, dropped) where dropped is [(entry, reason)]. Without this a quotation and
    its "Rev.1" (or a copy of the same file) would be counted twice.
    """
    groups: Dict[Tuple[str, str], List[Entry]] = {}
    seller_keys_by_base: Dict[str, Dict[str, Entry]] = {}   # base -> {seller key -> a sample entry}
    kept: List[Entry] = []
    for e in entries:
        base = e.record.get("quote_base")
        if not base:                       # cannot de-dup without a number; keep it
            kept.append(e)
            continue
        seller_key = e.company.sheet_name if e.company else (e.record.get("seller_tax_id") or e.record.get("seller_name") or "")
        groups.setdefault((seller_key, base.upper()), []).append(e)
        seller_keys_by_base.setdefault(base.upper(), {}).setdefault(seller_key, e)

    # Same quote number under different seller keys is not merged (two companies may legitimately
    # share a number), but it may also be one quotation matched differently (e.g. the tax id was
    # missing in one read), which would be counted twice: warn on every involved entry.
    for base, by_key in seller_keys_by_base.items():
        if len(by_key) > 1:
            for e in (x for g_key, g in groups.items() if g_key[1] == base for x in g):
                e.warnings.append("เลขที่ %s ซ้ำกับผู้ขายอื่น (%s) ตรวจว่านับซ้ำหรือไม่" % (
                    base, ", ".join(sorted(k or "?" for k in by_key))))

    dropped: List[Tuple[Entry, str]] = []
    for group in groups.values():
        # Highest rev first, then file name for determinism (first_seen is not used).
        group.sort(key=lambda x: (-x.record.get("rev", 0), x.file_name))
        winner = group[0]
        kept.append(winner)
        for loser in group[1:]:
            if loser.record.get("rev", 0) < winner.record.get("rev", 0):
                why = "มีฉบับแก้ไขใหม่กว่า (%s) จึงไม่นับฉบับนี้" % (winner.record.get("quote_no") or winner.file_name)
            else:
                why = "เลขที่ซ้ำกับไฟล์ %s จึงไม่นับซ้ำ" % winner.file_name
            dropped.append((loser, why))
    return kept, dropped
