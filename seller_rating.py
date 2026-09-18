"""Filtre de note avec échelles explicites; aucune note manquante inventée."""
import math


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def seller_rating(item, cfg):
    seller = item.get('user')
    if not isinstance(seller, dict):
        seller = item.get('seller')
    if not isinstance(seller, dict):
        return None
    # Un champ au nom ambigu (rating, feedback_reputation) exige une
    # échelle configurée après vérification du fournisseur de données.
    field = cfg.get('seller_rating_field', 'rating_out_of_5')
    maximum = number(cfg.get('seller_rating_scale', 5))
    value = number(seller.get(field))
    if maximum is None or maximum <= 0 or value is None or not 0 <= value <= maximum:
        return None
    return value * 5 / maximum


def seller_is_allowed(item, cfg):
    threshold = number(cfg.get('min_seller_rating', 4))
    if threshold is None or not 0 <= threshold <= 5:
        raise ValueError('min_seller_rating doit être entre 0 et 5')
    rating = seller_rating(item, cfg)
    if rating is None:
        return False, None, 'missing'
    if rating < threshold:
        return False, rating, 'low'
    return True, rating, ''
