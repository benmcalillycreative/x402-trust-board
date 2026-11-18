"""Solana payment counting, against a fake Solana RPC."""

import http.server
import json
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path

from x402trust import chain_sol, config, db, report

MINT = chain_sol.USDC_MINT
OWNER = "SeLLer1111111111111111111111111111111111aB"   # case matters in base58
ACCT = "SeLLerUsdcAcct11111111111111111111111111aB"
BUSY_OWNER = "BusyOwner111111111111111111111111111111aB"
BUSY_ACCT = "BusyAcct11111111111111111111111111111111aB"
NOW = int(time.time())
CALLS = Counter()
SIGS = {ACCT: [], BUSY_ACCT: []}
TXS = {}


def payment(sig, payer, amount, t):
    """A USDC transfer from payer's token account (index 1) to the seller's (index 2)."""
    SIGS[ACCT].insert(0, {"signature": sig, "blockTime": t, "err": None})  # newest first
    TXS[sig] = {"meta": {"err": None,
                         "preTokenBalances": [
                             {"accountIndex": 1, "mint": MINT, "owner": payer, "uiTokenAmount": {"amount": "5000000"}},
                             {"accountIndex": 2, "mint": MINT, "owner": OWNER, "uiTokenAmount": {"amount": "0"}}],
                         "postTokenBalances": [
                             {"accountIndex": 1, "mint": MINT, "owner": payer,
                              "uiTokenAmount": {"amount": str(5000000 - amount)}},
                             {"accountIndex": 2, "mint": MINT, "owner": OWNER, "uiTokenAmount": {"amount": str(amount)}}]},
                "transaction": {"message": {"accountKeys": [{"pubkey": "Fee"}, {"pubkey": payer + "Ata"},
                                                            {"pubkey": ACCT}]}}}


class RPC(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        m, p = req["method"], req["params"]
        CALLS[m] += 1
        if m == "getTokenAccountsByOwner":
            acct = {OWNER: ACCT, BUSY_OWNER: BUSY_ACCT}.get(p[0])
            result = {"value": [{"pubkey": acct}] if acct else []}
        elif m == "getSignaturesForAddress":
            sigs, opts = SIGS.get(p[0], []), p[1]
            if opts.get("before"):
                sigs = sigs[[s["signature"] for s in sigs].index(opts["before"]) + 1:]
            if opts.get("until"):
                names = [s["signature"] for s in sigs]
                sigs = sigs[:names.index(opts["until"])] if opts["until"] in names else sigs
            result = sigs[:opts.get("limit", 1000)]
        elif m == "getTransaction":
            result = TXS.get(p[0])
        else:
            result = None
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)


SERVER = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RPC)


class Solana(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved = (config.DATA_DIR, config.DB_PATH, config.SOL_RPC_URLS, config.SOL_MIN_INTERVAL,
                     config.SOL_MAX_TX, chain_sol.PAGE)
        config.DATA_DIR = Path(tempfile.mkdtemp())
        config.DB_PATH = config.DATA_DIR / "trust.db"
        config.SOL_RPC_URLS = [f"http://127.0.0.1:{SERVER.server_address[1]}"]
        config.SOL_MIN_INTERVAL = 0
        config.SOL_MAX_TX = 50
        chain_sol.PAGE = 10  # force paging
        threading.Thread(target=SERVER.serve_forever, daemon=True).start()
        # 25 payments from 25 buyers inside the window, one too old, one failed.
        for i in range(25):
            payment(f"sig{i:03d}", f"Buyer{i:02d}", 10000, NOW - 86400 * 20 + i * 3600)
        payment("old", "BuyerOld", 10000, NOW - 86400 * 40)
        SIGS[ACCT].sort(key=lambda s: -s["blockTime"])
        SIGS[ACCT].insert(0, {"signature": "failed", "blockTime": NOW - 100, "err": {"x": 1}})
        SIGS[BUSY_ACCT] = [{"signature": f"b{i}", "blockTime": NOW - i, "err": None} for i in range(80)]
        c = cls.conn = db.connect()
        for host, owner in (("sol.example", OWNER), ("busy.example", BUSY_OWNER)):
            c.execute("INSERT INTO endpoints (resource, host, first_seen, last_seen, method, catalog_json) "
                      "VALUES (?,?,?,?,?,?)", (f"https://{host}/x", host, db.now(), db.now(), "GET", "{}"))
            c.execute("INSERT INTO probes (resource, ts, outcome, status_code, quoted_network, quoted_asset, "
                      "quoted_pay_to) VALUES (?,?,?,?,?,?,?)",
                      (f"https://{host}/x", db.now(), "healthy", 402, chain_sol.CHAIN, MINT.lower(), owner))
        c.commit()

    @classmethod
    def tearDownClass(cls):
        SERVER.shutdown()
        (config.DATA_DIR, config.DB_PATH, config.SOL_RPC_URLS, config.SOL_MIN_INTERVAL,
         config.SOL_MAX_TX, chain_sol.PAGE) = cls.saved

    def test_counts_payments_incrementally_and_skips_busy_accounts(self):
        out = chain_sol.run(self.conn, log=lambda *_: None)
        self.assertTrue(out["finished"])
        self.assertEqual(out["fetched"], 25)          # the failed one isn't fetched, the old one isn't reached
        s = chain_sol.summaries(self.conn)
        self.assertEqual(s[OWNER]["payments"], 25)
        self.assertEqual(s[OWNER]["payers"], 25)
        self.assertEqual(s[OWNER]["usd"], 0.25)
        self.assertNotIn(BUSY_OWNER, s)                 # too many transactions to read: left out
        self.assertEqual(self.conn.execute("SELECT too_busy FROM sol_accounts WHERE owner=?",
                                           (BUSY_OWNER,)).fetchone()[0], 1)
        # Next run: only the new payment is fetched.
        payment("new1", "BuyerNew", 20000, NOW - 10)
        before = CALLS["getTransaction"]
        chain_sol.run(self.conn, log=lambda *_: None)
        self.assertEqual(CALLS["getTransaction"] - before, 1)
        self.assertEqual(chain_sol.summaries(self.conn)[OWNER]["payments"], 26)
        # The board shows it under its host, address case kept.
        hosts = {h["host"]: h for h in report.onchain_by_host(self.conn)}
        self.assertEqual(hosts["sol.example"]["payments"], 26)
        self.assertEqual(hosts["sol.example"]["addresses"], [OWNER])

    def test_read_payment_ignores_outgoing(self):
        tx = json.loads(json.dumps(TXS["sig000"]))
        self.assertEqual(chain_sol.read_payment(tx, ACCT), (10000, "Buyer00"))
        self.assertEqual(chain_sol.read_payment(tx, "Buyer00Ata")[0], 0)


if __name__ == "__main__":
    unittest.main()
