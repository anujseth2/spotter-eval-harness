# Spotter accuracy eval harness

A small, dependency-light harness for answering the question customers actually ask before
they launch ThoughtSpot Spotter to end users: **out of a real list of questions our people
would type, how many does Spotter get right?**

You give it a list of natural-language questions plus a domain expert's answer key. It runs
every question through Spotter's conversation API in AUTO_MODE, captures what Spotter
*resolved* rather than just what it said, grades each one semantically with an LLM judge,
and produces a reviewable spreadsheet plus a headline accuracy number you can put in front
of a customer.

## Why grade the resolved query and not the prose

Spotter's narrative text will often sound right when the underlying query is wrong, and will
often sound unfamiliar when the query is exactly right. So the harness captures, per question:

* the analytical tokens Spotter resolved (measures, attributes, filters, time grain)
* the underlying sage query
* which model AUTO_MODE picked, which tells you whether routing works without the user
  knowing which dataset holds which metric
* the chart type and the final narrative

The judge grades intent against the answer key, not string equality, because a well-curated
model answers correctly through derived measures whose names will never match an expected
metric list literally.

## Verdicts

| Verdict | Meaning |
|---|---|
| Correct | The resolved query genuinely answers the question |
| Partial | Right direction, but misses or oversimplifies an aspect |
| Wrong | Misinterprets the question or resolves unrelated tokens |
| Clarification | No analytical answer, but it appropriately asked for a missing specific. This is correct behaviour on an under-specified prompt, not a failure |
| Infra | Transient gateway or tool error after retries. Excluded from the accuracy denominator and reported separately |
| JudgeError | The grader itself failed: rejected API key, rate limit, truncated response. This says nothing about Spotter, so it is excluded from the denominator and reported loudly |

Headline accuracy is Correct only. The harness also reports a weighted score that gives
Partial half credit, and a "usable" number that adds appropriate clarifications.

## Setup

```bash
git clone https://github.com/anujseth2/spotter-eval-harness.git
cd spotter-eval-harness
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`. It needs a cluster URL, a trusted-auth username and secret key from
Develop then Customizations then Security Settings, and an Anthropic API key for the judge.
Append the secret rather than pasting it into an editor session that might get shared:

```bash
printf 'TS_SECRET_KEY=%s\n' "$YOUR_SECRET" >> .env
```

Then check auth before you spend a long run on it:

```bash
python3 src/ts_client.py
```

## Your question list

Copy the sample and replace it with the customer's real questions:

```bash
cp data/sample_questions.json data/questions.json
```

Each entry looks like this. Only `id` and `question` are strictly required, but the judge is
much better with the answer key fields filled in:

```json
{
  "id": "1.0",
  "question": "Show me the stores whose gross margin improved the most in the last 6 months",
  "expected_source": "Sales",
  "expected_answer": "Stores ranked by greatest increase in gross margin percentage over the trailing 6 months.",
  "bucket": "Profitability",
  "primary_eval_metrics": ["Gross Margin", "Gross Margin %", "Net Sales"]
}
```

`bucket` is how you group questions into themes. It drives the per-theme accuracy breakdown,
which is usually the most actionable output, because gaps cluster by theme rather than
scattering randomly.

`data/questions.json` is gitignored. Customer question lists are their intellectual property
and should not end up in this repo.

## Running

Start with a calibration pass on a handful of questions so you can sanity-check the judge
before committing to the full run:

```bash
cp data/sample_calibration_ids.txt data/calibration_ids.txt
python3 src/run_batch.py --ids-file data/calibration_ids.txt --out results/calibration.json
```

Then the full run. Keep workers low, because heavy questions are exactly the ones that time
out under concurrency:

```bash
python3 src/run_full.py --workers 3 --out results/full_run.json
python3 src/grade.py  --in results/full_run.json --out results/graded.json
python3 src/report.py --in results/graded.json
python3 src/build_xlsx.py
```

That leaves you a workbook in `results/` with a Summary sheet of live formulas, a Results
sheet with one row per question, and a Gaps sheet listing every Partial and Wrong grouped by
bucket, which is effectively the customer's improvement roadmap.

## Entity-context questions

Real question lists are full of things like "compare this customer's spend to the segment
average". Run cold, Spotter correctly asks which customer, because in the live product the
embedding page supplies that selection. Scoring those as failures understates accuracy badly.

Set `SUBSTITUTE_ENTITY` in `.env` to a real value from the customer's data and those
questions run twice, once cold and once with the entity substituted in. The substituted run
scores analytical accuracy and the cold run is reported separately as a UX finding.

## When questions time out

Heavy questions, especially correlations, can exceed the gateway cap on the synchronous
endpoint, and concurrency makes it worse. Those come back as `Infra` and are excluded from
accuracy rather than counted as comprehension failures.

Recover them before you report, because most of them are not real failures:

```bash
python3 src/rerun_infra.py    # serial, more retries, same synchronous transport
python3 src/rerun_stream.py   # the SSE transport the Spotter UI itself uses
```

`src/stream_client.py` is the streaming client. If the synchronous endpoint is giving you
timeouts on a specific cluster, it is worth running the whole eval through it.

## Files

| File | What it does |
|---|---|
| `src/ts_client.py` | Trusted-auth token minting and the single-answer endpoint. Run it directly to test auth |
| `src/stream_client.py` | Streaming SSE client, the transport the Spotter UI uses |
| `src/run_batch.py` | Runs a selected set of questions, one fresh conversation each, with retry on transient failures |
| `src/run_full.py` | Full run including the entity-substitution second pass |
| `src/grade.py` | LLM-as-judge grading against the answer key |
| `src/report.py` | Console summary plus `results/summary.json` |
| `src/build_xlsx.py` | The reviewable workbook |
| `src/rerun_infra.py` | Serial re-run of timed-out questions |
| `src/rerun_stream.py` | Streaming re-run of whatever is still failing |
| `src/auth_diag.py` | Tries username variants when trusted auth returns a misleading error |

## Notes and gotchas

Every question runs in its own fresh conversation, so there is no multi-turn context bleed
and each question is measured independently. That is deliberately harsher than the real
product experience, where a user can follow up.

AUTO_MODE is used on purpose, because it reflects what an end user gets. If you pin a model
with `--model <guid>` in `run_batch.py` you are measuring something easier than production.

The judge is an LLM and will occasionally be wrong. Calibrate on a small set first, read the
rationales, and adjust the answer key if the judge is penalising a genuinely correct answer.
The rationale and gap columns are in the workbook so a human can overrule any row.

A grader failure is never scored as a Spotter failure. `grade.py` probes the judge on one
question before it starts, so a rejected API key stops the run in seconds instead of after a
full pass, and anything that fails mid-run becomes `JudgeError` rather than `Wrong`. That
distinction matters: treating a 401 as a wrong answer silently understates the customer's
accuracy, which is the one direction an error must never fall.

`.env` is read as authoritative and the OS environment deliberately does not override it. A
stale exported `TS_SECRET_KEY` in your shell profile is a classic way to get a confusing auth
failure against the wrong cluster.

## Licence

MIT.
