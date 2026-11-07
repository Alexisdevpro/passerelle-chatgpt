# Vérifications de la publication 0.4.0

Date : 7 novembre 2026. Cette édition publique est préparée depuis la version personnelle 0.3.1, sans copier ses credentials, réglages réseau ou journaux d'exploitation.

## Contrôles locaux

Sur Windows avec Python 3.14, dans un environnement virtuel neuf :

| Contrôle | Résultat |
|---|---|
| Installation de `requirements-dev.lock` | Réussie |
| `python -m pytest -q` | 90 tests réussis |
| `python -m ruff check gateway tests scripts` | Réussi |
| `python -m pip check` | Aucune incohérence détectée |
| Construction et installation du package 0.4.0 | Réussies |
| `docker compose config --quiet` | Configuration acceptée |
| `node --check examples/verify.mjs` | Syntaxe acceptée |
| Audit de `requirements.lock` via pip-audit | Aucune vulnérabilité connue détectée à cette date |

Les tests couvrent l'authentification, les permissions de modèles, quotas, limites de taille et de concurrence, schémas de requêtes, texte et images, JSON/SSE, erreurs tardives, sélection des modèles, OIDC, rotation des sessions, migration privée et annulation par déconnexion réelle de socket. Les appels fournisseur sont simulés ; le test de socket utilise un serveur HTTP temporaire local.

## CI et limites de validation

Le [workflow CI](https://github.com/Alexisdevpro/passerelle-chatgpt/actions/workflows/ci.yml) définit les tests sur Linux Python 3.12 et 3.14, Windows Python 3.14, le contrôle de publication, la construction Docker et un contrôle HTTP du conteneur. Consulter la conclusion du run correspondant au commit : la présence du workflow ne prouve pas à elle seule sa réussite.

Le moteur Docker local était arrêté pendant la préparation ; la construction et l'exécution Linux sont confiées au job Docker de CI. Aucun appel ChatGPT réel ni nouvelle connexion OAuth n'a été effectué pour cette publication. Les résultats de la précédente installation personnelle ne sont pas présentés comme une validation de cette édition sur tous les comptes. Les tests automatiques ne démontrent ni l'éligibilité du compte au fournisseur ni l'accès à chaque modèle.

`scripts/check_publication.py` vérifie les fichiers Git publiables, certains formats de secrets, les fichiers d'exécution privés et les liens Markdown locaux. Il ne constitue pas une recherche exhaustive de secrets ou un audit indépendant.

Pour une validation réelle volontaire, installer le SDK `openai` et utiliser `scripts/verify_live.py` contre une instance personnelle connectée. Ce script consomme du quota et écrit un rapport qui peut contenir les sorties de modèle et l'adresse de l'instance ; conserver ce rapport hors du dépôt. Aucune clé ou session n'est nécessaire aux jobs CI.
