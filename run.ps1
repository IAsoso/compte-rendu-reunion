# run.ps1 — Lance le serveur Synthia (backend FastAPI) via le venv du projet.
# Usage : clic droit > "Exécuter avec PowerShell", ou dans un terminal : .\run.ps1

# On se place dans le dossier du script (le projet), quel que soit le répertoire courant.
Set-Location -Path $PSScriptRoot

$pythonVenv = Join-Path $PSScriptRoot "venv\Scripts\python.exe"

# Vérifie que le venv existe bien avant de lancer.
if (-not (Test-Path $pythonVenv)) {
    Write-Host "ERREUR : venv introuvable ($pythonVenv)." -ForegroundColor Red
    Write-Host "Créez-le puis installez les dépendances :" -ForegroundColor Yellow
    Write-Host "    py -m venv venv"
    Write-Host "    .\venv\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}

# Active le venv pour la session courante (met le venv en tête du PATH).
& (Join-Path $PSScriptRoot "venv\Scripts\Activate.ps1")

Write-Host "Demarrage du serveur sur http://127.0.0.1:8000 (Ctrl+C pour arreter)..." -ForegroundColor Green

# Lance uvicorn avec le python du venv (rechargement auto en developpement).
& $pythonVenv -m uvicorn main:app --reload
