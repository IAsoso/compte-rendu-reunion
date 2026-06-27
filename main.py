# --- Imports ---
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
import whisper
import tempfile
import os
from google import genai
from dotenv import load_dotenv

# --- Charger les variables du fichier .env (dont la clé Gemini) ---
load_dotenv()
cle_gemini = os.getenv("GEMINI_API_KEY")

# On configure Gemini avec la clé
client_gemini = genai.Client(api_key=cle_gemini)

# --- App + CORS ---
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Chargement de Whisper (une seule fois) ---
print("Chargement du modèle Whisper...")
modele_whisper = whisper.load_model("base")
print("Modèle Whisper prêt !")


# --- Fonction qui génère le compte-rendu avec Gemini ---
def generer_compte_rendu(texte_transcription):
    # Le prompt : rôle + tâche + format + données
    prompt = f"""Tu es un assistant spécialisé dans la rédaction de comptes-rendus de réunion professionnels.

À partir de la transcription brute ci-dessous, rédige un compte-rendu clair et structuré avec les sections suivantes :
- Résumé général (2-3 phrases)
- Points clés abordés
- Décisions prises
- Actions à faire (avec qui si mentionné)

Sois concis et professionnel. Si une section n'a pas d'information, indique "Aucune information".

Transcription :
{texte_transcription}
"""
    reponse = client_gemini.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return reponse.text


# --- Routes ---
@app.get("/")
def accueil():
    return {"message": "Le backend fonctionne !"}


@app.post("/transcrire")
async def transcrire(fichier: UploadFile = File(...)):
    # 1. Lire l'audio reçu
    contenu = await fichier.read()

    # 2. Écrire dans un fichier temporaire
    with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as fichier_temp:
        fichier_temp.write(contenu)
        chemin_temp = fichier_temp.name

    # 3. Transcrire avec Whisper
    try:
        resultat = modele_whisper.transcribe(chemin_temp, language="fr")
        texte = resultat["text"]
    finally:
        os.remove(chemin_temp)

    # 4. Générer le compte-rendu avec Gemini
    compte_rendu = generer_compte_rendu(texte)

    # 5. Renvoyer la transcription ET le compte-rendu
    return {
        "texte": texte,
        "compte_rendu": compte_rendu,
        "message": "Transcription et compte-rendu réussis !"
    }