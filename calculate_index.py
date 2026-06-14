"""
Cálculo del Índice de Precios de la Canasta Básica Alimentaria — Venezuela

METODOLOGÍA
===========
Índice encadenado de relativos por serie tienda-producto, alineado con la
práctica de institutos de estadística (INE/BLS/Eurostat) y adaptado a precios
recolectados en línea (cf. Billion Prices Project / PriceStats).

  1. NORMALIZACIÓN A PRECIO POR UNIDAD. Cada precio se divide por el tamaño del
     producto para obtener $/kg, $/l o $/unidad. Así se comparan artículos de
     distinta presentación entre tiendas (900 g vs 1 kg vs 2 kg) y se neutraliza
     el cambio de tamaño dentro de una misma serie (cuasi ajuste por cantidad).
  2. RELATIVO POR SERIE (tienda, producto): r = pu(t)/pu(t-1), solo si esa misma
     serie tiene precio unitario en t y t-1. Cada tienda se compara consigo misma.
  3. RELATIVO DE PRODUCTO: media geométrica (Jevons) de los relativos de tienda.
  4. AGREGACIÓN LASPEYRES: media geométrica ponderada por la PARTICIPACIÓN EN EL
     GASTO en el período base (cantidad mensual del hogar × precio unitario base).
  5. ENCADENAMIENTO: I(t) = I(t-1) × factor. Base 2026-06-12 = 100.

Costo de la canasta = costo MENSUAL para una familia de referencia (~5 personas):
  Σ (cantidad_mensual_i × precio_unitario_mediano_i).

SALIDAS: index.csv, detail.json, summary.json, api/index.json, api/latest.json
"""

import json
import logging
import math
import pandas as pd
from pathlib import Path
from datetime import datetime

from scrapers.matching import unit_price

logger = logging.getLogger("calculate_index")

PRICES_FILE = Path("docs/data/prices_raw.csv")
FX_FILE = Path("docs/data/fx.csv")
INDEX_FILE = Path("docs/data/index.csv")
DETAIL_FILE = Path("docs/data/detail.json")
SUMMARY_FILE = Path("docs/data/summary.json")
API_DIR = Path("docs/data/api")
BASKET_FILE = Path("docs/data/basket.json")

BASE_DATE = "2026-06-12"
MAX_REL = 2.0
MIN_REL = 0.5


# ── Carga ──────────────────────────────────────────────────────────────────

def load_basket() -> dict:
    """{id: {name, category, ref_unit, household_qty, spec}}"""
    with open(BASKET_FILE, "r", encoding="utf-8") as f:
        basket = json.load(f)
    meta = {}
    for p in basket["products"]:
        meta[p["id"]] = {
            "name": p["name"],
            "category": p.get("category", ""),
            "ref_unit": p.get("ref_unit", "kg"),
            "household_qty": float(p.get("household_qty_month", 0) or 0),
            "spec": p.get("spec", ""),
        }
    return meta


def load_prices(meta: dict, prices_file: Path = PRICES_FILE) -> pd.DataFrame:
    """Carga precios y añade columna unit_price (precio por unidad de referencia)."""
    if not prices_file.exists():
        return pd.DataFrame(columns=["date", "product_id", "store", "unit_price"])
    df = pd.read_csv(prices_file)
    df["date"] = df["date"].astype(str)
    df["price_usd"] = pd.to_numeric(df["price_usd"], errors="coerce")
    if "product_name_found" not in df.columns:
        df["product_name_found"] = ""

    def to_unit(row):
        m = meta.get(row["product_id"])
        if not m or pd.isna(row["price_usd"]):
            return None
        return unit_price(row["price_usd"], str(row.get("product_name_found", "")), m["ref_unit"])

    df["unit_price"] = df.apply(to_unit, axis=1)
    return df


def load_fx() -> dict:
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


def _valid(df: pd.DataFrame) -> pd.DataFrame:
    """Precios unitarios usables (>0), promediando duplicados por re-corridas."""
    d = df[df["unit_price"] > 0].copy()
    return d.groupby(["date", "product_id", "store"], as_index=False)["unit_price"].mean()


def _median(vals: list) -> float:
    v = sorted(vals); n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


# ── Serie del índice ─────────────────────────────────────────────────────────

def compute_series(df: pd.DataFrame, meta: dict, fx: dict, base_date: str) -> pd.DataFrame:
    d = _valid(df)
    if d.empty:
        return pd.DataFrame()
    d = d[d["date"] >= base_date]
    if d.empty:
        return pd.DataFrame()

    dates = sorted(d["date"].unique())
    total_products = len(meta)

    # price[date][pid][store] = precio unitario
    price = {}
    for _, row in d.iterrows():
        price.setdefault(row["date"], {}).setdefault(row["product_id"], {})[row["store"]] = row["unit_price"]

    # Pesos Laspeyres = participación en el gasto en la base:
    #   w_i = cantidad_mensual_i × precio_unitario_mediano_base_i
    base_prices = price.get(base_date, {})
    weights = {}
    for pid, m in meta.items():
        stores = base_prices.get(pid, {})
        if stores and m["household_qty"] > 0:
            weights[pid] = m["household_qty"] * _median(list(stores.values()))
    if not weights:  # respaldo: cantidades como peso si no hay precios base
        weights = {pid: m["household_qty"] for pid, m in meta.items() if m["household_qty"] > 0}

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

        # Costo MENSUAL de la canasta familiar: Σ cantidad × precio unitario mediano
        cost = 0.0
        for pid, m in meta.items():
            stores = price.get(dt, {}).get(pid, {})
            if stores and m["household_qty"] > 0:
                cost += m["household_qty"] * _median(list(stores.values()))

        rows.append({"date": dt, "_factor": factor,
                     "products_priced": len(products_used),
                     "products_total": total_products,
                     "basket_cost_usd": round(cost, 2)})
        prev_date = dt

    series = pd.DataFrame(rows)

    cum, acc = [], 1.0
    for i in range(len(series)):
        acc = acc * series.iloc[i]["_factor"] if i > 0 else 1.0
        cum.append(acc)
    series["_cum"] = cum
    base_cum = (series.loc[series["date"] == base_date, "_cum"].iloc[0]
                if base_date in series["date"].values else series["_cum"].iloc[0])
    series["index_usd"] = (series["_cum"] / base_cum * 100).round(2)

    base_oficial = (fx.get(base_date) or {}).get("oficial")
    def bs_index(r):
        of = (fx.get(r["date"]) or {}).get("oficial")
        return round(r["index_usd"] * of / base_oficial, 2) if of and base_oficial else None
    series["index_bs"] = series.apply(bs_index, axis=1)

    series["weekly_change_pct"] = (series["index_usd"].pct_change() * 100).round(2)
    series.loc[series.index[0], "weekly_change_pct"] = 0.0
    series["base_date"] = base_date
    series["notes"] = series.apply(
        lambda r: (f"{r['products_priced']}/{r['products_total']} productos con variación"
                   if r["products_total"] - r["products_priced"] > 0 else ""), axis=1)

    return series[["date", "index_usd", "index_bs", "weekly_change_pct",
                   "basket_cost_usd", "products_priced", "products_total",
                   "base_date", "notes"]]


# ── Detalle por producto (precios unitarios) ─────────────────────────────────

def build_detail(df: pd.DataFrame, meta: dict) -> dict:
    d = _valid(df)
    if d.empty:
        return {}
    dates = sorted(d["date"].unique())
    last = dates[-1]
    prev = dates[-2] if len(dates) > 1 else None

    products = []
    for pid, m in meta.items():
        cur = d[(d["date"] == last) & (d["product_id"] == pid)]
        if cur.empty:
            continue
        by_store = {r["store"]: round(r["unit_price"], 2) for _, r in cur.iterrows()}
        rep = round(_median(list(by_store.values())), 2)
        change = None
        if prev is not None:
            prv = d[(d["date"] == prev) & (d["product_id"] == pid)]
            if not prv.empty:
                pm = _median(list(prv["unit_price"]))
                if pm > 0:
                    change = round((rep - pm) / pm * 100, 2)
        products.append({"id": pid, "name": m["name"], "spec": m["spec"],
                         "ref_unit": m["ref_unit"], "household_qty": m["household_qty"],
                         "price_usd": rep, "by_store": by_store, "weekly_change_pct": change})
    return {"last_date": last, "prev_date": prev,
            "stores": sorted(d[d["date"] == last]["store"].unique().tolist()),
            "products": products}


# ── Métricas + dispersión ────────────────────────────────────────────────────

def _pct_between(series: pd.DataFrame, last_date: str, days: int) -> float | None:
    last_dt = datetime.fromisoformat(last_date)
    target = last_dt.timestamp() - days * 86400
    cand = series[series["date"] < last_date].copy()
    if cand.empty:
        return None
    cand["ts"] = cand["date"].apply(lambda s: datetime.fromisoformat(s).timestamp())
    cand = cand[cand["ts"] <= target]
    if cand.empty:
        return None
    cur = series[series["date"] == last_date]["index_usd"].iloc[0]
    return round((cur / cand.iloc[-1]["index_usd"] - 1) * 100, 2)


def build_summary(df: pd.DataFrame, series: pd.DataFrame, fx: dict, meta: dict, base_date: str) -> dict:
    if series.empty:
        return {}
    last = series.iloc[-1]
    last_date = last["date"]
    days = max(1, (datetime.fromisoformat(last_date) - datetime.fromisoformat(base_date)).days)
    annualized = (round(((last["index_usd"] / 100) ** (365 / days) - 1) * 100, 1)
                  if days >= 7 else None)

    rates = dict(fx.get(last_date) or (fx.get(max(fx)) if fx else {}) or {})
    if rates:
        rates["date"] = last_date if last_date in fx else (max(fx) if fx else None)

    # Dispersión: costo familiar por tienda usando productos comunes
    d = _valid(df)
    dl = d[d["date"] == last_date]
    stores = sorted(dl["store"].unique().tolist())
    prod_by_store = {s: set(dl[dl["store"] == s]["product_id"]) for s in stores}
    common = set.intersection(*prod_by_store.values()) if prod_by_store else set()
    store_cost = {}
    for s in stores:
        sub = dl[(dl["store"] == s) & (dl["product_id"].isin(common))]
        tot = sum(meta[r["product_id"]]["household_qty"] * r["unit_price"]
                  for _, r in sub.iterrows() if r["product_id"] in meta)
        store_cost[s] = round(tot, 2)
    costs = list(store_cost.values())
    dispersion = None
    if costs:
        mean = sum(costs) / len(costs)
        cv = round((sum((c - mean) ** 2 for c in costs) / len(costs)) ** 0.5 / mean * 100, 1) if mean else None
        dispersion = {"by_store": store_cost, "common_products": len(common),
                      "min": min(costs), "median": round(_median(costs), 2),
                      "max": max(costs), "cv_pct": cv}

    return {
        "base_date": base_date, "last_date": last_date, "weeks": len(series),
        "household_size": 5,
        "index_usd": last["index_usd"],
        "index_bs": last["index_bs"] if pd.notna(last["index_bs"]) else None,
        "basket_cost_usd": last["basket_cost_usd"],
        "basket_cost_bs_oficial": (round(last["basket_cost_usd"] * rates["oficial"], 2)
                                   if rates.get("oficial") else None),
        "basket_cost_bs_paralelo": (round(last["basket_cost_usd"] * rates["paralelo"], 2)
                                    if rates.get("paralelo") else None),
        "coverage": f"{int(last['products_priced'])}/{int(last['products_total'])}",
        "stores": stores,
        "metrics": {
            "weekly_pct": last["weekly_change_pct"],
            "monthly_pct": _pct_between(series, last_date, 30),
            "accumulated_pct": round(last["index_usd"] - 100, 2),
            "annualized_pct": annualized,
            "interannual_pct": _pct_between(series, last_date, 365),
            "accumulated_bs_pct": round(last["index_bs"] - 100, 2) if pd.notna(last["index_bs"]) else None,
        },
        "rates": rates,
        "dispersion": dispersion,
    }


def build_api(series: pd.DataFrame, summary: dict, fx: dict, api_dir: Path = API_DIR, base_date: str = BASE_DATE):
    api_dir.mkdir(parents=True, exist_ok=True)
    serie = [{"date": r["date"], "index_usd": r["index_usd"],
              "index_bs": (r["index_bs"] if pd.notna(r["index_bs"]) else None),
              "weekly_change_pct": r["weekly_change_pct"],
              "basket_cost_usd": r["basket_cost_usd"]} for _, r in series.iterrows()]
    with open(api_dir / "index.json", "w", encoding="utf-8") as f:
        json.dump({"base_date": base_date, "series": serie}, f, ensure_ascii=False, indent=2)
    fx_series = [{"date": dt, **vals} for dt, vals in sorted(fx.items())]
    with open(api_dir / "fx.json", "w", encoding="utf-8") as f:
        json.dump({"series": fx_series}, f, ensure_ascii=False, indent=2)
    with open(api_dir / "latest.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)


# ── Orquestación ─────────────────────────────────────────────────────────────

def rebuild_index(data_dir: Path = None, base_date: str = BASE_DATE) -> pd.DataFrame:
    """
    Reconstruye el índice de una ciudad. data_dir define dónde están sus archivos
    (prices_raw.csv) y dónde se escriben (index.csv, detail.json, summary.json, api/).
    basket.json y fx.csv son globales (compartidos entre ciudades).
    """
    data_dir = Path(data_dir) if data_dir else INDEX_FILE.parent
    prices_file = data_dir / "prices_raw.csv"
    index_file = data_dir / "index.csv"
    detail_file = data_dir / "detail.json"
    summary_file = data_dir / "summary.json"
    api_dir = data_dir / "api"

    meta = load_basket()
    df = load_prices(meta, prices_file)
    fx = load_fx()
    if df.empty:
        logger.error(f"No hay datos de precios en {prices_file}.")
        return pd.DataFrame()

    series = compute_series(df, meta, fx, base_date)
    if series.empty:
        logger.error("No se pudo construir la serie del índice.")
        return series

    data_dir.mkdir(parents=True, exist_ok=True)
    series.to_csv(index_file, index=False)
    logger.info(f"Índice reconstruido: {index_file} ({len(series)} semanas)")

    detail = build_detail(df, meta)
    with open(detail_file, "w", encoding="utf-8") as f:
        json.dump(detail, f, ensure_ascii=False, indent=2)
    summary = build_summary(df, series, fx, meta, base_date)
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    build_api(series, summary, fx, api_dir, base_date)
    logger.info("Detalle, resumen y mini-API generados")
    return series


# ── Compatibilidad con main.py ───────────────────────────────────────────────

def calculate_index(week_date: str = None, data_dir: Path = None, base_date: str = BASE_DATE) -> dict:
    series = rebuild_index(data_dir, base_date)
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
    print(f"  Cambio semanal:    {change:+.2f}%")
    print(f"  Costo canasta/mes: ${result.get('basket_cost_usd', 0):.2f} (familia ref. ~5)")
    print(f"  Productos:         {result['products_priced']}/{result['products_total']}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    series = rebuild_index()
    if not series.empty:
        for _, r in series.iterrows():
            bs = f"{r['index_bs']:>8.2f}" if pd.notna(r['index_bs']) else "    n/a"
            print(f"{r['date']}  USD={r['index_usd']:>7.2f}  Bs={bs}  "
                  f"sem={r['weekly_change_pct']:>+6.2f}%  canasta/mes=${r['basket_cost_usd']:.2f}")
