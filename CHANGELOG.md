# Journal des modifications

## v1.4.0
- **Pause et reprise** : le bouton « Pause » enregistre l'avancement. On peut alors fermer l'application ou éteindre le PC : au prochain lancement, le traitement **reprend tout seul à l'image exacte** où il s'était arrêté, sans image perdue ou doublée et avec le son synchronisé.
- Fermer la fenêtre pendant un traitement le met d'abord en pause proprement. Après une coupure de courant ou un plantage, la reprise se fait depuis le dernier morceau enregistré.
- **Réglages figés au clic** : chaque fichier lancé garde exactement les réglages choisis à ce moment-là, même si on les modifie ensuite et même après un redémarrage. Ils sont affichés sur sa carte (🔒).
- **Ajouter à la file** pendant un traitement : de nouveaux fichiers peuvent être ajoutés avec d'autres réglages.
- La vidéo est désormais encodée par morceaux, puis assemblée sans réencodage, avec des durées exactes (aucune dérive du son).
- Si l'application est fermée brutalement, les outils (FFmpeg, IA) s'arrêtent avec elle au lieu de continuer en arrière-plan.

## v1.3.0
- **Interpolation d'images par IA (RIFE v4.6)** : vidéos 2, 3 ou 4 fois plus fluides (par exemple 30 → 60 i/s). Les changements de plan sont détectés automatiquement, pour ne jamais mélanger deux scènes. Elle se combine avec l'agrandissement, ou s'utilise seule (« Taille d'origine »).
- **Les images restent des images** : une photo PNG, JPG ou WebP donne maintenant une image agrandie, et non plus une vidéo MP4. La transparence des PNG est conservée. Le format de sortie se choisit : identique, PNG, JPG ou WebP.
- L'aperçu d'une image s'ouvre directement, sans étape de choix d'image. Les cartes affichent un badge « Image ».
- Correction : l'aperçu IA d'une image ne fonctionnait pas.

## v1.2.0
- **Choix de l'image d'aperçu** : une pellicule et une ligne de temps permettent de choisir précisément l'image à améliorer. On avance image par image (← →), seconde par seconde (Maj + ← →), on saisit un temps exact ou on tire une image au hasard.
- **Mises à jour automatiques** : l'application vérifie au démarrage si une nouvelle version existe, puis la télécharge (avec vérification SHA-256) et l'installe en un clic avant de redémarrer.
- Bouton « Aperçu » sur chaque carte vidéo. Un clic sur le numéro de version lance une recherche de mise à jour.
- Nouveau réglage : activer ou désactiver les mises à jour automatiques.

## v1.1.0
- Nouvelle interface moderne : thème sombre « aurora », cartes vidéo, comparateur avant / après avec zoom.

## v1.0.0
- Première version : upscaling vidéo par IA (Real-ESRGAN), 5 modèles, sortie jusqu'en 8K, son et sous-titres conservés.
