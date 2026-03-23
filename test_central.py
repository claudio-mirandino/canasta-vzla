"""Test rápido del scraper de Central Madeirense."""
import sys, io, json, logging
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S"
)

from scrapers.central import CentralScraper

basket_path = Path("docs/data/basket.json")
with open(basket_path, encoding="utf-8") as f:
    basket = json.load(f)

products = basket["products"]
scraper = CentralScraper()
results = scraper.scrape_all(products)

print("\n" + "="*60)
print("  RESULTADOS CENTRAL MADEIRENSE")
print("="*60)
found = 0
for r in results:
    pid = r["product_id"]
    price = r.get("price_usd")
    name = r.get("product_name_found", "")
    flag = r.get("flag_reason", "")
    if price and price > 0:
        found += 1
        print(f"  OK  {pid:<15} ${price:>7.2f}  {name[:45]}")
    else:
        status = "N/D" if "no disponible" in flag else "ERR"
        print(f" {status}  {pid:<15}          {flag[:50]}")

print("="*60)
print(f"  Total: {found}/{len(products)} productos encontrados")
print("="*60)
