# PhotoGram

Site de photogrammétrie auto-hébergé : on téléverse des photos depuis un
navigateur, le serveur reconstruit un modèle 3D, et le résultat se visualise
puis se télécharge depuis la même page.

Conçu pour tourner sur un Raspberry Pi 3, puis déménager sans réécriture vers
une machine plus puissante.

---

## Ce qu'un Raspberry Pi 3 peut réellement faire

Autant le dire tout de suite : **un RPi3 (1 Go de RAM, pas de GPU CUDA) ne fait
pas de photogrammétrie complète.** Meshroom exige CUDA ; la densification de
COLMAP aussi ; OpenMVS en CPU fonctionne mais réclame plusieurs Go de mémoire.

Ce que ça donne concrètement, pour ~20 photos :

| Profil | Ce qui est produit | Sur RPi3 | Sur une machine 16 Go |
|---|---|---|---|
| **Nuage épars seulement** | Positions des caméras + nuage de points coloré | 10 à 40 min — **utilisable** | 1 à 3 min |
| **Raspberry Pi (maillage minimal)** | Maillage texturé très basse résolution | 3 à 8 h, OOM fréquent sans swap | 10 à 20 min |
| **Équilibré** | Maillage texturé correct | hors de portée | 30 à 60 min |
| **Qualité maximale** | Maillage raffiné pleine résolution | hors de portée | 2 à 6 h |

Le profil **« Nuage épars seulement »** est le mode par défaut, et le seul
vraiment confortable sur RPi3. Il suffit largement pour vérifier qu'une série
de photos « tient » avant de la rejouer en qualité sur une vraie machine — ce
qui est précisément le flux de travail que ce site vise.

L'application web, elle, est parfaitement à l'aise sur un RPi3 : téléversement,
analyse EXIF, détection de flou, vignettes, suivi des jobs et visionneuse 3D
côté navigateur.

## Architecture

Deux processus, une base SQLite partagée :

```
  navigateur ──HTTP──►  photogram-web        (FastAPI, ~60 Mo de RAM)
                             │
                             ▼
                        photogram.db         (SQLite en mode WAL = file d'attente)
                             │
                             ▼
                        photogram-worker     (un job à la fois, nice + OOM-sacrifiable)
                             │
                             ▼
                     COLMAP  ou  OpenMVG + OpenMVS
```

Le découpage n'est pas gratuit :

- une reconstruction sature les quatre cœurs pendant des heures ; dans le même
  processus que le web, elle rendrait le site injoignable ;
- le service du worker est *niced*, déprioritisé en E/S, et marqué comme
  sacrifiable pour l'OOM killer — quand la mémoire manque, c'est lui qui tombe
  et le site reste debout pour l'annoncer ;
- **pour migrer vers une machine plus puissante, il suffit d'y installer le
  worker** et de lui donner accès au même répertoire de données (NFS, SMB). Le
  RPi continue à servir l'interface. Aucun code à modifier.

### Deux chaînes de reconstruction

| | COLMAP | OpenMVG + OpenMVS |
|---|---|---|
| Installation | `apt install colmap`, une minute | compilation, plusieurs heures sur RPi3 |
| Nuage épars (CPU) | oui | oui |
| Maillage texturé (CPU) | non (densification CUDA uniquement) | oui |

La chaîne est choisie automatiquement : OpenMVG dès qu'il est complet pour le
profil demandé, COLMAP sinon. `PHOTOGRAM_BACKEND` permet de forcer l'un ou
l'autre. La page **État du système** affiche ce qui est réellement installé, et
l'interface grise les profils que la machine ne sait pas honorer.

## Installation sur Raspberry Pi

```bash
git clone https://github.com/vira19/PhotoGram.git
cd PhotoGram

sudo ./scripts/install.sh            # app, venv, services systemd, mot de passe généré
sudo ./scripts/install_pipeline.sh   # COLMAP depuis les dépôts
```

Le site écoute alors sur `http://<ip-du-pi>:8000`. Le mot de passe généré est
affiché en fin d'installation et stocké dans `/opt/photogram/.env`.

Pour la chaîne complète jusqu'au maillage (long, et à réserver à une machine
plus costaude) :

```bash
sudo ./scripts/install_pipeline.sh --openmvg
```

### Swap : à faire avant tout essai de densification

Sur un RPi3, sans swap, la densification est tuée par l'OOM killer :

```bash
sudo sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=2048/' /etc/dphys-swapfile
sudo systemctl restart dphys-swapfile
```

Utiliser un disque USB plutôt que la carte SD pour `PHOTOGRAM_DATA_DIR` est
vivement conseillé : une reconstruction écrit plusieurs Go de fichiers
temporaires, ce qui use une carte SD rapidement.

## Développement

```bash
./scripts/dev.sh     # crée le venv et le .env, lance le web + le worker
```

`http://localhost:8000`, mot de passe `photogram`.

```bash
.venv/bin/python -m pytest tests/ -q
```

Les tests couvrent le parcours web, le rendu de tous les gabarits et
l'orchestration complète du pipeline — cette dernière grâce à des binaires
factices qui imitent OpenMVG, OpenMVS et COLMAP. Aucun outil externe n'est
donc requis pour lancer la suite.

## Configuration

Tout passe par `.env` (voir `.env.example`). Les réglages qui comptent sur une
petite machine :

| Variable | Défaut | Rôle |
|---|---|---|
| `PHOTOGRAM_WORK_MAX_DIM` | `1600` | Côté long des copies envoyées au calcul. Au-delà de 2400, l'OOM guette sur RPi3. |
| `PHOTOGRAM_MAX_PHOTOS` | `40` | Plafond de photos par reconstruction. |
| `PHOTOGRAM_THREADS` | `3` | Threads laissés au pipeline (sur 4 cœurs, en garder un pour le web). |
| `PHOTOGRAM_KEEP_INTERMEDIATES` | `0` | À `1`, conserve les fichiers de travail — plusieurs Go par job. |
| `PHOTOGRAM_BACKEND` | `auto` | `openmvg` ou `colmap` pour forcer une chaîne. |
| `PHOTOGRAM_DATA_DIR` | `/var/lib/photogram` | Photos, base, résultats. À placer sur un disque USB. |

Les originaux sont toujours conservés intacts ; le pipeline ne travaille que
sur des copies réduites, avec l'EXIF préservé (la focale sert à la calibration).

## Utilisation

1. **Créer un projet**, un par sujet.
2. **Téléverser les photos.** Chacune est analysée : dimensions, appareil,
   focale EXIF et un score de netteté (variance du laplacien) affiché en
   pastille. Sous 60, l'image est probablement floue.
3. **Écarter les mauvaises photos** — une seule photo floue suffit parfois à
   faire échouer le positionnement des caméras. Le bouton « Écarter
   automatiquement les photos floues » fait le tri en un clic.
4. **Lancer une reconstruction** avec le profil voulu. Elle part en file ; le
   worker la prend dès qu'il est libre.
5. **Suivre l'avancement** : étapes, durées et journal brut des outils se
   rafraîchissent tout seuls.
6. **Visualiser et télécharger.** La visionneuse 3D intégrée (WebGL, sans
   aucune dépendance externe) affiche les nuages `.ply` et les maillages
   `.obj`. Pour un rendu texturé, télécharger le `.obj`, son `.mtl` et l'image,
   puis ouvrir dans MeshLab ou CloudCompare.

### Réussir ses photos

C'est ici que tout se joue — bien plus que dans le choix du profil :

- **Recouvrement de 60 à 80 %** entre deux photos consécutives. Tourner autour
  du sujet par petits pas, sans sauter de position.
- **Lumière diffuse et constante.** Les ombres dures se figent dans la texture.
  Ni soleil direct, ni flash.
- **Sujet texturé.** Une surface unie, brillante ou transparente ne donne aucun
  point caractéristique.
- **Mise au point et exposition figées** si l'appareil le permet.
- **Le sujet ne bouge pas, l'appareil tourne.** Un plateau tournant devant un
  fond fixe donne des résultats incohérents.
- **Trois hauteurs de prise de vue** valent mieux qu'un seul tour horizontal.

## Dépannage

| Symptôme | Piste |
|---|---|
| « Le SfM n'a produit aucune reconstruction » | Recouvrement insuffisant, sujet sans texture, ou photos floues. Regarder les scores de netteté. |
| « série fragmentée » dans le journal | Deux groupes de photos sans lien visuel. Ajouter des vues de transition. |
| Job repassé en échec après un redémarrage | Le worker a été tué (mémoire). Réduire le nombre de photos, prendre un profil plus léger, ajouter du swap. |
| Jobs en attente mais rien ne démarre | `systemctl status photogram-worker` |
| Profils grisés dans l'interface | Chaîne non installée : voir la page **État du système**. |
| Plus de place sur le disque | Vérifier que `PHOTOGRAM_KEEP_INTERMEDIATES` vaut `0`, et supprimer les vieilles reconstructions. |

État complet de la machine et du pipeline : `/health` (aussi disponible en JSON
avec l'en-tête `Accept: application/json`).

## Structure

```
app/
  main.py            application FastAPI, middleware d'authentification
  worker.py          boucle de traitement de la file
  config.py          configuration issue de l'environnement
  db.py              SQLite (WAL) + migrations
  imaging.py         EXIF, vignettes, copies de travail, détection de flou
  system.py          mémoire, disque, charge, température
  pipeline/
    binaries.py      détection des outils et choix de la chaîne
    presets.py       profils de qualité
    plan.py          plan OpenMVG + OpenMVS
    colmap.py        plan COLMAP
    runner.py        exécution, journalisation, annulation
  routes/            authentification, projets et photos, reconstructions
  templates/         gabarits Jinja2
  static/            CSS, JS, visionneuse WebGL (aucune dépendance externe)
deploy/              unités systemd
scripts/             installation et développement
tests/               parcours web, rendu des gabarits, pipeline complet
```

## Sécurité

Un mot de passe unique, pensé pour un réseau local : pas de comptes, pas de
TLS. **Ne pas exposer directement sur Internet.** Pour un accès distant,
passer par un VPN (WireGuard, Tailscale) plutôt que par une redirection de
port.
