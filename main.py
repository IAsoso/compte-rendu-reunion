from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
import whisper
import tempfile
import os
from google import genai
from dotenv import load_dotenv
import json

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
# --- Fonction de NETTOYAGE de la transcription (preprocessing) ---
def nettoyer_transcription(texte_brut):
    prompt = f"""Tu es un correcteur de transcriptions audio.
Voici une transcription brute issue d'une reconnaissance vocale. Elle peut contenir des fautes, une ponctuation manquante, des hésitations (euh, ben...), des répétitions, et des mots mal transcrits.

Ta tâche : retourne UNIQUEMENT le texte corrigé et lisible.
RÈGLES STRICTES :
- Corrige l'orthographe, la grammaire et la ponctuation.
- Supprime les hésitations et répétitions inutiles.
- NE résume PAS, NE raccourcis PAS : garde tout le contenu et le sens d'origine.
- N'invente RIEN, n'ajoute aucune information.
- Ne mets aucun commentaire, retourne seulement le texte corrigé.

Transcription brute :
{texte_brut}
"""
    reponse = client_gemini.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return reponse.text

# --- Fonction qui génère le compte-rendu avec Gemini ---
# --- Fonction qui génère le compte-rendu avec Gemini ---
def generer_compte_rendu(texte_transcription, type_reunion, format_souhaite):
    # On adapte la consigne de longueur selon le format choisi
    if format_souhaite == "concis":
        consigne_format = "Sois bref et va à l'essentiel. Compte-rendu court."
    else:
        consigne_format = "Sois complet et détaillé, développe chaque point."

    prompt = f"""Tu es un assistant spécialisé dans la rédaction de comptes-rendus de réunion professionnels.

Type de réunion : {type_reunion}.
{consigne_format}

À partir de la transcription brute ci-dessous, rédige un compte-rendu clair et structuré avec les sections suivantes :
- Résumé général
- Points clés abordés
- Décisions prises
- Actions à faire (avec qui si mentionné)

Si une section n'a pas d'information, indique "Aucune information". N'utilise pas de titre principal redondant.

Transcription :
{texte_transcription}
"""
    reponse = client_gemini.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return reponse.text

# --- Fonction qui extrait les ACTIONS sous forme structurée (JSON) ---
def extraire_actions(texte_propre):
    prompt = f"""Analyse cette transcription de réunion et extrais UNIQUEMENT les actions à faire (tâches, engagements, choses à accomplir).

Pour chaque action, identifie :
- "tache" : ce qu'il faut faire (clair et concis)
- "responsable" : qui doit le faire (ou "Non spécifié" si pas mentionné)
- "echeance" : la date ou le délai (ou "Non spécifié" si pas mentionné)

Réponds avec un tableau JSON d'objets. S'il n'y a aucune action, réponds avec un tableau vide [].
N'invente aucune action qui n'est pas dans le texte.

Transcription :
{texte_propre}
"""
    reponse = client_gemini.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={"response_mime_type": "application/json"}
    )
    # On convertit la réponse JSON (texte) en vraie liste Python
    try:
        actions = json.loads(reponse.text)
    except json.JSONDecodeError:
        # Filet de sécurité : si le JSON est mal formé, on renvoie une liste vide
        actions = []
    return actions
@app.post("/transcrire")
async def transcrire(
    fichier: UploadFile = File(...),
    type_reunion: str = Form("réunion générale"),
    format: str = Form("concis"),
):
    contenu = await fichier.read()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as fichier_temp:
        fichier_temp.write(contenu)
        chemin_temp = fichier_temp.name

    try:
        resultat = modele_whisper.transcribe(chemin_temp, language="fr")
        texte_brut = resultat["text"]
    finally:
        os.remove(chemin_temp)

    # Étape de nettoyage (preprocessing) : on corrige le texte brut
    texte_propre = nettoyer_transcription(texte_brut)

    # On génère le compte-rendu à partir du texte PROPRE
    compte_rendu = generer_compte_rendu(texte_propre, type_reunion, format)

    # On extrait les actions sous forme structurée
    actions = extraire_actions(texte_propre)

    return {
        "texte_brut": texte_brut,
        "texte": texte_propre,
        "compte_rendu": compte_rendu,
        "actions": actions,
        "message": "Transcription, nettoyage, compte-rendu et actions réussis !"
    }


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