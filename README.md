# Odysseus

**A fast GTK4 / libadwaita file manager for Linux, with the look of Nautilus and the power of its extensions built in.**
**Gestionnaire de fichiers GTK4 / libadwaita rapide, au look de Nautilus, avec ses extensions intégrées.**

[English](#english) · [Français](#français)

![File browser / Navigateur de fichiers](docs/captures/accueil.png)

| Dual panel / Double panneau | Column view / Vue en colonnes | Grid / Grille |
|---|---|---|
| ![Dual panel](docs/captures/double-panneau.png) | ![Columns](docs/captures/colonnes.png) | ![Grid](docs/captures/grille.png) |

| Archive browser / Archive ouverte | PDF watermark / Filigrane PDF | Features / Fonctionnalités | Dark mode / Mode sombre |
|---|---|---|---|
| ![Archive](docs/captures/archive.png) | ![Watermark](docs/captures/filigrane.png) | ![Features](docs/captures/fonctionnalites.png) | ![Dark](docs/captures/sombre.png) |

---

## English

Odysseus is a file manager that looks and behaves like GNOME Files (Nautilus) and natively includes every feature of the [nautilus-extensions](https://github.com/ToFpon/nautilus-extensions) collection. Each feature can be turned off in **Preferences → Features**, without restarting. It follows your desktop's GTK4 theme (Adwaita, light/dark, accent color, or a theme installed in `~/.config/gtk-4.0/`). The archive engine comes from [Pressoir](https://github.com/ToFpon/Pressoir).

### Speed

Huge folders open almost instantly: a folder of **100,000 files is displayed in about one second**.

- the folder is read with `os.scandir` in a background thread; names appear first, sizes and dates follow;
- the views use a virtual list model: row objects are only created for the rows on screen;
- metadata reads (`stat`) of all panels and columns are serialized through a single lock, which avoids the Python GIL “convoy” effect that made two panels ten times slower than one;
- only the visible view (list, grid or columns) is attached to the model;
- thumbnails and folder sizes are computed in a LIFO worker pool: what you are looking at comes first.

### Features

#### Views (from Dual Panel, Column Browser, Preview Panel…)
- **List** (compact 35 px rows like Nautilus, optional columns: size, type, date, permissions, owner, duration), **grid** (64 to 256 px) and **columns** (Miller columns, resizable, ← / → between columns). Ctrl+1 / 2 / 3, Ctrl+plus / minus / 0.
- **Dual panel** (F3): two locations side by side, F6 / Shift+F6 copy or move to the other panel, Ctrl+Tab switches panels.
- **Preview panel** (F4): images, videos, PDF pages, text, detailed information.
- **Thumbnails** shared with Nautilus (freedesktop cache).
- Hidden files dimmed (icon only or icon and name), cut items dimmed until pasted, a colored bar in front of copied items.
- **Folder colors and emblems**, compatible with Folder Color.
- **Duration column** for audio and video files (ffprobe, cached).

#### Files
- Tabs, history, breadcrumb path bar with Tab completion, bookmarks shared with GTK, Recent, Trash (restore, empty), Network, devices with the same eject rules as Nautilus (no eject button for fstab and internal disks).
- Copy / move with conflict dialog (skip, rename, merge, replace, apply to all), **progress window** for long operations, undo (Ctrl+Z), drag and drop, clipboard compatible with Nautilus.
- **Archives** (F7): browse 50+ formats like folders, extract here, extract to, compress, drag files out of an archive (Pressoir engine, 7-Zip).
- **Content search** (F8): search and replace text in files with ripgrep or grep, with preview and backups. Recursive file name search.
- Open in text editor, open terminal here (Ctrl+Alt+T).
- Optional `org.freedesktop.FileManager1` D-Bus service, so “Show in folder” from other applications opens Odysseus.

#### PDF (rewritten and improved)
- **Compress** with Ghostscript: real page-by-page progress, before/after sizes, original kept if smaller.
- **Merge** in any order, with page ranges per file (`1-3,7,10-`).
- **Watermark**: vector text or image, nine positions or tiled, opacity, angle, pages, live preview, optional flattening so the watermark cannot be removed.
- **Pages**: extract, delete, rotate or split pages, with thumbnails.

#### Media and system
- **Video to audio**: MP3, M4A, OGG, OPUS, FLAC, WAV.
- **Image annotation**: arrows, frames, text, highlighter, freehand.
- **.deb installer** with dependency check and live log (pkexec).

Duplicates in the original extensions were merged: Archive Browser + Extract Here, Hidden Dim icon/all (now an option), the extensions manager (now Preferences → Features), Progress Mirror (now the progress window).

### Installation

Requirements: Python 3.10+, PyGObject, GTK ≥ 4.14, libadwaita ≥ 1.5. Every other program is optional: a feature whose tool is missing tells you which package to install.

```sh
# Debian / Ubuntu 24.04+ / Zorin OS 18
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1
# optional
sudo apt install 7zip lbzip2 pigz ghostscript qpdf python3-pypdf ffmpeg ffmpegthumbnailer ripgrep gir1.2-poppler-0.18
```

```sh
wget https://github.com/ToFpon/Odysseus/archive/refs/heads/main.tar.gz -O odysseus.tar.gz
tar -xzf odysseus.tar.gz && cd Odysseus-main
./install.sh              # installs into ~/.local (no sudo)
./install.sh --default    # same, and makes Odysseus the default file manager
./install.sh --uninstall
```

To try it without installing: `./bin/odysseus`.

#### Command line

```sh
odysseus [FOLDERS…]       # open folders (one tab each)
odysseus --new-window     # new window
odysseus --dual A B       # dual panel with A on the left and B on the right
odysseus --search-content FOLDER
```

### Languages

French (source), English and German. The language follows the system (`LANGUAGE`) or can be forced in Preferences.

### Architecture

- `odysseus/backend/`: no GTK — folder scanning (`scanner.py`), file operations, trash, bookmarks, thumbnails, folder colors, archives (Pressoir engine), PDF, content search, media, .deb.
- `odysseus/ui/`: virtual model and selection (`model.py`), list/grid views, Miller columns, panes, workspace (dual panel), window, sidebar, path bar, dialogs.
- `odysseus/features.py`: the list of features, their required tools and their on/off state.

### Tests

```sh
python3 -m unittest discover -s tests       # back end
xvfb-run -a python3 tests/ui_smoke.py       # interface
```

### License

GPL-3.0-or-later.

---

## Français

Odysseus est un gestionnaire de fichiers qui ressemble à Fichiers (Nautilus) et se comporte comme lui, en intégrant nativement toutes les fonctionnalités des extensions [nautilus-extensions](https://github.com/ToFpon/nautilus-extensions). Chacune se désactive dans **Préférences → Fonctionnalités**, sans redémarrer. Il suit le thème GTK4 du bureau (Adwaita, clair/sombre, couleur d'accentuation ou thème installé dans `~/.config/gtk-4.0/`). Le moteur d'archives vient de [Pressoir](https://github.com/ToFpon/Pressoir).

### Rapidité

Les très gros dossiers s'ouvrent quasi instantanément : un dossier de **100 000 fichiers s'affiche en une seconde environ**.

- le dossier est lu avec `os.scandir` dans un fil d'exécution séparé ; les noms s'affichent d'abord, tailles et dates suivent ;
- les vues reposent sur un modèle de liste virtuel : les objets ne sont créés que pour les lignes à l'écran ;
- les lectures de métadonnées (`stat`) de tous les panneaux et colonnes passent par un verrou unique, ce qui évite l'effet « convoi » du GIL Python qui rendait deux panneaux dix fois plus lents qu'un seul ;
- seule la vue visible (liste, grille ou colonnes) est branchée sur le modèle ;
- miniatures et tailles de dossiers sont calculées par une file LIFO : ce que tu regardes passe en premier.

### Fonctionnalités

#### Affichage (Dual Panel, Column Browser, Preview Panel…)
- **Liste** (lignes compactes de 35 px comme Nautilus, colonnes facultatives : taille, type, date, permissions, propriétaire, durée), **grille** (64 à 256 px) et **colonnes** (colonnes de Miller redimensionnables, ← / → d'une colonne à l'autre). Ctrl+1 / 2 / 3, Ctrl+plus / moins / 0.
- **Double panneau** (F3) : deux emplacements côte à côte, F6 / Maj+F6 pour copier ou déplacer vers l'autre panneau, Ctrl+Tab pour changer de panneau.
- **Panneau d'aperçu** (F4) : images, vidéos, pages PDF, textes, informations détaillées.
- **Miniatures** partagées avec Nautilus (cache freedesktop).
- Fichiers cachés estompés (icône seule ou icône et nom), éléments coupés estompés jusqu'au collage, barre colorée devant les éléments copiés.
- **Couleurs et emblèmes de dossiers**, compatibles avec Folder Color.
- **Colonne Durée** des fichiers audio et vidéo (ffprobe, avec cache).

#### Fichiers
- Onglets, historique, fil d'Ariane avec complétion par Tab, signets partagés avec GTK, Récents, Corbeille (restaurer, vider), Réseau, périphériques avec les mêmes règles d'éjection que Nautilus (jamais de bouton d'éjection pour les disques fstab et internes).
- Copie / déplacement avec gestion des conflits (ignorer, renommer, fusionner, remplacer, appliquer à tous), **fenêtre de progression** pour les opérations longues, annulation (Ctrl+Z), glisser-déposer, presse-papiers compatible avec Nautilus.
- **Archives** (F7) : parcourir plus de 50 formats comme des dossiers, extraire ici, extraire vers, compresser, glisser des fichiers hors d'une archive (moteur Pressoir, 7-Zip).
- **Recherche dans le contenu** (F8) : rechercher et remplacer du texte dans les fichiers avec ripgrep ou grep, avec aperçu et sauvegardes. Recherche récursive par nom.
- Ouvrir avec l'éditeur de texte, terminal ici (Ctrl+Alt+T).
- Service D-Bus `org.freedesktop.FileManager1` facultatif : « Afficher dans le dossier » depuis les autres applications ouvre Odysseus.

#### PDF (réécrits et améliorés)
- **Compresser** avec Ghostscript : vraie progression page par page, tailles avant / après, original conservé s'il est plus petit.
- **Fusionner** dans l'ordre voulu, avec des plages de pages par fichier (`1-3,7,10-`).
- **Filigrane** : texte vectoriel ou image, neuf positions ou mosaïque, opacité, angle, pages, aperçu en direct, aplatissement facultatif pour qu'il ne puisse pas être retiré.
- **Pages** : extraire, supprimer, faire pivoter ou éclater les pages, avec miniatures.

#### Multimédia et système
- **Vidéo vers audio** : MP3, M4A, OGG, OPUS, FLAC, WAV.
- **Annotation d'images** : flèches, cadres, texte, surligneur, main levée.
- **Installateur .deb** avec contrôle des dépendances et journal en direct (pkexec).

Les doublons des extensions d'origine ont été fusionnés : Archive Browser + Extract Here, Hidden Dim icon/all (devenu une option), le gestionnaire d'extensions (devenu Préférences → Fonctionnalités), Progress Mirror (devenu la fenêtre de progression).

### Installation

Prérequis : Python 3.10+, PyGObject, GTK ≥ 4.14, libadwaita ≥ 1.5. Tous les autres programmes sont facultatifs : une fonctionnalité dont l'outil manque indique le paquet à installer.

```sh
# Debian / Ubuntu 24.04+ / Zorin OS 18
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1
# facultatif
sudo apt install 7zip lbzip2 pigz ghostscript qpdf python3-pypdf ffmpeg ffmpegthumbnailer ripgrep gir1.2-poppler-0.18
```

```sh
wget https://github.com/ToFpon/Odysseus/archive/refs/heads/main.tar.gz -O odysseus.tar.gz
tar -xzf odysseus.tar.gz && cd Odysseus-main
./install.sh              # installe dans ~/.local (sans sudo)
./install.sh --default    # idem, et fait d'Odysseus le gestionnaire de fichiers par défaut
./install.sh --uninstall
```

Pour essayer sans installer : `./bin/odysseus`.

#### Ligne de commande

```sh
odysseus [DOSSIERS…]      # ouvrir des dossiers (un onglet chacun)
odysseus --new-window     # nouvelle fenêtre
odysseus --dual A B       # double panneau, A à gauche et B à droite
odysseus --search-content DOSSIER
```

### Langues

Français (langue source), anglais et allemand. La langue suit le système (`LANGUAGE`) ou se choisit dans les Préférences.

### Architecture

- `odysseus/backend/` : sans GTK — lecture des dossiers (`scanner.py`), opérations sur les fichiers, corbeille, signets, miniatures, couleurs de dossiers, archives (moteur Pressoir), PDF, recherche dans le contenu, multimédia, .deb.
- `odysseus/ui/` : modèle virtuel et sélection (`model.py`), vues liste/grille, colonnes de Miller, panneaux, espace de travail (double panneau), fenêtre, barre latérale, fil d'Ariane, dialogues.
- `odysseus/features.py` : la liste des fonctionnalités, leurs outils nécessaires et leur état activé / désactivé.

### Tests

```sh
python3 -m unittest discover -s tests       # moteur
xvfb-run -a python3 tests/ui_smoke.py       # interface
```

### Licence

GPL-3.0-or-later.
