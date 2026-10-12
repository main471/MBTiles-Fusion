# SPDX-License-Identifier: GPL-3.0-or-later
"""
Coordonnées : UTM / MGRS (WGS84), analyse d'une saisie libre, recherche de lieux.
Aucune dépendance externe.
"""

import json
import math
import re
import urllib.parse

A = 6378137.0
F = 1 / 298.257223563
E2 = F * (2 - F)
EP2 = E2 / (1 - E2)
K0 = 0.9996
BANDES = "CDEFGHJKLMNPQRSTUVWX"
COLONNES = ("ABCDEFGH", "JKLMNPQR", "STUVWXYZ")
LIGNES = "ABCDEFGHJKLMNPQRSTUV"


# ---------------------------------------------------------------------------
# UTM
# ---------------------------------------------------------------------------

def zone_utm(lat, lon):
    zone = int((lon + 180) // 6) + 1
    if zone > 60:
        zone = 60
    if 56 <= lat < 64 and 3 <= lon < 12:
        zone = 32
    if 72 <= lat < 84:
        if 0 <= lon < 9:
            zone = 31
        elif 9 <= lon < 21:
            zone = 33
        elif 21 <= lon < 33:
            zone = 35
        elif 33 <= lon < 42:
            zone = 37
    return zone


def _m(phi):
    e4, e6 = E2 * E2, E2 ** 3
    return A * ((1 - E2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * phi
                - (3 * E2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * phi)
                + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * phi)
                - (35 * e6 / 3072) * math.sin(6 * phi))


def vers_utm(lat, lon, zone=None):
    """(zone, hémisphère 'N'/'S', est, nord)."""
    zone = zone or zone_utm(lat, lon)
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    phi, lam = math.radians(lat), math.radians(lon)
    n = A / math.sqrt(1 - E2 * math.sin(phi) ** 2)
    t = math.tan(phi) ** 2
    c = EP2 * math.cos(phi) ** 2
    a = math.cos(phi) * (lam - lon0)
    est = K0 * n * (a + (1 - t + c) * a ** 3 / 6
                    + (5 - 18 * t + t * t + 72 * c - 58 * EP2) * a ** 5 / 120) + 500000
    nord = K0 * (_m(phi) + n * math.tan(phi) * (a * a / 2 + (5 - t + 9 * c + 4 * c * c) * a ** 4 / 24
                                                 + (61 - 58 * t + t * t + 600 * c - 330 * EP2) * a ** 6 / 720))
    if lat < 0:
        nord += 10000000
    return zone, ("N" if lat >= 0 else "S"), est, nord


def depuis_utm(zone, hemisphere, est, nord):
    """(lat, lon) en degrés."""
    x = est - 500000
    y = nord - (10000000 if hemisphere == "S" else 0)
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    m = y / K0
    mu = m / (A * (1 - E2 / 4 - 3 * E2 ** 2 / 64 - 5 * E2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - E2)) / (1 + math.sqrt(1 - E2))
    phi1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
            + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu) + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    n1 = A / math.sqrt(1 - E2 * math.sin(phi1) ** 2)
    t1 = math.tan(phi1) ** 2
    c1 = EP2 * math.cos(phi1) ** 2
    r1 = A * (1 - E2) / (1 - E2 * math.sin(phi1) ** 2) ** 1.5
    d = x / (n1 * K0)
    phi = phi1 - (n1 * math.tan(phi1) / r1) * (
        d * d / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * EP2) * d ** 4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1 * t1 - 252 * EP2 - 3 * c1 * c1) * d ** 6 / 720)
    lam = lon0 + (d - (1 + 2 * t1 + c1) * d ** 3 / 6
                  + (5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * EP2 + 24 * t1 * t1) * d ** 5 / 120) / math.cos(phi1)
    return math.degrees(phi), math.degrees(lam)


# ---------------------------------------------------------------------------
# MGRS
# ---------------------------------------------------------------------------

def bande(lat):
    if lat < -80 or lat > 84:
        return None
    return BANDES[min(19, int((lat + 80) // 8))]


def vers_mgrs(lat, lon, chiffres=5):
    """« 30T XP 12345 67890 » (chiffres = 1 à 5 par axe). None hors des latitudes UTM."""
    b = bande(lat)
    if b is None:
        return None
    zone, _h, est, nord = vers_utm(lat, lon)
    col = COLONNES[(zone - 1) % 3][int(est // 100000) - 1]
    ligne = LIGNES[(int(nord // 100000) + (5 if zone % 2 == 0 else 0)) % 20]
    div = 10 ** (5 - chiffres)
    e = int(est % 100000) // div
    n = int(nord % 100000) // div
    return f"{zone}{b} {col}{ligne} {e:0{chiffres}d} {n:0{chiffres}d}"


_RE_MGRS = re.compile(r"^\s*(\d{1,2})\s*([C-HJ-NP-X])\s*([A-HJ-NP-Z])\s*([A-HJ-NP-V])\s*(\d*)\s*(\d*)\s*$", re.I)


def depuis_mgrs(texte):
    """(lat, lon, précision en m) du centre de la case désignée, ou ValueError."""
    mo = _RE_MGRS.match(texte)
    if not mo:
        raise ValueError("référence MGRS invalide")
    zone = int(mo.group(1))
    b, col, lig = (g.upper() for g in mo.group(2, 3, 4))
    e_txt, n_txt = mo.group(5), mo.group(6)
    if not n_txt:  # chiffres collés : moitié / moitié
        if len(e_txt) % 2:
            raise ValueError("MGRS : nombre de chiffres impair")
        e_txt, n_txt = e_txt[:len(e_txt) // 2], e_txt[len(e_txt) // 2:]
    if len(e_txt) != len(n_txt) or len(e_txt) > 5:
        raise ValueError("MGRS : est et nord doivent avoir le même nombre de chiffres (5 max)")
    if not 1 <= zone <= 60:
        raise ValueError("MGRS : zone hors 1–60")
    jeu = COLONNES[(zone - 1) % 3]
    if col not in jeu:
        raise ValueError(f"MGRS : lettre de colonne « {col} » impossible en zone {zone}")
    prec = 10 ** (5 - len(e_txt))
    e = (int(e_txt) if e_txt else 0) * prec + prec / 2
    n = (int(n_txt) if n_txt else 0) * prec + prec / 2
    est = (jeu.index(col) + 1) * 100000 + e
    idx = (LIGNES.index(lig) - (5 if zone % 2 == 0 else 0)) % 20
    base = idx * 100000 + n
    hemi = "N" if b >= "N" else "S"
    # Choisit le cycle de 2 000 km qui retombe dans la bande demandée.
    meilleur = None
    for k in range(10):
        nord = base + k * 2000000
        lat, lon = depuis_utm(zone, hemi, est, nord)
        if bande(lat) == b:
            return lat, lon, prec
        lat_b = -80 + 8 * BANDES.index(b) + 4
        ecart = abs(lat - lat_b)
        if meilleur is None or ecart < meilleur[0]:
            meilleur = (ecart, lat, lon)
    if meilleur and meilleur[0] < 6:
        return meilleur[1], meilleur[2], prec
    raise ValueError("MGRS : référence hors de sa bande de latitude")


# ---------------------------------------------------------------------------
# Saisie libre : décimal, DMS ou MGRS
# ---------------------------------------------------------------------------

_RE_DMS = re.compile(
    r"(\d{1,3})\s*[°d]\s*(?:(\d{1,2}(?:[.,]\d+)?)\s*['′m]\s*)?(?:(\d{1,2}(?:[.,]\d+)?)\s*(?:\"|″|''|s)\s*)?([NSEWO])",
    re.I)


def _nombre(t):
    return float(t.replace(",", "."))


def analyser_saisie(texte):
    """
    (lat, lon, précision en m) si le texte est une coordonnée, sinon None.
    Formats : « 43.4832, -1.4712 », « 43,4832 -1,4712 », « 43°28'59"N 1°28'16"W »,
    « 30T XP 12345 67890 », « 30TXP1234567890 ».
    """
    t = texte.strip()
    if not t:
        return None
    try:
        return depuis_mgrs(t)
    except ValueError:
        pass
    dms = _RE_DMS.findall(t)
    if len(dms) == 2:
        vals = {}
        for d, mi, s, h in dms:
            v = int(d) + (_nombre(mi) if mi else 0) / 60 + (_nombre(s) if s else 0) / 3600
            h = h.upper()
            if h in "SWO":
                v = -v
            vals["lat" if h in "NS" else "lon"] = v
        if len(vals) == 2:
            return vals["lat"], vals["lon"], 30
    # Décimal : deux nombres, séparés par « , » « ; » ou espace (virgule décimale acceptée).
    mo = re.match(r"^\s*(-?\d+(?:[.,]\d+)?)\s*([NS])?\s*[,; ]\s*(-?\d+(?:[.,]\d+)?)\s*([EWO])?\s*$", t, re.I)
    if not mo:
        mo = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*([NS])?\s*,\s*(-?\d+(?:\.\d+)?)\s*([EWO])?\s*$", t, re.I)
    if mo:
        lat, lon = _nombre(mo.group(1)), _nombre(mo.group(3))
        if mo.group(2) and mo.group(2).upper() == "S":
            lat = -abs(lat)
        if mo.group(4) and mo.group(4).upper() in "WO":
            lon = -abs(lon)
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon, 10
    return None


def texte_position(lat, lon):
    """« 30T XP 12345 67890 · 43,48321 N 1,47012 O »."""
    mgrs = vers_mgrs(lat, lon)
    dec = (f"{abs(lat):.5f} {'N' if lat >= 0 else 'S'} {abs(lon):.5f} {'E' if lon >= 0 else 'O'}"
           .replace(".", ","))
    return f"{mgrs} · {dec}" if mgrs else dec


# ---------------------------------------------------------------------------
# Recherche de lieux (en ligne)
# ---------------------------------------------------------------------------

SERVICES = {"nominatim": "OpenStreetMap (Nominatim)", "ign": "IGN Géoplateforme (France)"}


def rechercher_lieu(client, entetes, texte, service="nominatim"):
    """Liste de {nom, lat, lon, emprise (O,S,E,N) | None}."""
    q = urllib.parse.quote(texte)
    if service == "ign":
        url = f"https://data.geopf.fr/geocodage/search?q={q}&limit=8"
    else:
        url = f"https://nominatim.openstreetmap.org/search?q={q}&format=jsonv2&limit=8"
    statut, _h, corps = client.get(url, dict(entetes, Accept="application/json"))
    if statut != 200:
        raise ValueError(f"service de recherche : HTTP {statut}")
    d = json.loads(corps.decode("utf-8"))
    res = []
    if service == "ign":
        for f in d.get("features", []):
            lon, lat = f["geometry"]["coordinates"][:2]
            p = f.get("properties", {})
            res.append({"nom": p.get("label") or p.get("name") or "?", "lat": lat, "lon": lon,
                        "emprise": None, "type": p.get("type", "")})
    else:
        for r in d:
            emp = None
            bb = r.get("boundingbox")
            if bb and len(bb) == 4:
                s, n, o, e = (float(v) for v in bb)
                emp = (o, s, e, n)
            res.append({"nom": r.get("display_name", "?"), "lat": float(r["lat"]), "lon": float(r["lon"]),
                        "emprise": emp, "type": r.get("type", "")})
    return res
