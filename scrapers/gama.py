"""
Scraper para Excelsior Gama - gamaenlinea.com

Estrategia:
- Navega a páginas de categoría (más estables que búsqueda)
- Espera networkidle para que cargue el JS
- Extrae nombre y precio del producto más relevante
- Si la página sigue en blanco, intenta la URL de búsqueda como fallback
"""

import re
import time
import logging
from playwright.sync_api import Page
from scrapers.base import BaseScraper

logger = logging.getLogger("gama")

STORE_BASE = "https://gamaenlinea.com/es"

# URLs de categoría directas (más confiables que búsqueda)
CATEGORY_URLS = {
    "harina_maiz":  f"{STORE_BASE}/harinas/c/A040101",
    "arroz":        f"{STORE_BASE}/arroz/c/A040201",
    "pasta":        f"{STORE_BASE}/pastas/c/A040301",
    "caraotas":     f"{STORE_BASE}/granos/c/A040401",
    "lentejas":     f"{STORE_BASE}/granos/c/A040401",
    "aceite":       f"{STORE_BASE}/aceites-y-margarinas/c/A050101",
    "margarina":    f"{STORE_BASE}/aceites-y-margarinas/c/A050101",
    "azucar":       f"{STORE_BASE}/azucar-y-endulzantes/c/A060101",
    "sal":          f"{STORE_BASE}/sal-y-especias/c/A060201",
    "cafe":         f"{STORE_BASE}/cafe-y-te/c/A060301",
    "carne_molida": f"{STORE_BASE}/carnes/c/A020101",
    "pollo":        f"{STORE_BASE}/aves/c/A020201",
    "huevos":       f"{STORE_BASE}/huevos/c/A020301",
    "sardinas":     f"{STORE_BASE}/enlatados/c/A030201",
    "atun":         f"{STORE_BASE}/enlatados/c/A030201",
    "leche_polvo":  f"{STORE_BASE}/lacteos/c/A030101",
    "queso_blanco": f"{STORE_BASE}/quesos/c/A030301",
    "mantequilla":  f"{STORE_BASE}/lacteos/c/A030101",
    "tomate":       f"{STORE_BASE}/frutas-y-verduras/c/A010101",
    "cebolla":      f"{STORE_BASE}/frutas-y-verduras/c/A010101",
    "papa":         f"{STORE_BASE}/frutas-y-verduras/c/A010101",
    "platano":      f"{STORE_BASE}/frutas-y-verduras/c/A010101",
}

# Fallback: URL de búsqueda general
SEARCH_URL = f"{STORE_BASE}/search?text={{term}}"


class GamaScraper(BaseScraper):

    STORE_NAME = "gama"
    BASE_URL = STORE_BASE

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

        page = self.new_page()

        try:
            # Estrategia 1: URL de categoría específica
            cat_url = CATEGORY_URLS.get(product_id)
            if cat_url:
                price_text, name = self._load_and_extract(page, cat_url, search_term, product_id)
                if price_text:
                    result["url_found"] = cat_url
                    price = self.parse_price(price_text)
                    if price and price > 0:
                        result["price_usd"] = round(price, 2)
                        result["price_original"] = price_text
                        result["product_name_found"] = name
                        logger.info(f"[gama] {product_id}: {name} → ${price:.2f}")
                        return result

            # Estrategia 2: Búsqueda general
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
            result["flag_reason"] = "Producto no encontrado en ninguna estrategia"

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
            # Navegar y esperar a que la red esté idle (contenido JS cargado)
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Esperar explícitamente a que aparezca cualquier producto
            # Gama usa SAP Spartacus — los selectores son cx-* o similares
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
                # La página cargó pero no hay productos visibles
                # Intentar extraer precios directamente del HTML actual
                content = page.content()
                if len(content) < 500:
                    logger.debug(f"[gama] Página en blanco en {url}")
                    return "", ""
                return self._extract_price_from_html(content, search_term)

            # Hay productos — extraer del DOM
            return self._extract_from_dom(page, search_term)

        except Exception as e:
            logger.debug(f"[gama] _load_and_extract falló en {url}: {e}")
            return "", ""

    def _extract_from_dom(self, page: Page, search_term: str) -> tuple[str, str]:
        """Extrae precio y nombre del DOM cargado."""
        search_words = search_term.lower().split()

        # Intentar múltiples combinaciones de selectores
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

        # Intentar con cada selector de precio
        for p_sel in price_selectors:
            try:
                elements = page.query_selector_all(p_sel)
                for el in elements[:10]:  # Max 10 primeros resultados
                    price_text = el.inner_text().strip()
                    if not price_text or not any(c.isdigit() for c in price_text):
                        continue
                    # Intentar obtener el nombre del producto asociado
                    # Buscar el contenedor padre y luego el nombre
                    name = search_term  # default
                    try:
                        parent = el.evaluate_handle("el => el.closest('[class*=\"product\"]') || el.parentElement.parentElement")
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

        # Fallback: extraer del HTML completo
        return self._extract_price_from_html(page.content(), search_term)

    def _extract_price_from_html(self, html: str, search_term: str) -> tuple[str, str]:
        """Último recurso: buscar precios por regex en el HTML."""
        # Buscar patrones de precio en USD
        prices = re.findall(r'\$\s*(\d+\.\d{2})', html)
        if prices:
            # Filtrar precios razonables (entre $0.50 y $200)
            valid = [p for p in prices if 0.5 <= float(p) <= 200]
            if valid:
                return f"${valid[0]}", f"(extracción HTML - {search_term})"
        return "", ""
