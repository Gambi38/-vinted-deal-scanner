#!/usr/bin/env python3
"""Scoring and ranking logic optimized for v8."""

from typing import Dict, List, Tuple, Any, Optional
import re
from src.utils import norm, present, token_similarity, hamming_sim

# ============================================================================
# Category Price Bands
# ============================================================================

CATEGORY_DEFAULT_CAP = {
    "JEU_SWITCH": 18.0,
    "JEU_PS5": 28.0,
    "JEU_3DS": 22.0,
    "JEU_DS": 15.0,
    "JEU_VITA": 22.0,
    "JEU_PSP": 15.0,
    "JEU_RETRO": 25.0,
    "ELECTRONIQUE": 25.0,
    "CONSOLE": 200.0,
}

CATEGORY_MIN_SCORE = {
    "JEU_SWITCH": 66,
    "JEU_PS5": 66,
    "JEU_3DS": 68,
    "JEU_DS": 70,
    "JEU_VITA": 68,
    "JEU_PSP": 70,
    "JEU_RETRO": 74,
    "ELECTRONIQUE": 72,
    "CONSOLE": 72,
}


# ============================================================================
# Price Score Calculation
# ============================================================================

def category_price_score(
    category: str,
    price: float,
    cap: Optional[float],
    search_name: str = "",
    title: str = "",
) -> Tuple[int, str]:
    """
    Calculate price-based score for a listing.
    V8: Faster category-specific scoring with clear bands.
    
    Returns: (score, reason)
    """
    score = 0
    reason = []
    cap = float(cap or CATEGORY_DEFAULT_CAP.get(category, 30.0))
    
    if category == "JEU_SWITCH":
        if price <= 5:
            score += 45
            reason.append("<=5€")
        elif price <= 10:
            score += 38
            reason.append("<=10€")
        elif price <= 15:
            score += 30
            reason.append("<=15€")
        elif price <= min(18, cap):
            score += 18
        else:
            return 0, "prix_switch_trop_haut"
    
    elif category == "JEU_PS5":
        if price <= 8:
            score += 42
            reason.append("<=8€")
        elif price <= 15:
            score += 34
            reason.append("<=15€")
        elif price <= 22:
            score += 24
        elif price <= min(28, cap):
            score += 14
        else:
            return 0, "prix_ps5_trop_haut"
    
    elif category in {"JEU_3DS", "JEU_DS", "JEU_VITA", "JEU_PSP"}:
        if price <= 8:
            score += 38
        elif price <= 15:
            score += 28
        elif price <= cap:
            score += 15
        else:
            return 0, "prix_portable_trop_haut"
    
    elif category == "JEU_RETRO":
        if price <= 8:
            score += 32
        elif price <= 15:
            score += 22
        elif price <= cap:
            score += 10
        else:
            return 0, "retro_trop_cher"
    
    elif category == "ELECTRONIQUE":
        s = norm(f"{search_name} {title}")
        if "ti" in s:
            if price <= 10:
                score += 40
            else:
                return 0, "ti_gt_10"
        else:
            ratio = price / max(cap, 1)
            if ratio <= 0.35:
                score += 34
            elif ratio <= 0.60:
                score += 22
            elif ratio <= 0.85:
                score += 10
            else:
                return 0, "electronique_trop_proche_plafond"
    
    elif category == "CONSOLE":
        ratio = price / max(cap, 1)
        if ratio <= 0.40:
            score += 38
        elif ratio <= 0.60:
            score += 28
        elif ratio <= 0.80:
            score += 16
        elif ratio <= 1.0:
            score += 8
        else:
            return 0, "console_au_dessus_plafond"
    
    else:
        ratio = price / max(cap, 1)
        if ratio <= 0.50:
            score += 22
        elif ratio <= 0.80:
            score += 10
        else:
            return 0, "prix_peu_agressif"
    
    return score, ",".join(reason)


# ============================================================================
# Learning Bonus (v8)
# ============================================================================

def learned_bonus(
    title: str,
    price: float,
    category: str,
    image_hash: str,
    base: Dict[str, Any],
) -> Tuple[int, str]:
    """
    Score bonus based on learned patterns from fast sales and examples.
    
    Returns: (bonus_score, reason)
    """
    best = 0
    reasons = []
    v8 = base.get("v8", {})
    
    # Fast sales similarity (highest priority)
    for ex in v8.get("fast_sales", [])[-180:]:
        if ex.get("category") and category and ex.get("category") != category:
            continue
        
        ts = token_similarity(title, ex.get("title", ""))
        ps = 0.0
        try:
            ep = float(ex.get("price") or 0)
            if ep > 0:
                ps = max(0.0, 1.0 - abs(price - ep) / max(ep, price, 1))
        except Exception:
            pass
        
        ims = hamming_sim(image_hash, ex.get("image_hash", "")) if image_hash else 0.0
        
        # Weighted combo: 55% title, 25% price, 20% image
        combo = ts * 0.55 + ps * 0.25 + ims * 0.20
        
        if combo >= 0.68:
            best = max(best, 24)
            reasons = ["très_proche_vente_rapide"]
        elif combo >= 0.50 and best < 16:
            best = 16
            reasons = ["proche_vente_rapide"]
        elif combo >= 0.36 and best < 8:
            best = 8
            reasons = ["profil_vendu_vite"]
    
    # User positive examples (lower priority)
    for ex in v8.get("positive_examples", [])[-120:]:
        if ex.get("category") and category and ex.get("category") != category:
            continue
        ts = token_similarity(title, ex.get("title", ""))
        if ts >= 0.62:
            best = max(best, 12)
            reasons = ["proche_exemple_positif"]
        elif ts >= 0.42:
            best = max(best, 6)
    
    # Negative learned examples (strong penalty)
    for ex in v8.get("negative_examples", [])[-120:]:
        ts = token_similarity(title, ex.get("title", ""))
        ims = hamming_sim(image_hash, ex.get("image_hash", "")) if image_hash else 0.0
        if max(ts, ims) >= 0.72:
            return -40, "proche_exemple_rejeté"
    
    return best, ",".join(reasons)


# ============================================================================
# Popularity Bonus
# ============================================================================

def popularity_bonus(
    favs: Optional[int],
    fav_delta: Optional[int],
    age_minutes: Optional[int],
) -> Tuple[int, str]:
    """
    Score bonus based on favorites and listing age.
    
    Returns: (bonus_score, reason)
    """
    score = 0
    reasons = []
    
    # Absolute favorite count
    if favs is not None:
        if favs >= 20:
            score += 14
            reasons.append(f"{favs}_favs")
        elif favs >= 10:
            score += 10
            reasons.append(f"{favs}_favs")
        elif favs >= 5:
            score += 6
            reasons.append(f"{favs}_favs")
        elif favs >= 2:
            score += 3
    
    # Favorite change since last check
    if fav_delta is not None:
        if fav_delta >= 8:
            score += 14
            reasons.append(f"+{fav_delta}_favs")
        elif fav_delta >= 4:
            score += 9
            reasons.append(f"+{fav_delta}_favs")
        elif fav_delta >= 2:
            score += 5
    
    # Listing age
    if age_minutes is not None:
        if age_minutes <= 2:
            score += 12
            reasons.append("≤2min")
        elif age_minutes <= 5:
            score += 9
            reasons.append("≤5min")
        elif age_minutes <= 10:
            score += 6
        elif age_minutes <= 20:
            score += 3
    
    return score, ",".join(reasons)


# ============================================================================
# Card Scoring (v8 fast pass)
# ============================================================================

def score_card(
    card: Dict[str, Any],
    search: Dict[str, Any],
    blacklist: Dict[str, Any],
    base: Dict[str, Any],
    previous_obs: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Score a single card listing. Returns None if price can't be parsed.
    
    Args:
        card: Card data with title, text, url
        search: Search config with category, rules
        blacklist: Blacklist dict
        base: Learning base (v8)
        previous_obs: Previous observation for favorites tracking
    
    Returns: Dict with score/reason or None
    """
    from src.utils import parse_price, extract_favourites, extract_age_minutes
    from src.filtering import hard_reject, infer_category_from_search, rule_match
    
    title = card.get("title", "")
    text = card.get("text", "")
    category = search.get("category", "")
    category = category or infer_category_from_search(search)
    
    price = parse_price(text)
    if price is None:
        return None
    
    # Quick hard rejection
    rejected, why = hard_reject(title, text, category, blacklist)
    if rejected:
        return {"score": 0, "price": price, "reject": why}
    
    # Price band scoring
    cap = search.get("price_to") or CATEGORY_DEFAULT_CAP.get(category, 30)
    pscore, preason = category_price_score(
        category, price, cap, search.get("name", ""), title
    )
    if pscore <= 0:
        return {"score": 0, "price": price, "reject": preason}
    
    # Base score: new listing starts high (44) + price score
    score = 44 + pscore
    reasons = ["nouvelle_annonce"]
    if preason:
        reasons.append(preason)
    
    # Extract metadata
    favs = extract_favourites(text)
    age = extract_age_minutes(text)
    prev_favs = None
    if previous_obs:
        try:
            prev_favs = previous_obs.get("favourites")
        except Exception:
            pass
    
    fav_delta = None
    if favs is not None and prev_favs is not None:
        fav_delta = favs - prev_favs
    
    # Popularity bonus
    pop_score, pop_reason = popularity_bonus(favs, fav_delta, age)
    score += pop_score
    if pop_reason:
        reasons.append(pop_reason)
    
    # Rule matching bonus (small, just for identity)
    title_n = norm(title)
    rule_bonus = 0
    matched_label = ""
    for rule in search.get("rules", []):
        must = rule.get("must_contain", [])
        anyc = rule.get("any_contain", [])
        if must and not all(present(title_n, x) for x in must):
            continue
        if anyc and not any(present(title_n, x) for x in anyc):
            continue
        rule_bonus = max(rule_bonus, 8)
        matched_label = rule.get("label", "")
        break
    score += rule_bonus
    if matched_label:
        reasons.append(f"reconnu:{matched_label[:28]}")
    
    return {
        "score": int(min(100, score)),
        "price": price,
        "favourites": favs,
        "age_minutes": age,
        "fav_delta": fav_delta,
        "reason": "; ".join(reasons[:5]),
        "matched_label": matched_label,
    }


# ============================================================================
# Opportunity Scoring (v6 legacy)
# ============================================================================

def opportunity_score(
    price: float,
    reference_price: Optional[float],
    margin_low: float,
    motivation_hits: List[str],
    authenticity_risk: bool = False,
    rare_condition: bool = False,
) -> int:
    """Legacy scoring for v6.8 compatibility."""
    if not reference_price or reference_price <= 0:
        return 1
    
    ratio = price / reference_price
    
    if ratio <= 0.12:
        score = 10
    elif ratio <= 0.15:
        score = 9
    elif ratio <= 0.20:
        score = 8
    elif ratio <= 0.25:
        score = 7
    elif ratio <= 0.30:
        score = 6
    elif ratio <= 0.40:
        score = 5
    else:
        score = 3
    
    if margin_low >= 100:
        score += 1
    elif margin_low >= 70:
        score += 0.5
    
    if motivation_hits:
        score += 1
    
    if authenticity_risk:
        score -= 1
    
    if rare_condition:
        score -= 1
    
    score = int(round(score))
    return max(1, min(10, score))
