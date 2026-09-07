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
    parse_number as common_parse_number,
)

BASE_URL = "https://www.destockplus.com"

# Une page de recherche par type de mémoire. On pourra en ajouter
# (ex: "ddr4-sodimm", "ddr5-ecc") si on veut affiner plus tard.
# Le "0" dans l'URL est l'index de page (0 = première page). On s'en
# sert comme gabarit pour générer les pages suivantes (1, 2, 3, ...).
SEARCH_PAGES = {
    "DDR4": f"{BASE_URL}/acheter/recherche-fournisseur-0-ddr4.html",
    "DDR5": f"{BASE_URL}/acheter/recherche-fournisseur-0-ddr5.html",
}

# Filet de sécurité : nombre maximum de pages qu'on ira chercher pour un
# même type de mémoire, même si le site semble en proposer indéfiniment.
# Évite une boucle infinie / un scraping trop long en cas de mauvaise
# détection de la fin de pagination.
MAX_PAGES = 30

# Motif pour repérer l'index de page dans l'URL (le nombre juste avant
# "-ddr4.html" ou "-ddr5.html") et pouvoir le remplacer.
PAGE_INDEX_RE = re.compile(r"-(\d+)-(ddr[45])\.html$")


def build_page_url(first_page_url: str, page_index: int) -> str:
    """Remplace l'index de page dans l'URL de la 1ère page par
    page_index. Ex: (".../recherche-fournisseur-0-ddr4.html", 2)
    -> ".../recherche-fournisseur-2-ddr4.html"."""
    return PAGE_INDEX_RE.sub(lambda m: f"-{page_index}-{m.group(2)}.html", first_page_url)

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
# Secours : un simple montant suivi de € (pas de mot "Prix" devant),
# ex: "620,00 €" ou "12.5€" - format qu'on suppose être utilisé sur la
# page de résultats (à confirmer via les diagnostics).
RE_PRIX_NU = re.compile(r"([\d]{1,3}(?:[\s.,]\d{3})*(?:[.,]\d{1,2})?)\s*€")
RE_CAPACITY_GO = re.compile(r"(\d+)\s*Go", re.IGNORECASE)


def fetch(url: str, allow_missing: bool = False) -> Optional[BeautifulSoup]:
    """Récupère et parse une page. Si allow_missing=True, une réponse
    404 (page de pagination inexistante = fin de pagination) renvoie
    None au lieu de lever une exception ; les autres erreurs HTTP
    continuent de lever normalement."""
    resp = requests.get(url, headers=HEADERS, timeout=20)
    print(
        f"[diag] GET {url} -> status={resp.status_code} "
        f"taille={len(resp.text)} caractères",
        file=sys.stderr,
    )
    if allow_missing and resp.status_code == 404:
        return None
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


parse_number = common_parse_number


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


def title_link_for_group(links: list) -> object:
    """Retourne le lien du groupe qui porte le vrai titre (texte le plus
    long, pas juste un nombre) plutôt qu'un lien de notation/étoiles."""
    best_link = links[0]
    best_len = -1
    for link in links:
        text = link.get_text(strip=True)
        if text and not text.isdigit() and len(text) > best_len:
            best_link = link
            best_len = len(text)
    return best_link


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

        # On ne garde que les annonces qui parlent vraiment de barrettes
        # DDR4/DDR5 (le mot-clé de recherche remonte aussi des PC
        # portables ou postes fixes qui *contiennent* de la DDR4/DDR5,
        # et de la DDR3 dont le titre contient quand même "RAM").
        # is_relevant_ddr_title() exige "ddr4"/"ddr5" explicite dans le
        # titre et exclut les appareils complets (thinkpad, laptop...).
        if not is_relevant_ddr_title(title):
            if debug_shown < 8:
                print(f"[diag] Titre ignoré (pas DDR4/DDR5 pur) : {title!r}", file=sys.stderr)
                debug_shown += 1
            continue

        # On cherche le texte "Quantité / Prix" dans le voisinage du
        # lien qui porte le VRAI titre (pas un lien de notation), en
        # remontant jusqu'à trouver un bloc qui contient ces infos ou
        # un montant en €.
        title_link = title_link_for_group(group_links)
        container = title_link
        context_text = ""
        for _ in range(10):
            if container.parent is None:
                break
            container = container.parent
            context_text = container.get_text(" ", strip=True)
            if "Quantité" in context_text or "Prix" in context_text or "€" in context_text:
                break

        qte_match = RE_QTE.search(context_text)
        prix_match = RE_PRIX.search(context_text)
        prix_nu_match = None
        if not prix_match:
            prix_nu_match = RE_PRIX_NU.search(context_text)

        if debug_shown < 8:
            print(
                f"[diag] Annonce gardée : titre={title!r} "
                f"qte_trouvee={bool(qte_match)} prix_trouve={bool(prix_match)} "
                f"prix_nu_trouve={bool(prix_nu_match)} "
                f"contexte(300c)={context_text[:300]!r}",
                file=sys.stderr,
            )
            debug_shown += 1

        if html_dump_shown < 3 and not prix_match and not prix_nu_match:
            print(
                f"[diag] --- HTML complet du conteneur pour {title!r} ---",
                file=sys.stderr,
            )
            print(str(container)[:3000], file=sys.stderr)
            print("[diag] --- fin HTML conteneur ---", file=sys.stderr)
            html_dump_shown += 1

        quantity = int(parse_number(qte_match.group(1))) if qte_match else None
        if prix_match:
            price_total = parse_number(prix_match.group(1))
            price_is_per_unit = bool(prix_match.group(3))
        elif prix_nu_match:
            price_total = parse_number(prix_nu_match.group(1))
            price_is_per_unit = False
        else:
            price_total = None
            price_is_per_unit = False

        capacity_match = RE_CAPACITY_GO.search(title)
        unit_capacity_go = int(capacity_match.group(1)) if capacity_match else None

        # On se fie au titre (plus fiable que la page de recherche
        # d'origine : une page "recherche DDR4" peut très bien remonter
        # une annonce dont le titre dit clairement "DDR5").
        detected_type = detect_memory_type(title) or memory_type

        total_go, price_per_go = compute_derived_fields(
            quantity, unit_capacity_go, price_total, price_is_per_unit
        )

        lots.append(
            Lot(
                id=f"destockplus-{ad_id}",
                title=title,
                memory_type=detected_type,
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


def collect_ad_ids(soup: BeautifulSoup) -> set:
    ids = set()
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if AD_LINK_RE.match(href):
            ids.add(extract_ad_id(href))
    return ids


def scrape_type(memory_type: str, first_page_url: str) -> list[Lot]:
    """Parcourt toutes les pages de résultats pour un type de mémoire
    (DDR4 ou DDR5), en s'arrêtant dès qu'une page :
    - renvoie une 404 (page de pagination inexistante),
    - ne contient plus aucune annonce,
    - ou ne contient que des annonces déjà vues sur les pages
      précédentes (signe qu'on a bouclé / atteint la fin réelle).
    Un plafond MAX_PAGES protège contre une boucle infinie."""
    all_lots: list[Lot] = []
    seen_ids: set = set()

    for page_index in range(MAX_PAGES):
        url = build_page_url(first_page_url, page_index)
        soup = fetch(url, allow_missing=True)

        if soup is None:
            print(f"[diag] {memory_type} page {page_index} -> 404, fin de pagination", file=sys.stderr)
            break

        page_ids = collect_ad_ids(soup)
        if not page_ids:
            print(f"[diag] {memory_type} page {page_index} -> aucune annonce, fin de pagination", file=sys.stderr)
            break

        new_ids = page_ids - seen_ids
        if not new_ids:
            print(
                f"[diag] {memory_type} page {page_index} -> {len(page_ids)} annonce(s), "
                "toutes déjà vues, fin de pagination",
                file=sys.stderr,
            )
            break

        seen_ids |= page_ids
        page_lots = parse_search_page(soup, memory_type, url)
        # On ne garde que les lots dont l'id n'a pas déjà été ajouté
        # (au cas où une même annonce apparaîtrait sur deux pages).
        existing_ids = {lot.id for lot in all_lots}
        for lot in page_lots:
            if lot.id not in existing_ids:
                all_lots.append(lot)
                existing_ids.add(lot.id)

        print(
            f"[diag] {memory_type} page {page_index} -> {len(new_ids)} nouvelle(s) annonce(s), "
            f"{len(all_lots)} au total jusqu'ici",
            file=sys.stderr,
        )

    return all_lots


def scrape_all() -> list[Lot]:
    all_lots: list[Lot] = []
    for memory_type, url in SEARCH_PAGES.items():
        all_lots.extend(scrape_type(memory_type, url))
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
