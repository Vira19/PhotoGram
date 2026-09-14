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

Sur un ordinateur de bureau, les quatre profils sont accessibles ; les durées
de la dernière colonne donnent l'ordre de grandeur.

Le profil **« Nuage épars seulement »** est le mode par défaut, et le seul
vraiment confortable sur RPi3. Il suffit largement pour vérifier qu'une série
de photos « tient » avant de la rejouer en qualité sur une vraie machine — ce
qui est précisément le flux de travail que ce site vise.

L'application web, elle, est parfaitement à l'aise sur un RPi3 : téléversement,
analyse EXIF, détection de flou, vignettes, suivi des jobs et visionneuse 3D
côté navigateur. Ses dépendances sont tenues au strict minimum — **Pillow est
le seul paquet natif** — précisément pour que l'installation ne dépende pas
d'un compilateur.

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

## Déployer sur son propre ordinateur

C'est le chemin le plus court, et il ne demande ni droits administrateur, ni
systemd, ni installation système : le dépôt se suffit à lui-même.

**Linux / macOS**

```bash
git clone https://github.com/vira19/PhotoGram.git
cd PhotoGram

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m app.run
```

**Windows** — double-cliquer sur `scripts\run.bat`, qui crée l'environnement au
premier lancement puis démarre le site. En ligne de commande :

```
git clone https://github.com/vira19/PhotoGram.git
cd PhotoGram
scripts\run.bat
```

Prérequis : Python 3.9 ou plus récent, installé depuis
[python.org](https://www.python.org/downloads/) **en cochant « Add Python to
PATH »**. Le script le vérifie et le dit clairement sinon.

Au premier lancement, `app.run` crée un `.env`, **génère un mot de passe et
l'affiche**, puis démarre l'interface web et le worker ensemble. Ctrl+C arrête
les deux. Les données vont dans `./data`.

### Retrouver ou changer le mot de passe

Il n'est affiché en clair qu'à sa création. Ensuite il se lit dans le fichier
`.env`, à la racine du projet, sur la ligne `PHOTOGRAM_PASSWORD` :

```bash
grep PHOTOGRAM_PASSWORD .env       # Linux, macOS
```
```
findstr PHOTOGRAM_PASSWORD .env    :: Windows
```

Pour le changer, éditer cette ligne (Bloc-notes convient) et redémarrer. Un
`.env` supprimé est régénéré au lancement suivant, avec un nouveau mot de
passe — les projets et les photos, eux, restent dans `./data`.

Les réglages initiaux sont déduits de la machine : sur un ordinateur de bureau,
les photos ne sont plus bridées à 1600 px ni le lot à 40 images comme sur un
Pi. Tout reste modifiable dans `.env`.

Quelques options :

```bash
python -m app.run --port 8080     # autre port
python -m app.run --sans-worker   # interface seule
python -m app.run --dev           # rechargement auto du code
```

Sous Windows, les mêmes options se passent au `.bat` : `scripts\run.bat --port 8080`.

### Installer la chaîne de reconstruction

COLMAP suffit pour le nuage épars et s'installe partout :

| Système | Commande |
|---|---|
| Debian / Ubuntu / Raspberry Pi OS | `sudo apt install colmap` |
| Fedora | `sudo dnf install colmap` |
| Arch | `sudo pacman -S colmap` |
| macOS | `brew install colmap` |
| Windows | [Binaires officiels](https://github.com/colmap/colmap/releases) (`...-windows-no-cuda.zip`), à décompresser |

Sous Windows, après avoir décompressé COLMAP, indiquer le dossier contenant
`colmap.exe` dans `.env` :

```
PHOTOGRAM_COLMAP_BIN=C:\Outils\colmap\bin
```

Inutile de toucher au `PATH` du système. La page **État du système** confirme
la détection.

Pour aller jusqu'au maillage texturé, il faut OpenMVG + OpenMVS. Sous Linux,
`sudo ./scripts/install_pipeline.sh --openmvg` les compile. Sous Windows, les
deux projets publient des binaires précompilés dans leurs *releases* GitHub.

Si les exécutables ne sont pas dans le `PATH`, indiquer leur dossier dans
`.env` — `PHOTOGRAM_COLMAP_BIN`, `PHOTOGRAM_OPENMVG_BIN`,
`PHOTOGRAM_OPENMVS_BIN`. La page **État du système** confirme ce qui est
détecté.

### En faire un service permanent

`python -m app.run` s'arrête avec le terminal. Pour qu'il tourne en permanence
et redémarre tout seul, utiliser les unités systemd via
`sudo ./scripts/install.sh` (Linux uniquement) — c'est ce que décrit la section
suivante.

## Installation sur Raspberry Pi

```bash
git clone https://github.com/vira19/PhotoGram.git
cd PhotoGram

sudo ./scripts/install.sh            # app, venv, services systemd, mot de passe généré
sudo ./scripts/install_pipeline.sh   # COLMAP depuis les dépôts
```

Si quelque chose échoue, le script indique l'étape exacte. Pour un état des
lieux complet :

```bash
./scripts/diagnose.sh
```

Le rapport couvre le système, Python, les dépendances, les services, la chaîne
de reconstruction et le réseau. Il ne contient ni mot de passe ni clé, et peut
être copié tel quel dans un ticket.

### Raspberry Pi OS 64 bits fortement conseillé

Un RPi3 supporte le 64 bits, et c'est la configuration à privilégier :
certaines dépendances Python (`pydantic-core`) n'ont pas de version
précompilée pour les architectures 32 bits (`armv6l`, `armv7l`) et devraient
alors être compilées avec Rust — ce qui échoue presque toujours sur un Pi.
Vérifier avec `uname -m` : `aarch64` est le bon résultat.

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

Premier réflexe : `./scripts/diagnose.sh`.

| Symptôme | Piste |
|---|---|
| L'installation des dépendances Python échoue | Probablement un système 32 bits (`uname -m`). Voir ci-dessus. |
| `rsync: command not found` | Corrigé : le script utilise désormais `tar`, présent partout. |
| Le service démarre mais n'écrit rien | `PHOTOGRAM_DATA_DIR` hors de `ReadWritePaths` du service. Relancer `install.sh` le corrige. |
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
  run.py             lancement local : web + worker en une commande
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
scripts/             installation, diagnostic et développement
tests/               parcours web, rendu des gabarits, pipeline complet
```

## Portabilité

Le code tourne sur Linux, macOS et Windows. Trois points demandent un
traitement par système, et sont isolés pour cela :

- **la sonde mémoire** (`app/hardware.py`) : `/proc/meminfo` sous Linux,
  `GlobalMemoryStatusEx` sous Windows, `vm_stat` sous macOS. Quand seul le
  total est lisible, l'interface affiche « — » plutôt qu'un zéro trompeur ;
- **l'arrêt d'une reconstruction** (`app/pipeline/runner.py`) : les outils
  externes essaiment des processus fils, qu'il faut emporter avec le parent.
  Groupe de processus et `SIGTERM`/`SIGKILL` sous POSIX, `taskkill /T` sous
  Windows ;
- **la surveillance du lanceur** (`app/worker.py`) : sous POSIX un orphelin est
  rattaché à init, ce que le worker détecte au `PPID`. Windows ne réattribue
  rien, et l'état du processus parent doit être interrogé directement — surtout
  pas avec `os.kill(pid, 0)`, qui sous Windows ne teste rien mais **tue** le
  processus visé.

Les unités systemd de `deploy/` sont évidemment propres à Linux ; ailleurs,
`python -m app.run` reste le mode d'emploi.

## Sécurité

Un mot de passe unique, pensé pour un réseau local : pas de comptes, pas de
TLS. **Ne pas exposer directement sur Internet.** Pour un accès distant,
passer par un VPN (WireGuard, Tailscale) plutôt que par une redirection de
port.
