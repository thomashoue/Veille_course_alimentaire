#!/usr/bin/env bash
# Capture du vendredi — veille courses.
#
# Déroulé : une FENÊTRE par enseigne ; pour chaque fenêtre, vous cliquez dans
# SingleFile « Enregistrer tous les onglets » (l'auto-save rate les onglets en
# arrière-plan). Le script lit ensuite tout le dossier et sort le comparatif.
#
# Pré-requis SingleFile (une fois) :
#   - « sauvegarder avec SingleFile Companion » → dossier destination : captures/
#   Sans le Companion, les pages tombent dans Téléchargements : passez alors
#   CAPTURES=~/Downloads à ce script.
#
# Usage :  bash scripts/capture-veille.sh
set -u
cd "$(dirname "$0")/.." || exit 1
PY="python"; command -v python >/dev/null 2>&1 || PY="python3"

CAPTURES="${CAPTURES:-captures}"
DELAY="${DELAY:-6}"     # secondes entre chaque onglet (évite l'anti-robot)
mkdir -p "$CAPTURES"

# Vider les pages de la semaine précédente : sinon d'anciens prix se
# mélangeraient aux nouveaux relevés.
old=$(find "$CAPTURES" -maxdepth 1 -type f \( -name '*.html' -o -name '*.htm' \) 2>/dev/null | wc -l)
if [ "$old" -gt 0 ]; then
  echo "Nettoyage : $old ancienne(s) page(s) supprimée(s) de $CAPTURES/"
  find "$CAPTURES" -maxdepth 1 -type f \( -name '*.html' -o -name '*.htm' \) -delete
fi

# Seules les enseignes à drive lisible : Leclerc, Intermarché, Hyper U.
# Aldi (produits chargés dynamiquement) et Lidl (flyer images) ne se lisent pas
# depuis une page enregistrée — ils passent par les agrégateurs (run --collect).
STORES=(leclerc_pleumeleuc intermarche_montauban hyperu_yffiniac)

echo "=== Veille courses — capture du vendredi ==="
echo "Dossier de destination : $CAPTURES/"
echo

# Une fenêtre par enseigne. Après chaque, vous enregistrez ses onglets.
for store in "${STORES[@]}"; do
  echo "--- $store : ouverture des recherches (nouvelle fenêtre) ---"
  "$PY" -m src.cli open-tabs --store "$store" --bulk --delay "$DELAY"
  echo
  echo ">> Dans SingleFile : clic droit sur l'icône → « Enregistrer tous les"
  echo "   onglets ». Attendez que la fenêtre de $store soit enregistrée."
  read -r -p "   Entrée pour passer à l'enseigne suivante… " _
  echo
done

echo "=== Lecture (magasin auto-détecté par page) ==="
rm -f data/manual.json
"$PY" -m src.cli parse-page --dir "$CAPTURES"

echo
echo "=== Doutes à lever (fiche produit) ==="
"$PY" -m src.cli review --manual data/manual.json --prompt

echo
echo "=== Comparatif ==="
"$PY" -m src.cli compare --manual data/manual.json

echo
echo "=== Rapport complet + liste papier (Aldi, Lidl, Netto, Action…) ==="
# --collect ajoute les promos des enseignes sans drive via les agrégateurs.
"$PY" -m src.cli run --no-drive --manual data/manual.json --collect --out data/reports
echo
echo "Rapport écrit dans data/reports/. Terminé."
