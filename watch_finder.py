"""Surveillance longue durée d'un article précis sur Vinted.

Au lieu de relire toutes les pages, le bot :
  1. fait un état des lieux au départ (pages par pertinence + les plus récentes de
     chaque recherche voisine), calcule le prix médian et marque tout comme « vu »
     SANS alerter (sinon des centaines de vieilles annonces déclencheraient des alertes) ;
  2. recharge ensuite en boucle la page « plus récent d'abord » de plusieurs
     recherches voisines (« iphone 11 », « iphone11 », synonymes, alias…) ;
  3. ne regarde que les annonces jamais vues, applique les filtres stricts de
     title_rules.py, lit les frais de port et alerte si c'est une affaire ;
  4. tous les quelques cycles, balaie les annonces les moins chères pour repérer
     les baisses de prix d'annonces déjà vues.

Usage :
    python watch_finder.py "iPhone 11" --max-price 120 --duration-minutes 300
    python watch_finder.py "Nintendo Switch OLED" --alias "switch oled" --once
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import re
import statistics
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from cheapest_finder import (
    CARDS_JS, SUSPICIOUS_RATIO, VintedBlocked, _goto, catalog_url, cost,
    find_cheapest, load_bad_terms, model_mismatch, parse_card, plain,
    read_catalog, read_shipping, rejection_reason,
)

BASE_DIR = Path(__file__).resolve().parent
SEEN_RETENTION_DAYS = 14
MAX_NEWEST_PAGES = 3            # pages lues par cycle quand tout est nouveau
SHIPPING_LOOKUPS_PER_CYCLE = 6  # pages d'annonces ouvertes par cycle (anti-429)
MAX_CONSECUTIVE_BLOCKS = 6      # on arrête si Vinted bloque sans jamais répondre
MAX_CONSECUTIVE_ERRORS = 20
SWEEP_EVERY = 8                 # cycles entre deux balayages « moins cher d'abord »

# Un même article s'écrit de plusieurs façons : les deux sens sont utilisés
# pour chercher ET pour reconnaître les titres.
SYNONYM_PAIRS = (
    ("ps5", "playstation 5"), ("ps4", "playstation 4"), ("ps3", "playstation 3"),
    ("ps2", "playstation 2"), ("psp", "playstation portable"),
    ("gba", "game boy advance"), ("gbc", "game boy color"),
    ("switch", "nintendo switch"), ("wii", "nintendo wii"), ("3ds", "nintendo 3ds"),
    ("airpods", "apple airpods"), ("xbox series x", "xbox series x console"),
)


# ---------------------------------------------------------------- recherches

def synonyms_for(article: str) -> list[str]:
    key = plain(article)
    found = []
    for left, right in SYNONYM_PAIRS:
        if key == plain(left):
            found.append(right)
        elif key == plain(right):
            found.append(left)
    return found


def match_terms(article: str, aliases=()) -> list[str]:
    """Noms acceptés pour reconnaître un titre (l'article, ses alias, ses synonymes)."""
    result, seen = [], set()
    for term in (article, *aliases, *synonyms_for(article)):
        term = " ".join(str(term).split())
        if term and plain(term) not in seen:
            seen.add(plain(term))
            result.append(term)
    return result


def expand_queries(article: str, aliases=()) -> list[str]:
    """Recherches voisines qui ramènent les mêmes annonces par des chemins différents."""
    base = " ".join(str(article).split())
    tokens = plain(base).split()                      # « iphone 11 », sans accents
    glued, current = [], ""
    for token in tokens:                              # chiffre collé au mot : « iphone11 »
        if token.isdigit() and current:
            current += token
        else:
            if current:
                glued.append(current)
            current = token
    if current:
        glued.append(current)
    candidates = [base, " ".join(tokens), " ".join(glued), *aliases, *synonyms_for(article)]
    seen, result = set(), []
    for query in candidates:
        query = " ".join(str(query).split())
        if query and query.lower() not in seen:
            seen.add(query.lower())
            result.append(query)
    return result


def matches_any(row, queries, bad_terms, allow_lots=False) -> bool:
    return any(
        rejection_reason(row["title"], q, bad_terms, allow_lots) is None
        and not model_mismatch(row.get("model"), q)
        for q in queries
    )


def classify(row, max_price, median, deal_ratio) -> str | None:
    """'bon' = à signaler, 'suspect' = prix trop bas pour être sûr, None = ignorer."""
    total = cost(row)
    if max_price is not None and total > max_price:
        return None
    if median:
        if row["price"] < SUSPICIOUS_RATIO * median:
            return "suspect"
        if max_price is None and total > deal_ratio * median:
            return None
    return "bon"


def backoff_seconds(failures: int) -> float:
    return float(min(60 * 2 ** max(0, failures - 1), 900))


# -------------------------------------------------------------------- état

def slug(article: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", plain(article)).strip("-") or "article"


def load_state(path: Path) -> dict:
    fresh = {"seen": {}, "alerted": {}, "median": None, "baseline_done": False}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("seen"), dict):
            return {**fresh, **data}
    except (OSError, ValueError):
        pass
    return fresh


def save_state(path: Path, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def prune_seen(state: dict, now: float | None = None) -> None:
    limit = (now or time.time()) - SEEN_RETENTION_DAYS * 86400
    for key in ("seen", "alerted"):
        state[key] = {k: v for k, v in state.get(key, {}).items() if v >= limit}


def mark_seen(state, row, now=None) -> None:
    state["seen"][row["id"]] = now or time.time()


def split_new(rows, state, now=None, mark=True):
    """Retourne (nouvelles annonces sans doublon, nombre déjà vues).

    mark=False laisse l'annonce « non vue » : si Vinted bloque en plein
    traitement, elle sera reprise au cycle suivant au lieu d'être perdue.
    """
    fresh, known, batch = [], 0, set()
    for row in rows:
        if row["id"] in state["seen"] or row["id"] in batch:
            known += 1
            continue
        batch.add(row["id"])
        if mark:
            mark_seen(state, row, now)
        fresh.append(row)
    return fresh, known


# ------------------------------------------------------------ notifications

def ascii_header(text: str) -> str:
    return plain(text).title()[:120] or "Vinted"


def send_ntfy(title: str, body: str, url: str = "", priority: str = "default") -> bool:
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Title": ascii_header(title), "Priority": priority}
    if url:
        headers["Click"] = url
    request = urllib.request.Request(
        f"{server}/{topic}", data=body.encode("utf-8")[:3500], headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=15):
            return True
    except Exception as exc:  # une alerte ratée ne doit jamais arrêter la veille
        print(f"[ntfy] échec : {type(exc).__name__}")
        return False


def describe(row) -> str:
    total, shipping = row.get("total"), row.get("shipping")
    port = f"+ {shipping:.2f} € port" if shipping is not None else "port non lu"
    return (f"{cost(row):.2f} € TOTAL ({row['price']:.2f} € article, "
            f"{(total or row['price']):.2f} € avec protection, {port})\n"
            f"{row['title']} [{row.get('status', '')}]\n{row['url']}")


def log(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


# --------------------------------------------------------------- navigation

def parse_cards(cards):
    return [r for r in (parse_card(c.get("href"), c.get("title")) for c in cards) if r]


async def read_newest(page, query, known_ids, max_pages=MAX_NEWEST_PAGES):
    """Recharge « plus récent d'abord » ; descend d'une page seulement si tout est nouveau."""
    rows = []
    for page_no in range(1, max_pages + 1):
        await _goto(page, catalog_url(query, page_no, order="newest_first"), delay=1.0)
        batch = parse_cards(await page.evaluate(CARDS_JS))
        rows.extend(batch)
        if not batch or any(r["id"] in known_ids for r in batch):
            break
    return rows


async def read_cheapest(page, query, price_from, price_to):
    """Une page « moins cher d'abord » : repère les baisses de prix et les affaires."""
    await _goto(page, catalog_url(query, 1, price_from, price_to, order="price_low_to_high"),
                delay=1.0)
    return parse_cards(await page.evaluate(CARDS_JS))


async def new_page(browser):
    context = await browser.new_context(locale="fr-BE")
    return await context.new_page()


async def lookup_shipping(page, row):
    try:
        row["shipping"] = await read_shipping(page, row["url"])
    except VintedBlocked:
        raise                                    # l'annonce reste « non vue »
    except Exception:
        row["shipping"] = None


async def baseline(page, args, queries, match_queries, bad_terms, state):
    """État des lieux silencieux : médiane du marché, tout est marqué « vu »."""
    items = await read_catalog(page, args.article, args.baseline_pages)
    for query in queries:                         # + les plus récentes de chaque recherche
        items.extend(await read_newest(page, query, set(), max_pages=1))
    matches, _rejected = find_cheapest(
        items, match_queries, top=None, bad_terms=bad_terms, allow_lots=args.lots,
    )
    now = time.time()
    for row in items:
        state["seen"][row["id"]] = now
    prices = [row["price"] for row in matches]            # médiane SANS plafond de prix
    state["median"] = statistics.median(prices) if len(prices) >= 4 else None
    good = [r for r in matches if not r["suspicious"]]
    if args.max_price is not None:
        good = [r for r in good if cost(r) <= args.max_price]
    good = good[:5]
    for row in good:
        await lookup_shipping(page, row)
        state["alerted"][row["id"]] = now
    good.sort(key=cost)
    state["baseline_done"] = True
    log(f"État des lieux : {len(set(r['id'] for r in items))} annonces lues, {len(matches)} exactes, "
        f"médiane {state['median'] if state['median'] else 'inconnue'} €")
    if good:
        body = "\n\n".join(f"{i}. {describe(r)}" for i, r in enumerate(good, 1))
        log("Meilleures annonces actuelles :\n" + body)
        send_ntfy(f"Depart : {args.article}", "Meilleures annonces actuelles\n\n" + body,
                  good[0]["url"])


async def consider(page, row, args, queries, bad_terms, state, found_path, budget):
    """Décide si une annonce mérite une alerte ; retourne 1 si alerte envoyée."""
    if row["id"] in state["alerted"]:
        return 0
    if not matches_any(row, queries, bad_terms, args.lots):
        return 0
    verdict = classify(row, args.max_price, state.get("median"), args.deal_ratio)
    if not verdict:
        return 0
    if budget["shipping"] > 0:                    # limite les navigations (anti-429)
        budget["shipping"] -= 1
        await lookup_shipping(page, row)
    if args.max_price is not None and cost(row) > args.max_price:
        return 0                                  # le port le rend trop cher
    state["alerted"][row["id"]] = time.time()
    label = "AFFAIRE" if verdict == "bon" else "A VERIFIER (prix tres bas)"
    log(f"{label} : {row['title']} | {cost(row):.2f} €")
    send_ntfy(f"{label} {args.article} {cost(row):.0f} EUR", describe(row), row["url"],
              "high" if verdict == "bon" else "default")
    with open(found_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "time": datetime.now(timezone.utc).isoformat(), "verdict": verdict,
            "cost": round(cost(row), 2), **row}, ensure_ascii=False) + "\n")
    return 1


async def handle_new(page, fresh, args, queries, bad_terms, state, found_path, budget):
    alerts = 0
    for row in fresh:
        alerts += await consider(page, row, args, queries, bad_terms, state, found_path, budget)
        mark_seen(state, row)                     # seulement après traitement
    return alerts


def sweep_bounds(args, median):
    """Fourchette de prix du balayage : sous le plafond, au-dessus des prix absurdes."""
    price_from = math.floor(SUSPICIOUS_RATIO * median) if median else None
    if args.max_price is not None:
        price_to = args.max_price
    elif median:
        price_to = math.ceil(args.deal_ratio * median)
    else:
        price_to = None
    return price_from, price_to


async def watch(args) -> int:
    from playwright.async_api import async_playwright

    queries = expand_queries(args.article, args.alias)
    match_queries = match_terms(args.article, args.alias)
    bad_terms = load_bad_terms()
    state_dir = Path(args.state_dir)
    state_path = state_dir / f"finder_{slug(args.article)}.json"
    found_path = state_dir / f"trouvailles_{slug(args.article)}.jsonl"
    state_dir.mkdir(parents=True, exist_ok=True)
    state = load_state(state_path)
    prune_seen(state)
    deadline = time.monotonic() + args.duration_minutes * 60
    log(f"Surveillance de « {args.article} » | recherches : {queries} | reconnu comme : "
        f"{match_queries} | {'un seul cycle' if args.once else f'{args.duration_minutes:g} min'}")

    blocks = errors = successes = cycle = total_alerts = 0
    last_heartbeat = time.monotonic()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = None
        try:
            page = await new_page(browser)
            while True:
                cycle += 1
                try:
                    if not state["baseline_done"]:
                        await baseline(page, args, queries, match_queries, bad_terms, state)
                        save_state(state_path, state)
                    budget = {"shipping": SHIPPING_LOOKUPS_PER_CYCLE}
                    new_total = 0
                    for query in queries:
                        rows = await read_newest(page, query, state["seen"])
                        fresh, _known = split_new(rows, state, mark=False)
                        new_total += len(fresh)
                        total_alerts += await handle_new(
                            page, fresh, args, match_queries, bad_terms, state, found_path, budget)
                        await asyncio.sleep(random.uniform(1.0, 2.5))
                    if cycle % SWEEP_EVERY == 0:
                        price_from, price_to = sweep_bounds(args, state.get("median"))
                        for row in await read_cheapest(page, args.article, price_from, price_to):
                            total_alerts += await consider(
                                page, row, args, match_queries, bad_terms, state, found_path, budget)
                    blocks = errors = 0
                    successes += 1
                    save_state(state_path, state)
                    log(f"cycle {cycle} | {len(queries)} recherches | {new_total} nouvelles "
                        f"annonces | {len(state['seen'])} vues | {total_alerts} alertes")
                except VintedBlocked as exc:
                    blocks += 1
                    wait = backoff_seconds(blocks)
                    log(f"{exc} Pause {wait:.0f} s (blocage {blocks}).")
                    if successes == 0 and blocks >= MAX_CONSECUTIVE_BLOCKS:
                        send_ntfy(f"Vinted bloque {args.article}",
                                  "Vinted refuse ce serveur. Surveillance arrêtée.")
                        log("Vinted bloque ce serveur dès le départ : arrêt.")
                        return 2
                    if args.once:
                        return 2
                    await asyncio.sleep(min(wait, max(1.0, deadline - time.monotonic())))
                except Exception as exc:
                    errors += 1
                    log(f"Erreur {type(exc).__name__} : {str(exc)[:120]} — navigateur relancé.")
                    try:
                        await browser.close()
                    except Exception:
                        pass
                    try:
                        browser = await pw.chromium.launch(headless=True)
                        page = await new_page(browser)
                    except Exception as launch_error:
                        log(f"Impossible de relancer le navigateur : {type(launch_error).__name__}")
                        send_ntfy(f"Erreur {args.article}", "Le navigateur ne redémarre plus.")
                        return 3
                    if errors >= MAX_CONSECUTIVE_ERRORS:
                        send_ntfy(f"Erreurs {args.article}", "Trop d'erreurs de suite : arrêt.")
                        return 3
                    if args.once:
                        return 3
                    await asyncio.sleep(min(30, backoff_seconds(errors)))

                if args.once or time.monotonic() >= deadline:
                    break
                if cycle % 40 == 0:                      # évite les fuites mémoire
                    await page.context.close()
                    page = await new_page(browser)
                if (args.heartbeat_minutes
                        and time.monotonic() - last_heartbeat > args.heartbeat_minutes * 60):
                    last_heartbeat = time.monotonic()
                    send_ntfy(f"Toujours actif : {args.article}",
                              f"{cycle} cycles, {total_alerts} alertes, {len(state['seen'])} annonces vues.")
                pause = args.interval * random.uniform(0.7, 1.3)
                await asyncio.sleep(max(1.0, min(pause, deadline - time.monotonic())))
        finally:
            save_state(state_path, state)
            try:
                await browser.close()
            except Exception:
                pass
    log(f"Fin : {cycle} cycles, {total_alerts} alertes.")
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Surveille un article précis sur Vinted.")
    parser.add_argument("article", help='Ex. "iPhone 11"')
    parser.add_argument("--alias", action="append", default=[],
                        help="Autre façon de nommer l'article (répétable), ex. --alias='switch oled'")
    parser.add_argument("--max-price", type=float, default=None, help="Coût total maximum en €")
    parser.add_argument("--deal-ratio", type=float, default=0.85,
                        help="Sans prix max : alerte sous ce ratio du prix médian (défaut 0.85)")
    parser.add_argument("--duration-minutes", type=float, default=330)
    parser.add_argument("--interval", type=float, default=45, help="Secondes entre deux cycles")
    parser.add_argument("--baseline-pages", type=int, default=3)
    parser.add_argument("--heartbeat-minutes", type=float, default=60)
    parser.add_argument("--state-dir", default=str(BASE_DIR / "runtime_data"))
    parser.add_argument("--lots", action="store_true")
    parser.add_argument("--once", action="store_true", help="Un seul cycle (test)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(watch(args))
    except KeyboardInterrupt:
        log("Arrêt demandé.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
