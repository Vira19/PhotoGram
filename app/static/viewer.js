/* PhotoGram — visionneuse 3D autonome (WebGL brut, sans bibliotheque).
 *
 * Le site doit rester utilisable sur un LAN sans acces Internet : charger
 * three.js depuis un CDN casserait la page hors ligne, et l'embarquer pesait
 * plus lourd que ce fichier. On ne couvre donc que ce que le pipeline produit :
 * nuages de points PLY (colores) et maillages OBJ (ombrage a plat).
 *
 * Les textures ne sont pas rendues : pour un .obj texture, MeshLab ou
 * CloudCompare restent les bons outils.
 */
(function () {
  "use strict";

  // ---------------------------------------------------------------- matrices

  function identite() {
    return new Float32Array([1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]);
  }

  function multiplier(a, b) {
    var out = new Float32Array(16);
    for (var i = 0; i < 4; i++) {
      for (var j = 0; j < 4; j++) {
        var somme = 0;
        for (var k = 0; k < 4; k++) somme += a[k * 4 + j] * b[i * 4 + k];
        out[i * 4 + j] = somme;
      }
    }
    return out;
  }

  function perspective(fovRad, aspect, proche, loin) {
    var f = 1 / Math.tan(fovRad / 2);
    var out = new Float32Array(16);
    out[0] = f / aspect; out[5] = f;
    out[10] = (loin + proche) / (proche - loin); out[11] = -1;
    out[14] = (2 * loin * proche) / (proche - loin);
    return out;
  }

  function normaliser(v) {
    var n = Math.hypot(v[0], v[1], v[2]) || 1;
    return [v[0] / n, v[1] / n, v[2] / n];
  }

  function produitVectoriel(a, b) {
    return [a[1]*b[2] - a[2]*b[1], a[2]*b[0] - a[0]*b[2], a[0]*b[1] - a[1]*b[0]];
  }

  function regarder(oeil, cible, haut) {
    var z = normaliser([oeil[0]-cible[0], oeil[1]-cible[1], oeil[2]-cible[2]]);
    var x = normaliser(produitVectoriel(haut, z));
    var y = produitVectoriel(z, x);
    return new Float32Array([
      x[0], y[0], z[0], 0,
      x[1], y[1], z[1], 0,
      x[2], y[2], z[2], 0,
      -(x[0]*oeil[0] + x[1]*oeil[1] + x[2]*oeil[2]),
      -(y[0]*oeil[0] + y[1]*oeil[1] + y[2]*oeil[2]),
      -(z[0]*oeil[0] + z[1]*oeil[1] + z[2]*oeil[2]), 1,
    ]);
  }

  // ------------------------------------------------------------- lecture PLY

  var TAILLES = {
    char: 1, uchar: 1, int8: 1, uint8: 1,
    short: 2, ushort: 2, int16: 2, uint16: 2,
    int: 4, uint: 4, int32: 4, uint32: 4, float: 4, float32: 4,
    double: 8, float64: 8,
  };

  function lireScalaire(vue, offset, type, petitBoutien) {
    switch (type) {
      case "char": case "int8":     return vue.getInt8(offset);
      case "uchar": case "uint8":   return vue.getUint8(offset);
      case "short": case "int16":   return vue.getInt16(offset, petitBoutien);
      case "ushort": case "uint16": return vue.getUint16(offset, petitBoutien);
      case "int": case "int32":     return vue.getInt32(offset, petitBoutien);
      case "uint": case "uint32":   return vue.getUint32(offset, petitBoutien);
      case "float": case "float32": return vue.getFloat32(offset, petitBoutien);
      case "double": case "float64":return vue.getFloat64(offset, petitBoutien);
      default: throw new Error("Type PLY inconnu : " + type);
    }
  }

  function analyserEntetePly(tampon) {
    // L'entete est en ASCII ; on ne decode que le premier bloc pour ne pas
    // convertir en texte plusieurs dizaines de Mio de donnees binaires.
    var octets = new Uint8Array(tampon, 0, Math.min(tampon.byteLength, 65536));
    var texte = "";
    for (var i = 0; i < octets.length; i++) texte += String.fromCharCode(octets[i]);
    var fin = texte.indexOf("end_header");
    if (fin < 0) throw new Error("Entete PLY introuvable.");
    var finLigne = texte.indexOf("\n", fin);

    var lignes = texte.slice(0, fin).split(/\r?\n/);
    var entete = { format: "ascii", elements: [], debutDonnees: finLigne + 1 };
    var courant = null;

    lignes.forEach(function (ligne) {
      var mots = ligne.trim().split(/\s+/);
      if (mots[0] === "format") {
        entete.format = mots[1];
      } else if (mots[0] === "element") {
        courant = { nom: mots[1], nombre: parseInt(mots[2], 10), proprietes: [] };
        entete.elements.push(courant);
      } else if (mots[0] === "property" && courant) {
        if (mots[1] === "list") {
          courant.proprietes.push({ liste: true, typeCompte: mots[2], typeValeur: mots[3], nom: mots[4] });
        } else {
          courant.proprietes.push({ liste: false, type: mots[1], nom: mots[2] });
        }
      }
    });
    return entete;
  }

  function lirePly(tampon) {
    var entete = analyserEntetePly(tampon);
    var elementSommets = entete.elements.filter(function (e) { return e.nom === "vertex"; })[0];
    if (!elementSommets) throw new Error("Ce PLY ne contient aucun sommet.");

    var nombre = elementSommets.nombre;
    if (!nombre) {
      // Une commande peut reussir et n'ecrire qu'un en-tete. Le dire
      // explicitement evite de chercher la panne du cote de l'affichage.
      throw new Error(
        "Ce fichier declare 0 sommet : la reconstruction s'est terminee sans rien " +
        "trouver a reconstruire. Voyez le journal du job."
      );
    }
    var positions = new Float32Array(nombre * 3);
    var couleurs = new Float32Array(nombre * 3);
    var aDesCouleurs = elementSommets.proprietes.some(function (p) { return p.nom === "red"; });
    var indices = [];

    if (entete.format === "ascii") {
      var texte = new TextDecoder("utf-8").decode(new Uint8Array(tampon, entete.debutDonnees));
      var lignes = texte.split(/\r?\n/);
      var noms = elementSommets.proprietes.map(function (p) { return p.nom; });
      var ligne = 0;
      for (var s = 0; s < nombre; s++) {
        while (ligne < lignes.length && !lignes[ligne].trim()) ligne++;
        var valeurs = lignes[ligne++].trim().split(/\s+/).map(Number);
        remplirSommet(positions, couleurs, s, noms, valeurs, aDesCouleurs);
      }
      // Les faces d'un PLY ascii sont lues dans la foulee.
      var elementFaces = entete.elements.filter(function (e) { return e.nom === "face"; })[0];
      if (elementFaces) {
        for (var f = 0; f < elementFaces.nombre; f++) {
          while (ligne < lignes.length && !lignes[ligne].trim()) ligne++;
          if (ligne >= lignes.length) break;
          var parts = lignes[ligne++].trim().split(/\s+/).map(Number);
          trianguler(indices, parts.slice(1, parts[0] + 1));
        }
      }
    } else {
      var petitBoutien = entete.format !== "binary_big_endian";
      var vue = new DataView(tampon);
      var offset = entete.debutDonnees;

      // Les offsets de chaque champ sont calcules une fois pour toutes : un
      // nuage dense compte des millions de sommets, et allouer un objet par
      // sommet suffisait a figer l'onglet. On ne lit ensuite que les champs
      // reellement utilises, en ignorant les normales et autres attributs.
      var pas = 0;
      var champs = {};
      elementSommets.proprietes.forEach(function (prop) {
        champs[prop.nom] = { offset: pas, type: prop.type };
        pas += TAILLES[prop.type];
      });

      var cx = champs.x, cy = champs.y, cz = champs.z;
      if (!cx || !cy || !cz) throw new Error("Ce PLY n'a pas de coordonnees x/y/z.");
      var cr = champs.red, cv = champs.green, cb = champs.blue;

      for (var v = 0; v < nombre; v++) {
        var base = offset + v * pas;
        positions[v * 3] = lireScalaire(vue, base + cx.offset, cx.type, petitBoutien);
        positions[v * 3 + 1] = lireScalaire(vue, base + cy.offset, cy.type, petitBoutien);
        positions[v * 3 + 2] = lireScalaire(vue, base + cz.offset, cz.type, petitBoutien);
        if (aDesCouleurs && cr && cv && cb) {
          couleurs[v * 3] = lireScalaire(vue, base + cr.offset, cr.type, petitBoutien) / 255;
          couleurs[v * 3 + 1] = lireScalaire(vue, base + cv.offset, cv.type, petitBoutien) / 255;
          couleurs[v * 3 + 2] = lireScalaire(vue, base + cb.offset, cb.type, petitBoutien) / 255;
        }
      }
      offset += nombre * pas;

      var faces = entete.elements.filter(function (e) { return e.nom === "face"; })[0];
      if (faces && faces.nombre) {
        for (var k = 0; k < faces.nombre; k++) {
          var prop = faces.proprietes[0];
          var compte = lireScalaire(vue, offset, prop.typeCompte, petitBoutien);
          offset += TAILLES[prop.typeCompte];
          var sommets = [];
          for (var c = 0; c < compte; c++) {
            sommets.push(lireScalaire(vue, offset, prop.typeValeur, petitBoutien));
            offset += TAILLES[prop.typeValeur];
          }
          trianguler(indices, sommets);
        }
      }
    }

    if (!aDesCouleurs) couleurs.fill(0.78);
    return { positions: positions, couleurs: couleurs, indices: indices };
  }

  function remplirSommet(positions, couleurs, index, noms, valeurs, aDesCouleurs) {
    for (var i = 0; i < noms.length; i++) {
      var valeur = valeurs[i];
      if (noms[i] === "x") positions[index * 3] = valeur;
      else if (noms[i] === "y") positions[index * 3 + 1] = valeur;
      else if (noms[i] === "z") positions[index * 3 + 2] = valeur;
      else if (aDesCouleurs && noms[i] === "red") couleurs[index * 3] = valeur / 255;
      else if (aDesCouleurs && noms[i] === "green") couleurs[index * 3 + 1] = valeur / 255;
      else if (aDesCouleurs && noms[i] === "blue") couleurs[index * 3 + 2] = valeur / 255;
    }
  }

  function trianguler(indices, sommets) {
    for (var i = 1; i + 1 < sommets.length; i++) {
      indices.push(sommets[0], sommets[i], sommets[i + 1]);
    }
  }

  // ------------------------------------------------------------- lecture OBJ

  function lireObj(texte) {
    var positions = [];
    var indices = [];
    var lignes = texte.split(/\r?\n/);

    for (var i = 0; i < lignes.length; i++) {
      var ligne = lignes[i];
      if (ligne.charCodeAt(0) === 118 && ligne.charCodeAt(1) === 32) { // "v "
        var coords = ligne.split(/\s+/);
        positions.push(+coords[1], +coords[2], +coords[3]);
      } else if (ligne.charCodeAt(0) === 102 && ligne.charCodeAt(1) === 32) { // "f "
        var mots = ligne.trim().split(/\s+/);
        var face = [];
        for (var m = 1; m < mots.length; m++) {
          var indice = parseInt(mots[m].split("/")[0], 10);
          // Un indice negatif compte a rebours depuis le dernier sommet lu.
          face.push(indice < 0 ? positions.length / 3 + indice : indice - 1);
        }
        trianguler(indices, face);
      }
    }

    var tableau = new Float32Array(positions);
    var couleurs = new Float32Array(tableau.length);
    couleurs.fill(0.78);
    return { positions: tableau, couleurs: couleurs, indices: indices };
  }

  /** Normales par sommet, moyennees sur les faces adjacentes. */
  function calculerNormales(positions, indices) {
    var normales = new Float32Array(positions.length);
    for (var i = 0; i < indices.length; i += 3) {
      var a = indices[i] * 3, b = indices[i + 1] * 3, c = indices[i + 2] * 3;
      var u = [positions[b]-positions[a], positions[b+1]-positions[a+1], positions[b+2]-positions[a+2]];
      var v = [positions[c]-positions[a], positions[c+1]-positions[a+1], positions[c+2]-positions[a+2]];
      var n = produitVectoriel(u, v);
      for (var j = 0; j < 3; j++) {
        normales[a + j] += n[j]; normales[b + j] += n[j]; normales[c + j] += n[j];
      }
    }
    for (var k = 0; k < normales.length; k += 3) {
      var longueur = Math.hypot(normales[k], normales[k+1], normales[k+2]) || 1;
      normales[k] /= longueur; normales[k+1] /= longueur; normales[k+2] /= longueur;
    }
    return normales;
  }

  // ----------------------------------------------------------------- shaders

  var VS_POINTS = [
    "attribute vec3 position; attribute vec3 couleur;",
    "uniform mat4 projection; uniform mat4 vue; uniform float taille;",
    "varying vec3 vCouleur;",
    "void main() {",
    "  vCouleur = couleur;",
    "  vec4 pos = vue * vec4(position, 1.0);",
    "  gl_Position = projection * pos;",
    "  gl_PointSize = max(1.0, taille / max(0.001, -pos.z));",
    "}",
  ].join("\n");

  var FS_POINTS = [
    "precision mediump float; varying vec3 vCouleur;",
    "void main() {",
    "  vec2 c = gl_PointCoord - vec2(0.5);",
    "  if (dot(c, c) > 0.25) discard;",  // points ronds plutot que carres
    "  gl_FragColor = vec4(vCouleur, 1.0);",
    "}",
  ].join("\n");

  var VS_MAILLAGE = [
    "attribute vec3 position; attribute vec3 normale; attribute vec3 couleur;",
    "uniform mat4 projection; uniform mat4 vue;",
    "varying vec3 vNormale; varying vec3 vCouleur;",
    "void main() {",
    "  vNormale = normale; vCouleur = couleur;",
    "  gl_Position = projection * vue * vec4(position, 1.0);",
    "}",
  ].join("\n");

  // COLMAP colore ses maillages par sommet plutot que par texture : les
  // ignorer rendrait un modele gris la ou l'information existe.
  var FS_MAILLAGE = [
    "precision mediump float; varying vec3 vNormale; varying vec3 vCouleur;",
    "void main() {",
    "  vec3 lumiere = normalize(vec3(0.4, 0.8, 0.6));",
    "  float diffus = abs(dot(normalize(vNormale), lumiere));",  // abs : les normales du pipeline sont parfois inversees
    "  gl_FragColor = vec4(vCouleur * (0.32 + 0.68 * diffus), 1.0);",
    "}",
  ].join("\n");

  function compiler(gl, source, type) {
    var shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      throw new Error("Shader : " + gl.getShaderInfoLog(shader));
    }
    return shader;
  }

  function programme(gl, vs, fs) {
    var prog = gl.createProgram();
    gl.attachShader(prog, compiler(gl, vs, gl.VERTEX_SHADER));
    gl.attachShader(prog, compiler(gl, fs, gl.FRAGMENT_SHADER));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      throw new Error("Programme : " + gl.getProgramInfoLog(prog));
    }
    return prog;
  }

  // ------------------------------------------------------------------- scene

  function demarrer(canvas, geometrie, statut) {
    var gl = canvas.getContext("webgl", { antialias: true, alpha: false });
    if (!gl) throw new Error("WebGL n'est pas disponible dans ce navigateur.");

    var positions = geometrie.positions;
    var aDesFaces = geometrie.indices && geometrie.indices.length > 0;

    // Recentrage : les repères issus du SfM sont arbitraires et souvent loin
    // de l'origine, ce qui ruine la precision des flottants a l'affichage.
    var min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
    for (var i = 0; i < positions.length; i += 3) {
      for (var a = 0; a < 3; a++) {
        if (positions[i + a] < min[a]) min[a] = positions[i + a];
        if (positions[i + a] > max[a]) max[a] = positions[i + a];
      }
    }
    var centre = [(min[0]+max[0])/2, (min[1]+max[1])/2, (min[2]+max[2])/2];
    for (var p = 0; p < positions.length; p += 3) {
      positions[p] -= centre[0]; positions[p+1] -= centre[1]; positions[p+2] -= centre[2];
    }
    var rayon = Math.max(0.001, Math.hypot(max[0]-min[0], max[1]-min[1], max[2]-min[2]) / 2);

    var typeIndices = gl.UNSIGNED_SHORT;
    var prog = programme(gl, aDesFaces ? VS_MAILLAGE : VS_POINTS, aDesFaces ? FS_MAILLAGE : FS_POINTS);
    gl.useProgram(prog);

    function tampon(donnees, nom, taille) {
      var buf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.bufferData(gl.ARRAY_BUFFER, donnees, gl.STATIC_DRAW);
      var loc = gl.getAttribLocation(prog, nom);
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, taille, gl.FLOAT, false, 0, 0);
    }

    tampon(positions, "position", 3);
    var nombreIndices = 0;
    if (aDesFaces) {
      tampon(calculerNormales(positions, geometrie.indices), "normale", 3);
      tampon(geometrie.couleurs, "couleur", 3);
      var tamponIndices = gl.createBuffer();
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, tamponIndices);
      // Au-dela de 65 535 sommets il faut des indices 32 bits, via une
      // extension qui n'existe pas partout : on tronque plutot que planter.
      var extension = gl.getExtension("OES_element_index_uint");
      var tropGrand = positions.length / 3 > 65535;
      if (tropGrand && !extension) {
        statut("Maillage trop dense pour ce navigateur : affichage partiel.");
        geometrie.indices = geometrie.indices.filter(function (v) { return v < 65536; });
      }
      var indices32 = tropGrand && !!extension;
      var donneesIndices = indices32
        ? new Uint32Array(geometrie.indices)
        : new Uint16Array(geometrie.indices);
      gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, donneesIndices, gl.STATIC_DRAW);
      nombreIndices = donneesIndices.length;
      typeIndices = indices32 ? gl.UNSIGNED_INT : gl.UNSIGNED_SHORT;
    } else {
      tampon(geometrie.couleurs, "couleur", 3);
    }

    gl.enable(gl.DEPTH_TEST);
    gl.clearColor(0.059, 0.075, 0.098, 1.0);

    var camera = { theta: Math.PI / 4, phi: Math.PI / 3, distance: rayon * 3, cible: [0, 0, 0] };
    var uProjection = gl.getUniformLocation(prog, "projection");
    var uVue = gl.getUniformLocation(prog, "vue");
    var uTaille = gl.getUniformLocation(prog, "taille");

    function dessiner() {
      var ratio = Math.min(window.devicePixelRatio || 1, 2);
      var largeur = Math.floor(canvas.clientWidth * ratio);
      var hauteur = Math.floor(canvas.clientHeight * ratio);
      if (canvas.width !== largeur || canvas.height !== hauteur) {
        canvas.width = largeur; canvas.height = hauteur;
      }
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);

      var sinPhi = Math.sin(camera.phi);
      var oeil = [
        camera.cible[0] + camera.distance * sinPhi * Math.cos(camera.theta),
        camera.cible[1] + camera.distance * Math.cos(camera.phi),
        camera.cible[2] + camera.distance * sinPhi * Math.sin(camera.theta),
      ];
      gl.uniformMatrix4fv(uProjection, false,
        perspective(Math.PI / 4, canvas.width / canvas.height, rayon / 100, rayon * 40));
      gl.uniformMatrix4fv(uVue, false, regarder(oeil, camera.cible, [0, 1, 0]));
      if (uTaille) gl.uniform1f(uTaille, rayon * 2.2);

      if (aDesFaces) gl.drawElements(gl.TRIANGLES, nombreIndices, typeIndices, 0);
      else gl.drawArrays(gl.POINTS, 0, positions.length / 3);
    }

    installerControles(canvas, camera, rayon, dessiner);
    window.addEventListener("resize", dessiner);
    dessiner();

    statut((positions.length / 3).toLocaleString("fr-FR") + " sommets" +
      (aDesFaces ? ", " + (nombreIndices / 3).toLocaleString("fr-FR") + " triangles" : "") + ".");
  }

  function installerControles(canvas, camera, rayon, dessiner) {
    var actif = null, dernierX = 0, dernierY = 0;

    function debut(x, y, bouton) { actif = bouton; dernierX = x; dernierY = y; }
    function deplacement(x, y) {
      if (actif === null) return;
      var dx = x - dernierX, dy = y - dernierY;
      dernierX = x; dernierY = y;
      if (actif === 2) {
        // Deplacement lateral, proportionnel a la distance : le pas ressenti
        // reste constant quel que soit le niveau de zoom.
        var facteur = camera.distance * 0.0015;
        var droite = [-Math.sin(camera.theta), 0, Math.cos(camera.theta)];
        camera.cible[0] -= droite[0] * dx * facteur;
        camera.cible[2] -= droite[2] * dx * facteur;
        camera.cible[1] += dy * facteur;
      } else {
        camera.theta -= dx * 0.007;
        camera.phi = Math.min(Math.PI - 0.05, Math.max(0.05, camera.phi - dy * 0.007));
      }
      dessiner();
    }

    canvas.addEventListener("mousedown", function (e) { e.preventDefault(); debut(e.clientX, e.clientY, e.button); });
    window.addEventListener("mousemove", function (e) { deplacement(e.clientX, e.clientY); });
    window.addEventListener("mouseup", function () { actif = null; });
    canvas.addEventListener("contextmenu", function (e) { e.preventDefault(); });

    canvas.addEventListener("wheel", function (e) {
      e.preventDefault();
      camera.distance = Math.min(rayon * 30, Math.max(rayon * 0.05,
        camera.distance * (e.deltaY > 0 ? 1.12 : 0.89)));
      dessiner();
    }, { passive: false });

    canvas.addEventListener("touchstart", function (e) {
      if (e.touches.length === 1) debut(e.touches[0].clientX, e.touches[0].clientY, 0);
      else if (e.touches.length === 2) {
        actif = 2;
        dernierX = Math.hypot(
          e.touches[0].clientX - e.touches[1].clientX,
          e.touches[0].clientY - e.touches[1].clientY);
      }
    }, { passive: true });

    canvas.addEventListener("touchmove", function (e) {
      e.preventDefault();
      if (e.touches.length === 1 && actif === 0) {
        deplacement(e.touches[0].clientX, e.touches[0].clientY);
      } else if (e.touches.length === 2) {
        var ecart = Math.hypot(
          e.touches[0].clientX - e.touches[1].clientX,
          e.touches[0].clientY - e.touches[1].clientY);
        camera.distance = Math.min(rayon * 30, Math.max(rayon * 0.05,
          camera.distance * (dernierX / (ecart || 1))));
        dernierX = ecart;
        dessiner();
      }
    }, { passive: false });

    canvas.addEventListener("touchend", function () { actif = null; });
  }

  // ------------------------------------------------------------- chargement

  window.PhotoGramViewer = {
    // Les lecteurs sont exposes : ce sont les seules parties verifiables sans
    // contexte WebGL, et leur arithmetique d'offsets merite d'etre testee.
    lirePly: lirePly,
    lireObj: lireObj,

    charger: function (canvas, url, extension, elementStatut) {
      function statut(message) { if (elementStatut) elementStatut.textContent = message; }
      statut("Telechargement du modele…");

      fetch(url)
        .then(function (reponse) {
          if (!reponse.ok) throw new Error("Telechargement impossible (" + reponse.status + ").");
          return extension === "obj" ? reponse.text() : reponse.arrayBuffer();
        })
        .then(function (donnees) {
          statut("Analyse du fichier…");
          var geometrie = extension === "obj" ? lireObj(donnees) : lirePly(donnees);
          if (!geometrie.positions.length) {
            var taille = donnees.byteLength !== undefined ? donnees.byteLength : donnees.length;
            throw new Error(
              "Le fichier ne contient aucune geometrie exploitable (" + taille + " octets lus)."
            );
          }
          demarrer(canvas, geometrie, statut);
        })
        .catch(function (erreur) {
          statut("Erreur : " + erreur.message);
        });
    },
  };
})();
