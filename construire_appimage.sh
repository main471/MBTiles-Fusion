#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Construit MBTiles_Fusion-x86_64.AppImage.
# À lancer sur Ubuntu 24.04 ou plus récent, depuis ce dossier.
# Prérequis : sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-shumate-1.0
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

APP=AppDir
rm -rf "$APP"
mkdir -p "$APP/usr/share/mbtiles-fusion" "$APP/usr/lib/girepository-1.0" "$APP/usr/lib/secours" \
         "$APP/usr/lib/girepository-carte" "$APP/usr/lib/carte" \
         "$APP/usr/share/icons/hicolor/scalable/apps" "$APP/usr/share/applications"

cp AppRun "$APP/"
chmod +x "$APP/AppRun"
cp mbtiles_fusion.py moteur.py telechargeur.py geo.py ui_carte.py ui_sources.py "$APP/usr/share/mbtiles-fusion/"
# HTTP/2 (facultatif) : httpx + h2, en Python pur, dans vendor/
if python3 -m pip --version >/dev/null 2>&1; then
  # Versions figées et vérifiées par empreinte (requirements-http2.txt)
  python3 -m pip install -q --no-compile --require-hashes --only-binary=:all: \
    --target "$APP/usr/share/mbtiles-fusion/vendor" -r requirements-http2.txt \
    ${PIP_BREAK:+--break-system-packages} 2>/dev/null \
    || echo "⚠ httpx/h2 non installés : l'option HTTP/2 sera indisponible"
  rm -rf "$APP/usr/share/mbtiles-fusion/vendor/"*.dist-info "$APP/usr/share/mbtiles-fusion/vendor/bin"
else
  echo "⚠ pip absent : l'option HTTP/2 sera indisponible (sudo apt install python3-pip)"
fi
cp fr.local.MbtilesFusion.svg "$APP/"
cp fr.local.MbtilesFusion.svg "$APP/usr/share/icons/hicolor/scalable/apps/"
cp fr.local.MbtilesFusion.desktop "$APP/"
cp fr.local.MbtilesFusion.desktop "$APP/usr/share/applications/"

GIR=/usr/lib/x86_64-linux-gnu/girepository-1.0
LIB=/usr/lib/x86_64-linux-gnu
copie_lib() { cp -L "$LIB/$1" "$2/" 2>/dev/null || cp -L "/lib/x86_64-linux-gnu/$1" "$2/"; }

# Bibliothèques d'introspection GTK / libadwaita (utilisées seulement si absentes du système)
for t in Adw-1 Gtk-4.0 Gdk-4.0 Gsk-4.0 Graphene-1.0 Pango-1.0 PangoCairo-1.0 \
         HarfBuzz-0.0 GdkPixbuf-2.0 cairo-1.0 freetype2-2.0; do
  cp "$GIR/$t.typelib" "$APP/usr/lib/girepository-1.0/"
done

# libadwaita et ses dépendances peu courantes (utilisées seulement si libadwaita est absente)
for l in libadwaita-1.so.0 libappstream.so.5 libxmlb.so.2 libstemmer.so.0d libyaml-0.so.2; do
  copie_lib "$l" "$APP/usr/lib/secours"
done

# Carte (libshumate) : rarement installée par défaut. Dossiers séparés pour ne
# jamais masquer les versions GTK du système.
cp "$GIR/Shumate-1.0.typelib" "$APP/usr/lib/girepository-carte/"
for l in libshumate-1.0.so.1 libprotobuf-c.so.1; do
  copie_lib "$l" "$APP/usr/lib/carte"
done

# appimagetool et runtime : versions figées, empreintes SHA-256 vérifiées.
OUTIL_URL=https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage
OUTIL_SHA=ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0
RUNTIME_URL=https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64
RUNTIME_SHA=2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d
recuperer() {  # fichier url empreinte
  if [ ! -f "$1" ] || ! echo "$3  $1" | sha256sum -c --status; then
    curl -sSfL -o "$1.tmp" "$2"
    echo "$3  $1.tmp" | sha256sum -c --status || { rm -f "$1.tmp"; echo "✖ Empreinte invalide : $1" >&2; exit 1; }
    mv "$1.tmp" "$1"
  fi
}
recuperer appimagetool "$OUTIL_URL" "$OUTIL_SHA"
recuperer runtime-x86_64 "$RUNTIME_URL" "$RUNTIME_SHA"
chmod +x appimagetool

ARCH=x86_64 ./appimagetool --appimage-extract-and-run --runtime-file runtime-x86_64 \
  "$APP" MBTiles_Fusion-x86_64.AppImage
sha256sum MBTiles_Fusion-x86_64.AppImage > MBTiles_Fusion-x86_64.AppImage.sha256
echo "OK : MBTiles_Fusion-x86_64.AppImage ($(cut -c1-16 MBTiles_Fusion-x86_64.AppImage.sha256)…)"
