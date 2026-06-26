# --- Imports : les outils dont on a besoin ---
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware

# On crée notre application backend
app = FastAPI()

# --- Configuration CORS ---
# Ceci autorise notre page web (le frontend) à parler à ce backend.
# Sans ça, le navigateur bloquerait la communication par sécurité.
# Pour le développement on autorise tout ("*"). On restreindra avant la mise en ligne.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # quelles pages ont le droit de nous parler
    allow_methods=["*"],      # quelles actions sont autorisées (GET, POST...)
    allow_headers=["*"],
)

# --- Route de test (on la garde) ---
@app.get("/")
def accueil():
    return {"message": "Le backend fonctionne !"}

# --- Nouvelle route : recevoir l'audio ---
# Cette route attend qu'on lui ENVOIE un fichier (méthode POST).
# "fichier: UploadFile" = le fichier audio envoyé par le navigateur.
@app.post("/transcrire")
async def transcrire(fichier: UploadFile = File(...)):
    # Pour l'instant, on ne transcrit pas encore.
    # On lit juste le fichier reçu pour mesurer sa taille,
    # histoire de vérifier que la communication marche.
    contenu = await fichier.read()
    taille = len(contenu)

    # On renvoie une réponse au frontend
    return {
        "nom_fichier": fichier.filename,
        "taille_octets": taille,
        "message": "Audio bien reçu par le backend !"
    }