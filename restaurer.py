"""
Restauration d'une sauvegarde Synthia depuis le stockage objet (R2/B2).

  ⚠️ ARRÊTEZ le service backend avant de restaurer (sinon écriture concurrente).

Usage :
  # Restaure la dernière sauvegarde vers l'emplacement de la base (DB_PATH) :
  DB_PATH=/var/data/synthia.db python restaurer.py

  # Restaure une sauvegarde précise :
  DB_PATH=/var/data/synthia.db python restaurer.py synthia/synthia-20260706-030000.db

  # Liste les sauvegardes disponibles sans rien restaurer :
  python restaurer.py --liste

Nécessite les mêmes variables que la sauvegarde : S3_ENDPOINT_URL, S3_BUCKET,
S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY (et éventuellement S3_PREFIXE).
La base actuelle est d'abord copiée en .avant-restauration par sécurité.
"""
import os
import sys
import shutil
from datetime import datetime

import boto3

S3_ENDPOINT = os.getenv("S3_ENDPOINT_URL", "")
S3_BUCKET = os.getenv("S3_BUCKET", "")
S3_KEY_ID = os.getenv("S3_ACCESS_KEY_ID", "")
S3_SECRET = os.getenv("S3_SECRET_ACCESS_KEY", "")
S3_PREFIXE = os.getenv("S3_PREFIXE", "synthia")
CHEMIN_DB = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "synthia.db"))


def client():
    if not all([S3_ENDPOINT, S3_BUCKET, S3_KEY_ID, S3_SECRET]):
        sys.exit("Variables S3 manquantes (S3_ENDPOINT_URL, S3_BUCKET, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY).")
    return boto3.client(
        "s3", endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_KEY_ID, aws_secret_access_key=S3_SECRET,
    )


def lister(c):
    reponse = c.list_objects_v2(Bucket=S3_BUCKET, Prefix=S3_PREFIXE + "/")
    objets = reponse.get("Contents", [])
    if not objets:
        print("Aucune sauvegarde trouvée sous", S3_PREFIXE + "/")
        return
    for o in sorted(objets, key=lambda x: x["LastModified"]):
        print(f"  {o['Key']}   ({o['Size']} octets, {o['LastModified']:%Y-%m-%d %H:%M})")


def main():
    args = sys.argv[1:]
    c = client()

    if args and args[0] == "--liste":
        lister(c)
        return

    cle = args[0] if args else f"{S3_PREFIXE}/synthia-latest.db"

    # Filet de sécurité : on sauvegarde la base actuelle avant d'écraser.
    if os.path.exists(CHEMIN_DB):
        secours = f"{CHEMIN_DB}.avant-restauration-{datetime.now():%Y%m%d-%H%M%S}"
        shutil.copy2(CHEMIN_DB, secours)
        print("Base actuelle sauvegardée sous :", secours)

    os.makedirs(os.path.dirname(os.path.abspath(CHEMIN_DB)), exist_ok=True)
    print(f"Téléchargement de {cle} -> {CHEMIN_DB} …")
    c.download_file(S3_BUCKET, cle, CHEMIN_DB)
    print("Restauration terminée. Redémarrez le service backend.")


if __name__ == "__main__":
    main()
