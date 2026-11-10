# Attribution des modèles

Les modèles ONNX de ce répertoire sont versionnés pour que la Veille fonctionne
hors ligne. Ils ne sont pas téléchargés au démarrage.

| Fichier | Origine | Licence |
| --- | --- | --- |
| `athena.onnx` | Modèle entraîné et fourni par le propriétaire du projet | Interne au projet — appartient à l'utilisateur |
| `melspectrogram.onnx` | [openWakeWord](https://github.com/dscripka/openWakeWord), release v0.5.1 | [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) |
| `embedding_model.onnx` | [openWakeWord](https://github.com/dscripka/openWakeWord), release v0.5.1 | [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) |
| `silero_vad.onnx` | [Silero VAD](https://github.com/snakers4/silero-vad) | MIT |

Empreintes SHA-256 :

```text
85872a0698859c45b91c92e62156837cb0bc79649e8d7db512bb1db6a91fb284  athena.onnx
ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f  melspectrogram.onnx
70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f  embedding_model.onnx
a35ebf52fd3ce5f1469b2a36158dba761bc47b973ea3382b3186ca15b1f5af28  silero_vad.onnx
```

## openWakeWord

Le code et les modèles de features sont distribués par David Scripka sous
licence Apache 2.0 pour le code (texte intégral dans
`LICENSE-openwakeword.txt`) et **CC BY-NC-SA 4.0** pour les modèles de
features `melspectrogram.onnx` et `embedding_model.onnx`.

Attribution CC BY-NC-SA 4.0 :

- Œuvre : modèles de features openWakeWord (`melspectrogram` et `embedding`),
  release v0.5.1.
- Auteur : David Scripka / projet openWakeWord.
- Source : <https://github.com/dscripka/openWakeWord/releases/tag/v0.5.1>
- Licence : <https://creativecommons.org/licenses/by-nc-sa/4.0/>
- Modifications : aucune ; les fichiers sont redistribués tels quels.

**Attention** : la clause « NC » interdit l'usage commercial de ces deux
modèles sans autorisation séparée. Les usages commerciaux du projet doivent
être vérifiés au regard de cette licence.

## Silero VAD

`silero_vad.onnx` provient du projet [Silero VAD](https://github.com/snakers4/silero-vad)
et est distribué sous licence MIT :

```text
MIT License

Copyright (c) 2020-present Silero Team

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
