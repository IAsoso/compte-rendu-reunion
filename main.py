# --- Imports : les outils dont on a besoin ---
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
import whisper
import tempfile
import os

# On crée notre application backend
app = FastAPI()

# --- Configuration CORS ---
# Autorise notre page web à parler à ce backend.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Chargement du modèle Whisper (UNE SEULE FOIS au démarrage) ---
# Ça s'exécute quand le serveur démarre, pas à chaque requête.
print("Chargement du modèle Whisper... (peut prendre quelques secondes)")
modele_whisper = whisper.load_model("base")
print("Modèle Whisper prêt !")

# --- Route de test ---
@app.get("/")
def accueil():
    return {"message": "Le backend fonctionne !"}

# --- Route de transcription (la vraie, maintenant) ---
@app.post("/transcrire")
async def transcrire(fichier: UploadFile = File(...)):
    # 1. On lit l'audio reçu (en mémoire)
    contenu = await fichier.read()

    # 2. On l'écrit dans un fichier temporaire sur le disque,
    #    car Whisper a besoin d'un chemin de fichier.
    #    delete=False : on gère la suppression nous-mêmes après.
    with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as fichier_temp:
        fichier_temp.write(contenu)
        chemin_temp = fichier_temp.name

    # 3. On transcrit avec Whisper (en précisant le français)
    try:
        resultat = modele_whisper.transcribe(chemin_temp, language="fr")
        texte = resultat["text"]
    finally:
        # 4. On supprime TOUJOURS le fichier temporaire (même en cas d'erreur).
        #    C'est l'hygiène dont on a parlé.
        os.remove(chemin_temp)

    # 5. On renvoie le texte transcrit au frontend
    return {
        "texte": texte,
        "message": "Transcription réussie !"
    }