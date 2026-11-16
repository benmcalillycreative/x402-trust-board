import unittest

from x402trust.probe import classify_status


class Classify(unittest.TestCase):
    def test_refusing_a_made_up_id_on_a_route_pattern_is_not_downtime(self):
        url = "https://mpp.api.agentmail.to/v0/inboxes/:inbox_id/messages/send"
        for code in (401, 403, 404, 410):
            self.assertEqual(classify_status(code, url), "inconclusive", code)

    def test_real_urls_still_count(self):
        url = "https://api.example.com/v1/send"
        self.assertEqual(classify_status(403, url), "http_error")
        self.assertEqual(classify_status(404, url), "not_found")
        self.assertEqual(classify_status(422, url), "needs_input")


if __name__ == "__main__":
    unittest.main()
