# Attribution des fixtures audio

`athena.wav` est un WAV **synthétique**, pas un enregistrement humain : il a été
produit hors ligne par [Piper](https://github.com/rhasspy/piper) pour servir de
fixture au test d'intégration positif de la Veille. Il ne contient aucune voix
réelle et ne peut pas remplacer une validation au micro.

| Fichier | Contenu | Voix | Licence |
| --- | --- | --- | --- |
| `athena.wav` | « Athéna » prononcé en français, 16 kHz mono PCM16, 0,78 s | `fr_FR-siwis-medium` | Dataset [SIWIS](https://datashare.ed.ac.uk/handle/10283/2353), CC BY 4.0 |

- Voix : `fr_FR-siwis-medium`, distribuée par le projet Piper pour Rhasspy.
- Dataset d'entraînement : SIWIS (University of Edinburgh), CC BY 4.0.
- Texte synthétisé : « Athéna », vitesse 1.0, silence de padding avant et après.
- Usage : uniquement vérifier qu'`athena.onnx`, `melspectrogram.onnx` et
  `embedding_model.onnx` déclenchent une détection positive sur de l'audio
  réel, sans micro.
- Empreinte SHA-256 :
  `03f715551b0d79d92cbe3a984adddd6e6c34f62e4b0336ad99d057c5e3645dd9`

La licence CC BY 4.0 du dataset couvre la voix ; le WAV généré est redistribué
avec cette attribution. Voir aussi `client/models/ATTRIBUTION.md` pour les
modèles ONNX eux-mêmes.
