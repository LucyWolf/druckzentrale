**Language / Sprache:** [🇬🇧 English](#druckzentrale) | [🇩🇪 Deutsch](#druckzentrale-1)

---

# Druckzentrale

Print, scan, check ink or toner, maintain and fax – for **printers on Linux**, in one window. Works over **USB, LAN and Wi-Fi** and with printers from any manufacturer that Linux supports. HP printers get extra functions through HPLIP (fax, printhead tools, printer reports).

Unofficial and independent – not made by or affiliated with any printer manufacturer.

The app builds on the standard Linux tools: **CUPS** for printing and status, **IPP Everywhere / AirPrint** for driverless printing, **SANE** with **sane-airscan** and **ipp-usb** for scanning, free drivers (**Gutenprint**, **Foomatic**) for older printers, and **HPLIP** for HP devices.

## Installation

Download the installer for your distribution from the [Releases](https://github.com/LucyWolf/druckzentrale/releases/latest) page and double-click it. The first time, your file manager asks whether it may run the file.

| Distribution | Installer |
|---|---|
| Arch / CachyOS / Manjaro | [`Druckzentrale-arch-installer.desktop`](https://github.com/LucyWolf/druckzentrale/releases/latest/download/Druckzentrale-arch-installer.desktop) |
| Debian / Ubuntu | [`Druckzentrale-deb-installer.desktop`](https://github.com/LucyWolf/druckzentrale/releases/latest/download/Druckzentrale-deb-installer.desktop) |
| Everything else (Fedora, openSUSE, …) | [`Druckzentrale-installer.desktop`](https://github.com/LucyWolf/druckzentrale/releases/latest/download/Druckzentrale-installer.desktop) |

The installer sets up everything with a single password prompt:

- packages: Qt (PySide6, native Wayland), CUPS, free printer drivers (Gutenprint, Foomatic), HPLIP, SANE, sane-airscan, ipp-usb, Avahi
- turns on the printing service (CUPS) and network discovery (Avahi)
- installs the app and adds a menu entry

Running it again offers **Update** or **Uninstall**. After that, updates come through the app (*Version · Info* at the bottom left).

<details>
<summary>Prefer the terminal?</summary>

```bash
curl -fsSL -o Druckzentrale-installieren.sh https://github.com/LucyWolf/druckzentrale/releases/latest/download/Druckzentrale-installieren.sh
bash Druckzentrale-installieren.sh
```
</details>

## Features

- **Find printers** over USB and the network, any brand; the same printer found several ways is shown once
- **Set up** with one click: driverless where possible, otherwise the matching installed driver is chosen automatically; a wizard guides through the first start
- **Overview** per printer: status and messages, ink or toner levels; scan and fax only appear if the printer has them
- **Print** several files at once: copies, pages, double-sided, color/black and white, paper size, quality
- **Scan** from the glass or the document feeder (detected automatically), preview, scan area, edge detection; save as **PDF** (multi-page), **JPG, PNG, BMP, TIFF** or **WEBP**; asks before unsaved scans are discarded
- **Maintenance:** print jobs (view, cancel), test page, cleaning and self-test where the driver offers it; for HP printers also printhead cleaning in levels, print quality diagnostics, line feed calibration and printer reports
- **Fax** for HP printers that have it – set up from the app with one click

## Notes

- Driverless printing and scanning works for most printers from about 2015 on. Older printers need a driver; the free ones (Gutenprint, Foomatic, HPLIP) come with the installer, others only from the manufacturer.
- Fax and printhead tools are HP-only (HPLIP).
- Only tested with an HP OfficeJet Pro 8620 so far.

## Running from source

```bash
python3 druckzentrale.py
```

Needs PySide6, pycups and Pillow; the installer pulls in everything else.

**Releases (maintainers):** raise `APP_VERSION` in `druckzentrale.py` (the last digit counts up to 99), commit, push, run `tools/release.sh`.

License: MIT, see [LICENSE](LICENSE).

---

# Druckzentrale

Drucken, Scannen, Tinte oder Toner, Wartung und Fax – für **Drucker unter Linux**, in einem Fenster. Funktioniert über **USB, LAN und WLAN** und mit Druckern jedes Herstellers, den Linux unterstützt. HP-Drucker bekommen über HPLIP Zusatzfunktionen (Fax, Druckkopf-Werkzeuge, Druckerberichte).

Inoffiziell und unabhängig – nicht von einem Druckerhersteller und nicht mit einem verbunden.

Die App baut auf den Linux-Standardwerkzeugen auf: **CUPS** für Drucken und Status, **IPP Everywhere / AirPrint** für treiberloses Drucken, **SANE** mit **sane-airscan** und **ipp-usb** fürs Scannen, freie Treiber (**Gutenprint**, **Foomatic**) für ältere Drucker und **HPLIP** für HP-Geräte.

## Installation

Den Installer für deine Distribution von der [Releases](https://github.com/LucyWolf/druckzentrale/releases/latest)-Seite herunterladen und doppelklicken. Beim ersten Mal fragt der Dateimanager, ob er die Datei ausführen darf.

| Distribution | Installer |
|---|---|
| Arch / CachyOS / Manjaro | [`Druckzentrale-arch-installer.desktop`](https://github.com/LucyWolf/druckzentrale/releases/latest/download/Druckzentrale-arch-installer.desktop) |
| Debian / Ubuntu | [`Druckzentrale-deb-installer.desktop`](https://github.com/LucyWolf/druckzentrale/releases/latest/download/Druckzentrale-deb-installer.desktop) |
| Alle anderen (Fedora, openSUSE, …) | [`Druckzentrale-installer.desktop`](https://github.com/LucyWolf/druckzentrale/releases/latest/download/Druckzentrale-installer.desktop) |

Der Installer richtet alles mit einer einzigen Passwortabfrage ein:

- Pakete: Qt (PySide6, nativ unter Wayland), CUPS, freie Druckertreiber (Gutenprint, Foomatic), HPLIP, SANE, sane-airscan, ipp-usb, Avahi
- schaltet den Druckdienst (CUPS) und die Netzwerksuche (Avahi) ein
- installiert die App und legt einen Menüeintrag an

Erneut gestartet bietet er **Aktualisieren** oder **Deinstallieren** an. Danach kommen Updates über die App (*Version · Info* links unten).

<details>
<summary>Lieber per Terminal?</summary>

```bash
curl -fsSL -o Druckzentrale-installieren.sh https://github.com/LucyWolf/druckzentrale/releases/latest/download/Druckzentrale-installieren.sh
bash Druckzentrale-installieren.sh
```
</details>

## Funktionen

- **Drucker finden** über USB und Netzwerk, jede Marke; derselbe Drucker auf mehreren Wegen erscheint nur einmal
- **Einrichten** mit einem Klick: treiberlos, wo es geht, sonst wählt die App den passenden installierten Treiber; beim ersten Start führt ein Assistent durch
- **Übersicht** je Drucker: Status und Meldungen, Tinte oder Toner; Scannen und Fax erscheinen nur, wenn der Drucker sie hat
- **Drucken** mehrerer Dateien auf einmal: Kopien, Seiten, beidseitig, Farbe/Schwarzweiß, Papierformat, Qualität
- **Scannen** von der Glasscheibe oder aus dem Vorlageneinzug (wird erkannt), Vorschau, Scanbereich, Kanten erkennen; speichern als **PDF** (mehrseitig), **JPG, PNG, BMP, TIFF** oder **WEBP**; fragt nach, bevor ungespeicherte Scans verloren gehen
- **Wartung:** Druckaufträge (ansehen, abbrechen), Testseite, Reinigung und Selbsttest, wo der Treiber es anbietet; bei HP-Druckern zusätzlich Druckkopfreinigung in Stufen, Druckqualitäts-Diagnose, Zeilenvorschub-Kalibrierung und Druckerberichte
- **Fax** bei HP-Druckern, die es können – aus der App mit einem Klick eingerichtet

## Hinweise

- Treiberlos drucken und scannen klappt bei den meisten Druckern ab etwa 2015. Ältere brauchen einen Treiber; die freien (Gutenprint, Foomatic, HPLIP) bringt der Installer mit, andere gibt es nur beim Hersteller.
- Fax und Druckkopf-Werkzeuge gibt es nur für HP (HPLIP).
- Bisher nur mit einem HP OfficeJet Pro 8620 getestet.

## Aus dem Quellcode starten

```bash
python3 druckzentrale.py
```

Braucht PySide6, pycups und Pillow; alles andere holt der Installer.

**Releases (für Betreuer):** `APP_VERSION` in `druckzentrale.py` erhöhen (die letzte Stelle zählt bis 99), committen, pushen, `tools/release.sh` ausführen.

Lizenz: MIT, siehe [LICENSE](LICENSE).
