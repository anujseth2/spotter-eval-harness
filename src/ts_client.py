#!/usr/bin/env python3
"""Thin ThoughtSpot REST client for the Spotter accuracy eval.

Auth: trusted-auth full token  (POST /api/rest/2.0/auth/token/full)
Spotter engine: single answer   (POST /api/rest/2.0/ai/answer/create)
Reads config from ../.env (never from argv, so the secret is never on a command line).
"""
import json, os, sys, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, "..", ".env")


def load_env(path=ENV_PATH):
    env = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    # The .env file is authoritative for this project. We deliberately do NOT
    # let the OS environment override it: a stale exported TS_SECRET_KEY (e.g. a
    # different cluster's secret in the shell profile) would silently shadow the
    # file and cause misleading auth failures.
    return env


def _post(url, body, headers, timeout=120):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}


def mint_token(env, validity_sec=1800):
    cluster = env["TS_CLUSTER"].rstrip("/")
    body = {
        "username": env["TS_TRUSTED_AUTH_USERNAME"],
        "secret_key": env["TS_SECRET_KEY"],
        "validity_time_in_sec": validity_sec,
    }
    if env.get("TS_ORG"):
        try:
            body["org_id"] = int(env["TS_ORG"])
        except ValueError:
            pass  # let secret_key's default org apply
    status, resp = _post(cluster + "/api/rest/2.0/auth/token/full", body, {})
    if status != 200 or "token" not in resp:
        raise SystemExit(f"AUTH FAILED (HTTP {status}): {json.dumps(resp)[:600]}")
    return resp["token"]


def spotter_answer(env, token, query, model_guid, timeout=120):
    cluster = env["TS_CLUSTER"].rstrip("/")
    body = {"query": query, "metadata_identifier": model_guid}
    return _post(cluster + "/api/rest/2.0/ai/answer/create", body,
                 {"Authorization": "Bearer " + token}, timeout=timeout)


if __name__ == "__main__":
    env = load_env()
    missing = [k for k in ("TS_CLUSTER", "TS_TRUSTED_AUTH_USERNAME", "TS_SECRET_KEY") if not env.get(k)]
    if missing:
        raise SystemExit("Missing in .env: " + ", ".join(missing))
    print("Cluster:", env["TS_CLUSTER"])
    print("User   :", env["TS_TRUSTED_AUTH_USERNAME"])
    print("Minting trusted-auth token...")
    tok = mint_token(env)
    print("OK. Token length:", len(tok), "| first 12:", tok[:12] + "...")
