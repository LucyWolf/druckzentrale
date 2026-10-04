# Druckervorlagen / Printer templates

PrintDock lädt beim Einbinden eines Druckers **nur die Vorlage dieses Modells** und merkt sie sich.
Gibt es keine, gilt die Standard-Vorlage (eingebaut in `printdock.py`, `STANDARD_TEMPLATE`).

**Dateiname:** Gerätename in Kleinbuchstaben, Leerzeichen und Sonderzeichen als `-`,
z. B. „HP Officejet Pro 8620“ → `hp-officejet-pro-8620.json`.

| Feld | Bedeutung | fehlt → |
|---|---|---|
| `name` | Anzeigename der Vorlage | „Standard“ |
| `treiber` | Pakete je Distribution (`arch`, `deb`, `rpm`, `suse`), nur für Drucker ohne treiberloses Drucken; `{}` = keine | freie Treiber (Gutenprint, Foomatic) |
| `treiber_suche` | Suchbegriff für den Treiber | Modellname |
| `papier` | Standardpapier (IPP-Name, z. B. `iso_a4_210x297mm`) | A4 |
| `beidseitig` | standardmäßig beidseitig drucken | `true` |
| `fax` | `true`/`false` legt fest, ob es Fax gibt | beim Drucker nachsehen |
| `wartung` | erlaubte Wartungs- und Berichtsaufträge (Druckkopfreinigung immer) | alles, was der Drucker anbietet |
| `hinweise` | kurze Hinweise, stehen in der Übersicht unter „Gerät“ | keine |

Eine neue Vorlage ist nach dem Hochladen sofort für alle da – ohne neue App-Version.
Bereits gemerkte Vorlagen liegen unter `~/.local/share/printdock/vorlagen/`.
