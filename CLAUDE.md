# Odysseus — notes de travail

Gestionnaire de fichiers GTK4 / libadwaita en Python, look Nautilus, intégrant les extensions de
`ToFpon/nautilus-extensions` (dépôt en lecture seule) ; moteur d'archives repris de Pressoir.

## Règles

- Répondre à Tof en français, tutoiement, avis francs. Messages de commit en français.
- Cible : Zorin OS 18.1 (Ubuntu 24.04, GNOME 46, X11, GTK 4.14, libadwaita 1.5). Pas d'API plus récente.
- Suivre le thème GTK du système : `data/base.css` ne fait que de la mise en page, aucune couleur fixe.
  La zone des fichiers utilise le fond / texte de la fenêtre (pas `view`), classe `nautilus-list-view`,
  lignes de liste compactes (~35 px).
- Dialogues : de vraies fenêtres comme Nautilus 46, pour que le thème du système (fond, transparence,
  bordure) s'y applique — `common.AlertDialog` (Adw.MessageDialog), `common.DialogWindow` (remplace
  Adw.Dialog, même usage), `Adw.PreferencesWindow`, `Adw.AboutWindow`. Jamais Adw.Dialog /
  Adw.AlertDialog (feuilles dessinées dans la fenêtre). Libellés des confirmations : ceux de Nautilus.
  La fenêtre de progression s'efface pendant une question « Remplacer ? ».
- Barre latérale : l'emplacement courant y est sélectionné ; jamais de bouton d'éjection pour les
  disques fstab ou internes (mêmes règles que Pressoir / Nautilus).
  Bureau et « Ce PC » en option (`sidebar_desktop`, `sidebar_this_pc`, suivis en direct par
  `Sidebar.fill_places`). « Ce PC » (`ui/this_pc.py`, `computer:///`) : page spéciale comme « Autres
  emplacements » (`pane.PAGES`, mode « other ») ; espace des disques lu par GIO en asynchrone.
  Ses tuiles portent les noms CSS de la grille des fichiers (`gridview > child.activatable`,
  `set_css_name`) : survol / sélection viennent du thème, comme dans la vue grille.
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
  avant le `stat` (`stat_all` en seconde phase). Tri en Python avec
  `GLib.utf8_collate_key_for_filename` (ordre naturel de Nautilus).
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

`workers.Pool` : un fil bloqué dans un appel système ne se tue pas ; si tous sont pris depuis plus
de 3 s, un fil est ajouté (6 au plus).

`tests/ui_smoke.py` pilote l'interface par des générateurs exécutés dans la vraie boucle GLib
(une boucle imbriquée dans un timeout ne traite pas les sources idle).

Captures (Gtk.WidgetPaintable) : `xvfb-run -a python3.12 tools/screenshot.py SORTIE.png [--dual]
[--other=DOSSIER] [--grid] [--columns] [--preview] [--select=NOMS] [--action=ACTION] [--prefs=PAGE]
[--dark] [--delay=S] [--size=LxH]` ; captures de la documentation dans `docs/captures/`, faites sur le
dossier personnel de démonstration de `tools/demo-home.py` (HOME pointé dessus, LANGUAGE=fr,
settings.json remis à zéro avant chaque capture : la vue et le double panneau sont mémorisés).
Les dialogues ouverts (fenêtres posées sur la principale) sont composés au centre de la capture.
Site de présentation (`site/index.html`, anglais ; version 1.0 publiée : installation par le PPA,
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
- « Afficher dans le dossier » (org.freedesktop.FileManager1, option `dbus_file_manager`) : Odysseus
  fermé, le bus lançait Nautilus (son service dans /usr/share). Option active, `dbus.sync_service`
  écrit le nôtre dans ~/.local/share/dbus-1/services (prioritaire, 1re ligne `# Odysseus`) ; option
  coupée, il est retiré et le nom rendu, sans redémarrage. Un fichier qui n'est pas à nous : jamais touché.
- Glisser au-dessus d'un dossier (vue, barre latérale sauf Corbeille, barre de chemin) l'ouvre après
  500 ms comme Nautilus 46 (`common.SpringLoad`, réglage `dnd_hover_open`) : minuteur lancé seulement
  après un seuil de glisser dans la cible, relancé tant que le pointeur bouge. Testé avec de vrais
  glisser dans `ui_mouse.py` ; y déposer au milieu (la notification « déplacé — Annuler » du dépôt
  précédent, en bas de la fenêtre, n'accepte pas de fichiers).
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
- Propriétés → Droits : ne recalculer que la catégorie modifiée (les listes ne montrent pas « -w- »,
  « -wx »… : tout recalculer changeait les autres catégories).
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
- Ghostscript `pdfimage24` : `-sCompression=JPEG` en majuscules.
- `view-column-symbolic` n'existe pas dans Adwaita : `icons.first_icon` avec repli.
- Emblèmes comme Nautilus (lien, `emblem-readonly`, `emblem-unreadable`, puis Folder Color), à la suite
  après le nom en liste, sur l'icône en grille. Droits calculés depuis mode / uid / gid du `stat`
  (`icons._access`, aucun appel système par ligne). Cadenas seulement si le dossier parent est
  modifiable (dans / ou /usr : aucun cadenas, comme Nautilus) ; parent lu une fois par dossier
  (`_parent_writable`, cache de 5 s). Le bac à sable tourne en root (aucun
  emblème) : les tests posent `icons._ids = (1000, {1000})`.
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
