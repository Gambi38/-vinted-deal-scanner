import asyncio
import csv
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import vinted_api_light as bot


class ApiOnlyTests(unittest.TestCase):
    def test_module_has_no_playwright_dependency(self):
        source = Path(bot.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import vinted_tarayici", source)
        self.assertNotIn("playwright", source.lower())

    def test_timestamp_seconds_milliseconds_and_iso(self):
        now = datetime.now(timezone.utc)
        for value in (now.timestamp(), now.timestamp() * 1000, now.isoformat()):
            self.assertIsNotNone(bot.parse_vinted_timestamp(value))

    def test_strict_freshness_rejects_over_24_hours(self):
        cfg = {"max_listing_age_hours": 24, "reject_unknown_listing_age": True}
        now = datetime.now(timezone.utc)
        self.assertTrue(bot.freshness_check((now - timedelta(hours=23)).isoformat(), cfg, now)[0])
        self.assertFalse(bot.freshness_check((now - timedelta(hours=25)).isoformat(), cfg, now)[0])

    def test_personal_filter_conversion(self):
        rows = bot.convert_personal_filter({
            "actif": True, "nom": "Switch OLED", "categorie": "CONSOLE",
            "recherches_vinted": ["switch oled"], "revente_prudente": 180,
            "mots_obligatoires": ["switch"], "un_des_mots": ["oled"],
        })
        self.assertEqual(rows[0]["query"], "switch oled")
        self.assertEqual(rows[0]["rules"][0]["resale_low"], 180)

    def test_hidden_fresh_deal_beats_popular_one(self):
        cfg = {"hidden_deal_bonus": 1, "favourite_penalty_per_user": .25,
               "view_penalty_per_view": .05, "popularity_penalty_cap": 2}
        hidden = bot.opportunity_score(30, 100, 60, [], 2/60, 0, 2, cfg)
        popular = bot.opportunity_score(30, 100, 60, [], 2/60, 8, 50, cfg)
        self.assertGreater(hidden, popular)

    def test_rule_requires_keywords_and_honors_exclusion(self):
        rule = {"must_contain": ["switch"], "any_contain": ["oled", "lite"],
                "exclude": ["housse"]}
        self.assertTrue(bot.rule_match(rule, "Nintendo Switch OLED", "console"))
        self.assertFalse(bot.rule_match(rule, "Nintendo Switch OLED housse", "housse"))

    def test_fee_and_margin_use_configuration(self):
        cfg = {"buyer_protection_estimate": {"fixed": 1, "pct": .1},
               "shipping_estimate": 5}
        result = bot.score_candidate({"resale_low": 100, "resale_high": 120}, 40, cfg)
        self.assertEqual(result[0], 50)
        self.assertEqual(result[3], 50)

    def test_seen_keys_are_scoped_then_alerted_globally(self):
        search = {"name": "Switch"}
        seen = {bot.search_seen_key(search, "123")}
        self.assertTrue(bot.item_already_seen(seen, search, "123"))
        self.assertFalse(bot.item_already_seen(seen, {"name": "PS5"}, "123"))
        seen.add(bot.alert_seen_key("123"))
        self.assertTrue(bot.item_already_seen(seen, {"name": "PS5"}, "123"))

    def test_csv_schema_contains_views_and_favourites(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "alertes.csv"
            with patch.object(bot, "ALERTS_CSV", target):
                bot.append_alert({"title": "Test", "view_count": 3, "favourite_count": 1})
                with target.open(encoding="utf-8-sig", newline="") as handle:
                    row = next(csv.DictReader(handle))
            self.assertEqual(row["view_count"], "3")
            self.assertEqual(row["favourite_count"], "1")

    def test_ntfy_without_secret_does_not_crash(self):
        async def run():
            class Session:
                pass
            with patch.dict(os.environ, {}, clear=True):
                return await bot.ntfy_send({"opportunity_score": 8}, Session())
        self.assertFalse(asyncio.run(run()))


if __name__ == "__main__":
    unittest.main()

