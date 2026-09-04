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
from monitoring import build_workflow_report
from product_classifier import canonicalize_product_title
from scanner_runtime import ApiBudgetExceeded, ApiCostController
from search_cache import load_rule_index, save_rule_index, search_fingerprint
from sold_listings import SoldListingsProvider


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
        self.assertEqual(cfg["max_items_per_search"], 100)
        self.assertEqual(cfg["max_catalog_items_per_run"], 1200)
        self.assertEqual(cfg["max_searches_per_run"], 12)
        self.assertEqual(cfg["max_alerts_per_run"], 12)
        self.assertTrue(cfg["snipe_mode"])
        self.assertEqual(cfg["snipe_max_pages"], 2)
        self.assertEqual(cfg["api_budget_max_requests_per_cycle"], 24)
        self.assertEqual(cfg["min_candidate_score"], 2.5)
        self.assertEqual(cfg["popularity_penalty_cap"], 1.0)
        self.assertLessEqual(cfg["request_delay_max_seconds"], 1.2)
        self.assertEqual(cfg["api_max_concurrency"], 3)

    def test_workflow_session_repeats_three_cycles(self):
        cfg = {
            "cycles_per_workflow": 3,
            "seconds_between_cycles": 75,
            "max_alerts_per_run": 10,
        }

        async def run():
            with patch.object(bot, "load_json", return_value=cfg), \
                    patch.object(bot, "main_async", new=AsyncMock()) as scan, \
                    patch.object(bot.asyncio, "sleep", new=AsyncMock()) as sleep:
                await bot.run_workflow_session()
                return scan.await_count, sleep.await_args_list

        scans, sleeps = asyncio.run(run())
        self.assertEqual(scans, 3)
        self.assertEqual([call.args[0] for call in sleeps], [75.0, 75.0])

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

    def test_notification_selection_limits_one_category_without_losing_diversity(self):
        candidates = [
            {"item_id": str(index), "category": category}
            for index, category in enumerate(
                ["SMARTPHONE"] * 7 + ["JEU_SWITCH"] * 4 + ["TOOL"] * 4
            )
        ]
        selected = bot.select_diverse_candidates(candidates, 12, 4)
        self.assertEqual(len(selected), 12)
        self.assertEqual(
            sum(row["category"] == "SMARTPHONE" for row in selected),
            4,
        )

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
        self.assertFalse(bot.strict_product_type_check(
            source, rule, "Manette PS5 pour console PlayStation 5",
        )[0])

    def test_console_rejects_protective_covers_and_comando(self):
        source = {"category": "CONSOLE", "product_type": "CONSOLE"}
        rule = {"product_type": "CONSOLE", "must_contain": ["switch"]}
        accessory_titles = (
            "Cover de protection pour Nintendo Switch",
            "Protective case Nintendo Switch",
            "Funda protectora para Nintendo Switch",
            "Custodia Nintendo Switch",
            "Capa de proteção Nintendo Switch",
            "Schutzhülle Nintendo Switch",
            "Comando para console Nintendo Switch",
        )
        for title in accessory_titles:
            self.assertFalse(
                bot.strict_product_type_check(source, rule, title)[0], title,
            )
        self.assertTrue(bot.strict_product_type_check(
            source, rule, "Console Nintendo Switch avec housse de protection",
        )[0])

    def test_controller_rejects_cover_but_keeps_controller_with_case(self):
        source = {"category": "ACCESSOIRE", "product_type": "ACCESSORY"}
        rule = {"product_type": "ACCESSORY", "accessory_type": "CONTROLLER"}
        self.assertFalse(bot.strict_product_type_check(
            source, rule, "Coque de protection DualSense PS5",
        )[0])
        self.assertFalse(bot.strict_product_type_check(
            source, rule, "Protective cover for PS5 controller",
        )[0])
        self.assertTrue(bot.strict_product_type_check(
            source, rule, "Manette DualSense avec coque de protection",
        )[0])

    def test_ds_battery_in_multiple_languages_is_never_a_console(self):
        source = {"category": "CONSOLE", "product_type": "CONSOLE"}
        rule = {"product_type": "CONSOLE", "must_contain": ["ds", "lite"]}
        battery_titles = (
            "Battery Nintendo DS Lite",
            "Nintendo DS Lite battery replacement",
            "Batterij voor Nintendo DS Lite",
            "Batteria per Nintendo DS Lite",
            "Akku für Nintendo DS Lite",
        )
        for title in battery_titles:
            self.assertFalse(
                bot.strict_product_type_check(source, rule, title)[0], title,
            )
        self.assertTrue(bot.strict_product_type_check(
            source, rule, "Console Nintendo DS Lite avec batterie neuve",
        )[0])
        self.assertTrue(bot.strict_product_type_check(
            source, rule, "Nintendo DS Lite avec batterie incluse",
        )[0])

    def test_little_nightmares_switch_is_a_game_not_the_console(self):
        targets = bot.load_target_products()
        index = bot.build_rule_index(targets, {"min_demand_score": 4})
        self.assertEqual(
            bot.choose_known_product(
                index, "Little Nightmares Nintendo Switch", 15,
            ),
            (None, None),
        )
        source, rule = bot.choose_known_product(
            index, "Console Nintendo Switch avec Little Nightmares", 45,
        )
        self.assertIsNotNone(rule)
        self.assertEqual(bot.infer_product_type(source, rule), "CONSOLE")

    def test_unknown_game_before_platform_never_inherits_console_value(self):
        targets = bot.load_target_products()
        index = bot.build_rule_index(targets, {"min_demand_score": 4})
        false_console_titles = (
            "Digimon Survive - Nintendo Switch (Nuovo)",
            "Hades Nintendo Switch",
            "Rampage World Tour Nintendo 64",
            "Harry Potter GameCube",
            "Michael Jackson PS Vita",
        )
        for title in false_console_titles:
            self.assertEqual(
                bot.choose_known_product(index, title, 25),
                (None, None),
                title,
            )

    def test_console_sales_prefixes_remain_accepted(self):
        targets = bot.load_target_products()
        index = bot.build_rule_index(targets, {"min_demand_score": 4})
        console_titles = (
            ("Vends ma Nintendo Switch", 45, "Nintendo Switch LCD"),
            ("Lot 2 Nintendo Switch", 45, "Nintendo Switch LCD"),
            ("Console Nintendo Switch avec Digimon", 45, "Nintendo Switch LCD"),
            ("New Nintendo 3DS XL", 100, "New Nintendo 3DS XL"),
        )
        for title, price, expected_model in console_titles:
            source, rule = bot.choose_known_product(index, title, price)
            self.assertIsNotNone(rule, title)
            self.assertEqual(rule.get("model"), expected_model, title)
            self.assertEqual(bot.infer_product_type(source, rule), "CONSOLE")

    def test_packaging_title_requires_confirmation_product_is_included(self):
        game_source = {"category": "JEU_PS5", "product_type": "GAME"}
        game_rule = {"product_type": "GAME", "must_contain": ["minecraft"],
                     "platform_any": ["ps5"]}
        self.assertFalse(bot.strict_product_type_check(
            game_source, game_rule, "Boîte Minecraft PS5",
        )[0])
        self.assertFalse(bot.strict_product_type_check(
            game_source, game_rule, "Minecraft PS5 boîte vide",
        )[0])
        self.assertTrue(bot.strict_product_type_check(
            game_source, game_rule, "Boîte Minecraft PS5 avec le jeu inclus",
        )[0])
        self.assertTrue(bot.strict_product_type_check(
            game_source, game_rule, "Minecraft PS5 avec boîte",
        )[0])

    def test_real_profile_rejects_box_and_controller_but_keeps_console_bundle(self):
        targets = bot.load_target_products()
        index = bot.build_rule_index(targets, {"min_demand_score": 4})
        self.assertEqual(
            bot.choose_known_product(
                index, "Boîte Minecraft Nintendo Switch", 10,
            ),
            (None, None),
        )
        self.assertEqual(
            bot.choose_known_product(
                index, "Manette PS5 pour console PlayStation 5 disque", 20,
            ),
            (None, None),
        )
        source, rule = bot.choose_known_product(
            index, "Console PS5 disque avec manette et jeu", 200,
        )
        self.assertIsNotNone(rule)
        self.assertEqual(bot.infer_product_type(source, rule), "CONSOLE")

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
        self.assertEqual(len(searches), 113)
        types = {search["product_type"] for search in searches}
        self.assertTrue({"CONSOLE", "GAME", "ACCESSORY"}.issubset(types))

    def test_dedicated_console_catalog_has_prices_and_descriptions(self):
        data = bot.load_json(bot.CONSOLES_TARGETS_PATH, {})
        products = data.get("products", [])
        self.assertEqual(len(products), 24)
        for product in products:
            self.assertEqual(product.get("type"), "CONSOLE", product.get("name"))
            self.assertTrue(product.get("description"), product.get("name"))
            self.assertGreater(float(product.get("price_max", 0)), 0)
            self.assertGreaterEqual(
                float(product.get("resale_low", 0)),
                float(product.get("price_max", 0)),
            )

    def test_console_catalog_overrides_general_duplicate(self):
        searches = bot.load_target_products()
        switch = next(
            search for search in searches
            if search["rules"][0].get("model") == "Nintendo Switch LCD"
        )
        rule = switch["rules"][0]
        self.assertEqual(rule["resale_low"], 110)
        self.assertIn("tablette", rule["description"].lower())

    def test_every_catalog_rule_has_explicit_type_description_and_price(self):
        for search in bot.load_target_products():
            for rule in search.get("rules", []):
                self.assertIn(rule.get("product_type"), {
                    "CONSOLE", "GAME", "ACCESSORY", "CALCULATOR", "CAMERA",
                    "ACTION_CAMERA", "MINI_PC", "AUDIO", "EREADER",
                    "STREAMING", "SMARTWATCH", "DRAWING_TABLET",
                })
                self.assertTrue(rule.get("description"), rule.get("model"))
                self.assertGreater(float(rule.get("resale_low", 0)), 0)

    def test_discovery_profile_ignores_legacy_and_broad_console_filters(self):
        cfg = bot.load_json(Path(bot.__file__).with_name("config.json"), {})
        legacy = list(cfg.get("searches", []))
        cfg["searches"] = bot.load_target_products()
        blacklist = {"accessory_blacklist": [], "title_accessory_blacklist": []}
        bot.apply_personal_filters(cfg, blacklist)
        names = {search.get("name") for search in cfg["searches"]}
        self.assertTrue(legacy)
        self.assertNotIn("JEU_RETRO - Pokemon Game Boy", names)
        self.assertFalse(any(
            name and name.startswith("FILTRE - Nintendo 3DS /")
            for name in names
        ))

    def test_logged_false_positives_no_longer_match_a_product(self):
        cfg = bot.load_json(Path(bot.__file__).with_name("config.json"), {})
        cfg["searches"] = bot.load_target_products()
        blacklist = bot.load_json(Path(bot.__file__).with_name("blacklist.json"), {})
        bot.apply_personal_filters(cfg, blacklist)
        index = bot.build_rule_index(cfg["searches"], cfg)
        false_positives = (
            ("gachette R nintendo switch retrogaming", 3),
            ("Pochette transport switch mario odyssey", 5),
            ("nintendo switch ac adapter", 22),
            ("Nintendo nes tennis", 8),
            ("Mario party star rush Nintendo 3DS nuovo", 15),
            ("Ps vita michael jackson", 7),
            ("inazuma eleven go luce per nintendo 3ds cartuccia", 13.9),
            ("Lector NFC Nintendo 3ds", 15),
            ("nintendo 3ds spelletjes", 10),
            ("Nintendo switch Sports", 25),
            ("Nintendo NES top gun second mission", 10),
        )
        for title, price in false_positives:
            self.assertEqual(
                bot.choose_known_product(index, title, price, cfg),
                (None, None), title,
            )
        self.assertTrue(bot.blacklist_check(
            "Pokemon Gameboy Rot Reproduktion", "", blacklist,
        )[0])

    def test_latest_run_accessories_never_inherit_console_value(self):
        cfg = bot.load_json(Path(bot.__file__).with_name("config.json"), {})
        index = bot.build_rule_index(bot.load_target_products(), cfg)
        accessories = (
            ("Mini sac pour Nintendo DS Lite DSi", 8),
            ("Nintendo 3DS Sacoche Disney Frozen", 10),
            ("Carregador PS Vita", 7),
            ("Pack stylets 3DS XL 2DS", 6),
            ("Nintendo 3DS AR card", 4),
            ("Nintendo Switch Thumb Grip & Button Cap", 5),
            ("Starfox Nintendo Switch 2 keychain", 39),
            ("Carcasa New Nintendo 3DS XL", 15),
            ("Ps4 pro controler", 30),
        )
        for title, price in accessories:
            self.assertEqual(
                bot.choose_known_product(index, title, price, cfg),
                (None, None), title,
            )

    def test_latest_run_games_never_inherit_console_value(self):
        cfg = bot.load_json(Path(bot.__file__).with_name("config.json"), {})
        index = bot.build_rule_index(bot.load_target_products(), cfg)
        games_without_a_curated_resale_profile = (
            ("House of Ashes Xbox One X", 10),
            ("Trials Rising Nintendo Switch", 9),
            ("Mario Tennis Open Nintendo 3DS", 10),
            ("Mario & Luigi Superstar Saga Nintendo 3DS", 15),
            ("Assassin's Creed Shadows Xbox Series X", 20),
            ("Nintendo Switch Ring Fit", 7),
            ("Nintendo 64 Rampage World Tour", 30),
            ("Batman Arkham Origins Blackgate para 3DS", 25),
            ("Nintendo Switch 2 Hogwarts", 20),
        )
        for title, price in games_without_a_curated_resale_profile:
            self.assertEqual(
                bot.choose_known_product(index, title, price, cfg),
                (None, None), title,
            )

    def test_mario_kart_remains_a_profitable_game(self):
        cfg = bot.load_json(Path(bot.__file__).with_name("config.json"), {})
        index = bot.build_rule_index(bot.load_target_products(), cfg)
        source, rule = bot.choose_known_product(
            index, "Mario Kart 8 Deluxe Nintendo Switch", 7, cfg,
        )
        self.assertIsNotNone(rule)
        self.assertEqual(bot.infer_product_type(source, rule), "GAME")
        self.assertEqual(rule["model"], "Mario Kart 8 Deluxe")
        self.assertGreater(bot.score_candidate(rule, 7, cfg)[3], 8)

    def test_plain_console_model_remains_a_candidate(self):
        cfg = bot.load_json(Path(bot.__file__).with_name("config.json"), {})
        index = bot.build_rule_index(bot.load_target_products(), cfg)
        for title, price in (("Xbox One S", 35), ("PS4 slim", 40)):
            source, rule = bot.choose_known_product(index, title, price, cfg)
            self.assertIsNotNone(rule, title)
            self.assertEqual(bot.infer_product_type(source, rule), "CONSOLE")

    def test_controlled_typo_detection_finds_hidden_products(self):
        self.assertEqual(
            canonicalize_product_title(
                "Nitendo Mariokart 8 Deluxe", ("nintendo", "mario kart"),
            ),
            "nintendo mario kart 8 deluxe",
        )
        cfg = bot.load_json(Path(bot.__file__).with_name("config.json"), {})
        index = bot.build_rule_index(bot.load_target_products(), cfg)
        source, rule = bot.choose_known_product(
            index, "Nitendo Mariokart 8 Deluxe Switch", 7, cfg,
        )
        self.assertIsNotNone(rule)
        self.assertEqual(bot.infer_product_type(source, rule), "GAME")

    def test_price_drop_event_bypasses_only_after_twenty_percent(self):
        history = {}
        cfg = {"price_drop_alert_pct": 20}
        self.assertEqual(
            bot.price_drop_event(history, "42", 100, cfg)[:2],
            (None, 0.0),
        )
        previous, drop, event = bot.price_drop_event(history, "42", 85, cfg)
        self.assertEqual((previous, drop, event), (100.0, 15.0, False))
        previous, drop, event = bot.price_drop_event(history, "42", 60, cfg)
        self.assertEqual(previous, 85.0)
        self.assertAlmostEqual(drop, 29.4)
        self.assertTrue(event)

    def test_api_cost_controller_stops_excess_requests(self):
        async def run():
            controller = ApiCostController(2, 2)
            await controller.spend(1, "catalog")
            await controller.spend(1, "catalog")
            with self.assertRaises(ApiBudgetExceeded):
                await controller.spend(1, "catalog")
            return controller.snapshot()

        snapshot = asyncio.run(run())
        self.assertEqual(snapshot["requests"], 2)
        self.assertEqual(snapshot["blocked"], 1)

    def test_confirmed_rejections_require_an_explicit_category(self):
        blacklist = {}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rejets.txt"
            path.write_text(
                "# commentaire\naccessory:keychain\nfake:reproduktion\nligne dangereuse\n",
                encoding="utf-8",
            )
            added = bot.apply_confirmed_rejections(path, blacklist)
        self.assertEqual(added, 2)
        self.assertEqual(blacklist["accessory_blacklist"], ["keychain"])
        self.assertEqual(blacklist["fake_blacklist"], ["reproduktion"])

    def test_snipe_pagination_requests_page_two_only_while_fresh(self):
        async def run(last_age_minutes):
            now = datetime.now(timezone.utc)
            items = []
            for index in range(50):
                age = last_age_minutes if index == 49 else 1
                items.append({
                    "id": index + 1,
                    "title": f"Article inconnu {index}",
                    "price": {"amount": "10"},
                    "created_at_ts": (now - timedelta(minutes=age)).timestamp(),
                    "user": {},
                })
            stats = {key: 0 for key in (
                "catalog_requested", "catalog_success", "catalog_items",
                "catalog_seconds", "items_examined", "age_known", "age_unknown",
                "notifications_sent", "candidates", "rejected_price",
                "rejected_old", "rejected_seen", "rejected_pro",
                "rejected_blacklist", "rejected_rule", "rejected_profit",
                "rejected_score", "catalog_budget_blocked",
            )}
            cfg = {
                "catalog_per_page": 50, "max_items_per_search": 100,
                "max_listing_age_hours": 0.5, "snipe_mode": True,
                "snipe_max_pages": 2, "reject_unknown_listing_age": True,
            }
            mocked = AsyncMock(side_effect=[items, []])
            with patch.object(bot, "catalog_items", mocked):
                await bot.scan_search(
                    {"name": "Snipe", "query": "test"}, cfg,
                    {"hard_blacklist": [], "fake_blacklist": []},
                    set(), {}, object(), object(), "https://www.vinted.be",
                    {}, stats, rule_index=[], price_history={},
                )
            return mocked.await_count

        self.assertEqual(asyncio.run(run(1)), 2)
        self.assertEqual(asyncio.run(run(31)), 1)

    def test_rule_index_local_cache_round_trip(self):
        searches = [{"name": "A", "rules": [{"model": "B"}]}]
        fingerprint = search_fingerprint(searches, {"min": 4})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "searches_cache.json"
            index = [(searches[0], searches[0]["rules"][0])]
            save_rule_index(path, fingerprint, index)
            loaded = load_rule_index(path, fingerprint)
            self.assertEqual(loaded, index)
            self.assertIsNone(load_rule_index(path, "autre"))

    def test_sold_listings_reference_is_conservative_and_recent(self):
        now = datetime.now(timezone.utc).timestamp()
        data = {"products": {"Mario Kart 8 Deluxe": {
            "source": "test autorisé",
            "samples": [
                {"price": 32, "sold_at": now},
                {"price": 34, "sold_at": now},
                {"price": 36, "sold_at": now},
            ],
        }}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sold.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            provider = SoldListingsProvider(path, 30, 3)
            searches = [{"rules": [{
                "model": "Mario Kart 8 Deluxe", "resale_low": 30,
            }]}]
            self.assertEqual(provider.enrich(searches), 1)
        self.assertEqual(searches[0]["rules"][0]["market_avg"], 30)
        self.assertEqual(searches[0]["rules"][0]["sold_samples"], 3)

    def test_workflow_report_contains_rejections_top_and_history(self):
        cycle = {
            "stats": {
                "items_examined": 100, "catalog_items": 120,
                "candidates": 3, "notifications_sent": 2,
                "rejected_rule": 70, "rejected_old": 20,
                "catalog_requested": 10, "catalog_success": 10,
            },
            "top_opportunities": [{
                "item_id": "1", "title": "Mario Kart", "url": "https://example/1",
                "rank_score": 900,
            }],
        }
        report = build_workflow_report([cycle])
        self.assertEqual(report["summary"]["listings_examined"], 100)
        self.assertEqual(report["rejection_rates"]["rule"]["rate_pct"], 70)
        self.assertEqual(report["best_opportunities"][0]["url"], "https://example/1")
        self.assertEqual(len(report["history_30_days"]), 1)

    def test_authentic_high_demand_gba_games_remain_targets(self):
        cfg = bot.load_json(Path(bot.__file__).with_name("config.json"), {})
        searches = bot.load_target_products()
        index = bot.build_rule_index(searches, cfg)
        for title, price in (
            ("Pokemon Émeraude GBA", 12),
            ("Pokemon FireRed GBA original", 11),
            ("Pokemon Saphir GBA", 11),
        ):
            source, rule = bot.choose_known_product(index, title, price, cfg)
            self.assertIsNotNone(rule, title)
            self.assertEqual(bot.infer_product_type(source, rule), "GAME")

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
