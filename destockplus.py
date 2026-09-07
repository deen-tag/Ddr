"""
Scraper Destockplus pour SERGIO DDR.

Récupère les annonces de RAM DDR4 et DDR5 via les pages de recherche
par mot-clé de Destockplus (place de marché B2B multi-vendeurs), et
produit une liste de lots normalisés au format attendu par le front.

Usage :
    python destockplus.py            -> écrit data/destockplus.json
    python destockplus.py --stdout   -> affiche le JSON sur stdout

Notes :
- Le site est en HTML statique classique (pas de JS nécessaire), donc
  un simple requests + BeautifulSoup suffit.
- La structure exacte des blocs d'annonces n'a pas pu être testée en
  conditions réelles (pas d'accès réseau dans l'environnement où ce
  script a été écrit) : le parsing repose sur les liens d'annonces
  (motif /acheter/c-<id>-<slug>.html) et sur une recherche de texte
  "Quantité :" / "Prix :" à proximité. Si Destockplus fait évoluer son
  gabarit, il faudra ajuster REGEX_QTE / REGEX_PRIX ou la logique de
  récupération du conteneur ci-dessous.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.destockplus.com"

# Une page de recherche par type de mémoire. On pourra en ajouter
# (ex: "ddr4-sodimm", "ddr5-ecc") si on veut affiner plus tard.
SEARCH_PAGES = {
    "DDR4": f"{BASE_URL}/acheter/recherche-fournisseur-0-ddr4.html",
    "DDR5": f"{BASE_URL}/acheter/recherche-fournisseur-0-ddr5.html",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Lien vers une annonce : soit relatif (/acheter/c-904002-...html), soit
# absolu (https://www.destockplus.com/acheter/c-904002-...html) — le site
# utilise en réalité des liens absolus.
AD_LINK_RE = re.compile(r"^(?:https://www\.destockplus\.com)?/acheter/c-\d+-.+\.html$")

RE_QTE = re.compile(r"Quantit[ée]\s*:\s*([\d\s]+)", re.IGNORECASE)
RE_PRIX = re.compile(
    r"Prix\s*:\s*([\d\s.,]+)\s*€?\s*(HT)?\s*(/\s*Unit[ée])?", re.IGNORECASE
)
RE_CAPACITY_GO = re.compile(r"(\d+)\s*Go", re.IGNORECASE)


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


def fetch(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    print(
        f"[diag] GET {url} -> status={resp.status_code} "
        f"taille={len(resp.text)} caractères",
        file=sys.stderr,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    all_links = soup.find_all("a", href=True)
    ad_links = [a for a in all_links if AD_LINK_RE.match(a["href"])]
    print(
        f"[diag] {len(all_links)} liens <a> trouvés, "
        f"{len(ad_links)} correspondent au motif d'annonce",
        file=sys.stderr,
    )
    if not ad_links:
        # Les 30 premiers liens sont souvent juste le menu de navigation
        # (toujours les mêmes catégories). Pour repérer le vrai format des
        # annonces, on regroupe TOUS les liens par "forme" (chiffres ->
        # remplacés par #) et on affiche un exemple par forme distincte.
        print("[diag] Aucun lien d'annonce trouvé. Formes d'URL distinctes trouvées :", file=sys.stderr)
        shapes = {}
        for a in all_links:
            href = a["href"]
            shape = re.sub(r"\d+", "#", href)
            if shape not in shapes:
                texte = a.get_text(strip=True)[:40]
                shapes[shape] = (href, texte)
        for shape, (href, texte) in shapes.items():
            print(f"[diag]   forme={shape!r}", file=sys.stderr)
            print(f"[diag]     exemple href={href!r}  texte={texte!r}", file=sys.stderr)
    return soup


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


def extract_ad_id(href: str) -> str:
    match = re.search(r"c-(\d+)-", href)
    return match.group(1) if match else href


def best_title_for_group(links: list) -> str:
    """Parmi tous les <a> qui pointent vers la même annonce (miniature
    photo, titre, etc.), choisit le texte le plus probable pour être le
    titre du produit : pas vide, pas juste un nombre, le plus long."""
    candidates = []
    for link in links:
        text = link.get_text(strip=True)
        if text:
            candidates.append(text)
        title_attr = link.get("title", "").strip()
        if title_attr:
            candidates.append(title_attr)
        for img in link.find_all("img"):
            alt = (img.get("alt") or "").strip()
            if alt:
                candidates.append(alt)

    # On préfère un texte "réel" (pas juste un nombre) et le plus long
    real_candidates = [c for c in candidates if not c.isdigit()]
    pool = real_candidates or candidates
    return max(pool, key=len) if pool else ""


def parse_search_page(soup: BeautifulSoup, memory_type: str, source_url: str) -> list[Lot]:
    lots: list[Lot] = []
    now = datetime.now(timezone.utc).isoformat()

    # 1er passage : on regroupe tous les <a> d'annonce par id, car une même
    # annonce a plusieurs liens (miniature, titre, etc.) sur la page de
    # résultats.
    groups: dict[str, list] = {}
    href_by_id: dict[str, str] = {}
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not AD_LINK_RE.match(href):
            continue
        ad_id = extract_ad_id(href)
        groups.setdefault(ad_id, []).append(link)
        href_by_id.setdefault(ad_id, href)

    debug_shown = 0
    html_dump_shown = 0

    for ad_id, group_links in groups.items():
        href = href_by_id[ad_id]
        title = best_title_for_group(group_links)

        if not title:
            continue

        # On ne garde que les annonces qui parlent vraiment de RAM DDR
        # (le mot-clé de recherche peut aussi remonter des PC portables
        # qui *contiennent* de la DDR4/DDR5, pas juste des barrettes).
        title_lower = title.lower()
        if "ram" not in title_lower and "ddr" not in title_lower:
            if debug_shown < 8:
                print(f"[diag] Titre ignoré (pas 'ram'/'ddr') : {title!r}", file=sys.stderr)
                debug_shown += 1
            continue

        # On cherche le texte "Quantité / Prix" dans le voisinage du
        # lien (le conteneur parent de l'annonce), en remontant jusqu'à
        # trouver un bloc qui contient ces deux informations.
        container = group_links[0]
        context_text = ""
        for _ in range(6):
            if container.parent is None:
                break
            container = container.parent
            context_text = container.get_text(" ", strip=True)
            if "Quantité" in context_text or "Prix" in context_text:
                break

        qte_match = RE_QTE.search(context_text)
        prix_match = RE_PRIX.search(context_text)

        if debug_shown < 8:
            print(
                f"[diag] Annonce gardée : titre={title!r} "
                f"qte_trouvee={bool(qte_match)} prix_trouve={bool(prix_match)} "
                f"contexte(200c)={context_text[:200]!r}",
                file=sys.stderr,
            )
            debug_shown += 1

        if html_dump_shown < 2 and not prix_match:
            print(
                f"[diag] --- HTML complet du conteneur pour {title!r} ---",
                file=sys.stderr,
            )
            print(str(container)[:3000], file=sys.stderr)
            print("[diag] --- fin HTML conteneur ---", file=sys.stderr)
            html_dump_shown += 1

        quantity = int(parse_number(qte_match.group(1))) if qte_match else None
        price_total = parse_number(prix_match.group(1)) if prix_match else None
        price_is_per_unit = bool(prix_match and prix_match.group(3))

        capacity_match = RE_CAPACITY_GO.search(title)
        unit_capacity_go = int(capacity_match.group(1)) if capacity_match else None

        total_go = None
        price_per_go = None
        if unit_capacity_go and quantity:
            total_go = unit_capacity_go * quantity
        if price_total is not None and total_go:
            effective_total_price = (
                price_total * quantity if price_is_per_unit else price_total
            )
            price_per_go = round(effective_total_price / total_go, 3)

        lots.append(
            Lot(
                id=f"destockplus-{ad_id}",
                title=title,
                memory_type=memory_type,
                quantity=quantity,
                unit_capacity_go=unit_capacity_go,
                total_go=total_go,
                price_total_eur=price_total,
                price_is_per_unit=price_is_per_unit,
                price_per_go_eur=price_per_go,
                source="Destockplus",
                url=BASE_URL + href if href.startswith("/") else href,
                scraped_at=now,
            )
        )

    return lots


def scrape_all() -> list[Lot]:
    all_lots: list[Lot] = []
    for memory_type, url in SEARCH_PAGES.items():
        soup = fetch(url)
        all_lots.extend(parse_search_page(soup, memory_type, url))
    return all_lots


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stdout", action="store_true", help="Affiche le JSON sur stdout au lieu d'écrire un fichier"
    )
    parser.add_argument(
        "-o", "--output", default="data/destockplus.json", help="Chemin du fichier de sortie"
    )
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
