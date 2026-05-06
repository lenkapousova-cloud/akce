"""
Rossmann scraper — statický scraping
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

ROSSMANN_URLS = [
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

def run():
    log.info("=== Rossmann scraper START ===")
    deals = []
    seen = set()
    now = datetime.now(timezone.utc).isoformat()

    for url in ROSSMANN_URLS:
        try:
            log.info(f"Scraping: {url}")
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
                    deals.append({
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
            log.error(f"Chyba {url}: {e}")

    log.info(f"Rossmann celkem: {len(deals)} akcí")

    if deals:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        supabase.table("deals").delete().eq("store", "Rossmann").execute()
        for i in range(0, len(deals), 50):
            supabase.table("deals").insert(deals[i:i+50]).execute()
        log.info(f"Uloženo {len(deals)} akcí do Supabase")

    log.info("=== Rossmann scraper END ===")

if __name__ == "__main__":
    run()
