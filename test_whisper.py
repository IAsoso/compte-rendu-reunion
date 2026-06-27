import whisper

# On charge le modèle. "base" = une version légère, bon compromis vitesse/qualité.
# (la 1re fois, ça télécharge le modèle, sois patient)
print("Chargement du modèle Whisper...")
modele = whisper.load_model("base")

# On transcrit un fichier audio.
# Remplace "test.webm" par le nom de ton fichier audio de test.
print("Transcription en cours...")
resultat = modele.transcribe("test.m4a", language="fr")

# On affiche le texte obtenu
print("\n--- TEXTE TRANSCRIT ---")
print(resultat["text"])