// ======================================================================
//  Service worker Synthia — PWA installable, cache du "shell" statique.
// ----------------------------------------------------------------------
//  Stratégie volontairement simple et sûre :
//  - Les fichiers statiques (HTML/CSS/JS/icônes du même domaine) sont
//    servis "réseau d'abord, cache en secours" : toujours frais quand on
//    est en ligne, disponibles hors-ligne en dernier recours.
//  - Les appels API (autre domaine : backend Render/local) ne sont JAMAIS
//    interceptés ni mis en cache — aucun risque de servir des données
//    d'un autre utilisateur ou périmées.
//  Incrémenter VERSION invalide l'ancien cache au déploiement suivant.
// ======================================================================
const VERSION = "synthia-v1";

const SHELL = [
  "index.html", "app.html", "historique.html", "resultat.html",
  "parametres.html", "tarifs.html", "connexion.html", "inscription.html",
  "style.css", "config.js", "auth.js", "nav.js",
  "favicon.svg", "icone-192.png", "icone-512.png", "manifest.json",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((cles) => Promise.all(cles.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // Uniquement les GET statiques du même domaine ; l'API n'est pas touchée.
  if (e.request.method !== "GET" || url.origin !== self.location.origin) return;

  e.respondWith(
    // no-cache : revalide auprès du serveur (304 léger) au lieu de faire
    // confiance au cache HTTP du disque — évite de servir du JS périmé
    // après un déploiement.
    fetch(e.request, { cache: "no-cache" })
      .then((reponse) => {
        const copie = reponse.clone();
        caches.open(VERSION).then((c) => c.put(e.request, copie));
        return reponse;
      })
      .catch(() => caches.match(e.request, { ignoreSearch: true }))
  );
});
