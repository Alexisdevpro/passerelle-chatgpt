# Sécurité

## Signaler une vulnérabilité

Utiliser **Security → Report a vulnerability** dans [le dépôt GitHub](https://github.com/Alexisdevpro/passerelle-chatgpt/security/advisories/new). Si cette fonction est indisponible, ouvrir une issue demandant un canal privé, sans détail exploitable ni secret. Il n'y a pas de délai de réponse garanti ; le projet est maintenu bénévolement. La dernière version publiée est la version suivie pour les correctifs.

## Périmètre de confiance

La passerelle est destinée à une instance personnelle. Les clients possèdent des clés locales d'inférence, sans endpoint d'administration distante. L'opérateur, le système, le navigateur OAuth et le fournisseur sont des composants de confiance. La passerelle n'exécute ni shell ni outil de modèle. Les protections applicatives ne remplacent pas la sécurité de la machine ou du réseau.

| Risque | Mesure | Limite |
|---|---|---|
| Requête sans autorisation | Clés aléatoires, digest SHA-256, expiration, révocation, modèles autorisés | Un détenteur de clé peut consommer le quota autorisé |
| Site hostile | Hôtes autorisés, origines navigateur refusées par défaut | Un logiciel local compromis sort de ce modèle |
| Session OAuth interceptée | PKCE S256, state, nonce, validation JWKS/issuer/audience/expiration | Le navigateur et l'OS doivent être fiables |
| Course de renouvellement | Verrou interprocessus, sérialisation async, écritures atomiques | Une même session ne doit pas tourner sur deux stockages indépendants |
| Saturation | Plafonds IP/clé, concurrence, délais, taille de corps et de sortie bornées | Pas de protection DDoS réseau |
| Images indésirables | Validation MIME/base64 et limites ; aucune URL téléchargée par la passerelle | Les URL et fichiers complets sont validés par le fournisseur |
| Faux succès du streaming | Événement terminal requis, erreurs tardives normalisées | HTTP 200 initial ne prouve pas le succès d'une génération |
| Adresse client usurpée | Aucun proxy approuvé par défaut ; liste CIDR explicite si nécessaire | Derrière un frontal non approuvé, la limite IP agrège ses clients |

## Stockage et journaux

Les tokens sont chiffrés avec Fernet dans `accounts.enc`. `master.key`, situé sur le même hôte, permet le déchiffrement : cela protège contre la lecture isolée du fichier chiffré, pas contre une compromission du système ou une copie complète du répertoire. Les fichiers privés sont en mode 0600 sous POSIX et les répertoires en 0700. Sous Windows, protéger le répertoire avec les ACL du compte ; les modes POSIX ne garantissent pas cet isolement.

Les clés d'inférence sont hachées en SQLite ; leur copie initiale ou nouvellement créée se trouve aussi dans un fichier privé afin que l'opérateur puisse la récupérer. Les fichiers `.env`, `.local`, clés, exports et volumes ne doivent jamais être publiés. Le `.gitignore`, le contexte Docker restreint et le contrôle de publication réduisent les erreurs, sans constituer un audit exhaustif de secrets.

Le journal applicatif enregistre les métadonnées techniques nécessaires, sans prompt ni jeton. Les photos sont transmises en mémoire au fournisseur. L'option `--save-request` d'un client écrit une copie contenant les données envoyées : ce fichier relève de la protection du poste client. Une réponse de modèle peut reproduire des données contenues dans son entrée.

## Réseau et sessions

Le service écoute sur localhost par défaut. Toute exposition distante exige un transport protégé et les restrictions réseau adaptées au périmètre personnel. Les routes `/readyz` et `/openapi.json` nécessitent une clé ; `/healthz` est anonyme. Le catalogue n'est pas une preuve de disponibilité de l'inférence.

En cas de fuite, révoquer les clés locales concernées et déconnecter la session ChatGPT dans les réglages du compte, puis reconnecter proprement la passerelle. Une sauvegarde contient des secrets et doit être chiffrée. Les erreurs de renouvellement temporaires ne doivent pas entraîner une suppression arbitraire de session ; les erreurs terminales imposent une reconnexion.

Le projet n'a pas bénéficié d'un audit de sécurité indépendant. Les tests disponibles sont décrits dans [TEST-REPORT.md](TEST-REPORT.md).
