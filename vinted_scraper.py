#!/usr/bin/env python3
import asyncio, csv, json, os, re, sys, unicodedata, urllib.parse, urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus, unquote
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
BLACKLIST_PATH = ROOT / "blacklist.json"
SEEN_PATH = ROOT / "seen.json"
ALERTS_CSV = ROOT / "alerts.csv"
PROFILE_DIR = ROOT / ".vinted_profile"

PRICE_RE = re.compile(r"(?:(\d{1,4}(?:[.,]\d{1,2})?)\s*€|€\s*(\d{1,4}(?:[.,]\d{1,2})?))")
ITEM_ID_RE = re.compile(r"/items/(\d+)")
SLUG_RE = re.compile(r"/items/\d+-([^/?#]+)")

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
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower()).strip()

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
    return (
        float(bp.get("fixed", 0.70))
        + float(bp.get("pct", 0.05)) * price
        + float(cfg.get("shipping_estimate", 4.50))
    )

def keyword_hits(text, words):
    t = norm(text)
    return [w for w in words if norm(w) and norm(w) in t]

def blacklist_check(title, text, blacklist):
    combined = f"{title} {text}"
    for group in ("hard_blacklist", "fake_blacklist", "accessory_blacklist"):
        hits = keyword_hits(combined, blacklist.get(group, []))
        if hits:
            return True, group, hits[:3], []
    risks = keyword_hits(combined, blacklist.get("suspicious_words", []))
    return False, "", [], risks[:3]

def rule_match(rule, title, text):
    title_n = norm(title)
    full_n = norm(f"{title} {text}")
    must = [norm(x) for x in rule.get("must_contain", [])]
    any_kw = [norm(x) for x in rule.get("any_contain", [])]
    hardware = [norm(x) for x in rule.get("hardware_any", [])]
    excludes = [norm(x) for x in rule.get("exclude", [])]

    if must and not all(x in title_n for x in must):
        return False
    if any_kw and not any(x in title_n for x in any_kw):
        return False
    if hardware and not any(x in full_n for x in hardware):
        return False
    if excludes and any(x in full_n for x in excludes):
        return False
    return True

def score_candidate(rule, price, cfg):
    total = price + fee_estimate(price, cfg)
    low = rule.get("resale_low")
    high = rule.get("resale_high")
    if low is None:
        return round(total, 2), None, None, None, None, None
    low = float(low)
    high = float(high or low)
    margin_low = low - total
    margin_high = high - total
    roi = margin_low / total * 100 if total > 0 else 0
    return round(total, 2), low, high, round(margin_low, 2), round(margin_high, 2), round(roi, 1)

def clean_title(value):
    value = re.sub(r"^(image|photo)\s+(de|of)\s+", "", (value or "").strip(), flags=re.I)
    value = re.sub(r"\s+", " ", value)
    if not 3 <= len(value) <= 220 or "€" in value:
        return ""
    return value

def title_from_card(card):
    for key in ("img_alt", "aria_label", "anchor_title", "anchor_text"):
        value = clean_title(card.get(key, ""))
        if value:
            return value
    m = SLUG_RE.search(card.get("href", ""))
    return unquote(m.group(1)).replace("-", " ") if m else "Annonce Vinted"

def ntfy_send(row):
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    url = f"{os.getenv('NTFY_SERVER','https://ntfy.sh').rstrip('/')}/{urllib.parse.quote(topic, safe='')}"
    title = "Deal Vinted"
    if row["risk"]:
        title = "Deal Vinted - A VERIFIER"
    elif row["margin_low"] >= 45:
        title = "Deal Vinted - TRES BON"

    body = (
        f"{row['title'][:160]}\n"
        f"Prix: {row['listing_price']:.2f} EUR\n"
        f"Cout estime: {row['total_buy_est']:.2f} EUR\n"
        f"Revente prudente: {row['resale_low']:.0f}-{row['resale_high']:.0f} EUR\n"
        f"Marge: +{row['margin_low']:.2f} a +{row['margin_high']:.2f} EUR\n"
        f"ROI mini: {row['roi_low']:.0f}%\n"
        f"Demande: {row['demand_score']}/5"
    )
    if row["risk"]:
        body += f"\nRisque: {row['risk']}"

    headers = {
        "Title": title,
        "Priority": "high" if row["margin_low"] >= 40 else "default",
        "Tags": "moneybag,shopping_cart",
        "Click": row["url"],
        "Actions": f"view, Ouvrir Vinted, {row['url']}",
    }
    if row.get("image_url"):
        headers["Attach"] = row["image_url"]
    try:
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            method="POST",
            headers=headers
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"  ! ntfy: {e}")
        return False

def append_alert(row):
    fields = [
        "timestamp","search","title","listing_price","total_buy_est",
        "resale_low","resale_high","margin_low","margin_high","url","item_id"
    ]
    new = not ALERTS_CSV.exists()
    with ALERTS_CSV.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader() 
        w.writerow({k: row.get(k, "") for k in fields})


async def extract_cards(page):
    data = await page.locator('a[href*="/items/"]').evaluate_all("""
    els => els.map(a => {
      let node = a;
      let text = (a.innerText || '').trim();

      for (let i = 0; i < 5 && node; i++, node = node.parentElement) {
        const t = (node.innerText || '').trim();
        if (t.length > text.length && t.length < 1200) text = t;
      }

      const img = a.querySelector('img');

      return {
        href: a.href || '',
        text: text,
        anchor_text: (a.innerText || '').trim(),
        aria_label: a.getAttribute('aria-label') || '',
        anchor_title: a.getAttribute('title') || '',
        img_alt: img ? (img.getAttribute('alt') || '') : ''
      };
    })
    """)

    out = []
    already = set()

    for x in data:
        href = x.get("href", "")
        m = ITEM_ID_RE.search(href)

        if not m:
            continue

        item_id = m.group(1)

        if item_id in already:
            continue

        already.add(item_id)

        x["item_id"] = item_id
        x["url"] = href.split("?")[0]
        x["title"] = title_from_card(x)

        out.append(x)

    return out

async def verify_listing(page, url, fallback_title=""):
    """
    Ouvre uniquement une annonce déjà jugée intéressante
    pour vérifier son titre, son texte complet et le vendeur.
    """
    detail = None

    try:
        detail = await page.context.new_page()

        await detail.goto(
            url,
            wait_until="domcontentloaded",
            timeout=30000
        )

        await detail.wait_for_timeout(1200)

        # Titre de la page / annonce
        title = fallback_title

        try:
            og_title = await detail.locator(
                'meta[property="og:title"]'
            ).get_attribute("content")

            cleaned = clean_title(og_title or "")

            if cleaned:
                title = cleaned
        except Exception:
            pass

        # Texte complet visible de l'annonce
                # Texte pertinent de l'annonce uniquement.
        # On évite le footer et une grande partie
        # des annonces recommandées par Vinted.
        description_parts = []

        for selector in (
            'meta[property="og:description"]',
            'meta[name="description"]'
        ):
            try:
                value = await detail.locator(
                    selector
                ).first.get_attribute("content")

                if value:
                    description_parts.append(value)
            except Exception:
                pass

        try:
            main_text = await detail.locator(
                "main"
            ).inner_text(timeout=7000)

            if main_text:
                description_parts.append(
                    main_text[:5000]
                )
        except Exception:
            pass

        full_text = "\n".join(
            description_parts
        )[:7000]
        # Essaie de récupérer le pseudo vendeur
        seller = ""

        try:
            seller_links = detail.locator(
                'a[href*="/member/"]'
            )

            if await seller_links.count() > 0:
                seller = (
                    await seller_links.first.inner_text()
                ).strip()
        except Exception:
            pass
        image_url = ""

        try:
            image_url = (
                await detail.locator(
                    'meta[property="og:image"]'
                ).get_attribute("content")
                or ""
            )
        except Exception:
            pass
        return {
            "ok": True,
            "title": title,
            "text": full_text[:12000],
            "seller": seller,
            "image_url": image_url
        }
    except PlaywrightTimeoutError:
        return {
            "ok": False,
            "title": fallback_title,
            "text": "",
            "seller": "",
            "error": "timeout annonce"
        }

    except Exception as e:
        return {
            "ok": False,
            "title": fallback_title,
            "text": "",
            "seller": "",
            "error": str(e)[:100]
        }

    finally:
        if detail:
            try:
                await detail.close()
            except Exception:
                pass

def suspicious_price(rule, price):
    resale_low = rule.get("resale_low")

    if resale_low is None:
        return ""

    resale_low = float(resale_low)

    # Exemple :
    # console normalement revendable 180 EUR,
    # annonce à 35 EUR => on ne l'ignore pas,
    # mais on la classe "à vérifier".
    threshold = float(rule.get("suspicious_price_ratio", 0.38))

    if price <= resale_low * threshold:
        return "prix anormalement bas"

    return ""


async def scan_search(page, search, cfg, blacklist, seen_ids):
    base = cfg.get("base_url", "https://www.vinted.be").rstrip("/")
    query = search["query"]
    price_to = search.get("price_to")

    url = (
        f"{base}/catalog?"
        f"search_text={quote_plus(query)}"
        f"&order=newest_first"
    )

    if price_to is not None:
        url += f"&price_to={float(price_to):g}"

    print(f"\n[SCAN] {search['name']} -> {url}")

    try:
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=45000
        )

        await page.wait_for_timeout(
            int(cfg.get("page_wait_ms", 1800))
        )

        await page.locator(
            'a[href*="/items/"]'
        ).first.wait_for(timeout=12000)

    except PlaywrightTimeoutError:
        print("  ! Aucun résultat visible / contrôle Vinted possible.")
        return []

    cards = await extract_cards(page)

    new_alerts = []

    max_items = int(
        cfg.get("max_items_per_search", 40)
    )

    global_min_roi = float(
        cfg.get("min_roi_pct", 20)
    )

    global_min_demand = int(
        cfg.get("min_demand_score", 0)
    )

    for c in cards[:max_items]:

        if c["item_id"] in seen_ids:
            continue

        title = c["title"]
        text = c.get("text", "")

        price = parse_price(text)

        if price is None:
            continue

        # Protection contre une mauvaise extraction
        # qui récupérerait un autre prix présent sur la carte.
        if (
            price_to is not None
            and price > float(price_to) * 1.05
        ):
            continue

        blocked, blacklist_group, blacklist_hits, risks = (
            blacklist_check(
                title,
                text,
                blacklist
            )
        )

        if blocked:
            print(
                f"  X BLACKLIST [{blacklist_group}] "
                f"{title[:70]} | {blacklist_hits}"
            )
            continue

        matched_rule = None

        for rule in search.get("rules", []):
            if rule_match(rule, title, text):
                matched_rule = rule
                break

        if matched_rule is None:
            continue

        (
            total,
            resale_low,
            resale_high,
            margin_low,
            margin_high,
            roi_low
        ) = score_candidate(
            matched_rule,
            price,
            cfg
        )

        if margin_low is None:
            continue

        min_margin = float(
            matched_rule.get(
                "min_margin",
                cfg.get("min_margin", 25)
            )
        )

        min_roi = float(
            matched_rule.get(
                "min_roi_pct",
                global_min_roi
            )
        )

        demand_score = int(
            matched_rule.get(
                "demand_score",
                3
            )
        )

        if demand_score < global_min_demand:
            continue

        if margin_low < min_margin:
            continue

        if roi_low < min_roi:
            continue
        # DOUBLE VERIFICATION DE L'ANNONCE
        # On ouvre uniquement les candidats déjà rentables.
        detail = await verify_listing(
            page,
            c["url"],
            title
        )

        # Si Vinted empêche la vérification,
        # on préfère ne pas envoyer une fausse bonne affaire.
        if not detail.get("ok"):
            print(
                f"  ? VERIFICATION IMPOSSIBLE | "
                f"{title[:65]} | {c['url']}"
            )
            continue

        verified_title = detail.get("title") or title
        verified_text = detail.get("text") or text
        seller = detail.get("seller", "").strip()

        # Vérification blacklist sur le contenu complet
        deep_blocked, deep_group, deep_hits, deep_risks = (
            blacklist_check(
                verified_title,
                verified_text,
                blacklist
            )
        )

        if deep_blocked:
            print(
                f"  X REJET APRES VERIFICATION "
                f"[{deep_group}] "
                f"{verified_title[:65]} | "
                f"{deep_hits}"
            )
            continue

        # Vérification du vendeur blacklisté
        seller_blacklist = [
            norm(x)
            for x in blacklist.get(
                "seller_blacklist",
                []
            )
        ]

        if seller and norm(seller) in seller_blacklist:
            print(
                f"  X VENDEUR BLACKLISTE | "
                f"{seller} | {c['url']}"
            )
            continue

        # Le produit doit encore correspondre
        # au modèle recherché une fois la vraie annonce ouverte.
        if not rule_match(
            matched_rule,
            verified_title,
            verified_text
        ):
            print(
                f"  X MAUVAIS PRODUIT | "
                f"{verified_title[:70]}"
            )
            continue

        # On remplace les données approximatives de la carte
        # par les données vérifiées de l'annonce.
        title = verified_title
        text = verified_text

        # On conserve aussi les avertissements trouvés
        # dans la description complète.
        risks = list(
            dict.fromkeys(
                list(risks) + list(deep_risks)
            )
        )
        
        risk_messages = list(risks)

        abnormal = suspicious_price(
            matched_rule,
            price
        )

        if abnormal:
            risk_messages.append(abnormal)

        risk = ", ".join(
            dict.fromkeys(risk_messages)
        )

        row = {
            "timestamp": datetime.now().isoformat(
                timespec="seconds"
            ),
            "search": (
                search["name"]
                + " / "
                + matched_rule.get("label", "")
            ),
            "title": title,
            "image_url": detail.get("image_url", ""),
            "listing_price": round(price, 2),
            "total_buy_est": total,
            "resale_low": resale_low,
            "resale_high": resale_high,
            "margin_low": margin_low,
            "margin_high": margin_high,
            "roi_low": roi_low,
            "demand_score": demand_score,
            "risk": risk,
            "url": c["url"],
            "item_id": c["item_id"]
        }

        append_alert(row)

        new_alerts.append(row)

        ntfy_send(row)

        status = (
            "A VERIFIER"
            if risk
            else "BON PLAN"
        )

        print(
            f"  ★ {status} | "
            f"{title[:65]} | "
            f"{price:.2f} EUR | "
            f"marge +{margin_low:.2f} EUR | "
            f"ROI {roi_low:.0f}% | "
            f"demande {demand_score}/5"
        )

        print(
            f"    {c['url']}"
        )

    # On mémorise seulement les annonces
    # qui ont réellement déclenché une alerte.
    for row in new_alerts:
        seen_ids.add(
            row["item_id"]
        )

    return new_alerts


async def main():
    cfg = load_json(
        CONFIG_PATH,
        {}
    )

    blacklist = load_json(
        BLACKLIST_PATH,
        {}
    )

    if not cfg:
        print("config.json introuvable.")
        sys.exit(1)

    if not blacklist:
        print(
            "ATTENTION: blacklist.json "
            "introuvable ou vide."
        )

    seen_ids = set(
        load_json(
            SEEN_PATH,
            []
        )
    )

    one_shot = "--once" in sys.argv
    headless_arg = "--headless" in sys.argv

    print("Vinted Deal Scanner V2")
    print(
        "Mode anti faux-positifs + "
        "marge + ROI + demande + blacklist."
    )

    async with async_playwright() as p:

        env_headless = (
            os.getenv(
                "HEADLESS",
                ""
            ).strip().lower()
            in {
                "1",
                "true",
                "yes",
                "on"
            }
        )

        effective_headless = (
            headless_arg
            or env_headless
        )

        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=effective_headless,
            viewport={
                "width": 1280,
                "height": 900
            },
            locale="fr-BE",
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ]
        )

        page = (
            context.pages[0]
            if context.pages
            else await context.new_page()
        )

        while True:

            cycle_alerts = 0

            for search in cfg.get(
                "searches",
                []
            ):
                try:
                    alerts = await scan_search(
                        page,
                        search,
                        cfg,
                        blacklist,
                        seen_ids
                    )

                    cycle_alerts += len(
                        alerts
                    )

                    save_json(
                        SEEN_PATH,
                        sorted(seen_ids)
                    )

                    await asyncio.sleep(
                        float(
                            cfg.get(
                                "delay_between_searches",
                                1
                            )
                        )
                    )

                except Exception as e:
                    print(
                        f"  ! Erreur "
                        f"{search.get('name')}: "
                        f"{e}"
                    )

            print(
                f"\n[{datetime.now().strftime('%H:%M:%S')}] "
                f"Cycle terminé — "
                f"{cycle_alerts} nouveau(x) "
                f"bon(s) plan(s)."
            )

            save_json(
                SEEN_PATH,
                sorted(seen_ids)
            )

            if one_shot:
                break

            await asyncio.sleep(
                float(
                    cfg.get(
                        "poll_seconds",
                        300
                    )
                )
            )

        await context.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nArrêt demandé.")
