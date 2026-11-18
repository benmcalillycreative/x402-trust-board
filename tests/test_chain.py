"""On-chain payment counting against a fake Base RPC: range splitting, incremental sync,
the time budget, self-payment detection, and the board section."""

import http.server
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from x402trust import chain, config, db, report, score

USDC = chain.USDC_BASE
A = "0x" + "aa" * 20          # honest seller (host-a)
B, B2 = "0x" + "bb" * 20, "0x" + "b2" * 20   # host-b asks for B, and also lists B2 (its own wallet)
C = "0x" + "cc" * 20          # host-c: nearly all money from one outside wallet
STATE = {"head": 20_000_000, "logs": [], "calls": 0, "max_range": 50_000}


def pad(a):
    return "0x" + "0" * 24 + a[2:]


def transfer(block, frm, to, usd, n=[0]):
    n[0] += 1
    return {"transactionHash": "0x%064x" % n[0], "logIndex": "0x0", "blockNumber": hex(block),
            "topics": [chain.TRANSFER, pad(frm), pad(to)], "data": hex(int(usd * 1e6)), "address": USDC}


class RPC(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        STATE["calls"] += 1
        m, params = req["method"], req["params"]
        if m == "eth_getBlockByNumber":
            res = {"number": hex(STATE["head"]), "timestamp": hex(int(time.time()))}
        elif m == "eth_getLogs":
            f = params[0]
            lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            if hi - lo + 1 > STATE["max_range"]:
                return self.send(json.dumps({"jsonrpc": "2.0", "id": 1,
                                             "error": {"code": -32005, "message": "block range too large"}}))
            want = set(f["topics"][2])
            res = [lg for lg in STATE["logs"] if lo <= int(lg["blockNumber"], 16) <= hi and lg["topics"][2] in want]
        else:
            res = None
        self.send(json.dumps({"jsonrpc": "2.0", "id": 1, "result": res}))

    def send(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())


class Busy(http.server.BaseHTTPRequestHandler):
    hits = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        Busy.hits += 1
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(429)
        self.send_header("Retry-After", "5")
        self.end_headers()


busy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Busy)
threading.Thread(target=busy.serve_forever, daemon=True).start()
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RPC)
threading.Thread(target=srv.serve_forever, daemon=True).start()


class OnChain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved = (config.DATA_DIR, config.DB_PATH, config.REPORT_DIR, config.BASE_RPC_URL,
                     config.CHAIN_BLOCK_STEP, config.CHAIN_RPC_URLS, config.CHAIN_MIN_INTERVAL)
        config.DATA_DIR = Path(tempfile.mkdtemp())
        config.DB_PATH = config.DATA_DIR / "trust.db"
        config.REPORT_DIR = config.DATA_DIR / "reports"
        config.BASE_RPC_URL = f"http://127.0.0.1:{srv.server_address[1]}"
        config.CHAIN_BLOCK_STEP = 200_000   # bigger than the fake node allows: forces splitting
        # A "busy" node that always answers 429 sits in front of the working one.
        config.CHAIN_RPC_URLS = [f"http://127.0.0.1:{busy.server_address[1]}", config.BASE_RPC_URL]
        config.CHAIN_MIN_INTERVAL = 0
        conn = cls.conn = db.connect()
        now = datetime.now(timezone.utc)
        for host, pay_to in (("host-a.example", A), ("host-b.example", B), ("host-c.example", C)):
            res = f"https://{host}/x"
            cat = {"accepts": [{"scheme": "exact", "network": chain.CHAIN, "amount": "10000",
                                "asset": USDC, "payTo": pay_to}]}
            conn.execute("INSERT INTO endpoints (resource, host, first_seen, last_seen, method, network, asset, "
                         "pay_to, listed_amount, listed_usd, l30_calls, l30_payers, catalog_json) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (res, host, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "GET", chain.CHAIN, USDC,
                          pay_to, "10000", 0.01, 500, 40, json.dumps(cat)))
            for i in range(3):
                ts = (now - timedelta(hours=i + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
                conn.execute("INSERT INTO probes (resource, ts, outcome, status_code, latency_ms, quoted_amount, "
                             "quoted_usd, quoted_network, quoted_asset, quoted_pay_to) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (res, ts, "healthy", 402, 100, "10000", 0.01, chain.CHAIN, USDC, pay_to))
        # host-b also lists B2: one more endpoint asking for B2 makes it "the same service's wallet".
        conn.execute("INSERT INTO endpoints (resource, host, first_seen, last_seen, method, network, asset, pay_to, "
                     "catalog_json) VALUES ('https://host-b.example/y','host-b.example','2026-01-01T00:00:00Z',"
                     "'2026-01-01T00:00:00Z','GET',?,?,?,'{}')", (chain.CHAIN, USDC, B2))
        conn.commit()
        h = STATE["head"]
        old = h - int(31 * 86400 / 2)  # outside the window: must be ignored
        STATE["logs"] = (
            [transfer(h - 1000 * i, "0x" + "%040x" % (i + 1), A, 0.01) for i in range(25)]   # 25 customers
            + [transfer(old, "0x" + "9" * 40, A, 5.0)]
            + [transfer(h - 500 * i, B2, B, 0.01) for i in range(30)]                       # self-paid
            + [transfer(h - 7, "0x" + "%040x" % 77, B, 0.01) for _ in range(1)]
            + [transfer(h - 300 * i, "0x" + "ee" * 20, C, 0.01) for i in range(24)]          # one big payer
            + [transfer(h - 3, "0x" + "%040x" % 88, C, 0.01)]
        )
        cls.first = chain.run(conn, log=lambda *_: None)
        cls.calls_first = STATE["calls"]

    @classmethod
    def tearDownClass(cls):
        (config.DATA_DIR, config.DB_PATH, config.REPORT_DIR, config.BASE_RPC_URL,
         config.CHAIN_BLOCK_STEP, config.CHAIN_RPC_URLS, config.CHAIN_MIN_INTERVAL) = cls.saved

    def test_backfill_counts_the_last_30_days_only(self):
        s = chain.summaries(self.conn)
        self.assertTrue(self.first["finished"])
        self.assertEqual((s[A]["payments"], s[A]["payers"], s[A]["usd"]), (25, 25, 0.25))
        self.assertEqual(s[A]["own_share"], 0.0)

    def test_self_payment_and_concentration(self):
        s = chain.summaries(self.conn)
        self.assertAlmostEqual(s[B]["own_share"], 30 / 31, places=3)
        self.assertEqual(s[C]["payers"], 2)
        self.assertGreaterEqual(s[C]["top_share"], 0.8)

    def test_flags_on_the_board(self):
        score.run(self.conn, log=lambda *_: None)
        flags = {r[0]: json.loads(r[1]) for r in self.conn.execute(
            "SELECT resource, flags FROM scores WHERE resource LIKE '%/x'")}
        self.assertNotIn("self_paid", flags["https://host-a.example/x"])
        self.assertIn("self_paid", flags["https://host-b.example/x"])
        self.assertIn("one_main_payer", flags["https://host-c.example/x"])
        html = Path(report.run(self.conn, log=lambda *_: None)).read_text()
        self.assertIn("Real payments on Base", html)
        seller = (config.REPORT_DIR / "seller" / "host-a.example.html").read_text()
        self.assertIn("Endpoints", seller)
        self.assertIn("Real payments on Base", seller)
        self.assertIn("<svg", (config.REPORT_DIR / "badge" / "host-a.example.svg").read_text())
        sj = json.loads((config.REPORT_DIR / "seller" / "host-a.example.json").read_text())
        self.assertEqual(sj["host"], "host-a.example")
        self.assertEqual(sj["endpoints"][0]["resource"], "https://host-a.example/x")
        self.assertIn("payout", sj)
        self.assertGreater(sj["onchain"]["payments"], 0)
        self.assertTrue(all({"code", "severity", "text"} <= set(f) for f in sj["endpoints"][0]["flags"]))
        self.assertIn('href="seller/host-a.example.html"', (config.REPORT_DIR / "sellers.html").read_text())
        data = json.loads((config.REPORT_DIR / "onchain.json").read_text())
        self.assertEqual({h["host"] for h in data["hosts"]}, {"host-a.example", "host-b.example", "host-c.example"})

    def test_monthly_snapshot(self):
        from x402trust import monthly
        score.run(self.conn, log=lambda *_: None)
        md = monthly.run(self.conn, label="2026-10", log=lambda *_: None).read_text()
        self.assertIn("# State of agent payments: October 2026", md)
        self.assertIn("Real money on chain", md)
        self.assertIn("| host-a.example |", md)
        data = json.loads((config.REPORT_DIR / "state" / "2026-10.json").read_text())
        self.assertGreater(data["onchain"]["payments"], 0)
        self.assertIn("<h1>", (config.REPORT_DIR / "state" / "2026-10.html").read_text())
        # The next issue compares itself with this one.
        md2 = monthly.run(self.conn, label="2026-11", log=lambda *_: None).read_text()
        self.assertIn("vs last month", md2.replace("(no change)", "vs last month"))
        self.assertIn("2026-10.html", (config.REPORT_DIR / "state" / "index.html").read_text())

    def test_next_run_only_scans_new_blocks(self):
        STATE["head"] += 100
        STATE["logs"].append(transfer(STATE["head"] - 1, "0x" + "%040x" % 99, A, 0.01))
        before = STATE["calls"]
        chain.run(self.conn, log=lambda *_: None)
        self.assertLessEqual(STATE["calls"] - before, 3, "head lookup plus one small range")
        s = chain.summaries(self.conn)
        self.assertEqual(s[A]["payments"], 26)
        chain.run(self.conn, log=lambda *_: None)  # nothing new: no double counting
        self.assertEqual(chain.summaries(self.conn)[A]["payments"], 26)

    def test_rate_limited_node_is_skipped_and_all_busy_stops_cleanly(self):
        self.assertGreaterEqual(Busy.hits, 1, "the busy node was tried")
        self.assertTrue(self.first["finished"], "work moved to the other node")
        saved = config.CHAIN_RPC_URLS
        chain._state["cooldown"].clear()
        config.CHAIN_RPC_URLS = [saved[0]]
        try:
            chain._state["cooldown"][saved[0]] = time.monotonic() + 1000
            out = chain.run(self.conn, log=lambda *_: None)
            self.assertTrue(out["rate_limited"])
            self.assertFalse(out["finished"])
        finally:
            config.CHAIN_RPC_URLS = saved
            chain._state["cooldown"].clear()
        self.assertEqual(chain.summaries(self.conn)[A]["payments"] >= 25, True, "earlier progress kept")

    def test_addresses_partway_through_backfill_are_not_shown(self):
        head = int(self.conn.execute("SELECT value FROM chain_meta WHERE key='head'").fetchone()[0])
        E = "0x" + "ee" * 20
        self.conn.execute("INSERT OR REPLACE INTO chain_addresses VALUES (?, ?, '2026-11-13T00:00:00Z')",
                          (E, head - int(10 * 86400 / 2)))
        self.conn.commit()
        self.assertNotIn(E, chain.summaries(self.conn), "10 days behind the head: still backfilling")
        self.conn.execute("DELETE FROM chain_addresses WHERE address=?", (E,))
        self.conn.commit()

    def test_board_skips_partly_counted_and_rotating_services(self):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        head = int(self.conn.execute("SELECT value FROM chain_meta WHERE key='head'").fetchone()[0])
        def add(host, addr, synced):
            res = f"https://{host}/{addr[-4:]}"
            self.conn.execute("INSERT OR IGNORE INTO endpoints (resource, host, first_seen, last_seen, method, network, "
                              "asset, pay_to, catalog_json) VALUES (?,?,?,?,'GET',?,?,?,'{}')",
                              (res, host, now, now, chain.CHAIN, USDC, addr))
            self.conn.execute("INSERT INTO probes (resource, ts, outcome, status_code, quoted_network, quoted_pay_to) "
                              "VALUES (?,?,'healthy',402,?,?)", (res, now, chain.CHAIN, addr))
            self.conn.execute("INSERT OR REPLACE INTO chain_addresses VALUES (?,?,?)", (addr, synced, now))
        add("half.example", "0x" + "71" * 20, head)                        # caught up
        add("half.example", "0x" + "72" * 20, head - int(6 * 86400 / 2))  # 6 days behind
        for i in range(7):                                                 # a fresh address per request
            add("rotate.example", "0x%040x" % (0x900 + i), head)
        self.conn.commit()
        hosts = {h["host"] for h in report.onchain_by_host(self.conn)}
        self.assertNotIn("half.example", hosts)
        self.assertNotIn("rotate.example", hosts)
        self.assertIn("host-a.example", hosts)
        self.conn.execute("DELETE FROM probes WHERE resource LIKE 'https://half.example/%' "
                          "OR resource LIKE 'https://rotate.example/%'")
        self.conn.execute("DELETE FROM endpoints WHERE host IN ('half.example','rotate.example')")
        self.conn.commit()

    def test_time_budget_leaves_new_addresses_out_until_caught_up(self):
        D = "0x" + "dd" * 20
        self.conn.execute("INSERT INTO endpoints (resource, host, first_seen, last_seen, method, network, asset, "
                          "pay_to, delisted, catalog_json) VALUES ('https://host-d.example/x','host-d.example',"
                          "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','GET',?,?,?,0,'{}')", (chain.CHAIN, USDC, D))
        self.conn.commit()
        out = chain.run(self.conn, log=lambda *_: None, budget_seconds=0)
        self.assertFalse(out["finished"])
        self.assertNotIn(D, chain.summaries(self.conn), "partial backfill must not be shown as real numbers")


if __name__ == "__main__":
    unittest.main()
