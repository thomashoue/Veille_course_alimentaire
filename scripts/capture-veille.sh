#!/usr/bin/env bash
# Capture du vendredi — veille courses.
#
# Pré-requis (réglage SingleFile, une seule fois) :
#   Options SingleFile → Auto-sauvegarde :
#     - cocher « auto-sauvegarder après le chargement de la page »
#     - délai après chargement : 3 à 5 s (les drives sont lents)
#     - « sauvegarder avec SingleFile Companion » → dossier de destination : captures/
#   Sans le Companion, les pages tombent dans Téléchargements : passez alors
#   CAPTURES=~/Downloads (ou votre dossier de téléchargement) à ce script.
#
# Usage :  bash scripts/capture-veille.sh
set -u
cd "$(dirname "$0")/.." || exit 1
PY="python"; command -v python >/dev/null 2>&1 || PY="python3"

CAPTURES="${CAPTURES:-captures}"
mkdir -p "$CAPTURES"

# Enseignes à ouvrir (une fenêtre chacune, SANS pause entre elles). Les trois
# premières ont un drive (recherche par article) ; Lidl et Aldi n'ont pas de
# recherche produit — leur page « offres de la semaine » s'ouvre en un onglet.
STORES=(leclerc_pleumeleuc intermarche_montauban hyperu_yffiniac lidl_langueux aldi_tregueux)

echo "=== Veille courses — capture du vendredi ==="
echo "Les pages s'enregistreront seules dans : $CAPTURES/"
echo

for store in "${STORES[@]}"; do
  echo "--- $store : ouverture des recherches ---"
  "$PY" -m src.cli open-tabs --store "$store" --bulk --delay 4
done

echo
echo ">> Laissez SingleFile enregistrer toutes les pages (délai 3–5 s chacune)."
read -r -p "   Appuyez sur Entrée quand tout est enregistré dans $CAPTURES/ … " _
echo

echo "=== Lecture (magasin auto-détecté par page) ==="
rm -f data/manual.json
"$PY" -m src.cli parse-page --dir "$CAPTURES"

echo
echo "=== Doutes à lever (fiche produit) ==="
"$PY" -m src.cli review --manual data/manual.json --prompt

echo
echo "=== Comparatif (enseignes à drive) ==="
"$PY" -m src.cli compare --manual data/manual.json

echo
echo "=== Rapport complet + liste papier (Lidl, Aldi, Netto, Action…) ==="
# --collect ajoute les promos des enseignes sans drive via les agrégateurs.
"$PY" -m src.cli run --no-drive --manual data/manual.json --collect --out data/reports
echo
echo "Rapport écrit dans data/reports/. Terminé."
