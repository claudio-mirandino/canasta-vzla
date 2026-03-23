"""
Scraper para Excelsior Gama - gamaenlinea.com

Estrategia:
1. Busca cada producto usando la URL de búsqueda de Gama
2. Espera a que carguen los resultados (JavaScript)
3. Toma el primer producto que mejor coincida con el nombre buscado
4. Extrae el precio

Si los selectores cambian (el sitio rediseña), buscar:
- Elementos con clase "product-item", "product-card", o similar
- Elemento de precio: buscar clase que contenga "price" o "precio"
"""

import time
import logging
from playwright.sync_api import Page
from scrapers.base import BaseScraper

logger = logging.getLogger("gama")


class GamaScraper(BaseScraper):

    STORE_NAME = "gama"
    BASE_URL = "https://gamaenlinea.com/es"

    def scrape_product(self, product: dict) -> dict:
        """Busca un producto en Gama y retorna su precio."""
        search_term = product["search_terms"]["gama"]
        search_url = f"{self.BASE_URL}/search?text={search_term.replace(' ', '+')}"

        page = self.new_page()
        result = {
            "product_id": product["id"],
            "store": self.STORE_NAME,
            "price_usd": None,
            "price_original": "",
            "currency_original": "USD",
            "product_name_found": "",
            "url_found": search_url,
            "flagged": False,
            "flag_reason": "",
        }

        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=30000)

            # Esperar a que carguen los productos (JavaScript)
            # Intentamos varios selectores comunes de tiendas basadas en hybris/SAP (que usa Gama)
            selectors_to_try = [
                ".product-item",
                ".product-list-item",
                "[class*='product-item']",
                "[class*='ProductItem']",
                "article.product",
                ".cx-product-container",  # SAP Commerce Cloud selector
            ]

            loaded = False
            for selector in selectors_to_try:
                try:
                    page.wait_for_selector(selector, timeout=8000)
                    loaded = True
                    logger.debug(f"Selector encontrado: {selector}")
                    break
                except Exception:
                    continue

            if not loaded:
                logger.warning(f"[gama] No se cargaron productos para '{search_term}'. Guardando screenshot.")
                self.save_screenshot(page, f"no_products_{product['id']}")
                result["flagged"] = True
                result["flag_reason"] = "No se encontraron productos en la búsqueda"
                return result

            # Intentar extraer nombre y precio del primer resultado
            price_text, product_name = self._extract_first_product(page, search_term)

            if price_text:
                price = self.parse_price(price_text)
                if price and price > 0:
                    result["price_usd"] = round(price, 2)
                    result["price_original"] = price_text
                    result["product_name_found"] = product_name
                    logger.info(f"[gama] {product['id']}: {product_name} → ${price:.2f}")
                else:
                    logger.warning(f"[gama] No se pudo parsear el precio '{price_text}' para {product['id']}")
                    self.save_screenshot(page, f"price_parse_error_{product['id']}")
                    result["flagged"] = True
                    result["flag_reason"] = f"Precio no parseado: '{price_text}'"
            else:
                logger.warning(f"[gama] Precio no encontrado para '{search_term}'")
                self.save_screenshot(page, f"no_price_{product['id']}")
                result["flagged"] = True
                result["flag_reason"] = "Precio no encontrado en página"

        except Exception as e:
            logger.error(f"[gama] Error en {product['id']}: {e}")
            self.save_screenshot(page, f"error_{product['id']}")
            result["flagged"] = True
            result["flag_reason"] = str(e)
        finally:
            page.close()

        return result

    def _extract_first_product(self, page: Page, search_term: str) -> tuple[str, str]:
        """
        Intenta extraer nombre y precio del primer resultado de búsqueda.
        Retorna (precio_texto, nombre_producto).

        Estrategias en orden de preferencia para SAP Commerce Cloud (hybris):
        """
        # Estrategia 1: SAP Commerce Cloud / hybris selectors
        try:
            # Buscar elementos de precio en el primer item
            price_selectors = [
                ".price .value",
                "[class*='price'] .value",
                ".cx-price .value",
                ".product-price",
                "[class*='Price']",
                "span[class*='price']",
                "[itemprop='price']",
                ".price",
            ]
            name_selectors = [
                ".product-name",
                "[class*='product-name']",
                ".cx-product-name",
                "h2.name",
                "[class*='Name']",
                "[itemprop='name']",
                ".name",
            ]

            price_text = ""
            product_name = ""

            for sel in price_selectors:
                try:
                    el = page.query_selector(sel)
                    if el:
                        text = el.inner_text().strip()
                        if text and any(c.isdigit() for c in text):
                            price_text = text
                            break
                except Exception:
                    continue

            for sel in name_selectors:
                try:
                    el = page.query_selector(sel)
                    if el:
                        text = el.inner_text().strip()
                        if text:
                            product_name = text
                            break
                except Exception:
                    continue

            if price_text:
                return price_text, product_name

        except Exception as e:
            logger.debug(f"Estrategia 1 falló: {e}")

        # Estrategia 2: Buscar cualquier elemento con número que parezca precio
        try:
            # Obtener todo el texto de la página y buscar patrones de precio
            content = page.content()
            import re
            # Buscar patrones como $1.50, $12.99, USD 5.00
            price_matches = re.findall(
                r'\$\s*(\d+[.,]\d{2})|(\d+[.,]\d{2})\s*\$|USD\s*(\d+[.,]\d{2})',
                content
            )
            if price_matches:
                for match in price_matches:
                    val = next(v for v in match if v)
                    price_text = f"${val}"
                    return price_text, f"Producto ({search_term})"
        except Exception as e:
            logger.debug(f"Estrategia 2 falló: {e}")

        return "", ""
