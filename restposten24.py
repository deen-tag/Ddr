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
#
# Format d'URL vérifié manuellement le 07/09/2026 (récupération directe
# du HTML, sans JS) :
#   /index.php?cat=<ID>&func=cat&mod=rp24_global&mode=singlecat
# renvoie bien les annonces en HTML côté serveur pour la page 1.
#
# L'ancienne version de ce script ajoutait "&page=<N>&orderBy=offers_date"
# dès la première page, ce qui semble faire échouer le rendu serveur
# (page vide malgré un statut 200) : c'est très probablement la cause du
# "0 résultat" observé en production. On n'ajoute donc "page" que pour
# les pages 2 et suivantes, et on retire orderBy. Si "page=2" s'avère
# lui aussi incorrect (à vérifier avec les logs [diag] ci-dessous), les
# noms de paramètres à essayer en premier sont : "p", "seite", ou un
# numéro dans le chemin plutôt qu'en query string.
CATEGORY_IDS = {
    "RAM-Speicher": 56,
    "Sonstige PC-Komponenten": 59,
}

# Nombre max de pages à suivre par catégorie.
MAX_PAGES_PER_CATEGORY = 20


def build_category_page_url(cat_id: int, page: int) -> str:
    url = f"{BASE_URL}/index.php?cat={cat_id}&func=cat&mod=rp24_global&mode=singlecat"
    if page > 1:
        url += f"&page={page}"
    return url

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

RE_NEXT_PAGE = re.compile(r"[?&]page=(\d+)")  # gardé pour référence, plus utilisé pour la pagination


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
    soup = BeautifulSoup(resp.text, "html.parser")

    all_links = soup.find_all("a", href=True)
    ad_links = [a for a in all_links if AD_LINK_RE.match(a["href"])]
    if not ad_links:
        # Aucun lien ne matche le motif attendu : on affiche les vraies
        # formes d'URL trouvées sur la page pour pouvoir corriger
        # AD_LINK_RE si le gabarit du site a changé (ou si cette
        # catégorie n'a simplement aucune annonce en ce moment).
        print(
            f"[diag] Aucun lien d'annonce trouvé sur {url} ({len(all_links)} liens <a> au total). "
            "Formes d'URL distinctes trouvées :",
            file=sys.stderr,
        )
        shapes = {}
        for a in all_links:
            href = a["href"]
            shape = re.sub(r"\d+", "#", href)
            if shape not in shapes:
                shapes[shape] = (href, a.get_text(strip=True)[:40])
        for shape, (href, texte) in list(shapes.items())[:25]:
            print(f"[diag]   forme={shape!r}  exemple={href!r}  texte={texte!r}", file=sys.stderr)

    return soup


def extract_ad_id(href: str) -> str:
    match = AD_LINK_RE.match(href)
    return match.group(1) if match else href


def collect_ad_ids(soup: BeautifulSoup) -> set:
    ids = set()
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if AD_LINK_RE.match(href):
            ids.add(extract_ad_id(href))
    return ids


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


def scrape_category(label: str, cat_id: int) -> list:
    """Parcourt toutes les pages d'une catégorie, en s'arrêtant dès
    qu'une page échoue, ne contient plus d'annonce, ou ne contient que
    des annonces déjà vues (fin réelle de la pagination). On boucle sur
    des numéros de page explicites plutôt que de deviner un lien
    "page suivante" dans le HTML, ce qui s'est révélé peu fiable en
    conditions réelles (le site propose plusieurs liens de pagination
    qui peuvent faire revenir en arrière)."""
    all_lots = []
    seen_ids: set = set()

    for page_num in range(1, MAX_PAGES_PER_CATEGORY + 1):
        url = build_category_page_url(cat_id, page_num)
        soup = fetch(url)
        if soup is None:
            print(f"[diag][{label}] page {page_num} -> échec de la requête, fin de pagination", file=sys.stderr)
            break

        page_ids = collect_ad_ids(soup)
        if not page_ids:
            print(f"[diag][{label}] page {page_num} -> aucune annonce, fin de pagination", file=sys.stderr)
            break

        new_ids = page_ids - seen_ids
        if not new_ids:
            print(
                f"[diag][{label}] page {page_num} -> {len(page_ids)} annonce(s), toutes déjà vues, fin de pagination",
                file=sys.stderr,
            )
            break

        seen_ids |= page_ids
        page_lots = parse_category_page(soup, label)
        existing_ids = {lot.id for lot in all_lots}
        for lot in page_lots:
            if lot.id not in existing_ids:
                all_lots.append(lot)
                existing_ids.add(lot.id)

        print(
            f"[diag][{label}] page {page_num} -> {len(new_ids)} nouvelle(s) annonce(s), "
            f"{len(all_lots)} lot(s) DDR4/DDR5 au total jusqu'ici",
            file=sys.stderr,
        )

    return all_lots


def scrape_all() -> list:
    all_lots = []
    for label, cat_id in CATEGORY_IDS.items():
        all_lots.extend(scrape_category(label, cat_id))
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
