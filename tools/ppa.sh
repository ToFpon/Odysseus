#!/bin/sh
# Publie sur le PPA la version indiquée en tête de debian/changelog.
#   tools/ppa.sh           construit le paquet source, le signe et l'envoie sur ppa:nourpon/odysseus
#   tools/ppa.sh --build   construit et signe seulement (rien n'est envoyé)
#
# Une fois : sudo apt install devscripts debhelper dh-python dput lintian
#            + une clé GPG enregistrée sur Launchpad, pour l'adresse du changelog.
# Nouvelle version : dch -v 1.0.1~noble1 "…" (puis dch -r), commit, tools/ppa.sh
set -e
PPA="ppa:nourpon/odysseus"
cd "$(dirname "$0")/.."
if [ -n "$(git status --porcelain)" ]; then
    echo "Des modifications ne sont pas commitées : le paquet doit correspondre au dépôt."
    exit 1
fi
VERSION=$(dpkg-parsechangelog -S Version)
DIST=$(dpkg-parsechangelog -S Distribution)
if [ "$DIST" = "UNRELEASED" ]; then
    echo "debian/changelog est encore en UNRELEASED : terminer l'entrée avec « dch -r »."
    exit 1
fi
OUT=$(mktemp -d)
git archive --prefix="odysseus-$VERSION/" HEAD | tar -x -C "$OUT"
(cd "$OUT/odysseus-$VERSION" && debuild -S -d)        # signé avec la clé GPG du changelog
lintian "$OUT/odysseus_${VERSION}_source.changes" || true
if [ "$1" = "--build" ]; then
    echo "Paquet source signé dans $OUT (rien n'a été envoyé)."
    exit 0
fi
dput "$PPA" "$OUT/odysseus_${VERSION}_source.changes"
echo "Envoyé. Launchpad le construit en quelques minutes : https://launchpad.net/~nourpon/+archive/ubuntu/odysseus/+packages"
