# SPDX-License-Identifier: GPL-3.0-or-later
"""
Téléchargeur de tuiles vers MBTiles — aucune dépendance graphique.

Le meilleur de QGIS et de MOBAC :
- sources XYZ personnalisées, import des connexions QGIS (.xml) et des
  sources MOBAC (customMapSource .xml) ;
- emprise rectangulaire + plage de zooms, comptage exact avant lancement ;
- écriture directe en MBTiles, reprise d'un téléchargement interrompu
  (les tuiles présentes et valides ne sont pas retéléchargées) ;
- plusieurs connexions persistantes, débit plafonné, nouvelles tentatives
  avec attente progressive (429 / 5xx / Retry-After) ;
- jamais de page d'erreur enregistrée à la place d'une tuile ;
- tuiles absentes côté serveur (404/204) mémorisées dans mbtf_absentes,
  pour que le contrôle de complétude ne les compte pas comme manquantes.
"""

import base64
import datetime
import email.utils
import hashlib
import http.client
import json
import os
import queue
import random
import re
import socket
import sqlite3
import ssl
import threading
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections import OrderedDict, deque
from pathlib import Path

import moteur as m

# HTTP/2 (facultatif) : httpx + h2, embarqués dans l'AppImage. Sans eux : HTTP/1.1 persistant.
try:
    import httpx
    import h2
    HTTP2_DISPONIBLE = bool(h2)
except Exception:
    httpx = None
    HTTP2_DISPONIBLE = False
UTILISER_HTTP2 = False  # réglage « HTTP/2 » des options avancées

UA_DEFAUT = f"MBTilesFusion/{m.VERSION} (usage hors ligne personnel)"
FORMATS = ("auto", "png", "jpg", "webp", "pbf")
TYPES_MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp",
              "gif": "image/gif", "pbf": "application/x-protobuf"}


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def nouvelle_source(**kw):
    s = {"id": uuid.uuid4().hex[:10], "nom": "Nouvelle source", "url": "",
         "sous_domaines": "", "format": "auto", "zoom_min": 0, "zoom_max": 19,
         "user_agent": "", "referer": "", "attribution": ""}
    s.update({k: v for k, v in kw.items() if v is not None})
    return s


_JETON = re.compile(r"\{(-y|z|x|y|q|s|switch:[^}]*)\}")


def normaliser_modele(url):
    """
    Ramène les variantes courantes à la syntaxe {z} {x} {y} {q} {s} :
    accolades encodées (%7B…%7D), syntaxe MOBAC ({$z}, {$serverpart}), {zoom}.
    """
    u = (url or "").strip()
    u = re.sub(r"%7[Dd]", "}", re.sub(r"%7[Bb]", "{", u))
    u = re.sub(r"\{\$(z|x|y|q)\}", r"{\1}", u)
    u = u.replace("{$serverpart}", "{s}").replace("{zoom}", "{z}")
    return u


def verifier_modele(url):
    """Message d'erreur, ou None si le modèle d'URL est utilisable."""
    url = normaliser_modele(url)
    if not re.match(r"^https?://", url or ""):
        return "L’URL doit commencer par http:// ou https://"
    jetons = set(j.split(":")[0] for j in _JETON.findall(url))
    if "q" in jetons:
        return None
    manque = [j for j in ("z", "x") if j not in jetons]
    if "y" not in jetons and "-y" not in jetons:
        manque.append("y")
    if manque:
        return "Variables manquantes : " + ", ".join("{" + j + "}" for j in manque)
    return None


def quadkey(x, y, z):
    q = []
    for i in range(z, 0, -1):
        c = 0
        masque = 1 << (i - 1)
        if x & masque:
            c += 1
        if y & masque:
            c += 2
        q.append(str(c))
    return "".join(q)


def _liste_sous_domaines(texte):
    texte = (texte or "").strip()
    if not texte:
        return []
    if "," in texte or " " in texte:
        return [p for p in re.split(r"[,\s]+", texte) if p]
    return list(texte)  # « abc » → a, b, c


def url_tuile(source, z, x, y):
    sd = _liste_sous_domaines(source.get("sous_domaines"))

    def remplace(mo):
        j = mo.group(1)
        if j == "z":
            return str(z)
        if j == "x":
            return str(x)
        if j == "y":
            return str(y)
        if j == "-y":
            return str(m.tms(y, z))
        if j == "q":
            return quadkey(x, y, z)
        if j == "s":
            return sd[(x + y) % len(sd)] if sd else ""
        choix = j.split(":", 1)[1].split(",")
        return choix[(x + y) % len(choix)]
    return _JETON.sub(remplace, normaliser_modele(source["url"]))


def entetes_source(source):
    h = {"User-Agent": source.get("user_agent") or UA_DEFAUT, "Accept": "image/*,*/*;q=0.8"}
    if source.get("referer"):
        h["Referer"] = source["referer"]
    return h


def _entier(v, defaut):
    try:
        return max(0, min(24, int(float(v))))
    except (TypeError, ValueError):
        return defaut


def _url_qgis(url):
    """QGIS encode parfois les accolades (%7Bz%7D) : on les rétablit."""
    return re.sub(r"%7[Dd]", "}", re.sub(r"%7[Bb]", "{", (url or "").strip()))


def _lire_xml(texte):
    """
    Parse tolérant. QGIS écrit des attributs à préfixe non déclaré
    (« http-header:referer ») que le parseur Python refuse : on les renomme
    en « http-header__referer ».
    """
    texte = re.sub(r"^\s*<\?xml[^>]*\?>", "", texte)
    texte = re.sub(r"(\s)(?!xmlns)([A-Za-z_][\w.-]*):([A-Za-z_][\w.-]*)(\s*=)", r"\1\2__\3\4", texte)
    return ET.fromstring(texte)


def _sources_projet_qgis(racine):
    """Couches XYZ d'un projet QGIS (.qgs/.qgz) ou d'une définition de couche (.qlr)."""
    trouvees = []
    for couche in racine.iter("maplayer"):
        ds = couche.findtext("datasource") or ""
        if "type=xyz" not in ds:
            continue
        p = urllib.parse.parse_qs(ds, keep_blank_values=True)
        url = _url_qgis((p.get("url") or [""])[0])
        nom = (couche.findtext("layername") or "Couche QGIS").strip()
        trouvees.append(nouvelle_source(
            nom=nom, url=url,
            zoom_min=_entier((p.get("zmin") or [0])[0], 0),
            zoom_max=_entier((p.get("zmax") or [19])[0], 19),
            referer=(p.get("referer") or p.get("http-header:referer") or [""])[0]))
    return trouvees


def importer_sources(chemin):
    """
    Sources lues dans :
    - un export de connexions QGIS (XYZ raster ou tuiles vectorielles, .xml) ;
    - un projet QGIS (.qgs, .qgz) ou une définition de couche (.qlr) ;
    - une source MOBAC (customMapSource, .xml).
    """
    if str(chemin).lower().endswith(".qgz"):
        import zipfile
        with zipfile.ZipFile(chemin) as z:
            nom = next((n for n in z.namelist() if n.lower().endswith(".qgs")), None)
            if not nom:
                raise ValueError("aucun projet .qgs dans l’archive .qgz")
            brut = z.read(nom)
    else:
        brut = Path(chemin).read_bytes()
    try:
        racine = _lire_xml(brut.decode("utf-8-sig", errors="replace"))
    except ET.ParseError as e:
        raise ValueError(f"XML illisible ({e})")
    sources = []
    if racine.tag in ("qgsXYZTilesConnections", "qgsVectorTileConnections"):
        vecteur = racine.tag == "qgsVectorTileConnections"
        for e in racine.iter("vectortile" if vecteur else "xyztiles"):
            a = e.attrib
            sources.append(nouvelle_source(
                nom=a.get("name") or "Source QGIS", url=_url_qgis(a.get("url")),
                zoom_min=_entier(a.get("zmin"), 0), zoom_max=_entier(a.get("zmax"), 19),
                format="pbf" if vecteur else "auto",
                referer=a.get("referer") or a.get("http-header__referer") or ""))
    elif racine.tag in ("qgis", "qlr"):
        sources = _sources_projet_qgis(racine)
    elif racine.tag in ("customMapSource", "customMapSources"):
        for e in ([racine] if racine.tag == "customMapSource" else racine.iter("customMapSource")):
            def t(nom, defaut=""):
                v = e.findtext(nom)
                return v.strip() if v else defaut
            url = t("url")
            url = (url.replace("{$z}", "{z}").replace("{$x}", "{x}").replace("{$y}", "{y}")
                   .replace("{$q}", "{q}").replace("{$serverpart}", "{s}"))
            fmt = t("tileType", "auto").lower()
            sources.append(nouvelle_source(
                nom=t("name", "Source MOBAC"), url=url,
                sous_domaines=" ".join(t("serverParts").split()),
                zoom_min=_entier(t("minZoom", "0"), 0), zoom_max=_entier(t("maxZoom", "19"), 19),
                format={"jpeg": "jpg"}.get(fmt, fmt if fmt in FORMATS else "auto")))
    else:
        raise ValueError(f"format non reconnu (balise racine « {racine.tag} »). Attendu : export "
                         "de connexions XYZ QGIS, projet QGIS, .qlr ou customMapSource MOBAC")
    sources = [s for s in sources if s["url"]]
    if not sources:
        raise ValueError("aucune source trouvée dans le fichier")
    return sources


# ---------------------------------------------------------------------------
# Client HTTP : connexions persistantes par fil, proxy système respecté
# ---------------------------------------------------------------------------

class ClientHTTP:
    """
    HTTPS : HTTP/2 via httpx si disponible (plusieurs requêtes simultanées sur une seule
    connexion, client partagé entre les fils). Sinon, et pour HTTP : HTTP/1.1 avec une
    connexion persistante par fil. Le proxy système est respecté dans les deux cas.
    """

    def __init__(self, delai=30, http2=None):
        self.delai = delai
        self.local = threading.local()
        self.ssl = ssl.create_default_context()
        self.hx = None
        self.versions = set()
        if HTTP2_DISPONIBLE and (UTILISER_HTTP2 if http2 is None else http2):
            try:
                self.hx = httpx.Client(http2=True, timeout=delai, verify=self.ssl, trust_env=True,
                                       follow_redirects=True,
                                       limits=httpx.Limits(max_connections=32, max_keepalive_connections=16))
            except Exception:
                self.hx = None

    def _connexions(self):
        if not hasattr(self.local, "c"):
            self.local.c = {}
        return self.local.c

    def _proxy(self, schema, hote):
        try:
            if urllib.request.proxy_bypass(hote):
                return None
        except Exception:
            pass
        p = urllib.request.getproxies().get(schema)
        if not p:
            return None
        u = urllib.parse.urlsplit(p if "://" in p else "http://" + p)
        auth = None
        if u.username:
            jeton = f"{urllib.parse.unquote(u.username)}:{urllib.parse.unquote(u.password or '')}"
            auth = "Basic " + base64.b64encode(jeton.encode()).decode()
        return u.hostname, u.port or 8080, auth

    def _connexion(self, schema, hote, port):
        cle = (schema, hote, port)
        conns = self._connexions()
        if cle in conns:
            return conns[cle]
        proxy = self._proxy(schema, hote)
        if proxy:
            ph, pp, auth = proxy
            if schema == "https":
                c = http.client.HTTPSConnection(ph, pp, timeout=self.delai, context=self.ssl)
                c.set_tunnel(hote, port, headers={"Proxy-Authorization": auth} if auth else None)
                c._mbtf_absolu = False
            else:
                c = http.client.HTTPConnection(ph, pp, timeout=self.delai)
                c._mbtf_absolu = True
                c._mbtf_auth = auth
        elif schema == "https":
            c = http.client.HTTPSConnection(hote, port, timeout=self.delai, context=self.ssl)
            c._mbtf_absolu = False
        else:
            c = http.client.HTTPConnection(hote, port, timeout=self.delai)
            c._mbtf_absolu = False
        conns[cle] = c
        return c

    def fermer(self):
        """Ferme les connexions HTTP/1.1 du fil courant."""
        for c in self._connexions().values():
            try:
                c.close()
            except Exception:
                pass
        self.local.c = {}

    def fermer_tout(self):
        self.fermer()
        if self.hx is not None:
            try:
                self.hx.close()
            except Exception:
                pass
            self.hx = None

    def get(self, url, entetes):
        """Retourne (statut, en-têtes en minuscules, corps)."""
        if self.hx is not None and url.startswith("https://"):
            r = self.hx.get(url, headers=entetes)
            self.versions.add(r.http_version)
            return r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.content
        self.versions.add("HTTP/1.1")
        u = urllib.parse.urlsplit(url)
        schema = u.scheme.lower()
        port = u.port or (443 if schema == "https" else 80)
        chemin = (u.path or "/") + ("?" + u.query if u.query else "")
        for essai in (1, 2):
            c = self._connexion(schema, u.hostname, port)
            cible = url if getattr(c, "_mbtf_absolu", False) else chemin
            h = dict(entetes)
            if getattr(c, "_mbtf_auth", None):
                h["Proxy-Authorization"] = c._mbtf_auth
            try:
                c.request("GET", cible, headers=h)
                r = c.getresponse()
                corps = r.read()
                hs = {k.lower(): v for k, v in r.getheaders()}
                if hs.get("connection", "").lower() == "close":
                    c.close()
                    self._connexions().pop((schema, u.hostname, port), None)
                return r.status, hs, corps
            except (http.client.HTTPException, ConnectionError, socket.timeout, ssl.SSLError, OSError):
                c.close()
                self._connexions().pop((schema, u.hostname, port), None)
                if essai == 2:
                    raise


def recuperer_tuile(client, source, z, x, y, essais=4, arret=None, depuis=None, limiteur=None):
    """
    ('ok', données, format) | ('absente', None, None) | ('inchangee', None, None)
    | ('erreur', message, None).
    `depuis` (horodatage) : requête conditionnelle If-Modified-Since → « inchangee » sur 304.
    Une réponse qui n'est pas une image n'est jamais acceptée comme tuile.
    """
    url = url_tuile(source, z, x, y)
    entetes = entetes_source(source)
    if depuis:
        entetes["If-Modified-Since"] = email.utils.formatdate(depuis, usegmt=True)
    vecteur = m.est_vecteur(source.get("format"))
    erreur = "échec"
    for essai in range(essais):
        if arret is not None and arret.is_set():
            return "erreur", "annulé", None
        dernier = essai == essais - 1
        attente = 0 if dernier else min(30, 2 ** essai) * (0.75 + random.random() / 2)
        try:
            statut, h, corps = client.get(url, entetes)
        except Exception as e:
            erreur = f"réseau : {str(e) or e.__class__.__name__}"[:160]
            _dormir(attente, arret)
            continue
        if limiteur is not None:
            try:
                apres = float(h.get("retry-after", 0) or 0)
            except ValueError:
                apres = 0
            limiteur.signaler(statut, apres)
        if statut == 304:
            return "inchangee", None, None
        if statut == 200:
            if not corps:
                return "absente", None, None
            fmt = m.detecter_format(corps)
            if fmt:
                return "ok", corps, fmt
            if vecteur and corps[:1] not in (b"<", b"{"):
                return "ok", corps, "pbf"
            erreur = f"réponse non image ({h.get('content-type', '?')})"
            _dormir(attente, arret)
            continue
        if statut in (204, 404, 410):
            return "absente", None, None
        if statut in (408, 429, 500, 502, 503, 504):
            erreur = f"HTTP {statut}"
            if limiteur is not None and statut in (429, 503):
                if not dernier:
                    limiteur.attendre(arret)  # attente commune à tous les fils
                continue
            if not dernier:
                try:
                    attente = max(attente, min(120, float(h.get("retry-after", 0))))
                except ValueError:
                    pass
            _dormir(attente, arret)
            continue
        return "erreur", f"HTTP {statut}", None
    return "erreur", erreur, None


def _dormir(s, arret):
    fin = time.monotonic() + s
    while True:
        reste = fin - time.monotonic()
        if reste <= 0 or (arret is not None and arret.is_set()):
            return
        time.sleep(min(0.2, reste))


class Limiteur:
    """
    Plafond de requêtes par seconde partagé entre les fils, et adaptatif :
    - refus fréquents (429 / 503) : débit × 0,6, Retry-After respecté par tous les fils ;
    - réponses normales : remontée progressive (+15 %) jusqu'au réglage.
    """

    def __init__(self, debit, rafale=1):
        self.max = float(debit) if debit and debit > 0 else 0.0
        self.debit = self.max
        self.rafale = max(1, int(rafale))  # requêtes permises d'un coup (seau à jetons)
        self.jetons = float(self.rafale)
        self.maj = time.monotonic()
        self.pause_jusqua = 0.0
        self.succes = 0
        self.dernier_recul = 0.0
        self.reculs = 0
        self.recentes = deque(maxlen=30)
        self.verrou = threading.Lock()

    def attendre(self, arret=None):
        with self.verrou:
            maintenant = time.monotonic()
            attente = max(0.0, self.pause_jusqua - maintenant)
            if self.debit:
                debit = max(0.2, self.debit)
                self.jetons = min(self.rafale, self.jetons + (maintenant - self.maj) * debit)
                self.maj = maintenant
                self.jetons -= 1
                if self.jetons < 0:
                    attente = max(attente, -self.jetons / debit)
        _dormir(attente, arret)

    def signaler(self, statut, retry_after=0.0):
        """
        Recul (débit × 0,6) seulement si les refus sont fréquents : au moins 3 refus (429/503)
        sur les 30 dernières réponses. Un refus isolé est simplement retenté.
        Remontée (+15 %) après une série de réponses sans refus.
        """
        with self.verrou:
            maintenant = time.monotonic()
            refus = statut in (429, 503)
            self.recentes.append(refus)
            if refus:
                if retry_after:
                    self.pause_jusqua = max(self.pause_jusqua, maintenant + min(120.0, retry_after))
                if sum(self.recentes) >= 3 and maintenant - self.dernier_recul > 2.0:
                    base = self.debit or 8.0
                    self.debit = max(0.5, base * 0.6)
                    self.dernier_recul = maintenant
                    self.reculs += 1
                    self.succes = 0
                    self.recentes.clear()
            elif statut and statut < 500 and self.debit < (self.max or float("inf")):
                self.succes += 1
                if self.succes >= max(10, int(self.debit)) and not any(self.recentes):
                    self.debit = min(self.max or self.debit * 2, self.debit * 1.15 + 0.2)
                    self.succes = 0

    @property
    def bride(self):
        """Le serveur a imposé un débit inférieur au réglage."""
        return self.reculs > 0 and self.debit < (self.max or float("inf")) * 0.9


def tester_source(source, emprise=None):
    """Télécharge une tuile de test. Retourne un texte lisible et un booléen de succès."""
    z = max(source.get("zoom_min", 0), min(source.get("zoom_max", 19), 12))
    if emprise:
        lon, lat = (emprise[0] + emprise[2]) / 2, (emprise[1] + emprise[3]) / 2
    else:
        lon, lat = 2.35, 46.6
    fx, fy = m.lonlat_vers_xy(lon, lat, z)
    x, y = int(fx), int(fy)
    client = ClientHTTP(delai=15)
    t0 = time.monotonic()
    etat, donnees, fmt = recuperer_tuile(client, source, z, x, y, essais=1)
    client.fermer_tout()
    ms = (time.monotonic() - t0) * 1000
    if etat == "ok":
        return True, f"OK — tuile {z}/{x}/{y} : {fmt.upper()}, {m.taille_lisible(len(donnees))}, {ms:.0f} ms"
    if etat == "absente":
        return False, f"Le serveur n’a pas de tuile en {z}/{x}/{y} (absente). Essaie une autre emprise."
    return False, f"Échec en {z}/{x}/{y} : {donnees}"


def echantillonner(source, emprise, z, n=8):
    """Poids moyen d'une tuile, mesuré sur quelques tuiles au hasard dans l'emprise."""
    x0, x1, y0, y1 = m.plage(emprise, z)
    r = random.Random(0)
    cles = {(r.randint(x0, x1), r.randint(y0, y1)) for _ in range(n * 2)}
    client = ClientHTTP(delai=15)
    tailles = []
    try:
        for x, y in list(cles)[:n]:
            etat, d, _ = recuperer_tuile(client, source, z, x, y, essais=2)
            if etat == "ok":
                tailles.append(len(d))
    finally:
        client.fermer_tout()
    return (sum(tailles) / len(tailles)) if tailles else None


# ---------------------------------------------------------------------------
# Cache de tuiles : un MBTiles par source, dans le dossier choisi
# ---------------------------------------------------------------------------

def chemin_cache(dossier, source):
    """Fichier cache de la source : identifié par l'URL (renommer la source ne le perd pas)."""
    h = hashlib.sha1(source["url"].encode()).hexdigest()[:8]
    try:
        for f in sorted(os.listdir(dossier)):
            if f.endswith(f"-{h}.mbtiles"):
                return os.path.join(dossier, f)
    except OSError:
        pass
    nom = re.sub(r"[^\w-]+", "_", source.get("nom") or "source").strip("_")[:40] or "source"
    return os.path.join(dossier, f"{nom}-{h}.mbtiles")


def source_json(source):
    return json.dumps({k: source.get(k) for k in (
        "nom", "url", "sous_domaines", "format", "zoom_min", "zoom_max",
        "user_agent", "referer", "attribution")}, ensure_ascii=False)


def source_du_fichier(chemin):
    """Source enregistrée dans un MBTiles téléchargé par l'outil (ou None)."""
    try:
        con = m.ouvrir_lecture(chemin)
        try:
            meta = m.lire_metadata(con)
        finally:
            con.close()
        s = json.loads(meta.get("mbtf_source", ""))
        return s if isinstance(s, dict) and s.get("url") else None
    except Exception:
        return None


def ouvrir_cache_ecriture(cache, source):
    """
    Ouvre (ou crée) le cache d'une source en écriture : journal WAL (les lectures de la
    carte continuent pendant les écritures), attente si un autre fil écrit.
    Retourne (connexion, format déjà connu ou None).
    """
    if os.path.exists(cache):
        ancienne = source_du_fichier(cache)
        if ancienne and ancienne.get("url") != source["url"]:
            raise ValueError(f"le cache {Path(cache).name} appartient à une autre source "
                             f"({ancienne.get('nom', '?')})")
    os.makedirs(os.path.dirname(os.path.abspath(cache)), exist_ok=True)
    con = sqlite3.connect(cache, isolation_level=None, check_same_thread=False, timeout=15)
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    m.creer_schema_cache(con)
    m.completer_dates(con)
    meta = m.lire_metadata(con)
    fmt = source.get("format", "auto")
    valeurs = {"name": f"Cache — {source.get('nom', '')}",
               "type": "baselayer", "version": "1.1",
               "attribution": source.get("attribution", ""),
               "mbtf_source": source_json(source)}
    if fmt != "auto" and not meta.get("format"):
        valeurs["format"] = fmt
    for k, v in valeurs.items():
        con.execute("INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)", (k, v))
    return con, meta.get("format") or (fmt if fmt != "auto" else None)


def mediane(valeurs):
    v = sorted(valeurs)
    if not v:
        return 0.0
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


class Telechargement:
    """
    Complète le cache `cache` pour les rectangles `niveaux` ({z: [emprise, …]}).
    Les tuiles déjà en cache (ou connues absentes du serveur) ne sont pas retéléchargées.
    `etat(dict)` est appelé régulièrement (depuis le fil du téléchargement).
    """

    def __init__(self, source, niveaux, cache, connexions=4, debit=8.0,
                 journal=print, etat=lambda d: None, limite_date=None):
        self.source = dict(source)
        self.limite_date = int(limite_date) if limite_date else None
        self.niveaux = {z: [tuple(e) for e in v] for z, v in niveaux.items()}
        self.cache = os.path.abspath(cache)
        self.connexions = max(1, int(connexions))
        self.limiteur = Limiteur(debit)
        self.journal = journal
        self.rappel_etat = etat
        self.pause = threading.Event()
        self.arret = threading.Event()
        self.client = ClientHTTP()

    # ----- préparation
    def _ouvrir(self):
        con, self.format_ecrit = ouvrir_cache_ecriture(self.cache, self.source)
        return con

    def _a_faire(self, con, z, emprise, planifiees):
        """
        Tuiles (x, y, depuis) du rectangle au niveau z, sans doublon entre rectangles :
        absentes du cache (depuis = None) ou périmées (depuis = date de la version en cache).
        """
        x0, x1, y0, y1 = m.plage(emprise, z)
        r0, r1 = m.tms(y1, z), m.tms(y0, z)
        valide = m.sql_valide(self.format_ecrit or "png")
        deja = set(con.execute(
            f"SELECT tile_column, tile_row FROM tiles WHERE zoom_level = ? AND tile_column BETWEEN ? AND ? "
            f"AND tile_row BETWEEN ? AND ? AND {valide}", (z, x0, x1, r0, r1)))
        deja.update(con.execute(
            "SELECT tile_column, tile_row FROM mbtf_absentes WHERE zoom_level = ? AND tile_column "
            "BETWEEN ? AND ? AND tile_row BETWEEN ? AND ?", (z, x0, x1, r0, r1)))
        perimees = {}
        if self.limite_date:
            perimees = {(c, r): d for c, r, d in con.execute(
                "SELECT tile_column, tile_row, date FROM mbtf_dates WHERE zoom_level = ? AND tile_column "
                "BETWEEN ? AND ? AND tile_row BETWEEN ? AND ? AND date < ?",
                (z, x0, x1, r0, r1, self.limite_date))}
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                cle = (x, m.tms(y, z))
                if cle in planifiees or (cle in deja and cle not in perimees):
                    continue
                planifiees.add(cle)
                yield x, y, perimees.get(cle)

    # ----- exécution
    def executer(self):
        cov = m.couverture(self.cache, self.niveaux, self.limite_date)
        total = sum(d["attendues"] for d in cov.values())
        manquantes = sum(d["manquantes"] for d in cov.values())
        perimees = sum(d["perimees"] for d in cov.values())
        a_faire = manquantes + perimees
        st = {"total": total, "deja": total - a_faire, "a_faire": a_faire, "ok": 0, "absentes": 0,
              "maj": 0, "inchangees": 0, "recues": 0,
              "erreurs": 0, "octets": 0, "zoom": min(self.niveaux) if self.niveaux else 0,
              "debut": time.monotonic(), "pause": False, "fini": False, "message": "",
              "interrompu": False, "instant": 0.0, "ko_s": 0.0, "latence_ms": 0, "cause": ""}
        if not a_faire:
            self.journal("Cache complet : rien à télécharger")
            st["fini"] = True
            self.rappel_etat(dict(st))
            return st
        con = self._ouvrir()
        self.journal(f"Téléchargement : {self.source['nom']}, {m.nombre(manquantes)} tuiles à télécharger, "
                     f"{m.nombre(perimees)} à vérifier ({m.nombre(total - a_faire)} à jour en cache)")
        taches = queue.Queue()
        resultats = queue.Queue()
        echecs = []
        fils = [threading.Thread(target=self._ouvrier, args=(taches, resultats), daemon=True)
                for _ in range(self.connexions)]
        for f in fils:
            f.start()

        derniere_maj = 0.0
        dernier_commit = time.monotonic()
        en_vol = 0
        erreurs_suivies = 0
        dernier_message_erreur = ""
        # Diagnostic : rythme sur 10 s, temps de réponse du serveur, cause du rythme.
        fenetre = deque()            # (instant, octets) des réponses reçues
        latences = deque(maxlen=60)  # temps de réponse récents (s)
        par_zoom = {}                # z -> temps de réponse (s)
        base_latence = [None]
        relance = [False]
        con.execute("BEGIN")

        def dater(z, col, row):
            con.execute("INSERT OR REPLACE INTO mbtf_dates VALUES (?, ?, ?, ?)", (z, col, row, int(time.time())))

        def ecrire(z, x, y, depuis, r):
            nonlocal erreurs_suivies, dernier_message_erreur
            etat, d, fmt = r
            row = m.tms(y, z)
            if etat != "erreur" or d != "annulé":
                st["recues"] += 1
            if etat == "ok":
                if self.format_ecrit is None:
                    self.format_ecrit = fmt
                    con.execute("INSERT OR REPLACE INTO metadata (name, value) VALUES ('format', ?)", (fmt,))
                ancienne = None
                if depuis is not None:
                    ancienne = con.execute("SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? "
                                           "AND tile_row = ?", (z, x, row)).fetchone()
                if ancienne is not None and bytes(ancienne[0]) == bytes(d):
                    st["inchangees"] += 1
                else:
                    con.execute("INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?)", (z, x, row, d))
                    st["maj" if ancienne is not None else "ok"] += 1
                    st["octets"] += len(d)
                con.execute("DELETE FROM mbtf_absentes WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?",
                            (z, x, row))
                dater(z, x, row)
                erreurs_suivies = 0
            elif etat == "inchangee":
                st["inchangees"] += 1
                dater(z, x, row)
                erreurs_suivies = 0
            elif etat == "absente":
                # Une tuile en cache que le serveur n'a plus est conservée.
                con.execute("INSERT OR IGNORE INTO mbtf_absentes SELECT ?, ?, ? WHERE NOT EXISTS "
                            "(SELECT 1 FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?)",
                            (z, x, row, z, x, row))
                st["absentes"] += 1
                dater(z, x, row)
                erreurs_suivies = 0
            elif d != "annulé":
                echecs.append((z, x, y, depuis, d))
                st["erreurs"] += 1
                erreurs_suivies += 1
                dernier_message_erreur = d

        def vider(bloquant):
            nonlocal en_vol, dernier_commit
            try:
                while True:
                    z, x, y, depuis, r, duree = (resultats.get(timeout=0.25) if bloquant
                                                 else resultats.get_nowait())
                    en_vol -= 1
                    if not (r[0] == "erreur" and r[1] == "annulé"):
                        fenetre.append((time.monotonic(), len(r[1]) if r[0] == "ok" else 0))
                    if r[0] != "erreur":  # réponses abouties seulement (sans les reprises)
                        latences.append(duree)
                        par_zoom.setdefault(z, []).append(duree)
                        if base_latence[0] is None and len(latences) >= 50:
                            base_latence[0] = mediane(latences)
                    ecrire(z, x, y, depuis, r)
                    bloquant = False
            except queue.Empty:
                pass
            if time.monotonic() - dernier_commit > 2:
                con.execute("COMMIT")
                con.execute("BEGIN")
                dernier_commit = time.monotonic()

        def signaler(force=False):
            nonlocal derniere_maj
            if force or time.monotonic() - derniere_maj > 0.3:
                derniere_maj = time.monotonic()
                st["pause"] = self.pause.is_set()
                st["debit_reglage"] = self.limiteur.max
                st["debit_actuel"] = self.limiteur.debit
                st["bride"] = self.limiteur.bride
                st["http"] = ", ".join(sorted(self.client.versions))
                diagnostiquer()
                self.rappel_etat(dict(st))

        def diagnostiquer():
            maintenant = time.monotonic()
            while fenetre and maintenant - fenetre[0][0] > 10:
                fenetre.popleft()
            duree = min(10.0, max(0.5, maintenant - st["debut"]))
            st["instant"] = len(fenetre) / duree
            st["ko_s"] = sum(o for _, o in fenetre) / duree / 1000
            lat = mediane(latences) if latences else 0.0
            st["latence_ms"] = round(lat * 1000)
            reste = st["a_faire"] - st["recues"]
            if st["pause"]:
                cause = ""
            elif relance[0]:
                cause = "relance"
            elif reste < 2 * self.connexions:
                cause = "fin"
            elif self.limiteur.bride:
                cause = "serveur"
            elif st["instant"] >= 0.85 * self.limiteur.max:
                cause = "reglage"
            elif base_latence[0] and lat > 1.8 * base_latence[0]:
                cause = "ralentit"
            elif lat and st["instant"] >= 0.6 * self.connexions / lat:
                cause = "parallelisme"
            else:
                cause = ""
            st["cause"] = cause

        def passe(liste_par_zoom):
            nonlocal en_vol
            for z, liste in liste_par_zoom:
                st["zoom"] = z
                for (x, y, depuis) in liste:
                    while en_vol >= self.connexions * 4 and not self.arret.is_set():
                        vider(True)
                        signaler()
                    if self.arret.is_set() or erreurs_suivies >= 40:
                        return
                    taches.put((z, x, y, depuis))
                    en_vol += 1
                    vider(False)
                    signaler()

        def toutes():
            for z in sorted(self.niveaux):
                planifiees = set()
                for e in self.niveaux[z]:
                    yield z, self._a_faire(con, z, e, planifiees)

        try:
            passe(toutes())
            while en_vol > 0:
                vider(True)
                signaler()

            # Deuxième passe pour les tuiles en échec (erreurs passagères).
            if echecs and not self.arret.is_set() and erreurs_suivies < 40:
                self.journal(f"Nouvel essai de {m.nombre(len(echecs))} tuiles en échec…")
                reprise = {}
                for z, x, y, depuis, _ in echecs:
                    reprise.setdefault(z, []).append((x, y, depuis))
                st["erreurs"] -= len(echecs)
                st["recues"] -= len(echecs)
                echecs.clear()
                relance[0] = True
                passe(sorted(reprise.items()))
                while en_vol > 0:
                    vider(True)
                    signaler()

            if erreurs_suivies >= 40:
                st["message"] = f"Arrêt : le serveur renvoie des erreurs en continu ({dernier_message_erreur})"
                self.journal("✖ " + st["message"])
            con.execute("INSERT OR REPLACE INTO metadata (name, value) VALUES ('mbtf_telecharge_le', ?)",
                        (datetime.datetime.now().astimezone().isoformat(timespec="seconds"),))
            con.execute("COMMIT")
        finally:
            for _ in fils:
                taches.put(None)
            for f in fils:
                f.join(timeout=5)
            try:
                con.execute("COMMIT")
            except sqlite3.Error:
                pass
            try:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            con.close()
            self.client.fermer_tout()
            m.oublier(self.cache)

        for z, x, y, _d, err in echecs[:10]:
            self.journal(f"  ✖ {z}/{x}/{y} : {err}")
        if len(echecs) > 10:
            self.journal(f"  … et {m.nombre(len(echecs) - 10)} autres échecs")
        duree = time.monotonic() - st["debut"]
        self.journal(f"{'Interrompu' if self.arret.is_set() or erreurs_suivies >= 40 else 'Terminé'} en {m.duree_lisible(duree)} : "
                     f"{m.nombre(st['ok'])} téléchargées ({m.taille_lisible(st['octets'])}), "
                     + (f"{m.nombre(st['maj'] + st['inchangees'])} vérifiées dont {m.nombre(st['maj'])} mises à jour, "
                        if st['maj'] or st['inchangees'] else "")
                     + f"{m.nombre(st['absentes'])} absentes du serveur, {m.nombre(st['erreurs'])} en échec")
        if st["recues"] and duree > 0:
            self.journal(f"  Rythme moyen {st['recues'] / duree:.0f} tuiles/s, "
                         f"{st['octets'] / duree / 1000:.0f} Ko/s ; réponse médiane du serveur "
                         f"{mediane([d for v in par_zoom.values() for d in v]) * 1000:.0f} ms")
            self.journal("  Réponse médiane par niveau : " + " · ".join(
                f"Z{z} {mediane(v) * 1000:.0f} ms" for z, v in sorted(par_zoom.items())))
        st["fini"] = True
        st["interrompu"] = self.arret.is_set() or erreurs_suivies >= 40
        signaler(force=True)
        return st

    def _ouvrier(self, taches, resultats):
        while True:
            t = taches.get()
            if t is None:
                break
            z, x, y, depuis = t
            while self.pause.is_set() and not self.arret.is_set():
                time.sleep(0.2)
            if self.arret.is_set():
                resultats.put((z, x, y, depuis, ("erreur", "annulé", None), 0.0))
                continue
            t0 = time.monotonic()
            try:
                self.limiteur.attendre(self.arret)
                t0 = time.monotonic()
                r = recuperer_tuile(self.client, self.source, z, x, y, arret=self.arret, depuis=depuis,
                                    limiteur=self.limiteur)
            except Exception as e:  # sécurité : un fil ne doit jamais mourir
                r = ("erreur", str(e), None)
            resultats.put((z, x, y, depuis, r, time.monotonic() - t0))
        self.client.fermer()


# ---------------------------------------------------------------------------
# Fournisseur de tuiles pour la carte de l'interface (sans serveur HTTP local)
# ---------------------------------------------------------------------------

class Fournisseur:
    """
    Tuiles de la carte : mémoire → cache → réseau.
    - plusieurs fils (TRAVAILLEURS) et une pile « dernier demandé, premier servi » :
      la vue courante passe avant les vues déjà quittées ;
    - une demande annulée par la carte (déplacement rapide) n'est pas téléchargée ;
    - une tuile n'est jamais demandée deux fois en même temps ;
    - les tuiles reçues du réseau sont enregistrées dans le cache (un seul fil d'écriture,
      validations groupées), donc pas retéléchargées à la génération ;
    - débit adaptatif propre à l'aperçu (recule sur 429/503).
    Les rappels sont appelés depuis des fils : à l'interface de repasser dans la boucle GTK.
    """

    TRAVAILLEURS = 8

    def __init__(self, debit=20, rafale=60):
        self.source = None
        self.cle = None
        self.cache = None
        self.memoire = OrderedDict()
        self.absentes = OrderedDict()
        self.verrou = threading.Lock()
        self.cond = threading.Condition(self.verrou)
        self.pile = []
        self.attente = {}
        self.client = ClientHTTP(delai=15, http2=False)  # aperçu : HTTP/1.1, plus réactif
        self.limiteur = Limiteur(debit, rafale)
        self.local = threading.local()
        self.generation = 0
        self.ouvertes = []
        self.couv = {"cache": None, "niveaux": {}, "niveau": 0}
        self.a_ecrire = queue.Queue()
        self.demande_fermeture = threading.Event()
        self.ecrivain_ferme = threading.Event()
        self.stats = {"memoire": 0, "cache": 0, "reseau": 0, "annulees": 0, "ecrites": 0}
        # Pendant un téléchargement : aperçu depuis le cache seulement (pas de requêtes en plus).
        self.hors_ligne = False
        for _ in range(self.TRAVAILLEURS):
            threading.Thread(target=self._travailleur, daemon=True).start()
        threading.Thread(target=self._ecrivain, daemon=True).start()
        from concurrent.futures import ThreadPoolExecutor
        self.pool_couv = ThreadPoolExecutor(2)

    # ----- réglages
    def definir_source(self, source, cache):
        """Retourne la clé de la source (change si l'URL change)."""
        cle = hashlib.sha1(source["url"].encode()).hexdigest()[:10] if source else None
        with self.verrou:
            self.source = dict(source) if source else None
            self.cle = cle
            self.cache = cache
            self.pile.clear()  # les demandes de l'ancienne source sont abandonnées
        return cle

    def definir_couverture(self, cache, niveaux, niveau):
        with self.verrou:
            self.couv = {"cache": cache, "niveaux": {z: [tuple(e) for e in v] for z, v in niveaux.items()},
                         "niveau": int(niveau)}

    # ----- demandes
    def demander(self, cle, z, x, y, rappel, annulee=lambda: False):
        k = (cle, z, x, y)
        with self.verrou:
            if k in self.memoire:
                self.memoire.move_to_end(k)
                d = self.memoire[k]
                self.stats["memoire"] += 1
            elif k in self.absentes:
                d = None
            else:
                if k in self.attente:
                    self.attente[k].append(rappel)
                else:
                    self.attente[k] = [rappel]
                    self.pile.append((k, annulee))
                    self.cond.notify()
                return
        rappel(d)

    def demander_couverture(self, z, x, y, rappel):
        def travail():
            with self.verrou:
                c = dict(self.couv)
            try:
                d = m.png_couverture(self._connexion_cache(c["cache"]), c["niveaux"], c["niveau"], z, x, y)
            except Exception:
                d = None
            rappel(d)
        self.pool_couv.submit(travail)

    def _travailleur(self):
        while True:
            with self.cond:
                while not self.pile:
                    self.cond.wait()
                k, annulee = self.pile.pop()
                source, cache, cle = self.source, self.cache, self.cle
            d = None
            try:
                if k[0] != cle or annulee():
                    with self.verrou:
                        self.stats["annulees"] += 1
                else:
                    d = self._obtenir(source, cache, k)
            except Exception:
                d = None
            with self.verrou:
                rappels = self.attente.pop(k, [])
            for r in rappels:
                try:
                    r(d)
                except Exception:
                    pass

    def _obtenir(self, source, cache, k):
        _cle, z, x, y = k
        if not (source.get("zoom_min", 0) <= z <= source.get("zoom_max", 22)):
            return None
        d = self._depuis_cache(cache, z, x, y)
        if d is not None:
            with self.verrou:
                self.stats["cache"] += 1
        elif self.hors_ligne:
            return None
        else:
            self.limiteur.attendre()
            etat, d, _ = recuperer_tuile(self.client, source, z, x, y, essais=1, limiteur=self.limiteur)
            if etat != "ok":
                if etat == "absente":
                    with self.verrou:
                        self.absentes[k] = True
                        while len(self.absentes) > 5000:
                            self.absentes.popitem(last=False)
                return None
            with self.verrou:
                self.stats["reseau"] += 1
            if cache:
                self.a_ecrire.put((cache, source, z, x, y, d))
        with self.verrou:
            self.memoire[k] = d
            while len(self.memoire) > 1500:
                self.memoire.popitem(last=False)
        return d

    # ----- cache : lecture
    def _connexion_cache(self, chemin):
        if not chemin or not os.path.exists(chemin):
            return None
        cons = getattr(self.local, "cons", None)
        if cons is None or getattr(self.local, "generation", -1) != self.generation:
            cons = self.local.cons = {}
            self.local.generation = self.generation
        if chemin not in cons:
            try:
                url = "file:" + urllib.request.pathname2url(os.path.abspath(chemin)) + "?mode=ro"
                con = sqlite3.connect(url, uri=True, check_same_thread=False)
            except sqlite3.Error:
                return None
            cons[chemin] = con
            with self.verrou:
                self.ouvertes.append(con)
        return cons[chemin]

    def _depuis_cache(self, chemin, z, x, y):
        con = self._connexion_cache(chemin)
        if con is None:
            return None
        try:
            r = con.execute("SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? "
                            "AND tile_row = ?", (z, x, m.tms(y, z))).fetchone()
            return bytes(r[0]) if r and r[0] else None
        except (sqlite3.Error, sqlite3.ProgrammingError):
            return None

    # ----- cache : écriture des tuiles vues
    def _ecrivain(self):
        con, chemin, n, dernier = None, None, 0, time.monotonic()

        def valider():
            nonlocal n, dernier
            if con is not None and n:
                try:
                    con.execute("COMMIT")
                    con.execute("BEGIN")
                except sqlite3.Error:
                    pass
            with self.verrou:
                self.stats["ecrites"] += n
            n, dernier = 0, time.monotonic()

        def fermer():
            nonlocal con, chemin
            valider()
            if con is not None:
                try:
                    con.execute("COMMIT")
                    con.close()
                except sqlite3.Error:
                    pass
            con, chemin = None, None

        while True:
            if self.demande_fermeture.is_set():
                fermer()
                self.demande_fermeture.clear()
                self.ecrivain_ferme.set()
            try:
                cache, source, z, x, y, d = self.a_ecrire.get(timeout=0.5)
            except queue.Empty:
                if n and time.monotonic() - dernier > 1.0:
                    valider()
                continue
            try:
                if cache != chemin:
                    fermer()
                    con, _fmt = ouvrir_cache_ecriture(cache, source)
                    chemin = cache
                    if not m.lire_metadata(con).get("format"):
                        fmt = m.detecter_format(d)
                        if fmt:
                            con.execute("INSERT OR REPLACE INTO metadata VALUES ('format', ?)", (fmt,))
                    con.execute("BEGIN")
                row = m.tms(y, z)
                if con.execute("INSERT OR IGNORE INTO tiles VALUES (?, ?, ?, ?)", (z, x, row, d)).rowcount:
                    con.execute("INSERT OR REPLACE INTO mbtf_dates VALUES (?, ?, ?, ?)",
                                (z, x, row, int(time.time())))
                    con.execute("DELETE FROM mbtf_absentes WHERE zoom_level = ? AND tile_column = ? "
                                "AND tile_row = ?", (z, x, row))
                    n += 1
                if n >= 200 or time.monotonic() - dernier > 1.0:
                    valider()
            except Exception:
                fermer()

    def fermer_caches(self):
        """Ferme lectures et écriture du cache (avant suppression ou compactage d'un fichier)."""
        self.ecrivain_ferme.clear()
        self.demande_fermeture.set()
        self.ecrivain_ferme.wait(3)
        with self.verrou:
            self.generation += 1
            ouvertes, self.ouvertes = self.ouvertes, []
        for con in ouvertes:
            try:
                con.close()
            except Exception:
                pass
