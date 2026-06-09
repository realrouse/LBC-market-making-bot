# Multi-host ZMQ authentication — future options

Current transport (implemented): **IPC per-UID** (`ipc://`) — all services run under the
same OS user; the kernel enforces isolation via `/run/user/$UID/` (mode 0700, set by
systemd-logind).  No authentication layer is needed for single-host single-user deployments.

For multi-host deployments (feed or indicators on a different machine from account bots),
IPC is unavailable and the connection must cross the network.  Two authentication options
exist; neither is implemented yet.

---

## Option B — ZMQ CURVE (asymmetric Curve25519)

**Security model:** mutual public-key authentication + encryption.  Neither side can be
impersonated without the private key; traffic is encrypted end-to-end.

**Threat model addressed:** active MITM, passive eavesdropping, unauthorized subscription.

### How it works

1. Generate a server keypair (feed / indicators): `zmq.curve_keypair()` → `(pubkey, secretkey)`.
2. Generate a client keypair per account bot.
3. On the PUB/REP side (`make_pub`, `make_rep`): set `CURVE_SERVER=1`, `CURVE_SECRETKEY`.
4. On the SUB/REQ side (`make_sub`, `make_req`): set `CURVE_SERVERKEY` (server public),
   `CURVE_PUBLICKEY` (client public), `CURVE_SECRETKEY` (client private).
5. Optionally add a ZAP handler to restrict which client public keys are allowed.

### Changes required

| File | Change |
|---|---|
| `tradinetools/zmq.py` | Add `make_pub_curve`, `make_sub_curve` (or extend factories with optional `curve_keys` arg) |
| `tradinetools/zmq.py` | Add `make_rep_curve`, `make_req_curve` |
| `feed.py`, `indicators.py` | Load server keypair from `credentials` or env |
| `account_bot.py`, `live_bot.py` | Load client keypair; pass server pubkey |
| `accumulation_bot.py`, `orderbook_bot.py`, `swing.py` | Same |
| `scripts/setup.py` | Add keypair generation step |
| `scripts/install_*.sh` | Deploy keypairs to correct paths |
| Config / env vars | `TRADINEBOTTE_ZMQ_CURVE_ENABLED=1`, key file paths |
| Unit files | Pass key paths via `EnvironmentFile` |

### Key management notes

- Server keypairs: one per service (feed, indicators PUB, indicators REP).
- Client keypairs: one per account bot instance (or shared — weaker but simpler).
- Store private keys in `credentials` file (chmod 600), never in code or CHANGELOG.
- Key rotation requires coordinated restart of all connected services.

### Backward compat

When `TRADINEBOTTE_ZMQ_CURVE_ENABLED` is unset/false, factories fall back to the current
IPC or plain TCP path — no breakage for single-host deployments.

---

## Option C — PLAIN auth + ZAP handler (username/password over loopback)

**Security model:** username/password authentication only; no encryption.
Suitable only when all sockets remain on loopback (`tcp://127.0.0.1`) and the threat is
unauthorized local process subscription (e.g. untrusted code running as a different user on
the same host).

**Not suitable for multi-host** — credentials cross the wire in cleartext.

### How it works

1. On the PUB/REP side: set `PLAIN_SERVER=1`.
2. On the SUB/REQ side: set `PLAIN_USERNAME`, `PLAIN_PASSWORD`.
3. Run a ZAP authenticator in a background thread (`zmq.auth.Authenticator`) that validates
   credentials against a store (file, env, or in-memory dict).

### Changes required

| File | Change |
|---|---|
| `tradinetools/zmq.py` | Add `start_zap_authenticator(ctx, credentials)` helper |
| `tradinetools/zmq.py` | Add optional `plain_creds` param to `make_pub`, `make_rep` |
| `feed.py`, `indicators.py` | Start ZAP thread; load credentials from env/file |
| `account_bot.py`, `live_bot.py`, CEX bots | Pass `PLAIN_USERNAME/PASSWORD` env vars |
| Config / env vars | `TRADINEBOTTE_ZMQ_PLAIN_USER`, `TRADINEBOTTE_ZMQ_PLAIN_PASS` |

### Limitations

- Credentials in env vars are visible to `ps` and `/proc/$PID/environ`.
- Provides no confidentiality — only identity check.
- For anything beyond localhost, use Option B (CURVE) instead.

---

## Decision guide

| Scenario | Recommended option |
|---|---|
| All services on same host, same user (current default) | IPC — already implemented |
| All services on same host, different users | Option C (PLAIN+ZAP) over TCP loopback |
| Services on different hosts | Option B (CURVE) — mandatory for confidentiality |
| Services on different hosts in a trusted VPN/wireguard | Option B still preferred |
