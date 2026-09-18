#!/usr/bin/env python3
"""Run questions through Spotter in AUTO_MODE and capture the resolved query.

Production-faithful: AUTO_MODE (agent auto-selects the model), one FRESH
conversation per question so each is measured independently (no multi-turn
context bleed). Captures per question:
  - chosen worksheet_id (did auto-mode route to the right model)
  - tml_tokens + sage_query (the resolved analytical tokens -> what we grade)
  - chart_type, answer title, final narrative text
  - any error / whether an answer was produced at all

Usage:
  python3 src/run_batch.py --ids 1,2,3            # specific question ids
  python3 src/run_batch.py --sample 10            # first N
  python3 src/run_batch.py --all --out results/full.json
  python3 src/run_batch.py --ids-file data/calibration_ids.txt
"""
import sys, os, json, time, argparse, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ts_client

ROOT = os.path.join(HERE, "..")


class Session:
    """Holds a bearer token, re-minting when close to expiry."""
    def __init__(self, env, validity=3600):
        self.env = env
        self.validity = validity
        self._tok = None
        self._exp = 0

    def token(self):
        now = time.time()
        if not self._tok or now > self._exp - 120:
            self._tok = ts_client.mint_token(self.env, self.validity)
            self._exp = now + self.validity
        return self._tok


def _post(host, path, body, token, timeout=200):
    req = urllib.request.Request(host + path, data=json.dumps(body).encode(), method="POST")
    for k, v in {"Content-Type": "application/json", "Accept": "application/json",
                 "Authorization": "Bearer " + token}.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode() or "{}")


def _looks_like_json_dump(txt):
    t = txt.strip()
    return t.startswith("```") or t.startswith("{") or '"search_mode"' in t or '"rules"' in t


TRANSIENT = ("504", "gateway timeout", "unknown error", "timed out", "timeout", "502", "503")


def _is_transient(err):
    e = (err or "").lower()
    return any(s in e for s in TRANSIENT)


def _attempt(sess, host, question, model_ctx):
    """One create+send round-trip. Returns (distilled_partial, error_or_None)."""
    tok = sess.token()
    s, c = _post(host, "/api/rest/2.0/ai/agent/conversation/create",
                 {"metadata_context": model_ctx, "conversation_settings": {}}, tok)
    conv = c.get("conversation_identifier")
    if not conv:
        return {}, f"create failed HTTP {s}: {str(c)[:200]}"
    s, r = _post(host, f"/api/rest/2.0/ai/agent/conversation/{conv}/send", {"messages": [question]}, tok)
    msgs = r.get("messages", r) if isinstance(r, dict) else r
    answers, narratives, err = [], [], None
    for m in msgs if isinstance(msgs, list) else []:
        t = m.get("type")
        if t == "answer":
            answers.append(m)
        elif t == "text":
            txt = m.get("text", "")
            if txt and not _looks_like_json_dump(txt):
                narratives.append(txt.strip())
        elif t == "error":
            err = str(m)[:300]
    part = {"narrative": narratives[-1] if narratives else None}
    if answers:
        a = answers[-1]
        md = a.get("metadata", {})
        part.update(produced_answer=True, chosen_worksheet_id=md.get("worksheet_id"),
                    tml_tokens=a.get("tml_tokens"), sage_query=a.get("sage_query"),
                    chart_type=md.get("chart_type"), answer_title=a.get("title"))
    return part, err


def run_one(sess, host, q, model_ctx, max_retries=2):
    """Run a single question with retry on transient (504/timeout) failures."""
    out = {"id": q["id"], "variant": q.get("variant", "cold"),
           "question": q["question"], "expected_source": q.get("expected_source"),
           "bucket": q.get("bucket"), "primary_eval_metrics": q.get("primary_eval_metrics", []),
           "chosen_worksheet_id": None, "tml_tokens": None, "sage_query": None,
           "chart_type": None, "answer_title": None, "narrative": None,
           "produced_answer": False, "error": None, "attempts": 0, "elapsed_s": None}
    t0 = time.time()
    for attempt in range(1, max_retries + 2):
        out["attempts"] = attempt
        err = None
        try:
            part, err = _attempt(sess, host, q["question"], model_ctx)
            out.update({k: v for k, v in part.items() if v is not None})
            if part.get("produced_answer") or (err and not _is_transient(err)):
                out["error"] = err
                break
            if not err and not part.get("produced_answer"):
                # clean no-answer (e.g. clarification request) — not transient, keep it
                out["error"] = None
                break
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}: {e.read().decode()[:150]}"
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        out["error"] = err
        if err and _is_transient(err) and attempt <= max_retries:
            time.sleep(3 * attempt)  # backoff before retry
            continue
        break
    out["elapsed_s"] = round(time.time() - t0, 1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids"); ap.add_argument("--ids-file"); ap.add_argument("--sample", type=int)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", default="results/run.json")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--model", default="AUTO", help="AUTO or a worksheet GUID to pin")
    args = ap.parse_args()

    env = ts_client.load_env()
    host = env["TS_CLUSTER"].rstrip("/")
    questions = json.load(open(os.path.join(ROOT, "data/questions.json")))
    qById = {str(q["id"]): q for q in questions}

    if args.ids:
        want = [s.strip() for s in args.ids.split(",")]
        sel = [qById[i] for i in want if i in qById]
    elif args.ids_file:
        want = [l.strip() for l in open(os.path.join(ROOT, args.ids_file)) if l.strip()]
        sel = [qById[i] for i in want if i in qById]
    elif args.sample:
        sel = questions[:args.sample]
    elif args.all:
        sel = questions
    else:
        raise SystemExit("specify --ids / --ids-file / --sample / --all")

    model_ctx = {"type": "AUTO_MODE"} if args.model == "AUTO" else \
                {"type": "DATA_SOURCE", "data_source_context": {"data_source_identifier": args.model}}
    print(f"Running {len(sel)} questions | model={args.model} | workers={args.workers}")

    sess = Session(env)
    sess.token()  # warm + fail fast on auth
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run_one, sess, host, q, model_ctx): q for q in sel}
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result(); results.append(r)
            flag = "OK " if r["produced_answer"] else ("ERR" if r["error"] else "NOANS")
            print(f"  [{i}/{len(sel)}] {flag} id={r['id']} {r['elapsed_s']}s :: {str(r['tml_tokens'])[:90]}")

    results.sort(key=lambda r: (len(str(r["id"])), str(r["id"])))
    outp = os.path.join(ROOT, args.out)
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    json.dump(results, open(outp, "w"), indent=2)
    ok = sum(1 for r in results if r["produced_answer"])
    print(f"\nWrote {outp} | produced_answer: {ok}/{len(results)}")


if __name__ == "__main__":
    main()
