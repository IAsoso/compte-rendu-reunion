from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
import tempfile
import os
import uuid
import threading
import time
import sqlite3
from datetime import datetime, timedelta, timezone
import secrets
from passlib.context import CryptContext
import jwt
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
from pydub import AudioSegment
import imageio_ffmpeg
from google import genai
from google.genai import errors as genai_errors

# FFmpeg n'est pas installé sur le système (ni en local Windows, ni sur Render).
# On pointe pydub vers le binaire FFmpeg embarqué par le paquet imageio-ffmpeg.
AudioSegment.converter = imageio_ffmpeg.get_ffmpeg_exe()
import groq
from groq import Groq
from dotenv import load_dotenv
import json
import stripe

# --- Charger les variables du fichier .env (dont la clé Gemini) ---
load_dotenv()
cle_gemini = os.getenv("GEMINI_API_KEY")

# On configure Gemini avec la clé
client_gemini = genai.Client(api_key=cle_gemini)

# On configure Groq (transcription via l'API Whisper large v3 turbo)
cle_groq = os.getenv("GROQ_API_KEY")
client_groq = Groq(api_key=cle_groq)
MODELE_TRANSCRIPTION = "whisper-large-v3-turbo"


# ======================================================================
#  APPELS GEMINI — respect du quota (10 req/min) + retry sur 429
# ----------------------------------------------------------------------
#  Le pipeline enchaîne plusieurs appels Gemini rapprochés (nettoyage,
#  résumé, actions, map-reduce). On les espace pour rester sous la limite,
#  et on réessaie automatiquement en cas d'erreur 429.
# ======================================================================
INTERVALLE_MIN_GEMINI_S = 6.5   # >= 6 s => au plus ~9-10 appels par minute
GEMINI_MAX_ESSAIS = 3           # nombre total de tentatives par appel

_gemini_verrou = threading.Lock()   # sérialise l'espacement (jobs concurrents)
_gemini_dernier_appel = [0.0]       # horodatage (monotonic) du dernier appel


def appeler_gemini(**kwargs):
    """Appelle Gemini en garantissant un intervalle minimum entre deux appels
    et en réessayant sur erreur 429 (backoff). Lève RuntimeError avec un
    message clair si la limite persiste après GEMINI_MAX_ESSAIS tentatives."""
    for essai in range(1, GEMINI_MAX_ESSAIS + 1):
        # Pause pour respecter l'intervalle minimum depuis le dernier appel.
        with _gemini_verrou:
            attente = INTERVALLE_MIN_GEMINI_S - (time.monotonic() - _gemini_dernier_appel[0])
            if attente > 0:
                time.sleep(attente)
            _gemini_dernier_appel[0] = time.monotonic()

        try:
            return client_gemini.models.generate_content(**kwargs)
        except genai_errors.ClientError as erreur:
            est_429 = getattr(erreur, "code", None) == 429
            if est_429 and essai < GEMINI_MAX_ESSAIS:
                time.sleep(INTERVALLE_MIN_GEMINI_S * essai)  # backoff croissant
                continue
            if est_429:
                raise RuntimeError(
                    "Limite de requêtes de l'IA atteinte (quota Gemini : 10/min). "
                    "Réessayez dans une minute."
                )
            raise

# ======================================================================
#  AUTHENTIFICATION (comptes email / mot de passe)
# ----------------------------------------------------------------------
#  - bcrypt : hachage à sens unique des mots de passe (jamais en clair).
#  - JWT    : jeton signé (HS256) prouvant l'identité, sans session serveur.
# ======================================================================

# Contexte de hachage : bcrypt, sel aléatoire par mot de passe géré en interne.
contexte_mdp = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Clé secrète de signature des JWT — OBLIGATOIRE, jamais commitée (.env / Render).
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not JWT_SECRET_KEY:
    raise RuntimeError(
        "JWT_SECRET_KEY manquante. Ajoutez-la dans .env (local) ou dans les "
        "variables d'environnement Render avant de démarrer le backend."
    )
JWT_ALGORITHME = "HS256"
JWT_DUREE_JOURS = 7  # durée de validité d'un jeton avant reconnexion

# Client ID OAuth Google (public, pas un secret). Requis pour la connexion Google.
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")


def hacher_mot_de_passe(mot_de_passe: str) -> str:
    """Renvoie le hash bcrypt (avec sel) d'un mot de passe en clair."""
    return contexte_mdp.hash(mot_de_passe)


def verifier_mot_de_passe(mot_de_passe: str, hash_stocke: str) -> bool:
    """Vérifie qu'un mot de passe en clair correspond au hash stocké."""
    return contexte_mdp.verify(mot_de_passe, hash_stocke)


def creer_token(user_id: int) -> str:
    """Génère un JWT signé contenant l'id utilisateur et une date d'expiration."""
    maintenant = datetime.now(timezone.utc)
    charge_utile = {
        "sub": str(user_id),                               # identifiant du compte
        "iat": maintenant,                                 # émis à
        "exp": maintenant + timedelta(days=JWT_DUREE_JOURS),  # expire à
    }
    return jwt.encode(charge_utile, JWT_SECRET_KEY, algorithm=JWT_ALGORITHME)


def utilisateur_courant(authorization: str = Header(default="")) -> int:
    """Dépendance FastAPI : lit l'en-tête « Authorization: Bearer <token> »,
    vérifie la signature et l'expiration, renvoie l'id de l'utilisateur.
    Lève 401 si le jeton est absent, invalide ou expiré."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Jeton d'authentification manquant.")
    token = authorization[len("Bearer "):].strip()
    try:
        charge_utile = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHME])
        return int(charge_utile["sub"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expirée, reconnectez-vous.")
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise HTTPException(status_code=401, detail="Jeton d'authentification invalide.")


# --- App + CORS ---
app = FastAPI()

# Origines autorisées : configurables via FRONTEND_URL (une ou plusieurs URLs
# séparées par des virgules). Si la variable est absente/vide, on retombe sur
# "*" — pratique en développement local.
origines_env = os.getenv("FRONTEND_URL", "")
origines_autorisees = [o.strip() for o in origines_env.split(",") if o.strip()] or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origines_autorisees,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Transcription déportée sur l'API Groq (plus de modèle local) ---
print("Backend prêt — transcription via l'API Groq.")


# ======================================================================
#  ABONNEMENTS & PAIEMENT (Stripe)
# ----------------------------------------------------------------------
#  - PLANS définit les offres commerciales : quota de minutes/mois et
#    l'identifiant du "Price" Stripe correspondant (créé dans le Dashboard
#    Stripe, PAS dans ce code). Le plan "gratuit" n'a pas de Price Stripe.
#  - URL_FRONTEND sert à construire les liens de retour après Checkout /
#    Customer Portal (reprend la 1ère origine autorisée en CORS).
# ======================================================================
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

URL_FRONTEND = (
    origines_autorisees[0]
    if origines_autorisees and origines_autorisees[0] != "*"
    else "http://127.0.0.1:5500"
)

PLANS = {
    "gratuit": {
        "label": "Gratuit",
        "prix_eur": 0,
        "minutes_mois": 120,
        "stripe_price_id": None,
    },
    "pro": {
        "label": "Pro",
        "prix_eur": 15,
        "minutes_mois": 1200,
        "stripe_price_id": os.getenv("STRIPE_PRICE_ID_PRO", ""),
    },
    "business": {
        "label": "Business",
        "prix_eur": 29,
        "minutes_mois": 3000,
        "stripe_price_id": os.getenv("STRIPE_PRICE_ID_BUSINESS", ""),
    },
}

# Table inverse : Price Stripe -> nom du plan (utile pour le webhook).
PRICE_VERS_PLAN = {
    infos["stripe_price_id"]: nom
    for nom, infos in PLANS.items()
    if infos["stripe_price_id"]
}


# ======================================================================
#  BASE DE DONNÉES (SQLite) — stockage permanent des comptes-rendus
# ======================================================================
# Chemin de la base : résolu par rapport à ce fichier (robuste quel que soit le
# répertoire de lancement), surchargeable via DB_PATH — utile sur Render pour
# pointer vers un disque persistant monté (ex. /var/data/synthia.db).
CHEMIN_DB = os.getenv(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "synthia.db"),
)


def init_db():
    """Crée/migre les tables au démarrage (idempotent)."""
    conn = sqlite3.connect(CHEMIN_DB)

    # Table des comptes-rendus (existante).
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

    # Table des utilisateurs (comptes email / mot de passe).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS utilisateurs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            email             TEXT NOT NULL UNIQUE,
            mot_de_passe_hash TEXT NOT NULL,
            date_creation     TEXT NOT NULL
        )
        """
    )

    # Migration : ajoute user_id à comptes_rendus si la colonne n'existe pas
    # encore. Les comptes-rendus déjà présents restent à NULL (orphelins) :
    # ils ne sont ni supprimés, ni rattachés automatiquement.
    colonnes = {ligne[1] for ligne in conn.execute("PRAGMA table_info(comptes_rendus)")}
    if "user_id" not in colonnes:
        conn.execute(
            "ALTER TABLE comptes_rendus "
            "ADD COLUMN user_id INTEGER REFERENCES utilisateurs(id)"
        )

    # Migration : préférences par défaut sur utilisateurs (NULL = valeurs par
    # défaut appliquées à la lecture). Idempotent.
    cols_users = {ligne[1] for ligne in conn.execute("PRAGMA table_info(utilisateurs)")}
    if "type_reunion_defaut" not in cols_users:
        conn.execute("ALTER TABLE utilisateurs ADD COLUMN type_reunion_defaut TEXT")
    if "format_defaut" not in cols_users:
        conn.execute("ALTER TABLE utilisateurs ADD COLUMN format_defaut TEXT")

    # Migration : abonnement / facturation Stripe. "plan" vaut "gratuit" par
    # défaut (NULL traité comme "gratuit" à la lecture, cf. get_plan_utilisateur).
    if "plan" not in cols_users:
        conn.execute("ALTER TABLE utilisateurs ADD COLUMN plan TEXT DEFAULT 'gratuit'")
    if "stripe_customer_id" not in cols_users:
        conn.execute("ALTER TABLE utilisateurs ADD COLUMN stripe_customer_id TEXT")
    if "stripe_subscription_id" not in cols_users:
        conn.execute("ALTER TABLE utilisateurs ADD COLUMN stripe_subscription_id TEXT")
    if "abonnement_statut" not in cols_users:
        conn.execute("ALTER TABLE utilisateurs ADD COLUMN abonnement_statut TEXT")

    # Table de suivi d'usage : une ligne par compte-rendu traité avec succès,
    # utilisée pour calculer la consommation du mois en cours par utilisateur.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_audio (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL REFERENCES utilisateurs(id),
            date_creation TEXT NOT NULL,
            duree_minutes REAL NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()


# Préférences : options autorisées (mêmes valeurs que les select de app.html)
TYPES_REUNION = {
    "réunion d'équipe", "réunion client", "réunion de projet",
    "point rapide", "réunion générale",
}
FORMATS = {"concis", "détaillé"}
TYPE_REUNION_DEFAUT = "réunion générale"
FORMAT_DEFAUT = "concis"


def get_preferences(user_id):
    """Renvoie les préférences de l'utilisateur, valeurs par défaut si non définies."""
    conn = sqlite3.connect(CHEMIN_DB)
    ligne = conn.execute(
        "SELECT type_reunion_defaut, format_defaut FROM utilisateurs WHERE id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    type_reunion = (ligne[0] if ligne else None) or TYPE_REUNION_DEFAUT
    format_defaut = (ligne[1] if ligne else None) or FORMAT_DEFAUT
    return {"type_reunion": type_reunion, "format": format_defaut}


def maj_preferences(user_id, type_reunion, format_souhaite):
    """Enregistre les préférences de l'utilisateur."""
    conn = sqlite3.connect(CHEMIN_DB)
    conn.execute(
        "UPDATE utilisateurs SET type_reunion_defaut = ?, format_defaut = ? WHERE id = ?",
        (type_reunion, format_souhaite, user_id),
    )
    conn.commit()
    conn.close()


# ======================================================================
#  ABONNEMENT & QUOTA (minutes d'audio traitées par mois)
# ----------------------------------------------------------------------
#  Le quota est vérifié AVANT tout appel Groq/Gemini (coût réel), et la
#  consommation n'est enregistrée qu'APRÈS un traitement réussi.
# ======================================================================

def get_plan_utilisateur(user_id):
    """Renvoie le nom du plan de l'utilisateur ("gratuit" par défaut/repli)."""
    conn = sqlite3.connect(CHEMIN_DB)
    ligne = conn.execute("SELECT plan FROM utilisateurs WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    plan = (ligne[0] if ligne else None) or "gratuit"
    return plan if plan in PLANS else "gratuit"


def minutes_utilisees_ce_mois(user_id):
    """Somme des minutes d'audio traitées depuis le 1er du mois en cours."""
    debut_mois = datetime.now().replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    ).isoformat(timespec="seconds")
    conn = sqlite3.connect(CHEMIN_DB)
    ligne = conn.execute(
        "SELECT COALESCE(SUM(duree_minutes), 0) FROM usage_audio "
        "WHERE user_id = ? AND date_creation >= ?",
        (user_id, debut_mois),
    ).fetchone()
    conn.close()
    return ligne[0] or 0.0


def enregistrer_usage(user_id, duree_minutes):
    """Enregistre la consommation d'un compte-rendu traité avec succès."""
    conn = sqlite3.connect(CHEMIN_DB)
    conn.execute(
        "INSERT INTO usage_audio (user_id, date_creation, duree_minutes) VALUES (?, ?, ?)",
        (user_id, datetime.now().isoformat(timespec="seconds"), duree_minutes),
    )
    conn.commit()
    conn.close()


def duree_minutes_fichier(chemin_audio):
    """Durée (en minutes) d'un fichier audio, sans lancer aucun traitement IA."""
    return len(AudioSegment.from_file(chemin_audio)) / 60000.0


def verifier_quota(user_id, duree_minutes_demandees):
    """Lève une HTTPException 402 si traiter cet audio dépasserait le quota
    mensuel du plan de l'utilisateur. Appelée AVANT tout appel Groq/Gemini
    pour ne jamais payer un traitement qu'on va refuser."""
    plan = get_plan_utilisateur(user_id)
    quota = PLANS[plan]["minutes_mois"]
    deja_utilisees = minutes_utilisees_ce_mois(user_id)
    if deja_utilisees + duree_minutes_demandees > quota:
        restantes = max(0.0, quota - deja_utilisees)
        raise HTTPException(
            status_code=402,
            detail=(
                f"Quota mensuel atteint ({quota} min incluses dans l'offre "
                f"{PLANS[plan]['label']}). Il vous reste {restantes:.1f} min ce "
                "mois-ci. Passez à une offre supérieure pour continuer."
            ),
        )


def mesurer_duree_et_verifier_quota(chemin_temp, user_id):
    """Mesure la durée de l'audio puis vérifie le quota. En cas d'échec (fichier
    illisible ou quota dépassé), supprime le fichier temporaire et lève une
    HTTPException adaptée. Renvoie la durée en minutes si tout est bon."""
    try:
        duree_min = duree_minutes_fichier(chemin_temp)
    except Exception:
        if os.path.exists(chemin_temp):
            os.remove(chemin_temp)
        raise HTTPException(status_code=400, detail="Fichier audio illisible ou invalide.")
    try:
        verifier_quota(user_id, duree_min)
    except HTTPException:
        if os.path.exists(chemin_temp):
            os.remove(chemin_temp)
        raise
    return duree_min


def get_infos_facturation(user_id):
    """Renvoie (email, stripe_customer_id) de l'utilisateur."""
    conn = sqlite3.connect(CHEMIN_DB)
    ligne = conn.execute(
        "SELECT email, stripe_customer_id FROM utilisateurs WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()
    if ligne is None:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
    return ligne[0], ligne[1]


def maj_stripe_customer_id(user_id, stripe_customer_id):
    conn = sqlite3.connect(CHEMIN_DB)
    conn.execute(
        "UPDATE utilisateurs SET stripe_customer_id = ? WHERE id = ?",
        (stripe_customer_id, user_id),
    )
    conn.commit()
    conn.close()


def maj_abonnement(user_id, plan, subscription_id, statut):
    """Met à jour le plan/l'état d'abonnement d'un utilisateur (appelé depuis
    le webhook Stripe, jamais directement depuis le frontend)."""
    conn = sqlite3.connect(CHEMIN_DB)
    conn.execute(
        "UPDATE utilisateurs SET plan = ?, stripe_subscription_id = ?, "
        "abonnement_statut = ? WHERE id = ?",
        (plan, subscription_id, statut, user_id),
    )
    conn.commit()
    conn.close()


def get_user_id_par_stripe_customer(stripe_customer_id):
    conn = sqlite3.connect(CHEMIN_DB)
    ligne = conn.execute(
        "SELECT id FROM utilisateurs WHERE stripe_customer_id = ?", (stripe_customer_id,)
    ).fetchone()
    conn.close()
    return ligne[0] if ligne else None


# ----------------------------------------------------------------------
#  Accès à la table utilisateurs
# ----------------------------------------------------------------------
def creer_utilisateur(email, mot_de_passe_hash):
    """Insère un nouvel utilisateur et renvoie son id. Lève sqlite3.IntegrityError
    si l'email existe déjà (contrainte UNIQUE)."""
    conn = sqlite3.connect(CHEMIN_DB)
    try:
        curseur = conn.execute(
            "INSERT INTO utilisateurs (email, mot_de_passe_hash, date_creation) "
            "VALUES (?, ?, ?)",
            (email, mot_de_passe_hash, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        return curseur.lastrowid
    finally:
        conn.close()


def get_utilisateur_par_email(email):
    """Renvoie {id, email, mot_de_passe_hash} pour un email, ou None si absent."""
    conn = sqlite3.connect(CHEMIN_DB)
    conn.row_factory = sqlite3.Row
    ligne = conn.execute(
        "SELECT id, email, mot_de_passe_hash FROM utilisateurs WHERE email = ?",
        (email,),
    ).fetchone()
    conn.close()
    return dict(ligne) if ligne else None


def get_ou_creer_utilisateur_google(email):
    """Renvoie l'id de l'utilisateur pour cet email (déjà prouvé par Google) :
    connecte au compte existant, ou en crée un nouveau. Le compte créé reçoit un
    hash bcrypt d'un secret aléatoire : la connexion par mot de passe est donc
    impossible tant qu'aucun mot de passe n'a été choisi."""
    utilisateur = get_utilisateur_par_email(email)
    if utilisateur is not None:
        return utilisateur["id"]
    hash_inutilisable = hacher_mot_de_passe(secrets.token_urlsafe(32))
    return creer_utilisateur(email, hash_inutilisable)


def enregistrer_compte_rendu(user_id, type_reunion, format_souhaite, transcription, compte_rendu, actions):
    """Insère un compte-rendu rattaché à un utilisateur et renvoie son identifiant."""
    conn = sqlite3.connect(CHEMIN_DB)
    curseur = conn.execute(
        """
        INSERT INTO comptes_rendus
            (date_creation, type_reunion, format, transcription, compte_rendu, actions, user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now().isoformat(timespec="seconds"),
            type_reunion,
            format_souhaite,
            transcription,
            compte_rendu,
            json.dumps(actions, ensure_ascii=False),  # liste -> texte JSON
            user_id,
        ),
    )
    conn.commit()
    nouvel_id = curseur.lastrowid
    conn.close()
    return nouvel_id


def lister_comptes_rendus(user_id, recherche=None):
    """Renvoie l'essentiel des comptes-rendus DE CET UTILISATEUR, du + récent au
    + ancien. Si `recherche` est fourni, filtre sur le type, la date, le
    compte-rendu et la transcription (sans jamais sortir des CR de l'utilisateur)."""
    conn = sqlite3.connect(CHEMIN_DB)
    conn.row_factory = sqlite3.Row  # accès par nom de colonne
    sql = "SELECT id, date_creation, type_reunion, format FROM comptes_rendus WHERE user_id = ?"
    params = [user_id]
    if recherche:
        motif = f"%{recherche}%"
        sql += (
            " AND (type_reunion LIKE ? OR date_creation LIKE ? "
            "OR compte_rendu LIKE ? OR transcription LIKE ?)"
        )
        params += [motif, motif, motif, motif]
    sql += " ORDER BY id DESC"
    lignes = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(ligne) for ligne in lignes]


def recuperer_compte_rendu(cr_id, user_id):
    """Renvoie un compte-rendu complet SEULEMENT s'il appartient à l'utilisateur,
    sinon None (isolation stricte : on ne révèle pas l'existence des CR d'autrui)."""
    conn = sqlite3.connect(CHEMIN_DB)
    conn.row_factory = sqlite3.Row
    ligne = conn.execute(
        "SELECT * FROM comptes_rendus WHERE id = ? AND user_id = ?", (cr_id, user_id)
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
    reponse = appeler_gemini(
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
    reponse = appeler_gemini(
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
    reponse = appeler_gemini(
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
    reponse = appeler_gemini(
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
    reponse = appeler_gemini(
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


def traiter_audio_complet(chemin_audio, type_reunion, format_souhaite, maj_progression, user_id):
    """Traite un audio de bout en bout. Aiguille vers le flux court (existant) ou
    le flux long (découpage + map-reduce). `maj_progression(phase, courant, total, message)`
    est appelé régulièrement pour suivre l'avancement. Le compte-rendu produit est
    rattaché à `user_id`."""
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
            user_id, type_reunion, format_souhaite, texte_propre, compte_rendu, actions
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
        user_id, type_reunion, format_souhaite, texte_complet, compte_rendu, actions
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
    user_id: int = Depends(utilisateur_courant),
):
    contenu = await fichier.read()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as fichier_temp:
        fichier_temp.write(contenu)
        chemin_temp = fichier_temp.name

    # Quota : vérifié AVANT tout appel Groq/Gemini (coût réel). On ne paie
    # jamais un traitement qu'on va de toute façon refuser.
    duree_min = mesurer_duree_et_verifier_quota(chemin_temp, user_id)

    # Progression neutre : cette route reste synchrone (compatibilité).
    def sans_progression(phase, courant, total, message):
        pass

    try:
        resultat = traiter_audio_complet(
            chemin_temp, type_reunion, format, sans_progression, user_id
        )
        enregistrer_usage(user_id, duree_min)
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


# ======================================================================
#  ROUTES D'AUTHENTIFICATION
# ======================================================================
class IdentifiantsAuth(BaseModel):
    email: EmailStr          # format d'email validé automatiquement
    mot_de_passe: str


class IdentifiantsInscription(IdentifiantsAuth):
    # Acceptation des CGU + politique de confidentialité. Optionnel dans le
    # schéma (défaut False) pour renvoyer une 400 claire plutôt qu'une 422
    # Pydantic si le champ est absent du payload.
    cgu_acceptees: bool = False


@app.post("/inscription")
def inscription(identifiants: IdentifiantsInscription):
    """Crée un compte : hache le mot de passe, enregistre l'utilisateur,
    renvoie un JWT. Refuse si l'email est déjà pris ou si les CGU ne sont pas
    acceptées (contrôle serveur, indépendant du JavaScript du formulaire)."""
    if not identifiants.cgu_acceptees:
        raise HTTPException(
            status_code=400,
            detail="Vous devez accepter les CGU et la politique de confidentialité.",
        )

    email = identifiants.email.lower().strip()

    if len(identifiants.mot_de_passe) < 8:
        raise HTTPException(
            status_code=400,
            detail="Le mot de passe doit contenir au moins 8 caractères.",
        )

    hash_mdp = hacher_mot_de_passe(identifiants.mot_de_passe)
    try:
        user_id = creer_utilisateur(email, hash_mdp)
    except sqlite3.IntegrityError:
        # Contrainte UNIQUE sur email : compte déjà existant.
        raise HTTPException(status_code=409, detail="Un compte existe déjà avec cet email.")

    return {"token": creer_token(user_id), "email": email}


@app.post("/connexion")
def connexion(identifiants: IdentifiantsAuth):
    """Vérifie l'email + le mot de passe et renvoie un JWT si valides."""
    email = identifiants.email.lower().strip()
    utilisateur = get_utilisateur_par_email(email)

    # Message identique que l'email existe ou non : on ne révèle pas quels
    # emails sont enregistrés (évite l'énumération de comptes).
    if utilisateur is None or not verifier_mot_de_passe(
        identifiants.mot_de_passe, utilisateur["mot_de_passe_hash"]
    ):
        raise HTTPException(status_code=401, detail="Email ou mot de passe incorrect.")

    return {"token": creer_token(utilisateur["id"]), "email": email}


class IdentifiantsGoogle(BaseModel):
    credential: str          # jeton d'identité (ID token) fourni par Google


@app.post("/auth/google")
def connexion_google(donnees: IdentifiantsGoogle):
    """Connexion via Google : vérifie le jeton d'identité côté serveur, crée le
    compte si besoin, et renvoie le MÊME type de JWT que l'auth email/mdp."""
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Connexion Google non configurée.")

    try:
        # Vérifie signature, expiration ET audience (= notre Client ID) ;
        # l'émetteur (accounts.google.com) est contrôlé par la bibliothèque.
        infos = google_id_token.verify_oauth2_token(
            donnees.credential, google_requests.Request(), GOOGLE_CLIENT_ID
        )
    except ValueError:
        raise HTTPException(status_code=401, detail="Jeton Google invalide.")

    # On n'accepte que des emails vérifiés par Google.
    if not infos.get("email_verified") or not infos.get("email"):
        raise HTTPException(status_code=401, detail="Email Google non vérifié.")

    email = infos["email"].lower().strip()
    user_id = get_ou_creer_utilisateur_google(email)
    return {"token": creer_token(user_id), "email": email}


# ======================================================================
#  PRÉFÉRENCES UTILISATEUR (valeurs par défaut des comptes-rendus)
# ======================================================================
class Preferences(BaseModel):
    type_reunion: str
    format: str


@app.get("/preferences")
def lire_preferences(user_id: int = Depends(utilisateur_courant)):
    return get_preferences(user_id)


@app.put("/preferences")
def ecrire_preferences(prefs: Preferences, user_id: int = Depends(utilisateur_courant)):
    # On valide contre les options connues pour ne pas stocker n'importe quoi.
    if prefs.type_reunion not in TYPES_REUNION:
        raise HTTPException(status_code=400, detail="Type de réunion invalide.")
    if prefs.format not in FORMATS:
        raise HTTPException(status_code=400, detail="Format invalide.")
    maj_preferences(user_id, prefs.type_reunion, prefs.format)
    return get_preferences(user_id)


# ======================================================================
#  ROUTES ABONNEMENT / PAIEMENT (Stripe)
# ----------------------------------------------------------------------
#  - /abonnement/statut          : plan + consommation du mois (frontend)
#  - /abonnement/creer-session   : lance un Stripe Checkout (souscription)
#  - /abonnement/portail         : Stripe Customer Portal (gérer/annuler)
#  - /abonnement/webhook         : reçoit les événements Stripe (PAS de JWT :
#    authentifié par la signature Stripe, pas par notre Authorization Bearer)
# ======================================================================

@app.get("/abonnement/statut")
def statut_abonnement(user_id: int = Depends(utilisateur_courant)):
    plan = get_plan_utilisateur(user_id)
    quota = PLANS[plan]["minutes_mois"]
    utilisees = minutes_utilisees_ce_mois(user_id)
    return {
        "plan": plan,
        "label": PLANS[plan]["label"],
        "minutes_utilisees": round(utilisees, 1),
        "minutes_quota": quota,
        "minutes_restantes": max(0.0, round(quota - utilisees, 1)),
    }


class SessionCheckoutDemande(BaseModel):
    plan: str  # "pro" ou "business"


@app.post("/abonnement/creer-session")
def creer_session_checkout(
    demande: SessionCheckoutDemande, user_id: int = Depends(utilisateur_courant)
):
    """Crée une session Stripe Checkout (abonnement) et renvoie son URL :
    le frontend redirige simplement vers cette URL."""
    if not stripe.api_key:
        raise HTTPException(status_code=503, detail="Paiement non configuré (clé Stripe manquante).")
    if demande.plan not in PLANS or demande.plan == "gratuit":
        raise HTTPException(status_code=400, detail="Offre invalide.")
    price_id = PLANS[demande.plan]["stripe_price_id"]
    if not price_id:
        raise HTTPException(status_code=503, detail="Cette offre n'est pas encore configurée côté paiement.")

    email, stripe_customer_id = get_infos_facturation(user_id)
    if not stripe_customer_id:
        client = stripe.Customer.create(email=email, metadata={"user_id": str(user_id)})
        stripe_customer_id = client.id
        maj_stripe_customer_id(user_id, stripe_customer_id)

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=stripe_customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{URL_FRONTEND}/parametres.html?abonnement=succes",
        cancel_url=f"{URL_FRONTEND}/tarifs.html?abonnement=annule",
        client_reference_id=str(user_id),
        metadata={"user_id": str(user_id), "plan": demande.plan},
    )
    return {"url": session.url}


@app.post("/abonnement/portail")
def creer_session_portail(user_id: int = Depends(utilisateur_courant)):
    """Crée une session Stripe Customer Portal (gérer moyen de paiement,
    changer d'offre, résilier) et renvoie son URL."""
    if not stripe.api_key:
        raise HTTPException(status_code=503, detail="Paiement non configuré (clé Stripe manquante).")
    _, stripe_customer_id = get_infos_facturation(user_id)
    if not stripe_customer_id:
        raise HTTPException(status_code=400, detail="Aucun abonnement associé à ce compte pour le moment.")

    session = stripe.billing_portal.Session.create(
        customer=stripe_customer_id,
        return_url=f"{URL_FRONTEND}/parametres.html",
    )
    return {"url": session.url}


@app.post("/abonnement/webhook")
async def webhook_stripe(requete: Request):
    """Reçoit les événements Stripe. Authentifié par la signature Stripe
    (en-tête Stripe-Signature + STRIPE_WEBHOOK_SECRET), PAS par un JWT."""
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Webhook Stripe non configuré.")

    payload = await requete.body()
    signature = requete.headers.get("stripe-signature", "")
    try:
        stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="Signature webhook invalide.")

    # La signature est validée : on re-parse le payload en dict/list Python purs.
    # (Les StripeObject renvoyés par construct_event n'exposent pas .get ni un
    #  accès list standard ; json.loads évite ces pièges.)
    evenement = json.loads(payload)
    type_evt = evenement["type"]
    data = evenement["data"]["object"]

    if type_evt == "checkout.session.completed":
        # Premier paiement réussi : on active le plan choisi.
        metadonnees = data.get("metadata") or {}
        if "user_id" in metadonnees and "plan" in metadonnees:
            maj_abonnement(
                int(metadonnees["user_id"]),
                metadonnees["plan"],
                data.get("subscription"),
                "active",
            )

    elif type_evt in ("customer.subscription.updated", "customer.subscription.deleted"):
        # Changement d'offre, renouvellement, échec de paiement, résiliation…
        user_id = get_user_id_par_stripe_customer(data["customer"])
        if user_id is not None:
            statut = data.get("status", "inconnu")
            if type_evt == "customer.subscription.deleted" or statut in (
                "canceled", "unpaid", "incomplete_expired",
            ):
                maj_abonnement(user_id, "gratuit", None, "annule")
            else:
                price_id = data["items"]["data"][0]["price"]["id"]
                nouveau_plan = PRICE_VERS_PLAN.get(price_id, "gratuit")
                maj_abonnement(user_id, nouveau_plan, data["id"], statut)

    return {"recu": True}


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
    reponse = appeler_gemini(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return reponse.text


@app.post("/modifier-compte-rendu")
def modifier_compte_rendu(
    demande: DemandeModification,
    user_id: int = Depends(utilisateur_courant),
):
    # Protégé : appel IA payant, réservé aux utilisateurs connectés.
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
    user_id: int = Depends(utilisateur_courant),
):
    # On enregistre le fichier tout de suite (on ne peut pas lire l'upload dans le thread)
    contenu = await fichier.read()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as fichier_temp:
        fichier_temp.write(contenu)
        chemin_temp = fichier_temp.name

    # Quota : vérifié tout de suite, avant même de créer le job en arrière-plan.
    duree_min = mesurer_duree_et_verifier_quota(chemin_temp, user_id)

    job_id = uuid.uuid4().hex
    travaux[job_id] = {
        "statut": "en_cours",
        "phase": "demarrage",
        "courant": 0,
        "total": 1,
        "pourcentage": 0,
        "message": "Initialisation…",
        "user_id": user_id,   # propriétaire du job (isolation sur /progression)
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
                chemin_temp, type_reunion, format, maj_progression, user_id
            )
            enregistrer_usage(user_id, duree_min)
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
def progression(job_id: str, user_id: int = Depends(utilisateur_courant)):
    travail = travaux.get(job_id)
    if travail is None:
        return {"statut": "inconnu"}

    # Isolation : un utilisateur ne peut suivre que ses propres travaux.
    # On répond « inconnu » (et non 403) pour ne pas révéler l'existence du job.
    if travail.get("user_id") != user_id:
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
def historique(q: str = "", user_id: int = Depends(utilisateur_courant)):
    """Liste les comptes-rendus de l'utilisateur connecté (id, date, type, format).
    Paramètre optionnel `q` : filtre par mot-clé (type, date, contenu)."""
    return lister_comptes_rendus(user_id, q.strip() or None)


@app.get("/compte-rendu/{cr_id}")
def compte_rendu_par_id(cr_id: int, user_id: int = Depends(utilisateur_courant)):
    """Renvoie un compte-rendu complet SEULEMENT s'il appartient à l'utilisateur."""
    compte_rendu = recuperer_compte_rendu(cr_id, user_id)
    if compte_rendu is None:
        raise HTTPException(status_code=404, detail="Compte-rendu introuvable")
    return compte_rendu


# ======================================================================
#  LANCEMENT DIRECT (python main.py)
# ----------------------------------------------------------------------
#  Render fournit le port d'écoute via la variable d'environnement PORT.
#  On écoute sur 0.0.0.0 pour être joignable depuis l'extérieur du conteneur.
#  En local, PORT est absent -> repli sur 8000.
# ======================================================================
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)