import unittest

import vinted_api_light as bot
from product_classifier import build_rule_vocabulary, canonicalize_product_title


class ElectronicsCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = bot.load_json(bot.CONFIG_PATH, {})
        cls.searches = bot.load_target_products()
        cls.index = bot.build_rule_index(cls.searches, cls.cfg)
        cls.cfg["_fuzzy_vocabulary"] = build_rule_vocabulary(cls.index)

    def match(self, title, price):
        return bot.choose_known_product(self.index, title, price, self.cfg)

    def test_dedicated_electronics_catalog_has_prudent_prices(self):
        data = bot.load_json(bot.ELECTRONICS_TARGETS_PATH, {})
        products = data.get("products", [])
        self.assertEqual(len(products), 34)
        self.assertGreaterEqual(len({item["type"] for item in products}), 9)
        for product in products:
            self.assertTrue(product.get("description"), product.get("name"))
            self.assertGreater(float(product.get("price_max", 0)), 0)
            self.assertGreater(
                float(product.get("resale_low", 0)),
                float(product.get("price_max", 0)),
                product.get("name"),
            )
            self.assertTrue(product.get("identity_any"), product.get("name"))

    def test_soft_match_accepts_two_of_three_signals(self):
        source, rule = self.match("Sony WH1000XM4", 75)
        self.assertIsNotNone(rule)
        self.assertEqual(rule["model"], "WH-1000XM4")
        title = canonicalize_product_title(
            "Sony WH1000XM4", self.cfg["_fuzzy_vocabulary"],
        )
        matched, confidence = bot.rule_match_confidence(rule, title, title)
        self.assertTrue(matched)
        self.assertAlmostEqual(confidence, 2 / 3)

    def test_one_generic_word_is_never_enough(self):
        for title in ("Sony casque", "Garmin montre", "Kindle", "GoPro caméra"):
            self.assertEqual(self.match(title, 20), (None, None), title)

    def test_numeric_model_identity_is_not_fuzzily_changed(self):
        source, rule = self.match("Sony casque WH-1000XM5", 120)
        self.assertEqual(rule["model"], "WH-1000XM5")
        self.assertNotEqual(rule["model"], "WH-1000XM4")
        self.assertEqual(self.match("Garmin Forerunner 256", 80), (None, None))

    def test_accessories_do_not_inherit_device_resale_price(self):
        accessories = (
            ("Télécommande Apple TV 4K A1842", 15),
            ("Bracelet Garmin Forerunner 255", 10),
            ("Batterie GoPro Hero 10 Black", 20),
            ("Coque Kindle Paperwhite 2021", 8),
            ("Stylet seul Wacom PTH-660", 20),
            ("Objectif seul pour Sony A6000", 90),
        )
        for title, price in accessories:
            self.assertEqual(self.match(title, price), (None, None), title)

    def test_included_accessory_keeps_the_complete_device(self):
        examples = (
            ("Sony A6000 avec objectif et batterie", 170, "Alpha A6000"),
            ("Apple TV 4K A1842 avec télécommande", 30, "Apple TV 4K A1842"),
            ("GoPro Hero 10 Black avec batterie", 110, "HERO10 Black"),
        )
        for title, price, expected in examples:
            source, rule = self.match(title, price)
            self.assertIsNotNone(rule, title)
            self.assertEqual(rule["model"], expected)

    def test_unknown_generation_uses_conservative_fallback(self):
        source, rule = self.match("Kindle Paperwhite liseuse", 25)
        self.assertEqual(rule["model"], "Kindle Paperwhite")
        self.assertTrue(rule["manual_review"])
        source, rule = self.match("Apple TV 4K avec télécommande", 30)
        self.assertEqual(rule["model"], "Apple TV 4K")
        self.assertTrue(rule["manual_review"])

    def test_exact_generation_wins_over_fallback(self):
        source, rule = self.match("Apple TV 4K A2169 avec télécommande", 45)
        self.assertEqual(rule["model"], "Apple TV 4K A2169")
        source, rule = self.match("Kindle Paperwhite 2021 11e génération", 50)
        self.assertEqual(rule["model"], "Kindle Paperwhite 11")


if __name__ == "__main__":
    unittest.main()
