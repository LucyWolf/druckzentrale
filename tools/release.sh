#!/usr/bin/env bash
# Neues Release: APP_VERSION in printdock.py erhöhen (letzte Stelle zählt bis 99), committen, dann
# dieses Skript (oder einfach git push). Das Release erstellt der GitHub-Workflow .github/workflows/release.yml.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=LucyWolf/printdock
VER=$(grep -oP '^APP_VERSION = "\K[0-9.]+' printdock.py)
[ -z "$(git status --porcelain)" ] || { echo "Ungespeicherte Änderungen – erst committen."; exit 1; }
git push -q
echo "Hochgeladen – GitHub prüft und erstellt v$VER …"
sleep 8
RUN=$(gh run list --repo "$REPO" --workflow release.yml --limit 1 --json databaseId -q '.[0].databaseId')
gh run watch "$RUN" --repo "$REPO" --exit-status >/dev/null && echo "v$VER veröffentlicht: https://github.com/$REPO/releases/tag/v$VER"
