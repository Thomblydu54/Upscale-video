# Extensions officielles — API 1

Les extensions se téléchargent depuis **Extensions** dans l'application. La première est
**Calcul à distance**, version 1.0.0, compatible avec Upscale Video AI ≥ 1.9.0.

## Développer et publier

- `remote-compute/manifest.json` : identité, version, version minimale de l'app et point d'entrée.
- `remote-compute/upv_remote/` : véritable code optionnel, non embarqué dans l'exe de base.
- `python scripts/build_extensions.py` crée `release/remote-compute.upvext` et
  `release/extension-catalog.json`. Les archives sont reproductibles (ordre/date fixes).
- Le workflow publie les deux fichiers avec l'exe. Le catalogue est lu uniquement depuis
  la dernière release de `Thomblydu54/Upscale-video`, après vérification de son SHA-256
  fourni par l'API GitHub. Chaque paquet est ensuite vérifié avec l'empreinte du catalogue.
- `upscaler/extensions.py` fournit l'installation atomique, l'anti-Zip-Slip, les limites
  d'archive, la vérification du code sur disque et le chargement tardif du point d'entrée.
  Un nouveau type d'extension doit aussi être déclaré par l'hôte (pas de code tiers).
- `upscaler/extension_runtime.py` fournit TLS, stockage privé/DPAPI, verrou interprocessus
  et démarrage opt-in HKCU. Le service utilise le même exe avec
  `--extension-service remote-compute`. Il ne crée jamais d'interface, même en erreur.
- **Installer ne démarre rien**. Le rôle et l'accès au réseau s'activent séparément. La clé
  Run est créée seulement si le propriétaire coche le démarrage à l'ouverture de session.

### Tests

```sh
python -m unittest discover -s tests -p 'test_*.py' -v
cd tests && npm ci && npm test
```

En CI, `UPV_EXTENSION_TEST_PACKAGE` pointe vers l'archive construite. `main.py --selftest`
installe cette archive dans un dossier isolé et teste le vrai chargement dynamique depuis
l'exe : TLS épinglé, appairage unique, transfert multiblocs, reçu SHA-256 et nettoyage.

## Protocole Calcul à distance

Le relais est sur le **PC principal**. Les PC GPU établissent seulement des connexions
HTTPS sortantes. Le propriétaire configure une unique redirection TCP sur sa Freebox ;
ni UPnP, ni DMZ, ni désactivation du pare-feu, ni tunnel tiers ne sont utilisés.

- Certificat ECDSA propre au relais ; SHA-256 transmise dans le code hors bande. Le client
  vérifie cette empreinte **avant** l'envoi du bearer. Aucun redirect/proxy implicite.
- Code aléatoire de 256 bits, 10 minutes, usage unique ; un nouveau code révoque le précédent.
- Jetons de rôle distincts : contrôleur **loopback uniquement**, GPU limité à ses travaux.
  Jetons stockés chiffrés via DPAPI sous Windows ; seuls leurs hash sont conservés au relais.
- Requêtes JSON ≤ 64 Kio, blocs de transfert ≤ 2 Mio, 16 connexions simultanées et délai
  réseau par socket. Aucun chemin reçu d'un GPU n'est utilisé pour écrire/lire un fichier.
- Pas de shell, de bureau distant, de clavier/souris, d'installation de logiciels ou d'accès
  générique aux fichiers. Seuls les médias explicitement lancés par le contrôleur sont servis.
- La source du PC principal est référencée en lecture, pas dupliquée. Sa taille/date et son
  SHA sont vérifiés. Le GPU la télécharge dans un sous-dossier nommé par UUID, avec reprise.
- Le moteur existant est appelé avec les réglages figés du travail. Les dossiers de sortie/
  travail sont propres à chaque machine ; le GPU choisi est enregistré à la création du travail.
- Les checkpoints du moteur sont sauvegardés atomiquement sur le PC GPU. Une perte du
  relais ne détruit pas le travail ; un calcul déjà lancé peut finir sans connexion, puis
  attend le retour du relais pour transférer le résultat. Les blocs reprennent après coupure.
- Après réception, le contrôleur vérifie taille + SHA, synchronise le fichier sur disque,
  renomme `.partiel` en fichier final, **puis** envoie le reçu. Le relais marque durablement
  la livraison avant de supprimer sa copie ; le GPU ne supprime son dossier qu'au prochain
  reçu `delivered`. Un ACK perdu ou un crash après renommage ne relance pas le calcul.
- L'abandon est aussi une commande durable. Si le GPU est hors ligne, ses copies ne peuvent
  être effacées physiquement avant sa reconnexion. Révocation/désinstallation sont refusées
  tant qu'un travail attend encore cette confirmation de nettoyage.
- Les tombstones `cleaned` (métadonnées légères, sans chemin de source) expirent après 30 jours.
  Les vidéos non livrées n'expirent **jamais automatiquement** : aucune perte silencieuse.

Le relais reçoit des connexions Internet authentifiées : maintenez Windows, l'application
et ses dépendances à jour. Les tests automatisés utilisent de vraies connexions HTTPS
locales, pas une Freebox distante. Un test entre deux réseaux réels reste nécessaire lors
de la configuration (redirection TCP, pare-feu et débit de la connexion).
