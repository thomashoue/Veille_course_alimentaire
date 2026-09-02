@echo off
REM Capture du vendredi - veille courses (Windows). Double-cliquez ce fichier.
REM
REM Pre-requis SingleFile (une fois) : Options -> Auto-sauvegarde :
REM   - cocher "auto-sauvegarder apres le chargement de la page"
REM   - delai apres chargement : 3 a 5 s (les drives sont lents)
REM   - "sauvegarder avec SingleFile Companion" -> dossier captures
REM Sans Companion, les pages vont dans Telechargements : mettez alors
REM   set CAPTURES=%USERPROFILE%\Downloads   avant de lancer.
setlocal enabledelayedexpansion
cd /d "%~dp0.."
if "%CAPTURES%"=="" set CAPTURES=captures
if not exist "%CAPTURES%" mkdir "%CAPTURES%"

REM Vider les pages de la semaine precedente (sinon d'anciens prix se melangent).
if exist "%CAPTURES%\*.html" (
  echo Nettoyage des anciennes pages de %CAPTURES%\
  del /q "%CAPTURES%\*.html"
)
if exist "%CAPTURES%\*.htm" del /q "%CAPTURES%\*.htm"

echo === Veille courses - capture du vendredi ===
echo Les pages s'enregistreront seules dans : %CAPTURES%\
echo.

for %%S in (leclerc_pleumeleuc intermarche_montauban hyperu_yffiniac lidl_langueux aldi_tregueux) do (
  echo --- %%S : ouverture des recherches ---
  python -m src.cli open-tabs --store %%S --bulk --delay 4
)

echo.
echo ^>^> Laissez SingleFile enregistrer toutes les pages (delai 3-5 s chacune).
echo    Appuyez sur une touche quand tout est enregistre dans %CAPTURES%\
pause

echo === Lecture (magasin auto-detecte par page) ===
if exist data\manual.json del data\manual.json
python -m src.cli parse-page --dir "%CAPTURES%"

echo.
echo === Doutes a lever (fiche produit) ===
python -m src.cli review --manual data/manual.json --prompt

echo.
echo === Comparatif (enseignes a drive) ===
python -m src.cli compare --manual data/manual.json

echo.
echo === Rapport complet + liste papier (Lidl, Aldi, Netto, Action...) ===
python -m src.cli run --no-drive --manual data/manual.json --collect --out data/reports
echo.
echo Rapport ecrit dans data\reports\. Termine.
pause
