from pathlib import Path
import unittest


class StartKeySourcesTest(unittest.TestCase):
    def test_start_message_names_demo_and_live_key_sources(self):
        source = Path("bot/main.py").read_text(encoding="utf-8")

        self.assertIn("https://demo.binance.com/en/my/settings/api-management", source)
        self.assertIn("https://www.binance.com/en/my/settings/api-management", source)
        self.assertIn("/setkey exchange_provider binance_testnet", source)
        self.assertIn("/setkey exchange_provider binance", source)


if __name__ == "__main__":
    unittest.main()
