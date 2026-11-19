"""Règles strictes : ce titre d'annonce est-il EXACTEMENT l'article cherché ?

Principe : on préfère rater une annonce douteuse que proposer un mauvais
article, mais sans refuser les annonces légitimes (« iPhone 11 avec boîte »,
« batterie 89 % », « sans chargeur »…). Les mots d'accessoire ou de défaut sont
donc jugés selon leur contexte : un « avec » ou un « sans » devant eux, ou leur
place dans le titre (« Batterie iPhone 11 » ≠ « iPhone 11 batterie 89 % »).
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from product_classifier import levenshtein_distance

BLACKLIST_PATH = Path(__file__).resolve().parent / "blacklist.json"
# Termes du blacklist.json trop ambigus pour un titre (« copie de la facture »).
IGNORED_BLACKLIST_TERMS = {"copie"}

# Variantes qui changent le produit, où qu'elles soient dans le titre.
STRICT_VARIANTS = {
    "pro", "max", "mini", "lite", "xl", "slim", "oled", "fold", "flip",
    "digital", "xs", "xr", "turbo",
}
# Variantes ambiguës : comptées seulement juste après un mot de la recherche
# (« iPhone 8 Plus »), pas dans une phrase (« plus aucune rayure »).
ADJACENT_VARIANTS = {"plus", "se", "air", "s", "x", "ultra", "classic", "mega", "neo", "duo"}
PROSE_AFTER = {
    "aucune", "aucun", "de", "d", "que", "rien", "jamais", "vende", "vend",
    "vendre", "puede", "envia", "envoie", "la", "le", "les", "un", "une", "ce",
    "cette", "en", "est", "a", "et", "ou", "pour", "hd", "hdr", "besoin",
    "tard", "petit", "grand", "bas", "cher", "rapide",
}

# Accessoires « forts » : refusés sauf s'ils accompagnent l'appareil (avec/+/sans).
ACCESSORY_WORDS = {
    "coque", "etui", "housse", "protection", "protege", "protecteur", "vitre",
    "verre", "film", "chargeur", "cable", "adaptateur", "case", "cover",
    "charger", "pochette", "sacoche", "tempered", "protector", "funda",
    "fundas", "capa", "capas", "custodia", "cargador", "hoesje", "hulle",
    "handyhulle", "kabel", "ladekabel", "folie", "glas", "coussinets", "plaque",
    "plaques", "faceplate",
}
ACCESSORY_STEMS = (
    "coque", "housse", "hulle", "custod", "funda", "schutz", "protect", "cover",
    "hoesje", "etui", "pouch", "sleeve", "vitre", "folie", "bumper", "tempered",
)
# Mots ambigus : accessoire seulement s'ils ouvrent le titre ou suivent « pour ».
WEAK_ACCESSORY_WORDS = {
    "ecran", "batterie", "facade", "notice", "dock", "support", "stand", "skin",
    "arriere",
}
FOR_WORDS = {"pour", "for", "voor", "fur", "para", "per", "compatible",
             "compatibles", "kompatibel"}

# Autres produits (manette, jeu, casque…) quand on cherche une console, etc.
PRODUCT_TYPE_WORDS = {
    "manette", "manettes", "controller", "controllers", "gamepad", "jeu", "jeux",
    "game", "games", "casque", "joycon", "joy", "volant", "sticker",
}
CROSS_PRODUCT_WORDS = {"airpods", "ipad", "macbook", "imac", "airtag", "airpod"}
CONSOLE_TOKENS = {"ps", "playstation", "xbox", "switch", "wii", "wiiu", "psp"}
GAME_TITLE_WORDS = {
    "fifa", "fc", "spider", "spiderman", "mario", "zelda", "pokemon", "duty",
    "gta", "minecraft", "fortnite", "creed", "horizon", "uncharted", "forza",
    "gran", "turismo", "elden", "ring", "hogwarts", "legacy", "kart",
}
# Doubles / packs : « Épée + Bouclier » n'est pas « Épée ».
SIBLING_GROUPS = ({"epee", "bouclier"}, {"sword", "shield"}, {"ecarlate", "violet"})

# Boîte vide / maquette / pièces / verrouillé (toutes langues).
EMPTY_OR_BROKEN_WORDS = {
    "vide", "vides", "leere", "leer", "lege", "empty", "vacia", "vacio",
    "vuota", "vuoto", "dummy", "factice", "maquette", "imitation", "attrappe",
    "piezas", "pezzi", "ricambi", "teile", "ersatzteile", "onderdelen",
    "defecto", "rotto", "kaputt", "kapot", "defekt", "roto", "pecas",
    "concertar", "despiece", "despieces", "reparar", "reparacion", "reparation",
    "reparatie", "reparatur", "bastler", "bastelgerat", "blackliste",
    "blacklisted", "locked", "bloque", "bloquee", "verrouille", "verrouillee",
    "icloud", "mdm", "simlock", "fissure", "fissuree", "fissures", "probleme",
    "problemes", "rota", "quebrada", "geblokkeerd", "gebarsten", "seul", "seule",
    "alleen", "gauche", "droite", "hs", "ruckseite", "rueckseite", "achterkant",
    "backcover", "trasera",
}
BROKEN_PHRASES = (
    "non fonctionnel", "non fonctionnelle", "pas fonctionnel", "pour piece",
    "pour pieces", "nicht funktionsfahig", "niet werkend", "no funciona",
    "sin funcionar", "no funcional", "for repair", "to repair", "ne sallume pas",
    "face id hs", "touch id hs",
)
PACKAGING_WORDS = {
    "boite", "box", "doos", "verpackung", "originalverpackung", "emballage",
    "karton", "carton", "caja", "scatola", "coffret",
}
PACKAGING_FOLLOWERS = {"origine", "dorigine", "original", "complet", "complete",
                       "incluse", "inclus", "included"}

WITH_WORDS = {
    "avec", "with", "mit", "met", "con", "inclus", "incl", "incluse", "et",
    "and", "und", "en", "plus", "sans", "dans", "sa", "son", "offert",
    "offerte", "bonus", "gratuit", "ohne", "sin", "inc", "inkl", "inklusive",
    "inclusief", "incluye", "incluido", "y", "dazu",
}
NEGATORS = {"sans", "aucun", "aucune", "pas", "jamais", "zero", "geen", "ohne",
            "sin", "no", "libre", "non", "debloque", "debloquee", "unlocked",
            "sbloccato", "desbloqueado", "entsperrt", "ontgrendeld", "deverrouille"}

NOISE_ANYWHERE = {"recherche", "cherche", "wanted", "echange", "echanger", "troc"}
NOISE_AT_START = {"vendu", "sold", "reserve", "reserved"}
LOT_PHRASES = ("lot", "bundle", "pack de", "x2", "x3", "x4", "2x", "3x", "4x")

CAPACITIES = {"16", "32", "64", "128", "256", "512"}
UNIT_WORDS = {
    "go", "gb", "gig", "to", "tb", "mo", "mb", "g", "gen", "inch", "pouces",
    "pouce", "cm", "mm", "w", "v", "mah", "hz", "k", "ds", "manette",
    "manettes", "joy", "joycon", "joueurs", "joueur", "jeux", "jeu",
    "controller", "controllers", "pcs", "pieces", "accessoires", "games",
}


def plain(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("'", "").replace("’", "").replace("+", " et ").replace("&", " et ")
    # Lettres et chiffres collés sont séparés : « iPhone11 » = « iPhone 11 ».
    return " ".join(re.findall(r"[a-z]+|[0-9]+", text.lower()))


def load_bad_terms(path: Path = BLACKLIST_PATH) -> list[str]:
    """Termes 'cassé / faux' du blacklist.json du projet."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    terms: list[str] = []
    for key in ("hard_blacklist", "fake_blacklist"):
        terms.extend(str(t) for t in data.get(key, []) if str(t).strip())
    return [t for t in terms if plain(t) not in IGNORED_BLACKLIST_TERMS]


def _token_present(token, title_tokens) -> bool:
    """Mot exact, ou à une faute de frappe près pour les mots d'au moins 5 lettres."""
    if token in title_tokens:
        return True
    if token.isdigit() and any(                      # « iPhone 1164gb » = 11 + 64 Go
            t.isdigit() and t.startswith(token) and t[len(token):] in CAPACITIES
            for t in title_tokens):
        return True
    if len(token) < 5 or not token.isalpha():
        return False
    return any(
        t.isalpha() and abs(len(t) - len(token)) <= 1
        and levenshtein_distance(token, t, 1) <= 1
        for t in title_tokens
    )


def _covered(tokens, index, words, window=3) -> bool:
    return bool(words.intersection(tokens[max(0, index - window):index]))


def _find_phrase(tokens, phrase) -> int:
    words = plain(phrase).split()
    if not words:
        return -1
    for i in range(len(tokens) - len(words) + 1):
        if tokens[i:i + len(words)] == words:
            return i
    return -1


def _small_number_after(tokens, index):
    """Numéro de 1-2 chiffres juste après tokens[index], hors capacité/unité."""
    if index + 1 >= len(tokens):
        return None
    nxt = tokens[index + 1]
    if not nxt.isdigit() or len(nxt) > 2:
        return None
    if index + 2 < len(tokens) and tokens[index + 2] in UNIT_WORDS:
        return None
    return nxt


def _other_model_number(q_tokens, t_tokens):
    """« iPhone 11 » n'accepte pas « iPhone 12 » ; « Switch » pas « Switch 2 »."""
    for position, tok in enumerate(q_tokens):
        if tok.isdigit() and position > 0:
            anchor = q_tokens[position - 1]
            for j, title_tok in enumerate(t_tokens):
                if _token_present(anchor, [title_tok]):
                    found = _small_number_after(t_tokens, j)
                    if found and found != tok:
                        return found
    last = q_tokens[-1]
    if not last.isdigit():
        for j, title_tok in enumerate(t_tokens):
            if title_tok == last:
                found = _small_number_after(t_tokens, j)
                if found:
                    return found
    return None


def rejection_reason(title: str, query: str, bad_terms=(), allow_lots=False):
    """None si le titre correspond exactement à la recherche, sinon la raison."""
    t, q = plain(title), plain(query)
    q_tokens, t_tokens = q.split(), t.split()
    if not q_tokens:
        return "recherche vide"
    missing = [tok for tok in q_tokens if not _token_present(tok, t_tokens)]
    if missing:
        return "mot absent : " + missing[0]
    q_set = set(q_tokens)
    anchor = next((i for i, tok in enumerate(t_tokens) if tok in q_set), 0)

    for i, tok in enumerate(t_tokens):
        if tok in q_set:
            continue
        if tok in STRICT_VARIANTS or (
                len(tok) >= 5 and re.fullmatch(r"(?:pro|max|plus|mini|ultra|lite){2,}", tok)):
            return "autre variante : " + tok
        if tok in ADJACENT_VARIANTS and i > 0 and t_tokens[i - 1] in q_set:
            nxt = t_tokens[i + 1] if i + 1 < len(t_tokens) else ""
            if nxt not in PROSE_AFTER:
                return "autre variante : " + tok

    other = _other_model_number(q_tokens, t_tokens)
    if other:
        return "autre numéro de modèle : " + other

    for group in SIBLING_GROUPS:
        if len(group.intersection(t_tokens)) >= 2 and len(group.intersection(q_set)) < 2:
            return "pack de plusieurs versions : " + "/".join(sorted(group))
    for i, tok in enumerate(t_tokens):
        if tok in CROSS_PRODUCT_WORDS and tok not in q_set:
            return "autre produit : " + tok

    for i, tok in enumerate(t_tokens):
        if tok in q_set:
            continue
        if tok in PRODUCT_TYPE_WORDS and not _covered(t_tokens, i, WITH_WORDS):
            return "autre produit : " + tok
        if tok in ACCESSORY_WORDS and not _covered(t_tokens, i, WITH_WORDS):
            return "accessoire : " + tok
        if tok in WEAK_ACCESSORY_WORDS and not _covered(t_tokens, i, WITH_WORDS):
            if i < anchor or _covered(t_tokens, i, FOR_WORDS, 2):
                return "accessoire : " + tok
        if tok in PACKAGING_WORDS and not _covered(t_tokens, i, WITH_WORDS):
            after = t_tokens[i + 1:i + 3]
            listed_with_phone = i > anchor and bool({"y", "et", "and", "und", "en"}.intersection(after))
            if not PACKAGING_FOLLOWERS.intersection(after) and not listed_with_phone:
                return "emballage seul : " + tok
    for stem in ACCESSORY_STEMS:
        if stem in q:
            continue
        for i, tok in enumerate(t_tokens):
            if (tok.startswith(stem) or tok.endswith(stem)) \
                    and not _covered(t_tokens, i, WITH_WORDS):
                return "accessoire : " + tok
    if CONSOLE_TOKENS.intersection(q_set) and "console" not in t_tokens:
        hit = next((tok for tok in t_tokens
                    if tok in GAME_TITLE_WORDS and tok not in q_set), None)
        if hit:
            return "jeu (pas la console) : " + hit

    for i, tok in enumerate(t_tokens):
        if tok in EMPTY_OR_BROKEN_WORDS and tok not in q_set \
                and not _covered(t_tokens, i, NEGATORS):
            return "vide / pièces / défaut : " + tok
    for term in (*BROKEN_PHRASES, *bad_terms):
        at = _find_phrase(t_tokens, term)
        if at >= 0 and _find_phrase(q_tokens, term) < 0 \
                and not _covered(t_tokens, at, NEGATORS):
            return "défaut / faux : " + term
    for i, tok in enumerate(t_tokens):
        if tok in NOISE_ANYWHERE and tok not in q_set:
            return "pas une vente directe : " + tok
    if t_tokens and t_tokens[0] in NOISE_AT_START and t_tokens[0] not in q_set:
        return "pas une vente directe : " + t_tokens[0]
    if not allow_lots:
        for phrase in LOT_PHRASES:
            if _find_phrase(t_tokens, phrase) >= 0 and phrase not in q_set:
                return "lot : " + phrase
    return None


def model_mismatch(model, query) -> bool:
    """Le champ « Modèle » de Vinted désigne un modèle plus précis (« iPhone 11 Pro »)."""
    if not model:
        return False
    m, q = set(plain(model).split()), set(plain(query).split())
    return q <= m and bool(m - q)
