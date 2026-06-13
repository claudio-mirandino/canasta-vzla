"""
Scraper para Excelsior Gama - gamaenlinea.com

Tienda fija: Gama Plus Santa Eduvigis (Sucre, Caracas)

Flujo de selección de tienda (descubierto inspeccionando el DOM en vivo):
  1. Ir a /es/multiwarehouse/change
  2. select[formcontrolname="county"]  → value "MUSucre"   (NO "Baruta" ni "municipio")
  3. Esperar 2s a que cargue el select de sector
  4. select[formcontrolname="sector"]  → value "SESantaEduvigis"
  5. Click botón "Buscar"
  6. Click radio button del resultado "Gama Plus Santa Eduvigis"
  7. Click botón "Confirmar sucursal"
  8. Esperar redirección al homepage

Flujo de búsqueda de productos (SAP Spartacus):
  - La URL /es/search?text=... devuelve cx-product-list vacío <!----> siempre
  - El AUTOCOMPLETE del searchbox SÍ devuelve productos con precios
  - Estrategia: llenar cx-searchbox input → esperar sugerencias → extraer precio
  - Los precios son en "Ref." que equivale a USD en Venezuela (confirmado: azúcar Kaly = Ref. 1,99 = $1,99)

Correcciones aplicadas:
  - formcontrolname era "municipio"/"urbanizacion" → correcto es "county"/"sector"
  - Municipio era "Baruta" → correcto es "Sucre" (value MUSucre)
  - Faltaba click en radio button + "Confirmar sucursal" después de Buscar
  - Búsqueda por URL no funciona → usar autocomplete del searchbox
"""

import re
import time
import logging
from playwright.sync_api import Page
from scrapers.base import BaseScraper
from scrapers.matching import pick_best, search_variants

logger = logging.getLogger("gama")

STORE_BASE       = "https://gamaenlinea.com/es"
STORE_CHANGE_URL = f"{STORE_BASE}/multiwarehouse/change"

# Valores exactos de los <select> (inspeccionados en vivo)
COUNTY_VALUE = "MUSucre"          # Municipio Sucre (Santa Eduvigis pertenece a Sucre)
SECTOR_VALUE = "SESantaEduvigis"  # Gama Plus Santa Eduvigis — tienda fija


class GamaScraper(BaseScraper):

    STORE_NAME = "gama"
    BASE_URL   = STORE_BASE

    def __init__(self):
        super().__init__()
        self._shared_context = None
        self._search_page = None

    # ------------------------------------------------------------------
    # Contexto compartido — mantiene cookies/localStorage entre productos
    # ------------------------------------------------------------------

    def new_page(self) -> Page:
        """Usa el contexto compartido si está activo (mantiene sesión de tienda)."""
        if self._shared_context is not None:
            return self._shared_context.new_page()
        return super().new_page()

    def scrape_all(self, products: list, previous_prices: dict = None) -> list:
        """
        Override: crea un contexto compartido, selecciona tienda UNA VEZ,
        luego scrapea todos los productos reutilizando UNA SOLA página
        (el SPA tarda ~10-15s en arrancar; recargarlo por producto causa timeouts).
        """
        if previous_prices is None:
            previous_prices = {}

        results = []
        self.start_browser()

        try:
            self._shared_context = self._browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/148.0.0.0 Safari/537.36"
                ),
                locale="es-VE",
                timezone_id="America/Caracas",
                # OJO: no forzar header "Accept" global — rompe las llamadas XHR
                # del autocomplete (la API espera Accept: application/json).
            )
            self._shared_context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )

            # UNA página persistente con el SPA ya cargado para todos los productos.
            # (Abrir páginas adicionales dispara rate-limiting del sitio: ERR_TIMED_OUT)
            self._search_page = self._open_search_page()

            # La tienda por defecto de una sesión nueva ES Santa Eduvigis.
            # Solo verificamos; si no coincide, intentamos la selección manual.
            store_ok = self._verify_store(self._search_page)
            if not store_ok:
                self.logger.warning("[gama] Tienda por defecto no es Santa Eduvigis — intentando selección manual")
                store_ok = self._select_store(self._search_page)
                if store_ok:
                    self._search_page.goto(STORE_BASE, wait_until="commit", timeout=40000)
                    self._search_page.wait_for_selector('cx-searchbox input', timeout=35000)
                else:
                    self.logger.warning("[gama] Selección de tienda falló — precios se marcarán como no verificados")

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
                    self.logger.error(f"Error scraping {product['id']}: {e}")
                    results.append({
                        "product_id": product["id"],
                        "store": self.STORE_NAME,
                        "price_usd": None,
                        "price_original": "",
                        "currency_original": "",
                        "product_name_found": "",
                        "url_found": "",
                        "flagged": True,
                        "flag_reason": f"Error: {e}"
                    })
                time.sleep(1.0)

        finally:
            if getattr(self, "_search_page", None):
                try:
                    self._search_page.close()
                except Exception:
                    pass
                self._search_page = None
            if self._shared_context:
                try:
                    self._shared_context.close()
                except Exception:
                    pass
                self._shared_context = None
            self.close_browser()

        found = sum(1 for r in results if r.get("price_usd") and r["price_usd"] > 0)
        self.logger.info(f"gama: {found}/{len(products)} productos encontrados")
        return results

    # ------------------------------------------------------------------
    # Selección de tienda (flujo completo descubierto en vivo)
    # ------------------------------------------------------------------

    def _verify_store(self, page: Page) -> bool:
        """
        Verifica que el header diga 'Entregando desde Gama Plus Santa Eduvigis'.
        La tienda por defecto de una sesión nueva es exactamente esa, así que
        normalmente no hace falta el flujo de selección manual.
        """
        try:
            page.wait_for_timeout(1500)
            header = page.query_selector("header")
            text = header.inner_text() if header else page.content()
            if "Santa Eduvigis" in text:
                self.logger.info("[gama] ✓ Tienda verificada: Gama Plus Santa Eduvigis")
                return True
            return False
        except Exception as e:
            self.logger.warning(f"[gama] _verify_store falló: {e}")
            return False

    def _select_store(self, page: Page) -> bool:
        """
        Selecciona Gama Plus Santa Eduvigis.
        Flujo de 7 pasos verificado inspeccionando el DOM real.
        """
        try:
            # "commit" en vez de "domcontentloaded": el SPA mantiene conexiones
            # abiertas y domcontentloaded puede no dispararse nunca (timeout).
            page.goto(STORE_CHANGE_URL, wait_until="commit", timeout=40000)

            # Paso 1: Seleccionar municipio (formcontrolname="county", NO "municipio")
            # El SPA Angular tarda 10-15s en renderizar los selects.
            page.wait_for_selector('select[formcontrolname="county"]', timeout=35000)
            page.select_option('select[formcontrolname="county"]', value=COUNTY_VALUE)
            self.logger.info(f"[gama] Municipio seleccionado: {COUNTY_VALUE}")

            # Paso 2: Esperar que cargue el select de sector (empieza disabled)
            page.wait_for_timeout(2000)
            page.wait_for_selector('select[formcontrolname="sector"]:not([disabled])', timeout=20000)

            # Paso 3: Seleccionar sector (formcontrolname="sector", NO "urbanizacion")
            page.select_option('select[formcontrolname="sector"]', value=SECTOR_VALUE)
            self.logger.info(f"[gama] Sector seleccionado: {SECTOR_VALUE}")

            # Paso 4: Click en "Buscar"
            page.click('button:has-text("Buscar")')
            page.wait_for_timeout(2500)

            # Paso 5: Click en el radio button de "Gama Plus Santa Eduvigis"
            radio = page.query_selector('input[type="radio"]')
            if radio:
                radio.click()
                page.wait_for_timeout(500)
            else:
                self.logger.warning("[gama] Radio button no encontrado después de Buscar")
                return False

            # Paso 6: Click en "Confirmar sucursal"
            page.click('button:has-text("Confirmar sucursal")')

            # Paso 7: Esperar redirección al homepage
            page.wait_for_url(f"{STORE_BASE}/", timeout=8000)
            self.logger.info("[gama] ✓ Tienda seleccionada: Gama Plus Santa Eduvigis (Sucre)")
            return True

        except Exception as e:
            self.logger.warning(f"[gama] _select_store falló: {e}")
            # Intentar verificar si la tienda ya estaba seleccionada
            try:
                if "Santa Eduvigis" in page.content():
                    self.logger.info("[gama] Tienda ya estaba seleccionada previamente")
                    return True
            except Exception:
                pass
            return False

    # ------------------------------------------------------------------
    # Scraping de productos vía autocomplete del searchbox
    # ------------------------------------------------------------------

    def _open_search_page(self) -> Page:
        """Abre el homepage una sola vez y espera a que el searchbox esté listo."""
        page = self.new_page()
        page.goto(STORE_BASE, wait_until="commit", timeout=40000)
        page.wait_for_selector('cx-searchbox input', timeout=35000)
        return page

    def scrape_product(self, product: dict) -> dict:
        search_term = product["search_terms"]["gama"]
        product_id  = product["id"]

        result = {
            "product_id": product_id,
            "store": self.STORE_NAME,
            "price_usd": None,
            "price_original": "",
            "currency_original": "USD",
            "product_name_found": "",
            "url_found": STORE_BASE,
            "flagged": False,
            "flag_reason": "",
        }

        try:
            page = self._search_page
            if page is None or page.is_closed():
                page = self._open_search_page()
                self._search_page = page

            price_text, name = self._search_via_autocomplete(
                page, search_term, product.get("match"))

            if price_text:
                # Precio viene en formato "Ref. 1,25" — Ref. ≈ USD en Venezuela
                price = self._parse_ref_price(price_text)
                if price and price > 0:
                    result["price_usd"]          = round(price, 2)
                    result["price_original"]     = price_text
                    result["product_name_found"] = name
                    logger.info(f"[gama] {product_id}: {name} → ${price:.2f} ({price_text})")
                    return result

            logger.warning(f"[gama] Precio no encontrado para '{search_term}'")
            self.save_screenshot(page, f"not_found_{product_id}")
            result["flagged"]     = True
            result["flag_reason"] = f"Producto '{search_term}' no encontrado en autocomplete"

        except Exception as e:
            logger.error(f"[gama] Error en {product_id}: {e}")
            if getattr(self, "_search_page", None):
                self.save_screenshot(self._search_page, f"error_{product_id}")
            result["flagged"]     = True
            result["flag_reason"] = str(e)

        return result

    def _search_via_autocomplete(self, page: Page, search_term: str,
                                 match_rules: dict | None) -> tuple[str, str]:
        """
        Usa el autocomplete del searchbox (cx-searchbox) para obtener precios.
        La URL /search?text=... devuelve cx-product-list vacío — el autocomplete SÍ funciona.
        Los precios vienen como 'Ref. X,XX' (Ref. = USD a tasa BCV).

        El autocomplete es un typeahead literal: términos largos como
        'harina maiz precocida 1kg' no devuelven nada. Se intentan variantes
        cada vez más cortas y se ACUMULAN todos los candidatos; al final
        matching.pick_best aplica las reglas (palabras obligatorias/prohibidas
        y tamaño objetivo) para elegir el artículo correcto.
        """
        all_candidates: list[tuple[str, str]] = []
        seen = set()
        for variant in search_variants(search_term):
            for name, price_line in self._fetch_suggestions(page, variant):
                key = (name, price_line)
                if key not in seen:
                    seen.add(key)
                    all_candidates.append(key)
            # Una vez que tenemos candidatos válidos para las reglas, no seguimos acortando
            if pick_best(all_candidates, search_term, match_rules):
                break

        best = pick_best(all_candidates, search_term, match_rules)
        if best:
            name, price_line, _ = best
            return price_line, name
        return "", ""

    def _fetch_suggestions(self, page: Page, term: str) -> list[tuple[str, str]]:
        """Escribe un término en el searchbox y devuelve los pares (nombre, precio)."""
        try:
            search_input = page.query_selector('cx-searchbox input')
            logger.debug(f"[gama] variante '{term}': input={'sí' if search_input else 'NO'}, url={page.url}")
            if not search_input:
                return []

            # Limpiar resultados de la búsqueda anterior
            search_input.fill("")
            search_input.dispatch_event("input")
            page.wait_for_timeout(600)

            search_input.fill(term)
            search_input.dispatch_event("input")

            # Esperar las sugerencias y extraerlas del TEXTO COMPLETO del
            # componente cx-searchbox: el dropdown no siempre se renderiza
            # como ul>li, así que parseamos pares "nombre / Ref. X,XX".
            # OJO: ignorar el banner BCV "Ref. 1 = Bs 382,00" (tiene "Bs"/"=").
            pairs = []
            deadline = time.time() + 8
            while time.time() < deadline:
                page.wait_for_timeout(700)
                box = page.query_selector('cx-searchbox')
                if not box:
                    continue
                text = box.inner_text() or ""
                pairs = self._parse_suggestions(text)
                if pairs:
                    break
            return pairs

        except Exception as e:
            logger.debug(f"[gama] _fetch_suggestions error: {e}")
            return []

    @staticmethod
    def _parse_suggestions(box_text: str) -> list[tuple[str, str]]:
        """
        Convierte el texto del cx-searchbox en pares (nombre, línea de precio).
        Una línea 'Ref. X,XX' (sin 'Bs' ni '=') es el precio de la línea
        de nombre inmediatamente anterior.
        """
        lines = [l.strip() for l in box_text.split("\n") if l.strip()]
        pairs = []
        for i, line in enumerate(lines):
            if "Bs" in line or "=" in line:
                continue
            if re.fullmatch(r'Ref\.\s*[\d.]+,\d{2}', line) and i > 0:
                name = lines[i - 1]
                if "Ref." not in name and len(name) > 3:
                    pairs.append((name, line))
        return pairs

    def _parse_ref_price(self, ref_text: str) -> float | None:
        """
        Parsea 'Ref. 1,25' o 'Ref. 10,99' → float.
        Ref. ≈ USD en Venezuela (confirmado: azúcar Kaly = Ref. 1,99 = $1,99 en Central).
        """
        try:
            # Extraer el número con regex — limpiar con sub() deja el punto
            # de "Ref." pegado al número y rompe float().
            m = re.search(r'([\d.]*\d),(\d{2})', ref_text)
            if not m:
                return None
            integer_part = m.group(1).replace('.', '')  # puntos = separador de miles
            return float(f"{integer_part}.{m.group(2)}")
        except Exception:
            return None
