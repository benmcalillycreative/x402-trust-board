"""Self-serve sign-ups: the board side, against a fake Worker queue."""

import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path

from x402trust import alerts, config, signups

QUEUE = {"items": []}
ACKS = []


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        ok = self.headers.get("Authorization") == "Bearer s3cret"
        self.send_response(200 if ok else 401)
        self.end_headers()
        self.wfile.write(json.dumps(QUEUE if ok else {}).encode())

    def do_POST(self):
        ACKS.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')


SERVER = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)


class Signups(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved = (config.DATA_DIR, config.API_URL, config.ALERTS_TOKEN, config.SMTP_USER, config.SMTP_PASS)
        config.DATA_DIR = Path(tempfile.mkdtemp())
        config.API_URL = f"http://127.0.0.1:{SERVER.server_address[1]}"
        config.ALERTS_TOKEN = "s3cret"
        config.SMTP_USER = config.SMTP_PASS = ""
        threading.Thread(target=SERVER.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        SERVER.shutdown()
        (config.DATA_DIR, config.API_URL, config.ALERTS_TOKEN, config.SMTP_USER, config.SMTP_PASS) = cls.saved

    def setUp(self):
        ACKS.clear()
        alerts.save_watchers([])

    def test_confirmed_becomes_a_watcher_and_stop_removes_it(self):
        QUEUE["items"] = [{"key": "p:" + "a" * 32, "kind": "signup", "state": "confirmed",
                           "email": "Pat@Example.com", "host": "api.seller.example"}]
        out = signups.run(log=lambda *_: None)
        self.assertEqual(out["added"], 1)
        self.assertEqual(alerts.load_watchers()[0]["match"], "api.seller.example")
        self.assertEqual(ACKS, [{"key": "p:" + "a" * 32, "state": "done"}])
        QUEUE["items"] = [{"key": "s:" + "b" * 64, "kind": "stop", "email": "pat@example.com",
                           "match": "api.seller.example"}]
        signups.run(log=lambda *_: None)
        self.assertEqual(alerts.load_watchers(), [])

    def test_new_request_is_not_acked_until_email_really_sent(self):
        # SMTP isn't set up here, so the confirmation lands in outbox/ and stays queued.
        QUEUE["items"] = [{"key": "p:" + "c" * 32, "kind": "signup", "state": "new",
                           "email": "pat@example.com", "host": "api.seller.example"}]
        out = signups.run(log=lambda *_: None)
        self.assertEqual(out["mailed"], 0)
        self.assertEqual(ACKS, [])
        mail = next((config.DATA_DIR / "outbox").iterdir()).read_text()
        self.assertIn("/v1/alerts/confirm?t=" + "c" * 32, mail)

    def test_seller_plan_lead_is_forwarded_only_when_sent(self):
        saved, config.ALERT_FROM = config.ALERT_FROM, "owner@example.com"
        try:
            QUEUE["items"] = [{"key": "i:" + "d" * 32, "kind": "interest", "state": "new", "email": "dev@seller.example",
                               "host": "api.seller.example", "note": "Interested", "ts": "2026-11-18T00:00:00Z"}]
            out = signups.run(log=lambda *_: None)
            self.assertEqual(out["leads"], 0)            # SMTP isn't set up here: saved to outbox, kept queued
            self.assertEqual(ACKS, [])
            mails = [m.read_text() for m in (config.DATA_DIR / "outbox").iterdir()]
            self.assertTrue(any("Seller plan interest: api.seller.example" in m for m in mails))
        finally:
            config.ALERT_FROM = saved

    def test_stop_link_is_signed(self):
        link = signups.stop_link("Pat@Example.com", "api.seller.example")
        self.assertIn("e=pat%40example.com", link)
        self.assertIn("s=" + signups.sign("pat@example.com", "api.seller.example"), link)
        self.assertIn(link, alerts.footer({"email": "pat@example.com", "match": "api.seller.example"}))

    def test_off_without_settings(self):
        saved = config.API_URL
        config.API_URL = ""
        try:
            self.assertIsNone(signups.run(log=lambda *_: None))
            self.assertIsNone(signups.stop_link("a@b.co", "x"))
        finally:
            config.API_URL = saved


if __name__ == "__main__":
    unittest.main()
