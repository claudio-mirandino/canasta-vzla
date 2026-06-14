"""
Scraper genérico para tiendas WooCommerce (requests + BeautifulSoup).

Muchos supermercados regionales de Venezuela usan WooCommerce con buscador
nativo /?s=término&post_type=product, sin Cloudflare. Esta clase se configura
por tienda (nombre, ciudad, URL base, moneda) y reutiliza el módulo de matching.

Uso:
    WooCommerceScraper(store_name="angelicas", city="maracaibo",
                       base_url="https://angelicasmarket.com", currency="USD")
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

logger = logging.getLogger("woocommerce")

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"),
    "Accept-Language": "es-VE,es;q=0.9,en;q=0.8",
}


class WooCommerceScraper(BaseScraper):

    def __init__(self, store_name: str, city: str, base_url: str, currency: str = "USD"):
        self.STORE_NAME = store_name
        self.BASE_URL = base_url.rstrip("/")
        self.city = city
        self.currency = currency.upper()  # "USD" o "VES"
        super().__init__()

    def scrape_all(self, products: list, previous_prices: dict = None) -> list:
        if previous_prices is None:
            previous_prices = {}

        self._rate = None
        if self.currency == "VES":
            self._rate = get_bcv_rate()
            if not self._rate:
                logger.error(f"[{self.STORE_NAME}] Sin tasa BCV — se omite la tienda")
                return [self._empty(p, "Sin tasa BCV") for p in products]

        self._session = requests.Session()
        self._session.headers.update(_HEADERS)

        results = []
        for product in products:
            self.logger.info(f"Scraping: {product['name']} ({self.STORE_NAME})")
            try:
                r = self.scrape_product(product)
                if r.get("price_usd") and r["price_usd"] > 0:
                    flagged, reason = self.check_anomaly(product["id"], r["price_usd"], previous_prices)
                    if flagged and not r.get("flagged"):
                        r["flagged"] = True; r["flag_reason"] = reason
                results.append(r)
            except Exception as e:
                self.logger.error(f"Error {product['id']}: {e}")
                results.append(self._empty(product, f"Error: {e}"))
            time.sleep(0.8)

        found = sum(1 for r in results if r.get("price_usd") and r["price_usd"] > 0)
        self.logger.info(f"{self.STORE_NAME}: {found}/{len(products)} productos encontrados")
        return results

    def scrape_product(self, product: dict) -> dict:
        # Acepta término propio de la tienda, o cae al de plansuarez/genérico
        terms = product.get("search_terms", {})
        search_term = terms.get(self.STORE_NAME) or terms.get("plansuarez") or terms.get("central") or product["name"]
        result = self._empty(product, "")
        result["url_found"] = self._search_url(search_term)

        candidates: dict[str, float] = {}
        for variant in search_variants(search_term):
            try:
                resp = self._session.get(self._search_url(variant), timeout=30)
                resp.raise_for_status()
            except Exception as e:
                logger.debug(f"[{self.STORE_NAME}] variante '{variant}' falló: {e}")
                continue
            for name, price in self._extract(resp.text):
                candidates.setdefault(name, price)
            if pick_best([(n, str(p)) for n, p in candidates.items()], search_term, product.get("match")):
                break
            time.sleep(0.3)

        if not candidates:
            result["flagged"] = True; result["flag_reason"] = f"Sin resultados para '{search_term}'"
            return result

        best = pick_best([(n, str(p)) for n, p in candidates.items()], search_term, product.get("match"))
        if not best:
            result["flagged"] = True; result["flag_reason"] = f"Ningún resultado cumple reglas para '{search_term}'"
            return result

        name, price_text, _ = best
        price = float(price_text)
        usd = round(price / self._rate, 2) if self.currency == "VES" else round(price, 2)
        if usd <= 0:
            result["flagged"] = True; result["flag_reason"] = "Precio <= 0"; return result

        result["price_usd"] = usd
        result["product_name_found"] = name
        if self.currency == "VES":
            result["currency_original"] = "VES"; result["price_original"] = f"Bs.{price:,.2f}"
        else:
            result["price_original"] = f"${price:.2f}"
        logger.info(f"[{self.STORE_NAME}] {product['id']}: {name} → ${usd:.2f}")
        return result

    def _search_url(self, term: str) -> str:
        return f"{self.BASE_URL}/?s={quote_plus(term)}&post_type=product"

    def _extract(self, html: str) -> list[tuple[str, float]]:
        """Extrae (nombre, precio_numérico) de los productos del grid WooCommerce."""
        soup = BeautifulSoup(html, "html.parser")
        items = soup.select("ul.products li.product") or soup.select("li.product")
        out = []
        for li in items:
            t = li.select_one(".woocommerce-loop-product__title") or li.select_one("h2, h3")
            if not t:
                continue
            name = t.get_text(strip=True)
            if not name or len(name) < 3:
                continue
            # Precio: preferir oferta (ins), si no el .amount
            pe = li.select_one(".price ins .woocommerce-Price-amount") or \
                 li.select_one(".price .woocommerce-Price-amount") or li.select_one(".price")
            if not pe:
                continue
            val = self._parse_price(pe.get_text(" ", strip=True))
            if val and val > 0:
                out.append((name, val))
        return out

    @staticmethod
    def _parse_price(text: str) -> float | None:
        """Parsea precios tipo '2,28 $', '$2.28', 'Bs. 1.234,56', '$1,234.56'."""
        m = re.search(r'(\d[\d.,]*\d|\d)', text)
        if not m:
            return None
        s = m.group(1)
        if "," in s and "." in s:
            # el último separador es el decimal
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif "," in s:
            s = s.replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None

    def _empty(self, product: dict, reason: str) -> dict:
        return {
            "product_id": product["id"], "store": self.STORE_NAME,
            "price_usd": None, "price_original": "", "currency_original": self.currency,
            "product_name_found": "", "url_found": "",
            "flagged": bool(reason), "flag_reason": reason,
        }
