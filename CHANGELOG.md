# Journal des modifications

## v1.6.1
- **Images par seconde plus claires** : chaque carte affiche maintenant la fréquence réelle de sa vidéo (ex. « 00:04:12 · 24 i/s »).
- Les boutons « Fluidité » et le résumé tenaient compte uniquement de la **première** vidéo de la liste (ou supposaient 30 i/s quand la liste était vide). Ils affichaient donc souvent « 60 i/s » pour toutes les vidéos. Ils montrent maintenant la plage réelle (ex. « 48 à 60 » pour des vidéos à 24 et 30 i/s), ou « double / triple / quadruple » sans vidéo.
- Le traitement lui-même n'a pas changé : chaque vidéo a toujours été traitée à sa propre fréquence.

## v1.6.0
- **Nettoyage automatique des vidéos compressées** (activé par défaut) : chaque fichier est analysé à l'ajout. Si des blocs de compression sont visibles, ils sont adoucis juste avant l'IA, avec une force adaptée (léger, moyen ou fort). Sinon, rien n'est modifié.
  - Ça concerne surtout les vieux formats (DVD / MPEG-2, DivX / Xvid / MPEG-4, caméscopes, vieux téléphones), les photos JPEG compressées et les vidéos H.264 très dégradées.
  - Les vidéos récentes bien encodées (H.264 / H.265 / VP9 / AV1) restent intactes : leur compression est déjà lissée et l'IA gère très bien le reste.
  - La carte du fichier indique « Compression détectée · nettoyage … ». L'aperçu IA montre l'original non retouché à gauche.
  - Le nettoyage est figé avec les autres réglages au lancement et conservé après une pause. Il peut être désactivé dans les réglages avancés.
- Les traitements en pause créés avec une version précédente reprennent sans nettoyage, comme prévu au moment du clic.

## v1.5.0
- **Nouvelle section « Finitions » (optionnelle, désactivée par défaut)** : des retouches appliquées après l'IA, à la résolution finale.
  - Réglages : netteté, couleurs, contraste, halo lumineux (« glow ») et teinte chaude ou froide.
  - Préréglages : Éclatant, Glow anime, Cinéma, Doré, Glacé et Netteté seule. Bouger un curseur passe en « Personnalisé ».
  - L'**aperçu IA** montre l'original à gauche et l'IA + finitions à droite. La carte indique « finitions … » dans les réglages figés.
  - Fonctionne aussi pour les images, en conservant la transparence des PNG.
- Les finitions sont figées au lancement comme les autres réglages, et la pause / reprise les conserve.
- Aucun réglage existant ne change : finitions désactivées, les commandes de traitement sont identiques à la v1.4.1.

## v1.4.1
- **Nettoyage automatique au démarrage** : les fichiers temporaires restés après un plantage, une fermeture forcée ou une coupure de courant sont supprimés. Cela comprend les images des lots en cours (parfois plusieurs Go), l'avancement de traitements qui n'existent plus, les fichiers décompressés de l'exe (~300 Mo par session interrompue), les aperçus de plus de 24 h et les mises à jour non installées. Les fichiers d'une application encore ouverte ne sont jamais touchés.
- Abandonner ou retirer un traitement en pause supprime aussi la vidéo finale à moitié assemblée, s'il y en a une.
- Le journal des erreurs est limité à 1 Mo.

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
