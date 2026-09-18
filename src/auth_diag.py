#!/usr/bin/env python3
"""Diagnose trusted-auth: try the secret in .env against several username forms.
Prints only masked info + HTTP codes, never the secret."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ts_client

env = ts_client.load_env()
sec = env.get("TS_SECRET_KEY", "")
print(f"cluster        : {env.get('TS_CLUSTER')}")
print(f"secret in file : prefix={sec[:4]} len={len(sec)}")
print(f"org in file    : {env.get('TS_ORG') or '<none / defaults to secret org>'}")

# Same user with and without the email domain — trusted auth is picky about which
# form the cluster has on record, and a mismatch shows up as a misleading 10003.
_u = env.get("TS_TRUSTED_AUTH_USERNAME", "")
candidates = [_u, _u.split("@")[0], _u + "@" + env.get("TS_EMAIL_DOMAIN", "example.com")]
seen = set()
for u in candidates:
    if not u or u in seen:
        continue
    seen.add(u)
    e2 = dict(env); e2["TS_TRUSTED_AUTH_USERNAME"] = u
    try:
        tok = ts_client.mint_token(e2, validity_sec=300)
        print(f"  username={u:34s} -> OK  (token len {len(tok)})")
    except SystemExit as ex:
        msg = str(ex)
        code = "?"
        for c in ("10003", "10002", "10023", "10097", "403", "400", "401"):
            if c in msg:
                code = c; break
        # extract the debug phrase
        phrase = msg.split('"debug":')[-1][:80] if '"debug":' in msg else msg[:80]
        print(f"  username={u:34s} -> FAIL code~{code}  {phrase}")
