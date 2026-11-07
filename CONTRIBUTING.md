# Contribuer

Les corrections, tests de compatibilité et améliorations de documentation sont bienvenus. Ouvrir une issue pour discuter d'une évolution importante avant de l'implémenter. Le projet est encore jeune ; la feuille de route décrit des intentions, sans date de livraison garantie.

## Environnement de développement

Python 3.12+ ; activer un environnement virtuel, puis :

```bash
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
python -m pytest -q
python -m ruff check gateway tests scripts
python scripts/check_publication.py
```

Les tests emploient des transports simulés et des données temporaires. Ils ne nécessitent ni compte ChatGPT ni clé API et ne doivent pas contacter un moteur réel. Les scripts `verify_live.py`, `examples/client.py`, `examples/verify.mjs` et `examples/vision.py` réalisent, eux, de vraies requêtes lorsqu'on les lance et peuvent consommer du quota.

Une PR doit expliquer le problème concret et la validation effectuée. Pour une modification de contrat, mettre à jour `COMPATIBILITY.md`, `gateway/openapi.py` et l'export `openapi.json`. Les commandes de CI couvrent Linux et Windows ; les droits POSIX sont vérifiés sur Linux.

## Données et comportement attendus

Utiliser des jetons factices courts et explicites dans les tests. Ne joindre aucun export de session, clé, fichier `.env`, donnée personnelle, prompt de production ou capture contenant ces informations. Masquer ces données avant tout diagnostic public. Les vulnérabilités se signalent en privé : voir `SECURITY.md`.

Conserver les refus explicites : une option non prise en charge doit produire une erreur, sans suppression silencieuse. Aucun changement ne doit ajouter de backend payant implicite, de réutilisation de session d'une autre application ou de publication de clés.

L'assistance d'une IA est acceptée. Le contributeur doit relire les modifications et rendre les tests reproductibles. La publication initiale de ce projet a été préparée avec une assistance IA ; cela ne remplace pas une revue de sécurité indépendante.

Les contributions sont distribuées sous la licence MIT du projet. Être respectueux, rester centré sur les faits techniques et protéger les données des autres participants.

