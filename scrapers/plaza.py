"""
Scraper para El Plazas - enlinea.elplazas.com / vallearriba.elplazas.com

Estrategia:
1. Accede a la tienda online de El Plazas (ubicación Valle Arriba por defecto)
2. Usa la búsqueda interna del sitio
3. Extrae precio del primer resultado relevante

El sitio bloquea requests directas (403) pero Playwright con browser completo
simula un usuario real y debe funcionar.

Si los selectores cambian, buscar elementos con "price", "precio", o el
símbolo $ cerca de un número.
"""

import time
import logging
from playwright.sync_api import Page
from scrapers.base import BaseScraper

logger = logging.getLogger("plaza")


class PlazaScraper(BaseScraper):

    STORE_NAME = "plaza"
    # Usamos la tienda online principal; si falla, intentamos la subdominio por ubicación
    BASE_URL = "https://enlinea.elplazas.com"
    FALLBACK_URL = "https://vallearriba.elplazas.com"

    def scrape_product(self, product: dict) -> dict:
        """Busca un producto en El Plazas y retorna su precio."""
        search_term = product["search_terms"]["plaza"]

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
            # Intentar URL principal
            base = self._get_working_base(page)
            if not base:
                result["flagged"] = True
                result["flag_reason"] = "Sitio no accesible (error de conexión)"
                return result

            result["url_found"] = base

            # Buscar el producto
            price_text, product_name = self._search_product(page, base, search_term, product["id"])

            if price_text:
                price = self.parse_price(price_text)
                if price and price > 0:
                    result["price_usd"] = round(price, 2)
                    result["price_original"] = price_text
                    result["product_name_found"] = product_name
                    logger.info(f"[plaza] {product['id']}: {product_name} → ${price:.2f}")
                else:
                    result["flagged"] = True
                    result["flag_reason"] = f"Precio no parseado: '{price_text}'"
                    self.save_screenshot(page, f"price_parse_{product['id']}")
            else:
                result["flagged"] = True
                result["flag_reason"] = "Producto no encontrado en búsqueda"
                self.save_screenshot(page, f"not_found_{product['id']}")

        except Exception as e:
            logger.error(f"[plaza] Error en {product['id']}: {e}")
            self.save_screenshot(page, f"error_{product['id']}")
            result["flagged"] = True
            result["flag_reason"] = str(e)
        finally:
            page.close()

        return result

    def _get_working_base(self, page: Page) -> str | None:
        """
        Prueba URLs hasta encontrar una que cargue.
        Retorna la URL base que funcionó, o None.
        """
        for url in [self.BASE_URL, self.FALLBACK_URL]:
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=20000)
                if response and response.status < 400:
                    logger.info(f"[plaza] Conectado a: {url}")
                    return url
                else:
                    logger.warning(f"[plaza] HTTP {response.status if response else 'None'} en {url}")
            except Exception as e:
                logger.warning(f"[plaza] Falló {url}: {e}")
        return None

    def _search_product(self, page: Page, base_url: str, search_term: str, product_id: str) -> tuple[str, str]:
        """
        Busca el producto en el sitio y retorna (precio_texto, nombre_producto).
        """
        # Estrategia 1: buscar campo de búsqueda en la página actual
        try:
            search_input_selectors = [
                "input[type='search']",
                "input[placeholder*='buscar' i]",
                "input[placeholder*='search' i]",
                "#search",
                "[name='search']",
                "[class*='search'] input",
            ]

            search_input = None
            for sel in search_input_selectors:
                try:
                    el = page.query_selector(sel)
                    if el:
                        search_input = el
                        break
                except Exception:
                    continue

            if search_input:
                search_input.fill(search_term)
                search_input.press("Enter")
                time.sleep(3)  # Esperar resultados

                price_text, name = self._extract_price_from_results(page)
                if price_text:
                    return price_text, name

        except Exception as e:
            logger.debug(f"Estrategia búsqueda interna falló: {e}")

        # Estrategia 2: URL de búsqueda directa
        try:
            search_url = f"{base_url}/?s={search_term.replace(' ', '+')}"
            page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
            time.sleep(2)

            price_text, name = self._extract_price_from_results(page)
            if price_text:
                return price_text, name
        except Exception as e:
            logger.debug(f"Estrategia URL directa falló: {e}")

        # Estrategia 3: Buscar en el HTML de la página cualquier patrón de precio
        try:
            price_text = self._extract_any_price(page)
            if price_text:
                return price_text, f"Producto ({search_term})"
        except Exception as e:
            logger.debug(f"Estrategia extracción genérica falló: {e}")

        self.save_screenshot(page, f"all_strategies_failed_{product_id}")
        return "", ""

    def _extract_price_from_results(self, page: Page) -> tuple[str, str]:
        """Extrae precio y nombre del primer resultado visible."""
        price_selectors = [
            ".price",
            "[class*='price']",
            "[class*='Price']",
            ".product-price",
            "span.amount",
            "[itemprop='price']",
        ]
        name_selectors = [
            ".product-title",
            ".product-name",
            "[class*='product-title']",
            "[class*='product-name']",
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

    def _extract_any_price(self, page: Page) -> str:
        """Último recurso: buscar patrón de precio en el HTML."""
        import re
        try:
            content = page.content()
            matches = re.findall(
                r'\$\s*(\d+[.,]\d{2})|(\d+[.,]\d{2})\s*\$',
                content
            )
            if matches:
                val = next(v for v in matches[0] if v)
                return f"${val}"
        except Exception:
            pass
        return ""
