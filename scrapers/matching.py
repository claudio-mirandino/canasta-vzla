"""
Matching compartido producto ↔ resultado de búsqueda.

Problema que resuelve: el scoring ingenuo por palabras sueltas producía
matches absurdos (cebolla → "DIP DE QUESO CON CEBOLLA CARAMELIZADA",
papa → "MELON CRIOLLO", tomate → "Tomate Cherry 300gr").

Cada producto de basket.json puede definir reglas explícitas:

    "match": {
      "must_include": [["cebolla"]],          # grupos AND; dentro del grupo, OR
      "must_exclude": ["dip", "crema"],       # si aparece alguna → rechazado
      "size_value": 1000, "size_unit": "g"    # tamaño objetivo para bonus
    }

Score = palabras del término coincidentes (ponderado)
      + bonus por tamaño cercano al objetivo
      + bonus si el nombre EMPIEZA con la palabra clave del producto.
Un candidato que viole must_include/must_exclude queda descartado (None).

Todas las comparaciones son sin acentos, sin mayúsculas y por palabra
completa ("sal" NO coincide dentro de "salsa").
"""

import re
import unicodedata


def normalize(text: str) -> str:
    """minúsculas + sin acentos."""
    text = unicodedata.normalize("NFD", text or "")
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text.lower()


def _words(text: str) -> list[str]:
    return re.findall(r'[a-z0-9]+', normalize(text))


def _word_in(word: str, name_words: list[str]) -> bool:
    """Coincidencia por palabra completa, tolerando plural simple (platano/platanos)."""
    w = normalize(word)
    for nw in name_words:
        if nw == w or nw == w + "s" or w == nw + "s":
            return True
    return False


# ----------------------------------------------------------------------
# Tamaños: "1KG", "900 GR", "1,5 LT", "30 UN", "850ML", "X140GRS"
# ----------------------------------------------------------------------

_SIZE_RE = re.compile(
    r'(\d+(?:[.,]\d+)?)\s*(kg|kgs|k|gr|grs|g|gramos|ml|cc|lt|lts|l|litros?|un|und|unds|unidades)\b',
    re.IGNORECASE,
)

# factor → unidad base (g, ml, un)
_UNIT_FACTORS = {
    "kg": ("g", 1000), "kgs": ("g", 1000), "k": ("g", 1000),
    "gr": ("g", 1), "grs": ("g", 1), "g": ("g", 1), "gramos": ("g", 1),
    "ml": ("ml", 1), "cc": ("ml", 1),
    "lt": ("ml", 1000), "lts": ("ml", 1000), "l": ("ml", 1000),
    "litro": ("ml", 1000), "litros": ("ml", 1000),
    "un": ("un", 1), "und": ("un", 1), "unds": ("un", 1), "unidades": ("un", 1),
}


def extract_size(name: str) -> tuple[str, float] | None:
    """Extrae (unidad_base, cantidad) del nombre de un producto, o None."""
    # Plan Suárez marca el tamaño como "!500!" / "!300!": quitar signos para parsear
    m = _SIZE_RE.search(normalize(name).replace("!", " ").replace(",", "."))
    if not m:
        return None
    qty = float(m.group(1).replace(",", "."))
    unit_raw = m.group(2).lower()
    if unit_raw not in _UNIT_FACTORS:
        return None
    base_unit, factor = _UNIT_FACTORS[unit_raw]
    return base_unit, qty * factor


def _size_bonus(name: str, size_value: float, size_unit: str) -> float:
    """
    Bonus [0..4] si el tamaño del candidato se acerca al objetivo.
    Penaliza -2 si difiere en más del doble/mitad (presentación incomparable).
    """
    found = extract_size(name)
    if not found:
        return 0.0
    unit, qty = found
    if unit != size_unit or size_value <= 0:
        return 0.0
    ratio = qty / size_value
    if 0.85 <= ratio <= 1.15:
        return 4.0
    if 0.6 <= ratio <= 1.5:
        return 1.5
    if ratio > 2.0 or ratio < 0.5:
        return -2.0
    return 0.0


# Conversión de unidad de referencia → unidad base detectable y su factor.
# ref kg ↔ g (÷1000), ref l ↔ ml (÷1000), ref u ↔ un (×1).
_REF_TO_BASE = {"kg": ("g", 1000.0), "l": ("ml", 1000.0), "u": ("un", 1.0)}


def _egg_count(name: str) -> int | None:
    """
    Número de huevos en una presentación: '30 unidades', '30UND', '15U',
    '1/2 carton' (=15), 'docena' (=12), 'carton' (=30). None si no se detecta.
    """
    n = normalize(name)
    if re.search(r'(1/2|medio)\s*cart', n):
        return 15
    m = re.search(r'(\d+)\s*(u|un|und|unds|unidad|unidades|huevos?)\b', n)
    if m:
        return int(m.group(1))
    if "docena" in n:
        return 12
    if "carton" in n:
        return 30
    return None


def unit_price(price: float, product_name: str, ref_unit: str) -> float | None:
    """
    Normaliza un precio a precio por unidad de referencia ($/kg, $/l, $/unidad)
    a partir del tamaño detectado en el nombre del producto.

    - Si se detecta el tamaño (p.ej. "ATÚN 140 GR" → 0,14 kg) → precio/tamaño.
    - Si NO se detecta tamaño, se asume que el precio YA es por unidad de
      referencia (caso típico de productos vendidos "por kg": "CARNE MOLIDA X KG").
    Devuelve None solo si el precio es inválido.
    """
    if price is None or price <= 0:
        return None
    if ref_unit == "u":
        cnt = _egg_count(product_name)
        return round(price / cnt, 4) if cnt else round(price, 4)
    if ref_unit not in _REF_TO_BASE:
        return round(price, 4)
    base_unit, factor = _REF_TO_BASE[ref_unit]
    size = extract_size(product_name)
    if size and size[0] == base_unit and size[1] > 0:
        qty_in_ref = size[1] / factor
        if qty_in_ref > 0:
            return round(price / qty_in_ref, 4)
    return round(price, 4)


def score_candidate(name: str, search_term: str, match_rules: dict | None) -> float | None:
    """
    Score de un candidato. None = rechazado por las reglas.
    Mayor score = mejor match.
    """
    if not name or len(name.strip()) < 3:
        return None

    name_words = _words(name)
    rules = match_rules or {}

    # Reglas duras
    for bad in rules.get("must_exclude", []):
        if _word_in(bad, name_words):
            return None

    include_groups = rules.get("must_include", [])
    for group in include_groups:
        if not any(_word_in(w, name_words) for w in group):
            return None

    # Unidad obligatoria: distingue p.ej. leche EN POLVO (g) de líquida (ml).
    # Solo rechaza si el candidato declara un tamaño en una unidad distinta;
    # si no se detecta tamaño, no se penaliza (falta de info ≠ violación).
    require_unit = rules.get("require_unit")
    if require_unit:
        found = extract_size(name)
        if found and found[0] != require_unit:
            return None

    # Score blando: palabras del término de búsqueda presentes en el nombre
    term_words = _words(search_term)
    matched = sum(1 for w in term_words if _word_in(w, name_words))
    score = 2.0 * matched

    # Bonus: el nombre empieza con la palabra clave principal del producto
    if include_groups and name_words:
        first_group = include_groups[0]
        if any(_word_in(w, name_words[:2]) for w in first_group):
            score += 3.0

    # Bonus/penalización por tamaño
    sv = rules.get("size_value")
    su = rules.get("size_unit")
    if sv and su:
        score += _size_bonus(name, float(sv), su)

    return score


def search_variants(search_term: str) -> list[str]:
    """
    Genera variantes de un término de menos agresivas a más cortas:
    completo → sin tokens de tamaño (1kg, 900g…) → recortando palabras.
    Útil para buscadores typeahead/AND que no toleran términos largos.
    """
    variants = [search_term]
    no_size = " ".join(
        w for w in search_term.split()
        if not re.match(r'^\d|^(kg|gr?|grs|ml|lt|litro|litros|unidades|un)$', w.lower())
    )
    if no_size and no_size != search_term:
        variants.append(no_size)
    words = no_size.split() if no_size else search_term.split()
    while len(words) > 1:
        words = words[:-1]
        v = " ".join(words)
        if v not in variants:
            variants.append(v)
    return variants


def pick_best(candidates: list[tuple[str, str]], search_term: str,
              match_rules: dict | None) -> tuple[str, str, float] | None:
    """
    candidates: lista de (nombre, texto_precio).
    Retorna (nombre, texto_precio, score) del mejor candidato válido, o None.
    Empates: gana el de nombre más corto (presentación más simple/genérica).
    """
    best = None
    for name, price_text in candidates:
        s = score_candidate(name, search_term, match_rules)
        if s is None or s <= 0:
            continue
        if best is None or s > best[2] or (s == best[2] and len(name) < len(best[0])):
            best = (name, price_text, s)
    return best
