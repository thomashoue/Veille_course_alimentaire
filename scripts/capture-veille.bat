@echo off
REM Capture du vendredi - veille courses (Windows). Double-cliquez ce fichier.
REM
REM Deroule : une FENETRE par enseigne ; pour chaque, cliquez dans SingleFile
REM "Enregistrer tous les onglets" (l'auto-save rate les onglets en arriere-plan).
REM
REM Pre-requis SingleFile (une fois) : "sauvegarder avec SingleFile Companion"
REM -> dossier destination captures. Sans Companion, les pages vont dans
REM Telechargements : mettez alors  set CAPTURES=%USERPROFILE%\Downloads  avant.
setlocal enabledelayedexpansion
cd /d "%~dp0.."
if "%CAPTURES%"=="" set CAPTURES=captures
if "%DELAY%"=="" set DELAY=6
if not exist "%CAPTURES%" mkdir "%CAPTURES%"

REM Vider les pages de la semaine precedente (sinon d'anciens prix se melangent).
if exist "%CAPTURES%\*.html" (
  echo Nettoyage des anciennes pages de %CAPTURES%\
  del /q "%CAPTURES%\*.html"
)
if exist "%CAPTURES%\*.htm" del /q "%CAPTURES%\*.htm"

echo === Veille courses - capture du vendredi ===
echo Dossier de destination : %CAPTURES%\
echo.

REM Seules les enseignes a drive lisible. Aldi (dynamique) et Lidl (flyer)
REM ne se lisent pas d'une page enregistree : agregateurs via run --collect.
for %%S in (leclerc_pleumeleuc intermarche_montauban hyperu_yffiniac) do (
  echo --- %%S : ouverture des recherches (nouvelle fenetre) ---
  python -m src.cli open-tabs --store %%S --bulk --delay !DELAY!
  echo.
  echo ^>^> Dans SingleFile : clic droit sur l'icone -^> "Enregistrer tous les
  echo    onglets". Attendez que la fenetre de %%S soit enregistree.
  echo    Appuyez sur une touche pour passer a l'enseigne suivante...
  pause >nul
  echo.
)

echo === Lecture (magasin auto-detecte par page) ===
if exist data\manual.json del data\manual.json
python -m src.cli parse-page --dir "%CAPTURES%"

echo.
echo === Doutes a lever (fiche produit) ===
python -m src.cli review --manual data/manual.json --prompt

echo.
echo === Comparatif ===
python -m src.cli compare --manual data/manual.json

echo.
echo === Rapport complet + liste papier (Aldi, Lidl, Netto, Action...) ===
python -m src.cli run --no-drive --manual data/manual.json --collect --out data/reports
echo.
echo Rapport ecrit dans data\reports\. Termine.
pause
