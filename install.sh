#!/bin/sh
# Installation d'Odysseus pour l'utilisateur courant (~/.local), sans droits root.
#   ./install.sh             installe
#   ./install.sh --default   installe et fait d'Odysseus le gestionnaire de fichiers par défaut
#   ./install.sh --uninstall désinstalle
set -e

PREFIX="${PREFIX:-$HOME/.local}"
SHARE="$PREFIX/share"
APPDIR="$SHARE/odysseus"
SRC="$(cd "$(dirname "$0")" && pwd)"
DESKTOP="io.github.odysseus.Odysseus.desktop"

if [ "$1" = "--uninstall" ]; then
    if [ "$(xdg-mime query default inode/directory 2>/dev/null)" = "$DESKTOP" ]; then
        xdg-mime default org.gnome.Nautilus.desktop inode/directory 2>/dev/null || true
    fi
    rm -rf "$APPDIR" "$PREFIX/bin/odysseus" "$SHARE/applications/$DESKTOP" \
        "$SHARE/icons/hicolor/scalable/apps/io.github.odysseus.Odysseus.svg"
    update-desktop-database "$SHARE/applications" 2>/dev/null || true
    echo "Odysseus a été désinstallé."
    exit 0
fi

missing=""
python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Adw; assert Adw.MINOR_VERSION >= 5" 2>/dev/null \
    || missing="$missing GTK4/libadwaita>=1.5 (python3-gi gir1.2-gtk-4.0 gir1.2-adw-1)"
if [ -n "$missing" ]; then
    echo "Dépendances manquantes :$missing"
    echo "Debian/Ubuntu/Zorin : sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1"
    exit 1
fi

# Programmes facultatifs : chaque fonctionnalité qui en dépend se désactive d'elle-même s'il manque.
optional=""
for prog in 7z:7zip lbzip2:lbzip2 pigz:pigz gs:ghostscript qpdf:qpdf ffmpeg:ffmpeg ffmpegthumbnailer:ffmpegthumbnailer rg:ripgrep pdftoppm:poppler-utils; do
    command -v "${prog%%:*}" >/dev/null 2>&1 || optional="$optional ${prog#*:}"
done
python3 -c "import cairo" 2>/dev/null || optional="$optional python3-cairo python3-gi-cairo"
python3 -c "import gi; gi.require_version('Poppler','0.18')" 2>/dev/null || optional="$optional gir1.2-poppler-0.18"

mkdir -p "$APPDIR" "$PREFIX/bin" "$SHARE/applications" "$SHARE/icons/hicolor/scalable/apps"
rm -rf "$APPDIR/odysseus" "$APPDIR/data"
cp -r "$SRC/odysseus" "$SRC/data" "$APPDIR/"
find "$APPDIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
cat > "$PREFIX/bin/odysseus" <<LAUNCH
#!/bin/sh
exec python3 -c 'import sys; sys.path.insert(0, "$APPDIR"); from odysseus.app import main; sys.exit(main(sys.argv))' "\$@"
LAUNCH
chmod +x "$PREFIX/bin/odysseus"
sed "s|^Exec=odysseus|Exec=$PREFIX/bin/odysseus|" "$SRC/data/$DESKTOP" > "$SHARE/applications/$DESKTOP"
cp "$SRC/data/icons/hicolor/scalable/apps/io.github.odysseus.Odysseus.svg" "$SHARE/icons/hicolor/scalable/apps/"
update-desktop-database "$SHARE/applications" 2>/dev/null || true
gtk-update-icon-cache -f -t "$SHARE/icons/hicolor" 2>/dev/null || true

if [ "$1" = "--default" ]; then
    xdg-mime default "$DESKTOP" inode/directory && echo "Odysseus est maintenant le gestionnaire de fichiers par défaut."
fi

echo "Odysseus est installé. Lancez-le depuis le menu des applications ou avec : odysseus"
if [ -n "$optional" ]; then
    echo "Pour profiter de toutes les fonctionnalités : sudo apt install$optional"
fi
case ":$PATH:" in *":$PREFIX/bin:"*) ;; *) echo "Astuce : ajoutez $PREFIX/bin à votre PATH." ;; esac
