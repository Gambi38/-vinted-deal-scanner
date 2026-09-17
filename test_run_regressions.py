import unittest

import vinted_api_light as bot


class RunRegressionTests(unittest.TestCase):
    def test_logged_accessories_cannot_be_devices(self):
        cases = [
            ("SMARTPHONE", "Iphone 14 pro max hülle."),
            ("SMARTPHONE", "iPhone 14 cases"),
            ("SMARTPHONE", "Lot de 3 coques iPhone 14 PRO Girly en très bon état"),
            ("SMARTPHONE", "Pantalla iPhone 13 Soft Oled NUEVA"),
            ("SMARTPHONE", "Salva schermo Iphone 15-16"),
            ("TABLET", "Logitech Folio Touch iPad Air 4/5 Tastiera Italiana Trackpad"),
            ("GAME", "Kinder - minecraft"),
            ("SMARTPHONE", "leeg iPhone 15 doosje"),
        ]
        for kind, title in cases:
            with self.subTest(title=title):
                self.assertFalse(bot.strict_product_type_check(
                    {}, {"product_type": kind}, title,
                    item_text="Très bon état, comme neuf",
                )[0])

    def test_complete_phone_with_included_case_survives(self):
        self.assertTrue(bot.strict_product_type_check(
            {}, {"product_type": "SMARTPHONE"},
            "iPhone 14 128GB avec sa coque fonctionne bien",
        )[0])
