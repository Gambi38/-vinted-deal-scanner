#!/usr/bin/env python3
"""Import et recherche locale du catalogue officiel PriceCharting.

Le scanner ne parcourt jamais les milliers de références une par une et ne
fait aucun appel PriceCharting pendant un scan Vinted. L'export CSV est
normalisé une fois, puis interrogé via un index inversé en mémoire.

L'import est volontairement conservateur : seules les lignes de jeux vidéo
avec une plateforme reconnue, un volume de ventes suffisant et un prix
utilisable sont conservées. Les consoles et accessoires restent dans le
catalogue manuel afin d'éviter qu'un contrôleur ou une coque hérite du prix
d'une console.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path


SCHEMA = 1
DEFAULT_OUTPUT = Path("runtime_data/pricecharting_catalog.json")
TOKEN_RE = re.compile(r"[a-z0-9]+")
BRACKET_RE = re.compile(r"\s*[\[(].*?[\])]\s*")


def normalise(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("'", "").replace("’", "")
    return " ".join(TOKEN_RE.findall(text.lower()))


@dataclass(frozen=True)
class Platform:
    canonical: str
    aliases: tuple[str, ...]
    category: str
    query: str


PLATFORMS = (
    Platform("Nintendo Switch 2", ("nintendo switch 2", "switch 2"), "JEU_SWITCH2", "nintendo switch 2"),
    Platform("Nintendo Switch", ("nintendo switch", "switch"), "JEU_SWITCH", "nintendo switch jeu"),
    Platform("Nintendo 3DS", ("nintendo 3ds", "3ds"), "JEU_3DS", "jeu nintendo 3ds"),
    Platform("Nintendo DS", ("nintendo ds", "jeu ds", " ds "), "JEU_DS", "jeu nintendo ds"),
    Platform("GameBoy Advance", ("game boy advance", "gameboy advance", "gba"), "JEU_GBA", "jeu game boy advance"),
    Platform("GameBoy Color", ("game boy color", "gameboy color", "gbc"), "JEU_GBC", "jeu game boy color"),
    Platform("GameBoy", ("game boy", "gameboy"), "JEU_GAMEBOY", "jeu game boy"),
    Platform("Nintendo 64", ("nintendo 64", "n64"), "JEU_N64", "jeu nintendo 64"),
    Platform("Gamecube", ("nintendo gamecube", "gamecube"), "JEU_GAMECUBE", "jeu gamecube"),
    Platform("Wii U", ("wii u",), "JEU_WIIU", "jeu wii u"),
    Platform("Wii", ("nintendo wii", "jeu wii", " wii "), "JEU_WII", "jeu nintendo wii"),
    Platform("Super Nintendo", ("super nintendo", "snes"), "JEU_SNES", "jeu super nintendo"),
    Platform("Nintendo NES", ("nintendo nes", "jeu nes", " nes "), "JEU_NES", "jeu nintendo nes"),
    Platform("Playstation 5", ("playstation 5", "ps5"), "JEU_PS5", "jeu ps5"),
    Platform("Playstation 4", ("playstation 4", "ps4"), "JEU_PS4", "jeu ps4"),
    Platform("Playstation 3", ("playstation 3", "ps3"), "JEU_PS3", "jeu ps3"),
    Platform("Playstation 2", ("playstation 2", "ps2"), "JEU_PS2", "jeu ps2"),
    Platform("Playstation", ("playstation 1", "ps1", "psx"), "JEU_PS1", "jeu ps1"),
    Platform("Playstation Vita", ("playstation vita", "ps vita", "psvita"), "JEU_VITA", "jeu ps vita"),
    Platform("PSP", ("sony psp", "jeu psp", " psp "), "JEU_PSP", "jeu psp"),
    Platform("Xbox Series X", ("xbox series x", "xbox series"), "JEU_XBOX_SERIES", "jeu xbox series"),
    Platform("Xbox One", ("xbox one",), "JEU_XBOX_ONE", "jeu xbox one"),
    Platform("Xbox 360", ("xbox 360",), "JEU_XBOX_360", "jeu xbox 360"),
    Platform("Xbox", ("original xbox", "jeu xbox"), "JEU_XBOX", "jeu xbox"),
    Platform("Dreamcast", ("sega dreamcast", "dreamcast"), "JEU_DREAMCAST", "jeu dreamcast"),
    Platform("Sega Saturn", ("sega saturn", "saturn"), "JEU_SATURN", "jeu sega saturn"),
    Platform("Sega Genesis", ("sega genesis", "mega drive", "megadrive"), "JEU_MEGADRIVE", "jeu mega drive"),
    Platform("Sega Master System", ("master system",), "JEU_MASTER_SYSTEM", "jeu master system"),
    Platform("Game Gear", ("game gear",), "JEU_GAME_GEAR", "jeu game gear"),
)

PLATFORM_BY_NAME = {normalise(item.canonical): item for item in PLATFORMS}

# Les exports PriceCharting incluent aussi du matériel. Sans champ de type
# fiable, ces lignes ne doivent jamais devenir des règles GAME automatiques.
HARDWARE_TERMS = {
    "console", "system", "controller", "gamepad", "joy con", "joycon",
    "dock", "adapter", "charger", "battery", "replacement", "shell",
    "case", "cover", "stand", "cable", "memory card", "accessory",
    "headset", "keyboard", "mouse", "remote", "camera", "microphone",
    "steering wheel", "light gun", "carrying bag", "protector",
}

LISTING_BLOCK_TERMS = {
    "console", "boite vide", "boitier vide", "box only", "case only",
    "empty box", "keychain", "key chain", "porte cles", "portachiavi",
    "coque", "cover", "carcasa", "funda", "housse", "pochette",
    "manette", "controller", "controler", "comando", "mando",
    "adapter", "adaptateur", "chargeur", "charger", "dock",
}

EDITION_NOISE = {
    "pal", "ntsc", "game", "edition", "version", "video", "the", "a",
    "of", "and", "for", "with", "nintendo", "sony", "microsoft",
    "playstation", "xbox", "switch", "gameboy", "game", "boy",
}


def platform_for(raw_name: object) -> Platform | None:
    value = normalise(raw_name)
    for prefix in ("pal ", "ntsc ", "jp ", "japanese "):
        if value.startswith(prefix):
            value = value[len(prefix):]
    direct = PLATFORM_BY_NAME.get(value)
    if direct:
        return direct
    # Certains exports ajoutent une région à la fin du nom de console.
    for name, platform in sorted(
            PLATFORM_BY_NAME.items(), key=lambda pair: len(pair[0]), reverse=True):
        if value == name or value.startswith(name + " "):
            return platform
    return None


def _column(row: dict, *names: str):
    lowered = {normalise(key).replace(" ", "-"): value for key, value in row.items()}
    for name in names:
        key = normalise(name).replace(" ", "-")
        if key in lowered and lowered[key] not in (None, ""):
            return lowered[key]
    return None


def _price(value: object, official_cents: bool = True) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    has_currency = any(mark in raw for mark in ("$", "€", "£"))
    cleaned = re.sub(r"[^0-9,.-]", "", raw).replace(",", "")
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if number <= 0:
        return None
    # L'API et l'export officiel utilisent des centimes. Un CSV retraité avec
    # un symbole monétaire ou des décimales est également accepté.
    if official_cents and not has_currency and "." not in raw and "," not in raw:
        number /= 100.0
    return round(number, 2)


def _integer(value: object) -> int | None:
    try:
        return max(0, int(float(str(value).strip())))
    except (TypeError, ValueError):
        return None


def _looks_like_hardware(name: str) -> bool:
    value = f" {normalise(name)} "
    return any(f" {normalise(term)} " in value for term in HARDWARE_TERMS)


def _title_variants(name: str) -> list[str]:
    exact = normalise(name)
    without_brackets = normalise(BRACKET_RE.sub(" ", name))
    variants = [exact]
    if without_brackets and without_brackets != exact:
        variants.append(without_brackets)
    return list(dict.fromkeys(value for value in variants if value))


def convert_row(row: dict, *, min_sales_volume: int, usd_to_eur: float,
                resale_haircut: float, max_buy_ratio: float,
                require_sales_volume: bool = True) -> dict | None:
    name = str(_column(row, "product-name", "product name", "name", "title") or "").strip()
    platform = platform_for(_column(row, "console-name", "console name", "console", "platform"))
    if not name or platform is None or _looks_like_hardware(name):
        return None

    sales_volume = _integer(_column(
        row, "sales-volume", "sales volume", "sales-per-month", "sales per month",
    ))
    if require_sales_volume and sales_volume is None:
        return None
    if (sales_volume or 0) < max(0, int(min_sales_volume)):
        return None

    loose = _price(_column(row, "loose-price", "loose price", "loose"))
    cib = _price(_column(row, "cib-price", "cib price", "complete-price", "complete price", "cib"))
    if loose is None:
        loose = cib
    if loose is None:
        return None

    factor = max(0.01, float(usd_to_eur)) * max(0.10, min(float(resale_haircut), 1.0))
    resale_low = round(loose * factor, 2)
    resale_high = round(max(loose, cib or loose) * factor, 2)
    if resale_low < 8:
        return None

    variants = _title_variants(name)
    meaningful = [
        token for token in variants[-1].split()
        if len(token) >= 3 and token not in EDITION_NOISE
    ]
    if len(meaningful) < 2:
        return None

    demand = 5 if (sales_volume or 0) >= 12 else 4
    price_max = round(max(1.0, resale_low * float(max_buy_ratio)), 2)
    return {
        "id": str(_column(row, "id", "product-id", "product id") or ""),
        "name": name,
        "title_variants": variants,
        "platform": platform.canonical,
        "platform_aliases": list(platform.aliases),
        "category": platform.category,
        "query": platform.query,
        "product_type": "GAME",
        "resale_low": resale_low,
        "resale_high": resale_high,
        "price_max": price_max,
        "hot_buy": round(price_max * 0.80, 2),
        "sales_volume": int(sales_volume or 0),
        "demand_score": demand,
        "description": (
            f"Jeu {platform.canonical}; référence PriceCharting "
            f"({int(sales_volume or 0)} ventes/mois)."
        ),
        "source": "PriceCharting CSV",
    }


def import_csv(stream, *, min_sales_volume: int = 3,
               usd_to_eur: float = 0.85, resale_haircut: float = 0.80,
               max_buy_ratio: float = 0.45,
               require_sales_volume: bool = True) -> dict:
    if isinstance(stream, (bytes, bytearray)):
        stream = io.StringIO(bytes(stream).decode("utf-8-sig", errors="replace"))
    reader = csv.DictReader(stream)
    references = []
    rejected = 0
    seen = set()
    for row in reader:
        converted = convert_row(
            row,
            min_sales_volume=min_sales_volume,
            usd_to_eur=usd_to_eur,
            resale_haircut=resale_haircut,
            max_buy_ratio=max_buy_ratio,
            require_sales_volume=require_sales_volume,
        )
        if converted is None:
            rejected += 1
            continue
        identity = (normalise(converted["name"]), normalise(converted["platform"]))
        if identity in seen:
            continue
        seen.add(identity)
        references.append(converted)
    return {
        "schema": SCHEMA,
        "source": "PriceCharting CSV",
        "imported_at": time.time(),
        "currency": "EUR",
        "reference_count": len(references),
        "rejected_count": rejected,
        "references": references,
    }


def _decode_download(payload: bytes) -> bytes:
    if payload[:2] != b"PK":
        return payload
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not members:
            raise ValueError("l'archive PriceCharting ne contient aucun CSV")
        return archive.read(members[0])


def _download(url: str, timeout: float = 30.0) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "VintedDealScanner/1.0", "Accept": "text/csv,application/zip"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return _decode_download(response.read())


def _fresh(path: Path, max_age_hours: float) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        imported_at = float(data.get("imported_at", 0))
        return (time.time() - imported_at) <= max(0.0, max_age_hours) * 3600
    except (OSError, ValueError, TypeError):
        return False


def save_catalog(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


class ReferenceCatalog:
    """Catalogue PriceCharting indexé sans boucle sur toutes les références."""

    def __init__(self, references=()):
        self.references = [row for row in references if isinstance(row, dict)]
        self._index = defaultdict(set)
        token_frequency = Counter()
        for row in self.references:
            tokens = set()
            for variant in row.get("title_variants", []):
                tokens.update(normalise(variant).split())
            token_frequency.update(
                token for token in tokens if len(token) >= 3 and token not in EDITION_NOISE
            )
        for index, row in enumerate(self.references):
            tokens = set()
            for variant in row.get("title_variants", []):
                tokens.update(normalise(variant).split())
            anchors = sorted(
                (token for token in tokens if len(token) >= 3 and token not in EDITION_NOISE),
                key=lambda token: (token_frequency[token], -len(token), token),
            )[:3]
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
    def _contains(text: str, phrase: str) -> bool:
        return f" {normalise(phrase)} " in f" {text} "

    def match(self, title: str, price: float):
        title_n = normalise(title)
        padded_title = f" {title_n} "
        if any(f" {normalise(term)} " in padded_title
               for term in LISTING_BLOCK_TERMS):
            return None, None
        candidates = set()
        for token in set(title_n.split()):
            candidates.update(self._index.get(token, ()))
        matches = []
        for index in candidates:
            row = self.references[index]
            if float(price) > float(row.get("price_max", 0)):
                continue
            if not any(self._contains(title_n, alias)
                       for alias in row.get("platform_aliases", [])):
                continue
            variants = row.get("title_variants", [])
            matched_variant = max(
                (variant for variant in variants if self._contains(title_n, variant)),
                key=len, default=None,
            )
            if not matched_variant:
                continue
            matches.append((
                len(normalise(matched_variant)), int(row.get("sales_volume", 0)),
                float(row.get("resale_low", 0)), row,
            ))
        if not matches:
            return None, None
        row = max(matches, key=lambda item: item[:3])[3]
        rule = {
            "label": row["name"], "model": row["name"], "brand": "",
            "description": row.get("description", ""), "product_type": "GAME",
            "must_contain": [max(row["title_variants"], key=len)],
            "platform_any": list(row.get("platform_aliases", [])),
            "exclude": [], "resale_low": float(row["resale_low"]),
            "resale_high": float(row.get("resale_high", row["resale_low"])),
            "min_margin": 8, "min_roi_pct": 20,
            "demand_score": int(row.get("demand_score", 4)),
            "profile_priority": 100, "hot_buy_price": float(row.get("hot_buy", 0)),
            "external_source": row.get("source", "PriceCharting"),
            "external_id": row.get("id", ""),
            "sales_volume": int(row.get("sales_volume", 0)),
        }
        search = {
            "name": f"PRICECHARTING - {row['platform']}",
            "category": row.get("category", "JEU_AUTRE"),
            "product_type": "GAME", "query": row.get("query", "jeu video"),
            "price_to": float(row["price_max"]), "rules": [rule],
        }
        return search, rule

    def discovery_searches(self, max_price: float = 300.0) -> list[dict]:
        by_query = {}
        for row in self.references:
            query = str(row.get("query", "")).strip()
            if not query:
                continue
            current = by_query.get(query, 0.0)
            by_query[query] = max(current, min(float(row.get("price_max", 0)), max_price))
        return [
            {"name": f"PRICECHARTING - {query}", "category": "DISCOVERY",
             "query": query, "price_to": round(price_to, 2)}
            for query, price_to in sorted(by_query.items()) if price_to > 0
        ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Normalise un export CSV PriceCharting")
    parser.add_argument("--input", type=Path, help="export CSV ou ZIP local")
    parser.add_argument("--url", help="URL privée de l'export CSV")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-age-hours", type=float, default=24)
    parser.add_argument("--min-sales-volume", type=int, default=3)
    parser.add_argument("--usd-to-eur", type=float, default=0.85)
    parser.add_argument("--resale-haircut", type=float, default=0.80)
    parser.add_argument("--max-buy-ratio", type=float, default=0.45)
    parser.add_argument("--allow-missing", action="store_true")
    args = parser.parse_args(argv)

    if _fresh(args.output, args.max_age_hours):
        print(f"PriceCharting: cache récent conservé ({args.output})")
        return 0

    source_url = (args.url or os.getenv("PRICECHARTING_CSV_URL", "")).strip()
    try:
        if args.input:
            payload = _decode_download(args.input.read_bytes())
        elif source_url:
            payload = _download(source_url)
        elif args.allow_missing:
            print("PriceCharting: aucun export configuré, catalogue contrôlé conservé")
            return 0
        else:
            parser.error("indiquer --input ou PRICECHARTING_CSV_URL")
    except (OSError, ValueError) as exc:
        print(f"PriceCharting: import impossible: {exc}", file=sys.stderr)
        if args.allow_missing:
            print("PriceCharting: ancien cache ou catalogue contrôlé conservé")
            return 0
        return 1

    data = import_csv(
        payload,
        min_sales_volume=args.min_sales_volume,
        usd_to_eur=args.usd_to_eur,
        resale_haircut=args.resale_haircut,
        max_buy_ratio=args.max_buy_ratio,
        require_sales_volume=True,
    )
    data["source_sha256"] = hashlib.sha256(payload).hexdigest()
    if not data["reference_count"]:
        print("PriceCharting: aucune référence valide; ancien cache conservé", file=sys.stderr)
        return 0 if args.allow_missing else 1
    save_catalog(args.output, data)
    print(
        f"PriceCharting: {data['reference_count']} références rapides importées "
        f"({data['rejected_count']} écartées)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
