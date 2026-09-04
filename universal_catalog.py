#!/usr/bin/env python3
"""Catalogue extensible pour smartphones, tablettes, ordinateurs et outils.

Le fichier CSV est normalisé une fois avant le scan. Le catalogue obtenu peut
contenir plusieurs milliers de références : un index inversé évite une boucle
complète pour chaque annonce Vinted. Un modèle/alias distinctif est toujours
requis, et les accessoires seuls ne peuvent pas hériter du prix de l'appareil.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import time
import unicodedata
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path


SCHEMA = 1
DEFAULT_OUTPUT = Path("runtime_data/device_catalog.json")
TOKEN_RE = re.compile(r"[a-z0-9]+")
SUPPORTED_TYPES = {
    "SMARTPHONE", "TABLET", "LAPTOP", "DESKTOP", "COMPUTER",
    "TOOL", "CAMERA", "AUDIO", "SMARTWATCH", "EREADER",
    "STREAMING", "MINI_PC", "DRAWING_TABLET", "ELECTRONICS",
}
NOISE = {
    "apple", "samsung", "google", "lenovo", "dell", "hp", "microsoft",
    "makita", "bosch", "dewalt", "milwaukee", "festool", "karcher",
    "ordinateur", "portable", "smartphone", "tablette", "outil", "the",
    "and", "avec", "pour", "pro", "plus", "max", "mini",
}
ACCESSORY_STARTS = (
    "coque", "housse", "etui", "étui", "case", "cover", "protection",
    "ecran", "écran", "screen", "vitre", "verre trempé", "verre trempe",
    "vitre de protection", "film de protection", "film hydrogel",
    "protecteur écran", "protecteur ecran", "tempered glass", "screen protector",
    "screen guard", "privacy glass", "hydrogel film", "phone case",
    "iphone case", "silicone case", "clear case", "bumper case",
    "cristal templado", "vidrio templado", "protector de pantalla",
    "vetro temperato", "pellicola protettiva", "handyhülle", "handyhulle",
    "panzerglas", "displayschutz", "schutzglas", "telefoonhoesje",
    "screenprotector", "beschermglas", "película de vidro",
    "pelicula de vidro", "vidro temperado", "protetor de tela",
    "szkło hartowane", "szklo hartowane", "folia ochronna",
    "funda", "carcasa", "custodia", "capa", "hoesje", "schutzhülle",
    "schutzhulle", "batterie", "battery", "chargeur",
    "charger", "cable", "câble", "clavier", "keyboard", "stylet", "pencil",
    "bracelet", "strap", "telecommande", "télécommande", "remote",
    "objectif", "lens", "sacoche", "support", "dock", "adaptateur",
    "coussinet", "coussinets", "earpad", "earpads", "ear pad", "ear pads",
    "arceau", "headband", "embout", "embouts", "ear tip", "ear tips",
    "charnière", "charniere", "hinge", "nappe", "flex cable", "châssis",
    "chassis", "dos", "back glass", "vitre arrière", "vitre arriere",
)
ACCESSORY_ONLY = (
    "seul", "seule", "only", "pour iphone", "pour ipad", "pour macbook",
    "compatible avec", "replacement", "piece detachee", "pièce détachée",
    "sans appareil", "sans telephone", "sans téléphone", "sans outil",
    "pièces uniquement", "pieces uniquement", "spare part", "spare parts",
)
INCLUDED = (
    "avec chargeur", "chargeur inclus", "with charger",
    "avec batterie", "batterie incluse", "with battery",
    "avec clavier", "clavier inclus", "with keyboard",
    "avec stylet", "stylet inclus", "with pencil",
    "avec objectif", "objectif inclus", "with lens",
    "avec housse", "avec coque", "with case",
)


def normalise(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("'", "").replace("’", "")
    return " ".join(TOKEN_RE.findall(text.lower()))


def _contains(text: str, phrase: str) -> bool:
    return f" {normalise(phrase)} " in f" {normalise(text)} "


def _split(value: object) -> list[str]:
    return list(dict.fromkeys(
        part.strip() for part in str(value or "").split("|") if part.strip()
    ))


def _number(value: object) -> float | None:
    try:
        number = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    return round(number, 2) if number > 0 else None


def _integer(value: object, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def convert_row(row: dict, *, min_demand: int = 4, source: str = "CSV appareils"):
    name = str(row.get("name", "")).strip()
    product_type = str(row.get("product_type", "")).strip().upper()
    category = str(row.get("category") or product_type).strip().upper()
    brand = str(row.get("brand", "")).strip()
    aliases = _split(row.get("aliases"))
    query = str(row.get("query", "")).strip()
    price_max = _number(row.get("price_max"))
    resale_low = _number(row.get("resale_low"))
    resale_high = _number(row.get("resale_high")) or resale_low
    demand = _integer(row.get("demand_score"), 0)
    sales_volume = _integer(row.get("sales_volume"), 0)
    if not aliases and name:
        aliases = [name]
    if (not name or product_type not in SUPPORTED_TYPES or not query
            or price_max is None or resale_low is None or resale_low <= price_max
            or demand < int(min_demand)):
        return None
    # Une identité générique telle que « Apple » ou « tablette » est interdite.
    distinctive = [
        alias for alias in aliases
        if any(char.isdigit() for char in normalise(alias))
        or len([token for token in normalise(alias).split() if token not in NOISE]) >= 2
    ]
    if not distinctive:
        return None
    return {
        "name": name,
        "product_type": product_type,
        "category": category,
        "brand": brand,
        "aliases": distinctive,
        "query": query,
        "price_max": price_max,
        "hot_buy": _number(row.get("hot_buy")) or round(price_max * 0.80, 2),
        "resale_low": resale_low,
        "resale_high": resale_high,
        "demand_score": min(5, demand),
        "sales_volume": max(0, sales_volume),
        "description": str(row.get("description", "")).strip(),
        "exclude": _split(row.get("exclude")),
        "source": str(row.get("source") or source),
    }


def import_csv(stream, *, min_demand: int = 4, source: str = "CSV appareils") -> list[dict]:
    if isinstance(stream, (bytes, bytearray)):
        stream = io.StringIO(bytes(stream).decode("utf-8-sig", errors="replace"))
    rows = {}
    for raw in csv.DictReader(stream):
        converted = convert_row(raw, min_demand=min_demand, source=source)
        if converted is None:
            continue
        identity = (converted["product_type"], normalise(converted["name"]))
        rows[identity] = converted
    return list(rows.values())


def _download(url: str, timeout: float = 30.0) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "VintedDealScanner/1.0", "Accept": "text/csv"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _source_fingerprint(paths, source_url: str, min_demand: int) -> str:
    digest = hashlib.sha256()
    digest.update(f"schema={SCHEMA};min_demand={int(min_demand)}".encode())
    for path in sorted((Path(value) for value in paths), key=lambda value: str(value)):
        digest.update(str(path).encode("utf-8", errors="replace"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
    digest.update(str(source_url or "").encode("utf-8", errors="replace"))
    return digest.hexdigest()


def _fresh(path: Path, max_age_hours: float, source_fingerprint: str = "") -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (data.get("schema") == SCHEMA
                and (not source_fingerprint
                     or data.get("source_fingerprint") == source_fingerprint)
                and time.time() - float(data.get("imported_at", 0))
                <= max(0.0, max_age_hours) * 3600)
    except (OSError, ValueError, TypeError):
        return False


def save_catalog(path: Path, references: list[dict], source_fingerprint: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({
        "schema": SCHEMA,
        "imported_at": time.time(),
        "currency": "EUR",
        "source_fingerprint": source_fingerprint,
        "reference_count": len(references),
        "references": references,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


class DeviceCatalog:
    def __init__(self, references=()):
        self.references = [row for row in references if isinstance(row, dict)]
        self._index = defaultdict(set)
        frequency = Counter()
        for row in self.references:
            tokens = set()
            for alias in row.get("aliases", []):
                tokens.update(normalise(alias).split())
            frequency.update(token for token in tokens if len(token) >= 2)
        for index, row in enumerate(self.references):
            tokens = set()
            for alias in row.get("aliases", []):
                tokens.update(normalise(alias).split())
            anchors = sorted(
                (token for token in tokens if len(token) >= 2),
                key=lambda token: (frequency[token], -len(token), token),
            )[:4]
            for token in anchors:
                self._index[token].add(index)

    @classmethod
    def load(cls, path: Path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return cls()
        if data.get("schema") != SCHEMA or not isinstance(data.get("references"), list):
            return cls()
        return cls(data["references"])

    def __len__(self):
        return len(self.references)

    @staticmethod
    def _accessory_only(title: str) -> bool:
        title_n = normalise(title)
        accessory_hit = any(_contains(title_n, term) for term in ACCESSORY_STARTS)
        included = any(_contains(title_n, term) for term in INCLUDED)
        explicit_only = any(_contains(title_n, term) for term in ACCESSORY_ONLY)
        return (accessory_hit or explicit_only) and not included

    def match(self, title: str, price: float):
        title_n = normalise(title)
        if self._accessory_only(title_n):
            return None, None
        candidates = set()
        for token in set(title_n.split()):
            candidates.update(self._index.get(token, ()))
        matches = []
        for index in candidates:
            row = self.references[index]
            if float(price) > float(row.get("price_max", 0)):
                continue
            if any(_contains(title_n, term) for term in row.get("exclude", [])):
                continue
            alias = max(
                (value for value in row.get("aliases", []) if _contains(title_n, value)),
                key=lambda value: len(normalise(value)), default=None,
            )
            if not alias:
                continue
            matches.append((
                len(normalise(alias)), int(row.get("demand_score", 0)),
                float(row.get("resale_low", 0)), row,
            ))
        if not matches:
            return None, None
        row = max(matches, key=lambda item: item[:3])[3]
        rule = {
            "label": row["name"], "model": row["name"], "brand": row.get("brand", ""),
            "description": row.get("description", ""),
            "product_type": row["product_type"],
            "identity_any": list(row.get("aliases", [])),
            "match_groups": [list(row.get("aliases", []))],
            "match_threshold": 0.95, "min_match_groups": 1,
            "exclude": list(row.get("exclude", [])),
            "resale_low": float(row["resale_low"]),
            "resale_high": float(row.get("resale_high", row["resale_low"])),
            "min_margin": 8, "min_roi_pct": 20,
            "demand_score": int(row.get("demand_score", 4)),
            "profile_priority": 90, "hot_buy_price": float(row.get("hot_buy", 0)),
            "external_source": row.get("source", "catalogue appareils"),
            "sales_volume": int(row.get("sales_volume", 0)),
        }
        search = {
            "name": f"APPAREILS - {row['category']}", "category": row["category"],
            "product_type": row["product_type"], "query": row["query"],
            "price_to": float(row["price_max"]), "rules": [rule],
        }
        return search, rule

    def discovery_searches(self, max_price: float = 800.0) -> list[dict]:
        by_query = {}
        for row in self.references:
            query = str(row.get("query", "")).strip()
            if query:
                by_query[query] = max(
                    by_query.get(query, 0.0),
                    min(float(row.get("price_max", 0)), float(max_price)),
                )
        return [
            {"name": f"APPAREILS - {query}", "category": "DISCOVERY",
             "query": query, "price_to": round(price, 2)}
            for query, price in sorted(by_query.items()) if price > 0
        ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Construit le catalogue appareils")
    parser.add_argument("--input", type=Path, action="append", default=[])
    parser.add_argument("--url")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-age-hours", type=float, default=24)
    parser.add_argument("--min-demand", type=int, default=4)
    parser.add_argument("--allow-missing", action="store_true")
    args = parser.parse_args(argv)
    source_url = (args.url or os.getenv("DEVICE_CATALOG_CSV_URL", "")).strip()
    source_fingerprint = _source_fingerprint(
        args.input, source_url, args.min_demand,
    )
    if _fresh(args.output, args.max_age_hours, source_fingerprint):
        print(f"Appareils: cache récent conservé ({args.output})")
        return 0
    references = {}
    try:
        for path in args.input:
            for row in import_csv(path.read_bytes(), min_demand=args.min_demand,
                                  source=f"catalogue contrôlé {path.name}"):
                references[(row["product_type"], normalise(row["name"]))] = row
        if source_url:
            for row in import_csv(_download(source_url), min_demand=args.min_demand,
                                  source="catalogue appareils externe"):
                references[(row["product_type"], normalise(row["name"]))] = row
    except (OSError, ValueError) as exc:
        print(f"Appareils: import impossible: {exc}")
        if not args.allow_missing:
            return 1
    if not references:
        if args.allow_missing:
            print("Appareils: aucune référence, ancien cache conservé")
            return 0
        parser.error("aucune référence valide")
    save_catalog(
        args.output, list(references.values()),
        source_fingerprint=source_fingerprint,
    )
    print(f"Appareils: {len(references)} références indexées")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
