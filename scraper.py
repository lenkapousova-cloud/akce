"""
TEST — len Lidl cez Claude API + web search
"""
import os
import json
import time
import logging
from datetime import datetime, timezone
import requests
from supabase import create_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://eifooaghbprllczieowj.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")


def ask_claude_lidl() -> list:
    system_prompt = """Jsi expert na české akční ceny. Vrať POUZE validní JSON pole, žádný jiný text.

Formát:
[{"name": "Ariel prací gel 1,8l", "brand": "Ariel", "price": 89.90, "old_price": 149.90, "discount": 40, "valid_from": "2026-05-05", "valid_to": "2026-05-11", "source_url": "https://lidl.cz"}]

Pravidla:
- POUZE JSON pole, žádný markdown
- Ceny jako čísla s desetinnou tečkou
- Pouze drogerie: prací prostředky, čisticí prostředky, kosmetika, šampóny, zubní pasty, toaletní papír, plenky, deodoranty
- Nezahrnuj potraviny, nápoje, oblečení"""

    payload = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 4096,
        "system": system_prompt,
        "messages": [{"role": "user", "content": "Najdi aktuální akční ceny drogerie v Lidl letáku. Prohledej https://www.kupi.cz/letaky/lidl a https://www.lidl.cz/aktualni-letak. Vrať pouze JSON."}],
        "tools": [{"type": "web_search_20250305", "name": "web_search"}]
    }

    for attempt in range(3):
        log.info(f"Volám Claude API (pokus {attempt+1})...")
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"},
            json=payload, timeout=90
        )
        if r.status_code == 429:
            wait = 30 * (attempt + 1)
            log.warning(f"Rate limit, čekám {wait}s...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        break

    data = r.json()
    text = "\n".join(b["text"] for b in data.get("content", []) if b.get("type") == "text").strip()
    log.info(f"Odpověď délka: {len(text)} znaků")
    log.info(f"Odpověď obsah: {text[:500]}")

    start, end = text.find("["), text.rfind("]") + 1
    if start == -1:
        log.warning("JSON nenalezeno")
        log.info(f"Odpověď: {text[:500]}")
        return []

    deals = json.loads(text[start:end])
    log.info(f"Nalezeno {len(deals)} akcí")
    return deals


def save_lidl(deals):
    if not deals:
        return 0
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    supabase.table("deals").delete().eq("store", "Lidl").execute()
    now = datetime.now(timezone.utc).isoformat()
    records = [{"name": str(d.get("name",""))[:200], "store": "Lidl", "brand": d.get("brand"),
                "price": float(d["price"]) if d.get("price") else None,
                "old_price": float(d["old_price"]) if d.get("old_price") else None,
                "discount": int(d["discount"]) if d.get("discount") else None,
                "valid_from": d.get("valid_from"), "valid_to": d.get("valid_to"),
                "source_url": d.get("source_url"), "category": "drogerie", "updated_at": now}
               for d in deals if d.get("name") and d.get("price")]
    result = supabase.table("deals").insert(records).execute()
    total = len(result.data) if result.data else 0
    log.info(f"Uloženo {total} akcí Lidl")
    return total


def run():
    log.info("=== TEST Lidl START ===")
    deals = ask_claude_lidl()
    if deals:
        save_lidl(deals)
    log.info(f"=== TEST Lidl END — {len(deals)} akcí ===")


if __name__ == "__main__":
    run()
