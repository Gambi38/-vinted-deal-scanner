import asyncio
import ast
import csv
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import vinted_api_light as bot


class ApiOnlyTests(unittest.TestCase):
    def test_module_has_no_playwright_dependency(self):
        source = Path(bot.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import vinted_tarayici", source)
        self.assertNotIn("playwright", source.lower())

    def test_catalog_only_has_no_detail_endpoint_or_function(self):
        source = Path(bot.__file__).read_text(encoding="utf-8")
        self.assertNotIn("/api/v2/items/", source)
        self.assertFalse(hasattr(bot, "detail_item"))

    def test_state_is_saved_once_in_main_cycle(self):
        tree = ast.parse(Path(bot.__file__).read_text(encoding="utf-8"))
        main = next(node for node in tree.body
                    if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "main_async")
        calls = [node for node in ast.walk(main)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name)
                 and node.func.id == "save_cycle_state"]
        self.assertEqual(len(calls), 1)

    def test_fast_catalog_configuration_reads_fifty_per_search(self):
        cfg = bot.load_json(Path(bot.__file__).with_name("config.json"), {})
        self.assertEqual(cfg["catalog_per_page"], 50)
        self.assertEqual(cfg["max_items_per_search"], 50)
        self.assertEqual(cfg["max_catalog_items_per_run"], 250)
        self.assertLessEqual(cfg["request_delay_max_seconds"], 1.2)
        self.assertEqual(cfg["api_max_concurrency"], 3)

    def test_term_regexes_are_cached(self):
        bot._term_regex.cache_clear()
        self.assertTrue(bot.term_present("Console Nintendo Switch", "switch"))
        self.assertTrue(bot.term_present("Jeu Switch", "switch"))
        self.assertGreaterEqual(bot._term_regex.cache_info().hits, 1)

    def test_timestamp_seconds_milliseconds_and_iso(self):
        now = datetime.now(timezone.utc)
        for value in (now.timestamp(), now.timestamp() * 1000, now.isoformat()):
            self.assertIsNotNone(bot.parse_vinted_timestamp(value))

    def test_catalog_uses_main_photo_timestamp_as_listing_age(self):
        item = {"photos": [
            {"is_main": False, "high_resolution": {"timestamp": 100}},
            {"is_main": True, "high_resolution": {"timestamp": 200}},
        ]}
        self.assertEqual(bot.catalog_timestamp(item), 200)

    def test_strict_freshness_rejects_over_30_minutes(self):
        cfg = {"max_listing_age_hours": 0.5, "reject_unknown_listing_age": True}
        now = datetime.now(timezone.utc)
        self.assertTrue(bot.freshness_check((now - timedelta(minutes=29)).isoformat(), cfg, now)[0])
        self.assertFalse(bot.freshness_check((now - timedelta(minutes=31)).isoformat(), cfg, now)[0])

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

    def test_only_ten_searches_are_selected_and_rotated(self):
        searches = [
            {"name": f"S{i}", "query": f"query {i}", "rules": []}
            for i in range(14)
        ]
        searches.extend([
            {"name": "Switch", "query": "nintendo switch", "rules": []},
            {"name": "PS5", "query": "ps5", "rules": []},
        ])
        cfg = {
            "max_searches_per_run": 10,
            "always_search_queries": ["nintendo switch", "ps5"],
        }
        with tempfile.TemporaryDirectory() as directory:
            cursor = Path(directory) / "cursor.json"
            with patch.object(bot, "SCAN_CURSOR_PATH", cursor):
                first = bot.select_searches_for_run(searches, cfg)
                second = bot.select_searches_for_run(searches, cfg)
        self.assertEqual(len(first), 10)
        self.assertEqual(len(second), 10)
        self.assertIn("nintendo switch", [x["query"] for x in first])
        self.assertNotEqual([x["query"] for x in first[2:]],
                            [x["query"] for x in second[2:]])

    def test_personal_variants_use_only_the_most_precise_query(self):
        searches = [
            {"name": "FILTRE - Nintendo 3DS / 3ds", "query": "3ds"},
            {"name": "FILTRE - Nintendo 3DS / nintendo 3ds", "query": "nintendo 3ds"},
            {"name": "Autre", "query": "ps5"},
        ]
        collapsed = bot._collapse_personal_variants(searches)
        self.assertEqual([x["query"] for x in collapsed], ["nintendo 3ds", "ps5"])

    def test_candidate_rank_prefers_fresher_more_profitable_deal(self):
        base = {"opportunity_score": 8, "demand_score": 5,
                "margin_low": 60, "view_count": 2, "favourite_count": 0}
        fresh = {**base, "age_minutes": 2}
        old = {**base, "age_minutes": 25}
        self.assertGreater(bot.candidate_rank(fresh), bot.candidate_rank(old))

    def test_zero_minute_listing_is_not_treated_as_unknown_age(self):
        base = {"opportunity_score": 8, "demand_score": 5,
                "margin_low": 30, "view_count": 0, "favourite_count": 0}
        self.assertGreater(
            bot.candidate_rank({**base, "age_minutes": 0}),
            bot.candidate_rank({**base, "age_minutes": 1}),
        )

    def test_title_match_does_not_require_missing_catalog_description(self):
        rule = {"must_contain": ["3ds"],
                "hardware_any": ["console", "fonctionne"]}
        self.assertTrue(bot.rule_match(rule, "Nintendo 3DS XL", "", deep=False))

    def test_discovery_recognises_known_high_demand_product(self):
        source = {"name": "Switch OLED", "category": "CONSOLE",
                  "price_to": 90}
        rule = {"brand": "Nintendo", "model": "Switch OLED",
                "must_contain": ["switch", "oled"],
                "resale_low": 220, "demand_score": 5}
        matched_search, matched_rule = bot.choose_known_product(
            [(source, rule)], "Nintendo Switch OLED console", 80,
        )
        self.assertIs(matched_search, source)
        self.assertIs(matched_rule, rule)
        self.assertEqual(
            bot.choose_known_product([(source, rule)],
                                     "Nintendo Switch OLED console", 120),
            (None, None),
        )

    def test_console_rule_rejects_game_empty_box_and_accessory(self):
        source = {"name": "Switch OLED", "category": "CONSOLE",
                  "price_to": 90}
        rule = {"must_contain": ["switch", "oled"],
                "resale_low": 220, "demand_score": 5}
        index = [(source, rule)]
        for title in (
            "Jeu Nintendo Switch OLED Edition",
            "Boîte vide Nintendo Switch OLED",
            "Coque Nintendo Switch OLED",
            "Mando para joysticks Nintendo Switch OLED",
        ):
            self.assertEqual(bot.choose_known_product(index, title, 20),
                             (None, None), title)
        self.assertIs(
            bot.choose_known_product(
                index, "Nintendo Switch OLED console", 80,
            )[1],
            rule,
        )

    def test_game_rule_rejects_console_and_wrong_platform(self):
        source = {"name": "Mario Kart", "category": "JEU_SWITCH",
                  "price_to": 20}
        rule = {"must_contain": ["mario kart"],
                "platform_any": ["switch", "nintendo switch"],
                "resale_low": 35, "demand_score": 5}
        index = [(source, rule)]
        self.assertEqual(
            bot.choose_known_product(
                index, "Console Nintendo Switch Mario Kart", 15,
            ),
            (None, None),
        )
        self.assertEqual(
            bot.choose_known_product(index, "Mario Kart Wii", 15),
            (None, None),
        )
        self.assertIs(
            bot.choose_known_product(
                index, "Mario Kart 8 Deluxe Nintendo Switch", 15,
            )[1],
            rule,
        )

    def test_ps5_game_name_wins_over_ps5_console_rule(self):
        console_source = {"category": "CONSOLE", "price_to": 250}
        console_rule = {
            "product_type": "CONSOLE", "must_contain": ["ps5"],
            "any_contain": ["disc", "disque", "lecteur"],
            "resale_low": 340, "demand_score": 5,
            "profile_priority": 10,
        }
        game_source = {"category": "JEU_PS5", "price_to": 16}
        game_rule = {
            "product_type": "GAME", "must_contain": ["elden ring"],
            "platform_any": ["ps5", "playstation 5"],
            "resale_low": 30, "demand_score": 5,
            "profile_priority": 10,
        }
        source, rule = bot.choose_known_product(
            [(console_source, console_rule), (game_source, game_rule)],
            "Elden Ring PS5 disque", 12,
        )
        self.assertIs(source, game_source)
        self.assertEqual(bot.infer_product_type(source, rule), "GAME")

        # Même trop cher pour être une affaire, le jeu ne devient jamais une
        # fausse console bon marché.
        self.assertEqual(
            bot.choose_known_product(
                [(console_source, console_rule), (game_source, game_rule)],
                "Elden Ring PS5 disque", 20,
            ),
            (None, None),
        )

        source, rule = bot.choose_known_product(
            [(console_source, console_rule), (game_source, game_rule)],
            "Console PS5 disc avec Elden Ring", 12,
        )
        self.assertIs(source, console_source)
        self.assertEqual(bot.infer_product_type(source, rule), "CONSOLE")

    def test_balanced_filter_keeps_missing_platform_as_a_risk(self):
        source = {"name": "Mario Kart", "category": "JEU_SWITCH"}
        rule = {"product_type": "GAME", "must_contain": ["mario kart"],
                "platform_any": ["switch", "nintendo switch"]}
        title = "Mario Kart 8 Deluxe comme neuf"
        self.assertTrue(bot.strict_product_type_check(source, rule, title)[0])
        self.assertIn(
            "plateforme non indiquée dans le titre",
            bot.soft_filter_risks(source, rule, title),
        )
        self.assertFalse(bot.strict_product_type_check(
            source, rule, "Mario Kart 8 Deluxe Wii",
        )[0])

    def test_console_with_included_controller_is_not_rejected(self):
        source = {"category": "CONSOLE", "product_type": "CONSOLE"}
        rule = {"product_type": "CONSOLE", "must_contain": ["ps5"]}
        self.assertTrue(bot.strict_product_type_check(
            source, rule, "Console PS5 avec manette DualSense",
        )[0])
        self.assertFalse(bot.strict_product_type_check(
            source, rule, "Manette compatible pour PS5",
        )[0])

    def test_incomplete_but_potentially_profitable_item_becomes_warning(self):
        source = {"category": "CONSOLE", "product_type": "CONSOLE"}
        rule = {"product_type": "CONSOLE", "must_contain": ["ps5"]}
        title = "Console PS5 sans manette"
        self.assertTrue(bot.strict_product_type_check(source, rule, title)[0])
        risks = bot.soft_filter_risks(source, rule, title)
        self.assertTrue(any("équipement à vérifier" in risk for risk in risks))

    def test_explicit_accessory_rule_is_separate_from_console_rules(self):
        source = {"name": "DualSense", "category": "ACCESSOIRE",
                  "product_type": "ACCESSORY", "price_to": 27}
        rule = {"product_type": "ACCESSORY", "accessory_type": "CONTROLLER",
                "must_contain": ["dualsense"], "resale_low": 45,
                "demand_score": 5}
        self.assertTrue(
            bot.strict_product_type_check(
                source, rule, "Manette PS5 DualSense officielle",
            )[0]
        )
        self.assertFalse(
            bot.strict_product_type_check(
                source, rule, "Support manette PS5 DualSense",
            )[0]
        )
        xbox_source = {"category": "ACCESSOIRE", "product_type": "ACCESSORY",
                       "price_to": 20}
        xbox_rule = {"product_type": "ACCESSORY", "accessory_type": "CONTROLLER",
                     "must_contain": ["xbox", "series"],
                     "any_contain": ["controller", "manette"]}
        self.assertFalse(bot.rule_match(
            xbox_rule, "Xbox 360 controller bedraad", "",
        ))

    def test_accessory_blacklist_can_allow_a_named_target_only(self):
        blacklist = {"hard_blacklist": [], "fake_blacklist": [],
                     "accessory_blacklist": ["manette"]}
        self.assertTrue(bot.blacklist_check(
            "Manette DualSense", "", blacklist,
        )[0])
        self.assertFalse(bot.blacklist_check(
            "Manette DualSense", "", blacklist, check_accessories=False,
        )[0])

    def test_loose_retro_game_exception_does_not_allow_empty_boxes(self):
        blacklist = {"hard_blacklist": [], "fake_blacklist": [],
                     "accessory_blacklist": ["cartouche seule", "boite vide"]}
        self.assertFalse(bot.blacklist_check(
            "Pokemon Rouge cartouche seule", "", blacklist,
            ignored_accessory_terms=bot.LOOSE_GAME_TERMS,
        )[0])
        self.assertTrue(bot.blacklist_check(
            "Pokemon Rouge boite vide", "", blacklist,
            ignored_accessory_terms=bot.LOOSE_GAME_TERMS,
        )[0])

    def test_bundle_rule_requires_the_announced_minimum_count(self):
        source = {"category": "JEU_SWITCH", "product_type": "GAME"}
        rule = {"product_type": "GAME", "platform_any": ["switch"],
                "bundle_min_items": 5}
        self.assertFalse(bot.strict_product_type_check(
            source, rule, "Lot de 3 jeux Nintendo Switch",
        )[0])
        self.assertTrue(bot.strict_product_type_check(
            source, rule, "Lot de 5 jeux Nintendo Switch",
        )[0])

    def test_market_profile_loads_all_valid_products(self):
        searches = bot.load_target_products()
        self.assertEqual(len(searches), 77)
        types = {search["product_type"] for search in searches}
        self.assertEqual(types, {"CONSOLE", "GAME", "ACCESSORY"})

    def test_scan_allows_named_accessory_but_rejects_console_false_positive(self):
        async def run():
            cfg = {
                "max_items_per_search": 10, "max_listing_age_hours": 0.5,
                "exclude_professional_sellers": True,
                "buyer_protection_estimate": {"fixed": 0.7, "pct": 0.05},
                "shipping_estimate": 4.5, "candidate_min_margin": 8,
                "candidate_min_roi_pct": 20, "min_candidate_score": 5,
                "instant_listing_minutes": 5, "hidden_deal_bonus": 1,
            }
            targets = bot.load_target_products()
            index = bot.build_rule_index(targets, {"min_demand_score": 4})
            timestamp = datetime.now(timezone.utc).timestamp()
            items = [
                {"id": 1, "title": "Mando para joysticks Nintendo Switch",
                 "price": {"amount": "10"}, "created_at_ts": timestamp,
                 "user": {}, "view_count": 0, "favourite_count": 0},
                {"id": 2, "title": "Manette PS5 DualSense officielle",
                 "price": {"amount": "20"}, "created_at_ts": timestamp,
                 "user": {}, "view_count": 0, "favourite_count": 0},
            ]
            stats = {key: 0 for key in (
                "catalog_requested", "catalog_success", "catalog_items",
                "catalog_seconds", "items_examined", "age_known", "age_unknown",
                "notifications_sent", "candidates", "rejected_price",
                "rejected_old", "rejected_seen", "rejected_pro",
                "rejected_blacklist", "rejected_rule", "rejected_profit",
                "rejected_score",
            )}
            blacklist = {"hard_blacklist": [], "fake_blacklist": [],
                         "accessory_blacklist": ["manette", "joystick"]}
            with patch.object(
                    bot, "catalog_items", AsyncMock(return_value=items)):
                rows = await bot.scan_search(
                    {"name": "Découverte", "query": "nintendo", "price_to": 200},
                    cfg, blacklist, set(), {}, object(), object(),
                    "https://www.vinted.be", {}, stats, rule_index=index,
                )
            return rows

        rows = asyncio.run(run())
        self.assertEqual([row["model"] for row in rows], ["Manette PS5 DualSense"])
        self.assertEqual(rows[0]["product_type"], "ACCESSORY")

    def test_personal_filter_carries_explicit_product_type_and_ratio(self):
        rows = bot.convert_personal_filter({
            "actif": True, "nom": "Mario Kart", "categorie": "JEU_SWITCH",
            "type_produit": "jeu", "recherches_vinted": ["mario kart"],
            "revente_prudente": 35, "prix_recherche_max": 15,
            "ratio_achat_max": 0.4, "mots_obligatoires": ["mario kart"],
        })
        self.assertEqual(rows[0]["product_type"], "JEU")
        self.assertEqual(rows[0]["rules"][0]["product_type"], "JEU")
        self.assertEqual(rows[0]["rules"][0]["max_buy_ratio"], 0.4)

    def test_rule_index_keeps_only_demanded_products(self):
        searches = [{"rules": [
            {"model": "Fast", "must_contain": ["fast"], "demand_score": 5},
            {"model": "Slow", "must_contain": ["slow"], "demand_score": 2},
        ]}]
        index = bot.build_rule_index(searches, {"min_demand_score": 4})
        self.assertEqual(len(index), 1)
        self.assertEqual(index[0][1]["model"], "Fast")

    def test_curated_profile_wins_over_an_optimistic_legacy_rule(self):
        curated_source = {"category": "CONSOLE", "price_to": 50}
        curated_rule = {"must_contain": ["nintendo", "switch"],
                        "exclude": ["oled", "lite"], "resale_low": 110,
                        "demand_score": 5, "profile_priority": 10}
        legacy_source = {"category": "CONSOLE", "price_to": 65}
        legacy_rule = {"must_contain": ["switch"], "any_contain": ["v2"],
                       "resale_low": 180, "demand_score": 5}
        source, rule = bot.choose_known_product(
            [(curated_source, curated_rule), (legacy_source, legacy_rule)],
            "Nintendo Switch V2 console", 40,
        )
        self.assertIs(source, curated_source)
        self.assertIs(rule, curated_rule)

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
