"""
Utilitaires partagés entre les différents scrapers SERGIO DDR.

Chaque scraper de site (destockplus.py, restposten24.py, ...) doit
produire une liste d'objets Lot, en utilisant is_relevant_ddr_title()
pour décider si une annonce doit être gardée, et detect_memory_type()
pour savoir si c'est de la DDR4 ou de la DDR5.
"""

import re
from dataclasses import dataclass
from typing import Optional

# Mots qui indiquent qu'il ne s'agit PAS d'un lot de barrettes de RAM
# nues, mais d'un appareil complet qui contient de la RAM (PC portable,
# poste fixe, tablette...). On veut des barrettes, pas des machines.
EXCLUDE_KEYWORDS = [
    "thinkpad", "elitebook", "probook", "latitude", "precision",
    "zbook", "macbook", "ultrabook", "chromebook",
    "pc portable", "ordinateur portable", "notebook", "laptop",
    "pc de bureau", "pc fixe", "poste de travail", "workstation",
    "all-in-one", "all in one", "aio",
    "tablette", "tablet", "smartphone", "téléphone",
    "imprimante", "écran", "moniteur", "monitor",
    # PC gamers / de bureau complets vendus avec de la RAM DDR4/DDR5
    # dedans (le titre annonce le PC, pas un lot de barrettes) :
    "gaming", "gamer", "legion", "omen", "predator", "alienware",
    "rtx", "geforce", "radeon", "nvidia", "quadro",
    "ryzen 3", "ryzen 5", "ryzen 7", "ryzen 9",
    "core i3", "core i5", "core i7", "core i9",
    "ultra 5", "ultra 7", "ultra 9",
]

RE_DDR4 = re.compile(r"\bddr4\b", re.IGNORECASE)
RE_DDR5 = re.compile(r"\bddr5\b", re.IGNORECASE)
RE_CAPACITY_GO = re.compile(r"(\d+)\s*(?:go|gb)\b", re.IGNORECASE)


def detect_memory_type(title: str) -> Optional[str]:
    """Renvoie 'DDR4' ou 'DDR5' si le titre le mentionne explicitement,
    sinon None (ce qui exclut par exemple la DDR3)."""
    if RE_DDR5.search(title):
        return "DDR5"
    if RE_DDR4.search(title):
        return "DDR4"
    return None


def is_relevant_ddr_title(title: str) -> bool:
    """True si le titre correspond à un lot de barrettes DDR4/DDR5 et
    pas à un appareil complet (PC portable, poste fixe, etc.)."""
    if not title:
        return False
    title_lower = title.lower()
    if any(kw in title_lower for kw in EXCLUDE_KEYWORDS):
        return False
    return detect_memory_type(title) is not None


def extract_capacity_go(title: str) -> Optional[int]:
    match = RE_CAPACITY_GO.search(title)
    return int(match.group(1)) if match else None


def parse_number(raw: str) -> Optional[float]:
    """Convertit '1 140,00' ou '620.00' ou '500' en float."""
    if not raw:
        return None
    cleaned = raw.strip().replace(" ", "").replace("\xa0", "")
    cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


@dataclass
class Lot:
    id: str
    title: str
    memory_type: str  # "DDR4" ou "DDR5"
    quantity: Optional[int]
    unit_capacity_go: Optional[int]
    total_go: Optional[float]
    price_total_eur: Optional[float]
    price_is_per_unit: bool
    price_per_go_eur: Optional[float]
    source: str
    url: str
    scraped_at: str
    is_new: bool = False


def compute_derived_fields(
    quantity: Optional[int],
    unit_capacity_go: Optional[int],
    price_total: Optional[float],
    price_is_per_unit: bool,
):
    """Calcule total_go et price_per_go_eur à partir des champs bruts."""
    total_go = None
    price_per_go = None
    if unit_capacity_go and quantity:
        total_go = unit_capacity_go * quantity
    if price_total is not None and total_go:
        effective_total_price = price_total * quantity if price_is_per_unit else price_total
        price_per_go = round(effective_total_price / total_go, 3)
    return total_go, price_per_go
