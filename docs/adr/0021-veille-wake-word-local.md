# Veille locale : openWakeWord ONNX dans le Client

## Status

accepted

Précise l'ADR-0001 : la Veille est un comportement de l'adaptateur Client desktop, pas une
décision du noyau. Le Protocole partagé et le serveur ne changent pas. Ne modifie aucun
autre ADR.

## Contexte

Le Client desktop n'ouvrait une Session que par un appui sur Entrée (push-to-talk). Le micro
était déjà ouvert en permanence, mais hors Session l'audio lu était jeté : rien ne partait
vers le serveur avant le déclenchement. Pour invoquer Athena sans clavier, il fallait
détecter localement un Wake word, ouvrir la Session immédiatement et transmettre la commande
qui suit sans en perdre la première syllabe.

Le mot choisi est « Athena », prononcé à la française. Le modèle fourni par le propriétaire
du projet est un **classifieur openWakeWord** (`athena.onnx`, entrée `[1,16,96]`, sortie
`[1,1]`), pas un modèle audio autonome : il exige les modèles de features partagés
`melspectrogram.onnx` et `embedding_model.onnx`. Un détecteur générique ne suffit donc pas,
et un service cloud contredirait la confidentialité et le fonctionnement hors ligne.

## Décision

La Veille est locale, dans le Client desktop, adossée à **openWakeWord en
`inference_framework="onnx"`**. Le classifieur `athena.onnx` est chargé avec les deux
modèles de features par des **chemins explicites** ; rien n'est téléchargé au premier
lancement et les ressources du paquet installé ne sont pas consultées.

**Assets versionnés.** `athena.onnx`, `melspectrogram.onnx`, `embedding_model.onnx` et
`silero_vad.onnx` vivent dans `client/models/`. Le modèle Athena appartient au projet ; les
modèles de features sont distribués par openWakeWord sous **CC BY-NC-SA 4.0** — la clause
« NC » interdit l'usage commercial sans autorisation — et Silero VAD sous MIT. Les licences
et empreintes sont versionnées dans `client/models/ATTRIBUTION.md`, avec le texte Apache 2.0
du code openWakeWord.

**Dépendance épinglée.** `openwakeword` est tiré depuis GitHub `main` au commit
`368c03716d1e92591906a84949bc477f3a834455` : la version PyPI 0.6.0 dépend de
`tflite-runtime`, qui n'a pas de wheel cp312, tandis que `main` utilise `ai-edge-litert` et
`speexdsp-ns`. `onnxruntime` est déclaré explicitement. Les deux sont des dépendances de
base, pas un extra optionnel : un Client sans elles ne peut pas démarrer quand la Veille est
active.

**Port.** `WakeWordDetector` expose `process(chunk: bytes) -> Detection | None` et `close()`.
`Detection` porte le score et le pré-roll PCM16. `OpenWakeWordDetector` encapsule le
classifieur, le seuil effectif, le VAD optionnel et les erreurs ; il est injecté dans
`ClientApp` comme `Audio` et `Transport`. Le modèle est chargé **une seule fois au
démarrage**, après validation du Jeton et avant l'ouverture du micro. Une erreur de
chargement quand la Veille est active fait sortir le Client avec le code 2.

**Déclenchement optimiste.** En `LISTENING`, chaque chunk micro est passé au détecteur. Sur
une détection, le Client envoie `SessionStart`, passe immédiatement en `ACTIVE`, puis envoie
le **pré-roll** (les 300 ms précédentes, chunk déclencheur inclus) avant l'audio live. Le
pré-roll peut contenir la fin de « Athena » ; Gemini l'ignore. Aucun audio n'est transmis
avant le Wake word.

**Armement.** La Veille n'est armée que si `enabled`, état `LISTENING`, transport connecté,
file de playback vide et cooldown écoulé. Elle ne voit donc pas l'audio d'une Session
active, ni celui qu'Athena est en train de jouer, ni une détection pendant une reconnexion.
Une détection hors connexion est ignorée et loguée, jamais mise en file.

**Fin et réarmement.** Tous les motifs de fin de Session (`SessionEnded`, erreur,
déconnexion, timeout) ramènent en `LISTENING`, vident le playback, puis imposent un
**cooldown de 2 s** avant de réarmer. Un déclenchement sans `SessionStarted` ni audio serveur
pendant **10 s** est considéré comme une Session fantôme et annulé ; ce garde-fou vaut aussi
pour le push-to-talk.

**Dégradation.** Une exception d'inférence est loguée en `ERROR`, condamne la Veille pour le
reste du processus et laisse le **push-to-talk** intact. Le push-to-talk reste disponible en
permanence, y compris Veille active ; `ClientState` reste à deux valeurs (`LISTENING`,
`ACTIVE`) et la Veille est un comportement orthogonal, pas un troisième état.

**Configuration.** `WakeWordConfig` gèle `enabled` (défaut vrai, `ATHENA_WAKE_WORD=0` pour
couper), `model_path`, `threshold` (0.5, surchargé par le `.json` posé à côté du modèle),
`cooldown_seconds` (2.0), `vad` (faux), `session_start_timeout_seconds` (10.0) et
`pre_roll_ms` (300). Les logs sont `INFO` au chargement et à la détection avec le score,
`DEBUG` pour les scores par trame, `ERROR` en cas d'échec.

## Pourquoi

- **Hors ligne et privé.** La détection ne sort pas de la machine et ne dépend d'aucun
  service : le micro peut rester ouvert sans envoyer d'audio avant le mot.
- **Le modèle existait déjà.** Le propriétaire a fourni un classifieur openWakeWord ; le
  charger est la voie la plus courte, et le seuil reste réglable sans réentraîner.
- **Un port, pas un couplage.** `ClientApp` reçoit un détecteur injecté : la Veille se teste
  avec des factices, sans micro ni ONNX, et le jour où le moteur change, seul
  l'adaptateur change.
- **Le pré-roll plutôt que l'attente.** Attendre la fin du mot pour ouvrir la Session
  perdrait la première syllabe de la commande ; 300 ms de mémoire suffisent à couvrir la
  transition.
- **Optimiste mais borné.** Ouvrir la Session sans attendre le serveur rend l'invocation
  instantanée ; le timeout de 10 s empêche de rester bloqué en `ACTIVE` si le serveur ne
  répond pas.
- **La dégradation est locale.** Une inférence cassée ne doit pas rendre le Client
  inutilisable : le push-to-talk est le filet, et il ne dépend pas du détecteur.

## Alternatives écartées

- **Porcupine (Picovoice).** Très bon détecteur embarqué, mais il faut une clé d'accès et
  entraîner un mot-clé personnalisé pour « Athena » ; le français et le hors-ligne ne sont
  pas gratuits, et le modèle fourni par l'utilisateur serait inutilisé.
- **Reconnaissance vocale locale complète (Vosk, Whisper).** Elle détecterait n'importe quel
  mot, mais coûte un modèle lourd, de la latence et de la batterie en permanence pour un
  besoin qui se limite à un déclencheur.
- **Détection dans le cloud.** Le micro enverrait de l'audio en continu avant même le Wake
  word : contraire à la confidentialité et au coût, et dépendant du réseau.
- **Entraîner un modèle français maison.** Le modèle Athena existe déjà et détecte la
  prononciation française ; réentraîner aurait ajouté une chaîne d'entraînement et une
  campagne de validation sans bénéfice immédiat.
- **openWakeWord 0.6.0 depuis PyPI.** Dépend de `tflite-runtime`, sans wheel cp312 : le
  Client ne s'installe pas sur l'environnement cible. La dépendance VCS est un
  contournement assumé, à remplacer par une release dès que le correctif cp312 est publié.
- **Télécharger les modèles au premier lancement.** Pratique pour une démo, mais incompatible
  avec un démarrage hors ligne et avec la reproductibilité des versions ; les assets sont
  donc versionnés.
- **Le VAD intégré d'openWakeWord.** Il charge `silero_vad.onnx` depuis les ressources du
  paquet installé ; pour rester hors ligne, le fichier versionné est passé explicitement et
  le gate est géré par le détecteur. Le VAD reste optionnel, désactivé par défaut.
- **Un troisième état `WAKING`.** La Veille n'est pas un état de Session : elle arme ou non
  le détecteur en `LISTENING`, et `ClientState` garde deux valeurs.

## Conséquences

- **La validation micro reste humaine.** Le test d'intégration versionné vérifie le
  chargement et une détection positive sur un WAV synthétique Piper ; il ne remplace ni une
  prononciation réelle au micro ni une campagne de faux positifs/faux négatifs. Le seuil et
  le VAD restent à régler en conditions réelles.
- **La licence des features limite l'usage commercial.** `melspectrogram.onnx` et
  `embedding_model.onnx` sont en CC BY-NC-SA 4.0 : tout déploiement commercial doit être
  vérifié séparément, ou les modèles remplacés.
- **La dépendance VCS est temporaire.** Un commit épinglé protège des dérives de `main`,
  mais toute mise à jour de sécurité d'openWakeWord exigera de changer l'URL et `uv.lock`.
- **Le modèle n'est pas rechargé à chaud.** Changer `ATHENA_WAKE_WORD_MODEL` exige un
  redémarrage du Client ; c'est le prix d'un chargement unique et d'un démarrage prévisible.
- **Pas de retour à la détection.** Ni earcon ni indicateur visuel : l'utilisateur sait que
  la Session a démarré parce qu'Athena répond. Un retour visuel reste un lot ultérieur.
- **Le push-to-talk gagne le timeout de Session fantôme.** Le garde-fou ne distingue pas
  l'origine du déclenchement : un tap sans réponse serveur revient aussi en `LISTENING`
  après 10 s.
- **Le glossaire gagne un terme et en corrige deux.** « Veille » précise qu'elle est active
  par défaut, « Détecteur de Wake word » apparaît, et « Playback » cesse de prétendre que le
  Wake word peut l'interrompre : la Veille est suspendue pendant la lecture.
