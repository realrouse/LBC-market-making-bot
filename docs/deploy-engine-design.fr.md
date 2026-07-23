# Moteur de déploiement — Design & plan par phases

> Statut : **proposition / brouillon à itérer.**
>
> ⚠ **`scripts/deploy_engine.py` (Phase A, l'ordonnanceur parallèle borné) a été RETIRÉ le 2026-07-23** —
> le déploiement natif single-tree a éliminé les étapes bash lentes qu'il parallélisait, le laissant
> orphelin. Ce document est conservé pour l'historique de design ; en attente d'une réécriture P5.
> Companion de [`audit-and-inventory-deploy-plan.md`](audit-and-inventory-deploy-plan.md),
> qui a livré le *dispatcher* piloté par l'inventaire (Phases 1/2/2b **faites**). Ce document
> propose l'étape suivante que ce plan avait différée : remplacer les **moteurs bash** par-famille
> par un **moteur de déploiement générique, parallèle et piloté par l'inventaire**.

---

## 1. Contexte — où on en est, et pourquoi aller plus loin

Le travail de dispatcher est fait : `scripts/deploy.py` dérive le plan de déploiement depuis
`inventory.toml` (fini la topologie triplée), injecte automatiquement l'index de compte de chaque
déployeur depuis `account_idx`, et déduplique. Mais son architecture cible **garde volontairement
les moteurs bash** et les exécute **séquentiellement, un compte à la fois** :

```
inventory.toml ─▶ deploy.py (dérive le plan) ─▶ 7 moteurs bash (~2000 lignes) ─▶ ssh
                                                 deploy_grid_mexc.sh · deploy_grid_binance.sh
                                                 deploy_accumulation.sh · update_swing.sh
                                                 update_standalone.sh · update_claude1.sh · setup_data_plane.sh
```

Ces 7 moteurs font ~300 lignes chacun et **réimplémentent le même pipeline** : parse conf → rsync
(exclude-lists divergentes) → écrit `config.json` (6 sur 7) → venv/pip → refresh `tradinetools` →
kill-stale (`pgrep -f`, sujet au self-match) → install/restart de l'unit systemd → `record_deploy`
(nom de bot hardcodé) → verify. C'est la **dernière grosse duplication** de l'arbre, et la source
des bugs de fragilité bash rencontrés en pratique :

- déploiement sur le **mauvais compte** (index deviné à la main ; `test-account` = idx 6, `the real-money account` = idx 7) ;
- `TEST_STANDALONE_USER_IDX` **écrasé par `source "$CONF"`** (a déjà envoyé tous les déploiements Polymarket sur le compte de test) ;
- `pgrep -f` qui **self-matche** le shell du déployeur lui-même ;
- `record_deploy` qui journalise sous les **anciens noms** (dérive à chaque déploiement après le renommage bot_id).

Et le modèle séquentiel est **lent** : un redéploiement complet de la flotte prend ~18 min (pip +
full rsync + verify **par bot**), même quand rien n'a changé.

**Objectif :** un moteur indépendant, dynamique, rapide — un seul outil Python piloté entièrement
par l'inventaire, avec **parallélisme borné** (défaut **2** connexions SSH simultanées) et les gains
de vitesse déjà prouvés par la fast-path (~18 min → ~1-2 min).

## 2. Objectifs & non-objectifs

**Objectifs**
- Un **moteur générique** — aucune logique par-famille ; un bot est entièrement décrit par des champs déclaratifs.
- **Parallélisme borné**, `jobs` défaut **2**, sûr sur un serveur partagé unique.
- **Plus rapide** : skip du pip si deps inchangées, sync ciblé, restart ciblé, parallèle entre comptes.
- `record_deploy` **conscient du bot_id** (règle le follow-up ouvert du journal).
- Préserver toutes les contraintes de sûreté ops que le dispatcher doit garder (§7).

**Non-objectifs**
- Changer *ce qui* est déployé (mêmes artefacts, services, sémantique de config).
- Un système de CI/cloud distant. On pilote du SSH vers un serveur unique toujours actif (the server), comme aujourd'hui.
- Réécriture big-bang — la migration est famille par famille, moteurs coexistant (§8).

## 3. Architecture cible

```
inventory.toml  (déclaratif : sync set · config · service_env · role · depends_on · serialize_key)
      │
      ▼
moteur de déploiement (Python)
   ├─ Planner    : inventaire → DAG de tâches (deps) + domaines de sérialisation
   ├─ Scheduler  : pool de workers borné (jobs, défaut 2) respectant deps + serialize_key
   └─ Actions    : étapes idempotentes, une bibliothèque partagée par tous les bots
         connect · sync · deps · tradinetools · config · service_install · restart
                  · read_bot_id · record_deploy · verify
      │  (une session ssh/rsync par worker)
      ▼
   the server (acct-1..7)
```

- **Le déploiement d'un bot = un pipeline d'actions** paramétré par sa ligne d'inventaire. Aucun branchement par-famille.
- **Actions idempotentes** et testables indépendamment (ex. `deps` no-op si le hash requirements est inchangé ; `sync` = diff-contenu rsync ; `config` n'écrit que si changé).
- Le moteur est **indépendant** : il possède la connexion, l'idempotence/retry, l'ordonnancement, le logging, le résumé — les pipelines bash s'effondrent en ~10 petites actions typées.

## 4. Modèle de concurrence — la capacité nouvelle centrale

```
jobs = 2 (défaut)              # max de connexions SSH simultanées ; --jobs N ou `jobs` dans l'inventaire
serialize_key (par bot)        # défaut = compte ; deux bots de même clé ne se chevauchent JAMAIS
depends_on (par bot)           # arêtes du DAG ; un bot ne démarre qu'après le succès de ses deps
```

- **Pool de workers** de `jobs` qui exécute les tâches prêtes en parallèle.
- **Domaines de sérialisation** : deux bots de même `serialize_key` (défaut **compte**) s'exécutent
  **strictement en séquence** — ils partagent `~/tradinebotte`, un venv, et la logique kill-stale, donc
  un rsync/restart concurrent ferait une race. Les bots de comptes **différents** tournent en parallèle
  jusqu'à `jobs`.
- **Dépendances** : les arêtes `depends_on` font attendre à un consommateur que son data-plane soit up
  (grids → `cex_feed`, accumulations → `indicators`, `account_bot` → feeds). Ceci **remplace le bloc
  order-critical acct-1 hardcodé** par un DAG déclaratif.
- **Réconcilier la règle mémoire « jamais parallèle, même serveur »** : cette règle était un garde-fou
  grossier. Le moteur en garde *l'intention* — jamais deux opérations conflictuelles à la fois — de
  façon précise, via les domaines de sérialisation + un défaut `jobs` bas (2) pour plafonner l'IO/CPU
  de l'hôte, tout en parallélisant les comptes réellement indépendants. Ce n'est *pas* une invitation à `jobs=12`.
- **Politique d'échec** : un bot en échec fait échouer ses dépendants (marqués *skipped*), est enregistré,
  mais les branches indépendantes continuent ; la run finit sur un résumé par-bot et un exit non-nul.

## 5. Ajouts au schéma d'inventaire

Champs déclaratifs qui permettent au moteur d'abandonner le bash. Tous optionnels avec défauts sûrs ;
garder `deployer`/`deploy_script` pendant la migration comme fallback.

| Champ | Type | Rôle |
|---|---|---|
| `role` | str | déjà implicite ; nomme le `bot_id` (`{exchange}-{strategy}-{pair}`) et son fichier `bot_id_<role>` |
| `sync` (ou `family`) | list / str | chemins repo (ou set nommé) à pousser → remplace les exclude-lists par-moteur |
| `config` | table | `config.json` déclaratif (`strategy`, `data_source`, `feed_addr`, …) → remplace les heredocs inline |
| `service_env` / `env_file` | table / path | `Environment=` / `EnvironmentFile=` systemd (ex. `TRADINEBOTTE_DIR`, `TRADINEBOTTE_IPV4_ONLY`, la clé MEXC staged) — secrets par chemin, jamais en git |
| `serialize_key` | str | clé d'exclusion mutuelle du scheduler (défaut = compte) |
| `depends_on` | list | bot_names / roles à avoir up d'abord |
| `data_dir` | str | dir de données s'il diffère du dir code/`install_dir` |
| `jobs` | int (niveau fichier) | plafond de parallélisme par défaut (défaut 2) |

Exemple (déclaratif, sans bash) :
```toml
[[bot]]
account_idx  = 7
bot_name     = "mexc-grid-lbcusdt-a00f5f"
role         = "grid"
sync         = "cex-grid"                         # set de fichiers nommé : live_bot.py, botcore, connectors, api_mexc, tradinetools
config       = { strategy = "strategies/grid/grid_LBC_USDT_mexc.json", data_source = "cex_feed", feed_addr = "tcp://127.0.0.1:5563" }
service_unit = "tradinebotte-live.service"
install_dir  = "~/tradinebotte"
depends_on   = ["infra-cexfeed-0e7b3a"]
is_live      = false
```

## 6. Vitesse — où passe le temps et comment il baisse

| Coût actuel (par bot) | Correctif |
|---|---|
| `pip install` même inchangé | **skip** quand le hash de `requirements.txt` matche un stamp distant |
| full rsync de l'arbre | **sync ciblé** depuis le set de fichiers du rôle (rsync reste diff-contenu) |
| kill-stale + poll + verify | restart ciblé via **`MainPID` systemd** ; verify depuis le heartbeat, pas une boucle sleep |
| strictement séquentiel | **parallèle** entre comptes (jobs) |

Combiné à la fast-path prouvée (push fichiers changés + restart), un redéploiement flotte passe de
**~18 min → ~1-2 min**, et une itération single-bot de ~90 s → quelques secondes.

## 7. Contraintes de sûreté que le moteur DOIT préserver

Reprises de la mémoire ops + `audit-and-inventory-deploy-plan.md §"Constraints"` :
- **Pas de self-match process :** cibler le **`MainPID` systemd** du service (ou le cgroup), jamais `pgrep -f`.
- **Ordre acct-1 :** les feeds doivent être up+flowing **avant** `account_bot` ; encodé en `depends_on`.
  `--restart-infra` garde le contrôle des restarts infra disruptifs ; le défaut laisse l'infra tranquille.
- **Substitution `{account}` du `service_unit`** pour l'unit `account_bot` par-utilisateur.
- **Pas de bug conf-source :** le moteur calcule la cible depuis `account_idx` en process ; il ne source
  jamais le conf dans un shell qui porte aussi une variable d'index.
- **`tradinetools` avant restart :** refresh site-packages avant de redémarrer tout bot qui importe un
  nouveau symbole (la classe de crash-loop `resolve_bot_id`).
- **Flow de test :** `--dry-run` + le compte éphémère `test-account` (wipe complet quand OK) avant les vrais
  comptes ; `prepare_release.sh` avant tout merge sur `main`.
- **`record_deploy`** lit le `bot_id_<role>` distant et journalise sous le **bot_id**.
- **`--exclude=bot_id*`** au sync pour qu'un redéploiement n'écrase jamais l'identité d'un bot.

## 8. Plan d'implémentation par phases

Chaque phase est livrable indépendamment, dry-runnable, et validée d'abord sur `test-account`.

**Phase A — Scheduler + runner d'actions, moteurs encore bash (risque minimal).**
Nouveau moteur `deploy/` : DAG de tâches depuis l'inventaire, pool borné (`--jobs`, défaut 2),
`serialize_key` (= compte) et `depends_on`. L'unique "action" de chaque bot shelle encore vers son
déployeur bash **existant**. *Gain :* parallélisme + ordre de dépendances **immédiatement**, sans aucun
changement de logique de déploiement. `deploy_all.sh`/`deploy.py` deviennent des shims sur le moteur.
Valider : `--verify-only` flotte, timings (attendre ~Nx), une dépendance forcée (grid attend cex_feed).

**Phase B — Actions natives, pilote grid.**
Implémenter la bibliothèque d'actions (`sync`/`deps`/`config`/`service`/`restart`/`read_bot_id`/`record_deploy`/`verify`)
et piloter la famille **grid** nativement depuis les champs déclaratifs (`sync`/`config`/`service_env`),
en contournant `deploy_grid_mexc.sh` / `deploy_grid_binance.sh`. Diff natif-vs-bash sur `test-account`
(mêmes fichiers, même service, même heartbeat). Retirer les deux moteurs bash grid.

**Phase C — Migrer les familles restantes.**
accumulation → swing → polymarket `update_standalone.sh`. Déplacer chaque preset dans les champs
d'inventaire ; supprimer le moteur bash une fois sa famille verte. Après C, seuls restent les scripts
bespoke acct-1.

**Phase D — Infra (acct-1) déclarative (blast radius max → en dernier).** *(déployeurs construits + validés ; cutover prod déféré en E.)*
`deploy_actions.py` a gagné une spec `INFRA` + `deploy_infra()` pour les 6 services infra
(`indicators`/`feed`/`feed5m`/`cexfeed`/`status`/`account`). Décision clé — **le déploiement infra ne
réécrit jamais l'unit** (`act_service_restart` install-if-absent), calqué sur `update_claude1.sh`
(`_restart_service` = rsync `.py` + restart, unit intacte) ; c'est ce qui préserve l'env hand-set de
l'unit remote. Validé sur `test-account` :
- **feed 15M → GREEN** avec le check discriminant : l'unit qui tourne porte
  `TRADINEBOTTE_MARKET_TAG_ID=102467`, installée depuis le **nouveau template baked**
  `tradinebotte-feed15m.service`, et `feed.log` montre `BTC 15M markets (tag=102467)` résolvant
  activement — via IPC per-user (`ipc:///run/user/N/…`, pas de collision host-wide).
- **cexfeed / indicators → pipeline validé, verify RED correct.** Tous les steps mécaniques ont tourné ;
  ne peuvent pas être green car ils bindent des **ports singleton loopback host-wide détenus par le prod
  acct-1** (cexfeed 5563 ; la config indicators force TCP 5559/5561). Finding structurel, pas un défaut :
  une instance infra par hôte.
- **feed5m / status / account** → couplage même-singleton ou `sg claudes`+shared-DB ; l'acceptation est le
  **diff d'équivalence statique** vs chaque déployeur bash (comme en Phase C), qui tient.

**Wart du tag éliminé :** le tag 15M était injecté impérativement par `setup_data_plane.sh`
(`sed -i … 102467`) car l'ancien `tradinebotte-feed.user.service` était tagless. Il est maintenant
**baked dans `tradinebotte-feed15m.service`** (symétrique du 102892 de feed5m). Le natif l'utilise ; le
`sed` bash live est laissé en place (chemin prod, intact) → **à retirer au cutover E**. Aussi remonté :
`tradinebotte-feedwatchdog.service/.timer` tourne sur acct-1 mais est **absent de l'inventaire** (gap de
tracking). Note observabilité : les units `StandardError=null` (indicators) cachent les crashes du journal
→ le check **MainPID systemd est le signal porteur**, pas le grep de log.

**Phase E — Enforce, vitesse, cleanup.**
pip-skip (hash requirements), ciblage `MainPID` systemd, `record_deploy` sous bot_id, `check_inventory`
valide les nouveaux champs (`sync`/`config`/`depends_on`/`serialize_key`) + DAG acyclique ; **supprimer
tous les moteurs bash** (~2000 lignes) et l'indirection par variable d'index de compte.
Aussi au cutover : **brancher `deploy_engine` → `deploy_family`/`deploy_infra` natifs** (dériver
family/service depuis `bot_type` + lire `deploy_env`, tout depuis l'inventaire — pour que 5557/mexc/etc.
cessent d'être des hardcodes de FAMILIES) ; **retirer le `sed … 102467` de `setup_data_plane.sh`** (redondant —
le tag est baked) ; **ajouter la row `feedwatchdog`** à l'inventaire.

**Profil de ports test test-account (quicktest indépendant).** La Phase D a prouvé que les services infra ne
peuvent pas être green sur `test-account` car ils bindent des **ports singleton loopback host-wide détenus par
le prod acct-1** (feed5m 5557, cexfeed 5563, indicators 5559/5561, status 5562) — le serveur partagé a une
instance par port. Fix : un flag de déploiement (ex. `--test-ports`, implicite quand la cible est l'index
test-account) qui applique un **offset uniforme `+10`** à chaque adresse TCP que l'unit/config déployée bind —
5557→5567, 5559→5569, 5561→5571, 5562→5572, 5563→5573 — via un drop-in systemd
(`Environment=…_ADDR=tcp://127.0.0.1:<port+10>`) pour les services pilotés par env et une réécriture de config
pour les `zmq_out_addr`/`zmq_reg_addr` d'indicators. Alors **toute la pile infra tourne self-contained sur
test-account** (ses propres feed5m + cexfeed + indicators + status, consumers pointés sur les ports offset),
donnant un vrai quicktest infra end-to-end avec **zéro collision** avec le prod. Le feed 15M n'a besoin
d'aucun offset (IPC per-user dans `/run/user/%U/`). Garder ça comme un *profil*, pas un hack par service :
une constante d'offset, appliquée uniformément, pour que la topo de test reflète le prod exactement à la
constante près. Wipe comme d'habitude après (test-account reste éphémère, selon sa politique).

**Fondation du pont natif Phase E — DONE (2026-07-13) ; cutover prod + suppression du bash toujours déférés.**
Les pièces qui rendent le natif atteignable depuis l'inventaire, toutes offline/testables :
- `deploy_actions.native_target(bot_type)` → `(kind, target)` mappe chacun des 17 bot_types de l'inventaire vers
  `deploy_family`/`deploy_infra` (0 non-mappé ; ordonné pour que `polymarket-multibot`→account batte la règle
  polymarket générique, et `infra-feed-15m/5m` battent un feed générique).
- `deploy_engine.py --native` imprime le plan natif **review-only** dérivé de l'inventaire (ordre fichier =
  acct-1 feeds-avant-account_bot) ; il résout la strategy de chaque famille depuis `deploy_env` et **signale les
  gaps**. Il a révélé 2 rows s'appuyant sur une strategy *par défaut* du bash absente de l'inventaire (swing
  acct-5, grid mexc-futures acct-6) — comblés en écrivant explicitement `TEST_SWING_STRATEGY` /
  `TEST_GRID_MEXC_STRATEGY` (= le défaut propre de chaque script → 0 changement de comportement), donc
  l'inventaire est maintenant auto-descriptif pour le chemin natif (plan **0 gap**).
- `check_inventory.check_native_coverage` impose (1) chaque bot_type a une cible native et (2) le graphe
  `depends_on` est ACYCLIQUE — les deux offline. Tests : `tests/test_deploy_actions.py` (12) +
  `test_check_inventory` (DAG cycle/acyclique/dep inconnue) verts.

Toujours **déféré** (nécessite un déploiement prod déclenché par l'utilisateur + observation — blast radius
feed/infra) : câbler `--native` pour *exécuter* réellement (appeler `deploy_family`/`deploy_infra` in-process au
lieu du script bash), retirer le `sed` du tag de `setup_data_plane.sh`, la row `feedwatchdog` (elle n'émet pas de
heartbeat → il faut un marqueur "no-heartbeat" pour ne pas polluer le set attendu de la statuspage), et
**supprimer les ~2000 lignes de bash**. Ne PAS supprimer le bash tant que l'exécution `--native` n'est pas
prouvée en prod.

## 9. Risques & mitigations

- **Contention parallèle même hôte** → `jobs` borné (2) + `serialize_key` ; jamais deux ops dans un domaine de sérialisation.
- **Risque big-bang** → famille par famille ; le moteur coexiste avec le bash jusqu'à la Phase C ; une phase ratée revient au moteur bash pour cette famille.
- **Disruption infra** → Phase D en dernier, gated par `--restart-infra`, data plane vérifié après restart.
- **Quirks par-famille cachés dans le bash** (exclude-lists spéciales, migrations one-off comme le déplacement de la DB accum) → les faire remonter en champs d'inventaire explicites pendant B/C ; le diff natif sur `test-account` attrape la dérive.
- **Testabilité** → `test-account` (idempotent, wipé) + `--dry-run` + tests de régression `test_deploy` étendus par phase.

## 10. Questions ouvertes

1. Défaut `serialize_key` — **compte** (sûr ; home/venv partagés) vs `install_dir` (plus fin, laisserait
   `~/tradinebotte` et `~/tradinebotte-grid` d'un même compte tourner en parallèle — mais ils partagent le venv). Commencer par **compte**.
2. `config` — table déclarative complète vs template par-famille ? La table déclarative suffit pour les configs actuelles.
3. Défaut `jobs` 2 — champ d'inventaire global **et** override `--jobs` (l'override gagne). Jamais d'auto-scale.
4. Garder les moteurs bash en fallback par-famille pendant une release après chaque migration, ou supprimer au vert ?
5. Les bots désactivés (ex. `orderbook_bot`) doivent-ils porter une ligne `enabled = false` pour que l'inventaire reste l'état désiré complet, le moteur les skippant ?
