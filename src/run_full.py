#!/usr/bin/env python3
"""Full accuracy run over data/questions.json in AUTO_MODE.

- Every question runs cold, exactly as written.
- Questions that reference an unnamed entity ("this athlete", "for him") ALSO run
  with a real entity substituted in, so analytical accuracy is measured separately
  from the "needs a selected entity" UX behaviour. Set SUBSTITUTE_ENTITY in .env to
  a real value from your data to enable this; leave it unset to skip the second pass.
- Retry on transient 504/timeout; concurrency kept low to ease the gateway.
"""
import sys, os, re, json, time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ts_client
from run_batch import Session, run_one

ROOT = os.path.join(HERE, "..")

# Nouns that stand in for "the entity the user currently has selected in the app".
# Override with ENTITY_NOUNS in .env (comma-separated) for your domain.
DEFAULT_NOUNS = "athlete,player,individual,person,customer,account,store,rep,employee,patient"


def entity_patterns(env):
    """Returns (detector, noun_pattern). The detector finds entity-context questions;
    the noun pattern is what gets replaced with the real entity value."""
    nouns = [n.strip() for n in (env.get("ENTITY_NOUNS") or DEFAULT_NOUNS).split(",") if n.strip()]
    alt = "|".join(re.escape(n) for n in nouns)
    noun_pat = re.compile(rf"\bthis ({alt})\b", re.I)
    pronoun_pat = re.compile(r"\bfor (him|her|them)\b", re.I)
    detector = re.compile(rf"\bthis ({alt})\b|\bfor (him|her|them)\b", re.I)
    return detector, noun_pat, pronoun_pat


def substitute(qtext, noun_pat, pronoun_pat, entity):
    """"this athlete's baseline" -> "<entity>'s baseline"; "for him" -> "for <entity>"."""
    out = noun_pat.sub(entity, qtext)
    return pronoun_pat.sub(f"for {entity}", out)


def main():
    out_path = os.path.join(ROOT, sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv \
        else os.path.join(ROOT, "results/full_run.json")
    workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 3

    env = ts_client.load_env()
    host = env["TS_CLUSTER"].rstrip("/")
    questions = json.load(open(os.path.join(ROOT, "data/questions.json")))
    model_ctx = {"type": "AUTO_MODE"}

    entity = env.get("SUBSTITUTE_ENTITY", "").strip()
    detector, noun_pat, pronoun_pat = entity_patterns(env)

    items = []
    for q in questions:
        items.append({**q, "variant": "cold"})
        if entity and detector.search(q["question"]):
            sq = dict(q)
            sq["question"] = substitute(q["question"], noun_pat, pronoun_pat, entity)
            sq["variant"] = "substituted"
            items.append(sq)

    n_sub = sum(1 for it in items if it["variant"] == "substituted")
    if not entity:
        print("SUBSTITUTE_ENTITY not set in .env — entity-context questions run cold only.")
    print(f"Total runs: {len(items)} ({len(questions)} cold + {n_sub} entity-substituted) | workers={workers}")

    sess = Session(env)
    sess.token()
    results = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(run_one, sess, host, it, model_ctx): it for it in items}
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result(); results.append(r)
            flag = "OK " if r["produced_answer"] else ("ERR" if r["error"] else "NOANS")
            retry = f" (x{r['attempts']})" if r["attempts"] > 1 else ""
            print(f"  [{i}/{len(items)}] {flag}{retry} id={r['id']}/{r['variant']} {r['elapsed_s']}s :: {str(r['tml_tokens'])[:75]}")
            # checkpoint every 20 so a long run is never lost
            if i % 20 == 0:
                os.makedirs(os.path.dirname(out_path), exist_ok=True)
                json.dump(results, open(out_path, "w"), indent=2)

    def sort_key(r):
        s = str(r["id"]).rstrip("0").rstrip(".")
        return (float(s) if s.replace(".", "", 1).isdigit() else 0, r["variant"])

    results.sort(key=sort_key)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    json.dump(results, open(out_path, "w"), indent=2)
    ok = sum(1 for r in results if r["produced_answer"])
    noans = sum(1 for r in results if not r["produced_answer"] and not r["error"])
    err = sum(1 for r in results if r["error"])
    print(f"\nWrote {out_path} | {len(results)} runs in {round((time.time()-t0)/60,1)}min")
    print(f"  produced_answer={ok}  no-answer(clarify)={noans}  error={err}")


if __name__ == "__main__":
    main()
