import unittest

from cheapest_finder import (
    cost, find_cheapest, load_bad_terms, parse_card, parse_money, rejection_reason,
)

BAD = load_bad_terms()


def item(i, title, price):
    return {"id": i, "title": title, "price": {"amount": str(price)}}


class ExactMatchTests(unittest.TestCase):
    def reason(self, title, query="iPhone 11"):
        return rejection_reason(title, query, BAD)

    def test_exact_model_is_accepted(self):
        for title in ("iPhone 11 64 Go noir", "Apple iPhone 11 128gb", "iphone 11"):
            self.assertIsNone(self.reason(title), title)

    def test_other_variants_and_models_are_rejected(self):
        for title in ("iPhone 11 Pro", "iPhone 11 Pro Max 256 Go", "iPhone 12",
                      "iPhone 11 Mini", "iPhone XR"):
            self.assertIsNotNone(self.reason(title), title)

    def test_accessories_are_rejected_unless_requested(self):
        self.assertIsNotNone(self.reason("Coque iPhone 11"))
        self.assertIsNotNone(self.reason("Chargeur pour iPhone 11"))
        self.assertIsNone(rejection_reason("Coque iPhone 11 rose", "coque iPhone 11", BAD))

    def test_broken_fake_wanted_and_lots_are_rejected(self):
        for title in ("iPhone 11 HS", "iPhone 11 pour pièces", "iPhone 11 écran cassé",
                      "Recherche iPhone 11", "Lot de 3 iPhone 11", "iPhone 11 réplique"):
            self.assertIsNotNone(self.reason(title), title)

    def test_every_query_word_is_required(self):
        self.assertIsNotNone(self.reason("Nintendo Switch", "Nintendo Switch OLED"))
        self.assertIsNone(self.reason("Nintendo Switch OLED blanche", "Nintendo Switch OLED"))

    def test_switch_lite_is_not_a_switch(self):
        self.assertIsNotNone(self.reason("Nintendo Switch Lite", "Nintendo Switch"))


class RealWorldTitleTests(unittest.TestCase):
    """Titres réellement vus sur Vinted lors du test en direct."""

    def test_empty_boxes_and_parts_in_any_language_are_rejected(self):
        for title in ("IPhone 11 Leere BOX", "iPhone 11 Originalverpackung",
                      "Apple iPhone 11 – Lege Doos | Groen 📱",
                      "Iphone 11 (para piezas)", "iPhone 11 caja vacía",
                      "iPhone 11 per ricambi", "iPhone 11 defekt"):
            self.assertIsNotNone(rejection_reason(title, "iPhone 11", BAD), title)

    def test_phone_sold_with_its_box_is_kept(self):
        for title in ("iPhone 11 64 Go avec boîte", "iPhone 11 mit Originalverpackung",
                      "iPhone 11 128gb met doos"):
            self.assertIsNone(rejection_reason(title, "iPhone 11", BAD), title)

    def test_portuguese_spanish_parts_and_back_panels_are_rejected(self):
        for title in ("Iphone 11 para concertar ou para pecas", "Despiece iPhone 11",
                      "Face arrière iPhone 11", "iPhone 11 Rückseite"):
            self.assertIsNotNone(rejection_reason(title, "iPhone 11", BAD), title)

    def test_vinted_model_field_rejects_a_more_specific_model(self):
        items = [
            {"id": 1, "title": "iPhone 11 128 Go", "price": 100, "model": "iPhone 11 Pro"},
            {"id": 2, "title": "iPhone 11 128 Go", "price": 110, "model": "iPhone 11"},
            {"id": 3, "title": "iPhone 11 128 Go", "price": 120, "model": ""},
        ]
        results, _ = find_cheapest(items, "iPhone 11", bad_terms=BAD)
        self.assertEqual([r["id"] for r in results], ["2", "3"])

    def test_foreign_cases_are_rejected(self):
        for title in ("Funda iPhone 11", "iPhone 11 Silikon Handyhülle", "Capas iPhone 11"):
            self.assertIsNotNone(rejection_reason(title, "iPhone 11", BAD), title)


class ParsingTests(unittest.TestCase):
    def test_card_title_is_parsed(self):
        row = parse_card(
            "/items/10880267895-iphone-11?referrer=catalog",
            "iPhone 11 64gb, Marque: Apple, Modèle: iPhone 11, État: Bon état, 90.00 €, 95.20 €",
        )
        self.assertEqual(row["id"], "10880267895")
        self.assertEqual(row["title"], "iPhone 11 64gb")
        self.assertEqual((row["price"], row["total"]), (90.0, 95.2))
        self.assertEqual(row["status"], "Bon état")

    def test_title_with_commas_and_vinted_fees_suffix(self):
        row = parse_card(
            "/items/1-x",
            "fundas iphone 11, 1,50 cada una, Marque: Shein, État: Bon état, 1.00 €, 1.75 € Frais Vinted inclus",
        )
        self.assertEqual(row["title"], "fundas iphone 11, 1,50 cada una")
        self.assertEqual(row["total"], 1.75)

    def test_unparseable_card_is_skipped(self):
        self.assertIsNone(parse_card("/items/5", "pas de prix ici"))
        self.assertIsNone(parse_card("/autre/chose", "x, 1.00 €, 2.00 €"))

    def test_shipping_text(self):
        self.assertEqual(parse_money("à partir de 5,49 €"), 5.49)
        self.assertEqual(parse_money("à partir de 0,00 €"), 0.0)
        self.assertIsNone(parse_money("Envoi"))

    def test_total_cost_includes_protection_and_shipping(self):
        row = {"price": 90.0, "total": 95.2, "shipping": 5.49}
        self.assertAlmostEqual(cost(row), 100.69)
        self.assertAlmostEqual(cost({"price": 90.0}), 90.0)

    def test_shipping_can_change_the_ranking(self):
        items = [
            {"id": 1, "title": "iPhone 11", "price": 100, "total": 106, "shipping": 9.0},
            {"id": 2, "title": "iPhone 11", "price": 104, "total": 110, "shipping": 0.0},
        ]
        results, _ = find_cheapest(items, "iPhone 11", bad_terms=BAD)
        self.assertEqual([r["id"] for r in results], ["2", "1"])


class CheapestTests(unittest.TestCase):
    def test_sorted_by_price_deduplicated_and_filtered(self):
        items = [
            item(1, "iPhone 11 64 Go", 180),
            item(2, "iPhone 11 Pro 64 Go", 90),   # autre modèle
            item(3, "iPhone 11 128 Go", 150),
            item(4, "Coque iPhone 11", 5),         # accessoire
            item(3, "iPhone 11 128 Go", 150),      # doublon
            item(5, "iPhone 11 HS", 40),           # cassé
            item(6, "iPhone 11 blanc", 170),
        ]
        results, rejected = find_cheapest(items, "iPhone 11", top=3, bad_terms=BAD)
        self.assertEqual([r["id"] for r in results], ["3", "6", "1"])
        self.assertEqual(rejected, 3)

    def test_max_price_and_missing_price(self):
        items = [item(1, "iPhone 11", 200), item(2, "iPhone 11", 100),
                 {"id": 3, "title": "iPhone 11", "price": None}]
        results, _ = find_cheapest(items, "iPhone 11", max_price=150, bad_terms=BAD)
        self.assertEqual([r["id"] for r in results], ["2"])

    def test_suspiciously_low_price_is_flagged_not_hidden(self):
        items = [item(i, "iPhone 11 64 Go", p) for i, p in enumerate((20, 150, 160, 170, 180))]
        results, _ = find_cheapest(items, "iPhone 11", bad_terms=BAD)
        self.assertEqual(results[0]["price"], 20)
        self.assertTrue(results[0]["suspicious"])
        self.assertFalse(results[1]["suspicious"])


if __name__ == "__main__":
    unittest.main()
