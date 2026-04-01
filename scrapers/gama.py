"""
Scraper para Excelsior Gama - gamaenlinea.com

Estrategia:
- Selecciona tienda Gama Plus Santa Eduvigis (Baruta) UNA VEZ por sesión
  usando un contexto de browser compartido (las cookies/localStorage persisten)
- Usa URL de búsqueda por término (las URLs de categoría /c/... dan 404)
- Espera a que SAP Spartacus (Angular) cargue los cx-product-list-item
- Extrae nombre y precio del producto más relevante
"""

import re
import time
import logging
from playwright.sync_api import Page
from scrapers.base import BaseScraper

logger = logging.getLogger("gama")

STORE_BASE = "https://gamaenlinea.com/es"
SEARCH_URL = f"{STORE_BASE}/search?text={{term}}"

STORE_MUNICIPIO = "Baruta"
STORE_URBANIZACION = "Santa Eduvigis"  # Gama Plus Santa Eduvigis — tienda fija


class GamaScraper(BaseScraper):

    STORE_NAME = "gama"
    BASE_URL = STORE_BASE

    def __init__(self):
        super().__init__()
        self._shared_context = None  # Contexto compartido para mantener sesión de tienda

    # ------------------------------------------------------------------
    # Contexto compartido — mantiene cookies y localStorage entre productos
    # ------------------------------------------------------------------

    def new_page(self) -> Page:
        """
        Override: si existe un contexto compartido, crea la página en él
        para que la selección de tienda persista entre productos.
        """
        if self._shared_context is not None:
            page = self._shared_context.new_page()
            return page
        return super().new_page()

    def scrape_all(self, products: list, previous_prices: dict = None) -> list:
        """
        Override: crea un contexto compartido, selecciona tienda una vez,
        luego scrapea todos los productos en ese mismo contexto.
        """
        if previous_prices is None:
            previous_prices = {}

        results = []
        self.start_browser()

        try:
            # Crear UN contexto para toda la sesión (mantiene cookies/localStorage)
            self._shared_context = self._browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="es-VE",
                timezone_id="America/Caracas",
                extra_http_headers={
                    "Accept-Language": "es-VE,es;q=0.9,en;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                }
            )
            self._shared_context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )

            # Seleccionar tienda UNA VEZ antes de scraping
            store_page = self._shared_context.new_page()
            store_ok = self._select_store(store_page)
            store_page.close()

            if not store_ok:
                self.logger.warning("[gama] Selección de tienda falló — continuando sin selección")

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
            if self._shared_context:
                try:
                    self._shared_context.close()
                except Exception:
                    pass
                self._shared_context = None
            self.close_browser()

        found = sum(1 for r in results if r.get("price_usd") and r["price_usd"] > 0)
        self.logger.info(f"{self.STORE_NAME}: {found}/{len(products)} productos encontrados")
        return results

    # ------------------------------------------------------------------
    # Selección de tienda
    # ------------------------------------------------------------------

    def _select_store(self, page: Page) -> bool:
        """
        Selecciona la tienda Gama Plus Santa Eduvigis antes de scraping.
        Es necesario hacerlo una vez por sesión para que los productos carguen.
        Retorna True si tuvo éxito.
        """
        try:
            page.goto(f"{STORE_BASE}/multiwarehouse/change",
                      wait_until="domcontentloaded", timeout=20000)

            # Seleccionar municipio Baruta
            municipio_sel = "select[formcontrolname='municipio'], select[id*='municipio'], select"
            page.wait_for_selector(municipio_sel, timeout=8000)
            page.select_option(municipio_sel, label=STORE_MUNICIPIO)
            page.wait_for_timeout(1500)

            # Seleccionar urbanización Santa Eduvigis
            urb_sel = "select[formcontrolname='urbanizacion'], select[id*='urban'], select:nth-of-type(2)"
            page.wait_for_selector(urb_sel, timeout=8000)
            page.select_option(urb_sel, label=STORE_URBANIZACION)
            page.wait_for_timeout(1000)

            # Hacer clic en Buscar/Confirmar
            for btn_text in ["Buscar", "Confirmar", "Seleccionar", "Ver tiendas"]:
                btn = page.query_selector(f"button:has-text('{btn_text}')")
                if btn:
                    btn.click()
                    break

            page.wait_for_timeout(2000)
            self.logger.info(f"[gama] Tienda seleccionada: {STORE_MUNICIPIO} / {STORE_URBANIZACION}")
            return True
        except Exception as e:
            self.logger.warning(f"[gama] No se pudo seleccionar tienda: {e}")
            return False

    # ------------------------------------------------------------------
    # Scraping de productos
    # ------------------------------------------------------------------

    def scrape_product(self, product: dict) -> dict:
        search_term = product["search_terms"]["gama"]
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

        page = self.new_page()  # Usa shared_context si está activo

        try:
            # URLs de categoría dan 404 — usar búsqueda directa
            search_url = SEARCH_URL.format(term=search_term.replace(" ", "+"))
            result["url_found"] = search_url
            price_text, name = self._load_and_extract(page, search_url, search_term, product_id)

            if price_text:
                price = self.parse_price(price_text)
                if price and price > 0:
                    result["price_usd"] = round(price, 2)
                    result["price_original"] = price_text
                    result["product_name_found"] = name
                    logger.info(f"[gama] {product_id}: {name} → ${price:.2f}")
                    return result

            logger.warning(f"[gama] Precio no encontrado para '{search_term}'")
            self.save_screenshot(page, f"not_found_{product_id}")
            result["flagged"] = True
            result["flag_reason"] = "Producto no encontrado"

        except Exception as e:
            logger.error(f"[gama] Error en {product_id}: {e}")
            self.save_screenshot(page, f"error_{product_id}")
            result["flagged"] = True
            result["flag_reason"] = str(e)
        finally:
            page.close()

        return result

    def _load_and_extract(self, page: Page, url: str, search_term: str, product_id: str) -> tuple[str, str]:
        """
        Carga la URL y espera a que el contenido esté listo.
        Retorna (precio_texto, nombre) o ("", "").
        """
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Gama usa SAP Spartacus — esperar a que Angular renderice los productos
            product_selectors = [
                "cx-product-list-item",
                ".product-item",
                "cx-product-grid-item",
                "[class*='product-item']",
                "[class*='ProductItem']",
                ".product",
                "a[href*='/p/']",  # Links a productos en SAP Commerce
            ]

            found_selector = None
            for sel in product_selectors:
                try:
                    page.wait_for_selector(sel, timeout=12000)
                    found_selector = sel
                    break
                except Exception:
                    continue

            if not found_selector:
                content = page.content()
                if len(content) < 500:
                    logger.debug(f"[gama] Página en blanco en {url}")
                    return "", ""
                return self._extract_price_from_html(content, search_term)

            return self._extract_from_dom(page, search_term)

        except Exception as e:
            logger.debug(f"[gama] _load_and_extract falló en {url}: {e}")
            return "", ""

    def _extract_from_dom(self, page: Page, search_term: str) -> tuple[str, str]:
        """Extrae precio y nombre del DOM cargado."""
        price_selectors = [
            "cx-price .value",
            ".cx-price",
            "[class*='price'] .value",
            ".price",
            "[class*='Price']",
            "[itemprop='price']",
        ]
        name_selectors = [
            "cx-product-list-item a",
            ".product-name a",
            "[class*='product-name']",
            ".cx-product-name",
            "h3", "h2",
        ]

        for p_sel in price_selectors:
            try:
                elements = page.query_selector_all(p_sel)
                for el in elements[:10]:
                    price_text = el.inner_text().strip()
                    if not price_text or not any(c.isdigit() for c in price_text):
                        continue
                    name = search_term
                    try:
                        parent = el.evaluate_handle(
                            "el => el.closest('[class*=\"product\"]') || el.parentElement.parentElement"
                        )
                        if parent:
                            for n_sel in name_selectors:
                                n_el = parent.query_selector(n_sel)
                                if n_el:
                                    name_text = n_el.inner_text().strip()
                                    if name_text and len(name_text) > 2:
                                        name = name_text
                                        break
                    except Exception:
                        pass
                    return price_text, name
            except Exception:
                continue

        return self._extract_price_from_html(page.content(), search_term)

    def _extract_price_from_html(self, html: str, search_term: str) -> tuple[str, str]:
        """Último recurso: buscar precios por regex en el HTML."""
        prices = re.findall(r'\$\s*(\d+\.\d{2})', html)
        if prices:
            valid = [p for p in prices if 0.5 <= float(p) <= 200]
            if valid:
                return f"${valid[0]}", f"(extracción HTML - {search_term})"
        return "", ""
