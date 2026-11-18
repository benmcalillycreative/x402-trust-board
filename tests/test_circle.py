"""Circle Agent Marketplace crawl, against a fake discovery API and a fake seller.

Covers: paging with limit/offset, mapping metadata.provider into the listing, a resource
that's also in the Bazaar staying a Bazaar row, Circle-only rows surviving a Bazaar crawl,
the Bazaar taking over a row once it lists it, and delisting after the grace period.
"""

import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path

from x402trust import config, crawl, crawl_circle, db, probe, report, score
from x402trust.report import listed_on

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAY = "0x" + "c3" * 20
STATE = {"bazaar": [], "circle": [], "requests": []}


def circle_item(path, name="Acme Data", method="GET", amount="1000"):
    return {
        "resource": f"{BASE}{path}",
        "type": "http",
        "x402Version": 2,
        "accepts": [{"scheme": "exact", "network": "eip155:8453", "asset": USDC, "amount": amount,
                     "payTo": PAY, "maxTimeoutSeconds": 60, "extra": {"name": "USD Coin", "version": "2"}}],
        "metadata": {
            "provider": {"name": name, "description": "Market data", "category": "data",
                         "tags": ["prices"], "website": "https://acme.example"},
            "path": path, "method": method, "description": f"{path} endpoint",
            "supportsVanillax402": True, "supportsCircleGateway": False,
        },
    }


def bazaar_item(path):
    return {"resource": f"{BASE}{path}", "type": "http", "x402Version": 2, "serviceName": "Acme (Bazaar)",
            "accepts": [{"scheme": "exact", "network": "eip155:8453", "asset": USDC, "amount": "1000",
                         "payTo": PAY}],
            "quality": {"l30DaysTotalCalls": 50, "l30DaysUniquePayers": 7}}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body=b"", headers=()):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        if path == "/circle":
            STATE["requests"].append(q)
            off, lim = int(q.get("offset", 0)), int(q.get("limit", 200))
            items = STATE["circle"][off:off + lim]
            return self.reply(200, json.dumps({"items": items, "pagination": {
                "limit": lim, "offset": off, "total": len(STATE["circle"])}}).encode())
        if path == "/discovery":
            return self.reply(200, json.dumps({"items": STATE["bazaar"],
                                               "pagination": {"total": len(STATE["bazaar"])}}).encode())
        body = {"x402Version": 2, "accepts": [{"scheme": "exact", "network": "eip155:8453", "asset": USDC,
                                               "amount": "1000", "payTo": PAY}]}
        return self.reply(402, json.dumps(body).encode(), [("Content-Type", "application/json")])

    do_POST = do_GET


SERVER = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
BASE = f"http://127.0.0.1:{SERVER.server_address[1]}"


class Circle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved = (config.DATA_DIR, config.DB_PATH, config.CIRCLE_CATALOG_URL, config.DISCOVERY_URL,
                     config.ALLOW_PRIVATE, config.REPORT_DIR, config.CRAWL_DELAY, crawl_circle.PAGE)
        config.DATA_DIR = Path(tempfile.mkdtemp())
        config.DB_PATH = config.DATA_DIR / "trust.db"
        config.REPORT_DIR = config.DATA_DIR / "reports"
        config.CIRCLE_CATALOG_URL = f"{BASE}/circle"
        config.DISCOVERY_URL = f"{BASE}/discovery"
        config.ALLOW_PRIVATE = True
        config.CRAWL_DELAY = 0
        crawl_circle.PAGE = 2  # force paging
        threading.Thread(target=SERVER.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        SERVER.shutdown()
        (config.DATA_DIR, config.DB_PATH, config.CIRCLE_CATALOG_URL, config.DISCOVERY_URL,
         config.ALLOW_PRIVATE, config.REPORT_DIR, config.CRAWL_DELAY, crawl_circle.PAGE) = cls.saved

    def setUp(self):
        self.conn = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        STATE["requests"].clear()

    def row(self, path):
        return self.conn.execute("SELECT * FROM endpoints WHERE resource=?", (f"{BASE}{path}",)).fetchone()

    def test_pages_and_maps_metadata(self):
        STATE["bazaar"] = []
        STATE["circle"] = [circle_item("/a"), circle_item("/b", method="post"), circle_item("/c"),
                           {"resource": f"{BASE}/mcp", "type": "mcp"}]
        out = crawl_circle.run(self.conn, log=lambda *_: None)
        self.assertEqual(out["fetched"], 3)
        self.assertEqual(out["new"], 3)
        self.assertEqual([r["offset"] for r in STATE["requests"]], ["0", "2"])
        r = self.row("/b")
        self.assertEqual(r["source"], "circle")
        self.assertEqual(r["method"], "POST")
        self.assertEqual(r["service_name"], "Acme Data")
        self.assertEqual(r["network"], "eip155:8453")
        self.assertEqual(r["pay_to"], PAY)
        self.assertIsNone(r["l30_payers"])
        self.assertIn("data", json.loads(r["tags"]))
        self.assertEqual(listed_on(dict(r)), ["circle"])

    def test_bazaar_rows_stay_bazaar_and_circle_rows_survive_bazaar_crawl(self):
        STATE["bazaar"] = [bazaar_item("/shared")]
        STATE["circle"] = [circle_item("/shared"), circle_item("/only")]
        crawl.run(self.conn, log=lambda *_: None)
        out = crawl_circle.run(self.conn, log=lambda *_: None)
        self.assertEqual((out["new"], out["also"]), (1, 1))
        shared = self.row("/shared")
        self.assertEqual(shared["source"], "bazaar")
        self.assertEqual(shared["service_name"], "Acme (Bazaar)")  # Bazaar data not overwritten
        self.assertEqual(shared["l30_payers"], 7)
        self.assertEqual(listed_on(dict(shared)), ["bazaar", "circle"])
        # An old Circle-only row isn't delisted by the Bazaar crawl.
        self.conn.execute("UPDATE endpoints SET last_seen='2000-01-01T00:00:00Z' WHERE resource=?",
                          (f"{BASE}/only",))
        crawl.run(self.conn, log=lambda *_: None)
        self.assertEqual(self.row("/only")["delisted"], 0)

    def test_bazaar_takes_over_and_circle_delists(self):
        STATE["bazaar"] = []
        STATE["circle"] = [circle_item("/x"), circle_item("/gone")]
        crawl_circle.run(self.conn, log=lambda *_: None)
        STATE["bazaar"] = [bazaar_item("/x")]
        crawl.run(self.conn, log=lambda *_: None)
        self.assertEqual(self.row("/x")["source"], "bazaar")
        STATE["circle"] = [circle_item("/x")]
        self.conn.execute("UPDATE endpoints SET last_seen='2000-01-01T00:00:00Z' WHERE resource=?",
                          (f"{BASE}/gone",))
        out = crawl_circle.run(self.conn, log=lambda *_: None)
        self.assertEqual(out["delisted"], 1)
        self.assertEqual(self.row("/gone")["delisted"], 1)
        self.assertEqual(self.row("/x")["delisted"], 0)

    def test_scored_without_usage_and_labelled(self):
        STATE["bazaar"] = []
        STATE["circle"] = [circle_item("/s")]
        crawl_circle.run(self.conn, log=lambda *_: None)
        for _ in range(config.MIN_PROBES_FOR_GRADE):
            probe.run(self.conn, log=lambda *_: None)
        score.run(self.conn, log=lambda *_: None)
        s = self.conn.execute("SELECT * FROM scores WHERE resource=?", (f"{BASE}/s",)).fetchone()
        self.assertIsNotNone(s)
        self.assertNotIn("no_payers", json.loads(s["flags"]))
        rows = report.load(self.conn)
        self.assertEqual(rows[0]["source"], "circle")


if __name__ == "__main__":
    unittest.main()
