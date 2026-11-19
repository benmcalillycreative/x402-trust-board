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

from x402trust import alerts, config, pages, plans, signups  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
