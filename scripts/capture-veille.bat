@echo off
REM Procedure de capture du vendredi - veille courses (Windows).
REM Double-cliquez ce fichier, ou lancez-le depuis cmd.
REM Vous enregistrez les pages vous-meme (Ctrl+S) ; le script fait le reste.
setlocal enabledelayedexpansion
cd /d "%~dp0.."

echo === Veille courses - capture du vendredi ===
echo.

call :capture leclerc_pleumeleuc    pages_leclerc "E.Leclerc Pleumeleuc"
call :capture intermarche_montauban pages_inter   "Intermarche Montauban"
call :capture hyperu_yffiniac       pages_u       "Hyper U Yffiniac"

echo === Lecture des pages enregistrees ===
if exist data\manual.json del data\manual.json
python -m src.cli parse-page --store leclerc_pleumeleuc    --dir pages_leclerc
python -m src.cli parse-page --store intermarche_montauban --dir pages_inter --append
python -m src.cli parse-page --store hyperu_yffiniac       --dir pages_u     --append

echo.
echo === Doutes a lever (fiche produit) ===
python -m src.cli review --manual data/manual.json --prompt

echo.
echo === Comparatif ===
python -m src.cli compare --manual data/manual.json

echo.
echo Termine. Pour le rapport complet :
echo   python -m src.cli run --no-drive --manual data/manual.json
pause
goto :eof

:capture
REM %1 = id magasin, %2 = dossier, %3 = nom lisible
if not exist "%~2" mkdir "%~2"
echo --- %~3 ---
echo Ouverture des recherches dans votre navigateur...
python -m src.cli open-tabs --store %~1 --bulk
echo.
echo ^>^> Enregistrez chaque page (Ctrl+S "page complete", ou SingleFile)
echo    dans le dossier : %~2
pause
echo.
goto :eof
