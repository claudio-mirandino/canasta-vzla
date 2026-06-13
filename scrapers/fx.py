"""
Tasa de cambio oficial BCV (Bs/USD).

Plan Suárez cotiza en bolívares; para mantener el índice en USD comparable
con Gama y Central (que ya cotizan en USD/Ref.) convertimos sus precios a
USD usando la tasa oficial del BCV del día de la recolección.

Fuente primaria: ve.dolarapi.com (espejo del BCV oficial).
Si falla, se intenta pydolarve. Si todo falla, devuelve None y el scraper
de Plan Suárez se omite esa semana (no se inventa una tasa).
"""

import logging
import requests

logger = logging.getLogger("fx")

_SOURCES = [
    ("https://ve.dolarapi.com/v1/dolares/oficial", lambda j: j.get("promedio")),
    ("https://pydolarve.org/api/v1/dollar?page=bcv",
     lambda j: j.get("monitors", {}).get("usd", {}).get("price")),
]

_cached_rate = None


def get_bcv_rate() -> float | None:
    """Devuelve la tasa Bs/USD del BCV, o None si ninguna fuente responde."""
    global _cached_rate
    if _cached_rate is not None:
        return _cached_rate

    headers = {"User-Agent": "Mozilla/5.0 (canasta-vzla index bot)"}
    for url, extract in _SOURCES:
        try:
            r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            rate = extract(r.json())
            if rate and float(rate) > 0:
                _cached_rate = round(float(rate), 4)
                logger.info(f"[fx] Tasa BCV: {_cached_rate} Bs/USD (fuente: {url})")
                return _cached_rate
        except Exception as e:
            logger.warning(f"[fx] Falló {url}: {e}")

    logger.error("[fx] No se pudo obtener la tasa BCV de ninguna fuente")
    return None
