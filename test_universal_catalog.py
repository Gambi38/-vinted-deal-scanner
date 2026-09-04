import io
import json
import tempfile
import unittest
from pathlib import Path

from universal_catalog import DeviceCatalog, import_csv, main


class UniversalCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).with_name("appareils_cibles.csv")
        cls.rows = import_csv(source.read_bytes())
        cls.catalog = DeviceCatalog(cls.rows)

    def test_seed_catalog_covers_requested_families(self):
        self.assertEqual(len(self.rows), 84)
        types = {row["product_type"] for row in self.rows}
        self.assertTrue({
            "SMARTPHONE", "TABLET", "LAPTOP", "TOOL", "EREADER", "AUDIO",
        }.issubset(types))

    def test_specific_phone_wins_over_base_model(self):
        source, rule = self.catalog.match("iPhone 13 Pro 128 Go", 250)
        self.assertEqual(rule["model"], "iPhone 13 Pro")
        source, rule = self.catalog.match("iPhone 13 classique 128 Go", 170)
        self.assertEqual(rule["model"], "iPhone 13")

    def test_accessories_never_inherit_device_value(self):
        examples = (
            ("Coque iPhone 13", 10),
            ("Écran iPad Air 5 M1", 80),
            ("Chargeur MacBook Air M1", 25),
            ("Batterie Makita DHP484", 25),
            ("Clavier pour Surface Pro 7", 40),
            ("Coussinets Sony WH-1000XM3", 12),
            ("Back glass iPhone 15 Pro Max", 25),
            ("Housse Kindle Oasis 10 génération", 15),
        )
        for title, price in examples:
            self.assertEqual(self.catalog.match(title, price), (None, None), title)

    def test_included_accessories_keep_complete_product(self):
        examples = (
            ("Makita DHP484 avec batterie", 40, "Makita DHP484"),
            ("MacBook Air M1 avec chargeur", 330, "MacBook Air M1 2020"),
            ("iPad Pro 11 M1 avec clavier", 420, "iPad Pro 11 M1 2021"),
        )
        for title, price, expected in examples:
            source, rule = self.catalog.match(title, price)
            self.assertIsNotNone(rule, title)
            self.assertEqual(rule["model"], expected)

    def test_model_code_is_exact_not_fuzzy(self):
        self.assertEqual(self.catalog.match("Makita DHP485", 40), (None, None))
        source, rule = self.catalog.match("Makita DHP484", 40)
        self.assertEqual(rule["model"], "Makita DHP484")

    def test_invalid_generic_or_unprofitable_rows_are_rejected(self):
        csv_data = io.StringIO(
            "name,product_type,aliases,query,price_max,resale_low,demand_score\n"
            "Apple,SMARTPHONE,Apple,iphone,100,200,5\n"
            "Bad,SMARTPHONE,Bad 2,bad,200,100,5\n"
            "Slow,SMARTPHONE,Slow 3,slow,20,50,2\n"
        )
        self.assertEqual(import_csv(csv_data), [])

    def test_thousands_of_references_use_indexed_matching(self):
        rows = [{
            "name": f"Device Model {index}", "product_type": "SMARTPHONE",
            "category": "SMARTPHONE", "brand": "Brand",
            "aliases": [f"device model {index}"], "query": "device",
            "price_max": 100, "hot_buy": 80, "resale_low": 180,
            "resale_high": 200, "demand_score": 4, "sales_volume": 4,
            "description": "", "exclude": [], "source": "test",
        } for index in range(3000)]
        catalog = DeviceCatalog(rows)
        self.assertEqual(len(catalog), 3000)
        source, rule = catalog.match("Device Model 2345", 50)
        self.assertEqual(rule["model"], "Device Model 2345")

    def test_discovery_searches_are_grouped_not_one_request_per_model(self):
        searches = self.catalog.discovery_searches()
        self.assertLess(len(searches), len(self.catalog))
        self.assertIn("iphone", {row["query"] for row in searches})

    def test_cli_builds_persistent_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "catalog.json"
            source = Path(__file__).with_name("appareils_cibles.csv")
            self.assertEqual(main([
                "--input", str(source), "--output", str(target),
                "--max-age-hours", "0",
            ]), 0)
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(data["reference_count"], 84)
            self.assertTrue(data["source_fingerprint"])

    def test_changed_local_seed_invalidates_recent_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "devices.csv"
            target = directory / "catalog.json"
            header = (
                "name,product_type,category,brand,aliases,query,price_max,"
                "hot_buy,resale_low,resale_high,demand_score,sales_volume,"
                "description,exclude\n"
            )
            source.write_text(
                header + "Phone 123,SMARTPHONE,SMARTPHONE,X,phone 123,phone,"
                "50,40,100,120,5,0,Test,\n",
                encoding="utf-8",
            )
            self.assertEqual(main([
                "--input", str(source), "--output", str(target),
                "--max-age-hours", "24",
            ]), 0)
            source.write_text(
                header + "Phone 456,SMARTPHONE,SMARTPHONE,X,phone 456,phone,"
                "60,45,110,130,5,0,Test,\n",
                encoding="utf-8",
            )
            self.assertEqual(main([
                "--input", str(source), "--output", str(target),
                "--max-age-hours", "24",
            ]), 0)
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(data["references"][0]["name"], "Phone 456")

    def test_low_turnover_seed_items_are_removed(self):
        names = " ".join(row["name"].lower() for row in self.rows)
        for unwanted in ("calculatrice", "walkman", "fluke", "wacom"):
            self.assertNotIn(unwanted, names)


if __name__ == "__main__":
    unittest.main()
