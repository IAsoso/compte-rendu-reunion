# Fiabilité des données — Synthia

Ce document explique **où vivent les données**, **comment elles sont sauvegardées**,
et **comment restaurer** en cas de problème. Base = fichier SQLite `synthia.db`
(comptes, comptes-rendus, usage/quota, abonnements, préférences, jetons email).

---

## 1. Où vit la base (`DB_PATH`)

Le code choisit le chemin ainsi ([main.py](main.py)) :

```python
CHEMIN_DB = os.getenv("DB_PATH", <dossier de main.py>/synthia.db)
```

| Environnement | `DB_PATH` | Emplacement réel | Persistance |
|---|---|---|---|
| **Local** | non défini | `synthia.db` à côté de `main.py` | fichier local (gitignored) — OK pour le dev |
| **Render SANS disque** | non défini | `/opt/render/project/src/synthia.db` (éphémère) | ❌ **effacé à chaque déploiement ET redémarrage** |
| **Render AVEC disque** | `/var/data/synthia.db` | disque persistant monté | ✅ conservé |

⚠️ **Tant que `DB_PATH` n'est pas défini sur un disque persistant en production,
toutes les données sont perdues à chaque redéploiement ou redémarrage.**

### Mettre en place le disque persistant (dashboard Render — à faire manuellement)

1. Render → **Web Service backend**.
2. Si l'instance est **Free** : **Settings → Instance Type → Starter** (7 $/mois).
   *(Les disques ne sont pas disponibles sur le plan gratuit.)*
3. Onglet **Disks → Add Disk** : Name `synthia-data`, **Mount Path `/var/data`**, Size `1` Go.
4. Onglet **Environment** → ajouter **`DB_PATH = /var/data/synthia.db`** → Save (redéploie).

**Coût** : Starter 7 $/mois + disque 0,25 $/Go/mois (≈ 0,25 $ pour 1 Go).

> Vérifié en local : un compte créé avant un redémarrage du process est toujours
> présent après, dès lors que `DB_PATH` pointe vers un emplacement stable —
> ce que garantit le disque persistant.

**Note** : en local, ne rien changer — le défaut (`synthia.db` à côté de `main.py`)
convient. `DB_PATH` ne sert qu'en production.

---

## 2. Sauvegardes automatiques ([sauvegarde.py](sauvegarde.py))

Une tâche de fond (démarrée avec l'app) réalise, **toutes les 24 h par défaut** :

1. un **snapshot cohérent** de la base via l'API `.backup` de SQLite (sûr même
   pendant que l'app écrit) ;
2. une **copie horodatée sur le disque**, dans `<dossier de la base>/backups/`
   (donc `/var/data/backups/` en prod), avec **rotation** (7 copies gardées) ;
3. un **envoi hors-site** vers un stockage objet S3-compatible (Cloudflare R2 ou
   Backblaze B2) : clés `synthia/synthia-<horodatage>.db` **et**
   `synthia/synthia-latest.db` (écrasée à chaque fois → restauration rapide).

Le niveau 3 protège d'une **perte/corruption du disque** ; les niveaux 1-2 d'une
suppression applicative accidentelle. Sans configuration, **rien ne tourne**
(aucune erreur) — la sauvegarde ne s'active qu'en production (`RENDER` présent)
ou en local avec `BACKUP_ACTIF=1`.

### Variables d'environnement (à définir sur Render → Environment)

| Variable | Rôle | Exemple |
|---|---|---|
| `S3_ENDPOINT_URL` | endpoint S3 du fournisseur | R2 : `https://<accountid>.r2.cloudflarestorage.com` |
| `S3_BUCKET` | nom du bucket | `synthia-backups` |
| `S3_ACCESS_KEY_ID` | clé d'accès | *(fournie par R2/B2)* |
| `S3_SECRET_ACCESS_KEY` | clé secrète | *(fournie par R2/B2)* |
| `S3_PREFIXE` | (optionnel) préfixe des clés | `synthia` |
| `BACKUP_INTERVALLE_HEURES` | (optionnel) fréquence | `24` |
| `BACKUP_RETENTION_LOCALE` | (optionnel) copies locales gardées | `7` |
| `BACKUP_ACTIF` | (local uniquement) forcer l'activation | `1` |

`RENDER` est posée automatiquement par Render → les sauvegardes locales (disque)
tournent dès qu'il y a un disque ; l'envoi S3 s'active en plus dès que les 4
variables `S3_*` sont renseignées.

### Créer le stockage objet (recommandé : Cloudflare R2, gratuit jusqu'à 10 Go)

1. Compte Cloudflare → **R2** → **Create bucket** (ex. `synthia-backups`).
2. **R2 → Manage R2 API Tokens → Create API Token** (permission *Object Read & Write*).
3. Récupérer *Access Key ID*, *Secret Access Key*, et l'*endpoint*
   `https://<accountid>.r2.cloudflarestorage.com`.
4. Renseigner les variables `S3_*` sur Render.
5. (Backblaze B2 fonctionne pareil : bucket + clé applicative + endpoint S3
   `https://s3.<region>.backblazeb2.com`.)

---

## 3. Restaurer une sauvegarde ([restaurer.py](restaurer.py))

En cas de pépin (base corrompue, données perdues) :

1. **Arrêter le service backend** (Render → Suspend, ou pendant une fenêtre de maintenance).
2. Depuis un shell ayant les variables `S3_*` et `DB_PATH` :

```bash
# Lister les sauvegardes disponibles
python restaurer.py --liste

# Restaurer la plus récente vers DB_PATH
DB_PATH=/var/data/synthia.db python restaurer.py

# ou une sauvegarde précise
DB_PATH=/var/data/synthia.db python restaurer.py synthia/synthia-20260706-030000.db
```

Le script **copie d'abord la base actuelle** en `synthia.db.avant-restauration-<horodatage>`
(filet de sécurité), puis télécharge la sauvegarde choisie vers `DB_PATH`.

3. **Redémarrer** le service.

> Sur Render, le plus simple pour exécuter `restaurer.py` est un **Shell** sur le
> service (onglet *Shell*), qui a déjà accès au disque `/var/data` et aux variables
> d'environnement. Alternative locale : copier la sauvegarde et la re-téléverser
> via le dashboard, ou restaurer une copie locale de `/var/data/backups/`.

### En cas d'urgence sans script
Une sauvegarde est un simple fichier SQLite. On peut la remettre en place « à la
main » : arrêter le service, remplacer `/var/data/synthia.db` par la copie voulue
(depuis `/var/data/backups/` ou téléchargée du bucket), redémarrer.

---

## 4. Récapitulatif « que faire si… »

| Problème | Action |
|---|---|
| Données effacées après un déploiement | Le disque persistant n'est pas monté ou `DB_PATH` n'y pointe pas → refaire §1. |
| Base corrompue | Restaurer la dernière sauvegarde saine → §3. |
| Perte du disque Render | Restaurer depuis le stockage objet (`synthia-latest.db`) → §3. |
| Vérifier que les sauvegardes tournent | Logs du service : lignes `[sauvegarde] OK -> …`. |
