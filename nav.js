// ======================================================================
//  Navigation et pied de page partagés — SOURCE UNIQUE pour tout le site.
// ----------------------------------------------------------------------
//  Chaque page contient simplement :
//      <header data-nav></header>       (rempli ici)
//      <footer class="pied" data-pied></footer>
//  et charge config.js puis nav.js. Fini les menus dupliqués/incohérents.
//
//  - Déconnecté : Tarifs · Se connecter · CTA "Commencer".
//  - Connecté   : Nouvelle réunion · Historique · Tarifs · menu compte
//    (avatar avec initiale, email, badge du plan, Paramètres, Déconnexion).
//  L'email est affiché via textContent (jamais innerHTML) : pas d'injection.
// ======================================================================
(function () {
  const CLE_TOKEN = "synthia_token";
  const CLE_EMAIL = "synthia_email";

  const connecte = !!localStorage.getItem(CLE_TOKEN);
  const email = localStorage.getItem(CLE_EMAIL) || "";
  const pageCourante = (location.pathname.split("/").pop() || "index.html");

  function seDeconnecter() {
    localStorage.removeItem(CLE_TOKEN);
    localStorage.removeItem(CLE_EMAIL);
    sessionStorage.clear();
    window.location.href = "connexion.html";
  }

  // --- Élément lien avec marquage de la page active ---
  function lien(href, texte, classe) {
    const a = document.createElement("a");
    a.href = href;
    a.textContent = texte;
    a.className = classe || "nav-lien";
    if (href === pageCourante) a.setAttribute("aria-current", "page");
    return a;
  }

  function construireHeader(header) {
    header.innerHTML = "";

    // Logo (identique à l'existant)
    const logoLien = document.createElement("a");
    logoLien.href = "index.html";
    logoLien.className = "logo-lien";
    logoLien.innerHTML =
      '<div class="logo">S</div><div><h1>Synthia</h1>' +
      '<div class="tagline">Vos réunions, résumées par l\'IA</div></div>';
    header.appendChild(logoLien);

    const nav = document.createElement("nav");
    nav.className = "nav-liens nav-unifiee";

    if (!connecte) {
      nav.appendChild(lien("tarifs.html", "Tarifs"));
      nav.appendChild(lien("connexion.html", "Se connecter"));
      nav.appendChild(lien("inscription.html", "Commencer →", "cta nav-cta"));
    } else {
      nav.appendChild(lien("app.html", "Nouvelle réunion"));
      nav.appendChild(lien("historique.html", "Historique"));
      nav.appendChild(lien("tarifs.html", "Tarifs"));
      nav.appendChild(construireMenuCompte());
    }

    header.appendChild(nav);
  }

  // --- Menu compte : avatar + dropdown ---
  function construireMenuCompte() {
    const conteneur = document.createElement("div");
    conteneur.className = "menu-compte";

    const bouton = document.createElement("button");
    bouton.type = "button";
    bouton.className = "avatar-bouton";
    bouton.setAttribute("aria-haspopup", "menu");
    bouton.setAttribute("aria-expanded", "false");
    bouton.setAttribute("aria-label", "Menu du compte");
    bouton.textContent = (email.charAt(0) || "•").toUpperCase();

    const panneau = document.createElement("div");
    panneau.className = "menu-compte-panneau cache";
    panneau.setAttribute("role", "menu");

    // En-tête du panneau : email + badge plan (chargé après coup)
    const entete = document.createElement("div");
    entete.className = "menu-compte-entete";
    const spanEmail = document.createElement("div");
    spanEmail.className = "menu-compte-email";
    spanEmail.textContent = email || "Mon compte";
    spanEmail.title = email;
    const badgePlan = document.createElement("span");
    badgePlan.className = "badge-plan";
    badgePlan.textContent = "…";
    entete.appendChild(spanEmail);
    entete.appendChild(badgePlan);
    panneau.appendChild(entete);

    // Liens du menu
    panneau.appendChild(lien("parametres.html", "Paramètres", "menu-compte-lien"));
    panneau.appendChild(lien("historique.html", "Historique", "menu-compte-lien"));
    panneau.appendChild(lien("tarifs.html", "Changer d'offre", "menu-compte-lien"));

    const btnDeco = document.createElement("button");
    btnDeco.type = "button";
    btnDeco.className = "menu-compte-lien menu-compte-deco";
    btnDeco.textContent = "Se déconnecter";
    btnDeco.onclick = seDeconnecter;
    panneau.appendChild(btnDeco);

    // Ouverture/fermeture
    bouton.onclick = (e) => {
      e.stopPropagation();
      const ouvert = !panneau.classList.contains("cache");
      panneau.classList.toggle("cache");
      bouton.setAttribute("aria-expanded", String(!ouvert));
    };
    document.addEventListener("click", () => {
      panneau.classList.add("cache");
      bouton.setAttribute("aria-expanded", "false");
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") panneau.classList.add("cache");
    });
    panneau.onclick = (e) => e.stopPropagation();

    conteneur.appendChild(bouton);
    conteneur.appendChild(panneau);

    // Badge du plan : silencieux si l'API est injoignable ou non configurée.
    if (window.API_BASE_URL) {
      fetch(window.API_BASE_URL + "/abonnement/statut", {
        headers: { Authorization: "Bearer " + localStorage.getItem(CLE_TOKEN) },
      })
        .then((r) => (r.ok ? r.json() : null))
        .then((s) => {
          if (!s) { badgePlan.remove(); return; }
          badgePlan.textContent = s.label;
          badgePlan.classList.add("plan-" + s.plan);
        })
        .catch(() => badgePlan.remove());
    } else {
      badgePlan.remove();
    }

    return conteneur;
  }

  function construireFooter(footer) {
    footer.innerHTML =
      "<p>Synthia — Comptes-rendus de réunion par IA</p>" +
      '<p class="pied-liens">' +
      '<a href="mentions-legales.html">Mentions légales</a> · ' +
      '<a href="cgu.html">CGU</a> · ' +
      '<a href="confidentialite.html">Confidentialité</a></p>';
  }

  function initialiser() {
    document.querySelectorAll("header[data-nav]").forEach(construireHeader);
    document.querySelectorAll("footer[data-pied]").forEach(construireFooter);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialiser);
  } else {
    initialiser();
  }

  // --- PWA : enregistrement du service worker (une seule fois, partout) ---
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("sw.js").catch(() => {
      /* hors-ligne non critique : l'app fonctionne sans */
    });
  }
})();
