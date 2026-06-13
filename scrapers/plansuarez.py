"""
Scraper para Plan Suárez - plansuarez.com (sucursales Caracas: Trinidad, Caurimare, Urbina)

Reemplaza a El Plazas, que quedó tras Cloudflare anti-bot (no automatizable).

Plataforma: OpenCart, server-rendered. Se puede scrapear con requests puro
(sin navegador) → rápido y estable en GitHub Actions.

Búsqueda nativa: /index.php?route=product/search&search=TERM
Estructura de cada resultado:
    <div class="caption">
      <div class="name"><a ...>HARINA DE MAIZ JUANA 1KG BLANCA</a></div>
      <div class="price">
        <span class="price-normal">Bs.740.35</span>   (o price-new / price-old)
      </div>
    </div>

Los precios están en BOLÍVARES (formato US: "Bs.1,432.83"). Se convierten a USD
con la tasa oficial del BCV del día (scrapers/fx.py). Si no hay tasa, se omite.
"""

import re
import time
import logging
import requests
from urllib.parse import quote_plus
from bs4 import BeautifulSoup

from scrapers.base import BaseScraper
from scrapers.matching import pick_best, search_variants
from scrapers.fx import get_bcv_rate

logger = logging.getLogger("plansuarez")

SEARCH_URL = "https://www.plansuarez.com/index.php?route=product/search&search={term}"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/148.0.0.0 Safari/537.36"),
    "Accept-Language": "es-VE,es;q=0.9,en;q=0.8",
}


class PlansuarezScraper(BaseScraper):

    STORE_NAME = "plansuarez"
    BASE_URL = "https://www.plansuarez.com"

    def scrape_all(self, products: list, previous_prices: dict = None) -> list:
        """No usa navegador: requests puro. Convierte Bs→USD con tasa BCV."""
        if previous_prices is None:
            previous_prices = {}

        self._rate = get_bcv_rate()
        if not self._rate:
            logger.error("[plansuarez] Sin tasa BCV — se omite la tienda esta semana")
            return [self._empty(p, "Sin tasa BCV disponible") for p in products]

        self._session = requests.Session()
        self._session.headers.update(_HEADERS)

        results = []
        for product in products:
            self.logger.info(f"Scraping: {product['name']} ({self.STORE_NAME})")
            try:
                result = self.scrape_product(product)
                if result.get("price_usd") and result["price_usd"] > 0:
                    flagged, reason = self.check_anomaly(
                        product["id"], result["price_usd"], previous_prices)
                    if flagged and not result.get("flagged"):
                        result["flagged"] = True
                        result["flag_reason"] = reason
                results.append(result)
            except Exception as e:
                self.logger.error(f"Error scraping {product['id']}: {e}")
                results.append(self._empty(product, f"Error: {e}"))
            time.sleep(1.0)

        found = sum(1 for r in results if r.get("price_usd") and r["price_usd"] > 0)
        self.logger.info(f"plansuarez: {found}/{len(products)} productos encontrados "
                         f"(tasa BCV {self._rate})")
        return results

    def scrape_product(self, product: dict) -> dict:
        search_term = product["search_terms"].get("plansuarez", product["search_terms"].get("plaza", ""))
        product_id = product["id"]
        result = self._empty(product, "")
        result["url_found"] = SEARCH_URL.format(term=quote_plus(search_term))

        # El buscador de OpenCart hace AND y, si no hay match exacto, muestra
        # productos de relleno (papel higiénico, pollo…). Por eso probamos
        # variantes cada vez más cortas y acumulamos; las reglas de matching
        # descartan el relleno. La 1ª palabra suele dar los resultados reales.
        candidates: dict[str, float] = {}
        for variant in search_variants(search_term):
            url = SEARCH_URL.format(term=quote_plus(variant))
            try:
                resp = self._session.get(url, timeout=40)
                resp.raise_for_status()
            except Exception as e:
                logger.debug(f"[plansuarez] variante '{variant}' falló: {e}")
                continue
            for name, bs in self._extract_candidates(resp.text):
                candidates.setdefault(name, bs)
            if pick_best([(n, str(b)) for n, b in candidates.items()],
                         search_term, product.get("match")):
                break
            time.sleep(0.4)

        if not candidates:
            result["flagged"] = True
            result["flag_reason"] = f"Sin resultados para '{search_term}'"
            return result

        best = pick_best([(n, str(b)) for n, b in candidates.items()],
                         search_term, product.get("match"))
        if not best:
            result["flagged"] = True
            result["flag_reason"] = f"Ningún resultado cumple las reglas para '{search_term}'"
            return result

        name, bs_text, _ = best
        bs_value = float(bs_text)
        usd = round(bs_value / self._rate, 2)
        if usd <= 0:
            result["flagged"] = True
            result["flag_reason"] = "Precio convertido <= 0"
            return result

        result["price_usd"] = usd
        result["price_original"] = f"Bs.{bs_value:,.2f}"
        result["currency_original"] = "VES"
        result["product_name_found"] = name
        logger.info(f"[plansuarez] {product_id}: {name} → Bs.{bs_value:,.2f} = ${usd:.2f}")
        return result

    def _extract_candidates(self, html: str) -> list[tuple[str, float]]:
        """Extrae (nombre, precio_bs) de cada producto del grid de resultados."""
        soup = BeautifulSoup(html, "html.parser")
        out = []
        for caption in soup.select("div.caption"):
            name_tag = caption.select_one("div.name a") or caption.select_one(".name")
            if not name_tag:
                continue
            name = name_tag.get_text(strip=True)
            if not name:
                continue

            price_tag = (caption.select_one(".price-new")
                         or caption.select_one(".price-normal")
                         or caption.select_one(".price"))
            if not price_tag:
                continue
            bs = self._parse_bs(price_tag.get_text(strip=True))
            if bs and bs > 0:
                out.append((name, bs))
        return out

    @staticmethod
    def _parse_bs(text: str) -> float | None:
        """'Bs.1,432.83' → 1432.83 (formato US: coma=miles, punto=decimal)."""
        m = re.search(r'([\d,]+\.\d{2})', text)
        if not m:
            m = re.search(r'([\d,]+)', text)
            if not m:
                return None
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None

    def _empty(self, product: dict, reason: str) -> dict:
        return {
            "product_id": product["id"],
            "store": self.STORE_NAME,
            "price_usd": None,
            "price_original": "",
            "currency_original": "VES",
            "product_name_found": "",
            "url_found": "",
            "flagged": bool(reason),
            "flag_reason": reason,
        }
