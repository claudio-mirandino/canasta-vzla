"""
Tasas de cambio (Bs/USD) — oficial BCV, paralelo y brecha.

Dos usos:
  1. Convertir a USD los precios de Plan Suárez (que cotiza en bolívares).
  2. Alimentar el dashboard: índice en bolívares y seguimiento de la brecha
     cambiaria (paralelo vs oficial), el principal indicador de riesgo
     cambiario en Venezuela.

Fuente: ve.dolarapi.com (espejo del BCV oficial y del promedio paralelo).
Cada corrida agrega una fila a docs/data/fx.csv para construir el histórico.
"""

import logging
import requests
import pandas as pd
from pathlib import Path
from datetime import date

logger = logging.getLogger("fx")

FX_FILE = Path("docs/data/fx.csv")
_HEADERS = {"User-Agent": "Mozilla/5.0 (canasta-vzla index bot)"}

_cached = {}


def _fetch(endpoint: str) -> float | None:
    try:
        r = requests.get(f"https://ve.dolarapi.com/v1/dolares/{endpoint}",
                          headers=_HEADERS, timeout=20)
        r.raise_for_status()
        val = r.json().get("promedio")
        return round(float(val), 4) if val and float(val) > 0 else None
    except Exception as e:
        logger.warning(f"[fx] Falló {endpoint}: {e}")
        return None


def get_rates() -> dict:
    """Devuelve {oficial, paralelo, brecha_pct}. Valores None si no responden."""
    global _cached
    if _cached:
        return _cached
    oficial = _fetch("oficial")
    paralelo = _fetch("paralelo")
    brecha = None
    if oficial and paralelo and oficial > 0:
        brecha = round((paralelo - oficial) / oficial * 100, 2)
    _cached = {"oficial": oficial, "paralelo": paralelo, "brecha_pct": brecha}
    if oficial:
        logger.info(f"[fx] Oficial {oficial} | Paralelo {paralelo} | Brecha {brecha}%")
    return _cached


def get_bcv_rate() -> float | None:
    """Tasa oficial Bs/USD (compatibilidad con el scraper de Plan Suárez)."""
    return get_rates().get("oficial")


def save_rates(collection_date: str = None):
    """Agrega/actualiza la fila de tasas del día en fx.csv."""
    collection_date = collection_date or str(date.today())
    rates = get_rates()
    if not rates.get("oficial"):
        logger.warning("[fx] Sin tasa oficial — no se guarda fx.csv esta corrida")
        return

    FX_FILE.parent.mkdir(parents=True, exist_ok=True)
    row = {"date": collection_date, **rates}
    new = pd.DataFrame([row])

    if FX_FILE.exists():
        existing = pd.read_csv(FX_FILE)
        existing = existing[existing["date"].astype(str) != collection_date]
        out = pd.concat([existing, new], ignore_index=True)
        out = out.sort_values("date")
    else:
        out = new
    out.to_csv(FX_FILE, index=False)
    logger.info(f"[fx] Tasas guardadas en {FX_FILE} para {collection_date}")
