"""Host-level wallet patterns, tested on the scoring context directly."""

import unittest
from collections import defaultdict

from x402trust.score import Context

LISTED = "0x" + "11" * 20


def ctx_with_listed():
    c = Context.__new__(Context)  # skip the DB scan; only wallet_domains matters here
    c.wallet_domains = defaultdict(set, {LISTED: {"example.com"}})
    c.rotating_hosts = set()
    return c


def addr(n):
    return "0x%040x" % n


class RotatingHosts(unittest.TestCase):
    def test_fresh_address_every_request(self):
        c = ctx_with_listed()
        c.find_rotating_hosts({"a.example": [addr(i) for i in range(1, 5)]})
        self.assertIn("a.example", c.rotating_hosts)

    def test_per_caller_deposit_addresses_reused_for_a_while(self):
        # Webacy/Stripe pattern: 7 checks, 4 addresses, the first reused across endpoints,
        # none ever in a listing.
        quotes = [addr(1)] * 3 + [addr(2)] * 2 + [addr(3), addr(4)]
        c = ctx_with_listed()
        c.find_rotating_hosts({"api.example": quotes})
        self.assertIn("api.example", c.rotating_hosts)

    def test_single_hijack_wallet_is_not_rotating(self):
        c = ctx_with_listed()
        c.find_rotating_hosts({"h.example": [addr(9)] * 6})
        self.assertNotIn("h.example", c.rotating_hosts)

    def test_repeats_alongside_the_listed_wallet_are_not_rotating(self):
        quotes = [LISTED] * 3 + [addr(1)] * 3 + [addr(2), addr(3)]
        c = ctx_with_listed()
        c.find_rotating_hosts({"m.example": quotes})
        self.assertNotIn("m.example", c.rotating_hosts)


if __name__ == "__main__":
    unittest.main()


class PriceFlagsFollowCurrentPrice(unittest.TestCase):
    """A seller who fixes an overcharge should see the flag clear on the next check."""

    USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

    def ep(self):
        import json
        return {"catalog_json": json.dumps({"accepts": [{
            "scheme": "exact", "network": "eip155:8453", "amount": "3000", "asset": self.USDC}]})}

    def quotes(self, *amounts):
        return [{"quoted_amount": str(a), "quoted_network": "eip155:8453", "quoted_asset": self.USDC}
                for a in amounts]

    def test_overcharge_then_fixed_is_clean(self):
        from x402trust.score import price_flags
        self.assertEqual(price_flags(self.ep(), self.quotes(10000, 10000, 2000)), set())

    def test_still_overcharging_reports_the_worst(self):
        from x402trust.score import price_flags
        self.assertEqual(price_flags(self.ep(), self.quotes(10000, 40000, 10000)), {"overcharge_10x"})

    def test_currently_overcharging_after_being_fine(self):
        from x402trust.score import price_flags
        self.assertEqual(price_flags(self.ep(), self.quotes(3000, 3000, 9000)), {"overcharge_2x"})


class NetworkChange(unittest.TestCase):
    """Going live from a test network brings a new address; that's not a hijack."""

    OLD, NEW = "0x" + "aa" * 20, "0x" + "bb" * 20

    def ep(self, network):
        import json
        return {"catalog_json": json.dumps({"accepts": [{
            "scheme": "exact", "network": network, "amount": "1000", "payTo": self.OLD,
            "asset": "0x036cbd53842c5426634e7929541ec2318f3dcf7e"}]}),
            "resource": "https://api.example.com/x", "host": "api.example.com"}

    def probe(self, network, pay_to):
        return {"quoted_pay_to": pay_to, "quoted_network": network}

    def ctx(self):
        c = Context.__new__(Context)
        c.wallet_domains = defaultdict(set, {self.OLD: {"example.com"}})
        c.rotating_hosts = set()
        return c

    def test_testnet_to_mainnet_is_network_changed(self):
        from x402trust.score import wallet_flags
        probes = [self.probe("eip155:84532", self.OLD)] * 3 + [self.probe("eip155:8453", self.NEW)]
        self.assertEqual(wallet_flags(self.ep("eip155:84532"), probes, self.ctx()), {"network_changed"})

    def test_same_network_switch_is_still_red(self):
        from x402trust.score import wallet_flags
        probes = [self.probe("eip155:8453", self.OLD)] * 3 + [self.probe("eip155:8453", self.NEW)]
        self.assertEqual(wallet_flags(self.ep("eip155:8453"), probes, self.ctx()), {"wallet_switched"})


class LiveListingTestPayment(NetworkChange):
    def test_mainnet_listing_asking_testnet_is_a_warning(self):
        from x402trust.score import wallet_flags, severity
        probes = [self.probe("eip155:84532", self.NEW)]
        flags = wallet_flags(self.ep("eip155:8453"), probes, self.ctx())
        self.assertEqual(flags, {"test_network_only"})
        self.assertEqual(severity("test_network_only"), "warn")
