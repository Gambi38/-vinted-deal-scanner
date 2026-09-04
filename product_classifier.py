"""Correction orthographique conservatrice pour les titres produits.

Le vocabulaire vient des règles actives. Aucune liste d'alias écrite à la main
n'est nécessaire. Les tokens courts et ceux contenant des chiffres ne sont
jamais corrigés afin d'éviter, par exemple, de transformer PS4 en PS5.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache


CLASSIFIER_SCHEMA = 4
TOKEN_RE = re.compile(r"[a-z0-9]+")


def _plain(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    # Une apostrophe au milieu d'un mot ne doit pas créer un token isolé :
    # « Assassin's Creed » doit rejoindre le terme « assassins creed ».
    text = text.replace("'", "").replace("’", "")
    return " ".join(TOKEN_RE.findall(text.lower()))


@lru_cache(maxsize=32768)
def levenshtein_distance(left: str, right: str, max_distance: int | None = None) -> int:
    """Distance de Levenshtein avec arrêt anticipé pour les titres en masse."""
    left, right = str(left), str(right)
    if left == right:
        return 0
    if len(left) > len(right):
        left, right = right, left
    if max_distance is not None and len(right) - len(left) > max_distance:
        return max_distance + 1
    previous = list(range(len(left) + 1))
    for row_index, right_char in enumerate(right, start=1):
        current = [row_index]
        row_min = row_index
        for column_index, left_char in enumerate(left, start=1):
            current.append(min(
                current[-1] + 1,
                previous[column_index] + 1,
                previous[column_index - 1] + (left_char != right_char),
            ))
            row_min = min(row_min, current[-1])
        if max_distance is not None and row_min > max_distance:
            return max_distance + 1
        previous = current
    return previous[-1]


def allowed_distance(token: str) -> int:
    token = _plain(token).replace(" ", "")
    if len(token) <= 4 or any(char.isdigit() for char in token):
        return 0
    if len(token) <= 8:
        return 1
    return 2


def _normalise_vocabulary(terms) -> tuple[str, ...]:
    values = {_plain(term) for term in terms if _plain(term)}
    return tuple(sorted(values))


def build_rule_vocabulary(rule_index, extra_terms=()) -> tuple[str, ...]:
    terms = list(extra_terms)
    for source, rule in rule_index:
        terms.extend((
            source.get("name", ""), source.get("query", ""),
            rule.get("brand", ""), rule.get("model", ""),
        ))
        for key in (
            "must_contain", "any_contain", "platform_any",
            "hardware_any", "title_prefix_any", "exclude", "identity_any",
        ):
            terms.extend(rule.get(key, []))
        for group in rule.get("match_groups", []):
            if isinstance(group, (list, tuple)):
                terms.extend(group)
            else:
                terms.append(group)
    return _normalise_vocabulary(terms)


def _unique_best(token: str, candidates: tuple[str, ...]):
    best_distance = None
    best = []
    for candidate in candidates:
        compact = candidate.replace(" ", "")
        limit = allowed_distance(compact)
        if not limit or abs(len(token) - len(compact)) > limit:
            continue
        distance = levenshtein_distance(token, compact, limit)
        if distance > limit:
            continue
        if best_distance is None or distance < best_distance:
            best_distance, best = distance, [candidate]
        elif distance == best_distance:
            best.append(candidate)
    return best[0] if best_distance is not None and len(best) == 1 else None


@lru_cache(maxsize=16384)
def _canonicalize_cached(value: str, vocabulary: tuple[str, ...]) -> str:
    tokens = _plain(value).split()
    if not tokens or not vocabulary:
        return " ".join(tokens)

    single_words = tuple(term for term in vocabulary if " " not in term)
    phrases = tuple(term for term in vocabulary if " " in term)
    output = []
    index = 0
    while index < len(tokens):
        # Corrige aussi « play station » -> « playstation » quand le vocabulaire
        # contient le mot joint, sans maintenir un alias spécial.
        if index + 1 < len(tokens):
            joined = tokens[index] + tokens[index + 1]
            joined_match = _unique_best(joined, single_words)
            if joined_match and levenshtein_distance(
                    joined, joined_match, allowed_distance(joined_match)) == 0:
                output.append(joined_match)
                index += 2
                continue

        token = tokens[index]
        if token in single_words:
            output.append(token)
            index += 1
            continue

        replacement = _unique_best(token, single_words + phrases)
        if replacement:
            output.extend(replacement.split())
        else:
            output.append(token)
        index += 1
    return " ".join(output)


def canonicalize_product_title(value: str, vocabulary=()) -> str:
    return _canonicalize_cached(str(value), _normalise_vocabulary(vocabulary))


def vocabulary_size(vocabulary) -> int:
    return len(_normalise_vocabulary(vocabulary))
