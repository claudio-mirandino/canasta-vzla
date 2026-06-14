"""
Cálculo del Índice de Precios de la Canasta Básica Alimentaria — Venezuela

METODOLOGÍA (índice encadenado de relativos por serie tienda-producto)
=======================================================================
Forma estándar de construir un IPC a partir de precios en línea (cf. Billion
Prices Project / PriceStats, lógica Laspeyres-Jevons de los institutos de
estadística). Robusta ante: empaques/marcas distintas entre tiendas, entrada
o salida de una tienda, y datos faltantes.

  1. Serie por (tienda, producto): precio USD semana a semana.
  2. Relativo semanal por serie: r = p(t)/p(t-1), solo si esa MISMA serie tiene
     precio en t y t-1. Cada tienda se compara consigo misma — nunca niveles
     entre tiendas.
  3. Relativo de producto: media geométrica (Jevons) de los relativos de tienda.
  4. Variación de la canasta: media geométrica ponderada por importancia.
  5. Encadenamiento: I(t) = I(t-1) × factor. Base 2026-06-12 = 100.

SALIDAS
  docs/data/index.csv     — serie (USD y Bs)
  docs/data/detail.json   — precios por producto y tienda de la última semana
  docs/data/summary.json  — métricas titulares (inflación anualizada, acumulada,
                            mensual, brecha cambiaria, dispersión)
  docs/data/api/index.json, api/latest.json — endpoints estables para terceros
"""

import json
import logging
import math
import pandas as pd
from pathlib import Path
from datetime import datetime

logger = logging.getLogger("calculate_index")

PRICES_FILE = Path("docs/data/prices_raw.csv")
FX_FILE = Path("docs/data/fx.csv")
INDEX_FILE = Path("docs/data/index.csv")
DETAIL_FILE = Path("docs/data/detail.json")
SUMMARY_FILE = Path("docs/data/summary.json")
API_DIR = Path("docs/data/api")
BASKET_FILE = Path("docs/data/basket.json")

BASE_DATE = "2026-06-12"   # Primer día con las 3 tiendas (serie consistente)

MAX_REL = 2.0   # relativos de serie fuera de [0.5, 2.0] se descartan (cambio de artículo)
MIN_REL = 0.5


# ── Carga ──────────────────────────────────────────────────────────────────

def load_basket() -> tuple[dict, dict]:
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


def load_fx() -> dict:
    """{date: {'oficial':x,'paralelo':y,'brecha_pct':z}}"""
    if not FX_FILE.exists():
        return {}
    fx = pd.read_csv(FX_FILE)
    fx["date"] = fx["date"].astype(str)
    out = {}
    for _, r in fx.iterrows():
        out[r["date"]] = {
            "oficial": float(r["oficial"]) if pd.notna(r.get("oficial")) else None,
            "paralelo": float(r["paralelo"]) if pd.notna(r.get("paralelo")) else None,
            "brecha_pct": float(r["brecha_pct"]) if pd.notna(r.get("brecha_pct")) else None,
        }
    return out


def _valid_prices(df: pd.DataFrame) -> pd.DataFrame:
    d = df[df["price_usd"] > 0].copy()
    d = d.groupby(["date", "product_id", "store"], as_index=False)["price_usd"].mean()
    return d


# ── Serie del índice (USD + Bs) ──────────────────────────────────────────────

def compute_series(df: pd.DataFrame, weights: dict, fx: dict, base_date: str) -> pd.DataFrame:
    d = _valid_prices(df)
    if d.empty:
        return pd.DataFrame()
    d = d[d["date"] >= base_date]
    if d.empty:
        return pd.DataFrame()

    dates = sorted(d["date"].unique())
    total_products = len(weights)

    price = {}
    for _, row in d.iterrows():
        price.setdefault(row["date"], {}).setdefault(row["product_id"], {})[row["store"]] = row["price_usd"]

    rows = []
    prev_date = None
    for dt in dates:
        if prev_date is None:
            factor = 1.0
            products_used = sorted(price[dt].keys())
        else:
            log_sum = w_sum = 0.0
            products_used = []
            for pid, w in weights.items():
                rels = []
                cur = price.get(dt, {}).get(pid, {})
                prv = price.get(prev_date, {}).get(pid, {})
                for store, p_cur in cur.items():
                    p_prev = prv.get(store)
                    if p_prev and p_prev > 0:
                        r = p_cur / p_prev
                        if MIN_REL <= r <= MAX_REL:
                            rels.append(r)
                if not rels:
                    continue
                prod_rel = math.exp(sum(math.log(r) for r in rels) / len(rels))
                log_sum += w * math.log(prod_rel)
                w_sum += w
                products_used.append(pid)
            factor = math.exp(log_sum / w_sum) if w_sum > 0 else 1.0

        # Costo de canasta (suma de la mediana entre tiendas por producto)
        cost = 0.0
        for pid in price.get(dt, {}):
            vals = sorted(price[dt][pid].values())
            n = len(vals)
            med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
            cost += med

        rows.append({
            "date": dt, "_factor": factor,
            "products_priced": len(products_used),
            "products_total": total_products,
            "basket_cost_usd": round(cost, 2),
        })
        prev_date = dt

    series = pd.DataFrame(rows)

    # Encadenar y anclar base = 100 (USD)
    cum, acc = [], 1.0
    for i in range(len(series)):
        acc = acc * series.iloc[i]["_factor"] if i > 0 else 1.0
        cum.append(acc)
    series["_cum"] = cum
    base_cum = (series.loc[series["date"] == base_date, "_cum"].iloc[0]
                if base_date in series["date"].values else series["_cum"].iloc[0])
    series["index_usd"] = (series["_cum"] / base_cum * 100).round(2)

    # Índice en bolívares: USD compuesto con la devaluación oficial
    base_oficial = (fx.get(base_date) or {}).get("oficial")
    def bs_index(r):
        of = (fx.get(r["date"]) or {}).get("oficial")
        if of and base_oficial:
            return round(r["index_usd"] * of / base_oficial, 2)
        return None
    series["index_bs"] = series.apply(bs_index, axis=1)

    series["weekly_change_pct"] = (series["index_usd"].pct_change() * 100).round(2)
    series.loc[series.index[0], "weekly_change_pct"] = 0.0

    def note(r):
        miss = r["products_total"] - r["products_priced"]
        return f"{r['products_priced']}/{r['products_total']} productos con variación" if miss > 0 else ""
    series["notes"] = series.apply(note, axis=1)
    series["base_date"] = base_date

    return series[["date", "index_usd", "index_bs", "weekly_change_pct",
                   "basket_cost_usd", "products_priced", "products_total",
                   "base_date", "notes"]]


# ── Detalle por producto ─────────────────────────────────────────────────────

def build_detail(df: pd.DataFrame, weights: dict, names: dict) -> dict:
    d = _valid_prices(df)
    if d.empty:
        return {}
    dates = sorted(d["date"].unique())
    last = dates[-1]
    prev = dates[-2] if len(dates) > 1 else None

    products = []
    for pid, w in weights.items():
        cur = d[(d["date"] == last) & (d["product_id"] == pid)]
        if cur.empty:
            continue
        by_store = {r["store"]: round(r["price_usd"], 2) for _, r in cur.iterrows()}
        rep = round(float(cur["price_usd"].median()), 2)
        change = None
        if prev is not None:
            prv = d[(d["date"] == prev) & (d["product_id"] == pid)]
            if not prv.empty:
                pm = float(prv["price_usd"].median())
                if pm > 0:
                    change = round((rep - pm) / pm * 100, 2)
        products.append({"id": pid, "name": names.get(pid, pid), "weight": w,
                         "price_usd": rep, "by_store": by_store, "weekly_change_pct": change})
    return {"last_date": last, "prev_date": prev,
            "stores": sorted(d[d["date"] == last]["store"].unique().tolist()),
            "products": products}


# ── Métricas titulares + dispersión ──────────────────────────────────────────

def _pct_between(series: pd.DataFrame, last_date: str, days: int) -> float | None:
    """Variación % del índice USD entre last_date y ~days atrás (fecha más cercana <=)."""
    last_dt = datetime.fromisoformat(last_date)
    target = last_dt.timestamp() - days * 86400
    cand = series[series["date"] < last_date].copy()
    if cand.empty:
        return None
    cand["ts"] = cand["date"].apply(lambda s: datetime.fromisoformat(s).timestamp())
    cand = cand[cand["ts"] <= target]
    if cand.empty:
        return None
    base_row = cand.iloc[-1]
    cur = series[series["date"] == last_date]["index_usd"].iloc[0]
    return round((cur / base_row["index_usd"] - 1) * 100, 2)


def build_summary(df: pd.DataFrame, series: pd.DataFrame, fx: dict,
                  detail: dict, base_date: str) -> dict:
    if series.empty:
        return {}
    last = series.iloc[-1]
    last_date = last["date"]

    days_since_base = max(1, (datetime.fromisoformat(last_date)
                             - datetime.fromisoformat(base_date)).days)
    accumulated = round(last["index_usd"] - 100, 2)
    accumulated_bs = round(last["index_bs"] - 100, 2) if pd.notna(last["index_bs"]) else None
    annualized = (round(((last["index_usd"] / 100) ** (365 / days_since_base) - 1) * 100, 1)
                  if days_since_base >= 7 else None)

    rates = fx.get(last_date) or fx.get(max(fx)) if fx else {}
    rates = dict(rates) if rates else {}
    if rates:
        rates["date"] = last_date if last_date in fx else (max(fx) if fx else None)

    # Dispersión: costo de canasta por tienda usando productos comunes a todas
    d = _valid_prices(df)
    dl = d[d["date"] == last_date]
    stores = sorted(dl["store"].unique().tolist())
    prod_by_store = {s: set(dl[dl["store"] == s]["product_id"]) for s in stores}
    common = set.intersection(*prod_by_store.values()) if prod_by_store else set()
    store_cost = {}
    for s in stores:
        sub = dl[(dl["store"] == s) & (dl["product_id"].isin(common))]
        store_cost[s] = round(float(sub["price_usd"].sum()), 2)
    costs = list(store_cost.values())
    dispersion = None
    if costs:
        costs_sorted = sorted(costs)
        n = len(costs_sorted)
        med = costs_sorted[n // 2] if n % 2 else (costs_sorted[n // 2 - 1] + costs_sorted[n // 2]) / 2
        mean = sum(costs) / len(costs)
        var = sum((c - mean) ** 2 for c in costs) / len(costs)
        cv = round((var ** 0.5) / mean * 100, 1) if mean else None
        dispersion = {"by_store": store_cost, "common_products": len(common),
                      "min": min(costs), "median": round(med, 2), "max": max(costs),
                      "cv_pct": cv}

    return {
        "base_date": base_date,
        "last_date": last_date,
        "weeks": len(series),
        "index_usd": last["index_usd"],
        "index_bs": last["index_bs"] if pd.notna(last["index_bs"]) else None,
        "basket_cost_usd": last["basket_cost_usd"],
        "basket_cost_bs_oficial": (round(last["basket_cost_usd"] * rates["oficial"], 2)
                                   if rates.get("oficial") else None),
        "basket_cost_bs_paralelo": (round(last["basket_cost_usd"] * rates["paralelo"], 2)
                                    if rates.get("paralelo") else None),
        "coverage": f"{int(last['products_priced'])}/{int(last['products_total'])}",
        "stores": detail.get("stores", stores),
        "metrics": {
            "weekly_pct": last["weekly_change_pct"],
            "monthly_pct": _pct_between(series, last_date, 30),
            "accumulated_pct": accumulated,
            "annualized_pct": annualized,
            "interannual_pct": _pct_between(series, last_date, 365),
            "accumulated_bs_pct": accumulated_bs,
        },
        "rates": rates,
        "dispersion": dispersion,
    }


def build_api(series: pd.DataFrame, summary: dict):
    API_DIR.mkdir(parents=True, exist_ok=True)
    serie = [{"date": r["date"], "index_usd": r["index_usd"],
              "index_bs": (r["index_bs"] if pd.notna(r["index_bs"]) else None),
              "weekly_change_pct": r["weekly_change_pct"],
              "basket_cost_usd": r["basket_cost_usd"]}
             for _, r in series.iterrows()]
    with open(API_DIR / "index.json", "w", encoding="utf-8") as f:
        json.dump({"base_date": BASE_DATE, "series": serie}, f, ensure_ascii=False, indent=2)
    with open(API_DIR / "latest.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)


# ── Orquestación ─────────────────────────────────────────────────────────────

def rebuild_index() -> pd.DataFrame:
    weights, names = load_basket()
    df = load_prices()
    fx = load_fx()
    if df.empty:
        logger.error("No hay datos de precios. Ejecuta el scraper primero.")
        return pd.DataFrame()

    series = compute_series(df, weights, fx, BASE_DATE)
    if series.empty:
        logger.error("No se pudo construir la serie del índice.")
        return series

    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    series.to_csv(INDEX_FILE, index=False)
    logger.info(f"Índice reconstruido: {INDEX_FILE} ({len(series)} semanas)")

    detail = build_detail(df, weights, names)
    with open(DETAIL_FILE, "w", encoding="utf-8") as f:
        json.dump(detail, f, ensure_ascii=False, indent=2)

    summary = build_summary(df, series, fx, detail, BASE_DATE)
    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    build_api(series, summary)
    logger.info(f"Detalle, resumen y mini-API generados")
    return series


# ── Compatibilidad con main.py ───────────────────────────────────────────────

def calculate_index(week_date: str = None) -> dict:
    series = rebuild_index()
    if series.empty:
        return None
    if week_date and week_date in series["date"].values:
        row = series[series["date"] == week_date].iloc[0]
    else:
        row = series.iloc[-1]
    return row.to_dict()


def save_index(result: dict):
    logger.debug("save_index: serie ya persistida por rebuild_index()")


def print_report(result: dict):
    print("\n" + "=" * 60)
    print("  ÍNDICE CANASTA BÁSICA ALIMENTARIA VENEZUELA")
    print("=" * 60)
    print(f"  Fecha:             {result['date']}")
    print(f"  Fecha base:        {result['base_date']} = 100")
    print(f"  Índice (USD):      {result['index_usd']:.2f}")
    if result.get('index_bs') and pd.notna(result['index_bs']):
        print(f"  Índice (Bs):       {result['index_bs']:.2f}")
    change = result.get('weekly_change_pct') or 0.0
    arrow = "+" if change > 0 else ("-" if change < 0 else "=")
    print(f"  Cambio semanal:    {arrow} {change:+.2f}%")
    print(f"  Costo canasta:     ${result.get('basket_cost_usd', 0):.2f}")
    print(f"  Productos:         {result['products_priced']}/{result['products_total']}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    series = rebuild_index()
    if not series.empty:
        for _, r in series.iterrows():
            bs = f"{r['index_bs']:>8.2f}" if pd.notna(r['index_bs']) else "    n/a"
            print(f"{r['date']}  USD={r['index_usd']:>7.2f}  Bs={bs}  "
                  f"sem={r['weekly_change_pct']:>+6.2f}%  ${r['basket_cost_usd']:.2f}")
