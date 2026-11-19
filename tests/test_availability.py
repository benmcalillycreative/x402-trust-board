"""30-day availability (Coinbase's 99% bar for featured listings), the alerts on it, and the listing check page."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("X402T_DATA_DIR", tempfile.mkdtemp())

from x402trust import alerts, config, db, pages, report, score  # noqa: E402

HOST = "api.example.com"
PAY = "0x1111111111111111111111111111111111111111"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
NOW = datetime.now(timezone.utc)


def ts(days_ago, hours=0):
    return (NOW - timedelta(days=days_ago, hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Availability(unittest.TestCase):
    def setUp(self):
        self.old = (config.DATA_DIR, config.DB_PATH, config.REPORT_DIR, config.SMTP_USER, config.SMTP_PASS)
        config.DATA_DIR = Path(tempfile.mkdtemp())
        config.DB_PATH = config.DATA_DIR / "trust.db"
        config.REPORT_DIR = config.DATA_DIR / "reports"
        config.SMTP_USER = config.SMTP_PASS = ""
        self.conn = db.connect()
        for path in ("/steady", "/shaky", "/new"):
            res = f"https://{HOST}{path}"
            self.conn.execute(
                "INSERT INTO endpoints (resource, host, first_seen, last_seen, method, network, asset, pay_to, "
                "listed_amount, listed_usd, l30_payers, catalog_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (res, HOST, ts(40), ts(0), "GET", "eip155:8453", USDC, PAY, "10000", 0.01, 5, "{}"))
        self.conn.commit()

    def tearDown(self):
        config.DATA_DIR, config.DB_PATH, config.REPORT_DIR, config.SMTP_USER, config.SMTP_PASS = self.old

    def probe(self, path, when, outcome="healthy", status=402, error=None):
        ok = outcome == "healthy"
        self.conn.execute(
            "INSERT INTO probes (resource, ts, outcome, status_code, latency_ms, quoted_amount, quoted_usd, "
            "quoted_network, quoted_asset, quoted_pay_to, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"https://{HOST}{path}", when, outcome, status, 300, "10000" if ok else None, 0.01 if ok else None,
             "eip155:8453" if ok else None, USDC if ok else None, PAY if ok else None, error))

    def fill(self):
        for d in range(29):  # two checks a day for four weeks, plus one from before the window
            self.probe("/steady", ts(d, 1))
            self.probe("/steady", ts(d, 13))
            self.probe("/shaky", ts(d, 1), *(("unreachable", None) if d % 5 == 0 else ("healthy", 402)))
            self.probe("/shaky", ts(d, 13))
        self.probe("/steady", ts(45), "unreachable", None)  # outside the window: ignored
        self.probe("/shaky", ts(2, 5), "unreachable", None, "SSL: TLSV1_ALERT_ACCESS_DENIED")  # our TLS: not counted
        for h in range(3):
            self.probe("/new", ts(0, h + 1))
        self.conn.commit()

    def test_window_counts_and_history(self):
        self.fill()
        a = score.availability(self.conn, now=NOW)
        up, n, days = a[f"https://{HOST}/steady"]
        self.assertEqual((up, n), (58, 58))
        self.assertGreaterEqual(days, 28)
        up, n, _ = a[f"https://{HOST}/shaky"]
        self.assertEqual(n, 58, "the client-side TLS failure is not counted against the seller")
        self.assertEqual(up, 52)
        self.assertEqual(a[f"https://{HOST}/new"][2], 1, "one day of history so far")

    def test_seller_json_and_page_show_the_bar(self):
        self.fill()
        score.run(self.conn, log=lambda *a: None)
        rows = [r for r in report.load(self.conn) if r["host"] == HOST]
        shaky = next(r for r in rows if r["resource"].endswith("/shaky"))
        self.assertAlmostEqual(shaky["uptime_30d"], 52 / 58, places=3)
        doc = pages.seller_json(HOST, rows, [], "Example")
        av = doc["availability_30d"]
        self.assertEqual(av["endpoints_below_bar"], 1)
        self.assertEqual(av["bar"], 0.99)
        ep = next(e for e in doc["endpoints"] if e["resource"].endswith("/steady"))
        self.assertEqual(ep["uptime_30d"], 1.0)
        html_text = pages.seller_page(HOST, rows, set(), [], [], None, None)[0]
        self.assertIn("Availability", html_text)
        self.assertIn("1 of 3 endpoints below 99%", html_text)
        self.assertIn("listing.html?url=https%3A%2F%2Fapi.example.com%2Fsteady", html_text)

    def test_alert_when_falling_below_and_recovering(self):
        self.fill()
        score.run(self.conn, log=lambda *a: None)
        alerts.watch("pat@example.com", HOST)
        alerts.run(self.conn, log=lambda *a: None)  # welcome email; records state
        st = json.loads(self.conn.execute("SELECT state FROM alert_state WHERE resource=?",
                                          (f"https://{HOST}/steady",)).fetchone()[0])
        self.assertIs(st["below_bar"], False)
        new_ep = json.loads(self.conn.execute("SELECT state FROM alert_state WHERE resource=?",
                                              (f"https://{HOST}/new",)).fetchone()[0])
        self.assertIsNone(new_ep["below_bar"], "too little history to judge")
        for h in range(5):  # /steady starts failing
            self.probe("/steady", ts(0, 0) if h == 0 else (NOW + timedelta(minutes=h)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "unreachable", None)
        self.conn.commit()
        score.run(self.conn, log=lambda *a: None)
        prev = st
        cur = alerts.current_state(self.conn, self.conn.execute(
            "SELECT * FROM endpoints WHERE resource=?", (f"https://{HOST}/steady",)).fetchone(), prev, [])
        self.assertIs(cur["below_bar"], True)
        kinds = [k for k, _ in alerts.events_for(f"https://{HOST}/steady", prev, cur)]
        self.assertIn("bar", kinds)
        self.assertIn("down", kinds)
        back = alerts.events_for("r", cur, {**cur, "below_bar": False, "status": "up"})
        self.assertIn("bar_ok", [k for k, _ in back])

    def test_listing_and_pro_pages(self):
        html_text = pages.listing_page()
        self.assertIn(f'data-api="{config.PUBLIC_API}"', html_text)
        self.assertIn("Why isn't my endpoint in the Bazaar?", html_text)
        self.assertIn("coinbase/cdp-sdk", html_text)
        self.assertIn("listing.html", pages.pro_page())
        self.assertIn("99%", pages.pro_page())
        self.assertIn("$11.99 a month", pages.pro_page())


if __name__ == "__main__":
    unittest.main()
