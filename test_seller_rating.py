import unittest
from seller_rating import seller_is_allowed


class SellerRatingTests(unittest.TestCase):
    def test_threshold_is_inclusive(self):
        for stars, expected in [(0, False), (3.99, False), (4, True), (5, True)]:
            self.assertEqual(seller_is_allowed({'user': {'rating_out_of_5': stars}}, {})[0], expected)

    def test_unknown_and_invalid_are_excluded(self):
        for seller in [None, {}, {'rating_out_of_5': True}, {'rating_out_of_5': 'nan'},
                       {'rating_out_of_5': 6}, {'rating_out_of_5': -1}]:
            self.assertFalse(seller_is_allowed({'user': seller}, {})[0])

    def test_no_automatic_scale_guess(self):
        self.assertFalse(seller_is_allowed({'user': {'feedback_reputation': 1}}, {})[0])

    def test_explicit_scale(self):
        cfg = {'seller_rating_field': 'feedback_reputation', 'seller_rating_scale': 1}
        self.assertTrue(seller_is_allowed({'user': {'feedback_reputation': .8}}, cfg)[0])
        self.assertFalse(seller_is_allowed({'user': {'feedback_reputation': .79}}, cfg)[0])

    def test_seller_container(self):
        self.assertTrue(seller_is_allowed({'seller': {'rating_out_of_5': 4}}, {})[0])

    def test_invalid_threshold(self):
        with self.assertRaises(ValueError):
            seller_is_allowed({}, {'min_seller_rating': 'nan'})
