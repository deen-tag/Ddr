"""
Lance tous les scrapers de sources (Destockplus, Restposten24, ...) et
fusionne leurs résultats dans un seul fichier data/lots.json, utilisé
par index.html.

Usage :
    python scrape_all.py
"""

import json
import sys
from dataclasses import asdict

import destockplus
import restposten24

OUTPUT_PATH = "data/lots.json"


def main():
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

    payload = [asdict(lot) for lot in all_lots]

    import os

    os.makedirs(os.path.dirname(OUTPUT_PATH) or ".", exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"{len(payload)} lot(s) au total écrit(s) dans {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
