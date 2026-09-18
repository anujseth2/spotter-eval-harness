#!/usr/bin/env python3
"""Re-run the still-failing infra questions via the streaming endpoint (UI transport), serially."""
import sys, os, json, time
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import ts_client
from stream_client import stream_answer

ROOT = os.path.join(HERE, "..")
env = ts_client.load_env(); host = env["TS_CLUSTER"].rstrip("/")

todo = json.load(open(os.path.join(ROOT, "results/_infra_todo.json")))
serial = json.load(open(os.path.join(ROOT, "results/rerun_infra.json")))
recovered = {str(x['id']) for x in serial if x['produced_answer']}
left = [q for q in todo if str(q['id']) not in recovered]
print(f"streaming re-run for {len(left)} leftovers (serial):", [str(q['id']).replace('.0','') for q in left], flush=True)

def mint():
    return ts_client.mint_token(env, 1800)
tok = mint(); minted = time.time()
out = []
for i, q in enumerate(left, 1):
    if time.time() - minted > 1500:
        tok = mint(); minted = time.time()
    t0 = time.time(); res = None
    for attempt in range(1, 3):
        r = stream_answer(host, tok, q["question"], timeout=300)
        if r["produced_answer"] or not r.get("error"):
            res = r; break
        res = r; time.sleep(3)
    rec = {"id": q["id"], "variant": q["variant"], "question": q["question"],
           "expected_source": q.get("expected_source"), "bucket": q.get("bucket"),
           "primary_eval_metrics": q.get("primary_eval_metrics", []),
           "elapsed_s": round(time.time() - t0, 1), "attempts": attempt, "transport": "stream", **res}
    out.append(rec)
    flag = "OK " if rec["produced_answer"] else ("ERR" if rec["error"] else "NOANS")
    print(f"[{i}/{len(left)}] {flag} id={str(rec['id']).replace('.0','')}/{rec['variant']} {rec['elapsed_s']}s :: {str(rec['tml_tokens'])[:70]}", flush=True)
    json.dump(out, open(os.path.join(ROOT, "results/rerun_stream.json"), "w"), indent=2)
ok = sum(1 for r in out if r["produced_answer"])
print(f"\nstreaming re-run complete: {ok}/{len(out)} recovered", flush=True)
