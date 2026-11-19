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
SUSPICIOUS_RATIO = 0.4     # prix < 40 % de la médiane = à vérifier

from title_rules import (  # noqa: E402  (règles de titre : voir title_rules.py)
    BLACKLIST_PATH, load_bad_terms, model_mismatch, plain, rejection_reason,
)


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


def cost(row) -> float:
    """Coût réel : prix + protection acheteur (total) + livraison si connue."""
    base = row.get("total") or row["price"]
    return base + (row.get("shipping") or 0.0)


def find_cheapest(items, query, *, top=5, max_price=None, bad_terms=(),
                  allow_lots=False):
    """Retourne (résultats triés par prix, nombre d'annonces rejetées).

    ``query`` peut être une chaîne ou une liste (article + alias) : une annonce
    est retenue si elle correspond exactement à l'une d'elles.
    """
    queries = [query] if isinstance(query, str) else list(query)
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
        if not any(
            rejection_reason(item.get("title", ""), q, bad_terms, allow_lots) is None
            and not model_mismatch(item.get("model"), q)
            for q in queries
        ):
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
            row["suspicious"] = row["price"] < SUSPICIOUS_RATIO * median
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


def catalog_url(query, page_no, price_from=None, price_to=None, order=None):
    params = {"search_text": query, "page": page_no}
    if order:
        params["order"] = order
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
        print(f"  page {page_no} : {len(cards)} cartes, {fresh} annonces lisibles", flush=True)
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


def reject_stats(items, query, bad_terms, allow_lots=False, limit=6):
    """Pourquoi les annonces sont rejetées (utile pour comprendre un résultat maigre)."""
    from collections import Counter
    counts = Counter()
    for item in items:
        reason = rejection_reason(item.get("title", ""), query, bad_terms, allow_lots)
        if reason:
            counts[reason.split(" : ")[0]] += 1
    return counts.most_common(limit)


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
