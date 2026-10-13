# MBTiles Fusion

Offline map builder for field apps (ATAK, AlpineQuest, and other MBTiles readers).

Draw a **regional base map** and any number of **high-zoom zones** directly on the map.
MBTiles Fusion downloads the tiles from a tile source you provide and assembles everything
into **one MBTiles file**: low zooms for the whole region, high zooms only where you need them.

<!-- Add a screenshot: docs/screenshot.png -->
<!-- ![MBTiles Fusion](docs/screenshot.png) -->

> The user interface is currently in **French**.

## Features

- **One screen**: draw the base (min/max zoom) and zoom zones (max zoom only) as rectangles,
  move them with corner handles, edit coordinates precisely, undo/redo.
- **Accurate estimate** before generating: tiles, size on disk, tiles to download, tiles to refresh.
- **Local tile cache** per source: tiles are never downloaded twice. Tiles older than N months are
  re-checked with conditional requests (`If-Modified-Since`) and updated only if changed.
- **Cache coverage overlay** (like MOBAC) per zoom level.
- **Fast, polite downloader**: keep-alive connections, adaptive rate limiting (backs off on
  HTTP 429/503, honours `Retry-After`), retry pass for failed tiles, automatic stop when the server
  keeps refusing. Live diagnostics: tiles/s, KB/s, server response time, and the current bottleneck.
- **Completeness check** and invalid-tile detection (HTML error pages, empty or corrupted images).
- **Deduplication** of identical tiles (sea, empty areas) and optional **split into 4 GB parts**.
- **Traceability**: the project (source, zones, zooms, dates) is stored inside the MBTiles file,
  so a generated map can be reopened and edited.
- **Search** by place name (Nominatim or IGN) or by coordinates: decimal, DMS or **MGRS**.
- **Source import** from QGIS (XYZ connections export, `.qgs`, `.qgz`, `.qlr`) and MOBAC
  (`customMapSource`). URL templates `{z} {x} {y} {-y} {q} {s}` are supported.
- **Source export** to an XML file (QGIS XYZ connections format): share your sources, re-import them
  in MBTiles Fusion with all settings, or load them in QGIS.

## Download

See [Releases](../../releases):

- **Linux**: `MBTiles_Fusion-x86_64.AppImage`. Make it executable and run it.
  Requires GTK 4 and Python 3 with PyGObject (`sudo apt install python3-gi gir1.2-gtk-4.0`).
  libadwaita and libshumate are bundled if missing.
- **Windows 10/11**: `MBTiles_Fusion_Windows_portable.zip`. Unzip anywhere (USB stick works)
  and run `MBTiles Fusion.exe`. No installation.

## Quick start

1. Add a tile source (URL template), or import [`examples/sources-ign.xml`](examples/sources-ign.xml).
2. Draw the base map and set its zoom range.
3. Add zoom zones and set their max zoom.
4. Check the estimate, choose the output file, click **Générer la carte**.
5. Copy the `.mbtiles` to your phone (ATAK: `atak/imagery/`, AlpineQuest: its maps folder).

## Tile sources and legal use

**MBTiles Fusion ships with no tile source.** You are responsible for complying with the terms of
use of the sources you add. Many providers (Google, Bing, Esri, OpenStreetMap's own tile servers…)
**forbid bulk downloading or offline storage** of their tiles.

Sources that allow it include:

- **IGN Géoplateforme** (France): open data under Etalab Open Licence 2.0, see
  [`examples/sources-ign.xml`](examples/sources-ign.xml). Respect their fair-use limits.
- Commercial providers whose plan explicitly allows offline use.
- Tiles you render yourself from OpenStreetMap data (ODbL).

Network settings (*Réseau*): start with 6–16 simultaneous requests and 20–60 requests/s.
Higher values increase the risk of being blocked.

## Compatibility notes

- Output follows the [MBTiles 1.3](https://github.com/mapbox/mbtiles-spec) specification.
- With deduplication enabled, `tiles` is an SQL view over `map` + `images` (the layout used by
  common MBTiles tools). If a reader does not display the map, regenerate it with deduplication off
  (*Options avancées*).

## Building from source

Linux (Ubuntu 24.04+):

```bash
sudo apt install python3-gi python3-pip gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-shumate-1.0
python3 mbtiles_fusion.py                    # run from source
PIP_BREAK=1 bash construire_appimage.sh      # build the AppImage
```

Windows: open the **MSYS2 UCRT64** terminal, then `bash windows/construire_windows.sh`
(see `windows/LISEZMOI-WINDOWS.txt`). Both builds also run on GitHub Actions
(manual run, or automatically when a `v*` tag is pushed).

## Project layout

| File | Role |
|---|---|
| `mbtiles_fusion.py` | Main window (GTK 4 / libadwaita) |
| `ui_carte.py` | Editable map (libshumate) |
| `ui_sources.py` | Source storage and editor |
| `telechargeur.py` | Sources, HTTP client, rate limiter, downloader, map preview provider |
| `moteur.py` | MBTiles engine: tile geometry, cache, estimate, generation (no GUI dependency) |
| `geo.py` | UTM / MGRS conversions, coordinate parsing, place search |

## License

[GPL-3.0-or-later](LICENSE).
