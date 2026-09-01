#!/usr/bin/env python3
import asyncio
import csv
import json
import re
import sys
import time
import os
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus, urljoin

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
SEEN_PATH = ROOT / "seen.json"
ALERTS_CSV = ROOT / "alerts.csv"
PROFILE_DIR = ROOT / ".vinted_profile"

PRICE_RE = re.compile(r"(?:(\d{1,4}(?:[.,]\d{1,2})?)\s*€|€\s*(\d{1,4}(?:[.,]\d{1,2})?))")
ITEM_ID_RE = re.compile(r"/items/(\d+)")

def load_json(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)

def norm(s):
    return re.sub(r"\s+", " ", (s or "").lower()).strip()

def parse_price(text):
    vals = []
    for m in PRICE_RE.finditer(text or ""):
        raw = m.group(1) or m.group(2)
        try:
            vals.append(float(raw.replace(",", ".")))
        except ValueError:
            pass
    return min(vals) if vals else None

def fee_estimate(price, cfg):
    bp = cfg.get("buyer_protection_estimate", {})
    fixed = float(bp.get("fixed", 0.70))
    pct = float(bp.get("pct", 0.05))
    shipping = float(cfg.get("shipping_estimate", 4.50))
    return fixed + pct * price + shipping

def rule_match(rule, text):
    t = norm(text)
    must = [norm(x) for x in rule.get("must_contain", [])]
    any_kw = [norm(x) for x in rule.get("any_contain", [])]
    exc = [norm(x) for x in rule.get("exclude", [])]
    if must and not all(x in t for x in must):
        return False
    if any_kw and not any(x in t for x in any_kw):
        return False
    if exc and any(x in t for x in exc):
        return False
    return True

def score_candidate(rule, listing_price, cfg):
    extras = fee_estimate(listing_price, cfg)
    total = listing_price + extras
    resale_low = rule.get("resale_low")
    resale_high = rule.get("resale_high")
    if resale_low is None:
        return {
            "total_buy": round(total, 2),
            "margin_low": None,
            "margin_high": None,
            "resale_low": None,
            "resale_high": None,
        }
    low = float(resale_low)
    high = float(resale_high or resale_low)
    return {
        "total_buy": round(total, 2),
        "margin_low": round(low - total, 2),
        "margin_high": round(high - total, 2),
        "resale_low": low,
        "resale_high": high,
    }




def ntfy_send(row):
    """Send a push notification through ntfy."""
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        return False

    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/")
    url = f"{server}/{urllib.parse.quote(topic, safe='')}"

    title = "🔥 Deal Vinted"
    if row["margin_low"] is None:
        title = "🔎 Annonce Vinted à vérifier"

    if row["margin_low"] is None:
        body = (
            f"{row.get('title', 'Annonce Vinted')[:160]}\n"
            f"Prix: {row['listing_price']:.2f} €\n"
            f"Coût estimé: {row['total_buy_est']:.2f} €"
        )
    else:
        body = (
            f"{row.get('title', 'Annonce Vinted')[:160]}\n"
            f"Prix: {row['listing_price']:.2f} €\n"
            f"Coût estimé: {row['total_buy_est']:.2f} €\n"
            f"Revente: {row['resale_low']:.0f}–{row['resale_high']:.0f} €\n"
            f"Marge: +{row['margin_low']:.2f} à +{row['margin_high']:.2f} €"
        )

    headers = {
        "Title": title,
        "Priority": "high" if row["margin_low"] is not None and row["margin_low"] >= 40 else "default",
        "Tags": "moneybag,shopping_cart",
        "Click": row["url"],
        "Actions": f"view, Ouvrir Vinted, {row['url']}",
    }

    auth_token = os.getenv("NTFY_ACCESS_TOKEN", "").strip()
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    try:
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"  ! ntfy: {e}")
        return False

def append_alert(row):
    new = not ALERTS_CSV.exists()
    with ALERTS_CSV.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=[
            "timestamp","search","title","listing_price","total_buy_est",
            "resale_low","resale_high","margin_low","margin_high","url","item_id"
        ])
        if new:
            w.writeheader()
        w.writerow(row)

async def extract_cards(page):
    # We intentionally use broad selectors because Vinted changes CSS class names often.
    data = await page.locator('a[href*="/items/"]').evaluate_all("""
    els => els.map(a => {
      const href = a.href;
      let node = a;
      let text = (a.innerText || '').trim();
      for (let i=0; i<5 && node; i++, node=node.parentElement) {
        const t = (node.innerText || '').trim();
        if (t.length > text.length && t.length < 1200) text = t;
      }
      return {href, text};
    })
    """)
    out, seen = [], set()
    for x in data:
        href = x.get("href","")
        m = ITEM_ID_RE.search(href)
        if not m:
            continue
        item_id = m.group(1)
        if item_id in seen:
            continue
        seen.add(item_id)
        out.append({"item_id": item_id, "url": href.split("?")[0], "text": x.get("text","")})
    return out

async def scan_search(page, search, cfg, seen_ids):
    base = cfg.get("base_url", "https://www.vinted.be").rstrip("/")
    query = search["query"]
    price_to = search.get("price_to")
    url = f"{base}/catalog?search_text={quote_plus(query)}&order=newest_first"
    if price_to is not None:
        url += f"&price_to={float(price_to):g}"

    print(f"\n[SCAN] {search['name']}  ->  {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(int(cfg.get("page_wait_ms", 2500)))
        await page.locator('a[href*="/items/"]').first.wait_for(timeout=12000)
    except PlaywrightTimeoutError:
        print("  ! Aucun résultat visible / validation Vinted possible.")
        return []

    cards = await extract_cards(page)
    new_alerts = []

    for c in cards[: int(cfg.get("max_items_per_search", 60))]:
        if c["item_id"] in seen_ids:
            continue

        text = c["text"]
        p = parse_price(text)
        if p is None:
            continue

        # Search-level ceiling first.
        if search.get("price_to") is not None and p > float(search["price_to"]) * 1.05:
            continue

        matched_rule = None
        for rule in search.get("rules", []):
            if rule_match(rule, text):
                matched_rule = rule
                break

        if matched_rule is None:
            if search.get("manual_review", False):
                matched_rule = {
                    "label": "REVUE MANUELLE",
                    "resale_low": None,
                    "resale_high": None,
                }
            else:
                continue

        s = score_candidate(matched_rule, p, cfg)
        min_margin = float(matched_rule.get("min_margin", cfg.get("min_margin", 25)))
        if s["margin_low"] is not None and s["margin_low"] < min_margin:
            continue

        title = norm(text)[:140]
        row = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "search": search["name"] + " / " + matched_rule.get("label",""),
            "title": title,
            "listing_price": round(p,2),
            "total_buy_est": s["total_buy"],
            "resale_low": s["resale_low"],
            "resale_high": s["resale_high"],
            "margin_low": s["margin_low"],
            "margin_high": s["margin_high"],
            "url": c["url"],
            "item_id": c["item_id"],
        }
        append_alert(row)
        new_alerts.append(row)
        ntfy_send(row)

        if s["margin_low"] is None:
            print(f"  ? REVUE | {p:.2f} € | {c['url']}")
        else:
            print(
                f"  ★ DEAL | annonce {p:.2f} € | coût ~{s['total_buy']:.2f} € | "
                f"revente {s['resale_low']:.0f}-{s['resale_high']:.0f} € | "
                f"marge {s['margin_low']:.2f}-{s['margin_high']:.2f} €\n"
                f"          {c['url']}"
            )

    # Only persist IDs that actually generated an alert.
    # This keeps GitHub history small while preventing duplicate Telegram alerts.
    for row in new_alerts:
        seen_ids.add(row["item_id"])
    return new_alerts

async def main():
    cfg = load_json(CONFIG_PATH, {})
    if not cfg:
        print("config.json introuvable.")
        sys.exit(1)

    seen_ids = set(load_json(SEEN_PATH, []))
    one_shot = "--once" in sys.argv
    headless = "--headless" in sys.argv

    print("Vinted Deal Scanner")
    print("Ctrl+C pour arrêter.")
    print("Les coûts/frais sont des ESTIMATIONS configurables dans config.json.")

    async with async_playwright() as p:
        # In Docker/server mode, HEADLESS=1 is used automatically.
        env_headless = os.getenv("HEADLESS", "").strip().lower() in {"1", "true", "yes", "on"}
        effective_headless = headless or env_headless
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=effective_headless,
            viewport={"width": 1280, "height": 900},
            locale="fr-BE",
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        while True:
            cycle_alerts = 0
            for search in cfg.get("searches", []):
                try:
                    alerts = await scan_search(page, search, cfg, seen_ids)
                    cycle_alerts += len(alerts)
                    save_json(SEEN_PATH, sorted(seen_ids))
                    await asyncio.sleep(float(cfg.get("delay_between_searches", 3)))
                except Exception as e:
                    print(f"  ! Erreur sur {search.get('name')}: {e}")

            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Cycle terminé — {cycle_alerts} nouveau(x) deal(s).")
            save_json(SEEN_PATH, sorted(seen_ids))

            if one_shot:
                break
            await asyncio.sleep(float(cfg.get("poll_seconds", 90)))

        await context.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nArrêt demandé.")
