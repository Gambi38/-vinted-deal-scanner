import csv
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import playwright.async_api  # noqa: F401
except ModuleNotFoundError:
    playwright = types.ModuleType("playwright")
    async_api = types.ModuleType("playwright.async_api")
    async_api.async_playwright = object()
    async_api.TimeoutError = TimeoutError
    playwright.async_api = async_api
    sys.modules["playwright"] = playwright
    sys.modules["playwright.async_api"] = async_api

import fusion_donnees
import vinted_tarayici as bot


class EnvironmentConfigTests(unittest.TestCase):
    def test_env_file_parses_values_without_overwriting_real_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "export VINTED_TEST_NEW='depuis le fichier'\n"
                "VINTED_TEST_KEEP=depuis-env\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"VINTED_TEST_KEEP": "valeur-reelle"},
                clear=False,
            ):
                bot.load_env_file(env_file)
                self.assertEqual(os.environ["VINTED_TEST_NEW"], "depuis le fichier")
                self.assertEqual(os.environ["VINTED_TEST_KEEP"], "valeur-reelle")

    def test_environment_overrides_are_typed(self):
        cfg = {
            "base_url": "https://www.vinted.be",
            "reject_unknown_listing_age": True,
        }
        overrides = {
            "VINTED_MAX_LISTING_AGE_HOURS": "12",
            "VINTED_MAX_ITEMS_PER_SEARCH": "8",
            "VINTED_REJECT_UNKNOWN_LISTING_AGE": "false",
        }
        with patch.dict(os.environ, overrides, clear=True):
            bot.apply_env_overrides(cfg)

        self.assertEqual(cfg["max_listing_age_hours"], 12.0)
        self.assertEqual(cfg["max_items_per_search"], 8)
        self.assertFalse(cfg["reject_unknown_listing_age"])

    def test_rate_limit_configuration_rejects_unsafe_values(self):
        base = {
            "base_url": "https://www.vinted.be",
            "request_delay_min_seconds": 1,
            "request_delay_max_seconds": 3,
            "startup_jitter_max_seconds": 20,
            "run_budget_seconds": 480,
            "max_listing_age_hours": 24,
            "max_items_per_search": 15,
        }
        bot.validate_runtime_config(dict(base))

        too_fast = dict(base, request_delay_min_seconds=0.1)
        with self.assertRaisesRegex(ValueError, ">= 0.5"):
            bot.validate_runtime_config(too_fast)

        reversed_range = dict(base, request_delay_min_seconds=3, request_delay_max_seconds=1)
        with self.assertRaisesRegex(ValueError, ">= au minimum"):
            bot.validate_runtime_config(reversed_range)


class FilteringAndScoringTests(unittest.TestCase):
    def test_rule_match_requires_keywords_and_honors_exclusions(self):
        rule = {
            "must_contain": ["switch"],
            "exclude": ["pochette"],
        }
        self.assertTrue(bot.rule_match(rule, "Nintendo Switch OLED", "console"))
        self.assertFalse(bot.rule_match(rule, "Nintendo OLED", "console"))
        self.assertFalse(
            bot.rule_match(rule, "Nintendo Switch OLED", "vendue avec pochette")
        )

    def test_score_candidate_uses_configured_fees(self):
        rule = {"resale_low": 60, "resale_high": 70}
        cfg = {
            "shipping_estimate": 4.5,
            "buyer_protection_estimate": {"fixed": 0.7, "pct": 0.05},
        }
        total, low, high, margin_low, margin_high, roi = bot.score_candidate(
            rule,
            20,
            cfg,
        )
        self.assertEqual((total, low, high), (26.2, 60.0, 70.0))
        self.assertEqual((margin_low, margin_high), (33.8, 43.8))
        self.assertEqual(roi, 129.0)

    def test_blacklist_blocks_accessories_and_reports_risk_words(self):
        blacklist = {
            "title_accessory_blacklist": ["boite"],
            "hard_blacklist": [],
            "fake_blacklist": [],
            "accessory_blacklist": ["chargeur"],
            "suspicious_words": ["urgent"],
        }
        blocked, group, hits, risks = bot.blacklist_check(
            "Boîte Nintendo Switch vide",
            "sans console",
            blacklist,
        )
        self.assertTrue(blocked)
        self.assertEqual(group, "title_accessory_blacklist")
        self.assertEqual(hits, ["boite"])
        self.assertEqual(risks, [])

        blocked, group, hits, risks = bot.blacklist_check(
            "Nintendo Switch OLED",
            "urgent, vente rapide",
            blacklist,
        )
        self.assertFalse(blocked)
        self.assertEqual((group, hits), ("", []))
        self.assertEqual(risks, ["urgent"])


class SeenStateTests(unittest.TestCase):
    def test_rejection_is_scoped_to_one_search(self):
        first = {"name": "Nintendo Switch"}
        second = {"name": "Switch OLED"}
        state = {bot.search_seen_key(first, "123")}

        self.assertTrue(bot.item_already_seen(state, first, "123"))
        self.assertFalse(bot.item_already_seen(state, second, "123"))

    def test_alert_marker_deduplicates_all_searches(self):
        state = {bot.alert_seen_key("123")}
        self.assertTrue(bot.item_already_seen(state, {"name": "A"}, "123"))
        self.assertTrue(bot.item_already_seen(state, {"name": "B"}, "123"))

    def test_legacy_ids_remain_supported(self):
        self.assertTrue(bot.item_already_seen({"123"}, {"name": "A"}, "123"))


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
        self.cfg = {
            "max_listing_age_hours": 24,
            "reject_unknown_listing_age": True,
            "instant_listing_minutes": 30,
        }

    def test_parses_unix_seconds_milliseconds_and_iso(self):
        expected = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
        seconds = expected.timestamp()
        self.assertEqual(bot.parse_vinted_timestamp(seconds), expected)
        self.assertEqual(bot.parse_vinted_timestamp(seconds * 1000), expected)
        self.assertEqual(
            bot.parse_vinted_timestamp("2026-09-02T10:00:00Z"),
            expected,
        )

    def test_accepts_24_hours_and_rejects_older(self):
        exactly_24h = self.now - timedelta(hours=24)
        older = self.now - timedelta(hours=24, seconds=1)
        self.assertTrue(bot.freshness_check(exactly_24h.isoformat(), self.cfg, self.now)[0])
        self.assertFalse(bot.freshness_check(older.isoformat(), self.cfg, self.now)[0])

    def test_unknown_age_is_rejected_in_strict_mode(self):
        accepted, age, reason = bot.freshness_check(None, self.cfg, self.now)
        self.assertFalse(accepted)
        self.assertIsNone(age)
        self.assertIn("introuvable", reason)

    def test_instant_listing_gets_a_score_bonus(self):
        base = bot.opportunity_score(25, 100, 50, [], age_hours=8)
        instant = bot.opportunity_score(25, 100, 50, [], age_hours=0.2)
        self.assertGreater(instant, base)

    def test_catalog_payload_variants(self):
        item = {"id": 123, "created_at_ts": 1}
        self.assertEqual(bot.catalog_items_from_payload({"items": [item]}), [item])
        self.assertEqual(
            bot.catalog_items_from_payload({"data": {"items": [item]}}),
            [item],
        )


class CsvTests(unittest.TestCase):
    def test_alert_csv_keeps_risk_demand_and_image(self):
        row = {
            "item_id": "123",
            "image_url": "https://example.test/image.jpg",
            "demand_score": 5,
            "risk": "prix anormalement bas",
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "alertes.csv"
            with patch.object(bot, "ALERTS_CSV", output):
                bot.append_alert(row)
            with output.open(encoding="utf-8-sig", newline="") as handle:
                saved = next(csv.DictReader(handle))

        self.assertEqual(saved["image_url"], row["image_url"])
        self.assertEqual(saved["demand_score"], "5")
        self.assertEqual(saved["risk"], row["risk"])

    def test_old_alert_csv_is_upgraded_before_append(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "alertes.csv"
            output.write_text(
                "timestamp,title,url,item_id\n2026-01-01,ancienne,https://old,1\n",
                encoding="utf-8",
            )
            with patch.object(bot, "ALERTS_CSV", output):
                bot.append_alert({
                    "title": "nouvelle",
                    "image_url": "https://image",
                    "demand_score": 5,
                    "risk": "aucun",
                    "url": "https://new",
                    "item_id": "2",
                })
            with output.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)

        self.assertEqual(reader.fieldnames, bot.ALERT_FIELDS)
        self.assertEqual(rows[0]["title"], "ancienne")
        self.assertEqual(rows[1]["risk"], "aucun")

    def test_merge_preserves_union_of_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "remote.csv"
            local = root / "local.csv"
            output = root / "output.csv"
            remote.write_text("item_id,title\n1,ancien\n", encoding="utf-8")
            local.write_text("item_id,title,risk\n2,nouveau,doute\n", encoding="utf-8")

            fusion_donnees.fusionner_alertes(remote, local, output)
            with output.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)

        self.assertEqual(reader.fieldnames, ["item_id", "title", "risk"])
        self.assertEqual(rows[1]["risk"], "doute")


if __name__ == "__main__":
    unittest.main()
