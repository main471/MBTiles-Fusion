#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Construit MBTiles_Fusion_Windows_portable.zip (version portable, Windows 10/11 64 bits).
#
# 1. Installe MSYS2 : https://www.msys2.org
# 2. Ouvre le terminal « MSYS2 UCRT64 ».
# 3. Va dans ce dossier (windows/) et lance :  bash construire_windows.sh
#
# Le script installe les composants (Python, GTK 4, libadwaita, libshumate), assemble
# un dossier autonome, vérifie qu'il démarre sans MSYS2, puis crée le .zip.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

if [ "${MSYSTEM:-}" != "UCRT64" ]; then
  echo "✖ Lance ce script dans le terminal « MSYS2 UCRT64 »." >&2
  exit 1
fi

P=mingw-w64-ucrt-x86_64
echo "— Installation des composants…"
pacman -S --needed --noconfirm \
  $P-python $P-python-gobject $P-python-cairo $P-gtk4 $P-libadwaita $P-adwaita-icon-theme \
  $P-librsvg $P-gdk-pixbuf2 $P-ca-certificates $P-ntldd $P-gcc $P-binutils zip
# HTTP/2 (facultatif)
pacman -S --needed --noconfirm $P-python-httpx $P-python-h2 || echo "⚠ httpx/h2 absents : pas d'option HTTP/2"
CARTE=1
if ! pacman -S --needed --noconfirm $P-libshumate; then
  echo "⚠ libshumate indisponible : la carte sera remplacée par la saisie de coordonnées."
  CARTE=0
fi

PFX=/ucrt64
PYVER=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
DIST=dist/MBTiles_Fusion
RT=$DIST/runtime
rm -rf dist
mkdir -p "$DIST/app" "$RT/bin" "$RT/lib" "$RT/share/glib-2.0" "$RT/share/icons" "$RT/etc/ssl/certs"

echo "— Python $PYVER…"
cp "$PFX/bin/python.exe" "$PFX/bin/pythonw.exe" "$RT/bin/"
cp -r "$PFX/lib/python$PYVER" "$RT/lib/"
LIBPY="$RT/lib/python$PYVER"
rm -rf "$LIBPY"/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3,config-*} "$LIBPY/site-packages"
find "$LIBPY" -name __pycache__ -type d -prune -exec rm -rf {} +
mkdir -p "$LIBPY/site-packages"
for m in gi cairo httpx httpcore h2 hpack hyperframe h11 anyio sniffio idna certifi; do
  [ -d "$PFX/lib/python$PYVER/site-packages/$m" ] && cp -r "$PFX/lib/python$PYVER/site-packages/$m" "$LIBPY/site-packages/"
done

echo "— GTK, libadwaita, libshumate…"
mkdir -p "$RT/lib/girepository-1.0"
cp "$PFX"/lib/girepository-1.0/*.typelib "$RT/lib/girepository-1.0/"
cp -r "$PFX/lib/gdk-pixbuf-2.0" "$RT/lib/"
cp -r "$PFX/share/glib-2.0/schemas" "$RT/share/glib-2.0/"
cp -r "$PFX/share/icons/Adwaita" "$PFX/share/icons/hicolor" "$RT/share/icons/"
cp "$PFX/etc/ssl/certs/ca-bundle.crt" "$RT/etc/ssl/certs/"
for dom in gtk40 libadwaita glib20 gdk-pixbuf libshumate shumate1; do
  f="$PFX/share/locale/fr/LC_MESSAGES/$dom.mo"
  [ -f "$f" ] && { mkdir -p "$RT/share/locale/fr/LC_MESSAGES"; cp "$f" "$RT/share/locale/fr/LC_MESSAGES/"; }
done

echo "— Bibliothèques DLL (dépendances transitives)…"
# Points de départ : Python, modules natifs, bibliothèques citées par les typelibs (chargées
# dynamiquement par PyGObject) et chargeurs d'images.
germes=("$PFX/bin/python.exe")
while IFS= read -r f; do germes+=("$f"); done < <(find "$LIBPY" "$RT/lib/gdk-pixbuf-2.0" -name '*.pyd' -o -name '*.dll')
for t in "$RT"/lib/girepository-1.0/*.typelib; do
  for d in $(strings -n 6 "$t" | grep -iE '^lib[^ /]*\.dll$' | sort -u); do
    for n in ${d//,/ }; do [ -f "$PFX/bin/$n" ] && germes+=("$PFX/bin/$n"); done
  done
done
for d in libgtk-4-1.dll libadwaita-1-0.dll libshumate-1.0-1.dll librsvg-2-2.dll; do
  [ -f "$PFX/bin/$d" ] && germes+=("$PFX/bin/$d")
done
for d in "$PFX"/bin/libgirepository-*.dll; do [ -f "$d" ] && germes+=("$d"); done
for g in "${germes[@]}"; do
  case "$g" in "$PFX/bin/"*) cp -n "$g" "$RT/bin/" ;; esac
  ntldd -R "$g" 2>/dev/null | awk '{print $3}' | tr '\\' '/' | grep -i '/ucrt64/bin/' || true
done | while IFS= read -r dll; do
  u=$(cygpath -u "$dll")
  [ -f "$u" ] && cp -n "$u" "$RT/bin/"
done
# Chargeurs d'images : chemins relatifs (résolus par gdk-pixbuf depuis le dossier runtime),
# pour que le dossier reste déplaçable.
CACHE=$(find "$RT/lib/gdk-pixbuf-2.0" -name loaders.cache | head -1)
if [ -n "$CACHE" ]; then
  python3 - "$CACHE" <<'PY'
import re, sys
p = sys.argv[1]
t = open(p, encoding="utf-8").read()
def rel(m):
    chemin = m.group(1).replace("\\\\", "/").replace("\\", "/")
    i = chemin.find("lib/gdk-pixbuf-2.0/")
    return '"' + chemin[i:].replace("/", "\\\\") + '"' if i >= 0 else m.group(0)
t = re.sub(r'"([^"]*gdk-pixbuf-2\.0[^"]*\.dll)"', rel, t)
open(p, "w", encoding="utf-8").write(t)
PY
fi

echo "— Application…"
cp ../mbtiles_fusion.py ../moteur.py ../telechargeur.py ../geo.py ../ui_carte.py ../ui_sources.py "$DIST/app/"
cp lancer.py "$DIST/app/"
mkdir -p "$RT/share/icons/hicolor/scalable/apps"
cp ../fr.local.MbtilesFusion.svg "$RT/share/icons/hicolor/scalable/apps/"
gtk4-update-icon-cache -q -t -f "$RT/share/icons/hicolor" 2>/dev/null || true

echo "— Lanceur « MBTiles Fusion.exe »…"
for s in 16 32 48 256; do rsvg-convert -w $s -h $s ../fr.local.MbtilesFusion.svg -o "icone_$s.png"; done
python3 faire_icone.py icone.ico icone_16.png icone_32.png icone_48.png icone_256.png
windres lanceur.rc -O coff -o lanceur_rc.o
gcc -O2 -municode -mwindows lanceur.c lanceur_rc.o -o "$DIST/MBTiles Fusion.exe"
rm -f icone_*.png lanceur_rc.o

echo "— Précompilation Python…"
"$RT/bin/python.exe" -m compileall -q "$LIBPY" "$DIST/app" || true

echo "— Vérification hors MSYS2…"
(
  export PATH="$(cygpath -u "$RT/bin"):/c/Windows/System32:/c/Windows"
  unset PYTHONHOME PYTHONPATH
  export GI_TYPELIB_PATH="$(cygpath -w "$RT/lib/girepository-1.0")"
  "$RT/bin/python.exe" -c "
import sys, sqlite3, ssl, gi
gi.require_version('Gtk', '4.0'); gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw
print('Python', sys.version.split()[0], '— GTK', Gtk.get_major_version(), Gtk.get_minor_version(), '— libadwaita OK')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import GdkPixbuf
GdkPixbuf.Pixbuf.new_from_file('../fr.local.MbtilesFusion.svg'); print('Icônes SVG OK')
try:
    gi.require_version('Shumate', '1.0'); from gi.repository import Shumate; print('libshumate OK')
except Exception as e:
    print('libshumate absente :', e)
"
)

echo "— Archive…"
cp LISEZMOI-WINDOWS.txt "$DIST/LISEZMOI.txt"
(cd dist && zip -qr ../MBTiles_Fusion_Windows_portable.zip MBTiles_Fusion)
echo "OK : $(pwd)/MBTiles_Fusion_Windows_portable.zip ($(du -h MBTiles_Fusion_Windows_portable.zip | cut -f1))"
[ $CARTE = 1 ] || echo "⚠ Construite sans libshumate (pas de carte)."
