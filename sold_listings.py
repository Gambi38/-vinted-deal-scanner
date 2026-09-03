"""Adaptateur local pour données de ventes réalisées provenant d'une source autorisée.

Le scanner ne contourne aucune authentification. Un autre processus peut
alimenter sold_listings_cache.json avec des données auxquelles l'utilisateur a
légalement accès. Les valeurs ne remplacent jamais la revente prudente : elles
servent uniquement de référence de classement si assez d'échantillons récents
sont présents.
"""

from __future__ import annotations

import json
import statistics
import time
import unicodedata
from pathlib import Path


def _key(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(c for c in text if not unicodedata.combining(c)).lower().strip()


class SoldListingsProvider:
    def __init__(self, path: Path, max_age_days: int = 30, min_samples: int = 3):
        self.path = path
        self.max_age_days = max(1, int(max_age_days))
        self.min_samples = max(2, int(min_samples))
        self.products = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        products = data.get("products", {}) if isinstance(data, dict) else {}
        return products if isinstance(products, dict) else {}

    def reference(self, model: str, now: float | None = None):
        raw = self.products.get(model) or self.products.get(_key(model))
        if not isinstance(raw, dict):
            return None
        cutoff = float(now if now is not None else time.time()) - self.max_age_days * 86400
        prices = []
        for sample in raw.get("samples", []):
            if not isinstance(sample, dict):
                continue
            try:
                price = float(sample.get("price"))
                sold_at = float(sample.get("sold_at", 0))
            except (TypeError, ValueError):
                continue
            if price > 0 and sold_at >= cutoff:
                prices.append(price)
        if len(prices) < self.min_samples:
            return None
        return {
            "median": round(statistics.median(prices), 2),
            "samples": len(prices),
            "source": str(raw.get("source", "cache local")),
        }

    def enrich(self, searches: list) -> int:
        enriched = 0
        for search in searches:
            for rule in search.get("rules", []):
                reference = self.reference(rule.get("model", ""))
                if not reference:
                    continue
                # La médiane externe ne gonfle jamais la référence prudente.
                rule["market_avg"] = min(
                    float(rule.get("resale_low", reference["median"])),
                    reference["median"],
                )
                rule["sold_samples"] = reference["samples"]
                rule["sold_source"] = reference["source"]
                enriched += 1
        return enriched

