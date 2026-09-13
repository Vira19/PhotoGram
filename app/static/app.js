/* PhotoGram — glue client minimale.
 *
 * Le serveur rend deja tout le HTML ; ce fichier ne fait que deux choses :
 * rafraichir des fragments pendant qu'une reconstruction tourne, et remplacer
 * une carte photo apres un clic. Pas de framework : sur un RPi3 servant un
 * navigateur distant, chaque kilo-octet economise est un aller-retour de moins.
 */
(function () {
  "use strict";

  /** Recharge periodiquement un fragment HTML rendu par le serveur.
   *  <div data-poll="/jobs/1/etat" data-poll-interval="3000"> */
  function installerFragments() {
    document.querySelectorAll("[data-poll]").forEach(function (cible) {
      var url = cible.getAttribute("data-poll");
      var delai = parseInt(cible.getAttribute("data-poll-interval") || "3000", 10);

      var timer = setInterval(function () {
        if (document.hidden) return; // onglet en arriere-plan : on epargne le RPi
        fetch(url, { headers: { "X-Requested-With": "fetch" } })
          .then(function (r) {
            if (r.status === 401) { window.location.href = "/login"; return null; }
            return r.ok ? r.text() : null;
          })
          .then(function (html) {
            if (html === null) return;
            cible.innerHTML = html;
            // Le fragment annonce lui-meme la fin du job : on cesse alors de
            // solliciter le serveur plutot que de scruter indefiniment.
            if (cible.querySelector("[data-termine]")) {
              clearInterval(timer);
              setTimeout(function () { window.location.reload(); }, 600);
            }
          })
          .catch(function () { /* coupure reseau : on retentera au tour suivant */ });
      }, delai);
    });
  }

  /** Recharge un journal texte et suit le bas si l'utilisateur y etait deja.
   *  <pre data-journal="/jobs/1/journal" data-poll-interval="4000"> */
  function installerJournaux() {
    document.querySelectorAll("[data-journal]").forEach(function (cible) {
      var url = cible.getAttribute("data-journal");
      var delai = parseInt(cible.getAttribute("data-poll-interval") || "4000", 10);

      function rafraichir() {
        if (document.hidden) return;
        var colleEnBas = cible.scrollHeight - cible.scrollTop - cible.clientHeight < 40;
        fetch(url)
          .then(function (r) { return r.ok ? r.text() : null; })
          .then(function (texte) {
            if (texte === null || texte === cible.textContent) return;
            cible.textContent = texte;
            if (colleEnBas) cible.scrollTop = cible.scrollHeight;
          })
          .catch(function () {});
      }

      cible.scrollTop = cible.scrollHeight;
      if (cible.hasAttribute("data-journal-actif")) setInterval(rafraichir, delai);
    });
  }

  /** POST qui remplace un element par le HTML renvoye.
   *  <button data-post="/photos/3/toggle" data-remplace="#photo-3"> */
  function installerActions() {
    document.addEventListener("click", function (evenement) {
      var bouton = evenement.target.closest("[data-post]");
      if (!bouton) return;
      evenement.preventDefault();

      var cible = document.querySelector(bouton.getAttribute("data-remplace"));
      if (!cible) return;

      bouton.disabled = true;
      fetch(bouton.getAttribute("data-post"), {
        method: "POST",
        headers: { "X-Requested-With": "fetch" },
      })
        .then(function (r) {
          if (r.status === 401) { window.location.href = "/login"; return null; }
          return r.ok ? r.text() : null;
        })
        .then(function (html) {
          if (html === null) { bouton.disabled = false; return; }
          cible.outerHTML = html;
          majCompteurRetenues();
        })
        .catch(function () { bouton.disabled = false; });
    });
  }

  /** Tient a jour le compteur « photos retenues » sans recharger la page. */
  function majCompteurRetenues() {
    var compteur = document.querySelector("[data-compteur-retenues]");
    if (!compteur) return;
    var retenues = document.querySelectorAll(".vignette:not(.ecartee)").length;
    compteur.textContent = retenues;
  }

  /** Demande confirmation avant les actions destructrices. */
  function installerConfirmations() {
    document.addEventListener("submit", function (evenement) {
      var message = evenement.target.getAttribute("data-confirmer");
      if (message && !window.confirm(message)) evenement.preventDefault();
    });
  }

  /** Affiche le nombre de fichiers choisis avant l'envoi. */
  function installerApercuFichiers() {
    var champ = document.querySelector("input[type=file][data-apercu]");
    if (!champ) return;
    var sortie = document.querySelector(champ.getAttribute("data-apercu"));
    champ.addEventListener("change", function () {
      var n = champ.files.length;
      sortie.textContent = n ? n + " fichier(s) selectionne(s)." : "";
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    installerFragments();
    installerJournaux();
    installerActions();
    installerConfirmations();
    installerApercuFichiers();
  });
})();
