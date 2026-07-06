"""
Sauvegarde automatique de la base SQLite de Synthia.

Trois niveaux, tous optionnels et à dégradation propre (sans configuration,
rien ne s'exécute — aucune erreur) :

  1. Snapshot COHÉRENT via l'API `.backup` de SQLite (sûr même pendant que
     l'application écrit — contrairement à une simple copie de fichier).
  2. Copies horodatées sur le DISQUE, à côté de la base (donc sur le disque
     persistant en production), avec rotation (on garde les N plus récentes).
  3. Envoi HORS-SITE vers un stockage objet S3-compatible (Cloudflare R2 ou
     Backblaze B2) — protège d'une perte/corruption du disque.

Activation : uniquement si la variable RENDER est présente (= production) ou
si BACKUP_ACTIF=1 (pour tester en local). En dev normal, rien ne tourne.

Variables d'environnement (voir FIABILITE-DONNEES.md) :
  BACKUP_ACTIF, BACKUP_INTERVALLE_HEURES (défaut 24), BACKUP_RETENTION_LOCALE (7),
  S3_ENDPOINT_URL, S3_BUCKET, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_PREFIXE.
"""
import os
import glob
import time
import sqlite3
import threading
from datetime import datetime

# --- Activation globale ---
BACKUP_ACTIF = bool(os.getenv("RENDER") or os.getenv("BACKUP_ACTIF"))
INTERVALLE_H = float(os.getenv("BACKUP_INTERVALLE_HEURES", "24"))
RETENTION_LOCALE = int(os.getenv("BACKUP_RETENTION_LOCALE", "7"))

# --- Stockage objet S3-compatible (optionnel) ---
S3_ENDPOINT = os.getenv("S3_ENDPOINT_URL", "")
S3_BUCKET = os.getenv("S3_BUCKET", "")
S3_KEY_ID = os.getenv("S3_ACCESS_KEY_ID", "")
S3_SECRET = os.getenv("S3_SECRET_ACCESS_KEY", "")
S3_PREFIXE = os.getenv("S3_PREFIXE", "synthia")
S3_ACTIF = all([S3_ENDPOINT, S3_BUCKET, S3_KEY_ID, S3_SECRET])


def _dossier_backups(chemin_db):
    """Dossier des copies locales, à côté de la base (donc sur le disque)."""
    dossier = os.path.join(os.path.dirname(os.path.abspath(chemin_db)), "backups")
    os.makedirs(dossier, exist_ok=True)
    return dossier


def _snapshot(chemin_db, chemin_sortie):
    """Copie cohérente via l'API `.backup` de SQLite (sûre en cours d'écriture)."""
    source = sqlite3.connect(chemin_db)
    dest = sqlite3.connect(chemin_sortie)
    try:
        with dest:
            source.backup(dest)
    finally:
        dest.close()
        source.close()


def _rotation(dossier, garder):
    """Ne conserve que les `garder` copies les plus récentes."""
    if garder <= 0:
        return
    fichiers = sorted(glob.glob(os.path.join(dossier, "synthia-*.db")))
    for vieux in fichiers[:-garder]:
        try:
            os.remove(vieux)
        except OSError:
            pass


def _client_s3():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_KEY_ID,
        aws_secret_access_key=S3_SECRET,
    )


def _uploader_s3(chemin_fichier, cle):
    _client_s3().upload_file(chemin_fichier, S3_BUCKET, cle)


def sauvegarder_maintenant(chemin_db):
    """Sauvegarde complète : snapshot -> copie locale (rotation) -> upload S3
    si configuré. Renvoie le chemin de la copie locale, ou None si la base
    n'existe pas encore."""
    if not os.path.exists(chemin_db):
        return None
    horodatage = datetime.now().strftime("%Y%m%d-%H%M%S")
    dossier = _dossier_backups(chemin_db)
    cible = os.path.join(dossier, f"synthia-{horodatage}.db")

    _snapshot(chemin_db, cible)
    _rotation(dossier, RETENTION_LOCALE)

    if S3_ACTIF:
        try:
            _uploader_s3(cible, f"{S3_PREFIXE}/synthia-{horodatage}.db")
            # Copie "latest" écrasée à chaque fois : restauration rapide du + récent.
            _uploader_s3(cible, f"{S3_PREFIXE}/synthia-latest.db")
        except Exception as erreur:
            print("[sauvegarde] envoi S3 échoué :", erreur)

    return cible


def _boucle(chemin_db):
    # Première sauvegarde peu après le démarrage, puis à intervalle régulier.
    time.sleep(120)
    while True:
        try:
            chemin = sauvegarder_maintenant(chemin_db)
            print(f"[sauvegarde] OK -> {chemin} | S3 : {'oui' if S3_ACTIF else 'non'}")
        except Exception as erreur:
            print("[sauvegarde] échec :", erreur)
        time.sleep(max(0.1, INTERVALLE_H) * 3600)


def demarrer_sauvegardes(chemin_db):
    """Démarre la sauvegarde planifiée en tâche de fond (si activée)."""
    if not BACKUP_ACTIF:
        return
    threading.Thread(target=_boucle, args=(chemin_db,), daemon=True).start()
    print(
        f"[sauvegarde] planifiée toutes les {INTERVALLE_H} h "
        f"(local : {RETENTION_LOCALE} copies ; "
        f"S3 : {'configuré' if S3_ACTIF else 'NON configuré'})."
    )
