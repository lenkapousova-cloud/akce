"""
Rossmann.cz Akční Ceny Scraper
==============================
Scrapuje akční nabídky z rossmann.cz a ukládá do Supabase.
Nasazení: Render.com (free tier)
Spouštění: Automaticky 2× týdně (Po + Čt) přes Render Cron Job
"""

import os
import re
import time
import logging
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client

# ============================================================
# KONFIGURACE — nastavte jako Environment Variables na Render
# ============================================================
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://eifooaghbprllczieowj.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")  # service_role key

# URL stránek k scrapování
ROSSMANN_URLS = [
    "https://www.rossmann.cz/obsah/akce-a-letaky",
    "https://www.rossmann.cz/category/tele/kosmetika-a-parfumerie",
    "https://www.rossmann.cz/category/tele/pece-o-telo",
    "https://www.rossmann.cz/category/tele/vlasova-kosmetika",
    "https://www.rossmann.cz/category/tele/prace-a-uklid",
    "https://www.rossmann.cz/category/tele/pece-o-zdravi",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "cs-CZ,cs;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ============================================================
# SUPABASE
# ============================================================

def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def save_deals(deals: list[dict]) -> int:
    """Uloží akce do Supabase. Vrátí počet uložených."""
    if not deals:
        return 0

    supabase = get_supabase()

    # Vymaž staré záznamy pro Rossmann
    supabase.table("deals").delete().eq("store", "Rossmann").execute()
    log.info("Smazány staré záznamy Rossmann")

    # Vlož nové
    result = supabase.table("deals").insert(deals).execute()
    count = len(result.data) if result.data else 0
    log.info(f"Uloženo {count} akcí do Supabase")
    return count


# ============================================================
# SCRAPING
# ============================================================

def parse_price(text: str) -> Optional[float]:
    """Parsuje cenu z textu, např. '49,90 Kč' -> 49.90"""
    if not text:
        return None
    match = re.search(r"(\d+)[,.](\d{2})", text.replace("\xa0", ""))
    if match:
        return float(f"{match.group(1)}.{match.group(2)}")
    match = re.search(r"(\d+)", text)
    if match:
        return float(match.group(1))
    return None


def scrape_rossmann_page(url: str) -> list[dict]:
    """Scrapuje jednu stránku Rossmann a vrátí seznam produktů."""
    deals = []

    try:
        log.info(f"Scraping: {url}")
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        # Rossmann používá různé CSS třídy — zkusíme více selektorů
        product_selectors = [
            "div.product-tile",
            "div.product-item",
            "article.product",
            "div[class*='product']",
            "li.product",
        ]

        products = []
        for selector in product_selectors:
            products = soup.select(selector)
            if products:
                log.info(f"Nalezeno {len(products)} produktů pomocí '{selector}'")
                break

        if not products:
            # Fallback — hledáme podle cen
            log.warning("Produkty nenalezeny přes CSS, zkouším fallback přes ceny")
            return scrape_rossmann_fallback(soup, url)

        for product in products:
            try:
                # Název produktu
                name_el = (
                    product.select_one("h2, h3, .product-name, .title, [class*='name']")
                )
                name = name_el.get_text(strip=True) if name_el else None
                if not name or len(name) < 2:
                    continue

                # Akční cena
                price_el = product.select_one(
                    ".price--sale, .sale-price, .action-price, "
                    "[class*='sale'], [class*='action'], [class*='akce']"
                )
                if not price_el:
                    price_el = product.select_one("[class*='price']")
                price = parse_price(price_el.get_text(strip=True)) if price_el else None

                if not price:
                    continue

                # Původní cena
                old_price_el = product.select_one(
                    ".price--original, .original-price, s, del, "
                    "[class*='original'], [class*='old'], [class*='before']"
                )
                old_price = parse_price(old_price_el.get_text(strip=True)) if old_price_el else None

                # Sleva
                discount = None
                if old_price and old_price > price:
                    discount = round(((old_price - price) / old_price) * 100)
                else:
                    old_price = None

                deals.append({
                    "name": name[:200],
                    "store": "Rossmann",
                    "price": price,
                    "old_price": old_price,
                    "discount": discount,
                    "category": "drogerie",
                    "updated_at": datetime.utcnow().isoformat(),
                })

            except Exception as e:
                log.debug(f"Chyba při parsování produktu: {e}")
                continue

    except requests.RequestException as e:
        log.error(f"HTTP chyba pro {url}: {e}")

    return deals


def scrape_rossmann_fallback(soup: BeautifulSoup, url: str) -> list[dict]:
    """
    Fallback scraper — hledá ceny v textu stránky.
    Používá se když CSS selektory nenajdou produkty.
    """
    deals = []
    text = soup.get_text(separator="\n")
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    for i, line in enumerate(lines):
        price_match = re.search(r"(\d+)[,.](\d{2})\s*(Kč|CZK)?", line)
        if not price_match:
            continue

        price = float(f"{price_match.group(1)}.{price_match.group(2)}")
        if price <= 0 or price > 10000:
            continue

        # Hledáme název v okolních řádcích
        name = None
        for offset in [-2, -1, 1, 2]:
            idx = i + offset
            if 0 <= idx < len(lines):
                candidate = lines[idx]
                if (len(candidate) > 4 and
                        not re.match(r"^\d+[,.]?\d*\s*(Kč|%|ks)?$", candidate) and
                        "rossmann" not in candidate.lower()):
                    name = candidate[:200]
                    break

        if not name:
            continue

        deals.append({
            "name": name,
            "store": "Rossmann",
            "price": price,
            "old_price": None,
            "discount": None,
            "category": "drogerie",
            "updated_at": datetime.utcnow().isoformat(),
        })

    log.info(f"Fallback nalezl {len(deals)} produktů")
    return deals


# ============================================================
# HLAVNÍ FUNKCE
# ============================================================

def run():
    log.info("=== Rossmann scraper START ===")
    all_deals = []

    for url in ROSSMANN_URLS:
        deals = scrape_rossmann_page(url)
        all_deals.extend(deals)
        log.info(f"  → {len(deals)} akcí z {url}")
        time.sleep(2)  # Pauza mezi požadavky

    # Deduplikace podle názvu
    seen = set()
    unique_deals = []
    for d in all_deals:
        key = d["name"].lower()[:50]
        if key not in seen:
            seen.add(key)
            unique_deals.append(d)

    log.info(f"Celkem unikátních akcí: {len(unique_deals)}")

    if unique_deals:
        saved = save_deals(unique_deals)
        log.info(f"Uloženo: {saved} akcí")
    else:
        log.warning("Žádné akce nenalezeny!")

    log.info("=== Rossmann scraper END ===")


if __name__ == "__main__":
    run()
