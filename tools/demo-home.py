#!/usr/bin/env python3
"""Fabrique un dossier personnel de démonstration pour les captures de la documentation.

    python3 tools/demo-home.py DOSSIER
    HOME=DOSSIER XDG_CONFIG_HOME=DOSSIER/.config XDG_CACHE_HOME=DOSSIER/.cache \\
        xvfb-run -a python3 tools/screenshot.py SORTIE.png DOSSIER/Documents …
"""
import math
import os
import random
import subprocess
import sys

import cairo

H = os.path.abspath(sys.argv[1])
random.seed(7)
DIRS = {"DESKTOP": "Bureau", "DOCUMENTS": "Documents", "DOWNLOAD": "Téléchargements", "MUSIC": "Musique",
        "PICTURES": "Images", "VIDEOS": "Vidéos", "TEMPLATES": "Modèles", "PUBLICSHARE": "Public"}


def write(path, data=b"", size=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data if data else os.urandom(size or random.randint(2_000, 900_000)))


def image(path, w, h, hue):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    s = cairo.ImageSurface(cairo.FORMAT_RGB24, w, h)
    c = cairo.Context(s)
    g = cairo.LinearGradient(0, 0, w, h)
    r, gg, b = (0.5 + 0.5 * math.cos(hue), 0.5 + 0.5 * math.cos(hue + 2.1), 0.5 + 0.5 * math.cos(hue + 4.2))
    g.add_color_stop_rgb(0, r, gg, b)
    g.add_color_stop_rgb(1, b * 0.6, r * 0.7, gg)
    c.set_source(g)
    c.paint()
    for _ in range(6):
        c.set_source_rgba(1, 1, 1, random.uniform(0.15, 0.4))
        c.arc(random.uniform(0, w), random.uniform(0, h), random.uniform(w / 12, w / 4), 0, 2 * math.pi)
        c.fill()
    s.write_to_png(path)


def pdf(path, title, pages=3):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    s = cairo.PDFSurface(path, 595, 842)
    c = cairo.Context(s)
    for i in range(pages):
        c.set_source_rgb(0.15, 0.25, 0.45)
        c.rectangle(0, 0, 595, 90)
        c.fill()
        c.set_source_rgb(1, 1, 1)
        c.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        c.set_font_size(26)
        c.move_to(40, 58)
        c.show_text(title)
        c.set_source_rgb(0.2, 0.2, 0.2)
        c.select_font_face("Sans")
        c.set_font_size(12)
        for line in range(30):
            c.move_to(40, 130 + line * 20)
            c.show_text("Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor."[:40 + line % 40])
        c.show_page()
    s.finish()


os.makedirs(os.path.join(H, ".config"), exist_ok=True)
with open(os.path.join(H, ".config", "user-dirs.dirs"), "w") as fh:
    for k, v in DIRS.items():
        fh.write(f'XDG_{k}_DIR="$HOME/{v}"\n')
        os.makedirs(os.path.join(H, v), exist_ok=True)

D = os.path.join(H, "Documents")
pdf(os.path.join(D, "Rapport annuel 2026.pdf"), "Rapport annuel 2026", 4)
pdf(os.path.join(D, "Factures", "Facture EDF septembre.pdf"), "Facture — septembre")
pdf(os.path.join(D, "Factures", "Facture Free octobre.pdf"), "Facture — octobre")
pdf(os.path.join(D, "Contrat de location.pdf"), "Contrat de location", 6)
for name in ("Budget familial.ods", "Lettre de motivation.odt", "Recettes.odt", "Présentation projet.odp"):
    write(os.path.join(D, name))
write(os.path.join(D, "Notes de réunion.md"), "# Réunion du 14 octobre\n\n- Odysseus : captures à jour\n".encode())
write(os.path.join(D, "liste-courses.txt"), "pain\nlait\ncafé\n".encode())
for sub in ("Projets/Odysseus/docs", "Projets/Odysseus/src", "Projets/Site web/images", "Projets/Pressoir",
            "Administratif/Impôts", "Administratif/Banque", "Administratif/Santé"):
    os.makedirs(os.path.join(D, sub), exist_ok=True)
for name in ("README.md", "CHANGELOG.md", "install.sh"):
    write(os.path.join(D, "Projets", "Odysseus", name), b"# Odysseus\n")
for name in ("main.py", "pane.py", "views.py"):
    write(os.path.join(D, "Projets", "Odysseus", "src", name), b"print('Odysseus')\n")
pdf(os.path.join(D, "Administratif", "Impôts", "Avis d'imposition 2026.pdf"), "Avis d'imposition")

I = os.path.join(H, "Images")
for i, name in enumerate(("Plage de Biarritz.png", "Coucher de soleil.png", "Montagne.png", "Forêt.png",
                          "Port de La Rochelle.png", "Lavande.png", "Neige.png", "Lac d'Annecy.png")):
    image(os.path.join(I, name), 640, 420, i * 0.8)
for i in range(5):
    image(os.path.join(I, "Vacances 2026", f"IMG_20260{7 + i % 2}{12 + i}_1{i}4502.png"), 600, 400, 3 + i * 0.6)
os.makedirs(os.path.join(I, "Captures d'écran"), exist_ok=True)

T = os.path.join(H, "Téléchargements")
src = os.path.join(I, "Vacances 2026")
subprocess.run(["7z", "a", "-bd", "-tzip", os.path.join(T, "Photos vacances.zip"), src], capture_output=True)
subprocess.run(["tar", "-czf", os.path.join(T, "odysseus-1.0.tar.gz"), "-C", os.path.join(D, "Projets"), "Odysseus"])
pdf(os.path.join(T, "Notice lave-linge.pdf"), "Notice d'utilisation", 8)
for name, size in (("zorin-os-18.1.iso", 3_000_000), ("pilote-imprimante.deb", 400_000),
                   ("facture-orange.pdf", 0)):
    if name.endswith(".pdf"):
        pdf(os.path.join(T, name), "Facture")
    else:
        write(os.path.join(T, name), size=size)

M = os.path.join(H, "Musique")
for name in ("Daft Punk", "Air", "Christine and the Queens"):
    os.makedirs(os.path.join(M, name), exist_ok=True)
for name in ("01 - Intro.mp3", "02 - Voyage.mp3", "03 - Retour.mp3"):
    write(os.path.join(M, "Air", name), size=random.randint(3_000_000, 6_000_000))
V = os.path.join(H, "Vidéos")
for name in ("Anniversaire Léa.mp4", "Randonnée.mkv"):
    write(os.path.join(V, name), size=2_000_000)
for name in (".bashrc", ".profile"):
    write(os.path.join(H, name), b"# shell\n")

os.makedirs(os.path.join(H, ".config", "gtk-3.0"), exist_ok=True)
with open(os.path.join(H, ".config", "gtk-3.0", "bookmarks"), "w") as fh:
    for p in ("Documents/Projets", "Images/Vacances 2026", "Documents/Administratif"):
        fh.write("file://" + os.path.join(H, p).replace(" ", "%20") + "\n")
# dates variées, comme un vrai dossier personnel (de quelques heures à un an)
import time  # noqa: E402
now = time.time()
for root, dirs, files in os.walk(H, topdown=False):
    if "/." in root[len(H):]:
        continue
    for name in files + dirs:
        age = random.choice((random.uniform(600, 20_000), random.uniform(86_400, 172_800),
                             random.uniform(3 * 86_400, 360 * 86_400)))
        t = now - age
        os.utime(os.path.join(root, name), (t, t), follow_symlinks=False)
print("dossier de démonstration prêt :", H)
