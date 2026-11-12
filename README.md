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

## Utilisation

1. **Ajoutez vos vidéos** avec le bouton « Ajouter des vidéos… » ou par glisser-déposer. Vous pouvez en ajouter plusieurs, elles seront traitées à la suite.
2. **Choisissez un modèle IA** :
   | Modèle | Pour quoi ? | Vitesse |
   |---|---|---|
   | Réaliste – Rapide *(par défaut)* | films, séries, vidéos de famille | ⚡⚡⚡ |
   | Réaliste – Haute qualité | vidéos réelles, détails maximum | ⚡ |
   | Real-ESRGAN x4plus | vidéos réelles très bruitées | ⚡ |
   | Animation / Anime – Rapide | dessins animés, anime | ⚡⚡⚡⚡ |
   | Animation / Anime – Haute qualité | anime, rendu plus net | ⚡ |
3. **Choisissez la résolution de sortie** : x2, x3, x4, 720p, 1080p, 1440p, 4K ou 8K.
4. Cliquez sur **🔍 Aperçu** pour tester sur une seule image. Le résultat s'ouvre en comparaison côte à côte (à gauche l'original, à droite l'IA).
5. Cliquez sur **▶ Démarrer**. La vidéo finale est enregistrée à côté de l'originale, sous le nom `nom_upscaled_3840x2160.mp4`.

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

### En cas de problème
- **« Aucune carte graphique compatible Vulkan »** : mettez à jour les pilotes de votre carte graphique.
- **Erreur de mémoire vidéo** : dans *Avancé*, mettez *Taille de tuile* sur 128 ou 64.
- **Encodeur NVENC, AMF ou QSV en erreur** : revenez à « H.264 – CPU ».
- Le **Journal** en bas à gauche affiche le détail de chaque erreur.

## Comment ça marche

```
vidéo ──ffmpeg──▶ images PNG ──Real-ESRGAN (GPU/Vulkan)──▶ images x2/x4 ──ffmpeg──▶ vidéo + audio d'origine
```

- **IA** : [Real-ESRGAN ncnn-vulkan](https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan), avec les modèles officiels Real-ESRGAN et les modèles [Upscayl](https://github.com/upscayl/upscayl) *lite* et *standard*.
- **Vidéo** : [FFmpeg](https://ffmpeg.org) (builds [BtbN](https://github.com/BtbN/FFmpeg-Builds)).
- **Interface** : Python / Tkinter avec le thème [Sun Valley](https://github.com/rdbende/Sun-Valley-ttk-theme). L'exe est empaqueté avec PyInstaller.

## Compiler soi-même

L'exe est compilé automatiquement par GitHub Actions (`.github/workflows/build-windows.yml`) à chaque push. Pour le compiler vous-même sous Windows :

```bat
pip install -r requirements-build.txt
python scripts/fetch_tools.py
pyinstaller --onefile --windowed --name UpscaleVideoAI --icon assets/icon.ico ^
  --splash assets/splash.png --add-data "bin;bin" --add-data "assets;assets" ^
  --collect-data sv_ttk --collect-all tkinterdnd2 main.py
```

L'exe se trouve ensuite dans `dist/`. En développement, `python main.py` lance l'interface directement.

## Crédits et licences
- Real-ESRGAN : © Xintao Wang, licence BSD-3-Clause
- Modèles Upscayl : © Upscayl, AGPL-3.0
- FFmpeg : GPL
- Sun Valley ttk theme : MIT. tkinterdnd2 : MIT.
