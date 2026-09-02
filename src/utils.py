#!/usr/bin/env python3
"""Shared utilities for Vinted scanner."""

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ============================================================================
# Text Processing
# ============================================================================

def norm(s: str) -> str:
    """Normalize text: remove accents, lowercase, collapse spaces."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def present(text: str, term: str) -> bool:
    """Check if term is present as a whole word in text."""
    t, q = norm(text), norm(term)
    if not q:
        return False
    if re.fullmatch(r"[a-z0-9]{1,3}", q):
        return re.search(rf"(?<![a-z0-9]){re.escape(q)}(?![a-z0-9])", t) is not None
    return q in t


def term_present_souple(text: str, term: str) -> bool:
    """Lenient word matching with typo tolerance (1-2 letters difference)."""
    if present(text, term):
        return True
    
    texte = re.findall(r"[a-z0-9]+", norm(text))
    cible = re.findall(r"[a-z0-9]+", norm(term))
    
    if not texte or not cible or len(cible) > len(texte):
        return False
    
    taille = len(cible)
    for i in range(len(texte) - taille + 1):
        bloc = texte[i:i + taille]
        if all(_mots_similaires(x, y) for x, y in zip(bloc, cible)):
            return True
    
    return False


def _mots_similaires(a: str, b: str, limit: int = 2) -> bool:
    """Levenshtein distance-based typo tolerance."""
    a = norm(a)
    b = norm(b)
    
    if a == b:
        return True
    if len(a) < 4 or len(b) < 4:
        return False
    if any(ch.isdigit() for ch in a + b):
        return False
    
    limite = 2 if max(len(a), len(b)) >= 8 else 1
    if abs(len(a) - len(b)) > limite:
        return False
    
    precedent = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        courant = [i]
        minimum_ligne = i
        for j, cb in enumerate(b, 1):
            cout = 0 if ca == cb else 1
            valeur = min(
                courant[j - 1] + 1,
                precedent[j] + 1,
                precedent[j - 1] + cout,
            )
            courant.append(valeur)
            minimum_ligne = min(minimum_ligne, valeur)
        if minimum_ligne > limite:
            return False
        precedent = courant
    
    return precedent[-1] <= limite


def keyword_hits(text: str, words: List[str]) -> List[str]:
    """Find which keywords from list are present in text."""
    return [w for w in words if present(text, w)]


# ============================================================================
# Price Parsing
# ============================================================================

PRICE_RE = re.compile(r"(?<!\d)(\d{1,4}(?:[.,]\d{1,2})?)\s*(?:€|EUR)\b", re.I)


def parse_price(text: str) -> Optional[float]:
    """Extract lowest price from text."""
    vals = []
    for m in PRICE_RE.finditer(str(text or "")):
        try:
            v = float(m.group(1).replace(",", "."))
            if 0.25 <= v <= 5000:
                vals.append(v)
        except ValueError:
            pass
    return vals[0] if vals else None


# ============================================================================
# JSON File Handling
# ============================================================================

def load_json(path: Path, default: Any = None) -> Any:
    """Load JSON file safely with fallback."""
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    """Save JSON with atomic write (tmp file then move)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ============================================================================
# Feature Extraction
# ============================================================================

def extract_favourites(text: str) -> Optional[int]:
    """Extract favorite count from text."""
    raw = norm(text)
    patterns = (
        r"(\d+)\s*(?:favoris|favourites|favorites|favorieten|favoritos|preferiti)\b",
        r"(?:favoris|favourites|favorites|favorieten|favoritos|preferiti)\s*[:\-]?\s*(\d+)",
    )
    for p in patterns:
        m = re.search(p, raw)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass
    return None


def extract_age_minutes(text: str) -> Optional[int]:
    """Extract listing age in minutes from text."""
    raw = norm(text)
    m = re.search(
        r"(?:il y a|vor|ago|hace|fa)\s*(\d+)\s*(?:min|minute|minutes|minuten|minuti)",
        raw,
    )
    if m:
        return int(m.group(1))
    if re.search(r"(?:a l'instant|à l'instant|just now|gerade eben|appena pubblicato)", raw):
        return 0
    m = re.search(
        r"(?:il y a|vor|ago|hace|fa)\s*(\d+)\s*(?:h|heure|heures|hour|hours|stunden|ore)",
        raw,
    )
    if m:
        return int(m.group(1)) * 60
    return None


def extract_size(text: str) -> str:
    """Extract clothing size from text."""
    raw = text or ""
    
    patterns = [
        r"\bW\s?(\d{2})\s*[xX/ -]?\s*L\s?(\d{2})\b",
        r"\b(?:EU|EUR|taille|size)\s*[:\-]?\s*(3[4-9]|4[0-9]|5[0-2])\b",
        r"\b(3[4-9]|4[0-9]|5[0-2])\s*(?:EU|EUR)\b",
        r"\b(XXS|XS|S|M|L|XL|XXL|XXXL)\b",
    ]
    
    m = re.search(patterns[0], raw, flags=re.I)
    if m:
        return f"W{m.group(1)} L{m.group(2)}"
    
    for p in patterns[1:]:
        m = re.search(p, raw, flags=re.I)
        if m:
            return m.group(1).upper()
    
    return "?"


# ============================================================================
# Percentile Calculation (for learning)
# ============================================================================

def percentile_simple(valeurs: List[float], fraction: float) -> Optional[float]:
    """Calculate percentile using linear interpolation."""
    valeurs = sorted(float(x) for x in valeurs)
    if not valeurs:
        return None
    
    if len(valeurs) == 1:
        return valeurs[0]
    
    position = (len(valeurs) - 1) * float(fraction)
    bas = int(position)
    haut = min(bas + 1, len(valeurs) - 1)
    poids = position - bas
    
    return valeurs[bas] * (1 - poids) + valeurs[haut] * poids


# ============================================================================
# Title Processing
# ============================================================================

def clean_title(value: str) -> str:
    """Clean and validate title."""
    value = re.sub(
        r"^(image|photo)\s+(de|of)\s+",
        "",
        (value or "").strip(),
        flags=re.I,
    )
    value = re.sub(r"\s+", " ", value)
    if not 3 <= len(value) <= 220 or "€" in value:
        return ""
    return value


def title_from_card(card: Dict[str, str]) -> str:
    """Extract best title from card data."""
    for key in ("img_alt", "aria_label", "anchor_title", "anchor_text"):
        value = clean_title(card.get(key, ""))
        if value:
            return value
    
    # Fallback to slug parsing
    SLUG_RE = re.compile(r"/items/\d+-([^/?#]+)")
    m = SLUG_RE.search(card.get("href", ""))
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1)).replace("-", " ").replace("_", " ")
    
    return "Annonce Vinted"


# ============================================================================
# Token Similarity (for image/title matching)
# ============================================================================

def title_tokens(text: str) -> set:
    """Extract meaningful tokens from title."""
    stop = {
        "nintendo", "playstation", "switch", "console", "jeu", "game", "games",
        "edition", "version", "original", "originale", "avec", "pour", "the", "of",
        "and", "vinted", "complet", "complete", "neuf", "neuve", "bon", "etat",
    }
    return {
        x for x in re.findall(r"[a-z0-9]{3,}", norm(text))
        if x not in stop
    }


def token_similarity(a: str, b: str) -> float:
    """Calculate Jaccard similarity between titles."""
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, len(ta | tb))


# ============================================================================
# Hamming Distance (for image hashing)
# ============================================================================

def hamming_sim(a: str, b: str) -> float:
    """Calculate Hamming similarity between hex hashes."""
    if not a or not b:
        return 0.0
    try:
        x = int(a, 16) ^ int(b, 16)
        d = x.bit_count()
        return 1.0 - d / max(1, len(a) * 4)
    except Exception:
        return 0.0
