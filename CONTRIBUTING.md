# Contributing

> 🇫🇷 [Version française](CONTRIBUTING.fr.md)

## Language policy

The project is **bilingual at the documentation level** and **English-only at the code level**:

| Artifact | Language |
|---|---|
| `README.md`, `CHANGELOG.md`, `INSTALL.md`, `QUICKSTART.md`, `UPDATE.md`, `CONTRIBUTING.md` | English |
| `README.fr.md`, `CHANGELOG.fr.md`, `INSTALL.fr.md`, `QUICKSTART.fr.md`, `UPDATE.fr.md`, `CONTRIBUTING.fr.md` | French |
| Source code (`.py`, `.sh`, `.json`) | **English only** |
| Code comments | **English only** |
| Log messages | **English only** |
| Docstrings | **English only** |

Never write French in source code, comments, log messages, or docstrings.

## Bilingual documentation rule

Documentation is maintained as paired EN/FR files. **Both files in a pair must be updated in the same commit** — never modify one without updating its counterpart:

| English | French |
|---|---|
| `README.md` | `README.fr.md` |
| `CHANGELOG.md` | `CHANGELOG.fr.md` |
| `INSTALL.md` | `INSTALL.fr.md` |
| `QUICKSTART.md` | `QUICKSTART.fr.md` |
| `UPDATE.md` | `UPDATE.fr.md` |
| `CONTRIBUTING.md` | `CONTRIBUTING.fr.md` |

## Release process

Before every merge from `dev` to `main`, run the pre-release script:

```bash
bash scripts/prepare_release.sh
```

This runs the full pre-release checklist (tests, linter, version bump, CHANGELOG) and aborts on blocking failures. Never merge `dev → main` without running it first.

## Running tests

```bash
bash scripts/run_tests.sh
```

1163 tests across 6 suites. No network access or credentials required — in-memory SQLite for every test.

## Code quality

```bash
# Linter (target: 9.91/10, non-blocking warnings only)
pylint tradinebotte-cex tradinebotte-indicators tradinebotte-polymarket tradinebotte-status tradinetools

# Type checker (must report 0 errors)
mypy tradinebotte-polymarket tradinebotte-cex tradinebotte-indicators tradinetools --ignore-missing-imports
```
