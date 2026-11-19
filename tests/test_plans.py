"""Seller plan bookkeeping: payments queued by the Worker become receipts, watchers and reminders."""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("X402T_DATA_DIR", tempfile.mkdtemp())

from x402trust import alerts, config, db, pages, plans, signups  # noqa: E402

NOW = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
PID = "a" * 24


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def payment(tx="0x" + "ee" * 32, months=1, until=NOW + timedelta(days=30), key="b:" + "1" * 32):
    return {"key": key, "kind": "payment", "state": "new", "id": PID, "host": "api.example.com",
            "email": "dev@example.com", "tx": tx, "payer": "0x" + "ab" * 20, "network": "eip155:8453",
            "amount": plans.price(months), "months": months, "ts": iso(NOW), "paid_until": iso(until),
            "previous_until": None}


class Plans(unittest.TestCase):
    def setUp(self):
        self.old = (config.DATA_DIR, config.API_URL, config.ALERTS_TOKEN, config.ALERT_FROM, config.SMTP_USER)
        config.DATA_DIR = Path(tempfile.mkdtemp())
        config.API_URL, config.ALERTS_TOKEN = "https://api.test", "tok"
        config.ALERT_FROM, config.SMTP_USER = "owner@settlewatch.test", ""
        self.mail = []
        self.calls = []

        def deliver(to, subject, body, log=print):
            self.mail.append((to, subject, body))
            return "sent"

        def call(path, payload=None):
            self.calls.append((path, payload))
            return {"items": self.queue} if path == "queue" else {"ok": True}

        self.queue = []
        self.patches = [mock.patch.object(alerts, "deliver", deliver), mock.patch.object(signups, "_call", call)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        config.DATA_DIR, config.API_URL, config.ALERTS_TOKEN, config.ALERT_FROM, config.SMTP_USER = self.old

    def test_payment_is_recorded_once_with_receipt_watcher_and_owner_note(self):
        self.queue = [payment()]
        signups.run(log=lambda *a: None)
        book = plans.load()
        self.assertEqual(book[PID]["paid_until"], iso(NOW + timedelta(days=30)))
        self.assertEqual(len(book[PID]["payments"]), 1)
        self.assertIn(("dev@example.com", "api.example.com"),
                      {(w["email"], w["match"]) for w in alerts.load_watchers()})
        to = [m[0] for m in self.mail]
        self.assertEqual(sorted(to), ["dev@example.com", "owner@settlewatch.test"])
        receipt = next(m for m in self.mail if m[0] == "dev@example.com")
        self.assertIn("basescan.org/tx/0x" + "ee" * 32, receipt[2])
        self.assertIn("Nothing renews by itself", receipt[2])
        self.assertIn("listing review", receipt[2])
        self.assertIn(("ack", {"key": "b:" + "1" * 32, "state": "done"}), self.calls)
        # The same item again (say the ack was lost): no second record, no second email.
        self.mail.clear()
        signups.run(log=lambda *a: None)
        self.assertEqual(len(plans.load()[PID]["payments"]), 1)
        self.assertEqual(self.mail, [])

    def test_receipt_waits_if_mail_fails_but_payment_is_kept(self):
        self.queue = [payment()]
        with mock.patch.object(alerts, "deliver", lambda *a, **k: "failed: smtp down"):
            signups.run(log=lambda *a: None)
        self.assertEqual(len(plans.load()[PID]["payments"]), 1, "recorded even though no email went out")
        self.assertFalse(any(c[0] == "ack" for c in self.calls), "left in the queue to retry the receipt")
        signups.run(log=lambda *a: None)
        self.assertEqual(len(plans.load()[PID]["payments"]), 1)
        self.assertTrue(any(c[0] == "ack" for c in self.calls))

    def test_renewal_moves_the_end_date_and_rearms_reminders(self):
        self.queue = [payment()]
        signups.run(log=lambda *a: None)
        soon = NOW + timedelta(days=27)
        self.mail.clear()
        self.assertEqual(plans.remind(now=soon, log=lambda *a: None), 1)
        to, subject, body = self.mail[0]
        self.assertEqual(to, "dev@example.com")
        self.assertIn("ends", subject)
        self.assertIn(f"{config.PUBLIC_API}/v1/plan/pay?p={PID}&months=1", body)
        self.assertIn("months=12", body)
        self.assertEqual(plans.remind(now=soon + timedelta(hours=6), log=lambda *a: None), 0, "only once")
        self.queue = [payment(tx="0x" + "ff" * 32, months=3, until=NOW + timedelta(days=120), key="b:" + "2" * 32)]
        self.mail.clear()
        signups.run(log=lambda *a: None)
        self.assertEqual(plans.load()[PID]["paid_until"], iso(NOW + timedelta(days=120)))
        self.assertIn("Seller plan renewed", next(m[1] for m in self.mail if m[0] == "owner@settlewatch.test"))
        self.assertEqual(plans.remind(now=soon, log=lambda *a: None), 0, "plenty of time left now")
        self.assertEqual(plans.remind(now=NOW + timedelta(days=117), log=lambda *a: None), 1, "re-armed for the new date")

    def test_expiry_sends_one_ended_email_and_leaves_free_alerts(self):
        self.queue = [payment()]
        signups.run(log=lambda *a: None)
        later = NOW + timedelta(days=31)
        self.mail.clear()
        self.assertEqual(plans.remind(now=later, log=lambda *a: None), 1)
        self.assertIn("has ended", self.mail[0][1])
        self.assertIn("free alerts carry on", self.mail[0][2])
        self.assertEqual(plans.remind(now=later + timedelta(days=1), log=lambda *a: None), 0)
        self.assertEqual(plans.active(now=later), {})
        self.assertTrue(alerts.load_watchers(), "the free alerts stay")

    def test_seller_page_offers_usdc(self):
        h = pages.pro_page()
        self.assertIn(f"{config.PUBLIC_API}/v1/plan/start", h)
        self.assertIn("$11.99 a month", h)
        self.assertIn("3 months: $35.97", h)
        self.assertIn("12 months: $143.88", h)
        self.assertIn("Nothing renews or charges by itself", h)
        self.assertNotIn("Early access", h)


HOST = "api.example.com"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


class ReviewAndReport(Plans):
    """The listing review a new plan gets, and the monthly availability report."""

    def setUp(self):
        super().setUp()
        plans.REVIEW_PAUSE = 0
        self.old_db = config.DB_PATH
        config.DB_PATH = config.DATA_DIR / "trust.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.addCleanup(setattr, config, "DB_PATH", self.old_db)
        for path, method, desc in (("/a", "GET", "Quotes for agents"), ("/b", "POST", None), ("/c", "GET", None)):
            self.conn.execute(
                "INSERT INTO endpoints (resource, host, first_seen, last_seen, method, network, asset, pay_to, "
                "listed_amount, listed_usd, l30_payers, catalog_json, service_name, description) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"https://{HOST}{path}", HOST, iso(NOW - timedelta(days=60)), iso(NOW), method, "eip155:8453", USDC,
                 "0x" + "11" * 20, "10000", 0.01, 3, "{}", "Example Quotes", desc))
        self.conn.commit()

    def probe(self, path, when, outcome="healthy", status=402, error=None):
        self.conn.execute("INSERT INTO probes (resource, ts, outcome, status_code, latency_ms, error) VALUES (?,?,?,?,?,?)",
                          (f"https://{HOST}{path}", when.strftime("%Y-%m-%dT%H:%M:%SZ"), outcome, status, 300, error))

    def fill(self, now):
        for d in range(30):
            for h in (2, 14):
                t = now - timedelta(days=d, hours=h)
                self.probe("/a", t)
                bad = h == 2 and d % 3 == 0  # /b fails 10 of 60
                self.probe("/b", t, *(("http_error", 503) if bad else ("healthy", 402)))
        self.probe("/c", now - timedelta(hours=3))
        self.probe("/a", now - timedelta(days=2, hours=5), "unreachable", None, "SSL: TLSV1_ALERT_ACCESS_DENIED")
        self.conn.commit()

    def fake_listing(self, url, method):
        self.looked.append((url, method))
        if url.endswith("/c"):
            raise OSError("timed out")
        if url.endswith("/b"):
            return {"listed": False, "summary": "Not listed: 1 thing to fix first.", "findings": [
                {"status": "fail", "title": "Answers an unpaid request with 402 Payment Required",
                 "detail": "got 400", "fix": "Let unpaid requests reach the middleware."},
                {"status": "warn", "title": "Listing metadata is incomplete", "detail": "missing: icon", "fix": "Set iconUrl."}]}
        return {"listed": True, "summary": "Listed in the Bazaar, and nothing blocking it.", "findings": [
            {"status": "warn", "title": "Listing metadata is incomplete", "detail": "missing: icon", "fix": "Set iconUrl."},
            {"status": "pass", "title": "Passes Coinbase's indexing checks"}]}

    def test_new_plan_gets_one_listing_review_with_owner_copy(self):
        self.fill(NOW)
        self.queue = [payment()]
        signups.run(log=lambda *a: None)
        self.looked, self.mail = [], []
        with mock.patch.object(plans, "listing", self.fake_listing):
            plans.cycle(self.conn, now=NOW, log=lambda *a: None)
            self.assertEqual(sorted(self.looked), [(f"https://{HOST}/a", "GET"), (f"https://{HOST}/b", "POST"),
                                                   (f"https://{HOST}/c", "GET")])
            review = next(m for m in self.mail if m[0] == "dev@example.com")
            self.assertEqual(review[1], f"Listing review for {HOST}")
            body = review[2]
            self.assertIn("In the Bazaar now: 1 of 2 checked.", body)
            self.assertLess(body.index("[Fix] Answers an unpaid request"), body.index("[Check] Listing metadata is incomplete (2 routes)"),
                            "blocking problems first, then the rest grouped across routes")
            self.assertIn("Fix: Let unpaid requests reach the middleware.", body)
            self.assertIn("Couldn't check", body)
            self.assertIn("github.com/coinbase/cdp-sdk/issues/new", body)
            self.assertIn("Service: Example Quotes", body)
            self.assertIn("Description: Quotes for agents", body)
            self.assertTrue(any(m[0] == "owner@settlewatch.test" and m[1].startswith("[copy]") for m in self.mail))
            self.assertTrue(plans.load()[PID]["reviewed"])
            self.mail, self.looked = [], []
            plans.cycle(self.conn, now=NOW + timedelta(hours=6), log=lambda *a: None)
            self.assertEqual(self.looked, [], "only once per plan")

    def test_review_waits_when_the_api_is_down(self):
        self.queue = [payment()]
        signups.run(log=lambda *a: None)
        with mock.patch.object(plans, "listing", side_effect=OSError("down")):
            plans.cycle(self.conn, now=NOW, log=lambda *a: None)
        self.assertFalse(plans.load()[PID].get("reviewed"), "tried again next run")

    def test_monthly_report_once_per_month_with_failed_checks(self):
        self.queue = [payment(until=NOW + timedelta(days=90))]  # paid Nov 18
        signups.run(log=lambda *a: None)
        book = plans.load()
        book[PID]["reviewed"] = iso(NOW)
        plans.save(book)
        self.mail = []
        plans.cycle(self.conn, now=NOW + timedelta(days=5), log=lambda *a: None)
        self.assertFalse(any("Availability report" in m[1] for m in self.mail), "no report in the month it started")
        dec1 = datetime(2026, 12, 1, 8, tzinfo=timezone.utc)
        self.fill(dec1)
        plans.cycle(self.conn, now=dec1, log=lambda *a: None)
        rep = [m for m in self.mail if "Availability report" in m[1]]
        self.assertEqual(len(rep), 1)
        self.assertEqual(rep[0][1], f"Availability report for {HOST}, November 2026")
        body = rep[0][2]
        self.assertIn("Below 99%: 1", body)
        self.assertIn("Above 99.5% (priority placement): 1", body)
        self.assertIn("Too few checks to judge (under 10): 1", body)
        self.assertIn(f"https://{HOST}/b: 10 failed", body)
        self.assertIn("HTTP 503", body)
        self.assertNotIn("TLSV1_ALERT_ACCESS_DENIED", body, "our own connection problems aren't counted")
        self.assertLess(body.index(f"BELOW 99%       https://{HOST}/b"), body.index(f"priority        https://{HOST}/a"))
        self.mail = []
        plans.cycle(self.conn, now=dec1 + timedelta(hours=6), log=lambda *a: None)
        self.assertFalse(any("Availability report" in m[1] for m in self.mail), "once a month")


if __name__ == "__main__":
    unittest.main()
