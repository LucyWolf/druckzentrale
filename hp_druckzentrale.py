#!/usr/bin/env python3
# HP Druckzentrale – Drucken, Scannen, Tintenstand und Fax fuer HP-Drucker unter Linux.
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

APP_NAME = "HP Druckzentrale"
APP_VERSION = "1.0.4"
UPDATE_REPO = "LucyWolf/hp-druckzentrale"
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
    out = {"fax": int(m.get("fax-type", 0) or 0), "scan": int(m.get("scan-type", 0) or 0)}
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
    """Alle HP-Drucker: eingerichtete Warteschlangen und von CUPS gefundene Geraete (USB, Netzwerk)."""
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
        if is_hp(uri) or is_hp(model) or is_hp(q.get("printer-info", "")):
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
        if is_hp(uri) or is_hp(model) or is_hp(d.get("device-info", "")):
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
                if not hp_uri_for(p) and shutil.which("hp-makeuri"):
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
    if uri.startswith("hp:") and not any(u.startswith(("ipp", "dnssd")) for u in p.uris):
        # Aeltere HP-Geraete: HPLIPs eigener Assistent waehlt Treiber (und bei Bedarf Fax) richtig
        subprocess.Popen(["hp-setup", uri], start_new_session=True)
        return "HPLIP-Einrichtung gestartet – bitte dort fertigstellen und danach „Drucker suchen“ drücken."
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", p.model or p.info or "HP_Drucker").strip("_")[:60] or "HP_Drucker"
    cmd = ["pkexec", "lpadmin", "-p", name, "-E", "-v", uri, "-m", "everywhere"]
    if uri.startswith("usb://"):
        # rohes USB ohne IPP: passenden HPLIP-Treiber suchen
        rc, out, _ = run(["lpinfo", "--make-and-model", p.model, "-m"], 30)
        drv = next((l.split()[0] for l in out.splitlines() if "hpcups" in l or "hplip" in l.lower()), None)
        if not drv:
            raise RuntimeError("Kein Treiber gefunden. Für USB ohne IPP bitte „ipp-usb“ installieren oder HPLIP nutzen.")
        cmd[-1] = drv
    rc, out, err = run(cmd, 120)
    if rc != 0:
        raise RuntimeError((err or out or "lpadmin fehlgeschlagen").strip())
    return f"Eingerichtet als „{name}“."


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
        if is_hp(dev) or is_hp(desc):
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
        t = QtWidgets.QLabel("Willkommen bei der HP Druckzentrale")
        t.setStyleSheet("font-size: 20px; font-weight: bold;")
        l1.addWidget(t)
        txt = QtWidgets.QLabel(
            "Zuerst richten wir deinen HP-Drucker ein.\n\n"
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
        self.search_label = QtWidgets.QLabel("Suche HP-Drucker über USB und im Netzwerk …")
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
        self.search_label.setText("Suche HP-Drucker über USB und im Netzwerk … (bis zu 15 Sekunden)")

        def done(ok, res):
            self.busy.hide()
            self.found = res if ok else []
            if not ok:
                self.search_label.setText(f"Suche fehlgeschlagen: {res}")
                return
            if not res:
                self.search_label.setText(
                    "Kein HP-Drucker gefunden.\n\nIst er eingeschaltet? Bei USB: Kabel ab- und wieder anstecken. "
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


# ---------- Optik (angelehnt an HP Smart: schwarz, dunkle Karten, blau-violetter Akzent) ----------
BG = "#000000"
CARD = "#26262F"
CARD_H = "#31313C"
ACC = "#5B6CF0"
ACC_H = "#6E7BFF"
ACC_T = "#7C88FF"
DIM = "#A6A6B3"
FIELD = "#1B1B22"
LINE = "#3A3A46"

QSS = f"""
QMainWindow, QWidget#page, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {BG}; }}
QWidget {{ color: #FFFFFF; font-size: 14px; }}
QLabel {{ background: transparent; }}
QLabel#dim {{ color: {DIM}; }}
QLabel#accent {{ color: {ACC_T}; }}
QFrame#card, QFrame#tile {{ background: {CARD}; border-radius: 18px; }}
QFrame#tile:hover, QFrame#printercard:hover {{ background: {CARD_H}; }}
QFrame#printercard {{ background: {CARD}; border-radius: 22px; }}
QFrame#row:hover {{ background: {CARD_H}; border-radius: 10px; }}
QFrame#sep {{ background: {LINE}; max-height: 1px; min-height: 1px; }}
QFrame#chip {{ background: #1E2050; border: 1.5px solid {ACC}; border-radius: 18px; }}
QFrame#pill {{ background: {CARD}; border-radius: 24px; }}
QPushButton {{ background: {CARD}; color: #FFFFFF; border: none; border-radius: 10px; padding: 8px 16px; }}
QPushButton:hover {{ background: {CARD_H}; }}
QPushButton:disabled {{ color: #666672; }}
QFrame#card QPushButton {{ background: #3A3A48; }}
QFrame#card QPushButton:hover {{ background: #464656; }}
QPushButton#primary, QFrame#card QPushButton#primary {{ background: {ACC}; border-radius: 18px; padding: 8px 20px; font-weight: bold; }}
QPushButton#primary:hover, QFrame#card QPushButton#primary:hover {{ background: {ACC_H}; }}
QPushButton#round {{ border-radius: 20px; min-width: 40px; max-width: 40px; min-height: 40px; max-height: 40px;
                     padding: 0; font-size: 18px; }}
QPushButton#roundacc {{ background: {ACC}; border-radius: 20px; min-width: 40px; max-width: 40px; min-height: 40px;
                        max-height: 40px; padding: 0; }}
QComboBox, QLineEdit, QSpinBox, QListWidget, QPlainTextEdit {{ background: {FIELD}; color: #FFFFFF;
    border: 1px solid {LINE}; border-radius: 8px; padding: 6px; }}
QComboBox QAbstractItemView {{ background: {CARD}; color: #FFFFFF; selection-background-color: {ACC}; }}
QListWidget::item:selected {{ background: {ACC}; }}
QCheckBox {{ color: #FFFFFF; }}
QProgressBar {{ background: {FIELD}; border: none; border-radius: 4px; height: 8px; }}
QProgressBar::chunk {{ background: {ACC}; border-radius: 4px; }}
QStatusBar {{ background: {BG}; color: {DIM}; }}
QMenu {{ background: {CARD}; color: #FFFFFF; border: 1px solid {LINE}; }}
QMenu::item:selected {{ background: {ACC}; }}
QScrollBar:vertical {{ background: {BG}; width: 10px; }}
QScrollBar::handle:vertical {{ background: {LINE}; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QToolBar {{ background: {BG}; border: none; }}
"""

INK_ORDER = ["M", "C", "Y", "K"]


def ink_letter(m):
    n = (m.get("name") or "").lower()
    for keys, letter in ((("magenta",), "M"), (("cyan",), "C"), (("yellow", "gelb"), "Y"),
                         (("black", "schwarz"), "K"), (("tri", "color", "farb"), "CMY")):
        if any(k in n for k in keys):
            return letter
    return (ink_name(m.get("name"))[:2] or "?").upper()


class InkTubes(QtWidgets.QWidget):
    """Senkrechte Tintenroehrchen wie in HP Smart (M C Y K), darunter Buchstabe und Farbpunkt."""

    def __init__(self, markers, big=True):
        super().__init__()
        self.markers = sorted(markers, key=lambda m: INK_ORDER.index(ink_letter(m)) if ink_letter(m) in INK_ORDER else 9)
        self.tw, self.th = (24, 80) if big else (18, 58)
        self.setFixedSize(max(1, len(self.markers)) * (self.tw + 8) + 4, self.th + 34)
        self.setToolTip("\n".join(f"{ink_name(m['name'])}: " + (f"{m['level']} %" if m["level"] >= 0 else "unbekannt")
                                  for m in self.markers))

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        for i, m in enumerate(self.markers):
            x = 2 + i * (self.tw + 8)
            tube = QtCore.QRectF(x, 2, self.tw, self.th)
            path = QtGui.QPainterPath()
            path.addRoundedRect(tube, self.tw / 2, self.tw / 2)
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(QtGui.QColor("#E9E9F0"))
            p.drawPath(path)
            level = m["level"]
            frac = level / 100 if level >= 0 else (0.5 if level == -3 else 0)
            if frac > 0:
                p.save()
                p.setClipPath(path)
                fill = QtCore.QRectF(x, 2 + self.th * (1 - frac), self.tw, self.th * frac)
                cols = marker_color(m)
                if len(cols) == 1:
                    p.setBrush(QtGui.QColor(cols[0]))
                else:
                    g = QtGui.QLinearGradient(fill.topLeft(), fill.topRight())
                    for j, c in enumerate(cols):
                        g.setColorAt(j / max(1, len(cols) - 1), QtGui.QColor(c))
                    p.setBrush(g)
                p.drawRect(fill)
                p.restore()
            dot = QtGui.QColor(marker_color(m)[0])
            p.setBrush(dot)
            p.drawEllipse(QtCore.QPointF(x + self.tw / 2, self.th + 9), 2.5, 2.5)
            p.setPen(QtGui.QColor("#FFFFFF"))
            f = p.font()
            f.setBold(True)
            f.setPointSize(9)
            p.setFont(f)
            p.drawText(QtCore.QRectF(x - 6, self.th + 13, self.tw + 12, 18), QtCore.Qt.AlignCenter, ink_letter(m))


def chip(text):
    f = QtWidgets.QFrame()
    f.setObjectName("chip")
    lay = QtWidgets.QHBoxLayout(f)
    lay.setContentsMargins(12, 6, 16, 6)
    icon = QtWidgets.QLabel("i")
    icon.setAlignment(QtCore.Qt.AlignCenter)
    icon.setFixedSize(20, 20)
    icon.setStyleSheet(f"background: {ACC}; border-radius: 10px; font-weight: bold; font-size: 12px;")
    lab = QtWidgets.QLabel(text)
    lab.setWordWrap(True)
    lab.setStyleSheet("font-size: 13px;")
    lay.addWidget(icon)
    lay.addWidget(lab, 1)
    f.setMaximumWidth(300)
    return f


class Clickable(QtWidgets.QFrame):
    clicked = QtCore.Signal()

    def mouseReleaseEvent(self, e):
        if e.button() == QtCore.Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()


def tile(title, desc, icon_name, cb):
    t = Clickable()
    t.setObjectName("tile")
    t.setCursor(QtCore.Qt.PointingHandCursor)
    t.setMinimumHeight(130)
    lay = QtWidgets.QHBoxLayout(t)
    lay.setContentsMargins(28, 18, 22, 18)
    col = QtWidgets.QVBoxLayout()
    h = QtWidgets.QLabel(title)
    h.setStyleSheet("font-size: 21px; font-weight: bold;")
    d = QtWidgets.QLabel(desc)
    d.setWordWrap(True)
    d.setStyleSheet("color: #D6D6DE; font-size: 13px;")
    col.addStretch(1)
    col.addWidget(h)
    col.addWidget(d)
    col.addStretch(1)
    lay.addLayout(col, 1)
    ic = QtWidgets.QLabel()
    ic.setPixmap(QtGui.QIcon.fromTheme(icon_name).pixmap(54, 54))
    lay.addWidget(ic)
    t.clicked.connect(cb)
    return t


def section(title, rows):
    """Karte mit Ueberschrift und Zeilen (Symbol, Text, rechter Text, Aktion) wie in HP Smart."""
    card = QtWidgets.QFrame()
    card.setObjectName("card")
    lay = QtWidgets.QVBoxLayout(card)
    lay.setContentsMargins(0, 18, 0, 10)
    lay.setSpacing(0)
    h = QtWidgets.QLabel(title)
    h.setStyleSheet("font-size: 24px; font-weight: bold; padding: 0 30px 14px 30px;")
    lay.addWidget(h)
    labels = {}
    for icon_name, text, right, cb in rows:
        sep = QtWidgets.QFrame()
        sep.setObjectName("sep")
        lay.addWidget(sep)
        r = Clickable()
        r.setObjectName("row")
        r.setCursor(QtCore.Qt.PointingHandCursor)
        rl = QtWidgets.QHBoxLayout(r)
        rl.setContentsMargins(30, 16, 30, 16)
        ic = QtWidgets.QLabel()
        ic.setPixmap(QtGui.QIcon.fromTheme(icon_name).pixmap(20, 20))
        rl.addWidget(ic)
        rl.addSpacing(10)
        rl.addWidget(QtWidgets.QLabel(text), 1)
        rlab = QtWidgets.QLabel(right or "→")
        rlab.setObjectName("accent" if right else "")
        rlab.setStyleSheet("font-size: 18px;" if not right else "")
        rl.addWidget(rlab)
        labels[text] = rlab
        r.clicked.connect(cb)
        lay.addWidget(r)
    card.labels = labels
    return card


def printer_key(p):
    for u in p.uris:
        m = re.search(r"uuid=([^&]+)", u)
        if m:
            return m.group(1)
    return p.serial or p.host or p.uri


CACHE_DIR = os.path.expanduser("~/.cache/hp-druckzentrale")


def printer_image(p):
    """Bild des Druckers von seiner eigenen Weboberflaeche (HP-Geraete liefern dort ein Foto)."""
    key = re.sub(r"[^A-Za-z0-9_-]", "_", printer_key(p))[:80]
    path = os.path.join(CACHE_DIR, f"{key}.png")
    if os.path.exists(path):
        return path
    if not re.fullmatch(r"[\d.]+", p.host or ""):
        return None
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # Drucker haben selbst ausgestellte Zertifikate
    for url in (f"https://{p.host}/webApps/images/printer.png", f"https://{p.host}/images/printer.png",
                f"http://{p.host}/images/printer.png"):
        try:
            with urllib.request.urlopen(url, timeout=6, context=ctx if url.startswith("https") else None) as r:
                data = r.read()
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                os.makedirs(CACHE_DIR, exist_ok=True)
                with open(path, "wb") as f:
                    f.write(data)
                return path
        except Exception:
            continue
    return None


def page_wrap(title, inner):
    """Unterseite: Ueberschrift und Inhalt in einer Karte, mittig mit begrenzter Breite."""
    page = QtWidgets.QWidget()
    page.setObjectName("page")
    outer = QtWidgets.QHBoxLayout(page)
    outer.addStretch(1)
    col = QtWidgets.QVBoxLayout()
    h = QtWidgets.QLabel(title)
    h.setStyleSheet("font-size: 34px; font-weight: bold; padding: 6px 0 12px 0;")
    col.addWidget(h)
    card = QtWidgets.QFrame()
    card.setObjectName("card")
    cl = QtWidgets.QVBoxLayout(card)
    cl.setContentsMargins(24, 20, 24, 20)
    cl.addWidget(inner)
    col.addWidget(card, 1)
    w = QtWidgets.QWidget()
    w.setLayout(col)
    w.setMaximumWidth(900)
    w.setMinimumWidth(640)
    outer.addWidget(w, 6)
    outer.addStretch(1)
    return page


class MainWindow(QtWidgets.QMainWindow):
    HOME, PRINTERS, PRINT, SCAN, FAX = range(5)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 860)
        self.setStyleSheet(QSS)
        self.printers = []
        self.current = None
        self.scanners = []
        self.pages = []          # PNG-Dateien der aktuellen Scans
        self.scan_dpi = "300"
        self.status_cache = {}   # Drucker-Schluessel -> letzter Status
        self.settings = QtCore.QSettings("hp-druckzentrale", "hp-druckzentrale")

        # Aktionen (Knoepfe und Zeilen loesen sie aus)
        self.act_search = QtGui.QAction("Drucker suchen", self, triggered=self.search)
        self.act_setup = QtGui.QAction("Einrichten", self, triggered=self.setup_current)
        self.act_test = QtGui.QAction("Testseite", self, triggered=self.test_page)
        self.act_web = QtGui.QAction("Weboberfläche", self, triggered=self.open_web)
        self.act_remove = QtGui.QAction("Entfernen", self, triggered=self.remove_current)
        self.act_update = QtGui.QAction("Nach Updates suchen", self, triggered=self.update_clicked)
        self.update_tag = None

        central = QtWidgets.QWidget()
        central.setObjectName("page")
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(24, 14, 24, 0)
        root.addLayout(self.build_topbar())
        self.banner = QtWidgets.QVBoxLayout()
        root.addLayout(self.banner)
        self.stack = QtWidgets.QStackedWidget()
        root.addWidget(self.stack, 1)

        self.home_scroll, self.home_body = self.scroll_page()
        self.list_scroll, self.list_body = self.scroll_page()
        self.stack.addWidget(self.home_scroll)
        self.stack.addWidget(self.list_scroll)
        self.tab_print = self.build_print_tab()
        self.tab_scan = self.build_scan_tab()
        self.tab_fax = self.build_fax_tab()
        self.print_page = page_wrap("Drucken", self.tab_print)
        self.stack.addWidget(self.print_page)
        self.stack.addWidget(self.tab_scan)
        self.stack.addWidget(page_wrap("Faxen", self.tab_fax))

        self.status = self.statusBar()
        self.check_dependencies()
        self.build_home()
        self.go(self.PRINTERS)
        QtCore.QTimer.singleShot(200, self.search)
        QtCore.QTimer.singleShot(400, self.refresh_scanners)
        QtCore.QTimer.singleShot(1500, self.check_update)
        if not self.settings.value("setup_done", False, type=bool) and not os.environ.get("HPDZ_SELFTEST"):
            QtCore.QTimer.singleShot(300, self.run_wizard)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh_status)
        self.timer.start(30000)

    # ----- Rahmen -----
    def build_topbar(self):
        bar = QtWidgets.QHBoxLayout()
        self.back_btn = QtWidgets.QPushButton("←")
        self.back_btn.setObjectName("round")
        self.back_btn.setToolTip("Alle Drucker")
        self.back_btn.clicked.connect(self.back)
        bar.addWidget(self.back_btn)
        bar.addStretch(1)
        pill = QtWidgets.QFrame()
        pill.setObjectName("pill")
        pl = QtWidgets.QHBoxLayout(pill)
        pl.setContentsMargins(4, 4, 4, 4)
        home = QtWidgets.QPushButton()
        home.setObjectName("roundacc")
        self.set_icon(home, "printer", "🖨")
        home.setToolTip("Mein Drucker")
        home.clicked.connect(lambda: self.go(self.HOME if self.current else self.PRINTERS))
        pl.addWidget(home)
        bar.addWidget(pill)
        bar.addStretch(1)
        self.update_btn = QtWidgets.QPushButton()
        self.update_btn.setObjectName("primary")
        self.update_btn.clicked.connect(self.update_clicked)
        self.update_btn.hide()
        bar.addWidget(self.update_btn)
        add = QtWidgets.QPushButton("+")
        add.setObjectName("round")
        add.setToolTip("Drucker hinzufügen")
        add.clicked.connect(self.run_wizard)
        bar.addWidget(add)
        self.bell = QtWidgets.QPushButton()
        self.bell.setObjectName("round")
        self.set_icon(self.bell, "notifications", "🔔")
        self.bell.setToolTip("Meldungen des Druckers")
        self.bell.clicked.connect(self.show_messages)
        bar.addWidget(self.bell)
        return bar

    @staticmethod
    def set_icon(btn, name, fallback):
        icon = QtGui.QIcon.fromTheme(name)
        if icon.isNull():
            btn.setText(fallback)   # ohne Symbolthema (z.B. andere Desktops) wenigstens ein Zeichen
        else:
            btn.setIcon(icon)

    def scroll_page(self):
        sc = QtWidgets.QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QtWidgets.QFrame.NoFrame)
        holder = QtWidgets.QWidget()
        holder.setObjectName("page")
        outer = QtWidgets.QHBoxLayout(holder)
        outer.addStretch(1)
        body = QtWidgets.QVBoxLayout()
        w = QtWidgets.QWidget()
        w.setLayout(body)
        w.setMaximumWidth(1100)
        outer.addWidget(w, 10)
        outer.addStretch(1)
        sc.setWidget(holder)
        return sc, body

    def go(self, page):
        self.stack.setCurrentIndex(page)
        self.back_btn.setToolTip("Alle Drucker" if page == self.HOME else "Zurück")

    def back(self):
        page = self.stack.currentIndex()
        if page == self.HOME:
            self.go(self.PRINTERS)
        elif page == self.PRINTERS and self.current:
            self.go(self.HOME)
        elif page in (self.PRINT, self.SCAN, self.FAX):
            self.go(self.HOME)

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

    # ----- Druckerkarte (Startseite und Liste) -----
    def printer_card(self, p, big):
        card = Clickable()
        card.setObjectName("printercard")
        lay = QtWidgets.QHBoxLayout(card)
        lay.setContentsMargins(34, 26, 34, 26)
        left = QtWidgets.QVBoxLayout()
        name = QtWidgets.QLabel(self.nickname(p))
        name.setStyleSheet(f"font-size: {48 if big else 30}px; font-weight: bold;")
        model = QtWidgets.QLabel((p.model or p.info or p.uri).replace(" - IPP Everywhere", ""))
        model.setObjectName("accent")
        model.setStyleSheet("font-size: 20px;" if big else "font-size: 16px;")
        left.addWidget(name)
        left.addWidget(model)
        left.addSpacing(14)
        bottom = QtWidgets.QHBoxLayout()
        ink_col = QtWidgets.QVBoxLayout()
        ink_holder = QtWidgets.QHBoxLayout()
        ink_col.addLayout(ink_holder)
        cap = QtWidgets.QLabel("Geschätzte Füllstände")
        cap.setObjectName("dim")
        cap.setStyleSheet("font-size: 11px;")
        ink_col.addWidget(cap)
        bottom.addLayout(ink_col)
        bottom.addSpacing(18)
        chips = QtWidgets.QVBoxLayout()
        bottom.addLayout(chips)
        bottom.addStretch(1)
        left.addLayout(bottom)
        if not big:
            state = QtWidgets.QHBoxLayout()
            info = QtWidgets.QLabel(f"{p.connection} · " + ("eingerichtet" if p.queue else "noch nicht eingerichtet"))
            info.setObjectName("dim")
            state.addWidget(info)
            if not p.queue:
                b = QtWidgets.QPushButton("Einrichten")
                b.setObjectName("primary")
                b.clicked.connect(lambda _=False, pp=p: self.setup_printer_from_list(pp))
                state.addWidget(b)
            state.addStretch(1)
            left.addSpacing(8)
            left.addLayout(state)
        left.addStretch(1)
        lay.addLayout(left, 1)
        img = QtWidgets.QLabel()
        img.setMinimumSize(300 if big else 200, 220 if big else 150)
        img.setAlignment(QtCore.Qt.AlignCenter)
        img.setPixmap(QtGui.QIcon.fromTheme("printer").pixmap(160 if big else 110))
        lay.addWidget(img)
        card.ink_holder, card.chips, card.img = ink_holder, chips, img
        card.big = big

        def got_image(ok, path, img=img, big=big):
            if ok and path:
                pix = QtGui.QPixmap(path)
                if not pix.isNull():
                    size = 320 if big else 200
                    img.setPixmap(pix.scaled(size, size, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))
        bg(lambda: printer_image(p), got_image)
        cached = self.status_cache.get(printer_key(p))
        if cached:
            self.fill_status(card, p, cached)
        return card

    def fill_status(self, card, p, res):
        self.clear(card.ink_holder)
        self.clear(card.chips)
        if res.get("markers"):
            card.ink_holder.addWidget(InkTubes(res["markers"], card.big))
        else:
            lab = QtWidgets.QLabel("Kein Tintenstand" if (p.queue or p.host or hp_uri_for(p)) else "Erst einrichten")
            lab.setObjectName("dim")
            card.ink_holder.addWidget(lab)
        texts = [reason_text(r) for r in res.get("reasons", [])]
        if res.get("state") and res["state"] != "Bereit":
            texts.insert(0, f"Der Drucker ist {res['state'].lower()}.")
        for t in texts[:3]:
            card.chips.addWidget(chip(t))
        card.chips.addStretch(1)

    # ----- Startseite -----
    def build_home(self):
        self.clear(self.home_body)
        p = self.current
        if not p:
            return
        self.home_card = self.printer_card(p, big=True)
        self.home_body.addWidget(self.home_card)
        self.home_body.addSpacing(18)
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(14)
        tiles = [tile("Scannen", "Mit dem Drucker scannen und als PDF oder Bild speichern.", "scanner",
                      lambda: self.go(self.SCAN)),
                 tile("Dokumente drucken", "PDFs und andere Dokumente drucken.", "x-office-document",
                      lambda: self.open_print(False)),
                 tile("Fotos drucken", "Bilder drucken, auch auf Fotopapier.", "image-x-generic",
                      lambda: self.open_print(True))]
        self.fax_tile = tile("Faxen", "Dokumente oder gescannte Seiten als Fax senden.", "mail-send",
                             lambda: self.go(self.FAX))
        tiles.append(self.fax_tile)
        if not p.queue:
            tiles.insert(0, tile("Drucker einrichten", "Einmalig einrichten, danach kannst du drucken.", "list-add",
                                 self.setup_current))
        for i, t in enumerate(tiles):
            grid.addWidget(t, i // 2, i % 2)
        self.home_body.addLayout(grid)
        self.home_body.addSpacing(18)
        uri = hp_uri_for(p)
        diag = [("document-print", "Testseite drucken", "", self.test_page)]
        if uri:
            diag += [("edit-clear", "Druckköpfe reinigen", "", lambda: self.hplip_tool("hp-clean")),
                     ("transform-move", "Druckköpfe ausrichten", "", lambda: self.hplip_tool("hp-align"))]
        diag.append(("view-refresh", "Status aktualisieren", "", self.refresh_status))
        self.home_body.addWidget(section("Diagnose", diag))
        self.home_body.addSpacing(14)
        self.nick_section = section("Personalisierung", [("document-edit", "Spitzname", self.nickname(p) + "  ✎",
                                                          self.rename)])
        self.home_body.addWidget(self.nick_section)
        self.home_body.addSpacing(14)
        settings_rows = []
        if p.host:
            settings_rows.append(("internet-web-browser", "Weboberfläche des Druckers", "", self.open_web))
        settings_rows.append(("tools-wizard", "Einrichtungs-Assistent", "", self.run_wizard))
        if p.queue:
            settings_rows.append(("list-remove", "Drucker von diesem PC entfernen", "", self.remove_current))
        settings_rows.append(("system-software-update", "Nach Updates suchen", f"v{APP_VERSION}", self.manual_update))
        self.home_body.addWidget(section("Druckereinstellungen", settings_rows))
        self.home_body.addSpacing(30)
        self.update_actions()

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
            self.build_home()
            self.build_list()

    def hplip_tool(self, tool):
        uri = hp_uri_for(self.current) if self.current else ""
        if uri:
            subprocess.Popen([tool, "-d", uri], start_new_session=True)
            self.status.showMessage(f"{tool} gestartet.")

    def show_messages(self):
        p = self.current
        res = self.status_cache.get(printer_key(p)) if p else None
        texts = [reason_text(r) for r in (res or {}).get("reasons", [])]
        if res and res.get("message"):
            texts.append(res["message"])
        menu = QtWidgets.QMenu(self)
        for t in texts or ["Keine Meldungen"]:
            menu.addAction(t).setEnabled(bool(texts))
        menu.exec(self.bell.mapToGlobal(QtCore.QPoint(0, self.bell.height())))

    # ----- Liste aller Drucker (Pfeil oben links) -----
    def build_list(self):
        self.clear(self.list_body)
        h = QtWidgets.QHBoxLayout()
        t = QtWidgets.QLabel("Meine Drucker")
        t.setStyleSheet("font-size: 34px; font-weight: bold;")
        h.addWidget(t)
        h.addStretch(1)
        s = QtWidgets.QPushButton("Drucker suchen")
        s.clicked.connect(self.search)
        h.addWidget(s)
        a = QtWidgets.QPushButton("+ Drucker hinzufügen")
        a.setObjectName("primary")
        a.clicked.connect(self.run_wizard)
        h.addWidget(a)
        self.list_body.addLayout(h)
        self.list_body.addSpacing(10)
        self.list_cards = {}
        if not self.printers:
            lab = QtWidgets.QLabel("Kein HP-Drucker gefunden.\n\nDrucker einschalten und per USB anstecken oder ins selbe "
                                   "LAN/WLAN bringen, dann „Drucker suchen“.")
            lab.setObjectName("dim")
            lab.setStyleSheet("font-size: 16px; padding: 30px 0;")
            self.list_body.addWidget(lab)
        for p in self.printers:
            card = self.printer_card(p, big=False)
            card.setCursor(QtCore.Qt.PointingHandCursor)
            card.clicked.connect(lambda pp=p: self.open_printer(pp))
            self.list_cards[printer_key(p)] = card
            self.list_body.addWidget(card)
            self.list_body.addSpacing(12)
        self.list_body.addStretch(1)

    def open_printer(self, p):
        self.select(self.printers.index(p))
        self.go(self.HOME)

    def setup_printer_from_list(self, p):
        self.current = p
        self.setup_current()

    # ----- Einrichtungs-Assistent (erster Start, oder ueber "+") -----
    def run_wizard(self):
        wiz = SetupWizard(self)
        wiz.setStyleSheet(QSS)
        wiz.exec()
        self.settings.setValue("setup_done", True)
        self.search()

    # ----- Updates -----
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

    # ----- Abhaengigkeiten -----
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
        bar.setObjectName("chip")
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

    # ----- Drucker suchen und waehlen -----
    def search(self):
        self.act_search.setEnabled(False)
        self.status.showMessage("Suche HP-Drucker (USB und Netzwerk)…")

        def done(ok, res):
            self.act_search.setEnabled(True)
            if not ok:
                self.status.showMessage(f"Suche fehlgeschlagen: {res}")
                return
            old = printer_key(self.current) if self.current else self.settings.value("last_printer", "")
            self.printers = res
            self.status.showMessage(f"{len(res)} HP-Drucker gefunden." if res else
                                    "Kein HP-Drucker gefunden. Ist er eingeschaltet und per USB oder im selben Netz verbunden?")
            idx = next((i for i, p in enumerate(res) if printer_key(p) == old), None)
            if idx is None:
                idx = next((i for i, p in enumerate(res) if p.queue), 0 if res else -1)
            self.select(idx)
            self.build_list()
            for p in res:
                self.refresh_status(p)
            if self.current and self.stack.currentIndex() == self.PRINTERS and (idx is not None and res[idx].queue):
                self.go(self.HOME)
        bg(discover, done)

    def select(self, row):
        self.current = self.printers[row] if row is not None and 0 <= row < len(self.printers) else None
        p = self.current
        if p:
            self.settings.setValue("last_printer", printer_key(p))
        self.build_home()
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
        if getattr(self, "fax_tile", None) is not None:
            try:
                self.fax_tile.setVisible(bool(p and p.caps and p.caps.get("fax")))
            except RuntimeError:
                pass   # Startseite wurde gerade neu aufgebaut
        self.update_fax_state()

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
        bg(lambda: hplip("caps", uri), done)

    # ----- Status und Tinte -----
    def refresh_status(self, p=None):
        p = p or self.current
        if not p:
            return

        def done(ok, res):
            if not ok:
                res = {"state": "", "reasons": [], "message": f"Status nicht abrufbar: {res}", "markers": [],
                       "supported": {}}
            self.status_cache[printer_key(p)] = res
            for card in (getattr(self, "home_card", None) if p is self.current else None,
                         getattr(self, "list_cards", {}).get(printer_key(p))):
                if card is not None:
                    try:
                        self.fill_status(card, p, res)
                    except RuntimeError:
                        pass   # Karte wurde inzwischen neu aufgebaut
            if p is self.current:
                self.apply_supported(res.get("supported", {}))
                self.bell.setStyleSheet(f"background: {ACC};" if res.get("reasons") else "")
        bg(lambda: query_status(p), done)


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
        """Feld mit kleiner Beschriftung ueber dem Wert, wie in HP Smart."""
        f = QtWidgets.QFrame()
        f.setStyleSheet(f"QFrame#field {{ border: 1px solid {LINE}; border-radius: 10px; }}"
                        "QFrame#field QComboBox { border: none; background: transparent; padding: 0; font-size: 15px; }")
        f.setObjectName("field")
        lay = QtWidgets.QVBoxLayout(f)
        lay.setContentsMargins(12, 6, 8, 6)
        lay.setSpacing(0)
        lab = QtWidgets.QLabel(label)
        lab.setStyleSheet("font-size: 11px;")
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
                                f'<a href="import" style="color:{ACC_T}; text-decoration:none;">importiere</a> eine Datei.')
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
        self.page_view.setStyleSheet(f"background: {BG}; border: none;")
        pl.addWidget(self.page_view, 1)
        row = QtWidgets.QHBoxLayout()
        more = QtWidgets.QPushButton("+ Seite importieren")
        more.clicked.connect(self.import_pages)
        delete = QtWidgets.QPushButton("Ausgewählte löschen")
        delete.clicked.connect(self.delete_pages)
        clear = QtWidgets.QPushButton("Alle löschen")
        clear.clicked.connect(self.clear_pages)
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
        panel.setObjectName("card")
        panel.setFixedWidth(300)
        rl = QtWidgets.QVBoxLayout(panel)
        rl.setContentsMargins(18, 20, 18, 18)
        rl.setSpacing(12)
        self.scanner_combo = QtWidgets.QComboBox()
        self.scanner_combo.currentIndexChanged.connect(self.load_scanner_options)
        self.scanner_field = self.field("Scanner", self.scanner_combo)
        rl.addWidget(self.scanner_field)
        self.scan_source = QtWidgets.QComboBox()
        rl.addWidget(self.field("Quelle", self.scan_source))
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
        reset.setStyleSheet(f"background: transparent; color: {DIM};")
        reset.clicked.connect(self.load_scanner_options)
        rl.addWidget(reset)
        rl.addStretch(1)
        self.preview_btn = QtWidgets.QPushButton("Vorschau")
        self.preview_btn.setMinimumHeight(44)
        self.preview_btn.setStyleSheet("background: transparent; border: 1.5px solid #D6D6DE; border-radius: 22px;")
        self.preview_btn.clicked.connect(self.do_preview)
        rl.addWidget(self.preview_btn)
        self.scan_btn = QtWidgets.QPushButton("Scannen")
        self.scan_btn.setObjectName("primary")
        self.scan_btn.setMinimumHeight(44)
        self.scan_btn.setStyleSheet("border-radius: 22px;")
        self.scan_btn.clicked.connect(self.do_scan)
        rl.addWidget(self.scan_btn)
        outer.addWidget(panel)
        return page

    def apply_preset(self):
        mode = "Gray" if self.scan_preset.currentData() == "Dokument" else "Color"
        for combo, val in ((self.scan_mode, mode), (self.scan_res, "300")):
            i = combo.findData(val)
            if i >= 0:
                combo.setCurrentIndex(i)

    def scan_args(self):
        return (self.scanner_combo.currentData(), self.scan_mode.currentData(), self.scan_res.currentData(),
                self.scan_source.currentData(), self.scan_area.currentData(), self.scan_crop.isChecked())

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
        if self.page_view.count():
            self.scan_area_stack.setCurrentIndex(2)

    def import_pages(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Bilder importieren", os.path.expanduser("~"),
                                                          "Bilder (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp)")
        self.add_page_files(files)

    def clear_pages(self):
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
                self.scanner_combo.addItem("Kein HP-Scanner gefunden", None)
            for dev, desc in self.scanners:
                kind = "Netzwerk/IPP" if dev.startswith("airscan") else "HPLIP"
                short = re.sub(r"^eSCL\s+|\s+ip=.*$", "", desc)   # airscan haengt Protokoll und IPs an
                self.scanner_combo.addItem(f"{short}  ({kind})", dev)
            self.scanner_combo.setEnabled(bool(self.scanners))
            self.scanner_field.setVisible(len(self.scanners) != 1)
            self.scanner_combo.blockSignals(False)
            self.match_scanner()
            self.load_scanner_options()
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
                     "ADF": "Einzug", "ADF Duplex": "Einzug beidseitig"}
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
        bg(lambda: save_pages(files, path, ext, self.scan_dpi),
           lambda ok, res: self.status.showMessage(("Gespeichert: " + ", ".join(res)) if ok else f"Speichern fehlgeschlagen: {res}"))

    # ----- Fax -----
    def build_fax_tab(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        self.fax_hint = QtWidgets.QLabel()
        self.fax_hint.setWordWrap(True)
        lay.addWidget(self.fax_hint)
        self.fax_setup_btn = QtWidgets.QPushButton("Fax einrichten (HPLIP-Assistent)")
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
                              "Dieser Drucker kann faxen. Dafür richtet HPLIP eine eigene Fax-Warteschlange ein – "
                              "einmalig über den Assistenten.")
        self.fax_setup_btn.setVisible(not has_queue)
        self.fax_send.setEnabled(has_queue)

    def setup_fax(self):
        p = self.current
        uri = hp_uri_for(p) if p else ""
        if not uri:
            return
        subprocess.Popen(["hp-setup", uri], start_new_session=True)
        self.status.showMessage("HPLIP-Assistent gestartet – dort „Fax einrichten“ wählen, danach „Drucker suchen“.")

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


def main():
    global _bridge
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
