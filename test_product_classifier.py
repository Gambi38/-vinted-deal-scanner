import unittest

from product_classifier import (
    allowed_distance,
    build_rule_vocabulary,
    canonicalize_product_title,
    levenshtein_distance,
)


class ProductClassifierTests(unittest.TestCase):
    def test_levenshtein_reference_cases(self):
        self.assertEqual(levenshtein_distance("kitten", "sitting"), 3)
        self.assertEqual(levenshtein_distance("nintendo", "nitendo"), 1)
        self.assertEqual(levenshtein_distance("", "ps5"), 3)

    def test_early_exit_returns_above_limit(self):
        self.assertGreater(levenshtein_distance("console", "cartouche", 1), 1)

    def test_short_and_numeric_tokens_are_never_fuzzy(self):
        self.assertEqual(allowed_distance("xbox"), 0)
        self.assertEqual(allowed_distance("ps5"), 0)
        self.assertEqual(allowed_distance("series5"), 0)
        self.assertEqual(
            canonicalize_product_title("PS4", ("ps5",)),
            "ps4",
        )

    def test_one_typo_is_corrected_for_medium_word(self):
        self.assertEqual(
            canonicalize_product_title("Nitendo Switch", ("nintendo", "switch")),
            "nintendo switch",
        )

    def test_two_typos_are_allowed_only_for_long_word(self):
        self.assertEqual(
            canonicalize_product_title("playstasion", ("playstation",)),
            "playstation",
        )
        self.assertEqual(
            canonicalize_product_title("nintxxdo", ("nintendo",)),
            "nintxxdo",
        )

    def test_apostrophe_does_not_split_a_known_game_title(self):
        self.assertEqual(
            canonicalize_product_title("Assassin's Creed", ("assassins creed",)),
            "assassins creed",
        )

    def test_joined_phrase_is_split_from_rule_vocabulary(self):
        self.assertEqual(
            canonicalize_product_title("Mariokart Deluxe", ("mario kart",)),
            "mario kart deluxe",
        )

    def test_spaced_word_can_join_without_static_alias(self):
        self.assertEqual(
            canonicalize_product_title("Play Station 5", ("playstation",)),
            "playstation 5",
        )

    def test_accents_and_punctuation_are_normalised(self):
        self.assertEqual(
            canonicalize_product_title("Pokémon—Émeraude!", ("pokemon", "emeraude")),
            "pokemon emeraude",
        )

    def test_ambiguous_equal_distance_is_not_corrected(self):
        self.assertEqual(
            canonicalize_product_title("cove", ("cover", "coven")),
            "cove",
        )

    def test_excessive_distance_is_rejected(self):
        self.assertEqual(
            canonicalize_product_title("ninendozz", ("nintendo",)),
            "ninendozz",
        )

    def test_vocabulary_is_derived_from_active_rules(self):
        source = {"name": "Nintendo", "query": "nintendo switch"}
        rule = {
            "brand": "Nintendo", "model": "Mario Kart 8 Deluxe",
            "must_contain": ["mario kart"], "platform_any": ["switch"],
        }
        vocabulary = build_rule_vocabulary([(source, rule)])
        self.assertIn("nintendo", vocabulary)
        self.assertIn("mario kart", vocabulary)
        self.assertIn("switch", vocabulary)


if __name__ == "__main__":
    unittest.main()
