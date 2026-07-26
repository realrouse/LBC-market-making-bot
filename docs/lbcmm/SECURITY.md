# Security checklist — LBC-market-making-bot operators

Install first: `bash install-lbcmm.sh` then `./bin/lbcmm gui` (see QUICKSTART-LBCMM.md).

- [ ] Use **paper mode** until you understand inventory risk  
- [ ] Create keys only at https://www.mexc.com/user/openapi  
- [ ] MEXC Spot permissions: **View Order Details** + **Trade** only  
      (optional: View Account Details; never Withdraw)  
- [ ] Prefer IP restriction on the key  
- [ ] Store secrets in env or `~/.config/lbcmm/config.toml` mode `600`  
- [ ] Never commit keys; never paste keys into Discord  
- [ ] On stop, confirm open orders cancelled (`python3 -m lbcmm cancel`)  
- [ ] Do not run untrusted strategy JSON from strangers without review  
- [ ] Keep GPL attribution; do not strip LICENSE  

## Threat notes

- The local GUI binds to `127.0.0.1` by default — do not expose to the public internet without auth.  
- Live mode requires `live_confirmed=true` in config after typing `LIVE` in setup.  
