# Déployer une instance personnelle

Le déploiement par défaut écoute uniquement sur localhost. Chaque opérateur connecte son propre compte éligible. La publication du code sous licence MIT ne confère aucun droit supplémentaire d'accès au fournisseur, de partage de compte ou d'exploitation d'un service commercial. Consulter le [périmètre OSS/local](https://developers.openai.com/siwc/token-sharing-open-source) et le [guide des VM auto-hébergées](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms).

## Installation native

Le [README](README.md) décrit la création de l'environnement virtuel, l'installation, `init`, `login`, `models` et `set-default`. La configuration est lue depuis les variables `GW_*` et, éventuellement, le fichier `.env`. Les valeurs par défaut sont décrites dans [gateway/config.py](gateway/config.py) ; `.env.example` fournit une base locale sans secret.

Pour utiliser Ollama sur la même machine, le démarrer séparément puis choisir explicitement `GW_PROVIDER=ollama`. L'adresse doit être HTTP sur loopback. Ce backend n'utilise pas le compte ChatGPT et ne prend pas en charge les images dans cet adaptateur. Le Compose fourni est destiné à ChatGPT OAuth.

## Docker Compose

Docker Engine et le plugin Compose sont nécessaires. Depuis la racine du dépôt :

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose exec gateway python -m gateway.cli init
```

`/healthz` indique que le processus fonctionne, pas que le compte est connecté. Le conteneur utilise UID/GID 10001, un système de fichiers en lecture seule et le volume `gateway_data` monté dans `/data`. Le port 8787 est publié sur `127.0.0.1` par défaut.

### Connecter le compte sur un serveur sans navigateur

Effectuer d'abord `login` sur une installation native de confiance, selon le README. Le callback OAuth utilise un port loopback local : le simple `docker compose exec ... login` n'est donc pas un parcours de connexion utilisable depuis un navigateur externe.

Après consentement, transférer l'état par SSH chiffré. Sur un poste Linux/macOS de confiance, adapter `operator@example-host` et le répertoire distant :

```bash
set -o pipefail
python -m gateway.cli export-state | ssh operator@example-host \
  'cd /srv/passerelle-chatgpt && docker compose exec -T gateway python -m gateway.cli import-state --stdin'
```

L'export transporte des secrets : ne pas l'afficher, le joindre à une issue ou le stocker dans Git. La CLI refuse l'export directement vers un terminal. Exécuter cette commande une fois, avant de servir des clients. L'import conserve l'identifiant hôte du serveur et transfère les clés et préférences. Après transfert, arrêter l'instance native et ne plus renouveler cette copie de session : les refresh tokens rotatifs ne doivent pas être utilisés par deux instances actives. Pour PowerShell, préférer un transfert binaire via un outil SSH plutôt qu'un pipeline de texte susceptible de réencoder les données.

```bash
docker compose exec gateway python -m gateway.cli models
docker compose exec gateway python -m gateway.cli set-default --model VOTRE_MODELE --effort auto
docker compose exec gateway python -m gateway.cli preferences
```

Lancer un exemple client est une vérification d'inférence réelle et consomme du quota. Sans consentement ni appel réussi, une construction Docker et un test de santé ne prouvent pas l'accès ChatGPT.

### Clés et administration

```bash
docker compose exec gateway python -m gateway.cli key-create --name mon-client --days 30 --daily-limit 100
docker compose exec gateway python -m gateway.cli keys
docker compose exec gateway python -m gateway.cli key-revoke IDENTIFIANT
docker compose exec gateway python -m gateway.cli logout
docker compose stop
docker compose up -d
```

Une nouvelle clé est écrite dans un fichier privé sous `/data`, et sa valeur n'est pas affichée. La récupérer uniquement par un canal privé de confiance. Une clé `gw_…` est une clé de cette instance ; elle ne fonctionne pas directement sur `api.openai.com`.

## Accès depuis un autre appareil

Pour un accès personnel distant, un tunnel SSH évite de publier directement le port :

```bash
ssh -N -L 8787:127.0.0.1:8787 operator@example-host
```

Le client utilise alors `http://127.0.0.1:8787/v1` et sa clé locale. Cela ne change pas l'authentification du fournisseur.

Un frontal HTTPS est possible pour une instance personnelle dont le périmètre a été vérifié. [deploy/Caddyfile.example](deploy/Caddyfile.example) est un exemple à adapter : domaine, certificat public, hôte autorisé dans `GW_ALLOWED_HOSTS`, filtrage réseau et transmission immédiate du SSE. Il faut un domaine que l'opérateur contrôle et un routage réseau adapté. Le dépôt n'effectue aucune configuration DNS, NAT ou exposition publique automatique. Ne pas exposer HTTP 8787 directement sur Internet.

## Sauvegarder, restaurer et mettre à jour

Arrêter le conteneur avant une sauvegarde cohérente du volume, avec l'outil de sauvegarde de l'opérateur. Sauvegarder ensemble SQLite, `accounts.enc`, `master.key`, les fichiers de clés et les métadonnées de session ; chiffrer la sauvegarde et restreindre son accès. `master.key` permet de déchiffrer les comptes. Restaurer le volume avec UID/GID 10001 et des droits privés, puis démarrer une seule instance. Garder un ancien état peut conduire à restaurer un refresh token déjà consommé : une nouvelle connexion sera alors nécessaire.

Pour une mise à jour, conserver la version Git précédente et une sauvegarde privée, récupérer une version identifiée puis reconstruire : `docker compose up -d --build`. Vérifier `/healthz`, une requête sans clé (refus attendu) puis, volontairement, une inférence avec clé. Les changements de schéma futurs nécessiteront des instructions de migration explicites. Ne pas exécuter `docker compose down -v` pour une mise à jour : cette option supprime les données.
