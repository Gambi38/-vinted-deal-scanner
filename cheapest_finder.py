"""Recherche d'un article précis : les annonces les moins chères, sans faux positifs.

Usage :
    python cheapest_finder.py "iPhone 11"
    python cheapest_finder.py "Nintendo Switch OLED" --max-price 200 --top 10

La logique de tri/filtrage (``find_cheapest``) est pure et testée sans réseau.
Un titre n'est retenu que si TOUS les mots de la recherche y figurent comme mots
entiers et s'il ne contient aucun mot de variante absent de la recherche
(« iPhone 11 » n'accepte pas « iPhone 11 Pro » ni « iPhone 12 »).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import unicodedata
from pathlib import Path
from urllib.parse import urlencode

BASE_DIR = Path(__file__).resolve().parent
BLACKLIST_PATH = BASE_DIR / "blacklist.json"

# Mots qui changent le produit : refusés s'ils ne sont pas dans la recherche.
VARIANT_WORDS = {
    "pro", "max", "plus", "mini", "ultra", "lite", "xl", "slim", "oled",
    "air", "se", "fold", "flip", "digital", "edition", "s", "x", "xs", "xr",
    "turbo", "classic", "mega", "neo", "duo",
}

# Accessoires : refusés sauf si la recherche les demande (FR / NL / DE / ES / IT / EN).
ACCESSORY_WORDS = {
    "coque", "etui", "housse", "protection", "protege", "protecteur", "vitre",
    "verre", "film", "chargeur", "cable", "adaptateur", "support", "notice",
    "case", "cover", "charger", "stand", "dock", "sticker", "skin", "pochette",
    "sacoche", "facade", "ecran", "batterie", "tempered", "protector",
    "funda", "fundas", "capa", "capas", "custodia", "cargador", "hoesje",
    "hulle", "handyhulle", "kabel", "ladekabel", "folie", "glas",
}

# Boîte vide / maquette / pièces : refusés quoi qu'il arrive (toutes langues).
EMPTY_OR_BROKEN_WORDS = {
    "vide", "vides", "leere", "leer", "lege", "empty", "vacia", "vacio",
    "vuota", "vuoto", "dummy", "factice", "maquette", "imitation", "attrappe",
    "piezas", "pezzi", "ricambi", "teile", "ersatzteile", "onderdelen",
    "defecto", "rotto", "kaputt", "kapot", "defekt", "roto",
    "pecas", "concertar", "despiece", "despieces", "reparar", "reparacion",
    "reparation", "reparatie", "reparatur", "bastler", "bastelgerat",
    "arriere", "trasera", "ruckseite", "rueckseite", "achterkant", "backcover",
}

# Emballage : refusé s'il est seul, accepté s'il accompagne l'appareil
# (« iPhone 11 avec boîte », « mit Originalverpackung »).
PACKAGING_WORDS = {
    "boite", "box", "doos", "verpackung", "originalverpackung", "emballage",
    "karton", "carton", "caja", "scatola", "coffret",
}
WITH_WORDS = {
    "avec", "with", "mit", "met", "con", "inclus", "incl", "incluse", "et",
    "and", "und", "en", "plus", "sans",
}

# Annonces qui ne sont pas une vente de l'article lui-même.
NOISE_PHRASES = (
    "recherche", "cherche", "wanted", "echange", "echanger", "troc",
    "reserve", "reserved", "vendu", "sold",
)
LOT_PHRASES = ("lot", "bundle", "pack de", "x2", "x3", "x4", "2x", "3x", "4x")


def plain(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("'", "").replace("’", "")
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _has_phrase(tokens_text: str, phrase: str) -> bool:
    phrase = plain(phrase)
    if not phrase:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", tokens_text) is not None


def load_bad_terms(path: Path = BLACKLIST_PATH) -> list[str]:
    """Termes 'cassé / faux' du blacklist.json du projet."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    terms: list[str] = []
    for key in ("hard_blacklist", "fake_blacklist"):
        terms.extend(str(t) for t in data.get(key, []) if str(t).strip())
    return terms


def _price(item) -> float | None:
    value = item.get("price")
    if isinstance(value, dict):
        value = value.get("amount") or value.get("value") or value.get("price")
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "."))
    except ValueError:
        return None
    return number if number > 0 else None


def rejection_reason(title: str, query: str, bad_terms=(), allow_lots=False):
    """None si le titre correspond exactement à la recherche, sinon la raison."""
    t, q = plain(title), plain(query)
    q_tokens = q.split()
    t_tokens = t.split()
    if not q_tokens:
        return "recherche vide"
    missing = [tok for tok in q_tokens if tok not in t_tokens]
    if missing:
        return "mot absent : " + missing[0]
    q_set = set(q_tokens)
    for tok in t_tokens:
        if tok in VARIANT_WORDS and tok not in q_set:
            return "autre variante : " + tok
    # Un chiffre collé au modèle (« 11 » puis « 12 ») signale un autre modèle.
    q_numbers = {tok for tok in q_tokens if tok.isdigit()}
    if q_numbers:
        for tok in t_tokens:
            if tok.isdigit() and tok not in q_numbers and len(tok) <= 2 \
                    and not _is_storage(t_tokens, tok):
                return "autre numéro de modèle : " + tok
    for word in ACCESSORY_WORDS:
        if word in t_tokens and word not in q_set:
            return "accessoire : " + word
    for word in EMPTY_OR_BROKEN_WORDS:
        if word in t_tokens and word not in q_set:
            return "vide / pièces / maquette : " + word
    for index, word in enumerate(t_tokens):
        if word in PACKAGING_WORDS and word not in q_set:
            if not WITH_WORDS.intersection(t_tokens[max(0, index - 3):index]):
                return "emballage seul : " + word
    for term in bad_terms:
        if _has_phrase(t, term) and not _has_phrase(q, term):
            return "défaut/faux : " + term
    for phrase in NOISE_PHRASES:
        if _has_phrase(t, phrase) and phrase not in q_set:
            return "pas une vente directe : " + phrase
    if not allow_lots:
        for phrase in LOT_PHRASES:
            if _has_phrase(t, phrase) and phrase not in q_set:
                return "lot : " + phrase
    return None


def _is_storage(tokens, number) -> bool:
    """« 64 go » / « 128 gb » : une capacité n'est pas un autre modèle."""
    i = tokens.index(number)
    return i + 1 < len(tokens) and tokens[i + 1] in {"go", "gb", "gig", "to", "tb", "mo"}


def model_mismatch(model, query) -> bool:
    """Le champ « Modèle » de Vinted désigne un modèle plus précis (« iPhone 11 Pro »)."""
    if not model:
        return False
    m, q = set(plain(model).split()), set(plain(query).split())
    return q <= m and bool(m - q)


def cost(row) -> float:
    """Coût réel : prix + protection acheteur (total) + livraison si connue."""
    base = row.get("total") or row["price"]
    return base + (row.get("shipping") or 0.0)


def find_cheapest(items, query, *, top=5, max_price=None, bad_terms=(),
                  allow_lots=False):
    """Retourne (résultats triés par prix, nombre d'annonces rejetées)."""
    seen: set[str] = set()
    kept = []
    rejected = 0
    for item in items:
        item_id = str(item.get("id"))
        if item_id in seen:
            continue
        seen.add(item_id)
        price = _price(item)
        if price is None or (max_price is not None and price > max_price):
            rejected += 1
            continue
        if rejection_reason(item.get("title", ""), query, bad_terms, allow_lots) \
                or model_mismatch(item.get("model"), query):
            rejected += 1
            continue
        kept.append({
            "id": item_id, "title": item.get("title", ""), "price": price,
            "total": item.get("total"), "shipping": item.get("shipping"),
            "status": item.get("status") or "", "url": item.get("url") or "",
        })
    kept.sort(key=lambda row: (cost(row), row["id"]))
    if len(kept) >= 4:
        median = statistics.median(row["price"] for row in kept)
        for row in kept:
            row["suspicious"] = row["price"] < 0.4 * median
    else:
        for row in kept:
            row["suspicious"] = False
    return (kept if top is None else kept[:top]), rejected


def parse_money(text) -> float | None:
    """« à partir de 5,49 € » / « 116.10 € » -> 5.49 / 116.1."""
    match = re.search(r"(\d+(?:[.,]\d{1,2})?)\s*€", str(text or ""))
    return float(match.group(1).replace(",", ".")) if match else None


_CARD_END = re.compile(
    r",\s*([\d.,]+)\s*€\s*,\s*([\d.,]+)\s*€(?:\s*Frais Vinted inclus)?\s*$")


def parse_card(href: str, raw: str) -> dict | None:
    """Lit l'attribut title d'une carte Vinted :
    « iPhone 11 64 Go, Marque: Apple, Modèle: iPhone 11, État: Bon état, 90.00 €, 95.20 € »."""
    ids = re.search(r"/items/(\d+)", href or "")
    ends = _CARD_END.search(raw or "")
    if not ids or not ends:
        return None
    body = raw[:ends.start()]
    status = re.search(r"État\s*:\s*([^,]+)", body)
    model = re.search(r"Modèle\s*:\s*([^,]+)", body)
    title = re.split(r",\s*(?:Marque|Modèle|État|Taille)\s*:", body, maxsplit=1)[0]
    return {
        "id": ids.group(1), "title": title.strip(),
        "price": float(ends.group(1).replace(",", ".")),
        "total": float(ends.group(2).replace(",", ".")),
        "status": status.group(1).strip() if status else "",
        "model": model.group(1).strip() if model else "",
        "url": "https://www.vinted.be/items/" + ids.group(1),
    }


CARDS_JS = """() => [...document.querySelectorAll('a[data-testid$="--overlay-link"]')]
  .map(a => ({href: a.getAttribute('href'), title: a.getAttribute('title')}))"""
BLOCK_MARKERS = ("verify you are human", "vérifiez que vous êtes humain",
                 "access denied", "unusual traffic", "confirmez que vous êtes humain")


class VintedBlocked(RuntimeError):
    pass


async def _goto(page, url, delay=2.0):
    response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    status = response.status if response else 0
    if status in (401, 403, 429):
        raise VintedBlocked(f"Vinted refuse l'accès (HTTP {status}). Réessaie plus tard.")
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    await asyncio.sleep(delay)
    body = (await page.locator("body").inner_text())[:5000].lower()
    if any(marker in body for marker in BLOCK_MARKERS):
        raise VintedBlocked("Vinted demande une vérification humaine. Lecture arrêtée.")
    return status


def catalog_url(query, page_no, price_from=None, price_to=None):
    params = {"search_text": query, "page": page_no}
    if price_from is not None:
        params["price_from"] = price_from
    if price_to is not None:
        params["price_to"] = price_to
    return "https://www.vinted.be/catalog?" + urlencode(params)


async def read_catalog(page, query, pages, price_from=None, price_to=None):
    items, seen = [], set()
    for page_no in range(1, pages + 1):
        await _goto(page, catalog_url(query, page_no, price_from, price_to))
        cards = await page.evaluate(CARDS_JS)
        fresh = 0
        for card in cards:
            row = parse_card(card.get("href"), card.get("title"))
            if row and row["id"] not in seen:
                seen.add(row["id"])
                items.append(row)
                fresh += 1
        if fresh == 0:
            break
    return items


async def read_shipping(page, url):
    """Livraison la moins chère (« à partir de X € »), None si introuvable."""
    await _goto(page, url, delay=1.5)
    loc = page.locator('[data-testid="item-shipping-banner-price"]')
    if await loc.count() == 0:
        return None
    return parse_money(await loc.first.inner_text())


async def search(query, *, top, pages, max_price, min_price, allow_lots, bad_terms,
                 with_shipping=True, headless=True):
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(locale="fr-BE")
            page = await context.new_page()
            items = await read_catalog(page, query, pages, min_price, max_price)
            matches, rejected = find_cheapest(
                items, query, top=None, max_price=max_price,
                bad_terms=bad_terms, allow_lots=allow_lots,
            )
            # Les prix anormalement bas (boîte vide, arnaque, pièces) ne
            # polluent pas le classement : ils sont listés à part.
            pool = [row for row in matches if not row["suspicious"]][:top + 5]
            doubtful = [row for row in matches if row["suspicious"]][:3]
            if with_shipping:
                for row in pool + doubtful:
                    try:
                        row["shipping"] = await read_shipping(page, row["url"])
                    except VintedBlocked:
                        raise
                    except Exception:
                        row["shipping"] = None
                pool.sort(key=lambda row: (cost(row), row["id"]))
            return items, pool[:top], doubtful, rejected
        finally:
            await browser.close()


def format_row(rank, row) -> str:
    total = row.get("total")
    shipping = row.get("shipping")
    parts = [f"{row['price']:.2f} € article"]
    if total:
        parts.append(f"{total:.2f} € avec protection")
    parts.append(f"+ {shipping:.2f} € port" if shipping is not None else "port inconnu")
    line = f"{rank}. {cost(row):.2f} € TOTAL  ({', '.join(parts)})\n   {row['title']}  [{row['status']}]"
    if row.get("suspicious"):
        line += "\n   ⚠ prix très bas : vérifier (arnaque / pièces ?)"
    return line + f"\n   {row['url']}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Trouve les annonces les moins chères d'un article précis.")
    parser.add_argument("article", help='Ex. "iPhone 11" ou "Nintendo Switch OLED"')
    parser.add_argument("--top", type=int, default=5, help="Nombre de résultats (défaut 5)")
    parser.add_argument("--max-price", type=float, default=None, help="Prix maximum en €")
    parser.add_argument("--min-price", type=float, default=None, help="Prix minimum en € (écarte les pièces à bas prix)")
    parser.add_argument("--pages", type=int, default=5, help="Pages Vinted à lire (1-10, ~96 annonces chacune, défaut 5)")
    parser.add_argument("--lots", action="store_true", help="Accepter aussi les lots")
    parser.add_argument("--no-shipping", action="store_true", help="Ne pas lire les frais de port (plus rapide)")
    args = parser.parse_args(argv)

    try:
        items, results, doubtful, rejected = asyncio.run(search(
            args.article, top=max(1, args.top), pages=max(1, min(args.pages, 10)),
            max_price=args.max_price, min_price=args.min_price,
            allow_lots=args.lots, bad_terms=load_bad_terms(),
            with_shipping=not args.no_shipping,
        ))
    except VintedBlocked as exc:
        print(f"\n{exc}")
        return 2
    print(f"\n{len(items)} annonces lues, {rejected} rejetées (mauvais modèle, accessoire, cassé, lot…)\n")
    if not items:
        print("Aucune annonce lue : Vinted n'a rien renvoyé (page vide ou changée).")
        return 2
    if not results:
        print("Aucune annonce exacte trouvée pour cet article.")
        return 1
    for rank, row in enumerate(results, 1):
        print(format_row(rank, row))
    if doubtful:
        print("\nÀ vérifier à la main (prix très bas par rapport aux autres : "
              "arnaque, pièces, boîte ?) :")
        for row in doubtful:
            print(format_row("-", row).replace("\n   ⚠ prix très bas : vérifier (arnaque / pièces ?)", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
