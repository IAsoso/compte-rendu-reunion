// ======================================================================
//  Configuration du frontend Synthia
// ----------------------------------------------------------------------
//  L'URL du backend est choisie AUTOMATIQUEMENT selon l'endroit où la page
//  est ouverte : plus besoin de modifier ce fichier à chaque aller-retour
//  entre développement local et production.
//
//    - Ouvert depuis localhost / 127.0.0.1  ->  backend local
//    - Ouvert par double-clic (fichier file://)  ->  backend local
//    - Ouvert depuis n'importe quel autre domaine (Render)  ->  backend prod
//
//  Si un jour vous changez l'URL du backend de production, il n'y a qu'UNE
//  seule ligne à ajuster : URL_PROD ci-dessous.
// ======================================================================
const URL_LOCALE = "http://127.0.0.1:8000";
const URL_PROD = "https://compte-rendu-reunionsynthia-api.onrender.com";

// Développement local si servi depuis localhost/127.0.0.1, ou ouvert
// directement depuis le disque (protocole file://, hostname vide).
const hoteLocal =
  ["127.0.0.1", "localhost"].includes(window.location.hostname) ||
  window.location.protocol === "file:";

window.API_BASE_URL = hoteLocal ? URL_LOCALE : URL_PROD;

// --- Thème : appliqué ICI (config.js est chargé en tout premier dans le
// <head>) pour éviter un flash clair/sombre au chargement. Réglage dans
// Paramètres : "auto" (par défaut, suit le système), "clair" ou "sombre".
(function () {
  const theme = localStorage.getItem("synthia_theme");
  if (theme === "sombre" || theme === "clair") {
    document.documentElement.classList.add(theme);
  }
})();

// Client ID OAuth Google (public, pas un secret) — pour le bouton « Se
// connecter avec Google ». Doit correspondre au GOOGLE_CLIENT_ID du backend.
window.GOOGLE_CLIENT_ID = "896098783552-0d1l3r5htlps8qift4enph9uociif381.apps.googleusercontent.com";
