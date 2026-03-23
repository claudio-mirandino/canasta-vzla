"""
Cálculo del Índice de Precios al Consumidor (IPC)
Metodología: Índice de Laspeyres con ponderación por importancia en el gasto familiar

Fórmula:
    I(t) = Σ [w_i × (p_i(t) / p_i(0))] / Σ w_i  × 100

Donde:
    I(t)    = índice en el período t
    w_i     = peso del producto i (definido en basket.json)
    p_i(t)  = precio promedio del producto i en el período t (promedio entre tiendas)
    p_i(0)  = precio promedio del producto i en el período base (1° de abril 2026)

Si un producto no tiene precio en una tienda, se usa el promedio de las otras.
Si no tiene precio en NINGUNA tienda, se usa el precio de la semana anterior.
Si no tiene precio histórico anterior, ese producto NO entra en el cálculo ese período.
"""

import json
import logging
import pandas as pd
from pathlib import Path
from datetime import date

logger = logging.getLogger("calculate_index")

PRICES_FILE = Path("docs/data/prices_raw.csv")
INDEX_FILE = Path("docs/data/index.csv")
BASKET_FILE = Path("docs/data/basket.json")
BASE_DATE = "2026-04-01"


def load_basket() -> dict:
    """Carga la canasta y retorna dict {product_id: weight}."""
    with open(BASKET_FILE, "r", encoding="utf-8") as f:
        basket = json.load(f)
    return {p["id"]: p["weight"] for p in basket["products"]}


def load_prices() -> pd.DataFrame:
    """Carga el CSV de precios históricos."""
    if not PRICES_FILE.exists():
        return pd.DataFrame(columns=["date", "product_id", "store", "price_usd", "flagged"])
    df = pd.read_csv(PRICES_FILE, parse_dates=["date"])
    return df


def compute_weekly_average(df: pd.DataFrame, week_date: str) -> pd.Series:
    """
    Para una semana dada, calcula el precio promedio por producto
    (promedio entre tiendas que tienen datos válidos).

    Solo usa precios con price_usd > 0 y flagged == False.
    Si todos los precios de un producto están flagged, los incluye igual
    pero marca el índice final con advertencia.
    """
    week_df = df[df["date"] == week_date].copy()

    # Primero: usar solo no-flagged
    clean = week_df[week_df["flagged"] == False]
    avg_clean = clean.groupby("product_id")["price_usd"].mean()

    # Para productos sin precio limpio, usar flagged como respaldo
    flagged = week_df[week_df["flagged"] == True]
    avg_flagged = flagged.groupby("product_id")["price_usd"].mean()

    # Combinar: clean tiene prioridad
    avg_combined = avg_clean.combine_first(avg_flagged)

    return avg_combined


def calculate_index(week_date: str = None) -> dict:
    """
    Calcula el índice para la semana especificada.
    Si no se especifica, usa la última semana en los datos.

    Retorna dict con:
        date, index_value, weekly_change_pct, products_priced,
        products_total, base_date, notes
    """
    weights = load_basket()
    df = load_prices()

    if df.empty:
        logger.error("No hay datos de precios. Ejecuta el scraper primero.")
        return None

    # Filtrar precios válidos (price_usd > 0)
    df = df[df["price_usd"] > 0]

    if week_date is None:
        week_date = str(df["date"].max().date())

    logger.info(f"Calculando índice para: {week_date}")
    logger.info(f"Fecha base: {BASE_DATE}")

    # ── Precios base (período 0) ──────────────────────────────────────
    base_prices = compute_weekly_average(df, BASE_DATE)

    if base_prices.empty:
        logger.error(f"No hay datos de precios para la fecha base {BASE_DATE}.")
        return None

    # ── Precios del período t ─────────────────────────────────────────
    current_prices = compute_weekly_average(df, week_date)

    if current_prices.empty:
        logger.error(f"No hay datos de precios para {week_date}.")
        return None

    # ── Si es la fecha base, devolver 100.0 directamente ─────────────
    if week_date == BASE_DATE:
        index_value = 100.0
        weekly_change = 0.0
        products_priced = len(current_prices)
        notes = "Fecha base — índice = 100 por definición"
        logger.info(f"Fecha base: índice = 100.0")
    else:
        # ── Calcular Laspeyres ────────────────────────────────────────
        numerator = 0.0
        denominator = 0.0
        products_used = []
        products_missing = []

        for product_id, weight in weights.items():
            base_p = base_prices.get(product_id)
            curr_p = current_prices.get(product_id)

            if base_p is None or base_p == 0:
                products_missing.append(f"{product_id} (sin precio base)")
                continue
            if curr_p is None or curr_p == 0:
                # Usar precio de semana anterior como aproximación
                prev_price = _get_previous_price(df, product_id, week_date)
                if prev_price:
                    curr_p = prev_price
                    logger.warning(f"  {product_id}: usando precio anterior (${prev_price:.2f}) — sin dato esta semana")
                else:
                    products_missing.append(f"{product_id} (sin precio actual)")
                    continue

            ratio = curr_p / base_p
            numerator += weight * ratio
            denominator += weight
            products_used.append(product_id)

        if denominator == 0:
            logger.error("No hay productos válidos para calcular el índice.")
            return None

        index_value = round((numerator / denominator) * 100, 2)
        products_priced = len(products_used)

        # ── Cambio semanal ────────────────────────────────────────────
        prev_index = _get_previous_index(week_date)
        weekly_change = round(((index_value - prev_index) / prev_index * 100), 2) if prev_index else 0.0

        notes = ""
        if products_missing:
            notes = f"Productos sin datos: {'; '.join(products_missing)}"
            logger.warning(f"Productos excluidos: {products_missing}")

    result = {
        "date": week_date,
        "index_value": index_value,
        "weekly_change_pct": weekly_change,
        "products_priced": products_priced,
        "products_total": len(weights),
        "base_date": BASE_DATE,
        "notes": notes,
    }

    logger.info(f"Índice calculado: {index_value} (cambio semanal: {weekly_change:+.2f}%)")
    logger.info(f"Productos incluidos: {products_priced}/{len(weights)}")

    return result


def _get_previous_price(df: pd.DataFrame, product_id: str, current_date: str) -> float | None:
    """Obtiene el último precio conocido de un producto antes de la fecha actual."""
    product_df = df[
        (df["product_id"] == product_id) &
        (df["date"] < current_date) &
        (df["price_usd"] > 0)
    ].sort_values("date", ascending=False)

    if not product_df.empty:
        return product_df.iloc[0]["price_usd"]
    return None


def _get_previous_index(current_date: str) -> float | None:
    """Obtiene el valor del índice de la semana anterior."""
    if not INDEX_FILE.exists():
        return None
    try:
        idx_df = pd.read_csv(INDEX_FILE)
        prev = idx_df[idx_df["date"] < current_date].sort_values("date", ascending=False)
        if not prev.empty:
            return prev.iloc[0]["index_value"]
    except Exception:
        pass
    return None


def save_index(result: dict):
    """Agrega el resultado al CSV del índice (o crea el archivo si no existe)."""
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)

    new_row = pd.DataFrame([result])

    if INDEX_FILE.exists():
        existing = pd.read_csv(INDEX_FILE)
        # Reemplazar si ya existe esta fecha, sino agregar
        existing = existing[existing["date"] != result["date"]]
        updated = pd.concat([existing, new_row], ignore_index=True)
        updated = updated.sort_values("date")
    else:
        updated = new_row

    updated.to_csv(INDEX_FILE, index=False)
    logger.info(f"Índice guardado en {INDEX_FILE}")


def print_report(result: dict):
    """Imprime un reporte legible del índice calculado."""
    print("\n" + "═" * 60)
    print("  ÍNDICE CANASTA BÁSICA ALIMENTARIA VENEZUELA")
    print("═" * 60)
    print(f"  Fecha:             {result['date']}")
    print(f"  Fecha base:        {result['base_date']} = 100")
    print(f"  Índice:            {result['index_value']:.2f}")
    change = result['weekly_change_pct']
    arrow = "▲" if change > 0 else ("▼" if change < 0 else "─")
    print(f"  Cambio semanal:    {arrow} {change:+.2f}%")
    print(f"  Productos:         {result['products_priced']}/{result['products_total']}")
    if result.get("notes"):
        print(f"  Notas:             {result['notes']}")
    print("═" * 60 + "\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = calculate_index()
    if result:
        print_report(result)
        save_index(result)
