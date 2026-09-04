import io
import json
import tempfile
import time
import unittest
from pathlib import Path

from pricecharting_catalog import (
    ReferenceCatalog,
    import_csv,
    main,
    platform_for,
)


CSV_HEADER = (
    "id,console-name,product-name,loose-price,cib-price,new-price,sales-volume\n"
)


class PriceChartingImportTests(unittest.TestCase):
    def test_import_converts_cents_and_keeps_fast_games_only(self):
        source = io.StringIO(CSV_HEADER + "".join((
            "1,Nintendo Switch,Mario Kart 8 Deluxe,3500,4200,5500,25\n",
            "2,Playstation 5,Gran Turismo 7,3000,3800,5000,2\n",
            "3,Nintendo Switch,Nintendo Switch Pro Controller,4000,5000,6000,40\n",
            "4,Unknown Box,Excellent Mystery Game,5000,6000,7000,50\n",
        )))
        data = import_csv(
            source, min_sales_volume=3, usd_to_eur=0.85,
            resale_haircut=0.80, max_buy_ratio=0.45,
        )
        self.assertEqual(data["reference_count"], 1)
        row = data["references"][0]
        self.assertEqual(row["product_type"], "GAME")
        self.assertEqual(row["platform"], "Nintendo Switch")
        self.assertEqual(row["sales_volume"], 25)
        self.assertAlmostEqual(row["resale_low"], 23.80)
        self.assertAlmostEqual(row["resale_high"], 28.56)

    def test_sales_volume_is_mandatory(self):
        source = io.StringIO(
            "id,console-name,product-name,loose-price,cib-price\n"
            "1,Nintendo Switch,Mario Kart 8 Deluxe,3500,4200\n"
        )
        self.assertEqual(import_csv(source)["reference_count"], 0)

    def test_pal_platform_names_are_normalised(self):
        platform = platform_for("PAL Nintendo Switch")
        self.assertIsNotNone(platform)
        self.assertEqual(platform.canonical, "Nintendo Switch")

    def test_fresh_cache_is_not_replaced_without_download(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "catalog.json"
            output.write_text(json.dumps({
                "schema": 1, "imported_at": time.time(), "references": [],
            }), encoding="utf-8")
            self.assertEqual(main([
                "--output", str(output), "--allow-missing",
            ]), 0)


class ReferenceCatalogTests(unittest.TestCase):
    def setUp(self):
        data = import_csv(io.StringIO(CSV_HEADER + "".join((
            "1,Nintendo Switch,Mario Kart 8 Deluxe,3500,4200,5500,25\n",
            "2,Playstation 5,Gran Turismo 7,3000,3800,5000,15\n",
            "3,Nintendo 64,Rampage World Tour,4500,6500,9000,8\n",
        ))), min_sales_volume=3, usd_to_eur=1, resale_haircut=1,
            max_buy_ratio=0.50)
        self.catalog = ReferenceCatalog(data["references"])

    def test_exact_game_and_platform_match(self):
        search, rule = self.catalog.match(
            "Mario Kart 8 Deluxe jeu Nintendo Switch", 15,
        )
        self.assertEqual(search["product_type"], "GAME")
        self.assertEqual(rule["model"], "Mario Kart 8 Deluxe")
        self.assertEqual(rule["external_source"], "PriceCharting CSV")

    def test_platform_is_required_even_for_exact_game_name(self):
        self.assertEqual(self.catalog.match("Mario Kart 8 Deluxe", 15), (None, None))
        self.assertEqual(
            self.catalog.match("Mario Kart 8 Deluxe PS5", 15),
            (None, None),
        )

    def test_box_accessory_and_console_bundle_are_not_games(self):
        for title in (
            "Boîte vide Mario Kart 8 Deluxe Nintendo Switch",
            "Coque Mario Kart 8 Deluxe Nintendo Switch",
            "Console Nintendo Switch avec Mario Kart 8 Deluxe",
        ):
            self.assertEqual(self.catalog.match(title, 10), (None, None), title)

    def test_game_never_becomes_console(self):
        search, rule = self.catalog.match(
            "Nintendo 64 Rampage World Tour cartouche", 20,
        )
        self.assertEqual(search["category"], "JEU_N64")
        self.assertEqual(rule["product_type"], "GAME")
        self.assertEqual(rule["model"], "Rampage World Tour")

    def test_price_limit_is_enforced(self):
        self.assertEqual(
            self.catalog.match("Gran Turismo 7 jeu PS5", 20),
            (None, None),
        )

    def test_discovery_queries_are_deduplicated_by_platform(self):
        queries = self.catalog.discovery_searches()
        self.assertEqual(len({row["query"] for row in queries}), len(queries))
        self.assertIn("nintendo switch jeu", {row["query"] for row in queries})

    def test_thousands_of_references_use_the_local_index(self):
        references = []
        for index in range(2500):
            references.append({
                "id": str(index), "name": f"Unique Adventure {index}",
                "title_variants": [f"unique adventure {index}"],
                "platform": "Nintendo Switch",
                "platform_aliases": ["nintendo switch", "switch"],
                "category": "JEU_SWITCH", "query": "nintendo switch jeu",
                "product_type": "GAME", "resale_low": 40,
                "resale_high": 45, "price_max": 18, "hot_buy": 14,
                "sales_volume": 10, "demand_score": 4,
                "description": "Jeu Nintendo Switch", "source": "test",
            })
        catalog = ReferenceCatalog(references)
        self.assertEqual(len(catalog), 2500)
        _, rule = catalog.match(
            "Unique Adventure 2388 Nintendo Switch", 12,
        )
        self.assertEqual(rule["model"], "Unique Adventure 2388")


if __name__ == "__main__":
    unittest.main()
