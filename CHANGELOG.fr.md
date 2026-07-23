# Journal des modifications

> 🇬🇧 [English version](CHANGELOG.md)

Toutes les modifications notables de ce projet sont documentées ici.

---

## [Non publié]

- **Le lanceur de tests utilise désormais pytest, si bien que l'isolation des tests s'applique toujours.** `scripts/run_tests.sh` exécutait la suite avec `unittest discover`, qui ne charge jamais le `conftest.py` redirigeant les poussées de test hors du collecteur de statut de production — le garde-fou ne fonctionnait que si quelqu'un lançait pytest à la main. Le lanceur invoque maintenant pytest (un appel par dossier de tests, préservant la résolution de modules de chaque sous-projet et son propre conftest) ; les classes de test façon unittest s'exécutent sans changement. La redirection vers un port mort que le lanceur exporte aussi reste un plancher à l'échelle du projet pour les sous-projets sans conftest.

- **Le CI exécute la même suite pytest que le lanceur local, et couvre désormais le service status.** Le job de tests GitHub Actions invoquait `unittest discover` directement et n'installait que les dépendances runtime — il ne correspondait donc ni au lanceur canonique ni au chargement du `conftest.py` d'isolation, et il omettait silencieusement toute la suite de tests `tradinebotte-status`. Il installe maintenant pytest, exécute pytest par dossier comme `scripts/run_tests.sh`, et inclut la suite status. `pytest` est déclaré dans `requirements-dev.txt`, et le lanceur échoue avec un message clair s'il est absent plutôt qu'une erreur d'import brute.

- **Tous les templates d'unités systemd vivent désormais dans un unique répertoire `systemd/` à la racine.** Ils étaient éparpillés dans `tradinebotte-polymarket/scripts/systemd/` (devenu l'emplacement partagé de fait — même les unités indicators et cex-feed vivaient sous le sous-projet polymarket), plus `tradinebotte-cex/` et `tradinebotte-status/`. Le déployeur natif n'a plus besoin d'un répertoire par service : chaque template (famille et infra) se résout en `systemd/<unité>` depuis une seule constante. Un nouveau test vérifie que chaque template nommé par une spec de déploiement existe bien là, si bien qu'un mauvais chemin échoue en CI plutôt qu'à un vrai déploiement.

- **Le lanceur de tests exerce désormais aussi le harness de fidélité du moteur CEX.** `scripts/run_tests.sh` lançait le backtest Polymarket (`backtest.py --all`) mais pas `backtest_engine.py`, qui pilote les vrais moteurs grid/swing sur des klines rejouées et rapporte leur écart avec les backtests réimplémentés. Il lance maintenant le harness sur un jeu de klines quand il y en a un — conditionné à la présence d'une table `klines`, donc il est ignoré proprement en CI (les bases de données ne sont pas dans git, où le test de parité en mémoire garde déjà le moteur) et tourne localement pour quiconque a des données kline. Non bloquant.

### Modifié
- **Le déploiement est désormais uniformément natif single-tree pour tous les bots de trading.** Tous les bots (polymarket, accumulation, grid, swing) se déploient via l'unique moteur déclaratif (`scripts/deploy_actions.py`, dispatché depuis `inventory.toml` par `scripts/deploy.py`), chacun dans l'arbre partagé `~/tradinebotte` sous des noms par instance (`config_<role>.json`, `live_<role>.db`, un drop-in systemd single-tree). Les derniers déployeurs bash par famille (`update_swing.sh`, `deploy_grid_mexc.sh`) ont été retirés ; déployer un bot ou toute la flotte avec `bash tradinebotte-cex/scripts/deploy_all.sh [--only "<compte> — <label>"]`.
- **`tradinetools` est importé depuis la source via un `.pth`, jamais une copie.** La bibliothèque partagée était copiée dans le virtualenv de chaque compte, où elle dérivait silencieusement de la source et imposait un rituel « rafraîchir avant restart sinon crashloop ». Elle est maintenant sur le chemin de l'interpréteur via un simple `.pth` pointant vers la source — un rsync de la source est instantanément actif, sans copie à périmer ni dist-info cassant les restarts.
- **Les bots Polymarket lisent leur wallet depuis une variable d'environnement, pas `config.json`.** Comme les clés d'exchange des bots CEX (`MEXC_API_KEY`), le wallet Polymarket se lit désormais depuis `POLY_PRIVATE_KEY` (un fichier env 0600 chargé par l'unité systemd), si bien que la config d'un bot est self-contained et que son déploiement l'écrase proprement — ce qui a permis à polymarket de rejoindre le déploiement single-tree uniforme.

### Ajouté
- **`scripts/transfer_bot.py`** — déplacer un bot de trading d'un compte à un autre : il transporte l'état et l'identité du bot, le redéploie en natif sur la cible, le retire de la source, et réconcilie la base d'état partagée pour que le bot n'apparaisse que sur un seul compte. Pour rééquilibrer les comptes ou vider un compte jusqu'aux seuls services d'infrastructure.

- **Sept templates systemd et scripts morts laissés par des retraits antérieurs.** `tradinebotte-cex/scripts/systemd/tradinebotte-accumulation.service` lançait `accumulation_bot.py`, un point d'entrée qui n'existe plus (l'accumulation passe par `live_bot.py`) ; son voisin `tradinebotte-orderbook.service` et le script `deploy_scalping_claude4.sh` (compte codé en dur) déployaient le bot orderbook, désactivé car non rentable depuis juin. `tradinebotte-indicators/scripts/systemd/tradinebotte-indicators.service` ainsi que `install_service.sh` + `tradinebotte.service` étaient les installeurs single-bot et system-unit dont les scripts d'installation avaient déjà été supprimés. `tradinebotte-feed.user.service` n'avait aucun consommateur. La section systemd single-bot du guide d'installation pointe désormais vers le moteur de déploiement natif. Les templates d'unités vivants et le chemin de déploiement argent-réel ne sont pas affectés.

- **`scripts/deploy_engine.py`** — l'ordonnanceur de déploiement parallèle borné (Phase A). Il ajoutait de la concurrence par-dessus les anciens déployeurs bash par famille pour accélérer un redéploiement de flotte ; une fois tous les bots passés au déployeur natif single-tree (dont les étapes prennent des secondes, pas les minutes que coûtaient les déployeurs bash), sa raison d'être avait entièrement disparu, et plus rien ne l'invoquait. Son test l'a suivi. Le document de design est conservé comme historique.

- **Le bot orderbook, retiré entièrement.** `orderbook_bot.py`, désactivé en juin car structurellement non rentable et conservé seulement comme code mort, est maintenant supprimé avec son backtest (`analysis/backtest_orderbook.py`), son fichier de stratégie, son rendu sur la page de statut (résumé de payload + branches de métrique, le libellé de famille, la chaîne i18n) et son emplacement d'inventaire. Les flux d'*indicateurs* orderbook (microstructure OBI/depth alimentant d'autres bots) sont sans rapport et intacts. La prose de documentation qui le mentionne encore sera balayée lors de la réécriture des docs.

### Retiré
- **Quatre scripts de migration et de test à usage unique, devenus sans objet.** `migrate_to_user_services.sh`, `migrate_claude1_services.sh` et `migrate_cex_bots.sh` réalisaient chacun un déplacement ponctuel des bots en cours d'exécution vers des unités systemd user ; ce déplacement est terminé depuis longtemps sur tous les comptes, et les nouveaux comptes sont installés directement sur des unités user par le moteur de déploiement — ces scripts ne pouvaient donc plus s'exécuter que sur une flotte déjà migrée. `test_all_accounts.sh` pilotait un test d'installation propre du déploiement multi-bots-par-compte, qui n'existe plus. La note d'installation multi-comptes du guide pointe désormais vers le moteur de déploiement.

- **Les deux derniers scripts de feed lancés à la main.** `install_feed_service.sh` générait une unité systemd pour le feed WebSocket partagé, et `start_feed.sh` lançait ce feed hors systemd ; le moteur de déploiement installe et gère désormais l'unité feed nativement, et le feed n'a plus de chemin de lancement manuel supporté. Les sections « lancement manuel » et « mise à jour multi-bots » du guide d'installation ont suivi : elles décrivaient le workflow, retiré, du bot par compte, dont les scripts n'existent plus.

- **Deux services d'infrastructure ne créent plus une identité de bot au simple fait d'être importés.** Les services indicators et CEX-feed résolvaient leur identifiant de flotte (`bot_id_<role>`) au moment de l'import du module, et la résolution persiste sur disque un identifiant fraîchement généré — importer l'un ou l'autre module (un test qui le collecte, un REPL, un lint) déposait donc un fichier `bot_id_*` parasite dans l'arbre source ou le répertoire courant, avec à chaque fois un identifiant aléatoire différent. Les deux résolvent désormais l'identifiant à l'intérieur du runner asynchrone du service, comme le feed et le bot polymarket le faisaient déjà ; un nouveau test de pureté d'import importe chaque point d'entrée dans un sous-processus propre et échoue si un `bot_id_*` apparaît. Les identifiants générés sont également gitignorés désormais.

- **Le script d'installation d'indicators est supprimé ; son lanceur manuel est désormais étiqueté debug.** `install_indicators_service.sh` générait une unité systemd pour le pipeline indicators partagé, supersédée par la cible d'infrastructure native `indicators` du moteur de déploiement (comme l'installateur du feed). `start_indicators.sh` est conservé — c'est le seul moyen de lancer un flux d'indicateur isolé pour le debug — mais son en-tête, et la section de `docs/indicators` qui le montrait, le disent désormais explicitement ; il sait aussi trouver le `.venv` de la flotte (il ne cherchait qu'un `venv/` hérité). Les docs INSTALL et multi-bots pointent leurs instructions systemd vers le moteur de déploiement.

### Déprécié
- **Le pipeline hebdomadaire de collecte de données est en sommeil, pas retiré.** `start_collector.sh`, `collect_db.sh` et `schedule_collect.sh` ne sont déployés nulle part et n'ont produit aucune archive depuis mai 2026, mais ils restent l'outillage qui construit les jeux de données haute résolution utilisés pour les backtests : ils sont conservés en vue d'une future campagne de collecte. Chaque script et sa documentation l'annoncent désormais d'emblée, au lieu de se lire comme des instructions actives.
- **`docs/multi.md` et sa version française décrivent une architecture retirée.** Ils documentent le schéma feed-partagé-plus-un-bot-par-compte, dont le composant par compte a été supprimé. Le raisonnement sur le feed partagé et le protocole de messages ZeroMQ restent exacts ; les instructions de déploiement et de lancement, non. Les deux fichiers s'ouvrent désormais sur un avertissement en ce sens, en attendant une réécriture.

- **Le lanceur de tests ne fait plus fuiter de trades dans la base de production partagée.** `run_tests.sh` exécute la suite avec `unittest`, qui ne charge pas le `conftest.py` pytest redirigeant le canal de statut vers un port mort — un test déclenchant un trade d'accumulation poussait donc une vraie ligne dans la base d'état partagée, étiquetée au compte de l'opérateur. Le lanceur pose désormais lui-même cette redirection vers un port mort, comme le chemin pytest ; une poussée résiduelle ne se connecte à rien.

- **`purge_account_state.py` purge désormais aussi `bot_trades`.** Le nettoyage de la base partagée supprimait les heartbeats et les deploys d'un compte de test mais pas ses lignes de trades par fill : un test ayant déclenché un trade laissait donc un résidu que l'outil ne savait pas retirer. Il vide maintenant les trois tables (avec garde pour une base antérieure à `bot_trades`), et le dry-run comme le récapitulatif rapportent le nombre de trades.

- **L'en-tête de sweep de `backtest_swing_dca.py` ne casse plus sous Python 3.10/3.11.** Trois f-strings plaçaient un backslash littéral dans l'expression `{...}` (`f"{'tp_pct\\sl_pct':<18}"`), ce qui est une `SyntaxError` avant Python 3.12 — importer le module (ce que fait le test de parité du moteur) échouait donc sur les interpréteurs 3.10/3.11 du CI tout en passant sur un plus récent en local. Les chaînes de libellé sont désormais construites hors de la f-string.

### Corrigé
- **Le rapport d'état de la flotte n'ignore plus le compte en argent réel.** `bot_status.sh` parcourait une liste de six comptes codée en dur pour établir l'état des services par compte : un compte ajouté par la suite n'était jamais interrogé — tous ses services pouvaient être à l'arrêt, le rapport concluait malgré tout « All systems nominal ». La liste des comptes est désormais dérivée d'`inventory.toml`, la même source unique de vérité que celle déjà utilisée pour les libellés, et un compte présent dans l'inventaire mais absent du fichier d'identifiants local est signalé au lieu d'être ignoré silencieusement.
- **Le rapport d'état de la flotte ne déclenche plus de fausse alerte pendant l'exécution du watchdog de feed.** Ce watchdog est un service one-shot déclenché par un timer ; un rapport généré pendant qu'il tournait le comptait comme un service en panne et sortait en erreur sur une flotte pourtant saine. Les services dans un état transitoire sont maintenant affichés distinctement et exclus du décompte des pannes, tandis que les services réellement en échec ou arrêtés restent signalés.

### Supprimé
- **Le point d'entrée multi-bot `account_bot` et son chemin de déploiement.** L'« account bot » historique colocalisé avec le feed — le premier bot du projet — est supplanté par les bots natifs standalone. Lui, ses templates systemd et scripts de déploiement, et le test d'intégration multi-bot obsolète ont été retirés ; le compte-1 ne fait plus tourner que l'infrastructure (feeds, indicateurs, collecteur de statut).

---

## [0.89.1] — 2026-07-02

### Documentation
- **Documentation alignée sur l'architecture des modules actuelle** — la 0.89 a réorganisé le code en un cœur neutre (`botcore` : interface de stratégie, registre de connecteurs, persistance, schéma de base) avec les bots Polymarket et CEX comme plugins pairs, mais la documentation décrivait encore l'ancienne organisation monofichier. Le README, INSTALL, CONTRIBUTING, le document de conception et les guides multi-bot / snapshots / grid / how-to décrivent désormais le cœur neutre et les modules-plugins plats `pm_*` / `cex_consumer`, attribuent les fonctions déplacées (p. ex. `save_snapshot`, `check_signal`, `cex_feed_consumer_loop`) aux fichiers qui les définissent désormais, et suppriment les chemins de l'ancien sous-répertoire `bot/` ainsi que l'instruction obsolète « modifier un import pour changer d'exchange » (l'exchange se choisit désormais par un nom de `connector` dans la config de stratégie, chargé depuis le registre). Les docs anglaise et française ont été mises à jour ensemble.
- **Nouveau diagramme d'architecture des modules** — ajout d'une figure compagnon (`docs/architecture_modules.png`, produite par `docs/gen_architecture_diagram.py`) montrant le cœur neutre avec les plugins Polymarket et CEX comme pairs, référencée depuis le document de conception. Le diagramme de topologie des services / ZMQ existant est inchangé, car la topologie d'exécution n'a pas changé — seule l'organisation interne du code a évolué.

---

## [0.89] — 2026-06-29

### Ajouté
- **Planification versionnée de la page de statut** — la page HTML était régénérée par une crontab opérateur maintenue à la main, hors du dépôt. Elle est désormais pilotée par un timer systemd `--user` versionné (`tradinebotte-statuspage.timer` → `.service`, toutes les 2 minutes) installé par `install_statuspage_timer.sh`, donc la cadence est reproductible depuis le checkout. L'installeur localise seul le dépôt/venv, avertit si le linger est désactivé (le timer se mettrait en pause à la déconnexion) et signale toute ligne de crontab résiduelle à retirer.
- **Endpoint HTTP de health-check** — un serveur HTTP `GET /health` optionnel (`tradinetools.health_server`) monté à côté du heartbeat dans chaque bot de trading (`live_bot`, `account_bot`, `accumulation_bot`, `orderbook_bot`). Il réutilise le payload du heartbeat du bot, de sorte qu'un cron externe, un reverse proxy ou un moniteur d'uptime peut récupérer la liveness et les stats (capital, PnL, trades ouverts, uptime) en HTTP sans parler ZMQ. Activé via `TRADINEBOTTE_HEALTH_PORT` (désactivé par défaut — comportement inchangé si non défini) ; bind `127.0.0.1` uniquement, avec un avertissement si pointé vers un hôte non-loopback.

### Sécurité
- **Les identifiants n'apparaissent plus dans un dump de config** — les champs `private_key`, `api_key`, `api_secret` et `api_passphrase` sont exclus du `repr` de `BotConfig` : journaliser ou afficher un objet de configuration ne peut plus faire fuiter de secrets dans les logs ou les tracebacks.

### Corrigé
- **Le script de nettoyage serveur ne supprime plus un répertoire désormais requis par le bot** — le `cleanup_server.sh` manuel supprimait `connectors/` d'un compte Polymarket en tant que « fichier CEX », mais le point d'entrée du bot importe maintenant le registre de connecteurs au démarrage quel que soit l'échange ; le supprimer empêcherait le bot de démarrer. Le nettoyage conserve `connectors/` (et le core neutre) et ne retire que les fichiers de moteur réellement inutilisés.
- **La page de statut affiche les heartbeats d'`orderbook_bot`** — les renderers de pill/ligne par bot n'avaient pas de branche pour `orderbook_bot` : s'il était activé, son pill n'affichait ni métrique clé ni détail. Ajout de sa forme de payload (`total_pnl` / `open_positions` / `last_price`) à `_key_metric` et `_render_payload_summary`.
- **La page de statut ne se vide plus silencieusement quand le collecteur est injoignable** — toutes les données heartbeat/inventory/deploy de la flotte viennent d'un seul compte (le collecteur) ; si son SSH/collecte échouait, la page affichait une vue flotte vide avec un « Bots alive » `0/0` faussement vert, lu comme « tout va bien » plutôt que « statut inconnu ». Elle affiche désormais une bannière rouge explicite (« Collector account unreachable — liveness inconnue, pas forcément hors-ligne »), passe l'en-tête au rouge, et rend « Bots alive — » au lieu d'un zéro vert. Une bannière ambre distincte liste les comptes (hors collecteur) dont la collecte a échoué, pour qu'une page partielle soit visiblement partielle.
- **La page de statut expose désormais l'état agrégé des bots grid** — les bots grid tiennent un état agrégé (bornes, cycles, niveaux remplis, flag halted) dans `grid_state` / `grid_levels` plutôt qu'un journal par-trade : la page (qui interrogeait la table `trades` type-Polymarket) n'affichait rien pour eux, seul leur PnL heartbeat apparaissait. Le collecteur lit maintenant l'état grid depuis chaque `live.db` candidat — le chemin standard et tout dossier alternatif (p. ex. un grid avec `TRADINEBOTTE_DIR=~/tradinebotte-grid`) — et la carte compte affiche une ligne `grid` : `symbole · bornes $bas–$haut · N/M niveaux détenus · cycles · PnL`, avec un badge `HALTED` si le grid s'est arrêté.
- **Durcissement de l'installation de service** — `install_status_service.sh` s'interrompt désormais avec une erreur claire si l'adresse de statut configurée contient un `|`, qui corromprait sinon la substitution `sed` écrivant le fichier d'unité.

### Interne
- **Nettoyage post-refactor** — suppression de quelques ré-exports morts laissés par le découplage (symboles que rien ne référençait), marquage du bloc de ré-exports intentionnel pour que le linter cesse de le signaler (et qu'un futur lecteur ne prenne pas les ré-exports pour du code mort), rafraîchissement de deux commentaires de repère, et correction d'une docstring périmée dans le core qui pointait encore une implémentation de stratégie vers son ancien emplacement.
- **Les tables de base de données agnostiques vis-à-vis de l'échange déplacées dans le core neutre** — le traqueur de version de migration (`schema_version`) et la table clé/valeur générique (`bot_meta`, dont les accesseurs vivent déjà dans le core) sortent du schéma du point d'entrée vers un petit schéma de base neutre dans le core, appliqué avant les tables spécifiques à l'échange. Aucune migration de données : chaque instruction est idempotente, et une base fraîchement créée et une base existante déjà migrée convergent de manière prouvée vers le même schéma et la même version (couvert par un nouveau test). Le comportement est inchangé.
- **Le dispatch de source de données est désormais piloté par registre au lieu d'être codé en dur** — le choix de la boucle de consommation de données à exécuter (feed partagé Polymarket, feed partagé CEX, ou WebSocket direct) était un `if/elif/else` codé en dur dans le point d'entrée qui nommait la boucle de chaque plugin et traitait le connecteur Polymarket en cas spécial. C'est maintenant un petit registre associant chaque source de données à sa run-loop de plugin, résolu par un sélecteur pur et testé unitairement — ainsi une nouvelle famille de stratégie se branche en ajoutant une entrée de registre plutôt qu'en modifiant le dispatch, et le point d'entrée ne privilégie plus un échange. Le comportement est inchangé (chaque cas de routage, y compris l'avertissement de source incompatible et le repli WebSocket direct, est couvert par des tests) ; le consommateur CEX est toujours importé paresseusement pour qu'un bot Polymarket seul ne le charge jamais.
- **Chemin de données Polymarket extrait du point d'entrée universel** — le chemin de mise à jour book/marché Polymarket — le dispatch du carnet d'ordres, l'écriture de snapshot, la découverte de marché, et les deux sources de données (le cycle WebSocket direct et le consommateur de feed partagé) — sort du fichier du point d'entrée universel vers un module Polymarket dédié (`pm_data`). Avec cela, le fichier du point d'entrée ne contient plus le code de trading ni le data plane Polymarket ; les deux sont regroupés dans leurs propres modules plugin. Le point d'entrée les ré-exporte, donc les appelants (y compris `account_bot`) sont inchangés, et chaque corps de fonction déplacé est identique au octet près.
- **Stratégie de seuil + logique de trade Polymarket extraites du point d'entrée universel** — le signal de seuil à entrée unique, le flux d'entrée d'ordre/résolution/clôture, le dimensionnement de mise, et le wrapper `ThresholdStrategy` sont sortis du fichier du point d'entrée universel vers un module Polymarket dédié (`pm_strategy`), poursuivant le regroupement du plugin Polymarket. Le point d'entrée les ré-exporte, donc les appelants sont inchangés ; la logique de trading est identique au octet près (toute la suite de tests signal/Kelly/latence/résolution passe sans modification).
- **Modules feuilles Polymarket extraits du point d'entrée universel** — le type de données de marché par token Polymarket, les compteurs de rejet de signal, et le filtre calendrier/jours fériés de trading US sont sortis du fichier du point d'entrée universel vers leurs propres modules Polymarket co-localisés (`pm_types`, `pm_calendar`), première étape du regroupement du code du plugin Polymarket. Le point d'entrée les ré-exporte, donc les appelants sont inchangés ; le comportement est identique.
- **La boucle de consommation de données CEX déplacée hors du point d'entrée Polymarket vers le package CEX** — la boucle de consommation du feed partagé et son écriture de snapshot (utilisées uniquement par les bots grid/swing CEX) vivaient dans le fichier du point d'entrée universel à côté du code Polymarket. Elles vivent désormais dans le package CEX et ne sont chargées que lorsqu'un bot est effectivement configuré pour le feed CEX ; un bot Polymarket seul ne les importe jamais. Le comportement est inchangé.
- **Le connecteur d'échange est injecté dans l'état du bot plutôt qu'un global de module** — le code de trading et de découverte de marché atteignait son adaptateur d'échange via un global `api` au niveau module que le point d'entrée reliait au démarrage. Le connecteur est désormais chargé par le point d'entrée et passé dans l'état du bot, et le code l'atteint via `state.connector` ; aucun global de module n'est muté et le code de trading ne porte plus de référence en dur à un échange précis. Le comportement est inchangé. Importer le point d'entrée ne charge plus un connecteur en effet de bord.
- **Helpers de persistance neutres déplacés dans le core** — les helpers de persistance/PnL agnostiques vis-à-vis de la stratégie (lecture/écriture de la base de capital, l'étape partagée de persistance de snapshot et sa constante de cadence de commit, et les calculateurs de PnL cumulé/equity) passent du point d'entrée Polymarket vers `botcore.persistence` ; ils prennent une connexion brute ou un état duck-typé et ne référencent aucun concept d'échange. Le point d'entrée les ré-exporte, donc les appelants existants sont inchangés. Fait partie de la consolidation de la machinerie réellement partagée dans le core neutre.
- **L'état partagé du bot ne code plus en dur une stratégie Polymarket** — `BotState` instanciait directement la stratégie de seuil Polymarket dans son constructeur, couplant l'objet d'état partagé à la stratégie d'un seul échange. La stratégie active est désormais fournie par le point d'entrée qui construit l'état (les bots Polymarket injectent la stratégie de seuil ; le chemin grid/swing la remplace ensuite par la sienne), de sorte que l'objet d'état lui-même ne nomme aucune stratégie. Le comportement est inchangé. Cela poursuit le déplacement de la machinerie agnostique vers un core neutre.
- **Le registre de connecteurs déplacé dans le core neutre** — l'interface de chargement des connecteurs (registre nom→module + chargeur paresseux) résidait physiquement dans le package CEX (`connectors/`) alors qu'elle est agnostique vis-à-vis des échanges. Elle vit désormais dans `botcore.connectors` ; l'ancien package `connectors/` le ré-exporte, donc `from connectors import load` continue de fonctionner pour le feed CEX, les moteurs de stratégie et les tests. Le core reste sans plugin — importer `botcore` ne charge aucun module d'échange (le chargement reste paresseux, par nom) — et un test vérifie cet invariant. L'installation du bot copie maintenant tout le package `botcore/` en tant que répertoire plutôt qu'une liste de fichiers codée en dur, afin que les futurs modules du core soient livrés sans modifier l'installateur.
- **Nouveau package core neutre (`botcore`) propriétaire de l'interface de stratégie** — le protocole `Strategy` a été déplacé hors du package CEX (`strategy_engines/base.py`) vers un nouveau package `botcore` agnostique vis-à-vis des échanges, qui n'importe rien d'aucun échange. `strategy_engines/base.py` le ré-exporte, donc l'ancien chemin d'import continue de fonctionner. La `ThresholdStrategy` Polymarket hérite désormais explicitement de `botcore.Strategy` (auparavant elle ne respectait l'interface que structurellement), devenant le premier vrai conformeur du protocole ; les moteurs CEX suivront à une étape ultérieure. `botcore/` est livré à plat à côté du bot par chaque script d'installation/mise à jour/déploiement (comme `connectors/`), et un test structurel échoue si un script de déploiement du bot l'oublie. C'est la première tranche d'une extraction continue du core agnostique hors du point d'entrée Polymarket.
- **Le point d'entrée universel des bots n'importe plus en dur un échange précis** — `live_bot.py` commençait par `import api_polymarket as api`, privilégiant un échange dans le point d'entrée partagé, et son chargeur de connecteur traitait `polymarket` comme un cas spécial sans effet. Il résout désormais son connecteur par défaut via le registre de connecteurs par nom (`connectors.load(CONNECTOR)`, défaut `"polymarket"`), exactement comme tout autre échange, de sorte qu'aucun connecteur n'est privilégié. Le comportement est inchangé (le défaut reste Polymarket et l'objet `api` résolu est identique), mais le point d'entrée ne dépend plus que du registre et d'un nom de connecteur, pas d'un module d'échange concret. Un test garde-fou échoue si un `import api_polymarket` privilégié est réintroduit.
- Suppression de deux schémas de messages inutilisés et obsolètes (`RegisterRequest` / `RegisterReply`) décrivant un protocole d'enregistrement qui n'est plus utilisé par le service indicators ; le protocole réel est `{cmd:"subscribe", …}` → `{status, stream_id}`.

---

## [0.88] — 2026-06-22

### Ajouté
- **MEXC spot dans le flux CEX partagé (réel, basse latence)** — `cex_feed` diffuse désormais les carnets d'ordres MEXC spot. MEXC a migré son WebSocket public spot vers **protobuf** sur `wbs-api.mexc.com` ; le connecteur `api_mexc` a été mis à jour (nouveau endpoint + canal de profondeur protobuf `spot@public.limit.depth.v3.api.pb`), avec un schéma protobuf minimal vendorisé (`tradinebotte-cex/mexc_proto/`, `mexc_spot_depth_pb2.py` généré) et le runtime `protobuf` ajouté aux dépendances. `cex_feed` gère les trames binaires (connecteurs `WS_BINARY`) en parallèle du chemin JSON inchangé.
- **Indicateur de scalping MEXC depuis le flux partagé** — une nouvelle source `cex_scalping` du service indicators consomme le `cex_feed` partagé (au lieu d'ouvrir son propre WS) et rediffuse un flux de scalping (`mid`/`obi`/`obi_ema`/`spread_bps`), p. ex. `btc_scalping_mexc` depuis le spot MEXC.
- **Enregistrement des flux d'indicateurs à la demande** — les bots déclarent les flux qu'ils consomment dans leur config (`indicators_streams`) et les enregistrent auprès de la socket REP du service indicators, en se ré-enregistrant périodiquement : un flux s'auto-répare si le service indicators redémarre — plus de dépendance à une config statique maintenue à la main. Implémenté pour `accumulation_bot`.
- **Bot d'accumulation MEXC en simulation (compte 2)** — instance paper-trading pilotée par le prix/OBI MEXC spot réel (`btc_scalping_mexc`) ; les gates macro restent sur les flux partagés dérivés de Binance. Frais spot MEXC, Earn désactivé. Nouvelle config de stratégie, wrapper de déploiement, unité systemd (installée par le déploiement) et ligne d'inventaire.
- **Keepalive WS public MEXC** — ping applicatif pour les sockets publiques MEXC spot + futures (qui coupent sinon une connexion inactive ~toutes les 30 s), éliminant les reconnexions en boucle.

### Modifié
- **Cadence de monitoring** — intervalle de heartbeat réduit de 3600 s à **120 s** ; seuils de statut resserrés à **STALE 240 s / DEAD 600 s**, de sorte qu'un bot réellement mort est visible en minutes plutôt qu'en heures.
- **Les consommateurs de `cex_feed` filtrent par (exchange, symbole)** — plusieurs places peuvent publier le même symbole (p. ex. binance + mexc spot BTC/USDT) sans contaminer le flux de carnet d'un consommateur.
- **Schéma de versionnage formalisé (x.y.z)** — seule une GitHub Release incrémente le mineur (x.y) ; un simple merge sur main incrémente le patch (z).
- **Reproductibilité du déploiement** — `setup_data_plane.sh` déploie désormais la capacité MEXC spot (clôture des connecteurs + runtime protobuf) sur l'hôte du plan de données ; `deploy_accumulation.sh` installe l'unité systemd (gabarit par stratégie) si absente ; `deploy_all.sh` inclut l'étape d'accumulation MEXC du compte 2.
- **Garde de fraîcheur du reset `botctl`** resserrée à 300 s (correspond au heartbeat de 120 s), pour que la confirmation du mode sim avant un reset destructif soit réellement actuelle.

### Corrigé
- **Le PnL de stop-loss swing omettait les frais** — `_close_sl` comptabilisait le PnL sans les frais aller-retour (le chemin take-profit les déduisait déjà) ; les trades swing clôturés en SL surévaluaient le PnL. Les frais des deux jambes sont désormais déduits. (Le backtest swing/DCA tenait déjà compte des frais de SL — aucun changement.)
- **PnL de simulation gonflé à chaque redémarrage** — la réconciliation au redémarrage des bots grid et swing traitait tous les ordres sauvegardés comme « remplis hors-ligne » en simulation (où `get_open_orders` renvoie vide), re-comptabilisant tout le carnet à chaque redémarrage. La réconciliation est désormais ignorée pour les ordres simulés (les remplissages sim sont détectés sur les ticks de prix).
- **NameError `_os` latent** dans `accumulation_bot._zmq_loop` (une suppression de code mort trop zélée avait laissé un alias non défini ; dormant car la config déployée court-circuitait la ligne) — corrigé en `os` au niveau module.

### Interne
- Nommage des venv standardisé sur `.venv` partout (sites de création, gabarits d'unités systemd, tous les comptes) ; `*_pb2.py` générés exclus du gate pylint.

---

## [0.87] — 2026-06-20

### Corrigé
- **Réparation du test d'intégration d'installation autonome — il était silencieusement cassé depuis la séparation en monorepo et n'exerçait jamais réellement le bot.** Trois problèmes : (1) il invoquait `scripts/start_bot.sh`, mais depuis la séparation ce script se trouve sous `tradinebotte-polymarket/scripts/` alors qu'`install.sh` déploie une arborescence à plat — il démarre désormais via le wrapper `run.sh` qu'`install.sh` crée et documente ; (2) il faisait un rsync de tout le dépôt *dans* le répertoire d'installation, de sorte que l'arborescence source `tradinetools/` masquait le paquet installé comme paquet d'espace de noms (`cannot import name 'heartbeat_loop'`) — il installe désormais depuis un répertoire source séparé vers un répertoire d'installation propre, à l'image d'un vrai utilisateur (cloner ici / installer ailleurs) ; (3) il attendait 8 s fixes la connexion WebSocket — il sonde désormais jusqu'à 60 s, car une installation propre à froid a besoin d'environ 10 à 20 s pour récupérer les marchés et se connecter. Le teardown efface aussi les répertoires d'installation et source afin que le compte de test dédié aux installations propres ne conserve aucun état persistant. L'étape d'intégration du garde-fou de release (autonome + multi-bots) passe désormais proprement de bout en bout.

---

## [0.86] — 2026-06-20

### Corrigé
- **Page de statut — le PnL de la flotte était un agrégat faux ; désormais une source unique de vérité.** La page recalculait le PnL Polymarket depuis le `live.db` de chaque bot mais lisait le PnL CEX depuis le payload du heartbeat — deux pipelines pour la même métrique. L'en-tête « Today / Lifetime » de la flotte, pourtant étiqueté « all accounts », excluait donc silencieusement tous les bots CEX (sous-estimant le PnL cumulé de la flotte d'environ 64 %), et le « today » d'un bot pouvait différer entre sa carte de compte et sa pastille de heartbeat. La carte, la pastille et l'en-tête de flotte lisent désormais tous le payload du heartbeat que chaque bot (Polymarket *et* CEX) émet déjà, de sorte que les totaux couvrent toute la flotte et ne peuvent plus diverger. Le `live.db` n'est plus interrogé que pour le taux de réussite Polymarket et les tables des trades récents/ouverts.
- **Page de statut — les métriques périmées sont signalées au lieu d'être affichées comme actuelles.** Quand le heartbeat d'un bot est STALE/DEAD, ses valeurs Capital / Today PnL sont atténuées et badgées pour que les dernières valeurs connues ne soient pas prises pour des valeurs en direct.
- **Page de statut — cohérence taux de réussite / nombre de trades.** Le nombre affiché à côté du taux de réussite est désormais le nombre de trades résolus sur lequel le taux est calculé (et non le total, qui inclut aussi les positions ouvertes) ; l'infobulle détaille la répartition gains/pertes et le nombre de positions ouvertes.
- **Le test d'intégration multi-bots est autonome et laisse le compte de test propre.** Le feed dont il a besoin tourne comme un processus d'arrière-plan éphémère depuis le virtualenv propre au test (pas de service systemd, pas de lingering utilisateur) ; le teardown arrête tous les processus, supprime les répertoires d'installation/test et purge les heartbeats du compte — ainsi le compte de test dédié aux installations propres n'accumule jamais d'état persistant.
- **Nettoyage de code mort (audit de code).** Suppression d'un helper HTML inutilisé et d'imports/variables locales inutilisés dans le générateur de statut, de ré-imports redondants de `os` dans les bots d'accumulation et de carnet d'ordres, et d'une variable locale inutilisée dans le moteur de stratégie DCA.

### Modifié
- **Page de statut — libellés temporels de PnL exacts.** « Today » → « Today (UTC) » (le PnL quotidien est agrégé à partir de minuit UTC) et « Lifetime » → « Since reset » (le total est le PnL depuis la base de capital du bot, remise à zéro lors d'un reset de simulation).
- **Page de statut — infobulles au survol plus grandes et lisibles**, avec une media query petit écran pour que les panneaux de détail soient lisibles sur un téléphone.

---

## [0.85] — 2026-06-18

### Ajouté
- **Séparation plan de données / plan d'ordres** — les données de marché externes (websockets, REST) sont désormais récupérées une seule fois par des services partagés puis diffusées via ZeroMQ, tandis que chaque bot continue de passer ses ordres indépendamment avec ses propres identifiants. Cela supprime la duplication des connexions amont par bot :
  - **`tradinebotte-polymarket/feed.py` — tag/horizon de marché configurable** : le feed lit `TRADINEBOTTE_MARKET_TAG_ID` et une fenêtre de marché depuis l'environnement au lieu de les coder en dur, de sorte qu'un processus feed sert un horizon et qu'un second feed peut en servir un autre ; le nom de heartbeat est aussi configurable via `TRADINEBOTTE_FEED_NAME` pour que plusieurs feeds se signalent distinctement
  - **`tradinebotte-polymarket/feed.py` — second feed 5 minutes** en parallèle du feed 15 minutes existant, avec son propre modèle d'unité systemd et son adresse ZeroMQ
  - **`tradinebotte-cex/cex_feed.py` — service de données de marché CEX partagé** : un nouveau service qui s'abonne une seule fois aux carnets d'ordres Binance et MEXC et rediffuse des mises à jour normalisées (meilleur bid/ask, spread, volumes, déséquilibre du carnet) via ZeroMQ ; une tâche indépendante par place de marché, de sorte qu'une reconnexion d'un feed ne perturbe pas les autres
  - **`tradinebotte-polymarket/live_bot.py` — modes consommateur de feed partagé** : le `data_source` d'un bot peut valoir `ws` (websocket direct, défaut), `feed` (feed Polymarket partagé) ou `cex_feed` (feed Binance/MEXC partagé) ; les bots consommateurs n'ouvrent aucun websocket amont propre
  - **`setup_data_plane.sh` — installateur idempotent** qui provisionne les services de feed partagés et rafraîchit la bibliothèque partagée `tradinetools` sur l'hôte du plan de données ; intégré à `deploy_all.sh --restart-infra`
- **Plan de contrôle ZeroMQ pour les bots et services** — un opérateur peut désormais envoyer des commandes aux bots en cours d'exécution via une socket de contrôle par compte :
  - **Cœur du plan de contrôle dans `tradinetools`** : un assistant `control_loop` avec une garde fail-closed qui refuse toute commande modifiant l'état sauf si le bot est explicitement en mode simulation, plus des assistants de marqueur de reset / effacement au démarrage
  - **Commande `reset`** : efface l'état d'un bot de simulation et le redémarre depuis un capital de départ paramétrable (les bots réels sont toujours refusés)
  - **CLI opérateur `botctl` + client de requête `ctl_client`** : `botctl.sh <compte> <bot> <ping|status|reset>` via SSH en loopback
  - **Persistance du capital de base** : le capital de départ d'un bot est stocké en base afin que l'équité reprenne correctement après un redémarrage au lieu d'être réinitialisée
- **Base d'état partagée unifiée** — heartbeats, inventaire et journal de déploiement vivent désormais dans une seule base SQLite partagée ; la page de statut gagne une section attendu-vs-réel et les bots déclarent eux-mêmes s'ils tournent en simulation ou en réel afin de vérifier le mode déclaré dans l'inventaire
- **Durcissement du garde-fou de release** — le script de pré-release exécute désormais un contrôle de dérive inventaire/pipeline de déploiement et une garde qui échoue si du code lit encore l'ancien emplacement par compte `heartbeat.db` au lieu de la base partagée

### Modifié
- **`tradinebotte-cex/scripts/deploy_all.sh`** — `--restart-infra` (re)démarre désormais aussi le plan de données partagé (feed 5 minutes + feed CEX + rafraîchissement de `tradinetools`) dans la même fenêtre qui reconnecte déjà les consommateurs ; les wrappers de déploiement Polymarket réaffirment `data_source=feed` à chaque déploiement pour qu'un redéploiement ne fasse jamais silencieusement revenir un bot à son propre websocket
- **`account_bot`** — déclaré en simulation (`is_live=false`) dans l'inventaire
- **Scalper de carnet désactivé** — le bot de scalping de carnet du compte 4 était structurellement non rentable (dominé par les frais) ; il est désormais arrêté et désactivé dans le pipeline de déploiement et dans l'inventaire ; son script est conservé pour référence en attendant un recalibrage
- **Page de statut** — le bot de carnet désactivé a été retiré du générateur

### Corrigé
- **`tradinebotte-cex` — comptabilité du PnL de grille** : les bots de grille comptabilisent désormais le PnL réalisé par cycle achat/vente complété et exportent le PnL cumulé, corrigeant un PnL rapporté quasi nul
- **`tradinebotte-cex/cex_feed.py` / grille MEXC** — déduplication de l'abonnement à la profondeur MEXC (un abonnement multi-token était envoyé sous forme de tableau JSON et rejeté) et recalibrage de la plage de grille
- **Le test d'intégration pouvait viser la production** — le test d'intégration ZeroMQ multi-bots utilisait par défaut les indices feed/compte `0`/`(0 1)`, qui correspondaient à des comptes de production ; il vise désormais par défaut le compte de test dédié (`TEST_STANDALONE_USER_IDX`) et échoue en fail-closed si un indice résolu n'est pas ce compte (contournement via `TEST_ALLOW_NONTEST_ACCOUNTS=true` pour un essai multi-comptes hors production délibéré)
- **`scripts/check_no_legacy_refs.sh`** — exclusion de l'outil de nettoyage de l'ancienne base du scan des chemins de lecture obsolètes (il ne fait que stat/supprimer l'ancien fichier, sans jamais l'ouvrir comme base)
- **`scripts/update_standalone.sh`** — préservation du `TEST_STANDALONE_USER_IDX` de l'appelant lors du `source` du fichier de configuration, corrigeant les déploiements standalone redirigés par erreur vers le compte de test

---

## [0.84] — 2026-06-13

### Corrigé
- **`tradinebotte-indicators/indicators.py` — boucle infinie de resynchronisation `btc_full_depth_perp`** : la vérification du flux live utilisait un garde strict `pu == last_update_id` qui identifiait incorrectement l'événement bridge Binance comme un écart de séquence, provoquant 163 resyncs en 10 jours ; corrigé avec un flag `first_live` qui accepte `0 <= pu < last_update_id` exactement une fois à la reconnexion (l'événement bridge enjambe par conception le point de snapshot), puis impose la continuité stricte ; la garde `pu >= 0` rejette la valeur par défaut `pu=-1` pour les événements sans ce champ ; 0 resync depuis le déploiement
- **`tradinebotte-indicators/indicators.py` — pré-buffer révoqué** : le pré-buffer de 500 ms introduit lors de la session précédente était inefficace (quand le snapshot REST arrive plus vite qu'un tick WS, le buffer est périmé et l'événement bridge déclenche quand même l'ancienne vérification) ; remplacé par la correction de l'événement bridge ci-dessus
- **`tradinebotte-indicators/indicators.py` — bruit de traceback websockets supprimé** : `logging.getLogger("websockets").setLevel(logging.ERROR)` dans `main()` empêche la bibliothèque websockets v16.0 d'écrire `TimeoutError: timed out while closing connection` directement sur stderr via son gestionnaire `lastResort`
- **`tradinebotte-indicators/indicators.py` — message d'erreur REST aggTrade** : le `except Exception` nu a été remplacé par un log incluant `type(exc).__name__: exc` afin que les erreurs REST ne soient plus enregistrées comme messages vides
- **`~/tradinebotte/strategies/indicators/indicators_all.json` (config serveur)** — suppression du paramètre `db_path` périmé dans les configs de flux `btc_full_depth` et `btc_full_depth_perp` ; le chemin `/data1/tmp/orderbook.db` se trouve hors de `TRADINEBOTTE_DIR` et provoquait une entrée `ERROR` à chaque redémarrage du service ; la fonctionnalité orderbook DB était de fait désactivée

### Ajouté
- **`tradinebotte-indicators/tests/test_indicators.py` — 3 nouveaux tests pour la gestion de l'événement bridge dans `_binance_full_depth_task`** : `test_bridge_event_not_resynced` (l'événement bridge est accepté, pas de resync), `test_genuine_forward_gap_still_resyncs` (un vrai écart déclenche bien un resync), `test_normal_continuation_no_resync` (la continuité stricte est imposée après l'événement bridge)

### Modifié
- **Nettoyages pylint** — suppression des imports inutilisés (`Iterator` dans `backtest_orderbook.py`, `Optional`/`Tuple` dans `calibrate_obi_proxy.py`, `math`/`Optional` dans `scripts/backtest_accumulation.py`, `math`/`MagicMock` dans `tests/test_scalping.py`, `uuid` dans `strategy_engines/dca.py`, `sqlite3` dans `tradinebotte-status/tests/test_status_collector.py`) ; ajout de `encoding="utf-8"` à tous les appels `open()` dans `tradinetools/tests/test_version_stamp.py` ; score pylint 9,89 → 9,90

---

## [0.83] — 2026-06-11

### Ajouté
- **`tradinebotte-cex/api_mexc_futures.py` — adaptateur MEXC Futures perpétuel** : connecteur complet pour `contract.mexc.com` REST + WebSocket ; format symbole `BTC_USDT` (underscore) ; authentification via en-têtes HMAC-SHA256 `ApiKey` + `Request-Time` + `Signature` ; taille de contrat 0,001 BTC ; frais taker 0,06 % ; `post_order()` convertit le notionnel USDT en nombre entier de contrats ; mode simulation automatique en l'absence des variables `MEXC_FUTURES_API_KEY`/`MEXC_FUTURES_API_SECRET` ; authentification WS privée via message JSON de login (pas de listenKey dans l'URL) ; `parse_user_stream_msg()` traduit les codes de sens MEXC (1–4) en BUY/SELL standard ; implémente l'interface complète grid/swing (`get_open_orders`, `cancel_order`, `get_order_status`, `get_listen_key`, `keepalive_listen_key`, `make_user_stream_url`, `parse_user_stream_msg`)
- **`tradinebotte-cex/connectors/__init__.py` — entrée de registre `mexc_futures`** : `"mexc_futures": "api_mexc_futures"` ajouté au registre des connecteurs ; `validate()` vérifie l'interface au démarrage
- **`tradinebotte-cex/strategies/grid/grid_BTC_USDT_mexc_futures.json` — config grid MEXC Futures** : grille statique 21 niveaux de 82 k$ à 124 k$ (±20 % autour de 103 k$), 100 $/ordre (≈ 1 contrat à 100 k$), capital 2 100 $, stop-loss journalier 200 $ ; mode simulation par défaut (pas de credentials API sur le compte de test)
- **`tradinebotte-cex/scripts/deploy_grid_mexc.sh` — script de déploiement du bot grid MEXC Futures** : déploie et redémarre le bot grid sur le compte de test (index 5 dans `TEST_USERS`) ; le service systemd `tradinebotte-live.service` est installé au premier déploiement (copié depuis le template rsynced), puis `systemctl --user restart` à chaque mise à jour ; bascule nohup si l'activation du service échoue ; garde `/proc/$P/exe` sur tous les `pgrep` ; flags `--skip-restart` et `--verify-only`
- **55 nouveaux tests dans `tests/test_api_cex.py`** : `TestMexcFuturesComputeFee`, `TestMexcFuturesMetadata`, `TestMexcFuturesParseBookUpdate`, `TestMexcFuturesMakeSubscribeMsg`, `TestMexcFuturesPostOrderSimulated`, `TestMexcFuturesGetOrderStatus`, `TestMexcFuturesCancelOrder`, `TestMexcFuturesGetOpenOrders`, `TestMexcFuturesParseUserStreamMsg`, `TestMexcFuturesUserStream`, `TestMexcFuturesRegistry` ; mexc_futures inclus dans toutes les boucles de contrat adaptateur ; total : 181 tests dans ce fichier, 400 dans la suite principale — tous passants

### Modifié
- **`tradinebotte-cex/scripts/deploy_all.sh` — résumé 11 bots** : ajout de `run_step "account-6 — grid live_bot (MEXC Futures sim)"` après l'étape swing account-5 ; en-tête et ligne de résumé mis à jour de 10 à 11 bots
- **`tradinebotte-status/scripts/bot_status.sh` — label account-6 mis à jour** : `"acct-6 [test]"` → `"acct-6 [grid-mexc-sim]"`
- **`README.md`, `README.fr.md` — table des adaptateurs mise à jour** : ligne `api_mexc_futures.py` ajoutée
- **`CONTRIBUTING.md`, `CONTRIBUTING.fr.md` — structure du projet mise à jour** : `api_mexc_futures.py` ajouté à la liste des fichiers adaptateurs CEX

### Corrigé
- **`tradinebotte-cex/api_mexc.py` — `get_markets()` rejetait des kwargs inattendus** : ajout de `**_` pour absorber les kwargs d'origine Polymarket (`tag_id`, `window_minutes`) transmis par `live_bot.py` ; même correction appliquée dans `api_mexc_futures.py` ; corrige le `TypeError` au démarrage du bot grid lors de l'appel à `get_markets()`

---

## [0.82] — 2026-06-11

### Ajouté
- **`scripts/prepare_release.sh` — porte de pré-release** : contrôle obligatoire en 7 étapes avant tout merge vers main ; bloquant : suite complète de tests unitaires (les 6 sous-modules), seuil pylint (FAIL si < 9,90, WARN si < 10,00), shellcheck `-S warning` sur tous les `.sh` suivis par git, vérification des paires de documentation bilingues (10 fichiers) ; non-bloquant : fraîcheur du CHANGELOG, scan complet de qualité des données, tests d'intégration (contournement via `--skip-integration`) ; flag optionnel `--tag v0.XX` pour créer un tag git local ; tableau récapitulatif coloré par étape ; sortie non-zéro uniquement sur les échecs bloquants
- **`version.py` — version du projet** : chaîne de version canonique au niveau dépôt (`__version__ = "0.82"`)

### Corrigé
- **shellcheck `-S warning` propre sur les 40 scripts shell suivis** : SC2174 dans 5 scripts (`install_indicators_service.sh`, `install_account_service.sh`, `install_feed_service.sh`, `start_account.sh`, `start_feed.sh`) — remplacement de `mkdir -p -m mode` par `mkdir -p` + `chmod` ; SC1083/SC2140 dans `collect_db.sh` (contexte d'échappement pour shell distant dans une commande SSH) ; SC2034 (variable `CYAN` inutilisée) et SC2188 (troncature `>` sans commande) dans `test_all_accounts.sh`

---

## [0.81] — 2026-06-11

### Corrigé
- **`scripts/deploy_all.sh` — résumé 10 bots + flag `--restart-infra`** : le résumé de déploiement affiche désormais correctement les 10 bots (les 3 services de account-1 étaient comptés comme 1 étape) ; ajout du flag `--restart-infra` pour redémarrer les services systemd de account-1 (indicators + feed + account_bot) quand ces fichiers changent ; par défaut account-1 reçoit seulement un rsync pour éviter ~30 s de déconnexion des live_bots pendant `RestartSec`
- **pylint — tous les fichiers modifiés à 10/10** : correction W0603 (global-statement) dans `indicators.py`, `feed.py`, `account_bot.py` ; correction subprocess `check=False`, variable inutilisée `rc` → `_`, et `encoding=` manquant dans `generate_status.py` ; ajout de `connectors`, `strategy_engines` dans `ignored-modules` de `.pylintrc` (imports paresseux du répertoire de déploiement absents de l'arborescence source)
- **shellcheck -S warning sur tous les scripts shell modifiés** : suppression des variables inutilisées (`CYAN` dans `bot_status.sh`, `RED`/`INSTALL` dans `cleanup_server.sh`, tableau orphelin `LEGACY_BOTS` dans `deploy_scalping_claude4.sh`) ; correction SC2155 dans `install_status_service.sh` ; ajout des directives SC1090 dans les scripts de déploiement

### Modifié
- **`docs/GridTrading.md`** — traduit du français vers l'anglais (violation de la règle de langue ; version française conservée dans `GridTrading.fr.md`)
- **`docs/accumulation.md`** — mis à jour pour refléter `accumulation_bot.py` v1.5 / config v2.0 : gestion du capital resserrée (`max_invested_pct` 0.90 → 0.65, nouveau `max_avg_entry_mult=1.20`), pipeline de 7 flux de signaux documenté, P1–P6 tous marqués ✅ implémentés, table de performance obsolète remplacée par des commandes SQL de monitoring
- **`docs/KellySizing.md`** — statut corrigé en "implémenté, désactivé par défaut" (`kelly_fraction=0.0` dans `live_bot.py`)
- **`docs/logging.md`** — ajout de trois nouvelles sections documentant les formats de logs de `accumulation_bot.py`, `orderbook_bot.py` et `status_collector.py`

### Supprimé
- **`docs/status_example.html`** — snapshot HTML obsolète supprimé (était ancré sur un ancien hash de commit ; la page live est maintenant générée dynamiquement par `generate_status.py`)

---

## [0.80] — 2026-06-10

### Ajouté
- **`tradinebotte-status/generate_status.py` — chemin de sortie par défaut configurable** : le script écrit désormais dans `~/public_html/tradinebottestatus.html` par défaut au lieu de stdout ; le répertoire de sortie est créé automatiquement ; le chemin est surchargeable via `--out /chemin/fichier.html` (option CLI, priorité maximale) ou la variable d'environnement `TRADINEBOTTE_STATUS_OUT` (priorité intermédiaire, utile pour cron ou `Environment=` systemd)
- **`INSTALL.md`, `INSTALL.fr.md` — section tableau de bord de statut multi-bot** : nouvelle section documentant `generate_status.py` (configuration du chemin de sortie, prérequis, contenu de la page, planification cron, configuration Apache mod_userdir)
- **`README.md`, `README.fr.md` — bullet tableau de bord de statut multi-bot** : documente le service collecteur de heartbeats et le générateur de tableau de bord HTML

---

## [0.79] — 2026-06-09

### Ajouté
- **`analysis/btc_variation.py`** — rapport de variation du prix BTC depuis les bases de données locales des collecteurs ; détecte automatiquement la base CEX la plus récente (table `ob_snapshots` ou `snapshots`), affiche un tableau OHLC journalier et la variation globale `▲/▼` avec les flags `--days N` et `--db PATH`
- **`analysis/check_data_quality.py`** — vérifications d'invariants sur toutes les bases de données des collecteurs ; classifie automatiquement chaque DB (ob\_cex, cex\_snap, polymarket, klines, daily) et exécute des contrôles ciblés : bornes de prix, contamination de type (probabilités Polymarket dans les tables CEX), sanité OBI, identité comptable du capital, détection de gaps de collecte via SQL `LAG()` ; flags `--no-gaps`, `--warn-only` et `--verbose` ; sort avec code non-zéro sur tout `FAIL` par défaut
- **`scripts/run_tests.sh`** — contrôle qualité des données ajouté en étape 3 (`--no-gaps --warn-only`, non bloquant) ; pylint ajouté en étape 6 (non bloquant, ignoré proprement si non installé)
- **`docs/HOWTO_tests_and_backtests.md`, `docs/HOWTO_tests_and_backtests.fr.md`** — nouvelle sous-section *Checklist pré-release* : commande de scan complet des gaps (`check_data_quality.py --warn-only` sans `--no-gaps`) et rappel des tests d'intégration

### Modifié
- **`docs/design.md`, `docs/design.fr.md`** — architecture IPC : diagramme Option B mis à jour des adresses TCP vers les chemins de sockets IPC ; labels des organigrammes de sonde corrigés ; table des adresses par défaut et table ENV mises à jour vers la détection automatique IPC
- **`docs/multi.md`, `docs/multi.fr.md`** — exemples de logs de monitoring remplacés par les chaînes réelles de `feed.py` (bind IPC, nombre de marchés, abonnement aux tokens, WebSocket connecté)

---

## [0.78] — 2026-06-09

### Correctifs
- **`INSTALL.md`, `INSTALL.fr.md` — arborescence du service account corrigée vers l'approche répertoire à plat** : l'approche sous-répertoire `bot/` était incorrecte pour une installation fraîche où seulement `account_bot.py` et `live_bot.py` y sont placés (sans `api_polymarket.py` et autres modules importés par `live_bot.py`) ; corrigé vers l'approche à plat : les trois services (`indicators.py`, `feed.py`, `account_bot.py`) s'exécutent depuis `~/tradinebotte/` afin que `sys.path.insert(0, dirname(__file__))` de `account_bot.py` résolve `~/tradinebotte/` et que `import live_bot` trouve toutes ses dépendances ; arborescence mise à jour ; note sur la copie miroir supprimée des notes d'architecture
- **`README.md`, `README.fr.md` — bullet multi-bot mis à jour** : référence à la copie miroir supprimée, arborescence à plat notée

---

## [0.77] — 2026-06-09

### Correctifs
- **`INSTALL.md`, `INSTALL.fr.md` — ExecStart du service account corrigé** : l'unité account_bot utilisait précédemment `%h/tradinebotte/account_bot.py` ; corrigé en `%h/tradinebotte/bot/account_bot.py` (WorkingDirectory `%h/tradinebotte/bot`) afin que `sys.path.insert(0, dirname(__file__))` dans `account_bot.py` résolve `live_bot` depuis `bot/live_bot.py`, conformément au déploiement actif ; arborescence mise à jour pour montrer `bot/account_bot.py` et `bot/live_bot.py`

---

## [0.76] — 2026-06-09

### Modifications
- **`INSTALL.md`, `INSTALL.fr.md` — section multi-bot réécrite pour l'architecture trois services IPC** : documente `indicators.py` + `feed.py` + `account_bot.py` comme trois processus managés distincts ; sockets IPC dans `/run/user/$UID/` avec isolation par utilisateur Linux enforced par le noyau ; services systemd utilisateur (`systemctl --user`, sans `sudo`) désormais l'approche principale recommandée ; modèles complets de fichiers d'unité pour les trois services inclus ; `loginctl enable-linger` documenté comme étape admin unique ; étape d'installation de tradinetools ajoutée avec fallback en cas d'échec de `pip` (`rm -rf $SITE/tradinetools && cp -r …`) ; prérequis `feed_auto_start: false` documenté ; exigence de synchronisation de `bot/live_bot.py` documentée
- **`QUICKSTART.md`, `QUICKSTART.fr.md` — résumé Option B multi-compte ajouté** : configuration des trois services systemd utilisateur décrite avec les commandes essentielles et lien vers la procédure complète ; commande d'arrêt du service système supprimée
- **`README.md`, `README.fr.md` — bullet fonctionnalité multi-bot mis à jour** : décrit désormais l'architecture trois processus et le déploiement IPC au lieu du modèle deux processus avec démarrage auto du feed
- **`UPDATE.md`, `UPDATE.fr.md` — commande de redémarrage des indicateurs mise à jour** : `sudo systemctl restart tradinebotte-indicators` remplacé par `systemctl --user restart tradinebotte-indicators.service` (unité utilisateur, sans sudo)

---

## [0.75] — 2026-06-09

### Ajout
- **`scripts/cleanup_server.sh`** : nouvel utilitaire pour supprimer les fichiers obsolètes de tous les comptes de déploiement en 6 phases — venvs dupliqués, résidus de git clone, anciens fichiers de stratégie à plat, fichiers PID/logs/DBs périmés, et fichiers Python du mauvais module ; la phase 1 (services système) affiche les commandes root sans les exécuter ; toutes les phases supportent `--dry-run` et `--phase=N`

### Modifié
- **`tradinebotte-cex/strategies/accumulation/btc_accumulation.json` v2.0 — gestion du capital renforcée** : `max_invested_pct` 0,90 → 0,65 (déployer au maximum 65 % du capital) ; nouveau garde `max_avg_entry_mult: 1.20` qui bloque le scale-in quand le prix est déjà 20 %+ au-dessus du prix d'entrée moyen ; `sell_fraction` 0,25 → 0,10 ; `rebuy_max_age_days: 60` ajouté

### Corrigé
- **`scripts/backtest_accumulation.py` — garde avg_entry et expiration des rebuys** : charge `MAX_AVG_ENTRY_MULT` et `REBUY_MAX_AGE_S` depuis la config ; `check_scale_in()` retourne immédiatement si prix > avg_entry × MAX_AVG_ENTRY_MULT ; `check_rebuys()` expire les rebuys en attente depuis plus de REBUY_MAX_AGE_S (nécessite un tuple à 4 éléments avec `created_ts`) ; valeurs par défaut CLI mises à jour : `--dip-pct` 3 → 4, `--dip-lookback` 48 → 72

---

## [0.74] — 2026-06-09

### Corrigé
- **`tradinebotte-polymarket/scripts/update_claude1.sh` — VERIFY signalait toujours FAILURE** : la logique de vérification était empruntée à `update_standalone.sh` qui cherche un processus `live_bot.py` autonome et `live.log` ; ni l'un ni l'autre n'existe sur ce compte qui utilise trois services systemd utilisateur (indicators + feed + account_bot) ; remplacé par `_verify_claude1_multiservice()` qui vérifie les trois unités de service, lit `account.log` et contrôle la connectivité au feed ; `--verify-only` exécute désormais la bonne vérification sans déclencher de mise à jour

---

## [0.73] — 2026-06-08

### Sécurité
- **`tradinebotte-polymarket/scripts/install_account_service.sh` — injection Python dans le code inline `-c` (H-4)** : `ACCOUNT_DIR` était interpolé directement dans un heredoc `python3 -c` ; des caractères spéciaux dans le chemin (guillemets, points-virgules) pouvaient exécuter du Python arbitraire ; corrigé en passant `ACCOUNT_DIR` comme argument CLI (`sys.argv[1]`)
- **`tradinebotte-polymarket/scripts/install_account_service.sh` — collision du délimiteur `|` dans `sed` (H-4)** : `sed "s|...|${ACCOUNT_DIR}|"` se cassait silencieusement si `ACCOUNT_DIR` contenait un `|` littéral ; corrigé avec un garde `[[ "$ACCOUNT_DIR" == *\|* ]]`
- **`tradinebotte-cex/orderbook_bot.py` — nom de colonne non sanitisé dans `ALTER TABLE` (M-1)** : `f"ALTER TABLE {self.symbol}"` permettait du SQL arbitraire via une valeur `symbol` malveillante dans la config ; corrigé avec une assertion sur liste blanche
- **`tradinebotte-polymarket/live_bot.py`, `feed.py` — exposition de la clé privée via tracebacks (M-2)** : `exc_info=True` dans les gestionnaires d'erreur WebSocket pouvait inclure la clé privée dans les tracebacks ; remplacé par `str(e)` explicite
- **`tradinebotte-polymarket/live_bot.py` — fichiers log et base de données lisibles par tous (M-3)** : `live.log` et `live.db` créés avec les permissions par défaut (0o644/0o666) ; `chmod 640` appliqué à l'initialisation du bot
- **`tradinebotte-indicators/indicators.py` — `os.getcwd()` pour `TRADINEBOTTE_DIR` (M-4)** : se résolvait au répertoire de lancement du processus ; remplacé par `os.path.dirname(os.path.abspath(__file__))` pour une résolution stable indépendante du CWD
- **`requirements.txt` — `bcrypt` absent ; repli silencieux sur SHA-1 dans `bot_utils.py` (H-1)** : `_htpasswd()` utilisait silencieusement SHA-1 non salé si `bcrypt` n'était pas installé ; lève désormais `ImportError` immédiatement ; `bcrypt==5.0.0` ajouté à `requirements.txt`
- **`tradinebotte-polymarket/scripts/update_standalone.sh` — ligne `PASS=` morte dans le heredoc distant (L-2)** : assignation résiduelle supprimée
- **`tradinebotte-cex/api_bitstamp.py` — `_has_creds` évalué à l'import (L-3)** : capturait les variables d'environnement `BITSTAMP_*` à l'import ; converti en fonction pour une réévaluation à chaque appel

---

## [0.72] — 2026-06-08

### Corrigé
- **`scripts/run_tests.sh` — `tradinetools/tests/` jamais exécuté** : 87 tests dans `test_math`, `test_schemas`, `test_zmq` absents de la boucle de découverte ; ajoutés à la suite
- **`analysis/backtest.py --all` — crash sur base de données corrompue** : un fichier `.db` malformé provoquait une `DatabaseError` non gérée qui interrompait l'exécution multi-db ; un gestionnaire `try/except DatabaseError` ignore désormais le fichier fautif et continue
- **`tests/test_bot.py::TestStrategyLoading::test_min_secs` — valeur attendue périmée** : `min_secs_remaining` était asserté à 45, mais `polymarket_BTC5M.json` avait été mis à jour à 30 ; test corrigé
- **`tests/test_regression.py::TestParamConsistency::test_obi_reject_thresh` — divergence silencieuse live/backtest** : `backtest.Params.obi_reject_thresh` valait -0,25 tandis que `live_bot.OBI_REJECT_THRESH` était à -0,40 (calibré le 2026-05-30, jamais reporté) ; synchronisé à -0,40
- **`README.md`, `README.fr.md` — compteur de tests périmé** : mis à jour de 891 tests / 9 suites à 1090 tests / 14 suites

---

## [0.71] — 2026-06-08

### Corrigé
- **31 incohérences docs/code corrigées dans tous les fichiers bilingues** : `SIGNAL_THRESHOLD` corrigé 0,96 → 0,95 dans README, INSTALL, CLAUDE.md, `snapshots.md`, `multi.md` et la docstring de `live_bot.py` ; chemins de scripts préfixés avec `tradinebotte-polymarket/` ; références à `polymarket_BTC5M_v2.json` remplacées par `polymarket_BTC5M_piste3.json` ; références aux DBs embarquées supprimées ; compteurs de tests mis à jour ; `venv/` → `.venv/` dans `multi.md` et `install.sh` ; `/tmp` → `~/tmp` dans les chemins de copie de fichiers de service ; chemin de stratégie CEX `longterm/` → `accumulation/` ; toutes les corrections appliquées aux docs EN et FR
- **`scripts/run_integration_tests.sh` — bug critique de chemin** : `test_standalone_deploy.sh` se résolvait au mauvais répertoire en mode `--standalone`, provoquant une erreur à chaque exécution ; chemin corrigé

---

## [0.70] — 2026-06-08

### Corrections
- **`UPDATE.md`, `UPDATE.fr.md` — commentaire Scénario 1 référençait encore `~/tradinebotte/venv`** : mis à jour en `~/tradinebotte/.venv`
- **`UPDATE.md`, `UPDATE.fr.md` — description Scénario 4 incomplète depuis la v0.68** : `update_standalone.sh` synchronise désormais aussi `tradinebotte-cex/connectors/`, `tradinebotte-cex/strategy_engines/` et `tradinetools/` ; description mise à jour pour lister tous les répertoires synchronisés

---

## [0.69] — 2026-06-08

### Corrections
- **`INSTALL.md`, `INSTALL.fr.md` — cinq références obsolètes `venv/` remplacées par `.venv/`** : (1) commande alternative sqlite3 utilisait `~/tradinebotte/venv/bin/python3` ; (2) section Dépendances listait le chemin du virtualenv comme `~/tradinebotte/venv/` ; (3) bullet Installation mentionnait `<TRADINEBOTTE_DIR>/venv/` ; (4–5) exemples Méthode 3 tar.gz utilisaient `cd tradinebotte-0.5.0` au lieu de `cd tradinebotte-0.63` ; (6) diagramme d'arborescence multi-bot affichait `venv/` au lieu de `.venv/`

---

## [0.68] — 2026-06-08

### Corrections
- **`tradinebotte-polymarket/scripts/update_standalone.sh` — `connectors/` et `strategy_engines/` jamais synchronisés** : les quatre scripts de déploiement (`update_claude2.sh` à `update_claude5.sh`) sont des wrappers autour de `update_standalone.sh` ; sa fonction `_rsync()` synchronisait `tradinebotte-polymarket/`, `strategies/`, `requirements.txt` et `tradinetools/` mais omettait `tradinebotte-cex/connectors/` et `tradinebotte-cex/strategy_engines/` ; après une mise à jour de code, `live_bot.py` démarrait et plantait immédiatement avec `ModuleNotFoundError: No module named 'connectors'` ; les deux répertoires ajoutés à la séquence rsync

---

## [0.67] — 2026-06-07

### Changements
- **`scripts/install.sh` — virtualenv standardisé en `.venv`** : création et toutes les références passées de `venv/` à `.venv/` ; le bloc de détection vérifie `.venv` en premier (nouvelles installations), puis bascule sur `venv` pour les installations existantes ; section runtime-paths de `CLAUDE.md` mise à jour pour refléter `.venv/`
- **`tradinebotte-polymarket/scripts/start_feed.sh` — détection dual venv** : vérifie `.venv/bin/python3` en premier, puis bascule sur `venv/bin/python3` pour la compatibilité descendante ; était précédemment codé en dur sur `venv` uniquement, ce qui échouait sur les comptes où `install.sh` avait déjà créé `.venv`
- **`tradinebotte-polymarket/scripts/start_account.sh` — chaîne complète de fallback venv pour Option B** : vérifie `.venv` puis `venv` dans le répertoire du compte, puis `~/tradinebotte/.venv`, puis `~/tradinebotte/venv` en dernier recours ; vérifiait précédemment uniquement le `venv` du répertoire de compte, ce qui échouait avec le layout venv partagé Option B

### Corrections
- **Tous les comptes de déploiement migrés de `venv/` vers `.venv/`** : unités de service `systemd --user` existantes mises à jour (chemins `ExecStart` patchés de `venv/bin/python3` vers `.venv/bin/python3`), anciens répertoires `venv/` supprimés ; répertoires `connectors/` et `strategy_engines/` synchronisés vers les comptes à layout plat qui en manquaient après les mises à jour rsync

---

## [0.66] — 2026-06-07

### Corrections
- **`UPDATE.md`, `UPDATE.fr.md` — Option B manquait le redémarrage de `start_feed.sh`** : la séquence de mise à jour multi-bot tuait les trois processus (feed + 2 comptes) mais ne redémarrait que les account bots ; le feed n'était jamais relancé, laissant les deux account bots sans données ; `bash tradinebotte-polymarket/scripts/start_feed.sh` ajouté avant les lignes `start_account.sh`
- **`INSTALL.md`, `INSTALL.fr.md`, `UPDATE.md`, `UPDATE.fr.md` — exclusions rsync manquantes pour `.venv/`** : `--exclude='venv/'` n'exclut pas les répertoires cachés `.venv/` ; la racine du repo contient un virtualenv de développement `.venv/` qui était silencieusement rsynced vers le serveur ; ajout de `--exclude='.venv/'` (UPDATE.md) et `--exclude='.venv'` (INSTALL.md) dans les quatre commandes rsync des quatre fichiers

---

## [0.65] — 2026-06-07

### Corrections
- **`tradinebotte-polymarket/scripts/start_account.sh` — lookup du venv échouait pour le layout multi-bot Option B** : le script cherchait le venv dans `$TRADINEBOTTE_DIR/venv` (ex. `~/account-a/venv`) alors que le layout Option B maintient un venv partagé unique dans `~/tradinebotte/venv` ; `install.sh` ne tourne qu'une seule fois pour le répertoire partagé, pas par compte ; corrigé en vérifiant `$INSTALL_DIR/venv` en premier puis en basculant sur `$HOME/tradinebotte/venv` ; message d'erreur mis à jour avec la commande de correction correcte
- **`scripts/setup.py` — message de mode simulation montrait une mauvaise commande de démarrage** : le message « Launch the bot: » affichait `bash scripts/start_bot.sh` (inexistant à la racine du repo) ; corrigé pour afficher `$INSTALL_DIR/run.sh` (le wrapper généré par `install.sh`)
- **`INSTALL.md`, `INSTALL.fr.md` — Méthode 1 (git clone) et Méthode 3 (tar.gz) référençaient encore `v0.5.0`** : mis à jour en `v0.63`

---

## [0.64] — 2026-06-07

### Corrections
- **`scripts/install.sh` — `run.sh` généré avec un mauvais chemin vers `start_bot.sh`** : `run.sh` était généré avec `exec bash "$REPO_DIR/scripts/start_bot.sh"` mais `start_bot.sh` se trouve dans `tradinebotte-polymarket/scripts/start_bot.sh`, pas dans `scripts/` à la racine du dépôt ; corrigé en `$REPO_DIR/tradinebotte-polymarket/scripts/start_bot.sh` ; le message NEXT STEPS pointe désormais directement vers le wrapper `$INSTALL_DIR/run.sh` généré plutôt que vers le chemin brut du script
- **Documentation — tous les chemins de scripts corrigés dans les 10 fichiers** : `bash scripts/start_bot.sh` remplacé par `~/tradinebotte/run.sh` dans tous les scénarios (INSTALL.md, INSTALL.fr.md, UPDATE.md, UPDATE.fr.md, QUICKSTART.md, QUICKSTART.fr.md) ; `bash scripts/monitor.sh` remplacé par `bash tradinebotte-polymarket/scripts/monitor.sh` ; `bash scripts/start_feed.sh` et `bash scripts/start_account.sh` remplacés par leurs équivalents dans `tradinebotte-polymarket/scripts/` ; `scripts/` à la racine ne contient que les scripts génériques (`install.sh`, `setup.py`, `run_tests.sh`, etc.) — les scripts de lancement spécifiques à polymarket n'y ont jamais été
- **Documentation — compteur de tests corrigé dans INSTALL.md, INSTALL.fr.md, README.md, README.fr.md** : `1 148 tests en 5 suites` mis à jour en `733 tests en 4 suites` suite à la suppression de `scalping_bot.py` et de ses 415 tests en v0.63 ; mentions des stratégies de scalping supprimées de la prose de la section Tests
- **QUICKSTART.md, QUICKSTART.fr.md — référence de version obsolète mise à jour** : les exemples de commandes référençaient `v0.50` ; mis à jour en `v0.63`

---

## [0.63] — 2026-06-07

### Modifications
- **Scripts de déploiement — exclusions rsync pour éviter le redéploiement de fichiers inutiles** : `update_standalone.sh`, `deploy_accumulation.sh`, `deploy_scalping_claude4.sh` et `update_swing.sh` excluent désormais `account_bot.py` et `feed.py` du rsync polymarket (inutiles sur les comptes live-only ou CEX) ; `deploy_accumulation.sh`, `deploy_scalping_claude4.sh` et `update_swing.sh` excluent `scalping_bot.py`, `scalping_math.py`, `api_bitstamp.py` et `api_mexc.py` du rsync CEX (non utilisés par les bots en cours d'exécution)

### Supprimé
- **`tradinebotte-cex/scalping_bot.py` et `tradinebotte-cex/tests/test_scalping_bot.py`** — bot de scalping OHLCV 1 minute et sa suite de 415 tests supprimés du dépôt source ; le bot n'était déployé sur aucun compte et n'était importé par aucun module en cours d'exécution ; `analysis/backtest_scalping.py` et `tests/test_scalping.py` (qui testent le moteur de backtest, pas le bot live) sont conservés ; `api_binance.py`, `api_bitstamp.py`, `api_mexc.py` et le registre `connectors/` sont maintenus car ils servent d'autres bots déployés

### Maintenance
- **Nettoyage serveur — suppression des fichiers obsolètes sur tous les comptes** : suppression des répertoires `venv/` dupliqués (108 Mo × 3 comptes ; les services actifs utilisent `.venv`) ; suppression des résidus de git clone (`scripts/`, `tests/`, `.github/`, `.git-hooks/`, `.mypy_cache/`, `reports/` et fichiers de documentation/notes) sur les trois comptes initialement déployés par git clone ; suppression des anciens fichiers JSON de stratégie en layout plat et des modules Python de stratégie obsolètes (`strategies/base.py`, `strategies/grid.py`, `strategies/__init__.py`) remplacés par `strategy_engines/` et le layout JSON ; suppression des fichiers PID obsolètes, des bases de données et logs scalping orphelins, et d'une sauvegarde de base de données corrompue ; suppression de neuf anciens fichiers d'unité système dans `/etc/systemd/system/` dont trois encore `enabled` qui auraient démarré au prochain reboot (désactivés avant suppression)

---

## [0.62] — 2026-06-07

### Ajouts
- **Migration des services user étendue à tous les comptes restants** : live_bot migré vers des unités user `tradinebotte-live.service` sur les quatre comptes restants (compte-2 : unité installée mais inactive ; compte-4 : aucune unité ; compte-5 : process nohup actif ; compte-3 : déjà actif) ; `migrate_to_user_services.sh` mis à jour pour couvrir les quatre comptes, ajout du kill nohup avant le démarrage (ignoré si le service est déjà actif), et ajout des options SSH manquantes `-o PreferredAuthentications=password -o ServerAliveInterval=10 -o ServerAliveCountMax=3` qui provoquaient un blocage indéfini sur les serveurs en auth par mot de passe
- **Unités user `tradinebotte-accumulation.service` sur deux comptes** : `accumulation_bot` sur le compte standalone (stratégie btc_accumulation) et sur le compte scalping (stratégie btc_accumulation_deepdip) migrés de nohup vers des unités user ; processus nohup stoppés proprement avant le démarrage ; chemin de stratégie intégré dans l'unité à l'installation
- **Unité user `tradinebotte-orderbook.service` sur le compte scalping** : `orderbook_bot` migré de nohup vers une unité user ; processus nohup stoppé proprement avant le démarrage
- **`migrate_cex_bots.sh` — script de migration en une seule passe pour les comptes CEX** : installe et démarre les trois unités user CEX (accumulation ×2, orderbook ×1) en une seule exécution ; détecte `.venv` vs `venv` à l'installation ; affiche les instructions Phase 2 `loginctl enable-linger` pour root ; modèles d'unités stockés dans `tradinebotte-cex/scripts/systemd/`

### Modifications
- **`update_swing.sh` — redémarrage dual-path remplace le chemin nohup-only** : détecte `tradinebotte-live.service` actif/activé et utilise `systemctl --user restart` si présent ; bascule sur nohup sinon ; étape VERIFY mise à jour du contrôle PID-file vers l'approche `pgrep`/filtre-exe des autres scripts ; attend 36s après un redémarrage systemd (RestartSec=30) contre 6s après nohup
- **`deploy_scalping_claude4.sh` — redémarrage dual-path pour orderbook_bot** : détecte `tradinebotte-orderbook.service` actif/activé ; chemin systemd préféré, fallback nohup conservé ; étape VERIFY mise à jour vers pgrep/filtre-exe
- **`deploy_accumulation.sh` — redémarrage dual-path pour accumulation_bot** : détecte `tradinebotte-accumulation.service` actif/activé ; chemin systemd préféré, fallback nohup conservé ; détection venv utilise le fallback `.venv` → `venv` au lieu du chemin `venv/` codé en dur ; étape VERIFY mise à jour vers pgrep/filtre-exe

---

## [0.61] — 2026-06-07

### Correctifs
- **`update_claude1.sh` — mot de passe du compte pipé via `sudo -S` en SSH pour redémarrer les services** : `_restart_service()` construisait des commandes distantes de la forme `echo '$password' | sudo -S systemctl restart <svc>` — exposant le credential du compte dans la liste des arguments de processus visible par n'importe quel utilisateur via `ps aux` ; cause racine : les services indicateurs, feed et account-bot étaient des services système (`/etc/systemd/system/`) nécessitant des privilèges root pour redémarrer ; les trois ont été migrés vers des unités user (`~/.config/systemd/user/`) pour que `systemctl --user restart` fonctionne sans sudo ni exposition de credential

### Ajouts
- **`migrate_claude1_services.sh` — script de migration des services système → services user pour le compte 1** : la phase 1 (SSH, sans sudo) écrit les trois unités user et les active ; la phase 2 affiche les étapes admin à exécuter sur le serveur (linger + stop/disable des services système + démarrage des services user dans le bon ordre) ; les services user ne doivent pas démarrer pendant que les services système occupent les ports ZeroMQ 5557, 5559 et 5561 — arrêter les services système en premier est obligatoire
- **`systemd/tradinebotte-indicators.user.service`**, **`systemd/tradinebotte-feed.user.service`**, **`systemd/tradinebotte-account.user.service`** — modèles d'unités user pour les trois services du compte 1 ; utilisent le spécificateur `%h` pour le répertoire home, `WantedBy=default.target`, `After=network.target` ; `ExecStart` et variables d'environnement copiés verbatim depuis les unités système actives

---

## [0.60] — 2026-06-07

### Correctifs
- **`update_standalone.sh` — l'étape VERIFY signalait "running" sur un bot mort** : le cmdline bash de la session SSH de vérification contient la chaîne littérale `live_bot` (issue du pattern pgrep) ; `pgrep -f 'python.*live_bot'` s'auto-correspondait au processus bash, rendant `MPID` non-vide même quand le bot réel n'était pas en cours d'exécution ; `grep -qE '^PID=[0-9]+ running'` retournait succès pour un bot mort ; remplacé par une boucle filtrée par `/proc/$P/exe` comme dans tous les autres scripts de déploiement
- **`update_standalone.sh` — branche "system service" morte : pgrep nu + pas de vrai redémarrage** : la branche `elif [ -f "/etc/systemd/system/$SVC" ]` utilisait `pgrep -u $SA_USER -f 'python3.*live_bot'` sans filtre exe (même bug d'auto-correspondance), et le corps de redémarrage se contentait d'afficher `systemd restarted` sans exécuter aucune commande `systemctl` ; un bot tué par ce chemin ne redémarrait jamais ; branche supprimée (tous les comptes sont sur des services user)
- **Scripts de déploiement — `ServerAliveInterval` manquant dans quatre scripts sur cinq** : `update_standalone.sh` avait `-o ServerAliveInterval=10 -o ServerAliveCountMax=3` dans `_ssh()` mais `deploy_scalping_claude4.sh`, `update_swing.sh` et le nouveau script base `deploy_accumulation.sh` en étaient dépourvus ; les connexions SSH de ces scripts pouvaient rester bloquées indéfiniment en cas d'interruption réseau

### Ajouts
- **`deploy_accumulation.sh` — script base paramétrable remplace 191 lignes dupliquées** : `deploy_accumulation_claude3.sh` et `deploy_accumulation_claude4.sh` étaient des copies quasi-identiques (4 variables différentes sur 191 lignes) ; les deux sont désormais de fins wrappers (variables d'environnement `ACCUM_USER_IDX` et `BOT_STRATEGY`) délégant au nouveau script base ; un seul correctif se propage maintenant aux deux comptes au lieu de nécessiter deux modifications
- **`update_claude4.sh` — wrapper manquant pour le compte scalping (index 3)** : tous les autres comptes avaient des wrappers `update_claudeN.sh` autour de `update_standalone.sh` ; le compte scalping n'en avait pas, nécessitant des invocations manuelles `TEST_STANDALONE_USER_IDX=3` ; ajouté suivant le même modèle que `update_claude2.sh`, `update_claude3.sh`, `update_claude5.sh`
- **`deploy_all.sh` — script de déploiement séquentiel global** : déploie les huit bots sur tous les comptes dans l'ordre requis (même serveur — jamais en parallèle) ; affiche un résumé OK/FAILED par compte en fin d'exécution ; transmet les flags `--skip-restart` et `--verify-only` à tous les sous-scripts

---

## [0.59] — 2026-06-06

### Correctifs
- **Scripts de déploiement — authentification SSH par mot de passe cassée par une clé installée** : `ssh-copy-id` avait installé une clé locale protégée par passphrase dans les `authorized_keys` des comptes de déploiement ; SSH tentait l'auth par clé en premier et sshpass injectait le mot de passe du compte comme passphrase de la clé, le consommant avant le défi d'auth par mot de passe ; ajout de `-o PreferredAuthentications=password` dans tous les helpers `_ssh()` et `_rsync()` des six scripts de déploiement afin que l'auth par clé soit entièrement contournée (`update_standalone.sh`, `update_claude1.sh`, `deploy_accumulation_claude3.sh`, `deploy_accumulation_claude4.sh`, `deploy_scalping_claude4.sh`, `update_swing.sh`)
- **Scripts de déploiement — processus orphelins périmés non tués au déploiement** : quand un processus bot mourait en dehors d'un redémarrage géré son fichier PID devenait périmé ; le déploiement suivant démarrait une nouvelle instance sans arrêter l'ancienne, laissant deux bots partager la même base de données SQLite et produisant des erreurs de verrou DB ; ajout de la détection d'orphelins via `pgrep -u $(whoami) -f "<bot_script>"` avant chaque redémarrage dans tous les scripts de déploiement
- **Scripts de déploiement — `pgrep -f` correspondait au processus bash du déployeur et se tuait lui-même** : `pgrep -f "python.*bot_script.py"` correspondait au processus bash SSH exécutant la commande de déploiement car le texte complet du script (incluant la chaîne littérale `python3 bot_script.py` de la ligne `nohup`) apparaît dans le cmdline du processus bash ; le tuer abandonnait silencieusement le déploiement avant l'exécution de `nohup`, ne produisant ni fichier PID ni bot en cours d'exécution ; corrigé en vérifiant `readlink /proc/$P/exe | grep python` avant de tuer afin que seuls les vrais processus python soient ciblés, pas le shell déployeur

---

## [0.58] — 2026-06-05

### Corrections
- **`scalping_bot.py` — double déduction de fee** : la fee d'entrée était déduite deux fois (une fois à l'ouverture, une fois intégrée dans le calcul du PnL à la clôture) ; tous les PnL historiques étaient sous-estimés d'environ une fee d'entrée par trade
- **`scalping_bot.py` — TP évalué avant SL quand les deux sont touchés dans la même bougie** : pour les positions longues, le TP est désormais vérifié en premier ; suppression aussi de la table `candles` DDL créée mais jamais écrite
- **`scalping_bot.py` — dénominateur ATR utilisait un close périmé** : la stratégie breakout divisait l'ATR par `closes[-2]` au lieu de `closes[-1]`
- **`dca.py` — clé `"orderId"` incorrecte** : `_check_rest_fills` utilisait `o["orderId"]` mais `api_binance.get_open_orders` normalise en `o["order_id"]` ; le set était toujours vide, chaque ordre BUY semblait rempli et des TP SELL en double étaient placés à chaque poll
- **`dca.py` — `_on_tp_hit` appelé sans condition pour status `"long"`** : ajout de la garde `pos.sell_order_id is None` pour éviter les TP SELL en double
- **`orderbook_bot.py` — les gates longs bloquaient les shorts en mode `direction="both"`** : les cinq gates faisaient un `return` global ; chaque gate switche désormais `direction` vers `"short"` au lieu d'abandonner
- **`orderbook_bot.py` — shutdown laissait `pnl_net`/`capital_after` NULL** : le handler d'arrêt écrit désormais une estimation mark-to-market pour les positions ouvertes
- **`swinghold.py` — `qty_held` persisté avant le fill réel** : les positions `buy_placed` sont créées avec `qty_held=0.0` et `_on_buy_filled` fixe la bonne valeur
- **`swinghold.py` — état de vente partielle intermédiaire jamais persisté** : `_on_partial_sell_filled` appelle `_save_state` avant la prochaine vente partielle pour les positions encore ouvertes
- **`swinghold.py` — `_close_sl` avalait silencieusement les erreurs de cancel** : l'exception est désormais loggée avec avertissement
- **`accumulation_bot.py` — floor du cooldown adaptatif ignoré** : `max(floor_iv, base_iv // 2)` était toujours `base_iv // 2` ; corrigé en `min(floor_iv, base_iv // 2)`
- **`accumulation_bot.py` — `avg_entry` non remis à zéro après vente totale** : `avg_entry` est maintenant réinitialisé à `0.0` quand `holdings_btc` atteint zéro
- **`grid.py` — tâche user stream recréée à chaque tick sans ordres actifs** : la garde vérifie maintenant qu'il existe au moins un ordre actif avant de créer la tâche
- **`grid.py` — annulation de la tâche user stream non awaitée** : `_cancel_all_orders` attend maintenant la fin de la tâche avant de halter
- **`api_bitstamp.py` — signature `post_order` incompatible** : corrigée pour correspondre à l'interface unifiée `(session, symbol, price, size_usdc, *, side)`
- **`api_bitstamp.py` — sim mode retournait un `dict` au lieu d'une `str`** : retourne désormais `f"sim_..."` comme tous les autres connecteurs
- **`api_bitstamp.py` — `get_open_orders` retournait le format Bitstamp brut** : normalisé vers `{"order_id", "side", "price", "qty", "status"}`
- **`api_bitstamp.py` — signature HMAC incorrecte** : nonce et timestamp étaient inversés, `"v2"` manquait ; toutes les requêtes authentifiées auraient retourné HTTP 403
- **`api_mexc.py` — log en français** : traduit en anglais conformément à la politique linguistique

### Ajout
- **`api_bitstamp.py` — stubs user-stream** : `get_listen_key`, `keepalive_listen_key`, `make_user_stream_url`, `parse_user_stream_msg` ; le connecteur est désormais chargeable via `connector="bitstamp"`
- **`connectors/__init__.py`** : `bitstamp` enregistré dans `_REGISTRY` ; `swinghold` et `dca` ajoutés dans `_STRATEGY_REQUIREMENTS`
- **`indicators.py` — streams dynamiques morts redémarrables** : `_start_stream` détecte les tâches terminées/crashées avant la garde `"already active"`
- **`indicators.py` — `db_path` restreint à `TRADINEBOTTE_DIR`** : empêche un souscripteur de créer ou chmodifier des fichiers arbitraires via le socket REP

---

## [0.57] — 2026-06-05

### Corrections
- **`bot_utils.py` — suppression de la fonction `warn_if_external_bind` morte** : la fonction était un doublon de `tradinetools.zmq.warn_if_external_bind` ; tous ses appelants avaient déjà migré vers la version `tradinetools` ; la copie dans `bot_utils` n'était plus atteignable
- **`api_polymarket.py` — mise en cache du `ClobClient` entre les trades** : le client CLOB authentifié était reconstruit à chaque ordre, déclenchant une dérivation de clé EIP-712 à chaque trade ; le client est désormais initialisé une seule fois par processus et mis en cache par `(private_key, install_dir)`, éliminant ce surcoût de la boucle critique des ordres
- **`account_bot.py` — chemin de verrou du feed stable entre les processus** : `abs(hash(addr))` servait à construire le nom du fichier de verrou exclusif pour la coordination du démarrage du feed ; `hash()` de Python étant aléatoire par processus depuis Python 3.3+, deux account_bots calculant ce chemin pour la même adresse de feed obtenaient des noms différents, cassant silencieusement la coordination et risquant de lancer deux instances `feed.py` qui se disputent le même port ; remplacé par `hashlib.md5(addr.encode()).hexdigest()[:8]`, déterministe entre tous les processus

---

## [0.52] — 2026-06-02

### Ajout
- **`docs/logging.md` — vocabulaire canonique des tags de log** : documente chaque ligne de log structurée préfixée par `[TAG]` dans les quatre modules (`live_bot.py`, `feed.py`, `account_bot.py`, `indicators.py`) ; inclut les deux lignes visuelles (`▶ TRADE`, `✓ WIN`/`✗ LOSS`) avec leurs patterns grep ; définit la convention dynamique `[<stream_id>]` utilisée par `indicators.py` ; inclut des règles pour l'ajout de nouvelles lignes structurées

### Modifications
- **`live_bot.py` — standardisation des préfixes structurés sans crochets** : quatre lignes de log ne respectant pas le format `[TAG]` sont désormais cohérentes avec le reste du code : `VOL FILTER` → `[VOL_FILTER]`, `Kelly:` → `[KELLY]`, `CIRCUIT-BREAKER:` → `[CIRCUIT_BREAKER]`, `post_order returned None — aborting entry to prevent ghost trade` → `[GHOST_GUARD] post_order returned None — aborting entry`
- **`feed.py` — suppression des tags parasites** : `[VERBOSE]` (pas un événement parseable — remplacé par du texte simple) et `[WS ERROR]` (redondant — fusionné dans la famille `[WS]` sous `[WS] traceback:`)

---

## [0.56] — 2026-06-02

### Modifications
- **`live_bot.py` — ligne de log `▶ TRADE` enrichie du contexte du signal** : ajout de `obi=%.3f` et `ask_vol=%.0f` pour que la ligne d'entrée soit auto-suffisante ; aucune jointure DB nécessaire pour retrouver les valeurs OBI et ask-volume ayant déclenché le signal
- **`live_bot.py` — ligne `✓ WIN` / `✗ LOSS` enrichie de la durée de hold** : ajout de `duration=%ds` calculé à partir de `resolution_ts_ms − signal_ts_ms` ; permet aux workflows grep-log de repérer les holds anormalement longs sans interroger la base de données

---

## [0.55] — 2026-06-02

### Correctifs
- **`.gitignore` — suppression des exceptions `data/*.db`** : les quatre bases de données de référence étaient explicitement dé-ignorées et committées comme blobs binaires ; suppression des exceptions `!data/*.db` afin que tous les fichiers `.db` soient ignorés ; fichiers retirés de l'index git avec `git rm --cached` (copies locales conservées)
- **GitHub Actions — épinglage de toutes les actions au SHA de commit** : `actions/checkout`, `actions/setup-python` et `anthropics/claude-code-action` étaient référencés par tag (`@v6`, `@v1`, etc.) ; un tag compromis pourrait silencieusement rediriger la CI vers du code malveillant ; les cinq fichiers de workflow sont désormais épinglés au SHA de commit exact avec le tag conservé en commentaire pour la lisibilité

---

## [0.54] — 2026-06-02

### Ajout
- **`tradinebotte-cex/strategies/accumulation/btc_accumulation_deepdip.json` — stratégie d'accumulation deep-dip v1.0** : stratégie différenciée pour le second compte d'accumulation ; backtesté 2024-01-01 → 2026-06-02 sur klines Binance 1h live ; +41% contre +38% pour la v1.5 standard (+3pp), pic à +96% vs +75% (+21pp au sommet du marché haussier) ; différences clés par rapport à la v1.5 : pas de mise initiale (attend les vrais dips), seuil OBI plus strict 0.70 vs 0.50, tranches plus larges $250 vs $100, bandes de profit plus hautes (15/30/50/75/100% vs 5/10/20/30/50%), fraction de vente réduite à 8% vs 15% (conserve plus de BTC), gates Fear&Greed et ratio L/S désactivées (évite le blocage lors de forts dips OBI en régime greed), seuil macro OBI resserré à -0.50
- **`tradinebotte-cex/scripts/deploy_accumulation_claude4.sh`** : `BOT_STRATEGY` mis à jour vers `btc_accumulation_deepdip.json`

---

## [0.53] — 2026-06-02

### Correctifs
- **`scripts/update_standalone.sh` — suppression du DB lock au déploiement** : le mécanisme nohup + fichier PID est remplacé par `systemctl --user restart tradinebotte-live.service` pour les comptes migrés vers les services utilisateur ; aucun sudo requis car `systemctl --user` opère entièrement dans l'instance systemd du compte ; retombe sur le kill + redémarrage par systemd pour les comptes encore sur des services système

### Ajout
- **`scripts/migrate_to_user_services.sh` — migration des services live bot vers les units utilisateur** : phase 1 (SSH en tant que l'utilisateur bot, zéro sudo) écrit `~/.config/systemd/user/tradinebotte-live.service`, exécute `systemctl --user enable` + `start` ; la phase 2 affiche les deux commandes admin nécessaires une fois par compte : `loginctl enable-linger <user>` (rend l'instance systemd de l'utilisateur persistante après reboot — nécessite root car écrit dans `/var/lib/systemd/linger/`) et `systemctl stop/disable tradinebotte-live-<user>.service` pour supprimer l'ancienne unit système
- **`scripts/systemd/tradinebotte-live.service` — template d'unit utilisateur** : identique à l'ancienne unit système sans `User=` (implicite pour les services utilisateur) et avec `WantedBy=default.target` + `After=network.target` (cibles accessibles sans sudo)

---

## [0.51] — 2026-06-02

### Correctifs
- **`tradinebotte-polymarket/live_bot.py` — fuite des logs vers syslog** : `logging.basicConfig()` est sans effet quand des bibliothèques tierces (`aiohttp`, `websockets`) configurent le root logger avant l'exécution de `_setup_logging()` ; sans `force=True`, le root logger conserve un `StreamHandler` pointant vers stderr, tous les enregistrements du logger `"live"` y propagent, et systemd les capture dans le journal système/syslog ; ajout de `force=True` à l'appel `basicConfig()` dans `_setup_logging()` afin que les handlers existants du root logger soient toujours remplacés par le pipeline `FileHandler`-only voulu
- **`tradinebotte-polymarket/feed.py` — logs vers fichier au lieu de stdout** : remplacement de `logging.basicConfig(handlers=[StreamHandler(stdout)])` par `tradinetools.setup_logger("feed", feed.log)` ; les logs vont désormais dans un fichier local rotatif ; la sortie stdout n'est conservée qu'en TTY interactif
- **`tradinebotte-polymarket/account_bot.py` — logs vers fichier au lieu de stdout** : même migration de `basicConfig(stream=sys.stdout)` vers `tradinetools.setup_logger("account", account.log)`
- **`tradinebotte-indicators/indicators.py` — logs vers fichier au lieu de stdout** : même migration vers `tradinetools.setup_logger("indicators", indicators.log)`
- **Fichiers de service systemd — défense en profondeur** : ajout de `StandardOutput=null` et `StandardError=null` dans les six fichiers de service tradinebotte afin que le journal système ne capture plus jamais la sortie des bots, même en cas de régression future du système de logging

---

## [0.50] — 2026-05-31

### Ajout
- **`tradinebotte-indicators/indicators.py` — flux full-depth futures (`btc_full_depth_perp`)** : nouvelle constante `_BINANCE_FUTURES_DEPTH_URL` (`https://fapi.binance.com/fapi/v1/depth`) ; `_fetch_depth_snapshot()` accepte désormais les paramètres `url` et `limit` (spot : limit=5000, futures : limit=1000) ; `_binance_full_depth_task` reçoit deux nouveaux paramètres — `market` (`"spot"` ou `"perp"`) sélectionne les bons endpoints WebSocket et REST, et `bid_depth_pct` / `ask_depth_pct` réduisent le carnet à une fenêtre de prix dynamique autour du mid-price (0=désactivé), appliquée au chargement du snapshot et à chaque cycle de publication pour borner la mémoire utilisée ; la validation de synchronisation futures utilise le chaînage `pu` (`ev["pu"] == last_update_id`) au lieu de `U == lastId+1` (le protocole diff Binance futures diffère du spot) ; nouveau flux `btc_full_depth_perp` déployé aux côtés du flux spot `btc_full_depth` existant
- **`tradinebotte-indicators/indicators.py` — base de données SQLite partagée du carnet d'ordres** : nouvelles fonctions utilitaires `_init_depth_db()` et `_write_depth_to_db()` ; mode journal SQLite = DELETE (pas WAL) afin que les lecteurs multi-utilisateurs n'aient besoin que des droits de lecture sur le fichier — aucun accès en écriture au répertoire requis pour les fichiers `-shm`/`-wal` ; deux tables : `orderbook_current` (dernier carnet bucketisé — 1 ligne par stream/side/bucket de prix, remplacée à chaque écriture) et `orderbook_snapshots` (ring-buffer de snapshots JSON horodatés avec rétention configurable) ; nouveaux paramètres de flux : `db_path` (défaut `""` = désactivé), `bucket_size_usd` (défaut 50), `db_write_every_n` (défaut 60, soit environ une fois par minute), `history_retention_h` (défaut 24) ; fichier DB créé avec les droits `0o644` — lisible par tous les utilisateurs ; `run_in_executor` utilisé pour que les écritures SQLite ne bloquent jamais l'event loop async ; le chemin de la base de données du carnet d'ordres partagée et la taille des buckets sont configurables par déploiement

---

## [0.49] — 2026-05-31

### Ajout
- **`tradinebotte-cex/accumulation_bot.py` v1.4 — six améliorations** : cooldown adaptatif qui raccourcit l'attente de scale-in quand la pression OBI est forte ; trailing stop de rebuy avec expiration (supprime automatiquement les niveaux de rebuy obsolètes) ; buffer Earn configurable (`earn_buffer_usd`) pour conserver un minimum de USDT liquide en spot ; gate VWAP appliquée à l'achat initial uniquement (`vwap_gate_initial`), les entrées de scale-in restent non filtrées ; seuil OBI plus élevé requis pour la réduction du cooldown
- **`tradinebotte-cex/accumulation_bot.py` v1.5 — quatre nouvelles gates de signal** :
  - Gate Fear & Greed (`fear_greed_gate`) : bloque les achats quand l'indice > 80 (avidité extrême), booste la mise quand l'indice < 25 (peur extrême)
  - Gate liquidations (`liq_gate`) : bloque l'entrée sur un pic de liquidations shorts importantes (signal de longs surchargés), booste la mise sur un pic de liquidations longs (vente forcée)
  - Gate ratio Long/Short (`ls_ratio_gate`) : bloque les nouveaux achats quand le ratio dépasse 3,0 (longs sur-levés)
  - Gate RSI 4h (`rsi4h_gate`) : bloque quand le RSI > 70 (sur-acheté), assouplit l'exigence VWAP quand le prix est sous le VWAP et le RSI < 35
- **`tradinebotte-cex/orderbook_bot.py` v2.12 — gate liquidations** : paramètre `liq_gate` (désactivé par défaut, `"liq_gate": false`) et seuil `liq_long_block_usd` ; reprend la logique de la gate du bot d'accumulation
- **`tradinebotte-cex/strategies/scalping/orderbook_btc.json` v2.12** — valeurs par défaut mises à jour
- **`tradinebotte-cex/strategies/longterm/btc_accumulation.json` v1.5** — valeurs par défaut mises à jour avec tous les nouveaux paramètres de gates

### Modifications
- **Tous les paramètres de stratégie codés en dur sont désormais surchargeables via JSON** (`accumulation_bot.py`, `orderbook_bot.py` et bots Polymarket) : chaque constante Python qui nécessitait auparavant une modification du code peut maintenant être définie dans le fichier JSON de stratégie ; les constantes restent actives comme valeurs par défaut si la clé est absente du JSON

### Correctifs
- **`tradinebotte-indicators/indicators.py` — watchdog de timeout WebSocket** : les trois boucles WebSocket Binance (`_binance_kline_task`, `_binance_scalping_task`, `_binance_full_depth_task`) encapsulent désormais `ws.recv()` dans `asyncio.wait_for(..., timeout=120)` — empêche le blocage indéfini quand Binance maintient le keepalive TCP sans envoyer de données (incident observé : prix figé pendant 38 heures) ; nouvelle constante `_WS_RECV_TIMEOUT_S = 120`
- **`tradinebotte-indicators/indicators.py` — flux de liquidations** : `_binance_liquidations_task` réécrit depuis un endpoint REST signé (nécessitait des credentials API, toujours désactivé en pratique) vers le WebSocket public `wss://fstream.binance.com/ws/{symbol}@forceOrder` ; aucun credential requis ; les données de liquidations sont désormais actives et disponibles pour tous les consommateurs de gates
- **`tradinebotte-indicators/indicators.py` — nettoyage** : imports inutilisés supprimés (`hashlib`, `hmac`, `urllib.parse`) et constante inutilisée `_BINANCE_FORCE_ORDERS_URL` supprimée

---

## [0.48] — 2026-05-30

### Ajout
- **Monorepo Phase 1 — bibliothèque partagée `tradinetools` v0.1** : package Python `tradinetools/` avec `zmq.py` (fabriques de sockets `make_pub/sub/rep/req`, `warn_if_external_bind`, constantes de ports), `schemas.py` (dataclasses ZMQ versionnées `BookMessage`, `IndicatorsMessage`, `RegisterRequest`, `RegisterReply`, etc. avec round-trip `to_dict()`/`from_dict()`), `math.py` (indicateurs scalaires `sma_last`, `ema_last`, `atr_last`, `bollinger_last`, `vwap_last`, `vol_zscore_last`, `rolling_max_last`), `logging.py` (`setup_logger` partagé) ; installé comme package éditable via `pyproject.toml`
- **Monorepo Phase 2 — `tradinebotte-indicators/`** : service d'indicateurs entièrement isolé dans son propre sous-répertoire avec `scripts/`, `strategies/` et `tests/` dédiés ; le service adopte les fabriques ZMQ de tradinetools et le schéma v1 pour tous les messages publiés
- **Monorepo Phase 3 — `tradinebotte-polymarket/`** : service Polymarket isolé avec `feed.py`, `live_bot.py`, `account_bot.py`, `api_polymarket.py`, `bot_utils.py` et tous les scripts/stratégies/tests associés
- **Monorepo Phase 4 — `tradinebotte-cex/`** : service CEX isolé avec tous les moteurs de stratégie, bots, scripts de déploiement, configs JSON et tests
- **`tradinebotte-cex/strategy_engines/dca.py` — plugin `DCAStrategy`** : achats DCA horodatés à intervalles configurables ; ordres à cours limité avec TP et SL optionnel ; persistance SQLite ; s'intègre au schéma d'injection de connecteur partagé
- **`tradinebotte-cex/strategy_engines/swinghold.py` — plugin `SwingHoldStrategy`** : variante swing qui sort partiellement à chaque niveau de résistance (`sell_fraction` par niveau) au lieu d'un seul TP ; `hold_fraction = 1 − sell_fraction` conservé pour l'accumulation long terme ; SL complet sur la position restante
- **`tradinebotte-cex/strategy_engines/swing.py` — SELL marché pour les sorties stop-loss** : le SL s'exécute désormais via un ordre marché REST (`post_market_order`) au lieu d'une annulation de limite, garantissant le fill dans les marchés en mouvement rapide
- **`tradinebotte-cex/accumulation_bot.py` — bot d'accumulation BTC v1.2** : achat sur creux OBI avec ratchet de profit progressif ; scale-in adaptatif avec logique de rebuy ; persistance d'état à travers les redémarrages ; v1.1 corrige des bugs et ajoute le rebuy adaptatif ; v1.2 promeut les bandes de profit larges issues de la calibration comme valeurs par défaut
- **`tradinebotte-cex/orderbook_bot.py` — scalping OBI v2.3–v2.10** : v2.3 ajoute le filtre TFI (Trade Flow Imbalance) ; v2.4 direction long uniquement ; v2.5 TP/SL élargis calibrés le 2026-05-26 ; v2.7 gate TFI plat + suppression de `obi_exit_thresh` ; v2.8 gate VWAP contextuel ; v2.9 gate profil de volume ; v2.10 gate OBI macro (filtre multi-temporel)
- **`tradinebotte-indicators/indicators.py` — 4 nouvelles sources** : liquidations Bitcoin (Coinalyze), open interest, ratio long/short, taux de financement ; tous publiés sur le flux ZMQ d'indicateurs unifié ; authentification HMAC ajoutée pour l'endpoint liquidations
- **`analysis/backtest_swing_dca.py` — backtesteur de stratégies CEX** : simule DCA, Swing et SwingHold sur des bases SQLite OHLCV 1 minute ; modèle de fill : BUY remplit quand `candle_low ≤ limit_price`, SELL quand `candle_high ≥ limit_price`, SL quand `candle_low ≤ sl_price` ; verrou de récupération empêchant les ré-entrées en cascade lors de mouvements brusques à travers le cluster de supports ; modèle de capital basé uniquement sur le PnL réalisé ; modes `--compare`, `--all-dbs`, `--sweep`, `--config`
- **`analysis/backtest_orderbook.py`** — backtesteur de scalping OBI sur snapshots de carnet d'ordres live
- **`analysis/calibrate_obi_proxy.py`** — script de calibration des paramètres du proxy OBI
- **`scripts/backtest_accumulation.py`** — backtesteur de la stratégie d'accumulation
- **`tradinebotte-cex/scripts/deploy_accumulation_claude4.sh`** — script de déploiement du bot d'accumulation
- **`tradinebotte-polymarket/scripts/update_claude3.sh`** — wrapper de déploiement ciblant le troisième compte de test
- **Extension de la suite de tests** : `tradinebotte-cex/tests/test_strategy_engines.py` (64 tests couvrant `SwingStrategy`, `DCAStrategy`, `SwingHoldStrategy` — validation d'init, méthodes de calcul pures, fills asynchrones simulés via `IsolatedAsyncioTestCase`) ; `tradinetools/tests/test_zmq.py`, `test_schemas.py`, `test_math.py` (87 tests au total) ; les 5 répertoires de tests des sous-services sont désormais découverts par la CI

### Modifications
- **`.github/workflows/tests.yml`** — la CI exécute désormais `unittest discover` sur les cinq répertoires de tests et installe `tradinetools` comme package éditable avant l'exécution
- **`tradinebotte-indicators/indicators.py`** — couche de publication ZMQ remplacée par les fabriques tradinetools `make_pub`/`make_sub`/`make_rep`/`make_req` ; tous les messages sérialisés en dicts schéma v1 ; `warn_if_external_bind` appelé sur chaque adresse de bind
- **Tous les scripts de déploiement** (`update_claude1.sh`, `deploy_scalping_claude4.sh`, `deploy_accumulation_claude4.sh`) mis à jour pour rsync `tradinetools/` vers le répertoire d'installation distant et l'installer dans le `.venv` distant

### Correctifs
- **`tradinebotte-polymarket/scripts/update_claude1.sh` — `--restart-feed`** : synchronise `feed.py` et `tradinetools/` vers le serveur distant ; installe tradinetools dans `.venv` ; corrige le fichier unit systemd si `ExecStart` pointe encore vers l'ancien chemin `bot/feed.py`
- **`tradinebotte-polymarket/scripts/install_feed_service.sh`** — `BOT_DIR` corrigé pour pointer vers la racine du projet au lieu du sous-répertoire `bot/`
- **`tradinebotte-indicators/indicators.py`** — TFI perpétuel basculé de WebSocket aggTrade (indisponible) vers polling REST ; clés depth Binance futures corrigées (`b`/`a` vs `bids`/`asks`)
- **Pylint 10,00/10** sur tous les nouveaux modules et scripts

### Tests
- Total : **1 148 tests réussis** sur cinq suites de sous-services (340 + 87 + 170 + 415 + 136)

---

## [0.47] — 2026-05-24

### Ajout
- **`bot/strategy_engines/swing.py` — moteur de stratégie `SwingStrategy`** : place des ordres limit BUY sur des niveaux de support configurables et des ordres SELL sur des niveaux de résistance ; filtre directionnel EMA(200) 4h — les entrées BUY sont ignorées quand le prix est sous l'EMA 200 périodes ; stop-loss dynamique ATR(14) avec multiplicateur configurable ; filtre RSI(14, 4h) de surachat qui supprime les achats en zone tendue ; souscription au service d'indicateurs partagé via ZMQ SUB ; persistance SQLite avec `restore_from_db()` pour survivre aux redémarrages avec les positions ouvertes
- **`strategies/swing/swing_BTCUSDT.json` — config swing BTC/USDT** : supports à `[70000, 72500, 75000, 76000]`, résistances à `[78000, 80000, 82500, 85000]`, 200 $/position, 3 positions simultanées maximum, multiplicateur ATR SL 1,5
- **`bot/connectors/__init__.py`** — exigences du connecteur pour la stratégie swing enregistrées
- **`bot/strategy_engines/__init__.py`** — `SwingStrategy` enregistrée sous le type de stratégie `"swing"`
- **`bot/live_bot.py` — dictionnaire `strategy_cfg` dans `BotConfig`** : les moteurs de stratégie peuvent désormais lire des clés JSON arbitraires du fichier de stratégie via `config.strategy_cfg`, sans avoir à les ajouter comme champs fixes dans `BotConfig`
- **`strategies/indicators/indicators_all.json` — processus d'indicateurs unifié en 9 flux** : PUB sur le port 5559, REP sur le port 5561 ; le flux `btc_4h` est étendu avec EMA(50), EMA(200) et ATR(14) ; `seed_periods` porté à 250 pour un échauffement fiable des indicateurs à longue fenêtre
- **`scripts/update_swing.sh` — script de déploiement du compte swing** : rsync + écriture de `config.json` + redémarrage + vérification dans une seule session SSH, selon le même schéma que `update_standalone.sh`

### Modifications
- **`bot/indicators.py` — source `binance_scalping`** : flux WebSocket Binance combiné (depth20 + aggTrade) calculant OBI, EMA, décélération, `spread_bps`, `realized_vol_bps` et TFI en temps réel ; consommé par `orderbook_bot.py` v2.1 et par la stratégie swing via le service d'indicateurs partagé
- **`scripts/test_multibot.conf.example`** — mis à jour pour couvrir un compte de test supplémentaire dédié à la validation de la stratégie swing

### Correctifs
- **`bot/orderbook_bot.py` v2.1 — direction du signal OBI inversée** : la stratégie est désormais SHORT uniquement ; un carnet d'ordres dominé par les bids est interprété comme du spoofing indiquant une baisse imminente des prix ; la branche LONG a été entièrement supprimée
- **`bot/orderbook_bot.py` v2.1 — mécanisme `obi_exit` désactivé** : les sorties prématurées au pire point de prix ont été éliminées ; TP élargi à 15 bps, SL à 8 bps, `max_hold` porté à 3 minutes
- **`bot/orderbook_bot.py` v2.1 — mode simulation d'ordres à cours limité** : les ordres simulés sont identifiés par des identifiants préfixés `sim_`, permettant une validation en paper trade sans modifier le flux des ordres réels
- **`bot/indicators.py` — endpoint DVOL Deribit corrigé** : `get_volatility_index_data` appelait le mauvais endpoint ; corrigé pour utiliser la bonne méthode de l'API Deribit

---

## [0.46] — 2026-05-23

### Correctifs
- **`bot/orderbook_bot.py` — noms de clés WebSocket depth Binance futures** : Binance spot envoie `"bids"`/`"asks"` mais le perpétuel envoie `"b"`/`"a"` ; l'OBI perp était toujours 0.000 (bug silencieux) ; corrigé avec `msg.get("bids") or msg.get("b")` / `msg.get("asks") or msg.get("a")`
- **`bot/live_bot.py` `make_config()` — chemin de stratégie par défaut obsolète** : le fallback pointait vers `strategies/polymarket_BTC5M_v2.json` (fichier supprimé) ; mis à jour vers `strategies/polymarket/polymarket_BTC5M_piste3.json` (stratégie active actuelle) ; une installation fraîche sans clé `"strategy"` dans `config.json` utilisait silencieusement les defaults du module au lieu des paramètres calibrés de piste3
- **Purge de tous les anciens chemins** après réorganisation de l'arborescence : 42 fichiers mis à jour (docstrings bot, aide argparse, champs `_run` JSON, docstrings tests, docs/)

### Modifications
- **Réorganisation de l'arborescence du projet — 5 étapes** :
  1. `notes/` — fichiers `.txt` de planification déplacés à la racine vers `notes/`
  2. `bot/strategy_engines/` — `bot/strategies/` renommé en `bot/strategy_engines/` pour éliminer la collision de nommage Python avec le répertoire de configs JSON `strategies/`
  3. `scripts/systemd/` — templates de service systemd (`tradinebotte*.service`) déplacés de la racine `scripts/` vers `scripts/systemd/`
  4. `analysis/` — 16 scripts Python d'analyse (`backtest*.py`, `analyze_*.py`, `calibrate_obi.py`, `benchmark_api.py`, `download_*.py`, `latency.py`, `profile_*.py`) déplacés de `scripts/` vers `analysis/`
  5. Sous-répertoires `strategies/` — 24 configs JSON de stratégies organisées en sous-répertoires typés : `strategies/polymarket/`, `strategies/grid/`, `strategies/scalping/`, `strategies/longterm/`, `strategies/indicators/`
- **Filtre rsync stratégies** dans les trois scripts de déploiement (`deploy_scalping_claude4.sh`, `update_standalone.sh`, `test_multibot_deploy.sh`) : `--include='*.json' --exclude='*'` (ne récursait pas dans les sous-répertoires) remplacé par `--filter='+ **/' --filter='+ *.json' --filter='- *'`
- **En-têtes des scripts shell** corrigés : noms de produits obsolètes corrigés, texte français supprimé des commentaires de code (politique anglais uniquement), affirmations dépassées mises à jour
- **Commentaires de code** audités sur tous les modules bot : commentaires QUOI supprimés ; commentaires POURQUOI ajoutés là où une contrainte cachée, un invariant subtil ou un contournement non évident était présent

---

## [0.45] — 2026-05-23

### Correctifs
- **`scripts/deploy_scalping_claude4.sh`** — ajout de `--exclude='live_ob.db'` au rsync pour protéger explicitement la base de collecte OBI ; remplacement de la variable indéfinie `${STRATEGIES[*]}` (plantait le pre-flight avec `set -u`) par `$BOT_STRATEGY`

---

## [0.44] — 2026-05-23

### Ajout
- **`bot/orderbook_bot.py`** — nouveau bot de scalping OBI sur Binance : connexion aux streams WebSocket depth20 spot et perpétuel de Binance (100 ms), calcul de l'OBI sur les N meilleurs niveaux bid/ask avec lissage EMA, ouverture de paper trades (long spot, long ou short perp) quand l'OBI dépasse un seuil configurable pendant N snapshots consécutifs ; sortie sur inversion de l'OBI, TP/SL ou dépassement de la durée maximale ; snapshots et trades enregistrés dans `live_ob.db`
- **`strategies/orderbook_btc.json`** — configuration initiale du bot de scalping OBI : `entry_thresh=0.30`, `confirm_n=3`, `tp=0.5%`, `sl=0.3%`, 10 niveaux OBI, mode spot + perp
- **`bot/scalping_math.py`** — helpers mathématiques extraits (ATR, bandes de Bollinger, VWAP, z-score de volume, maximum glissant) partagés entre `bot/scalping_bot.py` et `bot/indicators.py`
- **`bot/scalping_bot.py`** — bot de scalping live Binance avec trois stratégies (`candle_momentum`, `meanrev`, `breakout`) ; docstring de paramètres complète couvrant les 27 clés de `DEFAULTS`
- **`scripts/backtest_scalping.py`** — moteur de backtest pour les trois stratégies de scalping (`candle_momentum`, `meanrev`, `breakout`)
- **`bot/bot_utils.py`** — ajout de `setup_bot_logger()` et `warn_if_external_bind()`
- **`bot/indicators.py`** — ajout du ring-buffer `OHLCVSeries` (ATR, bandes de Bollinger, VWAP, z-score de volume, maximum glissant) ; imports des helpers partagés depuis `scalping_math`
- **Scripts de mise à jour par compte** — deux scripts fins ciblant les deux premiers comptes de déploiement de test ; chacun accepte les mêmes options que `update_standalone.sh` et lui délègue l'exécution
- **`.pylintrc`** — `zmq` et `py_clob_client` ajoutés à `ignored-modules`

### Correction
- **`scripts/test_multibot_deploy.sh`** — remplacement du `rsync --delete` sur le dépôt complet par un rsync `bot/` à plat + rsync `strategies/` séparé + `requirements.txt` ; l'ancien flag `--delete` effaçait `live_bot.py` sur les comptes autonomes qui utilisent la structure d'installation à plat
- **`scripts/install.sh`** — `run.sh` délègue désormais à `start_bot.sh` via `exec` au lieu de lancer `live_bot.py` directement ; le lancement direct contournait le fichier PID, permettant des instances dupliquées silencieuses qui corrompaient `live.db`
- **`scripts/update_standalone.sh`** — ajout du rsync de `requirements.txt` et de `pip install -r requirements.txt` avant le redémarrage ; les dépendances n'étaient jamais mises à jour lors des déploiements de code uniquement
- **`scripts/start_bot.sh`** — préfère `.venv` à `venv` ; préfère `bot/live_bot.py` à un `live_bot.py` à plat
- **Déploiement scalping OBI** — remplacement des trois bots de scalping OHLCV défaillants (`candle_momentum`, `meanrev`, `breakout`, tous <20 % de taux de victoire sur tous les régimes de marché) par une instance unique de `orderbook_bot.py` ; l'ancien script de déploiement est remplacé

### Tests
- **`tests/test_indicators.py`** — seuil du pic z-score corrigé de 5,0 à 4,3 (limite mathématique √(n−1) = √19 ≈ 4,36 avec n=20)
- **`tests/test_scalping_bot.py`** — cible du patch modifiée de `logging.getLogger` à `setup_bot_logger`

---

## [Unreleased] - 2026-05-22

### Ajout
- **`bot/earn_manager.py` — gestionnaire Binance Simple Earn Flexible** : la classe `EarnManager` place les USDT inactifs après une vente (`park_idle()`) et les rachète avant un achat (`ensure_liquid()`) ; mode simulation si `BINANCE_API_KEY`/`BINANCE_API_SECRET` sont absents ; MEXC Earn non pris en charge (API trop instable)
- **`bot/api_bitstamp.py` — adaptateur d'échange spot Bitstamp** : même interface que `api_binance.py` (`get_markets`, `post_order`, `parse_book_update`, `compute_fee`) ; FEE_RATE = 0,1 % taker ; WebSocket `wss://ws.bitstamp.net` ; identifiants via `BITSTAMP_API_KEY`, `BITSTAMP_API_SECRET`, `BITSTAMP_CUSTOMER_ID` ; mode simulation si les identifiants sont absents
- **`strategies/longtermcyclestrategygridV1.json` / `V2.json` / `V3.json` — configs de stratégie BTC cycle long terme** : V1 (rebond 5 %/tranche 25 %, ×24,0, CAGR 43,8 %, MaxDD 81,4 %, Calmar 0,54) ; V2 (4 %/20 %, ×24,2, CAGR 43,9 %, MaxDD 81,4 %, Calmar 0,54) ; V3 ajoute des paliers de prudence relatifs au halving (T1 à 400j post-halving, T2 à 480j), Calmar 0,75
- **`scripts/analyze_btc_cycles.py` — analyse des cycles de halving BTC** : rendements, durées et statistiques Mayer Multiple par cycle
- **`scripts/analyze_cycle_volatility.py` — analyse étendue de la volatilité par cycle** : gain glissant sur 730j, pente 200DMA, analyse frontrunning C3, table de fiabilité des indicateurs
- **`scripts/backtest_cycle_strategy.py` — backtest de stratégie cycle long terme** : options `--top-mm`, `--rebound`, `--drawback`, `--tranche`, `--prudence`, `--compare`
- **`scripts/download_btc_daily_extended.py` — téléchargement OHLCV journalier BTC étendu** : récupère l'historique pluriannuel pour l'analyse de cycles
- **`scripts/backtest.py` — sizing Kelly fractionnel, ratios Sharpe/Sortino, optimisation walk-forward, filtre de volatilité par jour de semaine, sizing de mise par paliers**

### Modifié
- **`strategies/grid_BTCUSDT_bear_trailing.json`** — bornes de grid mises à jour à 60 000 $–100 000 $ avec ±30 %/40 niveaux

### Correction
- **`bot/api_binance.py`** — repli sur l'API publique actif quand aucun identifiant n'est défini
- **`scripts/download_btc_history.py`** — les lacunes de données sont ignorées au lieu d'interrompre le téléchargement

## [0.5.0] - 2026-05-18

### Ajout
- **`bot/live_bot.py` + `strategies/polymarket_BTC5M_piste3.json` — mise dynamique et calibration OBI (stratégie Piste 3)** : la mise évolue selon le prix bid avec `stake = base × (1 + bid_α × (bid − 0.96))`, plafonnée à `stake_max` et `cap × capital` ; le filtre de rejet OBI ignore les trades dont l'OBI est inférieur à `obi_reject_thresh` (supprime les buckets à faible taux de victoire) ; un stop-loss hebdomadaire stoppe le bot quand `weekly_pnl < −weekly_stop_loss` ; nouveaux champs `BotConfig` : `bid_alpha`, `secs_alpha`, `stake_max`, `capital_cap`, `obi_reject_thresh`, `weekly_stop_loss` ; `strategies/polymarket_BTC5M_piste3.json` est livré avec `bid_alpha=2.0`, `stake_max=15 $`, `cap=12 %`, `weekly_stop=60 $`, `obi_reject_thresh=−0.65` ; backtest comparé à l'original : PnL +85 %, MaxDD −28 %, Sharpe 3,28 vs 1,97 ; nouveaux scripts d'analyse : `scripts/analyze_stake_secs.py`, `scripts/backtest_stake_secs.py`, `scripts/calibrate_obi.py`
- **`bot/live_bot.py` + `strategies/polymarket_BTC15M_piste3.json` — intervalle de marché configurable (support 15M BTC)** : `BotConfig` reçoit `market_tag_id` (défaut 102892 pour 5M) et `market_window_mins` (défaut 6) ; le fichier de stratégie JSON contrôle le tag et la fenêtre — `"market_tag_id": 102467, "market_window_mins": 16` active les marchés 15M ; le log de démarrage affiche la configuration active (`Markets: BTC Up/Down 15M (tag=102467, window=±16min)`) ; les constantes `GAMMA_TAG_5M` et `GAMMA_TAG_15M` sont exposées ; l'alias legacy `GAMMA_TAG` est conservé pour la compatibilité
- **`bot/strategies/grid.py` + fichiers JSON de stratégie — mode trail grid** : le paramètre `trail_mode` accepte `"bull"` (recentre la grid vers le haut quand le prix dépasse `grid_upper`), `"bear"` (recentre vers le bas quand le prix passe sous `grid_lower`) ou `"static"` (défaut, stoppe dans les deux directions) ; `_recenter_grid()` annule tous les ordres ouverts, décale les bornes en conservant le même intervalle centré sur le prix actuel et réinitialise ; la vérification du stop-loss se branche selon le mode ; `restore_from_db` restaure les bornes sauvegardées pour que le mode trail survive aux redémarrages ; nouveaux fichiers de config : `strategies/grid_BTCUSDT_bull_trailing.json`, `strategies/grid_BTCUSDT_bear_trailing.json`
- **`bot/connectors/__init__.py` + `bot/live_bot.py` — vérification de compatibilité connecteur/stratégie** : `validate(connector_module, strategy_type)` lève une `RuntimeError` au démarrage si le connecteur manque des méthodes requises par la stratégie choisie ; liste toutes les méthodes manquantes dans le message d'erreur ; appelé dans `main()` pour les stratégies non-threshold avant la création de l'objet stratégie
- **`scripts/update_standalone.sh` — script de déploiement allégé** : rsync du contenu de `bot/` à plat dans `$INSTALL_DIR/` (correspondant à la structure de install.sh) + rsync de `strategies/*.json` ; session SSH unique pour stop + start via fichier PID ; session SSH unique pour la vérification ; options : `--skip-restart`, `--verify-only`

### Correction
- **`scripts/profile_compare.py` — f-string SQL PRAGMA éliminé** : `c.execute(f"PRAGMA mmap_size = {MMAP_MB * 1024 * 1024};")` remplacé par `c.execute("PRAGMA mmap_size = ?", (MMAP_MB * 1024 * 1024,))` ; aucun risque d'injection réel (valeur constante), mais le pattern f-string SQL est éliminé du codebase
- **`bot/live_bot.py`, `bot/feed.py`, `bot/account_bot.py` — `ClientTimeout` session ajouté** : les trois instanciations `aiohttp.ClientSession()` passent désormais `timeout=aiohttp.ClientTimeout(total=30)` ; les timeouts par requête dans les modules `api_*` couvrent déjà les appels actuels, mais le timeout session-level empêche toute future omission de bloquer l'event loop indéfiniment
- **`bot/api_binance.py`, `bot/api_mexc.py` — corps de réponses d'erreur tronqués dans les logs** : 8 appels logger qui transmettaient le corps HTTP complet (`data`) utilisent désormais `%.300s` au lieu de `%s`, limitant la représentation loguée à 300 caractères ; appels concernés : `order error`, `get_order_status error`, `cancel_order error`, `get_open_orders error` dans les deux modules
- **`bot/indicators.py` — docstring RSI corrigée en Cutler** : docstring de `compute_rsi` changée de `"Wilder RSI(n)"` en `"Cutler RSI(n): simple-mean gains/losses over the last n bars"` ; l'implémentation utilise `sum(...) / n` sur une fenêtre fixe (méthode de Cutler), non la moyenne lissée EMA de Wilder
- **`CLAUDE.md` — nombre de lignes de `live_bot.py` corrigé** : `~617 lignes` mis à jour en `~1530 lignes` (réel : 1531) ; le chiffre périmé datait d'avant l'expansion majeure du code
- **`scripts/install.sh` — packages grid/connecteur ajoutés à l'installation** : "Copie du bot" inclut désormais `api_binance.py`, `api_mexc.py` (copies à plat) + `bot/connectors/__init__.py` → `connectors/` + `bot/strategies/__init__.py` et `bot/strategies/grid.py` → `strategies/` (aux côtés des fichiers JSON) ; une installation fraîche suivie de `connector=binance strategy_type=grid` ne lève plus `ModuleNotFoundError` ; la boucle de vérification syntaxe étendue pour couvrir les 8 fichiers Python
- **`bot/live_bot.py` — commits des snapshots regroupés toutes les 30 s** : `save_snapshot()` n'appelle désormais que `conn.execute()` (chemin rapide) ; `handle_book_update()` flush avec `conn.commit()` au plus une fois toutes les `SNAPSHOT_COMMIT_SECS = 30` secondes, réduisant les commits bloquants de 50 fois par fenêtre de 5 secondes à un par 30 secondes ; toutes les opérations SQLite restent sur le thread de l'event loop — aucun executor, aucun problème de thread-safety ; 3 tests ajoutés
- **`bot/strategies/grid.py` — flag sticky `_no_credentials` empêche la re-création infinie de la task user-stream** : `_user_stream_loop` positionne désormais `self._no_credentials = True` après `MAX_KEY_FAILURES` (3) échecs consécutifs de `get_listen_key` ; le garde de création de task dans `on_book_update()` vérifie `not self._no_credentials` en premier, évitant toute recréation une fois l'absence de credentials confirmée ; 3 tests ajoutés dans `TestNoCredentialsFlag`
- **`bot/account_bot.py` — correction TOCTOU symlink sur le log feed dans `/tmp`** : le fichier log du feed est désormais ouvert avec `os.open(O_CREAT|O_WRONLY|O_APPEND|O_NOFOLLOW)` au lieu de `open()` ; empêche un symlink malveillant placé dans le répertoire `/tmp` accessible en écriture par tous de rediriger les écritures du log vers un fichier arbitraire (ex. `authorized_keys`) ; le parent ferme son fd après `Popen`, le processus enfant le conserve ouvert ; cohérent avec le fichier de verrou qui utilisait déjà `O_NOFOLLOW`
- **`scripts/test_multibot.conf.example` + `.git-hooks/pre-commit` — noms d'utilisateurs OS génériques** : les vrais noms de comptes OS remplacés par des placeholders génériques (`user1 user2 user3`) dans l'exemple de config et son commentaire de rôle ; le hook pre-commit étendu pour que chaque nom d'utilisateur du fichier de test génère deux patterns grep — `$u@` (avec `@`, existant) et `\b$u\b` (mot entier, nouveau) — bloquant les deux formes dans les diffs stagés
- **`bot/live_bot.py` — protection contre les trades fantômes dans `enter_live_trade()`** : en mode live (`session + private_key`), si `post_order` retourne `None` (échec de l'API CLOB), la fonction retourne désormais prématurément après incrémentation de `api_fail_streak`, empêchant la création d'une ligne fantôme avec `order_id=NULL` qui bloquerait définitivement le capital ; le mode simulation (sans `private_key`) n'est pas affecté — l'INSERT continue de s'exécuter avec `oid=None` ; trois tests de non-régression ajoutés dans `TestEnterLiveTrade`
- **`requirements.txt` — `bcrypt` ajouté** : `bcrypt` était absent de `requirements.txt` ; une installation fraîche laissait `bot/bot_utils.py` se rabattre sur SHA-1 non salé pour le mot de passe de la page de statut web ; `bcrypt` est désormais une dépendance déclarée, installée automatiquement par `pip install -r requirements.txt`
- **`scripts/start_bot.sh`, `scripts/start_feed.sh`, `scripts/start_account.sh`, `scripts/start_indicators.sh`, `scripts/update_standalone.sh`, `scripts/collect_db.sh` — approche fichier PID pour tous les stop/start/restart** : cause racine : `pkill -f 'pattern'` intégré dans une commande SSH tue le shell distant lui-même — le `/proc/PID/cmdline` du shell contient le texte complet du script passé via `ssh "..."`, qui inclut le nom littéral du processus de la ligne `nohup` ; nouveau motif : le démarrage écrit `_pid=$!` → `disown "$_pid"` → `echo "$_pid" > live.pid` ; l'arrêt utilise `kill $(cat live.pid)` — sans regex, sans correspondance de motif, sans risque d'auto-correspondance ; la vérification de vivacité utilise `kill -0 $(cat live.pid)` ; les fichiers PID périmés (processus mort) sont nettoyés automatiquement au prochain démarrage ; fichiers PID : `live.pid`, `feed.pid`, `account.pid`, `indicators.pid`
- **`scripts/test_standalone_deploy.sh`, `scripts/test_multibot_deploy.sh` — scripts de test mis à jour pour l'arrêt par fichier PID** : les commandes de lancement écrivent désormais `indicators.pid` et `account.pid` immédiatement après nohup ; le nettoyage et le teardown utilisent les fichiers PID pour un arrêt gracieux ; `pkill -9` conservé en dernier recours pour les sessions kill-only
- **`scripts/test_multibot_deploy.sh`, `scripts/test_all_accounts.sh` — portée utilisateur des `pkill`** : ajout de `-u $(id -u)` à tous les appels `pkill` restants pour limiter les kills à l'utilisateur courant et éviter d'interrompre les processus d'autres utilisateurs
- **`scripts/start_bot.sh`, `scripts/start_feed.sh`, `scripts/start_account.sh`, `scripts/start_indicators.sh` — daemonisation `nohup` renforcée** : ajout de `</dev/null` sur toutes les lignes `nohup` pour que la session SSH puisse se terminer proprement ; `disown` ajouté là où il manquait
- **`bot/api_polymarket.py`, `bot/feed.py`, `bot/bot_utils.py`, `bot/api_mexc.py` — code en anglais uniquement** : messages de log en français traduits en anglais dans 4 fichiers (19 chaînes au total) ; `api_mexc.py` — 13 occurrences de `erreur` → `error` ; `bot/api_polymarket.py:271` — le warning CLOB logue désormais `keys=%s` au lieu du corps complet de la réponse
- **`bot/api_binance.py`, `bot/strategies/grid.py` — code en anglais uniquement** : messages de log restants en français traduits en anglais
- **`bot/api_binance.py` — compatibilité `get_markets(**_)`** : ajout de `**kwargs` pour accepter les arguments nommés spécifiques à Polymarket (`tag_id`, `window_minutes`) transmis par `_run_ws` quel que soit le type de connecteur
- **`scripts/collect_db.sh` — correction syntaxe `--rotate`** : erreur de syntaxe préexistante où `"\$(id -u)"` dans une chaîne SSH entre guillemets doubles fermait la chaîne externe prématurément ; corrigé en `\$(id -u)`

### Tests
- **`tests/test_bot.py` — 30 nouveaux tests** : `TestComputeStake` (11 cas couvrant la mise proportionnelle au bid, le plafond de capital, la pénalité de secondes, le plancher/plafond) ; `TestWeeklyStopLoss` (3 cas) ; `TestMarketDiscoveryConfig` (5 cas) ; `TestPurgeExpiredMarkets` (5 cas — suppression de token expiré, conservation des actifs, garde open-trade, signal effacé à la purge, tokens mixtes) ; `TestWsLoopBackoff` (3 cas — doublement, plafond à 60 s, remise à zéro après succès) ; `TestMarketRefreshLoop` (3 cas — enregistrement et abonnement de nouveaux marchés, purge des expirés, résilience aux erreurs API)
- **`tests/test_grid_trail.py` — 12 nouveaux tests** : `TestConnectorValidate` (4 cas) ; `TestCheckStopLoss` (3 cas) ; `TestRecenterGrid` (2 cas) ; `TestRestoreFromDb` (3 cas)
- **Suite complète : 659 tests passent**

---

## [0.4.5] - 2026-05-16

### Correction
- **`scripts/backtest.py` — saut gracieux des BDs klines** (`--all`, `--sweep-all`) : quand le répertoire de données contient des fichiers klines BTCUSDT (sans table `snapshots`), le script plantait avec `OperationalError: no such table: snapshots` ; affiche désormais un message de saut et continue vers la BD suivante
- **`scripts/profile_compare.py` — `bot.DB_PATH` supprimé** (E1101) : `DB_PATH` n'existe plus comme constante module-level dans `live_bot.py` ; remplacé par `os.path.join(PROFILE_DIR, "profile.db")`
- **`scripts/profile_hotpath.py` — argument `config` manquant** (E1120 ×2) : `bot.init_db()` et `bot.is_trading_hour()` requièrent tous deux un argument `BotConfig` depuis le refactor piloté par config ; appels mis à jour en `init_db(BotConfig())` et `is_trading_hour(BotConfig())` ; lambda redondant sur `is_trading_hour` résolu en parallèle
- **`tests/test_api_cex.py` — imports inutilisés** (W0611) : `importlib` et `inspect` supprimés
- **`tests/test_indicators.py` — import inutilisé `math`** (W0611) ; reimport de `_shift_addr` dans une méthode remplacé par le nom module-level (W0404) ; variable `spec` inutilisée → `_` (W0612) ; deux appels `NamedTemporaryFile` supprimés pour R1732
- **`tests/test_multibot.py` — reimport `os as _os`** (W0404/C0411/C0412) : remplacé par `os` module-level
- **`tests/test_bot.py` — multiples avertissements pylint** : `too-many-lines` supprimé en tête de module (C0302) ; trois `import time as _time` locaux remplacés par `time` module-level (W0404) ; `import asyncio` local dans une méthode supprimé (W0404/W0621) ; trois variables `ts` inutilisées remplacées par `_` (W0612) ; quatre `from datetime import datetime, timezone` locaux supprimés (W0404/W0621 ×4) ; imports inutilisés `GridLevel`, `GridState` supprimés (W0611) ; `import asyncio, unittest.mock` tardif supprimé (C0411/C0412) ; `TestStrategyLoading` mis à jour — tests v2 remplacés par assertions sur les paramètres v1 après suppression de `polymarket_BTC5M_v2.json`

### Modifié
- **`strategies/polymarket_BTC5M.json` — `signal_threshold` 0.96 → 0.95** : optimisation sweep-all sur 6 BDs (1 016 186 snapshots, 405 combos) ; meilleur ratio PnL/MaxDD 4.21 à thr=0.95/secs=45/obi=−0.75/dsl=30
- **`strategies/grid_BTCUSDT.json` — bornes de grille ±10% → ±30%** : `grid_lower`/`grid_upper` mis à jour à 70k–130k autour d'un midpoint 100k ; meilleur Calmar moyen sur 3 BDs klines (2026 range, 2022 bear, 2024 bull)

### Supprimé
- **`strategies/polymarket_BTC5M_v2.json`** : supprimé — après la mise à jour du seuil, v1 et v2 étaient fonctionnellement identiques ; `polymarket_BTC5M.json` est le seul fichier de stratégie actif

### Documentation
- **`QUICKSTART.md` + `QUICKSTART.fr.md`** : référence de version mise à jour `v0.40` → `v0.4.4` ; référence à `scripts/install_service.sh` obsolète remplacée par un lien vers la section systemd d'INSTALL.md ; `pkill -f live_bot.py` → `pkill -f '[l]ive_bot.py'` (bracket trick)
- **`INSTALL.md` + `INSTALL.fr.md`** : flag `--detail` ajouté au tableau des paramètres de `backtest.py` ; sous-section "Flags du feed" ajoutée avec `--verbose` pour `feed.py` (distinct de l'entrée `account_bot.py` existante)

### Divers
- **`.gitignore`** : `.coverage` ajouté

---

## [0.4.4] - 2026-05-16

### Ajout
- **Architecture d'indicateurs partagée** (`bot/indicators.py`, `bot/account_bot.py`) : `indicators.py` est désormais un processus unique par machine, démarré une seule fois sous l'utilisateur du feed ; chaque `account_bot` enregistre ses streams souhaités au démarrage via un socket ZMQ REP (`tcp://127.0.0.1:5561`) et reçoit les messages d'indicateurs sur le socket PUB partagé (`:5559`) ; remplace l'ancien modèle par processus par compte qui causait des conflits de port
- **Enregistrement dynamique des streams** (`bot/indicators.py` `--reg-addr`) : nouveau socket ZMQ REP bind `:5561` acceptant des requêtes JSON `{"streams": [...]}` des bots comptes au démarrage ; `indicators.py` n'active que l'union des streams enregistrés, éliminant les connexions Binance WebSocket inactives
- **Support de `feed_auto_start=false`** (`bot/account_bot.py`, `bot/bot_utils.py`) : quand `config.json` définit `"feed_auto_start": false`, `account_bot` sonde le feed avec une boucle de retry (6 tentatives × 5 s = 30 s max) au lieu de fork `feed.py` ; requis pour les déploiements gérés par systemd où `tradinebotte-feed.service` possède le processus feed
- **Templates de services systemd** — trois nouveaux scripts d'installation et templates d'unités :
  - `scripts/install_feed_service.sh` + `scripts/tradinebotte-feed.service` : installe le feed partagé en service système (`After=network-online.target`) ; détecte une unité déjà active/activée avant d'écraser
  - `scripts/install_indicators_service.sh` + `scripts/tradinebotte-indicators.service` : installe le processus d'indicateurs partagé en service utilisateur (`Wants=tradinebotte-feed.service`)
  - `scripts/install_account_service.sh` + `scripts/tradinebotte-account.service` : unité bot compte avec `Requires=tradinebotte-feed.service`, `Wants=tradinebotte-indicators.service` ; valide `feed_auto_start=false` dans `config.json` avant l'installation
- **`EnvironmentFile=-<credentials>`** dans `tradinebotte-feed.service` et `tradinebotte.service` : les unités systemd chargent désormais un fichier `credentials` optionnel depuis le répertoire d'installation pour l'injection des clés API, évitant les secrets dans le fichier d'unité
- **`scripts/test_multibot_deploy.sh` — phase indicateurs partagés** : la Phase 7 restructurée démarre un seul processus `indicators.py` sous l'utilisateur feed (pas un par compte) ; `TEST_INDICATORS_CONFIG` scalaire remplace l'ancien tableau `TEST_INDICATORS_CONFIGS` par compte ; la Phase 9 vérifie le processus unique ; la Phase 12 le stoppe proprement sans toucher aux ports du feed

### Modifié
- **`scripts/start_collector.sh` — unité transitoire `systemd-run --user`** : `nohup ... &` remplacé par `systemd-run --user --description=... --setenv=...` pour survivre à la déconnexion SSH sur les hôtes avec `KillUserProcesses=yes` (`loginctl enable-linger` requis une fois par utilisateur) ; la vérification de vivacité déplacée dans un appel SSH direct séparé après 15 s
- **`scripts/tradinebotte-feed.service` + `scripts/tradinebotte.service`** : `StartLimitIntervalSec` et `StartLimitBurst` déplacés de `[Service]` vers `[Unit]` (section systemd correcte) ; `EnvironmentFile=-__ENV_FILE__` ajouté

### Correction
- **Chaînes françaises restantes dans les scripts shell** (`scripts/install.sh`, `scripts/run_tests.sh`, `scripts/setup.py`, `scripts/start_bot.sh`) : commentaires d'en-tête et messages echo traduits en anglais ; docstring bilingue de `setup.py` réduite à l'anglais uniquement
- **Assertion de test en français obsolète** (`tests/test_bot.py`) : `assertIn("Aucun trade", ...)` mis à jour en `assertIn("No resolved trades", ...)` après la migration de `generate_status_html()` vers l'anglais en 0.4.3
- **Pylint 10.00/10** sur tous les fichiers scripts : `scripts/backtest_volfilter.py` (import `Optional` inutilisé, réimport redondant de `datetime`, f-strings sans interpolation), `scripts/download_btc_history.py` (import `sys` inutilisé, variable `total_expected` inutilisée, nom interdit `bar` → `progress`, f-strings sans interpolation), `scripts/backtest_grid.py` (`if/assign` → `max()`, variable `trail_label` inutilisée, f-string sans interpolation), `scripts/backtest.py` (f-string sans interpolation, `best_s` inutilisé → `_`), `scripts/profile_compare.py` (f-strings sans interpolation, faux positif `import-error` supprimé), `bot/account_bot.py` (`global-statement` supprimé inline)
- **`scripts/test_multibot_deploy.sh` — bugs de conflit de port** : suppression du `fuser -k 5557/tcp` erroné dans la boucle de teardown des comptes (aurait tué le service feed) ; suppression du tableau `INDICATORS_CONFIGS` mort qui était renseigné mais jamais utilisé après la restructuration de la Phase 7

### Documentation
- **`docs/design.md` + `docs/design.fr.md`** : inventaire des processus mis à jour avec le socket REP d'`indicators.py` (`:5561`) ; sous-section `feed_auto_start=false` ajoutée avec diagramme ASCII de la boucle de retry ; section ordre de démarrage mise à jour pour les indicateurs partagés ; table des scopes des variables d'env corrigée (`TRADINEBOTTE_INDICATORS_ADDR` et `TRADINEBOTTE_INDICATORS_REG_ADDR` utilisées par `indicators.py` et `account_bot.py`)
- **`docs/multi.md` + `docs/multi.fr.md`** : tableau des variables d'env enrichi de `TRADINEBOTTE_INDICATORS_ADDR` et `TRADINEBOTTE_INDICATORS_REG_ADDR` ; tableau des clés multi-bot `config.json` par compte ajouté (`feed_addr`, `feed_auto_start`, `indicators_reg_addr`, `indicators_streams`) ; séquence de lancement divisée en sous-sections manuelle et systemd ; tableau des services systemd étendu à 3 lignes incluant `install_indicators_service.sh`
- **`INSTALL.md` + `INSTALL.fr.md`** : tableau de référence complet des variables d'env ajouté ; note sur l'héritage d'environnement systemd avec exemple de fichier credentials ; section architecture indicateurs partagés remplaçant l'ancien modèle par compte ; flags `--config FILE` et `--reg-addr ADDR` documentés

---

## [0.4.3] - 2026-05-09

### Correction
- **Balayage de la politique anglais-uniquement — scripts et fichiers bot** (`bot/feed.py`, `bot/bot_utils.py`, `bot/strategies/grid.py`, `bot/strategies/__init__.py`, `scripts/start_feed.sh`, `scripts/start_account.sh`, `scripts/start_indicators.sh`, `scripts/run_integration_tests.sh`, `scripts/benchmark_api.py`, `scripts/profile_hotpath.py`, `scripts/strategy_compare.sh`, `scripts/backtest_volfilter.py`, `scripts/test_multibot_deploy.sh`, `scripts/test_standalone_deploy.sh`) : toutes les chaînes françaises restantes dans le code source, les messages de log, les commentaires, les sorties d'erreur et les en-têtes de scripts ont été traduits en anglais ; les seules exceptions — seconds arguments des helpers shell bilingues `_t()` et valeurs `"FR":` dans `setup.py` — sont intentionnelles et ont été préservées ; cela complète l'application rétroactive de la politique linguistique introduite en 0.4.2
- **Bracket trick `pgrep`/`pkill` étendu aux processus feed, account et indicators** (`scripts/start_feed.sh`, `scripts/start_account.sh`, `scripts/start_indicators.sh`) : les patterns `[f]eed.py`, `[a]ccount_bot.py`, `[i]ndicators.py` empêchent la session SSH exécutant le script de se correspondre via `pgrep -f`

---


## [0.4.2] - 2026-05-09

### Modifié
- **Politique linguistique appliquée dans les quatre modules du bot** (`bot/live_bot.py`, `bot/feed.py`, `bot/indicators.py`, `bot/account_bot.py`) : tous les messages de log, commentaires et docstrings en français ont été traduits en anglais ; `CLAUDE.md` a été mis à jour avec une section obligatoire "Language policy" codifiant que le code source (`.py`, `.sh`, `.json`), les commentaires, les messages de log et les docstrings doivent être exclusivement en anglais ; les fichiers de documentation (`*.fr.md`) restent le seul endroit où le français a sa place ; cette règle a été appliquée rétroactivement sur les quatre modules

### Ajout
- **`bot/live_bot.py` — refonte du système de logs** : `TimedRotatingFileHandler` (rotation à minuit, conservation 30 jours) remplace l'ancien `FileHandler`, évitant une croissance illimitée du fichier de log sur les déploiements longue durée ; `_SESSION_ID` (fragment UUID hexadécimal majuscule de 8 caractères) est injecté dans chaque ligne de log via `_SessionFilter`, permettant de grepper d'un coup toutes les lignes d'une durée de vie du bot ; le format de log inclut désormais `%(session)s` — chaque ligne porte l'identifiant de session ; le dataclass `RejectionStats` (13 champs, un par raison de sortie anticipée de `check_signal()`) est loggé toutes les 60 secondes sous forme de ligne `[REJECTIONS]` puis remis à zéro, facilitant le diagnostic quand le bot envoie moins de trades que prévu ; la ligne de log `[LATENCY]` est enrichie d'un champ `ts_ms=` portant l'horodatage du message WebSocket en millisecondes
- **Audit des docstrings — 14 fonctions/méthodes dans 6 modules** : toutes les fonctions et méthodes publiques sans docstring ont été complétées ; fichiers concernés : `bot/api_binance.py`, `bot/api_mexc.py`, `bot/feed.py`, `bot/indicators.py`, `bot/strategies/__init__.py`, `bot/strategies/grid.py`
- **Suite de tests étendue — 27 nouveaux tests dans 5 nouvelles classes** (`tests/test_bot.py`) : `TestSessionFilter` (4 tests — le filtre attache l'attribut `session`, `filter()` retourne `True`, `session` correspond à `_SESSION_ID`, valeur en hexadécimal majuscule de 8 caractères) ; `TestRejectionStats` (3 tests — les 13 champs sont à zéro à l'initialisation, indépendance entre instances, `BotState` initialise un `RejectionStats` vierge) ; `TestRejectionCounters` (13 tests — un par raison de rejet, plus un test d'absence de compteur sur déclenchement et un test de remise à zéro périodique) ; `TestLatencyLog` (1 test — la ligne `[LATENCY]` contient `ts_ms=`) ; `TestLogFormatters` (6 tests — `_PlainFmt` abrège `INFO`→`INFO ` et `WARNING`→`WARN `, `_ColorFmt` ajoute les codes d'échappement ANSI, le token `%(session)s` est présent dans la chaîne de format)

### Correction
- **Bogue de correspondance involontaire de `pgrep`/`pkill` corrigé dans 7 scripts** (`scripts/start_bot.sh`, `scripts/start_collector.sh`, `scripts/collect_db.sh`, `scripts/monitor.sh`, `scripts/test_all_accounts.sh`, `scripts/test_multibot_deploy.sh`, `scripts/test_standalone_deploy.sh`) : tous les patterns `pgrep -f live_bot.py` / `pkill -f live_bot.py` ont été remplacés par la variante avec crochet `[l]ive_bot\.py` ; le pattern sans crochet correspond à la session SSH qui exécute la commande, tuant le processus SSH lui-même plutôt que le bot ; les patterns `grep` en français obsolètes dans les scripts de test ont également été mis à jour pour correspondre aux nouveaux messages de log en anglais : `'Connecte au feed'` → `'Connected to feed'`, `'WebSocket connecte'` → `'WebSocket connected'`, `'Souscription'` → `'Subscribing'`, `'Marches BTC'` → `'BTC 5-min markets'`

### Sécurité
- **Purge de l'historique git** via `git filter-repo --replace-text` + `--message-callback` : le nom d'hôte du serveur et les trois mots de passe des comptes de déploiement présents dans d'anciens commits ont été supprimés de tous les blobs et messages de commit ; mots de passe renouvelés
- **`bot/bot_utils.py` — SHA-1 → bcrypt pour htpasswd** (`_htpasswd_sha1` → `_htpasswd`) : le SHA-1 sans sel (`{SHA}`) remplacé par bcrypt (`$2y$`, natif Apache 2.4+) ; `bcrypt` ajouté à `requirements.txt` ; repli avec avertissement si la bibliothèque est absente ; suite `TestHtpasswd` mise à jour en conséquence
- **`bot/bot_utils.py` — correction XSS dans la page de statut web** (`html.escape`) : le champ `question` des marchés, fourni par l'API Polymarket externe, était inséré brut dans les attributs et le contenu HTML ; désormais échappé avec `html.escape()` avant toute interpolation
- **`bot/account_bot.py` — lock file protégé contre les symlinks** (`O_NOFOLLOW`) : `open()` remplacé par `os.open(O_CREAT|O_WRONLY|O_NOFOLLOW, 0o600)` + `os.fdopen()` pour empêcher un attaquant local de placer un symlink au chemin du lock et de faire tronquer un fichier arbitraire par le bot
- **`bot/account_bot.py` — environnement minimal pour le sous-processus** `feed.py` : le processus enfant n'hérite plus de l'environnement complet du parent (incluant `POLY_PRIVATE_KEY`) ; seuls `PATH`, `HOME`, `LANG`, `VIRTUAL_ENV`, `PYTHONPATH`, `LC_ALL`, `LC_CTYPE` et `TRADINEBOTTE_FEED_ADDR` sont transmis
- **Scripts shell — suppression de `eval echo`** (injection de commande) : `INSTALL_DIR="$(eval echo "$INSTALL_DIR")"` remplacé par `INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"` dans les 7 occurrences de `start_bot.sh`, `monitor.sh`, `start_account.sh`, `start_feed.sh`, `install_account_service.sh` (×2) et `install_feed_service.sh`
- **Scripts shell — injection de chemin dans le Python inline corrigée** : `$CONFIG` était interpolé dans une chaîne `-c "..."` entre guillemets ; remplacé par du code entre apostrophes + `sys.argv[1]` dans `start_bot.sh` et `monitor.sh`
- **Scripts de déploiement — suppression de `sshpass -p`** (mot de passe visible dans `ps aux`) : remplacé par `SSHPASS="$pwd" sshpass -e` dans les trois scripts de déploiement (`test_all_accounts.sh`, `test_multibot_deploy.sh`, `test_standalone_deploy.sh`)
- **Scripts de déploiement — `StrictHostKeyChecking=no` → `accept-new`** : l'acceptation aveugle des clés SSH remplacée par `accept-new` (confiance au premier contact, rejet si la clé change) dans les 3 scripts de déploiement

### Modifié
- **`SNAPSHOT_INTERVAL` par défaut passé de 5s à 1s** : tous les bots écrivent désormais une ligne de snapshot par seconde par défaut, éliminant l'angle mort responsable de ~50 LOSS supplémentaires par session de 3 mois dans les backtests alignés ; utiliser `--snapshot-interval N` pour réduire les I/O disque si nécessaire

### Ajout
- **`strategies/indicators_4h_bitcoin.json`** + **`strategies/indicators_1d_bitcoin.json`** : configs d'indicateurs par compte séparées de `indicators.json` ; account-a reçoit btc_4h sur le port 5559, account-b reçoit btc_1d sur le port 5560
- **`scripts/start_indicators.sh`** : script de démarrage du service d'indicateurs ; le compte sélectionne son fichier via `TRADINEBOTTE_INDICATORS_CONFIG`
- **`tests/test_indicators.py`** : ajout de `TestSplitConfigs` (6 tests, 57→63 au total) vérifiant l'isolation des ports, la disjonction des stream_id et le contenu correct de chaque fichier séparé
- **`scripts/test_multibot_deploy.sh`** : phase 3b démarre `indicators.py` par compte avant les bots ; les phases de nettoyage tuent `indicators.py` et libèrent les ports 5559/5560
- **`scripts/test_multibot.conf.example`** : ajout du tableau `TEST_INDICATORS_CONFIGS` pour associer chaque compte à sa config d'indicateurs
- **`strategies/indicators.json`** : ajout du flux `btc_1d` (RSI 14 + volatilité 20 sur chandeliers quotidiens) aux côtés de `btc_4h` ; un seul processus `indicators.py` sert désormais plusieurs bots avec des besoins temporels différents
- **`tests/test_indicators.py`** : ajout de `TestMultiBotIndicatorSharing` (5 tests, 52→57 au total) vérifiant la diffusion PUB/SUB ZeroMQ 1→N et le filtrage applicatif par `stream_id` ; sockets ZMQ synchrones avec `RCVTIMEO=500ms`
- **`strategies/indicators.json`** — fichier de configuration JSON pour le service d'indicateurs : définit une liste de streams nommés, chacun spécifiant `asset`, `source` (`"binance_ws"` ou `"feed"`), `timeframe`, `seed_periods`, et une liste d'entrées d'indicateurs `{type, period}` ; config par défaut : RSI(14) et volatilité(20) sur les bougies 4h BTC/USDT depuis Binance (`source: "binance_ws"`, `timeframe: "4h"`, `seed_periods: 50`) ; `indicators.py` est étendu avec un nouveau flag `--config FILE` qui charge ce fichier ; `IndicatorSpec.from_dict()` valide le type (`rsi|sma|ema|volatility`) et la période (≥2) ; `StreamSpec.from_dict()` valide la source (`binance_ws|feed`), parse la liste d'indicateurs ; `load_config()` retourne `(feed_addr, out_addr, min_ticks, streams)` — les variables d'env priment sur les adresses du fichier config ; `PriceSeries.compute_indicators(specs)` nouvelle méthode aux côtés de la méthode legacy `indicators()` — format de clé `<abrév>_<période>` (`vol_20` et non `volatility_20`, cohérent avec le legacy) ; `_binance_kline_task()` ouvre `wss://stream.binance.com:9443/ws/<symbol>@kline_<tf>`, amorce depuis REST (`/api/v3/klines`) au démarrage, pousse les prix de clôture des bougies fermées (`k.x=true`), reconnexion avec backoff exponentiel (5s→60s) ; `_zmq_feed_task()` refactorisé depuis le `run()` legacy — supporte les streams feed pilotés par config ; `run()` dispatche les tâches par stream depuis la config ou retombe en mode CLI legacy (rétrocompatible) ; 24 nouveaux tests dans 4 nouvelles classes (`TestIndicatorSpec`, `TestStreamSpec`, `TestLoadConfig`, `TestPriceSeriesComputeIndicators`) — 52 au total ; pylint 10.00/10
- **`docs/design.md` + `docs/design.fr.md`** — nouvelle référence d'architecture multi-processus bilingue (EN+FR) : couvre tous les modes de déploiement (Option A autonome, Option B multi-bot), tableau complet de l'inventaire des processus (live_bot / feed / account_bot / indicators — rôle, credentials, socket ZMQ), diagramme de topologie ZeroMQ montrant toutes les connexions SUB/PUB avec les numéros de port, catalogue complet des messages avec exemples JSON et tableaux de champs pour les quatre types de message (`market`, `book`, `ping`, `indicators`), mécanisme de démarrage automatique du feed (organigramme verrou POSIX, env minimal du sous-processus, nommage du verrou par hash), tableau des garanties d'isolation des processus (BD, log, clés, paramètres de stratégie, état du capital, cache PnL journalier, set signalled), fonctionnement interne du pipeline d'indicateurs (ring-buffer → RSI/SMA/EMA/vol → publication quand prêt), ordre de démarrage avec note sur le mode sans connexion ZMQ, récapitulatif des variables d'environnement
- **`bot/indicators.py` — service d'indicateurs techniques ZeroMQ** (`compute_rsi`, `compute_sma`, `compute_ema`, `compute_volatility`, `PriceSeries`) : nouveau processus autonome qui souscrit au socket PUB de feed.py (défaut `tcp://127.0.0.1:5557`) et republie des messages d'indicateurs enrichis sur un second socket PUB (défaut `tcp://127.0.0.1:5559`) ; consomme les messages `{"t":"book"}`, accumule un ring-buffer par token des prix `best_bid` (maxlen configurable, défaut 200), et émet `{"t":"indicators", "token_id":…, "ts":…, "rsi_14":…, "sma_20":…, "ema_9":…, "vol_20":…}` une fois le nombre minimal de ticks atteint et tous les indicateurs calculables ; les maths des indicateurs sont en stdlib pur (pas de numpy) : RSI selon la formule de Wilder (gain moyen / perte moyenne sur les n derniers deltas), SMA moyenne simple de la queue, EMA amorcée par la SMA puis itérée avec `k = 2/(n+1)`, volatilité = écart-type population des log-rendements ; les quatre fonctions retournent `None` en cas de données insuffisantes ; `PriceSeries` encapsule un `collections.deque` avec `push()` et `indicators(rsi_n, sma_n, ema_n, vol_n)` pour la testabilité ; CLI : `--feed ADDR` (cible SUB), `--out ADDR` (bind PUB), `--rsi N` (défaut 14), `--sma N` (défaut 20), `--ema N` (défaut 9), `--vol N` (défaut 20), `--min-ticks N` (défaut 25), `--verbose` ; surcharge des adresses via les variables d'env `TRADINEBOTTE_FEED_ADDR` et `TRADINEBOTTE_INDICATORS_ADDR` ; pylint 10.00/10 ; 28 nouveaux tests dans `tests/test_indicators.py` couvrant tous les cas limites : données insuffisantes (retours None), série constante (SMA/EMA = valeur, vol = 0), RSI tous gains (100), RSI toutes pertes (0), formule du facteur k EMA, volatilité sur prix nuls (None), comportement ring-buffer maxlen PriceSeries, périodes personnalisées, test d'intégration sma_value_correct
- **Documentation — `backtest_grid.py` et `download_btc_history.py` ajoutés dans README et INSTALL** (EN + FR) : les deux scripts étaient entièrement absents des quatre fichiers de documentation utilisateur principaux (`README.md`, `README.fr.md`, `INSTALL.md`, `INSTALL.fr.md`) ; `README` reçoit un bullet **Grid trading** dans la section Features et une section **Backtest grid trading** avec les commandes de démarrage rapide ; `INSTALL` reçoit une section complète **Backtest grid trading** avec les 10 flags de `backtest_grid.py` documentés, les 6 flags de `download_btc_history.py`, des tableaux de paramètres complets et des liens vers `docs/AdaptedGridTrading.fr.md`
- **`docs/AdaptedGridTrading.md` + `docs/AdaptedGridTrading.fr.md`** — nouvelle documentation bilingue (EN+FR) couvrant les trois stratégies grid (statique, bear trailing, bull trailing) : concept et justification de chaque stratégie, fonctionnement détaillé du mécanisme de trailing avec des exemples de prix réels (séquence de recentrages crash 2022, séquence bull run 2024), tableaux de paramètres complets, explication de l'asymétrie (mode bear s'arrête à exit_high ; mode bull s'arrête à exit_low), gestion du capital au recentrage, section AVERTISSEMENT sur le danger de `trail=both` en marché directionnel (−23,9% vs +2,0% sur bear 2022), tableau de comparaison complet (3 stratégies × 3 régimes), tableaux de sweep de paramètres pour chaque mode, arbre de décision pour la sélection de stratégie, exemples CLI pour reproduire n'importe quel résultat, index des fichiers liés
- **`scripts/backtest_grid.py` — stratégies trailing grid (bear-adapté + bull-adapté)** : moteur étendu avec `--trail bear|bull|both|off`, `--max-recenters N` (défaut 10), `--compare` (comparaison statique vs trailing côte à côte par DB) ; mécanisme de trailing : quand le prix sort de `[grid_lower, grid_upper]`, au lieu de stopper le bot recentre la grille sur le prix de clôture actuel — mode `bear` : recentre uniquement vers le bas (suit le prix en descente, ignore les sorties vers le haut), mode `bull` : uniquement vers le haut, mode `both` : dans les deux directions (dangereux en marché directionnel, voir ci-dessous) ; nouvelles métriques : `realized_pnl` (cycles complétés nets de frais), `unrealized_pnl` (coût base BTC vs prix actuel), compteur `recenters`, liste `recenter_prices` ; au recentrage, les nouveaux ordres BUY sont alloués depuis le budget USDT restant (évite le surexposé après pertes accumulées) ; résultats (±15%/30L vs statique) : **bear trailing sur crash 2022** +2,0% vs −3,3% statique (102 cycles vs 18, 2 recentrages à $32K/$27K, sortie rentable sur le rebond), **bull trailing sur bull run 2024** +3,7% vs +0,1% statique (134 cycles vs 5, 3 recentrages à $76K/$87K/$101K, couverture complète 92j) ; les modes bear et bull sont asymétriques : `trail=bear` dans un bull run = identique au statique (stoppé à exit_high) ; `trail=bull` dans un bear market = identique au statique (stoppé à exit_low) ; **AVERTISSEMENT** : `trail=both` en marché bear directionnel = −23,9% de perte (9 recentrages oscillant haut/bas, accumulation de $409 de perte latente BTC) — utiliser `both` uniquement en conditions de range confirmé ; sweep (mode bear) : meilleur Calmar moyen ±30%/20L, meilleur PnL moyen ±15%/30L (+2,5%) ; sweep (mode bull) : meilleur Calmar moyen ±20%/20L, meilleur PnL moyen ±15%/30L (+1,8%) ; CLI : `--all`, `--range`, `--levels`, `--size`, `--fee`, `--trail`, `--max-recenters`, `--compare`, `--sweep`, `--sort calmar|pnl`
- **`scripts/backtest_grid.py`** — (version initiale) moteur de backtest de la stratégie grid sur des bases SQLite OHLCV ; modèle de fill : contact de prix sur la plage `[low, high]` de chaque bougie — ordres BUY limit placés aux niveaux sous le prix de départ, SELL placé un step au-dessus après chaque fill BUY, BUY replacé un step en dessous après chaque fill SELL ; capital = `n_levels × order_size` (pire cas : tous les niveaux simultanément remplis) ; stop-loss quand `low < grid_lower` ou `high > grid_upper` — BTC restant liquidé au close ; métriques : PnL net, PnL%, annualisé%, PnL brut, frais, drawdown max, ratio Calmar (PnL%/MaxDD%), temps-dans-la-grille% ; CLI : `--all` (tous les `BTCUSDT_1m*.db` dans `data/`), `--range` (±% depuis le prix de départ, défaut 15), `--levels` (défaut 30), `--size` (USDT/ordre, défaut 50), `--fee`, `--sweep` (balayage range_pct × levels : 5×3=15 combinaisons), `--sort calmar|pnl` ; résultats du sweep (3 bases × 15 combos) : la grille statique performe le mieux en marché latéral (+5%/90j avec ±15%/30 niveaux) ; les tendances bear/bull forcent une sortie dans 10–47% de la période, limitant la perte à 3–5% du capital grâce au stop-loss précoce ; meilleur Calmar toutes conditions : ±30%/20 niveaux (survie totale en marché latéral, perte bear contenue) ; meilleur PnL absolu en condition latérale : ±15%/30 niveaux
- **`strategies/grid_BTCUSDT_bear_trailing.json`** — stratégie bear-adaptée : ±15%, 30 niveaux, $50/ordre ; trail=bear (recentrage vers le bas uniquement) ; calibrée BTC=$80 705 : grille [$68 599, $92 811] ; backtest : +5,0% latéral, +2,0% bear 2022 (2 recentrages à $32K/$27K, sortie sur rebond), +0,1% bull (identique au statique) ; à utiliser quand une poursuite de la baisse est attendue
- **`strategies/grid_BTCUSDT_bull_trailing.json`** — stratégie bull-adaptée : mêmes paramètres de grille ; trail=bull (recentrage vers le haut uniquement) ; backtest : +5,0% latéral (1 recentrage, 100% temps dans la grille), +3,7% bull run (3 recentrages à $76K/$87K/$101K, couverture 92j complète), −3,3% bear (identique au statique) ; à utiliser quand une poursuite de la hausse est attendue
- **`strategies/grid_BTCUSDT_moderate.json`** — stratégie backtestée : ±20%, 30 niveaux, $50/ordre, capital $1 500 ; calibrée à BTC=$80 705 (2026-05-09) : grille [$64 564, $96 846], step $1 113 ; résultats backtest : +3,9%/90j (+16%/an) marché latéral 2026 (100% temps dans la grille), −4,6% de perte bear 2022 (stop après 10% de la période), +0,2% bull run 2024 (stop après 28%) ; recommandée pour des conditions de marché générales ou incertaines
- **`strategies/grid_BTCUSDT_tight.json`** — stratégie backtestée : ±15%, 30 niveaux, $50/ordre, capital $1 500 ; calibrée à BTC=$80 705 : grille [$68 599, $92 811], step $829 ; résultats backtest : +5,0%/90j (+20%/an) marché latéral 2026 (96% temps dans la grille), −3,3% de perte bear 2022 (stop après 9%), +0,1% bull run 2024 (stop après 25%) ; recommandée pour des conditions de marché en range/consolidation attendues — le step plus serré génère davantage de cycles par jour
- **Bases de données OHLCV historiques — trois régimes de marché** (`data/`) : trois bases de données BTCUSDT à la minute assemblées pour couvrir des régimes de volatilité distincts lors du backtest grid — (1) `BTCUSDT_1m90d_range_20260208-20260509.db` : marché latéral actuel (129 600 bougies, plage $63K–$83K, amplitude moyenne par bougie $48,5) ; (2) `BTCUSDT_1m92d_bullrun20241015-20250115.db` : bull run oct 2024 – jan 2025 (132 481 bougies, $64 800 → ATH $108 353, amplitude moyenne $66,9) ; (3) `BTCUSDT_1m92d_bearmarket20220501-20220801.db` : bear market mai – août 2022, effondrement LUNA (132 481 bougies, BTC de ~$38K à ~$17K, amplitude moyenne la plus élevée des trois — période de volatilité extrême) ; tous les fichiers sont exclus du git (`.gitignore`), regénérer avec `python scripts/download_btc_history.py --start YYYY-MM-DD --end YYYY-MM-DD`
- **`scripts/download_btc_history.py`** — télécharge l'historique OHLCV (bougies) depuis l'API publique Binance ; flags `--start YYYY-MM-DD` / `--end YYYY-MM-DD` ajoutés pour cibler des périodes historiques (ex. bullmarket) ; le nom de fichier encode désormais la plage de dates réelle (`BTCUSDT_1m92d_range_20241015-20250115.db`) ; `--days` est utilisé comme fallback quand `--start` est absent ; `BTCUSDT_1m92d_range_20241015-20250115.db` téléchargé : 132 481 bougies couvrant le bull run oct 2024 – jan 2025 ($64 800 → ATH $108 353) ; original (aucun credential requis) dans une base SQLite dans `data/` ; schéma : `klines(ts_ms PK, open, high, low, close, volume, close_ms)` + table `meta` stockant le symbole, l'intervalle et l'horodatage de téléchargement ; options : `--symbol`, `--interval` (1m/5m/15m/1h/…), `--days`, `--out` ; reprend depuis la dernière bougie enregistrée en cas de relance ; barre de progression avec estimation ; limité à ~8 req/s (limite Binance : 1200 weight/min, poids 2 par requête klines) ; 90 jours de données BTCUSDT à la minute téléchargés en ~59s → 129 600 lignes, 10,2 Mo ; la DB de sortie est exclue du git (`.gitignore`) — regénérer avec `python scripts/download_btc_history.py` ; destiné au backtest de grid trading où les fills sont détectés par contact de prix sur la plage `[low, high]` de chaque bougie
- **Grid trading — WebSocket user data stream** (`bot/strategies/grid.py`, `bot/api_binance.py`, `bot/api_mexc.py`) : les notifications de fill en temps réel remplacent le polling REST dès que le stream est connecté ; `get_listen_key(session)` crée une clé user data stream (TTL 60 min) via `POST /api/v3/userDataStream` ; `keepalive_listen_key(session, listen_key)` renouvelle la TTL toutes les 30 min via `PUT /api/v3/userDataStream` ; `make_user_stream_url(listen_key)` retourne l'URL WebSocket spécifique au connecteur (`wss://stream.binance.com:9443/ws/<key>` pour Binance, `wss://wbs.mexc.com/ws?listenKey=<key>` pour MEXC) ; `parse_user_stream_msg(msg)` extrait les événements de fill — Binance utilise `"e":"executionReport"` avec le statut string `"X"`, MEXC utilise un dict imbriqué `"d"` avec statut numérique (2=FILLED, 3=PARTIALLY_FILLED) et côté numérique (1=BUY, 2=SELL) ; `GridStrategy._user_stream_loop(state)` s'exécute comme tâche asyncio en background démarrée après l'initialisation de la grille, gère le cycle de vie de la listenKey et se reconnecte avec backoff exponentiel (5 s → 60 s max), s'arrête après 3 échecs consécutifs d'obtention de clé (pas de credentials) ; `_on_user_stream_fill(state, fill)` fait correspondre l'`order_id` au niveau actif de la grille et dispatche vers `_on_buy_filled`/`_on_sell_filled` ; quand le stream est connecté (`_user_ws_connected=True`), le polling REST est ignoré — il devient un fallback pour la période déconnectée uniquement ; la tâche stream est annulée proprement lors d'un stop-loss ; le mode simulation (IDs `sim_`) ne démarre jamais le stream ; 14 nouveaux tests dans `TestUserDataStream` couvrant les fonctions parse Binance/MEXC, la génération d'URL, le dispatch de fill et le no-op pour ordre inconnu (278 au total)
- **Grid trading — persistance SQLite + reprise au redémarrage** (`bot/strategies/grid.py`, `bot/live_bot.py` points 4–5) : `_save_state(conn)` upserte la ligne `grid_state` (bornes, step, taille, cycles, profit, `initialised`, `halted`) et toutes les lignes `grid_levels` (IDs d'ordres par niveau, prix, statut) après chaque changement d'état significatif — init de la grille, stop-loss, et dès que les IDs d'ordres changent après un poll de fill ; `restore_from_db(state)` est appelé au démarrage depuis `main()` et : (1) charge l'état sauvegardé depuis la DB, (2) valide que la config n'a pas changé (bornes/step/taille dans une tolérance de 1 centime — tout écart déclenche une re-initialisation propre), (3) si initialisé et non halté, appelle `get_open_orders()` pour se réconcilier avec l'exchange et détecte les fills survenus pendant l'arrêt du bot — les counter-orders appropriés sont placés immédiatement ; tables `grid_state` et `grid_levels` ajoutées via la migration de schéma v2 (`MIGRATIONS[2]`) ; `main()` définit désormais `state.session` avant le chargement de la stratégie pour que `restore_from_db` puisse appeler l'API REST de l'exchange ; 8 nouveaux tests dans `TestGridPersistence` couvrant la sauvegarde/upsert, la restauration sans état sauvegardé, l'incompatibilité de config, la restauration halted (sans réconciliation), la détection de fill offline et l'ordre encore ouvert (pas de faux fill) ; 264 tests au total
- **Grid trading — détection des fills, counter-orders, stop-loss** (`bot/strategies/grid.py` points 1–3) : `GridStrategy` est désormais entièrement opérationnel ; `_initialise_grid()` place les ordres BUY initiaux sous `best_ask` et les ordres SELL au-dessus au premier tick ; `_poll_fills()` détecte les ordres exécutés toutes les `poll_interval` secondes (défaut 2 s) — en mode simulation, vérification par croisement de prix (`best_ask <= buy_price` / `best_bid >= sell_price`), en mode réel, un seul appel `get_open_orders()` par cycle (les IDs absents de la réponse sont considérés exécutés, poids 40 Binance vs 4×N pour les requêtes individuelles) ; `_on_buy_filled()` place un SELL counter à `buy_price + grid_step` (ou marque idle si au-dessus de `grid_upper`) ; `_on_sell_filled()` comptabilise le PnL pour les cycles complets BUY→SELL (`profit = (sell_p − buy_p) × qty − frais_achat − frais_vente`) et place un BUY counter à `sell_price − grid_step` (ou marque idle si en-dessous de `grid_lower`) ; `_check_stop_loss()` déclenche `_cancel_all_orders()` et passe `grid.halted = True` quand `best_bid` sort de `[grid_lower, grid_upper]` ; `GridLevel` reçoit les champs `buy_price` et `sell_price` (prix réels des ordres, distincts du `price` de référence quand les counter-orders déplacent le slot) ; `GridState` reçoit `halted`, `poll_interval` et `last_poll_ts` ; 29 nouveaux tests dans `tests/test_bot.py` couvrant l'initialisation, les handlers de fill, la détection simulée des fills, le stop-loss et le comportement sim-mode des connecteurs CEX (444 tests au total)
- **Fonctions de gestion d'ordres CEX** (`bot/api_binance.py`, `bot/api_mexc.py`) : trois nouvelles fonctions async ajoutées aux deux connecteurs : `get_order_status(session, symbol, order_id)` → `"NEW"|"FILLED"|"CANCELED"|"PARTIALLY_FILLED"` ou `None` (GET `/api/v3/order`, poids 4) ; `cancel_order(session, symbol, order_id)` → `bool` (DELETE `/api/v3/order`, poids 1 ; retourne `True` immédiatement pour les IDs `sim_` ou si les credentials sont absents) ; `get_open_orders(session, symbol)` → `list[dict]` avec les clés `order_id`, `side`, `price`, `qty`, `status` (GET `/api/v3/openOrders`, poids 40 Binance / non spécifié MEXC) ; les deux connecteurs utilisent `str(symbol).split(":", maxsplit=1)[0]` pour supprimer le suffixe `:SELL` ; l'en-tête d'authentification diffère : `X-MBX-APIKEY` (Binance) contre `X-MEXC-APIKEY` (MEXC)
- **Socle grid trading — couche abstraction Strategy + Connector** (`bot/connectors/`, `bot/strategies/`) : `bot/connectors/__init__.py` fournit la factory `load(name)` (`"polymarket"`, `"binance"`, `"mexc"`) retournant le module `api_*` approprié via `importlib.import_module` ; `bot/strategies/base.py` définit le Protocole `Strategy` (`STRATEGY_TYPE: str`, `async on_book_update(state, ts, _t_ws=None)`) marqué `@runtime_checkable` ; `bot/strategies/__init__.py` fournit la factory `load(name, config)` — `"threshold"` retourne `None` (chemin intégré dans `live_bot.py`), `"grid"` retourne une instance `GridStrategy` ; `bot/live_bot.py` reçoit : `_load_connector(name)` qui remplace le module global `api` au démarrage via `global api; api = importlib.import_module(...)` (sans effet pour `"polymarket"`) ; le champ `BotState.strategy` (`None` → compatibilité ascendante threshold, `GridStrategy` → mode grid) ; `handle_book_update()` route vers `state.strategy.on_book_update()` quand non-None ; `BotConfig` reçoit `connector`, `strategy_type`, `grid_symbol`, `grid_lower`, `grid_upper`, `grid_levels`, `grid_order_size_usdt` ; les JSON de stratégie existants reçoivent `"strategy_type": "threshold"` et `"connector": "polymarket"` ; `strategies/grid_BTCUSDT.json` créé comme config d'exemple ; `.pylintrc` `max-module-lines` 1200 → 1300
- **`docs/GridTrading.md` + `docs/GridTrading.fr.md`** — nouvelle documentation bilingue du grid trading (333 lignes chacun) : détail de l'algorithme (formule du step, logique d'initialisation, cycle complet BUY→SELL avec exemple chiffré, stop-loss), tableau de comparaison threshold vs grid, schéma d'architecture avec flux d'exécution, tous les paramètres de config JSON avec types et contraintes, setup des variables d'environnement (Binance et MEXC), guide de démarrage en 5 étapes, calcul de rentabilité (formule profit brut/net, détail des frais, estimation de fréquence des cycles, scénario de perte maximale), tableau de statut d'implémentation (✅/🔲), instructions pour ajouter un nouveau connecteur CEX
- **Filtre jours fériés US** (`_us_holidays`, `_is_us_holiday`, `BotConfig.us_holiday_filter`) : nouveau booléen `us_holiday_filter` dans `BotConfig` (défaut `false`) ; quand activé, `is_trading_hour` retourne `False` pour les 10 jours fériés fédéraux US reconnus par le NYSE (New Year's Day, MLK Day, Presidents' Day, Good Friday, Memorial Day, Juneteenth, Independence Day, Labor Day, Thanksgiving, Christmas), avec décalage samedi→vendredi et dimanche→lundi pour les jours observés ; les dates sont calculées par un algorithme pur stdlib (aucune dépendance externe) et mises en cache par année via `functools.lru_cache` ; le filtre est indépendant de `hour_filter_enabled` — il s'active même quand le filtre de plage horaire est désactivé ; s'active via `"us_holiday_filter": true` dans le bloc `hour_filter` du JSON de stratégie ; 14 nouveaux tests dans `TestUsHolidays` couvrant les dates fixes, flottantes, les décalages observés, le Vendredi saint (algorithme de Pâques) et l'intégration avec `is_trading_hour`
- **Mode collecte de données** (`--snapshot-interval N` + `start_collector.sh` + `collect_db.sh` + `schedule_collect.sh` + `.claude/agents/data-collector.md`) : le premier compte de déploiement VPS est réaffecté en collecteur passif — `--simulate` (aucun ordre réel) + `--snapshot-interval 1` écrit une ligne de snapshot par seconde au lieu de toutes les 5s, éliminant l'angle mort responsable de ~50 LOSS supplémentaires dans les backtests alignés ; `scripts/start_collector.sh` déploie le code via rsync (session SSH unique via `bash -s` stdin pour éviter le rate-limiting du serveur), crée un `config.json` minimal et lance le bot depuis `bot/live_bot.py` avec `TRADINEBOTTE_DIR` pointant vers le répertoire isolé du collecteur ; `scripts/collect_db.sh` télécharge `live.db` sous `data/live_YYYY_WNN.db` chaque semaine, avec `--rotate` pour archiver la DB distante et redémarrer le collecteur, `--yes` pour les exécutions non interactives (cron) ; `scripts/schedule_collect.sh` installe/retire/affiche une entrée crontab (`--install`, `--remove`, `--status`, `--run-now`) qui exécute `collect_db.sh --rotate --yes` chaque dimanche à 03:00 UTC, en journalisant dans `~/tradinebotte/collect.log` ; `start_bot.sh` transmet désormais les flags inconnus à `live_bot.py` ; un nouvel agent Claude `data-collector` orchestre le workflow complet déploiement → collecte → backtest
- **`bot/live_bot.py` — flag `--snapshot-interval SECS`** : remplace `SNAPSHOT_INTERVAL` (défaut 5) à l'exécution sans toucher `config.json` ni le JSON de stratégie ; transmis via `make_config(snapshot_interval=...)` dans `BotConfig.snapshot_interval` ; utile pour la collecte de données (1s) ou les déploiements à faible espace disque (60s+)
- **`docs/HOWTO_tests_and_backtests.md` + `.fr.md`** — nouveau guide bilingue lisible par un humain (525 lignes chacun) : glossaire complet de tous les termes (snapshot, formule OBI, issues des trades WIN/LOSS/OPEN/STOP/GHOST, tous les paramètres de stratégie, métriques de performance), distinction explicite entre *backtest aligné* (simulation avec les paramètres corrigés, aucun ordre réel) et *bot réel* (trades réellement exécutés depuis la table `trades`) ; comment lancer les tests et interpréter la sortie ; les 13 flags du backtest documentés ; colonnes du tableau de sweep et du tableau de comparaison définies ; cadre de décision de mise à jour de stratégie (KEEP/MONITOR/UPDATE avec seuils chiffrés)
- **`scripts/backtest.py --compare` — colonne paramètres remise à l'échelle du stake/capital détecté** : quand le bot a tourné avec un stake/capital différent des valeurs par défaut de la stratégie (ex. paper3 : 150 $/1000 $ vs 10 $/100 $), la première colonne était sur une base économique différente rendant le PnL% incomparable ; désormais, quand stake ou capital_start divergent, le backtest de la colonne paramètres est relancé avec les valeurs détectées tout en conservant les paramètres de signal de l'utilisateur (threshold, min_secs, obi) ; le `_stat_block` d'en-tête affiche toujours les valeurs par défaut de la stratégie pour référence
- **Documentation — flags `--sweep-all`, `--sort`, `--top` ajoutés dans README et INSTALL** (EN + FR) : les trois flags avancés de backtest introduits dans la version précédente étaient documentés dans le CHANGELOG mais absents des tableaux de référence utilisateur de `README.md`, `README.fr.md`, `INSTALL.md`, `INSTALL.fr.md` ; les quatre fichiers incluent désormais ces flags dans les exemples de commandes backtest et dans les tableaux de paramètres
- **`scripts/backtest.py` — PnL% ajouté partout** (`summarize`, `_stat_block`, `print_aggregate`, `print_comparison`, `print_sweep_table`, `print_recommendations`) : chaque valeur PnL affiche désormais le rendement en pourcentage sur `capital_start` ; le tableau de sweep reçoit une colonne `PnL%` ; le tableau de comparaison reçoit une ligne de résultat `PnL%` et une ligne de config `Capital start` ; `_stat_block` affiche désormais la mise et le capital de départ
- **`scripts/backtest.py` — correctifs `detect_actual_params`** : `capital_start` utilise désormais le `capital_before` du premier trade (trié par `signal_ts_ms ASC`) au lieu de `MIN()`, qui sélectionnait auparavant une valeur en milieu de session appauvrie ; seuil de fiabilité DSL abaissé de 10× à 5× la mise — quand le DSL estimé dépasse 5× la mise, la détection est abandonnée (mise > DSL signifie qu'une seule perte suffit à déclencher la limite journalière, rendant l'heuristique du pire jour non fiable) ; le backtest aligné retombe sur le DSL de l'utilisateur quand la détection échoue (auparavant utilisait `mise × 5`, ce qui gonflait le PnL aligné en supprimant les déclenchements de stop-loss)
- **`.claude/agents/strategy-optimizer.md`** — nouveau subagent dédié à l'optimisation des paramètres de stratégie : exécute le workflow complet grid search + comparaison par base via `scripts/strategy_compare.sh`, interprète les résultats (ratio PnL/MaxDD, drawdown, win rate), produit un verdict structuré KEEP/MONITOR/UPDATE, et applique la meilleure configuration en créant un nouveau fichier de stratégie versionné (`polymarket_BTC5M_v3.json` etc.) et en mettant à jour le pointeur par défaut dans `live_bot.py` ; workflow en 5 étapes : lancer → lire le rapport → interpréter → recommander → appliquer
- **`scripts/strategy_compare.sh`** — nouveau script de workflow de comparaison : exécute `--sweep-all` + `--compare` en séquence, redirige la sortie simultanément vers stdout et un fichier horodaté dans `reports/` ; flags : `--top N` (défaut 10 configs uniques), `--sort ratio|pnl|wr`, `--db PATH`, `--out FILE`, `--no-save`
- **`scripts/backtest.py --top N`** — nouveau flag sweep : affiche uniquement les N meilleures configurations de stratégie uniques dans le tableau, dédupliquées sur `(threshold, min_secs, obi)` — supprime les variantes redondantes `min_ask` et `dsl` qui produisent des résultats identiques ; défaut 0 (tout afficher) ; la note `(top N configs uniques thr/secs/obi — 405 combos au total)` est ajoutée à la ligne de séparateur
- **Suite de tests étendue à 368 tests** — `test_backtest.py` reçoit 5 nouvelles classes couvrant des fonctions précédemment non testées : `TestRatio` (5 tests pour `_ratio`), `TestPercentile` (5 tests pour `_percentile`), `TestDetectActualParams` (6 tests pour `detect_actual_params`), `TestActualStats` (4 tests pour `_actual_stats`), `TestCollectDbs` (4 tests pour `_collect_dbs`) ; `test_bot.py` reçoit `TestStrategyLoading` (8 tests vérifiant l'existence du fichier de stratégie v2, le chargement des bons paramètres, la présence de la v1, et le retour None pour un fichier absent)
- **`strategies/polymarket_BTC5M_v2.json` — nouvelle stratégie par défaut** (optimisée sweep-all 2026-05-08) : `signal_threshold=0.95` (était 0.96), `min_secs_remaining=45`, `obi_reject_thresh=-0.75`, `daily_stop_loss=30` ; ratio PnL/MaxDD 4.42 contre 3.61 pour la v1 sur 5 bases de données / 912k snapshots ; pointeur de stratégie par défaut de `live_bot.py` mis à jour vers `polymarket_BTC5M_v2.json` ; la v1 est conservée pour référence
- **`scripts/backtest.py --sweep-all`** — grid search agrégé sur toutes les bases de données : exécute chacune des 405 combinaisons de paramètres (5 seuils × 3 min_secs × 3 min_ask × 3 OBI × 3 daily_stop_loss) indépendamment par DB et agrège les résultats (somme PnL, pire MaxDD entre sessions) ; affiche le tableau de sweep complet trié plus `print_recommendations()` — top-5 configs par ratio PnL/MaxDD, par PnL total et par win rate, avec la commande CLI exacte pour la meilleure config globale
- **`scripts/backtest.py --sort wr|pnl|ratio`** — le tableau de sweep est désormais triable ; le tri par défaut est `ratio` (PnL/MaxDD, style Calmar) ; `--sort pnl` classe par PnL total ; `--sort wr` classe par win rate
- **`scripts/backtest.py --sweep` — grille étendue** — `daily_stop_loss` ajouté comme dimension de sweep (`[30, 100, 500]`) ; la grille est maintenant 5×3×3×3×3 = 405 combinaisons (contre 5×3×3 = 45 auparavant) ; colonne `ratio` (PnL/MaxDD) et colonne `dsl` ajoutées au tableau de sweep
- **`scripts/backtest.py --compare` — tableau de comparaison à trois colonnes** (`detect_actual_params`) : `--compare` détecte automatiquement la config réelle du bot depuis la table `trades` (stake modal, seuil et min_secs au 5e percentile robuste aux valeurs aberrantes, `capital_before` minimum, stop-loss journalier estimé depuis la pire journée observée +20 % de marge), lance un second backtest aligné sur ces paramètres, et affiche un tableau côte à côte : BACKTEST(paramètres utilisateur) | BACKTEST(aligné sur l'actuel) | BOT RÉEL ; affiche aussi les lignes STOP/GHOST, avertit sur les écarts de config avec les flags CLI exacts pour reproduire, et fonctionne avec `--all` (comparaison par fichier en mode multi) ; gère les DB sans table `trades` (DB d'exemple) sans planter
- **Circuit-breaker sur les échecs API CLOB** (`BotState.api_fail_streak` / `api_cooldown_until`) : après 3 échecs consécutifs de `post_order` avec une clé privée configurée, les nouvelles entrées sont suspendues 5 minutes ; le compteur se remet à 0 au premier ordre réussi ; `check_signal` vérifie le cooldown avant le `signalled.add()` ; 6 nouveaux tests dans `TestCircuitBreaker`
- **Versionnage du schéma DB** (dictionnaire `MIGRATIONS` + `_apply_migrations`) : table `schema_version` ajoutée au SCHEMA ; `_apply_migrations(conn)` applique les migrations en attente dans l'ordre des versions et enregistre la version la plus haute appliquée ; `init_db` l'appelle après `executescript(SCHEMA)` ; `make_db()` dans les tests mis à jour en conséquence ; 5 nouveaux tests dans `TestSchemaVersioning`
- **`.pylintrc` — `[TYPECHECK] ignored-modules = websockets`** : supprime le faux positif `E0401 import-error` préexistant qui apparaît quand pylint s'exécute sous Python système (paquet venv uniquement) ; restaure 10,00/10

### Correction
- **pylint 10,00/10 rétabli** après le commit sécurité : helper `_today_ms_utc()` extrait dans `bot_utils.py` (élimine R0801 duplicate-code entre `generate_status_html` et `restore_state_from_db`) ; imports `from` stdlib déplacés avant les tiers-partis dans `live_bot.py` (C0411) ; `max-module-lines=1200` ajouté au `.pylintrc` (live_bot a dépassé 1000 lignes après BotConfig + cache PnL journalier)
- **`scripts/test_all_accounts.sh` — le parseur de résultat reconnaît désormais `OK (skipped=N)`** : le regex `^OK$` échouait quand unittest produit `OK (skipped=13)` ; changé en `^OK( |$)` pour que les déploiements avec des tests ignorés rapportent le succès correctement
- **`live_bot.py` — restauration du `import aiohttp, websockets` manquant** : supprimé par erreur lors du refactor BotConfig ; détecté par pylint 4.0 (`E0602 undefined-variable`) ; avertissement `global-statement` dans `_setup_logging` supprimé par directive inline (singleton légitime au niveau processus) ; score 9,44 → **10,00/10**

### Refactorisation
- **`purge_expired_markets(state)` extrait dans `live_bot.py`** : la boucle de nettoyage des marchés expirés (suppression dans `tokens`, `market_tokens`, `signalled`), identique en 9 lignes et dupliquée entre `_market_refresh_loop` et `account_bot._run`, est désormais une fonction partagée unique ; `account_bot.py` appelle `bot.purge_expired_markets(state)` ; commentaire `pylint: disable=duplicate-code` supprimé

### Performance
- **`check_signal` — cache mémoire du PnL journalier** (`state.daily_pnl`) : supprime le `SELECT SUM(pnl_net)` SQL exécuté à chaque message WebSocket ; `close_trade` met à jour le compteur de façon incrémentale ; le passage à minuit UTC est détecté en tête de `check_signal` (s'exécute à chaque mise à jour du carnet, pas seulement lors d'une tentative de signal) et remet le compteur à zéro ; `restore_state_from_db` l'initialise depuis la DB au démarrage pour que le cache soit exact après un redémarrage ; 7 nouveaux tests dans `TestDailyPnlCache`

### Correction
- **`restore_state_from_db` — garde anti-réentrée pour les marchés récemment résolus** : les trades résolus dans les 10 dernières minutes sont désormais ajoutés à `state.signalled` au démarrage ; auparavant, un redémarrage dans la même fenêtre de 5 minutes pouvait re-entrer sur un marché dont le prix affichait encore un bid ≥ 0.96 après résolution ; 6 nouveaux tests dans `TestSignalledRestore`

### Modifié
- **Refactorisation de `scripts/install.sh`** — flag `--lang EN|FR` pour les exécutions non interactives ; expansion du tilde sécurisée (`${var/#\~/$HOME}` remplace `eval echo`) ; helpers `_check_syntax` et `_pip_install` pour éliminer le code dupliqué ; copie des fichiers bot et vérification de syntaxe pilotées par une boucle commune ; `set -eo pipefail` remplace le simple `set -e`

### Fonctionnalité
- **Suite de tests étendue à 163 tests** — `tests/test_bot.py` contient désormais 105 tests (contre 95 auparavant) ; `test_backtest.py` et `test_multibot.py` inchangés à 28 et 30 ; passage confirmé sur les trois comptes de déploiement VPS en ~11s chacun
- **`bot/api_binance.py`** — nouveau connecteur API Binance spot implémentant la même interface publique que `api_polymarket.py` (`get_markets`, `post_order`, `parse_book_update`, `compute_fee`, helpers de métadonnées de marché) ; credentials via les variables d'environnement `BINANCE_API_KEY` / `BINANCE_API_SECRET` ou en kwargs ; signature HMAC-SHA256 ; mode simulation quand les credentials sont absents ; taux de frais 0,1 % ; `WS_URL` pointe sur `wss://stream.binance.com:9443/stream` (flux depth combiné) ; changer d'exchange ne nécessite qu'une modification d'import dans `live_bot.py`
- **`bot/api_mexc.py`** — nouveau connecteur API MEXC spot avec la même interface ; l'API REST v3 MEXC est compatible Binance mais utilise un cadrage WebSocket différent (méthode `SUBSCRIPTION`, flux `spot@public.limit.depth.v3.api@SYMBOL@5`, enveloppe de message `{"d": {...}, "s": "SYMBOL"}`) ; les ordres LIMIT MEXC ne requièrent pas `timeInForce` (le serveur applique GTC par défaut) ; taux de frais 0,2 % ; credentials via `MEXC_API_KEY` / `MEXC_API_SECRET`
- **`scripts/benchmark_api.py`** — nouvel outil de benchmark de latence ; mesure le temps aller-retour HTTP sur N requêtes séquentielles par endpoint (Polymarket Gamma, Binance, MEXC) et le temps jusqu'au premier message WebSocket pour chaque exchange ; rapporte min/mean/p50/p90/p99/max/σ avec barres ASCII ; options `--rounds N` (défaut 15) et `--no-ws` ; utilise le client WS intégré d'`aiohttp` si `websockets` n'est pas installé ; récupère dynamiquement un token Polymarket actif pour le test de souscription WS ; résultats sur VPS Amsterdam : Polymarket 12–20 ms REST / 52–103 ms WS, MEXC 10–30 ms REST / 880–960 ms WS, Binance 218–235 ms REST / 930–1090 ms WS
- **Interface bilingue** — `scripts/setup.py`, `install.sh`, `start_bot.sh` et `monitor.sh` proposent désormais un choix `[E] English / [F] Français` au démarrage ; `setup.py` persiste le choix sous la clé `"lang": "EN"|"FR"` dans `config.json` ; les scripts suivants lisent cette clé automatiquement — aucune re-saisie ; toutes les chaînes visibles par l'utilisateur sont traduites dans les deux sens via un dict `T` (Python) ou une fonction `_t()` (bash) ; les alias de colonnes SQL dans `monitor.sh` sont également traduits (`wins`/`victoires`, `current_capital`/`capital_actuel`)
- **`.claude/agents/bilingual-quality.md`** — nouveau subagent Claude Code (Sonnet) qui audite, met à jour et traduit à travers les 10 fichiers de documentation bilingues ; trois modes : AUDIT (rapport des manques, aucune édition), UPDATE (ajoute du contenu dans les deux langues simultanément), TRANSLATE (EN↔FR avec vocabulaire du projet) ; déclenché automatiquement par le rappel du hook post-commit et à la demande via `/bilingual-quality`
- **`config.json.example`** — ajout des champs de credentials `binance_api_key`, `binance_api_secret`, `mexc_api_key`, `mexc_api_secret` avec commentaires ; ajout du champ `lang` (écrit par `setup.py`, lu par les scripts shell)
- **`.claude/agents/doc-sync.md`** — subagent Claude Code (Haiku) qui audite tous les flags CLI des scripts utilisateur par rapport aux quatre fichiers de doc principaux (README.md, README.fr.md, INSTALL.md, INSTALL.fr.md) ; signale uniquement les manques, n'édite jamais ; intégré dans `scripts/run_tests.sh` comme étape post-suite non bloquante quand le CLI `claude` est présent
- **`scripts/start_bot.sh` — option `--reset-db`** : sauvegarde `live.db` vers `live.db.bak.AAAAMMJJ_HHMMSS`, puis le supprime avant le lancement pour que le bot reparte à zéro (capital, trades, historique) ; demande une confirmation `yes` avant d'agir ; sans effet si le fichier est absent
- **`bot/live_bot.py` v0.41 — paramètres par défaut optimisés** (grid search sur liveweek.db, 110 952 snapshots) :
  - `SIGNAL_THRESHOLD` 0.96 → **0.95** (plus de trades, même WR 99.3%, +14,73 $ vs +13,14 $)
  - `MIN_SECS_REMAINING` 45 → **30 s** (gain ~+7 $ de PnL à WR égal ; 45 s était trop restrictif)
  - `OBI_REJECT_THRESH` -0.50 → **-0.25** (filtre carnet plus sélectif — retourne la PnL liveweek de −2,43 $ à +4,97 $ pour la config 0.96/45 s)
  - Mêmes valeurs appliquées dans `scripts/backtest.py` (dataclass `Params` + valeurs par défaut CLI) et `strategies/polymarket_BTC5M.json`
  - Tests mis à jour : `test_blocked_bid_below_threshold` (0.95→0.94), `test_at_min_secs_remaining_blocked` (44s→29s), `test_no_signal_insufficient_secs` (30s→29s)
- **`bot/live_bot.py` — refonte du format des logs** (5 améliorations + uptime) :
  - Horodatage sans millisecondes : `2026-05-04 20:04:03` (était `2026-05-04 20:04:03,123`)
  - Niveau à largeur fixe : `[INFO ]` / `[WARN ]` / `[ERROR]` / `[CRIT ]` — les messages s'alignent dans le fichier
  - Couleurs ANSI sur stdout uniquement (le fichier reste plain) : jaune pour WARN, rouge pour ERROR, magenta pour CRIT
  - Séparateurs visuels sur les événements de trade : `▶ TRADE`, `✓ WIN `, `✗ LOSS`
  - Espacement des métriques plus lisible : `entry=0.9710  bid=0.9700  secs=52s` (double espace entre les champs)
  - Uptime dans la bannière de démarrage : `LIVE BOT v0.40 | start=2026-05-04 19:11:48 UTC | up 1h02m03s | ...`
- **`scripts/test_all_accounts.sh`** — nouveau script qui vide et réinstalle la dernière version sur tous les comptes de test configurés en séquence ; lit le serveur et les credentials depuis `~/.tradinebotte-test.conf` (même fichier que `test_multibot_deploy.sh`) ; utilise `sshpass` partout ; attend un délai configurable entre les comptes (180 s par défaut) ; options : `--delay SECONDES`, `--no-wait`, `--parallel` ; affiche un résumé final avec statut par compte

### Correctif
- **`bot/live_bot.py`** — `QueueHandler.prepare()` pré-formatait les records (appelant `self.format()` et stockant le résultat dans `record.msg`) avant l'enfilage ; quand `logging.basicConfig` assignait le formateur complet `_LOG_FMT` au `QueueHandler`, chaque record était formaté deux fois — une fois par le queue handler, une fois par le `FileHandler` — produisant des timestamps et niveaux dupliqués dans `live.log` ; corrigé en assignant un formateur passthrough `Formatter("%(message)s")` au `QueueHandler` pour ne stocker que le texte brut du message dans la queue
- **`scripts/install.sh`** — ajout de `cd "$REPO_DIR"` juste après le calcul de REPO_DIR ; les chemins relatifs (`bot/live_bot.py`, `tests/`, etc.) sont désormais résolus correctement quel que soit le répertoire courant depuis lequel le script est appelé (échouait sur le VPS où le script était lancé depuis `~` plutôt que depuis la racine du dépôt)
- **`bot/account_bot.py`** — import `getpass` inutilisé supprimé (résidu du refactoring `~/tmp`) ; score pylint rétabli à 10.00/10
- **`bot/feed.py`** — ajout de `# pylint: disable=duplicate-code` (la boucle recv WebSocket reflète intentionnellement `live_bot.py`)
- **`tests/test_multibot.py`** — `TEST_PORT` était codé en dur à `15557` ; quand plusieurs utilisateurs Linux lançaient les tests en parallèle sur le même serveur, tous tentaient de `bind()` sur `tcp://127.0.0.1:15557` simultanément, provoquant `ZMQError: Address already in use` ; le port est désormais dérivé de `os.getuid() % 900 + 15000` pour que chaque utilisateur OS obtienne un port loopback distinct dans la plage 15000–15899

### Refactorisation
- **`bot/live_bot.py` — dataclass `BotConfig`** — effets de bord au niveau module supprimés : plus d'inspection de `sys.argv`, d'I/O fichier, de configuration du logging ni d'`os.makedirs` à l'import ; toute la configuration d'exécution (chemins, paramètres de stratégie, credentials, filtre horaire, filtre de volatilité, timing, statut web) est portée par une dataclass `BotConfig` ; la factory `make_config(simulate, no_log, no_snapshots)` lit les variables d'environnement, `config.json` et le JSON de stratégie — appelée uniquement depuis `main()` ; `_setup_logging(config)` configure les handlers et le `QueueListener` — appelée uniquement depuis `main()` ; `BotState(conn, config)` embarque sa config pour que les fonctions du chemin critique lisent `state.config.X` au lieu des globales du module ; `init_db(config)` et `is_trading_hour(config, ts_ms=None)` prennent désormais un paramètre config explicite ; `bot/account_bot.py` mis à jour pour appeler `bot.make_config()` / `bot.init_db(config)` / `bot.BotState(conn, config)` ; `TestIsTradingHour` dans `tests/test_bot.py` mis à jour pour utiliser des objets `BotConfig` ; 311 tests toujours passants ; l'import de `live_bot` est désormais une opération neutre — plusieurs instances du bot peuvent coexister avec des configurations distinctes
- **Répertoires temporaires** — seul le feed partagé reste dans `/tmp` ; tous les chemins per-user déplacés dans `~/tmp/` :
  - `bot/account_bot.py` : `_FEED_TMP_DIR` passe de `/tmp/tradinebotte-<user>` à `/tmp/tradinebotte-feed` (sans suffixe utilisateur) ; créé avec chmod 1777 pour que chaque utilisateur Linux puisse y écrire ses fichiers lock/log, le sticky bit empêchant la suppression inter-utilisateurs ; le verrou fichier est désormais vraiment cross-user, garantissant une seule instance de `feed.py` pour tous les comptes
  - `tests/test_bot.py`, `tests/test_multibot.py` : sandbox de test déplacé dans `~/tmp/tradinebotte-test`
  - `scripts/run_tests.sh` : `TRADINEBOTTE_DIR` mis à jour vers `${HOME}/tmp/tradinebotte-test`
  - `scripts/profile_hotpath.py`, `scripts/profile_compare.py` : sandbox de profiling déplacé dans `~/tmp/profile-bot`
  - `scripts/install_service.sh`, `install_account_service.sh`, `install_feed_service.sh` : fichiers `.service` générés déplacés dans `~/tmp/`
  - `scripts/test_standalone_deploy.sh`, `test_multibot_deploy.sh` : chemins de nettoyage et de recherche du log feed mis à jour

---

## [0.40] - 2026-05-02

### Correctif
- **`tests/test_bot.py`** — le répertoire de test était codé en dur à `/tmp/tradinebotte-test`, provoquant une `PermissionError` quand deux utilisateurs lancaient les tests simultanément sur le même serveur ; utilise désormais `/tmp/tradinebotte-test-<user>` via `getpass.getuser()`
- **`scripts/install.sh`** — l'option `--with-tests` pouvait échouer avec « source et destination identiques » quand `REPO_DIR == INSTALL_DIR` (repo cloné directement dans le répertoire d'installation) ; le bloc de copie est maintenant ignoré dans ce cas, en miroir de la garde existante pour `strategies/`
- **`scripts/install.sh`** — les commandes pip en dur remplacées par `-r requirements.txt`, de sorte que toutes les dépendances (dont `pyzmq` pour `feed.py` / multibot) sont installées automatiquement sans maintenir une liste dupliquée dans le script

---

## [0.32] - 2026-05-02

### Fonctionnalité
- **`bot/live_bot.py` — filtre de volatilité** — nouvelle garde d'entrée qui bloque les trades lorsque le marché a fortement oscillé au cours des 60 dernières secondes ; trois métriques complémentaires calculées sur une fenêtre glissante de 12 échantillons (prélevés toutes les 5s) : `vol_bid` (écart-type du best_bid, seuil 0.07), `range_bid` (amplitude max−min, seuil 0.30), `obi_vol` (écart-type de l'OBI, seuil 0.40) ; un trade est ignoré si une métrique dépasse son seuil et qu'au moins 6 échantillons sont disponibles (30s de chauffe) ; calibré sur les données live du 2026-04-25 au 05-01 (301 trades) : pertes 8→1, win rate 97.3%→99.5%, EV −$0.050→+$0.160 par trade ; activé/désactivé via `VOL_FILTER_ENABLED` ; résultats de calibration dans `volstop.txt`
- **`bot/live_bot.py` — `VOL_FILTER_WEEKDAY_ONLY`** — option qui suspend le filtre de volatilité pendant la session weekend (ven. 20h00 UTC → lun. 13h30 UTC) ; désormais à `True` par défaut — le backtest multi-DB montre un meilleur EV global (+0.1284 vs +0.1196) et évite de bloquer des signaux valides le weekend où les patterns de liquidité BTC diffèrent ; nouveau helper `_in_weekend_session()` qui encapsule la logique de détection, couvert par 10 tests unitaires
- **`data/liveweek.db`** — base de données du bot live VPS Londres (2026-04-25 au 05-01, 110 883 snapshots, 301 trades résolus) ajoutée comme dataset de backtest ; utilisée pour la calibration du filtre de volatilité
- **`scripts/backtest_volfilter.py`** — script de simulation : charge snapshots et trades depuis une DB, calcule les indicateurs de volatilité au moment de l'entrée sur une fenêtre glissante, balaye les seuils de `vol_bid`/`range_bid`/`obi_vol`, et présente les performances baseline vs. filtrées avec un classement des 10 meilleures configurations par EV/trade

### Refactoring
- **`bot/bot_utils.py`** — nouveau module utilitaire extrait de `live_bot.py` : `print_dashboard`, `generate_status_html`, `write_web_status`, `setup_htaccess`, `_htpasswd_sha1` ; la configuration est injectée via des variables de module synchronisées depuis `live_bot` après chargement de `config.json` ; aucune dépendance circulaire ; `live_bot.py` passe de 991 à 847 lignes
- **`bot/live_bot.py`** — le module reste centré sur la logique de trading : traitement des signaux, classes d'état, boucle WebSocket, gestion des trades, initialisation de la DB

### Correction de bug
- **`scripts/install.sh`** — la vérification du CLI `sqlite3` est rétrogradée de l'erreur bloquante à un avertissement non-bloquant ; le bot utilise le module Python `sqlite3` intégré (toujours disponible) et n'appelle jamais le CLI — seul `monitor.sh` en a besoin pour les requêtes manuelles ; l'avertissement affiche toujours la commande d'installation mais ne bloque plus avec `exit 1`
- **`docs/CONTEXT_AI.md`** — retiré du tracking git ; le fichier reste sur le disque pour le contexte AI local mais est désormais listé dans `.gitignore` afin de ne jamais être poussé sur le dépôt public ; contient des détails d'infrastructure et des notes opérationnelles internes qui n'ont pas leur place dans un dépôt public
- **`docs/CONTEXT_AI.md`** — credentials commités supprimés : clé privée, API key, API secret, passphrase, adresse wallet et IP VPS remplacés par des tokens `<PLACEHOLDER>` ; les credentials ne doivent être stockés que dans `config.json` (ignoré par .gitignore)
- **`scripts/test_standalone_deploy.sh`** — nouveau test d'intégration SSH pour le scénario Option A multi-utilisateur : déploie sur 2 utilisateurs Linux, démarre `start_bot.sh` en tant qu'utilisateur 1, puis vérifie que l'utilisateur 2 peut aussi démarrer sans être bloqué par le processus de l'utilisateur 1 (détecte la classe de bugs de portée `pgrep -f`) ; confirme les deux connexions WebSocket dans les logs ; structure en 6 phases (nettoyage, déploiement, lancement×2, vérification logs, teardown, rapport)
- **`scripts/run_integration_tests.sh`** — wrapper qui lance `test_standalone_deploy.sh` puis `test_multibot_deploy.sh` en séquence ; options `--standalone` / `--multibot` pour n'en lancer qu'un seul ; résumé final avec comptage réussi/échoué et temps écoulé ; `INSTALL.fr.md` mis à jour pour documenter les deux tests et le wrapper
- **`UPDATE.md` / `UPDATE.fr.md`** — nouveau guide bilingue documentant le workflow de mise à jour pour les trois scénarios : repo et répertoire d'installation séparés (`git pull` + `install.sh`), repo = répertoire d'installation (idem), et rsync depuis une machine de dev (avec l'avertissement critique `--exclude='config.json'`) ; mise à jour multi-bot Option B couverte aussi ; ajouté au tableau des docs bilingues dans `CLAUDE.md`
- **`scripts/install.sh`** — détecte le virtualenv existant lors d'une mise à jour : si `$INSTALL_DIR/venv/` existe déjà, saute `python3 -m venv` et exécute uniquement `pip install --upgrade` ; les installations fraîches sont inchangées ; réduit le temps de mise à jour de ~2 min (reconstruction complète du venv) à quelques secondes
- **`QUICKSTART.md` / `QUICKSTART.fr.md`** — réécrit de ~185 lignes à ~40 lignes ; seuls les deux flux de commandes (Option A et B), le tableau de décision et la note simulation « sans wallet » sont conservés ; tous les détails déplacés vers `INSTALL.md` ; lien vers `UPDATE.md` ajouté dans l'en-tête
- **`scripts/start_bot.sh`** — la vérification d'instance unique utilisait `pgrep -f live_bot.py` qui trouve les processus de tous les utilisateurs Linux sur le même hôte ; sur un serveur partagé cela empêchait tout second utilisateur de démarrer son bot ; corrigé en limitant à l'utilisateur courant avec `pgrep -u "$(id -u)" -f live_bot.py`
- **`scripts/install.sh`** — garde anti-copie-sur-soi pour `strategies/*.json` : quand le dépôt est cloné directement dans `INSTALL_DIR` (ex. `git clone → ~/tradinebotte`, `install → ~/tradinebotte`), l'ancien `cp strategies/*.json "$INSTALL_DIR/strategies/"` copiait chaque fichier sur lui-même et le corrompait silencieusement ; corrigé en comparant les chemins absolus résolus (`_STRAT_SRC` vs `_STRAT_DST`) et en sautant la copie quand ils sont identiques
- **`bot/account_bot.py`** — les fichiers lock et log sont désormais créés dans `/tmp/tradinebotte-<user>/` au lieu du chemin plat `/tmp/tradinebotte-feed-<hash>.*` ; chaque utilisateur Linux possède entièrement son sous-répertoire, ce qui élimine les erreurs « Operation not permitted » sur les hôtes partagés où le sticky bit de `/tmp` empêche un utilisateur de supprimer les fichiers d'un autre
- **`scripts/test_multibot_deploy.sh`** — le nettoyage (Phase 1) et le teardown (Phase 8) utilisent désormais `rm -rf /tmp/tradinebotte-$USER` conformément au nouveau chemin par utilisateur ; la détection du log du feed (Phases 5 et 7) parcourt le sous-répertoire de chaque utilisateur et mémorise lequel a démarré le feed (`FEED_LOG_IDX`) pour que les lectures suivantes utilisent le bon compte SSH

---

## [0.3] - 2026-05-01

### Correction de bug
- **`scripts/start_bot.sh`** — le message de lancement et le chemin du log s'affichaient avec le chemin absolu `$HOME` ; remplacé par des chemins relatifs avec `~` via substitution bash (`${VAR/$HOME/\~}`)
- **`scripts/start_bot.sh`** — utilisait `python3` système au lieu du `python3` du virtualenv ; le bot crashait immédiatement car aiohttp/web3/etc. sont installés dans le venv, pas dans le Python système ; corrigé pour utiliser `$INSTALL_DIR/venv/bin/python3` ; la sortie `nohup` est désormais redirigée vers `live.log` au lieu de `/dev/null` pour que les erreurs de démarrage soient visibles dans le tail du log ; ajout d'une vérification d'existence du venv avec un message d'erreur clair

### Amélioration
- **`scripts/setup.py`** — appuyer sur Entrée sans clé privée crée désormais un `config.json` de simulation (credentials vides) et sort proprement ; les imports blockchain sont entièrement ignorés dans ce chemin ; le prompt mentionne l'option ; docs QUICKSTART + INSTALL mis à jour (EN + FR)
- **`scripts/install.sh`** — n'appelle plus `apt-get` directement ; détecte à la place les paquets système manquants (`python3`, `python3-venv`, `python3.X-venv`, `sqlite3`) et affiche la commande `sudo apt-get install` exacte avec le numéro de version Python auto-détecté ; sort en erreur si quelque chose manque, continue silencieusement si tout est présent ; docs mis à jour dans INSTALL, QUICKSTART, README (EN + FR)

### Qualité de code
- `bot/account_bot.py` — pylint 10/10 : import `json` inutilisé supprimé ; `open(lock_file)` et `open(log_path)` portent désormais un encodage explicite (`utf-8`) ou le mode binaire (`ab`) ; les usages intentionnels sans `with` sont annotés `# pylint: disable=consider-using-with` ; `# pylint: disable=duplicate-code` ajouté au niveau module (boucle de purge des marchés expirés identique à `feed.py` par conception)
- `bot/account_bot.py` — ResourceWarning corrigé : `_run()` encapsule désormais sa boucle d'événements dans un `try/finally` pour appeler `sock.close(linger=0)` et `ctx.term()` à l'annulation ; le socket et le contexte ZMQ sont garantis d'être libérés lors de l'annulation de la tâche en test ou à l'arrêt

### Tests
- **`scripts/test_multibot_deploy.sh`** — deux bogues corrigés : (1) `grep -c || echo 0` produisait `"0\n0"` car `grep -c` imprime toujours le compteur sur stdout avant de quitter avec code 1 quand aucun résultat, et `|| echo 0` ajoutait alors un second zéro — causant l'échec de la comparaison arithmétique `[[ -eq ]]` avec une erreur de syntaxe ; corrigé en remplaçant `|| echo 0` par `|| true` (grep a déjà imprimé le compteur) ; (2) `ELAPSED=20` n'avait pas été mis à jour quand l'attente de stabilisation Phase 4 avait été portée de 20 à 30 s, faisant tourner la boucle heartbeat une itération de trop en Phase 6 ; corrigé en `ELAPSED=30`
- **`scripts/test_multibot_deploy.sh`** — test d'intégration de bout en bout pour le multi-bot Option B sur des comptes de test configurables : Phase 1 nettoyage (tue les processus, supprime les répertoires, efface les lock files), Phase 2 déploiement (rsync + création venv + pip install sans root), Phase 3 lancement simultané des N account_bots en mode `--verbose` pour stresser le démarrage automatique du feed avec verrou sans race condition, Phase 4 opération soutenue avec heartbeats toutes les 30s, Phase 5 analyse des logs (confirmation WebSocket du feed, nombre de book updates par bot, comptage des lignes ERROR/CRITICAL), Phase 6 teardown et comptage final des processus ; sort 0 en cas de succès total, 1 en cas d'échec ; `--skip-deploy` réutilise une installation existante ; `--duration N` remplace les 3 minutes par défaut ; l'adresse serveur, le port SSH, les noms d'utilisateur et les mots de passe sont lus depuis `~/.tradinebotte-test.conf` (ou la variable `TEST_MULTIBOT_CONF`) — jamais codés en dur ; `scripts/test_multibot.conf.example` fournit le template

### Données
- **`data/basicsunday.db`** — 24 870 snapshots d'une session de simulation live ~26h (2026-04-25 20:57 → 2026-04-26 22:55), 312 marchés distincts ; résultat backtest : 42/43 victoires (97,7 %), PnL -3,58 $ ; combiné avec `calmsaturday.db` via `--all` : 52 trades, 51 victoires, **98,1 % de taux de victoire agrégé** ; `README.fr.md` mis à jour avec un tableau comparatif des jeux de données

### Documentation
- **Prérequis administrateur serveur** — confirmé en conditions réelles sur Ubuntu 22.04 / Python 3.10 : les trois paquets `python3-venv`, `python3-pip`, et `python3.10-venv` sont requis ; `python3.10-venv` est désormais un prérequis principal (non un fallback) dans `INSTALL.md`, `INSTALL.fr.md`, `QUICKSTART.md`, `QUICKSTART.fr.md`, `README.md`, `README.fr.md` ; sans lui la création du venv échoue avec *"ensurepip is not available"*

### Fonctionnalité
- **Partage WebSocket multi-bot (Option B — ZeroMQ)** — `bot/feed.py` maintient une seule connexion WebSocket vers Polymarket et publie chaque mise à jour du carnet d'ordres via un socket ZeroMQ PUB (`tcp://127.0.0.1:5557` par défaut, configurable via `TRADINEBOTTE_FEED_ADDR`). `bot/account_bot.py` souscrit au feed et exécute la stratégie complète pour un compte en isolation. Plusieurs processus `account_bot.py` peuvent tourner en parallèle, chacun avec son propre `TRADINEBOTTE_DIR` (config, DB, log), sans ouvrir de connexion WebSocket supplémentaire. `scripts/start_feed.sh` et `scripts/start_account.sh` gèrent le lancement et les logs. `pyzmq` ajouté dans `requirements.txt`.
- **Filtre heure/jour** — nouveau bloc `hour_filter` dans `strategies/polymarket_BTC5M.json` (désactivé par défaut). Quand actif, restreint les entrées à des plages horaires UTC configurables par type de jour (semaine/weekend), avec gestion spéciale de l'ouverture hebdomadaire US (lundi avant 13h30 UTC) et de la fermeture (vendredi à partir de 20h00 UTC). Appliqué identiquement dans le bot live (garde `is_trading_hour()` dans `check_signal()`) et dans le moteur de backtest (`_is_trading_hour()` dans `run_backtest()`). 15 nouveaux tests unitaires.

### Correctif
- `bot/live_bot.py` — `--simulate` n'écrase plus `TRADINEBOTTE_DIR` si la variable est déjà définie dans l'environnement ; plusieurs bots peuvent désormais tourner en simulation parallèle avec des répertoires totalement isolés (`TRADINEBOTTE_DIR=~/compte-a python3 live_bot.py --simulate`)

### Documentation
- `INSTALL.md` / `INSTALL.fr.md` — nouvelle section « Filtre heure / jour » : tableau de justification (sessions asiatique/EU/US, ouverture/fermeture hebdomadaire), référence complète des paramètres, logique de décision pas-à-pas avec exemple du lundi, 3 configurations prêtes à l'emploi, workflow de validation par backtest, exemple de log au démarrage
- `README.md` / `README.fr.md` — nouveau bullet fonctionnalité pour le filtre horaire ; compteur de tests mis à jour à 123
- `docs/multi.md` / `docs/multi.fr.md` — documentation bilingue complète de l'architecture : guide de décision Option A vs B (quand utiliser chaque mode, tableau de tradeoffs), diagramme ASCII, référence des composants, évaluation indépendante du signal par compte, protocole de messages (3 types avec tous les champs), variables d'environnement, arborescence, séquence de lancement, monitoring par compte, modes de défaillance, déploiement cross-user (comptes Linux différents), ajout d'un troisième compte, tableau comparatif avec le mode autonome ; lié depuis README, INSTALL, QUICKSTART, feed.py, account_bot.py
- `QUICKSTART.md` / `QUICKSTART.fr.md` — section de choix de déploiement restructurée : format liste de définitions pour Option A/B, tableau de décision couvrant compte unique, deux wallets même/différents utilisateurs Linux, comparaison de stratégies, tradeoffs simplicité vs efficacité
- `tests/test_multibot.py` — 30 nouveaux tests pour `feed.py` et `account_bot.py` : 7 tests unitaires pour `feed.register_market()`, 9 tests unitaires pour `account_bot._register_from_market_msg()`, 8 tests d'intégration ZMQ async pour l'Option A (bot seul), 6 tests d'intégration ZMQ async pour l'Option B (deux bots simultanés partageant le même feed) ; compteur total porté à 153

- **Mode diagnostic `--verbose`** — `bot/feed.py` et `bot/account_bot.py` acceptent tous deux `--verbose` ; active le niveau DEBUG et émet des traces détaillées : messages WebSocket bruts (tronqués à 200 caractères), chaque publication ZMQ PUB avec les champs clés, résultats et timing des sondes ZMQ, étapes de la course au verrou dans `_ensure_feed()` et boucle d'attente seconde par seconde, comparaisons de seuil de signal sur les books, tokens inconnus ignorés, enregistrements de marchés, paramètres d'init au démarrage ; le sous-processus `feed.py` hérite de `--verbose` quand `account_bot.py` est démarré avec ce flag ; la sortie INFO normale est inchangée sans le flag

- **Démarrage automatique du feed dans account_bot.py** — `_ensure_feed()` sonde l'adresse du feed pendant 5 s au démarrage ; si inaccessible, acquiert un verrou exclusif (`/tmp/tradinebotte-feed-<hash>.lock`) et lance `feed.py` en sous-processus, attendant jusqu'à 30 s qu'il soit prêt avant de libérer le verrou ; les account_bots concurrents qui perdent la course se bloquent sur un verrou partagé et se connectent dès que le gagnant libère le verrou — tous les bots peuvent désormais être démarrés simultanément sans gestion manuelle du feed ; les logs du feed vont dans `/tmp/tradinebotte-feed-<hash>.log` ; `docs/multi.md`, `QUICKSTART.md` mis à jour pour refléter la séquence de lancement simplifiée
- **Services systemd pour le multi-bot (Option B)** — `scripts/tradinebotte-feed.service` et `scripts/tradinebotte-account.service` sont des templates d'unité pour le feed ZeroMQ et les bots par compte. `scripts/install_feed_service.sh` auto-détecte le virtualenv (`.venv` pour dev, `venv` pour prod), valide `feed.py` et `pyzmq`, génère un service système prêt à installer avec `User=`, `WorkingDirectory=`, `ExecStart=`, et `TRADINEBOTTE_FEED_ADDR=`. `scripts/install_account_service.sh` dérive le nom du service depuis le basename du répertoire de compte (`tradinebotte-account-<nom>`), applique la même auto-détection du venv, et déclare `Requires=tradinebotte-feed.service` pour que systemd impose l'ordre de démarrage/redémarrage. Les deux scripts affichent les commandes `sudo cp / daemon-reload / enable / start` exactes. `docs/multi.md` et `docs/multi.fr.md` mis à jour avec un guide d'installation systemd complet.

### Précédent
- `scripts/tradinebotte.service` — template d'unité systemd : `After=network-online.target`, `Restart=on-failure`, `RestartSec=30`, `StartLimitBurst=5` (max 5 redémarrages par 5 min) ; les placeholders `__USER__` et `__TRADINEBOTTE_DIR__` sont substitués à l'installation
- `scripts/install_service.sh` — script générateur : lit `TRADINEBOTTE_DIR` (ou utilise `~/tradinebotte` par défaut), valide que l'installation existe, substitue les placeholders avec `sed`, écrit dans `/tmp/tradinebotte.service` et affiche les quatre commandes `sudo` pour activer le service

### Qualité de code
- `bot/live_bot.py` — mypy strict : 0 erreur ; ajout d'annotations de type explicites pour `_log_handlers: list[logging.Handler]`, `_log_queue: queue.Queue[logging.LogRecord]` et les cinq attributs dict/set de `BotState.__init__` (`tokens`, `market_tokens`, `open_trades`, `traded_direction`, `signalled`) ; `cur.lastrowid or 0` protège le type de retour `int | None`
- `tests/test_bot.py` — ResourceWarning corrigé : suppression du `warnings.filterwarnings` global ; les sept classes de test créant des connexions SQLite utilisent désormais `setUp`/`tearDown` ou `self.addCleanup(conn.close)` ; aucun avertissement de connexion non fermée sur Python 3.13
- `.github/workflows/mypy.yml` — nouveau workflow CI : exécute `mypy bot/live_bot.py bot/api_polymarket.py --ignore-missing-imports` à chaque push et pull request (Python 3.12)
- `requirements-dev.txt` — `mypy` ajouté

### Documentation
- `QUICKSTART.md` / `QUICKSTART.fr.md` — nouveau guide de démarrage rapide bilingue : cinq commandes (clone, install, setup, start, monitor) couvrant le chemin minimal du zéro à un bot opérationnel ; inclut une note sur le mode simulation et la commande d'arrêt ; croisé depuis `README`, `INSTALL` et `CLAUDE.md`
- `CLAUDE.md` — règle de documentation bilingue étendue de 6 à 8 fichiers pour inclure `QUICKSTART.md` / `QUICKSTART.fr.md`

### Fonctionnalité
- `scripts/backtest.py` — backtest multi-fichiers : `--db` accepte désormais un ou plusieurs chemins (expansion de glob shell, ex. `--db data/*.db`) ; nouveau flag `--all` qui scanne le répertoire `data/` pour tous les fichiers `.db` et ajoute `live.db` en tête s'il contient ≥ 100 snapshots ; le capital se réinitialise à `capital_start` indépendamment par fichier ; le bloc BACKTEST par fichier affiche le nom du fichier quand plusieurs fichiers sont traités ; un bloc AGGREGATE (wins/losses/PnL/taux de victoire/pire drawdown combinés) est affiché après tous les fichiers lorsqu'il y en a plus d'un et que `--sweep` n'est pas actif

### Sécurité
- `requirements.txt` / `requirements-dev.txt` — manifestes de dépendances : toutes les dépendances runtime (`aiohttp`, `websockets`, `web3`, `py-clob-client`) et dev (`pylint`, `pip-audit`) sont désormais déclarées dans des fichiers versionnés ; tous les workflows CI installent depuis ces fichiers plutôt que de lister les paquets en dur
- `.github/workflows/audit.yml` — nouveau workflow CI : exécute `pip-audit -r requirements.txt` à chaque push et chaque lundi à 06 h 00 UTC pour détecter les CVE connus dans les dépendances runtime avant qu'elles n'atteignent la production
- `.github/dependabot.yml` — Dependabot activé pour les paquets `pip` et les `github-actions` ; crée des PRs automatiques chaque lundi lorsque des versions plus récentes sont disponibles

### Qualité de code
- `bot/live_bot.py` — annotations de types complètes : les 28 fonctions et méthodes de classe portent désormais des annotations de paramètres et de retour (`dict[str, Any]`, `list[str]`, `Optional[float]`, `sqlite3.Connection`, `aiohttp.ClientSession`, etc.) ; le paramètre websocket `ws` utilise `Any` pour rester compatible avec les différentes versions de la bibliothèque websockets
- `tests/test_bot.py` — 9 nouveaux tests ajoutés (71 → 80) : `TestHtpasswd` (préfixe SHA1, valeur connue, collision), `TestGenerateStatusHtml` (capital, table, taux de victoire, état vide), `TestHandleBookUpdate` (mise à jour d'état depuis un message parsé, token inconnu ignoré) ; utilise SQLite en mémoire et `unittest.IsolatedAsyncioTestCase` — aucun réseau ni credentials requis
- `.github/workflows/tests.yml` — nouveau workflow CI : exécute `unittest discover` sur Python 3.10, 3.11 et 3.12 à chaque push ; suite totale : 108 tests (80 bot + 28 backtest)

### Fonctionnalité
- `bot/live_bot.py` — mesure de latence : chaque trade émet une ligne `[LATENCY]` avec `signal_ms` (message WebSocket reçu → décision d'ordre, inclut tous les gardes du signal et la requête SQLite PnL journalier) et `order_rtt_ms` (round-trip HTTP API CLOB) ; les timestamps utilisent `time.monotonic()` et sont passés en paramètre optionnel `_t_ws` à travers `handle_book_update` → `check_signal` → `enter_live_trade` ; zéro surcharge sur les messages sans trade
- `scripts/latency.py` — nouvel outil d'analyse : parse les lignes `[LATENCY]` de `live.log` et affiche min / mean / p50 / p90 / p99 / max pour signal_ms, order_rtt_ms et total_ms ; usage : `python3 scripts/latency.py [logfile]`

### Fonctionnalité
- `bot/live_bot.py` — les écritures de logs sont désormais entièrement asynchrones : un thread daemon `QueueListener` vide la queue de logs sur disque ; le event loop asyncio n'est plus jamais bloqué par des I/O fichier ; aucun changement de comportement pour les déploiements existants
- `bot/live_bot.py` — nouveau flag `--no-log` : supprime entièrement le fichier log (`NullHandler`) ; la DB SQLite (trades + snapshots) n'est pas affectée ; combiner avec `--simulate` pour conserver la sortie stdout ; destiné aux déploiements de production où les I/O disque doivent être minimaux

### Correction de bug
- Tous les scripts, `bot/live_bot.py` et toute la documentation — variable d'environnement renommée de `POLYMARKET_DIR` en `TRADINEBOTTE_DIR` pour correspondre au nom du projet ; mettre à jour tout `export POLYMARKET_DIR=...` dans le profil shell ou l'unité systemd en `export TRADINEBOTTE_DIR=...`

### Correction de bug
- Tous les scripts, `bot/live_bot.py` et toute la documentation — répertoire d'installation par défaut renommé de `~/polymarket` en `~/tradinebotte` pour correspondre au nom du bot ; `TRADINEBOTTE_DIR` reste prioritaire comme avant ; répertoire temporaire de simulation renommé de `/tmp/polymarket-sim` en `/tmp/tradinebotte-sim` ; répertoire temporaire de test renommé de `/tmp/polymarket-test` en `/tmp/tradinebotte-test`

### Correction de bug
- Tous les scripts et `bot/live_bot.py` — chemin d'installation par défaut changé de `/opt/polymarket-live` (nécessitait root) vers `~/polymarket` (aucun root requis) ; `TRADINEBOTTE_DIR` reste prioritaire comme avant ; les entrées historiques du CHANGELOG référençant `/opt/polymarket-live` reflètent l'ancien défaut et sont conservées

### Correction de bug
- `scripts/backtest.py` — le fallback vers le dataset embarqué exige désormais que `live.db` contienne au moins 100 snapshots (auparavant, tout fichier non vide était accepté, ce qui permettait à un artefact de test périmé de 16 snapshots de masquer le dataset embarqué) ; affiche également quel fichier est sélectionné (`(live)` ou `(sample)`) au démarrage

### Fonctionnalité
- `bot/live_bot.py` — nouveau flag `--simulate` : redirige tous les fichiers vers `/tmp/polymarket-sim` (surcharge `TRADINEBOTTE_DIR`), duplique les logs sur stdout en plus du fichier log, et affiche un avertissement `MODE SIMULATION` visible ; le `live.db` et `live.log` de production ne sont jamais touchés ; utilisable sur n'importe quelle machine sans credentials
- `INSTALL.md` / `INSTALL.fr.md` — section Tests mise à jour : `timeout 20 python3 bot/live_bot.py --simulate` remplace la commande précédente qui écrivait dans le chemin de production par défaut
- `README.md` / `README.fr.md` — bullet Mode simulation mis à jour pour décrire `--simulate`

### Correction de bug
- `scripts/start_bot.sh` — refuse de démarrer si une instance de `live_bot.py` tourne déjà (quitte avec erreur et affiche le PID existant) ; précédemment tuait l'instance en cours automatiquement, ce qui pouvait interrompre un trade ouvert en cours de résolution
- `INSTALL.md` / `INSTALL.fr.md` — section Lancement mise à jour pour documenter le nouveau comportement et la commande d'arrêt manuel (`pkill -f live_bot.py`)

### Données
- `data/backtest_sample_btc5m_range_2026.db` — jeu de données SQLite embarqué : 2430 snapshots collectés en mode simulation le 2026-04-25 sur de vrais marchés BTC 5 minutes Polymarket (table snapshots uniquement, aucune credential ni donnée de trade)
- `scripts/backtest.py` — fallback automatique vers `data/backtest_sample_btc5m_range_2026.db` si `TRADINEBOTTE_DIR/live.db` est absent ; permet de lancer le backtest sur n'importe quelle machine sans base de données du bot live

### Performance
- `bot/live_bot.py` — `MARKET_REFRESH` réduit de 90 s à 30 s : le bot découvre désormais les nouveaux marchés au plus 30 s après leur entrée dans la fenêtre ±6 minutes, au lieu de 90 s maximum ; l'appel Gamma API reste une requête unique (filtre tag_id=102892, sans pagination), la surcharge est négligeable

### Documentation
- `INSTALL.md` / `INSTALL.fr.md` — CLI sqlite3 ajouté aux prérequis avec note indiquant que le bot fonctionne sans lui ; commande Python de remplacement fournie pour les hôtes sans sudo

### Fonctionnalité
- `strategies/polymarket_BTC5M.json` — nouveau fichier de stratégie : tous les paramètres de signal et de capital backtestés (`signal_threshold`, `entry_max`, `min_secs_remaining`, `min_ask_vol`, `win_threshold`, `loss_threshold`, `obi_reject_thresh`, `daily_stop_loss`, `stake`, `capital_start`, `gas_fee_usd`) extraits des constantes en dur vers un fichier JSON versionné ; ajouter `"strategy": "<chemin>"` dans `config.json` pour changer de stratégie
- `bot/live_bot.py` — `load_strategy()` charge le fichier JSON au démarrage ; les paramètres surchargent les valeurs par défaut ; fallback silencieux sur les valeurs par défaut si le fichier est absent (dev/tests) ; nom de la stratégie loggé au démarrage
- `config.json.example` — nouvelle clé optionnelle `strategy` pointant vers le fichier de stratégie
- `scripts/install.sh` — copie `strategies/*.json` dans le répertoire d'installation
- `scripts/install.sh` — nouveau flag `--with-tests` : copie `tests/` et `scripts/backtest.py` dans le répertoire d'installation et lance immédiatement la suite complète de 99 tests ; fonctionne avec n'importe quel chemin d'installation ; usage : `bash scripts/install.sh ~/polymarket --with-tests`
- `INSTALL.md` / `INSTALL.fr.md` — option `--with-tests` documentée

### Refactorisation
- `bot/api_polymarket.py` — nouvel adaptateur API Polymarket : tout le code spécifique à l'exchange extrait de `live_bot.py` dans un module dédié (`get_markets`, `post_order`, `parse_book_update`, `compute_fee`, `get_market_id/question/end_ts/start_ts/up_token/down_token`, `make_subscribe_msg`, `WS_URL`, `WS_BATCH_SIZE`, `FEE_RATE`). Pour cibler un autre exchange à l'avenir, il suffira de créer `api_<exchange>.py` avec la même interface publique et de modifier l'unique ligne d'import dans `live_bot.py`.
- `bot/live_bot.py` — importe `api_polymarket as api` ; toutes les constantes et fonctions spécifiques Polymarket remplacées par des appels `api.xxx()` ; `register_market` utilise `api.get_market_id/question/etc.` ; `enter_live_trade` appelle `api.compute_fee` et `api.post_order` ; la boucle WebSocket utilise `api.WS_URL`, `api.WS_BATCH_SIZE`, `api.make_subscribe_msg`, `api.get_markets`, `api.parse_book_update` ; imports de haut niveau inutilisés supprimés (`hashlib`, `hmac`, `uuid`)
- `tests/test_bot.py` — `TestComputeFee`, `TestParseBookMessage`, `TestMarketHelpers` importent désormais depuis `api_polymarket` directement ; l'utilitaire `insert_trade` utilise `api_poly.compute_fee`
- `scripts/install.sh` — copie `bot/api_polymarket.py` aux côtés de `live_bot.py` ; vérification syntaxique étendue aux deux fichiers

### Documentation
- `docs/status_example.html` — aperçu HTML statique de la page de statut web ; illustre la mise en page en thème sombre, les cartes de métriques (capital, PnL, taux de victoire, trades, stats journalières, positions ouvertes) et le tableau des trades résolus avec coloration WIN/LOSS
- `README.md` / `README.fr.md` — lien vers `docs/status_example.html` ajouté dans la ligne de fonctionnalité "Page de statut HTML optionnelle"
- `INSTALL.md` / `INSTALL.fr.md` — référence à `docs/status_example.html` ajoutée dans la section "Page de statut web"
- `README.md` / `README.fr.md` — section Fonctionnalités ajoutée listant les 11 capacités du bot avec descriptions en une ligne
- `INSTALL.md` / `INSTALL.fr.md` — section "Page de statut web" développée avec les prérequis Apache pas-à-pas : `a2enmod userdir auth_basic authn_file`, directive `AllowOverride AuthConfig`, deux options pour donner au processus `www-data` l'accès en lecture au `.htpasswd` (`chmod o+r` ou `usermod -aG`) ; note nginx expliquant que `.htaccess` n'est pas traité et montrant le bloc server équivalent avec `auth_basic` / `auth_basic_user_file` ; note sur les chemins personnalisés en dehors de `~/public_html`
- `README.md` / `README.fr.md` — entrées du tableau de configuration `webstatuspage_*` mises à jour pour mentionner les modules Apache requis, `AllowOverride AuthConfig`, les permissions `www-data`, et la configuration manuelle nginx dans la colonne description

### Fonctionnalité
- `bot/live_bot.py` — nouvelle page de statut web : si `webstatuspage_html` vaut `true` dans `config.json`, le bot écrit une page HTML autonome en thème sombre affichant le capital, le PnL total et journalier, le taux de victoire, les positions ouvertes et les 10 derniers trades résolus ; la page est écrite toutes les `DASHBOARD_INTERVAL` secondes (5 min) et immédiatement après chaque résolution de trade ; le répertoire est créé automatiquement ; la page inclut un `<meta http-equiv="refresh" content="60">` pour le rechargement automatique dans le navigateur
- `bot/live_bot.py` — protection `.htaccess` / `.htpasswd` Basic Auth : si `webstatus_password` est défini, `setup_htaccess()` écrit un `.htpasswd` (format Apache `{SHA}`, sans dépendance externe) dans `TRADINEBOTTE_DIR` (hors de la racine web) et un `.htaccess` pointant vers lui dans le répertoire de la page HTML ; les changements de mot de passe sont appliqués à la prochaine écriture de la page ; le `.htaccess` n'est écrit qu'une fois et non écrasé s'il existe déjà pour préserver les modifications manuelles
- `config.json.example` — quatre nouvelles clés optionnelles : `webstatuspage_html` (booléen, défaut `false`), `webstatuspage_path` (chaîne, défaut `~/public_html/tradinebot_status.html`), `webstatus_user` (chaîne, défaut `"tradinebot"`), `webstatus_password` (chaîne, défaut `""`)

### Performance
- `bot/live_bot.py` — `_market_refresh_loop()` extrait en tâche `asyncio.Task` de fond : le polling de l'API Gamma (jusqu'à 15 s de timeout HTTP toutes les 90 s) ne bloque plus le traitement des messages WebSocket ; la boucle `recv()` et la découverte des marchés s'exécutent désormais en concurrence dans le même event loop
- `bot/live_bot.py` — `_run_ws()` : timeout recv réduit de 90 s à 30 s ; `TimeoutError` déclenche maintenant `continue` au lieu d'une reconnexion complète — les keepalives `ping_interval=20` / `ping_timeout=10` détectent les connexions mortes ; la reconnexion ne s'active que lorsque tous les marchés suivis ont expiré ; le bloc `finally` garantit l'annulation de la tâche de refresh à toute déconnexion WebSocket
- `bot/live_bot.py` — `fetch_markets()` : `tag_id=102892` (tag Polymarket `5M`) ajouté à `POLY_GAMMA_PARAMS` pour un pré-filtrage côté serveur ; la fenêtre temporelle ±6 minutes retourne désormais ~12–20 marchés au lieu de potentiellement des milliers ; la boucle de pagination (jusqu'à 20 requêtes × 100 marchés) remplacée par un seul appel API ; le filtre Python `BTC_5M_KEYWORDS` conservé comme filet de sécurité
- `README.md` / `README.fr.md` — section Notes mise à jour pour refléter le timeout à 30 s, la tâche de refresh en fond et le filtre tag de l'API Gamma

### Fonctionnalité
- `scripts/backtest.py` — moteur de backtest autonome : rejoue la table `snapshots` chronologiquement, applique la logique de signal configurable et produit des statistiques de trades simulés ; mode `--sweep` pour une recherche en grille (5×3×3×3 = 135 combinaisons de paramètres triées par taux de victoire), `--detail` pour un tableau trade par trade, et `--compare` pour afficher les résultats réels du bot en regard de la simulation ; configurable via le dataclass `Params` (`signal_threshold`, `entry_max`, `min_secs_remaining`, `min_ask_vol`, `win_threshold`, `loss_threshold`, `obi_reject_thresh`, `stake`, `daily_stop_loss`)
- `tests/test_backtest.py` — 28 tests pour le moteur de backtest : `TestFeeHelper` (2), `TestRunBacktestBasic` (14, couvrant toutes les gardes du signal et les chemins de résolution), `TestRunBacktestMultiMarket` (4, marchés indépendants, isolation de direction, résolution à l'expiration), `TestRunBacktestDailyStopLoss` (1), `TestRunBacktestParams` (3, sensibilité aux seuils win/loss), `TestSummarize` (6, drawdown, taux de victoire, comptage des trades ouverts)
- `tests/test_bot.py` — suite de tests automatisés (71 tests, aucun service externe requis) : `TestComputeFee` (4), `TestParseBookMessage` (14), `TestMarketHelpers` (9), `TestTokenState` (7), `TestRegisterMarket` (5), `TestCheckSignal` (13 gardes dont les 8 conditions d'entrée, le stop-loss journalier et la prévention des doublons), `TestCheckResolution` (7), `TestCloseTrade` (6), `TestRestoreState` (5) ; tous les tests utilisent une base SQLite en mémoire et un `TRADINEBOTTE_DIR=/tmp/polymarket-test` fixe pour ne jamais toucher les fichiers de production
- `scripts/run_tests.sh` — le lanceur de tests supprime désormais les `ResourceWarning` Python 3.13 pour les connexions SQLite en mémoire non fermées (`-W ignore::ResourceWarning`) ; suite totale : 99 tests
- `config.json` / `config.json.example` — nouvelle clé optionnelle `db_mmap_mb` (entier, défaut `0`) : quand elle est non nulle, active `PRAGMA mmap_size` pour que SQLite mappe le fichier de base de données via le page cache du kernel ; mettre à ex. `256` pour 256 Mo
- `bot/live_bot.py` — `load_config()` refactorisée pour retourner le dict de config complet (extensible pour de futures options) ; `DB_MMAP_MB` dérivé de la config au démarrage ; `init_db()` applique le pragma et enregistre une ligne de confirmation dans les logs quand le mmap est actif

### Documentation
- `README.md` — nouvelle section "Database" : justification SQLite/WAL, schéma complet de la table `trades` (29 colonnes avec type et description), schéma de la table `snapshots`, et 4 exemples de requêtes commentés
- `README.fr.md` — traduction française de la nouvelle section Base de données
- `bot/live_bot.py` — docstring du module traduit en anglais ; docstrings ajoutées à toutes les fonctions et classes ; commentaires inline expliquant les invariants non évidents : rôle du filtre temporel, les 8 gardes du signal (dont la garde `ask_vol=0` d'initialisation et la garde `best_ask>=1.0` pour les marchés expirés), la formule OBI, le calcul du PnL, la résolution dynamique du chemin sysconfig, l'import paresseux de ClobClient, le backoff exponentiel, le nettoyage des marchés expirés, et le mode journal WAL
- `scripts/setup.py` — docstring du module traduit en anglais ; commentaires inline expliquant les décisions de sécurité (getpass, approbations ERC-20 à montant exact), les paramètres du swap Uniswap V3 (fee tier 100, garde slippage 0,5%, deadline 5 min), le chemin dynamique sysconfig, la dérivation ECDSA des clés API, et le chmod 600

### Fonctionnalité
- La variable d'environnement `TRADINEBOTTE_DIR` contrôle désormais le chemin d'installation dans tous les scripts et dans le bot, avec `/opt/polymarket-live` comme valeur par défaut
- `scripts/install.sh` — accepte le répertoire d'installation en argument positionnel ou via `TRADINEBOTTE_DIR` ; génère un wrapper `run.sh` dans le répertoire d'installation avec le chemin pré-défini
- `scripts/start_bot.sh` — lit `TRADINEBOTTE_DIR` et l'exporte lors du lancement du bot
- `scripts/monitor.sh` — lit `TRADINEBOTTE_DIR` pour les chemins des logs et de la base de données
- `scripts/setup.py` — lit `TRADINEBOTTE_DIR` pour le chemin de `config.json` et les site-packages du venv ; corrige également le chemin venv codé en dur pour Python 3.12 (utilise `sysconfig` comme le bot)
- `bot/live_bot.py` — `DB_PATH`, `LOG_PATH`, `CONFIG_PATH` et la recherche du venv sont tous dérivés de `TRADINEBOTTE_DIR`

### Documentation
- `INSTALL` — nouveau guide d'installation en anglais extrait du README.md (prérequis, dépendances, configuration du wallet, lancement, monitoring, test en environnement virtuel)
- `INSTALL.fr` — traduction française du guide d'installation
- `README.md` — sections d'installation remplacées par une référence au fichier INSTALL
- `README.fr.md` — sections d'installation remplacées par une référence au fichier INSTALL.fr

---

## [2026-04-23]

### Sécurité — `9e6247c`
**Correctifs de sécurité suite à l'audit (4 vulnérabilités)**
- `scripts/setup.py` — clé privée lue via `getpass()` au lieu de `sys.argv[1]` : n'apparaît plus dans `ps aux` ni dans l'historique shell
- `scripts/setup.py` — suppression de tout affichage de credentials sur stdout (clé privée partielle, api_key, api_secret, api_passphrase) : la sortie n'affiche plus que l'adresse du wallet
- `scripts/setup.py` — remplacement des approbations ERC-20 illimitées (`2**256-1`) par des montants exacts : `amount_in` pour le swap Uniswap V3, `bal_e` pour le CTF Exchange

### Documentation — `6aa8360`
**Ajout des instructions de test en environnement virtuel dans le README**
- `README.md` — ajout d'une section complète "Testing in a virtual environment" avec toutes les commandes : installation de `uv`, création du venv, vérification de syntaxe, import check, dry-run de 20 secondes, sortie attendue

### Correction de bug — `3c0ad40`
**Correction d'une erreur d'indentation dans le bloc except WebSocket et de la version Python codée en dur**
- `bot/live_bot.py` — correction d'une `IndentationError` sur le bloc `except:` dans `_run_ws()` (10 espaces au lieu de 12) qui empêchait le démarrage du bot
- `bot/live_bot.py` — le chemin des site-packages du venv était codé en dur pour Python 3.12 ; remplacé par `sysconfig.get_path()` qui résout le bon chemin dynamiquement selon la version Python installée

### Fonctionnalité — `cbdbf2a`
**Migration des credentials des variables d'environnement vers config.json**
- `bot/live_bot.py` — ajout de `CONFIG_PATH` et d'une fonction `load_config()` qui lit `/opt/polymarket-live/config.json` en priorité, avec fallback sur les variables d'environnement
- `scripts/setup.py` — écrit automatiquement `config.json` (chmod 600) après dérivation des clés API, au lieu d'afficher des `export` à copier manuellement
- `scripts/start_bot.sh` — suppression des `export` de variables ; vérifie l'existence de `config.json` au démarrage
- `config.json.example` — template de référence ajouté au dépôt
- `.gitignore` — `config.json` ajouté pour éviter tout commit accidentel de credentials

### Documentation — `f452161`
**Rédaction complète du README**
- `README.md` — réécriture complète avec description de la stratégie, prérequis, dépendances, installation, configuration, monitoring et notes opérationnelles

### Documentation — `beed5e1`
**Ajout du fichier CLAUDE.md avec l'architecture et les consignes opérationnelles**
- `CLAUDE.md` — documentation de l'architecture pour Claude Code : commandes, flux de données, paramètres critiques à ne pas modifier, décisions de conception, chemins de déploiement

### CI — `d225a5f`
**Ajout du workflow GitHub Actions pour Claude Code**
- `.github/workflows/claude.yml` — workflow permettant de déclencher Claude Code depuis les issues et pull requests GitHub

### Initial — `85886ea`
**Import de la base — bot de trading Polymarket v3**
- `bot/live_bot.py` — bot async complet (617 lignes) : state machine WebSocket, signal `best_bid >= 0.96`, placement d'ordres LIMIT via `py_clob_client`, résolution WIN/LOSS, persistance SQLite (WAL), reconnexion automatique avec backoff exponentiel
- `scripts/install.sh` — installation des dépendances système et création du venv `/opt/polymarket-live/venv`
- `scripts/setup.py` — setup wallet : vérification des balances, swap USDC natif → USDC.e via Uniswap V3, approbation CTF Exchange, dérivation des clés API Polymarket
- `scripts/start_bot.sh` — script de lancement avec vérification des prérequis et gestion d'instance unique
- `scripts/monitor.sh` — dashboard de monitoring : statut du processus, logs temps réel, stats SQLite (trades, win rate, PnL)
- `docs/CONTEXT_AI.md` — documentation technique complète : stratégie, architecture, historique des bugs corrigés, résultats de backtest (1663 trades, 98,3% de taux de victoire), checklist de déploiement

### Initial — `7c119fd`
**Premier commit**
- Initialisation du dépôt git
