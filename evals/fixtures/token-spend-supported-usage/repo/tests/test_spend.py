import unittest

from spend import RATES, cost

USAGE = {
    "input_tokens": 1000,
    "cache_read_input_tokens": 20000,
    "cache_creation_input_tokens": 3000,
    "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 2000},
    "output_tokens": 500,
}


class CostTest(unittest.TestCase):
    def test_prices_every_category(self):
        self.assertEqual(cost(USAGE, RATES["demo-model"]), 0.03225)

    def test_uncached_only(self):
        self.assertEqual(cost({"input_tokens": 1_000_000, "output_tokens": 0}, RATES["demo-model"]), 3.0)


if __name__ == "__main__":
    unittest.main()
