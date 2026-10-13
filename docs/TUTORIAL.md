# MBTiles Fusion tutorial

This tutorial builds an offline map in 6 steps. You get one `.mbtiles` file containing:

- a **regional base map**: the whole area, at low zoom levels;
- **zoom zones**: only the places you need, at high zoom levels.

Result: detailed where it matters, light everywhere else.

> The interface is in French. French labels are shown in **bold**, with a translation in brackets.

---

## 1. Start the application

- **Linux**: make `MBTiles_Fusion-x86_64.AppImage` executable (right-click → Properties → "Allow executing"), then double-click it.
- **Windows**: unzip the archive, then double-click `MBTiles Fusion.exe`.

The window has two parts: the **map** on the left and the **settings** on the right.

## 2. Choose a source

A source is a tile server, that is, an online base map.

**Option A: import a source file**
1. In the **Source** group, click the folder icon (**Importer** [Import]).
2. Pick an `.xml` file, for example `examples/sources-ign.xml`.

**Option B: add a source by hand**
1. Click **+** (**Ajouter une source** [Add a source]).
2. Enter a **Nom** [Name].
3. Paste the **Modèle d'URL** [URL template], for example `https://server/{z}/{x}/{y}.png`
4. Click **Tester** [Test]. A tile should appear.
5. Click **Enregistrer** [Save].

Then pick the source in the list. The base map appears.

> ⚠ Only use sources whose licence allows downloading, such as IGN. Google, Bing and Esri forbid it.

## 3. Draw the regional base map

1. Find your area with the search bar at the top right of the map. You can type a place, coordinates or an MGRS reference.
2. Click **Tracer la base** [Draw the base].
3. **Drag** on the map to draw a rectangle.
4. On the right, set the **minimum and maximum zoom**.
   Example: Z8 to Z14 for a French département.

To adjust the base:
- drag the **corner handles** to resize it;
- drag **inside the rectangle** to move it;
- click the **pencil** to type exact coordinates.

## 4. Add zoom zones

1. Click **Ajouter une zone** [Add a zone].
2. Drag on the map around a place you need.
3. In the **Zones de zoom** [Zoom zones] list, set **Z max** with **+** and **−**.
   Example: Z17 to read trails, Z19 to see buildings.

Each zone starts just above the base's maximum zoom and goes up to its Z max.
Add as many zones as you like.

Shortcuts:
- **Delete**: removes the selected zone.
- **Ctrl+Z**: undo.
- **Ctrl+Shift+Z**: redo.
- **Esc**: cancels a drawing in progress.

## 5. Check and generate

1. **Carte finale** [Final map] → **Enregistrer sous…** [Save as…]: choose the file name and location.
2. Read the **Estimation** [Estimate]: number of tiles, size, tiles to download.
   - Click **Mesurer** [Measure] for a more accurate size.
   - If the map is too large, lower a Z max or shrink a zone.
3. Click **Générer la carte** [Generate the map].

While downloading:
- the bar shows progress, speed and time left;
- the line below explains what is limiting the speed;
- **Pause** and **Arrêter** [Stop] are available. Tiles already received stay in the cache.

When it is done, the **Journal** [Log] shows the result.

## 6. Copy the map to your phone

Copy the `.mbtiles` file to the phone:

- **ATAK**: `atak/imagery/` folder
- **AlpineQuest**: the app's maps folder, or import it from the app

If the map does not display, regenerate it with deduplication turned off: **Options avancées** [Advanced options] → **Dédoublonner les tuiles identiques** [Deduplicate identical tiles].

---

## Edit an existing map

1. Click the **Open** icon at the top, or menu → **Ouvrir une carte…** [Open a map…].
2. Pick the `.mbtiles` map. Its base and zones come back.
3. Change them, then click **Générer la carte** [Generate the map].

Only missing tiles are downloaded. The rest comes from the cache.

## The cache

Every downloaded tile is kept in the **cache folder**. The same tile is never downloaded twice.

- **See what is cached**: turn on **Cache** at the bottom left of the map, then choose a zoom level.
- **Free up space**: menu → **Gérer le cache…** [Manage the cache…].
  - **Nettoyer** [Clean up]: keeps only the tiles of the open map.
  - **Supprimer** [Delete]: erases the whole cache for that source.
- **Old tiles**: after 6 months, they are checked again with the server. They are replaced only if they changed.

## Share your sources

In the **Source** group, click the floppy-disk icon (**Exporter les sources** [Export sources]).
The file can be re-imported into MBTiles Fusion with all settings, or loaded in QGIS.

## Network settings

**Réseau** [Network] panel:

| Setting | Cautious | Fast |
|---|---|---|
| Simultaneous requests | 6 | 16 |
| Maximum rate | 20 req/s | 60 req/s |

The faster you go, the higher the risk of being blocked by the server. If the server starts refusing requests, the application slows down by itself.

## Common problems

| Problem | Fix |
|---|---|
| The base map stays blank | Check the source with **Tester** [Test], and your connection. |
| "Variable manquante" [Missing variable] in the URL | The URL must contain `{z}`, `{x}` and `{y}`. |
| Many failed tiles | The server is blocking you: lower the rate, wait, start again. Only missing tiles will be downloaded. |
| Map over 4 GB rejected | Turn on **Options avancées** → **Découper en fichiers de 4 Go maximum** [Split into 4 GB files]. |
| The map does not display on the phone | Regenerate it without deduplication. |
