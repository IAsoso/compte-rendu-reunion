from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import tempfile
import os
import uuid
import threading
import sqlite3
from datetime import datetime
from pydub import AudioSegment
from google import genai
import groq
from groq import Groq
from dotenv import load_dotenv
import json

# --- Charger les variables du fichier .env (dont la clé Gemini) ---
load_dotenv()
cle_gemini = os.getenv("GEMINI_API_KEY")

# On configure Gemini avec la clé
client_gemini = genai.Client(api_key=cle_gemini)

# On configure Groq (transcription via l'API Whisper large v3 turbo)
cle_groq = os.getenv("GROQ_API_KEY")
client_groq = Groq(api_key=cle_groq)
MODELE_TRANSCRIPTION = "whisper-large-v3-turbo"

# --- App + CORS ---
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Transcription déportée sur l'API Groq (plus de modèle local) ---
print("Backend prêt — transcription via l'API Groq.")


# ======================================================================
#  BASE DE DONNÉES (SQLite) — stockage permanent des comptes-rendus
# ======================================================================
CHEMIN_DB = "synthia.db"


def init_db():
    """Crée la table si elle n'existe pas encore (appelé au démarrage)."""
    conn = sqlite3.connect(CHEMIN_DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS comptes_rendus (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            date_creation TEXT NOT NULL,
            type_reunion  TEXT,
            format        TEXT,
            transcription TEXT,
            compte_rendu  TEXT,
            actions       TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def enregistrer_compte_rendu(type_reunion, format_souhaite, transcription, compte_rendu, actions):
    """Insère un compte-rendu et renvoie son identifiant."""
    conn = sqlite3.connect(CHEMIN_DB)
    curseur = conn.execute(
        """
        INSERT INTO comptes_rendus
            (date_creation, type_reunion, format, transcription, compte_rendu, actions)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now().isoformat(timespec="seconds"),
            type_reunion,
            format_souhaite,
            transcription,
            compte_rendu,
            json.dumps(actions, ensure_ascii=False),  # liste -> texte JSON
        ),
    )
    conn.commit()
    nouvel_id = curseur.lastrowid
    conn.close()
    return nouvel_id


def lister_comptes_rendus():
    """Renvoie l'essentiel de chaque compte-rendu, du plus récent au plus ancien."""
    conn = sqlite3.connect(CHEMIN_DB)
    conn.row_factory = sqlite3.Row  # accès par nom de colonne
    lignes = conn.execute(
        "SELECT id, date_creation, type_reunion, format FROM comptes_rendus ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return [dict(ligne) for ligne in lignes]


def recuperer_compte_rendu(cr_id):
    """Renvoie un compte-rendu complet (avec actions décodées), ou None si introuvable."""
    conn = sqlite3.connect(CHEMIN_DB)
    conn.row_factory = sqlite3.Row
    ligne = conn.execute(
        "SELECT * FROM comptes_rendus WHERE id = ?", (cr_id,)
    ).fetchone()
    conn.close()
    if ligne is None:
        return None
    compte_rendu = dict(ligne)
    try:
        compte_rendu["actions"] = json.loads(compte_rendu["actions"]) if compte_rendu["actions"] else []
    except (json.JSONDecodeError, TypeError):
        compte_rendu["actions"] = []
    return compte_rendu


# On prépare la base dès le démarrage du serveur
init_db()


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


# ======================================================================
#  GESTION DES LONGUES RÉUNIONS (découpage + map-reduce)
# ======================================================================

# Durée d'une tranche pour les audios longs (5 minutes)
DUREE_TRANCHE_MS = 5 * 60 * 1000


def trouver_coupure_silencieuse(audio, position_ms, fenetre_ms=3000, pas_ms=100):
    """Cherche le moment le plus silencieux autour d'une position, pour couper
    proprement (pas au milieu d'un mot). N'analyse qu'une petite fenêtre."""
    debut = max(0, position_ms - fenetre_ms)
    fin = min(len(audio), position_ms + fenetre_ms)
    meilleure_position = position_ms
    volume_min = float("inf")
    for p in range(debut, fin, pas_ms):
        volume = audio[p:p + pas_ms].dBFS  # -inf pour un silence total
        if volume < volume_min:
            volume_min = volume
            meilleure_position = p
    return meilleure_position


def decouper_audio(audio):
    """Découpe un AudioSegment en tranches d'environ 5 min, coupées sur les
    zones les plus silencieuses. Renvoie la liste des chemins de fichiers WAV."""
    duree = len(audio)
    # 1. Calcul des points de coupe (calés sur les silences)
    points = [0]
    position = DUREE_TRANCHE_MS
    while position < duree:
        coupure = trouver_coupure_silencieuse(audio, position)
        if coupure <= points[-1]:      # sécurité : toujours avancer
            coupure = position
        points.append(coupure)
        position += DUREE_TRANCHE_MS
    points.append(duree)

    # 2. Export de chaque tranche en WAV temporaire
    chemins = []
    for i in range(len(points) - 1):
        morceau = audio[points[i]:points[i + 1]]
        fichier_temp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        # 16 kHz mono : suffisant pour Whisper et bien plus léger à uploader
        morceau.set_frame_rate(16000).set_channels(1).export(fichier_temp.name, format="wav")
        fichier_temp.close()
        chemins.append(fichier_temp.name)
    return chemins


def resumer_morceau(texte_morceau, type_reunion):
    """MAP : produit des notes structurées concises pour un extrait de réunion."""
    prompt = f"""Tu analyses UN EXTRAIT d'une réunion ({type_reunion}).
À partir de cet extrait, produis des notes concises et structurées : points abordés,
décisions évoquées, actions/engagements mentionnés. Ne rédige PAS encore le compte-rendu
final, ne fais pas d'introduction : uniquement des notes fidèles à cet extrait.

Extrait :
{texte_morceau}
"""
    reponse = client_gemini.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return reponse.text


def generer_compte_rendu_depuis_resumes(resumes, type_reunion, format_souhaite):
    """REDUCE : assemble les notes de toutes les parties en UN compte-rendu final."""
    if format_souhaite == "concis":
        consigne_format = "Sois bref et va à l'essentiel. Compte-rendu court."
    else:
        consigne_format = "Sois complet et détaillé, développe chaque point."

    corpus = "\n\n".join(
        f"[Partie {i + 1}]\n{r}" for i, r in enumerate(resumes)
    )

    prompt = f"""Tu es un assistant spécialisé dans la rédaction de comptes-rendus de réunion professionnels.

Type de réunion : {type_reunion}.
{consigne_format}

Voici les NOTES de plusieurs parties successives d'UNE MÊME réunion. Rédige UN SEUL
compte-rendu final, cohérent et non redondant (fusionne les répétitions entre parties),
avec les sections suivantes :
- Résumé général
- Points clés abordés
- Décisions prises
- Actions à faire (avec qui si mentionné)

Si une section n'a pas d'information, indique "Aucune information". N'utilise pas de titre principal redondant.

Notes des différentes parties :
{corpus}
"""
    reponse = client_gemini.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return reponse.text


def transcrire_audio_groq(chemin_audio):
    """Transcrit un fichier audio via l'API Groq (Whisper large v3 turbo).
    Lève une RuntimeError avec un message clair en cas d'échec."""
    try:
        with open(chemin_audio, "rb") as fichier:
            transcription = client_groq.audio.transcriptions.create(
                file=(os.path.basename(chemin_audio), fichier.read()),
                model=MODELE_TRANSCRIPTION,
                language="fr",
            )
        return transcription.text
    except groq.AuthenticationError:
        raise RuntimeError("Clé API Groq invalide ou manquante (GROQ_API_KEY).")
    except groq.RateLimitError:
        raise RuntimeError("Quota Groq dépassé, réessayez dans un instant.")
    except groq.APIConnectionError:
        raise RuntimeError("Impossible de joindre l'API Groq (problème de connexion).")
    except groq.APIStatusError as erreur:
        raise RuntimeError(f"Erreur de l'API Groq (code {erreur.status_code}).")
    except Exception as erreur:  # filet de sécurité
        raise RuntimeError(f"Échec de la transcription Groq : {erreur}")


def traiter_audio_complet(chemin_audio, type_reunion, format_souhaite, maj_progression):
    """Traite un audio de bout en bout. Aiguille vers le flux court (existant) ou
    le flux long (découpage + map-reduce). `maj_progression(phase, courant, total, message)`
    est appelé régulièrement pour suivre l'avancement."""
    audio = AudioSegment.from_file(chemin_audio)
    duree = len(audio)

    # ---------- Audio COURT : flux existant, inchangé ----------
    if duree <= DUREE_TRANCHE_MS:
        maj_progression("transcription", 0, 1, "Transcription en cours…")
        texte_brut = transcrire_audio_groq(chemin_audio)

        maj_progression("redaction", 0, 1, "Rédaction du compte-rendu…")
        texte_propre = nettoyer_transcription(texte_brut)
        compte_rendu = generer_compte_rendu(texte_propre, type_reunion, format_souhaite)
        actions = extraire_actions(texte_propre)

        cr_id = enregistrer_compte_rendu(
            type_reunion, format_souhaite, texte_propre, compte_rendu, actions
        )
        return {
            "id": cr_id,
            "texte_brut": texte_brut,
            "texte": texte_propre,
            "compte_rendu": compte_rendu,
            "actions": actions,
            "message": "Transcription, nettoyage, compte-rendu et actions réussis !",
        }

    # ---------- Audio LONG : découpage + map-reduce ----------
    maj_progression("decoupage", 0, 1, "Découpage de l'audio en tranches…")
    morceaux = decouper_audio(audio)
    total = len(morceaux)

    transcriptions = []
    try:
        for i, chemin_morceau in enumerate(morceaux):
            maj_progression("transcription", i, total, f"Transcription {i + 1}/{total}…")
            transcriptions.append(transcrire_audio_groq(chemin_morceau).strip())
        maj_progression("transcription", total, total, "Transcription terminée")
    finally:
        for chemin_morceau in morceaux:
            os.remove(chemin_morceau)

    texte_complet = " ".join(transcriptions)

    # MAP : notes par tranche
    resumes = []
    for i, texte_morceau in enumerate(transcriptions):
        maj_progression("redaction", i, total, f"Analyse de la partie {i + 1}/{total}…")
        resumes.append(resumer_morceau(texte_morceau, type_reunion))

    # REDUCE : compte-rendu final + actions (sur les notes, courtes)
    maj_progression("redaction", total, total, "Rédaction du compte-rendu final…")
    compte_rendu = generer_compte_rendu_depuis_resumes(resumes, type_reunion, format_souhaite)
    actions = extraire_actions("\n\n".join(resumes))

    cr_id = enregistrer_compte_rendu(
        type_reunion, format_souhaite, texte_complet, compte_rendu, actions
    )
    return {
        "id": cr_id,
        "texte_brut": texte_complet,
        "texte": texte_complet,
        "compte_rendu": compte_rendu,
        "actions": actions,
        "message": f"Réunion longue traitée en {total} tranches.",
    }


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

    # Progression neutre : cette route reste synchrone (compatibilité).
    def sans_progression(phase, courant, total, message):
        pass

    try:
        resultat = traiter_audio_complet(
            chemin_temp, type_reunion, format, sans_progression
        )
    except RuntimeError as erreur:
        raise HTTPException(status_code=502, detail=str(erreur))
    finally:
        if os.path.exists(chemin_temp):
            os.remove(chemin_temp)

    return resultat


# --- Routes ---
@app.get("/")
def accueil():
    return {"message": "Le backend fonctionne !"}


# --- Modification du compte-rendu par IA (chat en langage naturel) ---
class DemandeModification(BaseModel):
    compte_rendu: str
    instruction: str


def modifier_compte_rendu_ia(compte_rendu, instruction):
    prompt = f"""Voici un compte-rendu de réunion. Applique cette modification demandée par l'utilisateur, et renvoie UNIQUEMENT le compte-rendu modifié en entier, sans commentaire.

Modification demandée : {instruction}

Compte-rendu actuel :
{compte_rendu}
"""
    reponse = client_gemini.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return reponse.text


@app.post("/modifier-compte-rendu")
def modifier_compte_rendu(demande: DemandeModification):
    compte_rendu_modifie = modifier_compte_rendu_ia(
        demande.compte_rendu, demande.instruction
    )
    return {"compte_rendu": compte_rendu_modifie}


# ======================================================================
#  TRAITEMENT ASYNCHRONE + PROGRESSION (pour les longues réunions)
# ======================================================================

# Magasin en mémoire des travaux en cours (clé = job_id)
travaux = {}

# Pondération des phases pour convertir l'avancement en pourcentage global
PONDERATION_PHASES = {
    "demarrage": (0, 2),
    "decoupage": (2, 6),
    "transcription": (6, 72),
    "redaction": (72, 99),
}


def _calculer_pourcentage(phase, courant, total):
    bas, haut = PONDERATION_PHASES.get(phase, (0, 99))
    fraction = (courant / total) if total else 0
    return int(bas + (haut - bas) * fraction)


@app.post("/transcrire-async")
async def transcrire_async(
    fichier: UploadFile = File(...),
    type_reunion: str = Form("réunion générale"),
    format: str = Form("concis"),
):
    # On enregistre le fichier tout de suite (on ne peut pas lire l'upload dans le thread)
    contenu = await fichier.read()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as fichier_temp:
        fichier_temp.write(contenu)
        chemin_temp = fichier_temp.name

    job_id = uuid.uuid4().hex
    travaux[job_id] = {
        "statut": "en_cours",
        "phase": "demarrage",
        "courant": 0,
        "total": 1,
        "pourcentage": 0,
        "message": "Initialisation…",
    }

    def worker():
        def maj_progression(phase, courant, total, message):
            travaux[job_id].update(
                phase=phase,
                courant=courant,
                total=total,
                message=message,
                pourcentage=_calculer_pourcentage(phase, courant, total),
            )

        try:
            resultat = traiter_audio_complet(
                chemin_temp, type_reunion, format, maj_progression
            )
            travaux[job_id].update(
                statut="termine",
                pourcentage=100,
                message="Compte-rendu prêt !",
                resultat=resultat,
            )
        except Exception as erreur:
            travaux[job_id].update(statut="erreur", erreur=str(erreur))
            print("Erreur de traitement :", erreur)
        finally:
            if os.path.exists(chemin_temp):
                os.remove(chemin_temp)

    threading.Thread(target=worker, daemon=True).start()
    return {"job_id": job_id}


@app.get("/progression/{job_id}")
def progression(job_id: str):
    travail = travaux.get(job_id)
    if travail is None:
        return {"statut": "inconnu"}

    reponse = {
        "statut": travail["statut"],
        "phase": travail["phase"],
        "courant": travail["courant"],
        "total": travail["total"],
        "pourcentage": travail["pourcentage"],
        "message": travail["message"],
    }
    if travail["statut"] == "termine":
        reponse["resultat"] = travail["resultat"]
        travaux.pop(job_id, None)  # nettoyage après livraison
    elif travail["statut"] == "erreur":
        reponse["erreur"] = travail.get("erreur", "Erreur inconnue")
        travaux.pop(job_id, None)
    return reponse


# ======================================================================
#  HISTORIQUE (lecture des comptes-rendus enregistrés)
# ======================================================================

@app.get("/historique")
def historique():
    """Liste tous les comptes-rendus (essentiel : id, date, type, format)."""
    return lister_comptes_rendus()


@app.get("/compte-rendu/{cr_id}")
def compte_rendu_par_id(cr_id: int):
    """Renvoie un compte-rendu complet par son identifiant."""
    compte_rendu = recuperer_compte_rendu(cr_id)
    if compte_rendu is None:
        raise HTTPException(status_code=404, detail="Compte-rendu introuvable")
    return compte_rendu