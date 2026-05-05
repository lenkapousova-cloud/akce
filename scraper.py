"""
Drogerie Akční Ceny Scraper — Rossmann + DM + Teta
===================================================
Scrapuje akční nabídky z rossmann.cz, dm.cz, teta.cz a ukládá do Supabase.
Nasazení: Render.com (free tier)
Spouštění: Automaticky 2× týdně (Po + Čt) přes Render Cron Job

Extrahuje: název, obchod, EAN, akční cena, původní cena, sleva %, platnost akce
"""

import os
import re
import time
import logging
from datetime import datetime, timezone
from typing import Optional

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
from supabase import create_client, Client

# ============================================================
# KONFIGURACE
# ============================================================
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://eifooaghbprllczieowj.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "cs-CZ,cs;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

DM_URLS = [
    "https://www.dm.cz/on/demandware.store/Sites-dm-CZ-Site/cs_CZ/Search-UpdateGrid?cgid=020000&start=0&sz=72",
    "https://www.dm.cz/kosmetika/parfemy/",
    "https://www.dm.cz/telo-a-koupel/telova-kosmetika/",
    "https://www.dm.cz/vlasova-kosmetika/sampony/",
    "https://www.dm.cz/dum-a-zahrada/",
    "https://www.dm.cz/zdravi-a-lekarnicka/",
]

ROSSMANN_URLS = [
    "https://www.rossmann.cz/akce-a-slevy",
    "https://www.rossmann.cz/dekorativni-kosmetika",
    "https://www.rossmann.cz/vlasova-kosmetika",
    "https://www.rossmann.cz/pece-o-plet-a-telo",
    "https://www.rossmann.cz/prace-a-uklid-v-domacnosti",
]

TETA_URLS = [
    "https://www.tetadrogerie.cz/akce",
    "https://www.tetadrogerie.cz/kosmetika",
    "https://www.tetadrogerie.cz/pece-o-telo",
    "https://www.tetadrogerie.cz/vlasy",
    "https://www.tetadrogerie.cz/cistici-prostredky",
    "https://www.tetadrogerie.cz/pece-o-zdravi",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ============================================================
# SUPABASE
# ============================================================

def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def save_deals(deals: list, store: str) -> int:
    if not deals:
        return 0
    supabase = get_supabase()
    supabase.table("deals").delete().eq("store", store).execute()
    log.info(f"Smazány staré záznamy {store}")
    # Vkládáme po dávkách 50 aby nedošlo k timeout
    total = 0
    for i in range(0, len(deals), 50):
        batch = deals[i:i+50]
        result = supabase.table("deals").insert(batch).execute()
        total += len(result.data) if result.data else 0
    log.info(f"Uloženo {total} akcí ({store}) do Supabase")
    return total


# ============================================================
# HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc).isoformat()


def parse_price(text: str) -> Optional[float]:
    if not text:
        return None
    text = text.replace("\xa0", "").replace(" ", "")
    match = re.search(r"(\d+)[,.](\d{2})", text)
    if match:
        return float(f"{match.group(1)}.{match.group(2)}")
    match = re.search(r"(\d+)", text)
    if match:
        return float(match.group(1))
    return None


def parse_ean(text: str) -> Optional[str]:
    """Extrahuje EAN kód (8 nebo 13 číslic)."""
    if not text:
        return None
    match = re.search(r"\b(\d{8}|\d{13})\b", text)
    return match.group(1) if match else None


def parse_date_cz(text: str) -> Optional[str]:
    """Parsuje česká data jako '1.5.2026', '01.05.2026', 'do 7.5.' → ISO formát."""
    if not text:
        return None
    # dd.mm.yyyy
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if match:
        try:
            return f"{match.group(3)}-{int(match.group(2)):02d}-{int(match.group(1)):02d}"
        except:
            pass
    # dd.mm. (bez roku — doplníme aktuální rok)
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.", text)
    if match:
        try:
            year = datetime.now().year
            return f"{year}-{int(match.group(2)):02d}-{int(match.group(1)):02d}"
        except:
            pass
    return None


def parse_validity(text: str):
    """Vrátí (valid_from, valid_to) z textu platnosti."""
    if not text:
        return None, None
    dates = re.findall(r"\d{1,2}\.\d{1,2}\.(?:\d{4})?", text)
    if len(dates) >= 2:
        return parse_date_cz(dates[0]), parse_date_cz(dates[1])
    elif len(dates) == 1:
        return None, parse_date_cz(dates[0])
    return None, None


def deduplicate(deals: list) -> list:
    seen = set()
    unique = []
    for d in deals:
        key = d["name"].lower()[:50]
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def make_deal(name, store, price, old_price=None, ean=None, valid_from=None, valid_to=None, brand=None, source_url=None):
    """Vytvoří standardizovaný slovník akce."""
    if old_price and old_price <= price:
        old_price = None
    discount = round(((old_price - price) / old_price) * 100) if old_price else None
    return {
        "name": name[:200],
        "store": store,
        "price": price,
        "old_price": old_price,
        "discount": discount,
        "ean": ean,
        "brand": brand,
        "source_url": source_url,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "category": "drogerie",
        "updated_at": now_utc(),
    }


# ============================================================
# ROSSMANN SCRAPER
# ============================================================

def scrape_rossmann_page(url: str) -> list:
    deals = []
    try:
        log.info(f"Scraping Rossmann: {url}")
        r = requests.get(url, headers=HEADERS, timeout=30, verify=False)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        products = soup.select("div.product-tile") or soup.select("div.product-item")

        if not products:
            log.warning(f"Rossmann: žádné produkty na {url}")
            return deals

        # Platnost akce — hledáme globálně na stránce
        validity_text = ""
        for el in soup.select("[class*='valid'], [class*='period'], [class*='platnost'], [class*='date']"):
            validity_text = el.get_text(strip=True)
            if re.search(r"\d{1,2}\.\d{1,2}", validity_text):
                break

        valid_from, valid_to = parse_validity(validity_text)

        for product in products:
            try:
                name_el = product.select_one("h2, h3, h4, .product-name, [class*='name'], [class*='title']")
                name = name_el.get_text(strip=True) if name_el else None
                if not name or len(name) < 2:
                    continue

                price_el = product.select_one("[class*='sale'], [class*='offer'], [class*='price'], [class*='Price']")
                price = parse_price(price_el.get_text(strip=True)) if price_el else None
                if not price:
                    continue

                old_price_el = product.select_one("s, del, [class*='original'], [class*='old'], [class*='before']")
                old_price = parse_price(old_price_el.get_text(strip=True)) if old_price_el else None

                # EAN — někdy v data atributech
                ean = None
                for attr in ["data-ean", "data-gtin", "data-id", "data-product-id"]:
                    val = product.get(attr, "")
                    ean = parse_ean(str(val))
                    if ean:
                        break

                # Pokus o extrakci značky (brand) — bývá v názvu nebo v atributu
                brand_el = product.select_one("[class*='brand'], [class*='manufacturer'], [class*='Brand']")
                brand = brand_el.get_text(strip=True) if brand_el else None
                if not brand and name:
                    # Zkusíme první slovo jako značku (např. "Dove sprchový gel" → "Dove")
                    brand = name.split()[0] if len(name.split()) > 1 else None

                deals.append(make_deal(name, "Rossmann", price, old_price, ean, valid_from, valid_to, brand=brand, source_url=url))
            except Exception as e:
                log.debug(f"Rossmann produkt chyba: {e}")

    except requests.RequestException as e:
        log.error(f"HTTP chyba Rossmann {url}: {e}")
    return deals



# ============================================================
# PLAYWRIGHT — pro JavaScript-rendered stránky (DM, Teta)
# ============================================================

def scrape_with_playwright(url: str, wait_selector: str = None, timeout: int = 15000) -> str:
    """Načte stránku přes headless Chromium a vrátí HTML."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=8000)
                except:
                    pass
            else:
                page.wait_for_timeout(3000)
            html = page.content()
            browser.close()
            return html
    except Exception as e:
        log.error(f"Playwright chyba pro {url}: {e}")
        return ""


# ============================================================
# DM SCRAPER
# ============================================================

def scrape_dm_page(url: str) -> list:
    deals = []
    try:
        log.info(f"Scraping DM: {url}")
        html = scrape_with_playwright(url, wait_selector="div.product-tile, li.product-grid-item")
        if not html:
            log.warning(f"DM: prázdná odpověď pro {url}")
            return deals
        soup = BeautifulSoup(html, "html.parser")

        # Platnost akce
        validity_text = ""
        for el in soup.select("[class*='valid'], [class*='period'], [class*='offer-period'], [class*='platnost']"):
            t = el.get_text(strip=True)
            if re.search(r"\d{1,2}\.\d{1,2}", t):
                validity_text = t
                break
        valid_from, valid_to = parse_validity(validity_text)

        product_selectors = [
            "div.product-tile", "li.product-grid-item",
            "div[class*='ProductTile']", "div[data-dmid]",
        ]
        products = []
        for sel in product_selectors:
            products = soup.select(sel)
            if products:
                log.info(f"DM: {len(products)} produktů pomocí '{sel}'")
                break

        if not products:
            log.warning(f"DM: žádné produkty na {url}")
            return deals

        for product in products:
            try:
                name_el = product.select_one("h2, h3, h4, [class*='name'], [class*='title'], [class*='Name']")
                name = name_el.get_text(strip=True) if name_el else None
                if not name or len(name) < 2:
                    continue

                price_el = product.select_one("[class*='offer'], [class*='sale'], [class*='price'], [class*='Price']")
                price = parse_price(price_el.get_text(strip=True)) if price_el else None
                if not price:
                    continue

                old_price_el = product.select_one("s, del, [class*='original'], [class*='old'], [class*='was']")
                old_price = parse_price(old_price_el.get_text(strip=True)) if old_price_el else None

                # EAN — DM často má v data atributech
                ean = None
                for attr in ["data-ean", "data-gtin", "data-articleid", "data-id"]:
                    val = product.get(attr, "")
                    ean = parse_ean(str(val))
                    if ean:
                        break

                brand_el = product.select_one("[class*='brand'], [class*='Brand'], [class*='manufacturer']")
                brand = brand_el.get_text(strip=True) if brand_el else None
                if not brand and name:
                    brand = name.split()[0] if len(name.split()) > 1 else None

                deals.append(make_deal(name, "DM", price, old_price, ean, valid_from, valid_to, brand=brand, source_url=url))
            except Exception as e:
                log.debug(f"DM produkt chyba: {e}")

    except requests.RequestException as e:
        log.error(f"HTTP chyba DM {url}: {e}")
    return deals


# ============================================================
# TETA SCRAPER
# ============================================================

def scrape_teta_page(url: str) -> list:
    deals = []
    try:
        log.info(f"Scraping Teta: {url}")
        html = scrape_with_playwright(url, wait_selector="div.product-tile, div.product-item, article.product")
        if not html:
            log.warning(f"Teta: prázdná odpověď pro {url}")
            return deals
        soup = BeautifulSoup(html, "html.parser")

        # Platnost akce
        validity_text = ""
        for el in soup.select("[class*='valid'], [class*='period'], [class*='platnost'], [class*='date'], [class*='akce']"):
            t = el.get_text(strip=True)
            if re.search(r"\d{1,2}\.\d{1,2}", t):
                validity_text = t
                break
        valid_from, valid_to = parse_validity(validity_text)

        product_selectors = [
            "div.product-tile", "div.product-item",
            "li.product", "div[class*='product']",
            "article[class*='product']",
        ]
        products = []
        for sel in product_selectors:
            products = soup.select(sel)
            if products:
                log.info(f"Teta: {len(products)} produktů pomocí '{sel}'")
                break

        if not products:
            log.warning(f"Teta: žádné produkty na {url}")
            return deals

        for product in products:
            try:
                name_el = product.select_one("h2, h3, h4, [class*='name'], [class*='title']")
                name = name_el.get_text(strip=True) if name_el else None
                if not name or len(name) < 2:
                    continue

                price_el = product.select_one("[class*='sale'], [class*='akce'], [class*='action'], [class*='price']")
                price = parse_price(price_el.get_text(strip=True)) if price_el else None
                if not price:
                    continue

                old_price_el = product.select_one("s, del, [class*='original'], [class*='before'], [class*='old']")
                old_price = parse_price(old_price_el.get_text(strip=True)) if old_price_el else None

                # EAN
                ean = None
                for attr in ["data-ean", "data-gtin", "data-id", "data-product-id"]:
                    val = product.get(attr, "")
                    ean = parse_ean(str(val))
                    if ean:
                        break

                brand_el = product.select_one("[class*='brand'], [class*='Brand'], [class*='manufacturer']")
                brand = brand_el.get_text(strip=True) if brand_el else None
                if not brand and name:
                    brand = name.split()[0] if len(name.split()) > 1 else None

                deals.append(make_deal(name, "Teta", price, old_price, ean, valid_from, valid_to, brand=brand, source_url=url))
            except Exception as e:
                log.debug(f"Teta produkt chyba: {e}")

    except requests.RequestException as e:
        log.error(f"HTTP chyba Teta {url}: {e}")
    return deals


# ============================================================
# HLAVNÍ FUNKCE
# ============================================================

def run():
    log.info("=== Drogerie scraper START ===")
    results = {}

    # ROSSMANN
    log.info("--- Scrapuji Rossmann ---")
    rossmann_deals = []
    for url in ROSSMANN_URLS:
        rossmann_deals.extend(scrape_rossmann_page(url))
        time.sleep(2)
    rossmann_unique = deduplicate(rossmann_deals)
    results["Rossmann"] = len(rossmann_unique)
    if rossmann_unique:
        save_deals(rossmann_unique, "Rossmann")

    # DM
    log.info("--- Scrapuji DM ---")
    dm_deals = []
    for url in DM_URLS:
        dm_deals.extend(scrape_dm_page(url))
        time.sleep(2)
    dm_unique = deduplicate(dm_deals)
    results["DM"] = len(dm_unique)
    if dm_unique:
        save_deals(dm_unique, "DM")
    else:
        log.warning("DM: žádné akce nenalezeny!")

    # TETA
    log.info("--- Scrapuji Teta ---")
    teta_deals = []
    for url in TETA_URLS:
        teta_deals.extend(scrape_teta_page(url))
        time.sleep(2)
    teta_unique = deduplicate(teta_deals)
    results["Teta"] = len(teta_unique)
    if teta_unique:
        save_deals(teta_unique, "Teta")
    else:
        log.warning("Teta: žádné akce nenalezeny!")

    log.info(f"=== Drogerie scraper END === {results}")


if __name__ == "__main__":
    run()
