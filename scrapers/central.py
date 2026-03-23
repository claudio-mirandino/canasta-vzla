"""
Scraper para Central Madeirense - tucentralonline.com

Estrategia:
1. Usa la búsqueda de WooCommerce (?s=TERM&post_type=product)
2. Extrae nombre y precio del primer resultado relevante
3. Fallback: navegar a categorías específicas

Nota: robots.txt restringe muchos bots, pero Playwright con browser completo
simula un usuario real con user-agent normal.
"""

import time
import logging
from playwright.sync_api import Page
from scrapers.base import BaseScraper

logger = logging.getLogger("central")


class CentralScraper(BaseScraper):

    STORE_NAME = "central"
    BASE_URL = "https://tucentralonline.com"

    # Mapeo de categorías de WooCommerce para búsqueda directa
    # (útil si la búsqueda falla)
    CATEGORY_URLS = {
        "cereales":   "/comprar/despensa/cereales-y-harinas/",
        "granos":     "/comprar/despensa/granos-y-menestras/",
        "grasas":     "/comprar/despensa/aceites-y-condimentos/",
        "proteinas":  "/comprar/carniceria-pescaderia/",
        "lacteos":    "/comprar/lacteos-y-derivados/",
        "vegetales":  "/comprar/frutas-y-vegetales/",
        "bebidas":    "/comprar/bebidas/cafe-y-chocolate/",
        "condimentos": "/comprar/despensa/aceites-y-condimentos/",
        "endulzantes": "/comprar/despensa/endulzantes/",
    }

    def scrape_product(self, product: dict) -> dict:
        """Busca un producto en Central Madeirense y retorna su precio."""
        search_term = product["search_terms"]["central"]
        category = product.get("category", "")

        result = {
            "product_id": product["id"],
            "store": self.STORE_NAME,
            "price_usd": None,
            "price_original": "",
            "currency_original": "USD",
            "product_name_found": "",
            "url_found": "",
            "flagged": False,
            "flag_reason": "",
        }

        page = self.new_page()
        try:
            # Estrategia 1: Búsqueda WooCommerce
            price_text, product_name, found_url = self._search_woocommerce(
                page, search_term, product["id"]
            )

            # Estrategia 2: Navegar a categoría si búsqueda falló
            if not price_text and category in self.CATEGORY_URLS:
                logger.info(f"[central] Intentando por categoría '{category}' para {product['id']}")
                price_text, product_name, found_url = self._search_by_category(
                    page, category, search_term, product["id"]
                )

            result["url_found"] = found_url

            if price_text:
                price = self.parse_price(price_text)
                if price and price > 0:
                    result["price_usd"] = round(price, 2)
                    result["price_original"] = price_text
                    result["product_name_found"] = product_name
                    logger.info(f"[central] {product['id']}: {product_name} → ${price:.2f}")
                else:
                    result["flagged"] = True
                    result["flag_reason"] = f"Precio no parseado: '{price_text}'"
                    self.save_screenshot(page, f"price_parse_{product['id']}")
            else:
                result["flagged"] = True
                result["flag_reason"] = "Producto no encontrado"
                self.save_screenshot(page, f"not_found_{product['id']}")

        except Exception as e:
            logger.error(f"[central] Error en {product['id']}: {e}")
            self.save_screenshot(page, f"error_{product['id']}")
            result["flagged"] = True
            result["flag_reason"] = str(e)
        finally:
            page.close()

        return result

    def _search_woocommerce(self, page: Page, search_term: str, product_id: str) -> tuple[str, str, str]:
        """
        Usa la búsqueda de WooCommerce.
        Retorna (precio_texto, nombre, url_usada).
        """
        search_url = f"{self.BASE_URL}/?s={search_term.replace(' ', '+')}&post_type=product"

        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
            time.sleep(2)

            # WooCommerce usa estos selectores estándar:
            product_selectors = [
                "li.product",
                ".products li",
                "[class*='product-type']",
                ".woocommerce-loop-product__title",
            ]

            loaded = False
            for sel in product_selectors:
                try:
                    page.wait_for_selector(sel, timeout=5000)
                    loaded = True
                    break
                except Exception:
                    continue

            if not loaded:
                logger.debug(f"[central] Búsqueda sin resultados WooCommerce para '{search_term}'")
                return "", "", search_url

            # Extraer precio y nombre del primer resultado
            price_text, name = self._extract_woocommerce_product(page)
            return price_text, name, search_url

        except Exception as e:
            logger.debug(f"[central] Búsqueda WooCommerce falló: {e}")
            return "", "", search_url

    def _search_by_category(self, page: Page, category: str, search_term: str, product_id: str) -> tuple[str, str, str]:
        """
        Navega a la página de categoría y busca el producto por nombre.
        Retorna (precio_texto, nombre, url_usada).
        """
        cat_path = self.CATEGORY_URLS.get(category, "")
        if not cat_path:
            return "", "", ""

        cat_url = self.BASE_URL + cat_path
        try:
            page.goto(cat_url, wait_until="domcontentloaded", timeout=25000)
            time.sleep(2)

            # Intentar encontrar el producto por nombre en la lista de categoría
            # Buscar todos los productos visibles y filtrar por nombre
            all_products = page.query_selector_all("li.product, .product-item")
            search_lower = search_term.lower()

            for prod_el in all_products:
                try:
                    prod_text = prod_el.inner_text().lower()
                    if any(word in prod_text for word in search_lower.split()):
                        # Extraer precio de este elemento
                        price_el = prod_el.query_selector(
                            ".price, .woocommerce-Price-amount, [class*='price']"
                        )
                        name_el = prod_el.query_selector(
                            "h2, h3, .woocommerce-loop-product__title, [class*='title']"
                        )
                        if price_el:
                            price_text = price_el.inner_text().strip()
                            name = name_el.inner_text().strip() if name_el else search_term
                            return price_text, name, cat_url
                except Exception:
                    continue

        except Exception as e:
            logger.debug(f"[central] Categoría {category} falló: {e}")

        return "", "", cat_url

    def _extract_woocommerce_product(self, page: Page) -> tuple[str, str]:
        """
        Extrae nombre y precio del primer producto en resultados WooCommerce.
        """
        price_selectors = [
            ".woocommerce-Price-amount",       # WooCommerce estándar
            ".price .amount",
            "ins .woocommerce-Price-amount",    # precio en oferta
            ".price",
            "[class*='price']",
        ]
        name_selectors = [
            ".woocommerce-loop-product__title",
            "h2.woocommerce-loop-product__title",
            ".product_title",
            "h2", "h3",
        ]

        price_text = ""
        name = ""

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
                    if text and len(text) > 2:
                        name = text
                        break
            except Exception:
                continue

        return price_text, name
