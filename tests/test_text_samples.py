"""pdftotext fallback against real sample quotations (skipped if they are not on this Mac).

The PDFs and their expected values contain customer data, so NEITHER is in the repo:
  - PDFs: a folder pointed to by QS_SAMPLES (default ~/Downloads)
  - expected values: tests/samples.local.json (git-ignored; override the path with QS_SAMPLES_EXPECT).
    Format: see tests/samples.local.example.json (fictional values).
The test skips when the expectations file or the PDFs are missing.
"""
import json
import os
import shutil
import unittest
from pathlib import Path

from qsum import records
from qsum.extract_text import extract

SAMPLES = Path(os.environ.get("QS_SAMPLES", str(Path.home() / "Downloads")))
EXPECT_FILE = Path(os.environ.get("QS_SAMPLES_EXPECT", str(Path(__file__).parent / "samples.local.json")))


@unittest.skipUnless(shutil.which("pdftotext") or Path("/opt/homebrew/bin/pdftotext").exists(), "pdftotext not installed")
class TextSamples(unittest.TestCase):
    def test_samples(self):
        if not EXPECT_FILE.exists():
            self.skipTest("no expectations file (%s)" % EXPECT_FILE)
        expect = json.loads(EXPECT_FILE.read_text(encoding="utf-8"))
        found = 0
        for name, x in expect.items():
            p = SAMPLES / name
            if not p.exists():
                continue
            found += 1
            with self.subTest(name):
                r = records.normalize_record(extract(p))
                self.assertEqual((r["quote_no"], r["date"], r["seller_tax_id"], r["customer"]),
                                 (x["quote_no"], x["date"], x["seller_tax_id"], x["customer"]))
                self.assertEqual((r["subtotal"], r["vat"], r["total"]), (x["subtotal"], x["vat"], x["total"]))
                self.assertEqual(len(r["items"]), x["n_items"])
                self.assertEqual(records.validate_record(r), [])
        if not found:
            self.skipTest("sample PDFs not found in %s" % SAMPLES)


if __name__ == "__main__":
    unittest.main()
