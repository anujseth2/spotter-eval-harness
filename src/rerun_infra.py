#!/usr/bin/env python3
"""Re-run the infra-timeout questions serially (workers=1) with extra retries."""
import sys, os, json, time
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import ts_client
from run_batch import Session, run_one

ROOT = os.path.join(HERE, "..")
env = ts_client.load_env(); host = env["TS_CLUSTER"].rstrip("/")
todo = json.load(open(os.path.join(ROOT, "results/_infra_todo.json")))
model_ctx = {"type": "AUTO_MODE"}
sess = Session(env); sess.token()
out = []
for i, q in enumerate(todo, 1):
    r = run_one(sess, host, q, model_ctx, max_retries=3)   # up to 4 attempts, serial
    flag = "OK " if r["produced_answer"] else ("ERR" if r["error"] else "NOANS")
    print(f"[{i}/{len(todo)}] {flag} (x{r['attempts']}) id={r['id']}/{r['variant']} {r['elapsed_s']}s :: {str(r['tml_tokens'])[:70]}", flush=True)
    out.append(r)
    json.dump(out, open(os.path.join(ROOT, "results/rerun_infra.json"), "w"), indent=2)
ok = sum(1 for r in out if r["produced_answer"])
print(f"\nRe-run complete: {ok}/{len(out)} now produced an answer")
