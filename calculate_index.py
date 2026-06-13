"""
Cálculo del Índice de Precios de la Canasta Básica Alimentaria — Venezuela

METODOLOGÍA (índice encadenado de relativos por serie tienda-producto)
=======================================================================
Es la forma estándar de construir un IPC a partir de precios recolectados
en línea (cf. Billion Prices Project / PriceStats, y la lógica Laspeyres-
Jevons que usan los institutos de estadística). Es robusta ante:
  • diferencias de empaque/marca entre tiendas (300gr vs 1kg),
  • la entrada o salida de una tienda sin saltos artificiales,
  • datos faltantes en una semana.

Pasos:
  1. Serie por (tienda, producto): precio en USD semana a semana.
  2. Relativo semanal por serie: r = p(t) / p(t-1), SOLO si esa misma serie
     (misma tienda y producto) tiene precio en t y en t-1. Así nunca se
     comparan niveles entre tiendas: cada tienda se compara consigo misma.
  3. Relativo del producto: media geométrica (Jevons) de los relativos de
     las tiendas que aportaron ese producto esa semana.
  4. Variación de la canasta: media geométrica ponderada de los relativos
     de producto, con los pesos de importancia definidos en basket.json
     (Laspeyres sobre la canasta fija).
  5. Encadenamiento: I(t) = I(t-1) × factor_semanal.  Base: I(base)=100.

El índice histórico se RECONSTRUYE completo desde prices_raw.csv en cada
corrida, garantizando consistencia de punta a punta.
"""

import json
import logging
import math
import pandas as pd
from pathlib import Path

logger = logging.getLogger("calculate_index")

PRICES_FILE = Path("docs/data/prices_raw.csv")
INDEX_FILE = Path("docs/data/index.csv")
DETAIL_FILE = Path("docs/data/detail.json")
BASKET_FILE = Path("docs/data/basket.json")
BASE_DATE = "2026-04-01"

# Cambio relativo por serie superior a esto → se considera ruido/cambio de
# artículo y se descarta del cálculo del relativo (no del registro de precio).
MAX_REL = 2.0   # +100%
MIN_REL = 0.5   # -50%


def load_basket() -> tuple[dict, dict]:
    """Retorna (pesos {id: weight}, meta {id: nombre})."""
    with open(BASKET_FILE, "r", encoding="utf-8") as f:
        basket = json.load(f)
    weights = {p["id"]: p["weight"] for p in basket["products"]}
    names = {p["id"]: p["name"] for p in basket["products"]}
    return weights, names


def load_prices() -> pd.DataFrame:
    if not PRICES_FILE.exists():
        return pd.DataFrame(columns=["date", "product_id", "store", "price_usd", "flagged"])
    df = pd.read_csv(PRICES_FILE)
    df["date"] = df["date"].astype(str)
    df["price_usd"] = pd.to_numeric(df["price_usd"], errors="coerce")
    return df


def _valid_prices(df: pd.DataFrame) -> pd.DataFrame:
    """Precios usables: > 0. (flagged se conserva como dato pero no bloquea.)"""
    d = df[df["price_usd"] > 0].copy()
    # Promediar duplicados (misma fecha/tienda/producto) por si hay reruns
    d = d.groupby(["date", "product_id", "store"], as_index=False)["price_usd"].mean()
    return d


def compute_series(df: pd.DataFrame, weights: dict, base_date: str) -> pd.DataFrame:
    """
    Reconstruye la serie completa del índice encadenado.
    Retorna DataFrame con columnas:
      date, index_value, weekly_change_pct, products_priced,
      products_total, base_date, notes
    """
    d = _valid_prices(df)
    if d.empty:
        return pd.DataFrame()

    # La serie publicada arranca en la fecha base (lo anterior es ruido
    # de calibración previo al lanzamiento). prices_raw.csv conserva todo.
    d = d[d["date"] >= base_date]
    if d.empty:
        return pd.DataFrame()

    dates = sorted(d["date"].unique())
    total_products = len(weights)

    # Precio por (date, product, store) en dict anidado para acceso rápido
    price = {}
    for _, row in d.iterrows():
        price.setdefault(row["date"], {}).setdefault(row["product_id"], {})[row["store"]] = row["price_usd"]

    rows = []
    prev_date = None
    for dt in dates:
        if prev_date is None:
            # Primera fecha con datos: arranque de la cadena
            factor = 1.0
            products_used = sorted(price[dt].keys())
            note = "Inicio de la serie"
        else:
            log_sum = 0.0
            w_sum = 0.0
            products_used = []
            for pid, w in weights.items():
                # Relativos por tienda donde existe precio en dt y prev_date
                rels = []
                cur_stores = price.get(dt, {}).get(pid, {})
                prev_stores = price.get(prev_date, {}).get(pid, {})
                for store, p_cur in cur_stores.items():
                    p_prev = prev_stores.get(store)
                    if p_prev and p_prev > 0:
                        r = p_cur / p_prev
                        if MIN_REL <= r <= MAX_REL:
                            rels.append(r)
                if not rels:
                    continue
                # Media geométrica (Jevons) de los relativos de tienda
                prod_rel = math.exp(sum(math.log(r) for r in rels) / len(rels))
                log_sum += w * math.log(prod_rel)
                w_sum += w
                products_used.append(pid)

            factor = math.exp(log_sum / w_sum) if w_sum > 0 else 1.0
            note = ""

        rows.append({
            "date": dt,
            "_factor": factor,
            "products_priced": len(products_used),
            "products_total": total_products,
            "_note": note,
        })
        prev_date = dt

    series = pd.DataFrame(rows)

    # ── Encadenar y anclar la base en 100 ────────────────────────────────
    # index acumulado relativo al primer punto:
    cum = []
    acc = 1.0
    for i, r in series.iterrows():
        acc = acc * r["_factor"] if i > 0 else 1.0
        cum.append(acc)
    series["_cum"] = cum

    # Normalizar para que la fecha base = 100
    if base_date in series["date"].values:
        base_cum = series.loc[series["date"] == base_date, "_cum"].iloc[0]
    else:
        # Si no hay datos exactamente en la base, usar el primer punto
        base_cum = series["_cum"].iloc[0]
        logger.warning(f"Sin datos en la fecha base {base_date}; anclando al primer punto disponible")

    series["index_value"] = (series["_cum"] / base_cum * 100).round(2)

    # Cambio semanal
    series["weekly_change_pct"] = (series["index_value"].pct_change() * 100).round(2)
    series.loc[series.index[0], "weekly_change_pct"] = 0.0

    # Notas
    def note_for(r):
        if r["_note"]:
            return r["_note"]
        missing = r["products_total"] - r["products_priced"]
        if missing > 0:
            return f"{r['products_priced']}/{r['products_total']} productos con variación esta semana"
        return ""
    series["notes"] = series.apply(note_for, axis=1)
    series["base_date"] = base_date

    out_cols = ["date", "index_value", "weekly_change_pct",
                "products_priced", "products_total", "base_date", "notes"]
    return series[out_cols]


def build_detail(df: pd.DataFrame, weights: dict, names: dict) -> dict:
    """
    Detalle por producto para el dashboard: último precio por tienda,
    precio representativo (mediana entre tiendas) y variación vs semana previa.
    """
    d = _valid_prices(df)
    if d.empty:
        return {}

    dates = sorted(d["date"].unique())
    if len(dates) == 0:
        return {}
    last = dates[-1]
    prev = dates[-2] if len(dates) > 1 else None

    products = []
    for pid, w in weights.items():
        cur = d[(d["date"] == last) & (d["product_id"] == pid)]
        if cur.empty:
            continue
        by_store = {row["store"]: round(row["price_usd"], 2) for _, row in cur.iterrows()}
        rep = round(cur["price_usd"].median(), 2)

        change = None
        if prev is not None:
            prv = d[(d["date"] == prev) & (d["product_id"] == pid)]
            if not prv.empty:
                prev_rep = prv["price_usd"].median()
                if prev_rep > 0:
                    change = round((rep - prev_rep) / prev_rep * 100, 2)

        products.append({
            "id": pid,
            "name": names.get(pid, pid),
            "weight": w,
            "price_usd": rep,
            "by_store": by_store,
            "weekly_change_pct": change,
        })

    return {
        "last_date": last,
        "prev_date": prev,
        "stores": sorted(d[d["date"] == last]["store"].unique().tolist()),
        "products": products,
    }


def rebuild_index() -> pd.DataFrame:
    """Recalcula toda la serie y la guarda en index.csv + detail.json."""
    weights, names = load_basket()
    df = load_prices()
    if df.empty:
        logger.error("No hay datos de precios. Ejecuta el scraper primero.")
        return pd.DataFrame()

    series = compute_series(df, weights, BASE_DATE)
    if series.empty:
        logger.error("No se pudo construir la serie del índice.")
        return series

    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    series.to_csv(INDEX_FILE, index=False)
    logger.info(f"Índice reconstruido y guardado en {INDEX_FILE} ({len(series)} semanas)")

    detail = build_detail(df, weights, names)
    with open(DETAIL_FILE, "w", encoding="utf-8") as f:
        json.dump(detail, f, ensure_ascii=False, indent=2)
    logger.info(f"Detalle por producto guardado en {DETAIL_FILE}")

    return series


# ── Interfaz compatible con main.py ───────────────────────────────────────

def calculate_index(week_date: str = None) -> dict:
    """
    Reconstruye la serie completa y devuelve la fila de la fecha pedida
    (o la última disponible). Mantiene la firma usada por main.py.
    """
    series = rebuild_index()
    if series.empty:
        return None
    if week_date and week_date in series["date"].values:
        row = series[series["date"] == week_date].iloc[0]
    else:
        row = series.iloc[-1]
    return row.to_dict()


def save_index(result: dict):
    """No-op de compatibilidad: rebuild_index() ya escribió index.csv completo."""
    logger.debug("save_index: serie ya persistida por rebuild_index()")


def print_report(result: dict):
    print("\n" + "=" * 60)
    print("  ÍNDICE CANASTA BÁSICA ALIMENTARIA VENEZUELA")
    print("=" * 60)
    print(f"  Fecha:             {result['date']}")
    print(f"  Fecha base:        {result['base_date']} = 100")
    print(f"  Índice:            {result['index_value']:.2f}")
    change = result.get('weekly_change_pct') or 0.0
    arrow = "+" if change > 0 else ("-" if change < 0 else "=")
    print(f"  Cambio semanal:    {arrow} {change:+.2f}%")
    print(f"  Productos:         {result['products_priced']}/{result['products_total']}")
    if result.get("notes"):
        print(f"  Notas:             {result['notes']}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    series = rebuild_index()
    if not series.empty:
        for _, r in series.iterrows():
            print(f"{r['date']}  índice={r['index_value']:>7.2f}  "
                  f"sem={r['weekly_change_pct']:>+6.2f}%  "
                  f"prod={r['products_priced']}/{r['products_total']}")
