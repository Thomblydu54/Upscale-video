# Upscale Video AI

**Une alternative gratuite à Topaz Video AI.** Upscale Video AI agrandit vos vidéos (480p, 720p, DVD…) jusqu'en 1080p, 4K ou 8K grâce à l'IA. Par défaut, tout se passe sur votre PC, sans compte ni abonnement. Une extension optionnelle permet aussi de calculer sur un autre PC à vous.

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
   - **Fluidité (interpolation IA)** : ×2, ×3 ou ×4 images par seconde (par exemple 30 → 60 i/s), grâce à [RIFE](https://github.com/hzwer/ECCV2022-RIFE). Avec **Perso**, vous entrez vous-même la fréquence de sortie (ex. 50, 60 ou 144 i/s), même si ce n'est pas un multiple de celle de la vidéo. Les changements de plan sont détectés, pour ne jamais fondre deux scènes l'une dans l'autre. Ce réglage ne concerne que les vidéos.
4. Cliquez sur **Aperçu IA**, ou sur « Aperçu » sur une carte vidéo, puis **choisissez l'image** à tester :
   - glissez sur la pellicule, ou utilisez ← → (image par image) et Maj + ← → (±1 seconde) ;
   - saisissez un temps exact (ex. `1:23`), ou tirez une image au hasard ;
   - cliquez enfin sur **Améliorer cette image avec l'IA**. Le comparateur avant / après s'ouvre, et « Changer d'image » permet d'en essayer une autre.
   - **Nettoyage automatique des vidéos compressées** (activé par défaut, réglages avancés) : l'application mesure les blocs de compression de chaque fichier. Elle ne les adoucit avant l'IA que lorsqu'ils sont réellement visibles (vieux formats, DVD, DivX, JPEG compressés, H.264 très dégradé). La carte indique alors « Compression détectée · nettoyage léger / moyen / fort ».
   - **Finitions (optionnel)** : section à part, désactivée par défaut. Elle ajoute des retouches après l'IA : netteté, couleurs, contraste, halo lumineux et teinte chaude ou froide. Six préréglages sont proposés (Éclatant, Glow anime, Cinéma, Doré, Glacé, Netteté seule), et chaque curseur peut aussi se régler à la main (« Personnalisé »). L'aperçu IA montre l'image originale à gauche et le résultat IA + finitions à droite. Quand les finitions sont désactivées, le résultat est exactement le même qu'avant cette option.
     - **✦ Auto (image par image)** : l'application choisit elle-même la meilleure finition pour **chaque image**, selon son contenu : Doré pour une lumière chaude (coucher de soleil, bougies), Glacé pour une teinte froide (neige, ciel bleu, nuit bleutée), Glow anime pour des lumières vives sur fond sombre (néons, ville de nuit) ou un dessin animé lumineux, Cinéma pour une scène sombre ou terne, Netteté seule pour le noir et blanc ou une image déjà très colorée, et Éclatant pour une scène de jour classique. Une même vidéo peut donc utiliser une seule finition, deux, ou les six. Pour éviter tout clignotement, le changement est franc à chaque changement de plan, et progressif (fondu de 0,6 s) quand la lumière évolue au sein d'un même plan. L'aperçu IA indique la finition choisie pour l'image affichée.
5. Cliquez sur **Lancer l'upscale**. La vidéo finale est enregistrée à côté de l'originale, sous le nom `nom_upscaled_3840x2160.mp4`.

### Pause et reprise, même après un arrêt du PC
- Pendant un traitement, le bouton **Pause** enregistre l'avancement sur le disque. Vous pouvez ensuite **fermer l'application ou éteindre le PC**.
- Au prochain lancement, le traitement **reprend automatiquement à l'image exacte** où il s'était arrêté. Aucune image n'est perdue ni doublée, et le son reste synchronisé.
- Si vous fermez la fenêtre pendant un traitement, l'application le met d'abord en pause toute seule. Même en cas de coupure de courant ou de plantage, la reprise repart du dernier morceau enregistré (quelques secondes de vidéo au plus).
- **Les réglages sont figés au moment du clic** : chaque fichier lancé garde le modèle, la résolution, la fluidité et l'encodage choisis à cet instant. Ils sont rappelés sur sa carte (🔒). Changer les réglages ensuite, même après un redémarrage, ne modifie jamais un traitement en cours, en file ou en pause.
- Pendant un traitement, vous pouvez ajouter d'autres fichiers avec d'autres réglages : **Ajouter à la file**. Ils utiliseront les réglages du moment.
- **Abandonner** supprime l'avancement enregistré des traitements en pause.
- L'avancement est conservé dans `%LOCALAPPDATA%\UpscaleVideoAI\jobs`, ou dans le *dossier temporaire* choisi dans les réglages avancés. Il est supprimé automatiquement à la fin.

### Aucun fichier ne s'accumule
- Pendant un traitement, chaque lot d'images est supprimé dès qu'il est encodé. À la fin, tous les fichiers de travail sont supprimés.
- Au démarrage, l'application supprime ce qu'une session interrompue (plantage, coupure de courant) a pu laisser : images temporaires, avancement de traitements qui n'existent plus, fichiers décompressés de l'exe, aperçus de plus de 24 h, mises à jour non installées. Ce qui appartient à une fenêtre encore ouverte n'est jamais supprimé.
- Seuls les traitements **en pause** gardent leurs données, jusqu'à leur fin ou jusqu'à « Abandonner ».

### Ce que l'application gère toute seule
- La **piste audio** est conservée telle quelle (ou convertie en AAC si le format MP4 ne l'accepte pas). Toutes les pistes audio sont gardées.
- Les **sous-titres** sont conservés : tous en MKV, les sous-titres texte en MP4.
- Les **chapitres** et les métadonnées sont conservés.
- **Désentrelacement** automatique (DVD, vidéos TV)
- Gestion des **pixels non carrés** (DVD 4:3 / 16:9) et des vidéos tournées (smartphone)
- **Couleurs** correctes : conversion BT.601/BT.709
- **Espace disque limité** : les images sont traitées par lots, puis supprimées au fur et à mesure. La vidéo est encodée par morceaux, assemblés à la fin sans réencodage.
- Le PC ne se met pas en veille pendant un traitement.
- Encodage **H.264 ou H.265** : sur le processeur (CPU), ou accéléré par la carte graphique (NVIDIA NVENC, AMD AMF, Intel QSV).

### Mises à jour automatiques
Au démarrage, l'application vérifie si une nouvelle version est disponible sur GitHub. Si c'est le cas, une bannière propose **Mettre à jour**. La nouvelle version est alors téléchargée, son intégrité est vérifiée (SHA-256), puis elle est installée et l'application redémarre toute seule. Vous pouvez désactiver cette vérification dans *Réglages avancés*. Un clic sur le numéro de version lance une vérification manuelle.

> Si l'exe se trouve dans un dossier protégé (comme `C:\Program Files`), l'installation automatique est impossible. Placez-le plutôt sur le Bureau ou dans `Documents`.

## Extensions officielles · Calcul à distance (v1.9)

Le bouton **Extensions** permet de télécharger, configurer et désinstaller les extensions
publiées dans le catalogue officiel. La première, **Calcul à distance**, est un vrai paquet
optionnel : son code n'est pas embarqué dans l'exe de base. Installer le paquet **n'active
ni serveur, ni connexion à l'autre PC, ni démarrage automatique**. Les options de traitement
local (modèles, interpolation, FPS personnalisés, finitions, encodage…) restent inchangées.

### Une seule application, deux rôles

```text
PC principal (application + relais HTTPS privé)  ← Internet →  PC de calcul (GPU)
       réglages + progression + résultat                     service sans fenêtre
```

- Le **PC principal** héberge le relais ; pas de VPS à louer, pas de Tailscale. Il n'a pas
  besoin d'un GPU puissant pour relayer. Les vidéos ne sont jamais envoyées sur GitHub.
- Le **PC de calcul** reçoit uniquement les médias lancés par le PC principal, utilise
  sa propre carte graphique et renvoie le résultat. Aucun contrôle du bureau ou du système.
- Le service utilise le **même exe**, en mode sans interface ni console. Après la
  configuration initiale sur le PC de calcul, vous pouvez fermer sa fenêtre ; il continue.
- Le démarrage automatique est **facultatif**, à cocher sur chaque PC concerné. Il se fait
  **à l'ouverture de votre session Windows, pas avant connexion** ; ce n'est pas un service
  système privilégié. Gardez l'exe à son emplacement (ou rouvrez l'app après l'avoir déplacé).
  Le service est identifiable dans le Gestionnaire des tâches et désactivable dans l'app.

### 1. Préparer le PC principal et la Freebox

1. Mettez l'application à jour sur les deux PC, ouvrez **Extensions**, puis **Installer**
   sous **Calcul à distance**. Une connexion Internet au catalogue officiel est nécessaire.
2. Sur le PC principal, choisissez **PC principal · interface + relais**.
3. Dans **Freebox OS → État de la Freebox → Internet**, relevez votre IPv4 publique et
   votre plage de ports. Choisissez **un port libre dans cette plage**. Une IPv4 partagée
   suffit si le port appartient à votre plage. L'option full-stack est facultative ; elle
   donne tous les ports ([assistance Free](https://assistance.free.fr/articles/1758)).
4. Réservez une IPv4 locale fixe au PC principal dans le DHCP de la Freebox. Dans
   **Paramètres de la Freebox → Mode avancé → Gestion des ports**, ajoutez une redirection
   **TCP**, pour ce seul port externe vers cette IPv4 locale et le **même port interne**.
5. Saisissez le port choisi et l'adresse **`https://VOTRE_IPV4_PUBLIQUE:PORT`** dans
   l'extension. Cochez l'autorisation, puis **Activer le relais**. Autorisez l'application
   dans le pare-feu Windows pour votre réseau privé si Windows le demande. Aucun réglage
   de box ou de pare-feu n'est modifié silencieusement. **Ni DMZ, ni pare-feu désactivé.**
6. Cliquez sur **Créer un code d'appairage**. Le code est confidentiel, valable **3 heures**
   et à **usage unique**. En créer un autre invalide le précédent.

La durée de 3 heures nécessite l'extension **Calcul à distance 1.0.1** sur le PC principal
(**Extensions → Actualiser → Mettre à jour**, puis **Démarrer** si le relais est arrêté).
Les codes créés avant la mise à jour conservent leur ancienne échéance : créez-en un nouveau.
Les PC déjà appairés n'ont pas besoin d'être appairés à nouveau.

### 2. Appairer le PC de calcul

1. Sur l'autre PC, dans la même extension, choisissez **PC de calcul · son GPU travaille**.
2. Collez le code, donnez un nom au PC et choisissez son GPU (ou Automatique).
3. Cochez l'autorisation. Le démarrage avec Windows reste au choix, désactivé par défaut.
4. Cliquez sur **Appairer et activer le calcul**. **Aucun port à ouvrir sur ce PC** : ses
   connexions sont sortantes. Vous pouvez ensuite fermer sa fenêtre.
5. Sur le PC principal, le PC appairé apparaît. Cliquez **Utiliser**, ou choisissez-le dans
   **Calculer sur**, au-dessus des modèles. **Ce PC · traitement local** reste disponible.

Le premier test doit être fait **depuis l'autre réseau** : tester votre adresse publique
depuis le Wi-Fi de la même Freebox ne prouve pas que la redirection fonctionne. Les tests
automatiques du projet utilisent deux pairs HTTPS locaux ; ils ne peuvent pas vérifier
votre box, votre pare-feu, vos pilotes GPU ni votre débit à distance.

### Utilisation, reprise et nettoyage

- Choisissez normalement la résolution, le modèle, la fluidité, les finitions et l'encodeur.
  **L'aperçu IA et les traitements** utilisent le PC choisi. Un aperçu vidéo transfère la
  source entière au PC de calcul, puis n'améliore que l'image choisie.
- **Destination + réglages + GPU distant sont figés au clic.** Changer ensuite de PC ou de
  réglages ne déplace ni ne modifie les travaux en file ou en pause. Les dossiers de sortie
  et temporaires sont propres à chaque machine ; aucun chemin du PC principal n'est exécuté
  comme chemin de sortie sur l'autre PC.
- La carte montre la destination verrouillée ; la barre affiche l'envoi, le calcul distant,
  le retour et la vérification. Il n'y a **jamais de repli silencieux sur le GPU local**.
- **Pause**, fermeture du PC principal et reprise de la file gardent leur comportement.
  Les commandes de pause/abandon sont durables. Si le PC GPU est déconnecté, il ne peut les
  appliquer qu'à sa reconnexion. Un calcul déjà lancé peut finir pendant une coupure du
  relais, puis conserver son résultat en attendant le retour du PC principal.
- Les sources et résultats se transfèrent par blocs avec reprise. Les checkpoints du
  moteur sont conservés sur le PC GPU. Après un arrêt, aucun calcul déjà confirmé n'est
  répété ; les lots non confirmés peuvent être refaits comme dans le traitement local.
- Une erreur GPU ou disque met le travail **en pause sans effacer les copies ni les
  checkpoints**. Consultez le journal, corrigez le problème puis cliquez **Reprendre** ;
  seul un abandon explicite autorise le nettoyage d'un travail non livré.
- À réception : contrôle **taille + SHA-256**, enregistrement du fichier final, **puis reçu
  de livraison**. Le cache du relais et le dossier complet du travail sur le PC GPU
  (source reçue, segments, résultat) sont alors supprimés. **L'original du PC principal
  n'est jamais supprimé.** Un GPU éteint ne peut nettoyer ses copies qu'à sa reconnexion.
- Désinstallation et révocation attendent la livraison ou l'abandon **et le nettoyage
  confirmé**, pour ne pas couper l'unique moyen de récupérer/effacer un résultat.
- Si l'IP publique ou le port change : corrigez le relais sur le PC principal, puis
  l'adresse du relais sur le PC GPU, sans nouveau code. L'empreinte du même relais et les
  travaux sont conservés. Le code n'est nécessaire qu'au premier appairage.
- Connexions chiffrées avec certificat privé **épinglé** : aucun domaine ni certificat
  payant requis. Le client vérifie l'empreinte avant d'envoyer ses clés. Les jetons sont
  chiffrés via **DPAPI** sous Windows. Une adresse ouverte dans un navigateur ne constitue
  pas l'interface du relais ; n'ajoutez pas son certificat au système pour le faire marcher.

Les détails du protocole et du développement sont dans [extensions/README.md](extensions/README.md).

### En cas de problème
- **« Aucune carte graphique compatible Vulkan »** : mettez à jour les pilotes de votre carte graphique.
- **Erreur de mémoire vidéo** : dans *Avancé*, mettez *Taille de tuile* sur 128 ou 64.
- **Encodeur NVENC, AMF ou QSV en erreur** : revenez à « H.264 – CPU ».
- **Interpolation trop lente ou erreur de mémoire** : choisissez ×2 plutôt que ×4. Interpoler double le nombre d'images à agrandir, ce qui double aussi le temps de traitement.
- Le **Journal** en bas à gauche affiche le détail de chaque erreur.

## Comment ça marche

```
vidéo ──ffmpeg──▶ images PNG ──[RIFE]──Real-ESRGAN (GPU/Vulkan)──▶ images x2/x4 ──ffmpeg──▶ morceaux encodés
                                                                         │ (avancement enregistré après chaque morceau)
                                    assemblage sans réencodage + audio, sous-titres, chapitres d'origine ◀──┘
```

- **Interpolation** : [RIFE ncnn-vulkan](https://github.com/nihui/rife-ncnn-vulkan), modèle RIFE v4.6.
- **IA** : [Real-ESRGAN ncnn-vulkan](https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan), avec les modèles officiels Real-ESRGAN et les modèles [Upscayl](https://github.com/upscayl/upscayl) *lite* et *standard*.
- **Nettoyage** : mesure des blocs avec `blockdetect` (+ quantification H.264), puis filtre `pp7` dosé selon la mesure.
- **Finitions** : filtres FFmpeg `colortemperature`, `curves`, `vibrance`, `cas` (netteté adaptative) et halo par `gblur` + `blend` (mode « écran »), appliqués à la résolution finale. En mode Auto, une analyse rapide (image réduite à 64×36) choisit la finition de chaque image ; les réglages changent pendant l'encodage grâce au filtre `sendcmd`, à l'image près, sans retraitement par l'IA.
- **Vidéo** : [FFmpeg](https://ffmpeg.org) (builds [BtbN](https://github.com/BtbN/FFmpeg-Builds)).
- **Interface** : HTML/CSS/JS affiché par [pywebview](https://pywebview.flowrl.com) (WebView2). L'interface de secours utilise Tkinter avec le thème Sun Valley. L'exe est empaqueté avec PyInstaller.

## Compiler soi-même

L'exe est compilé automatiquement par GitHub Actions (`.github/workflows/build-windows.yml`) à chaque push. Pour le compiler vous-même sous Windows :

```bat
pip install -r requirements-build.txt
python scripts/fetch_tools.py
pyinstaller --onefile --windowed --name UpscaleVideoAI --icon assets/icon.ico ^
  --add-data "bin;bin" --add-data "assets;assets" --add-data "ui;ui" ^
  --collect-data sv_ttk --collect-all tkinterdnd2 --collect-all cryptography ^
  --hidden-import http.server --hidden-import http.client --hidden-import socketserver ^
  --hidden-import secrets --hidden-import ipaddress main.py
python scripts/build_extensions.py
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
- cryptography : Apache-2.0 / BSD-3-Clause (identité TLS du relais et certificats).
- pywebview : BSD-3-Clause. Sun Valley ttk theme : MIT. tkinterdnd2 : MIT.
