@echo off
REM Lancement de PhotoGram sous Windows.
REM
REM   Double-cliquer sur ce fichier, ou depuis un terminal :  scripts\run.bat
REM
REM Un .bat plutot qu'un .ps1 : PowerShell refuse par defaut d'executer les
REM scripts non signes, ce qui bloquerait des le premier essai.
REM
REM Pas d'accents dans ce fichier : la console Windows utilise une page de
REM codes qui les afficherait de travers.

setlocal
cd /d "%~dp0.."

REM Le lanceur "py" est installe avec Python sous Windows et choisit la
REM bonne version ; "python" sert de repli.
where py >nul 2>&1
if %ERRORLEVEL% equ 0 (set PYTHON=py -3) else (set PYTHON=python)

%PYTHON% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if errorlevel 1 goto :pas_de_python

if exist ".venv\Scripts\python.exe" goto :lancer

echo.
echo ==^> Creation de l'environnement Python (une seule fois)
%PYTHON% -m venv .venv
if errorlevel 1 goto :erreur

.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
echo ==^> Installation des dependances
.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
if errorlevel 1 goto :erreur
echo.

:lancer
.venv\Scripts\python.exe -m app.run %*
goto :fin

:pas_de_python
echo.
echo Python 3.9 ou plus recent est introuvable.
echo.
echo Installez-le depuis https://www.python.org/downloads/
echo en cochant bien "Add Python to PATH", puis relancez ce fichier.
echo.
pause
exit /b 1

:erreur
echo.
echo L'installation a echoue. Le detail figure dans les messages ci-dessus.
echo.
pause
exit /b 1

:fin
endlocal
