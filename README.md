# Upscale Video AI

**Une alternative gratuite à Topaz Video AI.** Upscale Video AI agrandit vos vidéos (480p, 720p, DVD…) jusqu'en 1080p, 4K ou 8K grâce à l'IA. Tout se passe sur votre PC, sans compte ni abonnement.

![icône](assets/icon.png)

## Téléchargement (Windows 10/11 64 bits)

1. Ouvrez la page [**Releases**](../../releases/latest).
2. Téléchargez **`UpscaleVideoAI.exe`**.
3. Double-cliquez dessus. Il n'y a rien à installer.

> Windows SmartScreen peut afficher « Windows a protégé votre ordinateur », parce que l'exe n'est pas signé. Cliquez sur **Informations complémentaires**, puis sur **Exécuter quand même**.

Il existe aussi **`UpscaleVideoAI-portable.zip`** : c'est la même application, sous forme de dossier. Elle démarre plus vite, car elle n'a pas besoin de s'extraire à chaque lancement.

### Configuration requise
- Windows 10 ou 11, 64 bits
- Une carte graphique compatible **Vulkan** (NVIDIA GTX 600 et plus récentes, AMD Radeon HD 7000 et plus récentes, Intel HD/Iris/Arc), avec des **pilotes à jour**
- Quelques Go d'espace disque libre pour les fichiers temporaires

## L'interface

Thème sombre « aurora » avec effet de verre dépoli. On y trouve :
- des cartes vidéo avec miniature, résolution avant → après et progression en direct ;
- les modèles d'IA présentés sous forme de cartes, avec des jauges de vitesse et de qualité ;
- un **comparateur avant / après** avec curseur, zoom à la molette et déplacement de l'image ;
- des notifications, un journal escamotable et des confettis à la fin du traitement 🎉

L'interface est faite en HTML/CSS et s'affiche dans une fenêtre native grâce à Microsoft Edge WebView2, déjà présent sur Windows 10/11. Si WebView2 manque, l'application ouvre une interface classique de secours. Vous pouvez aussi la forcer avec `UpscaleVideoAI.exe --classic`.

Pour voir l'interface en **mode démo** dans un navigateur : `cd ui && python -m http.server`, puis ouvrez http://localhost:8000.

## Utilisation

1. **Ajoutez vos vidéos ou vos images** (PNG, JPG, WebP, BMP, TIFF…) avec le bouton « Ajouter » ou par glisser-déposer. Vous pouvez en ajouter plusieurs, elles seront traitées à la suite. Une **image donne une image** (`photo_upscaled_1600x1200.jpg`), une vidéo donne une vidéo.
2. **Choisissez un modèle IA** :
   | Modèle | Pour quoi ? | Vitesse |
   |---|---|---|
   | Réaliste – Rapide *(par défaut)* | films, séries, vidéos de famille | ⚡⚡⚡ |
   | Réaliste – Haute qualité | vidéos réelles, détails maximum | ⚡ |
   | Real-ESRGAN x4plus | vidéos réelles très bruitées | ⚡ |
   | Animation / Anime – Rapide | dessins animés, anime | ⚡⚡⚡⚡ |
   | Animation / Anime – Haute qualité | anime, rendu plus net | ⚡ |
3. **Choisissez la résolution de sortie** : x2, x3, x4, 720p, 1080p, 1440p, 4K ou 8K. Vous pouvez aussi garder la *taille d'origine* pour n'utiliser que l'interpolation.
   - **Fluidité (interpolation IA)** : ×2, ×3 ou ×4 images par seconde (par exemple 30 → 60 i/s), grâce à [RIFE](https://github.com/hzwer/ECCV2022-RIFE). Les changements de plan sont détectés, pour ne jamais fondre deux scènes l'une dans l'autre. Ce réglage ne concerne que les vidéos.
4. Cliquez sur **Aperçu IA**, ou sur « Aperçu » sur une carte vidéo, puis **choisissez l'image** à tester :
   - glissez sur la pellicule, ou utilisez ← → (image par image) et Maj + ← → (±1 seconde) ;
   - saisissez un temps exact (ex. `1:23`), ou tirez une image au hasard ;
   - cliquez enfin sur **Améliorer cette image avec l'IA**. Le comparateur avant / après s'ouvre, et « Changer d'image » permet d'en essayer une autre.
5. Cliquez sur **Lancer l'upscale**. La vidéo finale est enregistrée à côté de l'originale, sous le nom `nom_upscaled_3840x2160.mp4`.

### Ce que l'application gère toute seule
- La **piste audio** est conservée telle quelle (ou convertie en AAC si le format MP4 ne l'accepte pas). Toutes les pistes audio sont gardées.
- Les **sous-titres** sont conservés : tous en MKV, les sous-titres texte en MP4.
- Les **chapitres** et les métadonnées sont conservés.
- **Désentrelacement** automatique (DVD, vidéos TV)
- Gestion des **pixels non carrés** (DVD 4:3 / 16:9) et des vidéos tournées (smartphone)
- **Couleurs** correctes : conversion BT.601/BT.709
- **Espace disque limité** : les images sont traitées par lots, puis supprimées au fur et à mesure.
- Le PC ne se met pas en veille pendant un traitement.
- Encodage **H.264 ou H.265** : sur le processeur (CPU), ou accéléré par la carte graphique (NVIDIA NVENC, AMD AMF, Intel QSV).

### Mises à jour automatiques
Au démarrage, l'application vérifie si une nouvelle version est disponible sur GitHub. Si c'est le cas, une bannière propose **Mettre à jour**. La nouvelle version est alors téléchargée, son intégrité est vérifiée (SHA-256), puis elle est installée et l'application redémarre toute seule. Vous pouvez désactiver cette vérification dans *Réglages avancés*. Un clic sur le numéro de version lance une vérification manuelle.

> Si l'exe se trouve dans un dossier protégé (comme `C:\Program Files`), l'installation automatique est impossible. Placez-le plutôt sur le Bureau ou dans `Documents`.

### En cas de problème
- **« Aucune carte graphique compatible Vulkan »** : mettez à jour les pilotes de votre carte graphique.
- **Erreur de mémoire vidéo** : dans *Avancé*, mettez *Taille de tuile* sur 128 ou 64.
- **Encodeur NVENC, AMF ou QSV en erreur** : revenez à « H.264 – CPU ».
- **Interpolation trop lente ou erreur de mémoire** : choisissez ×2 plutôt que ×4. Interpoler double le nombre d'images à agrandir, ce qui double aussi le temps de traitement.
- Le **Journal** en bas à gauche affiche le détail de chaque erreur.

## Comment ça marche

```
vidéo ──ffmpeg──▶ images PNG ──Real-ESRGAN (GPU/Vulkan)──▶ images x2/x4 ──ffmpeg──▶ vidéo + audio d'origine
```

- **Interpolation** : [RIFE ncnn-vulkan](https://github.com/nihui/rife-ncnn-vulkan), modèle RIFE v4.6.
- **IA** : [Real-ESRGAN ncnn-vulkan](https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan), avec les modèles officiels Real-ESRGAN et les modèles [Upscayl](https://github.com/upscayl/upscayl) *lite* et *standard*.
- **Vidéo** : [FFmpeg](https://ffmpeg.org) (builds [BtbN](https://github.com/BtbN/FFmpeg-Builds)).
- **Interface** : HTML/CSS/JS affiché par [pywebview](https://pywebview.flowrl.com) (WebView2). L'interface de secours utilise Tkinter avec le thème Sun Valley. L'exe est empaqueté avec PyInstaller.

## Compiler soi-même

L'exe est compilé automatiquement par GitHub Actions (`.github/workflows/build-windows.yml`) à chaque push. Pour le compiler vous-même sous Windows :

```bat
pip install -r requirements-build.txt
python scripts/fetch_tools.py
pyinstaller --onefile --windowed --name UpscaleVideoAI --icon assets/icon.ico ^
  --splash assets/splash.png --add-data "bin;bin" --add-data "assets;assets" --add-data "ui;ui" ^
  --collect-data sv_ttk --collect-all tkinterdnd2 main.py
```

L'exe se trouve ensuite dans `dist/`.

### Publier une nouvelle version (mise à jour automatique)
1. Augmentez `__version__` dans `upscaler/__init__.py` (ex. `1.2.0` → `1.3.0`).
2. Ajoutez une section `## v1.3.0` dans `CHANGELOG.md`. Elle sert de « Nouveautés » dans l'application.
3. Poussez sur `main`, ou ajoutez `[release]` dans le message de commit. GitHub Actions compile l'exe, le teste et publie la release, que les applications installées détectent ensuite toutes seules. En développement, `python main.py` lance l'interface directement.

## Crédits et licences
- Real-ESRGAN : © Xintao Wang, licence BSD-3-Clause
- RIFE / rife-ncnn-vulkan : © hzwer, nihui, licence MIT
- Modèles Upscayl : © Upscayl, AGPL-3.0
- FFmpeg : GPL
- pywebview : BSD-3-Clause. Sun Valley ttk theme : MIT. tkinterdnd2 : MIT.
