"""
Scraper para Central Madeirense - tucentralonline.com

BUENA NOTICIA: Este sitio es server-rendered. NO necesita Playwright.
Usamos requests + BeautifulSoup + búsqueda interna del sitio (?s=término).

Estrategia: URL_de_categoría + ?s=término_de_búsqueda
Esto usa el propio buscador del sitio y elimina falsos positivos.

URL base: https://tucentralonline.com/Av-Presidente-Medina-02/comprar/[categoria]/
Selectores: li.product-col, h3 (nombre), .price (precio)
"""

import re
import time
import logging
import requests
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper

logger = logging.getLogger("central")

STORE_BASE = "https://tucentralonline.com/Av-Presidente-Medina-02/comprar"

# Mapeo de categorías del basket → URL base de Central
# Se le agregará ?s=término para búsqueda precisa
CATEGORY_URLS = {
    "cereales":    f"{STORE_BASE}/viveres/harinas/",
    "granos":      f"{STORE_BASE}/viveres/arroz-y-granos/",
    "grasas":      f"{STORE_BASE}/viveres/margarinas-y-aceites/",
    "endulzantes": f"{STORE_BASE}/viveres/cafe-y-endulzantes/",
    "condimentos": f"{STORE_BASE}/viveres/",
    "bebidas":     f"{STORE_BASE}/viveres/cafe-y-endulzantes/",
    "proteinas":   f"{STORE_BASE}/refrigerados/carniceria-pescaderia/carnes-aves/",
    "lacteos":     f"{STORE_BASE}/viveres/lacteos-y-derivados/",
    "vegetales":   f"{STORE_BASE}/fruteria-y-vegetales/vegetales/",
}

# Para productos específicos que necesitan una URL de categoría diferente
# El buscador ?s= se agrega dinámicamente en scrape_product
PRODUCT_OVERRIDES = {
    "sardinas":    None,  # No disponible en Central Madeirense
    "platano":     None,  # No disponible en Central Madeirense
    "atun":        f"{STORE_BASE}/viveres/enlatados/",
    "pasta":       f"{STORE_BASE}/viveres/pastas/",
    "arroz":       f"{STORE_BASE}/viveres/arroz-y-granos/",
    "caraotas":    f"{STORE_BASE}/viveres/arroz-y-granos/",
    "lentejas":    f"{STORE_BASE}/viveres/arroz-y-granos/",
    "huevos":      f"{STORE_BASE}/refrigerados/huevos/",
    "leche_polvo": f"{STORE_BASE}/viveres/lacteos-y-derivados/",
    "queso_blanco":f"{STORE_BASE}/charcuteria/quesos/",
    "mantequilla": f"{STORE_BASE}/viveres/margarinas-y-aceites/",
    "cafe":        f"{STORE_BASE}/viveres/cafe-y-endulzantes/",
    "azucar":      f"{STORE_BASE}/viveres/cafe-y-endulzantes/",
    "sal":         f"{STORE_BASE}/viveres/",
    "tomate":      f"{STORE_BASE}/fruteria-y-vegetales/vegetales/",
    "cebolla":     f"{STORE_BASE}/fruteria-y-vegetales/vegetales/",
    "papa":        f"{STORE_BASE}/fruteria-y-vegetales/vegetales/",
    "pollo":       f"{STORE_BASE}/refrigerados/carniceria-pescaderia/carnes-aves/",
    "carne_molida":f"{STORE_BASE}/refrigerados/carniceria-pescaderia/carnes-aves/",
    "aceite":      f"{STORE_BASE}/viveres/margarinas-y-aceites/",
    "margarina":   f"{STORE_BASE}/viveres/margarinas-y-aceites/",
    "harina_maiz": f"{STORE_BASE}/viveres/harinas/",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-VE,es;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class CentralScraper(BaseScraper):

    STORE_NAME = "central"
    BASE_URL = STORE_BASE

    def scrape_product(self, product: dict) -> dict:
        """
        Busca un producto en Central Madeirense usando requests (no Playwright).
        Navega a la página de categoría y busca el producto por nombre.
        """
        search_term = product["search_terms"]["central"]
        product_id = product["id"]

        result = {
            "product_id": product_id,
            "store": self.STORE_NAME,
            "price_usd": None,
            "price_original": "",
            "currency_original": "USD",
            "product_name_found": "",
            "url_found": "",
            "flagged": False,
            "flag_reason": "",
        }

        # Determinar URL base a usar
        # None = producto explícitamente no disponible en esta tienda
        if product_id in PRODUCT_OVERRIDES:
            base_url = PRODUCT_OVERRIDES[product_id]
        else:
            base_url = CATEGORY_URLS.get(product.get("category", ""), "")

        if base_url is None:
            result["flag_reason"] = f"Producto no disponible en Central Madeirense"
            logger.info(f"[central] {product_id}: no disponible en esta tienda")
            return result

        if not base_url:
            result["flagged"] = True
            result["flag_reason"] = "No hay URL de categoría configurada"
            return result

        # Usar el buscador interno del sitio: ?s=término
        # Esto filtra resultados desde el servidor, mucho más preciso que fuzzy matching
        search_url = f"{base_url}?s={quote_plus(search_term)}"
        result["url_found"] = search_url

        try:
            resp = requests.get(search_url, headers=HEADERS, timeout=45)
            if resp.status_code == 404:
                result["flagged"] = True
                result["flag_reason"] = f"URL 404: {search_url}"
                return result

            if resp.status_code != 200:
                result["flagged"] = True
                result["flag_reason"] = f"HTTP {resp.status_code}: {search_url}"
                logger.warning(f"[central] HTTP {resp.status_code} en {search_url}")
                return result

            # Buscar producto en los resultados
            price_text, found_name = self._find_product_in_html(resp.text, search_term)

            if price_text:
                price = self.parse_price(price_text)
                if price and price > 0:
                    result["price_usd"] = round(price, 2)
                    result["price_original"] = price_text
                    result["product_name_found"] = found_name
                    logger.info(f"[central] {product_id}: {found_name} → ${price:.2f}")
                    return result

        except requests.RequestException as e:
            logger.error(f"[central] Error HTTP en {search_url}: {e}")
            result["flagged"] = True
            result["flag_reason"] = f"Error de red: {e}"
            return result

        result["flagged"] = True
        result["flag_reason"] = f"Producto '{search_term}' no encontrado en {search_url}"
        return result

    def _find_product_in_html(self, html: str, search_term: str) -> tuple[str, str]:
        """
        Busca el producto más relevante en el HTML de la categoría.
        Usa BeautifulSoup para parsing robusto.
        Retorna (precio_texto, nombre_encontrado).

        El sitio usa estructura WooCommerce:
          <article class="product ...">
            <h2 class="woocommerce-loop-product__title">Nombre</h2>
            <span class="price"><span class="woocommerce-Price-amount">$ 1,05</span></span>
        """
        soup = BeautifulSoup(html, "html.parser")

        # Selector primario: article.product (WooCommerce nuevo)
        product_items = soup.find_all("article", class_=lambda c: c and "product" in c)
        if not product_items:
            # Fallback: li con "product-col" (estructura anterior)
            product_items = soup.find_all("li", class_=lambda c: c and "product-col" in c)
        if not product_items:
            product_items = soup.find_all("li", class_=lambda c: c and "product" in c)

        if not product_items:
            logger.debug(f"[central] No se encontraron bloques de productos en la pagina")
            return "", ""

        search_words = search_term.lower().split()
        best_match = None
        best_score = 0

        for item in product_items:
            # Nombre: .woocommerce-loop-product__title, h2, o h3
            name_tag = (
                item.find(class_=lambda c: c and "woocommerce-loop-product__title" in c)
                or item.find(["h2", "h3"])
            )
            if not name_tag:
                continue
            raw_name = name_tag.get_text(strip=True)
            if not raw_name:
                continue

            # Score de coincidencia
            name_lower = raw_name.lower()
            score = sum(1 for word in search_words if word in name_lower)
            if score == 0:
                continue

            # Precio: .woocommerce-Price-amount dentro de .price, o cualquier .price
            price_container = item.find(class_=lambda c: c and "price" in c)
            if not price_container:
                continue

            price_raw = price_container.get_text(strip=True)
            # Soporta $1,05 / # 1,05 / 1.05 USD / etc.
            prices_found = re.findall(r'[\$#]?\s*(\d+[.,]\d{2})', price_raw)
            if not prices_found:
                continue

            # Tomar el precio más bajo (oferta sobre regular)
            price_text = min(prices_found, key=lambda p: float(p.replace(',', '.')))

            if score > best_score:
                best_score = score
                best_match = (price_text, raw_name)

        if best_match:
            return best_match

        # Fallback: cualquier precio en la página
        any_price = re.search(r'[\$#]\s*(\d+[.,]\d{2})', html)
        if any_price and len(search_words) >= 2:
            return any_price.group(1), f"(match aproximado para '{search_term}')"

        return "", ""

    # Override: Central no usa Playwright, así que sobreescribimos scrape_all
    def scrape_all(self, products: list, previous_prices: dict = None) -> list:
        """Versión sin Playwright para Central."""
        if previous_prices is None:
            previous_prices = {}

        results = []
        for product in products:
            logger.info(f"Scraping: {product['name']} (central)")
            try:
                result = self.scrape_product(product)
                if result.get("price_usd") and result["price_usd"] > 0:
                    flagged, reason = self.check_anomaly(
                        product["id"], result["price_usd"], previous_prices
                    )
                    if flagged and not result.get("flagged"):
                        result["flagged"] = True
                        result["flag_reason"] = reason
                results.append(result)
            except Exception as e:
                logger.error(f"Error en central/{product['id']}: {e}")
                results.append({
                    "product_id": product["id"],
                    "store": self.STORE_NAME,
                    "price_usd": None,
                    "price_original": "",
                    "currency_original": "USD",
                    "product_name_found": "",
                    "url_found": "",
                    "flagged": True,
                    "flag_reason": str(e),
                })
            time.sleep(0.8)

        found = sum(1 for r in results if r.get("price_usd") and r["price_usd"] > 0)
        logger.info(f"central: {found}/{len(products)} productos encontrados")
        return results
