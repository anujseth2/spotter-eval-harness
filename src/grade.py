#!/usr/bin/env python3
"""LLM-as-judge grader for the Spotter accuracy eval (5-way rubric, semantic).

Reads results/full_run.json, grades each run with Claude via structured output,
writes results/graded.json. Deterministic pre-classification handles infra
errors and clarifications before the judge sees the rest.

Rubric (user-approved):
  Correct      - resolved query answers the question
  Partial      - right direction, missing/oversimplifies an aspect
  Wrong        - misinterprets the question
  Clarification- appropriately asked for missing specifics (context-dependent Q)
  Infra        - 504/timeout/unknown error after retries (excluded from accuracy)
Also judged: routing_ok (did auto-mode pick a model containing the needed source).
"""
import sys, os, json, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ts_client

ROOT = os.path.join(HERE, "..")
MODEL = "claude-opus-5"
API = "https://api.anthropic.com/v1/messages"

VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["Correct", "Partial", "Wrong", "Clarification"]},
        "routing_ok": {"type": "boolean",
                       "description": "Did auto-mode route to a model whose data can answer this question?"},
        "rationale": {"type": "string", "description": "One or two sentences justifying the verdict."},
        "gap": {"type": "string", "description": "What is missing or wrong; empty string if Correct."},
    },
    "required": ["verdict", "routing_ok", "rationale", "gap"],
}

# Who the questions come from. Set EVAL_DOMAIN in .env, e.g. "retail merchandising managers"
# or "clinical operations analysts". The judge grades intent, so the domain framing matters.
DOMAIN = ts_client.load_env().get("EVAL_DOMAIN") or "business users"

SYSTEM = (
    "You are a strict but fair evaluator of a natural-language analytics assistant (ThoughtSpot Spotter) "
    f"used by {DOMAIN}. For each question you are given the user's question, the "
    "expected data source(s) and expected metrics from a domain-expert answer key, and what Spotter actually "
    "resolved: the analytical tokens (measures/attributes/filters/time grain), the underlying sage query, the "
    "model it auto-selected, and the narrative it returned.\n\n"
    "Grade SEMANTICALLY, not by string match. Spotter often answers correctly using curated/derived measures "
    "whose names will NOT literally match the expected metric names but which still correctly answer the intent. Reward a resolved query that captures the analytical "
    "intent (right measure family, right entity grouping, right filters/time window, right comparison).\n\n"
    "Verdicts: 'Correct' = the resolved query genuinely answers the question. 'Partial' = right direction but "
    "misses or oversimplifies a needed aspect (e.g. computes a related quantity but not the actual comparison/"
    "correlation asked for). 'Wrong' = misinterprets the question or resolves unrelated tokens. 'Clarification' "
    "= Spotter produced no analytical answer and instead appropriately asked for a missing specific (e.g. which "
    "entity or date range) or explained it lacked context — this is correct behavior for an under-specified prompt, not a failure.\n\n"
    "routing_ok: true if the auto-selected model plausibly contains the data the question needs; false if the "
    "question needs a source the chosen model lacks."
)


def load_key():
    env = ts_client.load_env()
    key = env.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise SystemExit("No ANTHROPIC_API_KEY in .env")
    return key


def judge(key, run):
    user = (
        f"QUESTION: {run['question']}\n"
        f"EXPECTED SOURCE(S): {run.get('expected_source')}\n"
        f"EXPECTED METRICS (answer key): {', '.join(run.get('primary_eval_metrics') or []) or 'n/a'}\n"
        f"BUCKET: {run.get('bucket')}\n\n"
        f"--- SPOTTER RESOLVED ---\n"
        f"produced_answer: {run.get('produced_answer')}\n"
        f"auto-selected model id: {run.get('chosen_worksheet_id')}\n"
        f"resolved tokens: {run.get('tml_tokens')}\n"
        f"sage query: {run.get('sage_query')}\n"
        f"narrative: {(run.get('narrative') or '')[:1200]}\n\n"
        "Grade this. If produced_answer is false and the narrative asks for a missing specific or explains "
        "missing context, use verdict 'Clarification'. Otherwise judge Correct/Partial/Wrong."
    )
    body = {
        "model": MODEL,
        "max_tokens": 1200,
        "system": SYSTEM,
        "output_config": {"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}, "effort": "medium"},
        "messages": [{"role": "user", "content": user}],
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(API, data=data, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", "2023-06-01")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.loads(r.read().decode())
            txt = next((b["text"] for b in resp.get("content", []) if b.get("type") == "text"), None)
            if not txt:
                return {"verdict": "Wrong", "routing_ok": False, "rationale": "no judge output", "gap": "judge returned no text"}
            return json.loads(txt)
        except urllib.error.HTTPError as e:
            code = e.code
            if code in (429, 500, 529, 503) and attempt < 3:
                time.sleep(3 * (attempt + 1)); continue
            return {"verdict": "Wrong", "routing_ok": False, "rationale": f"judge HTTP {code}", "gap": e.read().decode()[:150]}
        except Exception as e:
            if attempt < 3:
                time.sleep(2 * (attempt + 1)); continue
            return {"verdict": "Wrong", "routing_ok": False, "rationale": f"judge error {type(e).__name__}", "gap": str(e)[:150]}


TRANSIENT = ("504", "gateway timeout", "unknown error", "timed out", "timeout", "502", "503",
             "tool_error", "no response received from the tool", "toolexecutionerror")


def grade_run(key, run):
    out = dict(run)
    err = (run.get("error") or "").lower()
    if not run.get("produced_answer") and err and any(s in err for s in TRANSIENT):
        out.update(verdict="Infra", routing_ok=None, rationale="transient infra error after retries", gap=run.get("error", "")[:150])
        return out
    v = judge(key, run)
    out.update(verdict=v.get("verdict"), routing_ok=v.get("routing_ok"),
               rationale=v.get("rationale"), gap=v.get("gap"))
    return out


def main():
    inp = os.path.join(ROOT, sys.argv[sys.argv.index("--in") + 1]) if "--in" in sys.argv else os.path.join(ROOT, "results/full_run.json")
    outp = os.path.join(ROOT, sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else os.path.join(ROOT, "results/graded.json")
    workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 6
    key = load_key()
    runs = json.load(open(inp))
    print(f"Grading {len(runs)} runs with {MODEL} | workers={workers}")
    graded = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(grade_run, key, r): r for r in runs}
        for i, f in enumerate(as_completed(futs), 1):
            g = f.result(); graded.append(g)
            print(f"  [{i}/{len(runs)}] {g.get('verdict'):12s} id={g['id']}/{g.get('variant')}  {str(g.get('rationale'))[:70]}")
            if i % 25 == 0:
                json.dump(graded, open(outp, "w"), indent=2)
    json.dump(graded, open(outp, "w"), indent=2)
    print(f"\nWrote {outp}")


if __name__ == "__main__":
    main()
