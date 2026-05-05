"""
Drogerie Akční Ceny Scraper — kupiapi + Rossmann scraping
==========================================================
kupiapi: scrape všech obchodů z kupi.cz (Lidl, Albert, Teta, DM, atd.)
Rossmann: přímý scraping webu
Nasazení: Render.com (free tier)
Spouštění: Automaticky 2× týdně (Po + Čt)
"""

import os
import re
import time
import logging
from datetime import datetime, timezone

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from bs4 import BeautifulSoup
from supabase import create_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://eifooaghbprllczieowj.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "cs-CZ,cs;q=0.9",
}

# Kategórie drogerie na kupi.cz
KUPI_CATEGORIES = [
    "drogerie",
    "krasa",
]


# ============================================================
# SUPABASE
# ============================================================

def save_deals(deals: list, store: str, category: str = "drogerie") -> int:
    if not deals:
        return 0
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    supabase.table("deals").delete().eq("store", store).execute()
    now = datetime.now(timezone.utc).isoformat()
    records = []
    for d in deals:
        if not d.get("name") or not d.get("price"):
            continue
        records.append({
            "name": str(d.get("name", ""))[:200],
            "store": store,
            "brand": d.get("brand"),
            "price": d.get("price"),
            "old_price": d.get("old_price"),
            "discount": d.get("discount"),
            "valid_from": d.get("valid_from"),
            "valid_to": d.get("valid_to"),
            "source_url": d.get("source_url"),
            "category": category,
            "updated_at": now,
        })
    if not records:
        return 0
    total = 0
    for i in range(0, len(records), 50):
        result = supabase.table("deals").insert(records[i:i+50]).execute()
        total += len(result.data) if result.data else 0
    log.info(f"Uloženo {total} akcí ({store})")
    return total


def save_all(deals: list) -> int:
    """Uloží všechny deals najednou (smažeme vše kromě Rossmann a vložíme)."""
    if not deals:
        return 0
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    # Smaž staré záznamy z kupi (ne Rossmann)
    supabase.table("deals").delete().neq("store", "Rossmann").execute()
    log.info("Smazány staré záznamy (ne-Rossmann)")
    total = 0
    for i in range(0, len(deals), 50):
        result = supabase.table("deals").insert(deals[i:i+50]).execute()
        total += len(result.data) if result.data else 0
    log.info(f"Uloženo celkem {total} akcí z kupi.cz")
    return total


# ============================================================
# KUPI.CZ SCRAPER
# ============================================================

def parse_price(text: str):
    if not text:
        return None
    text = text.replace("\xa0", "").replace(" ", "").replace(",", ".")
    m = re.search(r"(\d+\.?\d*)", text)
    return float(m.group(1)) if m else None


def scrape_kupi_category(category: str) -> list:
    """Scrapuje kupi.cz kategorii a vrátí seznam akčních produktů."""
    url = f"https://www.kupi.cz/slevy/{category}"
    deals = []
    page = 1

    while page <= 5:  # Max 5 stránek
        page_url = f"{url}?page={page}" if page > 1 else url
        try:
            log.info(f"Scraping kupi.cz: {page_url}")
            r = requests.get(page_url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            # Kupi.cz produktové dlaždice
            products = soup.select(
                "article.offer, div.offer, "
                "[class*='offer-item'], [class*='OfferItem'], "
                "[class*='product-item'], div[data-offer]"
            )

            if not products:
                # Zkus alternativní selektory
                products = soup.select("li[class*='item'], div[class*='item']")

            if not products:
                log.info(f"Kupi.cz {category} strana {page}: žádné produkty")
                break

            log.info(f"Kupi.cz {category} strana {page}: {len(products)} produktů")

            for product in products:
                try:
                    # Název
                    name_el = product.select_one(
                        "h2, h3, [class*='name'], [class*='title'], [class*='Name']"
                    )
                    name = name_el.get_text(strip=True) if name_el else None
                    if not name or len(name) < 2:
                        continue

                    # Obchod
                    store_el = product.select_one(
                        "[class*='store'], [class*='shop'], [class*='vendor'], "
                        "[class*='retailer'], img[alt]"
                    )
                    store = store_el.get("alt") or store_el.get_text(strip=True) if store_el else "Neuvedeno"

                    # Akční cena
                    price_el = product.select_one(
                        "[class*='action'], [class*='sale'], [class*='current'], "
                        "[class*='price-action'], [class*='akce']"
                    )
                    if not price_el:
                        price_el = product.select_one("[class*='price']")
                    price = parse_price(price_el.get_text()) if price_el else None
                    if not price or price <= 0:
                        continue

                    # Původní cena
                    old_el = product.select_one(
                        "s, del, [class*='original'], [class*='before'], [class*='old'], [class*='regular']"
                    )
                    old_price = parse_price(old_el.get_text()) if old_el else None
                    if old_price and old_price <= price:
                        old_price = None

                    # Sleva %
                    discount_el = product.select_one("[class*='discount'], [class*='percent'], [class*='saving']")
                    discount = None
                    if discount_el:
                        dm = re.search(r"(\d+)", discount_el.get_text())
                        discount = int(dm.group(1)) if dm else None
                    if not discount and old_price:
                        discount = round(((old_price - price) / old_price) * 100)

                    # Platnost
                    valid_el = product.select_one("[class*='valid'], [class*='date'], [class*='period']")
                    valid_to = None
                    if valid_el:
                        vm = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", valid_el.get_text())
                        if vm:
                            valid_to = f"{vm.group(3)}-{int(vm.group(2)):02d}-{int(vm.group(1)):02d}"

                    # Link
                    link_el = product.select_one("a[href]")
                    source_url = "https://www.kupi.cz" + link_el["href"] if link_el and link_el.get("href", "").startswith("/") else (link_el["href"] if link_el else url)

                    deals.append({
                        "name": name[:200],
                        "store": store,
                        "brand": name.split()[0] if name else None,
                        "price": price,
                        "old_price": old_price,
                        "discount": discount,
                        "valid_from": None,
                        "valid_to": valid_to,
                        "source_url": source_url,
                        "category": "drogerie",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    })

                except Exception as e:
                    log.debug(f"Chyba při parsování: {e}")
                    continue

            # Zkontroluj zda je další strana
            next_btn = soup.select_one("a[rel='next'], [class*='next'], [aria-label='Next']")
            if not next_btn:
                break
            page += 1
            time.sleep(1)

        except Exception as e:
            log.error(f"Kupi.cz chyba {page_url}: {e}")
            break

    return deals


# ============================================================
# ROSSMANN — přímý scraping
# ============================================================

def scrape_rossmann() -> list:
    URLS = [
        "https://www.rossmann.cz/akce-a-slevy",
        "https://www.rossmann.cz/dekorativni-kosmetika",
        "https://www.rossmann.cz/vlasova-kosmetika",
        "https://www.rossmann.cz/pletova-kosmetika",
        "https://www.rossmann.cz/pece-o-telo",
        "https://www.rossmann.cz/pece-o-zuby",
        "https://www.rossmann.cz/zdravi",
        "https://www.rossmann.cz/domacnost",
    ]

    def parse_price(text):
        if not text:
            return None
        text = text.replace("\xa0", "").replace(" ", "")
        m = re.search(r"(\d+)[,.](\d{2})", text)
        return float(f"{m.group(1)}.{m.group(2)}") if m else None

    all_deals = []
    seen = set()
    now = datetime.now(timezone.utc).isoformat()

    for url in URLS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=30, verify=False)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            for product in soup.select("div.product-tile"):
                try:
                    name_el = product.select_one("h2, h3, h4, [class*='name']")
                    name = name_el.get_text(strip=True) if name_el else None
                    if not name or name.lower()[:50] in seen:
                        continue
                    seen.add(name.lower()[:50])
                    price_el = product.select_one("[class*='sale'], [class*='price']")
                    price = parse_price(price_el.get_text()) if price_el else None
                    if not price:
                        continue
                    old_el = product.select_one("s, del, [class*='original']")
                    old_price = parse_price(old_el.get_text()) if old_el else None
                    if old_price and old_price <= price:
                        old_price = None
                    discount = round(((old_price - price) / old_price) * 100) if old_price else None
                    all_deals.append({
                        "name": name[:200],
                        "store": "Rossmann",
                        "brand": name.split()[0],
                        "price": price,
                        "old_price": old_price,
                        "discount": discount,
                        "valid_from": None,
                        "valid_to": None,
                        "source_url": url,
                        "category": "drogerie",
                        "updated_at": now,
                    })
                except Exception:
                    continue
            time.sleep(2)
        except Exception as e:
            log.error(f"Rossmann chyba {url}: {e}")

    log.info(f"Rossmann: {len(all_deals)} akcí")
    return all_deals


# ============================================================
# HLAVNÍ FUNKCE
# ============================================================

def run():
    log.info("=== Scraper START ===")

    # 1. Rossmann
    rossmann_deals = scrape_rossmann()
    if rossmann_deals:
        save_deals(rossmann_deals, "Rossmann", "drogerie")

    # 2. Kupi.cz — drogerie ze všech obchodů
    all_kupi = []
    for cat in KUPI_CATEGORIES:
        log.info(f"--- Kupi.cz kategorie: {cat} ---")
        deals = scrape_kupi_category(cat)
        all_kupi.extend(deals)
        log.info(f"Kupi.cz {cat}: {len(deals)} akcí")
        time.sleep(2)

    log.info(f"Kupi.cz celkem: {len(all_kupi)} akcí")
    if all_kupi:
        save_all(all_kupi)

    log.info(f"=== Scraper END — Rossmann: {len(rossmann_deals)}, Kupi: {len(all_kupi)} ===")


if __name__ == "__main__":
    run()
