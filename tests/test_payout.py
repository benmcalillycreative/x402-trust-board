"""Verified payout addresses: signatures, the proof formats, and how they change scores.

Runs offline. The signatures below were made with a throwaway key (0x1111…1111);
the RPC call for smart-contract wallets is replaced with a stub.
"""

import http.server
import json
import threading
import unittest
from collections import defaultdict

from x402trust import config, ethsig, payout
from x402trust.score import Context, severity, wallet_flags

ADDR = "0x19e7e376e7c213b7e7e7e46cc70a5dd086daff2a"
SIG_API = ("0x0a309a77c37033e8b494cd1022f20a8e91be5d78e3af05a6fe4238d52fdf55f709ea729b23183bdadff8"
           "c28eb4af2b005a3faa6e5c332f948b454c5cdfba352b1c")  # Domain: api.example.com
SIG_ROOT = ("0x9e1f2439cc720198fe12a51cda95d938c0bc0e140944c65872c578885c57bcde5cd84e4a1118d86f6b4bab"
            "f381a63615d9564753932aef97c5fb681ea01528d91b")  # Domain: example.com
SIG_LOCAL = ("0xd22af2f9fa63ad6328000959329aea9caf38d1b9771b2bf156d08ee1113379ce1c0fbf9c37db04dce19062dcd4"
             "390bf4c968db5de9f7e8fbf70894126c74f94e1c")  # Domain: 127.0.0.1
OTHER = "0x" + "22" * 20


def no_contract(*_):
    return None  # eth_getCode says: not a contract


class Signatures(unittest.TestCase):
    def setUp(self):
        self._rpc, payout._rpc = payout._rpc, no_contract

    def tearDown(self):
        payout._rpc = self._rpc

    def test_keccak_known_value(self):
        self.assertEqual(ethsig.keccak256(b"").hex(),
                         "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")

    def test_good_signature(self):
        self.assertEqual(payout.check_signature("api.example.com", ADDR, "2026-11-04", SIG_API), ("wallet", ""))

    def test_checksummed_address_still_matches(self):
        mixed = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        self.assertEqual(payout.check_signature("API.example.com", mixed, "2026-11-04", SIG_API)[0], "wallet")

    def test_signature_for_another_domain_fails(self):
        self.assertEqual(payout.check_signature("evil.com", ADDR, "2026-11-04", SIG_API)[0], "invalid")

    def test_signature_for_another_address_fails(self):
        self.assertEqual(payout.check_signature("api.example.com", OTHER, "2026-11-04", SIG_API)[0], "invalid")

    def test_wrong_date_fails(self):
        self.assertEqual(payout.check_signature("api.example.com", ADDR, "2026-11-05", SIG_API)[0], "invalid")

    def test_unsigned_and_non_evm(self):
        self.assertEqual(payout.check_signature("api.example.com", ADDR, None, None)[0], "unsigned")
        self.assertEqual(payout.check_signature("x.com", "So1anaAddr111", "2026-11-04", "0xab")[0],
                         "unverifiable")

    def test_smart_wallet_via_erc1271(self):
        calls = []

        def fake_rpc(method, params):
            calls.append(method)
            return "0x6080" if method == "eth_getCode" else "0x1626ba7e" + "00" * 28

        payout._rpc = fake_rpc
        self.assertEqual(payout.check_signature("api.example.com", OTHER, "2026-11-04", SIG_API)[0], "contract")
        self.assertEqual(calls, ["eth_getCode", "eth_call"])


class Formats(unittest.TestCase):
    def test_file(self):
        text = json.dumps({"version": 1, "domain": "api.example.com",
                           "payTo": [{"address": ADDR, "issued": "2026-11-04", "signature": SIG_API}]})
        entries, problem = payout.parse_file(text, "api.example.com")
        self.assertEqual(problem, "")
        self.assertEqual(entries, [("api.example.com", ADDR, "2026-11-04", SIG_API)])

    def test_file_for_another_host_is_rejected(self):
        text = json.dumps({"domain": "example.com", "payTo": [ADDR]})
        self.assertEqual(payout.parse_file(text, "api.example.com")[0], [])

    def test_txt(self):
        rec = f"v=x402payto1; a={ADDR}; d=2026-11-04; s={SIG_ROOT}"
        self.assertEqual(payout.parse_txt([rec, "v=spf1 -all"], "example.com"),
                         [("example.com", ADDR, "2026-11-04", SIG_ROOT)])

    def test_dns_names_skip_shared_hosting(self):
        self.assertEqual(payout.dns_names("api.example.com"), ["api.example.com", "example.com"])
        self.assertEqual(payout.dns_names("thing.onrender.com"), [])
        self.assertEqual(payout.dns_names("shop.foo.co.uk"), ["shop.foo.co.uk", "foo.co.uk"])
        self.assertEqual(payout.dns_names("127.0.0.1"), [])


class Manifest(unittest.TestCase):
    def test_reads_pay_to_from_a_service_manifest(self):
        text = json.dumps({"spec": "agent402-service-manifest/1", "payment": {"x402": {
            "payTo": ADDR, "payToByNetwork": {"base": ADDR, "eip155:8453": ADDR}}},
            "tools": [{"route": "/a", "payTo": ADDR}, {"route": "/b", "payTo": "not an address"}]})
        self.assertEqual([a.lower() for a in payout.parse_manifest(text)], [ADDR])

    def test_junk_is_ignored(self):
        self.assertEqual(payout.parse_manifest("<html>"), [])


class Scoring(unittest.TestCase):
    def ctx(self, declared):
        c = Context.__new__(Context)
        c.wallet_domains = defaultdict(set, {OTHER: {"example.com"}})
        c.rotating_hosts = set()
        c.payout = {"api.example.com": declared}
        return c

    def ep(self, listed=OTHER):
        return {"host": "api.example.com", "resource": "https://api.example.com/x", "catalog_json": json.dumps(
            {"accepts": [{"scheme": "exact", "network": "eip155:8453", "amount": "1000", "payTo": listed}]})}

    def probes(self, *addrs):
        return [{"quoted_pay_to": a, "quoted_network": "eip155:8453"} for a in addrs]

    def test_verified_address_overrides_a_stale_listing(self):
        # Listing says OTHER, the endpoint switched to ADDR, and the seller proved ADDR is theirs.
        flags = wallet_flags(self.ep(), self.probes(OTHER, OTHER, ADDR), self.ctx({ADDR: "site_signed"}))
        self.assertEqual(flags, {"payout_verified"})
        self.assertEqual(severity("payout_verified"), "good")

    def test_dns_proof_is_its_own_level(self):
        flags = wallet_flags(self.ep(ADDR), self.probes(ADDR), self.ctx({ADDR: "dns_signed"}))
        self.assertEqual(flags, {"payout_verified_dns"})

    def test_unsigned_declaration(self):
        flags = wallet_flags(self.ep(ADDR), self.probes(ADDR), self.ctx({ADDR: "site"}))
        self.assertEqual(flags, {"payout_declared"})

    def test_paying_somewhere_the_site_doesnt_list_is_red(self):
        flags = wallet_flags(self.ep(OTHER), self.probes(OTHER), self.ctx({ADDR: "site_signed"}))
        self.assertEqual(flags, {"payout_mismatch"})
        self.assertEqual(severity("payout_mismatch"), "red")

    def test_manifest_confirms_but_never_accuses(self):
        # Like TurboShop: listing names the old wallet, the endpoint and its own manifest the new one.
        flags = wallet_flags(self.ep(OTHER), self.probes(OTHER, ADDR), self.ctx({ADDR: "manifest"}))
        self.assertEqual(flags, {"payout_declared"})
        # A manifest that doesn't mention the address the endpoint asks for proves nothing either way.
        flags = wallet_flags(self.ep(OTHER), self.probes(OTHER), self.ctx({ADDR: "manifest"}))
        self.assertNotIn("payout_mismatch", flags)

    def test_fresh_address_per_request_is_not_a_mismatch(self):
        flags = wallet_flags(self.ep(OTHER), self.probes("0x" + "aa" * 20, "0x" + "bb" * 20),
                             self.ctx({ADDR: "site"}))
        self.assertNotIn("payout_mismatch", flags)

    def test_rotating_hosts_are_never_called_mismatched(self):
        c = self.ctx({ADDR: "site"})
        c.rotating_hosts = {"api.example.com"}
        flags = wallet_flags(self.ep(OTHER), self.probes(OTHER), c)
        self.assertNotIn("payout_mismatch", flags)


class DnsDecides(unittest.TestCase):
    """Once a domain publishes a DNS proof, a file-only address doesn't count: a hijacked web server can rewrite
    its own .well-known file and sign it with a wallet of its own, but it can't change the DNS."""

    def test_file_only_address_does_not_count_when_dns_exists(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.executescript(payout.SCHEMA)
        rows = [("api.example.com", "dns", "example.com", ADDR, "2026-11-04", "wallet", ""),
                ("api.example.com", "site", "api.example.com", OTHER, "2026-11-19", "wallet", ""),  # attacker's file entry
                ("api.example.com", "site", "api.example.com", ADDR, "2026-11-04", "unsigned", ""),
                ("api.other.com", "site", "api.other.com", OTHER, "2026-11-04", "wallet", "")]  # file-only seller
        conn.executemany("INSERT INTO payout_proofs (host, source, domain, address, issued, signer, detail) "
                         "VALUES (?,?,?,?,?,?,?)", rows)
        levels = payout.load(conn)
        self.assertEqual(levels["api.example.com"], {ADDR: "dns_signed"})
        self.assertEqual(levels["api.other.com"], {OTHER: "site_signed"}, "file-only sellers keep their file proof")
        c = Context.__new__(Context)
        c.wallet_domains, c.rotating_hosts, c.payout = defaultdict(set), set(), levels
        ep = {"host": "api.example.com", "resource": "https://api.example.com/x", "catalog_json": json.dumps(
            {"accepts": [{"scheme": "exact", "network": "eip155:8453", "amount": "1000", "payTo": ADDR}]})}
        flags = wallet_flags(ep, [{"quoted_pay_to": OTHER, "quoted_network": "eip155:8453"}], c)
        self.assertEqual(flags, {"payout_mismatch"}, "the attacker's self-signed file entry doesn't clear the check")


class FileRotationHold(unittest.TestCase):
    """A new address in a seller's proof waits HOLD_HOURS before it counts, if the host already had a proven address:
    a hijacked server (file) or domain (DNS) can publish and sign its own, so the board keeps trusting the old one."""

    def setUp(self):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(payout.SCHEMA)

    def check(self, host, entries, now):
        payout.save(self.conn, host, [{"source": s, "domain": host, "address": a, "issued": "2026-11-01",
                                       "signer": signer, "detail": ""} for s, a, signer in entries], "", now=now)

    def test_swapped_file_address_is_held_then_counts(self):
        self.check("api.shop.com", [("site", ADDR, "wallet")], "2026-11-10T00:00:00Z")
        self.check("api.shop.com", [("site", ADDR, "wallet")], "2026-11-18T00:00:00Z")
        self.check("api.shop.com", [("site", OTHER, "wallet")], "2026-11-19T00:00:00Z")  # file rewritten
        self.assertEqual(payout.load(self.conn, "2026-11-19T01:00:00Z")["api.shop.com"], {ADDR: "site_signed"})
        self.assertEqual(payout.held(self.conn, "2026-11-19T01:00:00Z"), {"api.shop.com": [OTHER]})
        c = Context.__new__(Context)
        c.wallet_domains, c.rotating_hosts = defaultdict(set), set()
        c.payout = payout.load(self.conn, "2026-11-19T01:00:00Z")
        ep = {"host": "api.shop.com", "resource": "https://api.shop.com/x", "catalog_json": json.dumps(
            {"accepts": [{"scheme": "exact", "network": "eip155:8453", "amount": "1000", "payTo": OTHER}]})}
        self.assertEqual(wallet_flags(ep, [{"quoted_pay_to": OTHER, "quoted_network": "eip155:8453"}], c),
                         {"payout_mismatch"})
        later = payout.load(self.conn, "2026-11-22T00:00:01Z")["api.shop.com"]
        self.assertEqual(later, {OTHER: "site_signed"}, "after the hold the new address counts, the old one is gone")
        self.assertEqual(payout.held(self.conn, "2026-11-22T00:00:01Z"), {})

    def test_added_address_is_held_too(self):
        self.check("api.shop.com", [("site", ADDR, "wallet")], "2026-11-10T00:00:00Z")
        self.check("api.shop.com", [("site", ADDR, "wallet"), ("site", OTHER, "wallet")], "2026-11-19T00:00:00Z")
        self.assertEqual(payout.load(self.conn, "2026-11-19T01:00:00Z")["api.shop.com"], {ADDR: "site_signed"})

    def test_first_proof_and_fresh_upgrade_are_not_held(self):
        self.check("api.new.com", [("site", OTHER, "wallet")], "2026-11-19T00:00:00Z")
        self.assertEqual(payout.load(self.conn, "2026-11-19T01:00:00Z")["api.new.com"], {OTHER: "site_signed"})
        # Already on the board before this table existed: first check after the update sets a baseline, no hold.
        self.check("api.old.com", [("site", ADDR, "wallet")], "2026-11-19T00:00:00Z")
        self.assertEqual(payout.load(self.conn, "2026-11-19T01:00:00Z")["api.old.com"], {ADDR: "site_signed"})

    def test_stale_anchor_does_not_hold(self):
        self.check("api.shop.com", [("site", ADDR, "wallet")], "2026-09-01T00:00:00Z")
        self.check("api.shop.com", [], "2026-09-02T00:00:00Z")  # took the file down for months
        self.check("api.shop.com", [("site", OTHER, "wallet")], "2026-11-19T00:00:00Z")
        self.assertEqual(payout.load(self.conn, "2026-11-19T01:00:00Z")["api.shop.com"], {OTHER: "site_signed"})

    def test_dns_swap_is_held_too(self):
        """A registrar takeover lets someone publish their own signed DNS record; the board remembers the old one."""
        self.check("api.shop.com", [("dns", ADDR, "wallet")], "2026-11-10T00:00:00Z")
        self.check("api.shop.com", [("dns", OTHER, "wallet")], "2026-11-19T00:00:00Z")
        self.assertEqual(payout.load(self.conn, "2026-11-19T01:00:00Z")["api.shop.com"], {ADDR: "dns_signed"})
        self.assertEqual(payout.held(self.conn, "2026-11-19T01:00:00Z"), {"api.shop.com": [OTHER]})
        self.assertEqual(payout.load(self.conn, "2026-11-22T01:00:00Z")["api.shop.com"], {OTHER: "dns_signed"})

    def test_planned_rotation_passes(self):
        """List the new address next to the old one, wait out the hold, then switch: no mismatch at any point."""
        self.check("api.shop.com", [("dns", ADDR, "wallet")], "2026-11-01T00:00:00Z")
        self.check("api.shop.com", [("dns", ADDR, "wallet"), ("dns", OTHER, "wallet")], "2026-11-10T00:00:00Z")
        self.assertEqual(payout.load(self.conn, "2026-11-10T01:00:00Z")["api.shop.com"], {ADDR: "dns_signed"})
        self.check("api.shop.com", [("dns", ADDR, "wallet"), ("dns", OTHER, "wallet")], "2026-11-13T01:00:00Z")
        self.assertEqual(payout.load(self.conn, "2026-11-13T01:00:00Z")["api.shop.com"],
                         {ADDR: "dns_signed", OTHER: "dns_signed"})
        self.check("api.shop.com", [("dns", OTHER, "wallet")], "2026-11-20T00:00:00Z")  # old one removed
        self.assertEqual(payout.load(self.conn, "2026-11-20T01:00:00Z")["api.shop.com"], {OTHER: "dns_signed"})


class CheckHost(unittest.TestCase):
    """Serve a real .well-known file locally and run the whole check."""

    def test_check_host_end_to_end(self):
        body = json.dumps({"version": 1, "domain": "127.0.0.1", "payTo": [
            {"address": ADDR, "issued": "2026-11-04", "signature": SIG_LOCAL},
            {"address": OTHER, "issued": "2026-11-04", "signature": SIG_API},  # signed for another domain
        ]}).encode()

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == payout.WELL_KNOWN:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old = (payout.fetch_file, config.ALLOW_PRIVATE, payout._rpc)
        port = srv.server_address[1]

        def fetch_http(host, path=payout.WELL_KNOWN):  # the real one only speaks https
            import urllib.error
            import urllib.request
            try:
                with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=5) as r:
                    return r.read().decode(), ""
            except urllib.error.HTTPError as exc:
                return None, f"HTTP {exc.code}"

        payout.fetch_file, config.ALLOW_PRIVATE, payout._rpc = fetch_http, True, no_contract
        try:
            proofs, summary = payout.check_host("127.0.0.1")
        finally:
            payout.fetch_file, config.ALLOW_PRIVATE, payout._rpc = old
            srv.shutdown()
        by_addr = {p["address"]: p["signer"] for p in proofs}
        self.assertEqual(by_addr, {ADDR: "wallet", OTHER: "invalid"})
        self.assertIn("site file: found", summary)


if __name__ == "__main__":
    unittest.main()
