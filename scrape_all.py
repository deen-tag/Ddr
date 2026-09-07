"""
Lance tous les scrapers de sources (Destockplus, Restposten24, ...) et
fusionne leurs résultats dans un seul fichier data/lots.json, utilisé
par index.html.

Marque aussi chaque lot avec is_new=True s'il n'était pas présent dans
la version précédente de data/lots.json (donc pas encore vu au run
d'avant), pour que le site puisse afficher un badge "NOUVEAU" et un
filtre dédié.

Usage :
    python scrape_all.py
"""

import json
import sys
from dataclasses import asdict

import destockplus
import restposten24

OUTPUT_PATH = "data/lots.json"


def load_previous_ids(path: str) -> set:
    try:
        with open(path, "r", encoding="utf-8") as f:
            previous = json.load(f)
        return {lot["id"] for lot in previous}
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return set()


def main():
    previous_ids = load_previous_ids(OUTPUT_PATH)

    all_lots = []

    print("=== Destockplus ===", file=sys.stderr)
    try:
        all_lots.extend(destockplus.scrape_all())
    except Exception as exc:  # on continue même si une source échoue
        print(f"[erreur] Destockplus a échoué : {exc}", file=sys.stderr)

    print("=== Restposten24 ===", file=sys.stderr)
    try:
        all_lots.extend(restposten24.scrape_all())
    except Exception as exc:
        print(f"[erreur] Restposten24 a échoué : {exc}", file=sys.stderr)

    new_count = 0
    for lot in all_lots:
        lot.is_new = lot.id not in previous_ids
        if lot.is_new:
            new_count += 1

    payload = [asdict(lot) for lot in all_lots]

    import os

    os.makedirs(os.path.dirname(OUTPUT_PATH) or ".", exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"{len(payload)} lot(s) au total écrit(s) dans {OUTPUT_PATH} ({new_count} nouveau(x))")


if __name__ == "__main__":
    main()
