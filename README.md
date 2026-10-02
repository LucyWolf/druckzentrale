**Language / Sprache:** [🇬🇧 English](#hp-druckzentrale) | [🇩🇪 Deutsch](#hp-druckzentrale-1)

---

# HP Druckzentrale

Print, scan, check ink levels and fax with **HP printers on Linux**, all in one window. Works over **USB, LAN and Wi-Fi**.

The app builds on the standard Linux tools: **CUPS** for printing and status, **HPLIP** (HP's own Linux software) for HP-specific devices, ink and fax, and **SANE** with **sane-airscan** and **ipp-usb** for scanning without drivers.

## Installation

Download the installer for your distribution from the [Releases](https://github.com/LucyWolf/hp-druckzentrale/releases/latest) page and double-click it. The first time, your file manager asks whether it may run the file.

| Distribution | Installer |
|---|---|
| Arch / CachyOS / Manjaro | [`HP-Druckzentrale-arch-installer.desktop`](https://github.com/LucyWolf/hp-druckzentrale/releases/latest/download/HP-Druckzentrale-arch-installer.desktop) |
| Debian / Ubuntu | [`HP-Druckzentrale-deb-installer.desktop`](https://github.com/LucyWolf/hp-druckzentrale/releases/latest/download/HP-Druckzentrale-deb-installer.desktop) |
| Everything else (Fedora, openSUSE, …) | [`HP-Druckzentrale-installer.desktop`](https://github.com/LucyWolf/hp-druckzentrale/releases/latest/download/HP-Druckzentrale-installer.desktop) |

The installer sets up everything with a single password prompt:

- packages: Qt (PySide6), CUPS, HPLIP, SANE, sane-airscan, ipp-usb, Avahi
- turns on the printing service (CUPS) and network discovery (Avahi)
- installs the app and adds a menu entry

Running it again offers **Update** or **Uninstall**. After that, updates come through the **Update** button in the app.

<details>
<summary>Prefer the terminal?</summary>

```bash
curl -fsSL -o HP-Druckzentrale-installieren.sh https://github.com/LucyWolf/hp-druckzentrale/releases/latest/download/HP-Druckzentrale-installieren.sh
bash HP-Druckzentrale-installieren.sh
```
</details>

## Features

- **Find printers** over USB and the network; the same printer found several ways is shown once
- **Set up** with one click: newer printers driverless (IPP Everywhere), older ones through HPLIP's setup wizard
- **Ink levels** per color, plus status like "out of paper" or "cover open"
- **Print** several files at once: copies, pages, double-sided, color/black and white, paper size, quality – only what the printer supports
- **Scan** from the glass or the document feeder, preview, reorder and delete pages; save as **PDF** (multi-page), **JPG, PNG, BMP, TIFF** or **WEBP**
- **Fax** for HP printers that have it (set up once with HPLIP's wizard); send documents or scanned pages
- Test page, printer web interface, remove queue

## Notes

- Some inexpensive HP models only report rough ink levels instead of percentages.
- Fax uses HPLIP and only appears for printers that HPLIP lists as fax-capable.
- Some older HP models need HP's proprietary plugin (`hp-plugin`); HPLIP asks for it during setup if needed.

## Running from source

```bash
python3 hp_druckzentrale.py
```

Needs PySide6, pycups and Pillow; the installer pulls in everything else.

**Releases (maintainers):** raise `APP_VERSION` in `hp_druckzentrale.py` (the last digit counts up to 99), commit, push, run `tools/release.sh`.

License: MIT, see [LICENSE](LICENSE).

---

# HP Druckzentrale

Drucken, Scannen, Tintenstand und Fax für **HP-Drucker unter Linux**, alles in einem Fenster. Funktioniert über **USB, LAN und WLAN**.

Die App baut auf den Linux-Standardwerkzeugen auf: **CUPS** für Drucken und Status, **HPLIP** (HPs eigene Linux-Software) für HP-spezifische Geräte, Tinte und Fax, und **SANE** mit **sane-airscan** und **ipp-usb** für Scannen ohne Treiber.

## Installation

Den Installer für deine Distribution von der [Releases](https://github.com/LucyWolf/hp-druckzentrale/releases/latest)-Seite herunterladen und doppelklicken. Beim ersten Mal fragt der Dateimanager, ob er die Datei ausführen darf.

| Distribution | Installer |
|---|---|
| Arch / CachyOS / Manjaro | [`HP-Druckzentrale-arch-installer.desktop`](https://github.com/LucyWolf/hp-druckzentrale/releases/latest/download/HP-Druckzentrale-arch-installer.desktop) |
| Debian / Ubuntu | [`HP-Druckzentrale-deb-installer.desktop`](https://github.com/LucyWolf/hp-druckzentrale/releases/latest/download/HP-Druckzentrale-deb-installer.desktop) |
| Alle anderen (Fedora, openSUSE, …) | [`HP-Druckzentrale-installer.desktop`](https://github.com/LucyWolf/hp-druckzentrale/releases/latest/download/HP-Druckzentrale-installer.desktop) |

Der Installer richtet alles mit einer einzigen Passwortabfrage ein:

- Pakete: Qt (PySide6), CUPS, HPLIP, SANE, sane-airscan, ipp-usb, Avahi
- schaltet den Druckdienst (CUPS) und die Netzwerksuche (Avahi) ein
- installiert die App und legt einen Menüeintrag an

Erneut gestartet bietet er **Aktualisieren** oder **Deinstallieren** an. Danach kommen Updates über den **Update**-Knopf in der App.

<details>
<summary>Lieber per Terminal?</summary>

```bash
curl -fsSL -o HP-Druckzentrale-installieren.sh https://github.com/LucyWolf/hp-druckzentrale/releases/latest/download/HP-Druckzentrale-installieren.sh
bash HP-Druckzentrale-installieren.sh
```
</details>

## Funktionen

- **Drucker finden** über USB und Netzwerk; derselbe Drucker auf mehreren Wegen erscheint nur einmal
- **Einrichten** mit einem Klick: neuere Drucker treiberlos (IPP Everywhere), ältere über den HPLIP-Assistenten
- **Tintenstand** je Farbe, dazu Meldungen wie „Papier leer“ oder „Deckel offen“
- **Drucken** mehrerer Dateien auf einmal: Kopien, Seiten, beidseitig, Farbe/Schwarzweiß, Papierformat, Qualität – nur was der Drucker kann
- **Scannen** von der Glasscheibe oder aus dem Einzug, Vorschau, Seiten sortieren und löschen; speichern als **PDF** (mehrseitig), **JPG, PNG, BMP, TIFF** oder **WEBP**
- **Fax** bei HP-Druckern, die es können (einmalig über den HPLIP-Assistenten einrichten); Dokumente oder gescannte Seiten senden
- Testseite, Weboberfläche des Druckers, Warteschlange entfernen

## Hinweise

- Manche günstige HP-Modelle melden nur grobe Tintenstufen statt Prozent.
- Fax läuft über HPLIP und erscheint nur bei Druckern, die HPLIP als faxfähig kennt.
- Einige ältere HP-Modelle brauchen das unfreie HP-Plugin (`hp-plugin`); HPLIP fragt beim Einrichten danach, falls nötig.

## Aus dem Quellcode starten

```bash
python3 hp_druckzentrale.py
```

Braucht PySide6, pycups und Pillow; alles andere holt der Installer.

**Releases (für Betreuer):** `APP_VERSION` in `hp_druckzentrale.py` erhöhen (die letzte Stelle zählt bis 99), committen, pushen, `tools/release.sh` ausführen.

Lizenz: MIT, siehe [LICENSE](LICENSE).
