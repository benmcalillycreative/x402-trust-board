import tempfile
import unittest
from pathlib import Path

from x402trust import config, db, payout, report


class PayoutsExport(unittest.TestCase):
    def setUp(self):
        self.saved = (config.DATA_DIR, config.DB_PATH)
        config.DATA_DIR = Path(tempfile.mkdtemp())
        config.DB_PATH = config.DATA_DIR / "trust.db"
        self.conn = db.connect()
        self.conn.executescript(payout.SCHEMA)

    def tearDown(self):
        config.DATA_DIR, config.DB_PATH = self.saved

    def test_lists_proofs_with_freshness(self):
        c = self.conn
        addr = "0x" + "ab" * 20
        c.execute("INSERT INTO endpoints (resource, host, first_seen, last_seen, method, catalog_json) "
                  "VALUES ('https://api.x.example/a','api.x.example','2026-11-01T00:00:00Z','2026-11-01T00:00:00Z','GET','{}')")
        c.execute("INSERT INTO probes (resource, ts, outcome, status_code, quoted_pay_to) "
                  "VALUES ('https://api.x.example/a','2026-11-17T10:00:00Z','healthy',402,?)", (addr.upper().replace("0X", "0x"),))
        c.execute("INSERT INTO payout_checks VALUES ('api.x.example','2026-11-17T09:00:00Z','ok')")
        c.execute("INSERT INTO payout_proofs VALUES ('api.x.example','dns','x.example',?,'2026-11-12','wallet','')", (addr,))
        c.commit()
        out = report.payouts_export(c)
        self.assertIn("_x402-payto.", out["how_to_verify"]["dns"])
        [h] = out["hosts"]
        self.assertEqual(h["proofs_checked_at"], "2026-11-17T09:00:00Z")
        self.assertEqual(h["proofs"][0]["signature"], "wallet")
        self.assertEqual(h["proofs"][0]["last_seen_asking"], "2026-11-17T10:00:00Z")

    def test_solana_addresses_keep_their_case(self):
        c = self.conn
        sol = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
        c.execute("INSERT INTO endpoints (resource, host, first_seen, last_seen, method, catalog_json) "
                  "VALUES ('https://s.example/a','s.example','2026-11-01T00:00:00Z','2026-11-01T00:00:00Z','GET','{}')")
        c.execute("INSERT INTO probes (resource, ts, outcome, status_code, quoted_pay_to) "
                  "VALUES ('https://s.example/a','2026-11-17T10:00:00Z','healthy',402,?)", (sol,))
        c.execute("INSERT INTO payout_proofs VALUES ('s.example','manifest','s.example',?,NULL,'unsigned','')", (sol.lower(),))
        c.commit()
        [h] = report.payouts_export(c)["hosts"]
        self.assertEqual(h["proofs"][0]["address"], sol)
        self.assertEqual(h["proofs"][0]["last_seen_asking"], "2026-11-17T10:00:00Z")


if __name__ == "__main__":
    unittest.main()
