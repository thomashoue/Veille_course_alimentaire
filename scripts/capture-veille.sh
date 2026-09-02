#!/usr/bin/env bash
# Procédure de capture du vendredi — veille courses.
#
# Enchaîne : ouverture des recherches dans le navigateur (une enseigne à la
# fois), pause pour enregistrer les pages, lecture, revue des doutes,
# comparatif. Rien n'est automatisé côté site : vous enregistrez les pages
# vous-même (Ctrl+S ou SingleFile), le script fait tout le reste.
#
# Usage :  bash scripts/capture-veille.sh
set -u

# Se placer à la racine du dépôt (le script est dans scripts/).
cd "$(dirname "$0")/.." || exit 1

PY="python"
command -v python >/dev/null 2>&1 || PY="python3"

# Enseignes à capturer : id_magasin|dossier|nom lisible
STORES=(
  "leclerc_pleumeleuc|pages_leclerc|E.Leclerc Pleumeleuc"
  "intermarche_montauban|pages_inter|Intermarché Montauban"
  "hyperu_yffiniac|pages_u|Hyper U Yffiniac"
)

echo "=== Veille courses — capture du vendredi ==="
echo

for entry in "${STORES[@]}"; do
  IFS='|' read -r store dir label <<< "$entry"
  mkdir -p "$dir"
  echo "--- $label ---"
  echo "Ouverture des recherches dans votre navigateur…"
  "$PY" -m src.cli open-tabs --store "$store" --bulk
  echo
  echo ">> Enregistrez chaque page (Ctrl+S « page complète », ou SingleFile)"
  echo "   dans le dossier : $dir"
  read -r -p "   Appuyez sur Entrée quand c'est fait pour $label… " _
  echo
done

echo "=== Lecture des pages enregistrées ==="
rm -f data/manual.json
first=1
for entry in "${STORES[@]}"; do
  IFS='|' read -r store dir label <<< "$entry"
  if [ "$first" = "1" ]; then
    "$PY" -m src.cli parse-page --store "$store" --dir "$dir"
    first=0
  else
    "$PY" -m src.cli parse-page --store "$store" --dir "$dir" --append
  fi
done

echo
echo "=== Doutes à lever (fiche produit) ==="
"$PY" -m src.cli review --manual data/manual.json --prompt

echo
echo "=== Comparatif ==="
"$PY" -m src.cli compare --manual data/manual.json

echo
echo "Terminé. Pour le rapport complet :"
echo "  $PY -m src.cli run --no-drive --manual data/manual.json"
