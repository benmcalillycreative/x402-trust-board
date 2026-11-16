"""End-to-end test against a fake Bazaar and fake endpoints. No network needed.

Run:  python3 -m unittest discover -s tests -v
"""

import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TMP = tempfile.mkdtemp()
os.environ["X402T_DATA_DIR"] = TMP
os.environ["X402T_ALLOW_PRIVATE"] = "1"
os.environ["X402T_CRAWL_DELAY"] = "0"
os.environ["X402T_PROBE_TIMEOUT"] = "5"

from x402trust import config, crawl, db, netguard, probe, report, score  # noqa: E402

config.CRAWL_PAGE_SIZE = 4  # force pagination
# Other test modules may have imported config first, so set these directly too.
config.ALLOW_PRIVATE = True
config.CRAWL_DELAY = 0
config.PROBE_TIMEOUT = 5
config.DATA_DIR = Path(TMP)
config.DB_PATH = config.DATA_DIR / "trust.db"
config.REPORT_DIR = config.DATA_DIR / "reports"

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
GOOD_WALLET = "0x1111111111111111111111111111111111111111"
EVIL_WALLET = "0x9999999999999999999999999999999999999999"
SIB_WALLET = "0x2222222222222222222222222222222222222222"
STATE = {"flaky": 0, "drop": set(), "switch": 0, "rot": 0}


def v2_header(pay_to=GOOD_WALLET, amount="1000"):
    terms = {
        "x402Version": 2,
        "error": "payment required",
        "resource": {"url": "x"},
        "accepts": [
            {"scheme": "exact", "network": "eip155:84532", "amount": "5", "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e", "payTo": pay_to},
            {"scheme": "exact", "network": "eip155:8453", "amount": amount, "asset": USDC_BASE, "payTo": pay_to, "maxTimeoutSeconds": 60},
        ],
    }
    return base64.b64encode(json.dumps(terms).encode()).decode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body=b"", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/post-only":
            return self.send(402, b"{}", {"PAYMENT-REQUIRED": v2_header()})
        return self.send(405)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/discovery":
            return self.discovery()
        if p == "/good-v2":
            return self.send(402, b"", {"PAYMENT-REQUIRED": v2_header()})
        if p == "/good-v1":
            body = json.dumps({"x402Version": 1, "error": "X-PAYMENT header is required", "accepts": [
                {"scheme": "exact", "network": "base", "maxAmountRequired": "1000", "asset": USDC_BASE,
                 "payTo": GOOD_WALLET, "resource": "x", "maxTimeoutSeconds": 60}]}).encode()
            return self.send(402, body, {"Content-Type": "application/json"})
        if p == "/hijacked":
            return self.send(402, b"", {"PAYMENT-REQUIRED": v2_header(pay_to=EVIL_WALLET)})
        if p == "/pricey":
            return self.send(402, b"", {"PAYMENT-REQUIRED": v2_header(amount="50000")})
        if p == "/switch":
            STATE["switch"] += 1
            wallet = GOOD_WALLET if STATE["switch"] <= 2 else "0x7777777777777777777777777777777777777777"
            return self.send(402, b"", {"PAYMENT-REQUIRED": v2_header(pay_to=wallet)})
        if p == "/rotating":
            STATE["rot"] += 1
            return self.send(402, b"", {"PAYMENT-REQUIRED": v2_header(pay_to="0x%040x" % (0xabc000 + STATE["rot"]))})
        if p == "/sibling":
            return self.send(402, b"", {"PAYMENT-REQUIRED": v2_header(pay_to=SIB_WALLET)})
        if p == "/cheaper":
            return self.send(402, b"", {"PAYMENT-REQUIRED": v2_header(amount="500")})
        if p == "/a-bit-more":
            return self.send(402, b"", {"PAYMENT-REQUIRED": v2_header(amount="1800")})
        if p == "/burn":
            return self.send(402, b"", {"PAYMENT-REQUIRED": v2_header(pay_to="0x" + "0" * 40)})
        if p == "/ratelimited":
            return self.send(429)
        if p == "/needs-input":
            return self.send(400, b'{"error":"missing q"}')
        if p == "/notfound":
            return self.send(404)
        if p == "/free":
            return self.send(200, b'{"ok":true}')
        if p == "/broken":
            return self.send(402, b"pay me")
        if p == "/error":
            return self.send(500)
        if p == "/slow":
            time.sleep(3.3)
            return self.send(402, b"", {"PAYMENT-REQUIRED": v2_header()})
        if p == "/redirect":
            return self.send(302, b"", {"Location": "http://169.254.169.254/"})
        if p == "/flaky":
            STATE["flaky"] += 1
            if STATE["flaky"] % 2:
                return self.send(503)
            return self.send(402, b"", {"PAYMENT-REQUIRED": v2_header()})
        if p == "/post-only":
            return self.send(405)
        return self.send(404)

    def discovery(self):
        from urllib.parse import parse_qs, urlsplit
        qs = parse_qs(urlsplit(self.path).query)
        limit, offset = int(qs["limit"][0]), int(qs["offset"][0])
        items = [i for i in CATALOG if i["resource"] not in STATE["drop"]]
        page = {"x402Version": 2, "items": items[offset: offset + limit],
                "pagination": {"limit": limit, "offset": offset, "total": len(items)}}
        return self.send(200, json.dumps(page).encode(), {"Content-Type": "application/json"})


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
BASE = f"http://127.0.0.1:{server.server_address[1]}"
threading.Thread(target=server.serve_forever, daemon=True).start()
config.DISCOVERY_URL = f"{BASE}/discovery"


def listing(path, method="GET", payers=25, curated=False, name=None, pay_to=GOOD_WALLET):
    return {
        "resource": f"{BASE}{path}", "type": "http", "x402Version": 2,
        "description": f"Test endpoint {path}", "serviceName": name or path.strip("/"),
        "accepts": [{"scheme": "exact", "network": "eip155:8453", "amount": "1000",
                     "asset": USDC_BASE, "payTo": pay_to, "maxTimeoutSeconds": 60}],
        "extensions": {"bazaar": {"info": {"input": {"type": "http", "method": method}}}},
        "quality": {"l30DaysTotalCalls": payers * 4, "l30DaysUniquePayers": payers},
        "curated": curated,
    }


CATALOG = [
    listing("/good-v2", payers=900, curated=True, name="Weather Pro"),
    listing("/good-v1", payers=40),
    listing("/hijacked"),
    listing("/pricey"),
    listing("/free"),
    listing("/broken"),
    listing("/error"),
    listing("/slow"),
    listing("/redirect"),
    listing("/flaky"),
    listing("/post-only", method="POST"),
    {**listing("/gone", payers=0), "resource": "http://127.0.0.1:1/gone"},
    listing("/switch"),
    listing("/rotating"),
    listing("/sibling"),
    listing("/sibling-owner", pay_to=SIB_WALLET),
    listing("/cheaper"),
    listing("/a-bit-more"),
    listing("/needs-input"),
    listing("/notfound"),
    listing("/price/:address"),
    listing("/ratelimited"),
    listing("/burn"),
]


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = db.connect()
        crawl.run(cls.conn, log=lambda *_: None)
        for _ in range(4):
            probe.run(cls.conn, log=lambda *_: None)
        score.run(cls.conn, log=lambda *_: None)
        cls.scores = {
            r["resource"].rsplit("/", 1)[-1]: {**dict(r), "flags": json.loads(r["flags"])}
            for r in cls.conn.execute("SELECT * FROM scores")
        }

    def test_crawl_paginates_whole_catalog(self):
        n = self.conn.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0]
        self.assertEqual(n, len(CATALOG))
        row = self.conn.execute("SELECT * FROM endpoints WHERE resource LIKE '%good-v2'").fetchone()
        self.assertEqual(row["listed_usd"], 0.001)
        self.assertEqual(row["curated"], 1)

    def test_healthy_endpoints_grade_well(self):
        self.assertEqual(self.scores["good-v2"]["grade"], "A")
        self.assertIn(self.scores["good-v1"]["grade"], ("A", "B"))
        self.assertEqual(self.scores["good-v1"]["flags"], [])

    def test_v1_body_parsed_and_network_normalised(self):
        p = self.conn.execute(
            "SELECT * FROM probes WHERE resource LIKE '%good-v1' LIMIT 1").fetchone()
        self.assertEqual(p["protocol"], "v1")
        self.assertEqual(p["quoted_network"], "eip155:8453")
        self.assertEqual(p["payto_matches"], 1)

    def test_prefers_mainnet_usdc_over_testnet_option(self):
        p = self.conn.execute(
            "SELECT * FROM probes WHERE resource LIKE '%good-v2' LIMIT 1").fetchone()
        self.assertEqual(p["quoted_network"], "eip155:8453")
        self.assertEqual(p["price_matches"], 1)

    def test_unknown_wallet_is_a_warning_not_an_F(self):
        s = self.scores["hijacked"]
        self.assertIn("unlisted_wallet", s["flags"])
        self.assertNotEqual(s["grade"], "F")
        self.assertLessEqual(s["score"], 69)

    def test_wallet_switch_after_matching_is_F(self):
        s = self.scores["switch"]
        self.assertIn("wallet_switched", s["flags"])
        self.assertEqual(s["grade"], "F")
        self.assertLessEqual(s["score"], 30)

    def test_zero_address_is_F(self):
        s = self.scores["burn"]
        self.assertIn("burn_address", s["flags"])
        self.assertEqual(s["grade"], "F")

    def test_rotating_and_sibling_wallets_not_penalised(self):
        self.assertIn("rotating_wallet", self.scores["rotating"]["flags"])
        self.assertIn("sibling_wallet", self.scores["sibling"]["flags"])
        for k in ("rotating", "sibling"):
            self.assertEqual(self.scores[k]["grade"], "A", self.scores[k])

    def test_price_tolerance(self):
        self.assertEqual(self.scores["cheaper"]["flags"], [])
        self.assertIn("price_varies", self.scores["a-bit-more"]["flags"])
        self.assertEqual(self.scores["a-bit-more"]["grade"], "A")

    def test_route_patterns_and_rate_limits_are_not_dead(self):
        pattern = self.scores[":address"]
        self.assertEqual(pattern["probes"], 0)
        self.assertIn("route_pattern", pattern["flags"])
        self.assertNotIn("dead", pattern["flags"])
        self.assertNotIn("gone", pattern["flags"])
        self.assertNotIn("dead", self.scores["ratelimited"]["flags"])

    def test_needs_input_counts_as_up_and_404_is_gone(self):
        self.assertEqual(self.scores["needs-input"]["uptime"], 1.0)
        self.assertIn("needs_input", self.scores["needs-input"]["flags"])
        self.assertIn("gone", self.scores["notfound"]["flags"])

    def test_price_mismatch_flagged(self):
        self.assertIn("overcharge_10x", self.scores["pricey"]["flags"])
        self.assertLessEqual(self.scores["pricey"]["score"], 45)
        self.assertLessEqual(self.scores["free"]["score"], 69)

    def test_free_broken_error(self):
        self.assertIn("free_but_listed", self.scores["free"]["flags"])
        self.assertIn("broken_terms", self.scores["broken"]["flags"])
        self.assertNotIn("dead", self.scores["broken"]["flags"])
        self.assertIn("dead", self.scores["error"]["flags"])

    def test_slow_flagged(self):
        self.assertIn("slow", self.scores["slow"]["flags"])

    def test_redirect_not_followed(self):
        p = self.conn.execute(
            "SELECT * FROM probes WHERE resource LIKE '%redirect' LIMIT 1").fetchone()
        self.assertEqual(p["status_code"], 302)
        self.assertEqual(p["outcome"], "redirect")
        self.assertIn("redirects", self.scores["redirect"]["flags"])

    def test_flaky_flagged(self):
        s = self.scores["flaky"]
        self.assertIn("flaky", s["flags"])
        self.assertAlmostEqual(s["uptime"], 0.5, delta=0.26)

    def test_post_method_used(self):
        self.assertEqual(self.scores["post-only"]["uptime"], 1.0)

    def test_unreachable(self):
        self.assertIn("dead", self.scores["gone"]["flags"])
        self.assertIn("no_payers", self.scores["gone"]["flags"])

    def test_report_and_json(self):
        path = report.run(self.conn, log=lambda *_: None)
        html_text = path.read_text()
        self.assertIn("Weather Pro", html_text)
        self.assertIn("Payment wallet switched", html_text)
        self.assertIn("Who's really in the catalog", html_text)
        data = json.loads((config.REPORT_DIR / "scores.json").read_text())
        self.assertEqual(len(data["endpoints"]), len(CATALOG))
        self.assertIn("Weather Pro", [e["service"] for e in data["endpoints"][:5]])

    def test_host_cap_per_cycle(self):
        old = config.HOST_CAP_PER_CYCLE
        config.HOST_CAP_PER_CYCLE = 2
        try:
            mark = self.conn.execute("SELECT MAX(id) FROM probes").fetchone()[0]
            probe.run(self.conn, limit=50, log=lambda *_: None)
            added = self.conn.execute("SELECT COUNT(*) FROM probes WHERE id>?", (mark,)).fetchone()[0]
        finally:
            config.HOST_CAP_PER_CYCLE = old
        self.assertEqual(added, 2, "every test endpoint is on one host, so only 2 may be checked")

    def test_probe_rotates_oldest_first(self):
        mark = self.conn.execute("SELECT MAX(id) FROM probes").fetchone()[0]
        probe.run(self.conn, limit=5, log=lambda *_: None)
        first = {r[0] for r in self.conn.execute("SELECT resource FROM probes WHERE id>?", (mark,))}
        mark = self.conn.execute("SELECT MAX(id) FROM probes").fetchone()[0]
        probe.run(self.conn, limit=5, log=lambda *_: None)
        second = {r[0] for r in self.conn.execute("SELECT resource FROM probes WHERE id>?", (mark,))}
        self.assertEqual(len(first), 5)
        self.assertEqual(len(second), 5)
        self.assertFalse(first & second, "second batch should pick endpoints the first skipped")

    def test_report_caps_rows_but_keeps_red_flags(self):
        old = config.REPORT_MAX_ROWS
        config.REPORT_MAX_ROWS = 2
        try:
            html_text = report.run(self.conn, log=lambda *_: None).read_text()
        finally:
            config.REPORT_MAX_ROWS = old
        main_table = html_text.split('id="t"')[1].split("</table>")[0]
        red_table = html_text.split('id="rt"')[1].split("</table>")[0]
        # The cap trims mild problems, never red flags: all three stay in both tables.
        for name in ("/switch", "/burn", "/pricey"):
            self.assertIn(name, main_table)
            self.assertIn(name, red_table)
        self.assertNotIn("/hijacked", main_table, "a warning is trimmed before any red flag")
        self.assertIn("Weather Pro", main_table)
        self.assertIn('href="#red"', html_text)

    def test_dead_host_breaker(self):
        from x402trust.probe import HOST_FAILS_TO_TRIP, _host_fails, probe_one
        _host_fails.clear()
        row = {"resource": "http://127.0.0.1:1/x", "method": "GET", "pay_to": None, "listed_amount": None}
        results = [probe_one({**row, "resource": f"http://127.0.0.1:1/{i}"}) for i in range(HOST_FAILS_TO_TRIP + 3)]
        skipped = [r for r in results if "skipped" in (r.get("error") or "")]
        self.assertEqual(len(skipped), 3)
        self.assertTrue(all(r["outcome"] == "unreachable" for r in results))
        # A different port on the same machine is a different host and is unaffected.
        self.assertEqual(probe_one({**row, "resource": f"{BASE}/good-v2"})["outcome"], "healthy")
        _host_fails.clear()

    def test_crawl_due(self):
        self.assertFalse(crawl.due(self.conn))
        old = config.CRAWL_EVERY_HOURS
        config.CRAWL_EVERY_HOURS = 0
        try:
            self.assertTrue(crawl.due(self.conn))
        finally:
            config.CRAWL_EVERY_HOURS = old

    def test_zz_delisting(self):
        STATE["drop"].add(f"{BASE}/free")
        crawl.run(self.conn, log=lambda *_: None)
        row = self.conn.execute("SELECT delisted FROM endpoints WHERE resource LIKE '%/free'").fetchone()
        self.assertEqual(row[0], 0, "one missed crawl is not a delisting")
        self.conn.execute("UPDATE endpoints SET last_seen='2026-01-01T00:00:00Z' WHERE resource LIKE '%/free'")
        crawl.run(self.conn, log=lambda *_: None)
        row = self.conn.execute("SELECT delisted FROM endpoints WHERE resource LIKE '%/free'").fetchone()
        self.assertEqual(row[0], 1)
        STATE["drop"].clear()
        crawl.run(self.conn, log=lambda *_: None)
        row = self.conn.execute("SELECT delisted FROM endpoints WHERE resource LIKE '%/free'").fetchone()
        self.assertEqual(row[0], 0)


class Guard(unittest.TestCase):
    def test_private_addresses_blocked_by_default(self):
        config.ALLOW_PRIVATE = False
        try:
            for url in ("http://127.0.0.1/x", "http://192.168.1.1/", "http://169.254.169.254/",
                        "http://10.0.0.5/", "ftp://example.com/x"):
                with self.assertRaises(netguard.Blocked, msg=url):
                    netguard.check_url(url)
        finally:
            config.ALLOW_PRIVATE = True


if __name__ == "__main__":
    unittest.main()
