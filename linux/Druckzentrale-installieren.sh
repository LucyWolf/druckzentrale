#!/usr/bin/env bash
# Druckzentrale — Linux-Installer
# Per Doppelklick (ueber Druckzentrale-*-installer.desktop) oder im Terminal.
# Installiert alles, was die App braucht, mit einer einzigen Passwortabfrage:
#   Pakete (Qt mit Wayland-Modul, CUPS, freie Druckertreiber, HPLIP, SANE + sane-airscan, ipp-usb, Avahi),
#   Druck- und Netzwerkdienste,
#   die App selbst nach ~/.local/share und einen Menueeintrag.
# Ist sie schon installiert, fragt das Skript: aktualisieren oder deinstallieren.
set -euo pipefail

TITLE="Druckzentrale"
INSTALL_DIR="$HOME/.local/share/hp-druckzentrale"
DESKTOP_FILE="$HOME/.local/share/applications/hp-druckzentrale.desktop"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$PWD/x}")" && pwd)"
RELEASE_URL="https://github.com/LucyWolf/druckzentrale/releases/latest/download"

GUI=0
if command -v kdialog >/dev/null 2>&1 && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    GUI=1
elif command -v zenity >/dev/null 2>&1 && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    GUI=2
fi

info() {
    case "$GUI" in
        1) kdialog --title "$TITLE" --msgbox "$(printf '%b' "$1")" ;;
        2) zenity --info --title="$TITLE" --text="$1" --width=380 ;;
        *) printf '%b\n' "$1" ;;
    esac
}
fail() {
    case "$GUI" in
        1) kdialog --title "$TITLE" --error "$(printf '%b' "$1")" ;;
        2) zenity --error --title="$TITLE" --text="$1" --width=380 ;;
        *) printf 'FEHLER: %b\n' "$1" >&2 ;;
    esac
    exit 1
}
note() {
    case "$GUI" in
        1) kdialog --title "$TITLE" --passivepopup "$1" 8 & ;;
        2) zenity --notification --text="$TITLE: $1" & ;;
        *) echo "$1" ;;
    esac
}
as_root() {
    if [ "$GUI" != "0" ] && command -v pkexec >/dev/null 2>&1; then
        pkexec /bin/sh -c "$1"
    else
        sudo /bin/sh -c "$1"
    fi
}

uninstall() {
    rm -rf "$INSTALL_DIR"
    rm -f "$DESKTOP_FILE"
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$HOME/.local/share/applications" || true
    info "Druckzentrale wurde entfernt.\n\nDie Pakete (CUPS, HPLIP, SANE …) und eingerichtete Drucker bleiben erhalten,\nandere Programme nutzen sie auch."
    exit 0
}

# Schon installiert? Dann aktualisieren oder deinstallieren
if [ -f "$INSTALL_DIR/hp_druckzentrale.py" ]; then
    Q="Druckzentrale ist schon installiert."
    case "$GUI" in
        1) set +e; kdialog --title "$TITLE" --yesnocancel "$Q" --yes-label "Aktualisieren" --no-label "Deinstallieren"
           CHOICE=$?; set -e ;;
        2) set +e; OUT=$(zenity --question --title="$TITLE" --text="$Q" --ok-label="Aktualisieren" \
                --cancel-label="Abbrechen" --extra-button="Deinstallieren"); RC=$?; set -e
           if [ "$OUT" = "Deinstallieren" ]; then CHOICE=1; elif [ "$RC" = 0 ]; then CHOICE=0; else CHOICE=2; fi ;;
        *) read -rp "$Q [a]ktualisieren, [d]einstallieren, [x] abbrechen: " A
           case "$A" in a|A) CHOICE=0 ;; d|D) CHOICE=1 ;; *) CHOICE=2 ;; esac ;;
    esac
    case "$CHOICE" in
        0) ;;
        1) uninstall ;;
        *) exit 0 ;;
    esac
fi

# 1) Programmdatei: neben dem Installer, sonst die neueste aus dem Release
APP_SRC=""
if [ -z "${HPDZ_FROM_WEB:-}" ]; then
    for c in "$SCRIPT_DIR/hp_druckzentrale.py" "$SCRIPT_DIR/../hp_druckzentrale.py"; do
        if [ -f "$c" ]; then APP_SRC="$c"; break; fi
    done
fi
if [ -z "$APP_SRC" ]; then
    command -v curl >/dev/null 2>&1 || fail "curl fehlt – damit wird das Programm geladen."
    TMP_APP="$(mktemp)"
    trap 'rm -f "$TMP_APP"' EXIT
    note "Lade die neueste Version herunter …"
    curl -fsSL --retry 2 -o "$TMP_APP" "$RELEASE_URL/hp_druckzentrale.py" \
        || fail "Download fehlgeschlagen.\nBesteht eine Internetverbindung?"
    APP_SRC="$TMP_APP"
fi

# 2) Pakete je Distribution. Alles in einem Root-Schritt (ein Passwort).
if command -v pacman >/dev/null 2>&1; then
    PKGS="pyside6 qt6-wayland python-pycups python-pillow cups cups-filters gutenprint foomatic-db foomatic-db-engine foomatic-db-ppds hplip sane sane-airscan ipp-usb avahi nss-mdns"
    HAVE="pacman -Q"
    INSTALL="pacman -S --needed --noconfirm"
elif command -v apt-get >/dev/null 2>&1; then
    PKGS="qt6-wayland python3-pyside6.qtwidgets python3-pyside6.qtgui python3-pyside6.qtcore python3-cups python3-pil cups cups-filters printer-driver-gutenprint foomatic-db-compressed-ppds hplip sane-utils sane-airscan ipp-usb avahi-daemon libnss-mdns"
    HAVE="dpkg -s"
    INSTALL="env DEBIAN_FRONTEND=noninteractive apt-get install -y"
elif command -v dnf >/dev/null 2>&1; then
    PKGS="python3-pyside6 qt6-qtwayland python3-cups python3-pillow cups cups-filters gutenprint-cups foomatic-db foomatic-db-ppds hplip sane-backends sane-airscan ipp-usb avahi nss-mdns"
    HAVE="rpm -q"
    INSTALL="dnf install -y"
elif command -v zypper >/dev/null 2>&1; then
    PKGS="python3-pyside6 qt6-wayland python3-pycups python3-Pillow cups cups-filters gutenprint OpenPrintingPPDs hplip sane-backends sane-airscan ipp-usb avahi nss-mdns"
    HAVE="rpm -q"
    INSTALL="zypper --non-interactive install"
else
    fail "Unbekannte Paketverwaltung.\nBitte von Hand installieren: PySide6, pycups, Pillow, CUPS, Gutenprint, HPLIP, SANE, sane-airscan, ipp-usb, Avahi."
fi

MISSING=""
for p in $PKGS; do
    $HAVE "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"
done

# Dienste: CUPS (Drucken), Avahi (Drucker im Netz finden). ipp-usb startet von selbst beim Einstecken.
SERVICES=""
for s in cups avahi-daemon; do
    systemctl is-enabled "$s" >/dev/null 2>&1 && systemctl is-active "$s" >/dev/null 2>&1 || SERVICES="$SERVICES $s"
done

if [ -n "$MISSING" ] || [ -n "$SERVICES" ]; then
    MSG="Es wird eingerichtet:"
    [ -n "$MISSING" ] && MSG="$MSG\n\nPakete:$MISSING"
    [ -n "$SERVICES" ] && MSG="$MSG\n\nDienste einschalten:$SERVICES"
    MSG="$MSG\n\nDanach fragt ein Fenster einmal nach deinem Passwort."
    case "$GUI" in
        1) kdialog --title "$TITLE" --continuecancel "$(printf '%b' "$MSG")" || exit 0 ;;
        2) zenity --question --title="$TITLE" --text="$MSG" --width=420 || exit 0 ;;
        *) printf '%b\n' "$MSG" ;;
    esac
    note "Installiere Pakete – das kann ein paar Minuten dauern …"
    ROOT_CMD=""
    [ -n "$MISSING" ] && ROOT_CMD="$INSTALL$MISSING"
    for s in $SERVICES; do
        ROOT_CMD="${ROOT_CMD:+$ROOT_CMD && }systemctl enable --now $s"
    done
    as_root "$ROOT_CMD" || fail "Die Installation wurde abgebrochen oder ist fehlgeschlagen.\n\nFehlende Pakete:$MISSING"
fi

python3 -c "import PySide6, cups, PIL" 2>/dev/null \
    || fail "Python findet PySide6, pycups oder Pillow nicht.\nBitte prüfen, ob die Pakete installiert sind."

# 3) Programm kopieren
mkdir -p "$INSTALL_DIR"
cp -f "$APP_SRC" "$INSTALL_DIR/hp_druckzentrale.py"
chmod 755 "$INSTALL_DIR/hp_druckzentrale.py"

# 4) Menueeintrag
mkdir -p "$(dirname "$DESKTOP_FILE")"
cat > "$DESKTOP_FILE" << DESKTOP
[Desktop Entry]
Type=Application
Name=Druckzentrale
Comment=Drucken, Scannen, Tintenstand, Wartung und Fax für Drucker
Exec=python3 $INSTALL_DIR/hp_druckzentrale.py
Icon=printer
Categories=Office;Graphics;Utility;
StartupWMClass=hp-druckzentrale
DESKTOP
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$HOME/.local/share/applications" || true

info "Installation abgeschlossen!\n\nStart über das Anwendungsmenü: Druckzentrale\n\nUSB-Drucker, die schon stecken: einmal ab- und wieder anstecken."
