# Odysseus — notes de travail

Gestionnaire de fichiers GTK4 / libadwaita en Python, look Nautilus, intégrant les extensions de
`ToFpon/nautilus-extensions` (dépôt en lecture seule) ; moteur d'archives maison sur 7-Zip.

## Règles

- Le principe : l'utilisateur doit avoir l'impression d'utiliser Nautilus, sans ses lacunes. Même
  apparence, mêmes gestes, mêmes libellés ; ce qui manque à Nautilus (double panneau, archives qu'on
  parcourt et dont on glisse le contenu, tri dans le menu du fond…) s'ajoute sans le dépayser.
- Répondre à Tof en français, tutoiement, avis francs. Messages de commit en français.
- Cible : Zorin OS 18.1 (Ubuntu 24.04, GNOME 46, X11, GTK 4.14, libadwaita 1.5). Pas d'API plus récente.
  Validé aussi par des utilisateurs sous Wayland (Zorin 18) ; Ubuntu 26.04 (GNOME 50, Wayland seul) :
  premier essai rapide concluant (lancement, navigation), retours sur la durée attendus.
  Debian 13 (GNOME) : installé par `install.sh`, fonctionne (retour d'utilisateur) ; Debian 12 trop ancien
  (GTK 4.8, libadwaita 1.2).
  PCLinuxOS Debian sous KDE Plasma : le .deb resolute (Launchpad) s'installe et fonctionne, rendu correct
  (Adwaita sombre ; libadwaita ne suit pas le thème KDE, seulement clair / sombre) — vidéo d'un utilisateur.
  Pop!_OS 24.04 sous COSMIC 1.11 (base Ubuntu 24.04, PPA noble) : fonctionne, premier retour d'utilisateur
  (pas tout essayé).
  PPA : `tools/ppa.sh` envoie la même entrée du changelog pour noble (24.04) puis resolute (26.04,
  version « X~resolute1 », réécrite dans une copie) ; le changelog du dépôt ne vise que noble.
  Commit « Version X » poussé sur main (debian/changelog change) : `.github/workflows/release-deb.yml` construit le
  .deb sur ubuntu-24.04 (version « X~deb1 », plus ancienne que « X~noble1 » : le PPA prend le relais) et le
  publie dans les Releases (`odysseus_X_all.deb`, notes en anglais tirées de la fiche AppStream). Aussi
  lançable à la main (workflow_dispatch). Pour Debian 13, PCLinuxOS Debian… L'étiquette `vX` est créée par
  la publication (`gh release create --target`) : le clone HTTPS de Tof n'a pas d'identifiants pour pousser.
- Suivre le thème GTK du système : `data/base.css` ne fait que de la mise en page, aucune couleur fixe.
  La zone des fichiers utilise le fond / texte de la fenêtre (pas `view`), classe `nautilus-list-view`,
  lignes de liste compactes (~35 px).
- Dialogues : de vraies fenêtres comme Nautilus 46, pour que le thème du système (fond, transparence,
  bordure) s'y applique — `common.AlertDialog` (Adw.MessageDialog), `common.DialogWindow` (remplace
  Adw.Dialog, même usage), `Adw.PreferencesWindow`, `Adw.AboutWindow`. Jamais Adw.Dialog /
  Adw.AlertDialog (feuilles dessinées dans la fenêtre). Libellés des confirmations : ceux de Nautilus.
  La fenêtre de progression s'efface pendant une question « Remplacer ? ».
- Barre latérale : l'emplacement courant y est sélectionné ; jamais de bouton d'éjection pour les
  disques fstab ou internes (mêmes règles que Nautilus).
  Bureau et « Ce PC » en option (`sidebar_desktop`, `sidebar_this_pc`, suivis en direct par
  `Sidebar.fill_places`). « Ce PC » (`ui/this_pc.py`, `computer:///`) : page spéciale comme « Autres
  emplacements » (`pane.PAGES`, mode « other ») ; espace des disques lu par GIO en asynchrone.
  Ses tuiles portent les noms CSS de la grille des fichiers (`gridview > child.activatable`,
  `set_css_name`) : survol / sélection viennent du thème, comme dans la vue grille.
  Réglage `this_pc_folders` (idée d'un utilisateur, Préférences, grisé sans « Ce PC ») : coupé, la section
  « Dossiers » est masquée en direct (déjà dans la barre latérale), restent disques et réseau.
  « Ce PC » actif : « Autres emplacements » quitte la barre latérale (`Sidebar._sync_other`), donc
  Ce PC montre aussi le réseau découvert et la barre « Se connecter à un serveur » (`other_locations.
  connect_bar` / `discover_network`, partagés). Largeur de la barre latérale : assez pour lire en entier
  les emplacements fixes dans la langue et la police courantes (`MainWindow.fit_sidebar`, suivi en direct).
  Signets (`~/.config/gtk-3.0/bookmarks`, partagé avec Nautilus et Thunar) vers un dossier déjà affiché
  dans les emplacements : masqués comme dans Nautilus (Thunar y met Documents, Images…), jamais retirés
  du fichier.
- Chaque fonctionnalité issue d'une extension est déclarée dans `odysseus/features.py` et doit rester
  désactivable sans redémarrage (`features.enabled(id)` au moment de l'usage, pas à l'import).
- Tout texte visible passe par `tr()` / `ntr()` (jamais `_`). Après modification :
  `python3 tools/update-pot.py`, compléter `data/locale/en.po` et `de.po`, puis
  `msgfmt -c --statistics` et `msgcmp` (aucune chaîne non traduite, placeholders identiques).

## Lecture rapide des dossiers (ne pas casser)

- `backend/scanner.py` : `os.scandir` dans un fil ; au-delà de 4000 éléments les noms sont affichés
  avant le `stat` (`stat_all` en seconde phase). Tri en Python avec `scanner.collate_key`
  (`g_utf8_collate_key_for_filename` par ctypes, en octets : ordre naturel de Nautilus). Jamais par
  PyGObject : la clé vient de strxfrm et n'est pas toujours de l'UTF-8 (allemand : 0xed pour « ⽨ »…),
  le dossier entier devenait « Accès impossible » (`CollateKeyTest`).
- `scanner.scan_slot()` (verrou `SCAN_LOCK`) sérialise les lectures locales massives (panneaux,
  colonnes, comptage des dossiers). Sans lui, deux fils qui font des `stat` en parallèle se battent
  pour le GIL (« convoi ») : ×10 à ×50. L'attente est plafonnée à 0,3 s : un périphérique bloqué dans
  un `stat` ne gèle pas les autres panneaux. Les lectures GIO (réseau, corbeille, admin://) ne le
  prennent pas.
- `ui/model.py` : `DirModel` est un `Gio.ListModel` virtuel ; les `Entry` ne sont créés que pour les
  lignes affichées. `Selection` maison sur `Gtk.Bitset` (`difference` = différence symétrique,
  utiliser `subtract`).
- Au-delà de 2000 éléments, `items_changed` en deux temps (vider puis remplir) ; le contenu du modèle
  doit être cohérent avec chaque signal, sinon GTK plante (segfault).
- Grille = celle de Nautilus 46 (`nautilus-grid-cell.ui`, style.css) : case = icône + 18 + 2 + 16 (colonne
  des emblèmes) + 2 × 6, nom dans un `Adw.Clamp(maximum_size=0)` (il passe à la ligne sous l'icône,
  « … » au milieu), 18 px autour, 6 px entre les cases (`border-spacing`). `GRID_*` de views.py et
  base.css doivent rester d'accord : `max_columns` calculé = le choix de GTK avec max_columns=20.
- Seule la vue visible est branchée sur le modèle (`Pane._attach_views`). La grille ne reçoit son
  modèle qu'une fois dimensionnée (`GridView.attach`) et `max_columns` suit la largeur réelle :
  sinon Gtk.GridView crée 64 × 32 cases d'un coup (2 s figées sur 20 000 fichiers).
- Référence : un dossier de 100 500 fichiers est prêt en ~1 s, tout sélectionner ~0,4 s.

## Tests

Le `python3` du bac à sable n'a pas `gi` : utiliser `python3.12`.

```sh
python3.12 -m unittest discover -s tests            # moteur (sans GTK)
xvfb-run -a python3.12 tests/ui_smoke.py            # interface : doit finir par « Aucune erreur. »
LANGUAGE=en xvfb-run -a python3.12 tests/ui_smoke.py
LANGUAGE=de xvfb-run -a python3.12 tests/ui_smoke.py
xvfb-run -a -s "-screen 0 1600x1000x24" python3.12 tests/ui_mouse.py   # vrais clics (xdotool)
ODYSSEUS_UI_GVFS=1 dbus-run-session -- xvfb-run -a python3.12 tests/ui_smoke.py   # + corbeille GVfs
```

Avec `ODYSSEUS_UI_GVFS=1`, ui_smoke lance lui-même gvfsd sur son HOME jetable (un gvfsd lancé à part
garde le vrai HOME et montre la vraie corbeille) ; sans, les étapes qui lisent trash:/// sont sautées.
Les tests de corbeille du moteur retirent de la vraie corbeille ce qu'ils y ont mis (`_purge_trash`).

`tests/test_destructive.py` : remplacer, fusionner, annuler, déplacer entre deux systèmes de fichiers,
liens symboliques, corbeille — sur un arbre jetable, avec un dossier « précieux » qui ne doit jamais
changer. Règles : un déplacement ne supprime à la source que ce qui a été copié (`_prune_moved`) ;
« remplacer » copie sous un nom temporaire dans le dossier de destination (renommage atomique),
`fsync` des fichiers puis du dossier, puis échange (`_swap_into`) ; l'ancien reste si ça échoue.
Dossier remplacé : `renameat2(RENAME_EXCHANGE)` (ctypes, `_exchange`), le nom visé existe à tout
instant ; repli en deux renommages là où ce n'est pas géré (FAT, FUSE : testé sur ntfs-3g).
Annulation testée en annulant après N octets copiés (pas de délai fixe : la copie va trop vite).
Même élément vu à travers un lien symbolique (`Docs-lien → Documents`) : comparé par `_real_location`
(parent résolu + nom), jamais par `abspath` — sinon déplacer « sur lui-même » vidait le dossier.
Remplacement abandonné (erreur dans la copie temporaire) : `_Copier._forget` retire ses fichiers de
`copied`, sinon `_prune_moved` les supprimait de la source alors qu'ils n'existaient plus nulle part.
Opérations simultanées (une par fenêtre) vers le même nom : vérifier puis créer laissait une course.
Création exclusive (`O_EXCL`, liens compris) → `_Taken` → `_Copier.place` repose le conflit ;
renommages par `_rename_new` (renameat2 `RENAME_NOREPLACE`) ; sorties nouvelles (archives, 7z
compris via un nom temporaire, PDF, extraction, volumes) posées par `fileops.place_new` (« nom (2) »
si pris entre-temps) ; `os.replace` seulement pour un remplacement voulu (choix « Remplacer », PDF
modifié sur place). Boucles de reprise bornées (vfat : lexists dit libre, le noyau dit pris).
Testé par `ConcurrentTest` / `ConcurrentArchiveTest` : barrière après la vérification « libre »
(avant : contenus mélangés, premier fichier écrasé, sources de déplacement supprimées).
Odysseus tué net (SIGKILL) : un fichier copié est écrit sous un nom caché (`_tmp_name`) puis
renommé d'un coup une fois complet (`copy_file`), jamais de fichier tronqué sous le vrai nom ;
compression « supprimer après » : jamais `-sdel` (7-Zip supprimait avant que l'archive soit posée),
sources retirées par `archive._delete_sources_after` une fois l'archive en place. `KilledTest` tue
un vrai processus au pire moment.
Coupure de courant (le cache du noyau est perdu, pas seulement le processus) : un original n'est
supprimé qu'après `fileops.flush_to_disk` (syncfs du disque de destination) — déplacement vers un
autre disque (par lots de 64 éléments : un syncfs par élément coûtait cher sur une clé), « supprimer
après » de la compression, de l'extraction et du découpage en volumes. `PowerLossOrderTest` vérifie
l'ordre : mise sur disque, puis suppression.

Supports amovibles (`backend/fsinfo.py`, type lu dans `/proc/self/mountinfo`, sans `stat`) : vers
FAT / exFAT / NTFS, noms nettoyés (`: ? * " < > | \\`, points et espaces finaux → `safe_name`),
deux noms qui ne diffèrent que par la casse dans une même copie → le second est renommé, liens
symboliques refusés avec un message clair (la source reste), FAT32 : fichier > 4 Go refusé avant
d'écrire. `FatKeyTest` monte une vraie image `mkfs.vfat` avec `fusefat -o rw+` (pas de module vfat
dans le bac à sable ; fusefat refuse les accents, n'a pas d'inodes stables — pas de `samefile` — et
perd le contenu d'un dossier non vide renommé : ne pas y tester les remplacements de dossier).
Noms nettoyés qui se rejoignent (`a:b.txt` / `a_b.txt`, `fin.` / `fin`) : même renommage « (2) ».
Vu sur une vraie clé (vfat du noyau monté en utf8 par udisks) : `lexists("Photo.jpg")` répond non
alors que `photo.jpg` existe, puis la création échoue (« Le fichier existe »). Sur ces supports, les
conflits se décident sur la liste du dossier en casefold (`_Copier._existing`), jamais sur lexists ;
`_kernel_vfat_lookup` imite ce comportement dans les tests (fusefat, lui, est insensible à la casse).
Vue : un élément arrivé dans un dossier vide doit faire quitter la page « Dossier vide »
(`Pane._on_items_changed`, testé dans ui_smoke).
La page « Dossier vide » a son propre clic droit (`Pane._on_empty_click`) : elle remplace la vue, qui
portait seule le menu du fond (Coller, Nouveau dossier…) ; testé avec de vrais clics dans `ui_mouse.py`.

`tests/test_archive_safety.py` : archives piégées (`../`, chemins absolus, liens vers l'extérieur puis
écriture à travers, liens durs, fichiers spéciaux, setuid) extraites par le vrai moteur dans chaque
mode : rien ne doit changer hors du dossier d'extraction. C'est 7-Zip (23.01, celui d'Ubuntu 24.04)
qui protège ; il renvoie une erreur quand il IGNORE un lien dangereux alors que tout le reste est
extrait : `archive.skipped_links` reconnaît ce cas (on garde, on prévient). Il réécrit aussi les liens
absolus d'un tar vers l'intérieur du dossier de sortie (notre dossier temporaire caché) :
`_restore_absolute_links` leur rend leur cible une fois l'extraction finie.
Toute extraction passe par un dossier temporaire caché dans la destination, puis est rangée par
nous (`_merge_into` : un lien déjà présent à destination n'est jamais suivi ; 7-Zip, lui, écrivait à
travers). « Supprimer l'archive après » : gardée si des éléments ont été ignorés (`archive_kept`).
Sorties qui n'écrasent jamais : `archive.free_output` (« nom (2) », premier volume `.001` compris ;
en 7z / zip, `update_mode="new"` par défaut, la mise à jour d'une archive existante reste un choix
explicite du dialogue), fichier temporaire `_tmp_name` (jamais un « X.part » existant), Éclater PDF,
Pages / filigrane (`pdf_dialogs._free_output`, au lancement). Compression + « supprimer après » :
les fichiers illisibles (absents de l'archive) restent (`_remove_sources(keep=…)`).

Audit de novembre 2026 (agents, corrigé, tests `AuditFileopsTest` / `AuditArchiveTest`) : écriture
courte vers un autre disque complétée (boucle sur `os.write`) + taille vérifiée avant de poser le fichier ;
« Remplacer » sur FAT note le nom (`_created`, sinon la casse suivante écrasait) ; nouveau dossier créé
en exclusif (`copy_tree(fresh=True)` → `_Taken`, deux copies ne fusionnent plus) ; déplacement en partie
raté : l'annulation ramène les éléments partis (`_moved_parts`), jamais « annuler une copie » (corbeille) ;
`move_new` vers un autre disque sans `shutil.move` (nom caché, mise sur disque, puis suppression) ; EXDEV
sur un même st_dev (bind, mergerfs) → copie. Archives, « supprimer après » : rien n'est supprimé avec des
exclusions / options 7-Zip, en « Rafraîchir », ou si l'archive est dans un dossier compressé
(`_keep_sources_reason`, message « Originaux conservés ») ; extraction : l'archive (même inode,
`_protected`) n'est jamais remplacée ni prise pour un élément extrait ; `-spd` pour les noms passés à 7-Zip
(`*.txt` est un nom). Signets : URI comme `g_file_get_uri` (`to_uri`, comparées par chemin `_same`),
lecture tolérante. Réglages d'un mauvais type ignorés (`settings._fits`), sort.json / share-permissions.json
non-dict ignorés. `net usershare <cmd> -- …`.
Noms hostiles : titres / sous-titres de libadwaita (ActionRow, PreferencesGroup, StatusPage, Toast) lisent
du BALISAGE par défaut — un nom « Tom & Jerry » s'affichait vide. Nom de fichier dedans : `markup_safe()`
(échappé + `shown`), ou `use_markup=False` posé AVANT le sous-titre (au constructeur, il est lu avant) ;
`toast()` est en texte brut, `alert()` passe par `shown`. Corbeille : `scanner.trash_orig_path` (un seul nom
non UTF-8 faisait lever PyGObject, page Corbeille et annulation cassées). Vue en colonnes → liste : le
modèle est relu (`leaving_columns`), il n'avait suivi que le NOM du dossier ouvert dans les colonnes.
Archives : valeurs de `7z -slt` gardées telles quelles (espaces de tête / fin), sortie décodée en
surrogateescape (noms non UTF-8 extractibles), « ./ » des tar sans faux dossier « . » (chemins réels gardés
pour 7-Zip), `-snl` en 7z / zip (liens stockés comme liens), `volume_set` connaît .partN.rar, .rNN, .zNN ;
extraction ratée : pas de dossier vide laissé ; cache du glisser limité à la sélection + éléments glissés
(`DragCache._evict`), `odysseus-view-*` balayés au démarrage. Fermeture : `MainWindow._teardown` (panneaux
retirés, réglages / opérations débranchés, `Sidebar.stop`), `Pane._cleanup` arrête la recherche et les pages
spéciales, Autres emplacements branche ses moniteurs à l'affichage ; `SpringLoad` reçoit le contrôleur (la
cellule restait retenue) ; grille non affichée : attend « map » au lieu d'un minuteur ; `scroll_to` ignore
une position hors du modèle. Testé : `fermeture_sans_fuite` d'ui_smoke, `Audit*Test` de test_archives.

`workers.Pool` : un fil bloqué dans un appel système ne se tue pas ; si tous sont pris depuis plus
de 3 s, un fil est ajouté (6 au plus).

`tests/ui_smoke.py` pilote l'interface par des générateurs exécutés dans la vraie boucle GLib
(une boucle imbriquée dans un timeout ne traite pas les sources idle).

Captures (Gtk.WidgetPaintable) : `xvfb-run -a python3.12 tools/screenshot.py SORTIE.png [--dual]
[--other=DOSSIER] [--grid] [--columns] [--preview] [--select=NOMS] [--action=ACTION] [--prefs=PAGE]
[--dark] [--delay=S] [--size=LxH] [--rename=MODÈLE]` (`--rename` avec `--action=win.rename` : modèle
rempli, « {num2} » = balise 01, 02…) ; captures de la documentation dans `docs/captures/`, faites sur le
dossier personnel de démonstration de `tools/demo-home.py` (HOME pointé dessus, LANGUAGE=fr,
settings.json remis à zéro avant chaque capture : la vue et le double panneau sont mémorisés).
Les dialogues ouverts (fenêtres posées sur la principale) sont composés au centre de la capture.
Site de présentation (`site/index.html`, anglais ; en ligne sur https://tofmicro.pages-perso.free.fr/Odysseus/, envoyé par FTP
à la fin de `tools/ppa.sh` via `tools/site.sh` — lftp, identifiants dans ~/.netrc de Tof, tout renvoyé (`--transfer-all` : les dates trompaient), jamais de suppression ; version affichée dans la pastille et l'accroche, à tenir à jour ; installation par le PPA,
liens GitHub et Launchpad ; « Copy » ne copie que les deux commandes d'installation) : captures de
`site/img/` faites en anglais et en sombre (`--dark`, sauf `light.png`) sur `tools/demo-home.py
/home/tof --en` (chemin court : il s'affiche dans la recherche) ; `--search=… --replace=…` capture la
fenêtre de recherche dans le contenu.

## Pièges connus

- Actions de fenêtre : préfixe `win.` ; l'application gère la ligne de commande (`HANDLES_COMMAND_LINE`).
- Lanceur installé : `python3 -P -c …` (sans -P, le dossier courant était dans les chemins d'import :
  lancé depuis un dossier supprimé, l'import de gi plantait ; un « gi.py » du dossier courant aurait
  été exécuté). Ligne de commande : `app._cwd()` (dossier personnel si le dossier courant n'existe plus).
- Verdict de comparaison d'empreinte : `data/verdict.css`, priorité USER+1 (au-dessus du gtk.css du
  thème), `background-image: none` (thèmes à boutons en dégradé).
- Nom du processus : `app._process_name` (prctl PR_SET_NAME) → « odysseus » dans le Moniteur
  système, `ps`, `killall` (sinon « python3 », le lanceur étant `python3 -c …`). WM_CLASS :
  `GLib.set_prgname(APP_ID)`.
- Fenêtre de progression : `ProgressWindow._fit` recalcule la hauteur quand le nombre d'opérations
  change ou à la réouverture (GTK garde la taille d'une fenêtre masquée).
- `.desktop` en `DBusActivatable=true` : GIO (dock, `gio open`, autres applications) passe par D-Bus et
  le bus lance Odysseus (`--gapplication-service`, fichier de service écrit par `install.sh`) avec
  l'environnement de la session — un lanceur hors session (script udev) n'a plus besoin de DISPLAY /
  XAUTHORITY. Les actions du .desktop sont alors activées par leur nom : `app.new-window`, `app.dual`.
- Préférences → Général → « Gestionnaire de fichiers par défaut » : UN interrupteur (`defaultapp.py`, idée
  reprise de « Mira ») = `inode/directory` → notre .desktop dans mimeapps.list (comme `xdg-mime default`, la
  commande est écrite dans le sous-titre) + `dbus_file_manager` ; coupé : Nautilus redevient l'application
  par défaut. État affiché = celui du système. L'ancien interrupteur « Répondre à Afficher dans le dossier »
  a disparu (le réglage `dbus_file_manager` reste, piloté par celui-ci). Testé : `gestionnaire_par_defaut`.
  Validé chez Tof dans les deux sens (1.7.6). La Corbeille du dock ne suit le changement qu'après une
  reconnexion (dash-to-dock garde l'application de la corbeille en mémoire) : normal, pas un bug à chercher.
  Au démarrage, `defaultapp.reconcile` : Odysseus par défaut mais `dbus_file_manager` faux (vieux settings.json
  remis après un Timeshift, vu chez Tof : Firefox et le dock repartaient chez Nautilus, interrupteur « actif »)
  → remis à vrai. Jamais dans l'autre sens (FileManager1 activé à part avant 1.7.6 : on n'y touche pas).
- « Afficher dans le dossier » (org.freedesktop.FileManager1, option `dbus_file_manager`) : Odysseus
  fermé, le bus lançait Nautilus (son service dans /usr/share). Option active, `dbus.sync_service`
  écrit le nôtre dans ~/.local/share/dbus-1/services (prioritaire, 1re ligne `# Odysseus`) ; option
  coupée, il est retiré et le nom rendu, sans redémarrage. Un fichier qui n'est pas à nous : jamais touché.
  `ShowItemProperties` (« Propriétés » du bureau DING, Zorin et Debian) : seule la fenêtre des propriétés,
  comme Nautilus (`FileManager1.show_properties`, sans parent, rattachée à l'application) ; Odysseus fermé :
  MainWindow CACHÉE comme hôte, détruite avec elle sauf si « Ouvrir le dossier parent » l'a montrée. Avant,
  la fenêtre principale venait au premier plan derrière. `app._window` ne renvoie qu'une MainWindow.
  Testé : `proprietes_depuis_le_bureau` d'ui_smoke.
- FileManager1 publie aussi `OpenWindowsWithLocations` (a{sas}, propriété de Nautilus hors norme) : chemin
  D-Bus de chaque MainWindow (`<app>/window/<id>`, = _GTK_WINDOW_OBJECT_PATH vérifié avec xprop) → URI des
  onglets, republiée (PropertiesChanged) à l'ouverture / fermeture d'une fenêtre, d'un onglet, à chaque
  changement de dossier. dash-to-dock (fileManager1API.js) s'en sert : sa Corbeille passe en STARTING au
  clic et n'en sort que sur cette propriété ; sans elle, après le premier clic, plus rien ne se passait
  (activate_full n'a pas de cas STARTING). Demande l'interrupteur « Gestionnaire de fichiers par défaut ».
  Validé chez Tof (Zorin 18, 1.7.4) : clics répétés OK, et l'icône du dash passe bien en « pleine ».
- Glisser au-dessus d'un dossier (vue, barre latérale sauf Corbeille, barre de chemin) l'ouvre après
  500 ms comme Nautilus 46 (`common.SpringLoad`, réglage `dnd_hover_open`) : minuteur lancé seulement
  après un seuil de glisser dans la cible, relancé tant que le pointeur bouge. Testé avec de vrais
  glisser dans `ui_mouse.py` ; y déposer au milieu (la notification « déplacé — Annuler » du dépôt
  précédent, en bas de la fenêtre, n'accepte pas de fichiers).
- Recherche par nom comme Nautilus (`nautilus_query_matches_string`) : le texte est coupé en mots, chacun
  doit être dans le nom (accents et casse ignorés, ordre libre) — « fol sy » trouve « kfoldersync.svg »
  et « folder-symbolic.svg » (`model.query_words` / `matches` : filtre du dossier, recherche dans les
  sous-dossiers, filtre de la fenêtre d'archive).
- Recherche comme Nautilus : le meilleur résultat est sélectionné d'office (`Pane.select_best` : nom
  identique, puis qui commence par le texte, puis le plus court), Entrée dans le champ l'ouvre
  (`_on_search_activate`) ; ouvrir un autre dossier que celui de départ (`_search_origin`) ferme la
  recherche, sinon le filtre restait posé sur le nouveau dossier.
- Recherche : ↓ / ↑ dans le champ passe le focus à la liste sur le meilleur résultat (`MainWindow._on_search_key`),
  puis les flèches y naviguent.
- Taper pour aller au fichier (réglage `type_ahead`, coupé par défaut : sinon taper lance la recherche
  comme Nautilus, `search_bar.set_key_capture_widget`) : `MainWindow._on_type_ahead`, en capture avant les
  raccourcis (Retour arrière ne fait pas « Précédent »), seulement si le focus est dans la vue du panneau ;
  `/` et `~` restent des raccourcis. `Pane.type_ahead` : premier nom qui commence par le texte (accents
  ignorés), liste entière, encart `.type-ahead` en bas ; 1 s sans frappe → on repart de zéro.
  Dossier ouvert depuis Autres emplacements / Ce PC : la vue prend le focus (`Pane._focus_from_page`,
  seulement en quittant une de ces pages : sinon elle volait le focus à l'autre panneau), sinon il
  restait sur la ligne cliquée (invisible) et Entrée rouvrait « / ». GTK remonte aussi parfois le focus
  sur un conteneur (GtkPaned) ou nulle part : `Pane.lost_focus` ; la frappe donne alors le focus à la vue.
- Barre de chemin en saisie : suggestions des sous-dossiers comme Nautilus (`PathBar._update_suggestions`,
  lecture du dossier dans un fil, 10 au plus) dans un Gtk.Popover qui ne prend jamais le focus
  (`autohide=False`, `can_focus=False`) ; touches en capture sur le champ (sinon le texte prend les
  flèches) : ↑/↓ choisir, Tab / clic compléter, Entrée y aller, Échap ferme d'abord la liste.
- `Adw.TabView` capte Ctrl+Tab : `remove_shortcuts(...)` pour l'utiliser entre panneaux.
- D-Bus : `conn.register_object(path, info, method_cb, None, None)` (pas de `register_object_with_closures`).
- Double panneau : la position du séparateur se calcule avec `paned.get_width()`, et le ratio n'est
  mis à jour que pendant un glisser de l'utilisateur.
- Ouvrir un fichier : `common.launch_default` (Gio.AppInfo par défaut du type, lancée directement,
  comme Nautilus). Pas `Gtk.FileLauncher` en premier : il passe par le portail, qui affiche
  « Ouvrir avec… » sous GNOME même quand une application par défaut existe (repli seulement).
  Plusieurs fichiers : `common.launch_defaults` les regroupe par application par défaut, un seul
  lancement chacune (VLC met tout dans sa file), dans l'ordre naturel des noms (`name_order`) ;
  double-clic sur un fichier d'une sélection multiple = toute la sélection (`_on_pane_activated`).
  GTK 4.14 réduit la sélection au relâchement du 1er clic (`set_selection`) : `Selection` diffère cette
  réduction (`clicking`, posé par le geste en capture de `Pane`) jusqu'au délai du double-clic, annulée
  par l'activation ou un glisser ; testé avec de vrais clics dans `ui_mouse.py`.
  VLC (« une seule instance ») déjà ouvert démarrait la lecture sur un fichier au hasard de la liste :
  ses fichiers lui sont passés en une liste .m3u (`_vlc_playlist`, ~/.cache/odysseus/playlists),
  lue depuis le début dans tous les cas (vérifié avec VLC 3.0.20).
  Menu comme Nautilus : « Ouvrir avec <app par défaut> » puis « Ouvrir avec… »
  (`dialogs.AppChooser` : `Gtk.AppChooserWidget`, le composant de Nautilus 46, déprécié mais présent
  en GTK 4.14 ; ses titres de section viennent des traductions de GTK ; sa treeview a un fond `view`
  opaque : rendu transparent dans base.css, `.odysseus-app-chooser`).
- Raccourcis : GTK 4 traite ceux de l'application AVANT le widget qui a le focus. Ceux qui ont un
  sens dans un champ (Suppr, Retour arrière, `/`, `~`, Ctrl+A/X/C/V/Z…) sont renvoyés au texte qui a
  le focus par un contrôleur clavier en capture (`MainWindow._on_text_key`, qui passe avant les
  raccourcis) — jamais dans l'action : un clic sur « Précédent » doit naviguer ; testé dans `ui_mouse.py`.
  « / » et « ~ » se tapent avec Maj / AltGr en AZERTY (clavier de Tof) et QWERTZ : pour eux, Maj ne
  compte pas ; `ui_mouse.py` les tape aussi avec `setxkbmap fr` et `de`.
  Ctrl+1 / 2 / 3 (vues) et Ctrl+0 : en AZERTY, la rangée des chiffres donne `& é " à` sans Maj — raccourcis
  doublés (`<Ctrl>ampersand`…, plus le pavé numérique), sinon rien ne se passait chez Tof ; testé en fr / de.
- Renommage multiple (F2 / « Renommer… » sur plusieurs éléments locaux) = dialogue de Nautilus 46
  (`ui/batch_rename.py`, moteur `backend/batchrename.py`, testé par `test_batchrename` et ui_smoke) :
  modèle (texte + balises « [Nom du fichier original] », « [01, 02, 03] »… effacées d'un bloc,
  extension gardée), ordre de numérotation, ou rechercher / remplacer ; aperçu, conflits (nom non
  unique, nom pris par un élément non renommé ; casse ignorée sur FAT / NTFS), bouton grisé tant qu'il
  en reste. Métadonnées lues dans un fil (EXIF maison pour JPEG / TIFF / RAW, ffprobe pour l'audio /
  la vidéo) ; une balise n'est proposée que si TOUS les éléments l'ont. `batchrename.apply` : chaînes
  dans le bon ordre, permutations par un nom caché, `_rename_new` (jamais d'écrasement), un élément en
  erreur garde son nom. Une seule entrée d'annulation (« batch-rename »).
- Aperçu rapide (fonctionnalité `quick_look`) = Sushi piloté comme par Nautilus 46 (`ui/sushi.py`,
  D-Bus org.gnome.NautilusPreviewer2 : ShowFile(uri, « x11:<xid> », fermer si visible), propriété
  Visible, signal SelectionEvent(GtkDirectionType) → `Pane.move_selection`). Espace pris en capture par
  `MainWindow._quick_look` (avant type-ahead, sauf frappe en cours) ; l'aperçu ouvert suit la sélection
  de la fenêtre qui l'a ouvert (`previewer.owner`), clavier resté dans Odysseus compris (Sushi ne prend
  pas toujours le focus sous X11). Visible : d'après les ANNONCES de Sushi (PropertiesChanged écouté
  nous-mêmes, NameOwnerChanged = fermé, une flèche de Sushi = ouvert), jamais par Properties.Get — Sushi
  46 n'a pas de lecteur pour Visible, Get reste sans réponse (vu chez Tof avec dbus-monitor : flèches
  reçues, aucun ShowFile ensuite) — ni par le seul cache du proxy (vide quand Sushi démarre après lui).
  `fake_sushi.py` imite ça (Get sans réponse ; `--muet` : pas d'annonce non plus). Sushi présent = son fichier de service D-Bus (`lib:sushi`, features.LIBS),
  `gnome-sushi` en Recommends. Sous Wayland (Zorin 18), poignée « x11:0 » : Sushi s'ouvre centré, au
  premier plan, flèches comprises (deux retours d'utilisateurs) — pas besoin d'exporter une poignée
  Wayland. Testé avec `tests/fake_sushi.py` : ui_smoke sous `dbus-run-session`
  (sans bus de session, l'étape est sautée).
- Outils image (fonctionnalité `image_tools`, reprise de nautilus-image-converter + conversion) :
  `backend/images.py` + `ui/image_dialog.py`, menu « Outils image » (redimensionner / pivoter / convertir,
  combinables en une passe d'ImageMagick 6 : `convert`, `magick` si présent). Entrée avec sélecteur
  explicite (`[0]`, GIF animé → GIF : `[0-99999]` + coalesce) sinon « photo [1].jpg » est mal lu ;
  chemin absolu (un « x:y.png » relatif = format « x »). `-auto-orient` d'abord (photos de téléphone).
  Sortie sous un nom caché puis `place_new` ; « remplacer » : même format → `os.replace` après fsync,
  autre format → nouvelle image posée et mise sur disque, puis original à la corbeille. Ubuntu 24.04 :
  HEIC / AVIF en lecture seule, et le décodeur HEIC (`libheif-plugin-libde265`) n'est que suggéré par
  libheif → Recommends du paquet, composant `lib:heif-libde265` (features.LIBS), message clair sinon.
  Testé par `tests/test_images.py` et ui_smoke.
- Propriétés d'un dossier : icône personnalisée comme Nautilus 46 (crayon → Gtk.FileDialog d'images,
  croix → retirée avec « Annuler »), dans metadata::custom-icon (`folder_colors.set_custom_icon` /
  `reset_icon` / `restore_icon`, repli JSON si GVfs n'écrit pas) : Nautilus la voit aussi.
- Propriétés = disposition de Nautilus 46 (`ui/properties.py`, Adw.NavigationView), fenêtre NON modale comme
  Nautilus (plusieurs à la fois, comparer une archive et l'original ; testé par de vrais clics), à la hauteur
  du contenu (`FittedPage` : Adw.PreferencesPage ne transmet pas sa hauteur ; défilement au-delà de 85 %
  de l'écran), nom pas sélectionné d'office : en tête icône, nom,
  « N éléments, totalisant X » / « Type, taille », espace libre ; dossier parent ; dates (« 21 octobre
  2026 à 22:27:04 », création par GIO time::created) ; ligne « Permissions » → page « Définir des
  permissions personnalisées » (accès de Nautilus : dossiers Aucun / Lister / Accès / Création et
  suppression, fichiers Aucun / Lecture seule / Lecture et écriture ; combinaison hors liste montrée
  avec le libellé de Nautilus ; groupe changeable parmi ceux de l'utilisateur), « Changer les permissions
  des fichiers inclus… » (`chmod_tree` : dossier + contenu, fichiers r/w seulement — l'exécution reste —,
  dossiers rwx, valeurs proposées d'après l'umask, liens jamais suivis ni modifiés). Ne recalculer que
  la catégorie modifiée (tout recalculer changeait les autres catégories), jamais de chmod / chown à
  travers un lien.
- Favoris (fonctionnalité `starred`, COUPÉE par défaut — choix de Tof, activable dans les Préférences) = ceux de Nautilus 46 : MÊME base TinySPARQL privée
  (`$XDG_DATA_HOME/nautilus/tags`, ontologie /usr/share/nautilus/ontology — copie identique dans
  data/nautilus-ontology si Nautilus est absent), mêmes requêtes que nautilus-tag-manager.c
  (`nautilus:File` / `nautilus:starred`, sujets = URI GIO), `backend/starred.py` (gir Tsparql ou
  Tracker 3.0 ; validé par un utilisateur sur Zorin 18 et Debian 13, dépendance tirée par le paquet). Base relue quand ses fichiers changent (moniteur) : une étoile de Nautilus apparaît ;
  Nautilus, lui, ne relit qu'à son démarrage. Seulement dans le dossier personnel (pas lui-même).
  Barre latérale « Favoris » sous Récents (`starred:///`, déposer dessus = marquer), page `mode
  "starred"` (liste statique + colonne Emplacement, vide : « Aucun fichier favori »), colonne étoile
  en fin de ligne (`star`, sans titre, nautilus-star-cell.c : starred / non-starred-symbolic ; une Gtk.Image +
  GestureClick comme Nautilus, jamais un bouton : sa hauteur minimale du thème grossissait toutes les lignes), menu
  « Marquer comme favori » (absent de Nautilus 46, qui marque par l'étoile) / « Démarquer » + toast
  « Annuler ». Pas de raccourci : Ctrl+D = signet, comme Nautilus. Déplacements / renommages (y compris
  annuler, renommage multiple) : `starred.moved` dans `_follow_moves` / `_follow_rename`. Testé :
  `StarredTest` (autre processus = Nautilus), étape `favoris` d'ui_smoke, vrai clic sur l'étoile
  (`star_steps` d'ui_mouse).
- « Colonnes visibles… » (menu des vues, `win.visible-columns`) = sélecteur de Nautilus 46
  (`ui/column_chooser.py`) : Nom en tête, poignée (glisser la ligne), interrupteur, menu « Déplacer vers le
  haut / bas » (pas « Monter » : déjà le libellé de « monter un disque », une seule traduction possible),
  changements appliqués tout de suite ; en bas « N'appliquer qu'au dossier actuel » → `backend/folder_columns.py`,
  MÊMES métadonnées que Nautilus (`metadata::nautilus-list-view-visible-columns` / `-column-order`, stringv ;
  ids name, size, type, date_modified, permissions, owner), nos colonnes inconnues de lui (durée) dans
  columns.json avec sa valeur du moment (il gagne s'il change) ; coupé = métadonnées retirées. Par défaut :
  settings `list_columns` (visibles) + `column_order`. `Pane.column_prefs` / `set_column_prefs` ;
  `views.column_sequence` (Compressé / Ratio des archives après Taille). En-tête glissé (ColumnView
  reorderable) : ordre enregistré (`ListView._save_column_order`, les colonnes cachées gardent leur place).
  Testé : `selecteur_colonnes` d'ui_smoke (aussi sous GVfs).
- Tri propre à chaque dossier comme Nautilus 46 (`backend/folder_sort.py`) : mêmes métadonnées GVfs
  (`metadata::nautilus-icon-view-sort-by` = name / size / type / date_modified, `-sort-reversed` =
  "true" / "false") ; nos autres tris (durée, droits…) dans `sort.json` avec la valeur de Nautilus du
  moment (s'il la change, il gagne) — jamais dans son attribut, sa vue en liste resterait sans tri.
  Distant / corbeille / archive : session seulement. `Pane.sort_place` / `place_sort` /
  `_take_place_sort` (avant le chargement) ; un choix (menu, en-tête, menu du fond « Trier ») ne vaut que
  pour ce dossier, les panneaux qui l'affichent suivent (`folder_sort.listeners`). `settings` sort_key /
  sort_reverse = tri par défaut (Préférences), plus changé par les menus. Actions `win.sort-*` : état
  = tri du panneau actif (`MainWindow._sync_sort_actions`).
- Fenêtre d'archive (réglage `archive_window`, par défaut) = Archive Browser de Tof, pas une deuxième
  Odysseus : double-clic, « Parcourir l'archive », F7 → `actions_files.browse_archive_path` →
  `ui/archive_window.ArchiveWindow` (Adw.ApplicationWindow, une par archive). À gauche l'arbre de
  l'archive (Gtk.TreeListModel + TreeExpander, dossiers dépliés sur place, filtre = liste plate avec le
  chemin), à droite `FolderPanel` (↑, chemin, nouveau dossier — bouton, Ctrl+Maj+N, clic droit —,
  accueil, actualiser ; suivi par un moniteur, la sélection survit aux rafraîchissements ; dépôt sur un
  dossier de la liste ou dans le dossier affiché). `ask_name(…, parent=)` : question posée sur la
  fenêtre d'archive, pas sur la principale. « Tout extraire » / « Extraire la sélection » vers le
  dossier de droite. Copies et extractions passent par la fenêtre principale (`host_window` :
  `drop_files`, `_extract_one` → conflits, progression, mots de passe). Réglage coupé : archive dans le
  panneau, comme avant.
  Glisser hors d'une archive (fenêtre d'archive et panneau) : `ui/archive_drag.DragCache` — la sélection
  est extraite d'avance en fond (350 ms après la sélection, ≤ 4 Go) dans un cache `odysseus-drag-*` ; au
  début du glisser GTK veut les fichiers tout de suite (extraire à ce moment figeait la fenêtre). Pas prêt :
  extrait sur le moment jusqu'à 64 Mo, sinon message + préparation. Glisser en COPY seulement.
  Archive quittée : extractions jamais glissées retirées ; glissées gardées (Nautilus peut encore copier
  depuis elles) jusqu'à `archive.sweep_drag_caches` au démarrage (> 12 h). Testé dans ui_smoke
  (`fenetre_archive`) et par de vrais glisser (vers la fenêtre principale et vers un dossier du panneau
  de droite) dans `ui_mouse.py`.
- GTK 4.14 / Gtk.ColumnView : NE JAMAIS changer la visibilité d'une colonne d'une vue remplie (une
  cellule libérée reste dans la liste de la colonne → segfault à la suppression suivante d'une ligne,
  `gtk_column_view_column_queue_resize`). `ListView.update_columns` reconstruit la vue avec les
  bonnes colonnes. Symboles de libgtk-4 : `.ddeb` sur launchpad.net (même Build ID) + gdb.
- Ne jamais rebrancher une vue sur son modèle PENDANT un signal items-changed (GtkListItemManager
  est en plein traitement) : `Pane._on_items_changed` passe par `idle_add`.
- Panneau retiré (sortie du double panneau, onglet fermé) : `Pane.retire()` (réglages, presse-papiers,
  modèle débranché). « destroy » n'arrive pas tant que Python garde une référence.
- Gestionnaires des cellules : ne capturer ni la cellule ni le Gtk.ListItem dans une fermeture
  (`controller.get_widget()`, `widget._rec`).
- Menus contextuels (`common.popup_menu`) : un seul Gtk.PopoverMenu par widget, gardé et réutilisé,
  jamais détruit à la fermeture. Détruit après Échap (souris dessus), GTK 4.14 gardait une de ses
  entrées comme widget sous le pointeur et la mise en page suivante de la fenêtre plantait
  (`gtk_window_native_layout` → `gdk_surface_request_motion(NULL)`) ; testé dans `ui_mouse.py`.
  Menu ouvert à l'appui du clic droit (comme Nautilus) : l'ouvrir au relâchement laissait le glisser
  du clic droit lancer un lasso (GtkColumnListView) que GTK ne retirait plus. Clic droit maintenu puis
  relâché sur une entrée : « Broken accounting of active state » — bénin, bug de GTK 4.14 reproduit
  sans Odysseus.
- Jamais `GLib.idle_add(champ.grab_focus)` : grab_focus renvoie True, la source est rappelée sans fin,
  le texte est resélectionné en boucle (une seule lettre possible dans le champ) et le processeur
  tourne. `common.focus_later(widget)` ; un test interdit le motif. Toute fonction passée à
  `idle_add` / `timeout_add` doit renvoyer False (sauf répétition voulue).
- GSConnect (`backend/gsconnect.py`) : pas de lien vers nautilus-gsconnect.py ; on suit directement
  l'ObjectManager D-Bus de GSConnect (appareils en direct) et on envoie par l'action « shareFile »
  de l'appareil (`Gio.DBusActionGroup`). Testé avec `tests/fake_gsconnect.py` sous dbus-run-session.
  « Courriel… » : `xdg-email --attach …` (logiciel de courriel par défaut).
- Sommes de contrôle (fonctionnalité `checksums`) : `tools.expected_digest` lit l'empreinte collée
  (seule, avec le nom, format BSD, en groupes) ; `compare_digest` calcule en une lecture tous les
  algorithmes de cette longueur (64 : SHA-256, SHA3-256, BLAKE2s…). `verify_hash_file` renvoie des
  états (ok, mismatch, missing, unreadable) traduits par `checksum_dialog`.
- Installateur .deb : `pkexec env DEBIAN_FRONTEND=noninteractive apt-get install …` (`debpkg.install_command`),
  stdin fermé ; sans ça, debconf sans terminal affichait « unable to initialize frontend: Dialog /
  Readline… » (bénin mais inquiétant, reproduit avec apt-utils). Lignes d'avancement de dpkg
  (« (Lecture de la base… 45% ») écartées (`debpkg.is_progress_line`).
- RAR : le 7z d'Ubuntu / Debian n'a pas le décodeur RAR (licence) — la liste se lit, l'extraction des
  fichiers compressés dit « Unsupported Method » (les fichiers « stockés » passent : tester avec
  `rar a -m5`). Il vient de `7zip-rar` (multiverse ; Debian non-free) ou p7zip-rar, 7zz l'a déjà
  (`engine.rar_codec`). Sans lui : message avec `sudo apt install 7zip-rar` (`raise_for_7z`),
  composant « RAR » (`lib:7zip-rar`), Recommends du paquet, suggéré par install.sh. Les extensions de
  Tof passaient par `unrar`.
- Ghostscript `pdfimage24` : `-sCompression=JPEG` en majuscules.
- `view-column-symbolic` n'existe pas dans Adwaita : `icons.first_icon` avec repli.
- Partage réseau (fonctionnalité `samba_share`, comme nautilus-share) : `backend/usershare.py` (`net usershare`
  add / delete / info ; droits « Everyone:R,<moi>:F » ou « Everyone:F » ; `guest_ok` seulement si testparm
  « usershare allow guests ») + `ui/share_dialog.py` (clic droit sur un dossier → « Options de partage… »,
  libellés de nautilus-share). Erreurs reconnues : pas dans le groupe `sambashare` (« do not have
  permission » / « Permission denied ») → commande usermod + reconnexion ; smbd arrêté (« smbd is not
  running ») → systemctl. Noms comparés sans casse (Samba remplaçait « test » par « Test »). Droits du
  dossier manquants (invité : r-x autres, écriture : w autres) ajoutés après accord, notés dans
  share-permissions.json, retirés à l'arrêt ou quand l'écriture est retirée. Emblème `emblem-shared`
  depuis le cache (`icons.shared_paths`), relu en fond au plus toutes les 10 s (`refresh_soon`, jamais
  pendant le dessin). Testé avec la vraie Samba du bac à sable (`apt install samba`, `smbd -D`) :
  `UsershareTest`, étape `partage_samba` d'ui_smoke ; l'accès depuis une autre machine n'est pas testable ici.
  Présence de Samba = `/usr/sbin/smbd` (`lib:smbd`), pas `net` seul : `net` vient de samba-common-bin,
  installé sur bien des Zorin sans le serveur (vu chez Tof : « Samba ✓ » alors qu'il n'est pas installé).
- Fonctionnalité dont un programme indispensable manque (`features.blocked`, hors `GRACEFUL`) :
  interrupteur grisé et éteint dans Préférences → Fonctionnalités (`dialogs._lock_switch` : seul le
  Gtk.Switch interne est insensible, la ligne « ⚠ Manquant … » reste lisible) ; testé dans ui_smoke
  (`fonction_bloquee`).
- Lien cassé (cible absente) comme Nautilus : seul le `lstat` répond → `scanner.apply_stat` lui donne le
  type `inode/symlink` (`BROKEN_LINK`, celui de GIO), `Rec.is_broken_link`, libellé « Lien (brisé) »
  (`type_description`), icône des liens ; Propriétés : dates du lien, pas d'« Ouvrir avec » ; ouvert :
  question de Nautilus (`report_broken_link` : « Le lien … est cassé. Voulez-vous le mettre à la
  corbeille ? », sa cible dans le détail). Avant : il prenait le type de son nom (script Python, ouvrir
  avec File Roller…).
- Emblèmes comme Nautilus (lien, `emblem-readonly`, `emblem-unreadable`, puis Folder Color), à la suite
  après le nom en liste ; en grille, dans une colonne de 16 px à droite de l'icône comme Nautilus 46. Droits calculés depuis mode / uid / gid du `stat`
  (`icons._access`, aucun appel système par ligne). Cadenas seulement si le dossier parent est
  modifiable (dans / ou /usr : aucun cadenas, comme Nautilus) ; parent lu une fois par dossier
  (`_parent_writable`, cache de 5 s). Le bac à sable tourne en root (aucun
  emblème) : les tests posent `icons._ids = (1000, {1000})`.
- Corbeille comme Nautilus : colonne date = « Mis à la corbeille le » (`trash::deletion-date` → `trashed`,
  tri « date » dessus, par défaut les derniers d'abord : `Pane.place_sort`), « Emplacement d'origine »
  rempli depuis `trash::orig-path` (`scanner.trash_origin_label`, relatif au dossier personnel) — la
  colonne restait vide. Testé avec la vraie corbeille GVfs (`corbeille_colonnes` d'ui_smoke).
- Compteur à côté de « Corbeille » (réglage `sidebar_trash_count`, idée de Tof) : `Sidebar._setup_trash_row`,
  compté par nous (`trashwatch.expected_count`, toutes les corbeilles, pas GVfs), suivi par inotify sur les
  dossiers `files/` (aucune relecture périodique : un disque au repos le reste), recompté aussi après nos
  opérations et au montage d'un disque ; pastille aux couleurs d'accent du thème (`.trash-count`), icône
  pleine / vide comme Nautilus. `trashwatch.count_and_sync` : si GVfs annonce moins (trash::item-count),
  gvfsd-trash est relancé → le dash du bureau revoit les éléments des autres disques. Testé :
  `compteur_corbeille` d'ui_smoke, `TrashSyncTest`.
- Éjecter / démonter un support dont la corbeille n'est pas vide : question de Nautilus (« Souhaitez-vous
  vider la corbeille avant de démonter ? », Annuler / Ne pas vider la corbeille / Vider la corbeille),
  `Sidebar.eject` → `_ask_empty_trash` → `_do_eject`. Seule la corbeille de CE support est vidée
  (`trashwatch.empty_mount_trash` : .Trash-UID et .Trash/UID, contenu de files/ info/ expunged/, aucun
  lien suivi), par `_run` (progression, erreur → pas d'éjection). Idée d'un utilisateur (fork « Mira »).
  Testé : `MountTrashTest`, `ejecter_vider_corbeille` d'ui_smoke ; validé chez Tof avec une vraie clé (1.7.5).
  Retrait : vraie notification du bureau comme Nautilus (`sidebar.notify_unmount`, Gio.Notification, id
  « unmount ») — « Écriture des données sur X / Ne débranchez pas avant la fin » pendant le vidage du cache
  (signal `show-unmount-progress` du Gtk.MountOperation, bytes_left > 0), remplacée par « X peut être retiré
  en toute sécurité » ; échec : retirée. Sans bus / application non enregistrée : toast. Demandé par Tof (le
  toast passait inaperçu). Testé : `notification_retrait` d'ui_smoke (faux org.freedesktop.Notifications) ;
  validé chez Tof avec une vraie clé (1.8.2, Zorin 18), comme le sélecteur de colonnes.
- Corbeille / GVfs : au bout d'un moment, gvfsd-trash ne voit plus les `.Trash-1000` des autres disques
  (les fichiers y sont, trash:/// ne les montre plus ; `pkill gvfsd-trash` répare). `backend/trashwatch.py` :
  à chaque lecture de la corbeille, on compte nous-mêmes (éléments de `files/` ayant leur `.trashinfo`,
  disques réseau / FUSE exclus) ; s'il en manque, `pkill -u UID -x gvfsd-trash` (une fois par minute au
  plus) puis relecture. GVfs corrigé → comptes égaux, rien ne se passe. Le smoke test de la corbeille
  a besoin de GVfs : `dbus-run-session` + `/usr/libexec/gvfsd &` (sinon il est sauté).
- Boutons latéraux de la souris (8/9) : bug de GTK 4.14 (`gtkmain.c`, `handle_pointing_event`) — le
  verrou implicite du pointeur posé à l'appui n'est levé au relâchement que pour les boutons 1 à 5.
  Il reste accroché au widget visé (clic gauche suivant perdu, « Broken accounting of active state »
  quand la page change). Contournement dans `MainWindow._release_pointer_grab` : à l'appui, la
  fenêtre est rendue insensible puis sensible (GTK fait alors le ménage complet), puis navigation en
  `idle_add`. Événements lus par `Gtk.EventControllerLegacy` + `get_current_event()` (le paramètre
  `event` vaut None en Python). Test avec de vrais clics : `tests/ui_mouse.py` (xdotool).
