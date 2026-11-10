"""Tests for the extraction -> cache -> Excel pipeline using MOCKED Claude responses.

No network, no API key, no real PDFs needed (PDF bytes are arbitrary; the fake Claude client
answers by file content). Run:  .venv/bin/python -m unittest discover -s tests -v
Real-PDF checks for the pdftotext fallback live in test_text_samples.py (skipped if samples absent).
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openpyxl import load_workbook

from qsum import records
from qsum.cache import Cache, sha256_file
from qsum.config import Company, Config, load_config
from qsum.extract_claude import TOOL, ClaudeExtractor, ClaudeUnavailable
from qsum.extract_text import ExtractionError
from qsum.main import SourceUnreadable, process
from qsum.main import main as qsum_main
from qsum.state import RunLock, read_state
from qsum.records import Entry
from qsum.excel_writer import week_bucket, write_workbook
from datetime import date

COMPANY_A = Company("Company A", "บริษัท ตัวอย่าง เอ จำกัด", "0100000000001", ["ตัวอย่าง เอ"])
COMPANY_B = Company("Company B", "บริษัท ตัวอย่าง บี จำกัด", "0100000000002", ["ตัวอย่าง บี"])
THIRD = Company("บริษัทที่ 3", "", "", [])

# Mock "Claude" answers keyed by the marker text inside each fake PDF file.
MOCK = {
    b"A-rev0": dict(quote_no="QT0000000001", quote_date="2026-09-08", seller_name="บริษัท ตัวอย่าง เอ จำกัด (สำนักงานใหญ่)",
                    seller_tax_id="0100000000001", customer_name="บริษัท ลูกค้า หนึ่ง จำกัด (สำนักงานใหญ่)",
                    items=[dict(description="Non-woven handbag Size:M", quantity=1000, unit="PCS.", unit_price=10.0, amount=10000.0)],
                    subtotal=10000.0, vat=700.0, total=10700.0),
    b"A-rev1": dict(quote_no="QT0000000001 Rev.1", quote_date="2026-09-08", seller_name="บริษัท ตัวอย่าง เอ จำกัด",
                    seller_tax_id="0100000000001", customer_name="บริษัท ลูกค้า หนึ่ง จำกัด",
                    items=[dict(description="Non-woven handbag Size:M", quantity=1000, unit="PCS.", unit_price=10.0, amount=10000.0)],
                    subtotal=10000.0, vat=700.0, total=10700.0),
    b"B": dict(quote_no="QT0000000002", quote_date="2026-11-09", seller_name="บริษัท ตัวอย่าง เอ จำกัด",
               seller_tax_id="0100000000001", customer_name="ห้างหุ้นส่วนจำกัด ลูกค้า สอง (2001)",
               items=[dict(description="Biodegradable spoon", quantity=1000, unit="PCS.", unit_price=0.5, amount=500.0),
                      dict(description="Biodegradable forks", quantity=2000, unit="PCS.", unit_price=0.6, amount=1200.0),
                      dict(description="Biodegradable spoons", quantity=3000, unit="PCS.", unit_price=0.7, amount=2100.0)],
               subtotal=3800.0, vat=266.0, total=4066.0),
    # Buddhist-era date, seller matched by name only (no tax id), numbers as strings with commas.
    b"C": dict(quote_no="QT0000000003", quote_date="03/11/2569", seller_name="บริษัท ตัวอย่าง บี จำกัด",
               seller_tax_id=None, customer_name="บริษัท ลูกค้า สาม จำกัด",
               items=[dict(description="ถุงกระดาษตัวอย่าง", quantity="5,000", unit="pcs.", unit_price="8.50", amount="42,500.00")],
               subtotal="42,500.00", vat="2,975.00", total="45,475.00"),
    # Unknown seller + totals that do not add up -> must land on the catch-all sheet with warnings.
    b"D": dict(quote_no="X-1", quote_date="2026-11-04", seller_name="บริษัท อื่น จำกัด", seller_tax_id="1111111111111",
               customer_name="ลูกค้า ทดสอบ", items=[], subtotal=1000.0, vat=70.0, total=1100.0),
}


class FakeClient:
    """Mimics anthropic.Anthropic().messages.create; counts calls to prove caching works."""

    def __init__(self, seq=None, exc=None, stop_reason=None, replies=None):
        """seq: {marker: [record, ...]} answers returned in order (last one repeats);
        exc: exception instance raised on every call; stop_reason: value put on the response;
        replies: list of content-block lists returned in order (last repeats), overrides seq/MOCK."""
        self.replies = replies
        self.calls = 0
        self.messages = SimpleNamespace(create=self._create)
        self.last_kwargs = None
        self.seq = seq or {}
        self.exc = exc
        self.stop_reason = stop_reason

    def _create(self, **kw):
        self.calls += 1
        self.last_kwargs = kw
        if self.exc is not None:
            raise self.exc
        if self.replies is not None:
            content = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
            resp = SimpleNamespace(content=content)
            if self.stop_reason is not None:
                resp.stop_reason = self.stop_reason
            return resp
        import base64
        pdf = base64.b64decode(kw["messages"][0]["content"][0]["source"]["data"])
        if pdf in self.seq:
            q = self.seq[pdf]
            data = q.pop(0) if len(q) > 1 else q[0]
        else:
            data = MOCK[pdf]
        resp = SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=TOOL["name"], input=data)])
        if self.stop_reason is not None:
            resp.stop_reason = self.stop_reason
        return resp


def sdk_error(name, message="boom"):
    """An exception whose class name matches an anthropic SDK error (classification is by name)."""
    return type(name, (Exception,), {})(message)


def make_cfg(tmp: Path, companies=None) -> Config:
    src = tmp / "src"
    src.mkdir(exist_ok=True)
    return Config(source_folder=src, output_file=tmp / "out.xlsx", cache_file=tmp / "cache" / "c.json",
                  companies=companies or [COMPANY_A, COMPANY_B, THIRD])


def put(cfg, name, marker):
    p = cfg.source_folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(marker)
    return p


class RecordTests(unittest.TestCase):
    def test_normalize_thai_and_be_date(self):
        r = records.normalize_record(MOCK[b"C"])
        self.assertEqual(r["date"], "2026-11-03")          # 2569 BE -> 2026
        self.assertEqual(r["total"], 45475.0)
        self.assertEqual(r["quote_base"], "QT0000000003")

    def test_fix_thai_and_head_office(self):
        self.assertEqual(records.clean_name("บริษัท ตัวอย่าง จำากัด (สำานักงานใหญ)่"), "บริษัท ตัวอย่าง จำกัด")
        self.assertEqual(records.fix_thai("จําสินค้า"), "จำสินค้า")

    def test_rev_split(self):
        self.assertEqual(records.split_quote_no("QT0000000001 Rev.1"), ("QT0000000001", 1))
        self.assertEqual(records.split_quote_no("QT0000000001"), ("QT0000000001", 0))

    def test_validate_flags_bad_totals(self):
        w = records.validate_record(records.normalize_record(MOCK[b"D"]))
        self.assertTrue(any("ยอดไม่ลงตัว" in x for x in w))
        self.assertEqual(records.validate_record(records.normalize_record(MOCK[b"B"])), [])

    def test_week_bucket(self):
        mon, label = week_bucket(date(2026, 11, 8))        # a Sunday belongs to the week starting Mon 2026-11-02
        self.assertEqual(mon, date(2026, 11, 2))
        self.assertTrue(label.startswith("2026-W45"))


class PipelineTests(unittest.TestCase):
    def test_full_run_cache_dedupe_and_excel(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            cfg = make_cfg(tmp)
            put(cfg, "a0.pdf", b"A-rev0")
            put(cfg, "a1 Rev.1.pdf", b"A-rev1")
            put(cfg, "sub/b.pdf", b"B")
            put(cfg, "c.pdf", b"C")
            put(cfg, "d.pdf", b"D")
            (cfg.source_folder / "notes.txt").write_text("ignored")
            (cfg.source_folder / "~$lock.pdf").write_bytes(b"junk")

            client = FakeClient()
            claude = ClaudeExtractor("m", client=client)
            entries, issues, n_pdf, stats = process(cfg, claude, log=lambda *_: None, today=date(2026, 11, 9))
            self.assertEqual((n_pdf, stats["claude"], stats["cached"], client.calls), (5, 5, 0, 5))
            # tool_choice is auto (forced tool use is rejected by some models); PDF goes as a document block
            self.assertEqual(client.last_kwargs["tool_choice"], {"type": "auto"})
            self.assertEqual(client.last_kwargs["messages"][0]["content"][0]["type"], "document")
            # Rev.0 dropped in favour of Rev.1 -> 4 kept
            self.assertEqual(len(entries), 4)
            self.assertTrue(any("มีฉบับแก้ไขใหม่กว่า" in m for m in issues))
            self.assertTrue(any("ยอดไม่ลงตัว" in m for m in issues))

            sheets = write_workbook(cfg.output_file, cfg.companies, entries, issues, n_pdf)
            self.assertEqual(sheets, ["Company A", "Company B", "บริษัทที่ 3", "ไม่ระบุบริษัท", "สรุป"])
            wb = load_workbook(cfg.output_file)
            g = wb["Company A"]
            self.assertEqual(g["B2"].value, "QT0000000001 Rev.1")
            self.assertEqual(g["B3"].value, "QT0000000002")
            self.assertEqual(g["G2"].value, 10700)
            self.assertEqual(g["E5"].value, 13800)             # subtotal sum (row 5 = total row)
            self.assertEqual(g["G5"].value, 14766)
            self.assertEqual(wb["Company B"]["A2"].value.date(), date(2026, 11, 3))
            self.assertEqual(wb["Company B"]["G2"].value, 45475)
            self.assertEqual(wb["ไม่ระบุบริษัท"]["B2"].value, "X-1")
            self.assertIn("ยอดไม่ลงตัว", wb["ไม่ระบุบริษัท"]["J2"].value)
            s = {}
            for r in wb["สรุป"].iter_rows(values_only=True):
                if r[0]:
                    s.setdefault(r[0], r)   # first occurrence wins ("รวมทั้งหมด" appears in 3 tables)
            self.assertEqual(s["Company A"][1:5], (2, 13800, 966, 14766))
            self.assertEqual(s["Company B"][1:5], (1, 42500, 2975, 45475))
            self.assertEqual(s["รวมทั้งหมด"][1:5], (4, 56300 + 1000, 3941 + 70, 60241 + 1100))
            self.assertEqual(s["2026-11"][1:3], (4066, 45475))   # month bucket: company A, company B
            self.assertIn("2026-W46 (09/11-15/11)", s)

            # Second run: only d.pdf (its record has validation warnings) is read once more;
            # everything else comes from the cache (by hash). Result identical.
            client.calls = 0
            entries2, _, _, stats2 = process(cfg, ClaudeExtractor("m", client=client), log=lambda *_: None)
            self.assertEqual((client.calls, stats2["cached"]), (1, 4))
            self.assertEqual(len(entries2), 4)

            # Third run: d.pdf has used its 2 attempts, so nothing is sent any more.
            client.calls = 0
            _, _, _, stats3 = process(cfg, ClaudeExtractor("m", client=client), log=lambda *_: None)
            self.assertEqual((client.calls, stats3["cached"]), (0, 5))

            # A new PDF with the same bytes as sub/b.pdf -> same hash -> cached + deduped.
            put(cfg, "new.pdf", b"B")
            process(cfg, ClaudeExtractor("m", client=client), log=lambda *_: None)
            self.assertEqual(client.calls, 0)

    def test_first_seen_is_kept_across_runs(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            put(cfg, "b.pdf", b"B")
            claude = ClaudeExtractor("m", client=FakeClient())
            e1, *_ = process(cfg, claude, log=lambda *_: None, today=date(2026, 11, 2))
            e2, *_ = process(cfg, claude, log=lambda *_: None, today=date(2026, 11, 9))
            self.assertEqual(e2[0].first_seen, "2026-11-02")

    def test_claude_auth_failure_falls_back_instead_of_crashing(self):
        class Boom(ClaudeExtractor):
            def extract(self, path):
                raise ClaudeUnavailable("API key ใช้ไม่ได้")
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            put(cfg, "junk.pdf", b"not a real pdf")           # text fallback also fails -> reported, no crash
            entries, issues, n, stats = process(cfg, Boom("m", client=FakeClient()), log=lambda *_: None)
            self.assertEqual(stats["failed"], 1)
            self.assertTrue(any("Claude ใช้ไม่ได้" in m for m in issues))

    def test_no_key_raises_unavailable(self):
        with self.assertRaises(ClaudeUnavailable):
            ClaudeExtractor("m", api_key=None)

    # ------------------------------------------------------------ unreadable source folder
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file permissions")
    def test_unreadable_source_folder_gives_thai_message(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            cfg = make_cfg(tmp)
            cfg_file = tmp / "c.json"
            cfg_file.write_text(json.dumps({
                "source_folder": str(cfg.source_folder), "extractor": "text", "cache_file": str(tmp / "c" / "c.json"),
                "companies": [{"sheet_name": "A"}]}), encoding="utf-8")
            os.chmod(cfg.source_folder, 0)
            try:
                with self.assertRaises(SourceUnreadable) as cm:
                    process(cfg, None, log=lambda *_: None)
                self.assertIn("ย้ายโฟลเดอร์", str(cm.exception))
                err = io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    rc = qsum_main(["--config", str(cfg_file)])
                self.assertEqual(rc, 1)                      # clean exit code, no traceback
                self.assertIn("อ่านโฟลเดอร์", err.getvalue())
            finally:
                os.chmod(cfg.source_folder, 0o755)

    # ------------------------------------------------------------ Claude errors are reported
    def test_not_found_model_is_unavailable_and_named(self):
        ex = ClaudeExtractor("claude-typo-1", client=FakeClient(exc=sdk_error("NotFoundError", "model: claude-typo-1")))
        with tempfile.TemporaryDirectory() as t:
            p = put(make_cfg(Path(t)), "a.pdf", b"A-rev0")
            with self.assertRaises(ClaudeUnavailable) as cm:
                ex.extract(p)
            self.assertIn("claude-typo-1", str(cm.exception))

    def test_billing_bad_request_is_unavailable_but_other_400_is_per_file(self):
        with tempfile.TemporaryDirectory() as t:
            p = put(make_cfg(Path(t)), "a.pdf", b"A-rev0")
            with self.assertRaises(ClaudeUnavailable):
                ClaudeExtractor("m", client=FakeClient(exc=sdk_error("BadRequestError", "Your credit balance is too low"))).extract(p)
            with self.assertRaises(ExtractionError):
                ClaudeExtractor("m", client=FakeClient(exc=sdk_error("BadRequestError", "invalid pdf"))).extract(p)

    def test_generic_model_word_in_400_is_per_file_not_run_wide(self):
        # a bare "model"/"plan" in a 400 message must not switch Claude off for the whole run.
        with tempfile.TemporaryDirectory() as t:
            p = put(make_cfg(Path(t)), "a.pdf", b"A-rev0")
            for msg in ("could not parse the model output", "pdf page plan is invalid"):
                with self.assertRaises(ExtractionError):
                    ClaudeExtractor("m", client=FakeClient(exc=sdk_error("BadRequestError", msg))).extract(p)

    def test_auto_mode_reports_claude_errors_and_does_not_say_add_a_key(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            put(cfg, "a.pdf", b"A-rev0")
            # 404 -> Claude unavailable for the run; text fallback fails on the fake bytes.
            claude = ClaudeExtractor("claude-typo-1", client=FakeClient(exc=sdk_error("NotFoundError")))
            _, issues, _, _ = process(cfg, claude, log=lambda *_: None)
            self.assertTrue(any("Claude ใช้ไม่ได้" in m and "claude-typo-1" in m for m in issues))
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            put(cfg, "a.pdf", b"A-rev0")
            # a per-file failure (500) in auto mode must still show up in `issues` as one summary line.
            claude = ClaudeExtractor("m", client=FakeClient(exc=sdk_error("InternalServerError")))
            _, issues, _, _ = process(cfg, claude, log=lambda *_: None)
            self.assertTrue(any(m.startswith("Claude อ่านไม่สำเร็จ 1 ไฟล์") for m in issues))

    def test_text_fallback_note_depends_on_whether_a_key_exists(self):
        raw = {"quote_no": "QT1", "quote_date": "2026-01-05", "seller_name": "บริษัท ตัวอย่าง เอ จำกัด",
               "seller_tax_id": "0100000000001", "customer_name": "x", "items": [],
               "subtotal": 100.0, "vat": 7.0, "total": 107.0}
        from unittest import mock
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            put(cfg, "a.pdf", b"whatever")
            with mock.patch("qsum.main.extract_text", return_value=raw):
                _, issues, _, _ = process(cfg, None, log=lambda *_: None)                       # no key
                self.assertTrue(any("เมื่อตั้งค่า Claude ได้" in m for m in issues))
                claude = ClaudeExtractor("m", client=FakeClient(exc=sdk_error("NotFoundError")))
                _, issues, _, _ = process(cfg, claude, log=lambda *_: None, reextract=True)    # key exists
                self.assertFalse(any("เมื่อตั้งค่า Claude ได้" in m for m in issues))

    # ------------------------------------------------------------ truncation / incomplete results
    def test_max_tokens_stop_reason_is_rejected_and_budget_is_large(self):
        client = FakeClient(stop_reason="max_tokens")
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            p = put(cfg, "b.pdf", b"B")
            with self.assertRaises(ExtractionError):
                ClaudeExtractor("m", client=client).extract(p)
            self.assertGreaterEqual(client.last_kwargs["max_tokens"], 8192)
            # and nothing is cached for it
            process(cfg, ClaudeExtractor("m", client=client), log=lambda *_: None)
            self.assertEqual(Cache(cfg.cache_file).entries, {})

    def test_warned_claude_result_is_reread_once_then_kept(self):
        bad = dict(MOCK[b"B"], total=1.0)                     # totals do not add up
        good = MOCK[b"B"]
        client = FakeClient(seq={b"B": [bad, good]})
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            put(cfg, "b.pdf", b"B")
            claude = ClaudeExtractor("m", client=client)
            e1, *_ = process(cfg, claude, log=lambda *_: None)
            self.assertTrue(e1[0].warnings)                   # first read is flagged
            e2, *_ = process(cfg, claude, log=lambda *_: None)
            self.assertEqual(client.calls, 2)                 # re-read -> good result
            self.assertEqual(e2[0].warnings, [])
            e3, *_ = process(cfg, claude, log=lambda *_: None)
            self.assertEqual(client.calls, 2)                 # now cached for good

    def test_failed_reread_keeps_the_earlier_claude_result(self):
        bad = dict(MOCK[b"B"], total=1.0)
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            put(cfg, "b.pdf", b"B")
            process(cfg, ClaudeExtractor("m", client=FakeClient(seq={b"B": [bad]})), log=lambda *_: None)
            # re-read fails with a per-file error: the Claude record must not be replaced by pdftotext's
            e, *_ = process(cfg, ClaudeExtractor("m", client=FakeClient(exc=sdk_error("InternalServerError"))), log=lambda *_: None)
            self.assertEqual((len(e), e[0].method), (1, "claude"))

    # ------------------------------------------------------------ tool_choice auto, thinking blocks, breaker
    @staticmethod
    def _tool_block(data=None):
        return SimpleNamespace(type="tool_use", name=TOOL["name"], input=data or MOCK[b"B"])

    def test_request_never_forces_tool_choice_and_sends_no_thinking_params(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as t:
            ClaudeExtractor("m", client=client).extract(put(make_cfg(Path(t)), "b.pdf", b"B"))
        self.assertNotEqual(client.last_kwargs["tool_choice"].get("type"), "tool")
        self.assertEqual(client.last_kwargs["tool_choice"], {"type": "auto"})
        self.assertNotIn("thinking", client.last_kwargs)
        self.assertNotIn("temperature", client.last_kwargs)
        self.assertGreaterEqual(client.last_kwargs["max_tokens"], 16000)

    def test_thinking_block_before_tool_use_is_parsed(self):
        think = SimpleNamespace(type="thinking", thinking="...")
        text = SimpleNamespace(type="text", text="ok")
        client = FakeClient(replies=[[think, text, self._tool_block()]])
        with tempfile.TemporaryDirectory() as t:
            out = ClaudeExtractor("m", client=client).extract(put(make_cfg(Path(t)), "b.pdf", b"B"))
        self.assertEqual(out["quote_no"], "QT0000000002")
        self.assertEqual(client.calls, 1)

    def test_text_only_reply_retries_once_then_extraction_error_and_is_not_cached(self):
        client = FakeClient(replies=[[SimpleNamespace(type="text", text="sorry")]])
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            p = put(cfg, "b.pdf", b"B")
            with self.assertRaises(ExtractionError):
                ClaudeExtractor("m", client=client).extract(p)
            self.assertEqual(client.calls, 2)                  # 1 try + 1 retry
            process(cfg, ClaudeExtractor("m", client=client), log=lambda *_: None)
            self.assertEqual(Cache(cfg.cache_file).entries, {})

    def test_retry_recovers_when_second_reply_has_the_tool_call(self):
        client = FakeClient(replies=[[SimpleNamespace(type="text", text="hm")], [self._tool_block()]])
        with tempfile.TemporaryDirectory() as t:
            out = ClaudeExtractor("m", client=client).extract(put(make_cfg(Path(t)), "b.pdf", b"B"))
        self.assertEqual((out["quote_no"], client.calls), ("QT0000000002", 2))

    def test_tool_choice_not_supported_400_is_unavailable_and_names_model_and_key(self):
        msg = "tool_choice: forced tool use is not supported for this model"
        ex = ClaudeExtractor("claude-x", client=FakeClient(exc=sdk_error("BadRequestError", msg)))
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ClaudeUnavailable) as cm:
                ex.extract(put(make_cfg(Path(t)), "b.pdf", b"B"))
        self.assertIn("claude-x", str(cm.exception))
        self.assertIn("claude_model", str(cm.exception))

    def test_billing_error_type_in_body_is_unavailable(self):
        e = sdk_error("BadRequestError", "something")
        e.body = {"error": {"type": "billing_error", "message": "x"}}
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ClaudeUnavailable):
                ClaudeExtractor("m", client=FakeClient(exc=e)).extract(put(make_cfg(Path(t)), "b.pdf", b"B"))

    def test_three_empty_files_in_a_row_trip_the_breaker_and_reason_reaches_summary(self):
        client = FakeClient(replies=[[SimpleNamespace(type="text", text="no")]])
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            for i, m in enumerate((b"A-rev0", b"B", b"C", b"D")):
                put(cfg, "f%d.pdf" % i, m)
            _, issues, _, _ = process(cfg, ClaudeExtractor("claude-x", client=client), log=lambda *_: None)
        # files 1-2: per-file ExtractionError (2 calls each); file 3 trips the breaker -> file 4 never sent
        self.assertEqual(client.calls, 6)
        self.assertTrue(any("Claude ใช้ไม่ได้" in m and "claude-x" in m and "claude_model" in m for m in issues))

    def test_success_resets_the_empty_counter(self):
        empty = [SimpleNamespace(type="text", text="no")]
        client = FakeClient(replies=[empty, empty, [self._tool_block()], empty, empty])
        ex = ClaudeExtractor("m", client=client)
        with tempfile.TemporaryDirectory() as t:
            p = put(make_cfg(Path(t)), "b.pdf", b"B")
            with self.assertRaises(ExtractionError):
                ex.extract(p)                                  # empty, empty
            ex.extract(p)                                      # success -> counter reset
            self.assertEqual(ex.empty_in_a_row, 0)

    def test_malformed_tool_input_is_rejected_and_not_cached(self):
        bad_inputs = [
            {k: v for k, v in MOCK[b"B"].items() if k != "total"},     # missing key
            dict(MOCK[b"B"], items="not a list"),                       # items not a list
            dict(MOCK[b"B"], total=[1]),                                # number field is a list
            dict(MOCK[b"B"], quote_no=123),                             # string field is a number
        ]
        for bad in bad_inputs:
            client = FakeClient(replies=[[self._tool_block(bad)]])
            with tempfile.TemporaryDirectory() as t:
                cfg = make_cfg(Path(t))
                p = put(cfg, "b.pdf", b"B")
                with self.assertRaises(ExtractionError):
                    ClaudeExtractor("m", client=client).extract(p)
                process(cfg, ClaudeExtractor("m", client=client), log=lambda *_: None)
                self.assertEqual(Cache(cfg.cache_file).entries, {})

    # ------------------------------------------------------------ is_dir() raising OSError
    def test_is_dir_oserror_gives_thai_message_not_traceback(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            cfg_file = tmp / "c.json"
            cfg_file.write_text(json.dumps({
                "source_folder": str(tmp / "src"), "extractor": "text", "cache_file": str(tmp / "c" / "c.json"),
                "companies": [{"sheet_name": "A"}]}), encoding="utf-8")
            err = io.StringIO()
            with mock.patch.object(Path, "is_dir", side_effect=PermissionError(1, "Operation not permitted")), \
                    contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                rc = qsum_main(["--config", str(cfg_file)])
            self.assertEqual(rc, 1)
            self.assertIn("อ่านโฟลเดอร์", err.getvalue())

    # ------------------------------------------------------------ same number, different seller key
    def test_same_quote_base_with_different_seller_keys_warns(self):
        a = Entry("a.pdf", "1", records.normalize_record(MOCK[b"A-rev1"]), "claude", "2026-01-01", company=COMPANY_A)
        no_tax = dict(MOCK[b"A-rev0"], seller_tax_id=None, seller_name="บริษัท อื่น จำกัด")
        b = Entry("b.pdf", "2", records.normalize_record(no_tax), "text", "2026-01-01", company=None)
        kept, dropped = records.dedupe([a, b])
        self.assertEqual((len(kept), len(dropped)), (2, 0))   # not merged ...
        self.assertTrue(all(any("ซ้ำกับผู้ขายอื่น" in w for w in e.warnings) for e in kept))   # ... but flagged

    def test_config_validation(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "c.json"
            p.write_text(json.dumps({"source_folder": t, "companies": [{"sheet_name": "สรุป"}]}), encoding="utf-8")
            from qsum.config import ConfigError
            with self.assertRaises(ConfigError):
                load_config(str(p))


class RecordingReporter:
    """Collects every reporter call of process() so the tests can check order and arguments."""

    def __init__(self):
        self.events = []

    def begin(self, total):
        self.events.append(("begin", total))

    def file_started(self, rel):
        self.events.append(("started", rel))

    def file_done(self, rel, sha, outcome, message):
        self.events.append(("done", rel, sha, outcome, message))


class ReporterTests(unittest.TestCase):
    """process(reporter=...): the contract the desktop app's progress display relies on."""

    def test_events_and_unchanged_return_value(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            pa, pb, pc = put(cfg, "a.pdf", b"A-rev0"), put(cfg, "b.pdf", b"B"), put(cfg, "c.pdf", b"B")   # c = copy of b
            put(cfg, "notes.txt", b"ignored")
            client = FakeClient()
            rep = RecordingReporter()
            kept, issues, n_pdf, stats = process(cfg, ClaudeExtractor("m", client=client), log=lambda *_: None, reporter=rep)
            ev = rep.events
            self.assertEqual(ev[0], ("begin", 3))
            self.assertEqual([e[:2] for e in ev[1:]],
                             [("started", "a.pdf"), ("done", "a.pdf"), ("started", "b.pdf"), ("done", "b.pdf"),
                              ("started", "c.pdf"), ("done", "c.pdf")])
            self.assertEqual([e[2:] for e in ev if e[0] == "done"],
                             [(sha256_file(pa), "claude", ""), (sha256_file(pb), "claude", ""),
                              (sha256_file(pc), "cached", "")])       # c has the same bytes as b: cache hit in this run
            # the same run without a reporter returns the same tuple shape and numbers
            (cfg.cache_file).unlink()
            plain = process(cfg, ClaudeExtractor("m", client=FakeClient()), log=lambda *_: None)
            self.assertEqual((len(plain[0]), plain[2], plain[3]), (len(kept), n_pdf, stats))
            self.assertEqual(plain[1], issues)

    def test_cached_text_and_failed_outcomes(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            cfg.extractor = "text"
            put(cfg, "ok.pdf", b"B")
            put(cfg, "bad.pdf", b"BAD")
            raw = MOCK[b"B"]

            def fake_text(path):
                if path.name == "bad.pdf":
                    raise ExtractionError("PDF เสีย")
                return raw
            rep = RecordingReporter()
            with mock.patch("qsum.main.extract_text", side_effect=fake_text):
                _, _, _, stats = process(cfg, None, log=lambda *_: None, reporter=rep)
                rep2 = RecordingReporter()
                process(cfg, None, log=lambda *_: None, reporter=rep2)
            outcomes = {e[1]: e[3:] for e in rep.events if e[0] == "done"}
            self.assertEqual(outcomes["ok.pdf"], ("text", ""))
            self.assertEqual(outcomes["bad.pdf"][0], "failed")
            self.assertIn("PDF เสีย", outcomes["bad.pdf"][1])
            self.assertEqual(stats["failed"], 1)
            outcomes2 = {e[1]: e[3] for e in rep2.events if e[0] == "done"}
            self.assertEqual(outcomes2, {"ok.pdf": "cached", "bad.pdf": "failed"})

    def test_hash_error_reports_failed_with_empty_sha(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            cfg.extractor = "text"
            put(cfg, "x.pdf", b"B")
            rep = RecordingReporter()
            with mock.patch("qsum.main.sha256_file", side_effect=PermissionError(1, "Operation not permitted")):
                process(cfg, None, log=lambda *_: None, reporter=rep)
            self.assertEqual(rep.events[-1][2:4], ("", "failed"))

    def test_claude_failure_with_strict_extractor_is_failed_with_the_reason(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            cfg.extractor = "claude"
            put(cfg, "x.pdf", b"B")
            claude = mock.Mock(model_label="m")
            claude.extract.side_effect = ExtractionError("Claude หมดเวลา")
            rep = RecordingReporter()
            process(cfg, claude, log=lambda *_: None, reporter=rep)
            self.assertEqual(rep.events[-1][3:], ("failed", "Claude หมดเวลา"))


class StateIntegrationTests(unittest.TestCase):
    """qsum.main.main(): run lock, exit code 3, state.json (current / last_run / files), QS_TRIGGER.

    Everything uses extractor "text" with extract_text mocked, so no Claude / Keychain is involved, and the
    cache + state + lock live in a temp folder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "src").mkdir()
        self.cfg_path = self.tmp / "config.json"
        self.write_cfg()
        self.state_path = self.tmp / "app" / "state.json"
        self._env = mock.patch.dict(os.environ, {})
        self._env.start()
        os.environ.pop("QS_TRIGGER", None)
        os.environ.pop("QS_CONFIG", None)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def write_cfg(self, **extra):
        data = {"source_folder": str(self.tmp / "src"), "output_file": str(self.tmp / "out.xlsx"),
                "cache_file": str(self.tmp / "app" / "cache.json"), "extractor": "text",
                "companies": [{"sheet_name": "Company A", "tax_id": "0100000000001"}]}
        data.update(extra)
        self.cfg_path.write_text(json.dumps(data), encoding="utf-8")

    def run_main(self, *extra, text=None):
        out, err = io.StringIO(), io.StringIO()
        text = text or (lambda p: MOCK[b"B"])
        with mock.patch("qsum.main.extract_text", side_effect=text), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = qsum_main(["--config", str(self.cfg_path)] + list(extra))
        return rc, out.getvalue(), err.getvalue()

    def pdf(self, name, content=b"B"):
        p = self.tmp / "src" / name
        p.write_bytes(content)
        return p

    def test_config_defaults_for_state_and_lock_files(self):
        cfg = load_config(str(self.cfg_path))
        self.assertEqual(cfg.state_file, self.tmp / "app" / "state.json")
        self.assertEqual(cfg.lock_file, self.tmp / "app" / "run.lock")
        self.write_cfg(state_file=str(self.tmp / "elsewhere" / "s.json"))
        cfg = load_config(str(self.cfg_path))
        self.assertEqual((cfg.state_file, cfg.lock_file), (self.tmp / "elsewhere" / "s.json", self.tmp / "elsewhere" / "run.lock"))
        self.assertEqual(make_cfg(self.tmp).state_file, self.tmp / "cache" / "state.json")     # Config built by hand

    def test_successful_run_writes_last_run_and_files(self):
        pa = self.pdf("a.pdf", b"B")
        rc, out, _ = self.run_main()
        self.assertEqual(rc, 0)
        s = read_state(self.state_path)
        self.assertIsNone(s["current"])
        lr = s["last_run"]
        self.assertEqual((lr["exit_code"], lr["fatal"], lr["trigger"], lr["n_pdf"], lr["excel_written"]), (0, None, "manual", 1, True))
        self.assertEqual(lr["stats"], {"cached": 0, "claude": 0, "text": 1, "failed": 0})
        self.assertEqual(lr["output_file"], str(self.tmp / "out.xlsx"))
        self.assertEqual(s["files"][sha256_file(pa)]["outcome"], "text")
        self.assertEqual(s["files"][sha256_file(pa)]["rel"], "a.pdf")
        # second run: everything comes from the cache
        rc, _, _ = self.run_main()
        s = read_state(self.state_path)
        self.assertEqual(s["last_run"]["stats"]["cached"], 1)
        self.assertEqual(s["files"][sha256_file(pa)]["outcome"], "cached")
        probe = RunLock(self.tmp / "app" / "run.lock")
        self.assertTrue(probe.acquire())                                    # the lock was released at the end
        probe.release()

    def test_trigger_comes_from_env(self):
        self.pdf("a.pdf")
        for value, expected in (("schedule", "schedule"), ("app", "app"), ("manual", "manual"), ("junk", "manual")):
            with mock.patch.dict(os.environ, {"QS_TRIGGER": value}):
                self.run_main()
            self.assertEqual(read_state(self.state_path)["last_run"]["trigger"], expected, value)
        self.run_main()                                                    # unset -> manual
        self.assertEqual(read_state(self.state_path)["last_run"]["trigger"], "manual")

    def test_exit_2_when_a_pdf_fails_and_the_file_is_recorded_as_failed(self):
        self.pdf("ok.pdf", b"B")
        bad = self.pdf("bad.pdf", b"BAD")

        def text(p):
            if p.name == "bad.pdf":
                raise ExtractionError("PDF เสีย")
            return MOCK[b"B"]
        rc, _, _ = self.run_main(text=text)
        self.assertEqual(rc, 2)
        s = read_state(self.state_path)
        self.assertEqual(s["last_run"]["exit_code"], 2)
        self.assertTrue(s["last_run"]["excel_written"])
        self.assertEqual(s["last_run"]["stats"]["failed"], 1)
        self.assertEqual(s["files"][sha256_file(bad)]["outcome"], "failed")
        self.assertIn("PDF เสีย", s["files"][sha256_file(bad)]["message"])

    def test_current_is_written_at_start_and_updated_per_file(self):
        self.pdf("a.pdf", b"B")
        self.pdf("b.pdf", b"B2")
        seen = []

        def text(p):
            c = read_state(self.state_path)["current"]
            seen.append((p.name, c["trigger"], c["pid"], c["total"], c["done"], c["current_file"]))
            return MOCK[b"B"]
        with mock.patch.dict(os.environ, {"QS_TRIGGER": "app"}):
            self.run_main(text=text)
        self.assertEqual(seen, [("a.pdf", "app", os.getpid(), 2, 0, "a.pdf"), ("b.pdf", "app", os.getpid(), 2, 1, "b.pdf")])
        self.assertIsNone(read_state(self.state_path)["current"])

    def test_lock_is_held_during_the_run_and_free_afterwards(self):
        self.pdf("a.pdf", b"B")
        from qsum.state import is_running
        during = []

        def text(p):
            during.append(is_running(self.tmp / "app" / "run.lock"))
            return MOCK[b"B"]
        self.run_main(text=text)
        self.assertEqual(during, [True])
        self.assertFalse(is_running(self.tmp / "app" / "run.lock"))

    def test_busy_lock_exits_3_with_thai_message_and_touches_nothing(self):
        self.pdf("a.pdf", b"B")
        self.run_main()                                                     # one finished run, to have a state to compare
        before = self.state_path.read_text(encoding="utf-8")
        cache_before = (self.tmp / "app" / "cache.json").read_text(encoding="utf-8")
        out_mtime = (self.tmp / "out.xlsx").stat().st_mtime_ns
        holder = RunLock(self.tmp / "app" / "run.lock")
        self.assertTrue(holder.acquire())
        try:
            rc, out, err = self.run_main()
        finally:
            holder.release()
        self.assertEqual(rc, 3)
        self.assertIn("กำลังทำงานอยู่แล้ว", err)
        self.assertNotIn("qsum 1.", out)                                    # never got to the real run
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)
        self.assertEqual((self.tmp / "app" / "cache.json").read_text(encoding="utf-8"), cache_before)
        self.assertEqual((self.tmp / "out.xlsx").stat().st_mtime_ns, out_mtime)
        rc, _, _ = self.run_main()                                          # and it works again once the lock is free
        self.assertEqual(rc, 0)

    def test_print_flags_do_not_take_the_lock_or_write_state(self):
        holder = RunLock(self.tmp / "app" / "run.lock")
        self.assertTrue(holder.acquire())
        try:
            for flag in ("--print-output-path", "--print-keychain-service", "--print-claude-token-service",
                         "--print-claude-config-dir"):
                rc, out, _ = self.run_main(flag)
                self.assertEqual(rc, 0, flag)
                self.assertTrue(out.strip(), flag)
        finally:
            holder.release()
        self.assertFalse(self.state_path.exists())                          # no state written by the --print-* calls

    def test_config_error_writes_no_state(self):
        self.cfg_path.write_text("{broken", encoding="utf-8")
        rc, _, err = self.run_main()
        self.assertEqual(rc, 1)
        self.assertIn("ผิดพลาด", err)
        self.assertFalse((self.tmp / "app").exists())

    def test_missing_source_folder_is_fatal_in_last_run(self):
        shutil_rmtree(self.tmp / "src")
        rc, _, err = self.run_main()
        self.assertEqual(rc, 1)
        lr = read_state(self.state_path)["last_run"]
        self.assertEqual((lr["exit_code"], lr["excel_written"]), (1, False))
        self.assertIn("ไม่พบโฟลเดอร์", lr["fatal"])
        self.assertIn("ไม่พบโฟลเดอร์", err)
        self.assertIsNone(read_state(self.state_path)["current"])

    def test_fatal_before_listing_keeps_the_previous_files(self):
        self.pdf("a.pdf", b"B")
        self.run_main()
        shutil_rmtree(self.tmp / "src")
        self.run_main()
        s = read_state(self.state_path)
        self.assertEqual(s["last_run"]["exit_code"], 1)
        self.assertEqual(len(s["files"]), 1)                                # the file list of the earlier good run stays
        self.assertEqual(next(iter(s["files"].values()))["rel"], "a.pdf")

    def test_strict_claude_unavailable_is_fatal_in_last_run(self):
        self.write_cfg(extractor="claude")
        with mock.patch("qsum.main.get_oauth_token", return_value=None), \
                mock.patch("qsum.main.resolve_claude_path", return_value=None):
            rc, _, _ = self.run_main()
        self.assertEqual(rc, 1)
        lr = read_state(self.state_path)["last_run"]
        self.assertEqual(lr["exit_code"], 1)
        self.assertTrue(lr["fatal"])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file permissions")
    def test_unreadable_source_folder_is_fatal_in_last_run(self):
        os.chmod(self.tmp / "src", 0)
        try:
            rc, _, _ = self.run_main()
        finally:
            os.chmod(self.tmp / "src", 0o755)
        self.assertEqual(rc, 1)
        self.assertIn("อ่านโฟลเดอร์", read_state(self.state_path)["last_run"]["fatal"])

    def test_excel_write_failure_is_fatal_but_keeps_stats_and_files(self):
        pa = self.pdf("a.pdf", b"B")
        with mock.patch("qsum.main.write_workbook", side_effect=PermissionError(13, "Permission denied")):
            rc, _, _ = self.run_main()
        self.assertEqual(rc, 1)
        s = read_state(self.state_path)
        lr = s["last_run"]
        self.assertEqual((lr["exit_code"], lr["excel_written"], lr["n_pdf"]), (1, False, 1))
        self.assertIn("เขียนไฟล์ Excel ไม่ได้", lr["fatal"])
        self.assertEqual(lr["stats"]["text"], 1)
        self.assertIn(sha256_file(pa), s["files"])

    def test_unexpected_exception_records_fatal_releases_lock_and_propagates(self):
        from qsum.state import is_running
        self.pdf("a.pdf", b"B")
        with mock.patch("qsum.main.process", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.run_main()
        s = read_state(self.state_path)
        self.assertEqual(s["last_run"]["exit_code"], 1)
        self.assertIn("boom", s["last_run"]["fatal"])
        self.assertIsNone(s["current"])
        self.assertFalse(is_running(self.tmp / "app" / "run.lock"))

    def test_keyboard_interrupt_records_a_thai_fatal_and_propagates(self):
        # Ctrl-C in Terminal must not leave "KeyboardInterrupt: " (English, empty) as the app's last-run problem.
        from qsum.state import is_running
        self.pdf("a.pdf", b"B")
        with mock.patch("qsum.main.process", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.run_main()
        s = read_state(self.state_path)
        self.assertEqual(s["last_run"]["exit_code"], 1)
        self.assertEqual(s["last_run"]["fatal"], "ถูกหยุดกลางทาง (กด Ctrl-C)")
        self.assertIsNone(s["current"])
        self.assertFalse(is_running(self.tmp / "app" / "run.lock"))

    def test_flock_not_supported_warns_and_runs_instead_of_claiming_busy(self):
        # an flock error that is not "held elsewhere" must not give exit 3.
        import errno
        self.pdf("a.pdf", b"B")
        with mock.patch("qsum.state.fcntl.flock", side_effect=OSError(errno.ENOLCK, "No locks available")):
            rc, _, err = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn("คำเตือน", err)
        self.assertNotIn("กำลังทำงานอยู่แล้ว", err)
        self.assertTrue((self.tmp / "out.xlsx").exists())

    def test_unwritable_state_folder_does_not_stop_the_run(self):
        # state_file in a place that cannot be created: the run (and its exit code) must not depend on it
        blocker = self.tmp / "blocker"
        blocker.write_text("file", encoding="utf-8")
        self.write_cfg(state_file=str(blocker / "sub" / "state.json"))
        self.pdf("a.pdf", b"B")
        rc, out, err = self.run_main()
        self.assertEqual(rc, 0)
        self.assertTrue((self.tmp / "out.xlsx").exists())
        self.assertIn("คำเตือน", err)                                         # lock file could not be created: warned


def shutil_rmtree(path):
    import shutil
    shutil.rmtree(str(path))


if __name__ == "__main__":
    unittest.main()
