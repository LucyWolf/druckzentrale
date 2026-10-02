#!/usr/bin/env python3
# Druckzentrale – Drucken, Scannen, Tintenstand, Wartung und Fax fuer Drucker unter Linux (alle Marken,
# Zusatzfunktionen fuer HP ueber HPLIP).
# Baut auf den Linux-Standardwegen auf: CUPS (Drucken, Status, Tinte ueber IPP), HPLIP (HP-eigene
# Geraete, Tinte, Fax), SANE (Scannen; Netzwerk/IPP-over-USB ueber sane-airscan, sonst hpaio).
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from PySide6 import QtCore, QtGui, QtWidgets

try:
    import cups
except ImportError:
    cups = None
try:
    from PIL import Image
except ImportError:
    Image = None

APP_NAME = "Druckzentrale"
APP_VERSION = "1.0.9"
# Repo hiess frueher hp-druckzentrale (GitHub leitet weiter). Die Programmdatei heisst weiter
# hp_druckzentrale.py, weil aeltere Fassungen beim Update genau diesen Namen laden.
UPDATE_REPO = "LucyWolf/druckzentrale"
INSTALL_DIR = os.path.expanduser("~/.local/share/hp-druckzentrale")
HPLIP_DIR = "/usr/share/hplip"
NEEDED_PACKAGES = ["python-pycups", "sane", "sane-airscan", "ipp-usb", "python-pillow"]


# ---------- Hilfen ----------
def run(cmd, timeout=30, cwd=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return -1, "", f"{cmd[0]} nicht gefunden"
    except subprocess.TimeoutExpired:
        return -1, "", f"{cmd[0]}: Zeitüberschreitung"


def is_hp(text):
    return bool(re.search(r"\b(hp|hewlett[- ]?packard)\b", text or "", re.I)) or (text or "").startswith(("hp:", "hpfax:", "hpaio:"))


def norm(text):
    t = (text or "").lower().replace("hewlett-packard", "hp").replace("hewlett packard", "hp")
    t = re.sub(r"\s+-\s+(ipp everywhere|hpcups.*|hplip.*|driverless.*)$", "", t)   # Treiberzusatz der Warteschlange
    t = re.sub(r"^hp\s+", "", t)
    return re.sub(r"[^a-z0-9]", "", t)


class Bridge(QtCore.QObject):
    call = QtCore.Signal(object)


_bridge = None


def ui(fn):
    """Fuehrt fn im Oberflaechen-Thread aus."""
    _bridge.call.emit(fn)


def bg(fn, done=None):
    """fn im Hintergrund, Ergebnis (ok, wert) an done im Oberflaechen-Thread."""
    def work():
        try:
            res = (True, fn())
        except Exception as e:
            res = (False, e)
        if done:
            ui(lambda: done(*res))
    threading.Thread(target=work, daemon=True).start()


# ---------- HPLIP (in eigenem Prozess, damit sein Logging und Fehler die App nicht stoeren) ----------
HPLIP_SNIPPET = r'''
import sys, json
sys.path.insert(0, "/usr/share/hplip")
from base import device
mode, uri = sys.argv[1], sys.argv[2]
out = {}
if mode == "caps":
    m = device.queryModelByURI(uri)
    out = {k: int(m.get(k + "-type", 0) or 0) for k in ("fax", "scan", "clean", "align", "color-cal", "linefeed-cal", "pq-diag")}
elif mode == "faxppd":
    from prnt import cups as hcups
    ppd, kind, nick = hcups.getFaxPPDFile(device.queryModelByURI(uri), "fax")
    out = {"ppd": ppd or "", "nick": nick or ""}
else:
    d = device.Device(uri)
    try:
        d.queryDevice(quick=False)
    finally:
        try:
            d.close()
        except Exception:
            pass
    agents, i = [], 1
    while "agent%d-type" % i in d.dq:
        agents.append({"desc": str(d.dq.get("agent%d-desc" % i, "")), "level": int(d.dq.get("agent%d-level" % i, -1)),
                       "kind": int(d.dq.get("agent%d-kind" % i, 0))})
        i += 1
    out = {"agents": agents, "status": str(d.dq.get("status-desc", ""))}
print("JSON:" + json.dumps(out))
'''


def hplip(mode, uri, timeout=40):
    if not os.path.isdir(HPLIP_DIR):
        return None
    rc, out, err = run([sys.executable if os.path.basename(sys.executable).startswith("python") else "python3",
                        "-c", HPLIP_SNIPPET, mode, uri], timeout)
    for line in out.splitlines()[::-1]:
        if line.startswith("JSON:"):
            return json.loads(line[5:])
    return None


def hp_uri_for(printer):
    return next((u for u in printer.uris if u.startswith("hp:")), "")


# ---------- Drucker ----------
class Printer:
    def __init__(self):
        self.queue = None        # CUPS-Warteschlange, falls eingerichtet
        self.fax_queue = None    # HPLIP-Fax-Warteschlange (hpfax:/...)
        self.uris = []
        self.model = ""
        self.info = ""
        self.serial = ""
        self.host = ""          # IP-Adresse im Netz (fuer Weboberflaeche und direkte IPP-Abfrage)
        self.caps = None         # {"fax", "scan"} aus HPLIP

    @property
    def uri(self):
        return self.uris[0] if self.uris else ""

    @property
    def connection(self):
        u = " ".join(self.uris)
        if "usb" in u or re.search(r"://(localhost|127\.0\.0\.1)", u):
            return "USB"
        return "Netzwerk (LAN/WLAN)"

    @property
    def title(self):
        return self.info or self.model or self.queue or self.uri

    def best_setup_uri(self):
        # Treiberlos (IPP Everywhere) zuerst, dann HPLIP, dann rohes USB
        for pref in ("ipps://", "ipp://", "dnssd://", "hp:", "usb://"):
            u = next((x for x in self.uris if x.startswith(pref)), None)
            if u:
                return u
        return self.uri

    def ipp_uri(self):
        if self.queue:
            return None
        if re.fullmatch(r"[\d.]+", self.host or ""):
            return f"ipp://{self.host}:631/ipp/print"   # mDNS-Namen kann CUPS hier nicht aufloesen
        return next((u for u in self.uris if u.startswith(("ipp://", "ipps://")) and ".local" not in u), None)


def uri_ids(uri):
    serial = re.search(r"serial=([^&]+)", uri)
    uuid = re.search(r"uuid=([^&]+)", uri)
    host = re.search(r"(?:ip=|://)([^/:?&]+)", uri)
    return (serial.group(1) if serial else ""), (uuid.group(1) if uuid else ""), (host.group(1) if host else "")


def mdns_ipv4():
    """Dienstname -> IPv4 fuer alle IPP-Drucker im Netz (Avahi)."""
    rc, out, _ = run(["avahi-browse", "-rpt", "_ipp._tcp"], 12)
    ips = {}
    for line in out.splitlines():
        f = line.split(";")
        if len(f) > 8 and f[0] == "=" and f[2] == "IPv4":
            name = re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), f[3])
            ips[name] = f[7]
    return ips


_hp_uri_cache = {}


def hp_uri_from_ip(ip):
    """HPLIP-Adresse (hp:/net/...) aus der IP; HPLIP braucht sie fuer Tinte, Faehigkeiten und Fax."""
    if ip not in _hp_uri_cache:
        rc, out, _ = run(["hp-makeuri", ip], 40)
        m = re.search(r"CUPS URI:\s*(hp:/net/\S+)", out)
        _hp_uri_cache[ip] = m.group(1) if m else ""
    return _hp_uri_cache[ip]


def service_name(uri):
    m = re.match(r"(?:dnssd|ipps?)://([^/]+?)\._ipps?\._tcp", uri)
    return urllib.parse.unquote(m.group(1)) if m else None


def discover():
    """Alle Drucker: eingerichtete Warteschlangen und von CUPS gefundene Geraete (USB, Netzwerk)."""
    if cups is None:
        raise RuntimeError("python-pycups fehlt")
    conn = cups.Connection()
    printers = []

    def find(uri, model):
        serial, uuid, host = uri_ids(uri)
        name = service_name(uri)
        for p in printers:
            if uuid and any(uuid in u for u in p.uris):
                return p
            if name and any(service_name(u) == name for u in p.uris):
                return p
            if serial and serial == p.serial:
                return p
            if host and host == p.host and host not in ("localhost", "127.0.0.1") and not uri.startswith("usb"):
                return p
            if uri in p.uris:
                return p
            # gleiches Modell im Netz (dnssd und hp:/net beschreiben dasselbe Geraet)
            if model and norm(model) == norm(p.model) and "usb" not in uri and "usb" not in p.uri:
                return p
        return None

    def add(uri, model, info, queue=None):
        if uri.startswith("hpfax:"):
            return
        p = find(uri, model)
        if p is None:
            p = Printer()
            printers.append(p)
        if uri not in p.uris:
            p.uris.append(uri)
        serial, uuid, host = uri_ids(uri)
        p.serial = p.serial or serial
        if host and not uri.startswith("usb") and host not in ("localhost", "127.0.0.1"):
            p.host = p.host or host
        p.model = p.model or model
        p.info = p.info or info
        if queue and not p.queue:
            p.queue = queue

    queues = conn.getPrinters()
    for name, q in queues.items():
        uri, model = q.get("device-uri", ""), q.get("printer-make-and-model", "")
        if uri.startswith(("file:", "cups-pdf:")):
            continue   # PDF-Drucker und Dateiausgabe sind keine Geraete
        add(uri, model, q.get("printer-info", ""), queue=name)
    try:
        found = conn.getDevices(timeout=8)
    except cups.IPPError:
        found = {}
    for uri, d in found.items():
        model = d.get("device-make-and-model", "")
        if model.strip().lower() == "unknown":
            model = ""
        if ":/" not in uri:
            continue   # nur Backend-Platzhalter wie "hp", "hpfax" oder "socket", kein Geraet
        if uri.startswith(("file:", "cups-pdf:")):
            continue
        add(uri, model, d.get("device-info", ""))
    # Netzwerkdrucker: IP ueber mDNS, daraus die HPLIP-Adresse
    if any("usb" not in p.uri for p in printers):
        ips = mdns_ipv4()
        for p in printers:
            if p.connection == "USB":
                continue
            ip = next((ips[n] for n in (service_name(u) for u in p.uris) if n in ips), None) \
                or next((m.group(1) for m in (re.search(r"ip=([\d.]+)", u) for u in p.uris) if m), None)
            if ip:
                p.host = ip
                if not hp_uri_for(p) and shutil.which("hp-makeuri") and (is_hp(p.model) or is_hp(" ".join(p.uris))):
                    u = hp_uri_from_ip(ip)
                    if u:
                        p.uris.append(u)
            elif p.host and not re.fullmatch(r"[\w.-]+", p.host):
                p.host = ""   # kein brauchbarer Hostname (z.B. mDNS-Dienstname)
    # Fax-Warteschlangen dem Geraet zuordnen
    for name, q in queues.items():
        uri = q.get("device-uri", "")
        if uri.startswith("hpfax:"):
            target = uri.replace("hpfax:", "hp:")
            serial = uri_ids(uri)[0]
            p = next((p for p in printers if target in p.uris or (serial and serial == p.serial)), None)
            if p:
                p.fax_queue = name
    return printers


STATUS_ATTRS = ["printer-state", "printer-state-reasons", "printer-state-message", "marker-names", "marker-levels",
                "marker-colors", "marker-types", "sides-supported", "print-color-mode-supported", "media-supported",
                "print-quality-supported", "printer-make-and-model"]


def query_status(p):
    """Status und Tinte: erst ueber IPP (CUPS), sonst ueber HPLIP."""
    res = {"state": "", "reasons": [], "message": "", "markers": [], "supported": {}}
    attrs = None
    if cups is not None:
        conn = cups.Connection()
        try:
            if p.queue:
                attrs = conn.getPrinterAttributes(p.queue, requested_attributes=STATUS_ATTRS)
            elif re.fullmatch(r"[\d.]+", p.host or ""):
                # pycups fragt sonst den lokalen CUPS-Dienst; der Drucker ist selbst ein IPP-Server
                attrs = cups.Connection(host=p.host, port=631).getPrinterAttributes(
                    uri=f"ipp://{p.host}:631/ipp/print", requested_attributes=STATUS_ATTRS)
        except (cups.IPPError, RuntimeError):   # RuntimeError: Drucker gerade nicht erreichbar
            attrs = None
    if attrs:
        state = {3: "Bereit", 4: "Druckt", 5: "Angehalten"}.get(attrs.get("printer-state"), "")
        res["state"] = state
        res["message"] = attrs.get("printer-state-message", "")
        res["reasons"] = [r for r in _list(attrs.get("printer-state-reasons")) if r != "none"]
        names, levels = _list(attrs.get("marker-names")), _list(attrs.get("marker-levels"))
        colors = _list(attrs.get("marker-colors"))
        for i, name in enumerate(names):
            res["markers"].append({"name": name, "level": levels[i] if i < len(levels) else -1,
                                   "color": colors[i] if i < len(colors) else ""})
        for key in ("sides-supported", "print-color-mode-supported", "media-supported", "print-quality-supported"):
            res["supported"][key] = _list(attrs.get(key))
    # CUPS kennt die Tinte einer Warteschlange oft erst nach dem ersten Auftrag: dann den Drucker direkt fragen
    if not res["markers"] and cups is not None and re.fullmatch(r"[\d.]+", p.host or ""):
        try:
            direct = cups.Connection(host=p.host, port=631).getPrinterAttributes(
                uri=f"ipp://{p.host}:631/ipp/print",
                requested_attributes=["marker-names", "marker-levels", "marker-colors", "printer-state-reasons"])
            res["reasons"] += [r for r in _list(direct.get("printer-state-reasons"))
                               if r != "none" and r not in res["reasons"]]
            names, levels = _list(direct.get("marker-names")), _list(direct.get("marker-levels"))
            colors = _list(direct.get("marker-colors"))
            for i, name in enumerate(names):
                res["markers"].append({"name": name, "level": levels[i] if i < len(levels) else -1,
                                       "color": colors[i] if i < len(colors) else ""})
        except (cups.IPPError, RuntimeError):
            pass
    hp = hp_uri_for(p)
    if not res["markers"] and hp:
        data = hplip("levels", hp)
        if data:
            for a in data.get("agents", []):
                if a["kind"] in (1, 3, 5, 6) or a["desc"]:   # Tinte/Toner (HPLIP-Agentenarten)
                    res["markers"].append({"name": a["desc"] or "Tinte", "level": a["level"], "color": ""})
            res["state"] = res["state"] or data.get("status", "")
    return res


def _list(v):
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


REASONS_DE = {
    "media-empty": "Papier leer", "media-jam": "Papierstau", "media-needed": "Papier einlegen",
    "door-open": "Klappe offen", "cover-open": "Deckel offen", "marker-supply-low": "Tinte fast leer",
    "marker-supply-empty": "Tinte leer", "toner-low": "Toner fast leer", "toner-empty": "Toner leer",
    "offline": "Offline", "paused": "Angehalten", "connecting-to-device": "Verbindet…",
    "input-tray-missing": "Papierfach fehlt", "output-area-full": "Ausgabefach voll",
}


def reason_text(r):
    base = re.sub(r"-(report|warning|error)$", "", r)
    return REASONS_DE.get(base, base)


INK_DE = {"black": "Schwarz", "cyan": "Cyan", "magenta": "Magenta", "yellow": "Gelb", "photo black": "Fotoschwarz",
          "tri-color": "Dreifarbig", "color": "Farbe", "gray": "Grau", "light cyan": "Hellcyan", "light magenta": "Hellmagenta"}


def ink_name(name):
    base = re.sub(r"\s*(ink|toner|cartridge|patrone)s?\s*$", "", (name or "").strip(), flags=re.I).lower()
    return INK_DE.get(base, name or "Tinte")


def marker_color(m):
    c = m.get("color") or ""
    hexes = re.findall(r"#[0-9A-Fa-f]{6}", c)
    if hexes:
        return hexes
    n = (m.get("name") or "").lower()
    for keys, col in ((("black", "schwarz", "photo black"), ["#222222"]), (("cyan",), ["#00AEEF"]),
                      (("magenta",), ["#EC008C"]), (("yellow", "gelb"), ["#FFD400"]),
                      (("tri", "color", "farb"), ["#00AEEF", "#EC008C", "#FFD400"])):
        if any(k in n for k in keys):
            return col
    return ["#888888"]


# ---------- Einrichten ----------
def setup_printer(p):
    uri = p.best_setup_uri()
    if uri.startswith("hp:") and not any(u.startswith(("ipp", "dnssd")) for u in p.uris) and shutil.which("hp-setup"):
        # Aeltere HP-Geraete: HPLIPs eigener Assistent waehlt Treiber (und bei Bedarf Fax) richtig
        subprocess.Popen(["hp-setup", uri], start_new_session=True)
        return "HPLIP-Einrichtung gestartet – bitte dort fertigstellen und danach „Drucker suchen“ drücken."
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", p.model or p.info or "Drucker").strip("_")[:60] or "Drucker"
    cmd = ["pkexec", "lpadmin", "-p", name, "-E", "-v", uri, "-m", "everywhere"]
    if not uri.startswith(("ipp://", "ipps://", "dnssd://")):
        # Ohne IPP (aeltere USB- oder Netzwerkdrucker): passenden Treiber aus allen installierten suchen
        # (HPLIP, Gutenprint, Foomatic …). CUPS markiert den empfohlenen.
        rc, out, _ = run(["lpinfo", "--make-and-model", p.model or p.info, "-m"], 30)
        lines = [l for l in out.splitlines() if l.strip()]
        best = next((l for l in lines if "recommended" in l.lower()), None) or \
            next((l for l in lines if "hpcups" in l or "gutenprint" in l.lower()), None) or (lines[0] if lines else None)
        if not best:
            raise RuntimeError("Kein passender Treiber installiert. Neuere Drucker gehen treiberlos über "
                               "Netzwerk oder „ipp-usb“; für ältere hilft der Treiber des Herstellers.")
        cmd[-1] = best.split()[0]
    rc, out, err = run(cmd, 120)
    if rc != 0:
        raise RuntimeError((err or out or "lpadmin fehlgeschlagen").strip())
    return f"Eingerichtet als „{name}“."


# ---------- Wartung ----------
def _ssl_ctx():
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # Drucker haben selbst ausgestellte Zertifikate
    return ctx


def ledm_jobs(ip):
    """HP-Geraeteschnittstelle (LEDM): welche Wartungs- und Berichtsseiten der Drucker selbst drucken kann."""
    for url in (f"https://{ip}/DevMgmt/InternalPrintCap.xml", f"http://{ip}/DevMgmt/InternalPrintCap.xml"):
        try:
            with urllib.request.urlopen(url, timeout=6, context=_ssl_ctx() if url.startswith("https") else None) as r:
                return re.findall(r"<ipdyn:JobType>([A-Za-z0-9]+)</ipdyn:JobType>", r.read().decode("utf-8", "replace"))
        except Exception:
            continue
    return []


# Aufbau wie in HPLIP (base/maint.py, CleanXML)
LEDM_JOB_XML = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<ipcap:InternalPrintCap xmlns:ipcap="http://www.hp.com/schemas/imaging/con/ledm/internalprintcap/2008/03/21" '
                'xmlns:ipdyn="http://www.hp.com/schemas/imaging/con/ledm/internalprintdyn/2008/03/21" '
                'xmlns:dd="http://www.hp.com/schemas/imaging/con/dictionaries/1.0/" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
                '<ipdyn:JobType>%s</ipdyn:JobType>\n</ipcap:InternalPrintCap>')


def ledm_run(ip, job):
    last = None
    for url in (f"https://{ip}/DevMgmt/InternalPrintDyn.xml", f"http://{ip}/DevMgmt/InternalPrintDyn.xml"):
        req = urllib.request.Request(url, data=(LEDM_JOB_XML % job).encode(), method="POST",
                                     headers={"Content-Type": "text/xml"})
        try:
            with urllib.request.urlopen(req, timeout=15, context=_ssl_ctx() if url.startswith("https") else None):
                return
        except urllib.error.HTTPError as e:
            last = f"Drucker lehnt ab (HTTP {e.code})"
            break
        except Exception as e:
            last = str(e)
    raise RuntimeError(last or "Drucker nicht erreichbar")


LEDM_LABELS = {
    "cleaningPage": ("quality", "Druckkopf reinigen – Stufe 1", "Bei Streifen oder blassen Farben. Druckt eine Seite."),
    "cleaningPageLevel1": ("quality", "Druckkopf reinigen – Stufe 2", "Gründlicher, braucht mehr Tinte."),
    "cleaningPageLevel2": ("quality", "Druckkopf reinigen – Stufe 2", "Gründlicher, braucht mehr Tinte."),
    "cleaningPageLevel3": ("quality", "Druckkopf reinigen – Stufe 3", "Nur wenn Stufe 2 nicht reicht – viel Tinte."),
    "cleaningVerificationPage": ("quality", "Reinigungs-Prüfseite", "Zeigt, ob alle Düsen wieder drucken."),
    "pqDiagnosticsPage": ("quality", "Druckqualitäts-Diagnose", "Testseite zum Beurteilen von Farben und Streifen."),
    "lineFeedCalibrationPage": ("quality", "Zeilenvorschub kalibrieren", "Gegen helle oder dunkle Querstreifen."),
    "configurationPage": ("report", "Druckerstatusbericht", "Modell, Firmware, Füllstände, Einstellungen."),
    "usagePage": ("report", "Nutzungsseite", "Gedruckte Seiten und Verbrauch."),
    "diagnosticsPage": ("report", "Diagnoseseite", "Technische Angaben für die Fehlersuche."),
    "networkDiagnosticPage": ("report", "Netzwerk-Testbericht", "Prüft LAN/WLAN-Verbindung."),
    "networkSummary": ("report", "Netzwerkkonfiguration", "IP-Adresse und Netzwerkeinstellungen."),
    "wirelessNetworkPage": ("report", "WLAN-Testbericht", "Signal und WLAN-Einstellungen."),
    "faxConfigurationReport": ("report", "Fax-Konfigurationsbericht", ""),
    "faxActivityLog": ("report", "Faxprotokoll", "Gesendete und empfangene Faxe."),
    "faxLastCallReport": ("report", "Letzter Faxvorgang", ""),
    "faxErrorReport": ("report", "Fax-Fehlerbericht", ""),
}

HPLIP_TOOLS = [("clean", "hp-clean", "Druckköpfe reinigen", "Bei Streifen oder blassen Farben."),
               ("align", "hp-align", "Druckköpfe ausrichten", "Bei versetzten oder doppelten Linien."),
               ("color-cal", "hp-colorcal", "Farbkalibrierung", "Wenn Farben nicht stimmen."),
               ("linefeed-cal", "hp-linefeedcal", "Zeilenvorschub kalibrieren", "Gegen Querstreifen."),
               ("pq-diag", "hp-pqdiag", "Druckqualitäts-Diagnose", "Testseite zum Beurteilen der Qualität.")]

CUPS_COMMANDS = {"Clean": ("Druckkopf reinigen", "Reinigung über den Druckertreiber."),
                 "PrintSelfTestPage": ("Selbsttestseite", "Der Drucker druckt seine eigene Testseite."),
                 "AutoConfigure": ("Optionen neu erkennen", "Fragt installierte Fächer usw. beim Drucker ab.")}


def cups_commands(queue):
    try:
        a = cups.Connection().getPrinterAttributes(queue, requested_attributes=["printer-commands"])
    except Exception:
        return []
    cmds = []
    for c in _list(a.get("printer-commands")):
        cmds += [x.strip() for x in str(c).split(",") if x.strip() and x.strip() != "none"]
    return cmds


def cups_command(queue, cmd):
    """Wartungsbefehl ueber CUPS (Datei mit #CUPS-COMMAND erkennt CUPS selbst)."""
    path = os.path.join(tempfile.mkdtemp(prefix="dz-cmd-"), "befehl")
    with open(path, "w") as f:
        f.write(f"#CUPS-COMMAND\n{cmd}{' all' if cmd == 'Clean' else ''}\n")
    return cups.Connection().printFile(queue, path, cmd, {})


def setup_fax_queue(p):
    """Fax-Warteschlange wie HPLIP sie anlegt: hpfax:-Adresse und der Faxtreiber, den HPLIP zum Modell waehlt."""
    uri = hp_uri_for(p)
    if not uri:
        raise RuntimeError("HPLIP kennt diesen Drucker nicht.")
    data = hplip("faxppd", uri)
    ppd = (data or {}).get("ppd")
    if not ppd:
        raise RuntimeError("Kein HP-Faxtreiber gefunden (HPLIP vollständig installiert?).")
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", (p.queue or p.model or "HP")).strip("_")[:50] + "_Fax"
    model = re.sub(r"\s+-\s+.*$", "", p.model or "HP")
    cmd = ["pkexec", "lpadmin", "-p", name, "-E", "-v", uri.replace("hp:", "hpfax:", 1),
           "-P" if os.path.exists(ppd) else "-m", ppd, "-D", f"{model} Fax"]
    rc, out, err = run(cmd, 120)
    if rc != 0:
        raise RuntimeError((err or out or "lpadmin fehlgeschlagen").strip())
    return name


def remove_queue(name):
    rc, out, err = run(["pkexec", "lpadmin", "-x", name], 60)
    if rc != 0:
        raise RuntimeError((err or out or "lpadmin fehlgeschlagen").strip())


# ---------- Scannen ----------
def list_scanners():
    rc, out, err = run(["scanimage", "-L"], 60)
    if rc == -1:
        raise RuntimeError(err)
    found = []
    for m in re.finditer(r"device `([^']+)' is a (.+)", out):
        dev, desc = m.group(1), m.group(2).strip()
        if dev.startswith(("v4l:", "gphoto2:")):
            continue   # Webcams und Kameras sind keine Scanner
        found.append((dev, desc))
    # airscan (eSCL) zuerst: funktioniert bei Netzwerk und IPP-over-USB treiberlos. Dasselbe Geraet ueber
    # HPLIP (hpaio) nur behalten, wenn es keinen airscan-Weg dafuer gibt.
    found.sort(key=lambda d: (not d[0].startswith("airscan"), d[1]))
    seen, unique = set(), []
    for dev, desc in found:
        words = re.findall(r"\d{3,}", desc + " " + dev)   # Modellnummer, z.B. 8620
        key = words[0] if words else dev
        if key in seen:
            continue
        seen.add(key)
        unique.append((dev, desc))
    return unique


COMMON_DPI = [75, 100, 150, 200, 300, 600, 1200]


def scanner_options(dev):
    rc, out, err = run(["scanimage", "-d", dev, "-A"], 60)
    opts = {}
    for line in out.splitlines():
        m = re.match(r"\s+--(mode|resolution|source)\s+(.+?)\s*\[(.*?)\]\s*$", line)
        if not m:
            continue
        name, vals, cur = m.groups()
        vals = re.sub(r"dpi.*$", "", vals).strip()
        if ".." in vals:
            lo, hi = (int(float(x)) for x in re.findall(r"[\d.]+", vals)[:2])
            choices = [str(d) for d in COMMON_DPI if lo <= d <= hi]
        else:
            choices = [v.strip() for v in vals.split("|") if v.strip()]
        opts[name] = (choices, re.sub(r"dpi$", "", cur))
    return opts


SCAN_AREAS = [("Gesamter Scanbereich", None), ("A4", (210, 297)), ("A5", (148, 210)), ("Letter", (215.9, 279.4)),
              ("Foto 10×15", (101.6, 152.4)), ("Foto 13×18", (127, 178))]


def autocrop(path):
    """Kanten erkennen: weissen/hellen Rand um das Dokument abschneiden."""
    im = Image.open(path)
    gray = im.convert("L")
    mask = gray.point(lambda v: 255 if v < 235 else 0)
    box = mask.getbbox()
    if not box:
        return
    pad = 8
    box = (max(0, box[0] - pad), max(0, box[1] - pad), min(im.width, box[2] + pad), min(im.height, box[3] + pad))
    if (box[2] - box[0]) * (box[3] - box[1]) < 0.97 * im.width * im.height:
        im.crop(box).save(path)


def scan(dev, mode, res, source, area=None, crop=False):
    tmp = tempfile.mkdtemp(prefix="hp-scan-")
    args = ["scanimage", "-d", dev, "--format=png"]
    if area:
        args += ["-l", "0", "-t", "0", "-x", str(area[0]), "-y", str(area[1])]
    if mode:
        args += ["--mode", mode]
    if res:
        args += ["--resolution", res]
    if source:
        args += ["--source", source]
    feeder = bool(source) and re.search(r"adf|feeder|einzug|duplex", source, re.I)
    if feeder:
        args.append(f"--batch={tmp}/seite%03d.png")
    else:
        args += ["-o", f"{tmp}/seite001.png"]
    rc, out, err = run(args, 600, cwd=tmp)
    files = sorted(os.path.join(tmp, f) for f in os.listdir(tmp) if f.endswith(".png"))
    if not files:
        raise RuntimeError((err or out or "Scan fehlgeschlagen").strip().splitlines()[-1])
    if crop and Image is not None:
        for f in files:
            autocrop(f)
    return files


def adf_loaded(ip):
    """eSCL: liegt Papier im Vorlageneinzug? True/False, None wenn unbekannt."""
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for url in (f"https://{ip}/eSCL/ScannerStatus", f"http://{ip}/eSCL/ScannerStatus"):
        try:
            with urllib.request.urlopen(url, timeout=4, context=ctx if url.startswith("https") else None) as r:
                m = re.search(rb"<scan:AdfState>(\w+)</scan:AdfState>", r.read())
            return None if not m else m.group(1) == b"ScannerAdfLoaded"
        except Exception:
            continue
    return None


def scanner_ip(dev, desc):
    m = re.search(r"ip=([\d.]+)", desc or "") or re.search(r"ip=([\d.]+)", dev or "")
    return m.group(1) if m else None


SAVE_FORMATS = [("PDF", "pdf"), ("JPG", "jpg"), ("PNG", "png"), ("BMP", "bmp"), ("TIFF", "tiff"), ("WEBP", "webp")]


def save_pages(files, path, fmt, dpi):
    imgs = [Image.open(f) for f in files]
    if fmt in ("pdf", "tiff"):
        conv = [im.convert("RGB") for im in imgs]
        kw = {"save_all": True, "append_images": conv[1:]}
        if fmt == "pdf":
            kw["resolution"] = float(dpi or 300)
        else:
            kw["dpi"] = (int(dpi or 300),) * 2
        conv[0].save(path, "PDF" if fmt == "pdf" else "TIFF", **kw)
        return [path]
    out = []
    base, _ = os.path.splitext(path)
    for i, im in enumerate(imgs, 1):
        target = path if len(imgs) == 1 else f"{base}_{i}.{fmt}"
        im = im.convert("RGB") if fmt in ("jpg", "bmp") else im
        im.save(target, {"jpg": "JPEG"}.get(fmt, fmt.upper()), **({"quality": 92} if fmt in ("jpg", "webp") else {}))
        out.append(target)
    return out


# ---------- Updater: neuestes Release auf GitHub, ersetzt die installierte Programmdatei ----------
def ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def latest_release():
    req = urllib.request.Request(f"https://github.com/{UPDATE_REPO}/releases/latest", method="HEAD")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.geturl().rstrip("/").rsplit("/", 1)[-1]


def install_update(tag):
    me = os.path.realpath(__file__)
    url = f"https://github.com/{UPDATE_REPO}/releases/download/{tag}/hp_druckzentrale.py"
    with urllib.request.urlopen(url, timeout=60) as r:
        data = r.read()
    compile(data, "hp_druckzentrale.py", "exec")   # kaputter Download ersetzt nie die laufende Fassung
    tmp = me + ".neu"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, me)


# ---------- Oberflaeche ----------
class InkBar(QtWidgets.QWidget):
    def __init__(self, marker):
        super().__init__()
        self.m = marker
        self.setMinimumHeight(30)

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = self.rect().adjusted(150, 6, -60, -6)
        p.setPen(self.palette().color(QtGui.QPalette.WindowText))
        p.drawText(QtCore.QRect(0, 0, 145, self.height()), QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft,
                   self.fontMetrics().elidedText(ink_name(self.m["name"]), QtCore.Qt.ElideRight, 145))
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(self.palette().color(QtGui.QPalette.Mid))
        p.drawRoundedRect(r, 5, 5)
        level = self.m["level"]
        text = f"{level} %" if level >= 0 else ("vorhanden" if level == -3 else "unbekannt")
        if level >= 0 or level == -3:
            fill = QtCore.QRectF(r)
            fill.setWidth(r.width() * (max(level, 4) if level >= 0 else 50) / 100)
            cols = marker_color(self.m)
            if len(cols) == 1:
                p.setBrush(QtGui.QColor(cols[0]))
            else:
                g = QtGui.QLinearGradient(fill.topLeft(), fill.topRight())
                for i, c in enumerate(cols):
                    g.setColorAt(i / max(1, len(cols) - 1), QtGui.QColor(c))
                p.setBrush(g)
            p.drawRoundedRect(fill, 5, 5)
        p.setPen(self.palette().color(QtGui.QPalette.WindowText))
        p.drawText(QtCore.QRect(self.width() - 55, 0, 55, self.height()), QtCore.Qt.AlignVCenter | QtCore.Qt.AlignRight, text)


class SetupWizard(QtWidgets.QDialog):
    """Erster Start: Drucker einschalten -> suchen (USB und Netz) -> auswaehlen -> einrichten -> Testseite."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} – Einrichtung")
        self.resize(640, 460)
        self.found = []
        self.chosen = None
        lay = QtWidgets.QVBoxLayout(self)
        self.stack = QtWidgets.QStackedWidget()
        lay.addWidget(self.stack, 1)
        nav = QtWidgets.QHBoxLayout()
        self.skip = QtWidgets.QPushButton("Überspringen")
        self.skip.clicked.connect(self.reject)
        self.next = QtWidgets.QPushButton("Weiter")
        self.next.setDefault(True)
        self.next.clicked.connect(self.forward)
        nav.addWidget(self.skip)
        nav.addStretch(1)
        nav.addWidget(self.next)
        lay.addLayout(nav)

        # 1: Drucker vorbereiten
        p1 = QtWidgets.QWidget()
        l1 = QtWidgets.QVBoxLayout(p1)
        t = QtWidgets.QLabel("Willkommen bei der Druckzentrale")
        t.setStyleSheet("font-size: 20px; font-weight: bold;")
        l1.addWidget(t)
        txt = QtWidgets.QLabel(
            "Zuerst richten wir deinen Drucker ein.\n\n"
            "1.  Drucker einschalten und warten, bis er bereit ist.\n"
            "2.  Verbinden – eins von beiden:\n"
            "      •  USB: Kabel an diesen PC anstecken\n"
            "      •  Netzwerk: Drucker per LAN-Kabel oder WLAN mit demselben Netz verbinden wie diesen PC\n"
            "         (WLAN am Drucker einrichten: über sein Bedienfeld oder die WPS-Taste am Router)\n\n"
            "Dann auf „Drucker suchen“ klicken.")
        txt.setWordWrap(True)
        l1.addWidget(txt)
        l1.addStretch(1)
        self.stack.addWidget(p1)

        # 2: Suche und Auswahl
        p2 = QtWidgets.QWidget()
        l2 = QtWidgets.QVBoxLayout(p2)
        self.search_label = QtWidgets.QLabel("Suche Drucker über USB und im Netzwerk …")
        self.search_label.setWordWrap(True)
        l2.addWidget(self.search_label)
        self.busy = QtWidgets.QProgressBar()
        self.busy.setRange(0, 0)
        l2.addWidget(self.busy)
        self.flist = QtWidgets.QListWidget()
        self.flist.currentRowChanged.connect(lambda r: self.next.setEnabled(r >= 0))
        self.flist.itemDoubleClicked.connect(lambda _: self.forward())
        l2.addWidget(self.flist, 1)
        again = QtWidgets.QPushButton(QtGui.QIcon.fromTheme("view-refresh"), "Erneut suchen")
        again.clicked.connect(self.search)
        l2.addWidget(again, 0, QtCore.Qt.AlignLeft)
        self.stack.addWidget(p2)

        # 3: Einrichten
        p3 = QtWidgets.QWidget()
        l3 = QtWidgets.QVBoxLayout(p3)
        self.setup_label = QtWidgets.QLabel()
        self.setup_label.setWordWrap(True)
        l3.addWidget(self.setup_label)
        self.setup_busy = QtWidgets.QProgressBar()
        self.setup_busy.setRange(0, 0)
        l3.addWidget(self.setup_busy)
        self.test_btn = QtWidgets.QPushButton(QtGui.QIcon.fromTheme("document-print"), "Testseite drucken")
        self.test_btn.clicked.connect(self.test_page)
        self.test_btn.hide()
        l3.addWidget(self.test_btn, 0, QtCore.Qt.AlignLeft)
        l3.addStretch(1)
        self.stack.addWidget(p3)
        self.next.setText("Drucker suchen")

    def forward(self):
        i = self.stack.currentIndex()
        if i == 0:
            self.stack.setCurrentIndex(1)
            self.search()
        elif i == 1:
            row = self.flist.currentRow()
            if 0 <= row < len(self.found):
                self.chosen = self.found[row]
                self.stack.setCurrentIndex(2)
                self.do_setup()
        else:
            self.accept()

    def search(self):
        self.flist.clear()
        self.busy.show()
        self.next.setEnabled(False)
        self.search_label.setText("Suche Drucker über USB und im Netzwerk … (bis zu 15 Sekunden)")

        def done(ok, res):
            self.busy.hide()
            self.found = res if ok else []
            if not ok:
                self.search_label.setText(f"Suche fehlgeschlagen: {res}")
                return
            if not res:
                self.search_label.setText(
                    "Kein Drucker gefunden.\n\nIst er eingeschaltet? Bei USB: Kabel ab- und wieder anstecken. "
                    "Im Netzwerk: hängt er im selben WLAN/LAN wie dieser PC? Dann „Erneut suchen“.")
                return
            self.search_label.setText("Gefunden – Drucker auswählen und „Einrichten“ klicken:")
            for p in res:
                state = "schon eingerichtet" if p.queue else "noch nicht eingerichtet"
                self.flist.addItem(QtWidgets.QListWidgetItem(QtGui.QIcon.fromTheme("printer"),
                                                             f"{p.title}\n{p.connection} · {state}"))
            self.flist.setCurrentRow(next((i for i, p in enumerate(res) if not p.queue), 0))
            self.next.setText("Einrichten")
        bg(discover, done)

    def do_setup(self):
        p = self.chosen
        self.skip.setEnabled(False)
        self.next.setEnabled(False)
        if p.queue:
            self.finish(True, f"„{p.title}“ ist schon eingerichtet.")
            return
        self.setup_label.setText(f"Richte „{p.title}“ ein … Gleich fragt ein Fenster nach deinem Passwort.")

        def done(ok, res):
            if ok and "HPLIP" in res:
                self.finish(True, res)
                return
            self.finish(ok, res if ok else f"Einrichten fehlgeschlagen: {res}")
        bg(lambda: setup_printer(p), done)

    def finish(self, ok, text):
        self.setup_busy.hide()
        self.skip.setEnabled(True)
        self.next.setEnabled(True)
        self.next.setText("Fertig")
        self.setup_label.setText(text + ("\n\nAls Nächstes kannst du eine Testseite drucken." if ok else
                                         "\n\n„Fertig“ schließt die Einrichtung; sie lässt sich oben über „Einrichtung“ "
                                         "jederzeit erneut starten."))
        if ok:
            def find_queue():
                for q in discover():
                    if q.queue and (set(q.uris) & set(self.chosen.uris) or norm(q.model) == norm(self.chosen.model)):
                        return q.queue
                return None
            bg(find_queue, lambda ok2, q: (setattr(self, "queue", q), self.test_btn.setVisible(bool(ok2 and q))))

    def test_page(self):
        test = next((f for f in ("/usr/share/cups/data/testprint", "/usr/share/cups/data/default-testpage.pdf")
                     if os.path.exists(f)), None)
        if test and getattr(self, "queue", None):
            q = self.queue
            bg(lambda: cups.Connection().printFile(q, test, "Testseite", {}),
               lambda ok, res: self.setup_label.setText(self.setup_label.text() + (
                   "\n\nTestseite gesendet." if ok else f"\n\nTestseite fehlgeschlagen: {res}")))


# ---------- Optik: schlicht, folgt dem Systemthema (hell/dunkel wie im Desktop eingestellt) ----------
QSS = """
QFrame#group { border: 1px solid palette(mid); border-radius: 8px; background: palette(base); }
QLabel#title { font-size: 24px; font-weight: bold; }
QLabel#h2 { font-size: 15px; font-weight: bold; }
QLabel#dim { color: #8a8a8a; }
QListWidget#nav, QListWidget#printers { border: none; background: transparent; }
QListWidget#nav::item, QListWidget#printers::item { padding: 7px 8px; border-radius: 6px; }
QListWidget#nav::item:selected, QListWidget#printers::item:selected {
    background: palette(highlight); color: palette(highlighted-text); }
QPushButton#primary { background: palette(highlight); color: palette(highlighted-text); border: none;
    border-radius: 6px; padding: 7px 16px; font-weight: bold; }
QPushButton#primary:disabled { background: palette(mid); }
"""

STATE_COLORS = {"ok": "#2e9e5b", "warn": "#d97706", "off": "#9a9a9a", "err": "#d14343"}


def dot_icon(color):
    pix = QtGui.QPixmap(14, 14)
    pix.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pix)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(QtGui.QColor(color))
    p.drawEllipse(2, 2, 10, 10)
    p.end()
    return QtGui.QIcon(pix)


def group(title=None):
    """Umrandete Gruppe mit optionaler Ueberschrift."""
    f = QtWidgets.QFrame()
    f.setObjectName("group")
    lay = QtWidgets.QVBoxLayout(f)
    lay.setContentsMargins(18, 14, 18, 16)
    lay.setSpacing(8)
    if title:
        h = QtWidgets.QLabel(title)
        h.setObjectName("h2")
        lay.addWidget(h)
    return f, lay


def printer_key(p):
    for u in p.uris:
        m = re.search(r"uuid=([^&]+)", u)
        if m:
            return m.group(1)
    return p.serial or p.host or p.uri


def is_toner(markers):
    return any("toner" in (m.get("name") or "").lower() for m in markers)


class MainWindow(QtWidgets.QMainWindow):
    OVERVIEW, PRINT, SCAN, FAX, MAINT = range(5)
    HOME = OVERVIEW
    NAV = [("Übersicht", "go-home"), ("Drucken", "document-print"), ("Scannen", "scanner"),
           ("Fax", "mail-send"), ("Wartung", "configure")]

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1180, 800)
        self.setStyleSheet(QSS)
        self.printers = []
        self.current = None
        self.scanners = []
        self.pages = []          # PNG-Dateien der aktuellen Scans
        self.scan_saved = True   # False, solange gescannte Seiten nicht gespeichert sind
        self.scan_dpi = "300"
        self.status_cache = {}   # Drucker-Schluessel -> letzter Status
        self.settings = QtCore.QSettings("hp-druckzentrale", "hp-druckzentrale")

        self.act_search = QtGui.QAction("Drucker suchen", self, triggered=self.search)
        self.act_setup = QtGui.QAction("Einrichten", self, triggered=self.setup_current)
        self.act_test = QtGui.QAction("Testseite", self, triggered=self.test_page)
        self.act_web = QtGui.QAction("Weboberfläche", self, triggered=self.open_web)
        self.act_remove = QtGui.QAction("Entfernen", self, triggered=self.remove_current)
        self.update_tag = None

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.build_sidebar())
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.VLine)
        line.setFrameShadow(QtWidgets.QFrame.Sunken)
        root.addWidget(line)
        right = QtWidgets.QVBoxLayout()
        right.setContentsMargins(22, 16, 22, 8)
        self.banner = QtWidgets.QVBoxLayout()
        right.addLayout(self.banner)
        self.stack = QtWidgets.QStackedWidget()
        right.addWidget(self.stack, 1)
        root.addLayout(right, 1)

        self.ov_scroll, self.ov_body = self.scroll_page()
        self.stack.addWidget(self.ov_scroll)
        self.tab_print = self.build_print_tab()
        self.tab_scan = self.build_scan_tab()
        self.tab_fax = self.build_fax_tab()
        self.stack.addWidget(self.titled("Drucken", self.tab_print))
        self.stack.addWidget(self.titled("Scannen", self.tab_scan, framed=False))
        self.stack.addWidget(self.titled("Fax", self.tab_fax))
        self.mt_scroll, self.mt_body = self.scroll_page()
        self.stack.addWidget(self.mt_scroll)

        self.status = self.statusBar()
        self.check_dependencies()
        self.build_overview()
        self.build_maintenance()
        self.go(self.OVERVIEW)
        QtCore.QTimer.singleShot(200, self.search)
        QtCore.QTimer.singleShot(400, self.refresh_scanners)
        QtCore.QTimer.singleShot(1500, self.check_update)
        if not self.settings.value("setup_done", False, type=bool) and not os.environ.get("HPDZ_SELFTEST"):
            QtCore.QTimer.singleShot(300, self.run_wizard)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh_status)
        self.timer.start(30000)
        self.adf_timer = QtCore.QTimer(self)
        self.adf_timer.timeout.connect(self.poll_adf)
        self.adf_timer.start(3000)

    # ----- Rahmen -----
    def build_sidebar(self):
        side = QtWidgets.QWidget()
        side.setFixedWidth(250)
        lay = QtWidgets.QVBoxLayout(side)
        lay.setContentsMargins(12, 16, 12, 12)
        head = QtWidgets.QHBoxLayout()
        icon = QtWidgets.QLabel()
        icon.setPixmap(QtGui.QIcon.fromTheme("printer").pixmap(28, 28))
        head.addWidget(icon)
        t = QtWidgets.QLabel(APP_NAME)
        t.setStyleSheet("font-size: 16px; font-weight: bold;")
        head.addWidget(t, 1)
        lay.addLayout(head)
        lay.addSpacing(12)
        h = QtWidgets.QLabel("Drucker")
        h.setObjectName("dim")
        lay.addWidget(h)
        self.printer_list = QtWidgets.QListWidget()
        self.printer_list.setObjectName("printers")
        self.printer_list.setMaximumHeight(170)
        self.printer_list.currentRowChanged.connect(self.select)
        lay.addWidget(self.printer_list)
        row = QtWidgets.QHBoxLayout()
        b1 = QtWidgets.QPushButton(QtGui.QIcon.fromTheme("view-refresh"), "Suchen")
        b1.clicked.connect(self.search)
        b2 = QtWidgets.QPushButton(QtGui.QIcon.fromTheme("list-add"), "Hinzufügen")
        b2.clicked.connect(self.run_wizard)
        row.addWidget(b1)
        row.addWidget(b2)
        lay.addLayout(row)
        lay.addSpacing(14)
        self.nav = QtWidgets.QListWidget()
        self.nav.setObjectName("nav")
        for label, icon_name in self.NAV:
            self.nav.addItem(QtWidgets.QListWidgetItem(QtGui.QIcon.fromTheme(icon_name), label))
        self.nav.currentRowChanged.connect(lambda r: r >= 0 and self.stack.setCurrentIndex(r))
        lay.addWidget(self.nav, 1)
        self.update_btn = QtWidgets.QPushButton()
        self.update_btn.setObjectName("primary")
        self.update_btn.clicked.connect(self.update_clicked)
        self.update_btn.hide()
        lay.addWidget(self.update_btn)
        v = QtWidgets.QLabel(f"Version {APP_VERSION}")
        v.setObjectName("dim")
        lay.addWidget(v)
        return side

    def scroll_page(self):
        sc = QtWidgets.QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QtWidgets.QFrame.NoFrame)
        holder = QtWidgets.QWidget()
        body = QtWidgets.QVBoxLayout(holder)
        body.setContentsMargins(0, 0, 8, 0)
        body.setSpacing(14)
        sc.setWidget(holder)
        return sc, body

    def titled(self, title, inner, framed=True):
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        h = QtWidgets.QLabel(title)
        h.setObjectName("title")
        lay.addWidget(h)
        if framed:
            f, fl = group()
            fl.addWidget(inner)
            lay.addWidget(f, 1)
        else:
            lay.addWidget(inner, 1)
        return page

    def go(self, page):
        self.stack.setCurrentIndex(page)
        self.nav.blockSignals(True)
        self.nav.setCurrentRow(page)
        self.nav.blockSignals(False)

    # ----- Uebersicht -----
    def build_overview(self):
        self.clear(self.ov_body)
        p = self.current
        if not p:
            t = QtWidgets.QLabel("Kein Drucker")
            t.setObjectName("title")
            self.ov_body.addWidget(t)
            hint = QtWidgets.QLabel("Drucker einschalten und per USB anstecken oder ins selbe LAN/WLAN bringen, "
                                    "dann links auf „Suchen“ – oder „Hinzufügen“ für den Einrichtungs-Assistenten.")
            hint.setWordWrap(True)
            self.ov_body.addWidget(hint)
            self.ov_body.addStretch(1)
            return
        head = QtWidgets.QHBoxLayout()
        icon = QtWidgets.QLabel()
        icon.setPixmap(QtGui.QIcon.fromTheme("printer").pixmap(72, 72))
        head.addWidget(icon)
        col = QtWidgets.QVBoxLayout()
        t = QtWidgets.QLabel(self.nickname(p))
        t.setObjectName("title")
        col.addWidget(t)
        model = re.sub(r"\s+-\s+.*$", "", p.model or p.info or "")
        m = QtWidgets.QLabel(f"{model}  ·  {p.connection}")
        m.setObjectName("dim")
        col.addWidget(m)
        self.ov_state = QtWidgets.QLabel("Status wird abgefragt …")
        self.ov_state.setStyleSheet("font-weight: bold;")
        col.addWidget(self.ov_state)
        head.addLayout(col, 1)
        if not p.queue:
            b = QtWidgets.QPushButton("Drucker einrichten")
            b.setObjectName("primary")
            b.clicked.connect(self.setup_current)
            head.addWidget(b, 0, QtCore.Qt.AlignTop)
        self.ov_body.addLayout(head)

        self.ov_msgs_box, self.ov_msgs = group("Meldungen")
        self.ov_body.addWidget(self.ov_msgs_box)
        self.ov_msgs_box.hide()
        self.ov_ink_box, self.ov_ink = group("Tinte")
        self.ov_body.addWidget(self.ov_ink_box)
        self.ov_feat_box, self.ov_feat = group("Funktionen")
        self.ov_feat_label = QtWidgets.QLabel("wird erkannt …")
        self.ov_feat_label.setWordWrap(True)
        self.ov_feat.addWidget(self.ov_feat_label)
        self.ov_body.addWidget(self.ov_feat_box)

        dev, dl = group("Gerät")
        form = QtWidgets.QFormLayout()
        form.addRow("Modell", QtWidgets.QLabel(re.sub(r"\s+-\s+.*$", "", p.model or "–")))
        form.addRow("Verbindung", QtWidgets.QLabel(p.connection))
        if p.host:
            form.addRow("Adresse", QtWidgets.QLabel(p.host))
        form.addRow("Warteschlange", QtWidgets.QLabel(p.queue or "noch nicht eingerichtet"))
        nick = QtWidgets.QHBoxLayout()
        nick.addWidget(QtWidgets.QLabel(self.nickname(p)), 1)
        rn = QtWidgets.QPushButton(QtGui.QIcon.fromTheme("document-edit"), "Umbenennen")
        rn.clicked.connect(self.rename)
        nick.addWidget(rn)
        form.addRow("Name", nick)
        dl.addLayout(form)
        self.ov_body.addWidget(dev)
        self.ov_body.addStretch(1)
        cached = self.status_cache.get(printer_key(p))
        if cached:
            self.fill_overview(cached)

    def fill_overview(self, res):
        if not getattr(self, "ov_state", None):
            return
        try:
            state = res.get("state") or ("Erreichbar" if res.get("markers") else "Unbekannt")
            self.ov_state.setText(state)
            self.clear(self.ov_msgs)
            texts = [reason_text(r) for r in res.get("reasons", [])] + ([res["message"]] if res.get("message") else [])
            h = QtWidgets.QLabel("Meldungen")
            h.setObjectName("h2")
            self.ov_msgs.addWidget(h)
            for t in texts:
                lab = QtWidgets.QLabel(f"⚠  {t}")
                lab.setStyleSheet(f"color: {STATE_COLORS['warn']};")
                self.ov_msgs.addWidget(lab)
            self.ov_msgs_box.setVisible(bool(texts))
            self.fill_features(res)
            self.clear(self.ov_ink)
            markers = res.get("markers", [])
            h = QtWidgets.QLabel("Toner" if is_toner(markers) else "Tinte")
            h.setObjectName("h2")
            self.ov_ink.addWidget(h)
            for mk in markers:
                self.ov_ink.addWidget(InkBar(mk))
            if not markers:
                lab = QtWidgets.QLabel("Der Drucker meldet keinen Füllstand." if (self.current and (self.current.queue or self.current.host))
                                       else "Füllstände gibt es nach dem Einrichten.")
                lab.setObjectName("dim")
                self.ov_ink.addWidget(lab)
        except RuntimeError:
            pass   # Seite wurde gerade neu aufgebaut

    def fill_features(self, res=None):
        """Funktionsprofil: was dieser Drucker kann (aus IPP, Scanner-Suche und HPLIP)."""
        p = self.current
        if not p or not getattr(self, "ov_feat_label", None):
            return
        res = res or self.status_cache.get(printer_key(p)) or {}
        sup = res.get("supported", {})
        feats = []
        modes = sup.get("print-color-mode-supported") or []
        if modes:
            feats.append("✓ Farbdruck" if "color" in modes else "✓ Schwarzweißdruck")
        sides = sup.get("sides-supported") or []
        if sides:
            feats.append("✓ Beidseitig drucken" if any(x.startswith("two-sided") for x in sides) else "✗ nur einseitig")
        if self.printer_has_scanner(p):
            feats.append("✓ Scannen" + (" mit Vorlageneinzug" if getattr(self, "duplex_value", None) or
                                        any(re.search(r"adf|feeder", self.scan_source.itemData(i) or "", re.I)
                                            for i in range(self.scan_source.count())) else ""))
        if p.caps and p.caps.get("fax"):
            feats.append("✓ Fax")
        try:
            self.ov_feat_label.setText("     ".join(feats) if feats else "Erst nach dem Einrichten bekannt.")
        except RuntimeError:
            pass

    def printer_has_scanner(self, p):
        words = [w for w in re.findall(r"[a-z0-9]+", (p.model or p.title).lower())
                 if w not in ("hp", "series", "ipp", "everywhere") and len(w) > 2]
        return any(sum(w in (dev + desc).lower() for w in words) >= 1 for dev, desc in self.scanners)

    # ----- Wartung -----
    def build_maintenance(self):
        self.clear(self.mt_body)
        t = QtWidgets.QLabel("Wartung")
        t.setObjectName("title")
        self.mt_body.addWidget(t)
        p = self.current
        # Druckauftraege (alle Marken)
        jobs, jl = group("Druckaufträge")
        self.job_list = QtWidgets.QListWidget()
        self.job_list.setMaximumHeight(130)
        jl.addWidget(self.job_list)
        r = QtWidgets.QHBoxLayout()
        for text, cb in (("Aktualisieren", self.refresh_jobs), ("Ausgewählten abbrechen", self.cancel_job),
                         ("Alle abbrechen", self.cancel_all_jobs)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(cb)
            b.setEnabled(bool(p and p.queue))
            r.addWidget(b)
        r.addStretch(1)
        jl.addLayout(r)
        self.mt_body.addWidget(jobs)
        # Qualitaet und Berichte: werden je nach Drucker gefuellt
        self.mt_quality, self.mt_q = group("Druckkopf und Qualität")
        self.mt_reports, self.mt_r = group("Berichte")
        self.mt_body.addWidget(self.mt_quality)
        self.mt_body.addWidget(self.mt_reports)
        self.mt_reports.hide()
        if p:
            self.add_maint_row(self.mt_q, "document-print", "Testseite drucken", "Prüft, ob Druck und Farben stimmen.",
                               self.test_page, bool(p.queue))
            if p.host:
                self.add_maint_row(self.mt_q, "internet-web-browser", "Weboberfläche des Druckers",
                                   f"Weitere Werkzeuge am Drucker selbst ({p.host}), z. B. Ausrichten.", self.open_web, True)
            self.load_maintenance(p)
        else:
            lab = QtWidgets.QLabel("Kein Drucker gewählt.")
            lab.setObjectName("dim")
            self.mt_q.addWidget(lab)
        f2, fl2 = group("Einrichtung")
        r = QtWidgets.QHBoxLayout()
        a = QtWidgets.QPushButton(QtGui.QIcon.fromTheme("tools-wizard"), "Einrichtungs-Assistent")
        a.clicked.connect(self.run_wizard)
        r.addWidget(a)
        if p and p.queue:
            rm = QtWidgets.QPushButton(QtGui.QIcon.fromTheme("list-remove"), "Drucker von diesem PC entfernen")
            rm.clicked.connect(self.remove_current)
            r.addWidget(rm)
        r.addStretch(1)
        fl2.addLayout(r)
        self.mt_body.addWidget(f2)
        f3, fl3 = group("Programm")
        r = QtWidgets.QHBoxLayout()
        r.addWidget(QtWidgets.QLabel(f"{APP_NAME} {APP_VERSION}"), 1)
        u = QtWidgets.QPushButton(QtGui.QIcon.fromTheme("system-software-update"), "Nach Updates suchen")
        u.clicked.connect(self.manual_update)
        r.addWidget(u)
        fl3.addLayout(r)
        self.mt_body.addWidget(f3)
        self.mt_body.addStretch(1)
        self.refresh_jobs()

    def add_maint_row(self, lay, icon_name, text, desc, cb, enabled=True, button="Ausführen"):
        r = QtWidgets.QHBoxLayout()
        ic = QtWidgets.QLabel()
        ic.setPixmap(QtGui.QIcon.fromTheme(icon_name).pixmap(22, 22))
        r.addWidget(ic)
        col = QtWidgets.QVBoxLayout()
        col.addWidget(QtWidgets.QLabel(text))
        if desc:
            d = QtWidgets.QLabel(desc)
            d.setObjectName("dim")
            col.addWidget(d)
        r.addLayout(col, 1)
        b = QtWidgets.QPushButton(button)
        b.setEnabled(enabled)
        b.clicked.connect(cb)
        r.addWidget(b)
        lay.addLayout(r)

    def load_maintenance(self, p):
        """Was dieser Drucker an Wartung kann: HP-Geraeteschnittstelle, HPLIP-Werkzeuge, CUPS-Befehle."""
        ip = p.host if re.fullmatch(r"[\d.]+", p.host or "") else None
        hp = hp_uri_for(p)

        def work():
            return {"ledm": ledm_jobs(ip) if ip and (is_hp(p.model) or hp) else [],
                    "caps": hplip("caps", hp) if hp else None,
                    "cups": cups_commands(p.queue) if p.queue else []}

        def done(ok, res):
            if not ok or p is not self.current:
                return
            try:
                seen = set()
                for job in res["ledm"]:
                    if job not in LEDM_LABELS or LEDM_LABELS[job][1] in seen:
                        continue
                    kind, text, desc = LEDM_LABELS[job]
                    seen.add(text)
                    lay = self.mt_q if kind == "quality" else self.mt_r
                    self.add_maint_row(lay, "edit-clear" if kind == "quality" else "document-preview", text, desc,
                                       lambda _=False, j=job, t=text: self.run_ledm(ip, j, t),
                                       button="Drucken" if kind == "report" else "Ausführen")
                    if kind == "report":
                        self.mt_reports.show()
                caps = res["caps"] or {}
                if not res["ledm"]:
                    for key, tool, text, desc in HPLIP_TOOLS:
                        if caps.get(key, 0) > 0 and shutil.which(tool):
                            self.add_maint_row(self.mt_q, "edit-clear", text, desc,
                                               lambda _=False, tl=tool: self.hplip_tool(tl))
                for c in res["cups"]:
                    if c in CUPS_COMMANDS and not (c == "Clean" and seen):
                        text, desc = CUPS_COMMANDS[c]
                        self.add_maint_row(self.mt_q, "edit-clear", text, desc,
                                           lambda _=False, cc=c: self.run_cups_command(cc))
            except RuntimeError:
                pass   # Seite wurde inzwischen neu aufgebaut
        bg(work, done)

    def run_ledm(self, ip, job, text):
        if job.startswith("cleaningPage") and QtWidgets.QMessageBox.question(
                self, APP_NAME, f"{text} starten?\n\nDie Reinigung verbraucht Tinte und druckt eine Seite – "
                "Papier einlegen.") != QtWidgets.QMessageBox.Yes:
            return
        self.status.showMessage(f"{text} …")
        bg(lambda: ledm_run(ip, job),
           lambda ok, res: self.status.showMessage(f"{text}: gestartet." if ok else f"{text} fehlgeschlagen: {res}"))

    def run_cups_command(self, cmd):
        p = self.current
        if not p or not p.queue:
            return
        bg(lambda: cups_command(p.queue, cmd),
           lambda ok, res: self.status.showMessage(f"{CUPS_COMMANDS[cmd][0]}: gesendet." if ok else f"Fehlgeschlagen: {res}"))

    def refresh_jobs(self):
        p = self.current
        if not p or not p.queue or not getattr(self, "job_list", None):
            return

        def work():
            jobs = cups.Connection().getJobs(which_jobs="not-completed", requested_attributes=[
                "job-id", "job-name", "job-state", "job-printer-uri"])
            return {jid: j for jid, j in jobs.items() if str(j.get("job-printer-uri", "")).endswith("/" + p.queue)}

        def done(ok, jobs):
            try:
                self.job_list.clear()
                if not ok:
                    self.job_list.addItem(f"Nicht abrufbar: {jobs}")
                    return
                states = {3: "wartet", 4: "angehalten", 5: "druckt", 6: "gestoppt"}
                for jid, j in sorted(jobs.items()):
                    item = QtWidgets.QListWidgetItem(f"#{jid}  {j.get('job-name', '')}  –  {states.get(j.get('job-state'), '')}")
                    item.setData(QtCore.Qt.UserRole, jid)
                    self.job_list.addItem(item)
                if not jobs:
                    self.job_list.addItem("Keine offenen Druckaufträge.")
            except RuntimeError:
                pass
        bg(work, done)

    def cancel_job(self):
        item = self.job_list.currentItem()
        jid = item.data(QtCore.Qt.UserRole) if item else None
        if jid:
            bg(lambda: cups.Connection().cancelJob(jid),
               lambda ok, res: (self.status.showMessage("Auftrag abgebrochen." if ok else f"Abbrechen fehlgeschlagen: {res}"),
                                self.refresh_jobs()))

    def cancel_all_jobs(self):
        p = self.current
        if not p or not p.queue:
            return
        if QtWidgets.QMessageBox.question(self, APP_NAME, "Alle offenen Druckaufträge abbrechen?") != QtWidgets.QMessageBox.Yes:
            return
        bg(lambda: cups.Connection().cancelAllJobs(name=p.queue),
           lambda ok, res: (self.status.showMessage("Alle Aufträge abgebrochen." if ok else f"Fehlgeschlagen: {res}"),
                            self.refresh_jobs()))

    # ----- Druckerliste links -----
    def fill_printer_list(self):
        self.printer_list.blockSignals(True)
        self.printer_list.clear()
        for p in self.printers:
            res = self.status_cache.get(printer_key(p))
            if not p.queue:
                color = STATE_COLORS["off"]
            elif res and res.get("reasons"):
                color = STATE_COLORS["warn"]
            else:
                color = STATE_COLORS["ok"]
            sub = "nicht eingerichtet" if not p.queue else p.connection.split(" ")[0]
            self.printer_list.addItem(QtWidgets.QListWidgetItem(dot_icon(color), f"{self.nickname(p)}\n{sub}"))
        if self.current in self.printers:
            self.printer_list.setCurrentRow(self.printers.index(self.current))
        self.printer_list.blockSignals(False)

    # ----- Drucker suchen und waehlen -----
    def search(self):
        self.act_search.setEnabled(False)
        self.status.showMessage("Suche Drucker (USB und Netzwerk)…")

        def done(ok, res):
            self.act_search.setEnabled(True)
            if not ok:
                self.status.showMessage(f"Suche fehlgeschlagen: {res}")
                return
            old = printer_key(self.current) if self.current else self.settings.value("last_printer", "")
            self.printers = res
            self.status.showMessage(f"{len(res)} Drucker gefunden." if res else
                                    "Kein Drucker gefunden. Ist er eingeschaltet und per USB oder im selben Netz verbunden?")
            idx = next((i for i, p in enumerate(res) if printer_key(p) == old), None)
            if idx is None:
                idx = next((i for i, p in enumerate(res) if p.queue), 0 if res else -1)
            self.current = res[idx] if 0 <= idx < len(res) else None
            self.fill_printer_list()
            self.select(idx)
            for p in res:
                if p is not self.current:
                    self.refresh_status(p)
        bg(discover, done)

    def select(self, row):
        self.current = self.printers[row] if row is not None and 0 <= row < len(self.printers) else None
        p = self.current
        if p:
            self.settings.setValue("last_printer", printer_key(p))
        self.build_overview()
        self.build_maintenance()
        self.update_actions()
        if p:
            self.refresh_status()
            self.refresh_caps()
            self.match_scanner()

    def update_actions(self):
        p = self.current
        self.act_setup.setEnabled(bool(p and not p.queue))
        self.act_test.setEnabled(bool(p and p.queue))
        self.act_remove.setEnabled(bool(p and p.queue))
        self.act_web.setEnabled(bool(p and p.host))
        self.print_btn.setEnabled(bool(p and p.queue))
        self.print_hint.setVisible(bool(p and not p.queue))
        self.nav.item(self.SCAN).setHidden(not self.scanners)
        has_fax = bool(p and p.caps and p.caps.get("fax"))
        self.nav.item(self.FAX).setHidden(not has_fax)
        if not has_fax and self.stack.currentIndex() == self.FAX:
            self.go(self.OVERVIEW)
        self.update_fax_state()

    # ----- Status und Fuellstaende -----
    def refresh_status(self, p=None):
        p = p or self.current
        if not p:
            return

        def done(ok, res):
            if not ok:
                res = {"state": "", "reasons": [], "message": f"Status nicht abrufbar: {res}", "markers": [],
                       "supported": {}}
            self.status_cache[printer_key(p)] = res
            if p is self.current:
                self.fill_overview(res)
                self.apply_supported(res.get("supported", {}))
            if p in self.printers:
                i = self.printers.index(p)
                item = self.printer_list.item(i)
                if item:
                    color = STATE_COLORS["off"] if not p.queue else (STATE_COLORS["warn"] if res.get("reasons") else STATE_COLORS["ok"])
                    item.setIcon(dot_icon(color))
        bg(lambda: query_status(p), done)

    @staticmethod
    def set_icon(btn, name, fallback):
        icon = QtGui.QIcon.fromTheme(name)
        if icon.isNull():
            btn.setText(fallback)   # ohne Symbolthema (z.B. andere Desktops) wenigstens ein Zeichen
        else:
            btn.setIcon(icon)

    def leave_scan_ok(self, discard=False):
        """Ungespeicherte Scans: nachfragen. True = weitermachen (gespeichert oder verworfen)."""
        if self.stack.currentIndex() != self.SCAN and not discard:
            return True
        if not self.page_view.count() or self.scan_saved:
            if discard:
                self.clear_pages(ask=False)
            return True
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setIcon(QtWidgets.QMessageBox.Question)
        box.setText(f"{self.page_view.count()} gescannte Seite(n) sind noch nicht gespeichert.")
        box.setInformativeText("Willst du sie wirklich verwerfen?")
        save = box.addButton("Speichern…", QtWidgets.QMessageBox.AcceptRole)
        drop = box.addButton("Verwerfen", QtWidgets.QMessageBox.DestructiveRole)
        box.addButton("Abbrechen", QtWidgets.QMessageBox.RejectRole)
        box.setDefaultButton(save)
        box.exec()
        if box.clickedButton() is save:
            self.save_scan()
            return False
        if box.clickedButton() is drop:
            self.clear_pages(ask=False)
            return True
        return False

    def closeEvent(self, e):
        if self.page_view.count() and not self.scan_saved:
            self.go(self.SCAN)
            if not self.leave_scan_ok(discard=True):
                e.ignore()
                return
        e.accept()

    @staticmethod
    def clear(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                MainWindow.clear(item.layout())

    def nickname(self, p):
        return self.settings.value(f"nick/{printer_key(p)}", "") or "Mein Drucker"

    def open_print(self, photos):
        self.photo_mode = photos
        if photos:
            i = self.media.findData("na_index-4x6_4x6in")
            if i >= 0:
                self.media.setCurrentIndex(i)
        self.go(self.PRINT)

    def rename(self):
        p = self.current
        if not p:
            return
        name, ok = QtWidgets.QInputDialog.getText(self, "Spitzname", "Name für diesen Drucker:", text=self.nickname(p))
        if ok:
            self.settings.setValue(f"nick/{printer_key(p)}", name.strip())
            self.build_overview()
            self.fill_printer_list()

    def hplip_tool(self, tool):
        uri = hp_uri_for(self.current) if self.current else ""
        if uri:
            subprocess.Popen([tool, "-d", uri], start_new_session=True)
            self.status.showMessage(f"{tool} gestartet.")

    def run_wizard(self):
        wiz = SetupWizard(self)
        wiz.setStyleSheet(QSS)
        wiz.exec()
        self.settings.setValue("setup_done", True)
        self.search()

    def manual_update(self):
        if self.update_tag:
            self.update_clicked()
            return
        self.status.showMessage("Suche nach Updates…")
        self.check_update(manual=True)

    def check_update(self, manual=False):
        def done(ok, tag):
            if manual and not (ok and tag and ver_tuple(tag) > ver_tuple(APP_VERSION)):
                self.status.showMessage(f"v{APP_VERSION} ist aktuell." if ok else f"Update-Prüfung fehlgeschlagen: {tag}")
            if ok and tag and ver_tuple(tag) > ver_tuple(APP_VERSION):
                self.update_tag = tag
                self.update_btn.setText(f"⬆ Update {tag}")
                self.update_btn.show()
                self.status.showMessage(f"Update verfügbar: {tag} (installiert: v{APP_VERSION})")
                if manual:
                    self.update_clicked()
        bg(latest_release, done)

    def update_clicked(self):
        if not self.update_tag:
            return
        if QtWidgets.QMessageBox.question(self, APP_NAME, f"Auf {self.update_tag} aktualisieren?\n\n"
                                          "Das Programm startet danach neu.") != QtWidgets.QMessageBox.Yes:
            return
        self.update_btn.setEnabled(False)
        self.update_btn.setText("Lade Update…")

        def done(ok, res):
            if not ok:
                self.update_btn.setEnabled(True)
                self.update_btn.setText(f"⬆ Update {self.update_tag}")
                QtWidgets.QMessageBox.warning(self, APP_NAME, f"Update fehlgeschlagen: {res}")
                return
            os.execv(sys.executable, [sys.executable, os.path.realpath(__file__)] + sys.argv[1:])
        bg(lambda: install_update(self.update_tag), done)

    def check_dependencies(self):
        missing = []
        if cups is None:
            missing.append("python-pycups")
        if Image is None:
            missing.append("python-pillow")
        if not shutil.which("scanimage"):
            missing.append("sane")
        if not os.path.exists("/usr/lib/sane/libsane-airscan.so.1") and not os.path.exists("/usr/lib64/sane/libsane-airscan.so.1"):
            missing.append("sane-airscan")
        if not shutil.which("ipp-usb"):
            missing.append("ipp-usb")
        if not missing:
            return
        bar = QtWidgets.QFrame()
        bar.setObjectName("group")
        lay = QtWidgets.QHBoxLayout(bar)
        lab = QtWidgets.QLabel("Es fehlen Pakete: " + ", ".join(missing))
        lay.addWidget(lab, 1)
        if shutil.which("pacman") and shutil.which("pkexec"):
            btn = QtWidgets.QPushButton("Jetzt installieren")
            btn.setObjectName("primary")
            btn.clicked.connect(lambda: self.install_packages(missing, bar))
            lay.addWidget(btn)
        self.banner.addWidget(bar)

    def install_packages(self, pkgs, bar):
        self.status.showMessage("Installiere " + " ".join(pkgs) + " …")

        def work():
            rc, out, err = run(["pkexec", "pacman", "-S", "--needed", "--noconfirm"] + pkgs, 900)
            if rc != 0:
                raise RuntimeError((err or out).strip().splitlines()[-1] if (err or out).strip() else "abgebrochen")

        def done(ok, res):
            if ok:
                QtWidgets.QMessageBox.information(self, APP_NAME, "Installiert. Bitte das Programm neu starten.")
                bar.hide()
            else:
                QtWidgets.QMessageBox.warning(self, APP_NAME, f"Installation fehlgeschlagen: {res}")
        bg(work, done)

    def refresh_caps(self):
        p = self.current
        if not p or p.caps is not None:
            return
        uri = hp_uri_for(p)
        if not uri:
            p.caps = {"fax": 0, "scan": 0}
            return

        def done(ok, res):
            p.caps = res if ok and res else {"fax": 0, "scan": 0}
            if p is self.current:
                self.update_actions()
                self.fill_features()
        bg(lambda: hplip("caps", uri), done)


    # ----- Drucken -----
    def build_print_tab(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        self.print_hint = QtWidgets.QLabel("Zum Drucken den Drucker erst einrichten (oben „Einrichten“).")
        self.print_hint.setStyleSheet("color: #d97706;")
        lay.addWidget(self.print_hint)
        self.print_files = QtWidgets.QListWidget()
        lay.addWidget(self.print_files, 1)
        row = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton(QtGui.QIcon.fromTheme("document-open"), "Dateien hinzufügen…")
        add.clicked.connect(self.add_print_files)
        rem = QtWidgets.QPushButton("Liste leeren")
        rem.clicked.connect(self.print_files.clear)
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch(1)
        lay.addLayout(row)
        form = QtWidgets.QFormLayout()
        self.copies = QtWidgets.QSpinBox()
        self.copies.setRange(1, 99)
        self.pages_edit = QtWidgets.QLineEdit()
        self.pages_edit.setPlaceholderText("alle, oder z. B. 1-3,5")
        self.sides = QtWidgets.QComboBox()
        self.color = QtWidgets.QComboBox()
        self.media = QtWidgets.QComboBox()
        self.quality = QtWidgets.QComboBox()
        self.fit = QtWidgets.QCheckBox("Auf Seite einpassen (Bilder)")
        self.fit.setChecked(True)
        form.addRow("Kopien", self.copies)
        form.addRow("Seiten", self.pages_edit)
        form.addRow("Beidseitig", self.sides)
        form.addRow("Farbe", self.color)
        form.addRow("Papier", self.media)
        form.addRow("Qualität", self.quality)
        form.addRow("", self.fit)
        lay.addLayout(form)
        self.apply_supported({})
        self.print_btn = QtWidgets.QPushButton(QtGui.QIcon.fromTheme("document-print"), "Drucken")
        self.print_btn.setMinimumHeight(36)
        self.print_btn.setObjectName("primary")
        self.print_btn.clicked.connect(self.do_print)
        lay.addWidget(self.print_btn)
        return w

    def apply_supported(self, sup):
        def fill(combo, items, current=None):
            keep = combo.currentData()
            combo.clear()
            for label, val in items:
                combo.addItem(label, val)
            i = combo.findData(keep if keep is not None else current)
            combo.setCurrentIndex(max(0, i))
        sides = sup.get("sides-supported") or ["one-sided", "two-sided-long-edge", "two-sided-short-edge"]
        fill(self.sides, [(lbl, v) for v, lbl in (("one-sided", "Nein"), ("two-sided-long-edge", "Ja, lange Kante"),
                                                    ("two-sided-short-edge", "Ja, kurze Kante")) if v in sides])
        modes = sup.get("print-color-mode-supported") or ["color", "monochrome"]
        fill(self.color, [(lbl, v) for v, lbl in (("color", "Farbe"), ("monochrome", "Schwarzweiß")) if v in modes])
        media = sup.get("media-supported") or ["iso_a4_210x297mm", "iso_a5_148x210mm", "na_letter_8.5x11in", "na_index-4x6_4x6in"]
        nice = {"iso_a4_210x297mm": "A4", "iso_a5_148x210mm": "A5", "iso_a6_105x148mm": "A6", "na_letter_8.5x11in": "Letter",
                "na_legal_8.5x14in": "Legal", "na_index-4x6_4x6in": "Foto 10×15", "na_5x7_5x7in": "Foto 13×18",
                "iso_dl_110x220mm": "Umschlag DL", "iso_c5_162x229mm": "Umschlag C5"}
        items = [(nice.get(m, m), m) for m in media if not m.startswith("custom_")]
        items.sort(key=lambda x: (x[1] != "iso_a4_210x297mm", x[0] not in nice.values(), x[0]))
        fill(self.media, items, "iso_a4_210x297mm")
        qual = [str(q) for q in (sup.get("print-quality-supported") or [3, 4, 5])]
        fill(self.quality, [(lbl, v) for v, lbl in (("4", "Normal"), ("3", "Entwurf"), ("5", "Hoch")) if v in qual], "4")

    def add_print_files(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Dateien drucken", os.path.expanduser("~"),
                                                          "Druckbar (*.pdf *.jpg *.jpeg *.png *.bmp *.tif *.tiff *.txt *.ps);;Alle Dateien (*)")
        for f in files:
            self.print_files.addItem(f)

    def do_print(self):
        p = self.current
        files = [self.print_files.item(i).text() for i in range(self.print_files.count())]
        if not p or not p.queue:
            return
        if not files:
            self.status.showMessage("Erst Dateien hinzufügen.")
            return
        opts = {"copies": str(self.copies.value())}
        for combo, key in ((self.sides, "sides"), (self.color, "print-color-mode"), (self.media, "media"),
                           (self.quality, "print-quality")):
            if combo.currentData():
                opts[key] = str(combo.currentData())
        if self.pages_edit.text().strip():
            opts["page-ranges"] = self.pages_edit.text().strip().replace(" ", "")
        if self.fit.isChecked():
            opts["fit-to-page"] = "true"

        def work():
            conn = cups.Connection()
            return conn.printFiles(p.queue, files, os.path.basename(files[0]), opts)

        def done(ok, res):
            self.status.showMessage(f"Druckauftrag {res} gesendet." if ok else f"Drucken fehlgeschlagen: {res}")
            if ok:
                QtCore.QTimer.singleShot(3000, self.refresh_status)
        bg(work, done)

    def test_page(self):
        p = self.current
        test = next((f for f in ("/usr/share/cups/data/testprint", "/usr/share/cups/data/default-testpage.pdf")
                     if os.path.exists(f)), None)
        if not p or not p.queue or not test:
            return
        bg(lambda: cups.Connection().printFile(p.queue, test, "Testseite", {}),
           lambda ok, res: self.status.showMessage("Testseite gesendet." if ok else f"Testseite fehlgeschlagen: {res}"))

    def setup_current(self):
        p = self.current
        if not p:
            return
        self.status.showMessage("Richte Drucker ein…")

        def done(ok, res):
            self.status.showMessage(res if ok else f"Einrichten fehlgeschlagen: {res}")
            if ok:
                QtCore.QTimer.singleShot(1500, self.search)
        bg(lambda: setup_printer(p), done)

    def remove_current(self):
        p = self.current
        if not p or not p.queue:
            return
        if QtWidgets.QMessageBox.question(self, APP_NAME, f"Warteschlange „{p.queue}“ entfernen?\nDer Drucker selbst bleibt unverändert.") \
                != QtWidgets.QMessageBox.Yes:
            return
        bg(lambda: remove_queue(p.queue),
           lambda ok, res: (self.status.showMessage("Entfernt." if ok else f"Entfernen fehlgeschlagen: {res}"), ok and self.search()))

    def open_web(self):
        if self.current and self.current.host:
            webbrowser.open(f"http://{self.current.host}")

    # ----- Scannen -----
    def field(self, label, widget):
        """Feld mit kleiner Beschriftung ueber dem Wert."""
        f = QtWidgets.QFrame()
        lay = QtWidgets.QVBoxLayout(f)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lab = QtWidgets.QLabel(label)
        lab.setObjectName("dim")
        lay.addWidget(lab)
        lay.addWidget(widget)
        return f

    def build_scan_tab(self):
        page = QtWidgets.QWidget()
        page.setObjectName("page")
        outer = QtWidgets.QHBoxLayout(page)
        outer.setContentsMargins(0, 10, 0, 16)
        outer.setSpacing(18)

        # links: Hinweis, Vorschau oder gescannte Seiten
        left = QtWidgets.QVBoxLayout()
        self.scan_area_stack = QtWidgets.QStackedWidget()
        hint = QtWidgets.QLabel('Lege dein Dokument in den Scanner und wähle <b>Scannen</b> oder '
                                '<a href="import">importiere</a> eine Datei.')
        hint.setAlignment(QtCore.Qt.AlignCenter)
        hint.setWordWrap(True)
        hint.setStyleSheet("font-size: 17px;")
        hint.linkActivated.connect(lambda _: self.import_pages())
        hint_box = QtWidgets.QWidget()
        hl = QtWidgets.QVBoxLayout(hint_box)
        hl.addStretch(1)
        hl.addWidget(hint)
        hl.addStretch(1)
        self.scan_area_stack.addWidget(hint_box)
        self.preview_label = QtWidgets.QLabel()
        self.preview_label.setAlignment(QtCore.Qt.AlignCenter)
        self.scan_area_stack.addWidget(self.preview_label)
        pages_box = QtWidgets.QWidget()
        pl = QtWidgets.QVBoxLayout(pages_box)
        pl.setContentsMargins(0, 0, 0, 0)
        self.page_view = QtWidgets.QListWidget()
        self.page_view.setViewMode(QtWidgets.QListView.IconMode)
        self.page_view.setIconSize(QtCore.QSize(220, 300))
        self.page_view.setResizeMode(QtWidgets.QListView.Adjust)
        self.page_view.setMovement(QtWidgets.QListView.Snap)
        self.page_view.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.page_view.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        pl.addWidget(self.page_view, 1)
        row = QtWidgets.QHBoxLayout()
        more = QtWidgets.QPushButton("+ Seite importieren")
        more.clicked.connect(self.import_pages)
        delete = QtWidgets.QPushButton("Ausgewählte löschen")
        delete.clicked.connect(self.delete_pages)
        clear = QtWidgets.QPushButton("Alle löschen")
        clear.clicked.connect(lambda: self.clear_pages())
        self.save_fmt = QtWidgets.QComboBox()
        for lbl, ext in SAVE_FORMATS:
            self.save_fmt.addItem(lbl, ext)
        save = QtWidgets.QPushButton("Speichern…")
        save.setObjectName("primary")
        save.clicked.connect(self.save_scan)
        for w in (more, delete, clear):
            row.addWidget(w)
        row.addStretch(1)
        row.addWidget(self.save_fmt)
        row.addWidget(save)
        pl.addLayout(row)
        self.scan_area_stack.addWidget(pages_box)
        left.addWidget(self.scan_area_stack, 1)
        note = QtWidgets.QLabel("Nimm das Original nach dem Scannen aus dem Gerät – liegt es im Scanner, "
                                "kann jeder im Netzwerk es scannen.")
        note.setObjectName("dim")
        note.setAlignment(QtCore.Qt.AlignCenter)
        note.setWordWrap(True)
        note.setStyleSheet("font-size: 12px;")
        left.addWidget(note)
        outer.addLayout(left, 1)

        # rechts: Einstellungen
        panel = QtWidgets.QFrame()
        panel.setObjectName("group")
        panel.setFixedWidth(280)
        rl = QtWidgets.QVBoxLayout(panel)
        rl.setContentsMargins(18, 20, 18, 18)
        rl.setSpacing(12)
        self.scanner_combo = QtWidgets.QComboBox()
        self.scanner_combo.currentIndexChanged.connect(self.load_scanner_options)
        self.scanner_field = self.field("Scanner", self.scanner_combo)
        rl.addWidget(self.scanner_field)
        self.scan_source = QtWidgets.QComboBox()
        rl.addWidget(self.field("Quelle", self.scan_source))
        self.scan_duplex = QtWidgets.QCheckBox("Beidseitig")
        self.scan_duplex.hide()
        rl.addWidget(self.scan_duplex)
        self.scan_source.currentIndexChanged.connect(self.source_changed)
        self.scan_crop = QtWidgets.QCheckBox("Kanten erkennen")
        rl.addWidget(self.scan_crop)
        self.scan_preset = QtWidgets.QComboBox()
        for lbl in ("Dokument", "Foto"):
            self.scan_preset.addItem(lbl, lbl)
        self.scan_preset.currentIndexChanged.connect(self.apply_preset)
        rl.addWidget(self.field("Voreinstellungen", self.scan_preset))
        self.scan_area = QtWidgets.QComboBox()
        for lbl, area in SCAN_AREAS:
            self.scan_area.addItem(lbl, area)
        rl.addWidget(self.field("Scanbereich", self.scan_area))
        self.scan_mode = QtWidgets.QComboBox()
        rl.addWidget(self.field("Ausgabe", self.scan_mode))
        self.scan_res = QtWidgets.QComboBox()
        rl.addWidget(self.field("Auflösung", self.scan_res))
        reset = QtWidgets.QPushButton("Einstellungen zurücksetzen")
        reset.setFlat(True)
        reset.clicked.connect(self.load_scanner_options)
        rl.addWidget(reset)
        rl.addStretch(1)
        self.preview_btn = QtWidgets.QPushButton("Vorschau")
        self.preview_btn.setMinimumHeight(34)
        self.preview_btn.clicked.connect(self.do_preview)
        rl.addWidget(self.preview_btn)
        self.scan_btn = QtWidgets.QPushButton("Scannen")
        self.scan_btn.setObjectName("primary")
        self.scan_btn.setMinimumHeight(34)
        self.scan_btn.clicked.connect(self.do_scan)
        rl.addWidget(self.scan_btn)
        outer.insertWidget(0, panel)   # Einstellungen links, Vorschau rechts
        return page

    def apply_preset(self):
        mode = "Gray" if self.scan_preset.currentData() == "Dokument" else "Color"
        for combo, val in ((self.scan_mode, mode), (self.scan_res, "300")):
            i = combo.findData(val)
            if i >= 0:
                combo.setCurrentIndex(i)

    def is_feeder(self, src):
        return bool(src) and bool(re.search(r"adf|feeder", src, re.I))

    def source_changed(self):
        self.scan_duplex.setVisible(self.is_feeder(self.scan_source.currentData())
                                    and bool(getattr(self, "duplex_value", None)))

    def poll_adf(self):
        """Papier im Einzug -> Vorlageneinzug waehlen, Einzug leer -> Scannerglas (nur bei Aenderung)."""
        if self.stack.currentIndex() != self.SCAN or not self.scan_btn.isEnabled():
            return
        i = self.scanner_combo.currentIndex()
        dev = self.scanner_combo.currentData()
        ip = scanner_ip(dev, self.scanners[i][1] if 0 <= i < len(self.scanners) else "") or \
            (self.current.host if self.current else None)
        if not dev or not ip:
            return

        def done(ok, loaded):
            if not ok or loaded is None or loaded == getattr(self, "adf_state", None):
                return
            self.adf_state = loaded
            want = next((self.scan_source.itemData(j) for j in range(self.scan_source.count())
                         if self.is_feeder(self.scan_source.itemData(j)) == loaded), None)
            if want is not None:
                self.scan_source.setCurrentIndex(self.scan_source.findData(want))
                self.status.showMessage("Papier im Vorlageneinzug erkannt." if loaded else "Vorlageneinzug leer – Scannerglas.")
        bg(lambda: adf_loaded(ip), done)

    def scan_args(self):
        src = self.scan_source.currentData()
        if self.is_feeder(src) and self.scan_duplex.isChecked() and getattr(self, "duplex_value", None):
            src = self.duplex_value
        return (self.scanner_combo.currentData(), self.scan_mode.currentData(), self.scan_res.currentData(),
                src, self.scan_area.currentData(), self.scan_crop.isChecked())

    def do_preview(self):
        dev, mode, res, src, area, crop = self.scan_args()
        if not dev:
            return
        self.preview_btn.setEnabled(False)
        self.preview_btn.setText("Vorschau läuft…")

        def done(ok, files):
            self.preview_btn.setEnabled(True)
            self.preview_btn.setText("Vorschau")
            if not ok:
                self.status.showMessage(f"Vorschau fehlgeschlagen: {files}")
                return
            pix = QtGui.QPixmap(files[0])
            self.preview_label.setPixmap(pix.scaled(self.preview_label.size() * 0.95, QtCore.Qt.KeepAspectRatio,
                                                    QtCore.Qt.SmoothTransformation))
            self.scan_area_stack.setCurrentIndex(1)
        # Vorschau immer von der Glasscheibe, niedrige Aufloesung, nicht in die Seiten
        flat = src if not (src and re.search(r"adf|feeder|duplex", src, re.I)) else None
        bg(lambda: scan(dev, mode, "75" if "75" in [self.scan_res.itemData(i) for i in range(self.scan_res.count())]
                        else res, flat, area, crop), done)

    def add_page_files(self, files):
        for f in files:
            self.pages.append(f)
            item = QtWidgets.QListWidgetItem(QtGui.QIcon(QtGui.QPixmap(f).scaled(440, 600, QtCore.Qt.KeepAspectRatio,
                                                                                  QtCore.Qt.SmoothTransformation)),
                                             f"Seite {self.page_view.count() + 1}")
            item.setData(QtCore.Qt.UserRole, f)
            self.page_view.addItem(item)
        if files:
            self.scan_saved = False
        if self.page_view.count():
            self.scan_area_stack.setCurrentIndex(2)

    def import_pages(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Bilder importieren", os.path.expanduser("~"),
                                                          "Bilder (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp)")
        self.add_page_files(files)

    def clear_pages(self, ask=True):
        if ask and self.page_view.count() and not self.scan_saved and self.stack.currentIndex() == self.SCAN:
            self.leave_scan_ok(discard=True)
            return
        self.scan_saved = True
        self.page_view.clear()
        self.pages.clear()
        self.scan_area_stack.setCurrentIndex(0)

    def refresh_scanners(self):
        self.scanner_combo.clear()
        self.scanner_combo.addItem("Suche Scanner…", None)
        self.scanner_combo.setEnabled(False)

        def done(ok, res):
            self.scanner_combo.blockSignals(True)
            self.scanner_combo.clear()
            self.scanners = res if ok else []
            if not ok:
                self.scanner_combo.addItem(f"Scannen nicht möglich: {res}", None)
            elif not res:
                self.scanner_combo.addItem("Kein Scanner gefunden", None)
            for dev, desc in self.scanners:
                kind = "Netzwerk/IPP" if dev.startswith("airscan") else "HPLIP"
                short = re.sub(r"^eSCL\s+|\s+ip=.*$", "", desc)   # airscan haengt Protokoll und IPs an
                self.scanner_combo.addItem(f"{short}  ({kind})", dev)
            self.scanner_combo.setEnabled(bool(self.scanners))
            self.scanner_field.setVisible(len(self.scanners) != 1)
            self.scanner_combo.blockSignals(False)
            self.match_scanner()
            self.load_scanner_options()
            self.update_actions()
            self.fill_features()
        bg(list_scanners, done)

    def match_scanner(self):
        p = self.current
        if not p or not self.scanners:
            return
        words = [w for w in re.findall(r"[a-z0-9]+", (p.model or p.title).lower()) if w not in ("hp", "series")]
        best = max(range(len(self.scanners)), key=lambda i: sum(w in self.scanners[i][1].lower() or w in self.scanners[i][0].lower()
                                                                 for w in words), default=None)
        if best is not None:
            self.scanner_combo.setCurrentIndex(best)

    def load_scanner_options(self):
        dev = self.scanner_combo.currentData()
        for c in (self.scan_mode, self.scan_res, self.scan_source):
            c.clear()
        self.scan_btn.setEnabled(bool(dev))
        if not dev:
            return

        def done(ok, opts):
            if dev != self.scanner_combo.currentData():
                return
            opts = opts if ok else {}
            names = {"Color": "Farbe", "Gray": "Graustufen", "Lineart": "Schwarzweiß", "Flatbed": "Scannerglas",
                     "ADF": "Vorlageneinzug"}
            # wie HP Smart: nur Vorlageneinzug und Scannerglas; beidseitig ist ein Haken beim Einzug
            if "source" in opts:
                srcs, cur = opts["source"]
                self.duplex_value = next((x for x in srcs if re.search(r"duplex", x, re.I)), None)
                srcs = [x for x in srcs if not re.search(r"duplex", x, re.I)]
                srcs.sort(key=lambda x: not re.search(r"adf|feeder", x, re.I))
                opts["source"] = (srcs, cur if cur in srcs else (srcs[0] if srcs else ""))
            for key, combo, fallback in (("mode", self.scan_mode, ["Color", "Gray"]),
                                         ("resolution", self.scan_res, ["150", "300", "600"]),
                                         ("source", self.scan_source, [])):
                choices, cur = opts.get(key, (fallback, fallback[1] if len(fallback) > 1 else ""))
                for c in choices:
                    combo.addItem(f"{c} dpi" if key == "resolution" else names.get(c, c), c)
                i = combo.findData("300" if key == "resolution" and "300" in choices else cur)
                combo.setCurrentIndex(max(0, i))
            self.scan_source.setEnabled(self.scan_source.count() > 1)
            self.apply_preset()
            self.adf_state = None
            self.source_changed()
        bg(lambda: scanner_options(dev), done)

    def do_scan(self):
        dev, mode, res, src, area, crop = self.scan_args()
        if not dev:
            return
        self.scan_btn.setEnabled(False)
        self.scan_btn.setText("Scanne…")

        def done(ok, files):
            self.scan_btn.setEnabled(True)
            self.scan_btn.setText("Scannen")
            if not ok:
                self.status.showMessage(f"Scan fehlgeschlagen: {files}")
                return
            self.scan_dpi = res or "300"
            self.add_page_files(files)
            self.status.showMessage(f"{len(files)} Seite(n) gescannt.")
        bg(lambda: scan(dev, mode, res, src, area, crop), done)

    def delete_pages(self):
        for item in self.page_view.selectedItems():
            self.page_view.takeItem(self.page_view.row(item))
        if not self.page_view.count():
            self.scan_area_stack.setCurrentIndex(0)

    def save_scan(self):
        files = [self.page_view.item(i).data(QtCore.Qt.UserRole) for i in range(self.page_view.count())]
        if not files:
            self.status.showMessage("Noch nichts gescannt.")
            return
        if Image is None:
            self.status.showMessage("python-pillow fehlt.")
            return
        ext = self.save_fmt.currentData()
        start = os.path.join(QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.DocumentsLocation)
                             or os.path.expanduser("~"), f"Scan.{ext}")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Scan speichern", start, f"{ext.upper()} (*.{ext})")
        if not path:
            return
        if not path.lower().endswith("." + ext):
            path += "." + ext
        def done(ok, res):
            self.status.showMessage(("Gespeichert: " + ", ".join(res)) if ok else f"Speichern fehlgeschlagen: {res}")
            if ok:
                self.scan_saved = True
        bg(lambda: save_pages(files, path, ext, self.scan_dpi), done)

    # ----- Fax -----
    def build_fax_tab(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        self.fax_hint = QtWidgets.QLabel()
        self.fax_hint.setWordWrap(True)
        lay.addWidget(self.fax_hint)
        self.fax_setup_btn = QtWidgets.QPushButton("Fax einrichten")
        self.fax_setup_btn.setObjectName("primary")
        self.fax_setup_btn.clicked.connect(self.setup_fax)
        lay.addWidget(self.fax_setup_btn)
        form = QtWidgets.QFormLayout()
        self.fax_number = QtWidgets.QLineEdit()
        self.fax_number.setPlaceholderText("Faxnummer, mehrere mit Komma")
        form.addRow("An", self.fax_number)
        lay.addLayout(form)
        self.fax_files = QtWidgets.QListWidget()
        lay.addWidget(self.fax_files, 1)
        row = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton(QtGui.QIcon.fromTheme("document-open"), "Dokumente hinzufügen…")
        add.clicked.connect(self.add_fax_files)
        scan_btn = QtWidgets.QPushButton("Gescannte Seiten übernehmen")
        scan_btn.clicked.connect(self.fax_from_scan)
        row.addWidget(add)
        row.addWidget(scan_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.fax_send = QtWidgets.QPushButton("Fax senden")
        self.fax_send.setMinimumHeight(36)
        self.fax_send.setObjectName("primary")
        self.fax_send.clicked.connect(self.send_fax)
        lay.addWidget(self.fax_send)
        self.fax_log = QtWidgets.QPlainTextEdit()
        self.fax_log.setReadOnly(True)
        self.fax_log.setMaximumHeight(120)
        lay.addWidget(self.fax_log)
        return w

    def update_fax_state(self):
        p = self.current
        has_queue = bool(p and p.fax_queue)
        self.fax_hint.setText(f"Fax über die Warteschlange „{p.fax_queue}“." if has_queue else
                              "Dieser Drucker kann faxen. Dafür wird einmalig eine eigene Fax-Warteschlange eingerichtet "
                              "(fragt nach deinem Passwort). Am Drucker muss eine Telefonleitung angeschlossen sein.")
        self.fax_setup_btn.setVisible(not has_queue)
        self.fax_send.setEnabled(has_queue)

    def setup_fax(self):
        p = self.current
        uri = hp_uri_for(p) if p else ""
        if not uri:
            return
        self.fax_setup_btn.setEnabled(False)
        self.status.showMessage("Richte Fax ein … gleich fragt ein Fenster nach deinem Passwort.")

        def done(ok, res):
            self.fax_setup_btn.setEnabled(True)
            if ok:
                p.fax_queue = res
                self.update_fax_state()
                self.status.showMessage(f"Fax eingerichtet („{res}“).")
            else:
                self.status.showMessage(f"Fax einrichten fehlgeschlagen: {res}")
        bg(lambda: setup_fax_queue(p), done)

    def add_fax_files(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Dokumente faxen", os.path.expanduser("~"),
                                                          "Dokumente (*.pdf *.jpg *.jpeg *.png *.tif *.tiff *.txt *.ps);;Alle Dateien (*)")
        for f in files:
            self.fax_files.addItem(f)

    def fax_from_scan(self):
        files = [self.page_view.item(i).data(QtCore.Qt.UserRole) for i in range(self.page_view.count())]
        if not files or Image is None:
            self.status.showMessage("Keine gescannten Seiten.")
            return
        path = os.path.join(tempfile.mkdtemp(prefix="hp-fax-"), "Scan.pdf")
        save_pages(files, path, "pdf", self.scan_dpi)
        self.fax_files.addItem(path)

    def send_fax(self):
        p = self.current
        number = re.sub(r"[^0-9+,*#]", "", self.fax_number.text())
        files = [self.fax_files.item(i).text() for i in range(self.fax_files.count())]
        if not p or not p.fax_queue or not number or not files:
            self.status.showMessage("Nummer und mindestens ein Dokument angeben.")
            return
        self.fax_send.setEnabled(False)
        self.fax_log.setPlainText(f"Sende an {number} …")

        def done(ok, res):
            self.fax_send.setEnabled(True)
            rc, out, err = res if ok else (-1, "", str(res))
            self.fax_log.setPlainText((out + err).strip()[-3000:] or ("Gesendet." if rc == 0 else "Fehlgeschlagen."))
            self.status.showMessage("Fax gesendet." if rc == 0 else "Fax fehlgeschlagen – Details unten.")
        bg(lambda: run(["hp-sendfax", "-n", f"--fax={p.fax_queue}", "-f", number] + files, 1800), done)


def rename_menu_entry():
    """Aeltere Installationen hiessen „HP Druckzentrale“: Menueeintrag einmal umbenennen."""
    path = os.path.expanduser("~/.local/share/applications/hp-druckzentrale.desktop")
    try:
        text = open(path).read()
        if "Name=HP Druckzentrale" in text:
            open(path, "w").write(text.replace("Name=HP Druckzentrale", "Name=Druckzentrale")
                                  .replace("für HP-Drucker", "für Drucker"))
    except OSError:
        pass


def main():
    global _bridge
    rename_menu_entry()
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("hp-druckzentrale")
    _bridge = Bridge()
    _bridge.call.connect(lambda fn: fn(), QtCore.Qt.QueuedConnection)
    win = MainWindow()
    win.show()
    if os.environ.get("HPDZ_SELFTEST"):
        if os.environ.get("HPDZ_PAGE"):
            QtCore.QTimer.singleShot(int(os.environ.get("HPDZ_SELFTEST_MS", "3000")) - 500,
                                     lambda: win.go(int(os.environ["HPDZ_PAGE"])))
        QtCore.QTimer.singleShot(int(os.environ.get("HPDZ_SELFTEST_MS", "3000")),
                                 lambda: (win.grab().save(os.environ["HPDZ_SELFTEST"]), app.quit()))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
