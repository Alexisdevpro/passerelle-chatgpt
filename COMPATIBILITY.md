# Contrat de compatibilité

Version 0.4.0. Ce document décrit l'adaptateur implémenté, qui expose une partie de l'API OpenAI. Les capacités du fournisseur peuvent évoluer ; leur présence dans la documentation du fournisseur n'implique pas leur implémentation ici.

| Route | Comportement |
|---|---|
| `GET /v1/models` et `GET /v1/models/{model}` | Catalogue adapté et filtré par clé ; alias de modèle local si configuré |
| `POST /v1/responses` JSON | Objet terminal reconstruit depuis le flux fournisseur, éléments de sortie restaurés si nécessaire |
| `POST /v1/responses` SSE | Événements nommés, sans ajout de `[DONE]` |
| `POST /v1/chat/completions` JSON | Messages adaptés en Responses, résultat en `choices` |
| `POST /v1/chat/completions` SSE | Rôle, deltas, raison de fin, consommation optionnelle, `[DONE]` |
| `GET /healthz` | Santé du processus, anonyme |
| `GET /readyz` | Protégé ; vérifie le catalogue, sans réaliser une inférence |
| `GET /openapi.json` | Protégé ; export statique disponible dans le dépôt |
| Autres routes d'inférence | Non implémentées |

## Entrées et sélection

`model` est obligatoire : utiliser un identifiant du catalogue ou `passerelle-default` après `set-default`. Les préférences ne remplacent pas silencieusement un modèle nommé. L'effort explicite de la requête est prioritaire ; un effort indisponible produit une erreur. `--effort instant` correspond à `none` et n'est applicable que si pris en charge pour le modèle.

Responses accepte du texte ou des messages explicites avec `input_text` et `input_image`. Chat accepte `messages` avec texte ou parties `text` et `image_url`. Les images sont réservées aux messages `user` et au backend ChatGPT ; leur sortie est textuelle. `system` est accepté au début et adapté en `instructions` ; sa combinaison avec `instructions` est refusée. `developer` est transmis sur le chemin ChatGPT. L'historique est retransmis explicitement à chaque requête ; la réinjection brute de `response.output` n'est pas implémentée.

Le mode JSON client agrège un flux amont ; OAuth utilise `stream:true` et `store:false` en amont. `stream:true` côté client restitue les événements progressivement. Les erreurs de flux sont reconnues même après l'envoi des en-têtes HTTP.

## Paramètres

| Champ | Contrat |
|---|---|
| `reasoning.effort` / `reasoning_effort` | Transmission de l'effort disponible ; choix réel vérifié contre le catalogue/profil |
| `store` | `false` seulement |
| `stream_options.include_usage` | Chat SSE uniquement avec `stream:true` |
| `n` | Chat : entier 1 uniquement |
| `response_format` | Chat : type `text` seulement |
| `temperature`, `top_p`, plafonds de tokens | Refusés pour ChatGPT OAuth |
| `previous_response_id`, `conversation`, `metadata`, `seed`, `user`, `logprobs`, `stop` | Refusés |
| `tools`, `tool_choice`, résultats de fonctions | Non implémentés dans cet adaptateur |
| Audio, vidéo, Files, embeddings, génération d'images, JSON structuré | Non implémentés |
| Champs inconnus | Refus explicite avant inférence |

Les URL image doivent être HTTPS, ou des data URLs base64 JPEG/PNG/WEBP/GIF non animé. `detail` est transmis parmi `auto`, `low`, `high`, `original`, sous réserve de prise en charge par le modèle. Voir [PHOTOS.md](PHOTOS.md).

## Sorties, limites et erreurs

Responses conserve l'objet terminal ; si son `output` est vide, les événements `output_item.done` reconstituent les éléments reçus. Une sortie terminale non vide reste prioritaire. `usage` reste `null` lorsque le fournisseur ne fournit pas de consommation. Chat traduit la sortie en une choice et préserve le refus et la raison de fin ; les sous-détails de consommation cache/raisonnement ne sont pas traduits.

Les erreurs utilisent `error.message`, `type`, `param`, `code`. Les erreurs de validation deviennent HTTP 400. Un corps JSON non UTF-8 donne `invalid_encoding`. Les codes connus de quota et permission sont adaptés sans exposer le corps brut du fournisseur. HTTP 200 initial ne prouve pas qu'un flux s'est terminé correctement ; un événement `error` signale un échec tardif. Un terminal `incomplete` est distinct de `completed`.

Par défaut : corps 16 Mio, 8 images, 10 Mio par image base64 avant encodage, lecture 30 secondes, sortie amont cumulée 4 Mio, génération 180 secondes, 2 requêtes simultanées, 30 requêtes/minute/clé et 60/IP. Les tentatives admises après validation comptent dans le plafond journalier local même si le fournisseur refuse ensuite la requête ; remise à zéro à minuit UTC. Une seule instance/processus est prévue.

La passerelle ne télécharge pas les URL d'images. `X-Forwarded-For` n'est accepté que depuis un proxy explicitement approuvé par CIDR ; aucun proxy ne l'est par défaut. Les métadonnées `x_gateway` décrivent les efforts et l'alias sans exposer les instructions internes du catalogue.

Les tests automatiques valident les contrats avec transports simulés. Ils ne prouvent ni l'éligibilité d'un compte ni l'accès à chaque modèle. [Limites fournisseur](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations), [erreurs fournisseur](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery).
