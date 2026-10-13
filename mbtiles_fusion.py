#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
MBTiles Fusion — une carte = une source, une base régionale et des zones de zoom,
tracées et modifiées directement sur la carte.

- Les tuiles sont gardées dans un cache (un fichier par source, dans le dossier choisi) :
  modifier la carte ne retélécharge que ce qui manque.
- Le projet est enregistré dans la carte générée : rouvrir le .mbtiles permet de
  modifier, supprimer ou ajouter des zones.

Interface : GTK 4 + libadwaita, carte libshumate.
"""

import json
import os
import queue
import re
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")  # httpx / h2 (HTTP/2)
if os.path.isdir(_VENDOR):
    sys.path.insert(1, _VENDOR)

import geo  # noqa: E402
import moteur as m  # noqa: E402
from moteur import VERSION, nombre, taille_lisible  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

import telechargeur as t  # noqa: E402
from ui_carte import COULEUR_BASE, CarteEditable, charger_css, couleur_zone, pastille  # noqa: E402
from ui_sources import EditeurSource, charger_sources, enregistrer_sources, spin  # noqa: E402

APP_ID = "fr.local.MbtilesFusion"
DOSSIER_CONFIG = m.dossier_config()
FICHIER_PROJET = DOSSIER_CONFIG / "dernier_projet.json"
FICHIER_CONFIG = DOSSIER_CONFIG / "config.json"
LIMITE_FAT32 = 4 * 1024 ** 3 - 1


def projet_vide(source=None):
    return {"version": 3, "nom": "", "source": source, "base": None, "zones": [],
            "externes": [], "sortie": "", "options": {"dedup": True, "decoupe": False}}


def filtre(nom, motifs):
    f = Gtk.FileFilter()
    f.set_name(nom)
    for mo in motifs:
        f.add_pattern(mo)
    liste = Gio.ListStore.new(Gtk.FileFilter)
    liste.append(f)
    return liste


def dims(emprise):
    lx, ly = m.dimensions_km(emprise)
    return f"{lx:.1f} × {ly:.1f} km".replace(".", ",")


class Fenetre(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="MBTiles Fusion")
        self.set_default_size(1280, 820)
        charger_css()

        self.config = self._lire_json(FICHIER_CONFIG) or {}
        self.sources = charger_sources()
        self.projet = projet_vide()
        self.taille_moy = {}
        self.statuts = {}
        self.estimation = None
        self.num_calcul = 0
        self._minuterie_calcul = 0
        self._minuterie_sauvegarde = 0
        self._bloque = False
        self.dl = None
        self.en_cours = False
        self.fermer_apres = False
        self.lignes_zones = {}
        self.passe, self.futur = [], []
        self._groupe, self._t_groupe = None, 0.0
        self._glisse_memo = False
        self.apercu = t.Fournisseur()
        self.file_calcul = queue.Queue()
        threading.Thread(target=self._fil_calcul, daemon=True).start()

        # ---------- structure
        self.toasts = Adw.ToastOverlay()
        vue = Adw.ToolbarView()
        self.toasts.set_child(vue)
        self.set_content(self.toasts)

        entete = Adw.HeaderBar()
        self.titre = Adw.WindowTitle(title="MBTiles Fusion", subtitle="Nouvelle carte")
        entete.set_title_widget(self.titre)
        menu = Gio.Menu()
        menu.append("Nouvelle carte", "win.nouveau")
        menu.append("Ouvrir une carte…", "win.ouvrir")
        menu.append("Ajouter un fichier .mbtiles externe…", "win.externe")
        menu.append("Dossier du cache…", "win.cache")
        menu.append("Gérer le cache…", "win.gerer_cache")
        menu.append("À propos", "win.apropos")
        b = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        b.set_tooltip_text("Menu")
        entete.pack_end(b)
        b_ouvrir = Gtk.Button(icon_name="document-open-symbolic")
        b_ouvrir.set_tooltip_text("Ouvrir une carte générée pour la modifier")
        b_ouvrir.connect("clicked", lambda *_: self.ouvrir_carte())
        entete.pack_start(b_ouvrir)
        self.b_annuler = Gtk.Button(icon_name="edit-undo-symbolic", sensitive=False)
        self.b_annuler.set_tooltip_text("Annuler (Ctrl+Z)")
        self.b_annuler.connect("clicked", lambda *_: self.annuler())
        self.b_retablir = Gtk.Button(icon_name="edit-redo-symbolic", sensitive=False)
        self.b_retablir.set_tooltip_text("Rétablir (Ctrl+Maj+Z)")
        self.b_retablir.connect("clicked", lambda *_: self.retablir())
        entete.pack_start(self.b_annuler)
        entete.pack_start(self.b_retablir)
        vue.add_top_bar(entete)
        for nom, fn in (("nouveau", self.nouvelle_carte), ("ouvrir", self.ouvrir_carte),
                        ("externe", self.ajouter_externe), ("cache", self.choisir_cache),
                        ("gerer_cache", self.gerer_cache), ("apropos", self.a_propos)):
            act = Gio.SimpleAction.new(nom, None)
            act.connect("activate", lambda *_a, f=fn: f())
            self.add_action(act)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, shrink_end_child=False,
                          resize_end_child=False)
        vue.set_content(paned)
        paned.set_start_child(self._construire_carte())
        paned.set_end_child(self._construire_panneau())

        clavier = Gtk.EventControllerKey()
        clavier.connect("key-pressed", self._touche)
        self.add_controller(clavier)

        # ---------- état initial
        d = self._lire_json(FICHIER_PROJET)
        if d and d.get("version") == 3:
            self.charger_projet(d, centrer=True)
        else:
            src = self.sources[0] if self.sources else None
            self.projet = projet_vide(dict(src) if src else None)
            self.tout_rafraichir()
        self.connect("close-request", self._fermeture)

    # ====================================================================== construction
    def _construire_carte(self):
        self.carte = CarteEditable(self._trace_fini, self._rect_modifie, self._selection_carte,
                                   on_recherche=self.rechercher)
        barre = Gtk.Box(spacing=6, halign=Gtk.Align.START, valign=Gtk.Align.START,
                        margin_top=12, margin_start=12)
        self.b_base = Gtk.ToggleButton()
        self.b_base.set_child(Adw.ButtonContent(icon_name="edit-select-all-symbolic", label="Tracer la base"))
        self.b_base.set_tooltip_text("Tracer la base régionale (glisser sur la carte)")
        self.b_zone = Gtk.ToggleButton()
        self.b_zone.set_child(Adw.ButtonContent(icon_name="list-add-symbolic", label="Ajouter une zone"))
        self.b_zone.set_tooltip_text("Tracer une zone de zoom (glisser sur la carte)")
        b_centrer = Gtk.Button(icon_name="zoom-fit-best-symbolic")
        b_centrer.set_tooltip_text("Voir toute la carte")
        b_centrer.connect("clicked", lambda *_: self.centrer())
        for w in (self.b_base, self.b_zone, b_centrer):
            w.add_css_class("osd")
            barre.append(w)
        self.b_base.connect("toggled", lambda b: self._bascule_trace("base", b))
        self.b_zone.connect("toggled", lambda b: self._bascule_trace("zone", b))
        if self.carte.disponible:
            self.carte.add_overlay(barre)
            # Couverture du cache (façon MOBAC) : en bas à gauche, au-dessus de l'échelle
            bas = Gtk.Box(spacing=6, halign=Gtk.Align.START, valign=Gtk.Align.END,
                          margin_start=12, margin_bottom=60)
            self.b_couv = Gtk.ToggleButton()
            self.b_couv.set_child(Adw.ButtonContent(icon_name="view-grid-symbolic", label="Cache"))
            self.b_couv.set_tooltip_text("Montrer les tuiles en cache et celles qui manquent, pour un niveau")
            self.b_couv.add_css_class("osd")
            self.s_couv = Gtk.SpinButton(adjustment=Gtk.Adjustment(lower=0, upper=22, step_increment=1,
                                                                   page_increment=1, value=14),
                                         numeric=True, visible=False, valign=Gtk.Align.CENTER)
            self.s_couv.set_tooltip_text("Niveau de zoom affiché")
            self.s_couv.add_css_class("osd")
            self.b_couv.connect("toggled", lambda *_: self.maj_couverture(auto_niveau=True))
            self.s_couv.connect("value-changed", lambda *_: self.maj_couverture())
            bas.append(self.b_couv)
            bas.append(self.s_couv)
            self.carte.add_overlay(bas)
        return self.carte

    def _construire_panneau(self):
        page = Adw.PreferencesPage(width_request=440)

        # Source
        g = Adw.PreferencesGroup(title="Source", description="Une seule source par carte.")
        page.add(g)
        boutons = Gtk.Box(spacing=2)
        for icone, info, fn in (("list-add-symbolic", "Ajouter une source", self.ajouter_source),
                                ("document-edit-symbolic", "Modifier la source", self.modifier_source),
                                ("document-open-symbolic", "Importer (QGIS ou MOBAC)", self.importer_sources),
                                ("document-save-symbolic", "Exporter les sources (XML)", self.exporter_sources),
                                ("user-trash-symbolic", "Supprimer la source de la liste", self.supprimer_source)):
            b = Gtk.Button(icon_name=icone, valign=Gtk.Align.CENTER)
            b.set_tooltip_text(info)
            b.add_css_class("flat")
            b.connect("clicked", lambda *_a, f=fn: f())
            boutons.append(b)
        g.set_header_suffix(boutons)
        self.c_source = Adw.ComboRow(title="Source")
        # Traité après le signal (jamais pendant) : évite de modifier la liste en cours d'émission.
        self.c_source.connect("notify::selected",
                              lambda *_: GLib.idle_add(lambda: self._source_choisie() and False))
        g.add(self.c_source)

        # Base
        self.g_base = Adw.PreferencesGroup(title="Base régionale")
        page.add(self.g_base)
        self.b_suppr_base = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
        self.b_suppr_base.set_tooltip_text("Supprimer la base")
        self.b_suppr_base.add_css_class("flat")
        self.b_suppr_base.connect("clicked", lambda *_: self.supprimer_base())
        self.g_base.set_header_suffix(self.b_suppr_base)
        self.l_base = Adw.ActionRow(title="Base régionale", activatable=True)
        self.l_base.add_prefix(pastille("pastille-base"))
        self.l_base.connect("activated", lambda *_: self.selectionner("base", centrer=True))
        self.b_edit_base = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER)
        self.b_edit_base.set_tooltip_text("Modifier précisément (coordonnées, zooms)")
        self.b_edit_base.add_css_class("flat")
        self.b_edit_base.connect("clicked", lambda *_: self.modifier_rect("base"))
        self.l_base.add_suffix(self.b_edit_base)
        self.s_bzmin = spin("Zoom minimum", 0, 24, 1, 0, 6)
        self.s_bzmax = spin("Zoom maximum", 0, 24, 1, 0, 12)
        self.s_bzmin.connect("notify::value", lambda *_: self._zoom_base_change("zmin"))
        self.s_bzmax.connect("notify::value", lambda *_: self._zoom_base_change("zmax"))
        for r in (self.l_base, self.s_bzmin, self.s_bzmax):
            self.g_base.add(r)

        # Zones
        self.g_zones = Adw.PreferencesGroup(title="Zones de zoom")
        page.add(self.g_zones)
        ajouter = Gtk.Button(valign=Gtk.Align.CENTER)
        ajouter.set_child(Adw.ButtonContent(icon_name="list-add-symbolic", label="Ajouter"))
        ajouter.add_css_class("flat")
        ajouter.connect("clicked", lambda *_: self.b_zone.set_active(True))
        self.b_ajouter_zone = ajouter
        self.g_zones.set_header_suffix(ajouter)
        self.liste = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.liste.add_css_class("boxed-list")
        vide = Adw.ActionRow(title="Aucune zone", subtitle="Clique « Ajouter une zone » sur la carte")
        vide.add_css_class("dim-label")
        self.liste.set_placeholder(vide)
        self.liste.connect("row-activated", self._ligne_activee)
        self.g_zones.add(self.liste)

        # Fichiers externes (compatibilité)
        self.g_ext = Adw.PreferencesGroup(title="Fichiers externes",
                                          description="Ajoutés au-dessus du zoom max de la base. "
                                                      "Ils l’emportent sur les zones.")
        page.add(self.g_ext)
        self.liste_ext = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.liste_ext.add_css_class("boxed-list")
        self.g_ext.add(self.liste_ext)

        # Carte finale
        g = Adw.PreferencesGroup(title="Carte finale")
        page.add(g)
        self.l_sortie = Adw.ActionRow(title="Aucun fichier", subtitle="Fichier copié sur le téléphone")
        self.l_sortie.add_prefix(Gtk.Image(icon_name="document-save-symbolic"))
        b = Gtk.Button(label="Enregistrer sous…", valign=Gtk.Align.CENTER)
        b.connect("clicked", lambda *_: self.choisir_sortie())
        self.l_sortie.add_suffix(b)
        g.add(self.l_sortie)
        self.l_cache = Adw.ActionRow(title="Dossier du cache")
        self.l_cache.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
        b = Gtk.Button(label="Choisir…", valign=Gtk.Align.CENTER)
        b.connect("clicked", lambda *_: self.choisir_cache())
        self.l_cache.add_suffix(b)
        g.add(self.l_cache)
        self.l_estim = Adw.ActionRow(title="Estimation", subtitle="—")
        self.icone_estim = Gtk.Image(icon_name="drive-harddisk-symbolic")
        self.l_estim.add_prefix(self.icone_estim)
        self.b_mesurer = Gtk.Button(label="Mesurer", valign=Gtk.Align.CENTER, visible=False)
        self.b_mesurer.set_tooltip_text("Télécharge quelques tuiles pour estimer le poids")
        self.b_mesurer.connect("clicked", lambda *_: self.mesurer())
        self.l_estim.add_suffix(self.b_mesurer)
        self.spinner = Gtk.Spinner(valign=Gtk.Align.CENTER, visible=False)
        self.l_estim.add_suffix(self.spinner)
        g.add(self.l_estim)
        self.s_tel = spin("Place libre sur le téléphone", 0, 4096, 0.5, 1,
                          self.config.get("place_tel", 0), "En Go — 0 pour ne pas vérifier")
        self.s_tel.connect("notify::value", lambda *_: (self._config_change(), self.afficher_estimation()))
        g.add(self.s_tel)
        reseau = Adw.ExpanderRow(title="Réseau", subtitle="Requêtes simultanées et débit maximal")
        self.s_conn = spin("Requêtes simultanées", 1, 32, 1, 0, self.config.get("connexions", 6))
        self.s_debit = spin("Débit maximal", 1, 300, 1, 0, self.config.get("debit", 20),
                            "Requêtes par seconde. Réduit automatiquement si le serveur demande de ralentir.")
        for r in (self.s_conn, self.s_debit):
            r.connect("notify::value", lambda *_: (self._config_change(), self.afficher_estimation()))
            reseau.add_row(r)
        g.add(reseau)
        avance = Adw.ExpanderRow(title="Options avancées")
        self.sw_dedup = Adw.SwitchRow(title="Dédoublonner les tuiles identiques",
                                      subtitle="Carte plus légère (mer, forêt…). À désactiver si "
                                               "ton application n’affiche pas la carte.")
        self.sw_decoupe = Adw.SwitchRow(title="Découper en fichiers de 4 Go maximum",
                                        subtitle="Pour les cartes SD en FAT32 : carte_1.mbtiles, carte_2…")
        self.sw_dedup.connect("notify::active", lambda *_: self._option_change("dedup", self.sw_dedup))
        self.sw_decoupe.connect("notify::active", lambda *_: self._option_change("decoupe", self.sw_decoupe))
        self.s_mois = spin("Vérifier les tuiles de plus de", 0, 60, 1, 0, self.config.get("rafraichir_mois", 6),
                           "En mois — 0 : jamais. Comparées au serveur, mises à jour si elles ont changé.")
        self.s_mois.connect("notify::value", lambda *_: (self._config_change(), self.planifier_calcul()))
        self.c_geo = Adw.ComboRow(title="Recherche de lieux",
                                  model=Gtk.StringList.new(list(geo.SERVICES.values())))
        self.c_geo.set_selected(list(geo.SERVICES).index(self.config.get("geocodeur", "nominatim")))
        self.c_geo.connect("notify::selected", lambda *_: self._config_change())
        self.sw_http2 = Adw.SwitchRow(
            title="HTTP/2 (une seule connexion)",
            subtitle=("Pour les serveurs qui limitent le nombre de connexions : monte alors les "
                      "requêtes simultanées (16 à 32). Un peu plus lent à réglage égal."
                      if t.HTTP2_DISPONIBLE else "Indisponible : bibliothèques httpx / h2 absentes."))
        self.sw_http2.set_sensitive(t.HTTP2_DISPONIBLE)
        self.sw_http2.set_active(bool(self.config.get("http2", False)) and t.HTTP2_DISPONIBLE)
        t.UTILISER_HTTP2 = self.sw_http2.get_active()
        self.sw_http2.connect("notify::active", lambda *_: self._config_change())
        for r in (self.sw_dedup, self.sw_decoupe, self.s_mois, self.c_geo, self.sw_http2):
            avance.add_row(r)
        g.add(avance)

        # Action
        g = Adw.PreferencesGroup()
        page.add(g)
        boite = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        ligne = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        self.b_generer = Gtk.Button(label="Générer la carte")
        self.b_generer.add_css_class("suggested-action")
        self.b_generer.add_css_class("pill")
        self.b_generer.connect("clicked", lambda *_: self.lancer())
        self.b_pause = Gtk.Button(label="Pause", visible=False)
        self.b_pause.add_css_class("pill")
        self.b_pause.connect("clicked", lambda *_: self.basculer_pause())
        self.b_arret = Gtk.Button(label="Arrêter", visible=False)
        self.b_arret.add_css_class("pill")
        self.b_arret.add_css_class("destructive-action")
        self.b_arret.connect("clicked", lambda *_: self.arreter())
        for w in (self.b_generer, self.b_pause, self.b_arret):
            ligne.append(w)
        boite.append(ligne)
        self.barre = Gtk.ProgressBar(visible=False)
        boite.append(self.barre)
        self.etat = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.etat.add_css_class("dim-label")
        boite.append(self.etat)
        g.add(boite)

        # Journal
        g = Adw.PreferencesGroup()
        page.add(g)
        exp = Adw.ExpanderRow(title="Journal")
        self.journal_buf = Gtk.TextBuffer()
        texte = Gtk.TextView(buffer=self.journal_buf, editable=False, cursor_visible=False,
                             monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                             top_margin=8, bottom_margin=8, left_margin=12, right_margin=12)
        exp.add_row(Gtk.ScrolledWindow(min_content_height=160, child=texte))
        g.add(exp)

        self.editables = [self.g_base, self.g_zones, self.g_ext, self.c_source]
        return page

    # ====================================================================== utilitaires
    @staticmethod
    def _lire_json(chemin):
        try:
            return json.loads(Path(chemin).read_text(encoding="utf-8"))
        except Exception:
            return None

    def _config_change(self):
        self.config.update(place_tel=self.s_tel.get_value(), connexions=int(self.s_conn.get_value()),
                           debit=self.s_debit.get_value())
        if hasattr(self, "sw_http2"):
            self.config["http2"] = self.sw_http2.get_active()
            t.UTILISER_HTTP2 = self.config["http2"]
        if hasattr(self, "s_mois"):
            self.config["rafraichir_mois"] = int(self.s_mois.get_value())
            self.config["geocodeur"] = list(geo.SERVICES)[self.c_geo.get_selected()]
        self._ecrire_config()

    def _ecrire_config(self):
        try:
            DOSSIER_CONFIG.mkdir(parents=True, exist_ok=True)
            FICHIER_CONFIG.write_text(json.dumps(self.config, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def journal(self, texte):
        def _():
            self.journal_buf.insert(self.journal_buf.get_end_iter(), texte + "\n")
            return False
        GLib.idle_add(_)

    def toast(self, texte, bouton=None, action=None):
        to = Adw.Toast(title=texte, timeout=5)
        if bouton:
            to.set_button_label(bouton)
            to.connect("button-clicked", lambda *_: action())
        self.toasts.add_toast(to)

    def confirmer(self, titre, texte, oui, rappel, destructif=False):
        d = Adw.AlertDialog(heading=titre, body=texte) if hasattr(Adw, "AlertDialog") else \
            Adw.MessageDialog(transient_for=self, heading=titre, body=texte)
        d.add_response("non", "Annuler")
        d.add_response("oui", oui)
        d.set_response_appearance("oui", Adw.ResponseAppearance.DESTRUCTIVE if destructif
                                  else Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("oui")
        d.set_close_response("non")
        d.connect("response", lambda _d, r: rappel() if r == "oui" else None)
        d.present(self) if hasattr(Adw, "AlertDialog") else d.present()

    def erreur(self, texte):
        d = Adw.AlertDialog(heading="Erreur", body=texte) if hasattr(Adw, "AlertDialog") else \
            Adw.MessageDialog(transient_for=self, heading="Erreur", body=texte)
        d.add_response("ok", "OK")
        d.present(self) if hasattr(Adw, "AlertDialog") else d.present()

    def _enchainer(self, etapes, final):
        if not etapes:
            final()
            return
        titre, texte, bouton = etapes[0]
        self.confirmer(titre, texte, bouton, lambda: self._enchainer(etapes[1:], final))

    def _ouvrir(self, titre, rappel, filtres):
        d = Gtk.FileDialog(title=titre, filters=filtres)
        d.set_initial_folder(Gio.File.new_for_path(str(self.dossier_courant())))

        def fin(dlg, res):
            try:
                f = dlg.open_finish(res)
            except GLib.Error:
                return
            rappel(f.get_path())
        d.open(self, None, fin)

    def dossier_courant(self):
        s = self.projet.get("sortie")
        if s and Path(s).parent.exists():
            return Path(s).parent
        return Path.home()

    # ====================================================================== source et cache
    def source(self):
        return self.projet.get("source")

    def chemin_cache(self):
        d = self.config.get("dossier_cache")
        s = self.source()
        return t.chemin_cache(d, s) if d and s else None

    def maj_liste_sources(self):
        self._bloque = True
        noms = [s["nom"] for s in self.sources] or ["Aucune source — ajoute-en une"]
        # Remplacer le modèle seulement s'il change : un set_model pendant un signal
        # de la liste déroulante fait planter GTK.
        if noms != getattr(self, "_noms_sources", None):
            self._noms_sources = noms
            self.c_source.set_model(Gtk.StringList.new(noms))
        url = (self.source() or {}).get("url")
        idx = next((i for i, s in enumerate(self.sources) if s["url"] == url), 0)
        if self.c_source.get_selected() != idx:
            self.c_source.set_selected(idx)
        self._bloque = False
        s = self.source()
        if s:
            hote = s["url"].split("//", 1)[-1].split("/")[0]
            self.c_source.set_subtitle(GLib.markup_escape_text(f"{hote} · Z{s['zoom_min']}–Z{s['zoom_max']}"))
        else:
            self.c_source.set_subtitle("")

    def _source_choisie(self):
        if self._bloque or not self.sources:
            return
        i = self.c_source.get_selected()
        if not 0 <= i < len(self.sources):
            return
        s = self.sources[i]
        if self.source() and self.source()["url"] == s["url"]:
            return
        self._memoriser()
        self.projet["source"] = dict(s)
        self._borner_zooms()
        self.tout_rafraichir()

    def _remplacer_source(self, ancienne_url, nouvelle):
        """Après modification d'une source : met à jour le projet s'il l'utilise."""
        if self.source() and self.source()["url"] == ancienne_url:
            self.projet["source"] = dict(nouvelle)
            self._borner_zooms()

    def _enregistrer_sources(self):
        try:
            enregistrer_sources(self.sources)
        except OSError as e:
            self.erreur(f"Impossible d’enregistrer les sources : {e}")

    def ajouter_source(self):
        def ok(s):
            self.sources.append(s)
            self._enregistrer_sources()
            if not self.source():
                self.projet["source"] = dict(s)
            self.tout_rafraichir()
        EditeurSource(self, t.nouvelle_source(), self._emprise_test(), ok).present()

    def _emprise_test(self):
        b = self.projet.get("base")
        return tuple(b["emprise"]) if b else None

    def modifier_source(self):
        idx = self.c_source.get_selected()
        if not self.sources or idx >= len(self.sources):
            self.ajouter_source()
            return
        ancienne = self.sources[idx]

        def ok(nouvelle):
            self.sources[idx] = nouvelle
            self._enregistrer_sources()
            self._remplacer_source(ancienne["url"], nouvelle)
            self.tout_rafraichir()
        EditeurSource(self, ancienne, self._emprise_test(), ok).present()

    def supprimer_source(self):
        idx = self.c_source.get_selected()
        if not self.sources or idx >= len(self.sources):
            return
        s = self.sources[idx]

        def oui():
            self.sources.remove(s)
            self._enregistrer_sources()
            self.maj_liste_sources()
        self.confirmer("Retirer la source ?", f"« {s['nom']} » sera retirée de la liste. "
                       "Le cache et les cartes existantes ne sont pas touchés.", "Retirer", oui, True)

    def importer_sources(self):
        def ok(chemin):
            try:
                lues = t.importer_sources(chemin)
            except Exception as e:
                self.erreur(f"Import impossible : {e}")
                return
            urls = {s["url"] for s in self.sources}
            nouvelles = [s for s in lues if s["url"] not in urls]
            self.sources.extend(nouvelles)
            self._enregistrer_sources()
            if nouvelles and not self.source():
                self.projet["source"] = dict(nouvelles[0])
            self.tout_rafraichir()
            texte = f"{len(nouvelles)} source(s) importée(s)"
            if len(lues) > len(nouvelles):
                texte += f", {len(lues) - len(nouvelles)} déjà présente(s)"
            self.toast(texte)
            for s in nouvelles:
                err = t.verifier_modele(s["url"])
                if err:
                    self.journal(f"⚠ Source « {s['nom']} » : {err}")
        self._ouvrir("Importer des sources", ok,
                     filtre("Sources QGIS ou MOBAC (.xml, .qgs, .qgz, .qlr)",
                            ("*.xml", "*.XML", "*.qgs", "*.qgz", "*.qlr")))

    def exporter_sources(self):
        if not self.sources:
            self.erreur("Aucune source à exporter.")
            return
        d = Gtk.FileDialog(title="Exporter les sources", filters=filtre("Sources (.xml)", ("*.xml",)),
                           initial_name="sources-mbtiles-fusion.xml")
        d.set_initial_folder(Gio.File.new_for_path(str(self.dossier_courant())))

        def fin(dlg, res):
            try:
                f = dlg.save_finish(res)
            except GLib.Error:
                return
            chemin = f.get_path()
            if not chemin.lower().endswith(".xml"):
                chemin += ".xml"
            try:
                n = t.exporter_sources(self.sources, chemin)
            except Exception as e:
                self.erreur(f"Export impossible : {e}")
                return
            self.toast(f"{n} source(s) exportée(s) : {Path(chemin).name}")
        d.save(self, None, fin)

    def choisir_cache(self, puis=None):
        d = Gtk.FileDialog(title="Dossier du cache de tuiles")
        if self.config.get("dossier_cache") and Path(self.config["dossier_cache"]).exists():
            d.set_initial_folder(Gio.File.new_for_path(self.config["dossier_cache"]))

        def fin(dlg, res):
            try:
                f = dlg.select_folder_finish(res)
            except GLib.Error:
                return
            self.config["dossier_cache"] = f.get_path()
            self._ecrire_config()
            self.tout_rafraichir()
            if puis:
                puis()
        d.select_folder(self, None, fin)

    # ====================================================================== zooms
    def _borner_zooms(self):
        """Garde base et zones dans la plage de la source."""
        s, b = self.source(), self.projet.get("base")
        if not s or not b:
            return
        zs_min, zs_max = s["zoom_min"], s["zoom_max"]
        b["zmax"] = max(zs_min, min(zs_max, b["zmax"]))
        b["zmin"] = max(zs_min, min(b["zmax"], b["zmin"]))
        for z in self.projet["zones"]:
            z["zmax"] = max(min(b["zmax"] + 1, zs_max), min(zs_max, z["zmax"]))

    def _zoom_base_change(self, quoi):
        if self._bloque or not self.projet.get("base"):
            return
        self._memoriser("zooms-base")
        b = self.projet["base"]
        zmin, zmax = int(self.s_bzmin.get_value()), int(self.s_bzmax.get_value())
        if quoi == "zmin" and zmin > zmax:
            zmax = zmin
        if quoi == "zmax" and zmax < zmin:
            zmin = zmax
        b["zmin"], b["zmax"] = zmin, zmax
        self._borner_zooms()
        self._maj_base_ui()
        self._maj_bornes_zones()
        self.planifier_calcul()

    def _maj_bornes_zones(self):
        """Bornes des boutons +/− de chaque zone : de Z(base max + 1) au max de la source."""
        b, s = self.projet.get("base"), self.source()
        if not b or not s:
            return
        self._bloque = True
        for zone in self.projet["zones"]:
            ligne = self.lignes_zones.get(zone["id"])
            if ligne:
                adj = ligne["spin"].get_adjustment()
                adj.set_lower(min(b["zmax"] + 1, s["zoom_max"]))
                adj.set_upper(s["zoom_max"])
                ligne["spin"].set_value(zone["zmax"])
                self._sous_titre_zone(zone)
        self._bloque = False
        self.g_zones.set_description(f"Zoom de départ : Z{b['zmax'] + 1} (zoom max de la base + 1). "
                                     f"Zoom max limité à Z{s['zoom_max']} par la source.")

    def _zoom_zone_change(self, zone, spin_btn):
        if self._bloque:
            return
        self._memoriser(f"zoom-{zone['id']}")
        zone["zmax"] = int(spin_btn.get_value())
        self._sous_titre_zone(zone)
        self.planifier_calcul()

    # ====================================================================== affichage
    def tout_rafraichir(self):
        self.maj_liste_sources()
        self.carte.definir_fond(self.apercu, self.source(), self.chemin_cache())
        self._maj_rects()
        self._maj_base_ui()
        self._reconstruire_zones()
        self._reconstruire_externes()
        self._maj_sortie()
        d = self.config.get("dossier_cache")
        self.l_cache.set_subtitle(GLib.markup_escape_text(d) if d else "À choisir : les tuiles y sont gardées")
        opts = self.projet.setdefault("options", {"dedup": True, "decoupe": False})
        self._bloque = True
        self.sw_dedup.set_active(bool(opts.get("dedup", True)))
        self.sw_decoupe.set_active(bool(opts.get("decoupe", False)))
        self._bloque = False
        self.b_edit_base.set_visible(bool(self.projet.get("base")))
        self.planifier_calcul()

    def _maj_rects(self):
        rects = []
        b = self.projet.get("base")
        if b:
            rects.append(("base", b["emprise"], COULEUR_BASE, "base"))
        for zone in self.projet["zones"]:
            rects.append((zone["id"], zone["emprise"], couleur_zone(zone["couleur"]), "zone"))
        for i, ext in enumerate(self.projet.get("externes", [])):
            a = m.analyse_en_cache(ext)
            if a:
                rects.append((f"ext:{i}", a["emprise"], "#77767b", "externe"))
        self.carte.definir_rects(rects)
        self.b_base.set_visible(not b)
        self.b_zone.set_visible(bool(b))
        self.b_ajouter_zone.set_sensitive(bool(b))

    def _maj_base_ui(self):
        b, s = self.projet.get("base"), self.source()
        self._bloque = True
        self.b_suppr_base.set_visible(bool(b))
        self.s_bzmin.set_visible(bool(b))
        self.s_bzmax.set_visible(bool(b))
        if not b:
            self.l_base.set_title("Aucune base")
            self.l_base.set_subtitle("Clique « Tracer la base » sur la carte")
        else:
            lo, hi = (s["zoom_min"], s["zoom_max"]) if s else (0, 22)
            for r in (self.s_bzmin, self.s_bzmax):
                r.get_adjustment().set_lower(lo)
                r.get_adjustment().set_upper(hi)
            self.s_bzmin.set_value(b["zmin"])
            self.s_bzmax.set_value(b["zmax"])
            self.l_base.set_title(f"Base régionale · Z{b['zmin']}–Z{b['zmax']}")
            self.l_base.set_subtitle("\n".join([dims(b["emprise"])] + self.statuts.get("base", [])))
        self._bloque = False

    def _sous_titre_zone(self, zone):
        ligne = self.lignes_zones.get(zone["id"])
        b = self.projet.get("base")
        if not ligne or not b:
            return
        ligne["row"].set_title(GLib.markup_escape_text(zone["nom"]))
        lignes = [f"Z{b['zmax'] + 1}–Z{zone['zmax']} · {dims(zone['emprise'])}"]
        lignes += self.statuts.get(zone["id"], ["…"])
        ligne["row"].set_subtitle("\n".join(lignes))

    def _reconstruire_zones(self):
        self.liste.remove_all()
        self.lignes_zones = {}
        s = self.source()
        for zone in self.projet["zones"]:
            row = Adw.ActionRow(activatable=True)
            row.add_prefix(pastille(f"pastille-{zone['couleur'] % 8}"))
            sb = Gtk.SpinButton(adjustment=Gtk.Adjustment(lower=0, upper=s["zoom_max"] if s else 22,
                                                          step_increment=1, page_increment=1,
                                                          value=zone["zmax"]),
                                valign=Gtk.Align.CENTER, numeric=True)
            sb.set_tooltip_text("Zoom maximum de la zone")
            sb.connect("value-changed", lambda w, z=zone: self._zoom_zone_change(z, w))
            row.add_suffix(Gtk.Label(label="Z max", css_classes=["dim-label", "caption"]))
            row.add_suffix(sb)
            for icone, info, fn in (("document-edit-symbolic", "Modifier (nom, coordonnées, zoom)",
                                     self.modifier_rect),
                                    ("user-trash-symbolic", "Supprimer la zone", self.supprimer_zone)):
                b = Gtk.Button(icon_name=icone, valign=Gtk.Align.CENTER)
                b.set_tooltip_text(info)
                b.add_css_class("flat")
                b.connect("clicked", lambda *_a, f=fn, z=zone: f(z["id"]))
                row.add_suffix(b)
            row.zone_id = zone["id"]
            self.liste.append(row)
            self.lignes_zones[zone["id"]] = {"row": row, "spin": sb}
            self._sous_titre_zone(zone)
        n = len(self.projet["zones"])
        self.g_zones.set_title(f"Zones de zoom ({n})" if n else "Zones de zoom")
        self._maj_bornes_zones()
        if not self.projet.get("base"):
            self.g_zones.set_description("Trace d’abord la base régionale.")
        self._selection_liste(self.carte.selection)

    def _reconstruire_externes(self):
        self.liste_ext.remove_all()
        ext = self.projet.get("externes", [])
        self.g_ext.set_visible(bool(ext))
        for i, chemin in enumerate(ext):
            row = Adw.ActionRow(title=GLib.markup_escape_text(Path(chemin).name))
            row.add_prefix(pastille("pastille-externe"))
            row.set_subtitle("\n".join(self.statuts.get(f"ext:{i}", ["Analyse…"])))
            b = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            b.set_tooltip_text("Retirer de la carte")
            b.add_css_class("flat")
            b.connect("clicked", lambda *_a, c=chemin: self.retirer_externe(c))
            row.add_suffix(b)
            self.liste_ext.append(row)

    def _maj_sortie(self):
        s = self.projet.get("sortie")
        if s:
            self.l_sortie.set_title(GLib.markup_escape_text(Path(s).name))
            self.l_sortie.set_subtitle(GLib.markup_escape_text(str(Path(s).parent)))
            self.titre.set_subtitle(Path(s).name)
        else:
            self.l_sortie.set_title("Aucun fichier")
            self.l_sortie.set_subtitle("Fichier copié sur le téléphone")
            self.titre.set_subtitle("Nouvelle carte")

    # ====================================================================== sélection
    def selectionner(self, cle, centrer=False):
        self.carte.selectionner(cle)
        self._selection_liste(cle)
        if centrer and cle:
            emp = self._emprise(cle)
            if emp:
                self.carte.centrer_sur(emp, marge=0.6)

    def _emprise(self, cle):
        if cle == "base" and self.projet.get("base"):
            return self.projet["base"]["emprise"]
        z = self._zone(cle)
        return z["emprise"] if z else None

    def _zone(self, cle):
        return next((z for z in self.projet["zones"] if z["id"] == cle), None)

    def _selection_liste(self, cle):
        self._bloque = True
        ligne = self.lignes_zones.get(cle)
        if ligne:
            self.liste.select_row(ligne["row"])
        else:
            self.liste.unselect_all()
        self._bloque = False

    def _selection_carte(self, cle):
        self._selection_liste(cle)

    def _ligne_activee(self, _liste, row):
        self.selectionner(getattr(row, "zone_id", None), centrer=True)

    def centrer(self):
        emp = [self.projet["base"]["emprise"]] if self.projet.get("base") else []
        emp += [z["emprise"] for z in self.projet["zones"]]
        if emp:
            self.carte.centrer_sur(m.union_emprises([tuple(e) for e in emp]))

    # ====================================================================== édition sur la carte
    def _bascule_trace(self, mode, bouton):
        if self._bloque:
            return
        if bouton.get_active():
            if not self.source():
                self._bloque = True
                bouton.set_active(False)
                self._bloque = False
                self.toast("Choisis d’abord une source")
                return
            autre = self.b_zone if mode == "base" else self.b_base
            self._bloque = True
            autre.set_active(False)
            self._bloque = False
            self.carte.tracer(mode)
        else:
            self.carte.tracer(None)

    def _fin_trace_boutons(self):
        self._bloque = True
        self.b_base.set_active(False)
        self.b_zone.set_active(False)
        self._bloque = False

    def _trace_fini(self, mode, emprise):
        self._fin_trace_boutons()
        self._memoriser()
        s = self.source()
        if mode == "base":
            zmax = max(s["zoom_min"], min(s["zoom_max"], 12))
            self.projet["base"] = {"emprise": list(emprise), "zmin": max(s["zoom_min"], min(zmax, 6)),
                                   "zmax": zmax}
            self._borner_zooms()
            self.tout_rafraichir()
            self.selectionner("base")
            self.toast("Base tracée — règle ses zooms à droite, puis ajoute des zones")
        else:
            b = self.projet["base"]
            utilises = {z["couleur"] for z in self.projet["zones"]}
            couleur = next((i for i in range(64) if i not in utilises), len(self.projet["zones"]))
            numero = 1 + max([0] + [int(z["nom"].split()[-1]) for z in self.projet["zones"]
                                    if z["nom"].startswith("Zone ") and z["nom"].split()[-1].isdigit()])
            zone = {"id": uuid.uuid4().hex[:8], "nom": f"Zone {numero}", "emprise": list(emprise),
                    "zmax": min(s["zoom_max"], b["zmax"] + 3), "couleur": couleur}
            self.projet["zones"].append(zone)
            self.tout_rafraichir()
            self.selectionner(zone["id"])
        self.planifier_sauvegarde()

    def _rect_modifie(self, cle, emprise, fini):
        if not self._glisse_memo and self._emprise(cle) and list(self._emprise(cle)) != list(emprise):
            self._memoriser()
            self._glisse_memo = True
        if fini:
            self._glisse_memo = False
        if cle == "base":
            self.projet["base"]["emprise"] = list(emprise)
            if fini:
                self._maj_base_ui()
        else:
            zone = self._zone(cle)
            if not zone:
                return
            zone["emprise"] = list(emprise)
            self._sous_titre_zone(zone)
        if fini:
            self.planifier_calcul()
            self.planifier_sauvegarde()

    def supprimer_zone(self, cle):
        zone = self._zone(cle)
        if not zone:
            return
        self._memoriser()
        self.projet["zones"].remove(zone)
        self.tout_rafraichir()
        self.selectionner(None)
        self.planifier_sauvegarde()
        self.toast(f"{zone['nom']} supprimée", "Annuler", self.annuler)

    def supprimer_base(self):
        if not self.projet.get("base"):
            return

        def oui():
            self._memoriser()
            self.projet["base"] = None
            self.projet["zones"] = []
            self.tout_rafraichir()
            self.planifier_sauvegarde()
        n = len(self.projet["zones"])
        texte = "La base sera supprimée" + (f", ainsi que les {n} zones." if n else ".") + \
                " Le cache n’est pas touché."
        self.confirmer("Supprimer la base ?", texte, "Supprimer", oui, True)

    def _touche(self, _ctrl, val, _code, etat):
        if isinstance(self.get_focus(), (Gtk.Text, Gtk.Entry, Gtk.SpinButton)):
            return False
        ctrl = bool(etat & Gdk.ModifierType.CONTROL_MASK)
        maj = bool(etat & Gdk.ModifierType.SHIFT_MASK)
        if ctrl and val in (Gdk.KEY_z, Gdk.KEY_Z):
            self.retablir() if maj else self.annuler()
            return True
        if ctrl and val in (Gdk.KEY_y, Gdk.KEY_Y):
            self.retablir()
            return True
        if val == Gdk.KEY_Escape:
            if self.carte.mode_trace:
                self.carte.tracer(None)
                self._fin_trace_boutons()
            else:
                self.selectionner(None)
            return True
        if val in (Gdk.KEY_Delete, Gdk.KEY_BackSpace) and not self.en_cours:
            sel = self.carte.selection
            if sel == "base":
                self.supprimer_base()
                return True
            if self._zone(sel):
                self.supprimer_zone(sel)
                return True
        return False

    # ====================================================================== annuler / rétablir
    def _memoriser(self, groupe=None):
        """Mémorise l'état avant une modification. Les changements rapprochés d'un même
        groupe (clics répétés sur +/−) ne forment qu'une étape."""
        maintenant = time.monotonic()
        if groupe and groupe == self._groupe and maintenant - self._t_groupe < 1.5:
            self._t_groupe = maintenant
            return
        self._groupe, self._t_groupe = groupe, maintenant
        self.passe.append(json.dumps(self.projet))
        del self.passe[:-100]
        self.futur.clear()
        self._maj_historique()

    def _maj_historique(self):
        self.b_annuler.set_sensitive(bool(self.passe) and not self.en_cours)
        self.b_retablir.set_sensitive(bool(self.futur) and not self.en_cours)

    def _restaurer(self, depuis, vers):
        if not depuis or self.en_cours:
            return
        vers.append(json.dumps(self.projet))
        selection = self.carte.selection
        self.projet = json.loads(depuis.pop())
        self._groupe = None
        self.tout_rafraichir()
        if self._emprise(selection):
            self.selectionner(selection)
        self._maj_historique()
        self.planifier_sauvegarde()

    def annuler(self):
        self._restaurer(self.passe, self.futur)

    def retablir(self):
        self._restaurer(self.futur, self.passe)

    def _option_change(self, cle, interrupteur):
        if self._bloque:
            return
        self._memoriser()
        self.projet.setdefault("options", {})[cle] = interrupteur.get_active()
        self.planifier_calcul()
        self.planifier_sauvegarde()

    # ====================================================================== édition précise
    def modifier_rect(self, cle):
        """Fenêtre : nom (zone), coordonnées exactes, zooms."""
        est_base = cle == "base"
        objet = self.projet.get("base") if est_base else self._zone(cle)
        s = self.source()
        if not objet or not s:
            return
        b = self.projet["base"]
        fen = Adw.Window(transient_for=self, modal=True, default_width=460, default_height=620,
                         title="Base régionale" if est_base else objet["nom"])
        vue = Adw.ToolbarView()
        fen.set_content(vue)
        entete = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        annuler = Gtk.Button(label="Annuler")
        annuler.connect("clicked", lambda *_: fen.close())
        appliquer = Gtk.Button(label="Appliquer")
        appliquer.add_css_class("suggested-action")
        entete.pack_start(annuler)
        entete.pack_end(appliquer)
        vue.add_top_bar(entete)
        page = Adw.PreferencesPage()
        vue.set_content(page)

        e_nom = None
        if not est_base:
            g = Adw.PreferencesGroup()
            page.add(g)
            e_nom = Adw.EntryRow(title="Nom", text=objet["nom"])
            g.add(e_nom)

        o, s_, e, n = objet["emprise"]
        g = Adw.PreferencesGroup(title="Emprise", description="Degrés décimaux (WGS84).")
        page.add(g)
        champs = {}
        for nom, val, lo, hi in (("Nord", n, -85, 85), ("Sud", s_, -85, 85),
                                 ("Ouest", o, -180, 180), ("Est", e, -180, 180)):
            champs[nom] = spin(nom, lo, hi, 0.001, 6, val)
            g.add(champs[nom])
        l_info = Adw.ActionRow(title="Centre")
        l_info.add_css_class("property")
        g.add(l_info)

        g = Adw.PreferencesGroup(title="Niveaux de zoom")
        page.add(g)
        if est_base:
            z1 = spin("Zoom minimum", s["zoom_min"], s["zoom_max"], 1, 0, objet["zmin"])
            z2 = spin("Zoom maximum", s["zoom_min"], s["zoom_max"], 1, 0, objet["zmax"])
            g.add(z1)
            g.add(z2)
        else:
            z1 = None
            z2 = spin("Zoom maximum", min(b["zmax"] + 1, s["zoom_max"]), s["zoom_max"], 1, 0, objet["zmax"],
                      f"Départ : Z{b['zmax'] + 1} (zoom max de la base + 1)")
            g.add(z2)

        def lire():
            nn, ss, oo, ee = (champs[k].get_value() for k in ("Nord", "Sud", "Ouest", "Est"))
            return (min(oo, ee), min(ss, nn), max(oo, ee), max(ss, nn))

        def info(*_):
            emp = lire()
            lat, lon = (emp[1] + emp[3]) / 2, (emp[0] + emp[2]) / 2
            l_info.set_subtitle(f"{geo.texte_position(lat, lon)}\n{dims(emp)}")
        for c in champs.values():
            c.connect("notify::value", info)
        info()

        def ok(*_):
            emp = lire()
            if emp[2] - emp[0] < 1e-5 or emp[3] - emp[1] < 1e-5:
                self.toast("Emprise vide : vérifie les coordonnées")
                return
            self._memoriser()
            objet["emprise"] = [round(v, 6) for v in emp]
            if est_base:
                objet["zmin"] = int(min(z1.get_value(), z2.get_value()))
                objet["zmax"] = int(max(z1.get_value(), z2.get_value()))
                self._borner_zooms()
            else:
                objet["zmax"] = int(z2.get_value())
                if e_nom.get_text().strip():
                    objet["nom"] = e_nom.get_text().strip()
            fen.close()
            self.tout_rafraichir()
            self.selectionner(cle, centrer=True)
            self.planifier_sauvegarde()
        appliquer.connect("clicked", ok)
        fen.present()

    # ====================================================================== gestion du cache
    def gerer_cache(self):
        d = self.config.get("dossier_cache")
        if not d or not os.path.isdir(d):
            self.choisir_cache(puis=self.gerer_cache)
            return
        fen = Adw.Window(transient_for=self, modal=True, default_width=560, default_height=520,
                         title="Cache de tuiles")
        vue = Adw.ToolbarView()
        fen.set_content(vue)
        vue.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        vue.set_content(page)
        groupe = Adw.PreferencesGroup(title="Fichiers du cache", description=d)
        page.add(groupe)
        liste = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        liste.add_css_class("boxed-list")
        groupe.add(liste)
        actuel = self.chemin_cache()

        def remplir():
            liste.remove_all()
            fichiers = sorted(str(f) for f in Path(d).glob("*.mbtiles"))
            total = 0
            for f in fichiers:
                try:
                    st = m.statistiques_cache(f)
                except Exception:
                    continue
                total += st["taille"]
                row = Adw.ActionRow(title=GLib.markup_escape_text(st["source"].get("nom") or Path(f).name))
                date = st["date"][:10] if st["date"] else "?"
                row.set_subtitle(f"{taille_lisible(st['taille'])} · {nombre(st['tuiles'])} tuiles · "
                                 f"dernier téléchargement : {date}"
                                 + ("\nSource de la carte ouverte" if f == actuel else ""))
                if f == actuel and self.projet.get("base"):
                    b = Gtk.Button(label="Nettoyer", valign=Gtk.Align.CENTER)
                    b.set_tooltip_text("Garder seulement les tuiles de la carte ouverte")
                    b.connect("clicked", lambda *_a, c=f: nettoyer(c))
                    row.add_suffix(b)
                b = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
                b.set_tooltip_text("Supprimer ce cache")
                b.add_css_class("flat")
                b.connect("clicked", lambda *_a, c=f, nom=row.get_title(): supprimer(c, nom))
                row.add_suffix(b)
                liste.append(row)
            groupe.set_title(f"Fichiers du cache · {taille_lisible(total)}" if fichiers else "Cache vide")

        def supprimer(chemin, nom):
            def oui():
                self.apercu.fermer_caches()
                try:
                    os.remove(chemin)
                except OSError as e:
                    self.erreur(str(e))
                m.oublier(chemin)
                remplir()
                self.planifier_calcul()
                self.journal(f"Cache supprimé : {Path(chemin).name}")
            self.confirmer("Supprimer ce cache ?", f"Toutes les tuiles de « {nom} » seront effacées. "
                           "Les cartes déjà générées ne sont pas touchées.", "Supprimer", oui, True)

        def nettoyer(chemin):
            niveaux = m.niveaux_projet(self.projet)

            def oui():
                fen.set_sensitive(False)
                self.apercu.fermer_caches()

                def travail():
                    try:
                        n, libere = m.purger_cache(chemin, niveaux, journal=self.journal)
                        texte = f"{nombre(n)} tuiles supprimées, {taille_lisible(max(0, libere))} libérés"
                    except Exception as e:
                        texte = f"Nettoyage impossible : {e}"
                    GLib.idle_add(fin, texte)

                def fin(texte):
                    fen.set_sensitive(True)
                    remplir()
                    self.toast(texte)
                    self.planifier_calcul()
                    return False
                threading.Thread(target=travail, daemon=True).start()
            self.confirmer("Nettoyer le cache ?",
                           "Seules les tuiles de la base et des zones de la carte ouverte sont gardées. "
                           "Celles de tes autres cartes avec cette source seront retéléchargées si besoin.",
                           "Nettoyer", oui, True)
        remplir()
        fen.present()

    # ====================================================================== recherche
    def rechercher(self, texte):
        texte = (texte or "").strip()
        if not texte:
            return
        pos = geo.analyser_saisie(texte)
        if pos:
            lat, lon, prec = pos
            zoom = 16 if prec <= 10 else 14 if prec <= 100 else 12 if prec <= 1000 else 10
            self.carte.montrer_point(lat, lon, zoom)
            self.toast(geo.texte_position(lat, lon))
            return
        service = list(geo.SERVICES)[self.c_geo.get_selected()]
        client = t.ClientHTTP(delai=15)

        def travail():
            try:
                res = geo.rechercher_lieu(client, t.entetes_source({}), texte, service)
                err = None
            except Exception as e:
                res, err = [], str(e)
            client.fermer()
            GLib.idle_add(fin, res, err)

        def fin(res, err):
            if err:
                self.toast(f"Recherche impossible : {err}")
            elif not res:
                self.toast("Aucun lieu trouvé")
            elif len(res) == 1:
                aller(res[0])
            else:
                choix(res)
            return False

        def aller(r):
            if r["emprise"]:
                self.carte.centrer_sur(r["emprise"], marge=0.3)
                self.carte.montrer_point(r["lat"], r["lon"])
            else:
                self.carte.montrer_point(r["lat"], r["lon"], 14)

        def choix(res):
            pop = Gtk.Popover(autohide=True)
            pop.set_parent(self.carte.recherche)
            boite = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            boite.add_css_class("boxed-list")
            for r in res:
                row = Adw.ActionRow(title=GLib.markup_escape_text(r["nom"][:90]), activatable=True)
                row.set_subtitle(geo.texte_position(r["lat"], r["lon"]))
                row.connect("activated", lambda *_a, rr=r: (pop.popdown(), aller(rr)))
                boite.append(row)
            pop.set_child(Gtk.ScrolledWindow(child=boite, min_content_height=min(360, 64 * len(res)),
                                             min_content_width=420))
            pop.connect("closed", lambda p: GLib.idle_add(lambda: (p.unparent(), False)[1]))
            pop.popup()
        threading.Thread(target=travail, daemon=True).start()

    # ====================================================================== couverture du cache
    def maj_couverture(self, auto_niveau=False):
        if not self.carte.disponible or getattr(self, "_bloque_couv", False):
            return
        actif = self.b_couv.get_active()
        self.s_couv.set_visible(actif)
        if not actif:
            self.carte.definir_couverture(self.apercu, None, {}, None)
            return
        s = self.source()
        if s:
            self.s_couv.get_adjustment().set_upper(s["zoom_max"])
        if auto_niveau:
            b = self.projet.get("base")
            if b:
                self._bloque_couv = True
                self.s_couv.set_value(max([b["zmax"]] + [z["zmax"] for z in self.projet["zones"]]))
                self._bloque_couv = False
        self.carte.definir_couverture(self.apercu, self.chemin_cache(), m.niveaux_projet(self.projet),
                                      int(self.s_couv.get_value()))

    # ====================================================================== fichiers externes
    def ajouter_externe(self):
        def ok(chemin):
            if chemin in self.projet["externes"] or chemin == self.projet.get("sortie"):
                return
            self._memoriser()
            self.projet["externes"].append(chemin)
            self.tout_rafraichir()
            self.planifier_sauvegarde()
        self._ouvrir("Fichier .mbtiles externe", ok, filtre("Cartes MBTiles", ("*.mbtiles",)))

    def retirer_externe(self, chemin):
        self._memoriser()
        self.projet["externes"].remove(chemin)
        self.tout_rafraichir()
        self.planifier_sauvegarde()

    # ====================================================================== calculs en arrière-plan
    def planifier_calcul(self):
        if self._minuterie_calcul:
            GLib.source_remove(self._minuterie_calcul)
        self._minuterie_calcul = GLib.timeout_add(300, self._lancer_calcul)

    def _lancer_calcul(self):
        self._minuterie_calcul = 0
        self.num_calcul += 1
        self.spinner.set_visible(True)
        self.spinner.start()
        s = self.source()
        moy = self.taille_moy.get(s["url"]) if s else None
        self.file_calcul.put((self.num_calcul, json.loads(json.dumps(self.projet)), self.chemin_cache(), moy,
                              self.limite_date(), self.mois_rafraichir()))
        return False

    def mois_rafraichir(self):
        return int(self.config.get("rafraichir_mois", 6))

    def limite_date(self):
        mois = self.mois_rafraichir()
        return time.time() - mois * 30.44 * 86400 if mois > 0 else None

    def _fil_calcul(self):
        while True:
            travail = self.file_calcul.get()
            while not self.file_calcul.empty():  # seul le plus récent compte
                travail = self.file_calcul.get()
            num, projet, cache, moy, limite, mois = travail
            res = {"statuts": {}, "estimation": None, "erreur": None}
            try:
                b = projet.get("base")
                if b:
                    st = m.statut_rectangle(cache, b["emprise"], b["zmin"], b["zmax"], limite)
                    moy = moy or (st["octets"] / st["presentes"] if st["presentes"] else None)
                    res["statuts"]["base"] = m.texte_statut_cache(st, moy, mois)
                    for zone in projet["zones"]:
                        st = m.statut_rectangle(cache, zone["emprise"], b["zmax"] + 1, zone["zmax"], limite)
                        res["statuts"][zone["id"]] = m.texte_statut_cache(st, moy, mois)
                for i, ext in enumerate(projet.get("externes", [])):
                    try:
                        a = m.analyser(ext)
                        utile = b["zmax"] + 1 if b else None
                        lignes = [("✓ " if not m.niveaux_incomplets(a, utile) else "⚠ ")
                                  + m.texte_completude(a, utile)]
                        inv = m.texte_invalides(a, utile)
                        if inv:
                            lignes.append("⚠ " + inv + " — exclues")
                        res["statuts"][f"ext:{i}"] = lignes
                    except Exception as e:
                        res["statuts"][f"ext:{i}"] = [f"Illisible : {e}"]
                if b:
                    res["estimation"] = m.estimer_projet(cache, projet, moy, limite)
            except Exception as e:
                res["erreur"] = str(e)
            GLib.idle_add(self._calcul_fini, num, res)

    def _calcul_fini(self, num, res):
        if num != self.num_calcul:
            return False
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.statuts = res["statuts"]
        self.estimation = res["estimation"]
        self._maj_base_ui()
        for zone in self.projet["zones"]:
            self._sous_titre_zone(zone)
        self._reconstruire_externes()
        if any(k.startswith("ext:") for k in self.statuts):
            self._maj_rects()
        if res["erreur"]:
            self.l_estim.set_subtitle(f"Calcul impossible : {GLib.markup_escape_text(res['erreur'])}")
        else:
            self.afficher_estimation()
        if self.carte.disponible and self.b_couv.get_active():
            self.maj_couverture()
        return False

    def problemes_taille(self):
        r = self.estimation
        if not r:
            return []
        msgs = []
        if r["poids"] > LIMITE_FAT32 and not self.projet.get("options", {}).get("decoupe"):
            msgs.append("Dépasse 4 Go : une carte SD en FAT32 ne l’acceptera pas "
                        "(voir « Découper » dans les options avancées)")
        place = self.s_tel.get_value() * 1024 ** 3
        if place > 0 and r["poids"] * 1.03 > place:
            msgs.append(f"Plus grand que la place libre indiquée ({taille_lisible(place)})")
        return msgs

    def afficher_estimation(self):
        for c in ("success", "warning"):
            self.icone_estim.remove_css_class(c)
        r = self.estimation
        if not self.projet.get("base"):
            self.l_estim.set_subtitle("Trace une base pour commencer")
            self.b_mesurer.set_visible(False)
            return
        if not r:
            return
        if r["poids_incertain"]:
            poids = "poids inconnu"
        elif r["borne_haute"]:
            poids = f"≤ {taille_lisible(r['poids'])}"
        else:
            poids = f"≈ {taille_lisible(r['poids'])}"
        lignes = [f"{poids} · {nombre(r['tuiles'])} tuiles"
                  + (f" · Z{r['zoom_min']}–Z{r['zoom_max']}" if r["zoom_min"] is not None else "")]
        if r["borne_haute"] and not r["poids_incertain"]:
            lignes[0] += " (avant dédoublonnage)"
        if r["fichiers"] > 1:
            lignes.append(f"→ environ {r['fichiers']} fichiers de 4 Go maximum")
        a_faire = r["a_telecharger"] + r["a_verifier"]
        if a_faire:
            debit = min(self.s_debit.get_value(), self.s_conn.get_value() * 8)
            morceaux = []
            if r["a_telecharger"]:
                morceaux.append(f"{nombre(r['a_telecharger'])} à télécharger")
            if r["a_verifier"]:
                morceaux.append(f"{nombre(r['a_verifier'])} à vérifier")
            lignes.append(f"Tuiles : {', '.join(morceaux)} · ≈ {m.duree_lisible(a_faire / max(1, debit))}")
        else:
            lignes.append("✓ Tout est en cache et à jour")
        if not self.config.get("dossier_cache"):
            lignes.append("Choisis le dossier du cache")
        msgs = self.problemes_taille()
        lignes += ["⚠ " + x for x in msgs]
        place = self.s_tel.get_value() * 1024 ** 3
        if not msgs and place > 0 and not r["poids_incertain"]:
            lignes.append(f"✓ Rentre sur le téléphone (reste ≈ {taille_lisible(place - r['poids'])})")
        self.l_estim.set_subtitle("\n".join(lignes))
        self.icone_estim.add_css_class("warning" if msgs else "success")
        self.b_mesurer.set_visible(r["poids_incertain"])

    def mesurer(self):
        s, b = self.source(), self.projet.get("base")
        if not s or not b:
            return
        zmax = max([b["zmax"]] + [z["zmax"] for z in self.projet["zones"]])
        cible = self.projet["zones"][-1]["emprise"] if self.projet["zones"] else b["emprise"]
        self.b_mesurer.set_sensitive(False)

        def travail():
            try:
                moy = t.echantillonner(s, tuple(cible), zmax)
            except Exception:
                moy = None
            GLib.idle_add(fin, moy)

        def fin(moy):
            self.b_mesurer.set_sensitive(True)
            if moy:
                self.taille_moy[s["url"]] = moy
                self.planifier_calcul()
            else:
                self.toast("Mesure impossible : aucune tuile reçue")
            return False
        threading.Thread(target=travail, daemon=True).start()

    # ====================================================================== projet
    def planifier_sauvegarde(self):
        if self._minuterie_sauvegarde:
            GLib.source_remove(self._minuterie_sauvegarde)
        self._minuterie_sauvegarde = GLib.timeout_add(800, self._sauver)

    def _sauver(self):
        self._minuterie_sauvegarde = 0
        try:
            DOSSIER_CONFIG.mkdir(parents=True, exist_ok=True)
            FICHIER_PROJET.write_text(json.dumps(self.projet, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return False

    def charger_projet(self, p, centrer=False):
        p = dict(projet_vide(), **p)
        src = p.get("source")
        if src and src.get("url"):
            locale = next((s for s in self.sources if s["url"] == src["url"]), None)
            if not locale:
                locale = t.nouvelle_source(**{k: v for k, v in src.items() if k != "id" and v is not None})
                self.sources.append(locale)
                self._enregistrer_sources()
            p["source"] = dict(locale)
        self.projet = p
        self._borner_zooms()
        self.carte.selection = None
        self.tout_rafraichir()
        if centrer:
            GLib.timeout_add(200, lambda: (self.centrer(), False)[1])

    def nouvelle_carte(self):
        if self.en_cours:
            return
        self._memoriser()
        self.projet = projet_vide(self.source())
        self.etat.set_text("")
        self.tout_rafraichir()
        self.planifier_sauvegarde()

    def ouvrir_carte(self):
        if self.en_cours:
            return

        def ok(chemin):
            p = m.lire_projet(chemin)
            if not p:
                self.confirmer("Carte sans projet",
                               f"{Path(chemin).name} n’a pas été créée par MBTiles Fusion 3.\n"
                               "Tu peux l’ajouter à la carte actuelle comme fichier externe.",
                               "Ajouter comme externe",
                               lambda: (self.projet["externes"].append(chemin), self.tout_rafraichir()))
                return
            parties = self._parties(chemin)
            if parties:  # carte découpée : on travaille sur le nom commun « carte.mbtiles »
                chemin_base = str(Path(chemin).with_name(re.sub(r"_\d+$", "", Path(chemin).stem) + ".mbtiles"))
            else:
                chemin_base, parties = chemin, [chemin]
            p["sortie"] = chemin_base
            self.passe, self.futur = [], []
            self.charger_projet(p, centrer=True)
            self._maj_historique()
            self.planifier_sauvegarde()
            self.toast(f"{Path(chemin).name} ouverte : tu peux modifier la base et les zones")
            for f in parties:
                self._recuperer_tuiles(f)
        self._ouvrir("Ouvrir une carte", ok, filtre("Cartes MBTiles", ("*.mbtiles",)))

    @staticmethod
    def _parties(chemin):
        """Fichiers frères d'une carte découpée (carte_1, carte_2…), ou [] si non découpée."""
        try:
            con = m.ouvrir_lecture(chemin)
            try:
                partie = m.lire_metadata(con).get("mbtf_partie")
            finally:
                con.close()
        except Exception:
            return []
        if not partie:
            return []
        p = Path(chemin)
        racine = re.sub(r"_\d+$", "", p.stem)
        return sorted(str(f) for f in p.parent.glob(f"{racine}_*.mbtiles")
                      if f.stem[len(racine) + 1:].isdigit())

    def _recuperer_tuiles(self, carte):
        """Remplit le cache avec les tuiles de la carte ouverte (évite de les retélécharger)."""
        cache, src = self.chemin_cache(), self.source()
        if not cache or not src:
            if not self.config.get("dossier_cache"):
                self.journal("Astuce : choisis le dossier du cache pour réutiliser les tuiles de cette carte.")
            return
        projet = json.loads(json.dumps(self.projet))

        def travail():
            try:
                n = m.importer_vers_cache(carte, cache, projet, t.source_json(src))
                if n:
                    self.journal(f"{nombre(n)} tuiles récupérées depuis {Path(carte).name} dans le cache")
            except Exception as e:
                self.journal(f"⚠ Récupération des tuiles impossible : {e}")
            GLib.idle_add(lambda: (self.planifier_calcul(), False)[1])
        threading.Thread(target=travail, daemon=True).start()

    def choisir_sortie(self, puis=None):
        s = self.projet.get("sortie")
        d = Gtk.FileDialog(title="Carte finale", filters=filtre("Cartes MBTiles", ("*.mbtiles",)),
                           initial_name=Path(s).name if s else "carte.mbtiles")
        d.set_initial_folder(Gio.File.new_for_path(str(self.dossier_courant())))

        def fin(dlg, res):
            try:
                f = dlg.save_finish(res)
            except GLib.Error:
                return
            chemin = f.get_path()
            if not chemin.endswith(".mbtiles"):
                chemin += ".mbtiles"
            self.projet["sortie"] = chemin
            self._maj_sortie()
            self.planifier_sauvegarde()
            if puis:
                puis()
        d.save(self, None, fin)

    def a_propos(self):
        texte = ("Une source, une base régionale et des zones de zoom tracées sur la carte. "
                 "Les tuiles sont gardées en cache ; la carte générée peut être rouverte et modifiée.")
        if hasattr(Adw, "AboutDialog"):
            Adw.AboutDialog(application_name="MBTiles Fusion", version=VERSION,
                            application_icon=APP_ID, comments=texte,
                            license_type=Gtk.License.GPL_3_0,
                            developer_name="Contributeurs MBTiles Fusion").present(self)
        else:
            Adw.AboutWindow(transient_for=self, application_name="MBTiles Fusion", version=VERSION,
                            application_icon=APP_ID, comments=texte,
                            license_type=Gtk.License.GPL_3_0,
                            developer_name="Contributeurs MBTiles Fusion").present()

    # ====================================================================== génération
    def lancer(self):
        if self.en_cours:
            return
        if not self.source():
            self.toast("Choisis une source")
            return
        if not self.projet.get("base"):
            self.toast("Trace d’abord la base régionale")
            return
        if not self.config.get("dossier_cache"):
            self.choisir_cache(puis=self.lancer)
            return
        if not self.projet.get("sortie"):
            self.choisir_sortie(puis=self.lancer)
            return
        manquants = [e for e in self.projet["externes"] if not os.path.exists(e)]
        if manquants:
            self.erreur("Fichiers externes introuvables :\n" + "\n".join(Path(x).name for x in manquants))
            return
        etapes = []
        r = self.estimation
        if r and r["a_telecharger"] > 500_000:
            etapes.append(("Volume important", f"{nombre(r['a_telecharger'])} tuiles à télécharger. "
                           "Vérifie que les conditions du service l’autorisent.", "Continuer"))
        alertes = [f"• {Path(e).name} : " + " ; ".join(self.statuts.get(f"ext:{i}", []))
                   for i, e in enumerate(self.projet["externes"])
                   if any(x.startswith("⚠") for x in self.statuts.get(f"ext:{i}", []))]
        if alertes:
            etapes.append(("Fichiers externes à vérifier", "\n".join(alertes)
                           + "\n\nLes tuiles invalides seront exclues.", "Générer quand même"))
        taille = self.problemes_taille()
        if taille:
            etapes.append(("Carte trop lourde ?", "\n".join(taille), "Générer quand même"))
        if os.path.exists(self.projet["sortie"]):
            etapes.append(("Remplacer la carte ?", f"{Path(self.projet['sortie']).name} sera régénérée "
                           "avec la base et les zones actuelles.", "Remplacer"))
        self._enchainer(etapes, self._executer)

    def _executer(self):
        projet = json.loads(json.dumps(self.projet))
        projet["nom"] = projet.get("nom") or Path(projet["sortie"]).stem
        cache, source = self.chemin_cache(), dict(self.source())
        niveaux = m.niveaux_projet(projet)
        self.en_cours = True
        self.apercu.hors_ligne = True
        self._maj_historique()
        for w in self.editables:
            w.set_sensitive(False)
        self.b_base.set_sensitive(False)
        self.b_zone.set_sensitive(False)
        self.b_generer.set_visible(False)
        self.b_pause.set_visible(True)
        self.b_pause.set_label("Pause")
        self.b_arret.set_visible(True)
        self.barre.set_visible(True)
        self.barre.set_fraction(0)
        self.etat.set_text("Préparation…")
        self.journal("— Génération —")
        debut = time.time()
        self.dl = t.Telechargement(source, niveaux, cache, connexions=int(self.s_conn.get_value()),
                                   debit=self.s_debit.get_value(), journal=self.journal,
                                   etat=lambda st: GLib.idle_add(self._progression, st),
                                   limite_date=self.limite_date())
        dl = self.dl

        def travail():
            try:
                st = dl.executer()
                if st.get("interrompu"):
                    GLib.idle_add(self._fin, None, st.get("message") or "Interrompu : relance pour reprendre "
                                  "(les tuiles reçues sont gardées en cache)", None)
                    return
                GLib.idle_add(self._etape_assemblage)
                r = m.generer_projet(cache, projet, projet["sortie"], journal=self.journal,
                                     progression=lambda f: GLib.idle_add(
                                         lambda: (self.barre.set_fraction(f), False)[1]))
                r["duree"] = time.time() - debut
                r["echecs"] = st["erreurs"]
                r["maj"] = st.get("maj", 0)
                GLib.idle_add(self._fin, r, None, None)
            except Exception as e:
                self.journal(f"✖ Erreur : {e}")
                GLib.idle_add(self._fin, None, None, str(e))
        threading.Thread(target=travail, daemon=True).start()

    def _progression(self, st):
        if not self.dl:
            return False
        faites = st["deja"] + st["ok"] + st["absentes"] + st["erreurs"]
        total = max(1, st["total"])
        self.barre.set_fraction(min(1.0, faites / total))
        ecoule = max(0.1, time.monotonic() - st["debut"])
        recues = st.get("recues", st["ok"] + st["absentes"] + st["erreurs"])
        moyen = recues / ecoule
        instant = st.get("instant") or moyen
        # Temps restant : rythme récent si assez de recul, sinon moyen.
        rythme_reste = instant if ecoule > 10 and instant > 0 else moyen
        reste = (st["a_faire"] - recues) / rythme_reste if rythme_reste > 0 else 0
        morceaux = [f"{nombre(recues)} / {nombre(st['a_faire'])} tuiles", f"Z{st['zoom']}",
                    taille_lisible(st["octets"]), f"{instant:.0f} tuiles/s"]
        if st.get("ko_s"):
            morceaux.append(f"{st['ko_s']:.0f} Ko/s")
        if st.get("latence_ms"):
            morceaux.append(f"réponse {st['latence_ms']} ms")
        if reste > 0 and not st["fini"]:
            morceaux.append(f"reste ≈ {m.duree_lisible(reste)}")
        texte = " · ".join(morceaux)
        details = []
        if st.get("maj") or st.get("inchangees"):
            details.append(f"{nombre(st['maj'] + st['inchangees'])} vérifiées, {nombre(st['maj'])} mises à jour")
        if st["absentes"]:
            details.append(f"{nombre(st['absentes'])} absentes du serveur")
        if st["erreurs"]:
            details.append(f"{nombre(st['erreurs'])} en échec")
        cause = st.get("cause") if recues > 30 and not st["fini"] else ""
        if cause == "serveur":
            details.append(f"Limité par le serveur : débit réduit à {st['debit_actuel']:.0f} req/s")
        elif cause == "reglage":
            details.append(f"Limité par ton réglage ({st['debit_reglage']:.0f} req/s, voir Réseau)")
        elif cause == "parallelisme":
            details.append("Limité par le nombre de requêtes simultanées (voir Réseau)")
        elif cause == "ralentit":
            details.append("Le serveur répond plus lentement qu'au début")
        elif cause == "relance":
            details.append("Nouvel essai des tuiles en échec")
        elif cause == "fin":
            details.append("Dernières tuiles")
        if details:
            texte += "\n" + " · ".join(details)
        if st.get("pause"):
            texte = "En pause — " + texte
        self.etat.set_text(texte)
        return False

    def _etape_assemblage(self):
        self.b_pause.set_visible(False)
        self.b_arret.set_visible(False)
        self.barre.set_fraction(0)
        self.etat.set_text("Assemblage de la carte…")
        return False

    def basculer_pause(self):
        if not self.dl:
            return
        if self.dl.pause.is_set():
            self.dl.pause.clear()
            self.b_pause.set_label("Pause")
        else:
            self.dl.pause.set()
            self.b_pause.set_label("Reprendre")

    def arreter(self):
        if self.dl:
            self.dl.arret.set()
            self.dl.pause.clear()
            self.b_pause.set_sensitive(False)
            self.b_arret.set_sensitive(False)
            self.etat.set_text("Arrêt en cours… (les tuiles reçues restent en cache)")

    def _fin(self, resultat, message, erreur):
        self.dl = None
        self.en_cours = False
        self.apercu.hors_ligne = False
        if self.carte.disponible:
            self.carte._relancer_vue()  # recharge les tuiles non affichées pendant le téléchargement
        self._maj_historique()
        for w in self.editables:
            w.set_sensitive(True)
        self.b_base.set_sensitive(True)
        self.b_zone.set_sensitive(True)
        self.b_generer.set_visible(True)
        self.b_pause.set_visible(False)
        self.b_pause.set_sensitive(True)
        self.b_arret.set_visible(False)
        self.b_arret.set_sensitive(True)
        self.barre.set_visible(False)
        if erreur:
            self.etat.set_text("")
            self.erreur(erreur)
        elif message:
            self.etat.set_text(message)
            self.toast("Génération interrompue")
        else:
            fichiers = resultat.get("fichiers", [self.projet["sortie"]])
            noms = Path(fichiers[0]).name if len(fichiers) == 1 else f"{len(fichiers)} fichiers ({Path(fichiers[0]).name}…)"
            texte = f"{noms} · {taille_lisible(resultat['taille'])} · {resultat['duree']:.0f} s"
            if resultat.get("maj"):
                texte += f"\n{nombre(resultat['maj'])} tuiles mises à jour (changées sur le serveur)"
            est = self.estimation
            if est and not est["poids_incertain"]:
                if est["borne_haute"]:
                    gain = 100 * (1 - resultat["taille"] / max(1, est["poids"]))
                    if gain >= 1:
                        texte += f"\nDédoublonnage : {gain:.0f} % gagnés (≈ {taille_lisible(est['poids'])} sans)"
                else:
                    ecart = 100 * (est["poids"] - resultat["taille"]) / max(1, resultat["taille"])
                    texte += f"\nEstimation : {taille_lisible(est['poids'])} ({ecart:+.1f} %)".replace(".", ",")
            if resultat["manquantes"]:
                texte += f"\n⚠ {nombre(resultat['manquantes'])} tuiles n’ont pas pu être téléchargées : " \
                         "relance pour réessayer"
            self.etat.set_text(texte)
            self.toast("Carte générée")
        self._sauver()
        self.planifier_calcul()
        if self.fermer_apres:
            self.close()
        return False

    def _fermeture(self, *_):
        if self.en_cours and not self.fermer_apres:
            def oui():
                self.fermer_apres = True
                self.arreter()
            self.confirmer("Génération en cours", "Elle sera interrompue. Les tuiles déjà reçues "
                           "restent en cache.", "Quitter", oui, True)
            return True
        self._sauver()
        self.apercu.fermer_caches()  # enregistre les dernières tuiles vues
        return False


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)

    def do_activate(self):
        fen = self.props.active_window or Fenetre(self)
        fen.present()


def verifier_versions():
    """GTK 4.10+ et libadwaita 1.4+ sont nécessaires (Ubuntu 24.04 ou plus récent)."""
    gtk = (Gtk.get_major_version(), Gtk.get_minor_version())
    adw = (Adw.get_major_version(), Adw.get_minor_version())
    if gtk < (4, 10) or adw < (1, 4):
        return (f"Versions trop anciennes : GTK {gtk[0]}.{gtk[1]}, libadwaita {adw[0]}.{adw[1]}.\n"
                "MBTiles Fusion nécessite Ubuntu 24.04 ou plus récent.")
    return None


if __name__ == "__main__":
    probleme = verifier_versions()
    if probleme:
        print(probleme, file=sys.stderr)
        sys.exit(1)
    Gtk.Window.set_default_icon_name(APP_ID)
    sys.exit(Application().run(sys.argv))
