"""Regenerate the status fixtures of the desktop app (gui/Tests/QuotationSummaryAppTests/Fixtures/status-*.json).

    .venv/bin/python tests/make_gui_fixtures.py

The fixtures are REAL output of qsum.status (collect()) over a synthetic temp world (fake PDFs, made-up
companies, nothing from config.json or the real cache). Only three things are normalised so the files are
stable and carry no machine paths: the temp folder path, the "added" date of files that are not in the cache
(the file birth time) and the time zone (main() forces TZ to Asia/Bangkok). tests/test_contract.py checks that the
key set and enums of a fresh real output still equal these fixtures.

Nothing here touches the real Keychain, launchd, ~/Library or Claude: every path points into a temp folder.
"""
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from qsum import records, state as st            # noqa: E402
from qsum import status as S                      # noqa: E402
from qsum.cache import sha256_file                # noqa: E402

FIXTURES = PROJECT_DIR / "gui" / "Tests" / "QuotationSummaryAppTests" / "Fixtures"
FAKE_SOURCE = "/Users/example/QuotationSummary/ใบเสนอราคาที่ขายงานได้"
FAKE_APP = "/Users/example/QuotationSummary/quotation-summary"
NOW = datetime.fromisoformat("2026-11-10T09:30:00+07:00")


def record(quote_no, total=1070.0, seller="บริษัท ตัวอย่าง เอ จำกัด", tax="0100000000001"):
    return records.normalize_record(dict(
        quote_no=quote_no, quote_date="2026-11-03", seller_name=seller, seller_tax_id=tax,
        customer_name="บริษัท ลูกค้า ตัวอย่าง จำกัด",
        items=[dict(description="ถุงผ้าสปันบอนด์", quantity=1, unit="PCS.", unit_price=1000.0, amount=1000.0)],
        subtotal=1000.0, vat=70.0, total=total))


def entry(rec, method="claude"):
    return {"record": rec, "method": method, "first_seen": "2026-11-03", "model": "m", "warned": False, "attempts": 1}


def build_world(tmp: Path, running: bool):
    src = tmp / "src"
    src.mkdir()
    cfg_path = tmp / "config.json"
    cfg_path.write_text(json.dumps({
        "source_folder": str(src), "output_file": str(tmp / "out" / "สรุปใบเสนอราคา.xlsx"),
        "cache_file": str(tmp / "app" / "cache.json"), "extractor": "auto",
        "companies": [{"sheet_name": "Company A", "tax_id": "0100000000001"},
                      {"sheet_name": "Company B", "tax_id": "0100000000002"}]}), encoding="utf-8")
    (tmp / "out").mkdir()
    out = tmp / "out" / "สรุปใบเสนอราคา.xlsx"
    out.write_bytes(b"xlsx")
    mtime = NOW.timestamp() - 3600
    os.utime(out, (mtime, mtime))
    files = {                                    # name -> (bytes, cache entry or None)
        "QT0000000001 ลูกค้า ก.pdf": (b"one", entry(record("QT0000000001"))),
        "QT0000000002 ลูกค้า ข.pdf": (b"two", entry(record("QT0000000002"))),
        "QT0000000003 ตรวจยอด.pdf": (b"three", entry(record("QT0000000003", total=9999.0))),
        "QT0000000004 อ่านด้วย text.pdf": (b"four", entry(record("QT0000000004"), method="text")),
        "QT0000000005 ผู้ขายอื่น.pdf": (b"five", entry(record("QT0000000005", seller="บริษัท อื่น จำกัด", tax="1111111111111"))),
        "QT0000000006.pdf": (b"six-old", entry(record("QT0000000006"))),
        "QT0000000006 Rev.1.pdf": (b"six-new", entry(record("QT0000000006 Rev.1"))),
        "ใหม่ ยังไม่ได้อ่าน.pdf": (b"seven", None),
        "เสีย อ่านไม่ได้.pdf": (b"eight", None),
    }
    cache, state = {}, st.empty_state()
    for name, (data, ent) in files.items():
        p = src / name
        p.write_bytes(data)
        if ent:
            cache[sha256_file(p)] = ent
    bad = sha256_file(src / "เสีย อ่านไม่ได้.pdf")
    state["files"][bad] = {"rel": "เสีย อ่านไม่ได้.pdf", "outcome": "failed", "message": "Claude อ่านไฟล์นี้ไม่สำเร็จ (หมดเวลา)", "at": "x"}
    state["last_run"] = {
        "started_at": "2026-11-09T08:00:02+07:00", "finished_at": "2026-11-09T08:04:40+07:00", "trigger": "schedule",
        "exit_code": 2, "fatal": None, "n_pdf": 9, "stats": {"cached": 6, "claude": 2, "text": 0, "failed": 1},
        "issues": ["QT0000000003 ตรวจยอด.pdf: ยอดไม่ลงตัว"], "output_file": str(out), "excel_written": True}
    if running:
        state["current"] = {"started_at": "2026-11-10T09:29:10+07:00", "trigger": "app", "pid": os.getpid(),
                            "total": 9, "done": 2, "current_file": "ใหม่ ยังไม่ได้อ่าน.pdf"}
    (tmp / "app").mkdir()
    (tmp / "app" / "cache.json").write_text(json.dumps({"schema": 1, "entries": cache}), encoding="utf-8")
    st.write_state(tmp / "app" / "state.json", state)
    agents = tmp / "agents"
    agents.mkdir()
    with open(str(agents / (S.LAUNCHD_LABEL + ".plist")), "wb") as f:
        plistlib.dump({"Label": S.LAUNCHD_LABEL, "StartCalendarInterval": {"Weekday": 1, "Hour": 8, "Minute": 0}}, f)
    return cfg_path, agents


def normalise(doc, tmp: Path):
    text = json.dumps(doc, ensure_ascii=False).replace(str(tmp), "/Users/example/QuotationSummary")
    text = text.replace("/Users/example/QuotationSummary/src", FAKE_SOURCE)
    doc = json.loads(text)
    for row in doc.get("files", []):
        if row["status"] in ("waiting", "error"):
            row["added"] = "2026-11-09"       # birth time of a temp file: not stable
    return doc


def write(name, doc):
    path = FIXTURES / name
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("wrote", path)


def real_status(kind):
    """Real qsum.status output over the synthetic world, normalised. kind = v1 | running | error | not-installed."""
    saved = os.environ.get("QS_LAUNCH_AGENTS_DIR")
    try:
        if kind == "not-installed":
            # the real scripts/status.sh in a folder without .venv
            with tempfile.TemporaryDirectory() as t:
                app = Path(t) / "app"
                (app / "scripts").mkdir(parents=True)
                (app / "scripts" / "status.sh").write_bytes((PROJECT_DIR / "scripts" / "status.sh").read_bytes())
                p = subprocess.run(["/bin/bash", str(app / "scripts" / "status.sh")], capture_output=True)
            doc = json.loads(p.stdout.decode("utf-8"))
            doc["generated_at"] = "2026-11-10T09:30:00+07:00"
            return doc
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t).resolve()
            cfg_path, agents = build_world(tmp, kind == "running")
            os.environ["QS_LAUNCH_AGENTS_DIR"] = str(agents)
            if kind == "running":
                holder = st.RunLock(tmp / "app" / "run.lock")      # a real held lock = a real "running" probe
                assert holder.acquire()
                try:
                    doc = S.collect(str(cfg_path), now=NOW)
                finally:
                    holder.release()
            elif kind == "error":
                import shutil
                shutil.rmtree(str(tmp / "src"))                     # source folder gone: all fields, files empty
                doc = S.collect(str(cfg_path), now=NOW)
            else:
                doc = S.collect(str(cfg_path), now=NOW)
            doc = normalise(doc, tmp)
            if doc.get("error"):
                doc["error"] = doc["error"].replace(str(tmp), "/Users/example/QuotationSummary")
            return doc
    finally:
        if saved is None:
            os.environ.pop("QS_LAUNCH_AGENTS_DIR", None)
        else:
            os.environ["QS_LAUNCH_AGENTS_DIR"] = saved


def main():
    # Fixed time zone so the files are the same on every machine (set here, not at import: tests import this module).
    os.environ["TZ"] = "Asia/Bangkok"
    time.tzset()
    os.environ.pop("QS_CONFIG", None)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, kind in (("status-v1.json", "v1"), ("status-running.json", "running"),
                       ("status-error.json", "error"), ("status-not-installed.json", "not-installed")):
        write(name, real_status(kind))


if __name__ == "__main__":
    main()
