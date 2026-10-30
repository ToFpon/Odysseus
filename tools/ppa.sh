#!/bin/sh
# Publie sur le PPA la version indiquée en tête de debian/changelog, pour chaque version d'Ubuntu :
# noble (24.04, Zorin OS 18) telle qu'écrite dans le changelog, puis resolute (26.04) avec la même
# entrée renommée « X~resolute1 » (le paquet est le même : Python, architecture « all »).
#   tools/ppa.sh           construit les paquets source, les signe et les envoie sur ppa:nourpon/odysseus
#   tools/ppa.sh --build   construit et signe seulement (rien n'est envoyé)
#
# Une fois : sudo apt install devscripts debhelper dh-python dput lintian
#            + une clé GPG enregistrée sur Launchpad, pour l'adresse du changelog.
# Nouvelle version : dch -v 1.0.1~noble1 "…" (puis dch -r), commit, tools/ppa.sh
set -e
PPA="ppa:nourpon/odysseus"
SERIES="noble resolute"            # la première est celle du changelog
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
FIRST=${SERIES%% *}
if [ "$DIST" != "$FIRST" ]; then
    echo "debian/changelog vise « $DIST » : la première entrée doit viser « $FIRST »."
    exit 1
fi
OUT=$(mktemp -d)
for S in $SERIES; do
    V=$(echo "$VERSION" | sed "s/~$FIRST/~$S/")
    DIR="$OUT/$S/odysseus-$V"
    mkdir -p "$DIR"
    git archive HEAD | tar -x -C "$DIR"
    # même entrée, autre version d'Ubuntu (« 1.5.2~noble1) noble; » → « 1.5.2~resolute1) resolute; »)
    sed -i "1s/($VERSION) $FIRST;/($V) $S;/" "$DIR/debian/changelog"
    (cd "$DIR" && debuild -S -d)       # signé avec la clé GPG du changelog
    lintian "$OUT/$S/odysseus_${V}_source.changes" || true
done
if [ "$1" = "--build" ]; then
    echo "Paquets source signés dans $OUT (rien n'a été envoyé)."
    exit 0
fi
for S in $SERIES; do
    V=$(echo "$VERSION" | sed "s/~$FIRST/~$S/")
    dput "$PPA" "$OUT/$S/odysseus_${V}_source.changes"
done
echo "Envoyé ($SERIES). Launchpad les construit en quelques minutes : https://launchpad.net/~nourpon/+archive/ubuntu/odysseus/+packages"
