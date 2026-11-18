"""A zero-address quote the seller marks as an example (extra.example) is information,
not a burn-address red flag, and needs no owner note."""

import json
import unittest
from collections import defaultdict

from x402trust.parse import is_example, requirement_fields
from x402trust.score import Context, severity, wallet_flags

ZERO = "0x0000000000000000000000000000000000000000"
REAL = "0x" + "ab" * 20


def ctx():
    c = Context.__new__(Context)
    c.wallet_domains = defaultdict(set)
    c.rotating_hosts = set()
    c.payout = {}
    return c


def ep(listed=REAL):
    return {"host": "api.example.com", "resource": "https://api.example.com/x", "catalog_json": json.dumps(
        {"accepts": [{"scheme": "exact", "network": "eip155:8453", "amount": "1000", "payTo": listed}]})}


def probe(addr, example=None):
    return {"quoted_pay_to": addr, "quoted_network": "eip155:8453", "quoted_example": example}


class ExampleOffer(unittest.TestCase):
    def test_marker_is_read(self):
        self.assertTrue(is_example({"extra": {"example": True}}))
        self.assertTrue(is_example({"extra": {"isExample": "true"}}))
        self.assertTrue(is_example({"example": True}))
        self.assertFalse(is_example({"extra": {"example": "no"}}))
        self.assertFalse(is_example({"extra": None}))
        self.assertTrue(requirement_fields({"payTo": ZERO, "extra": {"example": True}})["example"])

    def test_marked_zero_address_is_an_example(self):
        flags = wallet_flags(ep(), [probe(ZERO, 1)], ctx())
        self.assertEqual(flags, {"example_offer"})
        self.assertEqual(severity("example_offer"), "info")

    def test_unmarked_zero_address_is_still_red(self):
        self.assertEqual(wallet_flags(ep(), [probe(ZERO)], ctx()), {"burn_address"})

    def test_listing_without_wallet_marked_example(self):
        self.assertEqual(wallet_flags(ep(listed=None), [probe(ZERO, 1)], ctx()), {"example_offer"})


if __name__ == "__main__":
    unittest.main()
