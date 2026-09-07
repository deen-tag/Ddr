"""
Scraper Restposten24 pour SERGIO DDR.

Restposten24 est une place de marché B2B de déstockage/liquidation
(Allemagne, avec des déclinaisons dans plusieurs pays européens :
restposten24.at, restposten24.ch, stocklots24.fr/it/uk/nl/pl/es/hu...).
Ce script cible la version allemande (.de), qui a le plus gros
catalogue, et est en HTML statique (pas de JS nécessaire pour lire le
contenu -> requests + BeautifulSoup suffit).

Usage :
    python restposten24.py            -> écrit data/restposten24.json
    python restposten24.py --stdout   -> affiche le JSON sur stdout

Notes :
- La structure exacte des blocs d'annonces n'a pas pu être testée en
  conditions réelles avec le vrai parseur (pas d'accès réseau dans
  l'environnement où ce script a été écrit). Le parsing repose sur les
  liens d'annonces (motif /<slug>/<id-numerique>) et sur un texte du
  type "12,50 €pro Stück, 500 Stück verfügbar" trouvé à proximité,
  d'après un exemplaire de page récupéré manuellement. Si Restposten24
  change son gabarit, ajuster AD_LINK_RE / RE_PRICE_QTY ci-dessous (les
  diagnostics stderr aident à repérer le nouveau format).
"""

import argparse
import json
import re
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup

from common import (
    Lot,
    compute_derived_fields,
    detect_memory_type,
    is_relevant_ddr_title,
    parse_number,
)

BASE_URL = "https://www.restposten24.de"

# Catégories informatique où des lots de RAM DDR4/DDR5 sont susceptibles
# d'apparaître. On filtre ensuite strictement par titre, donc une
# catégorie un peu large (Sonstige PC-Komponenten) ne pose pas de
# problème : les annonces hors-sujet seront simplement ignorées.
CATEGORY_PAGES = {
    "RAM-Speicher": f"{BASE_URL}/Computer/RAM-Speicher/cat56_0.html",
    "Sonstige PC-Komponenten": f"{BASE_URL}/Computer/Sonstige%20PC-Komponenten/cat59_0.html",
}

# Nombre max de pages à suivre par catégorie (pagination "page=2, 3...").
MAX_PAGES_PER_CATEGORY = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,fr;q=0.8,en;q=0.7",
}

# Lien vers une annonce : https://www.restposten24.de/<slug>/<id numérique>
AD_LINK_RE = re.compile(
    r"^(?:https://www\.restposten24\.de)?/[^/?#]+/(\d{5,})/?$"
)

# Ex: "12,50 €pro Stück, 500 Stück verfügbar" ou "1300,00 €pro VE, 49 VE verfügbar"
RE_PRICE_QTY = re.compile(
    r"([\d.,]+)\s*€\s*pro\s*(St(?:ü|ue)ck|VE)\s*,\s*([\d.,]+)\s*(?:St(?:ü|ue)ck|VE)\s*verf[üu]gbar",
    re.IGNORECASE,
)

RE_NEXT_PAGE = re.compile(r"[?&]page=(\d+)")


def fetch(url: str) -> Optional[BeautifulSoup]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
    except requests.RequestException as exc:
        print(f"[diag] Échec de la requête {url} : {exc}", file=sys.stderr)
        return None
    print(
        f"[diag] GET {url} -> status={resp.status_code} taille={len(resp.text)} caractères",
        file=sys.stderr,
    )
    if resp.status_code != 200:
        return None
    return BeautifulSoup(resp.text, "html.parser")


def extract_ad_id(href: str) -> str:
    match = AD_LINK_RE.match(href)
    return match.group(1) if match else href


def find_next_page_url(soup: BeautifulSoup, current_url: str) -> Optional[str]:
    for a in soup.find_all("a", href=True):
        if RE_NEXT_PAGE.search(a["href"]) and (
            a.get_text(strip=True) == "" or a.get_text(strip=True).isdigit() or "next" in a.get("rel", [])
        ):
            href = a["href"]
            return href if href.startswith("http") else BASE_URL + href
    return None


def parse_category_page(soup: BeautifulSoup, category_label: str) -> list:
    lots = []
    now = datetime.now(timezone.utc).isoformat()

    groups: dict = {}
    href_by_id: dict = {}
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not AD_LINK_RE.match(href):
            continue
        ad_id = extract_ad_id(href)
        groups.setdefault(ad_id, []).append(link)
        href_by_id.setdefault(ad_id, href)

    debug_shown = 0

    for ad_id, group_links in groups.items():
        href = href_by_id[ad_id]

        # Le titre est porté par le lien le plus long non numérique.
        title = ""
        for link in group_links:
            text = link.get_text(strip=True)
            if text and not text.isdigit() and len(text) > len(title):
                title = text
            img = link.find("img")
            if img and img.get("alt") and len(img["alt"].strip()) > len(title):
                title = img["alt"].strip()

        if not is_relevant_ddr_title(title):
            if debug_shown < 8:
                print(f"[diag][{category_label}] Titre ignoré (pas DDR4/DDR5 pur) : {title!r}", file=sys.stderr)
                debug_shown += 1
            continue

        # On remonte dans le DOM depuis le lien du titre pour trouver le
        # bloc contenant le prix + la quantité disponible.
        title_link = max(group_links, key=lambda l: len(l.get_text(strip=True)))
        container = title_link
        context_text = ""
        for _ in range(10):
            if container.parent is None:
                break
            container = container.parent
            context_text = container.get_text(" ", strip=True)
            if "verfügbar" in context_text or "€" in context_text:
                break

        price_match = RE_PRICE_QTY.search(context_text)
        if debug_shown < 8:
            print(
                f"[diag][{category_label}] Annonce gardée : titre={title!r} "
                f"prix_trouve={bool(price_match)} contexte(300c)={context_text[:300]!r}",
                file=sys.stderr,
            )
            debug_shown += 1

        price_total = None
        price_is_per_unit = False
        quantity = None
        if price_match:
            price_total = parse_number(price_match.group(1))
            price_is_per_unit = True  # Restposten24 affiche toujours un prix "pro Stück/VE"
            quantity = int(parse_number(price_match.group(3)) or 0) or None

        memory_type = detect_memory_type(title)
        from common import extract_capacity_go

        unit_capacity_go = extract_capacity_go(title)
        total_go, price_per_go = compute_derived_fields(
            quantity, unit_capacity_go, price_total, price_is_per_unit
        )

        lots.append(
            Lot(
                id=f"restposten24-{ad_id}",
                title=title,
                memory_type=memory_type,
                quantity=quantity,
                unit_capacity_go=unit_capacity_go,
                total_go=total_go,
                price_total_eur=price_total,
                price_is_per_unit=price_is_per_unit,
                price_per_go_eur=price_per_go,
                source="Restposten24",
                url=href if href.startswith("http") else BASE_URL + href,
                scraped_at=now,
            )
        )

    return lots


def scrape_all() -> list:
    all_lots = []
    for label, start_url in CATEGORY_PAGES.items():
        url = start_url
        for page_num in range(1, MAX_PAGES_PER_CATEGORY + 1):
            soup = fetch(url)
            if soup is None:
                break
            all_lots.extend(parse_category_page(soup, label))
            next_url = find_next_page_url(soup, url)
            if not next_url or next_url == url:
                break
            url = next_url
    return all_lots


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdout", action="store_true", help="Affiche le JSON sur stdout")
    parser.add_argument("-o", "--output", default="data/restposten24.json", help="Chemin du fichier de sortie")
    args = parser.parse_args()

    lots = scrape_all()
    payload = [asdict(lot) for lot in lots]

    if args.stdout:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        import os

        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"{len(lots)} lot(s) écrit(s) dans {args.output}")


if __name__ == "__main__":
    main()
