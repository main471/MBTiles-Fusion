# SPDX-License-Identifier: GPL-3.0-or-later
"""
Point d'entrée de la version portable Windows.
Arborescence :  MBTiles Fusion.exe   app\\   runtime\\ (Python + GTK)   donnees\\ (réglages, journal)
Tout reste dans le dossier : copiable sur une clé USB.
"""
import os
import sys
import traceback

ICI = os.path.dirname(os.path.abspath(__file__))
RACINE = os.path.dirname(ICI)
RT = os.path.join(RACINE, "runtime")

os.environ["GI_TYPELIB_PATH"] = os.path.join(RT, "lib", "girepository-1.0")
os.environ["XDG_DATA_DIRS"] = os.path.join(RT, "share")
os.environ["GSETTINGS_SCHEMA_DIR"] = os.path.join(RT, "share", "glib-2.0", "schemas")
_chargeurs = os.path.join(RT, "lib", "gdk-pixbuf-2.0", "2.10.0", "loaders.cache")
if os.path.exists(_chargeurs):
    os.environ["GDK_PIXBUF_MODULE_FILE"] = _chargeurs
_ca = os.path.join(RT, "etc", "ssl", "certs", "ca-bundle.crt")
if os.path.exists(_ca):
    os.environ["SSL_CERT_FILE"] = _ca
os.environ["PATH"] = os.path.join(RT, "bin") + os.pathsep + os.environ.get("PATH", "")
os.environ.setdefault("MBTF_CONFIG", os.path.join(RACINE, "donnees"))
os.makedirs(os.environ["MBTF_CONFIG"], exist_ok=True)

# pythonw n'a pas de console : les messages vont dans donnees\journal.txt
_journal = open(os.path.join(os.environ["MBTF_CONFIG"], "journal.txt"), "a", encoding="utf-8", buffering=1)
sys.stdout = sys.stderr = _journal

if hasattr(os, "add_dll_directory"):
    os.add_dll_directory(os.path.join(RT, "bin"))
sys.path.insert(0, ICI)

try:
    import runpy
    sys.argv = [os.path.join(ICI, "mbtiles_fusion.py")] + sys.argv[1:]
    runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, "MBTiles Fusion n’a pas pu démarrer.\nDétails : dossier « donnees », fichier journal.txt",
            "MBTiles Fusion", 0x10)
    except Exception:
        pass
    sys.exit(1)
