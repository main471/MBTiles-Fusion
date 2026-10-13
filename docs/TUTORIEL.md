# Tutoriel MBTiles Fusion

Ce tutoriel crée une carte hors ligne en 6 étapes. Tu obtiens un fichier `.mbtiles` qui contient :

- une **base régionale** : toute la zone, avec des zooms faibles ;
- des **zones de zoom** : seulement les endroits utiles, avec des zooms forts.

Résultat : une carte détaillée là où tu en as besoin, et légère partout ailleurs.

---

## 1. Lancer l'application

- **Linux** : rends `MBTiles_Fusion-x86_64.AppImage` exécutable (clic droit → Propriétés → « Autoriser l'exécution »), puis double-clic.
- **Windows** : décompresse le zip, puis double-clic sur `MBTiles Fusion.exe`.

La fenêtre a deux parties : la **carte** à gauche, les **réglages** à droite.

## 2. Choisir une source

Une source est un serveur de tuiles, c'est-à-dire un fond de carte en ligne.

**Option A : importer un fichier de sources**
1. Dans le groupe **Source**, clique l'icône dossier (« Importer (QGIS ou MOBAC) »).
2. Choisis un fichier `.xml`, par exemple `examples/sources-ign.xml`.

**Option B : ajouter une source à la main**
1. Clique **+** (« Ajouter une source »).
2. Donne un **nom**.
3. Colle le **modèle d'URL**, par exemple : `https://serveur/{z}/{x}/{y}.png`
4. Clique **Tester**. Une tuile doit s'afficher.
5. Clique **Enregistrer**.

Choisis ensuite la source dans la liste. Le fond de carte s'affiche.

> ⚠ N'utilise que des sources dont la licence autorise le téléchargement, comme l'IGN. Google, Bing et Esri l'interdisent.

## 3. Tracer la base régionale

1. Cherche ta zone avec la barre de recherche en haut à droite de la carte. Tu peux taper un lieu, des coordonnées ou un MGRS.
2. Clique **Tracer la base**.
3. **Glisse** sur la carte pour dessiner un rectangle.
4. À droite, règle le **zoom min** et le **zoom max**.
   Exemple : Z8 à Z14 pour un département.

Pour ajuster la base :
- tire les **ronds aux coins** pour la redimensionner ;
- glisse **l'intérieur du rectangle** pour la déplacer ;
- clique le **crayon** pour saisir des coordonnées exactes.

## 4. Ajouter des zones de zoom

1. Clique **Ajouter une zone**.
2. Glisse sur la carte autour du lieu utile.
3. Dans la liste **Zones de zoom**, règle **Z max** avec **+** et **−**.
   Exemple : Z17 pour lire les chemins, Z19 pour voir les bâtiments.

Chaque zone commence juste au-dessus du zoom max de la base et va jusqu'à son Z max.
Ajoute autant de zones que tu veux.

Raccourcis :
- **Suppr** : supprime la zone sélectionnée.
- **Ctrl+Z** : annule.
- **Ctrl+Maj+Z** : rétablit.
- **Échap** : annule un tracé en cours.

## 5. Vérifier et générer

1. **Carte finale** → **Enregistrer sous…** : choisis le nom et l'emplacement du fichier.
2. Lis l'**Estimation** : nombre de tuiles, poids, tuiles à télécharger.
   - Clique **Mesurer** pour une estimation plus précise du poids.
   - Si la carte est trop lourde, baisse un Z max ou réduis une zone.
3. Clique **Générer la carte**.

Pendant le téléchargement :
- la barre montre la progression, la vitesse et le temps restant ;
- la ligne en dessous explique ce qui limite la vitesse ;
- **Pause** et **Arrêter** sont disponibles. Les tuiles déjà reçues restent en cache.

À la fin, le **Journal** affiche le résultat.

## 6. Mettre la carte sur le téléphone

Copie le fichier `.mbtiles` sur le téléphone :

- **ATAK** : dossier `atak/imagery/`
- **AlpineQuest** : dossier des cartes de l'application, ou import depuis l'application

Si la carte ne s'affiche pas, régénère-la après avoir désactivé **Options avancées → Dédoublonner les tuiles identiques**.

---

## Modifier une carte existante

1. Clique l'icône **Ouvrir** en haut, ou menu → **Ouvrir une carte…**.
2. Choisis la carte `.mbtiles`. La base et les zones réapparaissent.
3. Modifie-les, puis clique **Générer la carte**.

Seules les tuiles manquantes sont téléchargées. Les autres viennent du cache.

## Le cache

Toutes les tuiles téléchargées sont gardées dans le **dossier du cache**. Tu ne télécharges jamais deux fois la même tuile.

- **Voir ce qui est en cache** : active **Cache** en bas à gauche de la carte, puis choisis un niveau de zoom.
- **Libérer de la place** : menu → **Gérer le cache…**.
  - **Nettoyer** : garde seulement les tuiles de la carte ouverte.
  - **Supprimer** : efface tout le cache de cette source.
- **Tuiles anciennes** : après 6 mois, elles sont revérifiées auprès du serveur. Elles ne sont remplacées que si elles ont changé.

## Partager ses sources

Dans le groupe **Source**, clique l'icône disquette (« Exporter les sources (XML) »).
Le fichier se réimporte dans MBTiles Fusion avec tous les réglages, ou dans QGIS.

## Réglages réseau

Panneau **Réseau** :

| Réglage | Prudent | Rapide |
|---|---|---|
| Requêtes simultanées | 6 | 16 |
| Débit maximal | 20 req/s | 60 req/s |

Plus c'est rapide, plus le risque d'être bloqué par le serveur augmente. Si le serveur refuse des requêtes, l'application ralentit toute seule.

## Problèmes fréquents

| Problème | Solution |
|---|---|
| Le fond de carte reste vide | Vérifie la source avec **Tester**, et ta connexion. |
| « Variable manquante » dans l'URL | L'URL doit contenir `{z}`, `{x}` et `{y}`. |
| Beaucoup de tuiles en échec | Le serveur bloque : baisse le débit, attends, relance. Seules les tuiles manquantes seront téléchargées. |
| Carte de plus de 4 Go refusée | Active **Options avancées → Découper en fichiers de 4 Go maximum**. |
| La carte ne s'affiche pas sur le téléphone | Régénère-la sans dédoublonnage. |
