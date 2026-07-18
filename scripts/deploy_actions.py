#!/usr/bin/env python3
"""deploy_actions.py — Phase B of the deploy-engine refactor (docs/deploy-engine-design.md).

The native action library: small, idempotent, typed steps that a bot deploys through,
driven by DECLARATIVE inventory fields instead of a ~300-line per-family bash script. It is
now the pipeline's deployer for the single-tree families (accumulation + binance grid — the
old deploy_accumulation.sh / deploy_grid_binance.sh were retired), and parallels the remaining
bash deploy_grid_mexc.sh, with the design's improvements:

  * verify via the service's systemd **MainPID** (no `pgrep -f` self-match),
  * `record_deploy` under the generated **bot_id** (no hardcoded name → no journal drift),
  * **pip-skip** when requirements.txt is unchanged (hash stamp),
  * a declarative **sync set** (`FILE_SETS[family]`) instead of an inline rsync block.

CLI (pilot / test-account):
  python3 scripts/deploy_actions.py grid --idx N --strategy strategies/grid/<f>.json
                                    [--dir ~/tradinebotte] [--connector mexc] [--verify-only]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONF = os.environ.get("TEST_MULTIBOT_CONF", os.path.expanduser("~/.tradinebotte-test.conf"))
C = {"g": "\033[0;32m", "r": "\033[0;31m", "y": "\033[1;33m", "n": "\033[0m"}


def _c(k, s):  # colour if tty
    return f"{C[k]}{s}{C['n']}" if sys.stdout.isatty() else s


def _rp(p: str) -> str:
    """Remote path safe to embed UNQUOTED in a shell command, tilde-expandable. Our paths
    are '~/...' with no spaces, so map '~' → $HOME (bash expands it). shlex.quote() would
    single-quote the tilde and defeat expansion → commands would hit a literal '~' dir."""
    return "$HOME" + p[1:] if p.startswith("~") else p


# test-account self-contained quicktest: infra binds host-wide singleton loopback ports owned by
# prod (5557/5559/5561/5562/5563), so a test copy can't run on the shared host. A UNIFORM +10
# offset moves the whole stack to a free range (5567–5573) → zero prod collision. +10 is
# self-safe (no overlap with the 5557–5563 base). One constant, applied to every TCP bind.
TEST_PORT_OFFSET = 10


def _offset_addr(addr: str | None, off: int) -> str | None:
    """tcp://host:PORT → tcp://host:PORT+off. IPC/None untouched (per-user, already isolated)."""
    if not addr or not addr.startswith("tcp://"):
        return addr
    host, _, port = addr.rpartition(":")
    return f"{host}:{int(port) + off}"


def _test_env(spec: dict) -> dict:
    """The offset addr env for an infra service under --test-ports ({} for IPC services). Value is
    a bare number for TRADINEBOTTE_PORT_BASE (indicators), else a tcp:// addr."""
    pe = spec.get("port_env")
    if not pe:
        return {}
    port = spec["base_port"] + TEST_PORT_OFFSET
    return {pe: (f"tcp://127.0.0.1:{port}" if spec.get("port_is_addr", True) else str(port))}


# Common code sync for every live_bot family (grid/swing/accumulation/polymarket all run
# live_bot.py from ~/tradinebotte). (local path, remote subpath, [excludes]). The per-family
# systemd unit template + the strategy JSONs are pushed separately in act_sync.
_BASE_SYNC: list[tuple[str, str, list[str]]] = [
    ("tradinebotte-polymarket/", "", ["scripts", "tests", "live.db", "*.log", "venv", ".venv"]),
    ("tradinebotte-cex/", "", ["scripts", "tests", "api_bitstamp.py"]),
    ("tradinebotte-core/botcore/", "botcore/", []),
    ("tradinetools/", "tradinetools/", ["*.egg-info"]),
    ("requirements.txt", "requirements.txt", []),
]

# Per-family deploy spec — the declarative data that replaces each ~300-line bash engine.
#   config_mode "write" = overwrite config with {strategy, data_source[, feed_addr]}. EVERY family is
#   "write" now: keys/wallet live in a 600 env file loaded by the systemd unit (MEXC_API_KEY for CEX,
#   POLY_PRIVATE_KEY for polymarket — read via env fallback in live_bot.make_config), NEVER in config.json.
#   So config is self-contained and single-tree migration carries no wallet (the old poly "merge" mode,
#   which kept a wallet in config.json, is gone — it was the last thing blocking poly from converging).
#   data_suffix = data dir relative to install_dir ("" = same dir; "-accum" = ~/tradinebotte-accum).
FAMILIES: dict[str, dict] = {
    "grid":         dict(role="grid", unit="tradinebotte-live.service",
                         template="tradinebotte-live.service", data_suffix="",
                         connector="mexc", config_mode="write",
                         data_source="cex_feed", feed_addr="tcp://127.0.0.1:5563"),
    # Binance grid: its OWN unit (tradinebotte-grid.service) + legacy data dir ~/tradinebotte-grid,
    # so it cohabits with the account's poly (live.service) + accum (accumulation.service) without
    # clobbering them. role="grid" (== strategy_type) keeps the single-tree instance + bot_id_<role>
    # coherent — safe because no account runs BOTH mexc-grid and binance-grid (verified in inventory),
    # so config_grid.json / live_grid.db never collide across the two grid families in one tree.
    "grid_binance": dict(role="grid", unit="tradinebotte-grid.service",
                         template="tradinebotte-grid.service", data_suffix="-grid",
                         connector="binance", config_mode="write",
                         data_source="cex_feed", feed_addr="tcp://127.0.0.1:5563"),
    "swing":        dict(role="swing", unit="tradinebotte-live.service",
                         template="tradinebotte-live.service", data_suffix="",
                         connector="binance", config_mode="write",
                         data_source="cex_feed", feed_addr="tcp://127.0.0.1:5563"),
    "accumulation": dict(role="accumulation", unit="tradinebotte-accumulation.service",
                         template="tradinebotte-accumulation.service", data_suffix="-accum",
                         connector="mexc", config_mode="write",
                         data_source="indicators", feed_addr=None,
                         # legacy accum DB is live_accum.db (not live.db) — see live_bot make_config;
                         # the P4 migration renames it to live_accumulation.db under single-tree.
                         legacy_db="live_accum.db"),
    # write-mode like every other family: the wallet is read from POLY_PRIVATE_KEY (a 600 env file
    # loaded by the unit), not config.json — so poly converges to single-tree natively (no merge gap).
    "polymarket":   dict(role="threshold", unit="tradinebotte-live.service",
                         template="tradinebotte-live.service", data_suffix="",
                         connector="polymarket", config_mode="write",
                         data_source="feed", feed_addr="tcp://127.0.0.1:5557"),
}


# Per-infra-service deploy spec (Phase D). Infra runs on the infra account (acct-1) and is
# configured via the systemd UNIT env, not config.json — so there is NO config step here.
# Crucially the deploy NEVER rewrites the unit (act_service_restart is install-if-absent):
# that mirrors bash update_claude1.sh (_restart_service = rsync .py + restart, unit untouched)
# and is what preserves hand-set remote env like the 15M feed's TRADINEBOTTE_MARKET_TAG_ID
# (102467, absent from the git template — see docs/deploy-engine-design §Phase D).
#   tdir      = repo dir holding the systemd template (varies: cex/status live outside polymarket)
#   extra     = entry .py not covered by _BASE_SYNC (indicators/status live in their own packages)
#   role      = bot_id_<role> to read back; "" = no generated bot_id (status uses a fixed bot_name)
#   data_dir  = where bot_id_<role> + <log> live (feed5m logs to ~/feed5m; the rest to ~/tradinebotte)
#   port_env  = env var that sets this service's TCP bind, for the test-account +10 offset drop-in.
#               None = per-user IPC (feed 15m / account), never offset. base_port = the prod port;
#               port_is_addr=False for indicators' TRADINEBOTTE_PORT_BASE (wants a bare number, and
#               shifts all 3 of its addrs — feed/out/reg — + the config addrs by one env var).
INFRA: dict[str, dict] = {
    "indicators": dict(unit="tradinebotte-indicators.service", template="tradinebotte-indicators.user.service",
                       tdir="tradinebotte-polymarket/scripts/systemd", role="indicators",
                       port_env="TRADINEBOTTE_PORT_BASE", base_port=5557, port_is_addr=False,
                       data_dir="~/tradinebotte", log="indicators.log",
                       extra=[("tradinebotte-indicators/indicators.py", "indicators.py", []),
                              # indicators.py --config strategies/indicators/indicators_all.json:
                              # its config lives in its own package, flattened into strategies/indicators/.
                              ("tradinebotte-indicators/strategies/", "strategies/indicators/", [])]),
    "feed":       dict(unit="tradinebotte-feed.service", template="tradinebotte-feed15m.service",
                       tdir="tradinebotte-polymarket/scripts/systemd", role="feed", port_env=None,
                       data_dir="~/tradinebotte", log="feed.log", extra=[]),
    "feed5m":     dict(unit="tradinebotte-feed5m.service", template="tradinebotte-feed5m.service",
                       tdir="tradinebotte-polymarket/scripts/systemd", role="feed5m",
                       port_env="TRADINEBOTTE_FEED_ADDR", base_port=5557, port_is_addr=True,
                       data_dir="~/feed5m", log="feed.log", extra=[]),
    "cexfeed":    dict(unit="tradinebotte-cexfeed.service", template="tradinebotte-cexfeed.service",
                       tdir="tradinebotte-cex/scripts/systemd", role="cexfeed",
                       port_env="TRADINEBOTTE_CEX_FEED_ADDR", base_port=5563, port_is_addr=True,
                       data_dir="~/tradinebotte", log="cex_feed.log", extra=[]),
    "status":     dict(unit="tradinebotte-status.service", template="tradinebotte-status.service",
                       tdir="tradinebotte-status/scripts/systemd", role="", bot_name="status_collector",
                       port_env="TRADINEBOTTE_STATUS_ADDR", base_port=5562, port_is_addr=True,
                       data_dir="~/tradinebotte", log="status.log",
                       extra=[("tradinebotte-status/status_collector.py", "status_collector.py", []),
                              ("tradinebotte-status/heartbeat_query.py", "heartbeat_query.py", [])]),
}


# bot_type → native deploy target, as (kind, target) with kind ∈ {"family","infra"}. This is the
# Phase-E bridge that lets the engine dispatch an inventory row to deploy_family/deploy_infra instead
# of a bash script. Ordered: the most specific prefix wins (polymarket-multibot before polymarket*;
# infra-feed-15m/5m before any generic "feed"). Returns None for an unknown bot_type (check_inventory
# flags it, and the engine falls back to the row's bash deployer — coexistence, not a hard failure).
_NATIVE_TARGET_RULES: list[tuple[str, tuple[str, str]]] = [
    ("infra-cex-feed",     ("infra", "cexfeed")),
    ("infra-feed-15m",     ("infra", "feed")),
    ("infra-feed-5m",      ("infra", "feed5m")),
    ("infra-indicators",   ("infra", "indicators")),
    ("infra-status",       ("infra", "status")),
    ("polymarket",         ("family", "polymarket")),
    ("cex-accumulation",   ("family", "accumulation")),
    ("cex-grid-binance",   ("family", "grid_binance")),   # before cex-grid (startswith) — own unit/dir
    ("cex-grid",           ("family", "grid")),
    ("cex-swing",          ("family", "swing")),
]


def native_target(bot_type: str) -> tuple[str, str] | None:
    """(kind, target) for an inventory bot_type, or None if no native deployer covers it yet.
    kind='family' → deploy_family(target); kind='infra' → deploy_infra(target)."""
    bt = (bot_type or "").strip().lower()
    for prefix, kt in _NATIVE_TARGET_RULES:
        if bt.startswith(prefix):
            return kt
    return None


def load_conf() -> dict:
    """(server, port, users[], passwords[]) from the untracked test conf via bash (arrays)."""
    def arr(var):
        out = subprocess.run(["bash", "-c", f'source "{CONF}"; printf "%s\\n" "${{{var}[@]}}"'],
                             capture_output=True, text=True)
        return [x for x in out.stdout.splitlines() if x]
    def sca(var):
        out = subprocess.run(["bash", "-c", f'source "{CONF}"; printf "%s" "${{{var}}}"'],
                             capture_output=True, text=True)
        return out.stdout.strip()
    sidx = sca("TEST_STANDALONE_USER_IDX")
    return {"server": sca("TEST_SERVER"), "port": sca("TEST_PORT") or "22",
            "users": arr("TEST_USERS"), "passwords": arr("TEST_PASSWORDS"),
            # the ephemeral test account (never a prod bot) — resolved at runtime, no name in git
            "standalone_idx": int(sidx) if sidx.isdigit() else None}


class Host:
    """One SSH/rsync target (an account). All process-targeting uses systemd, never pgrep."""
    SSH_OPTS = ["-o", "StrictHostKeyChecking=yes", "-o", "PreferredAuthentications=password",
                "-o", "ConnectTimeout=20",
                # Exclude ALL FIDO security-key host-key types from negotiation. A bloated/mixed
                # known_hosts can otherwise make ssh offer sk-* for the server, which the server has no
                # matching host key for → "no matching host key type" preauth failure (seen in
                # auth.log). '-sk-*' removes every sk-* variant, keeping ed25519/ecdsa/rsa.
                "-o", "HostKeyAlgorithms=-sk-*"]

    def __init__(self, user: str, password: str, server: str, port: str):
        self.user, self.password, self.server, self.port = user, password, server, port

    def _base_env(self):
        return {**os.environ, "SSHPASS": self.password}

    def ssh(self, script: str) -> subprocess.CompletedProcess:
        cmd = ["/usr/bin/sshpass", "-e", "ssh", "-p", self.port, *self.SSH_OPTS,
               f"{self.user}@{self.server}",
               "export XDG_RUNTIME_DIR=/run/user/$(id -u); " + script]
        return subprocess.run(cmd, capture_output=True, text=True, env=self._base_env())

    def rsync(self, local: str, remote: str, excludes: list[str]) -> int:
        ex = []
        for e in ["__pycache__", "*.pyc", "bot_id*", *excludes]:
            ex += ["--exclude", e]
        cmd = ["/usr/bin/sshpass", "-e", "rsync", "-az", *ex,
               "-e", f"ssh -p {self.port} {' '.join(self.SSH_OPTS)}",
               local, f"{self.user}@{self.server}:{remote}"]
        # Retry once on failure: a transient SSH/network blip on the shared host otherwise fails the
        # whole act_sync (assert) and, mid-fleet, took down a step. rsync is idempotent, so a retry
        # is safe. (Root cause of the first prod native-infra run's transient status-sync failure.)
        rc = subprocess.run(cmd, capture_output=True, text=True, env=self._base_env()).returncode
        if rc != 0:
            time.sleep(2)
            rc = subprocess.run(cmd, capture_output=True, text=True, env=self._base_env()).returncode
        return rc


# ── Actions ─────────────────────────────────────────────────────────────────────────

def act_sync(host: Host, install_dir: str, template: str) -> bool:
    ok = True
    for local, sub, ex in _BASE_SYNC:
        rc = host.rsync(os.path.join(REPO, local), f"{install_dir}/{sub}", ex)
        ok = ok and rc == 0
    # the family's systemd unit template (excluded from _BASE_SYNC's 'scripts')
    ok = ok and host.rsync(
        os.path.join(REPO, "tradinebotte-polymarket/scripts/systemd", template),
        f"{install_dir}/{template}", []) == 0
    # strategy JSONs (filtered) — every family reads strategies/*.json
    rc = subprocess.run(["/usr/bin/sshpass", "-e", "rsync", "-az",
                         "--include", "*/", "--include", "*.json", "--exclude", "*",
                         "-e", f"ssh -p {host.port} {' '.join(Host.SSH_OPTS)}",
                         os.path.join(REPO, "tradinebotte-cex/strategies/"),
                         f"{host.user}@{host.server}:{install_dir}/strategies/"],
                        capture_output=True, text=True, env=host._base_env()).returncode
    return ok and rc == 0


def act_config(host: Host, data_dir: str, config: dict, mode: str = "write",
               config_name: str = "config.json") -> bool:
    """Write the bot's config to {data_dir}/{config_name}. config_name is 'config.json' on the
    legacy layout and 'config_<instance>.json' under single-tree (matches live_bot.instance_paths)."""
    if mode == "merge":
        # Preserve an existing config (e.g. the Polymarket wallet setup); set only the
        # given keys. Run via python heredoc so quoting/tilde are handled in-process.
        py = (f"import json,os\n"
              f"p=os.path.expanduser('{data_dir}/{config_name}')\n"
              f"c=json.load(open(p)) if os.path.exists(p) else {{}}\n"
              f"c.update({json.dumps(config)})\n"
              f"json.dump(c,open(p,'w'),indent=2); print('cfg-merged')")
        r = host.ssh(f"$HOME/tradinebotte/.venv/bin/python3 - <<'PYEOF'\n{py}\nPYEOF")
        return "cfg-merged" in r.stdout
    body = json.dumps(config, indent=4)
    r = host.ssh(f"mkdir -p {_rp(data_dir)} && cat > {_rp(data_dir)}/{config_name} <<'EOJSON'\n{body}\nEOJSON\necho ok")
    return "ok" in r.stdout


def act_deps_and_tradinetools(host: Host, install_dir: str) -> bool:
    """pip install ONLY when requirements.txt changed (hash stamp); always refresh tradinetools
    (cheap, and it carries new symbols like resolve_bot_id). Returns import-sanity."""
    script = f"""
    cd {_rp(install_dir)} || exit 1
    V=.venv; [ -d venv ] && V=venv
    H=$(sha256sum requirements.txt 2>/dev/null | cut -d' ' -f1)
    # Skip pip ONLY when the requirements hash matches AND a core dep actually imports —
    # so a recreated/empty venv (stamp stale) is NOT wrongly skipped (else the bot crashes
    # at restart on a missing dep). Empty hash never skips.
    if [ -n "$H" ] && [ "$H" = "$(cat .deps_hash 2>/dev/null)" ] && "$V/bin/python3" -c 'import aiohttp, zmq' 2>/dev/null; then
        echo "deps:skipped(unchanged)"
    else
        "$V/bin/pip" install --quiet -r requirements.txt 2>/dev/null && echo "$H" > .deps_hash && echo "deps:installed" || echo "deps:pip-failed"
    fi
    PY=$("$V/bin/python3" -c 'import sys;print(f"{{sys.version_info.major}}.{{sys.version_info.minor}}")')
    S=$V/lib/python$PY/site-packages
    # Import tradinetools straight from the rsynced source via a plain path .pth — no vendored
    # copy. The old `cp -r` into site-packages went stale between deploys and forced the "refresh
    # tradinetools before restart or it crashloops" ritual; a .pth has no copy to drift (this rsync
    # is instantly live) and no dist-info (which historically broke restarts). Strip any prior
    # copy/editable first: a real dir in site-packages wins over the .pth path entry and would
    # shadow it. Skip if the rsynced source is missing (never wipe a working install for a bad source).
    if [ -d tradinetools/tradinetools ]; then
        rm -rf "$S/tradinetools" "$S/tradinetools.new" "$S"/tradinetools-*.dist-info "$S"/__editable__*tradinetools* "$S"/*tradinetools*.pth
        echo "$(pwd)/tradinetools" > "$S/tradinetools-source.pth"
    fi
    # Import test with one retry: on the flaky shared host the test itself can transiently fail
    # even when tradinetools is intact (false negative → whole deploy step fails).
    "$V/bin/python3" -c 'from tradinetools import resolve_bot_id' 2>/dev/null && echo "tt:ok" || \
        {{ sleep 1; "$V/bin/python3" -c 'from tradinetools import resolve_bot_id' 2>/dev/null && echo "tt:ok" || echo "tt:FAIL"; }}
    """
    r = host.ssh(script)
    print("   ", r.stdout.strip().replace("\n", " | "))
    return "tt:ok" in r.stdout


def act_service_restart(host: Host, install_dir: str, unit: str, template: str) -> bool:
    """Install the unit (from its template) if absent, write version.stamp, restart.
    systemd only — never the nohup fallback the bash engines carry."""
    githash = subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True).stdout.strip() or "unknown"
    script = f"""
    echo '{githash}' > {_rp(install_dir)}/version.stamp
    if ! systemctl --user is-enabled {unit} >/dev/null 2>&1; then
        mkdir -p ~/.config/systemd/user
        cp {_rp(install_dir)}/{template} ~/.config/systemd/user/{unit}
        systemctl --user daemon-reload; systemctl --user enable {unit}
        echo "svc:installed"
    fi
    systemctl --user restart {unit} && echo "svc:restarted"
    """
    r = host.ssh(script)
    return "svc:restarted" in r.stdout


def act_test_ports_dropin(host: Host, unit: str, env: dict) -> bool:
    """Offset a service's TCP bind via a systemd DROP-IN (a separate file, not a unit rewrite —
    keeps act_service_restart install-if-absent intact). Removes any stale drop-in when env is
    empty (idempotent: a service that stops being offset loses the override). Caller reloads
    before restart so the new port is picked up."""
    d = f"~/.config/systemd/user/{unit}.d"
    if not env:
        host.ssh(f"rm -f {_rp(d)}/test-ports.conf 2>/dev/null; systemctl --user daemon-reload 2>/dev/null; echo done")
        return True
    body = "[Service]\n" + "".join(f"Environment={k}={v}\n" for k, v in env.items())
    r = host.ssh(f"mkdir -p {_rp(d)} && cat > {_rp(d)}/test-ports.conf <<'EOF'\n{body}EOF\n"
                 f"systemctl --user daemon-reload && echo dropin-ok")
    return "dropin-ok" in r.stdout


def act_single_tree_dropin(host: Host, unit: str, instance: str) -> bool:
    """Point a unit at the single-tree layout via a systemd DROP-IN: TRADINEBOTTE_DIR=%h/tradinebotte
    + TRADINEBOTTE_INSTANCE=<instance>, so its live_bot writes config_<inst>.json / live_<inst>.db /
    <inst>.log into the ONE shared ~/tradinebotte (never colliding with a cohabiting bot). The drop-in
    OVERRIDES a template's own TRADINEBOTTE_DIR (e.g. the accum unit's %h/tradinebotte-accum): a
    repeated Environment= key resolves last-assignment-wins, and drop-ins apply after the main unit.
    A separate file (not a unit rewrite) keeps act_service_restart install-if-absent intact, orthogonal
    to test-ports.conf. Empty instance clears the drop-in (idempotent revert to the legacy layout)."""
    d = f"~/.config/systemd/user/{unit}.d"
    if not instance:
        host.ssh(f"rm -f {_rp(d)}/single-tree.conf 2>/dev/null; systemctl --user daemon-reload 2>/dev/null; echo done")
        return True
    body = ("[Service]\nEnvironment=TRADINEBOTTE_DIR=%h/tradinebotte\n"
            f"Environment=TRADINEBOTTE_INSTANCE={instance}\n")
    r = host.ssh(f"mkdir -p {_rp(d)} && cat > {_rp(d)}/single-tree.conf <<'EOF'\n{body}EOF\n"
                 f"systemctl --user daemon-reload && echo dropin-ok")
    return "dropin-ok" in r.stdout


def act_read_bot_id(host: Host, data_dir: str, role: str) -> str:
    r = host.ssh(f"cat {_rp(data_dir)}/bot_id_{role} 2>/dev/null")
    return r.stdout.strip()


def act_verify(host: Host, unit: str, log_path: str) -> tuple[bool, str]:
    """Running via systemd MainPID (never pgrep); no ERROR/CRITICAL in the recent log."""
    r = host.ssh(
        f"A=$(systemctl --user is-active {unit}); "
        f"P=$(systemctl --user show {unit} -p MainPID --value); "
        f"echo \"active=$A mainpid=$P\"; "
        f"E=$(tail -40 {_rp(log_path)} 2>/dev/null | grep -cE '\\[ERROR\\]|\\[CRITICAL\\]'); "
        f"echo \"errors=$E\"")
    out = r.stdout
    running = "active=active" in out and "mainpid=0" not in out
    clean = "errors=0" in out
    return (running and clean), out.strip().replace("\n", " ")


def act_migrate_single_tree(host: Host, legacy_dir: str, dest_dir: str, role: str,
                            legacy_db: str) -> bool:
    """P4 prod cutover: carry a bot's state from its legacy SEPARATE data dir into the shared
    single-tree dir under the per-instance names — the DB (renamed <legacy_db> → live_<role>.db,
    with its -wal/-shm so no committed-but-uncheckpointed rows are lost) and bot_id_<role> (so the
    id is REUSED, not regenerated → no heartbeat/statuspage orphan). COPY, never move: the legacy
    dir stays intact so revert = clear the drop-in + restart on the old dir. IDEMPOTENT: copy only
    when the destination is ABSENT, so a re-run is a no-op and never clobbers a now-live DB with the
    stale legacy one. The caller MUST stop the unit first (a live SQLite/-wal copy can be torn)."""
    src_db, dst_db = f"{legacy_dir}/{legacy_db}", f"{dest_dir}/live_{role}.db"
    src_id, dst_id = f"{legacy_dir}/bot_id_{role}", f"{dest_dir}/bot_id_{role}"
    script = f"""
    mkdir -p {_rp(dest_dir)}
    if [ -f {_rp(src_db)} ] && [ ! -e {_rp(dst_db)} ]; then
        cp -p {_rp(src_db)} {_rp(dst_db)}
        for ext in -wal -shm; do [ -f {_rp(src_db)}$ext ] && cp -p {_rp(src_db)}$ext {_rp(dst_db)}$ext; done
        echo "db:copied"
    elif [ -e {_rp(dst_db)} ]; then echo "db:skipped(dest-exists)"; else echo "db:skipped(no-src)"; fi
    if [ -f {_rp(src_id)} ] && [ ! -e {_rp(dst_id)} ]; then
        cp -p {_rp(src_id)} {_rp(dst_id)}; echo "botid:copied"
    elif [ -e {_rp(dst_id)} ]; then echo "botid:skipped(dest-exists)"; else echo "botid:skipped(no-src)"; fi
    """
    r = host.ssh(script)
    print("   ", r.stdout.strip().replace("\n", " | "))
    # success = we did not error; a skip (dest exists / no src) is a legitimate idempotent outcome
    return "db:" in r.stdout and "botid:" in r.stdout


def record_deploy(account: str, bot_id: str, ok: bool):
    githash = subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True).stdout.strip() or "unknown"
    subprocess.run(["python3", os.path.join(REPO, "tradinebotte-status/record_deploy.py"),
                    "--account", account, "--bot", bot_id, "--git-hash", githash,
                    "--script", "deploy_actions.py", "--result", "OK" if ok else "FAILED"],
                   check=False, capture_output=True)


# ── Native family deployer (grid / swing / accumulation / polymarket) ────────────────

def deploy_family(host: Host, family: str, *, install_dir: str, strategy: str = "",
                  verify_only: bool = False, test_ports: bool = False,
                  single_tree: bool = False, migrate: bool = False,
                  skip_restart: bool = False) -> int:
    spec = FAMILIES[family]
    unit, role, conn = spec["unit"], spec["role"], spec["connector"]
    assert not migrate or single_tree, "--migrate only makes sense with --single-tree"
    # single-tree (opt-in): every bot's data lives in the ONE ~/tradinebotte, per-instance suffixed
    # (config_<role>.json / live_<role>.db / <role>.log) so cohabiting bots never collide on the fixed
    # names; instance = role (== strategy_type for every family, so it also matches bot_id_<role>).
    # Legacy (default): data in install_dir+data_suffix, fixed config.json / live.db / live.log.
    if single_tree:
        data_dir, instance = install_dir, role
        config_name, log_name = f"config_{instance}.json", f"{instance}.log"
    else:
        data_dir, instance = install_dir + spec["data_suffix"], ""
        config_name, log_name = "config.json", "live.log"
    label = f"{host.user}/{family}" + ("  [single-tree]" if single_tree else "")
    if not verify_only:
        if migrate:
            # P4 cutover: STOP the old unit (consistent DB snapshot), then copy state (DB+bot_id)
            # from the legacy separate dir into the shared tree BEFORE the deploy restarts it there.
            legacy_dir = install_dir + spec["data_suffix"]
            print(_c("y", f"▶ {label}: migrate {legacy_dir} → {data_dir} (stop unit, copy state)"))
            host.ssh(f"systemctl --user stop {unit}")
            assert act_migrate_single_tree(host, legacy_dir, data_dir, role,
                                           spec.get("legacy_db", "live.db")), "migrate failed"
        print(_c("y", f"▶ {label}: sync"))
        assert act_sync(host, install_dir, spec["template"]), "sync failed"
        # write config for every family: strategy path is absolute when the data dir differs
        # from the code dir. (The wallet/keys never live here — they come from the unit's env.)
        strat = f"{install_dir}/{strategy}" if data_dir != install_dir else strategy
        cfg = {"strategy": strat, "data_source": spec["data_source"]}
        if spec["feed_addr"]:
            # test_ports: consumer points at the offset producer (grid/swing 5563→5573, poly 5557→5567)
            cfg["feed_addr"] = _offset_addr(spec["feed_addr"], TEST_PORT_OFFSET) if test_ports else spec["feed_addr"]
        print(_c("y", f"▶ {label}: config ({spec['config_mode']})"))
        assert act_config(host, data_dir, cfg, spec["config_mode"], config_name), "config failed"
        print(_c("y", f"▶ {label}: deps+tradinetools"))
        assert act_deps_and_tradinetools(host, install_dir), "tradinetools import failed"
        if single_tree:
            # write the TRADINEBOTTE_DIR/INSTANCE drop-in BEFORE restart so live_bot picks up the tree
            print(_c("y", f"▶ {label}: single-tree drop-in (instance={instance})"))
            assert act_single_tree_dropin(host, unit, instance), "single-tree drop-in failed"
        if skip_restart:
            # code/config refreshed but the bot is left running as-is (pipeline --skip-restart:
            # rsync-only, no disruption). Any drop-in written above takes effect on the next restart.
            print(_c("y", f"▶ {label}: skip-restart (code/config synced, unit NOT restarted)"))
        else:
            print(_c("y", f"▶ {label}: restart"))
            assert act_service_restart(host, install_dir, unit, spec["template"]), "restart failed"
            host.ssh("sleep 6")  # let it boot + generate bot_id
    bot_id = act_read_bot_id(host, data_dir, role) or f"{conn}-{family}-unknown"
    ok, detail = act_verify(host, unit, f"{data_dir}/{log_name}")
    mark = _c("g", "✓") if ok else _c("r", "✗")
    print(f"{mark} {label}  bot_id={bot_id}  [{detail}]")
    record_deploy(host.user, bot_id, ok)
    return 0 if ok else 1


# ── Native infra deployer (indicators / feed / feed5m / cexfeed / status / account) ──

def act_sync_infra(host: Host, install_dir: str, spec: dict) -> bool:
    """Base code (shared live_bot/cex packages + tradinetools) + any entry .py that lives in
    its own package (indicators/status) + the service's unit template. Mirrors update_claude1.sh
    (rsync .py + shared code); a superset of bash's single-.py targeting (lands the same code)."""
    ok = True
    for local, sub, ex in _BASE_SYNC:
        ok = ok and host.rsync(os.path.join(REPO, local), f"{install_dir}/{sub}", ex) == 0
    for local, sub, ex in spec.get("extra", []):
        ok = ok and host.rsync(os.path.join(REPO, local), f"{install_dir}/{sub}", ex) == 0
    ok = ok and host.rsync(
        os.path.join(REPO, spec["tdir"], spec["template"]), f"{install_dir}/{spec['template']}", []) == 0
    return ok


def deploy_infra(host: Host, service: str, *, install_dir: str = "~/tradinebotte",
                 verify_only: bool = False, test_ports: bool = False,
                 skip_restart: bool = False) -> int:
    spec = INFRA[service]
    unit = spec["unit"].replace("{account}", host.user)   # account_bot unit is per-user
    data_dir = spec["data_dir"]
    label = f"{host.user}/{service}"
    if not verify_only:
        print(_c("y", f"▶ {label}: sync"))
        assert act_sync_infra(host, install_dir, spec), "sync failed"
        print(_c("y", f"▶ {label}: deps+tradinetools"))   # tradinetools BEFORE restart (cex_feed imports PORT_CEX_FEED)
        assert act_deps_and_tradinetools(host, install_dir), "tradinetools import failed"
        # test_ports: write/clear the +10 offset drop-in BEFORE restart so the new bind is picked up
        env = _test_env(spec) if test_ports else {}
        if test_ports:
            print(_c("y", f"▶ {label}: test-ports {env or '(IPC — none)'}"))
        assert act_test_ports_dropin(host, unit, env), "test-ports drop-in failed"
        if skip_restart:
            print(_c("y", f"▶ {label}: skip-restart (code synced, unit NOT restarted)"))
        else:
            print(_c("y", f"▶ {label}: restart"))
            assert act_service_restart(host, install_dir, unit, spec["template"]), "restart failed"
            host.ssh("sleep 6")
    bot_id = spec.get("bot_name") or (act_read_bot_id(host, data_dir, spec["role"]) if spec["role"] else "") \
        or f"infra-{service}-unknown"
    ok, detail = act_verify(host, unit, f"{data_dir}/{spec['log']}")
    mark = _c("g", "✓") if ok else _c("r", "✗")
    print(f"{mark} {label}  bot_id={bot_id}  [{detail}]")
    record_deploy(host.user, bot_id, ok)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Native declarative deployer (grid/swing/accum/poly + infra).")
    ap.add_argument("target", choices=list(FAMILIES) + list(INFRA),
                    help="trading family (grid/swing/accumulation/polymarket) or infra service")
    ap.add_argument("--idx", type=int, required=True, help="account index in TEST_USERS")
    ap.add_argument("--strategy", default="", help="strategy JSON (write families; ignored for polymarket merge)")
    ap.add_argument("--dir", default="~/tradinebotte", help="install (code) dir")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--skip-restart", action="store_true",
                    help="sync code/config (+ drop-ins) but do NOT restart the unit — the bot keeps "
                         "running as-is (parity with the bash deployers' --skip-restart, so the "
                         "pipeline can forward it uniformly to native steps)")
    ap.add_argument("--test-ports", action="store_true",
                    help="offset every TCP bind by +10 for a self-contained test-account stack "
                         "(auto-on for the test-account idx; refused elsewhere)")
    ap.add_argument("--single-tree", action="store_true",
                    help="single-tree layout (families only): all bots' data lives in one "
                         "~/tradinebotte, per-instance suffixed (config_<role>.json / live_<role>.db / "
                         "<role>.log) via a TRADINEBOTTE_DIR/INSTANCE drop-in; opt-in, so prod native "
                         "deploys are unaffected")
    ap.add_argument("--migrate", action="store_true",
                    help="P4 cutover (implies --single-tree): before deploying, stop the unit and COPY "
                         "the bot's state (DB→live_<role>.db + bot_id_<role>) from its legacy separate "
                         "data dir into ~/tradinebotte, idempotently. Old dir kept for revert.")
    a = ap.parse_args()
    conf = load_conf()
    if a.idx >= len(conf["users"]):
        print(f"idx {a.idx} out of range ({len(conf['users'])} users)", file=sys.stderr); return 2
    # Fail-closed: --test-ports auto-implied for the ephemeral test account, and REFUSED on a prod
    # idx (offsetting a prod bot onto wrong ports would break the fleet). Forgetting the flag on
    # test-account → prod-port collision, so implying it is the safe default.
    sidx = conf.get("standalone_idx")
    if a.test_ports and sidx is not None and a.idx != sidx:
        print(f"--test-ports refused on idx {a.idx} (not the test-account idx {sidx}); would offset a "
              f"prod bot onto wrong ports", file=sys.stderr); return 2
    test_ports = a.test_ports or (sidx is not None and a.idx == sidx)
    host = Host(conf["users"][a.idx], conf["passwords"][a.idx], conf["server"], conf["port"])
    print(_c("y", f"native {a.target} deploy → {host.user} (idx {a.idx})"
             + ("  [test-ports +10]" if test_ports else "")))
    if a.target in INFRA:
        # infra is single-instance per account (one feed/cexfeed/indicators) → no cohabitation, no
        # single-tree needed; it already lives in ~/tradinebotte. Flag applies to families only.
        if a.single_tree:
            print("--single-tree applies to families only (infra is single-instance)", file=sys.stderr)
            return 2
        return deploy_infra(host, a.target, install_dir=a.dir, verify_only=a.verify_only,
                            test_ports=test_ports, skip_restart=a.skip_restart)
    if FAMILIES[a.target]["config_mode"] == "write" and not a.strategy:
        print(f"--strategy is required for family {a.target!r}", file=sys.stderr); return 2
    single_tree = a.single_tree or a.migrate   # --migrate implies single-tree
    return deploy_family(host, a.target, install_dir=a.dir, strategy=a.strategy,
                         verify_only=a.verify_only, test_ports=test_ports,
                         single_tree=single_tree, migrate=a.migrate,
                         skip_restart=a.skip_restart)


if __name__ == "__main__":
    raise SystemExit(main())
