"""
Scraper para Central Madeirense - tucentralonline.com

El sitio bloquea requests directas de Python (timeout/403), pero responde
correctamente a un browser real. Usamos Playwright + BeautifulSoup.

Estrategia: navegar a URL_categoría?s=término_de_búsqueda con Playwright,
extraer el HTML renderizado y parsearlo con BeautifulSoup.

URL base: https://tucentralonline.com/Av-Presidente-Medina-02/comprar/[categoria]/
Selectores: article.product (WooCommerce), h2.woocommerce-loop-product__title, .price
"""

import re
import time
import logging
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper

logger = logging.getLogger("central")

STORE_BASE = "https://tucentralonline.com/Av-Presidente-Medina-02/comprar"

# Mapeo de categorías del basket → URL base de Central
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
# None = producto no disponible en Central Madeirense
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


class CentralScraper(BaseScraper):

    STORE_NAME = "central"
    BASE_URL = STORE_BASE

    def scrape_product(self, product: dict) -> dict:
        """
        Busca un producto en Central Madeirense usando Playwright (no requests).
        Navega a la página de categoría con ?s=término y extrae el resultado.
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

        # Determinar URL base
        if product_id in PRODUCT_OVERRIDES:
            base_url = PRODUCT_OVERRIDES[product_id]
        else:
            base_url = CATEGORY_URLS.get(product.get("category", ""), "")

        if base_url is None:
            result["flag_reason"] = "Producto no disponible en Central Madeirense"
            logger.info(f"[central] {product_id}: no disponible en esta tienda")
            return result

        if not base_url:
            result["flagged"] = True
            result["flag_reason"] = "No hay URL de categoría configurada"
            return result

        # Buscador interno del sitio: ?s=término
        search_url = f"{base_url}?s={quote_plus(search_term)}"
        result["url_found"] = search_url

        page = self.new_page()
        try:
            # Usar Playwright para bypassear el bloqueo de Python requests
            page.goto(search_url, wait_until="domcontentloaded", timeout=45000)

            # Esperar a que la página termine de cargar (WooCommerce es server-rendered)
            try:
                page.wait_for_selector(
                    "article.product, li.product, .woocommerce-loop-product__title",
                    timeout=10000
                )
            except Exception:
                pass  # Continuar aunque no aparezca — el HTML podría tener resultados

            html = page.content()

            price_text, found_name = self._find_product_in_html(html, search_term)

            if price_text:
                price = self.parse_price(price_text)
                if price and price > 0:
                    result["price_usd"] = round(price, 2)
                    result["price_original"] = price_text
                    result["product_name_found"] = found_name
                    logger.info(f"[central] {product_id}: {found_name} → ${price:.2f}")
                    return result

            result["flagged"] = True
            result["flag_reason"] = f"Producto '{search_term}' no encontrado en {search_url}"
            self.save_screenshot(page, f"not_found_{product_id}")

        except Exception as e:
            logger.error(f"[central] Error en {search_url}: {e}")
            result["flagged"] = True
            result["flag_reason"] = f"Error: {e}"
            try:
                self.save_screenshot(page, f"error_{product_id}")
            except Exception:
                pass
        finally:
            page.close()

        return result

    def _find_product_in_html(self, html: str, search_term: str) -> tuple[str, str]:
        """
        Busca el producto más relevante en el HTML de la categoría.
        Usa BeautifulSoup para parsing robusto.
        Retorna (precio_texto, nombre_encontrado).

        El sitio usa WooCommerce:
          <article class="product ...">
            <h2 class="woocommerce-loop-product__title">Nombre</h2>
            <span class="price"><span class="woocommerce-Price-amount">$ 1,05</span></span>
        """
        soup = BeautifulSoup(html, "html.parser")

        # Selector primario: article.product (WooCommerce moderno)
        product_items = soup.find_all("article", class_=lambda c: c and "product" in c)
        if not product_items:
            product_items = soup.find_all("li", class_=lambda c: c and "product-col" in c)
        if not product_items:
            product_items = soup.find_all("li", class_=lambda c: c and "product" in c)

        if not product_items:
            logger.debug(f"[central] No se encontraron bloques de productos en la página")
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

            # Precio: .woocommerce-Price-amount o .price
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
