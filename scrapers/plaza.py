"""
Scraper para El Plazas - enlinea.elplazas.com

Estrategia:
1. Navega a la tienda online de El Plazas (Valle Arriba)
2. Usa la URL de búsqueda interna /?s=término
3. Espera a que los productos carguen (wait_for_selector)
4. Extrae precio y nombre del resultado más relevante

El sitio bloquea requests directas (403) pero Playwright con browser completo
simula un usuario real y funciona correctamente.
"""

import re
import time
import logging
from playwright.sync_api import Page
from scrapers.base import BaseScraper

logger = logging.getLogger("plaza")

# URLs a probar en orden
STORE_URLS = [
    "https://vallearriba.elplazas.com",
    "https://enlinea.elplazas.com",
]


class PlazaScraper(BaseScraper):

    STORE_NAME = "plaza"
    BASE_URL = STORE_URLS[0]

    # Base URL que funcionó en esta sesión (evita reintento para cada producto)
    _working_base = None

    def scrape_all(self, products: list, previous_prices: dict = None) -> list:
        """
        Override: determina la URL base que funciona UNA VEZ,
        luego scrapea todos los productos.
        """
        if previous_prices is None:
            previous_prices = {}

        results = []
        self.start_browser()

        try:
            # Verificar cuál URL funciona antes de empezar el loop
            probe_page = self.new_page()
            working_base = self._find_working_base(probe_page)
            probe_page.close()

            if not working_base:
                logger.error("[plaza] Ninguna URL de El Plazas accesible")
                for product in products:
                    results.append({
                        "product_id": product["id"],
                        "store": self.STORE_NAME,
                        "price_usd": None,
                        "price_original": "",
                        "currency_original": "USD",
                        "product_name_found": "",
                        "url_found": "",
                        "flagged": True,
                        "flag_reason": "Sitio El Plazas no accesible",
                    })
                return results

            PlazaScraper._working_base = working_base
            logger.info(f"[plaza] URL activa: {working_base}")

            for product in products:
                self.logger.info(f"Scraping: {product['name']} ({self.STORE_NAME})")
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
                    self.logger.error(f"Error scraping {product['id']} en {self.STORE_NAME}: {e}")
                    results.append({
                        "product_id": product["id"],
                        "store": self.STORE_NAME,
                        "price_usd": None,
                        "price_original": "",
                        "currency_original": "",
                        "product_name_found": "",
                        "url_found": "",
                        "flagged": True,
                        "flag_reason": f"Error de scraping: {str(e)}"
                    })
                time.sleep(1.5)

        finally:
            self.close_browser()

        found = sum(1 for r in results if r.get("price_usd") and r["price_usd"] > 0)
        self.logger.info(f"{self.STORE_NAME}: {found}/{len(products)} productos encontrados")
        return results

    def _find_working_base(self, page: Page) -> str | None:
        """Prueba URLs hasta encontrar una que responda. Retorna la URL base o None."""
        for url in STORE_URLS:
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=25000)
                if response and response.status < 400:
                    logger.info(f"[plaza] Conectado a: {url}")
                    return url
                else:
                    logger.warning(f"[plaza] HTTP {response.status if response else 'None'} en {url}")
            except Exception as e:
                logger.warning(f"[plaza] Falló {url}: {e}")
        return None

    def scrape_product(self, product: dict) -> dict:
        """Busca un producto en El Plazas y retorna su precio."""
        search_term = product["search_terms"]["plaza"]
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

        base = PlazaScraper._working_base or STORE_URLS[0]
        page = self.new_page()

        try:
            # Estrategia 1: URL de búsqueda directa /?s=término
            search_url = f"{base}/?s={search_term.replace(' ', '+')}"
            result["url_found"] = search_url

            page.goto(search_url, wait_until="domcontentloaded", timeout=25000)

            # Esperar a que aparezca un producto (WooCommerce / tienda genérica)
            product_appeared = False
            for sel in [
                "li.product", "article.product",
                ".product-grid-item", ".product-item",
                "[class*='product-col']", "[class*='ProductCard']",
                ".woocommerce-loop-product__title",
            ]:
                try:
                    page.wait_for_selector(sel, timeout=8000)
                    product_appeared = True
                    break
                except Exception:
                    continue

            if product_appeared:
                price_text, name = self._extract_best_match(page, search_term)
                if price_text:
                    price = self.parse_price(price_text)
                    if price and price > 0:
                        result["price_usd"] = round(price, 2)
                        result["price_original"] = price_text
                        result["product_name_found"] = name
                        logger.info(f"[plaza] {product_id}: {name} → ${price:.2f}")
                        return result

            # Estrategia 2: buscar campo de búsqueda en la página
            page.goto(base, wait_until="domcontentloaded", timeout=20000)
            search_input = None
            for sel in [
                "input[type='search']",
                "input[placeholder*='buscar' i]",
                "input[placeholder*='search' i]",
                "#search", "[name='search']",
                "[class*='search'] input",
                "input[type='text']",
            ]:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        search_input = el
                        break
                except Exception:
                    continue

            if search_input:
                search_input.fill(search_term)
                search_input.press("Enter")
                try:
                    page.wait_for_selector(
                        "li.product, article.product, .product-item, [class*='product']",
                        timeout=8000
                    )
                except Exception:
                    pass

                price_text, name = self._extract_best_match(page, search_term)
                if price_text:
                    price = self.parse_price(price_text)
                    if price and price > 0:
                        result["price_usd"] = round(price, 2)
                        result["price_original"] = price_text
                        result["product_name_found"] = name
                        result["url_found"] = page.url
                        logger.info(f"[plaza] {product_id}: {name} → ${price:.2f}")
                        return result

            # Fallback: regex en HTML completo
            price_text = self._extract_any_price(page)
            if price_text:
                price = self.parse_price(price_text)
                if price and price > 0:
                    result["price_usd"] = round(price, 2)
                    result["price_original"] = price_text
                    result["product_name_found"] = f"(búsqueda: {search_term})"
                    return result

            result["flagged"] = True
            result["flag_reason"] = "Producto no encontrado en búsqueda"
            self.save_screenshot(page, f"not_found_{product_id}")

        except Exception as e:
            logger.error(f"[plaza] Error en {product_id}: {e}")
            self.save_screenshot(page, f"error_{product_id}")
            result["flagged"] = True
            result["flag_reason"] = str(e)
        finally:
            page.close()

        return result

    def _extract_best_match(self, page: Page, search_term: str) -> tuple[str, str]:
        """
        Extrae el precio y nombre del resultado más relevante.
        Aplica scoring por coincidencia de palabras del término de búsqueda.
        """
        search_words = search_term.lower().split()

        # Selectores de contenedor de producto
        container_selectors = [
            "li.product", "article.product",
            "[class*='product-col']", "[class*='product-item']",
            "[class*='ProductCard']", ".product",
        ]

        # Selectores de nombre dentro del contenedor
        name_selectors = [
            ".woocommerce-loop-product__title",
            ".product-title", ".product-name",
            "[class*='product-title']", "[class*='product-name']",
            "h2", "h3",
        ]

        # Selectores de precio dentro del contenedor
        price_selectors = [
            ".woocommerce-Price-amount", ".price .amount",
            ".price", "[class*='price']", "[itemprop='price']",
        ]

        best_price = ""
        best_name = ""
        best_score = 0

        for cont_sel in container_selectors:
            try:
                containers = page.query_selector_all(cont_sel)
                if not containers:
                    continue

                for container in containers[:15]:
                    # Obtener nombre
                    name_text = ""
                    for n_sel in name_selectors:
                        try:
                            n_el = container.query_selector(n_sel)
                            if n_el:
                                t = n_el.inner_text().strip()
                                if t and len(t) > 2:
                                    name_text = t
                                    break
                        except Exception:
                            continue

                    if not name_text:
                        continue

                    # Calcular score
                    name_lower = name_text.lower()
                    score = sum(1 for w in search_words if w in name_lower)
                    if score == 0:
                        continue

                    # Obtener precio
                    price_text = ""
                    for p_sel in price_selectors:
                        try:
                            p_el = container.query_selector(p_sel)
                            if p_el:
                                t = p_el.inner_text().strip()
                                if t and any(c.isdigit() for c in t):
                                    price_text = t
                                    break
                        except Exception:
                            continue

                    if not price_text:
                        continue

                    if score > best_score:
                        best_score = score
                        best_price = price_text
                        best_name = name_text

                if best_price:
                    return best_price, best_name

            except Exception:
                continue

        return best_price, best_name

    def _extract_any_price(self, page: Page) -> str:
        """Último recurso: buscar patrón de precio en el HTML."""
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
