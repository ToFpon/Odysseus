"""Traductions de l'interface.

Le français est la langue source. Les catalogues gettext (.po) de data/locale/ sont lus
directement au démarrage : pas d'étape de compilation. Langues : fr, en, de ; toute autre
langue système retombe sur l'anglais.
"""

from __future__ import annotations

import ast
import os

from .settings import settings

SUPPORTED = ("fr", "en", "de")
LANGUAGE_NAMES = {"system": None, "fr": "Français", "en": "English", "de": "Deutsch"}
LOCALE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "locale")


def system_language() -> str:
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "")
        for part in value.split(":"):
            code = part.split(".")[0].split("_")[0].lower()
            if code in SUPPORTED:
                return code
            if code and code not in ("c", "posix"):
                return "en"
    return "en"


def current_language() -> str:
    lang = settings["language"]
    return lang if lang in SUPPORTED else system_language()


def _unquote(s: str) -> str:
    return ast.literal_eval(s)


def parse_po(path: str) -> dict:
    """Lit un fichier .po (msgid, msgid_plural, msgstr, msgstr[n], chaînes multi-lignes)."""
    catalog: dict = {}
    msgid = plural = None
    strs: dict = {}
    field = None

    def flush():
        if msgid:
            if plural is not None:
                forms = [strs.get(i, "") for i in range(len(strs))]
                if all(forms):
                    catalog[(msgid, plural)] = forms
            elif strs.get(0):
                catalog[msgid] = strs[0]

    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                if not line and msgid is not None:
                    flush()
                    msgid, plural, strs, field = None, None, {}, None
                continue
            if line.startswith("msgid_plural "):
                plural, field = _unquote(line[13:]), "plural"
            elif line.startswith("msgid "):
                if msgid is not None:
                    flush()
                    plural, strs = None, {}
                msgid, field = _unquote(line[6:]), "id"
            elif line.startswith("msgstr["):
                idx = int(line[7:line.index("]")])
                strs[idx], field = _unquote(line[line.index("]") + 2:]), idx
            elif line.startswith("msgstr "):
                strs[0], field = _unquote(line[7:]), 0
            elif line.startswith('"'):
                text = _unquote(line)
                if field == "id":
                    msgid += text
                elif field == "plural":
                    plural += text
                elif field is not None:
                    strs[field] += text
    flush()
    return catalog


LANGUAGE = current_language()
_catalog: dict = {}
if LANGUAGE != "fr":
    _po = os.path.join(LOCALE_DIR, LANGUAGE + ".po")
    if os.path.exists(_po):
        _catalog = parse_po(_po)


def tr(text: str) -> str:
    """Traduit un texte de l'interface."""
    return _catalog.get(text, text)


def ntr(singular: str, plural: str, n: int) -> str:
    """Traduit un texte au pluriel (règle commune à fr, en et de, sauf 0 en français)."""
    forms = _catalog.get((singular, plural))
    if LANGUAGE == "fr":
        many = n > 1
    else:
        many = n != 1
    if forms:
        return forms[1 if many and len(forms) > 1 else 0]
    return plural if many else singular


def decimal(value: float, digits: int = 1) -> str:
    text = f"{value:.{digits}f}"
    return text if LANGUAGE == "en" else text.replace(".", ",")
