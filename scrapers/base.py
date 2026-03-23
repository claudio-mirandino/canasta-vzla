"""
Base scraper class with common utilities:
- Playwright browser setup (headless Chromium, anti-bot headers)
- Price parsing (handles $, Bs, BsD, commas, periods)
- Anomaly detection
- Screenshot on error for debugging
"""

import re
import logging
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, Page, Browser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)


class BaseScraper:
    """
    Base class for all store scrapers.
    Each subclass implements `scrape_product(product: dict) -> dict`.
    """

    STORE_NAME = "unknown"
    BASE_URL = ""
    SCREENSHOTS_DIR = Path("debug_screenshots")

    def __init__(self):
        self.logger = logging.getLogger(self.STORE_NAME)
        self.SCREENSHOTS_DIR.mkdir(exist_ok=True)
        self._browser: Browser = None
        self._playwright = None

    # ------------------------------------------------------------------
    # Browser lifecycle
    # ------------------------------------------------------------------

    def start_browser(self):
        """Launch headless Chromium with human-like headers."""
        self._playwright = sync_playwright().start()
        # Intentar con Chrome instalado primero (evita spawn UNKNOWN en Windows)
        # Si no hay Chrome, usa Chromium de Playwright
        try:
            self._browser = self._playwright.chromium.launch(
                headless=True,
                channel="chrome",
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ]
            )
        except Exception:
            self._browser = self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ]
            )
        self.logger.info(f"Browser started for {self.STORE_NAME}")

    def new_page(self) -> Page:
        """Create a new browser page with realistic viewport and user-agent."""
        context = self._browser.new_context(
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
        # Remove webdriver flag to avoid bot detection
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        return context.new_page()

    def close_browser(self):
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()
        self.logger.info(f"Browser closed for {self.STORE_NAME}")

    # ------------------------------------------------------------------
    # Price parsing
    # ------------------------------------------------------------------

    def parse_price(self, raw: str) -> float | None:
        """
        Parse a price string into a float (USD).

        Handles formats commonly found in Venezuelan supermarkets:
          - "$1.50"          → 1.50
          - "1,50$"          → 1.50
          - "$1,234.56"      → 1234.56  (thousands separator)
          - "Bs. 5.000,00"   → converted to USD (NOT implemented here;
                               call convert_to_usd() separately)
          - "BsD 5000"       → same as above
          - "1.99 USD"       → 1.99

        Returns None if unparseable.
        """
        if not raw:
            return None

        raw = raw.strip()

        # Detect currency
        is_bolivares = bool(re.search(r'Bs\.?[DF]?|VES|VEF|Bolívar', raw, re.IGNORECASE))

        # Remove currency symbols and letters
        cleaned = re.sub(r'[^\d.,]', '', raw)

        if not cleaned:
            return None

        # Determine decimal separator:
        # If both comma and period present, the last one is the decimal separator
        has_comma = ',' in cleaned
        has_period = '.' in cleaned

        if has_comma and has_period:
            # e.g. "1.234,56" → 1234.56  or  "1,234.56" → 1234.56
            last_comma = cleaned.rfind(',')
            last_period = cleaned.rfind('.')
            if last_comma > last_period:
                # comma is decimal separator → "1.234,56"
                cleaned = cleaned.replace('.', '').replace(',', '.')
            else:
                # period is decimal separator → "1,234.56"
                cleaned = cleaned.replace(',', '')
        elif has_comma and not has_period:
            # comma is decimal separator → "1,50"
            cleaned = cleaned.replace(',', '.')
        # if only period or neither, leave as-is

        try:
            value = float(cleaned)
        except ValueError:
            self.logger.warning(f"Could not parse price: '{raw}' → '{cleaned}'")
            return None

        if is_bolivares:
            # Mark for conversion; caller must handle
            # We return negative as a sentinel — handled in subclass
            return -abs(value)

        return value

    # ------------------------------------------------------------------
    # Anomaly detection
    # ------------------------------------------------------------------

    def check_anomaly(self, product_id: str, new_price: float, previous_prices: dict) -> tuple[bool, str]:
        """
        Returns (is_flagged, reason).
        Flags if price moved more than 30% from the last recorded value.
        """
        prev = previous_prices.get(product_id)
        if prev is None or prev == 0:
            return False, ""

        change_pct = abs(new_price - prev) / prev * 100
        if change_pct > 30:
            reason = f"Cambio de {change_pct:.1f}% vs semana anterior (anterior: ${prev:.2f}, nuevo: ${new_price:.2f})"
            self.logger.warning(f"ANOMALÍA [{product_id}]: {reason}")
            return True, reason

        return False, ""

    # ------------------------------------------------------------------
    # Screenshot helper
    # ------------------------------------------------------------------

    def save_screenshot(self, page: Page, label: str):
        """Save a debug screenshot when a product is not found."""
        path = self.SCREENSHOTS_DIR / f"{self.STORE_NAME}_{label}_{int(time.time())}.png"
        try:
            page.screenshot(path=str(path))
            self.logger.info(f"Screenshot guardado: {path}")
        except Exception as e:
            self.logger.warning(f"No se pudo guardar screenshot: {e}")

    # ------------------------------------------------------------------
    # Main interface
    # ------------------------------------------------------------------

    def scrape_product(self, product: dict) -> dict:
        """
        Override in each store subclass.

        Args:
            product: dict from basket.json (id, name, search_terms, etc.)

        Returns:
            {
              "product_id": str,
              "store": str,
              "price_usd": float | None,
              "price_original": str,      # raw string as shown on site
              "currency_original": str,   # "USD" or "VES"
              "product_name_found": str,  # exact name on the site
              "url_found": str,
              "flagged": bool,
              "flag_reason": str,
            }
        """
        raise NotImplementedError

    def scrape_all(self, products: list, previous_prices: dict = None) -> list:
        """
        Scrape all products in the basket.
        Starts and stops the browser automatically.
        Returns list of result dicts.
        """
        if previous_prices is None:
            previous_prices = {}

        results = []
        self.start_browser()
        try:
            for product in products:
                self.logger.info(f"Scraping: {product['name']} ({self.STORE_NAME})")
                try:
                    result = self.scrape_product(product)
                    # Anomaly check on valid prices
                    if result.get("price_usd") and result["price_usd"] > 0:
                        flagged, reason = self.check_anomaly(
                            product["id"],
                            result["price_usd"],
                            previous_prices
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
                # Small pause between products to avoid rate limiting
                time.sleep(1.5)
        finally:
            self.close_browser()

        found = sum(1 for r in results if r.get("price_usd") and r["price_usd"] > 0)
        self.logger.info(f"{self.STORE_NAME}: {found}/{len(products)} productos encontrados")
        return results
