import json
import tempfile
import unittest
from pathlib import Path

from cheapest_finder import load_bad_terms, rejection_reason
from watch_finder import (
    backoff_seconds, classify, expand_queries, load_state, match_terms, matches_any,
    prune_seen, save_state, slug, split_new, sweep_bounds, synonyms_for,
)

BAD = load_bad_terms()


def row(i, title, price, total=None, shipping=None, model=""):
    return {"id": str(i), "title": title, "price": price,
            "total": total or price, "shipping": shipping, "model": model,
            "status": "Bon état", "url": f"https://www.vinted.be/items/{i}"}


class ExpandQueriesTests(unittest.TestCase):
    def test_similar_searches_reach_the_same_listings(self):
        queries = expand_queries("iPhone 11")
        self.assertEqual(queries[0], "iPhone 11")
        self.assertIn("iphone11", queries)

    def test_aliases_are_added_without_duplicates(self):
        queries = expand_queries("Nintendo Switch OLED", ["switch oled", "Nintendo Switch OLED"])
        self.assertEqual(len({q.lower() for q in queries}), len(queries))
        self.assertIn("switch oled", queries)

    def test_ps5_is_searched_both_ways(self):
        queries = expand_queries("PS5")
        self.assertIn("PS5", queries)
        self.assertIn("ps 5", queries)


class SynonymTests(unittest.TestCase):
    def test_ps5_and_playstation_5_are_the_same_article(self):
        self.assertEqual(synonyms_for("PS5"), ["playstation 5"])
        self.assertEqual(synonyms_for("PlayStation 5"), ["ps5"])
        self.assertIn("playstation 5", expand_queries("PS5"))
        terms = match_terms("PS5")
        self.assertTrue(matches_any(row(1, "Console PlayStation 5 édition standard", 400), terms, BAD))
        self.assertFalse(matches_any(row(2, "Console PlayStation 4", 100), terms, BAD))

    def test_unknown_article_has_no_synonym(self):
        self.assertEqual(synonyms_for("iPhone 11"), [])


class SweepTests(unittest.TestCase):
    class Args:
        max_price = None
        deal_ratio = 0.85

    def test_sweep_range_skips_absurdly_low_prices(self):
        args = self.Args()
        self.assertEqual(sweep_bounds(args, 150.0), (60, 128))
        args.max_price = 100.0
        self.assertEqual(sweep_bounds(args, 150.0), (60, 100.0))
        self.assertEqual(sweep_bounds(args, None), (None, 100.0))


class MatchingTests(unittest.TestCase):
    def ok(self, title, query="iPhone 11"):
        return rejection_reason(title, query, BAD) is None

    def test_spacing_and_typos_still_match(self):
        for title in ("iPhone11 64Go", "Iphon 11 noir", "IPHONE 11 128gb débloqué"):
            self.assertTrue(self.ok(title), title)

    def test_other_generation_numbers_are_rejected(self):
        for title in ("iPhone 12 64Go", "iPhone 13", "iPhone 11 échange iPhone 12"):
            self.assertFalse(self.ok(title), title)

    def test_sizes_storage_and_network_are_not_other_models(self):
        for title in ("iPhone 11 6.1 pouces", "iPhone 11 64 Go 4G", "iPhone 11 (A2221) 256Go"):
            self.assertTrue(self.ok(title), title)

    def test_switch_2_is_not_a_switch(self):
        self.assertFalse(self.ok("Nintendo Switch 2", "Nintendo Switch"))
        self.assertTrue(self.ok("Nintendo Switch 32 Go", "Nintendo Switch"))

    def test_ps4_is_not_ps5(self):
        self.assertFalse(self.ok("PS4 Slim 500 Go", "PS5"))
        self.assertTrue(self.ok("PS5 édition standard", "PS5"))

    def test_alias_can_accept_titles_without_the_brand(self):
        r = row(1, "Switch OLED blanche", 200)
        self.assertFalse(matches_any(r, ["Nintendo Switch OLED"], BAD))
        self.assertTrue(matches_any(r, ["Nintendo Switch OLED", "switch oled"], BAD))


class RealWorldLeakTests(unittest.TestCase):
    """Intrus vus lors du test en direct du 2026-11-19."""

    def test_leaks_seen_live_are_rejected(self):
        for title in ("JETech Bildschirmschutz für iPhone 11",
                      "Iphone 11 pour pièce non fonctionnel",
                      "2 custodie iPhone 11 promax", "iPhone 11 bloqué icloud",
                      "iPhone 11 verrouillé"):
            self.assertIsNotNone(rejection_reason(title, "iPhone 11", BAD), title)

    def test_legit_titles_are_not_over_filtered(self):
        for title in ("iPhone 11 64GB schwarz", "iPhone 11 - 128Go", "iPhone 11 blanco",
                      "iPhone 11 rouge 64 Go débloqué", "iPhone 11 avec facture"):
            self.assertIsNone(rejection_reason(title, "iPhone 11", BAD), title)


class ReviewerFindingsTests(unittest.TestCase):
    """Cas relevés par la relecture indépendante (faux rejets et faux positifs)."""

    def accepted(self, query, titles):
        for title in titles:
            self.assertIsNone(rejection_reason(title, query, BAD), f"{query!r} <- {title!r}")

    def rejected(self, query, titles):
        for title in titles:
            self.assertIsNotNone(rejection_reason(title, query, BAD), f"{query!r} <- {title!r}")

    def test_legit_phone_listings_are_kept(self):
        self.accepted("iPhone 11", [
            "iPhone 11 batterie 89%", "iPhone 11 64 Go - batterie 92%",
            "iPhone 11 écran 6,1 Liquid Retina", "iPhone 11 avec coque offerte",
            "iPhone 11 + coque", "iPhone 11 128 Go + boîte + chargeur",
            "iPhone 11 sans chargeur", "iPhone 11 64Go (boîte d'origine)",
            "iPhone 11 dans sa boîte", "iPhone 11 appareil photo arrière 12MP",
            "iPhone 11 avec copie de la facture", "iPhone 11 écran jamais cassé",
            "iPhone 11 vendu avec facture", "iPhone 11 plus aucune rayure",
            "iPhone 11 se vende rápido", "iPhone 11 sans icloud",
            "iPhone 11 aucun problème",
            "iPhone 11 64GB Parfait état - Débloqué iCloud, Débloqué Sim",
            "iPhone 11 128 GB – Muy buen estado – Caja y cargador",
            "iPhone 11 Lila 64 GB inklusive Originalverpackung",
            "Iphone 11 64gb inc. Hülle/powerbank", "iPhone 1164gb",
        ])

    def test_legit_console_and_game_listings_are_kept(self):
        self.accepted("Nintendo Switch OLED", [
            "Nintendo Switch OLED avec dock", "Nintendo Switch OLED sans dock",
            "Switch OLED + 2 Joy-Con Nintendo",
        ])
        self.accepted("Pokémon Épée", ["Pokemon Epee 3DS", "Pokémon Épée avec boîte et notice"])
        self.accepted("PS5", ["PS5 console lecteur 4K ultra HD"])

    def test_wrong_products_are_rejected(self):
        self.rejected("PS5", [
            "Manette PS5", "Manette DualSense PS5 blanche", "Casque PS5", "Jeu PS5 Spider-Man 2",
            "Jeux PS5", "FC 24 PS5", "PS5 faceplate", "PS5 plaques",
        ])
        self.rejected("Pokémon Épée", ["Pokémon Épée + Bouclier", "Épée et Bouclier double pack"])
        self.rejected("iPhone 11", [
            "iPhone 11 + AirPods", "iPhone 11 et iPad", "iPhone 11 icloud", "iPhone 11 problème Face ID",
            "iPhone 11 fissuré", "iPhone 11 pantalla rota", "iPhone 11 tela quebrada",
            "Batterie iPhone 11", "Écran iPhone 11", "Boîte iPhone 11", "iPhone 11 Plus",
            "Vendu iPhone 11",
        ])
        self.rejected("Sony WH-1000XM4", ["Coussinets Sony WH-1000XM4", "Sony WH-1000XM4 gauche seul"])


class ClassifyTests(unittest.TestCase):
    def test_max_price_applies_to_the_total_cost(self):
        r = row(1, "iPhone 11", 100, total=106, shipping=5)
        self.assertEqual(classify(r, 120, 150, 0.85), "bon")
        self.assertIsNone(classify(r, 105, 150, 0.85))

    def test_without_max_price_only_real_deals_alert(self):
        self.assertEqual(classify(row(1, "x", 90, total=95), None, 150, 0.85), "bon")
        self.assertIsNone(classify(row(2, "x", 140, total=148), None, 150, 0.85))

    def test_very_low_price_is_flagged_as_doubtful(self):
        self.assertEqual(classify(row(1, "x", 20), None, 150, 0.85), "suspect")

    def test_unknown_median_alerts_on_every_match(self):
        self.assertEqual(classify(row(1, "x", 300), None, None, 0.85), "bon")


class StateTests(unittest.TestCase):
    def test_new_listings_are_returned_once(self):
        state = {"seen": {}}
        fresh, known = split_new([row(1, "a", 1), row(2, "b", 1)], state, now=1000)
        self.assertEqual((len(fresh), known), (2, 0))
        fresh, known = split_new([row(2, "b", 1), row(3, "c", 1)], state, now=1001)
        self.assertEqual([r["id"] for r in fresh], ["3"])
        self.assertEqual(known, 1)

    def test_unmarked_listings_stay_new_until_handled(self):
        state = {"seen": {}}
        rows = [row(1, "a", 1), row(1, "a", 1), row(2, "b", 1)]
        fresh, known = split_new(rows, state, mark=False)
        self.assertEqual([r["id"] for r in fresh], ["1", "2"])
        self.assertEqual(state["seen"], {})
        again, _ = split_new(rows, state, mark=False)
        self.assertEqual(len(again), 2)

    def test_state_round_trip_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "s.json"
            self.assertFalse(load_state(path)["baseline_done"])
            state = {"seen": {"1": 5.0}, "alerted": {"1": 6.0}, "median": 120.0, "baseline_done": True}
            save_state(path, state)
            self.assertEqual(load_state(path), state)
            path.write_text("pas du json", encoding="utf-8")
            self.assertEqual(load_state(path)["seen"], {})

    def test_old_seen_ids_are_pruned(self):
        state = {"seen": {"old": 0.0, "new": 10_000_000.0}}
        prune_seen(state, now=10_000_000.0)
        self.assertEqual(list(state["seen"]), ["new"])

    def test_backoff_grows_and_is_capped(self):
        self.assertEqual([backoff_seconds(n) for n in (1, 2, 3)], [60, 120, 240])
        self.assertEqual(backoff_seconds(30), 900)

    def test_slug(self):
        self.assertEqual(slug("Nintendo Switch OLED"), "nintendo-switch-oled")


if __name__ == "__main__":
    unittest.main()
