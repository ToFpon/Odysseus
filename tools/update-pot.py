#!/usr/bin/env python3
"""Extrait les textes tr()/ntr() du code vers data/locale/odysseus.pot.

Mettre ensuite à jour une traduction :
    msgmerge --update data/locale/de.po data/locale/odysseus.pot
Ajouter une langue : copier odysseus.pot en xx.po, le traduire, puis ajouter « xx »
à SUPPORTED et LANGUAGE_NAMES dans odysseus/i18n.py.
"""
import ast
import glob
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def main():
    found: dict = {}
    files = sorted(glob.glob(os.path.join(ROOT, "odysseus", "**", "*.py"), recursive=True))
    for path in files:
        rel = os.path.relpath(path, ROOT)
        tree = ast.parse(open(path, encoding="utf-8").read())
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("tr", "ntr")):
                continue
            args = [a.value for a in n.args[:2] if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if n.func.id == "tr" and len(args) >= 1:
                key = (args[0],)
            elif n.func.id == "ntr" and len(args) == 2:
                key = tuple(args)
            else:
                continue
            found.setdefault(key, []).append(f"{rel}:{n.lineno}")
    out = ['# Odysseus — modèle de traduction (langue source : français).', 'msgid ""', 'msgstr ""',
           '"Project-Id-Version: Odysseus\\n"', '"MIME-Version: 1.0\\n"',
           '"Content-Type: text/plain; charset=UTF-8\\n"', '"Content-Transfer-Encoding: 8bit\\n"',
           '"Plural-Forms: nplurals=2; plural=(n != 1);\\n"', '']
    for key, locs in found.items():
        out.append("#: " + " ".join(locs[:4]))
        if len(key) == 2:
            out += ["msgid " + q(key[0]), "msgid_plural " + q(key[1]), 'msgstr[0] ""', 'msgstr[1] ""', ""]
        else:
            out += ["msgid " + q(key[0]), 'msgstr ""', ""]
    target = os.path.join(ROOT, "data", "locale", "odysseus.pot")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    print(f"{len(found)} textes → {os.path.relpath(target, ROOT)}")


if __name__ == "__main__":
    main()
