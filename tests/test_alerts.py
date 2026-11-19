"""Owner notes and alert emails, on a small hand-built database. No network needed."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("X402T_DATA_DIR", tempfile.mkdtemp())

from x402trust import alerts, config, db, notes, score  # noqa: E402

HOST = "api.example.com"
GOOD = "0x1111111111111111111111111111111111111111"
NEW = "0x2222222222222222222222222222222222222222"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def subject(path):
    import email
    from email import policy
    return email.message_from_bytes(path.read_bytes(), policy=policy.default)["Subject"]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.old_dir = config.DATA_DIR
        self.old_db = config.DB_PATH
        self.old_smtp = (config.SMTP_USER, config.SMTP_PASS)
        config.DATA_DIR = Path(tempfile.mkdtemp())
        config.DB_PATH = config.DATA_DIR / "trust.db"
        config.SMTP_USER = config.SMTP_PASS = ""
        self.conn = db.connect()
        self.t = 0
        for path in ("/a", "/b", "/gone"):
            res = f"https://{HOST}{path}"
            catalog = {"resource": res, "accepts": [{"scheme": "exact", "network": "eip155:8453",
                       "amount": "10000", "asset": USDC, "payTo": GOOD}]}
            self.conn.execute(
                "INSERT INTO endpoints (resource, host, first_seen, last_seen, method, network, asset, "
                "pay_to, listed_amount, listed_usd, l30_payers, catalog_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (res, HOST, "2026-10-29T00:00:00Z", "2026-10-29T00:00:00Z", "GET", "eip155:8453",
                 USDC, GOOD, "10000", 0.01, 20, json.dumps(catalog)))
        self.conn.commit()

    def tearDown(self):
        config.DATA_DIR, config.DB_PATH = self.old_dir, self.old_db
        config.SMTP_USER, config.SMTP_PASS = self.old_smtp

    def probe(self, path, outcome="healthy", status=402, pay_to=GOOD, amount="10000"):
        self.t += 1
        # Recent timestamps, one hour apart, so they always fall inside the scoring window.
        from datetime import datetime, timedelta, timezone
        ts = (datetime.now(timezone.utc) - timedelta(hours=48 - self.t)).strftime("%Y-%m-%dT%H:%M:%SZ")
        healthy = outcome == "healthy"
        self.conn.execute(
            "INSERT INTO probes (resource, ts, outcome, status_code, latency_ms, quoted_amount, quoted_usd, "
            "quoted_network, quoted_asset, quoted_pay_to) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"https://{HOST}{path}", ts, outcome, status, 200,
             amount if healthy else None, int(amount) / 1e6 if healthy else None,
             "eip155:8453" if healthy else None, USDC if healthy else None, pay_to if healthy else None))
        self.conn.commit()

    def outbox(self):
        box = config.DATA_DIR / "outbox"
        return sorted(box.iterdir()) if box.exists() else []

    def cycle(self):
        alerts.run(self.conn, log=lambda *_: None)
        return self.outbox()


class OwnerNotes(Fixture):
    def test_retired_routes_stop_counting_as_dead(self):
        for _ in range(3):
            self.probe("/a")
            self.probe("/gone", outcome="not_found", status=410)
        notes.add(HOST, "retired", "Retired on purpose (owner, Oct 29)", when_http=[410])
        score.run(self.conn, log=lambda *_: None)
        rows = {r["resource"].rsplit("/", 1)[-1]: json.loads(r["flags"])
                for r in self.conn.execute("SELECT resource, flags FROM scores")}
        self.assertIn("retired_by_owner", rows["gone"])
        self.assertNotIn("gone", rows["gone"])
        self.assertNotIn("retired_by_owner", rows["a"], "--when-http 410 only covers the 410 routes")

    def test_intended_drops_price_warning(self):
        for _ in range(3):
            self.probe("/a", amount="500000")  # 50x the listing
        score.run(self.conn, log=lambda *_: None)
        flags = json.loads(self.conn.execute("SELECT flags FROM scores WHERE resource LIKE '%/a'").fetchone()[0])
        self.assertIn("overcharge_10x", flags)
        notes.add(f"https://{HOST}/a", "intended", "Price depends on the request")
        score.run(self.conn, log=lambda *_: None)
        flags = json.loads(self.conn.execute("SELECT flags FROM scores WHERE resource LIKE '%/a'").fetchone()[0])
        self.assertNotIn("overcharge_10x", flags)
        self.assertIn("owner_confirmed", flags)

    def test_notes_survive_round_trip_and_remove(self):
        notes.add(HOST, "retired", "x")
        self.assertEqual(len(notes.load_local()), 1)
        self.assertEqual(notes.remove(HOST), 1)
        self.assertEqual(notes.load_local(), [])


class Alerts(Fixture):
    def setUp(self):
        super().setUp()
        for path in ("/a", "/b"):
            self.probe(path)
        self.probe("/gone", outcome="not_found", status=410)
        self.probe("/gone", outcome="not_found", status=410)
        alerts.watch("owner@example.com", HOST, "Example")

    def last_email(self):
        return self.outbox()[-1].read_text()

    def test_welcome_then_quiet_then_changes(self):
        self.assertEqual(len(self.cycle()), 1)
        welcome = self.last_email()
        self.assertIn("monitoring is on for api.example.com", welcome.lower())
        self.assertIn("2 answering, 1 not answering", welcome)

        self.assertEqual(len(self.cycle()), 1, "no change means no email")

        # /a fails twice -> down (urgent); /b changes wallet (urgent) and price (rides along).
        self.probe("/a", outcome="http_error", status=503)
        self.assertEqual(len(self.cycle()), 1, "one failure isn't enough to alert")
        self.probe("/a", outcome="http_error", status=503)
        self.probe("/b", pay_to=NEW, amount="20000")
        self.assertEqual(len(self.cycle()), 2)
        body = self.last_email()
        self.assertEqual(subject(self.outbox()[-1]),
                         "Settle Watch alert: 1 endpoint stopped answering, 1 wallet change, 1 price change on api.example.com")
        self.assertIn("Stopped answering on the last two checks (1):\n  /a (HTTP 503)", body)
        self.assertIn(f"/b: now {NEW.lower()} (was {GOOD.lower()})", body)
        self.assertIn("/b: $0.01 -> $0.02", body)

        self.probe("/a")
        self.assertEqual(len(self.cycle()), 3, "coming back is urgent too")
        self.assertIn("Answering again (1):\n  /a", self.last_email())

    def test_rotating_wallets_do_not_alert(self):
        self.cycle()
        for i in range(3):
            self.probe("/b", pay_to="0x%040x" % (0xabc + i))
        self.cycle()
        self.assertEqual(len(self.outbox()), 1, "fresh payment addresses per request aren't news")

    def test_retired_endpoints_stay_quiet(self):
        notes.add(HOST, "retired", "Retired", when_http=[410])
        self.cycle()
        self.assertIn("1 marked retired at your request", self.last_email())

    def test_watched_endpoints_are_in_the_probe_list(self):
        self.assertEqual(set(alerts.watched_resources(self.conn)),
                         {f"https://{HOST}/a", f"https://{HOST}/b", f"https://{HOST}/gone"})

    def test_smtp_path(self):
        sent = []

        class FakeSMTP:
            def __init__(self, host, port, timeout=None): sent.append(("connect", host, port))
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self): sent.append(("tls",))
            def login(self, u, p): sent.append(("login", u))
            def send_message(self, msg): sent.append(("send", msg["To"], msg["Subject"]))

        config.SMTP_USER, config.SMTP_PASS = "me@gmail.com", "app-password"
        real = alerts.smtplib.SMTP
        alerts.smtplib.SMTP = FakeSMTP
        try:
            alerts.run(self.conn, log=lambda *_: None)
        finally:
            alerts.smtplib.SMTP = real
        self.assertEqual(sent[0], ("connect", "smtp.gmail.com", 587))
        self.assertIn(("tls",), sent)
        self.assertIn(("login", "me@gmail.com"), sent)
        self.assertEqual(sent[-1][1], "owner@example.com")
        log = self.conn.execute("SELECT delivery FROM alert_log").fetchall()
        self.assertEqual(log[-1][0], "sent")

    def test_failed_send_retries_next_cycle(self):
        class Broken:
            def __init__(self, *a, **k): raise OSError("network down")
        config.SMTP_USER, config.SMTP_PASS = "me@gmail.com", "pw"
        real = alerts.smtplib.SMTP
        alerts.smtplib.SMTP = Broken
        try:
            alerts.run(self.conn, log=lambda *_: None)
        finally:
            alerts.smtplib.SMTP = real
        state = self.conn.execute("SELECT COUNT(*) FROM alert_state").fetchone()[0]
        self.assertEqual(state, 0, "a failed welcome must be retried, not recorded as delivered")


class Bundling(Fixture):
    """One email per person, a daily digest for small changes, and changes that undo themselves dropped."""

    def setUp(self):
        super().setUp()
        for path in ("/a", "/b"):
            self.probe(path)
        alerts.watch("owner@example.com", HOST, "Example")
        self.cycle()  # welcome

    def test_price_changes_wait_for_the_daily_digest_and_net_out(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        self.probe("/b", amount="20000")
        alerts.run(self.conn, log=lambda *_: None, now=now)
        self.assertEqual(len(self.outbox()), 1, "a price change alone waits for the digest")
        self.probe("/b", amount="10000")  # and moves back before the digest goes out
        self.probe("/a", amount="30000")
        alerts.run(self.conn, log=lambda *_: None, now=now + timedelta(hours=21))
        self.assertEqual(len(self.outbox()), 2)
        body = self.outbox()[-1].read_text()
        self.assertEqual(subject(self.outbox()[-1]), "Settle Watch daily: 1 price change on api.example.com")
        self.assertIn("/a: $0.01 -> $0.03", body)
        self.assertNotIn("/b:", body, "a price that moved and moved back isn't news")
        self.assertIn("once a day at most", body)
        alerts.run(self.conn, log=lambda *_: None, now=now + timedelta(hours=22))
        self.assertEqual(len(self.outbox()), 2, "nothing left waiting")

    def test_two_services_one_email_and_a_welcome_for_the_new_one(self):
        from datetime import datetime, timedelta, timezone
        other = "api.other.example"
        for i in range(25):
            res = f"https://{other}/r{i}"
            self.conn.execute("INSERT INTO endpoints (resource, host, first_seen, last_seen, method, network, asset, "
                              "pay_to, listed_amount, listed_usd, l30_payers, catalog_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                              (res, other, "2026-10-29T00:00:00Z", "2026-10-29T00:00:00Z", "GET", "eip155:8453",
                               USDC, GOOD, "10000", 0.01, 1, "{}"))
        self.conn.commit()
        alerts.watch("owner@example.com", other, "Example")
        self.cycle()
        mails = [m.read_text() for m in self.outbox()]
        self.assertEqual(len(mails), 2)
        self.assertIn(f"monitoring is on for {other}", mails[-1].lower(), "the second service gets its own welcome")
        self.assertFalse(any("Newly listed" in m for m in mails), "not 25 'newly listed' lines")
        # Now something on each: one email covering both, long lists capped.
        self.probe("/a", outcome="http_error", status=503)
        self.probe("/a", outcome="http_error", status=503)
        for i in range(25, 40):
            res = f"https://{other}/r{i}"
            self.conn.execute("INSERT INTO endpoints (resource, host, first_seen, last_seen, method, network, asset, "
                              "pay_to, listed_amount, listed_usd, l30_payers, catalog_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                              (res, other, "2026-10-29T00:00:00Z", "2026-10-29T00:00:00Z", "GET", "eip155:8453",
                               USDC, GOOD, "10000", 0.01, 1, "{}"))
        self.conn.commit()
        alerts.run(self.conn, log=lambda *_: None, now=datetime.now(timezone.utc) + timedelta(hours=1))
        self.assertEqual(len(self.outbox()), 3)
        body = self.outbox()[-1].read_text()
        self.assertEqual(subject(self.outbox()[-1]), "Settle Watch alert: 1 endpoint stopped answering, 15 new listings on 2 services")
        self.assertIn("Newly listed (15):", body)
        self.assertIn("and 5 more", body)
        self.assertIn("/a (HTTP 503)", body)

    def test_unwatched_services_drop_their_waiting_changes(self):
        self.probe("/b", amount="20000")
        self.cycle()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM alert_pending").fetchone()[0], 1)
        alerts.unwatch("owner@example.com", HOST)
        alerts.watch("owner@example.com", "api.elsewhere.example")
        self.cycle()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM alert_pending").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
