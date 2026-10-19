# SPDX-License-Identifier: GPL-3.0-or-later
"""
Carte éditable : base régionale et zones de zoom en rectangles.

- mode tracé : glisser sur la carte crée un rectangle ;
- clic sur un rectangle : sélection ;
- rectangle sélectionné : 8 poignées rondes (coins et milieux des côtés) pour
  le redimensionner, glisser l'intérieur pour le déplacer ;
- partout ailleurs, la carte se déplace normalement.
"""

import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

try:
    gi.require_version("Shumate", "1.0")
    from gi.repository import Shumate  # noqa: E402
except (ValueError, ImportError):
    Shumate = None

import geo  # noqa: E402
import moteur as m  # noqa: E402

COULEURS = ["#e66100", "#2ec27e", "#9141ac", "#e5a50a", "#c01c28", "#1c71d8", "#865e3c", "#26a269"]
COULEUR_BASE = "#3584e4"
RAYON_POIGNEE = 12  # pixels de tolérance autour d'une poignée

# Poignées : (nom, côtés déplacés horizontalement, verticalement, curseur)
POIGNEES = (("no", "o", "n", "nwse-resize"), ("n", None, "n", "ns-resize"),
            ("ne", "e", "n", "nesw-resize"), ("e", "e", None, "ew-resize"),
            ("se", "e", "s", "nwse-resize"), ("s", None, "s", "ns-resize"),
            ("so", "o", "s", "nesw-resize"), ("o", "o", None, "ew-resize"))

CSS = b"""
.poignee {
  background-color: white;
  border: 2px solid #1c71d8;
  border-radius: 9999px;
  min-width: 10px;
  min-height: 10px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.45);
}
.pastille { border-radius: 9999px; min-width: 12px; min-height: 12px; }
.astuce-carte { padding: 6px 12px; border-radius: 9999px; }
.position-carte { padding: 2px 8px; border-radius: 6px; font-family: monospace; font-size: 0.85em; }
.legende-carte { padding: 4px 10px; border-radius: 6px; font-size: 0.85em; }
.recherche-carte { background-color: alpha(@window_bg_color, 0.96); border-radius: 8px;
                   box-shadow: 0 1px 4px rgba(0,0,0,0.35); }
.repere { color: #e01b24; -gtk-icon-shadow: 0 1px 2px rgba(0,0,0,0.6); }
"""


def rgba(texte, alpha=None):
    c = Gdk.RGBA()
    c.parse(texte)
    if alpha is not None:
        c.alpha = alpha
    return c


def couleur_zone(i):
    return COULEURS[i % len(COULEURS)]


def charger_css():
    fournisseur = Gtk.CssProvider()
    fournisseur.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), fournisseur,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    # Pastilles de couleur des zones
    css = "".join(f".pastille-{i} {{ background-color: {c}; }}\n" for i, c in enumerate(COULEURS))
    css += f".pastille-base {{ background-color: {COULEUR_BASE}; }}\n"
    css += ".pastille-externe { background-color: #77767b; }\n"
    f2 = Gtk.CssProvider()
    f2.load_from_data(css.encode())
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), f2,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def pastille(classe):
    p = Gtk.Box(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
    p.add_css_class("pastille")
    p.add_css_class(classe)
    return p


if Shumate is not None:
    class SourceCarte(Shumate.DataSource):
        """
        Source de tuiles branchée directement sur le Fournisseur (Python) : la carte demande
        toutes les tuiles visibles d'un coup, sans la limite de 2 connexions de libshumate,
        et sans son cache disque en double.
        """

        def __init__(self, fournisseur, cle, zoom_max, couverture=False):
            super().__init__()
            self.set_min_zoom_level(0)
            self.set_max_zoom_level(zoom_max)
            self.f, self.cle, self.couverture = fournisseur, cle, couverture

        def do_start_request(self, x, y, z, annulation):
            req = Shumate.DataSourceRequest.new(x, y, z)

            def rappel(d):
                GLib.idle_add(SourceCarte._emettre, req, d)
            if self.couverture:
                self.f.demander_couverture(z, x, y, rappel)
            else:
                self.f.demander(self.cle, z, x, y, rappel,
                                (lambda: annulation is not None and annulation.is_cancelled()))
            return req

        @staticmethod
        def _emettre(req, d):
            if not req.is_completed():
                if d:
                    req.emit_data(GLib.Bytes.new(d), True)
                else:
                    req.emit_error(GLib.Error("tuile indisponible"))
            return False


class CarteEditable(Gtk.Overlay):
    """
    Rappels :
      on_trace(mode, emprise)   — rectangle tracé en mode « base » ou « zone »
      on_modif(cle, emprise, fini) — rectangle déplacé / redimensionné
      on_selection(cle | None)  — sélection changée par un clic sur la carte
    """

    def __init__(self, on_trace, on_modif, on_selection, on_recherche=None):
        super().__init__(hexpand=True, vexpand=True, width_request=360)
        self.on_trace, self.on_modif, self.on_selection = on_trace, on_modif, on_selection
        self.on_recherche = on_recherche
        self.couche_couv = None
        self.rects = {}        # cle -> {"emprise", "couleur", "style"}
        self.ordre = []
        self.couches = {}
        self.selection = None
        self.mode_trace = None
        self._geste = None     # état du glissement en cours
        self._cle_fond = None
        self.disponible = Shumate is not None

        if not self.disponible:
            self.set_child(Adw.StatusPage(
                icon_name="mark-location-symbolic", title="Carte indisponible",
                description="libshumate est introuvable :\nsudo apt install gir1.2-shumate-1.0"))
            return

        self.carte = Shumate.SimpleMap(hexpand=True, vexpand=True)
        self.map = self.carte.get_map()
        self.vp = self.carte.get_viewport()
        self.set_child(self.carte)

        # Couche des poignées (au-dessus de tout)
        self.couche_poignees = Shumate.MarkerLayer.new(self.vp)
        self.poignees = {}
        for nom, *_ in POIGNEES:
            mk = Shumate.Marker()
            w = Gtk.Box()
            w.add_css_class("poignee")
            mk.set_child(w)
            mk.set_can_target(False)
            mk.set_visible(False)
            self.couche_poignees.add_marker(mk)
            self.poignees[nom] = mk
        self.couche_poignees.set_can_target(False)
        self.map.add_layer(self.couche_poignees)

        # Repère du résultat de recherche
        self.repere = Shumate.Marker()
        icone = Gtk.Image(icon_name="mark-location-symbolic", pixel_size=36)
        icone.add_css_class("repere")
        self.repere.set_child(icone)
        self.repere.set_can_target(False)
        self.repere.set_visible(False)
        self.couche_poignees.add_marker(self.repere)

        # Recherche (lieu, coordonnées, MGRS)
        self.recherche = Gtk.SearchEntry(placeholder_text="Lieu, coordonnées ou MGRS",
                                         halign=Gtk.Align.END, valign=Gtk.Align.START,
                                         margin_top=12, margin_end=72, width_request=300)
        self.recherche.add_css_class("recherche-carte")
        self.recherche.connect("activate", lambda e: self.on_recherche and self.on_recherche(e.get_text()))
        self.recherche.connect("stop-search", lambda *_: self.masquer_repere())
        self.add_overlay(self.recherche)

        # Position du pointeur (MGRS + décimal) et légende de la couverture
        self.position = Gtk.Label(halign=Gtk.Align.END, valign=Gtk.Align.END, margin_end=8,
                                  margin_bottom=26, can_target=False, visible=False)
        self.position.add_css_class("osd")
        self.position.add_css_class("position-carte")
        self.add_overlay(self.position)
        self.legende = Gtk.Label(halign=Gtk.Align.START, valign=Gtk.Align.END, margin_start=12,
                                 margin_bottom=104, can_target=False, visible=False, use_markup=True)
        self.legende.set_markup('<span foreground="#26a269">■</span> en cache   '
                                '<span foreground="#e01b24">■</span> à télécharger   '
                                '<span foreground="#9a9996">■</span> absente du serveur')
        self.legende.add_css_class("osd")
        self.legende.add_css_class("legende-carte")
        self.add_overlay(self.legende)
        sortie = Gtk.EventControllerMotion()
        sortie.connect("leave", lambda *_: self.position.set_visible(False))
        self.map.add_controller(sortie)

        # Message central (pas de source) et astuce en bas (mode tracé)
        self.msg_fond = Gtk.Label(label="Choisis une source (à droite) pour afficher le fond de carte",
                                  halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, wrap=True,
                                  margin_start=24, margin_end=24, can_target=False)
        self.msg_fond.add_css_class("osd")
        self.msg_fond.add_css_class("title-4")
        self.msg_fond.add_css_class("astuce-carte")
        self.add_overlay(self.msg_fond)
        self.astuce = Gtk.Label(halign=Gtk.Align.CENTER, valign=Gtk.Align.END, margin_bottom=40,
                                visible=False, can_target=False)
        self.astuce.add_css_class("osd")
        self.astuce.add_css_class("astuce-carte")
        self.add_overlay(self.astuce)

        glisser = Gtk.GestureDrag(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        glisser.connect("drag-begin", self._debut)
        glisser.connect("drag-update", self._maj)
        glisser.connect("drag-end", self._fin)
        self.map.add_controller(glisser)
        clic = Gtk.GestureClick()
        clic.connect("released", self._clic)
        self.map.add_controller(clic)
        survol = Gtk.EventControllerMotion()
        survol.connect("motion", self._survol)
        self.map.add_controller(survol)
        self.vp.connect("notify::zoom-level", lambda *_: self._placer_poignees())

        self.vp.set_zoom_level(5)
        self.vp.set_location(46.6, 2.4)

    # ================================================= fond de carte
    def definir_fond(self, apercu, source, cache):
        if not self.disponible:
            return
        if not source:
            self.msg_fond.set_visible(True)
            return
        self.msg_fond.set_visible(False)
        cle = apercu.definir_source(source, cache)
        if cle == self._cle_fond:
            return
        self._cle_fond = cle
        self._source_fond = SourceCarte(apercu, cle, max(1, source["zoom_max"]))
        rendu = Shumate.RasterRenderer.new_full(
            cle, source["nom"], source.get("attribution") or "", "", 0, max(1, source["zoom_max"]), 256,
            Shumate.MapProjection.MERCATOR, self._source_fond)
        self.carte.set_map_source(rendu)
        self.vp.set_max_zoom_level(max(1, source["zoom_max"]))
        self._remonter_couches()
        # Sécurité : si la vue a été réglée avant que le fond soit prêt, on la relance.
        GLib.timeout_add(600, self._relancer_vue)

    def _remonter_couches(self):
        """set_map_source ajoute le fond par-dessus : on remonte couverture, rectangles et poignées."""
        def retirer(c):  # une couche pas encore ajoutée ne peut pas être retirée
            if c is not None and c.get_parent() is not None:
                self.map.remove_layer(c)
        retirer(self.couche_poignees)
        for c in self.couches.values():
            retirer(c)
        if self.couche_couv is not None:
            retirer(self.couche_couv)
            self.map.add_layer(self.couche_couv)
        for cle in self.ordre:
            if cle in self.couches:
                self.map.add_layer(self.couches[cle])
        self.map.add_layer(self.couche_poignees)

    def _relancer_vue(self):
        z = self.vp.get_zoom_level()
        self.vp.set_zoom_level(z + 0.01)
        self.vp.set_zoom_level(z)
        return False

    # ================================================= surcouche « couverture du cache »
    def definir_couverture(self, apercu, cache, niveaux, niveau):
        """Affiche (niveau = int) ou masque (niveau = None) la couverture du cache."""
        if not self.disponible:
            return
        if self.couche_couv is not None:
            self.map.remove_layer(self.couche_couv)
            self.couche_couv = None
        self.legende.set_visible(niveau is not None)
        if niveau is None:
            return
        apercu.definir_couverture(cache, niveaux, niveau)
        self._n_couv = getattr(self, "_n_couv", 0) + 1
        self._source_couv = SourceCarte(apercu, None, 22, couverture=True)
        rendu = Shumate.RasterRenderer.new_full(f"couverture-{self._n_couv}", "Couverture du cache", "", "",
                                                0, 22, 256, Shumate.MapProjection.MERCATOR, self._source_couv)
        self.couche_couv = Shumate.MapLayer.new(rendu, self.vp)
        self.couche_couv.set_can_target(False)
        self._remonter_couches()

    # ================================================= recherche
    def montrer_point(self, lat, lon, zoom=None):
        if not self.disponible:
            return
        self.repere.set_location(lat, lon)
        self.repere.set_visible(True)
        if zoom is not None:
            self.vp.set_zoom_level(max(1, min(self.vp.get_max_zoom_level(), zoom)))
        self.vp.set_location(lat, lon)

    def masquer_repere(self):
        if self.disponible:
            self.repere.set_visible(False)

    # ================================================= rectangles
    def definir_rects(self, rects):
        """rects : liste de (cle, emprise, couleur, style) — style : base | zone | externe."""
        self.rects = {c: {"emprise": tuple(e), "couleur": col, "style": st} for c, e, col, st in rects}
        self.ordre = [c for c, *_ in rects]
        if self.selection not in self.rects:
            self.selection = None
        self._redessiner()

    def _couche(self, cle):
        r = self.rects[cle]
        o, s, e, n = r["emprise"]
        c = Shumate.PathLayer.new(self.vp)
        for lat, lon in ((n, o), (n, e), (s, e), (s, o)):
            c.add_node(Shumate.Coordinate.new_full(lat, lon))
        c.set_closed(True)
        choisi = cle == self.selection
        c.set_stroke_color(rgba(r["couleur"]))
        c.set_stroke_width(3.0 if choisi else 2.0)
        c.set_fill(True)
        alpha = {"base": 0.06, "zone": 0.16, "externe": 0.08}[r["style"]] + (0.08 if choisi else 0)
        c.set_fill_color(rgba(r["couleur"], alpha))
        if r["style"] in ("base", "externe"):
            c.set_dash([8.0, 5.0])
        c.set_can_target(False)
        return c

    def _redessiner(self):
        if not self.disponible:
            return
        for c in self.couches.values():
            self.map.remove_layer(c)
        self.couches = {}
        for cle in self.ordre:
            c = self._couche(cle)
            self.map.insert_layer_behind(c, self.couche_poignees)
            self.couches[cle] = c
        self._placer_poignees()

    def _redessiner_un(self, cle):
        if cle in self.couches:
            self.map.remove_layer(self.couches[cle])
        c = self._couche(cle)
        self.map.insert_layer_behind(c, self.couche_poignees)
        self.couches[cle] = c
        self._placer_poignees()

    def _points_poignees(self, emprise):
        o, s, e, n = emprise
        # Milieu vertical en Mercator pour rester au milieu à l'écran
        _, y_n = m.lonlat_vers_xy(o, n, 0)
        _, y_s = m.lonlat_vers_xy(o, s, 0)
        _, lat_m = m.xy_vers_lonlat(0, (y_n + y_s) / 2, 0)
        lon_m = (o + e) / 2
        return {"no": (n, o), "n": (n, lon_m), "ne": (n, e), "e": (lat_m, e),
                "se": (s, e), "s": (s, lon_m), "so": (s, o), "o": (lat_m, o)}

    def _placer_poignees(self):
        if not self.disponible:
            return
        r = self.rects.get(self.selection)
        editable = r is not None and r["style"] != "externe"
        pts = self._points_poignees(r["emprise"]) if editable else {}
        for nom, mk in self.poignees.items():
            mk.set_visible(editable)
            if editable:
                mk.set_location(*pts[nom])

    def selectionner(self, cle):
        if not self.disponible:
            self.selection = cle
            return
        ancienne = self.selection
        self.selection = cle
        for c in (ancienne, cle):
            if c in self.rects:
                self._redessiner_un(c)
        self._placer_poignees()

    def maj_emprise(self, cle, emprise):
        if cle in self.rects:
            self.rects[cle]["emprise"] = tuple(emprise)
            if self.disponible:
                self._redessiner_un(cle)

    # ================================================= mode tracé
    def tracer(self, mode):
        """mode : « base », « zone » ou None (annuler)."""
        self.mode_trace = mode
        if not self.disponible:
            return
        if mode:
            self.astuce.set_label("Glisse sur la carte pour tracer la base régionale" if mode == "base"
                                  else "Glisse sur la carte pour tracer la zone de zoom")
            self.astuce.set_visible(True)
            self.map.set_cursor_from_name("crosshair")
        else:
            self.astuce.set_visible(False)
            self.map.set_cursor(None)

    # ================================================= géométrie écran
    def _ecran(self, lat, lon):
        return self.vp.location_to_widget_coords(self.map, lat, lon)

    def _lieu(self, x, y):
        lat, lon = self.vp.widget_coords_to_location(self.map, x, y)
        return max(-m.LAT_MAX, min(m.LAT_MAX, lat)), max(-180.0, min(180.0, lon))

    def _boite_ecran(self, emprise):
        o, s, e, n = emprise
        x0, y0 = self._ecran(n, o)
        x1, y1 = self._ecran(s, e)
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)

    def _poignee_sous(self, x, y):
        r = self.rects.get(self.selection)
        if not r or r["style"] == "externe":
            return None
        pts = self._points_poignees(r["emprise"])
        for nom, *_ in POIGNEES:
            px, py = self._ecran(*pts[nom])
            if math.hypot(px - x, py - y) <= RAYON_POIGNEE:
                return nom
        return None

    def _rect_sous(self, x, y):
        """Rectangle sous le pointeur : sélectionné d'abord, puis zones (dessus), puis base."""
        candidats = ([self.selection] if self.selection else []) + list(reversed(self.ordre))
        for cle in candidats:
            if cle not in self.rects:
                continue
            x0, y0, x1, y1 = self._boite_ecran(self.rects[cle]["emprise"])
            if x0 <= x <= x1 and y0 <= y <= y1:
                return cle
        return None

    # ================================================= gestes
    def _debut(self, geste, x, y):
        self._geste = None
        if self.mode_trace:
            geste.set_state(Gtk.EventSequenceState.CLAIMED)
            self._geste = {"type": "trace", "depart": self._lieu(x, y)}
            return
        poignee = self._poignee_sous(x, y)
        if poignee:
            geste.set_state(Gtk.EventSequenceState.CLAIMED)
            self._geste = {"type": "poignee", "nom": poignee, "cle": self.selection,
                           "origine": self.rects[self.selection]["emprise"]}
            return
        if self.selection and self._rect_sous(x, y) == self.selection \
                and self.rects[self.selection]["style"] != "externe":
            geste.set_state(Gtk.EventSequenceState.CLAIMED)
            lat, lon = self._lieu(x, y)
            self._geste = {"type": "deplacer", "cle": self.selection,
                           "origine": self.rects[self.selection]["emprise"],
                           "depart": m.lonlat_vers_xy(lon, lat, 0)}
            return
        geste.set_state(Gtk.EventSequenceState.DENIED)

    def _calcul(self, geste, dx, dy, g=None):
        g = g or self._geste
        ok, sx, sy = geste.get_start_point()
        lat, lon = self._lieu(sx + dx, sy + dy)
        if g["type"] == "trace":
            lat0, lon0 = g["depart"]
            return (min(lon0, lon), min(lat0, lat), max(lon0, lon), max(lat0, lat))
        if g["type"] == "poignee":
            o, s, e, n = g["origine"]
            _, horiz, vert, _c = next(p for p in POIGNEES if p[0] == g["nom"])
            if horiz == "o":
                o = lon
            elif horiz == "e":
                e = lon
            if vert == "n":
                n = lat
            elif vert == "s":
                s = lat
            return (min(o, e), min(s, n), max(o, e), max(s, n))
        # déplacement en coordonnées Mercator (forme conservée à l'écran)
        o, s, e, n = g["origine"]
        x0, y0 = g["depart"]
        x1, y1 = m.lonlat_vers_xy(lon, lat, 0)
        ddx, ddy = x1 - x0, y1 - y0
        ax, ay = m.lonlat_vers_xy(o, n, 0)
        bx, by = m.lonlat_vers_xy(e, s, 0)
        ddx = max(-ax, min(1 - bx, ddx))
        o2, n2 = m.xy_vers_lonlat(ax + ddx, ay + ddy, 0)
        e2, s2 = m.xy_vers_lonlat(bx + ddx, by + ddy, 0)
        return (o2, s2, e2, n2)

    def _maj(self, geste, dx, dy):
        if not self._geste:
            return
        emp = self._calcul(geste, dx, dy)
        if self._geste["type"] == "trace":
            self.rects["__trace__"] = {"emprise": emp, "couleur": "#1c71d8", "style": "zone"}
            self._redessiner_un("__trace__")
        else:
            self.maj_emprise(self._geste["cle"], emp)
            self.on_modif(self._geste["cle"], emp, False)

    def _fin(self, geste, dx, dy):
        g, self._geste = self._geste, None
        if not g:
            return
        emp = tuple(round(v, 6) for v in self._calcul(geste, dx, dy, g))
        valide = emp[2] - emp[0] > 1e-5 and emp[3] - emp[1] > 1e-5
        if g["type"] == "trace":
            if "__trace__" in self.couches:
                self.map.remove_layer(self.couches.pop("__trace__"))
            self.rects.pop("__trace__", None)
            mode = self.mode_trace
            self.tracer(None)
            if valide:
                self.on_trace(mode, emp)
            return
        if not valide:
            emp = g["origine"]
        self.maj_emprise(g["cle"], emp)
        self.on_modif(g["cle"], emp, True)

    def _clic(self, geste, n, x, y):
        if self.mode_trace or n != 1:
            return
        cle = self._rect_sous(x, y)
        if cle != self.selection:
            self.selectionner(cle)
            self.on_selection(cle)

    def _survol(self, ctrl, x, y):
        try:
            lat, lon = self._lieu(x, y)
            self.position.set_label(geo.texte_position(lat, lon))
            self.position.set_visible(True)
        except Exception:
            pass
        if self.mode_trace or self._geste:
            return
        poignee = self._poignee_sous(x, y)
        if poignee:
            self.map.set_cursor_from_name(next(p[3] for p in POIGNEES if p[0] == poignee))
        elif self.selection and self._rect_sous(x, y) == self.selection \
                and self.rects[self.selection]["style"] != "externe":
            self.map.set_cursor_from_name("move")
        else:
            self.map.set_cursor(None)

    # ================================================= vue
    def centrer_sur(self, emprise, marge=0.25):
        if not self.disponible or not emprise:
            return
        if not self.map.get_mapped() or self.map.get_width() < 50:
            GLib.timeout_add(150, lambda: (self.centrer_sur(emprise, marge), False)[1])
            return
        o, s, e, n = emprise
        x0, y0 = m.lonlat_vers_xy(o, n, 0)
        x1, y1 = m.lonlat_vers_xy(e, s, 0)
        larg, haut = self.map.get_width(), self.map.get_height()
        z = math.log2(max(1e-9, min(larg / (256 * max(1e-12, x1 - x0)),
                                    haut / (256 * max(1e-12, y1 - y0))))) - marge
        lon, lat = m.xy_vers_lonlat((x0 + x1) / 2, (y0 + y1) / 2, 0)
        self.vp.set_zoom_level(max(1, min(self.vp.get_max_zoom_level(), z)))
        self.vp.set_location(lat, lon)
