"""
main.py — Orquestador del IPC Canasta Básica Venezuela

Ejecuta los 3 scrapers, guarda los precios, y calcula el índice.

Uso:
    python main.py                  # Scraping completo + cálculo de índice
    python main.py --index-only     # Solo recalcula el índice (sin scraping)
    python main.py --date 2026-04-08  # Calcula índice para fecha específica
"""

import sys
import io
import json
import logging
import argparse
import threading
import queue
import pandas as pd
from pathlib import Path
from datetime import date

# Forzar UTF-8 en Windows para evitar error con tildes y ñ en la consola
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from scrapers.gama import GamaScraper
from scrapers.plaza import PlazaScraper
from scrapers.central import CentralScraper
from calculate_index import calculate_index, save_index, print_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("main")

BASKET_FILE = Path("docs/data/basket.json")
PRICES_FILE = Path("docs/data/prices_raw.csv")
TODAY = str(date.today())


def load_basket() -> list:
    with open(BASKET_FILE, "r", encoding="utf-8") as f:
        return json.load(f)["products"]


def load_previous_prices() -> dict:
    """
    Carga los precios más recientes de la semana anterior para detección de anomalías.
    Retorna dict {product_id: last_known_price_usd}.
    """
    if not PRICES_FILE.exists():
        return {}

    df = pd.read_csv(PRICES_FILE)
    df = df[df["price_usd"] > 0]
    if df.empty:
        return {}

    # Promedio por producto de la última semana disponible
    latest_date = df["date"].max()
    latest_df = df[df["date"] == latest_date]
    avg = latest_df.groupby("product_id")["price_usd"].mean()
    return avg.to_dict()


def save_prices(results: list, collection_date: str):
    """
    Guarda los resultados de scraping en el CSV de precios.
    Si el CSV no existe, lo crea. Si ya tiene datos de esa fecha, los reemplaza.
    """
    PRICES_FILE.parent.mkdir(parents=True, exist_ok=True)

    columns = [
        "date", "product_id", "store",
        "price_usd", "price_original", "currency_original",
        "product_name_found", "url_found", "flagged", "flag_reason"
    ]

    new_rows = []
    for r in results:
        new_rows.append({
            "date": collection_date,
            "product_id": r.get("product_id", ""),
            "store": r.get("store", ""),
            "price_usd": r.get("price_usd") or 0.0,
            "price_original": r.get("price_original", ""),
            "currency_original": r.get("currency_original", "USD"),
            "product_name_found": r.get("product_name_found", ""),
            "url_found": r.get("url_found", ""),
            "flagged": r.get("flagged", False),
            "flag_reason": r.get("flag_reason", ""),
        })

    new_df = pd.DataFrame(new_rows, columns=columns)

    if PRICES_FILE.exists():
        existing = pd.read_csv(PRICES_FILE)
        # Eliminar datos de la misma fecha + tienda si ya existen (re-run)
        stores_today = new_df["store"].unique().tolist()
        mask = ~((existing["date"] == collection_date) & (existing["store"].isin(stores_today)))
        existing = existing[mask]
        updated = pd.concat([existing, new_df], ignore_index=True)
        updated = updated.sort_values(["date", "product_id", "store"])
    else:
        updated = new_df

    updated.to_csv(PRICES_FILE, index=False)
    logger.info(f"Precios guardados en {PRICES_FILE} ({len(new_rows)} registros para {collection_date})")


def print_scraping_summary(all_results: list, collection_date: str):
    """Imprime un resumen del scraping al final."""
    total = len(all_results)
    found = sum(1 for r in all_results if r.get("price_usd") and r["price_usd"] > 0)
    flagged = sum(1 for r in all_results if r.get("flagged"))

    print("\n" + "═" * 60)
    print("  RESUMEN DE SCRAPING")
    print("═" * 60)
    print(f"  Fecha:             {collection_date}")
    print(f"  Total intentos:    {total}")
    pct_found = (found/total*100) if total > 0 else 0
    print(f"  Precios obtenidos: {found} ({pct_found:.0f}%)")
    print(f"  Anomalías/errores: {flagged}")
    print()

    # Por tienda
    stores = {}
    for r in all_results:
        store = r.get("store", "?")
        if store not in stores:
            stores[store] = {"found": 0, "total": 0}
        stores[store]["total"] += 1
        if r.get("price_usd") and r["price_usd"] > 0:
            stores[store]["found"] += 1

    for store, stats in stores.items():
        pct = stats["found"] / stats["total"] * 100 if stats["total"] > 0 else 0
        print(f"  {store:<20} {stats['found']}/{stats['total']} ({pct:.0f}%)")

    # Advertencias de anomalías
    anomalies = [r for r in all_results if r.get("flagged") and r.get("flag_reason")]
    if anomalies:
        print("\n  ⚠️  ANOMALÍAS DETECTADAS:")
        for r in anomalies:
            print(f"    [{r['store']}] {r['product_id']}: {r['flag_reason']}")

    print("═" * 60 + "\n")


def _run_scraper_in_thread(scraper, products, previous_prices, result_queue):
    """Wrapper para correr un scraper en un thread separado (evita conflicto con asyncio)."""
    try:
        results = scraper.scrape_all(products, previous_prices)
        result_queue.put(("ok", results))
    except Exception as e:
        result_queue.put(("error", str(e)))


def run_scrapers(products: list, previous_prices: dict, collection_date: str) -> list:
    """
    Ejecuta los 3 scrapers y retorna todos los resultados.
    Cada scraper corre en su propio thread para evitar conflicto
    con el event loop de asyncio del entorno.
    """
    all_results = []

    scrapers = [
        GamaScraper(),
        PlazaScraper(),
        CentralScraper(),
    ]

    for scraper in scrapers:
        logger.info(f"\n{'─'*40}")
        logger.info(f"Iniciando scraper: {scraper.STORE_NAME.upper()}")
        logger.info(f"{'─'*40}")

        result_queue = queue.Queue()
        t = threading.Thread(
            target=_run_scraper_in_thread,
            args=(scraper, products, previous_prices, result_queue),
            daemon=True
        )
        t.start()
        t.join(timeout=300)  # 5 min max por tienda

        if t.is_alive():
            logger.error(f"Timeout: scraper {scraper.STORE_NAME} tardó más de 5 minutos")
            continue

        status, payload = result_queue.get()
        if status == "ok":
            all_results.extend(payload)
        else:
            logger.error(f"Error fatal en scraper {scraper.STORE_NAME}: {payload}")

    return all_results


def main():
    parser = argparse.ArgumentParser(description="IPC Canasta Básica Venezuela")
    parser.add_argument("--index-only", action="store_true",
                        help="Solo recalcula el índice sin scraping")
    parser.add_argument("--date", default=TODAY,
                        help=f"Fecha para el índice (default: {TODAY})")
    parser.add_argument("--scrape-date", default=TODAY,
                        help=f"Fecha a usar al guardar precios (default: {TODAY})")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("  IPC CANASTA BÁSICA ALIMENTARIA VENEZUELA")
    logger.info("=" * 60)

    if not args.index_only:
        # ── Scraping ──────────────────────────────────────────────────
        logger.info(f"Fecha de recolección: {args.scrape_date}")
        products = load_basket()
        previous_prices = load_previous_prices()

        logger.info(f"Productos en canasta: {len(products)}")
        logger.info(f"Tiendas: Gama, Plaza, Central Madeirense")

        all_results = run_scrapers(products, previous_prices, args.scrape_date)
        save_prices(all_results, args.scrape_date)
        print_scraping_summary(all_results, args.scrape_date)

    # ── Cálculo del índice ────────────────────────────────────────────
    logger.info("Calculando índice...")
    result = calculate_index(args.date)

    if result:
        print_report(result)
        save_index(result)
        logger.info("✓ Proceso completado exitosamente")
    else:
        logger.error("✗ No se pudo calcular el índice")
        sys.exit(1)


if __name__ == "__main__":
    main()
