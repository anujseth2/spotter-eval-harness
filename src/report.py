#!/usr/bin/env python3
"""Aggregate graded results into an accuracy/confidence report.

Accuracy denominator rules (user-approved):
  - Infra verdicts are EXCLUDED from the accuracy denominator (reported separately).
  - JudgeError verdicts (the grader itself failed) are likewise EXCLUDED. They say
    nothing about Spotter, so counting them would understate the customer's accuracy.
  - For entity-context questions ("this athlete", "for him"), the SUBSTITUTED variant
    feeds accuracy; the cold variant is reported separately as a UX finding.
  - All other questions use their single cold run.
Correct + Partial(0.5) is the weighted score; headline accuracy is Correct-only.
"""
import sys, os, json
from collections import defaultdict, Counter

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def pct(n, d):
    return f"{(100.0*n/d):.1f}%" if d else "n/a"


def main():
    inp = os.path.join(ROOT, sys.argv[sys.argv.index("--in") + 1]) if "--in" in sys.argv else os.path.join(ROOT, "results/graded.json")
    graded = json.load(open(inp))

    by_id = defaultdict(dict)          # id -> {variant: run}
    for g in graded:
        by_id[str(g["id"])][g.get("variant", "cold")] = g

    # Build the scored set: one representative per question
    scored, ux_context = [], []
    for qid, variants in by_id.items():
        if "substituted" in variants:            # entity-context question
            scored.append(variants["substituted"])
            if "cold" in variants:
                ux_context.append(variants["cold"])
        else:
            scored.append(variants["cold"])

    infra = [r for r in scored if r.get("verdict") == "Infra"]
    judge_err = [r for r in scored if r.get("verdict") == "JudgeError"]
    graded_set = [r for r in scored if r.get("verdict") not in ("Infra", "JudgeError")]
    n = len(graded_set)
    cnt = Counter(r.get("verdict") for r in graded_set)
    correct, partial, wrong, clar = cnt["Correct"], cnt["Partial"], cnt["Wrong"], cnt["Clarification"]

    # routing over answered runs
    answered = [r for r in graded_set if r.get("produced_answer")]
    routing_ok = sum(1 for r in answered if r.get("routing_ok"))

    print("=" * 70)
    print(os.environ.get("EVAL_TITLE", "SPOTTER ACCURACY") + " — SUMMARY")
    print("=" * 70)
    print(f"Questions scored: {n}    Infra-excluded: {len(infra)}    Ungraded (JudgeError): {len(judge_err)}")
    print(f"  Correct       {correct:3d}  {pct(correct,n)}")
    print(f"  Partial       {partial:3d}  {pct(partial,n)}")
    print(f"  Clarification {clar:3d}  {pct(clar,n)}   (appropriate ask; context-dependent Qs)")
    print(f"  Wrong         {wrong:3d}  {pct(wrong,n)}")
    print(f"\n  Headline accuracy (Correct only):        {pct(correct,n)}")
    print(f"  Weighted (Correct + 0.5*Partial):        {pct(correct+0.5*partial,n)}")
    print(f"  Usable (Correct + Partial + Clarify):    {pct(correct+partial+clar,n)}")
    print(f"  Auto-mode routing correct:               {pct(routing_ok,len(answered))} of {len(answered)} answered")

    # by source type
    print("\n--- by expected source ---")
    src = defaultdict(Counter)
    for r in graded_set:
        src[r.get("expected_source")][r.get("verdict")] += 1
    for s, c in sorted(src.items(), key=lambda x: -sum(x[1].values())):
        tot = sum(c.values())
        print(f"  {s:22s} n={tot:3d}  correct {pct(c['Correct'],tot):>6s}  partial {c['Partial']}  wrong {c['Wrong']}  clar {c['Clarification']}")

    # by bucket
    print("\n--- by bucket (headline accuracy) ---")
    buck = defaultdict(Counter)
    for r in graded_set:
        buck[r.get("bucket")][r.get("verdict")] += 1
    for b, c in sorted(buck.items(), key=lambda x: (x[1]['Correct']/max(1,sum(x[1].values())))):
        tot = sum(c.values())
        print(f"  {b:34s} n={tot:2d}  {pct(c['Correct'],tot):>6s}  (P{c['Partial']} W{c['Wrong']} C{c['Clarification']})")

    # entity-context UX finding
    if ux_context:
        asked = sum(1 for r in ux_context if r.get("verdict") == "Clarification" or not r.get("produced_answer"))
        print(f"\n--- entity-context questions (n={len(ux_context)}) ---")
        print(f"  Run cold, {asked} asked for the entity / needed a selected-entity context.")
        print(f"  These need the embedding app to supply the entity filter; scored via substitution.")

    # judge failures: loud, because a number reported over these is wrong
    if judge_err:
        print(f"\n*** {len(judge_err)} question(s) COULD NOT BE GRADED (grader failure). ***")
        print("    The accuracy above is computed WITHOUT them. Re-run grading before")
        print("    reporting these numbers to a customer.")
        for r in judge_err:
            print(f"      id={r['id']}: {str(r.get('rationale'))[:110]}")

    # infra detail
    if infra:
        print(f"\n--- infra failures excluded (n={len(infra)}) ---")
        for r in infra:
            print(f"  id={r['id']}: {r['question'][:60]}")

    # wrong / partial for review
    print("\n--- WRONG (review) ---")
    for r in graded_set:
        if r.get("verdict") == "Wrong":
            print(f"  id={r['id']} [{r.get('expected_source')}] {r['question'][:55]}\n      -> {r.get('rationale')}")
    print("\n--- PARTIAL (review) ---")
    for r in graded_set:
        if r.get("verdict") == "Partial":
            print(f"  id={r['id']} {r['question'][:55]}\n      -> {r.get('gap')}")

    # dump a machine-readable summary
    summary = {
        "n_scored": n, "correct": correct, "partial": partial, "wrong": wrong,
        "clarification": clar, "infra_excluded": len(infra),
        "ungraded_judge_error": len(judge_err),
        "headline_accuracy": round(100.0*correct/n, 1) if n else None,
        "weighted_accuracy": round(100.0*(correct+0.5*partial)/n, 1) if n else None,
        "routing_accuracy": round(100.0*routing_ok/len(answered), 1) if answered else None,
    }
    json.dump(summary, open(os.path.join(ROOT, "results/summary.json"), "w"), indent=2)
    print("\nWrote results/summary.json")


if __name__ == "__main__":
    main()
