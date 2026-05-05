"""
Drogerie Akční Ceny Scraper — Claude API + Web Search
======================================================
Používá Claude API s web_search nástrojem k nalezení akčních cen.
Žádný scraping, žádný Playwright — jen AI + web search.
Nasazení: Render.com (free tier)
Spouštění: Automaticky 2× týdně (Po + Čt) přes Render Cron Job
"""

import os
import json
import time
import logging
from datetime import datetime, timezone

import requests
from supabase import create_client

# ============================================================
# KONFIGURACE
# ============================================================
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://eifooaghbprllczieowj.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")

HEADERS_ANTHROPIC = {
    "Content-Type": "application/json",
    "x-api-key": ANTHROPIC_KEY,
    "anthropic-version": "2023-06-01",
}

# Obchody k prohledání
STORES = [
    {
        "name": "DM",
        "query": "Jdi na https://www.dm.cz/akce/ a najdi POUZE akční drogerii: prací prostředky, kosmetiku, péči o tělo, vlasy, zuby, hygienické potřeby. Uveď název produktu, gramáž, akční cenu a původní cenu.",
        "category": "drogerie",
    },
    {
        "name": "Lidl",
        "query": "Jdi na https://www.lidl.cz/aktualni-letak a najdi POUZE drogerii se slevou: prací prostředky, čisticí prostředky, kosmetiku, sprchové gely, šampóny, zubní pasty, toaletní papír, plenky. Neuveď potraviny ani oblečení.",
        "category": "drogerie",
    },
    {
        "name": "Albert",
        "query": "Jdi na https://www.albert.cz/letaky a najdi POUZE akční drogerii se slevou: prací prostředky, čisticí prostředky, kosmetiku, hygienu, péči o tělo a vlasy. Neuveď potraviny.",
        "category": "drogerie",
    },
    {
        "name": "Kaufland",
        "query": "Jdi na https://www.kaufland.cz/akce a najdi POUZE akční drogerii se slevou: prací prostředky, čisticí prostředky, kosmetiku, hygienu, péči o tělo. Neuveď potraviny.",
        "category": "drogerie",
    },
    {
        "name": "BILLA",
        "query": "Jdi na https://www.billa.cz/letaky a najdi POUZE akční drogerii se slevou: prací prostředky, čisticí prostředky, kosmetiku, hygienu. Neuveď potraviny.",
        "category": "drogerie",
    },
    {
        "name": "Penny",
        "query": "Jdi na https://www.penny.cz/letak a najdi POUZE akční drogerii se slevou: prací prostředky, čisticí prostředky, kosmetiku, hygienu. Neuveď potraviny.",
        "category": "drogerie",
    },
    {
        "name": "Tesco",
        "query": "Jdi na https://www.tesco.com/cs-CZ/zones/letaky a najdi POUZE akční drogerii se slevou: prací prostředky, čisticí prostředky, kosmetiku, hygienu. Neuveď potraviny.",
        "category": "drogerie",
    },
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ============================================================
# CLAUDE API
# ============================================================

def ask_claude(store_name: str, query: str, category: str = "potraviny") -> list:
    """Zavolá Claude API s web search a vrátí seznam akcí jako JSON."""

    system_prompt = """Jsi expert na české akční ceny DROGERIE. Vyhledej aktuální akce a vrať POUZE validní JSON pole.

DEFINICE AKCE — produkt musí splňovat ALESPOŇ JEDNO z těchto kritérií:
1. Má přeškrtnutou původní cenu a nižší akční cenu (klasická sleva)
2. Je v akčním letáku obchodu (týdenní/měsíční leták)
3. Je označen jako "Akce", "Sleva", "Výhodná cena", "Týdenní nabídka", "Super cena"
4. Má časově omezenou cenu (platí jen do určitého data)
5. Je v sekci "Akce a slevy" na webu obchodu
6. Má badge/štítek se slevou v % nebo Kč

HLEDEJ POUZE TYTO KATEGORIE DROGERIE:
- Prací prostředky (prací prášky, gely, kapsle, aviváže)
- Čisticí prostředky (na nádobí, podlahy, koupelnu, WC)
- Přípravky do myčky
- Kosmetika (make-up, rtěnky, řasenky, oční stíny, základy)
- Péče o pleť (krémy, séra, masky, micelární vody)
- Péče o tělo (sprchové gely, tělová mléka, deodoranty, mýdla)
- Vlasová kosmetika (šampóny, kondicionéry, masky, laky)
- Péče o zuby (zubní pasty, kartáčky, ústní vody)
- Hygienické potřeby (toaletní papír, papírové kapesníky, vlhčené ubrousky)
- Epilace, depilace a holení (žiletky, krémy, vosk)
- Pro miminka a maminky (plenky, dětské krémy, šampóny)
- Zdraví (vitamíny, doplňky stravy, náplasti)
- Úklidové pomůcky (houby, mopy, rukavice)
- Vonné produkty (svíčky, osvěžovače vzduchu)

CO NENÍ AKCE — nezahrnuj:
- Potraviny, nápoje, alkohol
- Oblečení, textil
- Elektronika, hračky
- Běžné produkty bez označení slevy

Formát každého produktu:
{
  "name": "název produktu včetně gramáže/množství",
  "brand": "značka produktu",
  "price": 29.90,
  "old_price": 49.90,
  "discount": 40,
  "valid_from": "2026-05-05",
  "valid_to": "2026-05-11",
  "source_url": "https://..."
}

Pravidla výstupu:
- Vrať POUZE JSON pole [...], žádný jiný text, žádné markdown bloky
- Ceny jako čísla s desetinnou tečkou (ne string)
- discount jako celé číslo bez % (např. 40, ne "40%")
- Datumy ve formátu YYYY-MM-DD, pokud nejsou známy dej null
- Uveď 15-25 produktů
- Pokud nenajdeš žádné akce dle definice výše, vrať []"""

    payload = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 4096,
        "system": system_prompt,
        "messages": [{
            "role": "user",
            "content": f"{query} Vrať výsledky jako JSON pole."
        }],
        "tools": [{
            "type": "web_search_20250305",
            "name": "web_search"
        }]
    }

    try:
        log.info(f"Volám Claude API pro {store_name}...")
        # Retry při rate limitu
        for attempt in range(3):
            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=HEADERS_ANTHROPIC,
                json=payload,
                timeout=60
            )
            if response.status_code == 429:
                wait = 30 * (attempt + 1)
                log.warning(f"{store_name}: rate limit, čekám {wait}s (pokus {attempt+1}/3)")
                time.sleep(wait)
                continue
            response.raise_for_status()
            break
        data = response.json()

        # Extrahuj text z odpovědi
        text_parts = [
            block["text"]
            for block in data.get("content", [])
            if block.get("type") == "text"
        ]
        text = "\n".join(text_parts).strip()

        if not text:
            log.warning(f"{store_name}: prázdná odpověď")
            return []

        # Parsuj JSON
        # Najdi JSON pole v textu
        start = text.find("[")
        end = text.rfind("]") + 1
        if start == -1 or end == 0:
            log.warning(f"{store_name}: JSON pole nenalezeno v odpovědi")
            log.debug(f"Odpověď: {text[:200]}")
            return []

        json_str = text[start:end]
        deals = json.loads(json_str)

        if not isinstance(deals, list):
            log.warning(f"{store_name}: odpověď není pole")
            return []

        log.info(f"{store_name}: nalezeno {len(deals)} akcí")
        return deals

    except json.JSONDecodeError as e:
        log.error(f"{store_name}: chyba parsování JSON: {e}")
        return []
    except requests.RequestException as e:
        log.error(f"{store_name}: HTTP chyba: {e}")
        return []
    except Exception as e:
        log.error(f"{store_name}: neočekávaná chyba: {e}")
        return []


# ============================================================
# SUPABASE
# ============================================================

def save_deals(deals: list, store: str, category: str = "potraviny") -> int:
    if not deals:
        log.warning(f"{store}: žádné akce k uložení")
        return 0

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Vymaž staré záznamy
    supabase.table("deals").delete().eq("store", store).execute()
    log.info(f"Smazány staré záznamy {store}")

    # Připrav záznamy
    now = datetime.now(timezone.utc).isoformat()
    records = []
    for d in deals:
        if not d.get("name") or not d.get("price"):
            continue
        records.append({
            "name": str(d.get("name", ""))[:200],
            "store": store,
            "brand": d.get("brand"),
            "price": float(d["price"]) if d.get("price") else None,
            "old_price": float(d["old_price"]) if d.get("old_price") else None,
            "discount": int(d["discount"]) if d.get("discount") else None,
            "valid_from": d.get("valid_from"),
            "valid_to": d.get("valid_to"),
            "source_url": d.get("source_url"),
            "category": category,
            "updated_at": now,
        })

    if not records:
        return 0

    # Vlož po dávkách
    total = 0
    for i in range(0, len(records), 50):
        batch = records[i:i+50]
        result = supabase.table("deals").insert(batch).execute()
        total += len(result.data) if result.data else 0

    log.info(f"Uloženo {total} akcí ({store})")
    return total


# ============================================================
# ROSSMANN — statický scraper (funguje bez JS)
# ============================================================

def scrape_rossmann() -> list:
    """Rossmann funguje se statickým scrapingem — necháme jak je."""
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    from bs4 import BeautifulSoup
    import re

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

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "cs-CZ,cs;q=0.9",
    }

    def parse_price(text):
        if not text:
            return None
        text = text.replace("\xa0", "").replace(" ", "")
        m = re.search(r"(\d+)[,.](\d{2})", text)
        if m:
            return float(f"{m.group(1)}.{m.group(2)}")
        return None

    all_deals = []
    seen = set()
    now = datetime.now(timezone.utc).isoformat()

    for url in ROSSMANN_URLS:
        try:
            log.info(f"Scraping Rossmann: {url}")
            r = requests.get(url, headers=HEADERS, timeout=30, verify=False)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            products = soup.select("div.product-tile")
            if not products:
                continue

            for product in products:
                try:
                    name_el = product.select_one("h2, h3, h4, [class*='name'], [class*='title']")
                    name = name_el.get_text(strip=True) if name_el else None
                    if not name or len(name) < 2:
                        continue

                    key = name.lower()[:50]
                    if key in seen:
                        continue
                    seen.add(key)

                    price_el = product.select_one("[class*='sale'], [class*='offer'], [class*='price']")
                    price = parse_price(price_el.get_text(strip=True)) if price_el else None
                    if not price:
                        continue

                    old_el = product.select_one("s, del, [class*='original'], [class*='old']")
                    old_price = parse_price(old_el.get_text(strip=True)) if old_el else None
                    if old_price and old_price <= price:
                        old_price = None

                    discount = round(((old_price - price) / old_price) * 100) if old_price else None

                    brand_el = product.select_one("[class*='brand'], [class*='Brand']")
                    brand = brand_el.get_text(strip=True) if brand_el else (name.split()[0] if name else None)

                    all_deals.append({
                        "name": name[:200],
                        "store": "Rossmann",
                        "brand": brand,
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

    log.info(f"Rossmann celkem: {len(all_deals)} akcí")
    return all_deals


# ============================================================
# HLAVNÍ FUNKCE
# ============================================================

def run():
    log.info("=== Drogerie scraper START ===")
    results = {}

    # ROSSMANN — statický scraping
    log.info("--- Rossmann (scraping) ---")
    rossmann_deals = scrape_rossmann()
    results["Rossmann"] = len(rossmann_deals)
    if rossmann_deals:
        save_deals(rossmann_deals, "Rossmann")

    # Všechny obchody — Claude API + web search
    for store_config in STORES:
        store = store_config["name"]
        category = store_config.get("category", "potraviny")
        log.info(f"--- {store} (Claude API, kategorie: {category}) ---")
        deals = ask_claude(store, store_config["query"], category)
        results[store] = len(deals)
        if deals:
            save_deals(deals, store, category)
        time.sleep(90)  # Pauza mezi API voláními — rate limit

    log.info(f"=== Drogerie scraper END === {results}")
    total = sum(results.values())
    log.info(f"Celkem uloženo: {total} akcí ze {len(results)} obchodů")


if __name__ == "__main__":
    run()
