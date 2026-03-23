"""
Scraper para Central Madeirense - tucentralonline.com

BUENA NOTICIA: Este sitio es server-rendered. NO necesita Playwright.
Usamos requests + parsing de HTML simple.

URL base: https://tucentralonline.com/Av-Presidente-Medina-02/comprar/[categoria]/
Selectores: li.product-col, h3 (nombre), .price (precio)
"""

import re
import time
import logging
import requests
from scrapers.base import BaseScraper

logger = logging.getLogger("central")

STORE_BASE = "https://tucentralonline.com/Av-Presidente-Medina-02/comprar"

# Mapeo de categorías del basket → URL de Central
CATEGORY_URLS = {
    "cereales":    f"{STORE_BASE}/viveres/harinas/",
    "granos":      f"{STORE_BASE}/viveres/arroz-y-granos/",
    "grasas":      f"{STORE_BASE}/viveres/margarinas-y-aceites/",
    "endulzantes": f"{STORE_BASE}/viveres/cafe-y-endulzantes/",
    "condimentos": f"{STORE_BASE}/viveres/cafe-y-endulzantes/",
    "bebidas":     f"{STORE_BASE}/viveres/cafe-y-endulzantes/",
    "proteinas":   f"{STORE_BASE}/refrigerados/carniceria-pescaderia/carnes-aves/",
    "lacteos":     f"{STORE_BASE}/viveres/lacteos-y-derivados/",
    "vegetales":   f"{STORE_BASE}/refrigerados/frutas-y-vegetales/",
}

# Para productos específicos que necesitan una URL diferente a la de su categoría
PRODUCT_OVERRIDES = {
    "sardinas":    f"{STORE_BASE}/viveres/enlatados/",
    "atun":        f"{STORE_BASE}/viveres/enlatados/",
    "pasta":       f"{STORE_BASE}/viveres/pastas/",
    "arroz":       f"{STORE_BASE}/viveres/arroz-y-granos/",
    "caraotas":    f"{STORE_BASE}/viveres/arroz-y-granos/",
    "lentejas":    f"{STORE_BASE}/viveres/arroz-y-granos/",
    "huevos":      f"{STORE_BASE}/refrigerados/huevos/",
    "leche_polvo": f"{STORE_BASE}/viveres/lacteos-y-derivados/",
    "queso_blanco":f"{STORE_BASE}/lacteos-y-derivados/",
    "mantequilla": f"{STORE_BASE}/viveres/margarinas-y-aceites/",
    "cafe":        f"{STORE_BASE}/viveres/cafe-y-endulzantes/",
    "azucar":      f"{STORE_BASE}/viveres/cafe-y-endulzantes/",
    "sal":         f"{STORE_BASE}/viveres/salsas-aderezos-condimentos-y-sopas/",
    "tomate":      f"{STORE_BASE}/refrigerados/frutas-y-vegetales/",
    "cebolla":     f"{STORE_BASE}/refrigerados/frutas-y-vegetales/",
    "papa":        f"{STORE_BASE}/refrigerados/frutas-y-vegetales/",
    "platano":     f"{STORE_BASE}/refrigerados/frutas-y-vegetales/",
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

        # Determinar URL a usar
        url = PRODUCT_OVERRIDES.get(product_id) or CATEGORY_URLS.get(product.get("category", ""), "")
        if not url:
            result["flagged"] = True
            result["flag_reason"] = "No hay URL de categoría configurada"
            return result

        # Intentar varias páginas si hay paginación
        for page in range(1, 4):
            page_url = url if page == 1 else f"{url}page/{page}/"
            result["url_found"] = page_url

            try:
                resp = requests.get(page_url, headers=HEADERS, timeout=20)
                if resp.status_code == 404:
                    if page == 1:
                        # Intentar URL alternativa sin trailing slash
                        alt_url = url.rstrip("/")
                        resp = requests.get(alt_url, headers=HEADERS, timeout=20)
                        if resp.status_code != 200:
                            result["flagged"] = True
                            result["flag_reason"] = f"URL 404: {page_url}"
                            return result
                    else:
                        break  # No hay más páginas

                if resp.status_code != 200:
                    logger.warning(f"[central] HTTP {resp.status_code} en {page_url}")
                    break

                # Buscar producto en el HTML
                price_text, found_name = self._find_product_in_html(
                    resp.text, search_term
                )

                if price_text:
                    price = self.parse_price(price_text)
                    if price and price > 0:
                        result["price_usd"] = round(price, 2)
                        result["price_original"] = price_text
                        result["product_name_found"] = found_name
                        logger.info(f"[central] {product_id}: {found_name} → ${price:.2f}")
                        return result

            except requests.RequestException as e:
                logger.error(f"[central] Error HTTP en {page_url}: {e}")
                result["flagged"] = True
                result["flag_reason"] = f"Error de red: {e}"
                return result

            time.sleep(0.5)

        result["flagged"] = True
        result["flag_reason"] = f"Producto '{search_term}' no encontrado en {url}"
        return result

    def _find_product_in_html(self, html: str, search_term: str) -> tuple[str, str]:
        """
        Busca el producto más relevante en el HTML de la categoría.
        Retorna (precio_texto, nombre_encontrado).

        Estructura del HTML:
          <li class="product-col ...">
            <h3>Nombre Del Producto</h3>
            <span class="price">$X.XX</span>
          </li>
        """
        # Extraer todos los bloques de producto
        # Pattern: desde <li class="product-col hasta el siguiente </li>
        product_blocks = re.findall(
            r'<li[^>]*class="[^"]*product-col[^"]*"[^>]*>(.*?)</li>',
            html,
            re.DOTALL | re.IGNORECASE
        )

        if not product_blocks:
            # Fallback: buscar bloques con class="product"
            product_blocks = re.findall(
                r'<li[^>]*class="[^"]*product[^"]*"[^>]*>(.*?)</li>',
                html,
                re.DOTALL | re.IGNORECASE
            )

        if not product_blocks:
            logger.debug(f"[central] No se encontraron bloques de productos")
            return "", ""

        search_words = search_term.lower().split()
        best_match = None
        best_score = 0

        for block in product_blocks:
            # Extraer nombre (dentro de <h3>)
            name_match = re.search(r'<h3[^>]*>(.*?)</h3>', block, re.DOTALL | re.IGNORECASE)
            if not name_match:
                continue
            raw_name = re.sub(r'<[^>]+>', '', name_match.group(1)).strip()

            # Calcular score de coincidencia
            name_lower = raw_name.lower()
            score = sum(1 for word in search_words if word in name_lower)

            if score == 0:
                continue

            # Extraer precio
            price_match = re.search(
                r'<(?:span|div)[^>]*class="[^"]*price[^"]*"[^>]*>(.*?)</(?:span|div)>',
                block,
                re.DOTALL | re.IGNORECASE
            )
            if not price_match:
                continue

            price_raw = re.sub(r'<[^>]+>', '', price_match.group(1)).strip()
            # Limpiar y quedarnos con el precio actual (el último número si hay tachado)
            prices_found = re.findall(r'\$?\d+[.,]\d{2}', price_raw)
            if not prices_found:
                continue

            # Tomar el precio más bajo (precio en oferta > precio regular)
            price_text = min(prices_found, key=lambda p: float(
                p.replace('$', '').replace(',', '.')
            ))

            if score > best_score:
                best_score = score
                best_match = (price_text, raw_name)

        if best_match:
            return best_match

        # Fallback: si hay cualquier precio en la página, tomar el primero
        # solo si buscamos un término muy específico
        any_price = re.search(r'\$(\d+\.\d{2})', html)
        if any_price and len(search_words) >= 2:
            return f"${any_price.group(1)}", f"(match aproximado para '{search_term}')"

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
