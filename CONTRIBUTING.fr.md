# Contribuer

> 🇬🇧 [English version](CONTRIBUTING.md)

## Politique de langue

Le projet est **bilingue au niveau de la documentation** et **exclusivement en anglais au niveau du code** :

| Artefact | Langue |
|---|---|
| `README.md`, `CHANGELOG.md`, `INSTALL.md`, `QUICKSTART.md`, `UPDATE.md`, `CONTRIBUTING.md` | Anglais |
| `README.fr.md`, `CHANGELOG.fr.md`, `INSTALL.fr.md`, `QUICKSTART.fr.md`, `UPDATE.fr.md`, `CONTRIBUTING.fr.md` | Français |
| Code source (`.py`, `.sh`, `.json`) | **Anglais uniquement** |
| Commentaires de code | **Anglais uniquement** |
| Messages de log | **Anglais uniquement** |
| Docstrings | **Anglais uniquement** |

Ne jamais écrire en français dans le code source, les commentaires, les messages de log ou les docstrings.

## Règle de documentation bilingue

La documentation est maintenue sous forme de paires EN/FR. **Les deux fichiers d'une paire doivent être mis à jour dans le même commit** — ne jamais modifier l'un sans mettre à jour son équivalent :

| Anglais | Français |
|---|---|
| `README.md` | `README.fr.md` |
| `CHANGELOG.md` | `CHANGELOG.fr.md` |
| `INSTALL.md` | `INSTALL.fr.md` |
| `QUICKSTART.md` | `QUICKSTART.fr.md` |
| `UPDATE.md` | `UPDATE.fr.md` |
| `CONTRIBUTING.md` | `CONTRIBUTING.fr.md` |

## Processus de release

Avant chaque merge de `dev` vers `main`, exécuter le script de pré-release :

```bash
bash scripts/prepare_release.sh
```

Ce script exécute la checklist complète (tests, linter, version, CHANGELOG) et s'arrête en cas d'échec bloquant. Ne jamais merger `dev → main` sans l'avoir exécuté au préalable.

## Exécution des tests

```bash
bash scripts/run_tests.sh
```

1163 tests répartis en 6 suites. Aucun accès réseau ni credentials requis — base SQLite en mémoire pour chaque test.

## Qualité du code

```bash
# Linter (cible : 9,91/10, avertissements non bloquants uniquement)
pylint tradinebotte-cex tradinebotte-indicators tradinebotte-polymarket tradinebotte-status tradinetools

# Vérificateur de types (doit retourner 0 erreur)
mypy tradinebotte-polymarket tradinebotte-cex tradinebotte-indicators tradinetools --ignore-missing-imports
```
