import unittest

from spend import report

USAGE = {"input_tokens": 1000, "cache_read_input_tokens": 0, "output_tokens": 100}


class ReportTest(unittest.TestCase):
    def test_priced_record(self):
        self.assertEqual(report({"model": "demo-model", "usage": USAGE})["cost_usd"], 0.0045)


if __name__ == "__main__":
    unittest.main()
