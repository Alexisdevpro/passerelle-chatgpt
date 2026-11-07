# Photos en entrée, texte en sortie

Le backend ChatGPT accepte des images avec une question, sous réserve du modèle et des droits du compte. Ce mode ne génère pas d'image. Le backend Ollama de cet adaptateur refuse les images.

Installer le SDK client `openai`, démarrer une instance connectée, puis définir `GATEWAY_API_KEY` et un modèle autorisé. Les exemples utilisent localhost par défaut.

```bash
python examples/vision.py photo.jpg --prompt "Que vois-tu sur cette photo ?"
python examples/vision.py photo1.jpg photo2.png --prompt "Compare ces photos." --stream
```

Les requêtes Responses utilisent :

```json
{
  "model": "passerelle-default",
  "input": [{
    "role": "user",
    "content": [
      {"type": "input_text", "text": "Décris cette image."},
      {"type": "input_image", "image_url": "https://example.com/photo.jpg"}
    ]
  }]
}
```

Remplacer l'URL d'exemple par une image HTTPS accessible au fournisseur. Une data URL `data:image/jpeg;base64,...` est aussi possible. Le script `vision.py` encode les fichiers du client. Pour Chat Completions, utiliser une partie `{"type":"image_url","image_url":{"url":"URL_HTTPS_OU_DATA_URL","detail":"auto"}}` avec une partie texte et un rôle `user`.

Limites de la passerelle : 8 images par requête, 10 Mio par image base64 avant encodage et 16 Mio de JSON total. Formats JPEG, PNG, WEBP et GIF non animé. La validation locale vérifie base64 et signature MIME ; le fichier complet est validé par le fournisseur. Les URL ne sont pas téléchargées par la passerelle. Les images sont envoyées au fournisseur pour analyse et les politiques de celui-ci s'appliquent.

L'option `--save-request` écrit un JSON contenant l'image en base64. Conserver ce fichier en privé et ne pas l'ajouter à Git. Les données image ne sont pas incluses dans les journaux de la passerelle.
