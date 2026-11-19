"""stats.json: what the weekly summary reads."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("X402T_DATA_DIR", tempfile.mkdtemp())

from x402trust import config, stats  # noqa: E402


class Stats(unittest.TestCase):
    def setUp(self):
        self.old = (config.DATA_DIR, config.REPORT_DIR)
        config.DATA_DIR = Path(tempfile.mkdtemp())
        config.REPORT_DIR = config.DATA_DIR / "reports"

    def tearDown(self):
        config.DATA_DIR, config.REPORT_DIR = self.old

    def test_writes_api_npm_and_plans(self):
        def fake(url):
            if url.endswith("/v1/stats"):
                return {"last_7_days": {"checks": 4, "plans": 0, "usd": 0.04, "payers": 2}, "days": []}
            return {"downloads": 12 if "last-week" in url else 40}
        with mock.patch.object(stats, "_get", fake):
            stats.write(log=lambda *a: None)
        d = json.loads((config.REPORT_DIR / "stats.json").read_text())
        self.assertEqual(d["api"]["last_7_days"]["checks"], 4)
        self.assertEqual(d["guard_downloads"], {"last-week": 12, "last-month": 40})
        self.assertEqual(d["seller_plans_active"], 0)

    def test_unreachable_sources_dont_break_it(self):
        with mock.patch.object(stats, "_get", side_effect=OSError("offline")):
            d = stats.write(log=lambda *a: None)
        self.assertIsNone(d["api"])
        self.assertEqual(d["guard_downloads"], {"last-week": None, "last-month": None})


if __name__ == "__main__":
    unittest.main()
