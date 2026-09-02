#!/usr/bin/env python3
"""Filtering and hard rejection logic for listings."""

from typing import Dict, List, Tuple, Any
from src.utils import norm, present, keyword_hits

# ============================================================================
# Blacklist Keywords
# ============================================================================

EMPTY_PACKAGING_WORDS = (
    "boite vide", "boîte vide", "empty box", "box only", "case only",
    "caja vacia", "caja vacía", "solo caja", "scatola vuota",
    "custodia vuota", "doosje", "lege doos", "verpakking",
)

ACCESSORY_WORDS = (
    "manette", "controller", "joycon", "joy-con", "dock", "station",
    "chargeur", "charger", "cable", "câble", "adaptateur", "adapter",
    "support", "stand", "soporte", "houder", "pochette", "housse",
    "case", "custodia", "coque", "shell", "écran", "screen", "batterie",
    "battery", "stylet", "stylus", "volant", "wheel", "hub", "vr",
)

MERCH_WORDS = (
    "figurine", "goodie", "poster", "affiche", "pin", "medallion",
    "medaille", "médaille", "porte cle", "porte-clé", "keychain",
    "steelbook", "plv", "display", "artbook", "coin", "piece collector",
)

LOW_VALUE_SPORT_WORDS = (
    "fifa", "pes ", "efootball", "nba 2k", "madden", "nhl ",
    "just dance", "singstar", "fitness", "karaoke",
)

MULTICART_WORDS = (
    "r4", "flashcard", "multicart", "multi cart", "multijeux",
    "multigame", "multijuegos",
)

PLATFORM_CONFLICTS = {
    "JEU_SWITCH": ("ps5", "ps4", "ps3", "ps2", "xbox", "3ds", "2ds", "wii", "vita", "psp"),
    "JEU_PS5": ("ps4", "ps3", "ps2", "xbox", "switch", "3ds", "vita", "psp"),
    "JEU_3DS": ("switch", "ps5", "ps4", "xbox", "vita", "psp"),
    "JEU_VITA": ("switch", "ps5", "ps4", "xbox", "3ds", "psp"),
}

SUSPICIOUS_POKEMON_GBA = (
    "neon", "chrome", "cristal de jade", "ambre fire red",
    "feu distorsion", "gaulois", "duo emeraude", "new emeraude",
    "new rubis shadow"
)

SOLD_WORDS = (
    "vendu", "vendue", "sold", "verkocht", "vendido", "vendida",
    "venduto", "venduta", "verkauft", "sprzedane", "sprzedany",
)

RESERVED_WORDS = (
    "réservé", "reserve", "reserved", "gereserveerd", "reserviert",
)


# ============================================================================
# Hard Rejection Function (v8 optimized)
# ============================================================================

def hard_reject(
    title: str,
    text: str,
    category: str,
    blacklist: Dict[str, Any],
) -> Tuple[bool, str]:
    """
    Quick hard rejection check. Returns (is_rejected, reason).
    Optimized for speed: checks most likely rejections first.
    """
    t = norm(f"{title} {text}")
    title_n = norm(title)
    
    # 1. User blacklist - only fast groups (avoid deep scan)
    if isinstance(blacklist, dict):
        for key in ("hard_blacklist", "fake_blacklist", "title_accessory_blacklist",
                    "suspicious_words", "low_value_game_blacklist"):
            vals = blacklist.get(key, [])
            if isinstance(vals, list):
                for w in vals:
                    if w and present(title_n if "title" in key else t, w):
                        return True, f"blacklist:{key}"
    
    # 2. Empty packaging - common false positive
    if any(present(title_n, w) for w in EMPTY_PACKAGING_WORDS):
        return True, "emballage_seul"
    
    # 3. Accessories - but allow loose cartridges
    loose_ok = any(present(title_n, x) for x in (
        "cartouche seule", "cartridge only", "jeu sans boite", "jeu sans boîte",
        "disque seul", "disc only", "loose cartridge"
    ))
    
    if not loose_ok and any(present(title_n, w) for w in ACCESSORY_WORDS):
        product_led = category == "CONSOLE" and any(
            present(title_n, x) for x in ("console", "ps5", "ps4", "xbox", "switch", "3ds", "2ds", "psp", "vita")
        )
        bundle = any(present(title_n, x) for x in ("avec", "with", "+", "bundle", "pack"))
        if not (product_led and bundle):
            return True, "accessoire"
    
    # 4. Merchandise for games
    if category.startswith("JEU_") and any(present(title_n, w) for w in MERCH_WORDS):
        return True, "produit_derive"
    
    # 5. Low-value sports games
    if any(present(t, w) for w in LOW_VALUE_SPORT_WORDS):
        return True, "jeu_faible_valeur"
    
    # 6. Multicart detection
    if any(present(t, w) for w in MULTICART_WORDS):
        return True, "multicart_r4"
    
    import re
    if re.search(r"\b\d{2,4}\s*(?:jeux|games|juegos|giochi|jogos)\b", t) and \
       any(present(t, x) for x in ("cartouche", "cartridge", "tarjeta", "card", "r4")):
        return True, "multicart_numerique"
    
    # 7. Platform conflicts (for category-specific products)
    conflicts = PLATFORM_CONFLICTS.get(category, ())
    if conflicts:
        expected = {
            "JEU_SWITCH": ("switch",),
            "JEU_PS5": ("ps5", "playstation 5"),
            "JEU_3DS": ("3ds", "2ds"),
            "JEU_VITA": ("vita",),
        }.get(category, ())
        bad = [x for x in conflicts if present(title_n, x)]
        good = [x for x in expected if present(title_n, x)]
        if bad and not good:
            return True, "plateforme_contradictoire"
    
    # 8. Suspicious custom Pokémon GBA
    if category == "JEU_RETRO" and "pokemon" in t:
        if any(present(t, x) for x in SUSPICIOUS_POKEMON_GBA):
            return True, "pokemon_custom_hack"
    
    return False, ""


# ============================================================================
# Sold/Reserved Detection
# ============================================================================

def is_sold(text: str) -> bool:
    """Check if listing is marked as sold."""
    return any(present(text, w) for w in SOLD_WORDS)


def is_reserved(text: str) -> bool:
    """Check if listing is marked as reserved."""
    return any(present(text, w) for w in RESERVED_WORDS)


# ============================================================================
# Category Inference
# ============================================================================

def infer_category_from_search(search: Dict[str, str]) -> str:
    """Infer category from search name/query if not explicit."""
    cat = str(search.get("category", "") or "")
    if cat:
        return cat
    
    nme = norm(f"{search.get('name', '')} {search.get('query', '')}")
    if "switch" in nme:
        return "JEU_SWITCH"
    if "ps5" in nme:
        return "JEU_PS5"
    if "3ds" in nme:
        return "JEU_3DS"
    if "vita" in nme:
        return "JEU_VITA"
    if "psp" in nme:
        return "JEU_PSP"
    return "ARTICLE"


# ============================================================================
# Exact Title Matching
# ============================================================================

def exact_title_matches(title: str, candidates: List[str], tolerant: bool = False) -> bool:
    """Check if title matches any candidate exactly (with optional tolerance)."""
    from src.utils import term_present_souple
    
    t = norm(title).strip(" -|:/")
    for candidat in candidates:
        c = norm(candidat).strip(" -|:/")
        if not c:
            continue
        if t == c:
            return True
        if tolerant and term_present_souple(t, c):
            if abs(len(t.split()) - len(c.split())) <= 1:
                return True
    return False


# ============================================================================
# Rule Matching
# ============================================================================

def rule_match(
    rule: Dict[str, Any],
    title: str,
    text: str,
    deep: bool = False,
) -> bool:
    """
    Check if listing matches a rule.
    
    Args:
        rule: Rule dict with must_contain, any_contain, etc.
        title: Listing title
        text: Listing description
        deep: If True, apply strict platform/hardware matching
    """
    from src.utils import present, term_present_souple
    
    title_n = norm(title)
    full_n = norm(f"{title} {text}")
    
    must = rule.get("must_contain", [])
    any_kw = rule.get("any_contain", [])
    hardware = rule.get("hardware_any", [])
    platform = rule.get("platform_any", [])
    exact_titles = rule.get("exact_title_any", [])
    excludes = rule.get("exclude", [])
    tolerant = bool(rule.get("tolerer_fautes", False))
    
    positif = term_present_souple if tolerant else present
    
    # Must contain all words
    if must and not all(positif(title_n, x) for x in must):
        return False
    
    # Must contain any of these words
    if any_kw and not any(positif(title_n, x) for x in any_kw):
        return False
    
    # Exclusions stay strict to avoid false positives
    if excludes and any(present(full_n, x) for x in excludes):
        return False
    
    if not deep:
        return True
    
    # Deep matching: stricter platform/hardware checks
    if platform and not any(positif(full_n, x) for x in platform):
        return False
    
    if hardware:
        hardware_text = title_n if rule.get("hardware_in_title") else full_n
        has_hardware = any(positif(hardware_text, x) for x in hardware)
        has_exact = (
            exact_title_matches(title, exact_titles, tolerant=tolerant)
            if exact_titles else False
        )
        if not has_hardware and not has_exact:
            return False
    
    return True
