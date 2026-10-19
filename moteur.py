# SPDX-License-Identifier: GPL-3.0-or-later
"""
Moteur MBTiles — aucune dépendance graphique.

- géométrie des tuiles (Web Mercator, schéma XYZ ; MBTiles stocke en TMS) ;
- analyse d'un fichier : complétude par niveau, tuiles invalides ;
- estimation exacte de la carte finale (doublons et tuiles invalides exclus) ;
- fusion base + zones, avec traçabilité dans les métadonnées.
"""

import datetime
import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

VERSION = "5.5"
LAT_MAX = 85.0511287798

# Pages SQLite de la carte finale (doit rester cohérent avec generer()).
TAILLE_PAGE = 4096


# ---------------------------------------------------------------------------
# Formatage
# ---------------------------------------------------------------------------

def dossier_config():
    """Réglages : MBTF_CONFIG (version portable), sinon %APPDATA% sous Windows, ~/.config ailleurs."""
    d = os.environ.get("MBTF_CONFIG")
    if d:
        return Path(d)
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home()) / "MBTiles Fusion"
    return Path.home() / ".config" / "mbtiles-fusion"


def remplacer(source, cible, essais=10):
    """os.replace tolérant : sous Windows, un fichier ouvert ailleurs ne peut pas être remplacé."""
    for i in range(essais):
        try:
            os.replace(source, cible)
            return
        except PermissionError:
            if i == essais - 1:
                raise PermissionError(f"{Path(cible).name} est ouvert par un autre programme : "
                                      "ferme-le puis relance la génération.")
            time.sleep(0.5)


DATE_A_VERIFIER = 1  # date d'une tuile en cache dont l'origine n'est pas sûre


def ecrire_prive(chemin, texte):
    """
    Écrit un fichier de réglages lisible par son seul propriétaire (0600), de façon atomique :
    il peut contenir des clés d'API (URL des sources).
    """
    chemin = Path(chemin)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(chemin.parent, 0o700)
    except OSError:
        pass
    tmp = chemin.with_name(chemin.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(texte)
    try:
        os.chmod(tmp, 0o600)  # si le fichier temporaire existait déjà avec d'autres droits
    except OSError:
        pass
    remplacer(tmp, chemin)


def securiser(con):
    """
    Durcit une connexion SQLite qui peut lire des fichiers venus d'ailleurs (cartes reçues) :
    les vues et déclencheurs d'un fichier ne peuvent pas appeler de fonctions à effet de bord,
    et le schéma ne peut pas être modifié par des moyens détournés.
    """
    try:
        con.execute("PRAGMA trusted_schema = OFF")
    except sqlite3.Error:
        pass
    defensif = getattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE", None)
    if defensif is not None and hasattr(con, "setconfig"):
        try:
            con.setconfig(defensif, True)
        except sqlite3.Error:
            pass
    return con


def connecter(chemin, **kw):
    return securiser(sqlite3.connect(chemin, **kw))


def taille_lisible(octets):
    for unite in ("o", "Ko", "Mo", "Go"):
        if octets < 1024 or unite == "Go":
            return f"{octets:.0f} {unite}" if unite == "o" else f"{octets:.1f} {unite}".replace(".", ",")
        octets /= 1024


def nombre(n):
    return f"{n:,}".replace(",", " ")


def pourcentage(p):
    if 0 < p < 1:
        return "< 1 %"
    if 99 < p < 100:
        return "> 99 %"
    return f"{round(p)} %"


def duree_lisible(s):
    s = int(s)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    return f"{s // 3600} h {s % 3600 // 60:02d}"


# ---------------------------------------------------------------------------
# Géométrie des tuiles
# ---------------------------------------------------------------------------

def lonlat_vers_xy(lon, lat, z):
    """Coordonnées tuile (réelles) en schéma XYZ."""
    n = 2 ** z
    lat = max(-LAT_MAX, min(LAT_MAX, lat))
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def xy_vers_lonlat(x, y, z):
    n = 2 ** z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lon, lat


def plage(emprise, z, tolerance=1e-9):
    """
    Tuiles XYZ (x0, x1, y0, y1), bornes incluses, couvrant l'emprise (O, S, E, N).
    `tolerance` (en fraction de tuile) ignore un débord infime sur la tuile voisine.
    """
    o, s, e, n = emprise
    cote = 2 ** z
    fx0, fy0 = lonlat_vers_xy(o, n, z)
    fx1, fy1 = lonlat_vers_xy(e, s, z)
    tx = min(tolerance, (fx1 - fx0) / 4)
    ty = min(tolerance, (fy1 - fy0) / 4)
    x0 = math.floor(fx0 + tx)
    y0 = math.floor(fy0 + ty)
    x1 = math.ceil(fx1 - tx) - 1
    y1 = math.ceil(fy1 - ty) - 1
    x0, y0 = max(0, min(cote - 1, x0)), max(0, min(cote - 1, y0))
    x1, y1 = max(x0, min(cote - 1, x1)), max(y0, min(cote - 1, y1))
    return x0, x1, y0, y1


def nb_tuiles_niveau(emprise, z, tolerance=1e-9):
    x0, x1, y0, y1 = plage(emprise, z, tolerance)
    return (x1 - x0 + 1) * (y1 - y0 + 1)


def nb_tuiles(emprise, zmin, zmax):
    return sum(nb_tuiles_niveau(emprise, z) for z in range(zmin, zmax + 1))


def emprise_de_plage(x0, x1, y0, y1, z):
    o, n = xy_vers_lonlat(x0, y0, z)
    e, s = xy_vers_lonlat(x1 + 1, y1 + 1, z)
    return (o, s, e, n)


def tms(y, z):
    """Conversion de ligne XYZ <-> TMS (symétrique)."""
    return 2 ** z - 1 - y


def emprise_valide(e):
    try:
        o, s, est, n = (float(v) for v in e)
    except (TypeError, ValueError):
        return False
    return -180 <= o < est <= 180 and -90 <= s < n <= 90


def union_emprises(emprises):
    emprises = [e for e in emprises if e]
    if not emprises:
        return None
    return (min(e[0] for e in emprises), min(e[1] for e in emprises),
            max(e[2] for e in emprises), max(e[3] for e in emprises))


def dimensions_km(emprise):
    o, s, e, n = emprise
    lat = math.radians((s + n) / 2)
    return (e - o) * 111.32 * math.cos(lat), (n - s) * 110.57


# ---------------------------------------------------------------------------
# Validité des tuiles
# ---------------------------------------------------------------------------

def detecter_format(donnees):
    if not donnees:
        return None
    d = bytes(donnees[:12])
    if d.startswith(b"\x89PNG"):
        return "png"
    if d.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if d.startswith(b"RIFF") and d[8:12] == b"WEBP":
        return "webp"
    if d.startswith(b"GIF8"):
        return "gif"
    return None


FORMATS_VECTEUR = ("pbf", "mvt")


def est_vecteur(fmt):
    return (fmt or "").lower() in FORMATS_VECTEUR


def sql_valide(fmt, col="tile_data"):
    """Expression SQL vraie pour une tuile exploitable (jamais NULL)."""
    b = f"CAST({col} AS BLOB)"
    if est_vecteur(fmt):
        # Tuile vectorielle : non vide et pas une page HTML / JSON d'erreur.
        return (f"({col} IS NOT NULL AND length({b}) > 0 "
                f"AND substr({b},1,1) NOT IN (x'3C', x'7B'))")
    return (f"({col} IS NOT NULL AND length({b}) > 0 AND ("
            f"substr({b},1,4) = x'89504E47' OR substr({b},1,3) = x'FFD8FF' "
            f"OR (substr({b},1,4) = x'52494646' AND substr({b},9,4) = x'57454250') "
            f"OR substr({b},1,4) = x'47494638'))")


# ---------------------------------------------------------------------------
# Lecture d'un MBTiles
# ---------------------------------------------------------------------------

def ouvrir_lecture(chemin):
    url = "file:" + urllib.request.pathname2url(os.path.abspath(chemin)) + "?mode=ro"
    return connecter(url, uri=True)


def table_existe(con, nom, schema="main"):
    return con.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type IN ('table','view') AND name = ?",
                       (nom,)).fetchone() is not None


def lire_metadata(con):
    try:
        return {str(k): ("" if v is None else str(v))
                for k, v in con.execute("SELECT name, value FROM metadata")}
    except sqlite3.Error:
        return {}


def _cle_fichier(chemin):
    st = os.stat(chemin)
    return (os.path.abspath(chemin), st.st_size, st.st_mtime_ns)


_cache_infos = {}
_cache_analyses = {}
_verrou_cache = threading.Lock()


def infos_mbtiles(chemin):
    """Zoom min/max, nombre de tuiles (total et par niveau), format, taille, métadonnées."""
    cle = _cle_fichier(chemin)
    with _verrou_cache:
        if cle in _cache_infos:
            return _cache_infos[cle]
    con = ouvrir_lecture(chemin)
    try:
        par_zoom = dict(con.execute(
            "SELECT zoom_level, COUNT(*) FROM tiles GROUP BY zoom_level").fetchall())
        meta = lire_metadata(con)
        fmt = (meta.get("format") or "").lower()
        if not fmt and par_zoom:
            ligne = con.execute("SELECT tile_data FROM tiles LIMIT 1").fetchone()
            fmt = detecter_format(ligne[0]) if ligne else None
        fmt = {"jpeg": "jpg"}.get(fmt, fmt) or "?"
    finally:
        con.close()
    if not par_zoom:
        raise ValueError("le fichier ne contient aucune tuile")
    info = {
        "zoom_min": min(par_zoom),
        "zoom_max": max(par_zoom),
        "tuiles": sum(par_zoom.values()),
        "par_zoom": par_zoom,
        "format": fmt,
        "taille": cle[1],
        "meta": meta,
    }
    with _verrou_cache:
        _cache_infos[cle] = info
    return info


def emprise_mbtiles(con, meta, zoom_max):
    """Emprise (O, S, E, N) : métadonnées « bounds » si plausibles, sinon calculée sur les tuiles."""
    try:
        b = tuple(float(v) for v in meta.get("bounds", "").split(","))
        # Une emprise « monde entier » est presque toujours une valeur par défaut.
        if len(b) == 4 and emprise_valide(b) and b[2] - b[0] < 359.9:
            return b, "métadonnées"
    except ValueError:
        pass
    x0, x1, r0, r1 = con.execute(
        "SELECT MIN(tile_column), MAX(tile_column), MIN(tile_row), MAX(tile_row) "
        "FROM tiles WHERE zoom_level = ?", (zoom_max,)).fetchone()
    return emprise_de_plage(x0, x1, tms(r1, zoom_max), tms(r0, zoom_max), zoom_max), "tuiles"


def emprise_fichier(chemin):
    info = infos_mbtiles(chemin)
    con = ouvrir_lecture(chemin)
    try:
        return emprise_mbtiles(con, info["meta"], info["zoom_max"])[0]
    finally:
        con.close()


def _zooms_declares(info):
    zmin, zmax = info["zoom_min"], info["zoom_max"]
    try:
        zmin = min(zmin, int(info["meta"]["minzoom"]))
    except (KeyError, ValueError):
        pass
    try:
        zmax = max(zmax, int(info["meta"]["maxzoom"]))
    except (KeyError, ValueError):
        pass
    return max(0, zmin), min(24, zmax)


def analyser(chemin, progression=lambda f: None, annule=None):
    """
    Analyse complète d'un MBTiles (résultat mis en cache tant que le fichier ne change pas).

    - complétude : pour chaque niveau, tuiles attendues d'après l'emprise,
      présentes, connues absentes du serveur (table mbtf_absentes), manquantes ;
    - tuiles invalides : vides ou non images (page d'erreur enregistrée à la place).
    """
    cle = _cle_fichier(chemin)
    with _verrou_cache:
        if cle in _cache_analyses:
            return _cache_analyses[cle]
    info = infos_mbtiles(chemin)
    zmin, zmax = _zooms_declares(info)
    con = ouvrir_lecture(chemin)
    try:
        emprise, origine = emprise_mbtiles(con, info["meta"], info["zoom_max"])
        absentes_dispo = table_existe(con, "mbtf_absentes")
        valide = sql_valide(info["format"])

        completude = []
        for z in range(zmin, zmax + 1):
            x0, x1, y0, y1 = plage(emprise, z, tolerance=0.01)
            r0, r1 = tms(y1, z), tms(y0, z)
            attendues = (x1 - x0 + 1) * (y1 - y0 + 1)
            presentes = con.execute(
                "SELECT COUNT(*) FROM tiles WHERE zoom_level = ? AND tile_column BETWEEN ? AND ? "
                "AND tile_row BETWEEN ? AND ?", (z, x0, x1, r0, r1)).fetchone()[0]
            absentes = 0
            if absentes_dispo:
                absentes = con.execute(
                    "SELECT COUNT(*) FROM mbtf_absentes WHERE zoom_level = ? AND tile_column BETWEEN ? "
                    "AND ? AND tile_row BETWEEN ? AND ?", (z, x0, x1, r0, r1)).fetchone()[0]
            completude.append({"z": z, "attendues": attendues, "presentes": presentes,
                               "absentes": absentes,
                               "manquantes": max(0, attendues - presentes - absentes)})

        # Tuiles invalides : un passage par niveau (progression), filtrage en SQL.
        invalides = {"vides": 0, "non_images": 0, "par_zoom": {}, "cles": []}
        niveaux = sorted(info["par_zoom"])
        total = info["tuiles"] or 1
        faites = 0
        for z in niveaux:
            if annule is not None and annule.is_set():
                raise InterruptedError
            for col, row, vide in con.execute(
                    f"SELECT tile_column, tile_row, (tile_data IS NULL OR length(CAST(tile_data AS BLOB)) = 0) "
                    f"FROM tiles WHERE zoom_level = ? AND NOT {valide}", (z,)):
                invalides["vides" if vide else "non_images"] += 1
                invalides["par_zoom"][z] = invalides["par_zoom"].get(z, 0) + 1
                invalides["cles"].append((z, col, row))
            faites += info["par_zoom"][z]
            progression(faites / total)
    finally:
        con.close()

    resultat = {"info": info, "emprise": emprise, "origine_emprise": origine,
                "completude": completude, "invalides": invalides}
    with _verrou_cache:
        _cache_analyses[cle] = resultat
    return resultat


def analyse_en_cache(chemin):
    try:
        cle = _cle_fichier(chemin)
    except OSError:
        return None
    with _verrou_cache:
        return _cache_analyses.get(cle)


def oublier(chemin):
    """Invalide le cache d'un fichier (après un téléchargement, par exemple)."""
    p = os.path.abspath(chemin)
    with _verrou_cache:
        for d in (_cache_infos, _cache_analyses):
            for cle in [c for c in d if c[0] == p]:
                del d[cle]


def niveaux_incomplets(analyse, zoom_min_utile=None):
    return [c for c in analyse["completude"]
            if c["manquantes"] > 0 and (zoom_min_utile is None or c["z"] >= zoom_min_utile)]


def texte_completude(analyse, zoom_min_utile=None):
    """« Complète » ou « Incomplète : 3 % manquants en Z18 »."""
    inc = niveaux_incomplets(analyse, zoom_min_utile)
    if not inc:
        return "Complète"
    inc.sort(key=lambda c: -c["z"])
    morceaux = []
    for i, c in enumerate(inc[:2]):
        p = pourcentage(100 * c["manquantes"] / c["attendues"])
        morceaux.append(f"{p} manquants en Z{c['z']}" if i == 0 else f"{p} en Z{c['z']}")
    texte = "Incomplète : " + ", ".join(morceaux)
    if len(inc) > 2:
        texte += f" et {len(inc) - 2} autre{'s' if len(inc) > 3 else ''} niveau{'x' if len(inc) > 3 else ''}"
    return texte


def texte_invalides(analyse, zoom_min_utile=None):
    inv = analyse["invalides"]
    pz = {z: n for z, n in inv["par_zoom"].items() if zoom_min_utile is None or z >= zoom_min_utile}
    n = sum(pz.values())
    if not n:
        return ""
    detail = ", ".join(f"Z{z} : {nombre(v)}" for z, v in sorted(pz.items(), reverse=True)[:3])
    types = []
    if inv["vides"]:
        types.append("vides")
    if inv["non_images"]:
        types.append("non images")
    return f"{nombre(n)} tuile{'s' if n > 1 else ''} invalide{'s' if n > 1 else ''} ({' / '.join(types)}) — {detail}"


def rapport_texte(chemin, analyse, zoom_min_utile=None):
    """Rapport détaillé pour le journal."""
    lignes = [f"Analyse : {Path(chemin).name} — emprise d'après les {analyse['origine_emprise']}"]
    for c in analyse["completude"]:
        if zoom_min_utile is not None and c["z"] < zoom_min_utile:
            continue
        etat = "OK" if not c["manquantes"] else f"{nombre(c['manquantes'])} manquantes"
        abs_ = f", {nombre(c['absentes'])} absentes du serveur" if c["absentes"] else ""
        lignes.append(f"  Z{c['z']:<2} {nombre(c['presentes'])}/{nombre(c['attendues'])} — {etat}{abs_}")
    inv = analyse["invalides"]
    exemples = [k for k in inv["cles"] if zoom_min_utile is None or k[0] >= zoom_min_utile][:5]
    if exemples:
        lignes.append("  Tuiles invalides, exemples (z/x/y) : "
                      + ", ".join(f"{z}/{x}/{tms(r, z)}" for z, x, r in exemples))
    return "\n".join(lignes)


# ---------------------------------------------------------------------------
# Estimation de la carte finale
# ---------------------------------------------------------------------------

def _sql_octets_disque(col="l"):
    """Octets occupés sur disque par une ligne de tuile (feuille + débordement), page 4096."""
    u = TAILLE_PAGE
    x = u - 35                      # charge utile maximale stockée dans la feuille
    m = (u - 12) * 32 // 255 - 23   # charge minimale dans la feuille si débordement
    p = f"({col} + 15)"             # en-tête d'enregistrement + 3 entiers
    k = f"({m} + ({p} - {m}) % {u - 4})"
    local = f"(CASE WHEN {p} <= {x} THEN {p} WHEN {k} <= {x} THEN {k} ELSE {m} END)"
    debord = f"(CASE WHEN {p} <= {x} THEN 0 ELSE ({p} - {local} + {u - 5}) / {u - 4} END)"
    # Cellule de feuille : charge locale + taille, rowid, pointeur de cellule
    # (+ pointeur de débordement). Les pages se remplissent séquentiellement :
    # une page contient un nombre entier de cellules.
    cellule = f"({local} + 11 + CASE WHEN {p} <= {x} THEN 0 ELSE 4 END)"
    return f"({debord} * {u} + {u}.0 / ({u - 8} / {cellule}))"


def estimer(base, zones, progression=lambda f: None, annule=None):
    """
    Nombre de tuiles et poids de la carte finale, sans l'écrire.
    Mêmes règles que generer() : base complète, zones au-dessus du zoom max de la base,
    la dernière zone l'emporte, tuiles invalides exclues.
    """
    sources = [base] + list(zones)
    analyses = [analyser(s) for s in sources]
    seuil = analyses[0]["info"]["zoom_max"]
    dossier = tempfile.mkdtemp(prefix="mbtf-estim-")
    con = connecter(os.path.join(dossier, "k.db"), isolation_level=None)
    try:
        con.execute("PRAGMA journal_mode = OFF")
        con.execute("PRAGMA synchronous = OFF")
        con.execute("CREATE TABLE k (z INTEGER, x INTEGER, y INTEGER, l INTEGER, "
                    "PRIMARY KEY (z, x, y)) WITHOUT ROWID")
        con.execute("CREATE TABLE inv (z INTEGER, x INTEGER, y INTEGER, "
                    "PRIMARY KEY (z, x, y)) WITHOUT ROWID")
        for i, (src, an) in enumerate(zip(sources, analyses)):
            if annule is not None and annule.is_set():
                raise InterruptedError
            con.execute("DELETE FROM inv")
            con.executemany("INSERT OR IGNORE INTO inv VALUES (?, ?, ?)", an["invalides"]["cles"])
            con.execute("ATTACH DATABASE ? AS src", (os.path.abspath(src),))
            filtre = "" if i == 0 else f"AND t.zoom_level > {int(seuil)}"
            con.execute("BEGIN")
            con.execute(
                "INSERT OR REPLACE INTO k SELECT t.zoom_level, t.tile_column, t.tile_row, "
                "length(CAST(t.tile_data AS BLOB)) FROM src.tiles t "
                f"WHERE 1 {filtre} AND NOT EXISTS (SELECT 1 FROM inv WHERE inv.z = t.zoom_level "
                "AND inv.x = t.tile_column AND inv.y = t.tile_row)")
            con.execute("COMMIT")
            con.execute("DETACH DATABASE src")
            progression((i + 1) / len(sources))
        n, donnees, disque = con.execute(
            f"SELECT COUNT(*), COALESCE(SUM(l), 0), COALESCE(SUM({_sql_octets_disque()}), 0) FROM k"
        ).fetchone()
        par_zoom = dict(con.execute("SELECT z, COUNT(*) FROM k GROUP BY z").fetchall())
    finally:
        con.close()
        shutil.rmtree(dossier, ignore_errors=True)
    # Index unique (zoom, colonne, ligne) + métadonnées + pages de structure.
    poids = int(disque + n * 24 + 64 * 1024)
    return {"tuiles": n, "octets_tuiles": donnees, "poids": poids, "par_zoom": par_zoom,
            "zoom_min": min(par_zoom) if par_zoom else None,
            "zoom_max": max(par_zoom) if par_zoom else None}


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------

def generer(base, zones, sortie, journal=print, progression=lambda f: None):
    """
    Construit `sortie` à partir de `base` puis des `zones` (dans l'ordre).
    - Les zones n'ajoutent que les niveaux SUPÉRIEURS au zoom max de la base :
      la base n'est jamais écrasée.
    - En cas de chevauchement entre zones, la dernière de la liste l'emporte.
    - Les tuiles invalides (vides, pages d'erreur) ne sont jamais copiées.
    - Métadonnées : date de génération, liste des zones, emprise recalculée.
    - Écriture dans un fichier temporaire, renommé seulement à la fin :
      une carte existante n'est jamais abîmée en cas d'erreur.
    """
    base = os.path.abspath(base)
    sortie = os.path.abspath(sortie)
    zones = [os.path.abspath(z) for z in zones]

    if sortie == base or sortie in zones:
        raise ValueError("le fichier de sortie doit être différent de la base et des zones")

    info_base = infos_mbtiles(base)
    seuil = info_base["zoom_max"]
    valide = sql_valide(info_base["format"])

    # Espace disque : il faut au moins la somme des sources + 10 %
    besoin = int((os.path.getsize(base) + sum(os.path.getsize(z) for z in zones)) * 1.1)
    libre = shutil.disk_usage(os.path.dirname(sortie)).free
    if libre < besoin:
        raise ValueError(
            f"espace disque insuffisant : {taille_lisible(besoin)} nécessaires, "
            f"{taille_lisible(libre)} disponibles"
        )

    tmp = sortie + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)

    emprises = []
    trace_zones = []
    con = connecter(tmp, isolation_level=None)
    try:
        con.execute("PRAGMA journal_mode = OFF")
        con.execute("PRAGMA synchronous = OFF")
        con.execute(f"PRAGMA page_size = {TAILLE_PAGE}")
        con.executescript(
            """
            CREATE TABLE metadata (name TEXT, value TEXT);
            CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER,
                                tile_row INTEGER, tile_data BLOB);
            CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);
            CREATE UNIQUE INDEX name ON metadata (name);
            """
        )

        etapes = 1 + len(zones)

        # 1) Base
        journal(f"Base : {Path(base).name} (Z{info_base['zoom_min']}–Z{seuil}, "
                f"{nombre(info_base['tuiles'])} tuiles)")
        con.execute("ATTACH DATABASE ? AS src", (base,))
        con.execute("BEGIN")
        try:
            con.execute("INSERT OR IGNORE INTO metadata SELECT name, value FROM src.metadata")
        except sqlite3.Error:
            pass
        cur = con.execute(
            "INSERT OR REPLACE INTO tiles "
            f"SELECT zoom_level, tile_column, tile_row, tile_data FROM src.tiles WHERE {valide}"
        )
        ignorees = info_base["tuiles"] - cur.rowcount
        con.execute("COMMIT")
        emprises.append(_emprise_attachee(con, info_base))
        con.execute("DETACH DATABASE src")
        if ignorees > 0:
            journal(f"  ⚠ {nombre(ignorees)} tuiles invalides de la base ignorées")
        progression(1 / etapes)

        # 2) Zones
        for i, zone in enumerate(zones, start=1):
            info_z = infos_mbtiles(zone)
            con.execute("ATTACH DATABASE ? AS src", (zone,))
            candidates = con.execute(
                "SELECT COUNT(*) FROM src.tiles WHERE zoom_level > ?", (seuil,)).fetchone()[0]
            con.execute("BEGIN")
            cur = con.execute(
                "INSERT OR REPLACE INTO tiles "
                "SELECT zoom_level, tile_column, tile_row, tile_data "
                f"FROM src.tiles WHERE zoom_level > ? AND {valide}",
                (seuil,),
            )
            nb = cur.rowcount
            con.execute("COMMIT")
            emprise_z = _emprise_attachee(con, info_z)
            con.execute("DETACH DATABASE src")
            if nb == 0:
                journal(f"  ⚠ {Path(zone).name} : aucune tuile valide au-dessus de Z{seuil}, ignorée")
            else:
                emprises.append(emprise_z)
                journal(f"Zone : {Path(zone).name} → {nombre(nb)} tuiles ajoutées")
                if candidates > nb:
                    journal(f"  ⚠ {nombre(candidates - nb)} tuiles invalides ignorées")
                trace_zones.append({
                    "fichier": Path(zone).name,
                    "zooms": f"Z{max(seuil + 1, info_z['zoom_min'])}-Z{info_z['zoom_max']}",
                    "tuiles": nb,
                    "emprise": [round(v, 6) for v in emprise_z],
                })
            progression((1 + i) / etapes)

        # 3) Métadonnées : zooms, emprise recalculée, traçabilité
        zmin, zmax = con.execute(
            "SELECT MIN(zoom_level), MAX(zoom_level) FROM tiles"
        ).fetchone()
        if zmin is None:
            raise ValueError("aucune tuile valide à écrire")
        o, s, e, n = union_emprises(emprises)
        o, s = math.floor(o * 1e6) / 1e6, math.floor(s * 1e6) / 1e6
        e, n = math.ceil(e * 1e6) / 1e6, math.ceil(n * 1e6) / 1e6
        maintenant = datetime.datetime.now().astimezone()
        noms = ", ".join(z["fichier"] for z in trace_zones) or "aucune"
        con.execute("DELETE FROM metadata WHERE name LIKE 'mbtf\\_%' ESCAPE '\\'")
        valeurs = {
            "minzoom": str(zmin),
            "maxzoom": str(zmax),
            "bounds": f"{o},{s},{e},{n}",
            "center": f"{(o + e) / 2:.6f},{(s + n) / 2:.6f},{min(zmax, seuil)}",
            "description": (f"Générée le {maintenant:%d/%m/%Y à %H:%M} par MBTiles Fusion {VERSION}. "
                            f"Base : {Path(base).name}. Zones : {noms}."),
            "mbtf_genere_le": maintenant.isoformat(timespec="seconds"),
            "mbtf_version": VERSION,
            "mbtf_base": Path(base).name,
            "mbtf_zones": json.dumps(trace_zones, ensure_ascii=False),
        }
        for cle, val in valeurs.items():
            con.execute("INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)", (cle, val))
        if not con.execute("SELECT 1 FROM metadata WHERE name = 'name'").fetchone():
            con.execute("INSERT INTO metadata VALUES ('name', ?)", (Path(sortie).stem,))
        con.execute("ANALYZE")
    finally:
        con.close()

    remplacer(tmp, sortie)
    oublier(sortie)
    journal(f"Carte générée : {Path(sortie).name} — Z{zmin}–Z{zmax}, "
            f"{taille_lisible(os.path.getsize(sortie))}")
    return os.path.getsize(sortie)


def lire_metadata_schema(con, schema):
    try:
        return {str(k): str(v) for k, v in con.execute(f"SELECT name, value FROM {schema}.metadata")}
    except sqlite3.Error:
        return {}


def _emprise_attachee(con, info):
    """Emprise de la base attachée sous le nom « src »."""
    meta = lire_metadata_schema(con, "src")
    try:
        b = tuple(float(v) for v in meta.get("bounds", "").split(","))
        if len(b) == 4 and emprise_valide(b) and b[2] - b[0] < 359.9:
            return b
    except ValueError:
        pass
    z = info["zoom_max"]
    x0, x1, r0, r1 = con.execute(
        "SELECT MIN(tile_column), MAX(tile_column), MIN(tile_row), MAX(tile_row) "
        "FROM src.tiles WHERE zoom_level = ?", (z,)).fetchone()
    return emprise_de_plage(x0, x1, tms(r1, z), tms(r0, z), z)


# ---------------------------------------------------------------------------
# Projet v3 : base régionale + zones de zoom, tuiles servies par un cache local
#
# projet = {"version": 3, "nom": str, "source": {…},
#           "base": {"emprise": [O, S, E, N], "zmin": int, "zmax": int} | None,
#           "zones": [{"id", "nom", "emprise", "zmax", "couleur"}],
#           "externes": [chemins .mbtiles], "sortie": chemin}
# Le cache est un MBTiles par source (tuiles + mbtf_absentes), partagé entre cartes.
# ---------------------------------------------------------------------------

def octets_disque(longueur):
    """Version Python de _sql_octets_disque (même modèle de pages)."""
    u, x = TAILLE_PAGE, TAILLE_PAGE - 35
    m_ = (u - 12) * 32 // 255 - 23
    p = longueur + 15
    if p <= x:
        local, debord, cell = p, 0, p + 11
    else:
        k = m_ + (p - m_) % (u - 4)
        local = k if k <= x else m_
        debord = (p - local + u - 5) // (u - 4)
        cell = local + 15
    return debord * u + u / ((u - 8) // cell)


def niveaux_projet(projet):
    """{z: [emprise, …]} : base de zmin à zmax, chaque zone de zmax_base + 1 à son zmax."""
    base = projet.get("base")
    if not base:
        return {}
    niveaux = {}
    for z in range(base["zmin"], base["zmax"] + 1):
        niveaux.setdefault(z, []).append(tuple(base["emprise"]))
    for zone in projet.get("zones", []):
        for z in range(base["zmax"] + 1, zone["zmax"] + 1):
            niveaux.setdefault(z, []).append(tuple(zone["emprise"]))
    return niveaux


def _rects_tms(emprises, z):
    """Rectangles de tuiles (x0, x1, r0, r1), lignes en TMS comme dans MBTiles."""
    res = []
    for e in emprises:
        x0, x1, y0, y1 = plage(e, z)
        res.append((x0, x1, tms(y1, z), tms(y0, z)))
    return res


def _sans_doublon(rects):
    """
    Pour chaque rectangle : (rect, clause SQL, paramètres) excluant les rectangles
    précédents qui le chevauchent. Chaque tuile n'est ainsi comptée qu'une fois,
    tout en profitant de l'index (zoom, colonne, ligne).
    """
    for i, r in enumerate(rects):
        clauses, params = [], []
        for p in rects[:i]:
            if p[0] <= r[1] and r[0] <= p[1] and p[2] <= r[3] and r[2] <= p[3]:
                clauses.append("NOT (tile_column BETWEEN ? AND ? AND tile_row BETWEEN ? AND ?)")
                params += list(p)
        yield r, "".join(" AND " + c for c in clauses), params


def nb_union(rects):
    """Nombre de tuiles couvertes par l'union de rectangles (compression de coordonnées)."""
    if not rects:
        return 0
    xs = sorted({r[0] for r in rects} | {r[1] + 1 for r in rects})
    ys = sorted({r[2] for r in rects} | {r[3] + 1 for r in rects})
    total = 0
    for i in range(len(xs) - 1):
        for j in range(len(ys) - 1):
            if any(r[0] <= xs[i] and xs[i + 1] - 1 <= r[1] and r[2] <= ys[j] and ys[j + 1] - 1 <= r[3]
                   for r in rects):
                total += (xs[i + 1] - xs[i]) * (ys[j + 1] - ys[j])
    return total


def couverture(cache, niveaux, limite_date=None):
    """
    Par niveau : tuiles attendues (union des rectangles), présentes dans le cache,
    connues absentes du serveur, périmées (date < limite_date), octets en cache,
    octets disque estimés.
    """
    res = {}
    con = ouvrir_lecture(cache) if cache and os.path.exists(cache) else None
    try:
        absentes_dispo = con is not None and table_existe(con, "mbtf_absentes")
        tuiles_dispo = con is not None and table_existe(con, "tiles")
        dates_dispo = con is not None and limite_date and table_existe(con, "mbtf_dates")
        disque = _sql_octets_disque("length(tile_data)")
        for z, emprises in sorted(niveaux.items()):
            rects = _rects_tms(emprises, z)
            d = {"attendues": nb_union(rects), "presentes": 0, "absentes": 0, "perimees": 0,
                 "octets": 0, "disque": 0.0}
            if tuiles_dispo:
                for r, excl, params in _sans_doublon(rects):
                    args = [z, *r, *params]
                    cond = ("zoom_level = ? AND tile_column BETWEEN ? AND ? "
                            f"AND tile_row BETWEEN ? AND ?{excl}")
                    n, o, dk = con.execute(
                        f"SELECT COUNT(*), COALESCE(SUM(length(tile_data)), 0), COALESCE(SUM({disque}), 0) "
                        f"FROM tiles WHERE {cond}", args).fetchone()
                    d["presentes"] += n
                    d["octets"] += o
                    d["disque"] += dk
                    if absentes_dispo:
                        d["absentes"] += con.execute(
                            f"SELECT COUNT(*) FROM mbtf_absentes WHERE {cond}", args).fetchone()[0]
                    if dates_dispo:
                        d["perimees"] += con.execute(
                            f"SELECT COUNT(*) FROM mbtf_dates WHERE {cond} AND date < ?",
                            args + [int(limite_date)]).fetchone()[0]
            d["manquantes"] = max(0, d["attendues"] - d["presentes"] - d["absentes"])
            res[z] = d
    finally:
        if con is not None:
            con.close()
    return res


def statut_rectangle(cache, emprise, zmin, zmax, limite_date=None):
    """État d'un rectangle dans le cache, au format de analyser() (clé « completude »)."""
    cov = couverture(cache, {z: [emprise] for z in range(zmin, zmax + 1)}, limite_date)
    comp = [{"z": z, **{k: d[k] for k in ("attendues", "presentes", "absentes", "manquantes")}}
            for z, d in sorted(cov.items())]
    return {"completude": comp,
            "attendues": sum(c["attendues"] for c in comp),
            "presentes": sum(c["presentes"] for c in comp),
            "manquantes": sum(c["manquantes"] for c in comp),
            "perimees": sum(d["perimees"] for d in cov.values()),
            "octets": sum(d["octets"] for d in cov.values())}


def texte_statut_cache(statut, taille_moy=None, mois=None):
    """Lignes d'état d'un rectangle : « ✓ Complète en cache », « À télécharger : … »,
    « ⚠ Incomplète : 3 % manquants en Z18 », « ↻ N tuiles à vérifier (> 6 mois) »."""
    lignes = []
    n = statut["manquantes"]
    poids = f" (≈ {taille_lisible(n * taille_moy)})" if taille_moy and n else ""
    if not n:
        lignes.append("✓ Complète en cache")
    elif not statut["presentes"]:
        lignes.append(f"À télécharger : {nombre(n)} tuiles{poids}")
    else:
        inc = sorted((c for c in statut["completude"] if c["manquantes"]), key=lambda c: -c["z"])
        c = inc[0]
        texte = f"⚠ Incomplète : {pourcentage(100 * c['manquantes'] / c['attendues'])} manquants en Z{c['z']}"
        if len(inc) > 1:
            texte += f" (+{len(inc) - 1} niveau{'x' if len(inc) > 2 else ''})"
        lignes += [texte, f"{nombre(n)} tuiles à télécharger{poids}"]
    if statut.get("perimees"):
        lignes.append(f"↻ {nombre(statut['perimees'])} tuiles à vérifier"
                      + (f" (plus de {mois} mois)" if mois else ""))
    return lignes


def estimer_projet(cache, projet, taille_moy=None, limite_date=None):
    """
    Carte finale : nombre de tuiles, poids estimé, tuiles à télécharger et à vérifier.
    Avec le dédoublonnage, le poids est une borne haute (gain connu seulement à la génération).
    Fichiers externes : comptés au-dessus du zoom max de la base (chevauchements non déduits).
    """
    cov = couverture(cache, niveaux_projet(projet), limite_date)
    presentes = sum(d["presentes"] for d in cov.values())
    octets = sum(d["octets"] for d in cov.values())
    manquantes = sum(d["manquantes"] for d in cov.values())
    absentes = sum(d["absentes"] for d in cov.values())
    moy = octets / presentes if presentes else taille_moy
    disque = sum(d["disque"] for d in cov.values())
    if manquantes and moy:
        disque += manquantes * octets_disque(int(moy))
    tuiles = presentes + manquantes
    seuil = projet["base"]["zmax"] if projet.get("base") else -1
    for chemin in projet.get("externes", []):
        try:
            info = infos_mbtiles(chemin)
        except Exception:
            continue
        n = sum(v for z, v in info["par_zoom"].items() if z > seuil)
        if n:
            tuiles += n
            disque += info["taille"] * n / max(1, info["tuiles"])
    zooms = [z for z, d in cov.items() if d["attendues"] > d["absentes"]]
    opts = projet.get("options", {})
    poids = int(disque + tuiles * 24 + 64 * 1024)
    limite = LIMITE_FICHIER
    return {"tuiles": tuiles, "poids": poids,
            "a_telecharger": manquantes, "a_verifier": sum(d["perimees"] for d in cov.values()),
            "absentes": absentes, "taille_moy": moy,
            "poids_incertain": bool(manquantes) and not moy,
            "borne_haute": bool(opts.get("dedup", True)),
            "fichiers": max(1, math.ceil(poids / (limite * 0.97))) if opts.get("decoupe") else 1,
            "zoom_min": min(zooms) if zooms else None, "zoom_max": max(zooms) if zooms else None}


def creer_schema_cache(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS name ON metadata (name);
        CREATE TABLE IF NOT EXISTS tiles (zoom_level INTEGER, tile_column INTEGER,
                                          tile_row INTEGER, tile_data BLOB);
        CREATE UNIQUE INDEX IF NOT EXISTS tile_index ON tiles (zoom_level, tile_column, tile_row);
        CREATE TABLE IF NOT EXISTS mbtf_absentes (zoom_level INTEGER, tile_column INTEGER,
                                                  tile_row INTEGER,
                                                  PRIMARY KEY (zoom_level, tile_column, tile_row));
        CREATE TABLE IF NOT EXISTS mbtf_dates (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER,
                                               date INTEGER,
                                               PRIMARY KEY (zoom_level, tile_column, tile_row)) WITHOUT ROWID;
    """)


def completer_dates(con, ts=None):
    """Donne une date aux tuiles du cache qui n'en ont pas (cache ancien, tuiles importées)."""
    n_dates = con.execute("SELECT COUNT(*) FROM mbtf_dates").fetchone()[0]
    n_tuiles = (con.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
                + con.execute("SELECT COUNT(*) FROM mbtf_absentes").fetchone()[0])
    if n_dates >= n_tuiles:
        return 0
    if ts is None:
        meta = lire_metadata(con)
        try:
            ts = datetime.datetime.fromisoformat(meta["mbtf_telecharge_le"]).timestamp()
        except (KeyError, ValueError):
            ts = time.time()
    n = 0
    for table in ("tiles", "mbtf_absentes"):
        n += con.execute(f"INSERT OR IGNORE INTO mbtf_dates SELECT zoom_level, tile_column, tile_row, ? "
                         f"FROM {table}", (int(ts),)).rowcount
    return n


def lire_projet(chemin):
    """Projet enregistré dans une carte générée par MBTiles Fusion 3+, ou None."""
    try:
        con = ouvrir_lecture(chemin)
        try:
            meta = lire_metadata(con)
        finally:
            con.close()
        p = json.loads(meta.get("mbtf_projet", ""))
        return p if isinstance(p, dict) and p.get("version") == 3 else None
    except Exception:
        return None


def importer_vers_cache(carte, cache, projet, source_json):
    """
    Copie dans le cache les tuiles d'une carte générée (zones du projet seulement),
    pour éviter de les retélécharger : autre ordinateur, cache vidé…
    Retourne le nombre de tuiles ajoutées.
    """
    niveaux = niveaux_projet(projet)
    if not niveaux:
        return 0
    os.makedirs(os.path.dirname(os.path.abspath(cache)), exist_ok=True)
    con = connecter(cache, isolation_level=None)
    try:
        creer_schema_cache(con)
        meta = lire_metadata(con)
        if meta.get("mbtf_source") and json.loads(meta["mbtf_source"]).get("url") != json.loads(source_json).get("url"):
            return 0
        con.execute("INSERT OR IGNORE INTO metadata VALUES ('mbtf_source', ?)", (source_json,))
        con.execute("ATTACH DATABASE ? AS carte", (os.path.abspath(carte),))
        meta_carte = lire_metadata_schema(con, "carte")
        fmt = (meta_carte.get("format") or "png").lower()
        valide = sql_valide(fmt)
        if not meta.get("format"):
            con.execute("INSERT OR IGNORE INTO metadata VALUES ('format', ?)", (fmt,))
        # Tuiles d'une carte reçue : rien ne garantit qu'elles viennent vraiment de la source
        # annoncée. Elles sont datées « à vérifier » : la prochaine génération les compare au
        # serveur et remplace celles qui diffèrent.
        ts = DATE_A_VERIFIER
        ajout = 0
        con.execute("BEGIN")
        for z, emprises in sorted(niveaux.items()):
            for r in _rects_tms(emprises, z):
                ajout += con.execute(
                    "INSERT OR IGNORE INTO main.tiles SELECT zoom_level, tile_column, tile_row, tile_data "
                    "FROM carte.tiles WHERE zoom_level = ? AND tile_column BETWEEN ? AND ? "
                    f"AND tile_row BETWEEN ? AND ? AND {valide}", (z, *r)).rowcount
        completer_dates(con, ts)
        con.execute("COMMIT")
        con.execute("DETACH DATABASE carte")
    finally:
        con.close()
    oublier(cache)
    return ajout


# ---------------------------------------------------------------------------
# Gestion du cache
# ---------------------------------------------------------------------------

def statistiques_cache(chemin):
    con = ouvrir_lecture(chemin)
    try:
        meta = lire_metadata(con)
        n = con.execute("SELECT COUNT(*) FROM tiles").fetchone()[0] if table_existe(con, "tiles") else 0
    finally:
        con.close()
    try:
        source = json.loads(meta.get("mbtf_source", "{}"))
    except ValueError:
        source = {}
    return {"chemin": chemin, "taille": os.path.getsize(chemin), "tuiles": n,
            "source": source, "date": meta.get("mbtf_telecharge_le", "")}


def purger_cache(cache, niveaux, journal=print):
    """
    Supprime du cache les tuiles hors des rectangles `niveaux` ({z: [emprise]}), puis compacte
    le fichier. Retourne (tuiles supprimées, octets libérés).
    """
    avant = os.path.getsize(cache)
    con = connecter(cache, isolation_level=None)
    try:
        con.execute("CREATE TEMP TABLE garder (z INTEGER, x0 INTEGER, x1 INTEGER, r0 INTEGER, r1 INTEGER)")
        for z, emprises in niveaux.items():
            con.executemany("INSERT INTO temp.garder VALUES (?, ?, ?, ?, ?)",
                            [(z, *r) for r in _rects_tms(emprises, z)])
        hors = ("NOT EXISTS (SELECT 1 FROM temp.garder g WHERE g.z = {t}.zoom_level AND {t}.tile_column "
                "BETWEEN g.x0 AND g.x1 AND {t}.tile_row BETWEEN g.r0 AND g.r1)")
        con.execute("BEGIN")
        n = con.execute(f"DELETE FROM tiles WHERE {hors.format(t='tiles')}").rowcount
        for table in ("mbtf_absentes", "mbtf_dates"):
            if table_existe(con, table):
                con.execute(f"DELETE FROM {table} WHERE {hors.format(t=table)}")
        con.execute("COMMIT")
        journal(f"{nombre(n)} tuiles supprimées du cache, compactage…")
        con.execute("VACUUM")
    finally:
        con.close()
    oublier(cache)
    return n, avant - os.path.getsize(cache)


# ---------------------------------------------------------------------------
# Rendu de la couverture du cache (surcouche de la carte, façon MOBAC)
# ---------------------------------------------------------------------------

def _png_rgba(largeur, hauteur, pixels):
    import struct
    import zlib

    def bloc(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    pas = largeur * 4
    brut = b"".join(b"\x00" + bytes(pixels[i * pas:(i + 1) * pas]) for i in range(hauteur))
    return (b"\x89PNG\r\n\x1a\n" + bloc(b"IHDR", struct.pack(">IIBBBBB", largeur, hauteur, 8, 6, 0, 0, 0))
            + bloc(b"IDAT", zlib.compress(brut, 6)) + bloc(b"IEND", b""))


PNG_VIDE = None
COULEURS_COUV = {"cache": (38, 162, 105, 120), "manque": (224, 27, 36, 120), "absente": (119, 118, 123, 90)}


def png_couverture(con, niveaux, niveau, z, x, y):
    """
    Tuile PNG 256×256 de la surcouche : pour le niveau `niveau`, chaque tuile est verte si
    elle est en cache, grise si le serveur ne l'a pas, rouge si elle manque dans une zone
    de la carte. `con` : connexion en lecture au cache (ou None).
    """
    global PNG_VIDE
    if PNG_VIDE is None:
        PNG_VIDE = _png_rgba(1, 1, bytearray(4))
    if niveau < z or niveau - z > 8:
        # Une tuile du niveau couvre toute la tuile affichée : couleur unie de cette tuile.
        if niveau < z:
            px, py, n = x >> (z - niveau), y >> (z - niveau), 1
        else:
            return PNG_VIDE
    else:
        n = 1 << (niveau - z)
        px, py = x * n, y * n
    x0, x1, y0, y1 = px, px + n - 1, py, py + n - 1
    attendues = []
    for e in niveaux.get(niveau, []):
        a0, a1, b0, b1 = plage(e, niveau)
        if a0 <= x1 and x0 <= a1 and b0 <= y1 and y0 <= b1:
            attendues.append((max(a0, x0), min(a1, x1), max(b0, y0), min(b1, y1)))
    par_rangee = {}
    if con is not None:
        r0, r1 = tms(y1, niveau), tms(y0, niveau)
        haut = (1 << niveau) - 1 - y0  # ligne TMS → rangée locale : haut - tile_row
        for table, nom in (("tiles", "cache"), ("mbtf_absentes", "absente")):
            try:
                for i, j in con.execute(f"SELECT tile_column - ?, ? - tile_row FROM {table} WHERE zoom_level = ? "
                                        "AND tile_column BETWEEN ? AND ? AND tile_row BETWEEN ? AND ?",
                                        (x0, haut, niveau, x0, x1, r0, r1)):
                    par_rangee.setdefault(j, {})[i] = nom
            except sqlite3.Error:
                pass
    if not par_rangee and not attendues:
        return PNG_VIDE
    # Rendu par rangées : chaque rangée de cases est une liste d'états, convertie en une
    # ligne de pixels d'un seul bloc, puis répétée sur la hauteur de la case.
    cote = max(1, 256 // n)
    grille = cote >= 8
    vide = bytes(4)
    plein = {k: bytes(c) for k, c in COULEURS_COUV.items()}
    bord = {k: bytes((c[0], c[1], c[2], min(255, c[3] + 80))) for k, c in COULEURS_COUV.items()}
    ligne_vide = b"\x00" + vide * 256
    rangees = []
    for j in range(n):
        cases = [None] * n
        for a0, a1, b0, b1 in attendues:
            if b0 <= y0 + j <= b1:
                cases[a0 - x0:a1 - x0 + 1] = ["manque"] * (a1 - a0 + 1)
        for i, nom in par_rangee.get(j, {}).items():
            cases[i] = nom
        if not any(cases):
            rangees.append(ligne_vide * cote)
            continue
        if grille:
            haut = b"\x00" + b"".join(bord[c] * cote if c else vide * cote for c in cases)
            corps = b"\x00" + b"".join(bord[c] + plein[c] * (cote - 1) if c else vide * cote for c in cases)
            rangees.append(haut + corps * (cote - 1))
        else:
            rangees.append((b"\x00" + b"".join(plein[c] * cote if c else vide * cote for c in cases)) * cote)
    import struct
    import zlib

    def bloc(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + bloc(b"IHDR", struct.pack(">IIBBBBB", 256, 256, 8, 6, 0, 0, 0))
            + bloc(b"IDAT", zlib.compress(b"".join(rangees), 1)) + bloc(b"IEND", b""))


# ---------------------------------------------------------------------------
# Assemblage de la carte finale
# ---------------------------------------------------------------------------

LIMITE_FICHIER = 4 * 1024 ** 3 - 1  # FAT32

SCHEMA_SIMPLE = """
    CREATE TABLE metadata (name TEXT, value TEXT);
    CREATE UNIQUE INDEX name ON metadata (name);
    CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
    CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);
"""
# Schéma « dédoublonné » de la spécification MBTiles : chaque image n'est stockée qu'une fois,
# la vue « tiles » reste identique pour les applications.
SCHEMA_DEDUP = """
    CREATE TABLE metadata (name TEXT, value TEXT);
    CREATE UNIQUE INDEX name ON metadata (name);
    CREATE TABLE map (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_id TEXT);
    CREATE UNIQUE INDEX map_index ON map (zoom_level, tile_column, tile_row);
    CREATE TABLE images (tile_data BLOB, tile_id TEXT);
    CREATE UNIQUE INDEX images_id ON images (tile_id);
    CREATE VIEW tiles AS SELECT map.zoom_level AS zoom_level, map.tile_column AS tile_column,
        map.tile_row AS tile_row, images.tile_data AS tile_data
        FROM map JOIN images ON images.tile_id = map.tile_id;
"""


def _empreinte(donnees):
    return hashlib.md5(bytes(donnees)).hexdigest() if donnees is not None else None


def _nouvelle_carte(chemin, dedup):
    if os.path.exists(chemin):
        os.remove(chemin)
    con = connecter(chemin, isolation_level=None)
    con.execute("PRAGMA journal_mode = OFF")
    con.execute("PRAGMA synchronous = OFF")
    con.execute(f"PRAGMA page_size = {TAILLE_PAGE}")
    con.executescript(SCHEMA_DEDUP if dedup else SCHEMA_SIMPLE)
    con.create_function("mbtf_h", 1, _empreinte, deterministic=True)
    return con


def _inserer(con, dedup, origine, params, remplacer=False):
    """Copie des tuiles « SELECT … {origine} » ; origine = « FROM src.tiles WHERE … »."""
    verbe = "INSERT OR REPLACE" if remplacer else "INSERT OR IGNORE"
    if not dedup:
        return con.execute(f"{verbe} INTO main.tiles SELECT zoom_level, tile_column, tile_row, tile_data "
                           f"{origine}", params).rowcount
    con.execute(f"INSERT OR IGNORE INTO main.images SELECT tile_data, mbtf_h(tile_data) {origine}", params)
    return con.execute(f"{verbe} INTO main.map SELECT zoom_level, tile_column, tile_row, mbtf_h(tile_data) "
                       f"{origine}", params).rowcount


def _meta_contenu(con, table):
    """minzoom, maxzoom et emprise (union de tous les niveaux) d'après les tuiles présentes."""
    lignes = con.execute(f"SELECT zoom_level, MIN(tile_column), MAX(tile_column), MIN(tile_row), MAX(tile_row) "
                         f"FROM {table} GROUP BY zoom_level").fetchall()
    if not lignes:
        return None
    emprises = [emprise_de_plage(x0, x1, tms(r1, z), tms(r0, z), z) for z, x0, x1, r0, r1 in lignes]
    return min(l[0] for l in lignes), max(l[0] for l in lignes), union_emprises(emprises)


def _nettoyer_parties(sortie, garder=0):
    """Supprime les anciens fichiers « carte_N.mbtiles » générés par l'outil (N > garder)."""
    p = Path(sortie)
    for f in p.parent.glob(f"{p.stem}_*.mbtiles"):
        suffixe = f.stem[len(p.stem) + 1:]
        if not suffixe.isdigit() or int(suffixe) <= garder:
            continue
        try:
            con = ouvrir_lecture(str(f))
            try:
                ok = "mbtf_partie" in lire_metadata(con)
            finally:
                con.close()
            if ok:
                f.unlink()
        except Exception:
            pass


def _decouper(tmp, sortie, dedup, seuil_base, limite, journal):
    """
    Découpe la carte assemblée `tmp` en fichiers de moins de `limite` octets :
    la base d'abord, puis le reste en bandes nord-sud (chaque fichier couvre une
    bande continue, tous niveaux confondus). Retourne la liste des fichiers.
    """
    budget = limite * 0.96
    p = Path(sortie)
    src = connecter(tmp)
    meta = lire_metadata(src)
    table = "map" if dedup else "tiles"
    colonne = "tile_id" if dedup else "NULL"
    ordre = (f"SELECT zoom_level, tile_column, tile_row, {colonne}, "
             f"{'NULL' if dedup else 'tile_data'} FROM {table} "
             "ORDER BY (zoom_level > ?), (tile_column + 0.5) / (1 << zoom_level), zoom_level, tile_column, tile_row")
    parties = []
    con = None
    taille = 0.0
    vus = set()

    def ouvrir():
        nonlocal con, taille, vus
        chemin = str(p.with_name(f"{p.stem}_{len(parties) + 1}.mbtiles.tmp"))
        con = _nouvelle_carte(chemin, dedup)
        con.execute("BEGIN")
        parties.append(chemin)
        taille, vus = 64 * 1024, set()

    def fermer():
        con.execute("COMMIT")
        con.close()

    lecteur = src.cursor()
    for zl, col, row, tid, donnees in lecteur.execute(ordre, (seuil_base,)):
        if dedup:
            if tid not in vus:
                donnees = src.execute("SELECT tile_data FROM images WHERE tile_id = ?", (tid,)).fetchone()[0]
                cout = octets_disque(len(donnees)) + 60
            else:
                donnees, cout = None, 40
        else:
            cout = octets_disque(len(donnees)) + 24
        if con is None or taille + cout > budget:
            if con is not None:
                fermer()
            ouvrir()
            if dedup and donnees is None:  # image déjà vue dans la partie précédente
                donnees = src.execute("SELECT tile_data FROM images WHERE tile_id = ?", (tid,)).fetchone()[0]
                cout = octets_disque(len(donnees)) + 60
        if dedup:
            if donnees is not None:
                con.execute("INSERT OR IGNORE INTO images VALUES (?, ?)", (donnees, tid))
                vus.add(tid)
            con.execute("INSERT OR IGNORE INTO map VALUES (?, ?, ?, ?)", (zl, col, row, tid))
        else:
            con.execute("INSERT OR IGNORE INTO tiles VALUES (?, ?, ?, ?)", (zl, col, row, donnees))
        taille += cout
    if con is not None:
        fermer()
    src.close()

    finales = []
    for i, chemin in enumerate(parties, start=1):
        c = connecter(chemin, isolation_level=None)
        c.executemany("INSERT OR REPLACE INTO metadata VALUES (?, ?)", meta.items())
        contenu = _meta_contenu(c, table)
        if contenu:
            zmin, zmax, (o, s, e, n) = contenu
            for k, v in {"minzoom": str(zmin), "maxzoom": str(zmax), "bounds": f"{o},{s},{e},{n}",
                         "center": f"{(o + e) / 2:.6f},{(s + n) / 2:.6f},{zmax}",
                         "name": f"{meta.get('name', p.stem)} ({i}/{len(parties)})",
                         "mbtf_partie": f"{i}/{len(parties)}"}.items():
                c.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (k, v))
        c.execute("ANALYZE")
        c.close()
        final = chemin[:-4]
        remplacer(chemin, final)
        finales.append(final)
        journal(f"  Fichier {i}/{len(parties)} : {Path(final).name} — {taille_lisible(os.path.getsize(final))}"
                + (f", Z{contenu[0]}–Z{contenu[1]}" if contenu else ""))
    return finales


def generer_projet(cache, projet, sortie, journal=print, progression=lambda f: None):
    """
    Assemble la carte finale depuis le cache (base puis zones), puis les fichiers externes
    au-dessus du zoom max de la base. Écrit le projet dans les métadonnées (mbtf_projet)
    pour pouvoir rouvrir et modifier la carte.
    Options (projet["options"]) : dedup (images identiques stockées une fois, défaut oui),
    decoupe (fichiers de 4 Go max).
    """
    sortie = os.path.abspath(sortie)
    base = projet["base"]
    opts = projet.get("options", {})
    dedup = bool(opts.get("dedup", True))
    decoupe = bool(opts.get("decoupe", False))
    niveaux = niveaux_projet(projet)
    externes = [os.path.abspath(e) for e in projet.get("externes", [])]
    if sortie == os.path.abspath(cache) or sortie in externes:
        raise ValueError("le fichier de sortie doit être différent du cache et des fichiers externes")

    con_c = ouvrir_lecture(cache)
    try:
        meta_cache = lire_metadata(con_c)
    finally:
        con_c.close()
    fmt = (meta_cache.get("format") or "png").lower()
    valide = sql_valide(fmt)
    table = "map" if dedup else "tiles"

    tmp = sortie + ".tmp"
    total = sum(len(v) for v in niveaux.values()) + len(externes)
    fait = 0
    attendues = absentes = 0
    trace = []
    con = _nouvelle_carte(tmp, dedup)
    try:
        con.execute("ATTACH DATABASE ? AS src", (os.path.abspath(cache),))
        a_absentes = table_existe(con, "mbtf_absentes", "src")
        par_rect = {"base": 0}
        for zone in projet.get("zones", []):
            par_rect[zone["id"]] = 0
        con.execute("BEGIN")
        for z, emprises in sorted(niveaux.items()):
            proprietaires = (["base"] if z <= base["zmax"] else
                             [zn["id"] for zn in projet["zones"] if zn["zmax"] >= z])
            rects = _rects_tms(emprises, z)
            attendues += nb_union(rects)
            for (r, excl, params), prop in zip(_sans_doublon(rects), proprietaires):
                args = [z, *r, *params]
                cond = ("WHERE zoom_level = ? AND tile_column BETWEEN ? AND ? "
                        f"AND tile_row BETWEEN ? AND ?{excl}")
                par_rect[prop] += _inserer(con, dedup, f"FROM src.tiles {cond} AND {valide}", args)
                if a_absentes:
                    absentes += con.execute(f"SELECT COUNT(*) FROM src.mbtf_absentes {cond}", args).fetchone()[0]
                fait += 1
                progression(0.8 * fait / max(1, total))
        con.execute("COMMIT")
        con.execute("DETACH DATABASE src")
        ecrites = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        journal(f"Base : {nombre(par_rect['base'])} tuiles (Z{base['zmin']}–Z{base['zmax']})")
        emprises = [tuple(base["emprise"])]
        for zone in projet.get("zones", []):
            journal(f"{zone['nom']} : {nombre(par_rect[zone['id']])} tuiles ajoutées "
                    f"(Z{base['zmax'] + 1}–Z{zone['zmax']})")
            emprises.append(tuple(zone["emprise"]))
            trace.append({"nom": zone["nom"], "zooms": f"Z{base['zmax'] + 1}-Z{zone['zmax']}",
                          "tuiles": par_rect[zone["id"]],
                          "emprise": [round(v, 6) for v in zone["emprise"]]})
        manquantes = attendues - absentes - ecrites
        if manquantes > 0:
            journal(f"  ⚠ {nombre(manquantes)} tuiles absentes du cache (non téléchargées) : trous possibles")

        # Fichiers externes (compatibilité) : au-dessus de la base, ils l'emportent.
        for ext in externes:
            info = infos_mbtiles(ext)
            con.execute("ATTACH DATABASE ? AS src", (ext,))
            con.execute("BEGIN")
            nb = _inserer(con, dedup, f"FROM src.tiles WHERE zoom_level > ? AND {sql_valide(info['format'])}",
                          (base["zmax"],), remplacer=True)
            con.execute("COMMIT")
            e = _emprise_attachee(con, info)
            con.execute("DETACH DATABASE src")
            journal(f"Fichier externe : {Path(ext).name} → {nombre(nb)} tuiles")
            if nb:
                emprises.append(e)
                trace.append({"nom": Path(ext).name, "fichier": True, "tuiles": nb,
                              "emprise": [round(v, 6) for v in e]})
            fait += 1
            progression(0.8 * fait / max(1, total))
        if dedup and externes:
            con.execute("DELETE FROM images WHERE tile_id NOT IN (SELECT tile_id FROM map)")

        zmin, zmax = con.execute(f"SELECT MIN(zoom_level), MAX(zoom_level) FROM {table}").fetchone()
        if zmin is None:
            raise ValueError("aucune tuile à écrire : le cache est vide pour cette carte")
        if dedup:
            n_img = con.execute("SELECT COUNT(*) FROM images").fetchone()[0]
            n_map = con.execute("SELECT COUNT(*) FROM map").fetchone()[0]
            if n_map > n_img:
                journal(f"Dédoublonnage : {nombre(n_map - n_img)} tuiles identiques stockées une seule fois "
                        f"({pourcentage(100 * (n_map - n_img) / n_map)})")
        o, s, e, n = union_emprises(emprises)
        o, s = math.floor(o * 1e6) / 1e6, math.floor(s * 1e6) / 1e6
        e, n = math.ceil(e * 1e6) / 1e6, math.ceil(n * 1e6) / 1e6
        maintenant = datetime.datetime.now().astimezone()
        src = projet.get("source") or {}
        noms = ", ".join(t["nom"] for t in trace) or "aucune"
        valeurs = {
            "name": projet.get("nom") or Path(sortie).stem,
            "format": fmt,
            "type": "baselayer",
            "version": "1.1",
            "minzoom": str(zmin),
            "maxzoom": str(zmax),
            "bounds": f"{o},{s},{e},{n}",
            "center": f"{(o + e) / 2:.6f},{(s + n) / 2:.6f},{min(zmax, base['zmax'])}",
            "attribution": src.get("attribution", ""),
            "description": (f"Générée le {maintenant:%d/%m/%Y à %H:%M} par MBTiles Fusion {VERSION}. "
                            f"Source : {src.get('nom', '?')}. Zones : {noms}."),
            "mbtf_genere_le": maintenant.isoformat(timespec="seconds"),
            "mbtf_version": VERSION,
            "mbtf_zones": json.dumps(trace, ensure_ascii=False),
            "mbtf_projet": json.dumps(projet, ensure_ascii=False),
        }
        for cle, val in valeurs.items():
            con.execute("INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)", (cle, val))
        con.execute("ANALYZE")
    finally:
        con.close()
    progression(0.85)

    taille = os.path.getsize(tmp)
    if decoupe and taille > LIMITE_FICHIER:
        journal(f"Carte de {taille_lisible(taille)} : découpage en fichiers de 4 Go maximum…")
        fichiers = _decouper(tmp, sortie, dedup, base["zmax"], LIMITE_FICHIER, journal)
        os.remove(tmp)
        if os.path.exists(sortie):
            os.remove(sortie)  # l'ancienne carte d'un seul tenant n'est plus à jour
        _nettoyer_parties(sortie, garder=len(fichiers))
    else:
        remplacer(tmp, sortie)
        _nettoyer_parties(sortie, garder=0)
        fichiers = [sortie]
    for f in fichiers:
        oublier(f)
    progression(1.0)
    total_octets = sum(os.path.getsize(f) for f in fichiers)
    journal(f"Carte générée : {', '.join(Path(f).name for f in fichiers)} — Z{zmin}–Z{zmax}, "
            f"{taille_lisible(total_octets)}")
    return {"taille": total_octets, "manquantes": max(0, manquantes), "fichiers": fichiers}


# ---------------------------------------------------------------------------
# Ligne de commande (diagnostic)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "analyse":
        for f in sys.argv[2:]:
            a = analyser(f)
            print(rapport_texte(f, a))
            print(" ", texte_completude(a), "|", texte_invalides(a) or "aucune tuile invalide")
    elif len(sys.argv) >= 3 and sys.argv[1] == "estime":
        r = estimer(sys.argv[2], sys.argv[3:])
        print(f"{nombre(r['tuiles'])} tuiles, ≈ {taille_lisible(r['poids'])}")
    else:
        print("usage : moteur.py analyse FICHIER… | estime BASE [ZONE…]")
