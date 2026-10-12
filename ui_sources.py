# SPDX-License-Identifier: GPL-3.0-or-later
"""Sources de tuiles : stockage, éditeur, petits utilitaires d'interface."""

import json
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

import moteur as m  # noqa: E402
import telechargeur as t  # noqa: E402

DOSSIER_CONFIG = m.dossier_config()
FICHIER_SOURCES = DOSSIER_CONFIG / "sources.json"


def rgba(texte):
    c = Gdk.RGBA()
    c.parse(texte)
    return c


def charger_sources():
    try:
        d = json.loads(FICHIER_SOURCES.read_text(encoding="utf-8"))
        return [t.nouvelle_source(**s) for s in d if s.get("url")]
    except Exception:
        return []


def enregistrer_sources(sources):
    DOSSIER_CONFIG.mkdir(parents=True, exist_ok=True)
    FICHIER_SOURCES.write_text(json.dumps(sources, indent=2, ensure_ascii=False), encoding="utf-8")


def spin(titre, mini, maxi, pas, chiffres, valeur, sous_titre=None):
    r = Adw.SpinRow(title=titre, digits=chiffres,
                    adjustment=Gtk.Adjustment(lower=mini, upper=maxi, step_increment=pas,
                                              page_increment=pas * 10, value=valeur))
    if sous_titre:
        r.set_subtitle(sous_titre)
    return r


# ---------------------------------------------------------------------------
# Éditeur de source
# ---------------------------------------------------------------------------

class EditeurSource(Adw.Window):
    def __init__(self, parent, source, emprise, rappel):
        super().__init__(transient_for=parent, modal=True, title="Source de tuiles",
                         default_width=560, default_height=700)
        self.source = dict(source)
        self.emprise = emprise
        self.rappel = rappel
        echap = Gtk.ShortcutController()
        echap.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("Escape"),
                                            Gtk.CallbackAction.new(lambda *_: self.close() or True)))
        self.add_controller(echap)

        vue = Adw.ToolbarView()
        self.toasts = Adw.ToastOverlay(child=vue)
        self.set_content(self.toasts)
        entete = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        annuler = Gtk.Button(label="Annuler")
        annuler.connect("clicked", lambda *_: self.close())
        ok = Gtk.Button(label="Enregistrer")
        ok.add_css_class("suggested-action")
        ok.connect("clicked", lambda *_: self.valider())
        entete.pack_start(annuler)
        entete.pack_end(ok)
        vue.add_top_bar(entete)

        page = Adw.PreferencesPage()
        vue.set_content(page)

        g = Adw.PreferencesGroup(
            title="Adresse des tuiles",
            description="Variables : {z} {x} {y}, {-y} (TMS), {q} (quadkey), {s} (sous-domaine).\n"
                        "Utilise uniquement des services dont les conditions autorisent "
                        "le téléchargement hors ligne.")
        page.add(g)
        self.e_nom = Adw.EntryRow(title="Nom", text=self.source["nom"])
        self.e_url = Adw.EntryRow(title="Modèle d’URL", text=self.source["url"])
        self.e_sd = Adw.EntryRow(title="Sous-domaines (ex. : abc ou a,b,c)", text=self.source["sous_domaines"])
        for r in (self.e_nom, self.e_url, self.e_sd):
            g.add(r)

        g2 = Adw.PreferencesGroup(title="Caractéristiques")
        page.add(g2)
        self.s_zmin = spin("Zoom minimum", 0, 24, 1, 0, self.source["zoom_min"])
        self.s_zmax = spin("Zoom maximum", 0, 24, 1, 0, self.source["zoom_max"])
        self.c_fmt = Adw.ComboRow(title="Format", model=Gtk.StringList.new(
            ["Détection automatique", "PNG", "JPEG", "WebP", "Vectoriel (PBF)"]))
        self.c_fmt.set_selected(t.FORMATS.index(self.source.get("format", "auto"))
                                if self.source.get("format") in t.FORMATS else 0)
        for r in (self.s_zmin, self.s_zmax, self.c_fmt):
            g2.add(r)

        g3 = Adw.PreferencesGroup(title="En-têtes HTTP",
                                  description="Certains services exigent un Referer ou un User-Agent identifiable.")
        page.add(g3)
        self.e_ua = Adw.EntryRow(title=f"User-Agent (vide : {t.UA_DEFAUT})", text=self.source["user_agent"])
        self.e_ref = Adw.EntryRow(title="Referer", text=self.source["referer"])
        self.e_attr = Adw.EntryRow(title="Attribution / licence", text=self.source["attribution"])
        for r in (self.e_ua, self.e_ref, self.e_attr):
            g3.add(r)

        g4 = Adw.PreferencesGroup()
        page.add(g4)
        self.l_test = Adw.ActionRow(title="Tester la source",
                                    subtitle="Télécharge une tuile au centre de l’emprise")
        b = Gtk.Button(label="Tester", valign=Gtk.Align.CENTER)
        b.connect("clicked", lambda *_: self.tester())
        self.l_test.add_suffix(b)
        g4.add(self.l_test)

    def lire(self):
        s = dict(self.source)
        s.update(nom=self.e_nom.get_text().strip() or "Sans nom",
                 url=t.normaliser_modele(self.e_url.get_text()),
                 sous_domaines=self.e_sd.get_text().strip(),
                 zoom_min=int(self.s_zmin.get_value()), zoom_max=int(self.s_zmax.get_value()),
                 format=t.FORMATS[self.c_fmt.get_selected()],
                 user_agent=self.e_ua.get_text().strip(), referer=self.e_ref.get_text().strip(),
                 attribution=self.e_attr.get_text().strip())
        if s["zoom_max"] < s["zoom_min"]:
            s["zoom_min"], s["zoom_max"] = s["zoom_max"], s["zoom_min"]
        return s

    def tester(self):
        s = self.lire()
        err = t.verifier_modele(s["url"])
        if err:
            self.l_test.set_subtitle(err)
            return
        self.l_test.set_subtitle("Test en cours…")

        def travail():
            try:
                _ok, texte = t.tester_source(s, self.emprise)
            except Exception as e:
                texte = str(e)
            GLib.idle_add(lambda: (self.l_test.set_subtitle(GLib.markup_escape_text(texte)), False)[1])
        threading.Thread(target=travail, daemon=True).start()

    def valider(self):
        s = self.lire()
        err = t.verifier_modele(s["url"])
        if err:
            self.toasts.add_toast(Adw.Toast(title=err, timeout=4))
            return
        self.rappel(s)
        self.close()


