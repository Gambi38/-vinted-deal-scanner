#!/usr/bin/env python3
import asyncio
import csv
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
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

PRICE_RE = re.compile(
    r"(?:(\d{1,4}(?:[.,]\d{1,2})?)\s*€|€\s*(\d{1,4}(?:[.,]\d{1,2})?))"
)
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
    out = []
    for w in words:
        nw = norm(w)
        if nw and nw in t:
            out.append(w)
    return out


def clean_title(value):
    value = re.sub(
        r"^(image|photo)\s+(de|of)\s+",
        "",
        (value or "").strip(),
        flags=re.I,
    )
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
    return (
        unquote(m.group(1)).replace("-", " ")
        if m
        else "Annonce Vinted"
    )


def blacklist_check(title, text, blacklist):
    combined = f"{title} {text}"

    for group in (
        "hard_blacklist",
        "fake_blacklist",
        "accessory_blacklist",
    ):
        hits = keyword_hits(
            combined,
            blacklist.get(group, []),
        )
        if hits:
            return True, group, hits[:3], []

    risks = keyword_hits(
        combined,
        blacklist.get("suspicious_words", []),
    )

    return False, "", [], risks[:3]


def rule_match(rule, title, text):
    title_n = norm(title)
    full_n = norm(f"{title} {text}")

    must = [
        norm(x)
        for x in rule.get("must_contain", [])
    ]
    any_kw = [
        norm(x)
        for x in rule.get("any_contain", [])
    ]
    hardware = [
        norm(x)
        for x in rule.get("hardware_any", [])
    ]
    excludes = [
        norm(x)
        for x in rule.get("exclude", [])
    ]

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
        return (
            round(total, 2),
            None,
            None,
            None,
            None,
            None,
        )

    low = float(low)
    high = float(high or low)

    margin_low = low - total
    margin_high = high - total
    roi = (
        margin_low / total * 100
        if total > 0
        else 0
    )

    return (
        round(total, 2),
        low,
        high,
        round(margin_low, 2),
        round(margin_high, 2),
        round(roi, 1),
    )


def extract_size(text):
    raw = text or ""

    patterns = [
        r"\bW\s?(\d{2})\s*[xX/ -]?\s*L\s?(\d{2})\b",
        r"\b(?:EU|EUR|taille|size)\s*[:\-]?\s*(3[4-9]|4[0-9]|5[0-2])\b",
        r"\b(3[4-9]|4[0-9]|5[0-2])\s*(?:EU|EUR)\b",
        r"\b(XXS|XS|S|M|L|XL|XXL|XXXL)\b",
    ]

    m = re.search(patterns[0], raw, flags=re.I)
    if m:
        return f"W{m.group(1)} L{m.group(2)}"

    for p in patterns[1:]:
        m = re.search(p, raw, flags=re.I)
        if m:
            return m.group(1).upper()

    return "?"


def condition_check(text, cfg, rule):
    bad_hits = keyword_hits(
        text,
        cfg.get("fatal_condition_words", []),
    )

    if not bad_hits:
        return True, [], []

    if rule.get("rare_collectible"):
        rare_hits = keyword_hits(
            text,
            cfg.get("rare_exception_words", []),
        )
        min_hits = int(
            cfg.get("rare_exception_min_hits", 2)
        )

        if len(rare_hits) >= min_hits:
            return True, bad_hits[:3], rare_hits[:3]

    return False, bad_hits[:3], []


def ignored_brand_check(text, cfg):
    hits = keyword_hits(
        text,
        cfg.get("ignored_brands", []),
    )
    return hits[:3]


def opportunity_score(
    price,
    reference_price,
    margin_low,
    motivation_hits,
    authenticity_risk=False,
    rare_condition=False,
):
    if not reference_price or reference_price <= 0:
        return 1

    ratio = price / reference_price

    if ratio <= 0.12:
        score = 10
    elif ratio <= 0.15:
        score = 9
    elif ratio <= 0.20:
        score = 8
    elif ratio <= 0.25:
        score = 7
    elif ratio <= 0.30:
        score = 6
    elif ratio <= 0.40:
        score = 5
    else:
        score = 3

    if margin_low >= 100:
        score += 1
    elif margin_low >= 70:
        score += 0.5

    if motivation_hits:
        score += 1

    if authenticity_risk:
        score -= 1

    if rare_condition:
        score -= 1

    score = int(round(score))
    return max(1, min(10, score))


def reason_text(
    price,
    reference_price,
    motivation_hits,
    authenticity_risk=False,
    rare_condition_hits=None,
):
    parts = []

    if reference_price:
        pct = price / reference_price * 100
        parts.append(
            f"prix a environ {pct:.0f}% de la reference prudente"
        )

    if motivation_hits:
        parts.append(
            "vendeur motive: "
            + ", ".join(motivation_hits[:2])
        )

    if authenticity_risk:
        parts.append(
            "authenticite a verifier"
        )

    if rare_condition_hits:
        parts.append(
            "etat atypique tolere seulement car piece rare/vintage"
        )

    if not parts:
        return "rapport achat/revente interessant"

    return "; ".join(parts)


def ntfy_send(row):
    topic = os.getenv("NTFY_TOPIC", "").strip()

    if not topic:
        return False

    server = os.getenv(
        "NTFY_SERVER",
        "https://ntfy.sh",
    ).rstrip("/")

    url = (
        f"{server}/"
        f"{urllib.parse.quote(topic, safe='')}"
    )

    # ASCII uniquement dans les headers HTTP.
    title = (
        f"Vinted Deal {row['opportunity_score']}/10"
    )

    size = row.get("size") or "?"

    body = (
        f"🔥 Score d'opportunité : "
        f"{row['opportunity_score']}/10\n"
        f"📦 L'Article : "
        f"{row.get('brand','?')} - "
        f"{row.get('model','?')} - "
        f"{size}\n"
        f"💰 Prix Achat : "
        f"{row['listing_price']:.2f} €\n"
        f"📈 Prix Revente Estimé : "
        f"{row['resale_low']:.0f}-"
        f"{row['resale_high']:.0f} €\n"
        f"💎 La Raison : "
        f"{row['reason']}\n"
        f"🔗 Lien : {row['url']}"
    )

    headers = {
        "Title": title,
        "Priority": (
            "high"
            if row["opportunity_score"] >= 8
            else "default"
        ),
        "Tags": "moneybag,shopping_cart",
        "Click": row["url"],
        "Actions": (
            f"view, Ouvrir Vinted, {row['url']}"
        ),
    }

    if row.get("image_url"):
        headers["Attach"] = row["image_url"]

    try:
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            method="POST",
            headers=headers,
        )

        with urllib.request.urlopen(
            req,
            timeout=15,
        ) as resp:
            return 200 <= resp.status < 300

    except Exception as e:
        print(f"  ! ntfy: {e}")
        return False


def append_alert(row):
    fields = [
        "timestamp",
        "category",
        "search",
        "brand",
        "model",
        "size",
        "opportunity_score",
        "title",
        "listing_price",
        "total_buy_est",
        "resale_low",
        "resale_high",
        "margin_low",
        "margin_high",
        "roi_low",
        "reason",
        "url",
        "item_id",
    ]

    new = not ALERTS_CSV.exists()

    with ALERTS_CSV.open(
        "a",
        newline="",
        encoding="utf-8-sig",
    ) as f:
        w = csv.DictWriter(
            f,
            fieldnames=fields,
        )

        if new:
            w.writeheader()

        w.writerow(
            {
                k: row.get(k, "")
                for k in fields
            }
        )


async def extract_cards(page):
    data = await page.locator(
        'a[href*="/items/"]'
    ).evaluate_all(
        """
        els => els.map(a => {
          let node = a;
          let text = (a.innerText || '').trim();

          for (
            let i = 0;
            i < 5 && node;
            i++, node = node.parentElement
          ) {
            const t = (node.innerText || '').trim();
            if (
              t.length > text.length
              && t.length < 1200
            ) {
              text = t;
            }
          }

          const img = a.querySelector('img');

          return {
            href: a.href || '',
            text: text,
            anchor_text: (a.innerText || '').trim(),
            aria_label: a.getAttribute('aria-label') || '',
            anchor_title: a.getAttribute('title') || '',
            img_alt: img
              ? (img.getAttribute('alt') || '')
              : ''
          };
        })
        """
    )

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


async def verify_listing(
    page,
    url,
    fallback_title="",
):
    detail = None

    try:
        detail = await page.context.new_page()

        await detail.goto(
            url,
            wait_until="domcontentloaded",
            timeout=30000,
        )

        await detail.wait_for_timeout(1200)

        title = fallback_title

        try:
            og_title = await detail.locator(
                'meta[property="og:title"]'
            ).get_attribute("content")

            cleaned = clean_title(
                og_title or ""
            )

            if cleaned:
                title = cleaned

        except Exception:
            pass

        description_parts = []

        for selector in (
            'meta[property="og:description"]',
            'meta[name="description"]',
        ):
            try:
                value = await detail.locator(
                    selector
                ).first.get_attribute("content")

                if value:
                    description_parts.append(
                        value
                    )
            except Exception:
                pass

        try:
            main_text = await detail.locator(
                "main"
            ).inner_text(timeout=7000)

            if main_text:
                description_parts.append(
                    main_text[:4500]
                )

        except Exception:
            pass

        full_text = "\n".join(
            description_parts
        )[:7000]

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

        detail_price = None

        for selector, attr in (
            ('meta[property="product:price:amount"]', "content"),
            ('meta[itemprop="price"]', "content"),
            ('[itemprop="price"]', "content"),
        ):
            try:
                locator = detail.locator(
                    selector
                ).first

                value = await locator.get_attribute(
                    attr
                )

                if value:
                    value = value.replace(
                        ",",
                        ".",
                    )
                    m = re.search(
                        r"\d+(?:\.\d{1,2})?",
                        value,
                    )

                    if m:
                        detail_price = float(
                            m.group(0)
                        )
                        break

            except Exception:
                pass

        return {
            "ok": True,
            "title": title,
            "text": full_text,
            "seller": seller,
            "image_url": image_url,
            "price": detail_price,
        }

    except PlaywrightTimeoutError:
        return {
            "ok": False,
            "title": fallback_title,
            "text": "",
            "seller": "",
            "image_url": "",
            "price": None,
            "error": "timeout annonce",
        }

    except Exception as e:
        return {
            "ok": False,
            "title": fallback_title,
            "text": "",
            "seller": "",
            "image_url": "",
            "price": None,
            "error": str(e)[:120],
        }

    finally:
        if detail:
            try:
                await detail.close()
            except Exception:
                pass


def suspicious_price(rule, price):
    resale_low = rule.get(
        "resale_low"
    )

    if resale_low is None:
        return ""

    resale_low = float(
        resale_low
    )

    threshold = float(
        rule.get(
            "suspicious_price_ratio",
            0.18,
        )
    )

    if price <= resale_low * threshold:
        return "prix anormalement bas"

    return ""


async def scan_search(
    page,
    search,
    cfg,
    blacklist,
    seen_ids,
):
    base = cfg.get(
        "base_url",
        "https://www.vinted.be",
    ).rstrip("/")

    query = search["query"]
    price_to = search.get(
        "price_to"
    )

    url = (
        f"{base}/catalog?"
        f"search_text={quote_plus(query)}"
        f"&order=newest_first"
    )

    if price_to is not None:
        url += (
            f"&price_to="
            f"{float(price_to):g}"
        )

    print(
        f"\n[SCAN] "
        f"{search['name']} -> {url}"
    )

    try:
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=45000,
        )

        await page.wait_for_timeout(
            int(
                cfg.get(
                    "page_wait_ms",
                    1800,
                )
            )
        )

        await page.locator(
            'a[href*="/items/"]'
        ).first.wait_for(
            timeout=12000
        )

    except PlaywrightTimeoutError:
        print(
            "  ! Aucun résultat visible "
            "/ contrôle Vinted possible."
        )
        return []

    cards = await extract_cards(
        page
    )

    new_alerts = []

    max_items = int(
        cfg.get(
            "max_items_per_search",
            40,
        )
    )

    global_min_roi = float(
        cfg.get(
            "min_roi_pct",
            20,
        )
    )

    global_min_demand = int(
        cfg.get(
            "min_demand_score",
            0,
        )
    )

    for c in cards[:max_items]:
        if c["item_id"] in seen_ids:
            continue

        title = c["title"]
        text = c.get(
            "text",
            "",
        )

        ignored_hits = ignored_brand_check(
            f"{title} {text}",
            cfg,
        )

        if ignored_hits:
            print(
                f"  X MARQUE IGNORÉE | "
                f"{title[:65]} | "
                f"{ignored_hits}"
            )
            continue

        price = parse_price(
            text
        )

        if price is None:
            continue

        if (
            price_to is not None
            and price
            > float(price_to) * 1.05
        ):
            continue

        blocked, group, hits, risks = (
            blacklist_check(
                title,
                text,
                blacklist,
            )
        )

        if blocked:
            print(
                f"  X BLACKLIST "
                f"[{group}] "
                f"{title[:65]} | "
                f"{hits}"
            )
            continue

        matched_rule = None

        for rule in search.get(
            "rules",
            [],
        ):
            if rule_match(
                rule,
                title,
                text,
            ):
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
            roi_low,
        ) = score_candidate(
            matched_rule,
            price,
            cfg,
        )

        if margin_low is None:
            continue

        reference_price = float(
            matched_rule.get(
                "market_avg",
                resale_low,
            )
        )

        max_buy_ratio = (
            matched_rule.get(
                "max_buy_ratio"
            )
        )

        if (
            max_buy_ratio is not None
            and reference_price > 0
            and price
            > reference_price
            * float(max_buy_ratio)
        ):
            continue

        min_margin = float(
            matched_rule.get(
                "min_margin",
                cfg.get(
                    "min_margin",
                    25,
                ),
            )
        )

        min_roi = float(
            matched_rule.get(
                "min_roi_pct",
                global_min_roi,
            )
        )

        demand_score = int(
            matched_rule.get(
                "demand_score",
                3,
            )
        )

        if (
            demand_score
            < global_min_demand
        ):
            continue

        if margin_low < min_margin:
            continue

        if roi_low < min_roi:
            continue

        detail = await verify_listing(
            page,
            c["url"],
            title,
        )

        if not detail.get("ok"):
            print(
                f"  ? VERIFICATION "
                f"IMPOSSIBLE | "
                f"{title[:60]} | "
                f"{c['url']}"
            )
            continue

        verified_title = (
            detail.get("title")
            or title
        )

        verified_text = (
            detail.get("text")
            or text
        )

        seller = (
            detail.get(
                "seller",
                "",
            )
            .strip()
        )

        actual_price = detail.get(
            "price"
        )

        if (
            actual_price is not None
            and actual_price > 0
            and (
                price_to is None
                or actual_price
                <= float(price_to) * 1.10
            )
        ):
            price = actual_price

            (
                total,
                resale_low,
                resale_high,
                margin_low,
                margin_high,
                roi_low,
            ) = score_candidate(
                matched_rule,
                price,
                cfg,
            )

            reference_price = float(
                matched_rule.get(
                    "market_avg",
                    resale_low,
                )
            )

        deep_blocked, deep_group, deep_hits, deep_risks = (
            blacklist_check(
                verified_title,
                verified_text,
                blacklist,
            )
        )

        if deep_blocked:
            print(
                f"  X REJET APRES "
                f"VERIFICATION "
                f"[{deep_group}] "
                f"{verified_title[:60]} | "
                f"{deep_hits}"
            )
            continue

        ignored_hits = ignored_brand_check(
            f"{verified_title} "
            f"{verified_text}",
            cfg,
        )

        if ignored_hits:
            print(
                f"  X MARQUE IGNORÉE "
                f"APRES VERIFICATION | "
                f"{ignored_hits}"
            )
            continue

        condition_ok, bad_condition_hits, rare_hits = (
            condition_check(
                f"{verified_title} "
                f"{verified_text}",
                cfg,
                matched_rule,
            )
        )

        if not condition_ok:
            print(
                f"  X ETAT REDHIBITOIRE | "
                f"{verified_title[:60]} | "
                f"{bad_condition_hits}"
            )
            continue

        seller_blacklist = [
            norm(x)
            for x in blacklist.get(
                "seller_blacklist",
                [],
            )
        ]

        if (
            seller
            and norm(seller)
            in seller_blacklist
        ):
            print(
                f"  X VENDEUR "
                f"BLACKLISTE | "
                f"{seller}"
            )
            continue

        if not rule_match(
            matched_rule,
            verified_title,
            verified_text,
        ):
            print(
                f"  X MAUVAIS PRODUIT | "
                f"{verified_title[:65]}"
            )
            continue

        if (
            max_buy_ratio is not None
            and reference_price > 0
            and price
            > reference_price
            * float(max_buy_ratio)
        ):
            continue

        if margin_low < min_margin:
            continue

        if roi_low < min_roi:
            continue

        title = verified_title
        text = verified_text

        risks = list(
            dict.fromkeys(
                list(risks)
                + list(deep_risks)
            )
        )

        motivation_hits = keyword_hits(
            f"{title} {text}",
            cfg.get(
                "seller_motivation_words",
                [],
            ),
        )

        rare_condition = bool(
            bad_condition_hits
            and rare_hits
        )

        score = opportunity_score(
            price,
            reference_price,
            margin_low,
            motivation_hits,
            authenticity_risk=bool(
                matched_rule.get(
                    "authenticity_risk"
                )
            ),
            rare_condition=rare_condition,
        )

        reason = reason_text(
            price,
            reference_price,
            motivation_hits,
            authenticity_risk=bool(
                matched_rule.get(
                    "authenticity_risk"
                )
            ),
            rare_condition_hits=(
                bad_condition_hits
                if rare_condition
                else None
            ),
        )

        abnormal = suspicious_price(
            matched_rule,
            price,
        )

        if abnormal:
            reason += (
                "; prix extrêmement bas, "
                "verifier vendeur et authenticite"
            )

        size = extract_size(
            f"{title} {text}"
        )

        row = {
            "timestamp": datetime.now().isoformat(
                timespec="seconds"
            ),
            "category": search.get(
                "category",
                "",
            ),
            "search": (
                search["name"]
                + " / "
                + matched_rule.get(
                    "label",
                    "",
                )
            ),
            "brand": matched_rule.get(
                "brand",
                search.get(
                    "name",
                    "",
                ),
            ),
            "model": matched_rule.get(
                "model",
                matched_rule.get(
                    "label",
                    "",
                ),
            ),
            "size": size,
            "opportunity_score": score,
            "title": title,
            "image_url": detail.get(
                "image_url",
                "",
            ),
            "listing_price": round(
                price,
                2,
            ),
            "total_buy_est": total,
            "resale_low": resale_low,
            "resale_high": resale_high,
            "margin_low": margin_low,
            "margin_high": margin_high,
            "roi_low": roi_low,
            "demand_score": demand_score,
            "risk": ", ".join(
                dict.fromkeys(risks)
            ),
            "reason": reason,
            "url": c["url"],
            "item_id": c["item_id"],
        }

        append_alert(
            row
        )

        new_alerts.append(
            row
        )

        ntfy_send(
            row
        )

        print(
            f"  ★ SCORE {score}/10 | "
            f"{title[:58]} | "
            f"{price:.2f} EUR | "
            f"marge +{margin_low:.2f} EUR"
        )

        print(
            f"    {c['url']}"
        )

    for row in new_alerts:
        seen_ids.add(
            row["item_id"]
        )

    return new_alerts


async def main():
    cfg = load_json(
        CONFIG_PATH,
        {},
    )

    blacklist = load_json(
        BLACKLIST_PATH,
        {},
    )

    if not cfg:
        print(
            "config.json introuvable."
        )
        sys.exit(1)

    if not blacklist:
        print(
            "ATTENTION: blacklist.json "
            "introuvable ou vide."
        )

    seen_ids = set(
        load_json(
            SEEN_PATH,
            [],
        )
    )

    one_shot = (
        "--once"
        in sys.argv
    )

    headless_arg = (
        "--headless"
        in sys.argv
    )

    print(
        "Vinted Deal Scanner V3"
    )

    print(
        "Mode opportunites: "
        "prix/revente + marge + "
        "etat + vendeur motive + "
        "anti faux-positifs."
    )

    async with async_playwright() as p:
        env_headless = (
            os.getenv(
                "HEADLESS",
                "",
            )
            .strip()
            .lower()
            in {
                "1",
                "true",
                "yes",
                "on",
            }
        )

        effective_headless = (
            headless_arg
            or env_headless
        )

        context = (
            await p.chromium.launch_persistent_context(
                user_data_dir=str(
                    PROFILE_DIR
                ),
                headless=effective_headless,
                viewport={
                    "width": 1280,
                    "height": 900,
                },
                locale="fr-BE",
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
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
                [],
            ):
                try:
                    alerts = await scan_search(
                        page,
                        search,
                        cfg,
                        blacklist,
                        seen_ids,
                    )

                    cycle_alerts += len(
                        alerts
                    )

                    save_json(
                        SEEN_PATH,
                        sorted(
                            seen_ids
                        ),
                    )

                    await asyncio.sleep(
                        float(
                            cfg.get(
                                "delay_between_searches",
                                1,
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
                f"\n["
                f"{datetime.now().strftime('%H:%M:%S')}"
                f"] Cycle termine — "
                f"{cycle_alerts} "
                f"nouvelle(s) alerte(s)."
            )

            save_json(
                SEEN_PATH,
                sorted(
                    seen_ids
                ),
            )

            if one_shot:
                break

            await asyncio.sleep(
                float(
                    cfg.get(
                        "poll_seconds",
                        300,
                    )
                )
            )

        await context.close()


if __name__ == "__main__":
    try:
        asyncio.run(
            main()
        )
    except KeyboardInterrupt:
        print(
            "\nArret demande."
        )
