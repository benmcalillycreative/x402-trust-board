"""MPP (Machine Payments Protocol) support, end to end against a fake directory and fake services.

Covers: reading the directory, parsing `WWW-Authenticate: Payment` challenges (two
methods in one response), price checks against the listing, a wallet that changes
with nothing listed to compare against, and MPP rows surviving an x402 crawl.
"""

import base64
import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path

from x402trust import config, crawl, crawl_mpp, db, probe, report, score
from x402trust.parse import parse_402

TOKEN = "0x20c000000000000000000000b9537d11c60e8b50"
R1, R2 = "0x" + "a1" * 20, "0x" + "b2" * 20
CALLS = {"switch": 0}


def b64(d):
    return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")


def challenge(method, amount, recipient=None, currency=TOKEN, cid="x"):
    req = {"amount": str(amount), "currency": currency, "methodDetails": {"chainId": 4217}}
    if recipient:
        req["recipient"] = recipient
    return (f'Payment id="{cid}", realm="127.0.0.1", method="{method}", intent="charge", '
            f'request="{b64(req)}", expires="2099-01-01T00:00:00Z"')


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
        p = self.path.split("?")[0]
        if p == "/api/services":
            return self.reply(200, json.dumps(DIRECTORY).encode(), [("Content-Type", "application/json")])
        if p == "/discovery":
            return self.reply(200, json.dumps({"items": [X402_ITEM], "pagination": {"total": 1}}).encode())
        if p == "/mpp/good":  # two payment options, sent as two headers
            return self.reply(402, b'{"title":"Payment Required"}', [
                ("WWW-Authenticate", challenge("tempo", 10000, R1, cid="a")),
                ("WWW-Authenticate", challenge("stripe", 1, currency="usd", cid="b"))])
        if p == "/mpp/pricey":
            return self.reply(402, b"{}", [("WWW-Authenticate", challenge("tempo", 50000, R1))])
        if p == "/mpp/switch":
            CALLS["switch"] += 1
            who = R1 if CALLS["switch"] <= 3 else R2
            return self.reply(402, b"{}", [("WWW-Authenticate", challenge("tempo", 1000, who))])
        if p == "/mpp/free":
            return self.reply(200, b'{"ok":true}')
        if p == "/mpp/dynamic":
            return self.reply(402, b"{}", [("WWW-Authenticate", challenge("tempo", 777, R1))])
        if p == "/x402/item":
            return self.reply(404)
        return self.reply(404)

    do_POST = do_GET


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
BASE = f"http://127.0.0.1:{server.server_address[1]}"
threading.Thread(target=server.serve_forever, daemon=True).start()


def ep(path, amount, dynamic=False):
    pay = {"intent": "charge", "method": "tempo", "currency": TOKEN, "decimals": 6}
    pay.update({"dynamic": True, "amountHint": "$0.01/page"} if dynamic else {"amount": str(amount)})
    return {"method": "GET", "path": path, "description": path, "payment": pay}


DIRECTORY = {"version": 1, "services": [{
    "id": "fake", "name": "Fake MPP Service", "url": BASE, "serviceUrl": BASE,
    "description": "test", "categories": ["data"], "integration": "third-party", "tags": [],
    "status": "active", "methods": {"tempo": {}, "stripe": {}}, "realm": "127.0.0.1",
    "endpoints": [ep("/mpp/good", 10000), ep("/mpp/pricey", 1000), ep("/mpp/switch", 1000),
                  ep("/mpp/free", 1000), ep("/mpp/dynamic", 0, dynamic=True),
                  {"method": "GET", "path": "/mpp/unpaid", "description": "free", "payment": None}],
}]}
X402_ITEM = {"resource": f"{BASE}/x402/item", "type": "http", "x402Version": 2, "accepts": [
    {"scheme": "exact", "network": "eip155:8453", "amount": "1000",
     "asset": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "payTo": R1}]}


class MPP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved = (config.DATA_DIR, config.DB_PATH, config.MPP_CATALOG_URL, config.DISCOVERY_URL,
                     config.ALLOW_PRIVATE, config.REPORT_DIR)
        config.DATA_DIR = Path(tempfile.mkdtemp())
        config.DB_PATH = config.DATA_DIR / "trust.db"
        config.REPORT_DIR = config.DATA_DIR / "reports"
        config.MPP_CATALOG_URL = f"{BASE}/api/services"
        config.DISCOVERY_URL = f"{BASE}/discovery"
        config.ALLOW_PRIVATE = True
        cls.conn = db.connect()
        cls.result = crawl_mpp.run(cls.conn, log=lambda *_: None)
        for _ in range(4):
            probe._host_fails.clear()
            probe.run(cls.conn, log=lambda *_: None)
        score.run(cls.conn, log=lambda *_: None)
        cls.flags = {r[0].rsplit("/", 1)[-1]: json.loads(r[1]) for r in
                     cls.conn.execute("SELECT resource, flags FROM scores")}
        cls.grades = {r[0].rsplit("/", 1)[-1]: (r[1], r[2]) for r in
                      cls.conn.execute("SELECT resource, grade, score FROM scores")}

    @classmethod
    def tearDownClass(cls):
        (config.DATA_DIR, config.DB_PATH, config.MPP_CATALOG_URL, config.DISCOVERY_URL,
         config.ALLOW_PRIVATE, config.REPORT_DIR) = cls.saved

    def test_directory_is_read(self):
        self.assertEqual(self.result["endpoints"], 5, "the unpaid endpoint is skipped")
        rows = self.conn.execute("SELECT protocol, network, listed_usd FROM endpoints WHERE resource LIKE '%/mpp/good'")
        self.assertEqual(tuple(rows.fetchone()), ("mpp", "eip155:4217", 0.01))

    def test_challenges_are_parsed(self):
        p = self.conn.execute("SELECT protocol, quoted_amount, quoted_pay_to, quoted_network, quoted_usd FROM probes "
                              "WHERE resource LIKE '%/mpp/good' ORDER BY id DESC LIMIT 1").fetchone()
        # Of the two options, the one with an address to check (Tempo) is the one recorded.
        self.assertEqual(tuple(p), ("mpp", "10000", R1, "eip155:4217", 0.01))

    def test_clean_endpoint_grades_well_without_usage_data(self):
        self.assertEqual(self.flags["good"], [])
        self.assertEqual(self.grades["good"][0], "A", "no payer counts in MPP must not drag the grade down")

    def test_overcharge(self):
        self.assertIn("overcharge_10x", self.flags["pricey"])

    def test_wallet_change_with_nothing_listed(self):
        self.assertIn("payout_changed", self.flags["switch"])

    def test_free_and_dynamic(self):
        self.assertIn("free_but_listed", self.flags["free"])
        self.assertIn("variable_price", self.flags["dynamic"])

    def test_x402_crawl_leaves_mpp_rows_alone(self):
        self.conn.execute("UPDATE endpoints SET last_seen='2020-01-01T00:00:00Z' WHERE protocol='mpp'")
        crawl.run(self.conn, log=lambda *_: None)
        left = self.conn.execute("SELECT COUNT(*) FROM endpoints WHERE protocol='mpp' AND delisted=0").fetchone()[0]
        self.assertEqual(left, 5)
        self.assertEqual(self.conn.execute("SELECT protocol FROM endpoints WHERE resource LIKE '%/x402/item'")
                         .fetchone()[0], "x402")

    def test_report_labels_mpp(self):
        path = report.run(self.conn, log=lambda *_: None)
        html = Path(path).read_text()
        self.assertIn("<span class=proto>MPP</span>", html)
        self.assertIn('data-proto="mpp"', html)
        data = json.loads((config.REPORT_DIR / "scores.json").read_text())
        self.assertTrue(any(e["protocol"] == "mpp" for e in data["endpoints"]))

    def test_single_header_with_two_challenges(self):
        both = challenge("stripe", 1, currency="usd", cid="b") + ", " + challenge("tempo", 5, R1)
        proto, terms = parse_402([("WWW-Authenticate", both)], b"")
        self.assertEqual(proto, "mpp")
        self.assertEqual(len(terms["accepts"]), 2)


if __name__ == "__main__":
    unittest.main()
