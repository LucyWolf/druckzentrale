#!/usr/bin/env bash
# Neues Release: APP_VERSION in hp_druckzentrale.py erhoehen (letzte Stelle zaehlt bis 99), committen, pushen,
# dann dieses Skript. Es setzt den Tag und laedt Programm und Installer hoch.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=LucyWolf/druckzentrale
VER=$(grep -oP '^APP_VERSION = "\K[0-9.]+' hp_druckzentrale.py)
[ -z "$(git status --porcelain)" ] || { echo "Ungespeicherte Änderungen – erst committen."; exit 1; }
git fetch -q --tags
git rev-parse -q --verify "refs/tags/v$VER" >/dev/null && { echo "v$VER gibt es schon – APP_VERSION erhöhen."; exit 1; }
python3 -m py_compile hp_druckzentrale.py
bash -n linux/Druckzentrale-installieren.sh
git tag "v$VER" && git push -q origin "v$VER"
gh release create "v$VER" hp_druckzentrale.py linux/Druckzentrale-installieren.sh \
    linux/Druckzentrale-arch-installer.desktop linux/Druckzentrale-deb-installer.desktop \
    linux/Druckzentrale-installer.desktop --repo "$REPO" --title "Druckzentrale v$VER" --generate-notes --latest
echo "v$VER veröffentlicht: https://github.com/$REPO/releases/tag/v$VER"
