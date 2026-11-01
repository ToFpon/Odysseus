#!/bin/sh
# Envoie le site de présentation (dossier site/) sur les pages perso Free, comme FileZilla :
#   tools/site.sh            envoie les fichiers modifiés depuis le dernier envoi
#   tools/site.sh --essai    montre ce qui serait envoyé, sans rien envoyer
# Appelé à la fin de tools/ppa.sh.
#
# Une fois :
#   sudo apt install lftp
#   ~/.netrc (identifiants FTP, lisible par toi seul) :
#       machine tofmicro.free.fr login tofmicro password TON_MOT_DE_PASSE
#   chmod 600 ~/.netrc
# Le FTP de Free n'est pas chiffré (le mot de passe circule en clair, comme avec FileZilla) :
# chiffrer le fichier ne protégerait rien de plus ; ~/.netrc en 600 suffit.
# Autre serveur / dossier / port : ODYSSEUS_SITE_HOST, ODYSSEUS_SITE_DIR, ODYSSEUS_SITE_PORT.
set -e
HOST="${ODYSSEUS_SITE_HOST:-tofmicro.free.fr}"
DIR="${ODYSSEUS_SITE_DIR:-Odysseus}"
PORT="${ODYSSEUS_SITE_PORT:-21}"
cd "$(dirname "$0")/.."
if ! command -v lftp >/dev/null 2>&1; then
    echo "Site non envoyé : lftp manque (sudo apt install lftp)."
    exit 1
fi
if ! grep -qs "machine[[:space:]]\+$HOST\b" "$HOME/.netrc"; then
    echo "Site non envoyé : pas d'identifiants pour $HOST dans ~/.netrc."
    echo "  echo 'machine $HOST login IDENTIFIANT password MOT_DE_PASSE' >> ~/.netrc && chmod 600 ~/.netrc"
    exit 1
fi
if [ -n "$(find "$HOME/.netrc" -perm /077)" ]; then
    chmod 600 "$HOME/.netrc"            # lisible par les autres comptes : on referme
fi
DRY=""
[ "$1" = "--essai" ] && DRY="--dry-run"
# --only-newer : seulement ce qui a changé ; jamais --delete (rien n'est retiré du serveur)
lftp -e "set ftp:ssl-allow no; set net:max-retries 2; set net:timeout 30; \
mirror --reverse --only-newer --no-perms --verbose=1 $DRY site/ $DIR/; bye" -p "$PORT" "$HOST"
[ -z "$DRY" ] && echo "Site envoyé : https://tofmicro.pages-perso.free.fr/$DIR/"
exit 0
