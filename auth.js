// ======================================================================
//  Authentification côté client (jeton JWT)
// ----------------------------------------------------------------------
//  ⚠️ AVERTISSEMENT SÉCURITÉ — stockage du jeton dans localStorage :
//  localStorage est lisible par TOUT JavaScript qui s'exécute sur la page.
//  En cas de faille XSS (injection de script), le jeton pourrait être volé.
//  C'est un compromis accepté pour cette première mise en ligne (démo).
//  Alternative plus sûre pour plus tard : cookie httpOnly (illisible par JS)
//  + protection CSRF. Voir les explications fournies avec cette étape.
//
//  Dépend de config.js (window.API_BASE_URL), à charger AVANT ce fichier.
// ======================================================================
const SYNTHIA_CLE_TOKEN = "synthia_token";

function getToken() {
  return localStorage.getItem(SYNTHIA_CLE_TOKEN);
}

function setToken(token) {
  localStorage.setItem(SYNTHIA_CLE_TOKEN, token);
}

function deconnexion() {
  localStorage.removeItem(SYNTHIA_CLE_TOKEN);
  window.location.href = "connexion.html";
}

// À appeler en haut d'une page protégée : redirige vers la connexion si
// aucun jeton n'est présent. Renvoie false dans ce cas (page interrompue).
function exigerAuth() {
  if (!getToken()) {
    window.location.href = "connexion.html";
    return false;
  }
  return true;
}

// Construit les en-têtes d'une requête protégée (ajoute le jeton Bearer).
function enTetesAuth(entetesSupplementaires = {}) {
  return Object.assign(
    { Authorization: "Bearer " + getToken() },
    entetesSupplementaires
  );
}

// Wrapper de fetch pour les routes protégées : joint le jeton et gère le cas
// 401 (jeton absent/invalide/expiré) en déconnectant et redirigeant.
async function fetchAuth(url, options = {}) {
  options.headers = enTetesAuth(options.headers || {});
  const reponse = await fetch(url, options);
  if (reponse.status === 401) {
    deconnexion();
    throw new Error("Session expirée — reconnexion nécessaire.");
  }
  return reponse;
}

// ----------------------------------------------------------------------
//  Connexion Google (Google Identity Services)
// ----------------------------------------------------------------------
// Callback appelé par Google avec le jeton d'identité. On l'envoie au backend
// qui le vérifie et renvoie NOTRE JWT (même flux que l'auth email/mdp).
async function gererReponseGoogle(reponse) {
  const message = document.getElementById("message");
  try {
    const r = await fetch(window.API_BASE_URL + "/auth/google", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ credential: reponse.credential }),
    });
    const donnees = await r.json();
    if (!r.ok) throw new Error(donnees.detail || "Connexion Google impossible.");
    setToken(donnees.token);
    window.location.href = "app.html";
  } catch (erreur) {
    if (message) {
      message.className = "auth-message erreur";
      message.textContent = erreur.message || "Serveur injoignable.";
    }
  }
}

// Initialise et affiche le bouton Google (appelé au chargement de la lib GSI).
function initialiserBoutonGoogle() {
  if (!window.google || !window.GOOGLE_CLIENT_ID) return;
  google.accounts.id.initialize({
    client_id: window.GOOGLE_CLIENT_ID,
    callback: gererReponseGoogle,
  });
  const cible = document.getElementById("btnGoogle");
  if (cible) {
    google.accounts.id.renderButton(cible, {
      theme: "outline",
      size: "large",
      text: "continue_with",
      width: 352,
      locale: "fr",
    });
  }
}
