# Odysseus — notes de travail

Gestionnaire de fichiers GTK4 / libadwaita en Python, look Nautilus, intégrant les extensions de
`ToFpon/nautilus-extensions` (dépôt en lecture seule) ; moteur d'archives repris de Pressoir.

## Règles

- Répondre à Tof en français, tutoiement, avis francs. Messages de commit en français.
- Cible : Zorin OS 18.1 (Ubuntu 24.04, GNOME 46, X11, GTK 4.14, libadwaita 1.5). Pas d'API plus récente.
- Suivre le thème GTK du système : `data/base.css` ne fait que de la mise en page, aucune couleur fixe.
  La zone des fichiers utilise le fond / texte de la fenêtre (pas `view`), classe `nautilus-list-view`,
  lignes de liste compactes (~35 px).
- Barre latérale : l'emplacement courant y est sélectionné ; jamais de bouton d'éjection pour les
  disques fstab ou internes (mêmes règles que Pressoir / Nautilus).
- Chaque fonctionnalité issue d'une extension est déclarée dans `odysseus/features.py` et doit rester
  désactivable sans redémarrage (`features.enabled(id)` au moment de l'usage, pas à l'import).
- Tout texte visible passe par `tr()` / `ntr()` (jamais `_`). Après modification :
  `python3 tools/update-pot.py`, compléter `data/locale/en.po` et `de.po`, puis
  `msgfmt -c --statistics` et `msgcmp` (aucune chaîne non traduite, placeholders identiques).

## Lecture rapide des dossiers (ne pas casser)

- `backend/scanner.py` : `os.scandir` dans un fil ; au-delà de 4000 éléments les noms sont affichés
  avant le `stat` (`stat_all` en seconde phase). Tri en Python avec
  `GLib.utf8_collate_key_for_filename` (ordre naturel de Nautilus).
- `scanner.SCAN_LOCK` sérialise tous les `stat` massifs (panneaux, colonnes, comptage des dossiers).
  Sans lui, deux fils qui font des `stat` en parallèle se battent pour le GIL (« convoi ») : ×10 à ×50.
- `ui/model.py` : `DirModel` est un `Gio.ListModel` virtuel ; les `Entry` ne sont créés que pour les
  lignes affichées. `Selection` maison sur `Gtk.Bitset` (`difference` = différence symétrique,
  utiliser `subtract`).
- Au-delà de 2000 éléments, `items_changed` en deux temps (vider puis remplir) ; le contenu du modèle
  doit être cohérent avec chaque signal, sinon GTK plante (segfault).
- Seule la vue visible est branchée sur le modèle (`Pane._attach_views`).
- Référence : un dossier de 100 500 fichiers est prêt en ~1 s, tout sélectionner ~0,4 s.

## Tests

Le `python3` du bac à sable n'a pas `gi` : utiliser `python3.12`.

```sh
python3.12 -m unittest discover -s tests            # moteur (sans GTK)
xvfb-run -a python3.12 tests/ui_smoke.py            # interface : doit finir par « Aucune erreur. »
LANGUAGE=en xvfb-run -a python3.12 tests/ui_smoke.py
LANGUAGE=de xvfb-run -a python3.12 tests/ui_smoke.py
xvfb-run -a -s "-screen 0 1600x1000x24" python3.12 tests/ui_mouse.py   # vrais clics (xdotool)
```

`tests/ui_smoke.py` pilote l'interface par des générateurs exécutés dans la vraie boucle GLib
(une boucle imbriquée dans un timeout ne traite pas les sources idle).

Captures (Gtk.WidgetPaintable) : `xvfb-run -a python3.12 tools/screenshot.py SORTIE.png [--dual]
[--other=DOSSIER] [--grid] [--columns] [--preview] [--select=NOMS] [--action=ACTION] [--prefs=PAGE]
[--dark] [--delay=S] [--size=LxH]` ; captures de la documentation dans `docs/captures/`, faites sur le
dossier personnel de démonstration de `tools/demo-home.py` (HOME pointé dessus, LANGUAGE=fr,
settings.json remis à zéro avant chaque capture : la vue et le double panneau sont mémorisés).

## Pièges connus

- Actions de fenêtre : préfixe `win.` ; l'application gère la ligne de commande (`HANDLES_COMMAND_LINE`).
- `Adw.TabView` capte Ctrl+Tab : `remove_shortcuts(...)` pour l'utiliser entre panneaux.
- D-Bus : `conn.register_object(path, info, method_cb, None, None)` (pas de `register_object_with_closures`).
- Double panneau : la position du séparateur se calcule avec `paned.get_width()`, et le ratio n'est
  mis à jour que pendant un glisser de l'utilisateur.
- Ghostscript `pdfimage24` : `-sCompression=JPEG` en majuscules.
- `view-column-symbolic` n'existe pas dans Adwaita : `icons.first_icon` avec repli.
- Boutons latéraux de la souris (8/9) : bug de GTK 4.14 (`gtkmain.c`, `handle_pointing_event`) — le
  verrou implicite du pointeur posé à l'appui n'est levé au relâchement que pour les boutons 1 à 5.
  Il reste accroché au widget visé (clic gauche suivant perdu, « Broken accounting of active state »
  quand la page change). Contournement dans `MainWindow._release_pointer_grab` : à l'appui, la
  fenêtre est rendue insensible puis sensible (GTK fait alors le ménage complet), puis navigation en
  `idle_add`. Événements lus par `Gtk.EventControllerLegacy` + `get_current_event()` (le paramètre
  `event` vaut None en Python). Test avec de vrais clics : `tests/ui_mouse.py` (xdotool).
