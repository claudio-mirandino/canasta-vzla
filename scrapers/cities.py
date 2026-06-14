"""
Configuración multi-ciudad.

Cada ciudad tiene su propio conjunto de tiendas, su carpeta de datos y su fecha
base. La canasta (basket.json) y las tasas (fx.csv) son globales/compartidas.
Diseño ADITIVO: agregar una ciudad no toca las demás.
"""

from scrapers.gama import GamaScraper
from scrapers.central import CentralScraper
from scrapers.plansuarez import PlansuarezScraper
from scrapers.woocommerce import WooCommerceScraper


CITIES = {
    "caracas": {
        "name": "Caracas",
        "data_dir": "docs/data",
        "base_date": "2026-06-12",
        "build_stores": lambda: [GamaScraper(), CentralScraper(), PlansuarezScraper()],
        "store_info": {
            "gama": {"name": "Excelsior Gama", "loc": "Gama Plus Santa Eduvigis · Sucre"},
            "central": {"name": "Central Madeirense", "loc": "Av. Presidente Medina"},
            "plansuarez": {"name": "Plan Suárez", "loc": "Trinidad · Caurimare · La Urbina"},
        },
    },
    "maracaibo": {
        "name": "Maracaibo",
        "data_dir": "docs/data/maracaibo",
        "base_date": "2026-06-14",
        "build_stores": lambda: [
            WooCommerceScraper("angelicas", "maracaibo", "https://angelicasmarket.com", "USD"),
            WooCommerceScraper("superfresh", "maracaibo", "https://superfreshmarket.com.ve", "USD"),
            WooCommerceScraper("trio", "maracaibo", "https://triomcbo.com", "USD"),
        ],
        "store_info": {
            "angelicas": {"name": "Angélicas Market", "loc": "Maracaibo"},
            "superfresh": {"name": "Super Fresh Market", "loc": "Maracaibo"},
            "trio": {"name": "Tienda Trío", "loc": "Maracaibo · San Francisco"},
        },
    },
}
